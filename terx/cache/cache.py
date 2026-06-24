"""
TERX Memory Cache — the core caching engine.

Records successful CDP action sequences keyed by (domain, structural_hash, task).
On cache hit: replays raw CDP commands directly — zero LLM tokens.
On cache miss: lets the agent reason normally, then caches the result.

Writes TERX audit JSONL files for recorded and replayed browser sessions.
"""

from __future__ import annotations

import hashlib
import inspect
import json
import logging
import os
import re
import sqlite3
import threading
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from terx.cdp.bridge import CDPBridge
from terx.dom.extractor import DOMExtractor, DOMSnapshot, hash_similarity

logger = logging.getLogger(__name__)

# Cache hit threshold — role sequences more similar than this are treated as the same page
SIMILARITY_THRESHOLD = 0.85
SSIM_THRESHOLD = 0.85
AUDIT_DIR = Path(".terx/audit")
SCREENSHOT_DIR = Path(".terx/screenshots")

MUTATING_CDP_METHODS = {
    "Page.navigate",
    "Input.dispatchMouseEvent",
    "Input.dispatchKeyEvent",
    "Input.insertText",
    "DOM.focus",
    "Runtime.evaluate",
    "Runtime.callFunctionOn",
}

RECORDED_CDP_METHODS = MUTATING_CDP_METHODS | {
    # Needed to replay later Runtime.callFunctionOn commands with fresh objectIds.
    "DOM.resolveNode",
}

PLACEHOLDER_RE = re.compile(r"\{\{([a-zA-Z_][a-zA-Z0-9_]*)\}\}")
SENSITIVE_LABEL_PARTS = (
    "password",
    "passcode",
    "secret",
    "token",
    "api key",
    "apikey",
    "access key",
    "private key",
    "credit card",
    "card number",
    "cvv",
    "cvc",
    "ssn",
    "social security",
)


@dataclass
class CDPCommand:
    """A single recorded CDP command."""

    method: str
    params: dict
    result: dict
    latency_ms: float
    metadata: dict = field(default_factory=dict)


@dataclass
class CachedSequence:
    """A cached action sequence for one successful task."""

    domain: str
    structural_hash: str
    task_key: str
    task_description: str
    commands: list[CDPCommand]
    hit_count: int
    created_at: str
    last_used: str


@dataclass
class ReplayCostLedger:
    """Tracks savings from a cache replay."""

    task_description: str
    hit: bool
    commands_replayed: int
    estimated_llm_calls_saved: int
    latency_ms: float
    run_number: int

    def __str__(self) -> str:
        if self.hit:
            return (
                f"💾 Cache HIT · {self.commands_replayed} commands · "
                f"{self.latency_ms:.0f}ms · "
                f"~{self.estimated_llm_calls_saved} LLM calls saved · "
                f"run #{self.run_number}"
            )
        return f"🔍 Cache MISS · run #{self.run_number} (learning...)"


@dataclass
class ReplayReport:
    """Structured replay/recording report for CLI, MCP, and integrations."""

    task_description: str
    domain: str
    cache_hit: bool
    commands_recorded: int = 0
    commands_replayed: int = 0
    variables_used: list[str] = field(default_factory=list)
    redacted_fields: list[str] = field(default_factory=list)
    postcondition: Any = None
    latency_ms: float = 0.0
    run_number: int = 1
    mutation_count: int | None = None
    mutation_threshold: int | None = None

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


