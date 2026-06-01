"""
TERX — Memory layer for browser agents.
"""

from terx.cdp.bridge import CDPBridge
from terx.cdp.session import BrowserSession
from terx.cache.cache import MuscleMemorycache

__version__ = "0.1.0"
__all__ = ["CDPBridge", "BrowserSession", "MuscleMemorycache"]
