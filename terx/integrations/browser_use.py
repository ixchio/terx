"""
Browser Use integration surface.

Experimental adapter for agents that deliberately drive the supplied TERX CDP
bridge. Standard Browser Use sessions do not use that bridge and therefore are
not captured by this adapter.
"""

from __future__ import annotations

import inspect
from dataclasses import dataclass
from typing import Any

from terx.cache.cache import (
    ApprovalVerifier,
    MemoryCache,
    ReplayCostLedger,
    ReplayReport,
    session_for,
)
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
        scope_id: str | None = None,
        route_pattern: str | None = None,
        workflow_version: int = 1,
        side_effect: str = "mutating",
        ttl_seconds: int | None = 86_400,
        precondition: dict[str, Any] | None = None,
        postcondition: dict[str, Any] | Any | None = None,
        redact_secrets: bool = True,
        mutation_guard: bool = True,
        mutation_threshold: int = 20,
        approval_verifier: ApprovalVerifier | None = None,
    ) -> None:
        if not hasattr(agent, "run"):
            raise TypeError("Browser Use adapter expects an object with a run() method")
        self.agent = agent
        self.cache = cache
        self.bridge = bridge
        self.task = task
        self.variables = variables or {}
        self.scope_id = scope_id
        self.route_pattern = route_pattern
        self.workflow_version = workflow_version
        self.side_effect = side_effect
        self.ttl_seconds = ttl_seconds
        self.precondition = precondition
        self.postcondition = postcondition
        self.redact_secrets = redact_secrets
        self.mutation_guard = mutation_guard
        self.mutation_threshold = mutation_threshold
        self.approval_verifier = approval_verifier

    async def run(
        self,
        task: str | None = None,
        *args: Any,
        approval_token: str | None = None,
        **kwargs: Any,
    ) -> BrowserUseRunResult:
        task_description = task or self.task or getattr(self.agent, "task", None)
        if not task_description:
            raise ValueError(
                "Provide a TERX task description or use an agent with a .task attribute"
            )

        async with session_for(
            self.cache,
            self.bridge,
            str(task_description),
            variables=self.variables,
            scope_id=self.scope_id,
            route_pattern=self.route_pattern,
            workflow_version=self.workflow_version,
            side_effect=self.side_effect,
            precondition=self.precondition,
            postcondition=self.postcondition,
            ttl_seconds=self.ttl_seconds,
            redact_secrets=self.redact_secrets,
            mutation_guard=self.mutation_guard,
            mutation_threshold=self.mutation_threshold,
            approval_verifier=self.approval_verifier,
        ) as ctx:
            if ctx.hit:
                await ctx.replay(approval_token=approval_token)
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
    scope_id: str | None = None,
    route_pattern: str | None = None,
    workflow_version: int = 1,
    side_effect: str = "mutating",
    ttl_seconds: int | None = 86_400,
    precondition: dict[str, Any] | None = None,
    postcondition: dict[str, Any] | Any | None = None,
    redact_secrets: bool = True,
    mutation_guard: bool = True,
    mutation_threshold: int = 20,
    approval_verifier: ApprovalVerifier | None = None,
) -> TerxBrowserUseAdapter:
    """Return a TERX memory wrapper for a Browser Use-style agent."""
    return TerxBrowserUseAdapter(
        agent,
        cache=cache,
        bridge=bridge,
        task=task,
        variables=variables,
        scope_id=scope_id,
        route_pattern=route_pattern,
        workflow_version=workflow_version,
        side_effect=side_effect,
        ttl_seconds=ttl_seconds,
        precondition=precondition,
        postcondition=postcondition,
        redact_secrets=redact_secrets,
        mutation_guard=mutation_guard,
        mutation_threshold=mutation_threshold,
        approval_verifier=approval_verifier,
    )
