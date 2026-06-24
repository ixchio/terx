# Quick Start

Get TERX running against a local Chrome session in a few minutes.

## Install

```bash
pip install terx
```

For local development:

```bash
git clone https://github.com/ixchio/terx
cd terx
pip install -e ".[dev]"
```

## Start Chrome

```bash
google-chrome --remote-debugging-port=9222 --no-first-run
```

If you already have Chrome open, use a separate profile to avoid mixing personal
tabs with automated runs:

```bash
google-chrome --remote-debugging-port=9222 --user-data-dir=/tmp/terx-chrome --no-first-run
```

## Use the MCP Server

Start TERX:

```bash
terx-server
```

Add it to your MCP client:

```json
{
  "mcpServers": {
    "terx": {
      "command": "terx-server"
    }
  }
}
```

For cacheable workflows, tell the agent to wrap the browser actions:

1. Call `browser_task_start("short task description", variables={...}, postcondition={...})`.
2. If it returns `cache_hit: true`, TERX already replayed the workflow.
3. If it returns `cache_hit: false`, complete the workflow with `browser_*` tools.
4. Call `browser_task_finish(success=true)` after the workflow succeeds.

Example variables and postcondition:

```json
{
  "variables": {
    "email": "user@example.com",
    "password": "correct-horse-battery-staple"
  },
  "postcondition": {
    "text_contains": "Welcome"
  }
}
```

## Use the Python API

```python
import asyncio

from terx.cache.cache import MemoryCache, session_for
from terx.cdp.session import BrowserSession


async def main():
    cache = MemoryCache()

    async with BrowserSession() as session:
        bridge = session.bridge()

        async with session_for(
            cache,
            bridge,
            "login to dashboard",
            variables={"email": "user@example.com", "password": "..."},
            postcondition={"text_contains": "Welcome"},
        ) as ctx:
            if ctx.hit:
                await ctx.replay()
                return

            await bridge.send("Page.navigate", {"url": "https://example.com/login"})
            await bridge.wait_for_load()
            # Run your browser agent here. TERX records mutating CDP commands.


asyncio.run(main())
```

## Verify the Install

```bash
terx doctor
pytest tests/ -v
ruff check .
terx demo
terx eval-local
```

`terx demo` starts a local page and headless Chrome, records a login flow once,
then replays it with different variable values. `terx eval-local` runs three
deterministic replay cases and prints JSON metrics.

## Inspect Cache State

```bash
terx stats
terx inspect
terx purge 127.0.0.1:8897
```

The inspect command reports task descriptions, command counts, hit counts, and
redacted placeholder names without printing secret values.

## Security Notes

TERX stores replayable browser commands and audit JSONL locally under `.terx/`.
Treat that directory as sensitive if your workflows type passwords, tokens, or
customer data. It is ignored by this repository's `.gitignore`.

When `variables` are supplied, matching typed values are stored as placeholders
such as `{{email}}`. Password/token/API-key fields are redacted by default even
when no variable was supplied; replay then requires the matching variable.

Set `TERX_REDACT_ALL_TEXT=1` to force every typed value into a placeholder.
Set `TERX_REDACT_FIELDS=tenant,workspace` to extend the sensitive-label policy.
