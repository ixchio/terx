"""The replay contract: what makes a workflow eligible for replay, and approvals."""

from __future__ import annotations

import hashlib
import json
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any, Literal
from urllib.parse import urlparse


logger = logging.getLogger(__name__)


SIDE_EFFECTS = {"read_only", "mutating", "destructive"}


REPLAY_CONDITION_KEYS = {
    "url_contains",
    "title_contains",
    "text_contains",
    "selector_exists",
}


REPLAY_ACTIONS = {
    "TERX.navigate",
    "TERX.click",
    "TERX.type",
}


@dataclass(frozen=True)
class ReplayPolicy:
    """Explicit contract that makes a workflow eligible for replay.

    ``scope_id`` is never persisted in plaintext. The cache stores a keyed
    HMAC digest (see :meth:`MemoryCache.scope_digest`) that binds a workflow to
    a caller-controlled account, tenant, environment, or fixture. A scope is a
    namespace the caller asserts, not an authentication credential.

    :meth:`scope_hash` is the unkeyed SHA-256 sent to a host approval verifier,
    which already holds the raw scope; it is not what the cache persists.
    """

    scope_id: str | None = None
    route_pattern: str | None = None
    workflow_version: int = 1
    side_effect: Literal["read_only", "mutating", "destructive"] = "mutating"
    precondition: dict[str, Any] | None = None
    postcondition: dict[str, Any] | None = None
    ttl_seconds: int | None = 86_400

    def scope_hash(self) -> str:
        if not self.scope_id:
            return ""
        return hashlib.sha256(self.scope_id.encode()).hexdigest()

    def cacheability_reasons(self) -> list[str]:
        reasons: list[str] = []
        if self.side_effect not in SIDE_EFFECTS:
            reasons.append(f"invalid side_effect: {self.side_effect}")
        if self.workflow_version < 1:
            reasons.append("workflow_version must be at least 1")
        if not self.scope_id:
            reasons.append("scope_id is required for replayable workflows")
        if not isinstance(self.precondition, dict) or not self.precondition:
            reasons.append("a dict precondition is required for replayable workflows")
        if not isinstance(self.postcondition, dict) or not self.postcondition:
            reasons.append("a dict postcondition is required for replayable workflows")
        for label, condition in (
            ("precondition", self.precondition),
            ("postcondition", self.postcondition),
        ):
            if not isinstance(condition, dict):
                continue
            unknown = sorted(set(condition) - REPLAY_CONDITION_KEYS)
            if unknown:
                reasons.append(f"{label} contains unsupported checks: {', '.join(unknown)}")
            non_string_values = sorted(
                key
                for key, value in condition.items()
                if key in REPLAY_CONDITION_KEYS and not isinstance(value, str)
            )
            if non_string_values:
                reasons.append(f"{label} values must be strings: {', '.join(non_string_values)}")
            empty_values = sorted(
                key
                for key, value in condition.items()
                if key in REPLAY_CONDITION_KEYS and isinstance(value, str) and not value
            )
            if empty_values:
                reasons.append(
                    f"{label} values must be non-empty strings: {', '.join(empty_values)}"
                )
        if self.ttl_seconds is not None and self.ttl_seconds <= 0:
            reasons.append("ttl_seconds must be positive or None")
        return reasons

    def fingerprint(self) -> str:
        """Hash the full replay contract without persisting condition values."""
        contract = {
            "route_pattern": self.route_pattern,
            "workflow_version": self.workflow_version,
            "side_effect": self.side_effect,
            "precondition": self.precondition,
            "postcondition": self.postcondition,
            "ttl_seconds": self.ttl_seconds,
        }
        encoded = json.dumps(contract, sort_keys=True, separators=(",", ":"), default=str).encode()
        return hashlib.sha256(encoded).hexdigest()

    def persisted_summary(self) -> dict[str, Any]:
        return {
            "route_pattern": self.route_pattern,
            "workflow_version": self.workflow_version,
            "side_effect": self.side_effect,
            "precondition_checks": sorted((self.precondition or {}).keys()),
            "postcondition_checks": sorted((self.postcondition or {}).keys()),
            "ttl_seconds": self.ttl_seconds,
            "fingerprint": self.fingerprint(),
        }


@dataclass(frozen=True)
class ReplayApprovalRequest:
    """Opaque approval data a host must verify and atomically consume.

    TERX never persists the token. A verifier receives enough immutable replay
    identity to reject a token approved for another task, scope, policy, or
    semantic command sequence.
    """

    token: str
    task_description: str
    scope_hash: str
    workflow_version: int
    policy_fingerprint: str
    origin: str
    route_pattern: str
    structural_hash: str
    command_digest: str
    requested_at: float


@dataclass(frozen=True)
class ApprovalDecision:
    """The only successful result accepted from an approval verifier."""

    approved: bool
    consumed: bool
    reason: str | None = None


ApprovalVerifier = Callable[[ReplayApprovalRequest], ApprovalDecision | Awaitable[ApprovalDecision]]


@dataclass
class ReplayDecision:
    """Machine-readable replay outcome for agents and operators."""

    status: Literal["hit", "miss", "refused", "failed"]
    reasons: list[str] = field(default_factory=list)
    workflow_version: int = 1
    scope_verified: bool = False
    preconditions_verified: bool = False
    postconditions_verified: bool = False


def _is_replayable_url(url: str) -> bool:
    """Allow only credential- and query-free web navigation in replay data."""
    parsed = urlparse(url)
    return (
        parsed.scheme.lower() in {"http", "https"}
        and bool(parsed.netloc)
        and parsed.username is None
        and parsed.password is None
        and not parsed.query
        and not parsed.fragment
    )