class MemoryCache:
    """
    The TERX memory cache.

    Stores CDP command sequences keyed by (domain, DOM structure, task).
    On hit: replays raw CDP directly — zero LLM tokens.
    On miss: records the agent's commands for next time.

    Usage:
        cache = MemoryCache()

        async with session_for(cache, bridge, "login to salesforce") as ctx:
            if ctx.hit:
                await ctx.replay()
            else:
                await my_agent.run(task)

        print(ctx.ledger)
    """

    def __init__(
        self,
        db_path: str | Path = ".terx/cache.db",
        audit_dir: str | Path = AUDIT_DIR,
        similarity_threshold: float = SIMILARITY_THRESHOLD,
    ) -> None:
        self.db_path = Path(db_path)
        self.audit_dir = Path(audit_dir)
        self.similarity_threshold = similarity_threshold
        self._db: sqlite3.Connection | None = None
        self._db_lock = threading.RLock()  # Reentrant: public methods call _ensure_db().

    # ------------------------------------------------------------------ #
    # Setup                                                                 #
    # ------------------------------------------------------------------ #

    # Schema version for migrations
    SCHEMA_VERSION = 2

    def _ensure_db(self) -> sqlite3.Connection:
        with self._db_lock:
            if self._db is not None:
                return self._db
            self.db_path.parent.mkdir(parents=True, exist_ok=True)
            db = sqlite3.connect(self.db_path, check_same_thread=False)
            db.execute("PRAGMA journal_mode=WAL")

            # Create schema version table
            db.execute("""
                CREATE TABLE IF NOT EXISTS schema_version (
                    version INTEGER PRIMARY KEY
                )
            """)

            # Get current schema version
            cursor = db.execute("SELECT version FROM schema_version")
            row = cursor.fetchone()
            current_version = row[0] if row else 0

            if current_version < self.SCHEMA_VERSION:
                self._migrate_db(db, current_version)

            db.execute("""
                CREATE TABLE IF NOT EXISTS sequences (
                    id              INTEGER PRIMARY KEY AUTOINCREMENT,
                    domain          TEXT    NOT NULL,
                    structural_hash TEXT    NOT NULL,
                    task_key        TEXT    NOT NULL,
                    task_description TEXT   NOT NULL,
                    role_sequence   TEXT    NOT NULL DEFAULT '',
                    commands_json   TEXT    NOT NULL,
                    hit_count       INTEGER NOT NULL DEFAULT 0,
                    created_at      TEXT    NOT NULL,
                    last_used       TEXT    NOT NULL,
                    UNIQUE(domain, structural_hash, task_key)
                )
            """)
            db.execute("CREATE INDEX IF NOT EXISTS idx_domain_task ON sequences(domain, task_key)")
            db.commit()
            self._db = db
            return db

    def _migrate_db(self, db: sqlite3.Connection, from_version: int) -> None:
        """Migrate database schema from from_version to SCHEMA_VERSION."""
        if from_version < 1:
            # Version 1: initial schema (already created above)
            db.execute("INSERT OR REPLACE INTO schema_version (version) VALUES (1)")

        if from_version < 2:
            # Version 2: No changes to sequences table, just bump version
            # The behavioral change is in store() using INSERT OR IGNORE
            db.execute("INSERT OR REPLACE INTO schema_version (version) VALUES (2)")

        db.commit()
        logger.info(
            "Migrated cache database from version %d to %d", from_version, self.SCHEMA_VERSION
        )

    # ------------------------------------------------------------------ #
    # Core cache operations                                                 #
    # ------------------------------------------------------------------ #

    def lookup(
        self, domain: str, role_sequence: str, task_description: str
    ) -> CachedSequence | None:
        """
        Find a cached sequence for the given domain + DOM structure + task.
        Uses Levenshtein distance on role sequences for fuzzy DOM matching.
        Task key is derived from the normalized task description.
        """
        with self._db_lock:
            db = self._ensure_db()
            task_key = _task_key(task_description)
            rows = db.execute(
                "SELECT structural_hash, task_description, commands_json, "
                "hit_count, created_at, last_used, role_sequence, task_key "
                "FROM sequences WHERE domain = ? AND task_key = ?",
                (domain, task_key),
            ).fetchall()

            best_match: tuple[float, Any] | None = None
            for row in rows:
                cached_role_seq = row[6]
                sim = hash_similarity(role_sequence, cached_role_seq)
                if sim >= self.similarity_threshold:
                    if best_match is None or sim > best_match[0]:
                        best_match = (sim, row)

            if best_match is None:
                return None

            _, row = best_match
            commands = [CDPCommand(**c) for c in json.loads(row[2])]
            return CachedSequence(
                domain=domain,
                structural_hash=row[0],
                task_key=row[7],
                task_description=row[1],
                commands=commands,
                hit_count=row[3],
                created_at=row[4],
                last_used=row[5],
            )

    def store(
        self,
        domain: str,
        structural_hash: str,
        role_sequence: str,
        task_description: str,
        commands: list[CDPCommand],
    ) -> None:
        """Persist a successful action sequence.

        Uses INSERT OR IGNORE to preserve the first successful sequence for each
        (domain, structural_hash, task_key). Subsequent runs with the same DOM
        structure will not overwrite the cached sequence.
        """
        with self._db_lock:
            db = self._ensure_db()
            now = datetime.now(timezone.utc).isoformat()
            commands_json = json.dumps([asdict(c) for c in commands])
            task_key = _task_key(task_description)

            db.execute(
                """
                INSERT OR IGNORE INTO sequences
                    (domain, structural_hash, task_key, task_description,
                     role_sequence, commands_json, hit_count, created_at, last_used)
                VALUES (?, ?, ?, ?, ?, ?, 0, ?, ?)
                """,
                (
                    domain,
                    structural_hash,
                    task_key,
                    task_description,
                    role_sequence,
                    commands_json,
                    now,
                    now,
                ),
            )
            try:
                db.commit()
            except sqlite3.Error as e:
                logger.error("Failed to commit cache sequence: %s", e)
                raise RuntimeError(f"Cache storage failed: {e}") from e
            logger.info(
                "Cached %d commands for domain=%s task=%s hash=%.8s",
                len(commands),
                domain,
                task_key,
                structural_hash,
            )

    def increment_hit(self, domain: str, structural_hash: str, task_key: str) -> None:
        """Increment the hit counter for a cached sequence."""
        with self._db_lock:
            db = self._ensure_db()
            now = datetime.now(timezone.utc).isoformat()
            db.execute(
                "UPDATE sequences SET hit_count = hit_count + 1, last_used = ? "
                "WHERE domain = ? AND structural_hash = ? AND task_key = ?",
                (now, domain, structural_hash, task_key),
            )
            db.commit()

    def invalidate(self, domain: str) -> int:
        """Remove all cached sequences for a domain. Returns rows deleted."""
        with self._db_lock:
            db = self._ensure_db()
            cursor = db.execute("DELETE FROM sequences WHERE domain = ?", (domain,))
            db.commit()
            return cursor.rowcount

    def stats(self) -> dict:
        """Return cache statistics."""
        with self._db_lock:
            db = self._ensure_db()
            total = db.execute("SELECT COUNT(*) FROM sequences").fetchone()[0]
            hits = db.execute("SELECT SUM(hit_count) FROM sequences").fetchone()[0] or 0
            domains = db.execute("SELECT COUNT(DISTINCT domain) FROM sequences").fetchone()[0]
            return {"total_sequences": total, "total_hits": hits, "domains": domains}

    # ------------------------------------------------------------------ #
    # TERX audit writer                                                     #
    # ------------------------------------------------------------------ #

    def write_audit(
        self,
        session_id: str,
        task_description: str,
        commands: list[CDPCommand],
        domain: str,
        was_cache_hit: bool,
    ) -> Path:
        """
        Write a browser session in TERX audit JSONL format.

        Format:
            {"type": "session", "data": {...}}
            {"type": "frame",   "data": {...}}  ← one per CDP command
        """
        self.audit_dir.mkdir(parents=True, exist_ok=True)
        audit_path = self.audit_dir / f"{session_id}.jsonl"

        with audit_path.open("w") as f:
            # Session header
            session_record = {
                "type": "session",
                "data": {
                    "session_id": session_id,
                    "created_at": datetime.now(timezone.utc).isoformat(),
                    "agent_type": "browser",
                    "tool": "terx",
                    "task": task_description,
                    "domain": domain,
                    "cache_hit": was_cache_hit,
                    "tags": ["browser", "terx", "cdp"],
                },
            }
            f.write(json.dumps(session_record) + "\n")

            # One frame per CDP command
            for i, cmd in enumerate(commands):
                frame = {
                    "type": "frame",
                    "data": {
                        "node_name": cmd.method.replace(".", "_").lower(),
                        "input_state": {
                            "cdp_method": cmd.method,
                            "cdp_params": cmd.params,
                            "frame_index": i,
                        },
                        "output_state": {
                            "cdp_result": cmd.result,
                        },
                        "metadata": {
                            "latency_ms": cmd.latency_ms,
                            "cache_hit": was_cache_hit,
                            "cdp_method": cmd.method,
                        },
                    },
                }
                f.write(json.dumps(frame) + "\n")

        logger.info("Wrote audit session → %s (%d frames)", audit_path, len(commands))
        return audit_path


