import pytest
import tempfile
import json
from pathlib import Path

from terx.dom.extractor import AXElement, _structural_hash, _build_role_sequence, hash_similarity
from terx.cache.cache import MuscleMemorycache, CDPCommand, session_for

def test_deterministic_ids():
    from terx.dom.extractor import DOMExtractor
    # Create nodes similar to CDP response
    nodes = [
        {"role": "textbox", "name": "Email", "nodeId": "1", "backendDOMNodeId": 101, "parentId": "parent1"},
        {"role": "textbox", "name": "Password", "nodeId": "2", "backendDOMNodeId": 102, "parentId": "parent1"},
        {"role": "button", "name": "Submit", "nodeId": "3", "backendDOMNodeId": 103, "parentId": "parent1"},
    ]
    extractor = DOMExtractor()
    elements = extractor._extract_interactable(nodes)
    
    assert len(elements) == 3
    # Check that IDs are deterministic and not just sequential counter starting from 1
    assert all(el.id < 100000 for el in elements)
    # Re-run should produce same IDs
    elements_again = extractor._extract_interactable(nodes)
    assert [el.id for el in elements] == [el.id for el in elements_again]


def test_hash_similarity_levenshtein():
    # Identical
    seq_a = "button:Submit:1|textbox:Email:1"
    seq_b = "button:Submit:1|textbox:Email:1"
    assert hash_similarity(seq_a, seq_b) == 1.0

    # Minor difference (e.g. dynamic text changed or role inserted/deleted)
    seq_c = "button:Submit:1|textbox:Email_v2:1"
    # Should have high similarity but not 1.0
    sim = hash_similarity(seq_a, seq_c)
    assert 0.5 <= sim < 1.0

    # Completely different
    seq_d = "link:Home:0|checkbox:Agree:2|slider:Volume:1"
    assert hash_similarity(seq_a, seq_d) < 0.2


def test_cache_with_task_uniqueness():
    with tempfile.TemporaryDirectory() as tmp:
        cache = MuscleMemorycache(db_path=f"{tmp}/test.db", vcr_dir=f"{tmp}/vcr")
        
        domain = "example.com"
        role_seq = "button:Login:1"
        struct_hash = "fakehash"
        
        commands_task1 = [CDPCommand("Input.click", {}, {}, 10.0)]
        commands_task2 = [CDPCommand("Input.type", {}, {}, 20.0)]
        
        # Store task 1
        cache.store(domain, struct_hash, role_seq, "login to app", commands_task1)
        # Store task 2 on SAME domain and SAME DOM structure
        cache.store(domain, struct_hash, role_seq, "reset password", commands_task2)
        
        # Lookups should be task-specific
        hit1 = cache.lookup(domain, role_seq, "login to app")
        assert hit1 is not None
        assert hit1.commands[0].method == "Input.click"
        
        hit2 = cache.lookup(domain, role_seq, "reset password")
        assert hit2 is not None
        assert hit2.commands[0].method == "Input.type"
        
        # Lookups for non-existent tasks on the same page should miss
        hit_none = cache.lookup(domain, role_seq, "random task")
        assert hit_none is None
