"""Dependency-free semantic workflow integration for TERX.

This module builds on TERX's existing CDP bridge. It does not import a browser
framework, launch a second browser, start a daemon, or retain page snapshots
between calls. It exposes only the cold-path actions TERX can prove and replay.
"""

from __future__ import annotations

import asyncio
import inspect
import re
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any, Literal, TypeVar

from terx.cache.cache import (
    ApprovalVerifier,
    CDPCommand,
    MemoryCache,
    ReplayCostLedger,
    ReplayReport,
    ReplayRefused,
    RecordingContext,
    _assert_postcondition,
    _is_replayable_url,
    session_for,
)
from terx.cdp.bridge import CDPBridge
from terx.dom.extractor import AXElement, DOMExtractor, DOMSnapshot

T = TypeVar("T")
_VARIABLE_NAME = re.compile(r"[a-z_][a-z0-9_]*\Z")
_WAIT_CONDITIONS = {"url_contains", "title_contains", "text_contains", "selector_exists"}


@dataclass(frozen=True)
class WorkflowRunResult:
    """The single result shape returned by :meth:`TerxWorkflow.run`."""

    value: Any
    cache_hit: bool
    commands_recorded: int
    ledger: ReplayCostLedger | None
    report: ReplayReport | None


class TerxActions:
    """Cold-path actions eligible for TERX semantic replay.

    Do not mix direct mutating calls on the bridge with this object. TERX can
    prove and replay only this intentionally small surface, which prevents a
    successful cold path from silently becoming a partial or unsafe replay.
    """

    def __init__(
        self,
        bridge: CDPBridge,
        context: RecordingContext,
        variables: dict[str, Any],
    ) -> None:
        self._bridge = bridge
        self._context = context
        self._variables = variables
        self._extractor = DOMExtractor()
        self._snapshot: DOMSnapshot | None = None

    async def navigate(self, url: str) -> None:
        """Navigate to a replayable HTTP(S) origin-and-path URL."""
        if not isinstance(url, str) or not _is_replayable_url(url):
            raise ValueError(
                "TERX only records HTTP(S) navigation without query, fragment, or credentials"
            )
        started = time.perf_counter()
        await self._bridge.send_internal("Page.navigate", {"url": url})
        await self._bridge.wait_for_load(timeout=10.0)
        self._snapshot = None
        self._context.record_command(
            CDPCommand(
                "TERX.navigate",
                {"url": url},
                {},
                (time.perf_counter() - started) * 1000,
            )
        )

    async def click(self, role: str, label: str) -> None:
        """Click exactly one accessible element identified by role and label."""
        element = await self._element(role, label)
        started = time.perf_counter()
        resolved = await self._bridge.send_internal(
            "DOM.resolveNode", {"backendNodeId": element.backend_dom_id}
        )
        object_id = resolved.get("object", {}).get("objectId")
        if not isinstance(object_id, str) or not object_id:
            raise ReplayRefused("could not resolve semantic click target")
        await self._bridge.send_internal(
            "Runtime.callFunctionOn",
            {
                "objectId": object_id,
                "functionDeclaration": "function() { this.click(); }",
            },
        )
        # A click can alter the current page without a navigation event.
        self._snapshot = None
        self._context.record_command(
            CDPCommand(
                "TERX.click",
                {"target": {"role": element.role, "label": element.semantic_name}},
                {},
                (time.perf_counter() - started) * 1000,
            )
        )

    async def type_into(self, role: str, label: str, variable: str) -> None:
        """Type one declared value into exactly one labelled input.

        ``variable`` must be a lower-snake-case key in the workflow's
        ``variables`` mapping. Its literal value is sent only to Chrome and is
        persisted by TERX only as ``{{variable}}``.
        """
        if not isinstance(variable, str) or _VARIABLE_NAME.fullmatch(variable) is None:
            raise ValueError("TERX variable names must be lower-snake-case identifiers")
        if variable not in self._variables:
            raise ValueError(f"TERX variable {variable!r} was not supplied to this workflow")

        element = await self._element(role, label)
        started = time.perf_counter()
        await self._bridge.send_internal("DOM.focus", {"backendNodeId": element.backend_dom_id})
        await self._bridge.send_internal(
            "Input.insertText", {"text": str(self._variables[variable])}
        )
        placeholder = f"{{{{{variable}}}}}"
        self._context.record_command(
            CDPCommand(
                "TERX.type",
                {
                    "target": {"role": element.role, "label": element.semantic_name},
                    "text": placeholder,
                },
                {},
                (time.perf_counter() - started) * 1000,
                {
                    "redacted": True,
                    "placeholder": placeholder,
                    "placeholder_source": "variable",
                },
            )
        )

    async def wait_for(
        self, condition: dict[str, str], *, timeout: float = 10.0, poll_interval: float = 0.1
    ) -> None:
        """Wait for a safe TERX condition without creating a replay action."""
        if not isinstance(condition, dict) or not condition:
            raise ValueError("wait_for requires a non-empty TERX condition dictionary")
        if set(condition) - _WAIT_CONDITIONS:
            raise ValueError("wait_for supports only TERX's declarative condition keys")
        if not all(isinstance(value, str) and value for value in condition.values()):
            raise ValueError("wait_for condition values must be non-empty strings")
        if timeout <= 0 or poll_interval <= 0:
            raise ValueError("timeout and poll_interval must be positive")

        deadline = time.monotonic() + timeout
        while True:
            try:
                await _assert_postcondition(self._bridge, condition)
                return
            except Exception as exc:
                if time.monotonic() >= deadline:
                    raise TimeoutError(f"TERX condition was not met: {condition}") from exc
                await asyncio.sleep(poll_interval)

    async def _element(self, role: str, label: str) -> AXElement:
        if not isinstance(role, str) or not role or not isinstance(label, str) or not label:
            raise ValueError("TERX semantic targets need a non-empty role and label")
        if self._snapshot is None:
            self._snapshot = await self._extractor.snapshot(self._bridge)
        matches = [
            element
            for element in self._snapshot.elements
            if element.role == role and element.semantic_name == label
        ]
        if len(matches) != 1:
            state = "missing" if not matches else "ambiguous"
            raise ReplayRefused(f"semantic target is {state}: {role} {label!r}")
        return matches[0]


