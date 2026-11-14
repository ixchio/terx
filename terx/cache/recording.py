"""Recording contexts: observe a cold run, store it, and replay it behind policy checks."""

from __future__ import annotations

import inspect
import json
import logging
import time
import uuid
from typing import Any, NoReturn
from urllib.parse import urlparse

from terx.cdp.bridge import CDPBridge
from terx.dom.extractor import DOMExtractor, DOMSnapshot
from terx.cache.errors import (
    CacheReplayError,
    MissingReplayVariable,
    MutationDriftError,
    PostconditionFailed,
    ReplayRefused,
)
from terx.cache.policy import (
    validate_action,
    SideEffect,
    ApprovalDecision,
    ApprovalVerifier,
    REPLAY_ACTIONS,
    ReplayApprovalRequest,
    ReplayDecision,
    ReplayPolicy,
    _is_replayable_url,
)
from terx.cache.models import CDPCommand, CachedSequence, ReplayCostLedger, ReplayReport
from terx.cache.redaction import (
    PLACEHOLDER_RE,
    _normalize_variables,
    _placeholders_in_commands,
    _prepare_recorded_params,
    _templatize_url,
    _redacted_fields_in_commands,
    _replay_command_digest,
)
from terx.cache.conditions import _assert_postcondition, _postcondition_summary, _wait_for_condition
from terx.cache.actions import (
    MUTATING_CDP_METHODS,
    SAFE_CLICK_FUNCTIONS,
    _execute_replay_action,
    _preflight_replay,
    _target_for_backend_node,
)
from terx.cache.guard import _read_mutation_count, _start_mutation_guard, _stop_mutation_guard
from terx.cache.storage import MemoryCache, _task_key


logger = logging.getLogger(__name__)


