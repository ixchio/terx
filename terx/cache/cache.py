"""
Muscle Memory Cache — the core of TERX.

Records successful CDP action sequences keyed by (domain, structural_hash, task).
On cache hit: replays raw CDP commands directly — zero LLM tokens.
On cache miss: lets the agent reason normally, then caches the result.

Writes sessions in .vcr format (compatible with Agent VCR).
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import sqlite3
import time
from dataclasses import asdict, dataclass
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

# Methods used internally by TERX that should NOT be recorded
_INTERNAL_METHODS = {
    "Accessibility.getFullAXTree",
    "Page.captureScreenshot",
}


@dataclass
class CDPCommand:
    """A single recorded CDP command."""
    method: str
    params: dict
    result: dict
    latency_ms: float


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


class MuscleMemorycache:
    """
    The TERX muscle memory cache.

    Usage:
        cache = MuscleMemorycache()

        # Wrap the agent call
        async with cache.session(browser, task="login to salesforce") as ctx:
            if ctx.hit:
                # Cached path — replay CDP commands directly
                await ctx.replay()
            else:
                # New path — run your agent normally
                await my_agent.run(task)
                # TERX records the CDP stream automatically

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
        cache: MuscleMemorycache,
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

    async def _wait_for_load(self, timeout: float = 2.0) -> None:
        """Wait for the page readyState to be complete to prevent race conditions."""
        t0 = time.time()
        while time.time() - t0 < timeout:
            try:
                # Use _ws directly to avoid triggering recorders for internal checks
                self._bridge._id_counter += 1
                cmd_id = self._bridge._id_counter
                future = asyncio.get_running_loop().create_future()
                self._bridge._pending[cmd_id] = future
                import json as _json
                await self._bridge._ws.send(_json.dumps({
                    "id": cmd_id,
                    "method": "Runtime.evaluate",
                    "params": {"expression": "document.readyState"}
                }))
                res = await asyncio.wait_for(future, timeout=1.0)
                state = res.get("result", {}).get("value")
                if state == "complete":
                    return
            except Exception:
                pass
            await asyncio.sleep(0.05)

    async def replay(self) -> None:
        """Replay the cached CDP command sequence directly. Zero LLM calls."""
        if self._cached_seq is None:
            raise RuntimeError("No cached sequence to replay (cache miss)")

        t0 = time.perf_counter()
        for cmd in self._cached_seq.commands:
            if cmd.method in MUTATING_CDP_METHODS:
                await self._wait_for_load()
            try:
                await self._bridge.send(cmd.method, cmd.params)
            except Exception as exc:
                logger.warning(
                    "Replay failed at %s: %s — attempting self-healing",
                    cmd.method, exc
                )
                from terx.agent.healer import SelfHealer
                healer = SelfHealer()
                extractor = DOMExtractor()
                current_snapshot = await extractor.snapshot(self._bridge)
                
                new_params = await healer.heal_command(
                    failed_method=cmd.method,
                    old_params=cmd.params,
                    current_dom=current_snapshot.elements,
                    task_desc=self._task
                )
                
                if new_params:
                    logger.info("Self-healing generated new params: %s", new_params)
                    try:
                        await self._bridge.send(cmd.method, new_params)
                        continue # Successfully healed
                    except Exception as e2:
                        logger.error("Healed parameters failed: %s", e2)
                        
                raise CacheReplayError(cmd.method) from exc

        # --- Visual Audit (SSIM) ---
        SCREENSHOT_DIR.mkdir(parents=True, exist_ok=True)
        screenshot_path = SCREENSHOT_DIR / f"{self._cached_seq.structural_hash}.png"
        
        if screenshot_path.exists():
            try:
                import base64 as _b64
                result = await self._bridge.send("Page.captureScreenshot", {"format": "png"})
                new_screenshot = _b64.b64decode(result.get("data", ""))
                old_screenshot = screenshot_path.read_bytes()
                
                from terx.vision.ssim import compute_ssim
                ssim_score = compute_ssim(old_screenshot, new_screenshot)
                logger.info("Visual Audit SSIM Score: %.3f", ssim_score)
                
                if ssim_score < SSIM_THRESHOLD:
                    logger.warning("SSIM drift detected (%.3f < %.3f)! UI changed significantly.", ssim_score, SSIM_THRESHOLD)
                    # We still count it as a hit, but warn the agent.
            except Exception as e:
                logger.warning("Failed to run SSIM visual audit: %s", e)

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

    def record_command(self, cmd: CDPCommand) -> None:
        """Manually record a command (legacy). Transparent proxy now auto-records."""
        self._recorded_commands.append(cmd)

    def _auto_record(self, method: str, params: dict, result: dict, latency: float) -> None:
        """Transparent interceptor: auto-captures mutating commands sent through the bridge."""
        if method in MUTATING_CDP_METHODS:
            cmd = CDPCommand(method=method, params=params, result=result, latency_ms=latency)
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
                result = await self._bridge.send("Page.captureScreenshot", {"format": "png"})
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
    cache: MuscleMemorycache,
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
    Normalize task description into a cache key.
    Lowercase, strip whitespace, hash to fixed length.
    Two tasks that mean the same thing should produce the same key.
    """
    normalized = task_description.lower().strip()
    return hashlib.md5(normalized.encode()).hexdigest()[:16]


class CacheReplayError(Exception):
    """Raised when a cached CDP command fails during replay (DOM drift)."""
    def __init__(self, failed_method: str) -> None:
        self.failed_method = failed_method
        super().__init__(f"Replay failed at CDP method: {failed_method}")