class TerxWorkflow:
    """Run an approved browser workflow with no framework dependency.

    A call adds only the work TERX already needs: one active CDP bridge,
    short-lived accessibility snapshots, and local SQLite access. On a warm
    hit, the supplied cold path is never called.
    """

    def __init__(
        self,
        *,
        cache: MemoryCache,
        bridge: CDPBridge,
        task: str,
        scope_id: str,
        precondition: dict[str, Any],
        postcondition: dict[str, Any],
        variables: dict[str, Any] | None = None,
        route_pattern: str | None = None,
        workflow_version: int = 1,
        side_effect: Literal["read_only", "mutating", "destructive"] = "mutating",
        ttl_seconds: int | None = 86_400,
        mutation_guard: bool = True,
        mutation_threshold: int = 20,
        approval_verifier: ApprovalVerifier | None = None,
    ) -> None:
        self.cache = cache
        self.bridge = bridge
        self.task = task
        self.scope_id = scope_id
        self.precondition = precondition
        self.postcondition = postcondition
        self.variables = dict(variables or {})
        self.route_pattern = route_pattern
        self.workflow_version = workflow_version
        self.side_effect = side_effect
        self.ttl_seconds = ttl_seconds
        self.mutation_guard = mutation_guard
        self.mutation_threshold = mutation_threshold
        self.approval_verifier = approval_verifier

    async def run(
        self,
        cold_path: Callable[[TerxActions], Awaitable[T] | T] | None,
        *,
        approval_token: str | None = None,
    ) -> WorkflowRunResult:
        """Run ``cold_path`` once or replay it when its policy matches."""
        value: Any = None
        cache_hit = False
        commands_recorded = 0

        async with session_for(
            self.cache,
            self.bridge,
            self.task,
            variables=self.variables,
            scope_id=self.scope_id,
            route_pattern=self.route_pattern,
            workflow_version=self.workflow_version,
            side_effect=self.side_effect,
            precondition=self.precondition,
            postcondition=self.postcondition,
            ttl_seconds=self.ttl_seconds,
            mutation_guard=self.mutation_guard,
            mutation_threshold=self.mutation_threshold,
            approval_verifier=self.approval_verifier,
        ) as context:
            cache_hit = context.hit
            if cache_hit:
                await context.replay(approval_token=approval_token)
            else:
                if cold_path is None:
                    raise RuntimeError("TERX cache miss requires an explicit cold_path")
                value = cold_path(TerxActions(self.bridge, context, self.variables))
                if inspect.isawaitable(value):
                    value = await value
                commands_recorded = context.recorded_commands

        return WorkflowRunResult(
            value=value,
            cache_hit=cache_hit,
            commands_recorded=commands_recorded,
            ledger=context.ledger,
            report=context.report,
        )
