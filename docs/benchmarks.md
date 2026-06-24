# Benchmarks

Real measured performance of TERX vs a live LLM agent loop.

---

## Methodology

> No simulated latency. No modeled token counts. Every number measured.

| Metric | How |
|:---|:---|
| LLM agent time | `time.perf_counter()` wall-clock, end-to-end |
| Token counts | Groq API `usage.prompt_tokens` + `usage.completion_tokens` in response |
| TERX replay time | Wall-clock including DOM snapshot + hash lookup + CDP replay |
| Cost | Groq API pricing for `openai/gpt-oss-120b` ($2.50/1M in, $10.00/1M out) |

**Setup:**
- LLM: `openai/gpt-oss-120b` (reasoning model) via [Groq API](https://console.groq.com)
- Browser: Headless Chrome, `--remote-debugging-port=9222`
- Tasks: 10 form/search/navigation tasks on a local benchmark server (127.0.0.1)
- Cache: Fresh SQLite per run. Agent cold run → TERX warm replay

---

## Results

**10/10 cache hits. 100% token savings on every warm run.**

| Task | LLM Steps | Agent (cold) | TERX (warm) | Speedup | Tokens | Cost |
|:-----|:---------:|-------------:|------------:|--------:|-------:|-----:|
| User Login Flow | 4 | 3.05s | 0.090s | **34.0x** | 1,985 | $0.0065 |
| Search and Filter | 4 | 17.82s | 0.099s | **179.3x** | 2,634 | $0.0108 |
| Multi-step Signup Form | 6 | 41.05s | 0.103s | **399.4x** | 4,339 | $0.0142 |
| E-commerce Product Page | 4 | 9.84s | 0.100s | **98.1x** | 2,798 | $0.0123 |
| Settings Toggle Options | 4 | 15.42s | 0.100s | **154.5x** | 2,131 | $0.0082 |
| Data Table Pagination | 3 | 11.27s | 0.088s | **128.7x** | 1,756 | $0.0093 |
| Support Ticket Submit | 4 | 18.35s | 0.100s | **184.2x** | 2,130 | $0.0078 |
| Fuzzy Search Navigation | 3 | 10.41s | 0.083s | **124.8x** | 1,393 | $0.0055 |
| Profile Update Flow | 4 | 17.20s | 0.077s | **223.7x** | 2,544 | $0.0104 |
| Complex Nested Form | 4 | 16.51s | 0.086s | **192.2x** | 2,072 | $0.0074 |
| **Total / Average** | — | **160.93s** | **0.926s** | **173.9x** | **23,782** | **$0.0925** |

**TERX warm run: 0 tokens · $0.0000 — every time.**

---

## What these numbers mean

The agent used a **reasoning model** (`openai/gpt-oss-120b`), which is slower
and more expensive than standard LLMs. The cold agent loop spent 160.93s across
10 tasks. TERX replayed the same recorded workflows in 0.926s total.

With a faster model (GPT-4o-mini, Llama 3.3 70B), cold runs would be 1–3s per task. TERX replay stays the same regardless — it replays CDP commands, not LLM calls.

---

## How TERX achieves 0 tokens

On a cache hit, TERX:

1. Navigates to the page (real HTTP, ~100ms)
2. Snapshots the AX tree and computes a structural hash (~5ms)
3. Looks up the hash in SQLite (~1ms) → **HIT**
4. Replays stored CDP commands: `Input.insertText`, `Input.dispatchMouseEvent` (~50–250ms)

Zero LLM calls. The page state after replay is identical to after the original agent run.

---

## Reproduce

### Requirements
- Python 3.11+
- Google Chrome installed
- A Groq API key (free at [console.groq.com](https://console.groq.com))

### Run

```bash
# Clone and install
git clone https://github.com/ixchio/terx.git
cd terx
pip install -e ".[benchmark]"

# Set your key (never hardcode it)
cp .env.example .env
# edit .env and fill in GROQ_API_KEY=gsk_...

# Load env and run
export $(grep -v '^#' .env | xargs)
python -m terx.benchmarks.real_agent
# → prints results table and writes .benchmarks/real_agent_latest.md
```

For a fast local production-path smoke test without an API key:

```bash
terx demo
terx eval-local
# → records local workflows, replays with variables, checks postconditions
```

> **Security note:** Never commit `.env`. It's in `.gitignore`. Use `cp .env.example .env` and fill locally.

### What it outputs

```
🤖 Model:  openai/gpt-oss-120b
📋 Tasks:  10
================================================================

▶ [01/10] User Login Flow
   🧠 LLM agent running... ✓ 3.11s | 1,985 tokens | $0.0065 | 4 LLM steps
   ⚡ TERX replay... HIT ✓ 0.090s | 0 tokens | $0.0000

...

TOTAL / AVERAGE   —  160.93s  0.926s  173.9x  23,782  $0.0925  10/10
```

Source: [`terx/benchmarks/real_agent.py`](https://github.com/ixchio/terx/blob/main/terx/benchmarks/real_agent.py)

---

## Modeled baseline (original benchmark)

The original `terx-bench` (`terx/benchmarks/baseline.py`) uses a **modeled** LLM baseline:
- Real TERX timing (measured)
- LLM latency = 2.2s per step (GPT-4o p50) + real CDP execution time
- Token counts = 6,500 input + 150 output per step (from browser-use architecture)

This is documented for reproducibility without needing an API key:

```bash
terx-bench   # no API key needed, uses modeled LLM costs
```

The real benchmark (`terx-bench-real`) is authoritative for credibility. The modeled benchmark is for quick local verification.
