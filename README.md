<div align="center">

```
████████╗███████╗██████╗ ██╗  ██╗
╚══██╔══╝██╔════╝██╔══██╗╚██╗██╔╝
   ██║   █████╗  ██████╔╝ ╚███╔╝
   ██║   ██╔══╝  ██╔══██╗ ██╔██╗
   ██║   ███████╗██║  ██║██╔╝ ██╗
   ╚═╝   ╚══════╝╚═╝  ╚═╝╚═╝  ╚═╝
```

**browser agent memory. raw cdp. no playwright.**

[![CI](https://github.com/ixchio/terx/actions/workflows/ci.yml/badge.svg)](https://github.com/ixchio/terx/actions)
[![PyPI](https://img.shields.io/pypi/v/terx?color=3ddc84&label=PyPI)](https://pypi.org/project/terx/)
[![License: MIT](https://img.shields.io/badge/license-MIT-yellow)](LICENSE)
[![Python 3.11+](https://img.shields.io/badge/python-3.11+-blue)](https://python.org)

</div>

---

Your browser agent is goldfish-brained.

It logs into the same dashboard 50 times a day. It rediscovers the login button 50 times. Burns $0.008 worth of tokens 50 times. Every. Single. Run.

TERX remembers.

Run 1: your agent discovers the path. TERX watches and records the exact Chrome DevTools Protocol commands. Run 2 onwards: TERX replays them. No LLM. No screenshot. No re-discovery. ~100ms.

![TERX live demo](docs/assets/demo.gif)

```
Run 1:  agent runs normally              2.93s · 2,090 tokens · $0.0076
         TERX silently records CDP commands

Run 2:  TERX replays                     0.078s · 0 tokens · $0.0000
Run 50: TERX replays                     0.081s · 0 tokens · $0.0000
```

---

## Numbers

Real measurement. Real LLM (`openai/gpt-oss-120b` via Groq). Token counts from API response headers.

| task | agent | terx | speedup | tokens |
|:-----|------:|-----:|--------:|-------:|
| User Login | 2.93s · $0.0076 | **0.078s · $0** | 37.7x | 2,090 → 0 |
| Search + Filter | 3.99s · $0.0136 | **0.101s · $0** | 39.7x | 3,533 → 0 |
| Multi-step Signup | 1.54s · $0.0045 | **0.062s · $0** | 25x | 1,035 → 0 |
| Data Table (12 steps) | 90.84s · $0.0567 | **0.259s · $0** | 350x | 12,479 → 0 |
| **average** | **19.86s · $0.014** | **0.109s · $0** | **182.7x** | **32,993 → 0** |

Cache hit rate: **10/10**. Reproduce: `GROQ_API_KEY=... python -m terx.benchmarks.real_agent`

Full methodology → [docs/benchmarks.md](docs/benchmarks.md)

---

## Install

```bash
pip install terx
```

---

## Use it

**Option 1: MCP server** — drop into Claude Desktop, Cursor, Windsurf. Zero code changes.

```bash
google-chrome --remote-debugging-port=9222 --no-first-run
terx-server
```

`mcp.json`:
```json
{ "mcpServers": { "terx": { "command": "terx-server" } } }
```

Every browser task is now cached automatically. You don't write any code.

---

**Option 2: Python library** — wrap your existing agent.

```python
from terx.cdp.session import BrowserSession
from terx.cache.cache import MemoryCache, session_for

cache = MemoryCache()

async with BrowserSession() as session:
    bridge = session.bridge()
    async with session_for(cache, bridge, "login to salesforce") as ctx:
        if ctx.hit:
            await ctx.replay()        # 0 tokens, ~80ms
        else:
            await your_agent.run()    # first time: agent runs, TERX records
```

Works with browser-use, LangChain, raw Claude/GPT loops, anything.

---

## How it works

Three things, each doing one job:

**CDP Bridge** — raw asyncio WebSocket to Chrome. No Playwright subprocess. No Selenium. Direct wire protocol. `<50ms` startup, `~2MB` RAM.

**DOM Extractor** — reads Chrome's Accessibility Tree, not raw HTML. Assigns stable numeric IDs to interactive elements. Computes a fuzzy structural hash that survives CSS refactors and A/B tests without breaking cache hits.

**Muscle Memory Cache** — SQLite. On task success: stores the CDP command sequence keyed by `(domain, dom_hash, task)`. On future runs: replays directly. Uses `INSERT OR IGNORE` — first successful recording is canonical, never silently overwritten.

On replay, TERX re-snapshots the DOM and translates old `backendNodeId`s to current equivalents by matching `role + label` — so replays work even after Chrome restarts.

---

## Why not Playwright?

Playwright is a test framework. TERX is an execution layer with memory.

|  | Playwright | TERX |
|:--|:--:|:--:|
| Memory across runs | ✗ | ✓ |
| Raw CDP (no subprocess) | ✗ | ✓ |
| RAM per instance | ~120MB | ~2MB |
| Works with any agent | ✗ | ✓ |
| MCP server built-in | ✗ | ✓ |

---

## MCP tools

`browser_get_state` `browser_navigate` `browser_click` `browser_type` `browser_screenshot` `browser_scroll` `browser_new_tab` `cache_stats` `cache_invalidate`

Screenshots return hash refs, not base64 blobs — no context window poisoning.
Navigation validates URL schemes — blocks `javascript:` `data:` `file:` injections.

---

## Roadmap

- [x] Raw CDP bridge
- [x] AX tree extractor + stable element IDs
- [x] Fuzzy structural hasher
- [x] Muscle memory cache (SQLite, INSERT OR IGNORE)
- [x] Schema versioning + migrations
- [x] MCP server (9 tools)
- [x] Self-healing replay (LLM fallback on DOM drift)
- [x] Real LLM benchmark suite (`terx-bench-real`)
- [ ] Parametric replay — `{{email}}` variable interpolation
- [ ] MutationObserver cache invalidation
- [ ] `pip install "terx[browser-use]"` drop-in

---

## Docs

[ixchio.github.io/terx](https://ixchio.github.io/terx) · [Quick Start](docs/quickstart.md) · [Benchmarks](docs/benchmarks.md) · [Architecture](docs/development.md) · [Changelog](docs/changelog.md)

---

## Dev

```bash
git clone https://github.com/ixchio/terx && cd terx
pip install -e ".[dev]"
pytest tests/ -v          # 33 tests
terx-bench                # modeled baseline (no API key needed)
GROQ_API_KEY=... terx-bench-real  # real LLM run
```

---

MIT · built by [ixchio](https://github.com/ixchio)
