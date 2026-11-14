"""
Raw asyncio WebSocket connection to Chrome DevTools Protocol.
No Playwright. No Selenium. Direct CDP.
"""

from __future__ import annotations

import asyncio
import itertools
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

    def __init__(
        self,
        ws_url: str,
        timeout: float = 30.0,
        connect_timeout: float = 10.0,
        max_queued_events: int = 1000,
    ) -> None:
        if max_queued_events < 1:
            raise ValueError("max_queued_events must be at least 1")
        self.ws_url = ws_url
        self.timeout = timeout
        self.connect_timeout = connect_timeout

        # The connection class moved between websockets releases; keep it untyped.
        self._ws: Any | None = None
        self._id_counter = itertools.count(1)  # Never overflows
        self._pending: dict[int, asyncio.Future[dict[str, Any]]] = {}
        # Bounded: a host that enabled CDP domains but never drains events()
        # must not grow memory without limit. The oldest events are dropped.
        self._event_queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=max_queued_events)
        self.dropped_events = 0
        self._listener_task: asyncio.Task[None] | None = None
        self._connected: bool = False
        self._recorders: list[Callable[[str, dict[str, Any], dict[str, Any], float], None]] = []

    def add_recorder(
        self, recorder: Callable[[str, dict[str, Any], dict[str, Any], float], None]
    ) -> None:
        if recorder not in self._recorders:
            self._recorders.append(recorder)

    def remove_recorder(
        self, recorder: Callable[[str, dict[str, Any], dict[str, Any], float], None]
    ) -> None:
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

    async def reconnect(self) -> None:
        """Re-open the same target's WebSocket in place.

        Callers keep the same bridge object, and registered recorders stay
        attached. Commands in flight when the old socket dropped fail with
        ``ConnectionClosed``; they are never silently re-sent.
        """
        await self.close()
        await self.connect()

    async def __aenter__(self) -> "CDPBridge":
        await self.connect()
        return self

    async def __aexit__(self, *_: Any) -> None:
        await self.close()

    # ------------------------------------------------------------------ #
    # Send a CDP command, get a response                                    #
    # ------------------------------------------------------------------ #

    async def send(self, method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        """
        Send a CDP command and await its response.

        Returns:
            The 'result' dict from the CDP response.

        Raises:
            CDPError: if Chrome returns an error object.
            asyncio.TimeoutError: if no response within self.timeout.
        """
        result, latency_ms = await self._request(method, params, label="CDP command")
        for recorder in list(self._recorders):
            try:
                recorder(method, params or {}, result, latency_ms)
            except Exception as e:
                logger.error("CDP recorder failed: %s", e)
        return result

    async def send_internal(
        self, method: str, params: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        """
        Send a CDP command WITHOUT triggering recorders.

        Used by TERX internals (readyState checks, screenshots, AX tree reads)
        so that internal bookkeeping commands are never recorded into the cache.
        """
        result, _ = await self._request(method, params, label="Internal CDP command")
        return result

    async def _request(
        self, method: str, params: dict[str, Any] | None, *, label: str
    ) -> tuple[dict[str, Any], float]:
        """Send one command and await its response; never leaks a pending future."""
        if not self._connected or self._ws is None:
            raise RuntimeError("CDPBridge is not connected. Call connect() first.")

        cmd_id = next(self._id_counter)
        future: asyncio.Future[dict[str, Any]] = asyncio.get_running_loop().create_future()
        self._pending[cmd_id] = future
        message = json.dumps({"id": cmd_id, "method": method, "params": params or {}})
        t0 = time.perf_counter()
        try:
            await self._ws.send(message)
            logger.debug("CDP → %s %s", method, params)
            result = await asyncio.wait_for(future, timeout=self.timeout)
        except asyncio.TimeoutError:
            raise asyncio.TimeoutError(f"{label} '{method}' timed out after {self.timeout}s")
        finally:
            # Covers a failed socket send, a timeout, and cancellation alike.
            self._pending.pop(cmd_id, None)
        return result, (time.perf_counter() - t0) * 1000

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

    async def events(self) -> AsyncIterator[dict[str, Any]]:
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

    def _enqueue_event(self, event: dict[str, Any]) -> None:
        """Queue an event, dropping the oldest one when the queue is full."""
        if self._event_queue.full():
            try:
                self._event_queue.get_nowait()
            except asyncio.QueueEmpty:
                pass
            self.dropped_events += 1
            if self.dropped_events == 1 or self.dropped_events % 1000 == 0:
                logger.debug("CDP event queue full; %d events dropped", self.dropped_events)
        self._event_queue.put_nowait(event)

    async def _listen(self) -> None:
        """Background task: read from WebSocket, dispatch to futures/queue."""
        ws = self._ws
        if ws is None:
            return
        try:
            async for raw in ws:
                msg: dict[str, Any] = json.loads(raw)

                if "id" in msg:
                    # Response to a command we sent
                    cmd_id = msg["id"]
                    future = self._pending.pop(cmd_id, None)
                    if future:
                        try:
                            if "error" in msg:
                                future.set_exception(
                                    CDPError(msg["error"].get("message", "CDP error"))
                                )
                            else:
                                future.set_result(msg.get("result", {}))
                        except asyncio.InvalidStateError:
                            # Future was already set (timeout or cancellation) - safe to ignore
                            pass
                else:
                    # Unsolicited event (DOM mutation, network event, etc.)
                    self._enqueue_event(msg)
                    logger.debug("CDP event ← %s", msg.get("method"))

        except ConnectionClosed:
            logger.warning("CDP WebSocket closed unexpectedly")
        except asyncio.CancelledError:
            pass
        except Exception as e:
            logger.error("Unexpected error in CDP listener: %s", e)
        finally:
            # Always clean up pending futures
            self._connected = False
            for future in self._pending.values():
                if not future.done():
                    try:
                        future.set_exception(ConnectionClosed(None, None))
                    except asyncio.InvalidStateError:
                        pass
            self._pending.clear()


class CDPError(Exception):
    """Chrome returned an error for a CDP command."""

    pass
