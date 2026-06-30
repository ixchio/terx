<div align="center">

```
████████╗███████╗██████╗ ██╗  ██╗
╚══██╔══╝██╔════╝██╔══██╗╚██╗██╔╝
   ██║   █████╗  ██████╔╝ ╚███╔╝
   ██║   ██╔══╝  ██╔══██╗ ██╔██╗
   ██║   ███████╗██║  ██║██╔╝ ██╗
   ╚═╝   ╚══════╝╚═╝  ╚═╝╚═╝  ╚═╝
```

### Browser agent memory. Raw CDP. No Playwright dependency.

[![CI](https://github.com/ixchio/terx/actions/workflows/tests.yml/badge.svg)](https://github.com/ixchio/terx/actions)
[![PyPI](https://img.shields.io/pypi/v/terx?color=3ddc84&label=PyPI)](https://pypi.org/project/terx/)
[![Downloads](https://img.shields.io/pypi/dm/terx?color=3ddc84)](https://pypi.org/project/terx/)
[![License: MIT](https://img.shields.io/badge/license-MIT-yellow)](LICENSE)
[![Python 3.11+](https://img.shields.io/badge/python-3.11+-blue)](https://python.org)

</div>

---

## The Problem

Your browser agent repeats expensive work — every single time.

- Logs into the same dashboards ↻
- Rediscovers the same buttons ↻
- Re-parses the same screens ↻
- Burns model tokens on workflows it already solved ↻

**Run 100 tasks → pay for 100 LLM calls. TERX makes that 1.**

---

## What TERX Does

TERX is a **replay memory layer** for browser agents.

**Run 1:** your agent figures out the path. TERX silently records the exact Chrome DevTools Protocol (CDP) command sequence into a local SQLite cache.

**Run 2 onward:** TERX replays the cached CDP commands directly — no LLM call, no screenshot parsing, no reasoning loop.

```
Run 1:  agent runs normally              3.05s · 1,985 tokens · $0.0065
         TERX silently records CDP commands

Run 2:  TERX replays                     0.090s · 0 tokens · $0.0000
Run 50: TERX replays                     ~0.09s · 0 tokens · $0.0000
```

<div align="center">
  <img src="https://raw.githubusercontent.com/ixchio/terx/main/docs/assets/terx-demo.gif" alt="TERX local replay demo" width="100%">
</div>

---

## Benchmark Numbers

> Real measurement. Real LLM (`openai/gpt-oss-120b` via Groq). Token counts from API response headers.

| Task | Agent (cold) | TERX (warm) | Speedup | Tokens saved |
|:-----|:------------:|:-----------:|:-------:|:------------:|
| User Login | 3.05s · $0.0065 | **0.090s · $0** | **34×** | 1,985 → 0 |
| Search + Filter | 17.82s · $0.0108 | **0.099s · $0** | **179×** | 2,634 → 0 |
| Multi-step Signup | 41.05s · $0.0142 | **0.103s · $0** | **399×** | 4,339 → 0 |
| Data Table | 11.27s · $0.0093 | **0.088s · $0** | **128×** | 1,756 → 0 |
| **Average** | **160.93s · $0.0925** | **0.926s · $0** | **174×** | **23,782 → 0** |

Cache hit rate: **10/10** · Reproduce: `GROQ_API_KEY=... python -m terx.benchmarks.real_agent`

No API key? Run the local eval:

```bash
terx demo         # cold record → warm replay, variables, redaction, postconditions
terx eval-local   # deterministic headless Chrome suite — verifiable without a key
```

Full methodology → [docs/benchmarks.md](https://github.com/ixchio/terx/blob/main/docs/benchmarks.md)

---

## Install

```bash
pip install terx
```

---

## Quickstart

### Option 1 — MCP Server (Claude Desktop, Cursor, Windsurf)

```bash
# 1. Open Chrome with remote debugging
google-chrome --remote-debugging-port=9222 --no-first-run

# 2. Start the TERX MCP server
terx-server
```

Add to your `mcp.json`:
```json
{ "mcpServers": { "terx": { "command": "terx-server" } } }
```

Wrap repeatable workflows with task markers — TERX records on first run and replays on every subsequent match:

```text
browser_task_start("login to dashboard")
  ...normal browser tools...
browser_task_finish(success=true)
```

Each response includes a structured **replay report**: commands replayed, variables used, redacted fields, postcondition metadata, and mutation guard stats.

---

### Option 2 — Python Library

Wrap your existing agent with two lines of context management:

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
            await your_agent.run()    # first run: agent runs, TERX records
```

Variable interpolation — typed values matching `variables` are stored as `{{email}}`, `{{password}}`, etc.  
Sensitive fields (password / token / API key inputs) are **redacted by default**.

```bash
TERX_REDACT_ALL_TEXT=1          # force all typed values through placeholders
TERX_REDACT_FIELDS=tenant,ws    # add custom sensitive labels
```

---

### Option 3 — Browser Use Adapter

Drop TERX into any Browser Use-style agent without changing your agent code:

```python
from terx.integrations.browser_use import wrap_browser_use

agent = BrowserUseAgent(...)
agent = wrap_browser_use(
    agent,
    cache=cache,
    bridge=bridge,
    task="login to dashboard",
    variables={"email": "...", "password": "..."},
    postcondition={"text_contains": "Welcome"},
)

result = await agent.run()  # TERX handles record/replay transparently
```

---

## How It Works

Three components, each doing one job:

**CDP Bridge** — raw `asyncio` WebSocket to Chrome. No Playwright subprocess, no Selenium, no ChromeDriver. Direct wire protocol. `<50ms` startup, `~2MB` RAM.

**DOM Extractor** — reads Chrome's Accessibility Tree, not raw HTML. Assigns stable numeric IDs to interactive elements. Computes a fuzzy structural hash that survives CSS refactors and A/B tests without breaking cache hits.

**Muscle Memory Cache** — SQLite. On task success: stores the CDP command sequence keyed by `(domain, dom_hash, task)`. On future runs: replays directly. Uses `INSERT OR IGNORE` — the first successful recording is canonical and never silently overwritten.

On replay, TERX re-snapshots the DOM and translates old `backendNodeId`s to current equivalents by matching `role + label` — so replays work even after Chrome restarts.

**Replay safety checks:**
- Optional postcondition validation — a replay that lands on the wrong page is rejected
- `MutationObserver` guard — tracks DOM churn during replay and aborts on abnormal mutation drift
- Self-healing LLM fallback — if DOM structure has drifted too far, falls back to the agent automatically

---

## CLI

```bash
terx doctor                          # diagnose connection + cache health
terx stats                           # cache size, hit rate, task inventory
terx inspect --domain app.example.com   # inspect cached sequences for a domain
terx purge app.example.com           # invalidate domain cache
terx demo                            # live record → replay demo (local Chrome)
terx eval-local                      # full deterministic eval suite
```

---

## TERX vs. Playwright

Playwright is a browser automation framework. TERX is a **memory layer** for agents that already know how to drive Chrome.

|  | Playwright | TERX |
|:--|:--:|:--:|
| Memory across runs | ✗ | ✓ |
| Raw CDP (no subprocess) | ✗ | ✓ |
| RAM per instance | ~120MB | ~2MB |
| Works with any agent | ✗ | ✓ |
| MCP server built-in | ✗ | ✓ |
| Token cost on repeat tasks | Full price | **$0** |

---

## MCP Tools Reference

| Tool | Description |
|:-----|:------------|
| `browser_task_start` | Begin a recordable task session |
| `browser_task_finish` | End session and commit to cache |
| `browser_get_state` | Snapshot current DOM / AX tree |
| `browser_navigate` | Navigate to URL (validates scheme) |
| `browser_click` | Click by element label |
| `browser_click_at` | Click by coordinates |
| `browser_type` | Type text (auto-redacts sensitive fields) |
| `browser_screenshot` | Capture screenshot → returns hash ref, not base64 |
| `browser_screenshot_get` | Retrieve screenshot by hash ref |
| `browser_scroll` | Scroll the page |
| `browser_new_tab` | Open a new tab |
| `cache_stats` | Cache inventory for current domain |
| `cache_invalidate` | Invalidate cached sequences |

**Security notes:**
- Screenshots return hash refs — no context window poisoning with base64 blobs
- Navigation validates URL schemes — blocks `javascript:` `data:` `file:` injections
- Task wrappers accept `variables` + `postcondition` for safe parametric replay
- Every replay returns a structured `report` object for audit

---

## Roadmap

- [x] Raw CDP bridge
- [x] AX tree extractor + stable element IDs
- [x] Fuzzy structural hasher
- [x] Muscle memory cache (SQLite, `INSERT OR IGNORE`)
- [x] Schema versioning + migrations
- [x] MCP server (13 tools)
- [x] Self-healing replay (LLM fallback on DOM drift)
- [x] Real LLM benchmark suite (`terx-bench-real`)
- [x] Parametric replay — `{{variable}}` interpolation
- [x] Secret redaction for password / token / API-key fields
- [x] Replay postconditions
- [x] Browser Use-style adapter
- [x] MutationObserver replay drift guard
- [x] CLI doctor / stats / inspect / purge
- [x] Local Chrome eval suite (`terx eval-local`)
- [ ] Persistent cache across machines (optional remote backend)
- [ ] Stagehand adapter
- [ ] Playwright bridge compatibility layer

---

## Dev

```bash
git clone https://github.com/ixchio/terx && cd terx
pip install -e ".[dev]"
pytest tests/ -v
terx demo                              # local Chrome demo with variables + redaction
terx eval-local                        # deterministic local browser replay eval suite
python -m terx.benchmarks.baseline     # modeled baseline (no API key needed)
GROQ_API_KEY=... python -m terx.benchmarks.real_agent   # real LLM run
```

Contributions welcome — read [CONTRIBUTING.md](CONTRIBUTING.md) first.

---

## Docs

[ixchio.github.io/terx](https://ixchio.github.io/terx) · [Quick Start](https://github.com/ixchio/terx/blob/main/docs/quickstart.md) · [Benchmarks](https://github.com/ixchio/terx/blob/main/docs/benchmarks.md) · [Architecture](https://github.com/ixchio/terx/blob/main/docs/development.md) · [Project Structure](https://github.com/ixchio/terx/blob/main/docs/project-structure.md) · [Changelog](https://github.com/ixchio/terx/blob/main/docs/changelog.md)

---

<div align="center">

**Built by [ixchio](https://github.com/ixchio) · MIT License**

*If TERX saves you tokens, consider starring the repo ⭐*

</div>
