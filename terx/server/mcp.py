"""
TERX MCP Server — exposes the browser and muscle memory cache as MCP tools.

Start with: terx-server
Then connect any MCP client (Claude Desktop, Cursor, etc.)
"""

from __future__ import annotations

import asyncio
import base64
import collections
import hashlib
import json
import logging
import sys
import time
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

try:
    from mcp.server.fastmcp import FastMCP
except ImportError:
    print("ERROR: mcp not installed. Run: pip install mcp", file=sys.stderr)
    sys.exit(1)

from terx.cdp.session import BrowserSession
from terx.dom.extractor import DOMExtractor
from terx.cache.cache import (
    CDPCommand, MuscleMemorycache, ReplayCostLedger, session_for
)

logger = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO)

# ------------------------------------------------------------------ #
# URL Security — scheme-first validation                               #
# ------------------------------------------------------------------ #

BLOCKED_SCHEMES = {"data", "javascript", "blob", "file", "vbscript"}
ALLOWED_SCHEMES = {"http", "https", "about", "chrome"}


def _validate_url(url: str) -> str | None:
    """
    Validate URL before navigation. Returns error message if blocked, None if OK.
    Blocks data:, javascript:, blob:, file: schemes to prevent exfiltration.
    """
    parsed = urlparse(url)
    scheme = parsed.scheme.lower()

    if scheme in BLOCKED_SCHEMES:
        return f"Blocked: '{scheme}:' URLs are not allowed (security policy)"
    if scheme and scheme not in ALLOWED_SCHEMES:
        return f"Blocked: unknown URL scheme '{scheme}:'"
    if not scheme and not url.startswith("/"):
        return "Blocked: URL must have a scheme (http:// or https://)"
    return None


# ------------------------------------------------------------------ #
# LRU Screenshot Store (BUG 5 fix — bounded memory)                   #
# ------------------------------------------------------------------ #

class LRUScreenshotStore:
    """Bounded in-memory screenshot store. Evicts oldest when full."""

    def __init__(self, max_size: int = 20) -> None:
        self._store: collections.OrderedDict[str, bytes] = collections.OrderedDict()
        self._max_size = max_size

    def put(self, key: str, data: bytes) -> None:
        if key in self._store:
            self._store.move_to_end(key)
        else:
            if len(self._store) >= self._max_size:
                self._store.popitem(last=False)
            self._store[key] = data

    def get(self, key: str) -> bytes | None:
        if key in self._store:
            self._store.move_to_end(key)
            return self._store[key]
        return None


# ------------------------------------------------------------------ #
# Globals (one browser session per server instance)                    #
# ------------------------------------------------------------------ #

mcp = FastMCP(
    name="terx",
    instructions=(
        "TERX browser agent tools. "
        "Use browser_get_state first to see what's on the page. "
        "The cache automatically replays successful sequences — "
        "no need to re-discover elements you've found before."
    ),
)

_session: BrowserSession | None = None
_extractor = DOMExtractor()
_cache = MuscleMemorycache()
_screenshot_store = LRUScreenshotStore(max_size=20)
_startup_done = False


def _get_session() -> BrowserSession:
    if _session is None:
        raise RuntimeError(
            "Browser not connected. Start Chrome with: "
            "google-chrome --remote-debugging-port=9222"
        )
    return _session


async def _ensure_connected() -> None:
    """Lazy-connect to Chrome on first tool call (avoids event loop conflict)."""
    global _session, _startup_done
    if _startup_done:
        return
    _startup_done = True
    _session = BrowserSession()
    try:
        await _session.start()
        logger.info("✅ TERX connected to Chrome")
    except RuntimeError as exc:
        logger.error("❌ %s", exc)
        _session = None


# ------------------------------------------------------------------ #
# MCP Tools                                                             #
# ------------------------------------------------------------------ #

@mcp.tool()
async def browser_get_state() -> dict:
    """
    Get the current page state: URL, title, and all interactable elements.
    Call this first before any click or type action.
    Returns element IDs you can pass to browser_click and browser_type.
    """
    await _ensure_connected()
    session = _get_session()
    bridge = session.bridge()
    snapshot = await _extractor.snapshot(bridge)

    return {
        "url": snapshot.url,
        "title": snapshot.title,
        "element_count": snapshot.element_count,
        "structural_hash": snapshot.structural_hash[:16],
        "elements": [
            {
                "id": el.id,
                "role": el.role,
                "label": el.label,
            }
            for el in snapshot.elements
        ],
    }


