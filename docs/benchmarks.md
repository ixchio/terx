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
| User Login Flow | 4 | 2.93s | 0.078s | **37.7x** | 2,090 | $0.0076 |
| Search and Filter | 5 | 3.99s | 0.101s | **39.7x** | 3,533 | $0.0136 |
| Multi-step Signup Form | 2 | 1.54s | 0.062s | **25.0x** | 1,035 | $0.0045 |
| E-commerce Product Page | 5 | 25.97s | 0.091s | **286.0x** | 3,811 | $0.0161 |
| Settings Toggle Options | 4 | 16.01s | 0.103s | **155.9x** | 2,451 | $0.0095 |
| Data Table Pagination | 12 | 90.84s | 0.259s | **350.4x** | 12,479 | $0.0567 |
| Support Ticket Submit | 4 | 15.40s | 0.101s | **152.2x** | 2,135 | $0.0078 |
| Fuzzy Search Navigation | 3 | 11.85s | 0.089s | **132.9x** | 1,459 | $0.0062 |
| Profile Update Flow | 3 | 12.35s | 0.094s | **131.2x** | 1,854 | $0.0086 |
| Complex Nested Form | 4 | 17.74s | 0.110s | **161.5x** | 2,146 | $0.0082 |
| **Total / Average** | — | **198.62s** | **1.087s** | **🔥 182.7x** | **32,993** | **$0.1388** |

**TERX warm run: 0 tokens · $0.0000 — every time.**

---

## What these numbers mean

The agent used a **reasoning model** (`openai/gpt-oss-120b`), which is slower and more expensive than standard LLMs. On complex tasks like "Data Table Pagination" it took 12 LLM steps and 90 seconds. TERX replayed that in 259ms.

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
pip install -e ".[dev]"

# Set your key (never hardcode it)
cp .env.example .env
# edit .env and fill in GROQ_API_KEY=gsk_...

# Load env and run
export $(grep -v '^#' .env | xargs)
python -m terx.benchmarks.real_agent
# → prints results table and writes BENCHMARKS.md
```

> **Security note:** Never commit `.env`. It's in `.gitignore`. Use `cp .env.example .env` and fill locally.

### What it outputs

```
🤖 Model:  openai/gpt-oss-120b
📋 Tasks:  10
================================================================

▶ [01/10] User Login Flow
   🧠 LLM agent running... ✓ 2.93s | 2,090 tokens | $0.0076 | 4 LLM steps
   ⚡ TERX replay... HIT ✓ 0.078s | 0 tokens | $0.0000

...

TOTAL / AVERAGE   —  198.62s  1.087s  182.7x  32,993  $0.1388  10/10
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
