"""
TERX Core Tests — DOM extraction, cache operations, VCR writer, URL validation.
"""

import tempfile
import json

from terx.dom.extractor import (
    DOMExtractor,
    AXElement,
    hash_similarity,
    _structural_hash,
    _build_role_sequence,
)
from terx.cache.cache import MemoryCache, MuscleMemorycache, CDPCommand, _task_key


# ------------------------------------------------------------------ #
# DOM Extractor Tests                                                    #
# ------------------------------------------------------------------ #


def test_deterministic_ids():
    """Element IDs must be stable across repeated extractions."""
    nodes = [
        {
            "role": "textbox",
            "name": "Email",
            "nodeId": "1",
            "backendDOMNodeId": 101,
            "parentId": "p1",
        },
        {
            "role": "textbox",
            "name": "Password",
            "nodeId": "2",
            "backendDOMNodeId": 102,
            "parentId": "p1",
        },
        {
            "role": "button",
            "name": "Submit",
            "nodeId": "3",
            "backendDOMNodeId": 103,
            "parentId": "p1",
        },
    ]
    extractor = DOMExtractor()
    elements = extractor._extract_interactable(nodes)

    assert len(elements) == 3
    assert all(el.id < 100_000 for el in elements)

    # Re-run should produce identical IDs
    elements_again = extractor._extract_interactable(nodes)
    assert [el.id for el in elements] == [el.id for el in elements_again]


def test_id_collision_resolution():
    """When two elements hash to the same ID, one should be bumped."""
    # Create elements with deliberately similar signatures
    nodes = [
        {"role": "button", "name": "A", "nodeId": "1", "backendDOMNodeId": 1, "parentId": "same"},
        {"role": "button", "name": "A", "nodeId": "2", "backendDOMNodeId": 2, "parentId": "same"},
    ]
    extractor = DOMExtractor()
    elements = extractor._extract_interactable(nodes)

    ids = [el.id for el in elements]
    # IDs must be unique even if input hashes collide
    assert len(set(ids)) == len(ids)


def test_role_sequence_stable():
    """Role sequences should be deterministic and use semantic_name (not live value)."""
    elements = [
        AXElement(
            id=1,
            role="button",
            semantic_name="Submit",
            current_value="",
            node_id="1",
            backend_dom_id=1,
            depth=1,
        ),
        AXElement(
            id=2,
            role="textbox",
            semantic_name="Email",
            current_value="",
            node_id="2",
            backend_dom_id=2,
            depth=1,
        ),
    ]
    seq1 = _build_role_sequence(elements)
    seq2 = _build_role_sequence(elements)
    assert seq1 == seq2
    assert "button:Submit" in seq1
    assert "textbox:Email" in seq1


def test_structural_hash_deterministic():
    """Same role sequence must always produce the same hash."""
    seq = "button:Submit:1|textbox:Email:1"
    h1 = _structural_hash(seq)
    h2 = _structural_hash(seq)
    assert h1 == h2
    assert len(h1) > 0


# ------------------------------------------------------------------ #
# Hash Similarity Tests                                                  #
# ------------------------------------------------------------------ #


def test_hash_similarity_identical():
    seq_a = "button:Submit:1|textbox:Email:1"
    assert hash_similarity(seq_a, seq_a) == 1.0


def test_hash_similarity_minor_difference():
    seq_a = "button:Submit:1|textbox:Email:1"
    seq_b = "button:Submit:1|textbox:Email_v2:1"
    sim = hash_similarity(seq_a, seq_b)
    assert 0.4 <= sim < 1.0  # High similarity but not identical


def test_hash_similarity_completely_different():
    seq_a = "button:Submit:1|textbox:Email:1"
    seq_b = "link:Home:0|checkbox:Agree:2|slider:Volume:1"
    assert hash_similarity(seq_a, seq_b) < 0.3


def test_hash_similarity_empty_sequences():
    assert hash_similarity("", "") == 1.0
    assert hash_similarity("button:X:1", "") == 0.0
    assert hash_similarity("", "button:X:1") == 0.0


def test_hash_similarity_insertion():
    """Adding one element should only slightly reduce similarity."""
    seq_a = "button:A:1|button:B:1|button:C:1"
    seq_b = "button:A:1|button:B:1|button:C:1|button:D:1"
    sim = hash_similarity(seq_a, seq_b)
    assert sim >= 0.7  # Should be high — only one insertion


# ------------------------------------------------------------------ #
# Cache Operation Tests                                                  #
# ------------------------------------------------------------------ #


