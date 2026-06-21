# Launch Posts

## Hacker News — "Show HN"

**Title:**
> Show HN: TERX – browser agent memory that makes runs 2-50 cost zero tokens

**Body:**
> I built TERX because I was paying $0.80/day for an agent that logs into the same dashboard 100 times a day. It rediscovered the login button every single time.
>
> TERX sits below your agent framework (browser-use, LangChain, raw Claude, anything) and intercepts the raw Chrome DevTools Protocol. When your agent successfully completes a task, TERX records the exact CDP command sequence. Next run, it replays it deterministically — 40ms, zero tokens.
>
> Not a Playwright wrapper. Not screenshot-based. Raw CDP WebSocket to Chrome. ~2MB RAM vs Playwright's ~120MB.
>
> Benchmark: 10-task suite against GPT-4o baseline:
> - Average: 28.8x faster on warm runs
> - Token savings: 100% (the cached replay sends zero LLM requests)
> - $0.408 → $0.000 per run after first
>
> If the DOM changes (site redesign), it detects the structural hash mismatch and falls back to agent mode for one run to re-discover the path, then caches that too. SSIM visual auditing catches silent UI drift.
>
> Three lines to add it to any existing agent:
> ```python
> async with session_for(cache, bridge, "login to salesforce") as ctx:
>     if ctx.hit: await ctx.replay()
>     else: await your_agent.run(...)
> ```
>
> Or run `terx-server` and add it to your Claude Desktop / Cursor MCP config — no code changes needed.
>
> GitHub: https://github.com/ixchio/terx
> pip install terx

---

## Twitter / X

**Thread (post as thread):**

Tweet 1:
> Your browser agent rediscovers the login button every single run.
>
> I built TERX to fix that.
>
> Run 1: agent runs normally, TERX silently records
> Run 2-50: TERX replays. 40ms. $0.000.
>
> No Playwright. Raw CDP. Works with any agent.
>
> pip install terx

Tweet 2:
> Benchmark across 10 real browser tasks (Claude Desktop / browser-use equivalent):
>
> Average: 28.8x faster
> Token savings: 100%
> Cost: $0.408 → $0.000 per run
>
> Numbers are reproducible. Run terx-bench yourself.

Tweet 3:
> How it works:
>
> 1. CDP Bridge — raw WebSocket to Chrome. <50ms startup, 2MB RAM
> 2. DOM Extractor — reads accessibility tree, assigns stable IDs
> 3. Muscle Memory Cache — SQLite. Records CDP commands, replays them
>
> If DOM changes → self-heals via LLM → re-caches
> SSIM visual auditing catches silent UI drift

Tweet 4:
> 3 lines to add to any existing agent:
>
> async with session_for(cache, bridge, "task name") as ctx:
>     if ctx.hit: await ctx.replay()
>     else: await your_agent.run(...)
>
> Or run terx-server and add to Claude Desktop MCP config.
>
> github.com/ixchio/terx
> MIT. 33 tests green.

---

## Reddit — r/LocalLLaMA / r/MachineLearning

**Title:**
> I built a memory layer for browser agents — replays repeated tasks in 40ms with zero LLM tokens

**Body:**
> If you run browser agents (browser-use, computer-use, anything that drives Chrome), you've probably noticed they're expensive for repetitive work.
>
> TERX is a ~2MB Python library that sits below your agent and caches the exact Chrome DevTools Protocol command sequence for completed tasks. On future identical runs, it replays directly — no LLM, no screenshots, no rediscovery. 40ms. $0.000.
>
> Benchmarks (10-task suite, GPT-4o pricing):
> - 28.8x average speedup on cached runs
> - 100% token savings
> - $0.408 → $0.000 per run after first
>
> It uses raw CDP (not Playwright), reads the accessibility tree for stable element matching, and uses a fuzzy structural hash so the cache survives minor CSS changes. If the site redesigns, it self-heals via LLM for one run, then re-caches.
>
> Works with browser-use, LangChain agents, raw Claude/GPT loops, or as an MCP server for Claude Desktop.
>
> https://github.com/ixchio/terx
> MIT license. pip install terx.

---

## Key talking points for any post

1. **The number that lands:** "28.8x faster, 100% token savings, reproducible with `terx-bench`"
2. **The differentiator:** "Raw CDP — not Playwright, not screenshots. Below your agent framework."
3. **The hook:** "Your agent rediscovers the login button every run. TERX fixes that."
4. **The credibility:** "33 tests, CI green, MIT, pip installable today"
