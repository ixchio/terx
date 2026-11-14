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
from typing import Any, TypeVar

from terx.cache.actions import (
    click_element,
    describe_target,
    press_key_element,
    replace_field_text,
    select_option_element,
    set_checked_element,
)
from terx.cache.conditions import _assert_postcondition
from terx.cache.errors import ReplayRefused
from terx.cache.models import CDPCommand, ReplayCostLedger, ReplayReport
from terx.cache.policy import (
    PLACEHOLDER_RE,
    PRESS_KEYS,
    ApprovalVerifier,
    SideEffect,
    _is_replayable_url,
)
from terx.cache.redaction import _interpolate_url, _templatize_url
from terx.cache.recording import RecordingContext, session_for
from terx.cache.storage import MemoryCache
from terx.cdp.bridge import CDPBridge
from terx.dom.extractor import AXElement, DOMExtractor, DOMSnapshot
from terx.tools import call_result_reader

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

    Every element action takes the element's accessible ``role`` and ``label``,
    and an optional ``frame`` (the iframe path, for example ``"Checkout"``)
    for elements inside a same-origin iframe.
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
        """Navigate to a replayable HTTP(S) origin-and-path URL.

        The path may contain ``{{variable}}`` placeholders for declared
        variables. A path segment that equals a declared variable's value (3
        characters or longer) is recorded as that placeholder automatically, so
        ``/orders/A-100`` replays as ``/orders/<new order_id>``.
        """
        if not isinstance(url, str) or not _is_replayable_url(url):
            raise ValueError(
                "TERX only records HTTP(S) navigation without query, fragment, or credentials"
            )
        for name in PLACEHOLDER_RE.findall(url):
            if name not in self._variables:
                raise ValueError(f"TERX variable {name!r} was not supplied to this workflow")
        template, problems = _templatize_url(url, self._variables)
        if problems:
            raise ValueError("; ".join(problems))
        concrete = _interpolate_url(template, self._variables)
        started = time.perf_counter()
        await self._bridge.send_internal("Page.navigate", {"url": concrete})
        await self._bridge.wait_for_load(timeout=10.0)
        self._snapshot = None
        self._record("TERX.navigate", {"url": template}, started)

    async def click(self, role: str, label: str, *, frame: str = "") -> None:
        """Click exactly one accessible element identified by role and label."""
        element, target = await self._target(role, label, frame)
        started = time.perf_counter()
        await click_element(self._bridge, element.backend_dom_id)
        # A click can alter the current page without a navigation event.
        self._snapshot = None
        self._record("TERX.click", {"target": target}, started)

    async def type_into(self, role: str, label: str, variable: str, *, frame: str = "") -> None:
        """Replace one labelled input's value with a declared variable.

        ``variable`` must be a lower-snake-case key in the workflow's
        ``variables`` mapping. Its literal value is sent only to Chrome and is
        persisted by TERX only as ``{{variable}}``.
        """
        self._require_variable(variable)
        element, target = await self._target(role, label, frame)
        started = time.perf_counter()
        await replace_field_text(
            self._bridge, element.backend_dom_id, str(self._variables[variable])
        )
        placeholder = f"{{{{{variable}}}}}"
        self._record(
            "TERX.type",
            {"target": target, "text": placeholder},
            started,
            {"redacted": True, "placeholder": placeholder, "placeholder_source": "variable"},
        )

    async def set_checked(
        self, role: str, label: str, checked: bool = True, *, frame: str = ""
    ) -> None:
        """Make a checkbox, switch, or radio end checked (or unchecked).

        Unlike ``click``, this sets a state: replay leaves an already-checked
        box checked instead of toggling it off.
        """
        if not isinstance(checked, bool):
            raise ValueError("checked must be True or False")
        element, target = await self._target(role, label, frame)
        started = time.perf_counter()
        await set_checked_element(self._bridge, element.backend_dom_id, checked)
        self._snapshot = None
        self._record("TERX.check", {"target": target, "checked": checked}, started)

    async def select_option(self, role: str, label: str, variable: str, *, frame: str = "") -> None:
        """Choose the ``<select>`` option whose visible label is a declared variable."""
        self._require_variable(variable)
        element, target = await self._target(role, label, frame)
        started = time.perf_counter()
        await select_option_element(
            self._bridge, element.backend_dom_id, str(self._variables[variable])
        )
        self._snapshot = None
        self._record("TERX.select", {"target": target, "option": f"{{{{{variable}}}}}"}, started)

    async def press(self, role: str, label: str, key: str, *, frame: str = "") -> None:
        """Press ``Enter``, ``Tab``, or ``Escape`` on one labelled element."""
        if key not in PRESS_KEYS:
            raise ValueError(f"key must be one of {', '.join(sorted(PRESS_KEYS))}")
        element, target = await self._target(role, label, frame)
        started = time.perf_counter()
        await press_key_element(self._bridge, element.backend_dom_id, key)
        self._snapshot = None
        self._record("TERX.press", {"target": target, "key": key}, started)

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

    def _require_variable(self, variable: str) -> None:
        if not isinstance(variable, str) or _VARIABLE_NAME.fullmatch(variable) is None:
            raise ValueError("TERX variable names must be lower-snake-case identifiers")
        if variable not in self._variables:
            raise ValueError(f"TERX variable {variable!r} was not supplied to this workflow")

    def _record(
        self,
        method: str,
        params: dict[str, Any],
        started: float,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        self._context.record_command(
            CDPCommand(method, params, {}, (time.perf_counter() - started) * 1000, metadata or {})
        )

    async def _target(self, role: str, label: str, frame: str) -> tuple[AXElement, dict[str, Any]]:
        element = await self._element(role, label, frame)
        return element, await describe_target(self._bridge, element)

    async def _element(self, role: str, label: str, frame: str = "") -> AXElement:
        if not isinstance(role, str) or not role or not isinstance(label, str) or not label:
            raise ValueError("TERX semantic targets need a non-empty role and label")
        if self._snapshot is None:
            self._snapshot = await self._extractor.snapshot(self._bridge)
        matches = [
            element
            for element in self._snapshot.elements
            if element.role == role and element.semantic_name == label and element.frame == frame
        ]
        if len(matches) != 1 and not frame:
            # Not in the main frame: accept a unique match inside any iframe.
            framed = [
                element
                for element in self._snapshot.elements
                if element.role == role and element.semantic_name == label
            ]
            if len(framed) == 1:
                matches = framed
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
        side_effect: SideEffect = "mutating",
        ttl_seconds: int | None = 86_400,
        mutation_guard: bool = True,
        mutation_threshold: int = 20,
        approval_verifier: ApprovalVerifier | None = None,
        replay_timeout: float = 10.0,
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
        self.replay_timeout = replay_timeout

    async def run(
        self,
        cold_path: Callable[[TerxActions], Any] | None,
        *,
        variables: dict[str, Any] | None = None,
        result_reader: Callable[[CDPBridge], Awaitable[Any] | Any] | None = None,
        approval_token: str | None = None,
    ) -> WorkflowRunResult:
        """Run ``cold_path`` once or replay it when its policy matches.

        ``variables`` can replace values declared in the workflow constructor;
        undeclared names are rejected so a replay contract cannot silently grow.
        A ``result_reader`` runs after either a successful cold path or replay
        and is never cached. It lets a caller read current structured page data
        instead of receiving ``None`` on a warm hit.
        """
        run_variables = dict(self.variables)
        if variables is not None:
            unexpected = sorted(set(variables) - set(self.variables))
            if unexpected:
                raise ValueError(
                    "TERX workflow variables must be declared at construction: "
                    + ", ".join(unexpected)
                )
            run_variables.update(variables)
        value: Any = None
        cache_hit = False
        commands_recorded = 0

        async with session_for(
            self.cache,
            self.bridge,
            self.task,
            variables=run_variables,
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
            replay_timeout=self.replay_timeout,
        ) as context:
            cache_hit = context.hit
            if cache_hit:
                await context.replay(approval_token=approval_token)
            else:
                if cold_path is None:
                    raise RuntimeError("TERX cache miss requires an explicit cold_path")
                value = cold_path(TerxActions(self.bridge, context, run_variables))
                if inspect.isawaitable(value):
                    value = await value
                commands_recorded = context.recorded_commands
            if result_reader is not None:
                value = await call_result_reader(result_reader, self.bridge)

        return WorkflowRunResult(
            value=value,
            cache_hit=cache_hit,
            commands_recorded=commands_recorded,
            ledger=context.ledger,
            report=context.report,
        )
