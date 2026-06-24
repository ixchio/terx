# TERX Documentation

TERX is a memory layer for browser agents. Run a task once — TERX records the exact Chrome DevTools Protocol sequence. Every subsequent identical run replays in milliseconds with zero LLM calls.

---

## Docs

| Guide | Description |
|:---|:---|
| [Quick Start](quickstart.md) | Get running in 5 minutes |
| [Benchmarks](benchmarks.md) | Real measured numbers — 173.9x speedup, 100% token savings |
| [Architecture](development.md) | How TERX works internally |
| [Project Structure](project-structure.md) | Repository layout and artifact policy |
| [Stagehand](stagehand.md) | Practical Stagehand integration path |
| [Changelog](changelog.md) | Version history |

---

## At a glance

```
Run 1:  LLM agent discovers the path   →  $0.0065 · 1,985 tokens · 3.05s
         TERX silently records CDP commands

Run 2:  TERX replays                   →  $0.0000 · 0 tokens · 0.090s
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

Run local proof demos:

```bash
terx demo
terx eval-local
```

---

## Repository

- **Source**: [github.com/ixchio/terx](https://github.com/ixchio/terx)
- **Issues**: [github.com/ixchio/terx/issues](https://github.com/ixchio/terx/issues)
- **License**: MIT
