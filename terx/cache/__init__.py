"""TERX replay cache: scoped storage, policy, recording, and replay."""

from terx.cache.errors import (
    CacheReplayError,
    MissingReplayVariable,
    MutationDriftError,
    PostconditionFailed,
    ReplayRefused,
)
from terx.cache.models import CachedSequence, CDPCommand, ReplayCostLedger, ReplayReport
from terx.cache.policy import (
    ApprovalDecision,
    ApprovalVerifier,
    ReplayApprovalRequest,
    ReplayDecision,
    ReplayPolicy,
)
from terx.cache.recording import RecordingContext, session_for
from terx.cache.storage import MemoryCache

__all__ = [
    "ApprovalDecision",
    "ApprovalVerifier",
    "CDPCommand",
    "CacheReplayError",
    "CachedSequence",
    "MemoryCache",
    "MissingReplayVariable",
    "MutationDriftError",
    "PostconditionFailed",
    "RecordingContext",
    "ReplayApprovalRequest",
    "ReplayCostLedger",
    "ReplayDecision",
    "ReplayPolicy",
    "ReplayRefused",
    "ReplayReport",
    "session_for",
]