# ------------------------------------------------------------------ #
# Recording context manager                                             #
# ------------------------------------------------------------------ #


class RecordingContext:
    """
    Context returned by session_for().

    Records all CDP commands sent through the bridge.
    On exit: stores them in the cache + writes an audit JSONL file.
    """

    def __init__(
        self,
        cache: MemoryCache,
        bridge: CDPBridge,
        task: str,
        session_id: str | None = None,
        variables: dict[str, Any] | None = None,
        postcondition: dict[str, Any] | Any | None = None,
        redact_secrets: bool = True,
        mutation_guard: bool = True,
        mutation_threshold: int = 20,
    ) -> None:
        self._cache = cache
        self._bridge = bridge
        self._task = task
        self._session_id = session_id or f"browser_session_{int(time.time())}"
        self._variables = _normalize_variables(variables or {})
        self._postcondition = postcondition
        self._redact_secrets = redact_secrets
        self._mutation_guard = mutation_guard
        self._mutation_threshold = mutation_threshold
        self._snapshot: DOMSnapshot | None = None
        self._domain: str = "unknown"
        self._cached_seq: CachedSequence | None = None
        self._run_number: int = 1
        self._recorded_commands: list[CDPCommand] = []
        self._active_node_info: dict[str, str] | None = None
        self.ledger: ReplayCostLedger | None = None
        self.report: ReplayReport | None = None

    @property
    def hit(self) -> bool:
        return self._cached_seq is not None

    @property
    def recorded_commands(self) -> int:
        return len(self._recorded_commands)

    async def replay(self, variables: dict[str, Any] | None = None) -> None:
        """Replay the cached CDP command sequence directly. Zero LLM calls."""
        if self._cached_seq is None:
            raise RuntimeError("No cached sequence to replay (cache miss)")

        t0 = time.perf_counter()
        replay_variables = {**self._variables, **_normalize_variables(variables or {})}

        # Fresh DOM snapshot of the current page at replay start
        extractor = DOMExtractor()
        current_snapshot = await extractor.snapshot(self._bridge)
        current_elements = current_snapshot.elements

        # Tracks dynamic mappings for objectId, nodeId, etc.
        runtime_value_map = {}
        mutation_guard_started = False
        mutation_count: int | None = None

        try:
            if self._mutation_guard:
                mutation_guard_started = await _start_mutation_guard(self._bridge)

            for cmd in self._cached_seq.commands:
                if cmd.method in MUTATING_CDP_METHODS:
                    await self._bridge.wait_for_load(timeout=2.0)

                # Translate parameters dynamically using node metadata and value map
                mapped_params = _translate_parameters(
                    cmd.params, cmd.metadata, current_elements, runtime_value_map, replay_variables
                )

                try:
                    actual_result = await self._bridge.send(cmd.method, mapped_params)
                    # Discover new runtime mappings from the output (e.g. objectId returned)
                    _discover_mappings(cmd.result, actual_result, runtime_value_map)
                except Exception as exc:
                    logger.warning(
                        "Replay failed at %s: %s — attempting self-healing", cmd.method, exc
                    )
                    from terx.agent.healer import SelfHealer

                    healer = SelfHealer()

                    # Update current DOM snapshot before healing
                    current_snapshot = await extractor.snapshot(self._bridge)
                    current_elements = current_snapshot.elements

                    new_params = await healer.heal_command(
                        failed_method=cmd.method,
                        old_params=mapped_params,
                        current_dom=current_elements,
                        task_desc=self._task,
                    )

                    if new_params:
                        logger.info("Self-healing generated new params: %s", new_params)
                        try:
                            actual_result = await self._bridge.send(cmd.method, new_params)
                            _discover_mappings(cmd.result, actual_result, runtime_value_map)
                            continue  # Successfully healed
                        except Exception as e2:
                            logger.error("Healed parameters failed: %s", e2)
                            raise CacheReplayError(cmd.method) from e2

                    raise CacheReplayError(cmd.method) from exc

            if mutation_guard_started:
                mutation_count = await _read_mutation_count(self._bridge)
                if mutation_count is not None and mutation_count > self._mutation_threshold:
                    raise MutationDriftError(mutation_count, self._mutation_threshold)

            await _assert_postcondition(self._bridge, self._postcondition)
        finally:
            if mutation_guard_started:
                await _stop_mutation_guard(self._bridge)

        # --- Visual Audit (SSIM) ---
        await self._run_ssim_audit()

        latency = (time.perf_counter() - t0) * 1000
        self._cache.increment_hit(
            self._domain,
            self._cached_seq.structural_hash,
            self._cached_seq.task_key,
        )

        self.ledger = ReplayCostLedger(
            task_description=self._task,
            hit=True,
            commands_replayed=len(self._cached_seq.commands),
            estimated_llm_calls_saved=len(self._cached_seq.commands),
            latency_ms=latency,
            run_number=self._run_number,
        )
        self.report = ReplayReport(
            task_description=self._task,
            domain=self._domain,
            cache_hit=True,
            commands_replayed=len(self._cached_seq.commands),
            variables_used=_placeholders_in_commands(self._cached_seq.commands),
            redacted_fields=_redacted_fields_in_commands(self._cached_seq.commands),
            postcondition=_postcondition_summary(self._postcondition),
            latency_ms=latency,
            run_number=self._run_number,
            mutation_count=mutation_count,
            mutation_threshold=self._mutation_threshold if self._mutation_guard else None,
        )

        # Still write an audit file for the replay.
        self._cache.write_audit(
            session_id=self._session_id,
            task_description=self._task,
            commands=self._cached_seq.commands,
            domain=self._domain,
            was_cache_hit=True,
        )

    async def _run_ssim_audit(self) -> None:
        """Run SSIM visual audit comparing current screenshot to cached baseline."""
        if self._cached_seq is None:
            return

        SCREENSHOT_DIR.mkdir(parents=True, exist_ok=True)
        screenshot_path = SCREENSHOT_DIR / f"{self._cached_seq.structural_hash}.png"

        if screenshot_path.exists():
            try:
                import base64 as _b64

                result = await self._bridge.send_internal(
                    "Page.captureScreenshot", {"format": "png"}
                )
                new_screenshot = _b64.b64decode(result.get("data", ""))
                old_screenshot = screenshot_path.read_bytes()

                from terx.vision.ssim import compute_ssim

                ssim_score = compute_ssim(old_screenshot, new_screenshot)
                logger.info("Visual Audit SSIM Score: %.3f", ssim_score)

                if ssim_score < SSIM_THRESHOLD:
                    logger.warning(
                        "SSIM drift detected (%.3f < %.3f)! UI changed significantly.",
                        ssim_score,
                        SSIM_THRESHOLD,
                    )
            except ImportError:
                logger.debug("SSIM audit skipped — vision deps not installed")
            except Exception as e:
                logger.warning("Failed to run SSIM visual audit: %s", e)

    def record_command(self, cmd: CDPCommand) -> None:
        """Manually record a command (legacy). Transparent proxy now auto-records."""
        self._recorded_commands.append(cmd)

    def _auto_record(self, method: str, params: dict, result: dict, latency: float) -> None:
        """Transparent interceptor: auto-captures mutating commands sent through the bridge."""
        # Note: We don't intercept Accessibility.getFullAXTree anymore - that should
        # only be called via send_internal() and shouldn't mutate our snapshot.

        if method in RECORDED_CDP_METHODS:
            node_info = _extract_node_info(params, self._snapshot)
            active_node_info = _first_node_info(node_info) or self._active_node_info
            recorded_params, param_meta = _prepare_recorded_params(
                method=method,
                params=params,
                active_node_info=active_node_info,
                variables=self._variables,
                redact_secrets=self._redact_secrets,
            )

            if node_info:
                self._active_node_info = _first_node_info(node_info)

            cmd = CDPCommand(
                method=method,
                params=recorded_params,
                result=result,
                latency_ms=latency,
                metadata={"node_info": node_info, **param_meta},
            )
            self._recorded_commands.append(cmd)

    async def __aenter__(self) -> "RecordingContext":
        # Capture DOM snapshot asynchronously on enter
        extractor = DOMExtractor()
        self._snapshot = await extractor.snapshot(self._bridge)
        self._domain = urlparse(self._snapshot.url).netloc or "unknown"

        # Update session_id with domain if using default
        if self._session_id.startswith("browser_session_"):
            self._session_id = f"browser_{self._domain}_{self._session_id.split('_')[-1]}"

        # Lookup cached sequence
        self._cached_seq = self._cache.lookup(
            self._domain, self._snapshot.role_sequence, self._task
        )

        # Cold miss is run #1. Existing sequence + previous hits gives repeat count.
        with self._cache._db_lock:
            db = self._cache._ensure_db()
            task_key = _task_key(self._task)
            self._run_number = db.execute(
                "SELECT COALESCE(SUM(hit_count), 0) + COUNT(*) + 1 "
                "FROM sequences WHERE domain = ? AND task_key = ?",
                (self._domain, task_key),
            ).fetchone()[0]

        if not self.hit:
            self._bridge.add_recorder(self._auto_record)
        return self

    async def __aexit__(self, exc_type: Any, *_: Any) -> None:
        if not self.hit:
            self._bridge.remove_recorder(self._auto_record)

        if exc_type is not None:
            return  # Don't cache failed runs

        if not self.hit and self._recorded_commands:
            await _assert_postcondition(self._bridge, self._postcondition)

            # Cache the new sequence
            self._cache.store(
                domain=self._domain,
                structural_hash=self._snapshot.structural_hash,
                role_sequence=self._snapshot.role_sequence,
                task_description=self._task,
                commands=self._recorded_commands,
            )

            # Save visual baseline for future SSIM checks
            try:
                import base64 as _b64

                result = await self._bridge.send_internal(
                    "Page.captureScreenshot", {"format": "png"}
                )
                screenshot_bytes = _b64.b64decode(result.get("data", ""))
                SCREENSHOT_DIR.mkdir(parents=True, exist_ok=True)
                (SCREENSHOT_DIR / f"{self._snapshot.structural_hash}.png").write_bytes(
                    screenshot_bytes
                )
            except Exception as e:
                logger.warning("Failed to save baseline screenshot for SSIM: %s", e)

            # Write audit file.
            self._cache.write_audit(
                session_id=self._session_id,
                task_description=self._task,
                commands=self._recorded_commands,
                domain=self._domain,
                was_cache_hit=False,
            )
            self.ledger = ReplayCostLedger(
                task_description=self._task,
                hit=False,
                commands_replayed=0,
                estimated_llm_calls_saved=0,
                latency_ms=0,
                run_number=self._run_number,
            )
            self.report = ReplayReport(
                task_description=self._task,
                domain=self._domain,
                cache_hit=False,
                commands_recorded=len(self._recorded_commands),
                variables_used=_placeholders_in_commands(self._recorded_commands),
                redacted_fields=_redacted_fields_in_commands(self._recorded_commands),
                postcondition=_postcondition_summary(self._postcondition),
                run_number=self._run_number,
            )


