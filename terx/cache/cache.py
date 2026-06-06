"""
TERX Memory Cache — the core caching engine.

Records successful CDP action sequences keyed by (domain, structural_hash, task).
On cache hit: replays raw CDP commands directly — zero LLM tokens.
On cache miss: lets the agent reason normally, then caches the result.

Writes sessions in .vcr format (compatible with Agent VCR).
"""

from __future__ import annotations

import hashlib
import json
import logging
import sqlite3
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
VCR_DIR = Path(".vcr")
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
        vcr_dir: str | Path = ".vcr",
        similarity_threshold: float = SIMILARITY_THRESHOLD,
    ) -> None:
        self.db_path = Path(db_path)
        self.vcr_dir = Path(vcr_dir)
        self.similarity_threshold = similarity_threshold
        self._db: sqlite3.Connection | None = None

    # ------------------------------------------------------------------ #
    # Setup                                                                 #
    # ------------------------------------------------------------------ #

    def _ensure_db(self) -> sqlite3.Connection:
        if self._db is not None:
            return self._db
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        db = sqlite3.connect(self.db_path, check_same_thread=False)
        db.execute("PRAGMA journal_mode=WAL")
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
        db.execute(
            "CREATE INDEX IF NOT EXISTS idx_domain_task ON sequences(domain, task_key)"
        )
        db.commit()
        self._db = db
        return db

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
        db = self._ensure_db()
        task_key = _task_key(task_description)
        rows = db.execute(
            "SELECT structural_hash, task_description, commands_json, "
            "hit_count, created_at, last_used, role_sequence, task_key "
            "FROM sequences WHERE domain = ? AND task_key = ?",
            (domain, task_key)
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
        """Persist a successful action sequence."""
        db = self._ensure_db()
        now = datetime.now(timezone.utc).isoformat()
        commands_json = json.dumps([asdict(c) for c in commands])
        task_key = _task_key(task_description)

        db.execute(
            """
            INSERT INTO sequences
                (domain, structural_hash, task_key, task_description,
                 role_sequence, commands_json, hit_count, created_at, last_used)
            VALUES (?, ?, ?, ?, ?, ?, 0, ?, ?)
            ON CONFLICT(domain, structural_hash, task_key) DO UPDATE SET
                commands_json    = excluded.commands_json,
                task_description = excluded.task_description,
                role_sequence    = excluded.role_sequence,
                last_used        = excluded.last_used
            """,
            (domain, structural_hash, task_key, task_description,
             role_sequence, commands_json, now, now),
        )
        db.commit()
        logger.info(
            "Cached %d commands for domain=%s task=%s hash=%.8s",
            len(commands), domain, task_key, structural_hash
        )

    def increment_hit(self, domain: str, structural_hash: str, task_key: str) -> None:
        """Increment the hit counter for a cached sequence."""
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
        db = self._ensure_db()
        cursor = db.execute(
            "DELETE FROM sequences WHERE domain = ?", (domain,)
        )
        db.commit()
        return cursor.rowcount

    def stats(self) -> dict:
        """Return cache statistics."""
        db = self._ensure_db()
        total = db.execute("SELECT COUNT(*) FROM sequences").fetchone()[0]
        hits = db.execute("SELECT SUM(hit_count) FROM sequences").fetchone()[0] or 0
        domains = db.execute(
            "SELECT COUNT(DISTINCT domain) FROM sequences"
        ).fetchone()[0]
        return {"total_sequences": total, "total_hits": hits, "domains": domains}

    # ------------------------------------------------------------------ #
    # VCR-format writer (compatible with Agent VCR)                        #
    # ------------------------------------------------------------------ #

    def write_vcr(
        self,
        session_id: str,
        task_description: str,
        commands: list[CDPCommand],
        domain: str,
        was_cache_hit: bool,
    ) -> Path:
        """
        Write a browser session in .vcr JSONL format.
        Compatible with Agent VCR's VCRPlayer.

        Format:
            {"type": "session", "data": {...}}
            {"type": "frame",   "data": {...}}  ← one per CDP command
        """
        self.vcr_dir.mkdir(parents=True, exist_ok=True)
        vcr_path = self.vcr_dir / f"{session_id}.vcr"

        with vcr_path.open("w") as f:
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
                }
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
                        }
                    }
                }
                f.write(json.dumps(frame) + "\n")

        logger.info("Wrote .vcr session → %s (%d frames)", vcr_path, len(commands))
        return vcr_path


# ------------------------------------------------------------------ #
# Recording context manager                                             #
# ------------------------------------------------------------------ #

