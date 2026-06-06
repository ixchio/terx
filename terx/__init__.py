"""
TERX — Memory layer for browser agents.
"""

from terx.cdp.bridge import CDPBridge
from terx.cdp.session import BrowserSession
from terx.cache.cache import MemoryCache

# Backwards compatibility alias
MuscleMemorycache = MemoryCache

__version__ = "0.2.0"
__all__ = ["CDPBridge", "BrowserSession", "MemoryCache", "MuscleMemorycache"]
