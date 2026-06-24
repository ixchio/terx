"""
Browser Use integration surface.

This adapter intentionally wraps the public Agent.run()-style contract instead of
depending on Browser Use internals. The wrapped agent must drive the same TERX
CDP bridge for commands to be recorded; if it does not, TERX still short-circuits
cache hits, but misses will report that no commands were captured.
"""

from __future__ import annotations

import inspect
from dataclasses import dataclass
from typing import Any

from terx.cache.cache import MemoryCache, ReplayCostLedger, ReplayReport, session_for
from terx.cdp.bridge import CDPBridge


@dataclass
class BrowserUseRunResult:
    """Result returned by TerxBrowserUseAdapter.run()."""

    value: Any
    cache_hit: bool
    commands_recorded: int
    ledger: ReplayCostLedger | None
    report: ReplayReport | None = None


class TerxBrowserUseAdapter:
    """
    Wrap a Browser Use-style agent with TERX memory.

    The agent only needs a ``run`` method. This keeps TERX decoupled from
    Browser Use version-specific constructor details.
    """

    def __init__(
        self,
        agent: Any,
        *,
        cache: MemoryCache,
        bridge: CDPBridge,
        task: str | None = None,
        variables: dict[str, Any] | None = None,
        postcondition: dict[str, Any] | Any | None = None,
        redact_secrets: bool = True,
        mutation_guard: bool = True,
        mutation_threshold: int = 20,
    ) -> None:
        if not hasattr(agent, "run"):
            raise TypeError("Browser Use adapter expects an object with a run() method")
        self.agent = agent
        self.cache = cache
        self.bridge = bridge
        self.task = task
        self.variables = variables or {}
        self.postcondition = postcondition
        self.redact_secrets = redact_secrets
        self.mutation_guard = mutation_guard
        self.mutation_threshold = mutation_threshold

    async def run(self, task: str | None = None, *args: Any, **kwargs: Any) -> BrowserUseRunResult:
        task_description = task or self.task or getattr(self.agent, "task", None)
        if not task_description:
            raise ValueError("Provide a TERX task description or use an agent with a .task attribute")

        async with session_for(
            self.cache,
            self.bridge,
            str(task_description),
            variables=self.variables,
            postcondition=self.postcondition,
            redact_secrets=self.redact_secrets,
            mutation_guard=self.mutation_guard,
            mutation_threshold=self.mutation_threshold,
        ) as ctx:
            if ctx.hit:
                await ctx.replay()
                return BrowserUseRunResult(
                    value=None,
                    cache_hit=True,
                    commands_recorded=0,
                    ledger=ctx.ledger,
                    report=ctx.report,
                )

            value = self.agent.run(*args, **kwargs)
            if inspect.isawaitable(value):
                value = await value

        return BrowserUseRunResult(
            value=value,
            cache_hit=False,
            commands_recorded=ctx.recorded_commands,
            ledger=ctx.ledger,
            report=ctx.report,
        )


def wrap_browser_use(
    agent: Any,
    *,
    cache: MemoryCache,
    bridge: CDPBridge,
    task: str | None = None,
    variables: dict[str, Any] | None = None,
    postcondition: dict[str, Any] | Any | None = None,
    redact_secrets: bool = True,
    mutation_guard: bool = True,
    mutation_threshold: int = 20,
) -> TerxBrowserUseAdapter:
    """Return a TERX memory wrapper for a Browser Use-style agent."""
    return TerxBrowserUseAdapter(
        agent,
        cache=cache,
        bridge=bridge,
        task=task,
        variables=variables,
        postcondition=postcondition,
        redact_secrets=redact_secrets,
        mutation_guard=mutation_guard,
        mutation_threshold=mutation_threshold,
    )
