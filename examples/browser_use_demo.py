"""Browser Use-style TERX wrapper example.

This example is intentionally version-tolerant: TERX wraps any object with a
run() method. For real Browser Use projects, construct your Agent exactly as
your installed browser-use version documents, then pass it into wrap_browser_use.
"""

from __future__ import annotations

import asyncio

from terx.cache.cache import MemoryCache
from terx.cdp.session import BrowserSession
from terx.integrations.browser_use import wrap_browser_use


class BrowserUseLikeAgent:
    """Tiny stand-in showing the contract TERX expects from browser-use.Agent."""

    task = "login to demo dashboard"

    def __init__(self, bridge):
        self.bridge = bridge

    async def run(self) -> str:
        # In a real browser-use.Agent, the agent's own controller would send
        # browser actions. TERX records commands when they go through this bridge.
        await self.bridge.send("Page.navigate", {"url": "https://example.com"})
        await self.bridge.wait_for_load()
        return "done"


async def main() -> None:
    cache = MemoryCache()
    async with BrowserSession(port=9222) as session:
        bridge = session.bridge()
        agent = BrowserUseLikeAgent(bridge)
        terx_agent = wrap_browser_use(
            agent,
            cache=cache,
            bridge=bridge,
            variables={},
            postcondition={"url_contains": "example.com"},
        )
        result = await terx_agent.run()
        print(result)


if __name__ == "__main__":
    asyncio.run(main())
