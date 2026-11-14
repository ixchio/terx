"""
TERX — Memory layer for browser agents.
"""

from terx.cdp.bridge import CDPBridge
from terx.cdp.session import BrowserSession
from terx.cache import (
    ApprovalDecision,
    ApprovalVerifier,
    MemoryCache,
    ReplayApprovalRequest,
    ReplayDecision,
    ReplayPolicy,
    ReplayReport,
    session_for,
)
from terx.tools import SavedTool, ToolManifestError, ToolResultError


__version__ = "1.0.0"
__all__ = [
    "CDPBridge",
    "BrowserSession",
    "ApprovalDecision",
    "ApprovalVerifier",
    "MemoryCache",
    "ReplayDecision",
    "ReplayApprovalRequest",
    "ReplayPolicy",
    "ReplayReport",
    "SavedTool",
    "ToolManifestError",
    "ToolResultError",
    "session_for",
]
