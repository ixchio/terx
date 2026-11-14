"""Persisted actions, cached sequences, and replay reports."""

from __future__ import annotations

import logging
from dataclasses import asdict, dataclass, field
from typing import Any


logger = logging.getLogger(__name__)


@dataclass
class CDPCommand:
    """A persisted replay action.

    v0.4 stores only TERX's small semantic action set. ``method`` remains for
    backwards-compatible JSON decoding, but persisted values are ``TERX.*``
    actions rather than arbitrary CDP calls.
    """

    method: str
    params: dict[str, Any]
    result: dict[str, Any]
    latency_ms: float
    metadata: dict[str, Any] = field(default_factory=dict[str, Any])


@dataclass
class CachedSequence:
    """A cached action sequence for one successful task."""

    domain: str
    structural_hash: str
    task_key: str
    task_description: str
    commands: list[CDPCommand]
    hit_count: int
    created_at: str
    last_used: str
    origin: str = ""
    route_pattern: str = "*"
    scope_hash: str = ""
    workflow_version: int = 1
    policy_json: str = "{}"
    expires_at: float | None = None


@dataclass
class ReplayCostLedger:
    """Records replay activity without guessing provider-side model savings."""

    task_description: str
    hit: bool
    commands_replayed: int
    latency_ms: float
    run_number: int
    # Kept as a nullable compatibility field. TERX cannot infer calls saved
    # from browser actions; hosts must attach provider usage records instead.
    estimated_llm_calls_saved: int | None = None
    model_calls_during_replay: int = 0

    def __str__(self) -> str:
        if self.hit:
            return (
                f"💾 Cache HIT · {self.commands_replayed} commands · "
                f"{self.latency_ms:.0f}ms · "
                f"TERX model calls: {self.model_calls_during_replay} · "
                f"run #{self.run_number}"
            )
        return f"🔍 Cache MISS · run #{self.run_number} (learning...)"


@dataclass
class ReplayReport:
    """Structured replay/recording report for CLI, MCP, and integrations."""

    task_description: str
    domain: str
    cache_hit: bool
    commands_recorded: int = 0
    commands_replayed: int = 0
    variables_used: list[str] = field(default_factory=list)
    redacted_fields: list[str] = field(default_factory=list)
    postcondition: Any = None
    latency_ms: float = 0.0
    run_number: int = 1
    mutation_count: int | None = None
    mutation_threshold: int | None = None
    status: str = "miss"
    refusal_reasons: list[str] = field(default_factory=list)
    workflow_version: int = 1
    route_pattern: str | None = None
    scope_verified: bool = False
    preconditions_verified: bool = False
    postconditions_verified: bool = False
    # Targets found by a fallback signal (for example a test id after a label
    # change). Operators should review these: the page has drifted.
    locator_notes: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)
