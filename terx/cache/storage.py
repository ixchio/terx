"""Local SQLite storage for replay sequences, saved tools, scope digests, and audit files."""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
import re
import secrets
import sqlite3
import threading
import time
import uuid
from datetime import datetime, timezone
from fnmatch import fnmatchcase
from pathlib import Path
from typing import Any

from terx.dom.extractor import hash_similarity
from terx.tools import SavedTool, ToolManifestError
from terx.cache.models import CDPCommand, CachedSequence
from terx.cache.redaction import _sanitize_command_for_storage


logger = logging.getLogger(__name__)


# Cache hit threshold — role sequences more similar than this are treated as the same page.
# Scope, route, and workflow version must match before this fuzzy score is considered.
SIMILARITY_THRESHOLD = 0.85


AUDIT_DIR = Path(".terx/audit")


SCOPE_KEY_FILENAME = "scope.key"


_SCOPE_DIGEST_CONTEXT = b"terx-scope-v1:"


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
        *,
        scope_key: bytes | None = None,
    ) -> None:
        """Create a cache.

        ``scope_key`` keys scope digests. When omitted, a random key is created
        once at ``<db dir>/scope.key`` (mode 0600). Pass the same key to every
        process that must share replay entries.
        """
        if scope_key is not None and len(scope_key) < 32:
            raise ValueError("scope_key must be at least 32 bytes")
        self.db_path = Path(db_path)
        self.audit_dir = Path(audit_dir)
        self.similarity_threshold = similarity_threshold
        self._scope_key = scope_key
        self._adopted_scopes: set[str] = set()
        self._db: sqlite3.Connection | None = None
        self._db_lock = threading.RLock()  # Reentrant: public methods call _ensure_db().

    # ------------------------------------------------------------------ #
    # Scope digests                                                         #
    # ------------------------------------------------------------------ #

    def scope_digest(self, scope_id: str | None) -> str:
        """Return the keyed digest the cache stores for ``scope_id``.

        An unkeyed hash of a guessable scope such as ``acme-prod:billing`` can
        be reversed by trying candidates; an HMAC cannot without the local key,
        so a digest shown through MCP or ``terx inspect`` reveals nothing.
        """
        if not scope_id:
            return ""
        return hmac.new(
            self._get_scope_key(), _SCOPE_DIGEST_CONTEXT + scope_id.encode(), hashlib.sha256
        ).hexdigest()

    def _get_scope_key(self) -> bytes:
        with self._db_lock:
            if self._scope_key is None:
                self._scope_key = _load_or_create_scope_key(
                    self.db_path.parent / SCOPE_KEY_FILENAME
                )
            return self._scope_key

    def adopt_legacy_scope(self, scope_id: str | None) -> int:
        """Re-key pre-HMAC entries for a scope the caller has just presented.

        Earlier releases stored ``sha256(scope_id)``. The raw scope is needed to
        compute the new digest, so entries are upgraded lazily, the first time a
        caller supplies that scope. Returns the number of rows re-keyed.
        """
        if not scope_id or scope_id in self._adopted_scopes:
            return 0
        legacy = hashlib.sha256(scope_id.encode()).hexdigest()
        digest = self.scope_digest(scope_id)
        with self._db_lock:
            db = self._ensure_db()
            changed = db.execute(
                "UPDATE OR IGNORE sequences SET scope_hash = ? WHERE scope_hash = ?",
                (digest, legacy),
            ).rowcount
            # Rows left behind collided with an entry already under the new
            # digest, so they are superseded duplicates.
            db.execute("DELETE FROM sequences WHERE scope_hash = ?", (legacy,))
            changed += db.execute(
                "UPDATE saved_tools SET scope_hash = ? WHERE scope_hash = ?", (digest, legacy)
            ).rowcount
            db.commit()
            self._adopted_scopes.add(scope_id)
        if changed:
            logger.info("Re-keyed %d legacy cache rows to an HMAC scope digest", changed)
        return changed

    # ------------------------------------------------------------------ #
    # Setup                                                                 #
    # ------------------------------------------------------------------ #

    # Schema v4 adds data-only saved tool manifests. Their scope is stored only
    # as a digest; every invocation supplies and verifies the raw scope again.
    SCHEMA_VERSION = 4

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
            self._create_tools_table(db)
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

    @staticmethod
    def _create_tools_table(db: sqlite3.Connection) -> None:
        """Create compact manifests for MCP-discoverable saved tools."""
        db.execute(
            """
                CREATE TABLE IF NOT EXISTS saved_tools (
                    name                 TEXT PRIMARY KEY,
                    description          TEXT NOT NULL,
                    task_description     TEXT NOT NULL,
                    input_names_json     TEXT NOT NULL,
                    scope_hash           TEXT NOT NULL,
                    precondition_json    TEXT NOT NULL,
                    postcondition_json   TEXT NOT NULL,
                    route_pattern        TEXT,
                    workflow_version     INTEGER NOT NULL,
                    side_effect          TEXT NOT NULL,
                    ttl_seconds          INTEGER,
                    mutation_guard       INTEGER NOT NULL,
                    mutation_threshold   INTEGER NOT NULL,
                    result_spec_json     TEXT NOT NULL,
                    created_at           TEXT NOT NULL,
                    updated_at           TEXT NOT NULL
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
        if from_version < 4:
            self._create_tools_table(db)
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
    ) -> bool:
        """Persist a successful action sequence; return whether it was written.

        The first successful sequence for a scoped cache key is preserved while
        it is live and its policy is unchanged. An expired entry, or one whose
        persisted policy (and therefore fingerprint) differs, can never be
        replayed, so a new successful recording replaces it instead of being
        silently ignored.
        """
        with self._db_lock:
            db = self._ensure_db()
            now = datetime.now(timezone.utc).isoformat()
            commands_json = json.dumps([_sanitize_command_for_storage(c) for c in commands])
            task_key = _task_key(task_description)
            current_time = time.time()
            expires_at = current_time + ttl_seconds if ttl_seconds is not None else None

            cursor = db.execute(
                """
                INSERT INTO sequences
                    (domain, structural_hash, task_key, task_description, role_sequence,
                     commands_json, hit_count, created_at, last_used, origin,
                     route_pattern, scope_hash, workflow_version, policy_json, expires_at)
                VALUES (?, ?, ?, ?, ?, ?, 0, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(
                    domain, structural_hash, task_key, origin, route_pattern,
                    scope_hash, workflow_version
                ) DO UPDATE SET
                    task_description = excluded.task_description,
                    role_sequence    = excluded.role_sequence,
                    commands_json    = excluded.commands_json,
                    hit_count        = 0,
                    created_at       = excluded.created_at,
                    last_used        = excluded.last_used,
                    policy_json      = excluded.policy_json,
                    expires_at       = excluded.expires_at
                WHERE (sequences.expires_at IS NOT NULL AND sequences.expires_at <= ?)
                   OR sequences.policy_json != excluded.policy_json
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
                    current_time,
                ),
            )
            written = cursor.rowcount > 0
            try:
                db.commit()
            except sqlite3.Error as e:
                logger.error("Failed to commit cache sequence: %s", e)
                raise RuntimeError(f"Cache storage failed: {e}") from e
            if written:
                logger.info(
                    "Cached %d commands for domain=%s task=%s hash=%.8s",
                    len(commands),
                    domain,
                    task_key,
                    structural_hash,
                )
            else:
                logger.info(
                    "Kept existing live sequence for domain=%s task=%s hash=%.8s",
                    domain,
                    task_key,
                    structural_hash,
                )
            return written

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

    def invalidate(self, domain: str, *, scope_hash: str | None = None) -> int:
        """Remove cached sequences for a domain, optionally only one scope's.

        Returns rows deleted. Untrusted callers (such as MCP clients) should
        always pass ``scope_hash`` so they cannot wipe other tenants' entries.
        """
        with self._db_lock:
            db = self._ensure_db()
            if scope_hash is None:
                cursor = db.execute("DELETE FROM sequences WHERE domain = ?", (domain,))
            else:
                cursor = db.execute(
                    "DELETE FROM sequences WHERE domain = ? AND scope_hash = ?",
                    (domain, scope_hash),
                )
            db.commit()
            return cursor.rowcount

    def has_workflow(
        self,
        task_description: str,
        *,
        scope_hash: str,
        workflow_version: int,
        policy_fingerprint: str,
    ) -> bool:
        """Return whether a live recorded workflow matches this exact contract."""
        with self._db_lock:
            db = self._ensure_db()
            rows = db.execute(
                "SELECT policy_json, expires_at FROM sequences "
                "WHERE task_key = ? AND scope_hash = ? AND workflow_version = ?",
                (_task_key(task_description), scope_hash, workflow_version),
            ).fetchall()
        now = time.time()
        for policy_json, expires_at in rows:
            if expires_at is not None and expires_at <= now:
                continue
            try:
                stored = json.loads(policy_json)
            except (TypeError, json.JSONDecodeError):
                continue
            if isinstance(stored, dict) and stored.get("fingerprint") == policy_fingerprint:
                return True
        return False

    def stats(self) -> dict:
        """Return cache statistics."""
        with self._db_lock:
            db = self._ensure_db()
            total = db.execute("SELECT COUNT(*) FROM sequences").fetchone()[0]
            hits = db.execute("SELECT SUM(hit_count) FROM sequences").fetchone()[0] or 0
            domains = db.execute("SELECT COUNT(DISTINCT domain) FROM sequences").fetchone()[0]
            saved_tools = db.execute("SELECT COUNT(*) FROM saved_tools").fetchone()[0]
            return {
                "total_sequences": total,
                "total_hits": hits,
                "domains": domains,
                "saved_tools": saved_tools,
            }

    def save_tool(self, tool: SavedTool, *, replace: bool = False) -> None:
        """Persist a data-only tool contract without raw scope or input values."""
        if not isinstance(tool, SavedTool):
            raise TypeError("tool must be a SavedTool")
        with self._db_lock:
            db = self._ensure_db()
            existing = db.execute(
                "SELECT 1 FROM saved_tools WHERE name = ?", (tool.name,)
            ).fetchone()
            if existing and not replace:
                raise ToolManifestError(
                    f"saved tool {tool.name!r} already exists; pass replace=True to update it"
                )
            now = datetime.now(timezone.utc).isoformat()
            values = (
                tool.name,
                tool.description.strip(),
                tool.task.strip(),
                json.dumps(list(tool.input_names), sort_keys=True),
                tool.scope_hash,
                json.dumps(tool.precondition, sort_keys=True),
                json.dumps(tool.postcondition, sort_keys=True),
                tool.route_pattern,
                tool.workflow_version,
                tool.side_effect,
                tool.ttl_seconds,
                int(tool.mutation_guard),
                tool.mutation_threshold,
                json.dumps(tool.result_spec, sort_keys=True),
                now,
                now,
            )
            db.execute(
                """
                INSERT INTO saved_tools (
                    name, description, task_description, input_names_json, scope_hash,
                    precondition_json, postcondition_json, route_pattern, workflow_version,
                    side_effect, ttl_seconds, mutation_guard, mutation_threshold,
                    result_spec_json, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(name) DO UPDATE SET
                    description = excluded.description,
                    task_description = excluded.task_description,
                    input_names_json = excluded.input_names_json,
                    scope_hash = excluded.scope_hash,
                    precondition_json = excluded.precondition_json,
                    postcondition_json = excluded.postcondition_json,
                    route_pattern = excluded.route_pattern,
                    workflow_version = excluded.workflow_version,
                    side_effect = excluded.side_effect,
                    ttl_seconds = excluded.ttl_seconds,
                    mutation_guard = excluded.mutation_guard,
                    mutation_threshold = excluded.mutation_threshold,
                    result_spec_json = excluded.result_spec_json,
                    updated_at = excluded.updated_at
                """,
                values,
            )
            db.commit()

    def get_tool(self, name: str) -> SavedTool | None:
        """Return one saved tool manifest, or ``None`` when it does not exist."""
        with self._db_lock:
            db = self._ensure_db()
            row = db.execute(
                """
                SELECT name, description, task_description, input_names_json, scope_hash,
                       precondition_json, postcondition_json, route_pattern, workflow_version,
                       side_effect, ttl_seconds, mutation_guard, mutation_threshold, result_spec_json
                FROM saved_tools WHERE name = ?
                """,
                (name,),
            ).fetchone()
            return self._tool_from_row(row) if row is not None else None

    def list_tools(self) -> list[SavedTool]:
        """List saved tool manifests in stable name order."""
        with self._db_lock:
            db = self._ensure_db()
            rows = db.execute(
                """
                SELECT name, description, task_description, input_names_json, scope_hash,
                       precondition_json, postcondition_json, route_pattern, workflow_version,
                       side_effect, ttl_seconds, mutation_guard, mutation_threshold, result_spec_json
                FROM saved_tools ORDER BY name
                """
            ).fetchall()
            return [self._tool_from_row(row) for row in rows]

    def delete_tool(self, name: str) -> bool:
        """Delete one manifest. Cached workflows remain available for inspection."""
        with self._db_lock:
            db = self._ensure_db()
            deleted = db.execute("DELETE FROM saved_tools WHERE name = ?", (name,)).rowcount
            db.commit()
            return bool(deleted)

    @staticmethod
    def _tool_from_row(row: tuple[Any, ...]) -> SavedTool:
        return SavedTool(
            name=row[0],
            description=row[1],
            task=row[2],
            input_names=tuple(json.loads(row[3])),
            scope_hash=row[4],
            precondition=json.loads(row[5]),
            postcondition=json.loads(row[6]),
            route_pattern=row[7],
            workflow_version=row[8],
            side_effect=row[9],
            ttl_seconds=row[10],
            mutation_guard=bool(row[11]),
            mutation_threshold=row[12],
            result_spec=json.loads(row[13]),
        )

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
        # The id can come from a caller: keep it inside audit_dir and never
        # overwrite an earlier session's record.
        safe_id = re.sub(r"[^A-Za-z0-9._-]+", "_", session_id).strip("._") or "session"
        audit_path = self.audit_dir / f"{safe_id}.jsonl"
        try:
            handle = audit_path.open("x")
        except FileExistsError:
            audit_path = self.audit_dir / f"{safe_id}-{uuid.uuid4().hex[:12]}.jsonl"
            handle = audit_path.open("x")

        with handle as f:
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


def _load_or_create_scope_key(path: Path) -> bytes:
    """Read the local scope key, creating a random owner-only one if absent."""
    try:
        key = path.read_bytes()
    except FileNotFoundError:
        path.parent.mkdir(parents=True, exist_ok=True)
        key = secrets.token_bytes(32)
        try:
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError:
            # Another process created it first; wait briefly for its write.
            for _ in range(20):
                key = path.read_bytes()
                if len(key) >= 32:
                    break
                time.sleep(0.05)
        else:
            with os.fdopen(fd, "wb") as handle:
                handle.write(key)
    if len(key) < 32:
        raise RuntimeError(f"TERX scope key at {path} is invalid; delete it to regenerate")
    return key


def _task_key(task_description: str) -> str:
    """
    Normalize task description into a deterministic cache key.
    Lowercases and strips whitespace, then hashes to fixed length.
    NOTE: This is exact-match (not semantic). "log in" != "login".
    """
    normalized = task_description.lower().strip()
    return hashlib.md5(normalized.encode()).hexdigest()[:16]
