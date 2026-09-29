"""
TERX — Memory layer for browser agents.
"""

from terx.cdp.bridge import CDPBridge
from terx.cdp.session import BrowserSession
from terx.cache.cache import (
    ApprovalDecision,
    ApprovalVerifier,
    MemoryCache,
    ReplayApprovalRequest,
    ReplayDecision,
    ReplayPolicy,
    ReplayReport,
    session_for,
)

# Backwards compatibility alias
MuscleMemorycache = MemoryCache

__version__ = "0.4.0"
__all__ = [
    "CDPBridge",
    "BrowserSession",
    "ApprovalDecision",
    "ApprovalVerifier",
    "MemoryCache",
    "MuscleMemorycache",
    "ReplayDecision",
    "ReplayApprovalRequest",
    "ReplayPolicy",
    "ReplayReport",
    "session_for",
]