def session_for(
    cache: MemoryCache,
    bridge: CDPBridge,
    task: str,
    session_id: str | None = None,
    variables: dict[str, Any] | None = None,
    postcondition: dict[str, Any] | Any | None = None,
    redact_secrets: bool = True,
    mutation_guard: bool = True,
    mutation_threshold: int = 20,
) -> RecordingContext:
    """
    Factory: create a RecordingContext for a task on the current page.

    Example:
        async with session_for(cache, bridge, "login to salesforce") as ctx:
            if ctx.hit:
                await ctx.replay()
            else:
                await bridge.send("Page.navigate", {"url": "..."})
        print(ctx.ledger)
    """
    return RecordingContext(
        cache=cache,
        bridge=bridge,
        task=task,
        session_id=session_id,
        variables=variables,
        postcondition=postcondition,
        redact_secrets=redact_secrets,
        mutation_guard=mutation_guard,
        mutation_threshold=mutation_threshold,
    )


def _task_key(task_description: str) -> str:
    """
    Normalize task description into a deterministic cache key.
    Lowercases and strips whitespace, then hashes to fixed length.
    NOTE: This is exact-match (not semantic). "log in" != "login".
    For semantic matching, install terx[embeddings].
    """
    normalized = task_description.lower().strip()
    return hashlib.md5(normalized.encode()).hexdigest()[:16]


