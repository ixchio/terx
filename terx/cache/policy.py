"""The replay contract: what makes a workflow eligible for replay, and approvals."""

from __future__ import annotations

import hashlib
import json
import re
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any, Literal
from urllib.parse import urlparse


logger = logging.getLogger(__name__)


SideEffect = Literal["read_only", "mutating", "destructive"]
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
    "TERX.check",
    "TERX.select",
    "TERX.press",
}

# Keys TERX.press may send. Each has one effect on a focused control; any
# other key (and any modifier combination) is not replayable.
PRESS_KEYS = {"Enter", "Tab", "Escape"}

PLACEHOLDER_RE = re.compile(r"\{\{([a-zA-Z_][a-zA-Z0-9_]*)\}\}")


def validate_action(method: str, params: dict[str, Any]) -> None:
    """Raise ``ValueError`` unless ``params`` is a well-formed TERX action.

    This is the single definition of the persisted action format; storage
    and replay preflight both call it.
    """
    if method not in REPLAY_ACTIONS:
        raise ValueError(f"unsupported action: {method}")
    if method == "TERX.navigate":
        url = params.get("url")
        if not isinstance(url, str) or not _is_replayable_url(url):
            raise ValueError("TERX.navigate requires an http or https URL")
        return
    target = params.get("target")
    if not isinstance(target, dict) or not all(
        isinstance(target.get(key), str) and target[key] for key in ("role", "label")
    ):
        raise ValueError(f"{method} requires a labelled semantic target")
    if "frame" in target and not isinstance(target["frame"], str):
        raise ValueError("target frame must be a string")
    if "test_id" in target and not (isinstance(target["test_id"], str) and target["test_id"]):
        raise ValueError("target test_id must be a non-empty string")
    if "ordinal" in target and not (
        isinstance(target["ordinal"], int)
        and not isinstance(target["ordinal"], bool)
        and target["ordinal"] >= 0
    ):
        raise ValueError("target ordinal must be a non-negative integer")
    if method == "TERX.type":
        text = params.get("text")
        if not isinstance(text, str) or PLACEHOLDER_RE.fullmatch(text) is None:
            raise ValueError("TERX.type requires a named variable placeholder")
    elif method == "TERX.check":
        if not isinstance(params.get("checked"), bool):
            raise ValueError("TERX.check requires a boolean checked state")
    elif method == "TERX.select":
        option = params.get("option")
        if not isinstance(option, str) or not option:
            raise ValueError("TERX.select requires an option label or placeholder")
    elif method == "TERX.press":
        if params.get("key") not in PRESS_KEYS:
            raise ValueError(f"TERX.press key must be one of {', '.join(sorted(PRESS_KEYS))}")


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
    side_effect: SideEffect = "mutating"
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
    """Allow only credential- and query-free web navigation in replay data.

    A URL template may hold ``{{variable}}`` placeholders in its *path* only.
    A placeholder in the scheme or host could send a replay to any site, so it
    is never replayable.
    """
    parsed = urlparse(url)
    return (
        parsed.scheme.lower() in {"http", "https"}
        and bool(parsed.netloc)
        and "{" not in parsed.netloc
        and "}" not in parsed.netloc
        and parsed.username is None
        and parsed.password is None
        and not parsed.query
        and not parsed.fragment
    )
