# TERX + browser-use Integration

[browser-use](https://github.com/browser-use/browser-use) is the most popular browser agent framework. TERX makes it free to run on repeated tasks.

## How it works

browser-use discovers how to do a task using an LLM. TERX records that discovery and replays it for free next time. They're not competing — TERX sits *below* browser-use.

```
Run 1:  browser-use + GPT-4o → discovers path → [$0.008 · 3.2s]
         └── TERX silently records the CDP commands
Run 2:  TERX replays the recording → [$0.000 · 41ms]
Run 3+: Same
```

## Install

```bash
pip install terx browser-use
```

## Drop-in wrapper

```python
import asyncio
from browser_use import Agent
from langchain_openai import ChatOpenAI

from terx.cdp.session import BrowserSession
from terx.cache.cache import MuscleMemorycache, session_for

cache = MuscleMemorycache()

async def run_with_memory(task: str):
    async with BrowserSession() as session:
        bridge = session.bridge()

        async with session_for(cache, bridge, task) as ctx:
            if ctx.hit:
                # Cached — zero tokens, ~40ms
                await ctx.replay()
            else:
                # First run — browser-use discovers the path, TERX records it
                agent = Agent(
                    task=task,
                    llm=ChatOpenAI(model="gpt-4o"),
                )
                await agent.run()

        print(ctx.ledger)
        # ⚡ Cache HIT · 12 commands · 41ms · ~12 LLM calls saved

asyncio.run(run_with_memory("login to salesforce"))
```

## Real savings at scale

If you run `login to salesforce` 100 times a day:

| | Without TERX | With TERX |
|---|---|---|
| LLM calls | 100 × ~12 = 1,200/day | 12 (first run only) |
| Token cost | ~$0.80/day | ~$0.008/day |
| Execution time | ~320s total | ~3.2s + 40ms × 99 |
| Monthly cost | ~$24 | ~$0.24 |

## What gets cached

Everything TERX records is a CDP command — the exact sequence of:
- `Page.navigate` calls
- `Input.dispatchMouseEvent` clicks
- `Input.insertText` / `Input.dispatchKeyEvent` for typing
- Any other CDP method your agent triggers

On replay, TERX translates backend node IDs from the recording to current DOM IDs — so the replay works even if Chrome restarted.

## Cache invalidation

When a site redesigns its UI, the structural hash changes and TERX automatically falls back to browser-use for one run to discover the new path — then caches that too.

```python
# Or manually clear:
from terx.cache.cache import MuscleMemorycache
cache = MuscleMemorycache()
cache.invalidate("salesforce.com")  # clears all cached sequences for this domain
```

## MCP mode (no code changes)

```bash
terx-server
```

Add to `mcp.json` — Claude Desktop / Cursor / Windsurf will automatically use TERX for all browser tasks:

```json
{
  "mcpServers": {
    "terx": { "command": "terx-server" }
  }
}
```

Every browser action is cached. First run = normal. Every run after = free.
