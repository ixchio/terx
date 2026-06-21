<div align="center">

# ⚡ TERX

### Memory layer for browser agents.
**Run 1 costs tokens. Run 2 costs nothing.**

<br>
<a href="https://github.com/ixchio/terx/actions/workflows/tests.yml"><img src="https://github.com/ixchio/terx/actions/workflows/tests.yml/badge.svg" alt="CI Status"></a>
<a href="https://pypi.org/project/terx/"><img src="https://img.shields.io/pypi/v/terx?style=flat-square&color=00d4aa&label=PyPI&logo=pypi" alt="PyPI"></a>
<a href="https://opensource.org/licenses/MIT"><img src="https://img.shields.io/badge/license-MIT-yellow?style=flat-square" alt="License"></a>
<a href="https://www.python.org/downloads/"><img src="https://img.shields.io/badge/python-3.11+-blue?style=flat-square" alt="Python"></a>
<a href="https://github.com/ixchio/agent-vcr"><img src="https://img.shields.io/badge/Works%20with-Agent%20VCR-purple?style=flat-square" alt="Works with Agent VCR"></a>
<br><br>

```bash
pip install terx
```

No Playwright. No Selenium. Raw CDP. Works with any agent.

<br>

</div>

---

## The Problem

Every browser agent is amnesiac.

Your agent logs into Salesforce 50 times a day. It re-discovers the login button 50 times. Burns tokens 50 times.

```
Run 1:  LLM → finds login → clicks → succeeds   [$0.008 · 2,100 tokens · 3.2s]
Run 2:  LLM → finds login → clicks → succeeds   [$0.008 · 2,100 tokens · 3.1s]
...
Run 50: LLM → finds login → clicks → succeeds   [$0.008 · 2,100 tokens · 3.0s]

Total: $0.40 · 105,000 tokens — for the SAME task, repeated
```

**With TERX:**

```
Run 1:  Agent runs normally → TERX silently records      [$0.008 · 2,100 tokens · 3.2s]
Run 2:  TERX replays                                     [$0.000 ·     0 tokens · 41ms]
Run 3:  TERX replays                                     [$0.000 ·     0 tokens · 38ms]
...
Run 50: TERX replays                                     [$0.000 ·     0 tokens · 40ms]

Total: $0.008 — 98% cost reduction. Zero code changes after Run 1.
```

---

## Benchmarks

10-task suite. Real Chrome. Real CDP. Measured, not estimated.

| Task | Steps | Without TERX | With TERX | Speedup | Token savings |
|:---|:---:|:---:|:---:|:---:|:---:|
| User Login Flow | 2 | 4.57s · $0.036 | **0.47s · $0.000** | **9.6x** | **100%** |
| Search + Filter | 2 | 4.69s · $0.036 | **0.17s · $0.000** | **27.6x** | **100%** |
| Multi-step Signup | 3 | 6.84s · $0.053 | **0.13s · $0.000** | **51.3x** | **100%** |
| E-commerce Product | 2 | 4.70s · $0.036 | **0.16s · $0.000** | **29.1x** | **100%** |
| Settings Toggles | 2 | 4.82s · $0.036 | **0.17s · $0.000** | **27.7x** | **100%** |
| Data Table Pagination | 2 | 4.65s · $0.036 | **0.14s · $0.000** | **33.4x** | **100%** |
| Support Ticket | 3 | 6.77s · $0.053 | **0.13s · $0.000** | **51.1x** | **100%** |
| Fuzzy Search | 2 | 4.56s · $0.036 | **0.13s · $0.000** | **33.8x** | **100%** |
| Profile Update | 3 | 6.86s · $0.053 | **0.17s · $0.000** | **41.4x** | **100%** |
| Complex Form | 2 | 4.66s · $0.036 | **0.16s · $0.000** | **29.3x** | **100%** |
| **Average** | — | **5.31s · $0.041** | **0.18s · $0.000** | **🔥 28.8x faster** | **🔥 100%** |

GPT-4o pricing ($2.50/1M input, $10/1M output). [Reproduce it: `terx-bench`](BENCHMARKS.md)

---

## Quick Start

### 1. Start Chrome

```bash
google-chrome --remote-debugging-port=9222 --no-first-run
# headless:
google-chrome --remote-debugging-port=9222 --headless=new
```

### 2. Wrap your agent (3 lines)

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
                await ctx.replay()          # Run 2+: 40ms, $0.000
            else:
                await your_agent.run(...)   # Run 1: agent runs, TERX records

        print(ctx.ledger)
        # ⚡ Cache HIT · 12 commands · 41ms · ~12 LLM calls saved · run #3

asyncio.run(run_task())
```

### 3. Or use the MCP server (works with Claude Desktop, Cursor, Windsurf)

```bash
pip install terx
terx-server
```

`mcp.json`:
```json
{
  "mcpServers": {
    "terx": { "command": "terx-server" }
  }
}
```

---

## How It Works

Three components, each doing one job:

**1. CDP Bridge** — raw `asyncio` WebSocket to Chrome. No Playwright. No subprocess. `<50ms` startup, `~2MB` RAM.

**2. DOM Extractor** — reads the accessibility tree, not raw HTML. Assigns stable numeric IDs to interactable elements. Computes a fuzzy structural hash that survives minor CSS/class changes without breaking cache hits.

**3. Muscle Memory Cache** — SQLite-backed. On task success: stores the CDP command sequence keyed by `(domain, dom_hash, task)`. On future runs: replays directly. Writes `.vcr` audit files. Uses `INSERT OR IGNORE` — first successful recording is canonical, never silently overwritten.

**4. Self-Healing Replay** — If the DOM drifts, TERX evaluates the new state via LLM to heal parameters. SSIM visual auditing warns of silent UI changes.

---

## Works With Any Agent

TERX sits below your agent framework. It doesn't care what LLM you use.

```python
# browser-use
from browser_use import Agent
async with session_for(cache, bridge, "book a flight") as ctx:
    if not ctx.hit:
        await Agent(task="book a flight", llm=ChatOpenAI()).run()