class RecordingContext:
    """
    Context returned by session_for().

    Records a deliberately small semantic action set from a CDP bridge.

    Raw CDP is an implementation detail of a browser agent, not a stable or
    safe replay format. The recorder translates only navigation, labelled
    clicks, and labelled text entry into TERX actions; check, select, and press
    are recorded explicitly by the workflow adapter and MCP tools. Any other
    mutating action makes the run non-cacheable; it is still allowed to execute
    on the cold path.
    """

    def __init__(
        self,
        cache: MemoryCache,
        bridge: CDPBridge,
        task: str,
        session_id: str | None = None,
        variables: dict[str, Any] | None = None,
        scope_id: str | None = None,
        route_pattern: str | None = None,
        workflow_version: int = 1,
        side_effect: SideEffect = "mutating",
        precondition: dict[str, Any] | None = None,
        postcondition: dict[str, Any] | Any | None = None,
        ttl_seconds: int | None = 86_400,
        redact_secrets: bool = True,
        mutation_guard: bool = True,
        mutation_threshold: int = 20,
        approval_verifier: ApprovalVerifier | None = None,
        replay_timeout: float = 10.0,
    ) -> None:
        if replay_timeout <= 0:
            raise ValueError("replay_timeout must be positive")
        self._cache = cache
        self._bridge = bridge
        self._task = task
        self._session_id = (
            session_id or f"browser_session_{int(time.time())}-{uuid.uuid4().hex[:12]}"
        )
        self._variables = _normalize_variables(variables or {})
        self._policy = ReplayPolicy(
            scope_id=scope_id,
            route_pattern=route_pattern,
            workflow_version=workflow_version,
            side_effect=side_effect,
            precondition=precondition,
            postcondition=postcondition if isinstance(postcondition, dict) else None,
            ttl_seconds=ttl_seconds,
        )
        self._postcondition = postcondition
        self._redact_secrets = redact_secrets
        self._mutation_guard = mutation_guard
        self._mutation_threshold = mutation_threshold
        self._approval_verifier = approval_verifier
        self._replay_timeout = replay_timeout
        self._stored = False
        self._scope_digest = ""
        self._snapshot: DOMSnapshot | None = None
        self._target_snapshot: DOMSnapshot | None = None
        self._domain: str = "unknown"
        self._origin: str = ""
        self._route: str = "/"
        self._route_pattern: str = "*"
        self._cached_seq: CachedSequence | None = None
        self._run_number: int = 1
        self._recorded_commands: list[CDPCommand] = []
        self._active_target: dict[str, str] | None = None
        self._object_targets: dict[str, dict[str, str]] = {}
        self._unsafe_reasons: list[str] = []
        self._recording_precondition_verified = False
        self.ledger: ReplayCostLedger | None = None
        self.report: ReplayReport | None = None
        self.decision = ReplayDecision(status="miss", workflow_version=workflow_version)

    @property
    def hit(self) -> bool:
        return self._cached_seq is not None

    @property
    def recorded_commands(self) -> int:
        return len(self._recorded_commands)

    @property
    def stored(self) -> bool:
        """True only when this cold run actually wrote a replayable sequence."""
        return self._stored

    async def replay(
        self,
        variables: dict[str, Any] | None = None,
        *,
        approval_token: str | None = None,
    ) -> None:
        """Replay a cached semantic workflow after policy verification.

        TERX does not invoke an LLM during replay. A destructive
        workflow needs a host verifier that atomically consumes an approval for
        this replay identity; a bare token is never trusted.

        Every cached action and every required variable is validated before
        the first action runs or an approval is consumed. Semantic targets on
        later pages can only be resolved once those pages exist, so a target
        that never appears still refuses mid-sequence.
        """
        if self._cached_seq is None:
            raise RuntimeError("No cached sequence to replay (cache miss)")

        t0 = time.perf_counter()
        replay_variables = {**self._variables, **_normalize_variables(variables or {})}
        mutation_guard_started = False
        mutation_count: int | None = None

        try:
            try:
                await _assert_postcondition(self._bridge, self._policy.precondition)
            except PostconditionFailed as exc:
                self._refuse(f"precondition failed: {exc}")
            self.decision.preconditions_verified = True

            try:
                _preflight_replay(self._cached_seq.commands, replay_variables)
            except MissingReplayVariable as exc:
                self._refuse(f"missing replay variable: {exc.variable_name}")
            except ReplayRefused as exc:
                self._refuse(exc.reason)

            if self._policy.side_effect == "destructive":
                await self._verify_destructive_approval(approval_token)

            if self._mutation_guard:
                mutation_guard_started = await _start_mutation_guard(self._bridge)
                mutation_count = 0 if mutation_guard_started else None

            locator_notes: list[str] = []
            for cmd in self._cached_seq.commands:
                try:
                    note = await _execute_replay_action(
                        self._bridge, cmd, replay_variables, timeout=self._replay_timeout
                    )
                    if note:
                        locator_notes.append(note)
                except MissingReplayVariable as exc:
                    self._refuse(f"missing replay variable: {exc.variable_name}")
                except ReplayRefused as exc:
                    self._refuse(exc.reason)
                except Exception as exc:
                    self._fail(f"replay failed at {cmd.method}: {exc}")
                    raise CacheReplayError(cmd.method) from exc

                if mutation_guard_started:
                    # The observer lives in page JS and is lost on navigation,
                    # so accumulate after every step and reinstall it. Drift
                    # stops the sequence before the next action runs.
                    step_count = await _read_mutation_count(self._bridge)
                    total_mutations = (mutation_count or 0) + (step_count or 0)
                    mutation_count = total_mutations
                    if total_mutations > self._mutation_threshold:
                        self._fail(
                            f"mutation drift: {total_mutations} DOM changes exceeded "
                            f"threshold {self._mutation_threshold}"
                        )
                        raise MutationDriftError(total_mutations, self._mutation_threshold)
                    await _start_mutation_guard(self._bridge)

            try:
                await _wait_for_condition(
                    self._bridge, self._postcondition, timeout=self._replay_timeout
                )
            except PostconditionFailed as exc:
                self._fail(f"postcondition failed after replay: {exc}")
                raise
            self.decision.postconditions_verified = True
        finally:
            if mutation_guard_started:
                await _stop_mutation_guard(self._bridge)

        latency = (time.perf_counter() - t0) * 1000
        self._cache.increment_hit(
            self._domain,
            self._cached_seq.structural_hash,
            self._cached_seq.task_key,
            origin=self._cached_seq.origin,
            route_pattern=self._cached_seq.route_pattern,
            scope_hash=self._cached_seq.scope_hash,
            workflow_version=self._cached_seq.workflow_version,
        )
        self.decision.status = "hit"
        self.decision.scope_verified = True
        self.ledger = ReplayCostLedger(
            task_description=self._task,
            hit=True,
            commands_replayed=len(self._cached_seq.commands),
            latency_ms=latency,
            run_number=self._run_number,
            model_calls_during_replay=0,
        )
        self.report = ReplayReport(
            task_description=self._task,
            domain=self._domain,
            cache_hit=True,
            commands_replayed=len(self._cached_seq.commands),
            variables_used=_placeholders_in_commands(self._cached_seq.commands),
            redacted_fields=_redacted_fields_in_commands(self._cached_seq.commands),
            postcondition=_postcondition_summary(self._postcondition),
            latency_ms=latency,
            run_number=self._run_number,
            mutation_count=mutation_count,
            mutation_threshold=self._mutation_threshold if self._mutation_guard else None,
            status="hit",
            workflow_version=self._policy.workflow_version,
            route_pattern=self._route_pattern,
            scope_verified=True,
            preconditions_verified=True,
            postconditions_verified=True,
            locator_notes=locator_notes,
        )
        self._cache.write_audit(
            session_id=self._session_id,
            task_description=self._task,
            commands=self._cached_seq.commands,
            domain=self._domain,
            was_cache_hit=True,
        )

    def _refuse(self, reason: str) -> NoReturn:
        self.decision.status = "refused"
        self.decision.reasons = [reason]
        self.report = ReplayReport(
            task_description=self._task,
            domain=self._domain,
            cache_hit=True,
            status="refused",
            refusal_reasons=[reason],
            workflow_version=self._policy.workflow_version,
            route_pattern=self._route_pattern,
            scope_verified=True,
        )
        raise ReplayRefused(reason)

    def _fail(self, reason: str) -> None:
        """Record a replay that started acting but did not reach a verified end."""
        self.decision.status = "failed"
        self.decision.reasons = [reason]
        self.report = ReplayReport(
            task_description=self._task,
            domain=self._domain,
            cache_hit=True,
            status="failed",
            refusal_reasons=[reason],
            workflow_version=self._policy.workflow_version,
            route_pattern=self._route_pattern,
            scope_verified=True,
            preconditions_verified=self.decision.preconditions_verified,
        )

    async def _verify_destructive_approval(self, approval_token: str | None) -> None:
        """Require a host to validate and consume a destructive replay approval."""
        if not isinstance(approval_token, str) or not approval_token.strip():
            self._refuse("destructive replay requires approval_token")
        verifier = self._approval_verifier
        if verifier is None:
            self._refuse("destructive replay requires an approval_verifier")
        cached = self._cached_seq
        if cached is None:
            self._refuse("no cached sequence to approve")

        request = ReplayApprovalRequest(
            token=approval_token,
            task_description=self._task,
            scope_hash=self._policy.scope_hash(),
            workflow_version=self._policy.workflow_version,
            policy_fingerprint=self._policy.fingerprint(),
            origin=self._origin,
            route_pattern=self._route_pattern,
            structural_hash=cached.structural_hash,
            command_digest=_replay_command_digest(cached.commands),
            requested_at=time.time(),
        )
        try:
            decision = verifier(request)
            if inspect.isawaitable(decision):
                decision = await decision
        except Exception as exc:
            self._refuse(f"approval verification failed: {exc}")

        if not isinstance(decision, ApprovalDecision):
            self._refuse("approval_verifier must return ApprovalDecision")
        if not decision.approved:
            self._refuse(decision.reason or "destructive replay approval was rejected")
        if not decision.consumed:
            self._refuse(decision.reason or "destructive replay approval was not consumed")

    def record_command(self, cmd: CDPCommand) -> None:
        """Record a pre-built TERX semantic action for advanced integrations."""
        if cmd.method not in REPLAY_ACTIONS:
            self._unsafe_reasons.append(f"unsupported manual action: {cmd.method}")
            return
        try:
            validate_action(cmd.method, cmd.params)
        except ValueError as exc:
            self._unsafe_reasons.append(f"invalid manual action: {exc}")
            return
        self._recorded_commands.append(cmd)

    def mark_unreplayable(self, reason: str) -> None:
        """Record that this cold run did something TERX cannot replay."""
        self._unsafe_reasons.append(reason)

    @property
    def is_recording(self) -> bool:
        """True while this context is observing a cold run."""
        return not self.hit

    def observe_snapshot(self, snapshot: DOMSnapshot) -> None:
        """Give the CDP recorder the page an agent is about to act on.

        The recorder maps raw ``backendNodeId`` values to labelled targets
        using a snapshot. Without a fresh one, every action after the first
        navigation would be unidentifiable and make the run non-cacheable.
        The starting snapshot, which identifies the cached page, is unchanged.
        """
        self._target_snapshot = snapshot

    @property
    def variables(self) -> dict[str, Any]:
        """The normalized variables of this run (values are never persisted)."""
        return dict(self._variables)

    def _auto_record(
        self, method: str, params: dict[str, Any], result: dict[str, Any], latency: float
    ) -> None:
        """Translate known CDP gestures into safe semantic replay actions."""
        if method == "Page.navigate":
            url = params.get("url")
            if isinstance(url, str) and _is_replayable_url(url):
                template, problems = _templatize_url(url, self._variables)
                if problems:
                    self._unsafe_reasons.extend(problems)
                    return
                self._recorded_commands.append(
                    CDPCommand("TERX.navigate", {"url": template}, {}, latency)
                )
            else:
                self._unsafe_reasons.append("navigation URL is not replayable")
            return

        if method in {"DOM.focus", "DOM.resolveNode"}:
            target = _target_for_backend_node(
                params.get("backendNodeId"), self._target_snapshot or self._snapshot
            )
            if target is None:
                self._unsafe_reasons.append(f"{method} target is not uniquely identifiable")
                return
            if method == "DOM.focus":
                self._active_target = target
            else:
                object_id = result.get("object", {}).get("objectId")
                if isinstance(object_id, str):
                    self._object_targets[object_id] = target
            return

        if method == "Input.insertText":
            if self._active_target is None:
                self._unsafe_reasons.append("text input has no preceding labelled focus")
                return
            recorded_params, metadata = _prepare_recorded_params(
                method=method,
                params={"text": params.get("text")},
                active_node_info=self._active_target,
                variables=self._variables,
                redact_secrets=self._redact_secrets,
            )
            text = recorded_params.get("text")
            if not isinstance(text, str):
                self._unsafe_reasons.append("text input is not a string")
                return
            if (
                PLACEHOLDER_RE.fullmatch(text) is None
                or metadata.get("placeholder_source") != "variable"
            ):
                self._unsafe_reasons.append(
                    "typed text must be supplied as a named variable before it can be replayed"
                )
                return
            self._recorded_commands.append(
                CDPCommand(
                    "TERX.type",
                    {"target": self._active_target, "text": text},
                    {},
                    latency,
                    metadata,
                )
            )
            return

        if method == "Runtime.callFunctionOn":
            target = self._object_targets.get(str(params.get("objectId", "")))
            function = str(params.get("functionDeclaration", "")).strip()
            if target is not None and function in SAFE_CLICK_FUNCTIONS:
                self._recorded_commands.append(
                    CDPCommand("TERX.click", {"target": target}, {}, latency)
                )
            else:
                self._unsafe_reasons.append("arbitrary JavaScript cannot be replayed")
            return

        if method in MUTATING_CDP_METHODS:
            self._unsafe_reasons.append(f"unsupported mutating CDP action: {method}")

    async def __aenter__(self) -> "RecordingContext":
        # Capture DOM snapshot asynchronously on enter
        extractor = DOMExtractor()
        self._snapshot = await extractor.snapshot(self._bridge)
        self._domain = urlparse(self._snapshot.url).netloc or "unknown"
        parsed = urlparse(self._snapshot.url)
        self._origin = (
            f"{parsed.scheme}://{parsed.netloc}" if parsed.scheme and parsed.netloc else ""
        )
        self._route = parsed.path or "/"
        self._route_pattern = self._policy.route_pattern or self._route

        # Update session_id with domain if using default
        if self._session_id.startswith("browser_session_"):
            self._session_id = f"browser_{self._domain}_{self._session_id.split('_')[-1]}"

        # A policy is mandatory before a sequence can enter, or be read from,
        # the replay cache. Cold-path browser work remains available without it.
        cacheability_reasons = self._policy.cacheability_reasons()
        if self._policy.scope_id:
            self._cache.adopt_legacy_scope(self._policy.scope_id)
        self._scope_digest = self._cache.scope_digest(self._policy.scope_id)
        if cacheability_reasons:
            self.decision.reasons = cacheability_reasons
        else:
            try:
                await _assert_postcondition(self._bridge, self._policy.precondition)
                self._recording_precondition_verified = True
            except PostconditionFailed as exc:
                # Cold-path work can continue, but its outcome must not become
                # a replayable workflow from an unexpected starting state.
                self._unsafe_reasons.append(f"precondition failed at recording: {exc}")
            self._cached_seq = self._cache.lookup(
                self._domain,
                self._snapshot.role_sequence,
                self._task,
                origin=self._origin,
                route=self._route,
                scope_hash=self._scope_digest,
                workflow_version=self._policy.workflow_version,
                policy_fingerprint=self._policy.fingerprint(),
            )
            self.decision.scope_verified = self._cached_seq is not None

        # Cold miss is run #1. Existing sequence + previous hits gives repeat count.
        with self._cache._db_lock:
            db = self._cache._ensure_db()
            task_key = _task_key(self._task)
            self._run_number = db.execute(
                "SELECT COALESCE(SUM(hit_count), 0) + COUNT(*) + 1 "
                "FROM sequences WHERE domain = ? AND task_key = ? AND origin = ? AND scope_hash = ?",
                (self._domain, task_key, self._origin, self._scope_digest),
            ).fetchone()[0]

        if not self.hit:
            self._bridge.add_recorder(self._auto_record)
        return self

    async def __aexit__(self, exc_type: Any, *_: Any) -> None:
        if not self.hit:
            self._bridge.remove_recorder(self._auto_record)

        if exc_type is not None:
            return  # Don't cache failed runs

        if not self.hit and not self._recorded_commands:
            await _assert_postcondition(self._bridge, self._postcondition)
            self.decision.postconditions_verified = True
            reasons = [
                *self._policy.cacheability_reasons(),
                *self._unsafe_reasons,
                "workflow contains no replayable TERX actions",
            ]
            self.decision.status = "refused"
            self.decision.reasons = reasons
            self.ledger = ReplayCostLedger(
                task_description=self._task,
                hit=False,
                commands_replayed=0,
                latency_ms=0,
                run_number=self._run_number,
            )
            self.report = ReplayReport(
                task_description=self._task,
                domain=self._domain,
                cache_hit=False,
                status="refused",
                refusal_reasons=reasons,
                workflow_version=self._policy.workflow_version,
                route_pattern=self._route_pattern,
                postcondition=_postcondition_summary(self._postcondition),
                preconditions_verified=self._recording_precondition_verified,
                postconditions_verified=True,
            )

        if not self.hit and self._recorded_commands:
            await _assert_postcondition(self._bridge, self._postcondition)
            self.decision.postconditions_verified = True
            reasons = [*self._policy.cacheability_reasons(), *self._unsafe_reasons]
            if reasons:
                self.decision.status = "refused"
                self.decision.reasons = reasons
            else:
                if self._snapshot is None:
                    raise RuntimeError("RecordingContext was not entered")
                self._stored = self._cache.store(
                    domain=self._domain,
                    structural_hash=self._snapshot.structural_hash,
                    role_sequence=self._snapshot.role_sequence,
                    task_description=self._task,
                    commands=self._recorded_commands,
                    origin=self._origin,
                    route_pattern=self._route_pattern,
                    scope_hash=self._scope_digest,
                    workflow_version=self._policy.workflow_version,
                    policy_json=json.dumps(self._policy.persisted_summary(), sort_keys=True),
                    ttl_seconds=self._policy.ttl_seconds,
                )

            # Write audit file.
            self._cache.write_audit(
                session_id=self._session_id,
                task_description=self._task,
                commands=self._recorded_commands,
                domain=self._domain,
                was_cache_hit=False,
            )
            self.ledger = ReplayCostLedger(
                task_description=self._task,
                hit=False,
                commands_replayed=0,
                latency_ms=0,
                run_number=self._run_number,
            )
            self.report = ReplayReport(
                task_description=self._task,
                domain=self._domain,
                cache_hit=False,
                commands_recorded=len(self._recorded_commands),
                variables_used=_placeholders_in_commands(self._recorded_commands),
                redacted_fields=_redacted_fields_in_commands(self._recorded_commands),
                postcondition=_postcondition_summary(self._postcondition),
                run_number=self._run_number,
                status=self.decision.status,
                refusal_reasons=list(self.decision.reasons),
                workflow_version=self._policy.workflow_version,
                route_pattern=self._route_pattern,
                scope_verified=not bool(self._policy.cacheability_reasons()),
                preconditions_verified=self._recording_precondition_verified,
                postconditions_verified=self.decision.postconditions_verified,
            )