def test_cache_store_and_lookup():
    with tempfile.TemporaryDirectory() as tmp:
        cache = MemoryCache(db_path=f"{tmp}/test.db", vcr_dir=f"{tmp}/vcr")

        commands = [CDPCommand("Page.navigate", {"url": "https://x.com"}, {}, 100.0)]
        cache.store("example.com", "hash123", "btn:Login:1", "login to app", commands)

        hit = cache.lookup("example.com", "btn:Login:1", "login to app")
        assert hit is not None
        assert hit.commands[0].method == "Page.navigate"
        assert hit.domain == "example.com"


def test_cache_miss_on_different_task():
    with tempfile.TemporaryDirectory() as tmp:
        cache = MemoryCache(db_path=f"{tmp}/test.db", vcr_dir=f"{tmp}/vcr")

        commands = [CDPCommand("Input.click", {}, {}, 10.0)]
        cache.store("example.com", "hash1", "btn:Login:1", "login to app", commands)

        hit = cache.lookup("example.com", "btn:Login:1", "completely different task")
        assert hit is None


def test_cache_task_uniqueness():
    """Two different tasks on the same domain+DOM should be stored separately."""
    with tempfile.TemporaryDirectory() as tmp:
        cache = MemoryCache(db_path=f"{tmp}/test.db", vcr_dir=f"{tmp}/vcr")

        cache.store(
            "example.com",
            "h1",
            "btn:Login:1",
            "login to app",
            [CDPCommand("Input.click", {}, {}, 10.0)],
        )
        cache.store(
            "example.com",
            "h1",
            "btn:Login:1",
            "reset password",
            [CDPCommand("Input.type", {}, {}, 20.0)],
        )

        hit1 = cache.lookup("example.com", "btn:Login:1", "login to app")
        assert hit1 is not None
        assert hit1.commands[0].method == "Input.click"

        hit2 = cache.lookup("example.com", "btn:Login:1", "reset password")
        assert hit2 is not None
        assert hit2.commands[0].method == "Input.type"


def test_cache_hit_counter():
    with tempfile.TemporaryDirectory() as tmp:
        cache = MemoryCache(db_path=f"{tmp}/test.db", vcr_dir=f"{tmp}/vcr")

        cache.store("x.com", "h1", "seq", "task", [CDPCommand("M", {}, {}, 1.0)])
        cache.increment_hit("x.com", "h1", _task_key("task"))

        hit = cache.lookup("x.com", "seq", "task")
        assert hit is not None
        assert hit.hit_count == 1


def test_cache_invalidation():
    with tempfile.TemporaryDirectory() as tmp:
        cache = MemoryCache(db_path=f"{tmp}/test.db", vcr_dir=f"{tmp}/vcr")

        cache.store("kill.com", "h1", "seq", "task", [CDPCommand("M", {}, {}, 1.0)])
        deleted = cache.invalidate("kill.com")
        assert deleted == 1

        hit = cache.lookup("kill.com", "seq", "task")
        assert hit is None


def test_cache_stats():
    with tempfile.TemporaryDirectory() as tmp:
        cache = MemoryCache(db_path=f"{tmp}/test.db", vcr_dir=f"{tmp}/vcr")

        cache.store("a.com", "h1", "s1", "t1", [CDPCommand("M", {}, {}, 1.0)])
        cache.store("b.com", "h2", "s2", "t2", [CDPCommand("M", {}, {}, 1.0)])
        cache.increment_hit("a.com", "h1", _task_key("t1"))

        stats = cache.stats()
        assert stats["total_sequences"] == 2
        assert stats["total_hits"] == 1
        assert stats["domains"] == 2


def test_backwards_compat_alias():
    """MuscleMemorycache should still work as an alias."""
    assert MuscleMemorycache is MemoryCache


