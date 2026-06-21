# Quick Start

Get TERX running in 5 minutes.

---

## 1. Install

```bash
pip install terx
```

For all optional features:

```bash
pip install "terx[embeddings,vision,healing]"
```

---

## 2. Start Chrome

TERX connects to Chrome via its DevTools Protocol. Chrome must expose the debugging port.

**Close all existing Chrome windows first**, then:

```bash
# Standard (with UI)
google-chrome --remote-debugging-port=9222 --no-first-run

# Headless (CI, servers)
google-chrome --remote-debugging-port=9222 --headless=new --no-first-run
```

Verify it's running: `curl http://localhost:9222/json/version`

---

## 3. Use as MCP server (Claude Desktop / Cursor / Windsurf)

```bash
terx-server
```

Add to `~/.config/claude/claude_desktop_config.json` (Claude Desktop):

```json
{
  "mcpServers": {
    "terx": {
      "command": "terx-server"
    }
  }
}
```

That's it. Every browser task Claude runs is now automatically cached. First run = normal. Every repeat = free.

---

## 4. Use as a Python library

```python
import asyncio
from terx.cdp.session import BrowserSession
from terx.cache.cache import MuscleMemorycache, session_for

cache = MuscleMemorycache()

async def run_task():
    async with BrowserSession() as session:
        bridge = session.bridge()

        async with session_for(cache, bridge, "login to salesforce") as ctx:
            if ctx.hit:
                await ctx.replay()          # Warm: 0 tokens, ~80ms
            else:
                await your_agent.run(...)   # Cold: agent runs, TERX records

        print(ctx.ledger)
        # ⚡ Cache HIT · 12 commands · 78ms · 0 tokens

asyncio.run(run_task())
```

---

## 5. Available MCP tools

| Tool | What it does |
|:---|:---|
| `browser_get_state` | AX tree snapshot with stable element IDs |
| `browser_navigate` | Navigate to URL |
| `browser_click` | Click element by stable ID |
| `browser_type` | Type into input (React/Vue/Svelte safe) |
| `browser_screenshot` | Returns hash ref (no context poisoning) |
| `browser_scroll` | Scroll up/down |
| `browser_new_tab` | Open new tab |
| `cache_stats` | Hit rate, savings, unique domains |
| `cache_invalidate` | Clear cache for a domain |

---

## 6. Run the real benchmark

See how TERX performs against an actual LLM agent:

```bash
# Get a free Groq API key at console.groq.com
cp .env.example .env
# edit .env → set GROQ_API_KEY=gsk_...

export $(grep -v '^#' .env | xargs)
python -m terx.benchmarks.real_agent
```

Results: [10/10 cache hits · 182.7x speedup · 100% token savings →](benchmarks.md)

---

## 7. Environment variables

Copy `.env.example` to `.env` and configure:

| Variable | Default | Description |
|:---|:---|:---|
| `GROQ_API_KEY` | — | For `terx-bench-real` only |
| `OPENAI_API_KEY` | — | Alternative LLM provider |
| `TERX_CDP_URL` | `ws://localhost:9222` | Chrome DevTools endpoint |
| `TERX_CACHE_PATH` | `~/.terx/cache.db` | SQLite cache location |
| `TERX_SIMILARITY_THRESHOLD` | `0.85` | DOM hash fuzzy match threshold |
| `TERX_DEBUG` | `0` | Set to `1` for verbose CDP logging |

> **Never commit `.env`** — it's in `.gitignore`. Only commit `.env.example`.