# ------------------------------------------------------------------ #
# Replay ID/Handle Mapping Helpers                                     #
# ------------------------------------------------------------------ #


def _extract_node_info(params: Any, snapshot: DOMSnapshot | None) -> dict:
    """Extract role and label for any backendNodeId references in params."""
    node_info = {}
    if not snapshot:
        return node_info

    def recurse(p: Any):
        if isinstance(p, dict):
            for k, v in p.items():
                if k in ("backendNodeId", "nodeId") and isinstance(v, int):
                    for el in snapshot.elements:
                        if el.backend_dom_id == v:
                            node_info[str(v)] = {"role": el.role, "label": el.label}
                            break
                recurse(v)
        elif isinstance(p, list):
            for item in p:
                recurse(item)

    recurse(params)
    return node_info


def _first_node_info(node_info: dict) -> dict[str, str] | None:
    for info in node_info.values():
        return info
    return None


def _prepare_recorded_params(
    method: str,
    params: dict,
    active_node_info: dict[str, str] | None,
    variables: dict[str, Any],
    redact_secrets: bool,
) -> tuple[dict, dict]:
    """Replace variable values and sensitive text with placeholders before caching."""
    metadata: dict[str, Any] = {}
    if method != "Input.insertText" or "text" not in params:
        return params, metadata

    text = params.get("text")
    if not isinstance(text, str):
        return params, metadata

    placeholder = _placeholder_for_value(text, variables)
    source = "variable"

    if placeholder is None and redact_secrets:
        if _redact_all_text_enabled():
            placeholder = _placeholder_for_node(active_node_info)
            source = "redact-all"
        elif _is_sensitive_input(active_node_info):
            placeholder = _placeholder_for_node(active_node_info)
            source = "sensitive-field"

    if placeholder is None:
        return params, metadata

    redacted = dict(params)
    redacted["text"] = placeholder
    metadata["redacted"] = True
    metadata["placeholder"] = placeholder
    metadata["placeholder_source"] = source
    if active_node_info:
        metadata["input_info"] = active_node_info
    return redacted, metadata