def test_cache_multiple_dom_versions_same_task():
    """Multiple DOM structures for the same task should be stored separately."""
    with tempfile.TemporaryDirectory() as tmp:
        cache = MemoryCache(db_path=f"{tmp}/test.db", vcr_dir=f"{tmp}/vcr")

        # Store first version (original DOM)
        commands_v1 = [CDPCommand("Input.click", {"x": 10}, {}, 10.0)]
        cache.store("example.com", "hash_v1", "btn:Login:1", "login to app", commands_v1)

        # Store second version (DOM changed slightly - different structural hash)
        commands_v2 = [CDPCommand("Input.click", {"x": 20}, {}, 15.0)]
        cache.store("example.com", "hash_v2", "btn:Login:1", "login to app", commands_v2)

        # Both should be retrievable via lookup with their respective role sequences
        hit_v1 = cache.lookup("example.com", "btn:Login:1", "login to app")
        # The lookup uses fuzzy matching, so it will find the best match
        # Since we're using the same role_sequence for both, it will pick one
        assert hit_v1 is not None

        # Verify both are in the database
        db = cache._ensure_db()
        rows = db.execute(
            "SELECT structural_hash FROM sequences WHERE domain = ? AND task_key = ?",
            ("example.com", _task_key("login to app")),
        ).fetchall()
        hashes = {row[0] for row in rows}
        assert "hash_v1" in hashes
        assert "hash_v2" in hashes
        assert len(hashes) == 2  # Both versions preserved


def test_cache_store_preserves_first_version():
    """INSERT OR IGNORE preserves the first successful sequence for same hash."""
    with tempfile.TemporaryDirectory() as tmp:
        cache = MemoryCache(db_path=f"{tmp}/test.db", vcr_dir=f"{tmp}/vcr")

        # Store first version
        commands_v1 = [CDPCommand("Input.click", {"x": 10}, {}, 10.0)]
        cache.store("example.com", "same_hash", "btn:Login:1", "login to app", commands_v1)

        # Try to store second version with same hash - should be ignored
        commands_v2 = [CDPCommand("Input.click", {"x": 999}, {}, 999.0)]
        cache.store("example.com", "same_hash", "btn:Login:1", "login to app", commands_v2)

        # Lookup should return the FIRST version (x=10)
        hit = cache.lookup("example.com", "btn:Login:1", "login to app")
        assert hit is not None
        assert hit.commands[0].params["x"] == 10  # First version preserved


# ------------------------------------------------------------------ #
# Task Key Tests                                                        #
# ------------------------------------------------------------------ #


def test_task_key_normalization():
    """Task keys should be case-insensitive and strip whitespace."""
    assert _task_key("Login To App") == _task_key("login to app")
    assert _task_key("  login to app  ") == _task_key("login to app")


def test_task_key_different_tasks():
    """Different tasks MUST produce different keys."""
    assert _task_key("login to salesforce") != _task_key("reset password")


# ------------------------------------------------------------------ #
# VCR Writer Tests                                                      #
# ------------------------------------------------------------------ #


def test_vcr_write_format():
    """VCR files should be valid JSONL with session header + frames."""
    with tempfile.TemporaryDirectory() as tmp:
        cache = MemoryCache(db_path=f"{tmp}/test.db", vcr_dir=f"{tmp}/vcr")

        commands = [
            CDPCommand("Page.navigate", {"url": "https://x.com"}, {"frameId": "f1"}, 100.0),
            CDPCommand("Input.dispatchMouseEvent", {"x": 10, "y": 20}, {}, 5.0),
        ]

        vcr_path = cache.write_vcr(
            session_id="test_session_123",
            task_description="login to app",
            commands=commands,
            domain="x.com",
            was_cache_hit=False,
        )

        assert vcr_path.exists()
        lines = vcr_path.read_text().strip().split("\n")
        assert len(lines) == 3  # 1 session header + 2 frames

        # Parse each line as valid JSON
        session_line = json.loads(lines[0])
        assert session_line["type"] == "session"
        assert session_line["data"]["domain"] == "x.com"
        assert session_line["data"]["cache_hit"] is False

        frame1 = json.loads(lines[1])
        assert frame1["type"] == "frame"
        assert frame1["data"]["input_state"]["cdp_method"] == "Page.navigate"

        frame2 = json.loads(lines[2])
        assert frame2["data"]["input_state"]["cdp_method"] == "Input.dispatchMouseEvent"


# ------------------------------------------------------------------ #
# URL Validation Tests                                                  #
# ------------------------------------------------------------------ #


def test_url_validation_blocks_dangerous_schemes():
    from terx.server.mcp import _validate_url

    assert _validate_url("data:text/html,<script>alert(1)</script>") is not None
    assert _validate_url("javascript:alert(1)") is not None
    assert _validate_url("file:///etc/passwd") is not None
    assert _validate_url("blob:null") is not None
    assert _validate_url("vbscript:msgbox") is not None


def test_url_validation_allows_safe_schemes():
    from terx.server.mcp import _validate_url

    assert _validate_url("https://salesforce.com") is None
    assert _validate_url("http://localhost:3000") is None
    assert _validate_url("about:blank") is None


