"""
TERX Core Tests — DOM extraction, cache operations, audit writer, URL validation.
"""

import tempfile
import json
import sqlite3
from pathlib import Path

import pytest

from terx.dom.extractor import (
    DOMExtractor,
    AXElement,
    DOMSnapshot,
    hash_similarity,
    _structural_hash,
    _build_role_sequence,
)
from terx.cache.cache import (
    ApprovalDecision,
    MemoryCache,
    MuscleMemorycache,
    CDPCommand,
    MutationDriftError,
    PostconditionFailed,
    ReplayPolicy,
    ReplayRefused,
    _replay_command_digest,
    _task_key,
    session_for,
)


def _semantic_click(label: str = "Login") -> CDPCommand:
    return CDPCommand("TERX.click", {"target": {"role": "button", "label": label}}, {}, 1.0)


TEST_SCOPE = "tenant-a"
TEST_PRECONDITION = {"url_contains": "example.com"}
TEST_POSTCONDITION = {"text_contains": "Logged in"}


def _replay_options(postcondition: dict | None = None) -> dict:
    return {
        "scope_id": TEST_SCOPE,
        "precondition": TEST_PRECONDITION,
        "postcondition": postcondition or TEST_POSTCONDITION,
    }


def _scoped_lookup(cache: MemoryCache, snapshot: DOMSnapshot, task: str):
    return cache.lookup(
        "example.com",
        snapshot.role_sequence,
        task,
        origin="https://example.com",
        route="/login",
        scope_hash=ReplayPolicy(scope_id=TEST_SCOPE).scope_hash(),
    )


class FakeBridge:
    def __init__(
        self,
        snapshot: DOMSnapshot | None = None,
        text: str = "Logged in",
        mutation_count: int = 0,
    ):
        self.snapshot = snapshot or _snapshot(email_backend_id=101, password_backend_id=102)
        self.text = text
        self.mutation_count = mutation_count
        self.sent: list[tuple[str, dict]] = []
        self._recorders = []

    def add_recorder(self, recorder):
        self._recorders.append(recorder)

    def remove_recorder(self, recorder):
        if recorder in self._recorders:
            self._recorders.remove(recorder)

    async def send(self, method: str, params: dict | None = None) -> dict:
        params = params or {}
        self.sent.append((method, params))
        result = self._result_for(method, params)
        for recorder in list(self._recorders):
            recorder(method, params, result, 1.0)
        return result

    async def send_internal(self, method: str, params: dict | None = None) -> dict:
        params = params or {}
        if method in {"DOM.focus", "Input.insertText", "DOM.resolveNode", "Runtime.callFunctionOn"}:
            self.sent.append((method, params))
        expression = params.get("expression", "")
        if "__TERX_MUTATION_GUARD__" in expression and "return true" in expression:
            return {"result": {"value": True}}
        if "__TERX_MUTATION_GUARD__ ?" in expression:
            return {"result": {"value": self.mutation_count}}
        if "delete window.__TERX_MUTATION_GUARD__" in expression:
            return {}
        if "window.location.href" in expression:
            return {"result": {"value": self.snapshot.url}}
        if "document.title" in expression:
            return {"result": {"value": self.snapshot.title}}
        if "innerText" in expression:
            return {"result": {"value": self.text}}
        if "querySelector" in expression:
            return {"result": {"value": True}}
        if method == "Page.captureScreenshot":
            return {"data": ""}
        return self._result_for(method, params)

    async def wait_for_load(self, timeout: float = 10.0) -> None:
        return None

    def _result_for(self, method: str, params: dict) -> dict:
        if method == "DOM.resolveNode":
            return {"object": {"objectId": f"object-{params.get('backendNodeId')}"}}
        if method == "Runtime.callFunctionOn":
            return {"result": {"value": True}}
        return {}


def _snapshot(email_backend_id=101, password_backend_id=102) -> DOMSnapshot:
    elements = [
        AXElement(
            id=1,
            role="textbox",
            semantic_name="Email",
            current_value="",
            node_id="email",
            backend_dom_id=email_backend_id,
            depth=1,
        ),
        AXElement(
            id=2,
            role="textbox",
            semantic_name="Password",
            current_value="",
            node_id="password",
            backend_dom_id=password_backend_id,
            depth=1,
        ),
        AXElement(
            id=3,
            role="button",
            semantic_name="Login",
            current_value="",
            node_id="login",
            backend_dom_id=103,
            depth=1,
        ),
    ]
    role_sequence = _build_role_sequence(elements)
    return DOMSnapshot(
        url="https://example.com/login",
        title="Login",
        elements=elements,
        structural_hash=_structural_hash(role_sequence),
        role_sequence=role_sequence,
    )


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
        cache = MemoryCache(db_path=f"{tmp}/test.db", audit_dir=f"{tmp}/audit")

        commands = [CDPCommand("TERX.navigate", {"url": "https://x.com"}, {}, 100.0)]
        cache.store("example.com", "hash123", "btn:Login:1", "login to app", commands)

        hit = cache.lookup("example.com", "btn:Login:1", "login to app")
        assert hit is not None
        assert hit.commands[0].method == "TERX.navigate"
        assert hit.domain == "example.com"


def test_cache_miss_on_different_task():
    with tempfile.TemporaryDirectory() as tmp:
        cache = MemoryCache(db_path=f"{tmp}/test.db", audit_dir=f"{tmp}/audit")

        commands = [_semantic_click()]
        cache.store("example.com", "hash1", "btn:Login:1", "login to app", commands)

        hit = cache.lookup("example.com", "btn:Login:1", "completely different task")
        assert hit is None


