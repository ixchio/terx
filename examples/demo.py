"""
Demo: TERX muscle memory cache in action.

Run 1: agent navigates and logs in — TERX records the CDP commands.
Run 2: same task — TERX replays them directly, zero LLM calls.

Writes a .vcr file readable by Agent VCR's VCRPlayer.
"""

import asyncio
import time

from terx.cdp.session import BrowserSession
from terx.dom.extractor import DOMExtractor
from terx.cache.cache import CDPCommand, MuscleMemorycache, session_for


async def simulate_login(session: BrowserSession, ctx) -> list[CDPCommand]:
    """Simulate a login flow — in real usage this is your agent."""
    bridge = session.bridge()
    recorded = []

    async def tracked_send(method, params=None):
        t0 = time.perf_counter()
        result = await bridge.send(method, params or {})
        latency = (time.perf_counter() - t0) * 1000
        cmd = CDPCommand(method=method, params=params or {}, result=result, latency_ms=latency)
        ctx.record_command(cmd)
        return result

    await tracked_send("Page.navigate", {"url": "https://example.com/login"})
    await asyncio.sleep(0.5)
    await tracked_send("Runtime.evaluate", {
        "expression": "document.querySelector('#email').value = 'user@example.com'"
    })
    await tracked_send("Runtime.evaluate", {
        "expression": "document.querySelector('#password').value = 'secret'"
    })
    await tracked_send("Runtime.evaluate", {
        "expression": "document.querySelector('#login-btn').click()"
    })


async def main():
    cache = MuscleMemorycache()
    extractor = DOMExtractor()

    print("🌐 Starting Chrome session...")
    async with BrowserSession() as session:
        bridge = session.bridge()

        for run in range(1, 4):
            print(f"\n{'─' * 50}")
            print(f"RUN {run}: Login to example.com")

            # Snapshot current DOM
            snapshot = await extractor.snapshot(bridge)

            async with session_for(cache, bridge, snapshot, "login to example.com") as ctx:
                if ctx.hit:
                    print("💾 CACHE HIT — replaying, zero LLM calls")
                    await ctx.replay()
                else:
                    print("🔍 CACHE MISS — running agent, recording commands")
                    await simulate_login(session, ctx)

            if ctx.ledger:
                print(ctx.ledger)

            if ctx.hit:
                vcr_files = list(cache.vcr_dir.glob("*.vcr"))
                if vcr_files:
                    print(f"📼 .vcr written → {vcr_files[-1]}")
                    print("   Load in Agent VCR: VCRPlayer.load(path)")

    stats = cache.stats()
    print(f"\n📊 Cache stats: {stats}")


if __name__ == "__main__":
    asyncio.run(main())
