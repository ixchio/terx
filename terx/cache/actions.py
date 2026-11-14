"""The semantic actions TERX can record and replay, and their execution over CDP."""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

from terx.cdp.bridge import CDPBridge
from terx.dom.extractor import DOMExtractor, DOMSnapshot
from terx.cache.errors import ReplayRefused
from terx.cache.policy import REPLAY_ACTIONS, _is_replayable_url
from terx.cache.models import CDPCommand
from terx.cache.redaction import _interpolate_placeholders
from terx.cache.conditions import REPLAY_POLL_INTERVAL


logger = logging.getLogger(__name__)


MUTATING_CDP_METHODS = {
    "Page.navigate",
    "Input.dispatchMouseEvent",
    "Input.dispatchKeyEvent",
    "Input.insertText",
    "DOM.focus",
    "Runtime.evaluate",
    "Runtime.callFunctionOn",
}


SAFE_CLICK_FUNCTIONS = {
    "function() { this.click(); }",
    "function(){this.click();}",
}


# Selects an editable element's current contents so the next text insertion
# replaces it. Input.insertText alone appends to autofilled or prefilled values.
_SELECT_EDITABLE_CONTENTS = """function() {
  if ('value' in this && typeof this.select === 'function') {
    this.select();
    return true;
  }
  if (this.isContentEditable) {
    const range = document.createRange();
    range.selectNodeContents(this);
    const selection = window.getSelection();
    selection.removeAllRanges();
    selection.addRange(range);
    return true;
  }
  return false;
}"""


CLICK_FUNCTION = "function() { this.click(); }"


async def click_element(bridge: CDPBridge, backend_node_id: int, *, record: bool = False) -> None:
    """Click one element through its DOM node handle.

    With ``record=True`` the node resolution and the click go through
    ``send``, so a cold-path recorder can map the object back to a unique
    labelled element and record a replayable ``TERX.click``.
    """
    send = bridge.send if record else bridge.send_internal
    resolved = await send("DOM.resolveNode", {"backendNodeId": backend_node_id})
    object_id = resolved.get("object", {}).get("objectId")
    if not isinstance(object_id, str) or not object_id:
        raise ReplayRefused("could not resolve semantic click target")
    await send(
        "Runtime.callFunctionOn",
        {"objectId": object_id, "functionDeclaration": CLICK_FUNCTION},
    )


async def replace_field_text(
    bridge: CDPBridge, backend_node_id: int, text: str, *, record: bool = False
) -> None:
    """Focus one field and replace its whole value with ``text``.

    The selection step always uses ``send_internal`` so it never reaches a
    recorder. With ``record=True`` the focus and insertion go through
    ``send`` so a cold-path recorder sees an ordinary labelled focus followed
    by text entry; an empty value then deletes via a key event, which the
    recorder correctly treats as non-replayable.
    """
    send = bridge.send if record else bridge.send_internal
    await send("DOM.focus", {"backendNodeId": backend_node_id})
    resolved = await bridge.send_internal("DOM.resolveNode", {"backendNodeId": backend_node_id})
    object_id = resolved.get("object", {}).get("objectId")
    if isinstance(object_id, str) and object_id:
        await bridge.send_internal(
            "Runtime.callFunctionOn",
            {
                "objectId": object_id,
                "functionDeclaration": _SELECT_EDITABLE_CONTENTS,
                "returnByValue": True,
            },
        )
    if text:
        await send("Input.insertText", {"text": text})
        return
    for event_type in ("keyDown", "keyUp"):
        await send(
            "Input.dispatchKeyEvent",
            {
                "type": event_type,
                "key": "Backspace",
                "code": "Backspace",
                "windowsVirtualKeyCode": 8,
            },
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


def _preflight_replay(commands: list[CDPCommand], variables: dict[str, Any]) -> None:
    """Statically validate a whole cached sequence before any action runs.

    Raises ``ReplayRefused`` for a malformed or unsupported action and
    ``MissingReplayVariable`` for any placeholder without a supplied value, so
    neither can surface after earlier steps have already changed the page.
    """
    for command in commands:
        if command.method not in REPLAY_ACTIONS:
            raise ReplayRefused(f"unsupported cached action: {command.method}")
        if command.method == "TERX.navigate":
            url = command.params.get("url")
            if not isinstance(url, str) or not _is_replayable_url(url):
                raise ReplayRefused("cached navigation URL is not permitted")
            continue
        target = command.params.get("target")
        if not isinstance(target, dict) or not all(
            isinstance(target.get(key), str) and target[key] for key in ("role", "label")
        ):
            raise ReplayRefused("semantic target requires role and label")
        if command.method == "TERX.type":
            text = command.params.get("text")
            if not isinstance(text, str):
                raise ReplayRefused("cached text action has no string value")
            _interpolate_placeholders(text, variables)


async def _resolve_replay_target(bridge: CDPBridge, target: Any, timeout: float) -> Any:
    """Wait for a semantic target to appear; refuse at once if it is ambiguous.

    A click often starts a navigation or an async render, so the next target
    may not exist yet. A missing target is polled until ``timeout``; snapshot
    errors while a page is mid-navigation are treated as "not yet".
    """
    deadline = time.monotonic() + timeout
    while True:
        try:
            snapshot = await DOMExtractor().snapshot(bridge)
            return _find_replay_target(snapshot, target)
        except ReplayRefused as exc:
            if "ambiguous" in exc.reason or time.monotonic() >= deadline:
                raise
        except Exception:
            if time.monotonic() >= deadline:
                raise
        await asyncio.sleep(REPLAY_POLL_INTERVAL)


async def _execute_replay_action(
    bridge: CDPBridge,
    command: CDPCommand,
    variables: dict[str, Any],
    *,
    timeout: float = 10.0,
) -> None:
    """Execute one TERX semantic action without triggering bridge recorders."""
    if command.method not in REPLAY_ACTIONS:
        raise ReplayRefused(f"unsupported cached action: {command.method}")
    if command.method == "TERX.navigate":
        url = command.params.get("url")
        if not isinstance(url, str) or not _is_replayable_url(url):
            raise ReplayRefused("cached navigation URL is not permitted")
        await bridge.send_internal("Page.navigate", {"url": url})
        await bridge.wait_for_load(timeout=timeout)
        return

    element = await _resolve_replay_target(bridge, command.params.get("target"), timeout)
    if command.method == "TERX.type":
        text = command.params.get("text")
        if not isinstance(text, str):
            raise ReplayRefused("cached text action has no string value")
        text = _interpolate_placeholders(text, variables)
        await replace_field_text(bridge, element.backend_dom_id, text)
        return

    await click_element(bridge, element.backend_dom_id)
    # A click may start a navigation; let a started load settle before the
    # next step snapshots the page. Target polling covers loads that begin late.
    await bridge.wait_for_load(timeout=timeout)
