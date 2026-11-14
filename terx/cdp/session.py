"""
Browser session manager — connects to Chrome, manages tabs,
runs the CDP supervisor heartbeat.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

import aiohttp

from terx.cdp.bridge import CDPBridge

logger = logging.getLogger(__name__)

DEFAULT_DEVTOOLS_PORT = 9222


class BrowserSession:
    """
    Manages a connection to a running Chrome instance.

    Responsibilities:
    - Fetches the list of open tabs from /json/list
    - Creates/closes tabs via CDP Target domain
    - Maintains a CDPBridge per active tab
    - Runs a heartbeat to detect disconnections and auto-reconnect
    """

    def __init__(
        self,
        host: str = "localhost",
        port: int = DEFAULT_DEVTOOLS_PORT,
        heartbeat_interval: float = 5.0,
        connect_timeout: float = 10.0,
    ) -> None:
        self.host = host
        self.port = port
        self.heartbeat_interval = heartbeat_interval
        self.connect_timeout = connect_timeout

        self._bridges: dict[str, CDPBridge] = {}  # target_id → bridge
        self._active_target: str | None = None
        self._heartbeat_task: asyncio.Task | None = None

    # ------------------------------------------------------------------ #
    # Lifecycle                                                             #
    # ------------------------------------------------------------------ #

    async def start(self) -> None:
        """Connect to Chrome and start heartbeat."""
        targets = await self._fetch_targets()
        if not targets:
            raise RuntimeError(
                f"No Chrome targets found at {self.host}:{self.port}. "
                "Start Chrome with: --remote-debugging-port=9222"
            )

        # Connect to first page target
        page_targets = [t for t in targets if t.get("type") == "page"]
        if page_targets:
            await self._connect_target(page_targets[0]["id"])

        self._heartbeat_task = asyncio.create_task(self._heartbeat(), name="cdp-heartbeat")
        logger.info("BrowserSession started → %d active targets", len(self._bridges))

    async def stop(self) -> None:
        """Close all bridges and stop heartbeat."""
        if self._heartbeat_task:
            self._heartbeat_task.cancel()
            try:
                await self._heartbeat_task
            except asyncio.CancelledError:
                pass

        for bridge in list(self._bridges.values()):
            await bridge.close()
        self._bridges.clear()
        logger.info("BrowserSession stopped")

    async def __aenter__(self) -> "BrowserSession":
        await self.start()
        return self

    async def __aexit__(self, *_: Any) -> None:
        await self.stop()

    # ------------------------------------------------------------------ #
    # Tab management                                                        #
    # ------------------------------------------------------------------ #

    async def new_tab(self, url: str = "about:blank") -> str:
        """Open a new tab. Returns the target_id."""
        bridge = self.bridge()
        result = await bridge.send("Target.createTarget", {"url": url})
        target_id = result["targetId"]
        await self._connect_target(target_id)
        self._active_target = target_id
        return target_id

    async def close_tab(self, target_id: str | None = None) -> None:
        """Close a tab by target_id (defaults to active tab)."""
        tid = target_id or self._active_target
        if tid is None:
            return
        bridge = self._bridges.get(tid)
        if bridge:
            await bridge.send("Target.closeTarget", {"targetId": tid})
            await bridge.close()
            del self._bridges[tid]
        if self._active_target == tid:
            remaining = list(self._bridges.keys())
            self._active_target = remaining[0] if remaining else None

    def bridge(self, target_id: str | None = None) -> CDPBridge:
        """Return the CDPBridge for the given (or active) target."""
        tid = target_id or self._active_target
        if tid is None or tid not in self._bridges:
            raise RuntimeError("No active browser tab. Call start() or new_tab() first.")
        return self._bridges[tid]

    # ------------------------------------------------------------------ #
    # Internal helpers                                                      #
    # ------------------------------------------------------------------ #

    async def _fetch_targets(self) -> list[dict]:
        """GET http://host:port/json/list and return the targets."""
        url = f"http://{self.host}:{self.port}/json/list"
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(url, timeout=aiohttp.ClientTimeout(total=5)) as resp:
                    return await resp.json()
        except Exception as exc:
            raise RuntimeError(
                f"Cannot reach Chrome at {url}. "
                "Is Chrome running with --remote-debugging-port=9222?"
            ) from exc

    async def _connect_target(self, target_id: str) -> CDPBridge:
        """Connect a CDPBridge to a specific target and register it."""
        existing = self._bridges.get(target_id)
        if existing is not None:
            await existing.reconnect()
            self._active_target = target_id
            return existing
        ws_url = f"ws://{self.host}:{self.port}/devtools/page/{target_id}"
        bridge = CDPBridge(ws_url, connect_timeout=self.connect_timeout)
        await bridge.connect()
        self._bridges[target_id] = bridge
        self._active_target = target_id
        return bridge

    async def _heartbeat(self) -> None:
        """
        Ping Chrome every N seconds to detect dropped connections.

        On failure, reconnect the *same* tab in place with exponential backoff,
        so callers' bridge objects and recorders stay valid. TERX never moves
        to a different tab: if the active tab is gone, its bridge is closed and
        removed, and ``bridge()`` raises until the host picks a tab.
        """
        backoff = 0.1
        while True:
            await asyncio.sleep(self.heartbeat_interval)
            target_id = self._active_target
            bridge = self._bridges.get(target_id) if target_id else None
            if bridge is None:
                continue
            try:
                await bridge.send_internal("Browser.getVersion")
                backoff = 0.1  # reset on success
                continue
            except Exception:
                logger.warning("CDP heartbeat failed — attempting reconnect in %.1fs", backoff)
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 10.0)
            try:
                targets = await self._fetch_targets()
            except Exception as exc:
                logger.error("CDP reconnect failed: %s", exc)
                continue
            if not any(t.get("id") == target_id and t.get("type") == "page" for t in targets):
                logger.error(
                    "Active tab %s no longer exists; not switching to another tab", target_id
                )
                await bridge.close()
                self._bridges.pop(target_id, None)
                if self._active_target == target_id:
                    self._active_target = None
                continue
            try:
                await bridge.reconnect()
                logger.info("CDP reconnected to tab %s", target_id)
                backoff = 0.1
            except Exception as exc:
                logger.error("CDP reconnect failed: %s", exc)