def _placeholder_for_value(value: str, variables: dict[str, Any]) -> str | None:
    for name, var_value in variables.items():
        if value == str(var_value):
            return f"{{{{{name}}}}}"
    return None


def _placeholder_for_node(node_info: dict[str, str] | None) -> str:
    label = (node_info or {}).get("label", "")
    return f"{{{{{_normalize_variable_name(label) or 'secret'}}}}}"


def _normalize_variable_name(name: str) -> str:
    normalized = re.sub(r"[^a-zA-Z0-9]+", "_", name.strip().lower()).strip("_")
    if not normalized:
        return ""
    if normalized[0].isdigit():
        normalized = f"v_{normalized}"
    return normalized


def _normalize_variables(variables: dict[str, Any]) -> dict[str, Any]:
    normalized = {}
    for key, value in variables.items():
        safe_key = _normalize_variable_name(str(key))
        if safe_key:
            normalized[safe_key] = value
        elif str(key):
            normalized[str(key)] = value
    return normalized


def _is_sensitive_input(node_info: dict[str, str] | None) -> bool:
    if not node_info:
        return False
    haystack = f"{node_info.get('role', '')} {node_info.get('label', '')}".lower()
    return any(part in haystack for part in _sensitive_label_parts())


def _redact_all_text_enabled() -> bool:
    return os.environ.get("TERX_REDACT_ALL_TEXT", "").lower() in {"1", "true", "yes", "on"}