@mcp.tool()
async def browser_navigate(url: str) -> dict:
    """Navigate to a URL. Returns the new page title."""
    # BUG 10 FIX: URL validation
    error = _validate_url(url)
    if error:
        return {"success": False, "error": error}

    await _ensure_connected()
    session = _get_session()
    bridge = session.bridge()
    t0 = time.perf_counter()
    result = await bridge.send("Page.navigate", {"url": url})

    # Wait for load event instead of blind sleep
    try:
        await asyncio.wait_for(
            bridge.send("Page.loadEventFired"), timeout=10.0
        )
    except asyncio.TimeoutError:
        pass  # Page may not fire load event (SPA)

    latency = (time.perf_counter() - t0) * 1000
    return {
        "success": True,
        "navigated_to": url,
        "frame_id": result.get("frameId"),
        "latency_ms": round(latency, 1),
    }


@mcp.tool()
async def browser_click(element_id: int) -> dict:
    """
    Click an element by its ID (from browser_get_state).
    Returns success status and new page state summary.
    """
    await _ensure_connected()
    session = _get_session()
    bridge = session.bridge()
    snapshot = await _extractor.snapshot(bridge)

    el = snapshot.find_by_id(element_id)
    if el is None:
        return {
            "success": False,
            "error": f"Element {element_id} not found. Call browser_get_state to refresh.",
        }

    # Get element bounding box via DOM
    try:
        box_result = await bridge.send(
            "DOM.getBoxModel",
            {"backendNodeId": el.backend_dom_id}
        )
    except Exception as exc:
        return {"success": False, "error": f"Cannot get element position: {exc}"}

    model = box_result.get("model", {})
    content = model.get("content", [])

    # BUG 3 FIX: content is a flat array [x1,y1, x2,y2, x3,y3, x4,y4] (4 corners)
    if len(content) < 8:
        return {"success": False, "error": "Element has no renderable bounding box"}

    # Center = average of all 4 corner coordinates
    x = (content[0] + content[2] + content[4] + content[6]) / 4
    y = (content[1] + content[3] + content[5] + content[7]) / 4

    # Dispatch mouse events
    for event_type in ["mouseMoved", "mousePressed", "mouseReleased"]:
        await bridge.send("Input.dispatchMouseEvent", {
            "type": event_type,
            "x": x,
            "y": y,
            "button": "left",
            "clickCount": 1,
        })

    await asyncio.sleep(0.3)
    return {
        "success": True,
        "clicked": {"id": el.id, "role": el.role, "label": el.label},
        "coordinates": {"x": round(x), "y": round(y)},
    }


@mcp.tool()
async def browser_type(element_id: int, text: str) -> dict:
    """
    Type text into an input field by element ID.
    Uses framework-adaptive input (handles React/Vue without state conflicts).
    """
    await _ensure_connected()
    session = _get_session()
    bridge = session.bridge()
    snapshot = await _extractor.snapshot(bridge)

    el = snapshot.find_by_id(element_id)
    if el is None:
        return {"success": False, "error": f"Element {element_id} not found."}

    # Focus the element by backendNodeId
    await bridge.send("DOM.focus", {"backendNodeId": el.backend_dom_id})
    await asyncio.sleep(0.1)

    # BUG 9 FIX: Target the element by backendNodeId, not document.activeElement.
    # Resolve to a JS object reference first, then set its value.
    resolve_result = await bridge.send("DOM.resolveNode", {
        "backendNodeId": el.backend_dom_id
    })
    object_id = resolve_result.get("object", {}).get("objectId")

    if object_id:
        # Use callFunctionOn to target the EXACT element, not activeElement
        await bridge.send("Runtime.callFunctionOn", {
            "objectId": object_id,
            "functionDeclaration": """
                function(newValue) {
                    var nativeSetter = Object.getOwnPropertyDescriptor(
                        window.HTMLInputElement.prototype, 'value'
                    )?.set || Object.getOwnPropertyDescriptor(
                        window.HTMLTextAreaElement.prototype, 'value'
                    )?.set;
                    if (nativeSetter) {
                        nativeSetter.call(this, newValue);
                        this.dispatchEvent(new Event('input', { bubbles: true }));
                        this.dispatchEvent(new Event('change', { bubbles: true }));
                    } else {
                        this.value = newValue;
                    }
                }
            """,
            "arguments": [{"value": text}],
        })
    else:
        # Fallback: type character by character via key events
        for char in text:
            await bridge.send("Input.dispatchKeyEvent", {
                "type": "keyDown",
                "text": char,
            })
            await bridge.send("Input.dispatchKeyEvent", {
                "type": "keyUp",
                "text": char,
            })

    return {
        "success": True,
        "typed_into": {"id": el.id, "role": el.role, "label": el.label},
        "text_length": len(text),
    }