class RecordingContext:
    """
    Context returned by session_for().

    Records all CDP commands sent through the bridge.
    On exit: stores them in the cache + writes .vcr file.
    """

    def __init__(
        self,
        cache: MemoryCache,
        bridge: CDPBridge,
        task: str,
        session_id: str | None = None,
    ) -> None:
        self._cache = cache
        self._bridge = bridge
        self._task = task
        self._session_id = session_id or f"browser_session_{int(time.time())}"
        self._snapshot: DOMSnapshot | None = None
        self._domain: str = "unknown"
        self._cached_seq: CachedSequence | None = None
        self._run_number: int = 1
        self._recorded_commands: list[CDPCommand] = []
        self.ledger: ReplayCostLedger | None = None

    @property
    def hit(self) -> bool:
        return self._cached_seq is not None

    async def replay(self) -> None:
        """Replay the cached CDP command sequence directly. Zero LLM calls."""
        if self._cached_seq is None:
            raise RuntimeError("No cached sequence to replay (cache miss)")

        t0 = time.perf_counter()
        
        # Fresh DOM snapshot of the current page at replay start
        extractor = DOMExtractor()
        current_snapshot = await extractor.snapshot(self._bridge)
        current_elements = current_snapshot.elements
        
        # Tracks dynamic mappings for objectId, nodeId, etc.
        runtime_value_map = {}

        for cmd in self._cached_seq.commands:
            if cmd.method in MUTATING_CDP_METHODS:
                await self._bridge.wait_for_load(timeout=2.0)
                
            # Translate parameters dynamically using node metadata and value map
            mapped_params = _translate_parameters(
                cmd.params, cmd.metadata, current_elements, runtime_value_map
            )
            
            try:
                actual_result = await self._bridge.send(cmd.method, mapped_params)
                # Discover new runtime mappings from the output (e.g. objectId returned)
                _discover_mappings(cmd.result, actual_result, runtime_value_map)
            except Exception as exc:
                logger.warning(
                    "Replay failed at %s: %s — attempting self-healing",
                    cmd.method, exc
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
                    task_desc=self._task
                )

                if new_params:
                    logger.info("Self-healing generated new params: %s", new_params)
                    try:
                        actual_result = await self._bridge.send(cmd.method, new_params)
                        _discover_mappings(cmd.result, actual_result, runtime_value_map)
                        continue  # Successfully healed
                    except Exception as e2:
                        logger.error("Healed parameters failed: %s", e2)

                raise CacheReplayError(cmd.method) from exc

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

        # Still write a .vcr file for the replay (for audit trail)
        self._cache.write_vcr(
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
                        ssim_score, SSIM_THRESHOLD
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
        if method == "Accessibility.getFullAXTree":
            nodes = result.get("nodes", [])
            elements = DOMExtractor()._extract_interactable(nodes)
            if self._snapshot:
                self._snapshot.elements = elements
                
        elif method in MUTATING_CDP_METHODS:
            node_info = _extract_node_info(params, self._snapshot)
            cmd = CDPCommand(
                method=method,
                params=params,
                result=result,
                latency_ms=latency,
                metadata={"node_info": node_info}
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
        self._cached_seq = self._cache.lookup(self._domain, self._snapshot.role_sequence, self._task)

        # Calculate run number
        db = self._cache._ensure_db()
        task_key = _task_key(self._task)
        self._run_number = db.execute(
            "SELECT COALESCE(SUM(hit_count) + COUNT(*), 1) "
            "FROM sequences WHERE domain = ? AND task_key = ?",
            (self._domain, task_key)
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
                (SCREENSHOT_DIR / f"{self._snapshot.structural_hash}.png").write_bytes(screenshot_bytes)
            except Exception as e:
                logger.warning("Failed to save baseline screenshot for SSIM: %s", e)

            # Write .vcr file
            self._cache.write_vcr(
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


def session_for(
    cache: MemoryCache,
    bridge: CDPBridge,
    task: str,
    session_id: str | None = None,
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


def _translate_parameters(params: Any, metadata: dict, current_elements: list, value_map: dict) -> Any:
    """Recursively swap mapped values and translate old node IDs to new ones."""
    if isinstance(params, dict):
        new_dict = {}
        for k, v in params.items():
            if v in value_map:
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

            new_dict[k] = _translate_parameters(v, metadata, current_elements, value_map)
        return new_dict
    elif isinstance(params, list):
        return [_translate_parameters(item, metadata, current_elements, value_map) for item in params]
    else:
        return value_map.get(params, params)


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
            if isinstance(cached, str) and (len(cached) < 4 or cached.lower() in ("true", "false", "null", "undefined")):
                return
            value_map[cached] = actual


# Backwards compatibility alias
MuscleMemorycache = MemoryCache


class CacheReplayError(Exception):
    """Raised when a cached CDP command fails during replay (DOM drift)."""
    def __init__(self, failed_method: str) -> None:
        self.failed_method = failed_method
        super().__init__(f"Replay failed at CDP method: {failed_method}")
