"""
Raw asyncio WebSocket connection to Chrome DevTools Protocol.
No Playwright. No Selenium. Direct CDP.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from collections.abc import AsyncIterator
from typing import Any, Callable

import websockets
from websockets.exceptions import ConnectionClosed

logger = logging.getLogger(__name__)


class CDPBridge:
    """
    Bare-metal CDP connection over a single asyncio WebSocket.

    Usage:
        bridge = CDPBridge("ws://localhost:9222/devtools/page/TARGET_ID")
        await bridge.connect()
        result = await bridge.send("Page.navigate", {"url": "https://example.com"})
        await bridge.close()

    Or as a context manager:
        async with CDPBridge(ws_url) as bridge:
            await bridge.send("Page.navigate", {"url": "https://example.com"})
    """

    def __init__(self, ws_url: str, timeout: float = 30.0, connect_timeout: float = 10.0) -> None:
        self.ws_url = ws_url
        self.timeout = timeout
        self.connect_timeout = connect_timeout

        self._ws: websockets.WebSocketClientProtocol | None = None
        self._id_counter: int = 0
        self._pending: dict[int, asyncio.Future] = {}
        self._event_queue: asyncio.Queue = asyncio.Queue()
        self._listener_task: asyncio.Task | None = None
        self._connected: bool = False
        self._recorders: list[Callable[[str, dict, dict, float], None]] = []

    def add_recorder(self, recorder: Callable[[str, dict, dict, float], None]) -> None:
        if recorder not in self._recorders:
            self._recorders.append(recorder)

    def remove_recorder(self, recorder: Callable[[str, dict, dict, float], None]) -> None:
        if recorder in self._recorders:
            self._recorders.remove(recorder)

    @property
    def is_connected(self) -> bool:
        """Check if the bridge has an active WebSocket connection."""
        return self._connected and self._ws is not None

    # ------------------------------------------------------------------ #
    # Connection lifecycle                                                  #
    # ------------------------------------------------------------------ #

    async def connect(self) -> None:
        """Open the WebSocket and start the listener loop."""
        try:
            self._ws = await asyncio.wait_for(
                websockets.connect(
                    self.ws_url,
                    max_size=100 * 1024 * 1024,  # 100 MB — large pages can be big
                    ping_interval=20,
                    ping_timeout=10,
                ),
                timeout=self.connect_timeout,
            )
        except asyncio.TimeoutError:
            raise TimeoutError(f"CDP WebSocket connection timed out after {self.connect_timeout}s")
        self._connected = True
        self._listener_task = asyncio.create_task(self._listen(), name="cdp-listener")
        logger.debug("CDP connected → %s", self.ws_url)

    async def close(self) -> None:
        """Cleanly shut down the WebSocket and listener."""
        self._connected = False
        if self._listener_task:
            self._listener_task.cancel()
            try:
                await self._listener_task
            except asyncio.CancelledError:
                pass
        if self._ws:
            await self._ws.close()
            self._ws = None
        logger.debug("CDP closed")

    async def __aenter__(self) -> "CDPBridge":
        await self.connect()
        return self

    async def __aexit__(self, *_: Any) -> None:
        await self.close()

    # ------------------------------------------------------------------ #
    # Send a CDP command, get a response                                    #
    # ------------------------------------------------------------------ #

    async def send(self, method: str, params: dict | None = None) -> dict:
        """
        Send a CDP command and await its response.

        Returns:
            The 'result' dict from the CDP response.

        Raises:
            CDPError: if Chrome returns an error object.
            asyncio.TimeoutError: if no response within self.timeout.
        """
        if not self._connected or self._ws is None:
            raise RuntimeError("CDPBridge is not connected. Call connect() first.")

        self._id_counter += 1
        cmd_id = self._id_counter
        future: asyncio.Future = asyncio.get_running_loop().create_future()
        self._pending[cmd_id] = future

        message = json.dumps({"id": cmd_id, "method": method, "params": params or {}})
        t0 = time.perf_counter()
        await self._ws.send(message)
        logger.debug("CDP → %s %s", method, params)

        try:
            result = await asyncio.wait_for(future, timeout=self.timeout)
        except asyncio.TimeoutError:
            self._pending.pop(cmd_id, None)
            raise asyncio.TimeoutError(f"CDP command '{method}' timed out after {self.timeout}s")

        latency_ms = (time.perf_counter() - t0) * 1000

        for recorder in self._recorders:
            try:
                recorder(method, params or {}, result, latency_ms)
            except Exception as e:
                logger.error("CDP recorder failed: %s", e)

        return result

    async def send_internal(self, method: str, params: dict | None = None) -> dict:
        """
        Send a CDP command WITHOUT triggering recorders.

        Used by TERX internals (readyState checks, screenshots, AX tree reads)
        so that internal bookkeeping commands are never recorded into the cache.
        """
        if not self._connected or self._ws is None:
            raise RuntimeError("CDPBridge is not connected. Call connect() first.")

        self._id_counter += 1
        cmd_id = self._id_counter
        future: asyncio.Future = asyncio.get_running_loop().create_future()
        self._pending[cmd_id] = future

        message = json.dumps({"id": cmd_id, "method": method, "params": params or {}})
        await self._ws.send(message)

        try:
            return await asyncio.wait_for(future, timeout=self.timeout)
        except asyncio.TimeoutError:
            self._pending.pop(cmd_id, None)
            raise asyncio.TimeoutError(
                f"Internal CDP command '{method}' timed out after {self.timeout}s"
            )

    # ------------------------------------------------------------------ #
    # Page load waiting                                                     #
    # ------------------------------------------------------------------ #

    async def wait_for_load(self, timeout: float = 10.0) -> None:
        """
        Wait for document.readyState == 'complete' by polling via Runtime.evaluate.

        Uses send_internal() so these checks are never recorded into the cache.
        This replaces the broken Page.loadEventFired approach (which is an EVENT,
        not a command — sending it as a command causes a CDP error).
        """
        t0 = time.time()
        while time.time() - t0 < timeout:
            try:
                res = await self.send_internal(
                    "Runtime.evaluate", {"expression": "document.readyState", "returnByValue": True}
                )
                state = res.get("result", {}).get("value")
                if state == "complete":
                    return
            except Exception:
                pass
            await asyncio.sleep(0.05)
        logger.debug("wait_for_load timed out after %.1fs (proceeding anyway)", timeout)

    # ------------------------------------------------------------------ #
    # Event stream                                                          #
    # ------------------------------------------------------------------ #

    async def events(self) -> AsyncIterator[dict]:
        """Async-iterate over unsolicited CDP events."""
        while self._connected:
            try:
                event = await asyncio.wait_for(self._event_queue.get(), timeout=1.0)
                yield event
            except asyncio.TimeoutError:
                continue

    # ------------------------------------------------------------------ #
    # Internal listener loop                                                #
    # ------------------------------------------------------------------ #

    async def _listen(self) -> None:
        """Background task: read from WebSocket, dispatch to futures/queue."""
        try:
            async for raw in self._ws:
                msg: dict = json.loads(raw)

                if "id" in msg:
                    # Response to a command we sent
                    cmd_id = msg["id"]
                    future = self._pending.pop(cmd_id, None)
                    if future and not future.done():
                        if "error" in msg:
                            future.set_exception(CDPError(msg["error"].get("message", "CDP error")))
                        else:
                            future.set_result(msg.get("result", {}))
                else:
                    # Unsolicited event (DOM mutation, network event, etc.)
                    await self._event_queue.put(msg)
                    logger.debug("CDP event ← %s", msg.get("method"))

        except ConnectionClosed:
            logger.warning("CDP WebSocket closed unexpectedly")
            self._connected = False
            # Fail all pending futures
            for future in self._pending.values():
                if not future.done():
                    future.set_exception(ConnectionClosed(None, None))
            self._pending.clear()
        except asyncio.CancelledError:
            pass


class CDPError(Exception):
    """Chrome returned an error for a CDP command."""

    pass