def test_url_validation_blocks_schemeless():
    from terx.server.mcp import _validate_url

    assert _validate_url("not-a-url") is not None


# ------------------------------------------------------------------ #
# LRU Screenshot Store Tests                                            #
# ------------------------------------------------------------------ #


def test_lru_store_basic():
    from terx.server.mcp import LRUScreenshotStore

    store = LRUScreenshotStore(max_size=3)
    store.put("a", b"img_a")
    store.put("b", b"img_b")
    store.put("c", b"img_c")

    assert store.get("a") == b"img_a"
    assert store.get("b") == b"img_b"


def test_lru_store_eviction():
    from terx.server.mcp import LRUScreenshotStore

    store = LRUScreenshotStore(max_size=2)
    store.put("a", b"img_a")
    store.put("b", b"img_b")
    store.put("c", b"img_c")  # Should evict "a"

    assert store.get("a") is None
    assert store.get("b") == b"img_b"
    assert store.get("c") == b"img_c"


def test_lru_store_access_refreshes():
    from terx.server.mcp import LRUScreenshotStore

    store = LRUScreenshotStore(max_size=2)
    store.put("a", b"img_a")
    store.put("b", b"img_b")
    store.get("a")  # Access "a" to refresh it
    store.put("c", b"img_c")  # Should evict "b" (oldest untouched)

    assert store.get("a") == b"img_a"  # Still alive
    assert store.get("b") is None  # Evicted


# ------------------------------------------------------------------ #
# ReplayCostLedger Tests                                                #
# ------------------------------------------------------------------ #


def test_ledger_hit_str():
    from terx.cache.cache import ReplayCostLedger

    ledger = ReplayCostLedger(
        task_description="login",
        hit=True,
        commands_replayed=12,
        estimated_llm_calls_saved=12,
        latency_ms=41.0,
        run_number=3,
    )
    s = str(ledger)
    assert "Cache HIT" in s
    assert "12 commands" in s
    assert "run #3" in s


def test_ledger_miss_str():
    from terx.cache.cache import ReplayCostLedger

    ledger = ReplayCostLedger(
        task_description="login",
        hit=False,
        commands_replayed=0,
        estimated_llm_calls_saved=0,
        latency_ms=0,
        run_number=1,
    )
    s = str(ledger)
    assert "Cache MISS" in s
    assert "run #1" in s


# ------------------------------------------------------------------ #
# CDP Bridge Connection Timeout Tests                                  #
# ------------------------------------------------------------------ #


def test_cdp_bridge_connect_timeout_param():
    """CDPBridge should accept connect_timeout parameter."""
    from terx.cdp.bridge import CDPBridge

    bridge = CDPBridge("ws://localhost:9222/test", connect_timeout=5.0)
    assert bridge.connect_timeout == 5.0

    # Default should be 10.0
    bridge2 = CDPBridge("ws://localhost:9222/test")
    assert bridge2.connect_timeout == 10.0


def test_browser_session_connect_timeout_param():
    """BrowserSession should accept and forward connect_timeout."""
    from terx.cdp.session import BrowserSession

    session = BrowserSession(connect_timeout=3.0)
    assert session.connect_timeout == 3.0

    # Default should be 10.0
    session2 = BrowserSession()
    assert session2.connect_timeout == 10.0


# ------------------------------------------------------------------ #
# TERX Server Tests                                                    #
# ------------------------------------------------------------------ #


def test_terx_server_instantiation():
    """TERXServer should be instantiable with custom config."""
    from terx.server.mcp import TERXServer
    from terx.cache.cache import MemoryCache

    cache = MemoryCache(db_path="/tmp/test_terx_server.db")
    server = TERXServer(cache=cache, host="localhost", port=9223, connect_timeout=5.0)

    assert server._host == "localhost"
    assert server._port == 9223
    assert server._connect_timeout == 5.0
    assert server.cache is cache
    assert hasattr(server, "mcp")


def test_terx_server_backwards_compat():
    """Module-level exports should work for backwards compatibility."""
    from terx.server.mcp import mcp, main, _validate_url, LRUScreenshotStore

    assert mcp is not None
    assert callable(main)
    assert callable(_validate_url)
    assert LRUScreenshotStore is not None

    # Test LRU store
    store = LRUScreenshotStore(max_size=2)
    store.put("a", b"img_a")
    assert store.get("a") == b"img_a"