def test_cache_rejects_navigation_query_data():
    with tempfile.TemporaryDirectory() as tmp:
        cache = MemoryCache(db_path=f"{tmp}/test.db", audit_dir=f"{tmp}/audit")
        with pytest.raises(ValueError, match="http or https URL"):
            cache.store(
                "example.com",
                "hash",
                "button:Go:1",
                "navigate",
                [
                    CDPCommand(
                        "TERX.navigate", {"url": "https://example.com/?token=secret"}, {}, 1.0
                    )
                ],
            )


def test_cache_task_uniqueness():
    """Two different tasks on the same domain+DOM should be stored separately."""
    with tempfile.TemporaryDirectory() as tmp:
        cache = MemoryCache(db_path=f"{tmp}/test.db", audit_dir=f"{tmp}/audit")

        cache.store(
            "example.com",
            "h1",
            "btn:Login:1",
            "login to app",
            [_semantic_click("Login")],
        )
        cache.store(
            "example.com",
            "h1",
            "btn:Login:1",
            "reset password",
            [_semantic_click("Reset password")],
        )

        hit1 = cache.lookup("example.com", "btn:Login:1", "login to app")
        assert hit1 is not None
        assert hit1.commands[0].method == "TERX.click"

        hit2 = cache.lookup("example.com", "btn:Login:1", "reset password")
        assert hit2 is not None
        assert hit2.commands[0].method == "TERX.click"


def test_cache_hit_counter():
    with tempfile.TemporaryDirectory() as tmp:
        cache = MemoryCache(db_path=f"{tmp}/test.db", audit_dir=f"{tmp}/audit")

        cache.store("x.com", "h1", "seq", "task", [_semantic_click()])
        cache.increment_hit("x.com", "h1", _task_key("task"))

        hit = cache.lookup("x.com", "seq", "task")
        assert hit is not None
        assert hit.hit_count == 1


def test_cache_hit_counter_is_scoped():
    with tempfile.TemporaryDirectory() as tmp:
        cache = MemoryCache(db_path=f"{tmp}/test.db", audit_dir=f"{tmp}/audit")
        scope_a = ReplayPolicy(scope_id="tenant-a").scope_hash()
        scope_b = ReplayPolicy(scope_id="tenant-b").scope_hash()
        for scope in (scope_a, scope_b):
            cache.store(
                "x.com",
                "h1",
                "seq",
                "task",
                [_semantic_click()],
                origin="https://x.com",
                route_pattern="/start",
                scope_hash=scope,
            )
        cache.increment_hit(
            "x.com",
            "h1",
            _task_key("task"),
            origin="https://x.com",
            route_pattern="/start",
            scope_hash=scope_a,
        )
        a = cache.lookup(
            "x.com", "seq", "task", origin="https://x.com", route="/start", scope_hash=scope_a
        )
        b = cache.lookup(
            "x.com", "seq", "task", origin="https://x.com", route="/start", scope_hash=scope_b
        )
        assert a is not None and a.hit_count == 1
        assert b is not None and b.hit_count == 0


def test_v2_raw_entries_migrate_but_do_not_replay():
    with tempfile.TemporaryDirectory() as tmp:
        db_path = Path(tmp) / "legacy.db"
        db = sqlite3.connect(db_path)
        db.execute("CREATE TABLE schema_version (version INTEGER PRIMARY KEY)")
        db.execute("INSERT INTO schema_version VALUES (2)")
        db.execute(
            """
            CREATE TABLE sequences (
                id INTEGER PRIMARY KEY AUTOINCREMENT, domain TEXT NOT NULL,
                structural_hash TEXT NOT NULL, task_key TEXT NOT NULL,
                task_description TEXT NOT NULL, role_sequence TEXT NOT NULL,
                commands_json TEXT NOT NULL, hit_count INTEGER NOT NULL,
                created_at TEXT NOT NULL, last_used TEXT NOT NULL,
                UNIQUE(domain, structural_hash, task_key)
            )
            """
        )
        db.execute(
            "INSERT INTO sequences VALUES (1, ?, ?, ?, ?, ?, ?, 0, ?, ?)",
            (
                "example.com",
                "legacy",
                _task_key("login"),
                "login",
                "textbox:Email:1",
                json.dumps(
                    [{"method": "Runtime.evaluate", "params": {}, "result": {}, "latency_ms": 1}]
                ),
                "2026-01-01T00:00:00+00:00",
                "2026-01-01T00:00:00+00:00",
            ),
        )
        db.commit()
        db.close()

        cache = MemoryCache(db_path=db_path, audit_dir=Path(tmp) / "audit")
        assert (
            cache.lookup(
                "example.com",
                "textbox:Email:1",
                "login",
                origin="https://example.com",
                route="/login",
                scope_hash=ReplayPolicy(scope_id=TEST_SCOPE).scope_hash(),
            )
            is None
        )
        migrated = cache._ensure_db()
        assert migrated.execute("SELECT version FROM schema_version").fetchone()[0] == 3
        assert migrated.execute("SELECT expires_at FROM sequences").fetchone()[0] == 0


