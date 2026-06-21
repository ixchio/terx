# TERX Real Benchmark Results

## Methodology

**This is a fully real measurement — no simulated latency, no modeled token counts.**

| Component | How it's measured |
|:---|:---|
| LLM agent time | Real wall-clock (`time.perf_counter()`) end-to-end |
| Token counts | From Groq API response `usage.prompt_tokens` + `usage.completion_tokens` |
| TERX replay time | Real wall-clock including DOM snapshot + hash lookup + CDP replay |
| Cost | Groq API pricing for `openai/gpt-oss-120b` ($2.50/1M input, $10.00/1M output) |

- **LLM agent**: `openai/gpt-oss-120b` via [Groq API](https://groq.com). Real multi-step agent loop. Each step: AX tree snapshot → LLM call → CDP action (type/click). Conversation history maintained across steps.
- **TERX**: First run = agent records CDP sequence. Second run = pure replay — no LLM, no screenshot, no re-discovery.
- **Environment**: Headless Chrome on local machine. Benchmark HTTP server on 127.0.0.1. Fresh SQLite cache per run.
- **Cache hit condition**: DOM structural hash on warm entry must fuzzy-match (≥0.85) the hash stored from cold run.

---

## Results

10 tasks × 2 runs (1 LLM agent cold, 1 TERX warm replay). **10/10 cache hits.**

| Task | LLM Steps | Agent (cold) | TERX (warm) | Speedup | Tokens | Cost | Cache |
|:-----|:---------:|-------------:|------------:|--------:|-------:|-----:|:-----:|
| User Login Flow | 4 | 2.93s | 0.078s | **37.7x** | 2,090 | $0.0076 | ✓ |
| Search and Filter | 5 | 3.99s | 0.101s | **39.7x** | 3,533 | $0.0136 | ✓ |
| Multi-step Signup Form | 2 | 1.54s | 0.062s | **25.0x** | 1,035 | $0.0045 | ✓ |
| E-commerce Product Page | 5 | 25.97s | 0.091s | **286.0x** | 3,811 | $0.0161 | ✓ |
| Settings Toggle Options | 4 | 16.01s | 0.103s | **155.9x** | 2,451 | $0.0095 | ✓ |
| Data Table Pagination | 12 | 90.84s | 0.259s | **350.4x** | 12,479 | $0.0567 | ✓ |
| Support Ticket Submit | 4 | 15.40s | 0.101s | **152.2x** | 2,135 | $0.0078 | ✓ |
| Fuzzy Search Navigation | 3 | 11.85s | 0.089s | **132.9x** | 1,459 | $0.0062 | ✓ |
| Profile Update Flow | 3 | 12.35s | 0.094s | **131.2x** | 1,854 | $0.0086 | ✓ |
| Complex Nested Form | 4 | 17.74s | 0.110s | **161.5x** | 2,146 | $0.0082 | ✓ |
| **Total / Average** | — | **198.62s** | **1.087s** | **🔥 182.7x** | **32,993** | **$0.1388** | **10/10** |

**Per-repeat savings: $0.1388 → $0.0000. 100% token reduction.**

---

## The numbers explained

The agent used a **reasoning model** (`openai/gpt-oss-120b`) which explains:
- High token counts on complex tasks (Data Table Pagination: 12,479 tokens, 12 LLM steps)
- Slower cold runs than a non-reasoning model would produce
- TERX replay is unaffected — it replays CDP commands, not LLM calls

With a faster model (GPT-4o-mini, Llama 3.3 70B), cold run times would be 1–4s. The speedup ratio would be lower in absolute terms but **TERX replay time stays the same** regardless of which model the agent uses.

---

## Reproduce

```bash
pip install terx
export GROQ_API_KEY=your_groq_key   # get one at console.groq.com, it's free
python -m terx.benchmarks.real_agent
# → outputs BENCHMARKS.md with your measured numbers
```

Source: [`terx/benchmarks/real_agent.py`](terx/benchmarks/real_agent.py)

---

## How TERX achieves 0 tokens on warm runs

TERX intercepts the Chrome DevTools Protocol (CDP) at the WebSocket level. On a cache hit:

1. Navigate to page (real HTTP)
2. Snapshot AX tree, compute structural hash (real, ~5ms)
3. Hash lookup → HIT (SQLite, ~1ms)
4. Replay stored CDP commands: `Input.insertText`, `Input.dispatchMouseEvent` (real Chrome, ~50–250ms)
5. Done

Zero LLM calls. Zero token spend. The page state after replay is identical to after the original agent run.
