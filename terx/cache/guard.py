"""The per-step DOM mutation guard used during replay."""

from __future__ import annotations

import logging

from terx.cdp.bridge import CDPBridge


logger = logging.getLogger(__name__)


async def _start_mutation_guard(bridge: CDPBridge) -> bool:
    expression = """
(() => {
  const key = "__TERX_MUTATION_GUARD__";
  if (window[key] && window[key].observer) {
    window[key].observer.disconnect();
  }
  const root = document.documentElement || document.body;
  if (!root) {
    return false;
  }
  const state = { count: 0, observer: null };
  const observer = new MutationObserver((mutations) => {
    for (const mutation of mutations) {
      state.count += mutation.addedNodes.length + mutation.removedNodes.length;
    }
  });
  observer.observe(root, { childList: true, subtree: true });
  state.observer = observer;
  window[key] = state;
  return true;
})()
"""
    try:
        result = await bridge.send_internal(
            "Runtime.evaluate", {"expression": expression, "returnByValue": True}
        )
        return bool(result.get("result", {}).get("value"))
    except Exception as exc:
        logger.debug("Mutation guard start failed: %s", exc)
        return False


async def _read_mutation_count(bridge: CDPBridge) -> int | None:
    """Return the observed count, or ``None`` when the observer is gone.

    A navigation replaces ``window``, so a missing guard is not proof of zero
    mutations; callers reinstall it and accumulate across steps.
    """
    expression = "window.__TERX_MUTATION_GUARD__ ? window.__TERX_MUTATION_GUARD__.count : null"
    try:
        result = await bridge.send_internal(
            "Runtime.evaluate", {"expression": expression, "returnByValue": True}
        )
        value = result.get("result", {}).get("value")
        if value is None:
            return None
        return int(value)
    except Exception as exc:
        logger.debug("Mutation guard read failed: %s", exc)
        return None


async def _stop_mutation_guard(bridge: CDPBridge) -> None:
    expression = """
(() => {
  const state = window.__TERX_MUTATION_GUARD__;
  if (state && state.observer) {
    state.observer.disconnect();
  }
  delete window.__TERX_MUTATION_GUARD__;
})()
"""
    try:
        await bridge.send_internal("Runtime.evaluate", {"expression": expression})
    except Exception as exc:
        logger.debug("Mutation guard stop failed: %s", exc)