def _sensitive_label_parts() -> tuple[str, ...]:
    extra = tuple(
        part.strip().lower()
        for part in os.environ.get("TERX_REDACT_FIELDS", "").split(",")
        if part.strip()
    )
    return SENSITIVE_LABEL_PARTS + extra


def _placeholders_in_commands(commands: list[CDPCommand]) -> list[str]:
    found: set[str] = set()
    for command in commands:
        for match in PLACEHOLDER_RE.finditer(json.dumps(command.params, sort_keys=True)):
            found.add(match.group(1))
    return sorted(found)


def _redacted_fields_in_commands(commands: list[CDPCommand]) -> list[str]:
    fields: set[str] = set()
    for command in commands:
        if not command.metadata.get("redacted"):
            continue
        placeholder = str(command.metadata.get("placeholder", ""))
        match = PLACEHOLDER_RE.fullmatch(placeholder)
        fields.add(match.group(1) if match else placeholder)
    return sorted(fields)


def _postcondition_summary(postcondition: Any) -> Any:
    if callable(postcondition):
        return getattr(postcondition, "__name__", "callable")
    return postcondition


async def _start_mutation_guard(bridge: CDPBridge) -> bool:
    expression = """
(() => {
  const key = "__TERX_MUTATION_GUARD__";
  if (window[key] && window[key].observer) {
    window[key].observer.disconnect();
  }
  const root = document.documentElement || document.body;
  if (!root) {
    return false;
  }
  const state = { count: 0, observer: null };
  const observer = new MutationObserver((mutations) => {
    for (const mutation of mutations) {
      state.count += mutation.addedNodes.length + mutation.removedNodes.length;
    }
  });
  observer.observe(root, { childList: true, subtree: true });
  state.observer = observer;
  window[key] = state;
  return true;
})()
"""
    try:
        result = await bridge.send_internal(
            "Runtime.evaluate", {"expression": expression, "returnByValue": True}
        )
        return bool(result.get("result", {}).get("value"))
    except Exception as exc:
        logger.debug("Mutation guard start failed: %s", exc)
        return False


async def _read_mutation_count(bridge: CDPBridge) -> int | None:
    expression = "window.__TERX_MUTATION_GUARD__ ? window.__TERX_MUTATION_GUARD__.count : 0"
    try:
        result = await bridge.send_internal(
            "Runtime.evaluate", {"expression": expression, "returnByValue": True}
        )
        value = result.get("result", {}).get("value", 0)
        return int(value or 0)
    except Exception as exc:
        logger.debug("Mutation guard read failed: %s", exc)
        return None


async def _stop_mutation_guard(bridge: CDPBridge) -> None:
    expression = """
(() => {
  const state = window.__TERX_MUTATION_GUARD__;
  if (state && state.observer) {
    state.observer.disconnect();
  }
  delete window.__TERX_MUTATION_GUARD__;
})()
"""
    try:
        await bridge.send_internal("Runtime.evaluate", {"expression": expression})
    except Exception as exc:
        logger.debug("Mutation guard stop failed: %s", exc)


def _translate_parameters(
    params: Any,
    metadata: dict,
    current_elements: list,
    value_map: dict,
    variables: dict[str, Any] | None = None,
) -> Any:
    """Recursively swap mapped values and translate old node IDs to new ones."""
    variables = variables or {}
    if isinstance(params, dict):
        new_dict = {}
        for k, v in params.items():
            if _is_hashable(v) and v in value_map:
                new_dict[k] = value_map[v]
                continue

            if k in ("backendNodeId", "nodeId") and isinstance(v, int):
                node_info = metadata.get("node_info", {})
                old_id_str = str(v)
                if old_id_str in node_info:
                    info = node_info[old_id_str]
                    role = info.get("role")
                    label = info.get("label")
                    matched_id = None
                    for el in current_elements:
                        if el.role == role and el.label == label:
                            matched_id = el.backend_dom_id
                            break
                    if matched_id is not None:
                        new_dict[k] = matched_id
                        value_map[v] = matched_id
                        continue

            new_dict[k] = _translate_parameters(v, metadata, current_elements, value_map, variables)
        return new_dict
    elif isinstance(params, list):
        return [
            _translate_parameters(item, metadata, current_elements, value_map, variables)
            for item in params
        ]
    elif isinstance(params, str):
        return _interpolate_placeholders(params, variables)
    else:
        if _is_hashable(params):
            return value_map.get(params, params)
        return params