def test_cache_invalidation():
    with tempfile.TemporaryDirectory() as tmp:
        cache = MemoryCache(db_path=f"{tmp}/test.db", audit_dir=f"{tmp}/audit")

        cache.store("kill.com", "h1", "seq", "task", [_semantic_click()])
        deleted = cache.invalidate("kill.com")
        assert deleted == 1

        hit = cache.lookup("kill.com", "seq", "task")
        assert hit is None


def test_cache_stats():
    with tempfile.TemporaryDirectory() as tmp:
        cache = MemoryCache(db_path=f"{tmp}/test.db", audit_dir=f"{tmp}/audit")

        cache.store("a.com", "h1", "s1", "t1", [_semantic_click()])
        cache.store("b.com", "h2", "s2", "t2", [_semantic_click()])
        cache.increment_hit("a.com", "h1", _task_key("t1"))

        stats = cache.stats()
        assert stats["total_sequences"] == 2
        assert stats["total_hits"] == 1
        assert stats["domains"] == 2


def test_cli_inspect_cache_redacts_fields():
    from terx.cli import _inspect_cache

    with tempfile.TemporaryDirectory() as tmp:
        db_path = f"{tmp}/test.db"
        cache = MemoryCache(db_path=db_path, audit_dir=f"{tmp}/audit")
        cache.store(
            "example.com",
            "h1",
            "seq",
            "task",
            [
                CDPCommand(
                    "TERX.type",
                    {"target": {"role": "textbox", "label": "Password"}, "text": "{{password}}"},
                    {},
                    1.0,
                    metadata={"redacted": True, "placeholder": "{{password}}"},
                )
            ],
        )

        rows = _inspect_cache(Path(db_path), limit=5)

        assert rows[0]["domain"] == "example.com"
        assert rows[0]["commands"] == 1
        assert rows[0]["redacted_fields"] == ["password"]


def test_backwards_compat_alias():
    """MuscleMemorycache should still work as an alias."""
    assert MuscleMemorycache is MemoryCache


def test_cache_multiple_dom_versions_same_task():
    """Multiple DOM structures for the same task should be stored separately."""
    with tempfile.TemporaryDirectory() as tmp:
        cache = MemoryCache(db_path=f"{tmp}/test.db", audit_dir=f"{tmp}/audit")

        # Store first version (original DOM)
        commands_v1 = [_semantic_click("Version one")]
        cache.store("example.com", "hash_v1", "btn:Login:1", "login to app", commands_v1)

        # Store second version (DOM changed slightly - different structural hash)
        commands_v2 = [_semantic_click("Version two")]
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
        cache = MemoryCache(db_path=f"{tmp}/test.db", audit_dir=f"{tmp}/audit")

        # Store first version
        commands_v1 = [_semantic_click("First")]
        cache.store("example.com", "same_hash", "btn:Login:1", "login to app", commands_v1)

        # Try to store second version with same hash - should be ignored
        commands_v2 = [_semantic_click("Second")]
        cache.store("example.com", "same_hash", "btn:Login:1", "login to app", commands_v2)

        # Lookup should return the first successfully stored sequence.
        hit = cache.lookup("example.com", "btn:Login:1", "login to app")
        assert hit is not None
        assert hit.commands[0].params["target"]["label"] == "First"


# ------------------------------------------------------------------ #
# Recording / Replay Integration Tests                                #
# ------------------------------------------------------------------ #


@pytest.mark.asyncio
async def test_recording_redacts_and_parameterizes_inputs(monkeypatch):
    async def fake_snapshot(self, bridge):
        return bridge.snapshot

    monkeypatch.setattr(DOMExtractor, "snapshot", fake_snapshot)

    with tempfile.TemporaryDirectory() as tmp:
        cache = MemoryCache(db_path=f"{tmp}/test.db", audit_dir=f"{tmp}/audit")
        bridge = FakeBridge()

        async with session_for(
            cache,
            bridge,
            "login",
            variables={"email": "user@example.com", "password": "super-secret"},
            **_replay_options(),
        ) as ctx:
            assert not ctx.hit
            await bridge.send("DOM.focus", {"backendNodeId": 101})
            await bridge.send("Input.insertText", {"text": "user@example.com"})
            await bridge.send("DOM.focus", {"backendNodeId": 102})
            await bridge.send("Input.insertText", {"text": "super-secret"})

        hit = _scoped_lookup(cache, bridge.snapshot, "login")
        assert hit is not None

        params = [command.params for command in hit.commands]
        assert {"target": {"role": "textbox", "label": "Email"}, "text": "{{email}}"} in params
        assert {
            "target": {"role": "textbox", "label": "Password"},
            "text": "{{password}}",
        } in params
        assert "super-secret" not in json.dumps([command.__dict__ for command in hit.commands])
        assert ctx.report is not None
        assert ctx.report.cache_hit is False
        assert ctx.report.variables_used == ["email", "password"]
        assert ctx.report.redacted_fields == ["email", "password"]


