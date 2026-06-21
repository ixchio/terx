# TERX Documentation

TERX is a memory layer for browser agents. Run a task once — TERX records the exact Chrome DevTools Protocol sequence. Every subsequent identical run replays in milliseconds with zero LLM calls.

---

## Docs

| Guide | Description |
|:---|:---|
| [Quick Start](quickstart.md) | Get running in 5 minutes |
| [Benchmarks](benchmarks.md) | Real measured numbers — 182.7x speedup, 100% token savings |
| [Architecture](development.md) | How TERX works internally |
| [Changelog](changelog.md) | Version history |

---

## At a glance

```
Run 1:  LLM agent discovers the path   →  $0.0076 · 2,090 tokens · 2.93s
         TERX silently records CDP commands

Run 2:  TERX replays                   →  $0.0000 · 0 tokens · 0.078s
Run 3+: Same
```

**Numbers from a real measured run** against `openai/gpt-oss-120b` via Groq API. [See full benchmark →](benchmarks.md)

---

## Install

```bash
pip install terx
```

Start Chrome:

```bash
google-chrome --remote-debugging-port=9222 --no-first-run
```

Run the MCP server (works with Claude Desktop, Cursor, Windsurf):

```bash
terx-server
```

---

## Repository

- **Source**: [github.com/ixchio/terx](https://github.com/ixchio/terx)
- **Issues**: [github.com/ixchio/terx/issues](https://github.com/ixchio/terx/issues)
- **License**: MIT