# LangChain
async with session_for(cache, bridge, "scrape product prices") as ctx:
    if not ctx.hit:
        await langchain_agent.run("scrape product prices")

# Raw Claude / GPT
async with session_for(cache, bridge, "submit expense report") as ctx:
    if not ctx.hit:
        await your_claude_loop()
```

---

## MCP Tools

| Tool | What it does |
|---|---|
| `browser_get_state` | AX tree snapshot — stable element IDs, no hallucination-prone HTML |
| `browser_navigate` | Navigate to URL (scheme-validated) |
| `browser_click` | Click element by stable ID |
| `browser_type` | Type into input (React/Vue/Svelte safe — fires native setter) |
| `browser_screenshot` | Returns hash ref, NOT base64 blob (no context poisoning) |
| `browser_scroll` | Scroll up/down |
| `browser_new_tab` | Open new tab |
| `cache_stats` | Hit rate, savings, unique domains |
| `cache_invalidate` | Clear cache for a domain when the UI ships a redesign |

---

## Why Not Playwright?

Playwright is a testing framework. TERX is an agent execution layer.

| | Playwright | TERX |
|---|---|---|
| Purpose | Browser testing | AI agent execution |
| Protocol | CDP (wrapped) | CDP (raw) |
| RAM | ~120MB per subprocess | ~2MB |
| Startup | 800ms–2s | <50ms |
| Memory across runs | ❌ | ✅ Muscle memory cache |
| MCP integration | External wrapper | Built-in |
| `.vcr` audit files | ❌ | ✅ |
| Works with any agent | ❌ Playwright API only | ✅ Framework-agnostic |

---

## The `.vcr` Format

Plain JSONL. Human-readable. Git-diffable. Time-travel debuggable via [Agent VCR](https://github.com/ixchio/agent-vcr).

```jsonl
{"type": "session", "data": {"task": "login to salesforce", "domain": "salesforce.com", "cache_hit": true}}
{"type": "frame",   "data": {"cdp_method": "Page.navigate",              "latency_ms": 145, "cache_hit": true}}
{"type": "frame",   "data": {"cdp_method": "Input.dispatchMouseEvent",   "latency_ms": 8,   "cache_hit": true}}
{"type": "frame",   "data": {"cdp_method": "Input.insertText",           "latency_ms": 6,   "cache_hit": true}}
```

```python
from agent_vcr import VCRPlayer

player = VCRPlayer.load(".vcr/browser_salesforce_1234567890.vcr")
print(player.goto_frame(3))   # step-through replay
print(player.get_total_cost())  # 0.0 on cache-hit runs
```

---

## Security

TERX blocks unsafe URL schemes before any navigation:

```python
# Blocked automatically:
browser_navigate("javascript:alert(1)")    # JS injection
browser_navigate("data:text/html,<script>")  # data: bypass
browser_navigate("file:///etc/passwd")     # local file read

# Allowed:
browser_navigate("https://salesforce.com")   # ✅
browser_navigate("http://localhost:3000")    # ✅
```

Screenshots return hash references, not raw base64 — preventing context poisoning where large images consume your entire context window.

---

## Install

```bash
# Core — CDP bridge + cache + MCP server
pip install terx

# + local embeddings for semantic element lookup
pip install "terx[embeddings]"

# + visual SSIM auditing + LLM self-healing
pip install "terx[vision,healing]"
```

---

## Roadmap

- [x] Raw CDP bridge (no Playwright)
- [x] Accessibility tree extractor with stable element IDs
- [x] Fuzzy DOM structural hasher (survives CSS changes)
- [x] Muscle memory cache (SQLite, INSERT OR IGNORE semantics)
- [x] Schema versioning + migrations
- [x] `.vcr` output (Agent VCR compatible)
- [x] FastMCP Server — 9 tools
- [x] Framework-adaptive input (React/Vue native setter)
- [x] Screenshot hash refs (no context poisoning)
- [x] Self-healing replay (LLM fallback on DOM drift)
- [x] Visual audits via SSIM
- [x] Transparent CDP recording proxy
- [x] Configurable connection timeouts
- [x] 10-task benchmark suite (`terx-bench`)
- [ ] Parametric replay — `{{email}}` variable substitution in cached sequences
- [ ] MutationObserver-based cache invalidation
- [ ] Local embedding element lookup (`sentence-transformers`)
- [ ] browser-use drop-in integration (`pip install "terx[browser-use]"`)
- [ ] Published 100-task benchmark vs browser-use on real sites

---

## Contributing

```bash
git clone https://github.com/ixchio/terx
cd terx
pip install -e ".[dev]"
pytest tests/ -v      # 33 tests, all green
terx-bench            # run the 10-task benchmark yourself
```

---

## License

MIT

---

<div align="center">

### ⚡

**Run 1 costs tokens. Run 2 costs nothing.**

<br>

```bash
pip install terx
```

<br>

Works with [Agent VCR](https://github.com/ixchio/agent-vcr) — time-travel debugging for AI agents.

<br>

<sub>Built by <a href="https://github.com/ixchio">ixchio</a> · MIT License</sub>

</div>
