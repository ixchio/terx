"""The semantic actions TERX can record and replay, and their execution over CDP.

Persisted actions (validated by :func:`terx.cache.policy.validate_action`):

======================  ==================================================
``TERX.navigate``       ``{"url"}``: an HTTP(S) URL; its path may hold
                        ``{{variable}}`` placeholders
``TERX.click``          ``{"target"}``
``TERX.type``           ``{"target", "text": "{{variable}}"}``; replaces the
                        field's whole value
``TERX.check``          ``{"target", "checked": bool}``; sets, never toggles
``TERX.select``         ``{"target", "option"}``: an option label or
                        ``{{variable}}``
``TERX.press``          ``{"target", "key"}``: ``Enter``, ``Tab`` or ``Escape``
======================  ==================================================

A *target* always has ``role`` and ``label`` (the accessible name). Targets
recorded since 1.0 can also carry ``frame`` (the iframe path), ``ordinal``
(position among same-role elements in that frame) and ``test_id`` (a
``data-testid``-style attribute). See :func:`_locate_target` for how these
signals must agree.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

from terx.cache.conditions import REPLAY_POLL_INTERVAL
from terx.cache.errors import ReplayRefused
from terx.cache.models import CDPCommand
from terx.cache.policy import REPLAY_ACTIONS, _is_replayable_url, validate_action
from terx.cache.redaction import _interpolate_placeholders, _interpolate_url
from terx.cdp.bridge import CDPBridge
from terx.dom.extractor import AXElement, DOMExtractor, DOMSnapshot

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

CLICK_FUNCTION = "function() { this.click(); }"

# Attributes treated as developer-assigned, stable element identifiers.
TEST_ID_ATTRIBUTES = ("data-testid", "data-test-id", "data-test", "data-qa", "data-cy")


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

# Sets a native or ARIA checkbox/switch/radio to the wanted state, clicking at
# most once, and reports the resulting state.
_SET_CHECKED = """function(wanted) {
  const read = () => ('checked' in this && typeof this.checked === 'boolean')
    ? this.checked
    : this.getAttribute('aria-checked') === 'true';
  if (!('checked' in this) && !this.hasAttribute('aria-checked')) {
    return 'not-checkable';
  }
  if (read() !== wanted) {
    this.click();
  }
  return read() === wanted ? 'ok' : 'unchanged';
}"""

# Chooses exactly one <select> option by its visible label. The label is passed
# as a CDP call argument, never interpolated into script text.
_SELECT_OPTION = """function(label) {
  if (!(this instanceof HTMLSelectElement)) {
    return 'not-select';
  }
  const matches = Array.from(this.options).filter(
    (option) => option.label === label || option.text.trim() === label
  );
  if (matches.length !== 1) {
    return matches.length ? 'ambiguous' : 'missing';
  }
  const setter = Object.getOwnPropertyDescriptor(HTMLSelectElement.prototype, 'value').set;
  setter.call(this, matches[0].value);
  this.dispatchEvent(new Event('input', { bubbles: true }));
  this.dispatchEvent(new Event('change', { bubbles: true }));
  return 'ok';
}"""

_KEY_EVENTS: dict[str, dict[str, Any]] = {
    "Enter": {"key": "Enter", "code": "Enter", "windowsVirtualKeyCode": 13, "text": "\r"},
    "Tab": {"key": "Tab", "code": "Tab", "windowsVirtualKeyCode": 9},
    "Escape": {"key": "Escape", "code": "Escape", "windowsVirtualKeyCode": 27},
}


# ------------------------------------------------------------------ #
# Element actions (shared by replay, the workflow adapter, and MCP)    #
# ------------------------------------------------------------------ #


async def _object_id(bridge: CDPBridge, backend_node_id: int, *, record: bool = False) -> str:
    send = bridge.send if record else bridge.send_internal
    resolved = await send("DOM.resolveNode", {"backendNodeId": backend_node_id})
    object_id = resolved.get("object", {}).get("objectId")
    if not isinstance(object_id, str) or not object_id:
        raise ReplayRefused("could not resolve semantic target to a DOM node")
    return object_id


async def click_element(bridge: CDPBridge, backend_node_id: int, *, record: bool = False) -> None:
    """Click one element through its DOM node handle.

    With ``record=True`` the node resolution and the click go through
    ``send``, so a cold-path recorder can map the object back to a unique
    labelled element and record a replayable ``TERX.click``.
    """
    object_id = await _object_id(bridge, backend_node_id, record=record)
    send = bridge.send if record else bridge.send_internal
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


async def set_checked_element(bridge: CDPBridge, backend_node_id: int, checked: bool) -> None:
    """Make a checkbox, switch, or radio end in ``checked`` state.

    It clicks only when the current state differs, so replaying "turn updates
    on" never turns them off because they were already on.
    """
    object_id = await _object_id(bridge, backend_node_id)
    result = await bridge.send_internal(
        "Runtime.callFunctionOn",
        {
            "objectId": object_id,
            "functionDeclaration": _SET_CHECKED,
            "arguments": [{"value": checked}],
            "returnByValue": True,
        },
    )
    outcome = result.get("result", {}).get("value")
    if outcome != "ok":
        raise ReplayRefused(f"could not set checked state ({outcome})")


async def select_option_element(bridge: CDPBridge, backend_node_id: int, option: str) -> None:
    """Choose the single ``<select>`` option whose visible label is ``option``."""
    object_id = await _object_id(bridge, backend_node_id)
    result = await bridge.send_internal(
        "Runtime.callFunctionOn",
        {
            "objectId": object_id,
            "functionDeclaration": _SELECT_OPTION,
            "arguments": [{"value": option}],
            "returnByValue": True,
        },
    )
    outcome = result.get("result", {}).get("value")
    if outcome != "ok":
        raise ReplayRefused(f"could not select option {option!r} ({outcome})")


async def press_key_element(bridge: CDPBridge, backend_node_id: int, key: str) -> None:
    """Focus one element and press ``Enter``, ``Tab`` or ``Escape`` on it."""
    event = _KEY_EVENTS.get(key)
    if event is None:
        raise ReplayRefused(f"key {key!r} is not replayable")
    await bridge.send_internal("DOM.focus", {"backendNodeId": backend_node_id})
    down_type = "keyDown" if "text" in event else "rawKeyDown"
    await bridge.send_internal("Input.dispatchKeyEvent", {"type": down_type, **event})
    up = {name: value for name, value in event.items() if name != "text"}
    await bridge.send_internal("Input.dispatchKeyEvent", {"type": "keyUp", **up})


# ------------------------------------------------------------------ #
# Targets: recording and multi-signal resolution                       #
# ------------------------------------------------------------------ #


async def element_test_id(bridge: CDPBridge, backend_node_id: int) -> str | None:
    """Return the element's developer-assigned test id attribute, if any."""
    try:
        described = await bridge.send_internal(
            "DOM.describeNode", {"backendNodeId": backend_node_id}
        )
    except Exception:
        return None
    attributes = described.get("node", {}).get("attributes") or []
    pairs = dict(zip(attributes[::2], attributes[1::2]))
    for name in TEST_ID_ATTRIBUTES:
        value = pairs.get(name)
        if isinstance(value, str) and value:
            return value
    return None


