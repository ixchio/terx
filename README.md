<div align="center">

```
████████╗███████╗██████╗ ██╗  ██╗
╚══██╔══╝██╔════╝██╔══██╗╚██╗██╔╝
   ██║   █████╗  ██████╔╝ ╚███╔╝
   ██║   ██╔══╝  ██╔══██╗ ██╔██╗
   ██║   ███████╗██║  ██║██╔╝ ██╗
   ╚═╝   ╚══════╝╚═╝  ╚═╝╚═╝  ╚═╝
```

**Browser agent memory. Raw CDP. No Playwright dependency.**

[![CI](https://github.com/ixchio/terx/actions/workflows/tests.yml/badge.svg)](https://github.com/ixchio/terx/actions)
[![PyPI](https://img.shields.io/pypi/v/terx?color=3ddc84&label=PyPI)](https://pypi.org/project/terx/)
[![License: MIT](https://img.shields.io/badge/license-MIT-yellow)](LICENSE)
[![Python 3.11+](https://img.shields.io/badge/python-3.11+-blue)](https://python.org)

</div>

---

Browser agents repeat expensive work.

They log into the same dashboards, rediscover the same buttons, parse the same
screens, and spend model tokens on workflows they already solved.

TERX is a replay memory layer for browser agents.

Run 1: your agent figures out the path. TERX records the exact Chrome DevTools
Protocol (CDP) commands into a local SQLite cache.
Run 2 onward: TERX replays the CDP commands directly. No LLM call, no screenshot
parsing, and no repeated reasoning loop.

<div align="center">
  <img src="https://raw.githubusercontent.com/ixchio/terx/main/docs/assets/terx-demo.gif" alt="TERX local replay demo" width="100%">
</div>

```
Run 1:  agent runs normally              3.05s · 1,985 tokens · $0.0065
         TERX silently records CDP commands

Run 2:  TERX replays                     0.090s · 0 tokens · $0.0000
Run 50: TERX replays                     ~0.09s · 0 tokens · $0.0000
```

---

## Numbers

Real measurement. Real LLM (`openai/gpt-oss-120b` via Groq). Token counts from API response headers.

| task | agent | terx | speedup | tokens |
|:-----|------:|-----:|--------:|-------:|
| User Login | 3.05s · $0.0065 | **0.090s · $0** | 34.0x | 1,985 → 0 |
| Search + Filter | 17.82s · $0.0108 | **0.099s · $0** | 179.3x | 2,634 → 0 |
| Multi-step Signup | 41.05s · $0.0142 | **0.103s · $0** | 399.4x | 4,339 → 0 |
| Data Table | 11.27s · $0.0093 | **0.088s · $0** | 128.7x | 1,756 → 0 |
| **average** | **160.93s total · $0.0925** | **0.926s total · $0** | **173.9x** | **23,782 → 0** |

Cache hit rate: **10/10**. Reproduce: `GROQ_API_KEY=... python -m terx.benchmarks.real_agent`

Full methodology → [docs/benchmarks.md](docs/benchmarks.md)

No API key proof path:

```bash
terx demo
terx eval-local
```

Those commands run real headless Chrome against local pages and verify cold
recording, warm replay, variables, redaction, postconditions, and replay reports.

---

## Install

```bash
pip install terx
```

---

## Use it

**Option 1: MCP server** — use with Claude Desktop, Cursor, Windsurf.

```bash
google-chrome --remote-debugging-port=9222 --no-first-run
terx-server
```

`mcp.json`:
```json
{ "mcpServers": { "terx": { "command": "terx-server" } } }
```

For repeatable workflows, wrap the browser actions with:

```text
browser_task_start("login to dashboard")
...normal browser tools...
browser_task_finish(success=true)
```

On the next matching run, `browser_task_start` replays the cached CDP sequence immediately.
Each task response includes a structured replay report with commands replayed,
variables used, redacted fields, postcondition metadata, and mutation guard stats.

---

**Option 2: Python library** — wrap your existing agent.

```python
from terx.cdp.session import BrowserSession
from terx.cache.cache import MemoryCache, session_for

cache = MemoryCache()

async with BrowserSession() as session:
    bridge = session.bridge()
    variables = {"email": "user@example.com", "password": "..."}

    async with session_for(
        cache,
        bridge,
        "login to salesforce",
        variables=variables,
        postcondition={"text_contains": "Welcome"},
    ) as ctx:
        if ctx.hit:
            await ctx.replay()        # 0 tokens, ~80ms
        else:
            await your_agent.run()    # first time: agent runs, TERX records
```

Typed values that match `variables` are stored as `{{email}}`, `{{password}}`, etc.
Sensitive fields such as password/token/API-key inputs are redacted by default.
Set `TERX_REDACT_ALL_TEXT=1` to force every typed value through placeholders.
Set `TERX_REDACT_FIELDS=tenant,workspace` to add custom sensitive labels.

---

**Option 3: Browser Use-style adapter** — wrap any agent object with an async `run()`.

```python
from terx.integrations.browser_use import wrap_browser_use

agent = BrowserUseAgent(...)  # or any Browser Use-style object with run()
agent = wrap_browser_use(
    agent,
    cache=cache,
    bridge=bridge,
    task="login to dashboard",
    variables={"email": "...", "password": "..."},
    postcondition={"text_contains": "Welcome"},
)

result = await agent.run()
```

---

## How it works

Three things, each doing one job:

**CDP Bridge** — raw asyncio WebSocket to Chrome. No Playwright subprocess. No Selenium. Direct wire protocol. `<50ms` startup, `~2MB` RAM.

**DOM Extractor** — reads Chrome's Accessibility Tree, not raw HTML. Assigns stable numeric IDs to interactive elements. Computes a fuzzy structural hash that survives CSS refactors and A/B tests without breaking cache hits.

**Muscle Memory Cache** — SQLite. On task success: stores the CDP command sequence keyed by `(domain, dom_hash, task)`. On future runs: replays directly. Uses `INSERT OR IGNORE` — first successful recording is canonical, never silently overwritten.

On replay, TERX re-snapshots the DOM and translates old `backendNodeId`s to current equivalents by matching `role + label` — so replays work even after Chrome restarts.

TERX also validates optional postconditions after replay. A replay that executes
commands but lands on the wrong page does not count as a hit.
During replay, a MutationObserver guard tracks DOM churn and raises on abnormal
mutation drift before a suspicious replay is counted as healthy.

The `terx` CLI gives operators cache visibility:

```bash
terx doctor
terx stats
terx inspect --domain app.example.com
terx purge app.example.com
```

---

## Why not Playwright?

Playwright is a full browser automation framework. TERX is a lean replay/memory layer
for agents that already know how to drive Chrome.

|  | Playwright | TERX |
|:--|:--:|:--:|
| Memory across runs | ✗ | ✓ |
| Raw CDP (no subprocess) | ✗ | ✓ |
| RAM per instance | ~120MB | ~2MB |
| Works with any agent | ✗ | ✓ |
| MCP server built-in | ✗ | ✓ |

---

## MCP tools

`browser_task_start` `browser_task_finish` `browser_get_state` `browser_navigate`
`browser_click` `browser_click_at` `browser_type` `browser_screenshot`
`browser_screenshot_get` `browser_scroll` `browser_new_tab` `cache_stats`
`cache_invalidate`

Screenshots return hash refs, not base64 blobs — no context window poisoning.
Navigation validates URL schemes — blocks `javascript:` `data:` `file:` injections.
Task wrappers record successful workflows and replay cache hits without another LLM call.
Task wrappers accept `variables` and `postcondition` for safe parametric replay.
Task wrappers also return `report` objects so MCP clients can audit what TERX did.

---

## Roadmap

- [x] Raw CDP bridge
- [x] AX tree extractor + stable element IDs
- [x] Fuzzy structural hasher
- [x] Muscle memory cache (SQLite, INSERT OR IGNORE)
- [x] Schema versioning + migrations
- [x] MCP server (13 tools)
- [x] Self-healing replay (LLM fallback on DOM drift)
- [x] Real LLM benchmark suite (`terx-bench-real`)
- [x] Parametric replay — `{{email}}` variable interpolation
- [x] Secret redaction for typed password/token/API-key fields
- [x] Replay postconditions
- [x] Browser Use-style adapter
- [x] MutationObserver replay drift guard
- [x] CLI doctor/stats/inspect/purge
- [x] Local Chrome eval suite (`terx eval-local`)

---

## Docs

[ixchio.github.io/terx](https://ixchio.github.io/terx) · [Quick Start](docs/quickstart.md) · [Benchmarks](docs/benchmarks.md) · [Architecture](docs/development.md) · [Project Structure](docs/project-structure.md) · [Stagehand](docs/stagehand.md) · [Changelog](docs/changelog.md)

---

## Dev

```bash
git clone https://github.com/ixchio/terx && cd terx
pip install -e ".[dev]"
pytest tests/ -v
terx demo                 # local Chrome demo with variables + redaction
terx eval-local           # deterministic local browser replay eval suite
terx-bench                # modeled baseline (no API key needed)
GROQ_API_KEY=... terx-bench-real  # real LLM run
```

---

MIT · built by [ixchio](https://github.com/ixchio)
