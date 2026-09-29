"""
TERX Memory Cache — the core caching engine.

Records successful semantic browser actions behind an explicit replay policy.
On cache hit TERX re-checks scope, conditions, and target identity before
executing the supported action set. On cache miss the agent runs normally;
unsupported raw CDP work is never persisted for replay.

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
from collections.abc import Awaitable, Callable
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from fnmatch import fnmatchcase
from pathlib import Path
from typing import Any, Literal
from urllib.parse import urlparse

from terx.cdp.bridge import CDPBridge
from terx.dom.extractor import DOMExtractor, DOMSnapshot, hash_similarity

logger = logging.getLogger(__name__)

# Cache hit threshold — role sequences more similar than this are treated as the same page.
# Scope, route, and workflow version must match before this fuzzy score is considered.
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

REPLAY_ACTIONS = {
    "TERX.navigate",
    "TERX.click",
    "TERX.type",
}
SAFE_CLICK_FUNCTIONS = {
    "function() { this.click(); }",
    "function(){this.click();}",
}
SIDE_EFFECTS = {"read_only", "mutating", "destructive"}
REPLAY_CONDITION_KEYS = {
    "url_contains",
    "title_contains",
    "text_contains",
    "selector_exists",
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
    """A persisted replay action.

    v0.4 stores only TERX's small semantic action set. ``method`` remains for
    backwards-compatible JSON decoding, but persisted values are ``TERX.*``
    actions rather than arbitrary CDP calls.
    """

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
    origin: str = ""
    route_pattern: str = "*"
    scope_hash: str = ""
    workflow_version: int = 1
    policy_json: str = "{}"
    expires_at: float | None = None


@dataclass(frozen=True)
class ReplayPolicy:
    """Explicit contract that makes a workflow eligible for replay.

    ``scope_id`` is never persisted in plaintext. Its SHA-256 digest binds a
    workflow to a caller-controlled account, tenant, environment, or fixture.
    """

    scope_id: str | None = None
    route_pattern: str | None = None
    workflow_version: int = 1
    side_effect: Literal["read_only", "mutating", "destructive"] = "mutating"
    precondition: dict[str, Any] | None = None
    postcondition: dict[str, Any] | None = None
    ttl_seconds: int | None = 86_400

    def scope_hash(self) -> str:
        if not self.scope_id:
            return ""
        return hashlib.sha256(self.scope_id.encode()).hexdigest()

    def cacheability_reasons(self) -> list[str]:
        reasons: list[str] = []
        if self.side_effect not in SIDE_EFFECTS:
            reasons.append(f"invalid side_effect: {self.side_effect}")
        if self.workflow_version < 1:
            reasons.append("workflow_version must be at least 1")
        if not self.scope_id:
            reasons.append("scope_id is required for replayable workflows")
        if not isinstance(self.precondition, dict) or not self.precondition:
            reasons.append("a dict precondition is required for replayable workflows")
        if not isinstance(self.postcondition, dict) or not self.postcondition:
            reasons.append("a dict postcondition is required for replayable workflows")
        for label, condition in (
            ("precondition", self.precondition),
            ("postcondition", self.postcondition),
        ):
            if not isinstance(condition, dict):
                continue
            unknown = sorted(set(condition) - REPLAY_CONDITION_KEYS)
            if unknown:
                reasons.append(f"{label} contains unsupported checks: {', '.join(unknown)}")
            non_string_values = sorted(
                key
                for key, value in condition.items()
                if key in REPLAY_CONDITION_KEYS and not isinstance(value, str)
            )
            if non_string_values:
                reasons.append(f"{label} values must be strings: {', '.join(non_string_values)}")
            empty_values = sorted(
                key
                for key, value in condition.items()
                if key in REPLAY_CONDITION_KEYS and isinstance(value, str) and not value
            )
            if empty_values:
                reasons.append(
                    f"{label} values must be non-empty strings: {', '.join(empty_values)}"
                )
        if self.ttl_seconds is not None and self.ttl_seconds <= 0:
            reasons.append("ttl_seconds must be positive or None")
        return reasons

    def fingerprint(self) -> str:
        """Hash the full replay contract without persisting condition values."""
        contract = {
            "route_pattern": self.route_pattern,
            "workflow_version": self.workflow_version,
            "side_effect": self.side_effect,
            "precondition": self.precondition,
            "postcondition": self.postcondition,
            "ttl_seconds": self.ttl_seconds,
        }
        encoded = json.dumps(contract, sort_keys=True, separators=(",", ":"), default=str).encode()
        return hashlib.sha256(encoded).hexdigest()

    def persisted_summary(self) -> dict[str, Any]:
        return {
            "route_pattern": self.route_pattern,
            "workflow_version": self.workflow_version,
            "side_effect": self.side_effect,
            "precondition_checks": sorted((self.precondition or {}).keys()),
            "postcondition_checks": sorted((self.postcondition or {}).keys()),
            "ttl_seconds": self.ttl_seconds,
            "fingerprint": self.fingerprint(),
        }


@dataclass(frozen=True)
class ReplayApprovalRequest:
    """Opaque approval data a host must verify and atomically consume.

    TERX never persists the token. A verifier receives enough immutable replay
    identity to reject a token approved for another task, scope, policy, or
    semantic command sequence.
    """

    token: str
    task_description: str
    scope_hash: str
    workflow_version: int
    policy_fingerprint: str
    origin: str
    route_pattern: str
    structural_hash: str
    command_digest: str
    requested_at: float


@dataclass(frozen=True)
class ApprovalDecision:
    """The only successful result accepted from an approval verifier."""

    approved: bool
    consumed: bool
    reason: str | None = None


ApprovalVerifier = Callable[[ReplayApprovalRequest], ApprovalDecision | Awaitable[ApprovalDecision]]


@dataclass
class ReplayDecision:
    """Machine-readable replay outcome for agents and operators."""

    status: Literal["hit", "miss", "refused", "failed"]
    reasons: list[str] = field(default_factory=list)
    workflow_version: int = 1
    scope_verified: bool = False
    preconditions_verified: bool = False
    postconditions_verified: bool = False


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
    status: str = "miss"
    refusal_reasons: list[str] = field(default_factory=list)
    workflow_version: int = 1
    route_pattern: str | None = None
    scope_verified: bool = False
    preconditions_verified: bool = False
    postconditions_verified: bool = False

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


class MemoryCache:
    """
    The TERX memory cache.

    Stores semantic action sequences keyed by scoped page identity and task.
    On hit: replays only after policy checks. On miss: observes the agent's
    browser work and stores it only if every action is replayable.

    Usage:
        cache = MemoryCache()

        async with session_for(
            cache, bridge, "login to salesforce", scope_id="tenant-a",
            precondition={"url_contains": "/login"},
            postcondition={"text_contains": "Welcome"},
        ) as ctx:
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

    # Schema v3 introduces an explicit replay scope and expires legacy raw-CDP
    # entries from normal replay eligibility.
    SCHEMA_VERSION = 3

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

            # Get current schema version.
            cursor = db.execute("SELECT MAX(version) FROM schema_version")
            row = cursor.fetchone()
            current_version = row[0] if row and row[0] is not None else 0

            if current_version == 0:
                self._create_sequences_table(db)

            if current_version < self.SCHEMA_VERSION:
                self._migrate_db(db, current_version)

            self._create_sequences_table(db)
            db.execute("CREATE INDEX IF NOT EXISTS idx_domain_task ON sequences(domain, task_key)")
            db.execute(
                "CREATE INDEX IF NOT EXISTS idx_scope_lookup "
                "ON sequences(domain, task_key, origin, scope_hash, workflow_version)"
            )
            db.commit()
            self._db = db
            return db

    @staticmethod
    def _create_sequences_table(db: sqlite3.Connection, table_name: str = "sequences") -> None:
        """Create the v3 replay table without weakening its scope identity."""
        db.execute(
            f"""
                CREATE TABLE IF NOT EXISTS {table_name} (
                    id               INTEGER PRIMARY KEY AUTOINCREMENT,
                    domain           TEXT    NOT NULL,
                    structural_hash  TEXT    NOT NULL,
                    task_key         TEXT    NOT NULL,
                    task_description TEXT    NOT NULL,
                    role_sequence    TEXT    NOT NULL DEFAULT '',
                    commands_json    TEXT    NOT NULL,
                    hit_count        INTEGER NOT NULL DEFAULT 0,
                    created_at       TEXT    NOT NULL,
                    last_used        TEXT    NOT NULL,
                    origin           TEXT    NOT NULL DEFAULT '',
                    route_pattern    TEXT    NOT NULL DEFAULT '*',
                    scope_hash       TEXT    NOT NULL DEFAULT '',
                    workflow_version INTEGER NOT NULL DEFAULT 1,
                    policy_json      TEXT    NOT NULL DEFAULT '{{}}',
                    expires_at       REAL,
                    UNIQUE(
                        domain, structural_hash, task_key, origin, route_pattern,
                        scope_hash, workflow_version
                    )
                )
            """
        )

    def _migrate_db(self, db: sqlite3.Connection, from_version: int) -> None:
        """Migrate database schema from from_version to SCHEMA_VERSION."""
        if from_version < 1:
            # Version 1: initial schema (already created above).
            pass

        if from_version < 2:
            # Version 2 had no schema change.
            pass

        if from_version < 3:
            # v2's UNIQUE(domain, structural_hash, task_key) cannot represent
            # safely scoped variants of a workflow. Copy legacy entries into the
            # v3 shape with an empty scope; v0.4 contexts require a non-empty
            # scope, so these entries remain inspectable but are not replayed.
            columns = {row[1] for row in db.execute("PRAGMA table_info(sequences)").fetchall()}
            if columns and "scope_hash" not in columns:
                self._create_sequences_table(db, "sequences_v3")
                db.execute(
                    """
                    INSERT INTO sequences_v3 (
                        id, domain, structural_hash, task_key, task_description,
                        role_sequence, commands_json, hit_count, created_at, last_used,
                        origin, route_pattern, scope_hash, workflow_version,
                        policy_json, expires_at
                    )
                    SELECT id, domain, structural_hash, task_key, task_description,
                           role_sequence, commands_json, hit_count, created_at, last_used,
                           '', '*', '', 1, '{"legacy": true}', 0
                    FROM sequences
                    """
                )
                db.execute("DROP TABLE sequences")
                db.execute("ALTER TABLE sequences_v3 RENAME TO sequences")
        # schema_version historically used version as its primary key, so
        # INSERT OR REPLACE created multiple rows instead of replacing the
        # current value. Normalize it to exactly one authoritative version.
        db.execute("DELETE FROM schema_version")
        db.execute("INSERT INTO schema_version (version) VALUES (?)", (self.SCHEMA_VERSION,))

        db.commit()
        logger.info(
            "Migrated cache database from version %d to %d", from_version, self.SCHEMA_VERSION
        )

    # ------------------------------------------------------------------ #
    # Core cache operations                                                 #
    # ------------------------------------------------------------------ #

    def lookup(
        self,
        domain: str,
        role_sequence: str,
        task_description: str,
        *,
        origin: str = "",
        route: str = "",
        scope_hash: str = "",
        workflow_version: int = 1,
        policy_fingerprint: str | None = None,
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
                "SELECT structural_hash, task_description, commands_json, hit_count, "
                "created_at, last_used, role_sequence, task_key, origin, route_pattern, "
                "scope_hash, workflow_version, policy_json, expires_at "
                "FROM sequences WHERE domain = ? AND task_key = ? "
                "AND origin = ? AND scope_hash = ? AND workflow_version = ?",
                (domain, task_key, origin, scope_hash, workflow_version),
            ).fetchall()

            best_match: tuple[float, Any] | None = None
            for row in rows:
                if not fnmatchcase(route, row[9]):
                    continue
                if row[13] is not None and row[13] <= time.time():
                    continue
                if policy_fingerprint is not None:
                    try:
                        stored_policy = json.loads(row[12])
                    except (TypeError, json.JSONDecodeError):
                        continue
                    if stored_policy.get("fingerprint") != policy_fingerprint:
                        continue
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
                origin=row[8],
                route_pattern=row[9],
                scope_hash=row[10],
                workflow_version=row[11],
                policy_json=row[12],
                expires_at=row[13],
            )

    def store(
        self,
        domain: str,
        structural_hash: str,
        role_sequence: str,
        task_description: str,
        commands: list[CDPCommand],
        *,
        origin: str = "",
        route_pattern: str = "*",
        scope_hash: str = "",
        workflow_version: int = 1,
        policy_json: str = "{}",
        ttl_seconds: int | None = None,
    ) -> None:
        """Persist a successful action sequence.

        Uses INSERT OR IGNORE to preserve the first successful sequence for each
        (domain, structural_hash, task_key). Subsequent runs with the same DOM
        structure will not overwrite the cached sequence.
        """
        with self._db_lock:
            db = self._ensure_db()
            now = datetime.now(timezone.utc).isoformat()
            commands_json = json.dumps([_sanitize_command_for_storage(c) for c in commands])
            task_key = _task_key(task_description)
            expires_at = time.time() + ttl_seconds if ttl_seconds is not None else None

            db.execute(
                """
                INSERT OR IGNORE INTO sequences
                    (domain, structural_hash, task_key, task_description, role_sequence,
                     commands_json, hit_count, created_at, last_used, origin,
                     route_pattern, scope_hash, workflow_version, policy_json, expires_at)
                VALUES (?, ?, ?, ?, ?, ?, 0, ?, ?, ?, ?, ?, ?, ?, ?)
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
                    origin,
                    route_pattern,
                    scope_hash,
                    workflow_version,
                    policy_json,
                    expires_at,
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

    def increment_hit(
        self,
        domain: str,
        structural_hash: str,
        task_key: str,
        *,
        origin: str = "",
        route_pattern: str = "*",
        scope_hash: str = "",
        workflow_version: int = 1,
    ) -> None:
        """Increment one exact scoped workflow's hit counter."""
        with self._db_lock:
            db = self._ensure_db()
            now = datetime.now(timezone.utc).isoformat()
            db.execute(
                "UPDATE sequences SET hit_count = hit_count + 1, last_used = ? "
                "WHERE domain = ? AND structural_hash = ? AND task_key = ? AND origin = ? "
                "AND route_pattern = ? AND scope_hash = ? AND workflow_version = ?",
                (
                    now,
                    domain,
                    structural_hash,
                    task_key,
                    origin,
                    route_pattern,
                    scope_hash,
                    workflow_version,
                ),
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
                safe_command = _sanitize_command_for_storage(cmd)
                frame = {
                    "type": "frame",
                    "data": {
                        "node_name": safe_command["method"].replace(".", "_").lower(),
                        "input_state": {
                            "cdp_method": safe_command["method"],
                            "cdp_params": safe_command["params"],
                            "frame_index": i,
                        },
                        "output_state": {
                            "cdp_result": safe_command["result"],
                        },
                        "metadata": {
                            "latency_ms": safe_command["latency_ms"],
                            "cache_hit": was_cache_hit,
                            "cdp_method": safe_command["method"],
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

    Records a deliberately small semantic action set from a CDP bridge.

    Raw CDP is an implementation detail of a browser agent, not a stable or
    safe replay format. v0.4 translates only navigation, labelled clicks, and
    labelled text entry into TERX actions. Any other mutating action makes the
    run non-cacheable; it is still allowed to execute on the cold path.
    """

    def __init__(
        self,
        cache: MemoryCache,
        bridge: CDPBridge,
        task: str,
        session_id: str | None = None,
        variables: dict[str, Any] | None = None,
        scope_id: str | None = None,
        route_pattern: str | None = None,
        workflow_version: int = 1,
        side_effect: Literal["read_only", "mutating", "destructive"] = "mutating",
        precondition: dict[str, Any] | None = None,
        postcondition: dict[str, Any] | Any | None = None,
        ttl_seconds: int | None = 86_400,
        redact_secrets: bool = True,
        mutation_guard: bool = True,
        mutation_threshold: int = 20,
        approval_verifier: ApprovalVerifier | None = None,
    ) -> None:
        self._cache = cache
        self._bridge = bridge
        self._task = task
        self._session_id = session_id or f"browser_session_{int(time.time())}"
        self._variables = _normalize_variables(variables or {})
        self._policy = ReplayPolicy(
            scope_id=scope_id,
            route_pattern=route_pattern,
            workflow_version=workflow_version,
            side_effect=side_effect,
            precondition=precondition,
            postcondition=postcondition if isinstance(postcondition, dict) else None,
            ttl_seconds=ttl_seconds,
        )
        self._postcondition = postcondition
        self._redact_secrets = redact_secrets
        self._mutation_guard = mutation_guard
        self._mutation_threshold = mutation_threshold
        self._approval_verifier = approval_verifier
        self._snapshot: DOMSnapshot | None = None
        self._domain: str = "unknown"
        self._origin: str = ""
        self._route: str = "/"
        self._route_pattern: str = "*"
        self._cached_seq: CachedSequence | None = None
        self._run_number: int = 1
        self._recorded_commands: list[CDPCommand] = []
        self._active_target: dict[str, str] | None = None
        self._object_targets: dict[str, dict[str, str]] = {}
        self._unsafe_reasons: list[str] = []
        self._recording_precondition_verified = False
        self.ledger: ReplayCostLedger | None = None
        self.report: ReplayReport | None = None
        self.decision = ReplayDecision(status="miss", workflow_version=workflow_version)

    @property
    def hit(self) -> bool:
        return self._cached_seq is not None

    @property
    def recorded_commands(self) -> int:
        return len(self._recorded_commands)

    async def replay(
        self,
        variables: dict[str, Any] | None = None,
        *,
        approval_token: str | None = None,
    ) -> None:
        """Replay a cached semantic workflow after policy verification.

        TERX does not invoke an LLM or self-healer during replay. A destructive
        workflow needs a host verifier that atomically consumes an approval for
        this replay identity; a bare token is never trusted.
        """
        if self._cached_seq is None:
            raise RuntimeError("No cached sequence to replay (cache miss)")

        t0 = time.perf_counter()
        replay_variables = {**self._variables, **_normalize_variables(variables or {})}
        mutation_guard_started = False
        mutation_count: int | None = None

        try:
            try:
                await _assert_postcondition(self._bridge, self._policy.precondition)
            except PostconditionFailed as exc:
                self._refuse(f"precondition failed: {exc}")
            self.decision.preconditions_verified = True

            if self._policy.side_effect == "destructive":
                await self._verify_destructive_approval(approval_token)

            if self._mutation_guard:
                mutation_guard_started = await _start_mutation_guard(self._bridge)

            for cmd in self._cached_seq.commands:
                try:
                    await _execute_replay_action(self._bridge, cmd, replay_variables)
                except MissingReplayVariable as exc:
                    self._refuse(f"missing replay variable: {exc.variable_name}")
                except ReplayRefused as exc:
                    self._refuse(exc.reason)
                except Exception as exc:
                    self.decision.status = "failed"
                    self.decision.reasons = [f"replay failed at {cmd.method}: {exc}"]
                    raise CacheReplayError(cmd.method) from exc

            if mutation_guard_started:
                mutation_count = await _read_mutation_count(self._bridge)
                if mutation_count is not None and mutation_count > self._mutation_threshold:
                    raise MutationDriftError(mutation_count, self._mutation_threshold)

            await _assert_postcondition(self._bridge, self._postcondition)
            self.decision.postconditions_verified = True
        finally:
            if mutation_guard_started:
                await _stop_mutation_guard(self._bridge)

        await self._run_ssim_audit()
        latency = (time.perf_counter() - t0) * 1000
        self._cache.increment_hit(
            self._domain,
            self._cached_seq.structural_hash,
            self._cached_seq.task_key,
            origin=self._cached_seq.origin,
            route_pattern=self._cached_seq.route_pattern,
            scope_hash=self._cached_seq.scope_hash,
            workflow_version=self._cached_seq.workflow_version,
        )
        self.decision.status = "hit"
        self.decision.scope_verified = True
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
            status="hit",
            workflow_version=self._policy.workflow_version,
            route_pattern=self._route_pattern,
            scope_verified=True,
            preconditions_verified=True,
            postconditions_verified=True,
        )
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

    def _refuse(self, reason: str) -> None:
        self.decision.status = "refused"
        self.decision.reasons = [reason]
        self.report = ReplayReport(
            task_description=self._task,
            domain=self._domain,
            cache_hit=True,
            status="refused",
            refusal_reasons=[reason],
            workflow_version=self._policy.workflow_version,
            route_pattern=self._route_pattern,
            scope_verified=True,
        )
        raise ReplayRefused(reason)

    async def _verify_destructive_approval(self, approval_token: str | None) -> None:
        """Require a host to validate and consume a destructive replay approval."""
        if not isinstance(approval_token, str) or not approval_token.strip():
            self._refuse("destructive replay requires approval_token")
        if self._approval_verifier is None:
            self._refuse("destructive replay requires an approval_verifier")

        request = ReplayApprovalRequest(
            token=approval_token,
            task_description=self._task,
            scope_hash=self._policy.scope_hash(),
            workflow_version=self._policy.workflow_version,
            policy_fingerprint=self._policy.fingerprint(),
            origin=self._origin,
            route_pattern=self._route_pattern,
            structural_hash=self._cached_seq.structural_hash,
            command_digest=_replay_command_digest(self._cached_seq.commands),
            requested_at=time.time(),
        )
        try:
            decision = self._approval_verifier(request)
            if inspect.isawaitable(decision):
                decision = await decision
        except Exception as exc:
            self._refuse(f"approval verification failed: {exc}")

        if not isinstance(decision, ApprovalDecision):
            self._refuse("approval_verifier must return ApprovalDecision")
        if not decision.approved:
            self._refuse(decision.reason or "destructive replay approval was rejected")
        if not decision.consumed:
            self._refuse(decision.reason or "destructive replay approval was not consumed")

    def record_command(self, cmd: CDPCommand) -> None:
        """Record a pre-built TERX semantic action for advanced integrations."""
        if cmd.method not in REPLAY_ACTIONS:
            self._unsafe_reasons.append(f"unsupported manual action: {cmd.method}")
            return
        self._recorded_commands.append(cmd)

    def _auto_record(self, method: str, params: dict, result: dict, latency: float) -> None:
        """Translate known CDP gestures into safe semantic replay actions."""
        if method == "Page.navigate":
            url = params.get("url")
            if isinstance(url, str) and _is_replayable_url(url):
                self._recorded_commands.append(
                    CDPCommand("TERX.navigate", {"url": url}, {}, latency)
                )
            else:
                self._unsafe_reasons.append("navigation URL is not replayable")
            return

        if method in {"DOM.focus", "DOM.resolveNode"}:
            target = _target_for_backend_node(params.get("backendNodeId"), self._snapshot)
            if target is None:
                self._unsafe_reasons.append(f"{method} target is not uniquely identifiable")
                return
            if method == "DOM.focus":
                self._active_target = target
            else:
                object_id = result.get("object", {}).get("objectId")
                if isinstance(object_id, str):
                    self._object_targets[object_id] = target
            return

        if method == "Input.insertText":
            if self._active_target is None:
                self._unsafe_reasons.append("text input has no preceding labelled focus")
                return
            recorded_params, metadata = _prepare_recorded_params(
                method=method,
                params={"text": params.get("text")},
                active_node_info=self._active_target,
                variables=self._variables,
                redact_secrets=self._redact_secrets,
            )
            text = recorded_params.get("text")
            if not isinstance(text, str):
                self._unsafe_reasons.append("text input is not a string")
                return
            if (
                PLACEHOLDER_RE.fullmatch(text) is None
                or metadata.get("placeholder_source") != "variable"
            ):
                self._unsafe_reasons.append(
                    "typed text must be supplied as a named variable before it can be replayed"
                )
                return
            self._recorded_commands.append(
                CDPCommand(
                    "TERX.type",
                    {"target": self._active_target, "text": text},
                    {},
                    latency,
                    metadata,
                )
            )
            return

        if method == "Runtime.callFunctionOn":
            target = self._object_targets.get(str(params.get("objectId", "")))
            function = str(params.get("functionDeclaration", "")).strip()
            if target is not None and function in SAFE_CLICK_FUNCTIONS:
                self._recorded_commands.append(
                    CDPCommand("TERX.click", {"target": target}, {}, latency)
                )
            else:
                self._unsafe_reasons.append("arbitrary JavaScript cannot be replayed")
            return

        if method in MUTATING_CDP_METHODS:
            self._unsafe_reasons.append(f"unsupported mutating CDP action: {method}")

    async def __aenter__(self) -> "RecordingContext":
        # Capture DOM snapshot asynchronously on enter
        extractor = DOMExtractor()
        self._snapshot = await extractor.snapshot(self._bridge)
        self._domain = urlparse(self._snapshot.url).netloc or "unknown"
        parsed = urlparse(self._snapshot.url)
        self._origin = (
            f"{parsed.scheme}://{parsed.netloc}" if parsed.scheme and parsed.netloc else ""
        )
        self._route = parsed.path or "/"
        self._route_pattern = self._policy.route_pattern or self._route

        # Update session_id with domain if using default
        if self._session_id.startswith("browser_session_"):
            self._session_id = f"browser_{self._domain}_{self._session_id.split('_')[-1]}"

        # A policy is mandatory before a sequence can enter, or be read from,
        # the replay cache. Cold-path browser work remains available without it.
        cacheability_reasons = self._policy.cacheability_reasons()
        if cacheability_reasons:
            self.decision.reasons = cacheability_reasons
        else:
            try:
                await _assert_postcondition(self._bridge, self._policy.precondition)
                self._recording_precondition_verified = True
            except PostconditionFailed as exc:
                # Cold-path work can continue, but its outcome must not become
                # a replayable workflow from an unexpected starting state.
                self._unsafe_reasons.append(f"precondition failed at recording: {exc}")
            self._cached_seq = self._cache.lookup(
                self._domain,
                self._snapshot.role_sequence,
                self._task,
                origin=self._origin,
                route=self._route,
                scope_hash=self._policy.scope_hash(),
                workflow_version=self._policy.workflow_version,
                policy_fingerprint=self._policy.fingerprint(),
            )
            self.decision.scope_verified = self._cached_seq is not None

        # Cold miss is run #1. Existing sequence + previous hits gives repeat count.
        with self._cache._db_lock:
            db = self._cache._ensure_db()
            task_key = _task_key(self._task)
            self._run_number = db.execute(
                "SELECT COALESCE(SUM(hit_count), 0) + COUNT(*) + 1 "
                "FROM sequences WHERE domain = ? AND task_key = ? AND origin = ? AND scope_hash = ?",
                (self._domain, task_key, self._origin, self._policy.scope_hash()),
            ).fetchone()[0]

        if not self.hit:
            self._bridge.add_recorder(self._auto_record)
        return self

    async def __aexit__(self, exc_type: Any, *_: Any) -> None:
        if not self.hit:
            self._bridge.remove_recorder(self._auto_record)

        if exc_type is not None:
            return  # Don't cache failed runs

        if not self.hit and not self._recorded_commands:
            await _assert_postcondition(self._bridge, self._postcondition)
            self.decision.postconditions_verified = True
            reasons = [
                *self._policy.cacheability_reasons(),
                *self._unsafe_reasons,
                "workflow contains no replayable TERX actions",
            ]
            self.decision.status = "refused"
            self.decision.reasons = reasons
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
                status="refused",
                refusal_reasons=reasons,
                workflow_version=self._policy.workflow_version,
                route_pattern=self._route_pattern,
                postcondition=_postcondition_summary(self._postcondition),
                preconditions_verified=self._recording_precondition_verified,
                postconditions_verified=True,
            )

        if not self.hit and self._recorded_commands:
            await _assert_postcondition(self._bridge, self._postcondition)
            self.decision.postconditions_verified = True
            reasons = [*self._policy.cacheability_reasons(), *self._unsafe_reasons]
            if reasons:
                self.decision.status = "refused"
                self.decision.reasons = reasons
            else:
                self._cache.store(
                    domain=self._domain,
                    structural_hash=self._snapshot.structural_hash,
                    role_sequence=self._snapshot.role_sequence,
                    task_description=self._task,
                    commands=self._recorded_commands,
                    origin=self._origin,
                    route_pattern=self._route_pattern,
                    scope_hash=self._policy.scope_hash(),
                    workflow_version=self._policy.workflow_version,
                    policy_json=json.dumps(self._policy.persisted_summary(), sort_keys=True),
                    ttl_seconds=self._policy.ttl_seconds,
                )
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
                status=self.decision.status,
                refusal_reasons=list(self.decision.reasons),
                workflow_version=self._policy.workflow_version,
                route_pattern=self._route_pattern,
                scope_verified=not bool(self._policy.cacheability_reasons()),
                preconditions_verified=self._recording_precondition_verified,
                postconditions_verified=self.decision.postconditions_verified,
            )


def session_for(
    cache: MemoryCache,
    bridge: CDPBridge,
    task: str,
    session_id: str | None = None,
    variables: dict[str, Any] | None = None,
    scope_id: str | None = None,
    route_pattern: str | None = None,
    workflow_version: int = 1,
    side_effect: Literal["read_only", "mutating", "destructive"] = "mutating",
    precondition: dict[str, Any] | None = None,
    postcondition: dict[str, Any] | Any | None = None,
    ttl_seconds: int | None = 86_400,
    redact_secrets: bool = True,
    mutation_guard: bool = True,
    mutation_threshold: int = 20,
    approval_verifier: ApprovalVerifier | None = None,
) -> RecordingContext:
    """
    Factory: create a RecordingContext for a task on the current page.

    A scope ID and non-empty pre/postcondition dictionaries are required for a
    workflow to be cacheable. Without them, the context remains cold-path only.

    Example:
        async with session_for(
            cache, bridge, "login to salesforce", scope_id="tenant-a",
            precondition={"url_contains": "/login"},
            postcondition={"text_contains": "Welcome"},
        ) as ctx:
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
        scope_id=scope_id,
        route_pattern=route_pattern,
        workflow_version=workflow_version,
        side_effect=side_effect,
        precondition=precondition,
        postcondition=postcondition,
        ttl_seconds=ttl_seconds,
        redact_secrets=redact_secrets,
        mutation_guard=mutation_guard,
        mutation_threshold=mutation_threshold,
        approval_verifier=approval_verifier,
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


def _is_replayable_url(url: str) -> bool:
    """Allow only credential- and query-free web navigation in replay data."""
    parsed = urlparse(url)
    return (
        parsed.scheme.lower() in {"http", "https"}
        and bool(parsed.netloc)
        and parsed.username is None
        and parsed.password is None
        and not parsed.query
        and not parsed.fragment
    )


def _target_for_backend_node(
    backend_node_id: Any, snapshot: DOMSnapshot | None
) -> dict[str, str] | None:
    """Create a durable action target from a unique labelled AX element."""
    if not isinstance(backend_node_id, int) or snapshot is None:
        return None
    matches = [
        element for element in snapshot.elements if element.backend_dom_id == backend_node_id
    ]
    if len(matches) != 1:
        return None
    element = matches[0]
    if not element.semantic_name:
        return None
    return {"role": element.role, "label": element.semantic_name}


def _find_replay_target(snapshot: DOMSnapshot, target: Any) -> Any:
    """Resolve a target only when role and accessible name identify one node."""
    if not isinstance(target, dict):
        raise ReplayRefused("invalid semantic target")
    role = target.get("role")
    label = target.get("label")
    if not isinstance(role, str) or not isinstance(label, str) or not label:
        raise ReplayRefused("semantic target requires role and label")
    matches = [
        element
        for element in snapshot.elements
        if element.role == role and element.semantic_name == label
    ]
    if len(matches) != 1:
        raise ReplayRefused(
            f"semantic target is {'missing' if not matches else 'ambiguous'}: {role} {label!r}"
        )
    return matches[0]


async def _execute_replay_action(
    bridge: CDPBridge, command: CDPCommand, variables: dict[str, Any]
) -> None:
    """Execute one TERX semantic action without triggering bridge recorders."""
    if command.method not in REPLAY_ACTIONS:
        raise ReplayRefused(f"unsupported cached action: {command.method}")
    if command.method == "TERX.navigate":
        url = command.params.get("url")
        if not isinstance(url, str) or not _is_replayable_url(url):
            raise ReplayRefused("cached navigation URL is not permitted")
        await bridge.send_internal("Page.navigate", {"url": url})
        await bridge.wait_for_load(timeout=10.0)
        return

    snapshot = await DOMExtractor().snapshot(bridge)
    element = _find_replay_target(snapshot, command.params.get("target"))
    if command.method == "TERX.type":
        text = command.params.get("text")
        if not isinstance(text, str):
            raise ReplayRefused("cached text action has no string value")
        text = _interpolate_placeholders(text, variables)
        await bridge.send_internal("DOM.focus", {"backendNodeId": element.backend_dom_id})
        await bridge.send_internal("Input.insertText", {"text": text})
        return

    resolved = await bridge.send_internal(
        "DOM.resolveNode", {"backendNodeId": element.backend_dom_id}
    )
    object_id = resolved.get("object", {}).get("objectId")
    if not isinstance(object_id, str) or not object_id:
        raise ReplayRefused("could not resolve semantic click target")
    await bridge.send_internal(
        "Runtime.callFunctionOn",
        {"objectId": object_id, "functionDeclaration": "function() { this.click(); }"},
    )


def _is_sensitive_key(key: str) -> bool:
    normalized = key.lower().replace("_", " ").replace("-", " ")
    return any(part in normalized for part in _sensitive_label_parts())


def _sanitize_value(value: Any, key_hint: str = "") -> Any:
    """Drop secrets from persistence and audit output at every boundary."""
    if _is_sensitive_key(key_hint):
        return "{{redacted}}"
    if isinstance(value, dict):
        return {str(key): _sanitize_value(item, str(key)) for key, item in value.items()}
    if isinstance(value, list):
        return [_sanitize_value(item, key_hint) for item in value]
    if isinstance(value, tuple):
        return [_sanitize_value(item, key_hint) for item in value]
    return value


def _sanitize_command_for_storage(command: CDPCommand) -> dict[str, Any]:
    """Serialize only replayable semantic actions with no response payloads."""
    if command.method not in REPLAY_ACTIONS:
        raise ValueError(f"TERX only persists semantic replay actions, got {command.method}")
    if command.method == "TERX.navigate":
        if not isinstance(command.params.get("url"), str) or not _is_replayable_url(
            command.params["url"]
        ):
            raise ValueError("TERX.navigate requires an http or https URL")
    else:
        target = command.params.get("target")
        if not isinstance(target, dict) or not all(
            isinstance(target.get(field), str) and target[field] for field in ("role", "label")
        ):
            raise ValueError(f"{command.method} requires a labelled semantic target")
        if command.method == "TERX.type":
            text = command.params.get("text")
            if not isinstance(text, str) or PLACEHOLDER_RE.fullmatch(text) is None:
                raise ValueError("TERX.type requires a named variable placeholder")
    metadata = {
        key: value
        for key, value in _sanitize_value(command.metadata).items()
        if key in {"redacted", "placeholder", "placeholder_source"}
    }
    return {
        "method": command.method,
        "params": _sanitize_value(command.params),
        "result": {},
        "latency_ms": command.latency_ms,
        "metadata": metadata,
    }


def _replay_command_digest(commands: list[CDPCommand]) -> str:
    """Return a stable, secret-safe identity for selected replay actions.

    The cache has already validated these commands as TERX's semantic IR. The
    digest intentionally excludes transient result payloads, latency, and
    recording metadata: only executable method/parameter pairs identify the
    destructive sequence a host is being asked to approve.
    """
    executable_actions = [
        {"method": command.method, "params": _sanitize_value(command.params)}
        for command in commands
    ]
    canonical = json.dumps(
        executable_actions,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    ).encode()
    return hashlib.sha256(canonical).hexdigest()


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
    if "url_contains" in postcondition:
        expected = postcondition["url_contains"]
        if not isinstance(expected, str) or not expected:
            raise PostconditionFailed("url_contains requires a non-empty string")
        result = await bridge.send_internal(
            "Runtime.evaluate", {"expression": "window.location.href", "returnByValue": True}
        )
        checks.append(("url_contains", expected in result.get("result", {}).get("value", "")))

    if "title_contains" in postcondition:
        expected = postcondition["title_contains"]
        if not isinstance(expected, str) or not expected:
            raise PostconditionFailed("title_contains requires a non-empty string")
        result = await bridge.send_internal(
            "Runtime.evaluate", {"expression": "document.title", "returnByValue": True}
        )
        checks.append(("title_contains", expected in result.get("result", {}).get("value", "")))

    if "text_contains" in postcondition:
        expected = postcondition["text_contains"]
        if not isinstance(expected, str) or not expected:
            raise PostconditionFailed("text_contains requires a non-empty string")
        result = await bridge.send_internal(
            "Runtime.evaluate",
            {"expression": "document.body?.innerText || ''", "returnByValue": True},
        )
        checks.append(("text_contains", expected in result.get("result", {}).get("value", "")))

    if "selector_exists" in postcondition:
        selector = postcondition["selector_exists"]
        if not isinstance(selector, str) or not selector:
            raise PostconditionFailed("selector_exists requires a non-empty string")
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


# Backwards compatibility alias
MuscleMemorycache = MemoryCache


class CacheReplayError(Exception):
    """Raised when a cached CDP command fails during replay (DOM drift)."""

    def __init__(self, failed_method: str) -> None:
        self.failed_method = failed_method
        super().__init__(f"Replay failed at CDP method: {failed_method}")


class ReplayRefused(Exception):
    """Raised when TERX deliberately declines an unsafe or ambiguous replay."""

    def __init__(self, reason: str) -> None:
        self.reason = reason
        super().__init__(f"Replay refused: {reason}")


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