async def describe_target(bridge: CDPBridge, element: AXElement) -> dict[str, Any]:
    """Build the full persisted target for an element seen on the cold path."""
    target = _base_target(element)
    test_id = await element_test_id(bridge, element.backend_dom_id)
    if test_id:
        target["test_id"] = test_id
    return target


def _base_target(element: AXElement) -> dict[str, Any]:
    target: dict[str, Any] = {
        "role": element.role,
        "label": element.semantic_name,
        "ordinal": element.ordinal,
    }
    if element.frame:
        target["frame"] = element.frame
    return target


def _target_for_backend_node(
    backend_node_id: Any, snapshot: DOMSnapshot | None
) -> dict[str, Any] | None:
    """Create a durable action target from a unique labelled AX element.

    Used by the synchronous CDP recorder, so it records role, label, frame,
    and ordinal but not a test id (which needs another CDP call).
    """
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
    return _base_target(element)


def _find_replay_target(snapshot: DOMSnapshot, target: Any) -> AXElement:
    """Resolve a target by role and accessible name alone (no other signals)."""
    role, label, frame = _target_fields(target)
    matches = [
        element
        for element in snapshot.elements
        if element.role == role and element.semantic_name == label and element.frame == frame
    ]
    if len(matches) != 1:
        raise ReplayRefused(
            f"semantic target is {'missing' if not matches else 'ambiguous'}: {role} {label!r}"
        )
    return matches[0]


def _target_fields(target: Any) -> tuple[str, str, str]:
    if not isinstance(target, dict):
        raise ReplayRefused("invalid semantic target")
    role = target.get("role")
    label = target.get("label")
    frame = target.get("frame", "")
    if not isinstance(role, str) or not isinstance(label, str) or not label:
        raise ReplayRefused("semantic target requires role and label")
    if not isinstance(frame, str):
        raise ReplayRefused("invalid semantic target frame")
    return role, label, frame


