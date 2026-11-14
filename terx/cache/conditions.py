"""Declarative page conditions: preconditions, postconditions, and waits."""

from __future__ import annotations

import asyncio
import inspect
import json
import logging
import time
from typing import Any

from terx.cdp.bridge import CDPBridge
from terx.cache.errors import PostconditionFailed
from terx.cache.policy import REPLAY_CONDITION_KEYS


logger = logging.getLogger(__name__)

REPLAY_POLL_INTERVAL = 0.1


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

    # Only TERX's declarative checks are evaluated. An unknown key (including
    # the removed ``js`` check) fails closed instead of running page script.
    unsupported = sorted(set(postcondition) - REPLAY_CONDITION_KEYS)
    if unsupported:
        raise ValueError(f"unsupported condition checks: {', '.join(map(str, unsupported))}")

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

    if not checks:
        return

    failed = [name for name, ok in checks if not ok]
    if failed:
        raise PostconditionFailed(", ".join(failed))


async def _wait_for_condition(bridge: CDPBridge, condition: Any, timeout: float) -> None:
    """Poll a declarative or callable condition until it holds or times out."""
    deadline = time.monotonic() + timeout
    while True:
        try:
            await _assert_postcondition(bridge, condition)
            return
        except PostconditionFailed:
            if time.monotonic() >= deadline:
                raise
        except (TypeError, ValueError):
            raise  # A malformed condition will not become valid by waiting.
        except Exception as exc:
            # The execution context is destroyed while a navigation commits.
            if time.monotonic() >= deadline:
                raise PostconditionFailed(f"condition could not be evaluated: {exc}") from exc
        await asyncio.sleep(REPLAY_POLL_INTERVAL)


def _postcondition_summary(postcondition: Any) -> Any:
    if callable(postcondition):
        return getattr(postcondition, "__name__", "callable")
    return postcondition
