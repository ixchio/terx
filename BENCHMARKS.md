# TERX Real Benchmark Results

## Methodology

**This is a real measurement — no modeled constants.**

- **LLM agent**: `openai/gpt-oss-120b` via Groq API. Real API calls, real token counts from response headers.
- **TERX replay**: Real Chrome CDP replay. Measured wall-clock time end-to-end.
- **Benchmark pages**: Local HTTP server (127.0.0.1). Tasks are form fills, clicks, searches.
- **Cache**: Fresh SQLite per run. LLM agent records on first run, TERX replays on second.

## Results

| Task | Steps | LLM Agent | TERX Replay | Speedup | Tokens | Cost | Cache |
|:-----|:-----:|----------:|------------:|--------:|-------:|-----:|:-----:|
| User Login Flow | 4 | 3.54s | 0.082s | **43.0x** | 2,083 | $0.0075 | ✓ |
| **Total / Average** | — | **3.54s** | **0.082s** | **43.0x** | **2,083** | **$0.0075** | **1/1** |

## Pricing

Model: `openai/gpt-oss-120b` — $2.5/1M input, $10.0/1M output.
TERX replay: **$0.0000** — zero LLM calls.

## Reproduce

```bash
pip install terx
export GROQ_API_KEY=your_key
python -m terx.benchmarks.real_agent
```

Source: [`terx/benchmarks/real_agent.py`](terx/benchmarks/real_agent.py)