async def _locate_target(
    bridge: CDPBridge, snapshot: DOMSnapshot, target: Any
) -> tuple[AXElement, str | None]:
    """Resolve a target only when its recorded signals agree on one element.

    1. Role and accessible name (in the recorded frame) must not match more
       than one element; if they do, refuse.
    2. If they match exactly one element and a test id was recorded, that
       element's test id must not be a *different* one; if it is, the signals
       disagree and TERX refuses.
    3. If the name matches nothing, the recorded test id may identify the
       element instead (for example after a label was translated), but only
       when exactly one same-role element has it *and* its position among
       same-role elements is unchanged.

    Returns the element and, when it was found by test id, a note describing
    the label drift for the replay report.
    """
    role, label, frame = _target_fields(target)
    pool = [
        element for element in snapshot.elements if element.role == role and element.frame == frame
    ]
    by_label = [element for element in pool if element.semantic_name == label]
    recorded_test_id = target.get("test_id")
    if len(by_label) > 1:
        raise ReplayRefused(f"semantic target is ambiguous: {role} {label!r}")
    if len(by_label) == 1:
        element = by_label[0]
        if isinstance(recorded_test_id, str):
            current = await element_test_id(bridge, element.backend_dom_id)
            if current and current != recorded_test_id:
                raise ReplayRefused(
                    f"target signals disagree: {role} {label!r} now has test id "
                    f"{current!r}, recorded {recorded_test_id!r}"
                )
        return element, None
    if not isinstance(recorded_test_id, str):
        raise ReplayRefused(f"semantic target is missing: {role} {label!r}")
    by_test_id = [
        element
        for element in pool
        if await element_test_id(bridge, element.backend_dom_id) == recorded_test_id
    ]
    if len(by_test_id) != 1:
        state = "missing" if not by_test_id else "ambiguous"
        raise ReplayRefused(f"semantic target is {state}: {role} {label!r}")
    element = by_test_id[0]
    if target.get("ordinal") != element.ordinal:
        raise ReplayRefused(
            f"semantic target is missing: {role} {label!r} (test id {recorded_test_id!r} "
            "matched an element in a different position)"
        )
    note = (
        f"{role} {label!r} matched by test id {recorded_test_id!r} and position; "
        f"its label is now {element.semantic_name!r}"
    )
    return element, note


async def _resolve_replay_target(
    bridge: CDPBridge, target: Any, timeout: float
) -> tuple[AXElement, str | None]:
    """Wait for a semantic target to appear; refuse at once on ambiguity.

    A click often starts a navigation or an async render, so the next target
    may not exist yet. A missing target is polled until ``timeout``; snapshot
    errors while a page is mid-navigation are treated as "not yet".
    """
    deadline = time.monotonic() + timeout
    while True:
        try:
            snapshot = await DOMExtractor().snapshot(bridge)
            return await _locate_target(bridge, snapshot, target)
        except ReplayRefused as exc:
            if "ambiguous" in exc.reason or "disagree" in exc.reason:
                raise
            if time.monotonic() >= deadline:
                raise
        except Exception:
            if time.monotonic() >= deadline:
                raise
        await asyncio.sleep(REPLAY_POLL_INTERVAL)


# ------------------------------------------------------------------ #
# Replay                                                               #
# ------------------------------------------------------------------ #


def _preflight_replay(commands: list[CDPCommand], variables: dict[str, Any]) -> None:
    """Statically validate a whole cached sequence before any action runs.

    Raises ``ReplayRefused`` for a malformed or unsupported action and
    ``MissingReplayVariable`` for any placeholder without a supplied value, so
    neither can surface after earlier steps have already changed the page.
    """
    for command in commands:
        if command.method not in REPLAY_ACTIONS:
            raise ReplayRefused(f"unsupported cached action: {command.method}")
        try:
            validate_action(command.method, command.params)
        except ValueError as exc:
            raise ReplayRefused(f"invalid cached action: {exc}") from exc
        if command.method == "TERX.navigate":
            url = _interpolate_url(command.params["url"], variables)
            if not _is_replayable_url(url):
                raise ReplayRefused("cached navigation URL is not permitted")
        elif command.method == "TERX.type":
            _interpolate_placeholders(command.params["text"], variables)
        elif command.method == "TERX.select":
            _interpolate_placeholders(command.params["option"], variables)


async def _execute_replay_action(
    bridge: CDPBridge,
    command: CDPCommand,
    variables: dict[str, Any],
    *,
    timeout: float = 10.0,
) -> str | None:
    """Execute one TERX semantic action without triggering bridge recorders.

    Returns a note when the target was found by a fallback signal.
    """
    if command.method not in REPLAY_ACTIONS:
        raise ReplayRefused(f"unsupported cached action: {command.method}")
    if command.method == "TERX.navigate":
        template = command.params.get("url")
        if not isinstance(template, str):
            raise ReplayRefused("cached navigation URL is not permitted")
        url = _interpolate_url(template, variables)
        if not _is_replayable_url(url):
            raise ReplayRefused("cached navigation URL is not permitted")
        await bridge.send_internal("Page.navigate", {"url": url})
        await bridge.wait_for_load(timeout=timeout)
        return None

    element, note = await _resolve_replay_target(bridge, command.params.get("target"), timeout)
    node = element.backend_dom_id
    if command.method == "TERX.type":
        text = command.params.get("text")
        if not isinstance(text, str):
            raise ReplayRefused("cached text action has no string value")
        await replace_field_text(bridge, node, _interpolate_placeholders(text, variables))
        return note
    if command.method == "TERX.check":
        await set_checked_element(bridge, node, bool(command.params.get("checked")))
    elif command.method == "TERX.select":
        option = _interpolate_placeholders(str(command.params.get("option", "")), variables)
        await select_option_element(bridge, node, option)
    elif command.method == "TERX.press":
        await press_key_element(bridge, node, str(command.params.get("key", "")))
    else:
        await click_element(bridge, node)
    # These actions may start a navigation; let a started load settle before
    # the next step snapshots the page. Target polling covers late loads.
    await bridge.wait_for_load(timeout=timeout)
    return note