@pytest.mark.asyncio
async def test_replay_interpolates_variables_and_remaps_backend_ids(monkeypatch):
    async def fake_snapshot(self, bridge):
        return bridge.snapshot

    monkeypatch.setattr(DOMExtractor, "snapshot", fake_snapshot)

    with tempfile.TemporaryDirectory() as tmp:
        cache = MemoryCache(db_path=f"{tmp}/test.db", audit_dir=f"{tmp}/audit")
        record_bridge = FakeBridge()

        async with session_for(
            cache,
            record_bridge,
            "login",
            variables={"email": "old@example.com", "password": "old-secret"},
            **_replay_options(),
        ) as ctx:
            await record_bridge.send("DOM.focus", {"backendNodeId": 101})
            await record_bridge.send("Input.insertText", {"text": "old@example.com"})
            await record_bridge.send("DOM.focus", {"backendNodeId": 102})
            await record_bridge.send("Input.insertText", {"text": "old-secret"})

        replay_bridge = FakeBridge(
            snapshot=_snapshot(email_backend_id=201, password_backend_id=202)
        )
        async with session_for(
            cache,
            replay_bridge,
            "login",
            variables={"email": "new@example.com", "password": "new-secret"},
            **_replay_options(),
        ) as ctx:
            assert ctx.hit
            await ctx.replay()

        assert ("DOM.focus", {"backendNodeId": 201}) in replay_bridge.sent
        assert ("Input.insertText", {"text": "new@example.com"}) in replay_bridge.sent
        assert ("DOM.focus", {"backendNodeId": 202}) in replay_bridge.sent
        assert ("Input.insertText", {"text": "new-secret"}) in replay_bridge.sent
        assert ctx.report is not None
        assert ctx.report.cache_hit is True
        assert ctx.report.commands_replayed == 2
        assert ctx.report.mutation_count == 0


@pytest.mark.asyncio
async def test_replay_requires_missing_secret_variable(monkeypatch):
    async def fake_snapshot(self, bridge):
        return bridge.snapshot

    monkeypatch.setattr(DOMExtractor, "snapshot", fake_snapshot)

    with tempfile.TemporaryDirectory() as tmp:
        cache = MemoryCache(db_path=f"{tmp}/test.db", audit_dir=f"{tmp}/audit")
        record_bridge = FakeBridge()

        async with session_for(
            cache,
            record_bridge,
            "login",
            variables={"password": "redacted-by-default"},
            **_replay_options(),
        ) as ctx:
            await record_bridge.send("DOM.focus", {"backendNodeId": 102})
            await record_bridge.send("Input.insertText", {"text": "redacted-by-default"})

        replay_bridge = FakeBridge()
        async with session_for(cache, replay_bridge, "login", **_replay_options()) as ctx:
            assert ctx.hit
            with pytest.raises(ReplayRefused, match="missing replay variable"):
                await ctx.replay()

        assert ctx.report is not None
        assert ctx.report.status == "refused"


@pytest.mark.asyncio
async def test_variable_names_are_normalized_for_placeholders(monkeypatch):
    async def fake_snapshot(self, bridge):
        return bridge.snapshot

    monkeypatch.setattr(DOMExtractor, "snapshot", fake_snapshot)

    with tempfile.TemporaryDirectory() as tmp:
        cache = MemoryCache(db_path=f"{tmp}/test.db", audit_dir=f"{tmp}/audit")
        bridge = FakeBridge()

        async with session_for(
            cache, bridge, "api login", variables={"api-key": "abc123"}, **_replay_options()
        ) as ctx:
            await bridge.send("DOM.focus", {"backendNodeId": 102})
            await bridge.send("Input.insertText", {"text": "abc123"})

        hit = _scoped_lookup(cache, bridge.snapshot, "api login")
        assert hit is not None
        assert any(command.params["text"] == "{{api_key}}" for command in hit.commands)

        replay_bridge = FakeBridge()
        async with session_for(
            cache, replay_bridge, "api login", variables={"api-key": "xyz789"}, **_replay_options()
        ) as ctx:
            assert ctx.hit
            await ctx.replay()

        assert ("Input.insertText", {"text": "xyz789"}) in replay_bridge.sent


@pytest.mark.asyncio
async def test_redact_all_text_env_refuses_unnamed_value(monkeypatch):
    async def fake_snapshot(self, bridge):
        return bridge.snapshot

    monkeypatch.setattr(DOMExtractor, "snapshot", fake_snapshot)
    monkeypatch.setenv("TERX_REDACT_ALL_TEXT", "1")

    with tempfile.TemporaryDirectory() as tmp:
        cache = MemoryCache(db_path=f"{tmp}/test.db", audit_dir=f"{tmp}/audit")
        bridge = FakeBridge()

        async with session_for(cache, bridge, "capture email", **_replay_options()) as ctx:
            await bridge.send("DOM.focus", {"backendNodeId": 101})
            await bridge.send("Input.insertText", {"text": "not-a-variable@example.com"})

        assert _scoped_lookup(cache, bridge.snapshot, "capture email") is None
        assert ctx.report is not None
        assert ctx.report.status == "refused"
        assert "named variable" in ctx.report.refusal_reasons[0]


@pytest.mark.asyncio
async def test_mutation_guard_blocks_drifting_replay(monkeypatch):
    async def fake_snapshot(self, bridge):
        return bridge.snapshot

    monkeypatch.setattr(DOMExtractor, "snapshot", fake_snapshot)

    with tempfile.TemporaryDirectory() as tmp:
        cache = MemoryCache(db_path=f"{tmp}/test.db", audit_dir=f"{tmp}/audit")
        record_bridge = FakeBridge()

        async with session_for(
            cache,
            record_bridge,
            "login",
            variables={"email": "old@example.com"},
            **_replay_options(),
        ):
            await record_bridge.send("DOM.focus", {"backendNodeId": 101})
            await record_bridge.send("Input.insertText", {"text": "old@example.com"})

        replay_bridge = FakeBridge(mutation_count=25)
        async with session_for(
            cache,
            replay_bridge,
            "login",
            variables={"email": "new@example.com"},
            mutation_threshold=20,
            **_replay_options(),
        ) as ctx:
            assert ctx.hit
            with pytest.raises(MutationDriftError):
                await ctx.replay()