def session_for(
    cache: MemoryCache,
    bridge: CDPBridge,
    task: str,
    session_id: str | None = None,
    variables: dict[str, Any] | None = None,
    scope_id: str | None = None,
    route_pattern: str | None = None,
    workflow_version: int = 1,
    side_effect: SideEffect = "mutating",
    precondition: dict[str, Any] | None = None,
    postcondition: dict[str, Any] | Any | None = None,
    ttl_seconds: int | None = 86_400,
    redact_secrets: bool = True,
    mutation_guard: bool = True,
    mutation_threshold: int = 20,
    approval_verifier: ApprovalVerifier | None = None,
    replay_timeout: float = 10.0,
) -> RecordingContext:
    """
    Factory: create a RecordingContext for a task on the current page.

    A scope ID and non-empty pre/postcondition dictionaries are required for a
    workflow to be cacheable. Without them, the context remains cold-path only.

    Example:
        async with session_for(
            cache, bridge, "login to salesforce", scope_id="tenant-a",
            precondition={"url_contains": "/login"},
            postcondition={"text_contains": "Welcome"},
        ) as ctx:
            if ctx.hit:
                await ctx.replay()
            else:
                await bridge.send("Page.navigate", {"url": "..."})
        print(ctx.ledger)
    """
    return RecordingContext(
        cache=cache,
        bridge=bridge,
        task=task,
        session_id=session_id,
        variables=variables,
        scope_id=scope_id,
        route_pattern=route_pattern,
        workflow_version=workflow_version,
        side_effect=side_effect,
        precondition=precondition,
        postcondition=postcondition,
        ttl_seconds=ttl_seconds,
        redact_secrets=redact_secrets,
        mutation_guard=mutation_guard,
        mutation_threshold=mutation_threshold,
        approval_verifier=approval_verifier,
        replay_timeout=replay_timeout,
    )