def _is_hashable(value: Any) -> bool:
    try:
        hash(value)
        return True
    except TypeError:
        return False


def _interpolate_placeholders(value: str, variables: dict[str, Any]) -> str:
    def replace(match: re.Match[str]) -> str:
        name = match.group(1)
        if name not in variables:
            raise MissingReplayVariable(name)
        return str(variables[name])

    return PLACEHOLDER_RE.sub(replace, value)


async def _assert_postcondition(bridge: CDPBridge, postcondition: Any) -> None:
    if postcondition is None:
        return

    if callable(postcondition):
        result = postcondition(bridge)
        if inspect.isawaitable(result):
            result = await result
        if not result:
            raise PostconditionFailed("callable postcondition returned false")
        return

    if not isinstance(postcondition, dict):
        raise TypeError("postcondition must be a dict, callable, or None")

    checks: list[tuple[str, bool]] = []
    if expected := postcondition.get("url_contains"):
        result = await bridge.send_internal(
            "Runtime.evaluate", {"expression": "window.location.href", "returnByValue": True}
        )
        checks.append(("url_contains", expected in result.get("result", {}).get("value", "")))

    if expected := postcondition.get("title_contains"):
        result = await bridge.send_internal(
            "Runtime.evaluate", {"expression": "document.title", "returnByValue": True}
        )
        checks.append(("title_contains", expected in result.get("result", {}).get("value", "")))

    if expected := postcondition.get("text_contains"):
        result = await bridge.send_internal(
            "Runtime.evaluate", {"expression": "document.body?.innerText || ''", "returnByValue": True}
        )
        checks.append(("text_contains", expected in result.get("result", {}).get("value", "")))

    if selector := postcondition.get("selector_exists"):
        expression = f"Boolean(document.querySelector({json.dumps(selector)}))"
        result = await bridge.send_internal(
            "Runtime.evaluate", {"expression": expression, "returnByValue": True}
        )
        checks.append(("selector_exists", bool(result.get("result", {}).get("value"))))

    if expression := postcondition.get("js"):
        result = await bridge.send_internal(
            "Runtime.evaluate", {"expression": str(expression), "returnByValue": True}
        )
        checks.append(("js", bool(result.get("result", {}).get("value"))))

    if not checks:
        return

    failed = [name for name, ok in checks if not ok]
    if failed:
        raise PostconditionFailed(", ".join(failed))


def _discover_mappings(cached: Any, actual: Any, value_map: dict) -> None:
    """Compare cached vs actual responses to map dynamic identifiers (e.g. objectId)."""
    if isinstance(cached, dict) and isinstance(actual, dict):
        for k in cached:
            if k in actual:
                _discover_mappings(cached[k], actual[k], value_map)
    elif isinstance(cached, list) and isinstance(actual, list):
        for c_val, a_val in zip(cached, actual):
            _discover_mappings(c_val, a_val, value_map)
    elif type(cached) is type(actual):
        if isinstance(cached, (str, int)) and cached != actual:
            if isinstance(cached, int) and cached < 100:
                return
            if isinstance(cached, str) and (
                len(cached) < 4 or cached.lower() in ("true", "false", "null", "undefined")
            ):
                return
            value_map[cached] = actual


# Backwards compatibility alias
MuscleMemorycache = MemoryCache


class CacheReplayError(Exception):
    """Raised when a cached CDP command fails during replay (DOM drift)."""

    def __init__(self, failed_method: str) -> None:
        self.failed_method = failed_method
        super().__init__(f"Replay failed at CDP method: {failed_method}")


class MissingReplayVariable(Exception):
    """Raised when replay needs a `{{variable}}` value that was not supplied."""

    def __init__(self, variable_name: str) -> None:
        self.variable_name = variable_name
        super().__init__(f"Replay requires variable: {variable_name}")


class PostconditionFailed(Exception):
    """Raised when replay completes but the expected page state is not reached."""


class MutationDriftError(Exception):
    """Raised when replay causes unusually high DOM churn."""

    def __init__(self, count: int, threshold: int) -> None:
        self.count = count
        self.threshold = threshold
        super().__init__(
            f"Replay mutation drift detected: {count} DOM changes exceeded threshold {threshold}"
        )