@pytest.mark.asyncio
async def test_postcondition_failure_blocks_cache(monkeypatch):
    async def fake_snapshot(self, bridge):
        return bridge.snapshot

    monkeypatch.setattr(DOMExtractor, "snapshot", fake_snapshot)

    with tempfile.TemporaryDirectory() as tmp:
        cache = MemoryCache(db_path=f"{tmp}/test.db", audit_dir=f"{tmp}/audit")
        bridge = FakeBridge(text="Still on login")

        with pytest.raises(PostconditionFailed):
            async with session_for(
                cache,
                bridge,
                "login",
                **_replay_options(postcondition={"text_contains": "Logged in"}),
            ):
                await bridge.send("DOM.focus", {"backendNodeId": 101})
                await bridge.send("Input.insertText", {"text": "user@example.com"})

        assert cache.lookup("example.com", bridge.snapshot.role_sequence, "login") is None


@pytest.mark.asyncio
async def test_browser_use_adapter_wraps_agent_run(monkeypatch):
    from terx.integrations.browser_use import wrap_browser_use

    async def fake_snapshot(self, bridge):
        return bridge.snapshot

    monkeypatch.setattr(DOMExtractor, "snapshot", fake_snapshot)

    class BrowserUseLikeAgent:
        task = "login"

        def __init__(self, bridge):
            self.bridge = bridge
            self.runs = 0

        async def run(self):
            self.runs += 1
            await self.bridge.send("DOM.focus", {"backendNodeId": 101})
            await self.bridge.send("Input.insertText", {"text": "agent@example.com"})
            return "agent-result"

    with tempfile.TemporaryDirectory() as tmp:
        cache = MemoryCache(db_path=f"{tmp}/test.db", audit_dir=f"{tmp}/audit")
        bridge = FakeBridge()
        agent = BrowserUseLikeAgent(bridge)
        wrapped = wrap_browser_use(
            agent,
            cache=cache,
            bridge=bridge,
            variables={"email": "agent@example.com"},
            scope_id=TEST_SCOPE,
            precondition=TEST_PRECONDITION,
            postcondition=TEST_POSTCONDITION,
        )

        miss = await wrapped.run()
        assert miss.cache_hit is False
        assert miss.value == "agent-result"
        assert miss.commands_recorded == 1
        assert agent.runs == 1

        hit = await wrapped.run()
        assert hit.cache_hit is True
        assert hit.value is None
        assert agent.runs == 1


@pytest.mark.asyncio
async def test_dependency_free_workflow_adapter_replays_semantic_actions(monkeypatch):
    from terx.integrations.workflow import TerxWorkflow

    async def fake_snapshot(self, bridge):
        return bridge.snapshot

    monkeypatch.setattr(DOMExtractor, "snapshot", fake_snapshot)
    with tempfile.TemporaryDirectory() as tmp:
        cache = MemoryCache(db_path=f"{tmp}/test.db", audit_dir=f"{tmp}/audit")
        cold_bridge = FakeBridge()
        workflow = TerxWorkflow(
            cache=cache,
            bridge=cold_bridge,
            task="semantic login",
            scope_id=TEST_SCOPE,
            variables={"email": "agent@example.com"},
            precondition=TEST_PRECONDITION,
            postcondition=TEST_POSTCONDITION,
        )

        async def cold_path(browser):
            await browser.type_into("textbox", "Email", "email")
            await browser.click("button", "Login")
            await browser.wait_for(TEST_POSTCONDITION, timeout=0.2, poll_interval=0.01)
            return "cold-result"

        miss = await workflow.run(cold_path)
        assert miss.cache_hit is False
        assert miss.value == "cold-result"
        assert miss.commands_recorded == 2
        assert miss.report is not None
        assert miss.report.status == "miss"
        assert b"agent@example.com" not in (Path(tmp) / "test.db").read_bytes()

        warm_bridge = FakeBridge()
        warm_workflow = TerxWorkflow(
            cache=cache,
            bridge=warm_bridge,
            task="semantic login",
            scope_id=TEST_SCOPE,
            variables={"email": "next@example.com"},
            precondition=TEST_PRECONDITION,
            postcondition=TEST_POSTCONDITION,
        )

        async def should_not_run(_):
            raise AssertionError("cold path ran on TERX cache hit")

        hit = await warm_workflow.run(should_not_run)
        assert hit.cache_hit is True
        assert hit.value is None
        assert hit.report is not None
        assert hit.report.status == "hit"
        assert any(method == "Input.insertText" for method, _ in warm_bridge.sent)


@pytest.mark.asyncio
async def test_workflow_adapter_requires_declared_named_variables(monkeypatch):
    from terx.integrations.workflow import TerxWorkflow

    async def fake_snapshot(self, bridge):
        return bridge.snapshot

    monkeypatch.setattr(DOMExtractor, "snapshot", fake_snapshot)
    with tempfile.TemporaryDirectory() as tmp:
        cache = MemoryCache(db_path=f"{tmp}/test.db", audit_dir=f"{tmp}/audit")
        workflow = TerxWorkflow(
            cache=cache,
            bridge=FakeBridge(),
            task="missing variable",
            scope_id=TEST_SCOPE,
            precondition=TEST_PRECONDITION,
            postcondition=TEST_POSTCONDITION,
        )

        async def cold_path(browser):
            await browser.type_into("textbox", "Email", "email")

        with pytest.raises(ValueError, match="was not supplied"):
            await workflow.run(cold_path)