@mcp.tool()
async def browser_screenshot() -> dict:
    """
    Capture a screenshot.
    Returns a hash reference — NOT a raw base64 blob (prevents context poisoning).
    Use browser_screenshot_get to retrieve the actual image by hash.
    """
    await _ensure_connected()
    session = _get_session()
    bridge = session.bridge()
    result = await bridge.send("Page.captureScreenshot", {
        "format": "png",
        "captureBeyondViewport": False,
    })
    raw_b64: str = result.get("data", "")
    raw_bytes = base64.b64decode(raw_b64)

    img_hash = hashlib.sha256(raw_bytes).hexdigest()[:16]
    _screenshot_store.put(img_hash, raw_bytes)

    return {
        "screenshot_ref": f"sha256:{img_hash}",
        "size_bytes": len(raw_bytes),
        "note": "Use browser_screenshot_get(ref) to retrieve the image.",
    }


@mcp.tool()
async def browser_screenshot_get(ref: str) -> dict:
    """
    Retrieve a screenshot by hash reference (from browser_screenshot).
    Returns base64-encoded PNG.
    """
    img_hash = ref.replace("sha256:", "")
    raw = _screenshot_store.get(img_hash)
    if raw is None:
        return {"error": f"Screenshot {ref} not found or expired."}
    return {
        "data": base64.b64encode(raw).decode(),
        "format": "png",
        "ref": ref,
    }


@mcp.tool()
async def browser_scroll(direction: str = "down", amount: int = 300) -> dict:
    """Scroll the page. direction: 'up' | 'down'. amount: pixels."""
    await _ensure_connected()
    session = _get_session()
    bridge = session.bridge()
    delta_y = amount if direction == "down" else -amount
    await bridge.send("Input.dispatchMouseEvent", {
        "type": "mouseWheel",
        "x": 760,
        "y": 400,
        "deltaX": 0,
        "deltaY": delta_y,
    })
    await asyncio.sleep(0.2)
    return {"scrolled": direction, "pixels": amount}


@mcp.tool()
async def browser_new_tab(url: str = "about:blank") -> dict:
    """Open a new browser tab. Returns the target ID."""
    error = _validate_url(url)
    if error:
        return {"success": False, "error": error}

    await _ensure_connected()
    session = _get_session()
    target_id = await session.new_tab(url)
    return {"target_id": target_id, "url": url}


@mcp.tool()
async def cache_stats() -> dict:
    """Show TERX muscle memory cache statistics."""
    stats = _cache.stats()
    return {
        "cached_sequences": stats["total_sequences"],
        "total_cache_hits": stats["total_hits"],
        "unique_domains": stats["domains"],
        "note": "Each cache hit = zero LLM calls for that action sequence.",
    }


@mcp.tool()
async def cache_invalidate(domain: str) -> dict:
    """Clear cached sequences for a domain (use when the site UI changed)."""
    deleted = _cache.invalidate(domain)
    return {"domain": domain, "sequences_deleted": deleted}


# ------------------------------------------------------------------ #
# Server entry point                                                    #
# ------------------------------------------------------------------ #

def main() -> None:
    """Entry point: terx-server"""
    print("⚡ TERX MCP Server starting...")
    print("   Connect Chrome: google-chrome --remote-debugging-port=9222")
    print("   Connect MCP client: add terx-server to your MCP config\n")

    # BUG 4 FIX: Don't create a separate event loop for startup.
    # Instead, lazy-connect on first tool call inside FastMCP's event loop.
    # This avoids orphaning the WebSocket on a dead loop.
    mcp.run()


if __name__ == "__main__":
    main()