def test_cli_mcp_config_prints_client_snippets(capsys):
    from terx.cli import main

    assert main(["mcp-config", "--client", "cursor"]) == 0
    assert json.loads(capsys.readouterr().out) == {
        "mcpServers": {"terx": {"command": "terx-server"}}
    }

    assert main(["mcp-config", "--client", "codex"]) == 0
    assert capsys.readouterr().out == '[mcp_servers.terx]\ncommand = "terx-server"\n'


@pytest.mark.asyncio
async def test_scope_mismatch_is_a_cache_miss(monkeypatch):
    async def fake_snapshot(self, bridge):
        return bridge.snapshot

    monkeypatch.setattr(DOMExtractor, "snapshot", fake_snapshot)
    with tempfile.TemporaryDirectory() as tmp:
        cache = MemoryCache(db_path=f"{tmp}/test.db", audit_dir=f"{tmp}/audit")
        async with session_for(
            cache,
            FakeBridge(),
            "login",
            variables={"email": "one@example.com"},
            **_replay_options(),
        ) as ctx:
            await ctx._bridge.send("DOM.focus", {"backendNodeId": 101})
            await ctx._bridge.send("Input.insertText", {"text": "one@example.com"})

        async with session_for(
            cache,
            FakeBridge(),
            "login",
            variables={"email": "two@example.com"},
            scope_id="tenant-b",
            precondition=TEST_PRECONDITION,
            postcondition=TEST_POSTCONDITION,
        ) as ctx:
            assert not ctx.hit


@pytest.mark.asyncio
async def test_policy_mismatch_is_a_cache_miss(monkeypatch):
    async def fake_snapshot(self, bridge):
        return bridge.snapshot

    monkeypatch.setattr(DOMExtractor, "snapshot", fake_snapshot)
    with tempfile.TemporaryDirectory() as tmp:
        cache = MemoryCache(db_path=f"{tmp}/test.db", audit_dir=f"{tmp}/audit")
        record_bridge = FakeBridge()
        async with session_for(
            cache,
            record_bridge,
            "login",
            variables={"email": "one@example.com"},
            **_replay_options(),
        ):
            await record_bridge.send("DOM.focus", {"backendNodeId": 101})
            await record_bridge.send("Input.insertText", {"text": "one@example.com"})

        async with session_for(
            cache,
            FakeBridge(),
            "login",
            variables={"email": "two@example.com"},
            **_replay_options(postcondition={"title_contains": "Login"}),
        ) as ctx:
            assert not ctx.hit


@pytest.mark.asyncio
async def test_javascript_condition_cannot_enter_replay_cache(monkeypatch):
    async def fake_snapshot(self, bridge):
        return bridge.snapshot

    monkeypatch.setattr(DOMExtractor, "snapshot", fake_snapshot)
    with tempfile.TemporaryDirectory() as tmp:
        cache = MemoryCache(db_path=f"{tmp}/test.db", audit_dir=f"{tmp}/audit")
        bridge = FakeBridge()
        async with session_for(
            cache,
            bridge,
            "unsupported policy",
            variables={"email": "one@example.com"},
            scope_id=TEST_SCOPE,
            precondition={"js": "true"},
            postcondition=TEST_POSTCONDITION,
        ) as ctx:
            await bridge.send("DOM.focus", {"backendNodeId": 101})
            await bridge.send("Input.insertText", {"text": "one@example.com"})
        assert ctx.report is not None
        assert ctx.report.status == "refused"
        assert "unsupported checks: js" in ctx.report.refusal_reasons[0]


@pytest.mark.asyncio
async def test_precondition_failure_refuses_before_replay(monkeypatch):
    async def fake_snapshot(self, bridge):
        return bridge.snapshot

    monkeypatch.setattr(DOMExtractor, "snapshot", fake_snapshot)
    policy = {
        "scope_id": TEST_SCOPE,
        "precondition": {"text_contains": "Ready to submit"},
        "postcondition": {"url_contains": "example.com"},
    }
    with tempfile.TemporaryDirectory() as tmp:
        cache = MemoryCache(db_path=f"{tmp}/test.db", audit_dir=f"{tmp}/audit")
        record_bridge = FakeBridge(text="Ready to submit")
        async with session_for(
            cache, record_bridge, "submit", variables={"email": "one@example.com"}, **policy
        ):
            await record_bridge.send("DOM.focus", {"backendNodeId": 101})
            await record_bridge.send("Input.insertText", {"text": "one@example.com"})

        replay_bridge = FakeBridge(text="Not ready")
        async with session_for(
            cache, replay_bridge, "submit", variables={"email": "two@example.com"}, **policy
        ) as ctx:
            assert ctx.hit
            with pytest.raises(ReplayRefused, match="precondition failed"):
                await ctx.replay()
        assert not any(method == "Input.insertText" for method, _ in replay_bridge.sent)
        assert ctx.report is not None
        assert ctx.report.status == "refused"


@pytest.mark.asyncio
async def test_destructive_replay_requires_per_hit_approval(monkeypatch):
    async def fake_snapshot(self, bridge):
        return bridge.snapshot

    monkeypatch.setattr(DOMExtractor, "snapshot", fake_snapshot)
    policy = {
        "scope_id": TEST_SCOPE,
        "precondition": TEST_PRECONDITION,
        "postcondition": {"url_contains": "example.com"},
        "side_effect": "destructive",
    }
    with tempfile.TemporaryDirectory() as tmp:
        cache = MemoryCache(db_path=f"{tmp}/test.db", audit_dir=f"{tmp}/audit")
        record_bridge = FakeBridge()
        async with session_for(cache, record_bridge, "approve", **policy):
            resolved = await record_bridge.send("DOM.resolveNode", {"backendNodeId": 103})
            await record_bridge.send(
                "Runtime.callFunctionOn",
                {
                    "objectId": resolved["object"]["objectId"],
                    "functionDeclaration": "function() { this.click(); }",
                },
            )

        consumed_tokens: set[str] = set()
        expected_commands = [_semantic_click("Login")]

        def consume_approval(request):
            assert request.task_description == "approve"
            assert request.scope_hash == ReplayPolicy(scope_id=TEST_SCOPE).scope_hash()
            assert request.workflow_version == 1
            assert (
                request.policy_fingerprint
                == ReplayPolicy(
                    scope_id=TEST_SCOPE,
                    precondition=TEST_PRECONDITION,
                    postcondition={"url_contains": "example.com"},
                    side_effect="destructive",
                ).fingerprint()
            )
            assert request.structural_hash == record_bridge.snapshot.structural_hash
            assert request.command_digest == _replay_command_digest(expected_commands)
            if request.token in consumed_tokens:
                return ApprovalDecision(False, False, "approval already consumed")
            if request.token != "fresh-approval":
                return ApprovalDecision(False, False, "unknown approval")
            consumed_tokens.add(request.token)
            return ApprovalDecision(True, True)

        replay_bridge = FakeBridge()
        async with session_for(
            cache,
            replay_bridge,
            "approve",
            approval_verifier=consume_approval,
            **policy,
        ) as ctx:
            assert ctx.hit
            with pytest.raises(ReplayRefused, match="approval_token"):
                await ctx.replay()
            assert ctx.report is not None
            assert ctx.report.status == "refused"
            await ctx.replay(approval_token="fresh-approval")
        assert any(method == "Runtime.callFunctionOn" for method, _ in replay_bridge.sent)

        reused_bridge = FakeBridge()
        async with session_for(
            cache,
            reused_bridge,
            "approve",
            approval_verifier=consume_approval,
            **policy,
        ) as ctx:
            with pytest.raises(ReplayRefused, match="approval already consumed"):
                await ctx.replay(approval_token="fresh-approval")
        assert not any(method == "Runtime.callFunctionOn" for method, _ in reused_bridge.sent)

        no_verifier_bridge = FakeBridge()
        async with session_for(cache, no_verifier_bridge, "approve", **policy) as ctx:
            with pytest.raises(ReplayRefused, match="approval_verifier"):
                await ctx.replay(approval_token="fresh-approval")
        assert not any(method == "Runtime.callFunctionOn" for method, _ in no_verifier_bridge.sent)

        invalid_verifier_bridge = FakeBridge()
        async with session_for(
            cache,
            invalid_verifier_bridge,
            "approve",
            approval_verifier=lambda _: True,
            **policy,
        ) as ctx:
            with pytest.raises(ReplayRefused, match="must return ApprovalDecision"):
                await ctx.replay(approval_token="fresh-approval")
        assert not any(
            method == "Runtime.callFunctionOn" for method, _ in invalid_verifier_bridge.sent
        )


@pytest.mark.asyncio
async def test_destructive_approval_is_bound_to_the_selected_command_sequence(monkeypatch):
    async def fake_snapshot(self, bridge):
        return bridge.snapshot

    monkeypatch.setattr(DOMExtractor, "snapshot", fake_snapshot)
    policy = {
        "scope_id": TEST_SCOPE,
        "precondition": TEST_PRECONDITION,
        "postcondition": {"url_contains": "example.com"},
        "side_effect": "destructive",
    }
    with tempfile.TemporaryDirectory() as tmp:
        cache = MemoryCache(db_path=f"{tmp}/test.db", audit_dir=f"{tmp}/audit")
        record_bridge = FakeBridge()
        async with session_for(cache, record_bridge, "approve", **policy):
            resolved = await record_bridge.send("DOM.resolveNode", {"backendNodeId": 103})
            await record_bridge.send(
                "Runtime.callFunctionOn",
                {
                    "objectId": resolved["object"]["objectId"],
                    "functionDeclaration": "function() { this.click(); }",
                },
            )

        original_digest = _replay_command_digest([_semantic_click("Login")])
        db = cache._ensure_db()
        db.execute(
            "UPDATE sequences SET commands_json = ? WHERE domain = ? AND task_key = ?",
            (
                json.dumps(
                    [
                        {
                            "method": "TERX.click",
                            "params": {"target": {"role": "button", "label": "Approve invoice"}},
                            "result": {},
                            "latency_ms": 1.0,
                            "metadata": {},
                        }
                    ]
                ),
                "example.com",
                _task_key("approve"),
            ),
        )
        db.commit()

        def only_original_sequence(request):
            if request.command_digest == original_digest:
                return ApprovalDecision(True, True)
            return ApprovalDecision(False, False, "approval is for another command sequence")

        replay_bridge = FakeBridge()
        async with session_for(
            cache,
            replay_bridge,
            "approve",
            approval_verifier=only_original_sequence,
            **policy,
        ) as ctx:
            assert ctx.hit
            with pytest.raises(ReplayRefused, match="another command sequence"):
                await ctx.replay(approval_token="approval-for-login")
        assert not any(method == "Runtime.callFunctionOn" for method, _ in replay_bridge.sent)


@pytest.mark.asyncio
async def test_empty_replay_conditions_are_refused_and_never_cached(monkeypatch):
    async def fake_snapshot(self, bridge):
        return bridge.snapshot

    monkeypatch.setattr(DOMExtractor, "snapshot", fake_snapshot)
    with tempfile.TemporaryDirectory() as tmp:
        cache = MemoryCache(db_path=f"{tmp}/test.db", audit_dir=f"{tmp}/audit")
        bridge = FakeBridge()
        async with session_for(
            cache,
            bridge,
            "empty condition",
            scope_id=TEST_SCOPE,
            precondition={"url_contains": ""},
            postcondition=TEST_POSTCONDITION,
            variables={"email": "agent@example.com"},
        ) as ctx:
            await bridge.send("DOM.focus", {"backendNodeId": 101})
            await bridge.send("Input.insertText", {"text": "agent@example.com"})

        assert ctx.report is not None
        assert ctx.report.status == "refused"
        assert "precondition values must be non-empty strings: url_contains" in (
            ctx.report.refusal_reasons
        )
        assert cache.stats()["total_sequences"] == 0


@pytest.mark.asyncio
async def test_raw_script_disables_cache_and_never_persists_secret(monkeypatch):
    async def fake_snapshot(self, bridge):
        return bridge.snapshot

    monkeypatch.setattr(DOMExtractor, "snapshot", fake_snapshot)
    with tempfile.TemporaryDirectory() as tmp:
        cache = MemoryCache(db_path=f"{tmp}/test.db", audit_dir=f"{tmp}/audit")
        bridge = FakeBridge()
        async with session_for(
            cache,
            bridge,
            "script plus input",
            variables={"email": "secret@example.com"},
            **_replay_options(),
        ) as ctx:
            await bridge.send("Runtime.evaluate", {"expression": "window.secret = 'leak-me'"})
            await bridge.send("DOM.focus", {"backendNodeId": 101})
            await bridge.send("Input.insertText", {"text": "secret@example.com"})

        assert ctx.report is not None
        assert ctx.report.status == "refused"
        db_text = (Path(tmp) / "test.db").read_bytes()
        assert b"leak-me" not in db_text
        assert b"secret@example.com" not in db_text
        audit = next((Path(tmp) / "audit").glob("*.jsonl"))
        assert "leak-me" not in audit.read_text()
        assert "secret@example.com" not in audit.read_text()


@pytest.mark.asyncio
async def test_ambiguous_semantic_target_is_refused(monkeypatch):
    async def fake_snapshot(self, bridge):
        return bridge.snapshot

    monkeypatch.setattr(DOMExtractor, "snapshot", fake_snapshot)
    policy = {
        "scope_id": TEST_SCOPE,
        "precondition": TEST_PRECONDITION,
        "postcondition": {"url_contains": "example.com"},
    }
    with tempfile.TemporaryDirectory() as tmp:
        cache = MemoryCache(db_path=f"{tmp}/test.db", audit_dir=f"{tmp}/audit")
        record_bridge = FakeBridge()
        async with session_for(cache, record_bridge, "click login", **policy):
            resolved = await record_bridge.send("DOM.resolveNode", {"backendNodeId": 103})
            await record_bridge.send(
                "Runtime.callFunctionOn",
                {
                    "objectId": resolved["object"]["objectId"],
                    "functionDeclaration": "function() { this.click(); }",
                },
            )

        duplicate_snapshot = _snapshot()
        duplicate_snapshot.elements.append(
            AXElement(4, "button", "Login", "", "login-two", 104, depth=1)
        )
        replay_bridge = FakeBridge(snapshot=duplicate_snapshot)
        async with session_for(cache, replay_bridge, "click login", **policy) as ctx:
            assert ctx.hit
            with pytest.raises(ReplayRefused, match="ambiguous"):
                await ctx.replay()
        assert ctx.report is not None
        assert ctx.report.status == "refused"


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
# Audit Writer Tests                                                      #
# ------------------------------------------------------------------ #


def test_audit_write_format():
    """Audit files should be valid JSONL with session header + frames."""
    with tempfile.TemporaryDirectory() as tmp:
        cache = MemoryCache(db_path=f"{tmp}/test.db", audit_dir=f"{tmp}/audit")

        commands = [
            CDPCommand("TERX.navigate", {"url": "https://x.com"}, {"frameId": "f1"}, 100.0),
            _semantic_click(),
        ]

        audit_path = cache.write_audit(
            session_id="test_session_123",
            task_description="login to app",
            commands=commands,
            domain="x.com",
            was_cache_hit=False,
        )

        assert audit_path.exists()
        lines = audit_path.read_text().strip().split("\n")
        assert len(lines) == 3  # 1 session header + 2 frames

        # Parse each line as valid JSON
        session_line = json.loads(lines[0])
        assert session_line["type"] == "session"
        assert session_line["data"]["domain"] == "x.com"
        assert session_line["data"]["cache_hit"] is False

        frame1 = json.loads(lines[1])
        assert frame1["type"] == "frame"
        assert frame1["data"]["input_state"]["cdp_method"] == "TERX.navigate"

        frame2 = json.loads(lines[2])
        assert frame2["data"]["input_state"]["cdp_method"] == "TERX.click"


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
