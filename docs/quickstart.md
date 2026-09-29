# Quick start

TERX v0.4 caches only explicit, policy-bound semantic workflows. Before using
it, decide what caller scope can run the workflow, what page state proves it is
safe to start, and what outcome proves success.

## Install and start Chrome

```bash
pip install terx
google-chrome --remote-debugging-port=9222 --no-first-run \
  --user-data-dir=/tmp/terx-chrome
```

Use a dedicated Chrome profile. A remote-debugging port controls that profile.

## Python

```python
from terx.cache.cache import MemoryCache, session_for
from terx.cdp.session import BrowserSession


async def run_login():
    cache = MemoryCache()
    async with BrowserSession() as session:
        async with session_for(
            cache,
            session.bridge(),
            "log in to dashboard",
            scope_id="test:dashboard:service-account",
            route_pattern="/login",
            workflow_version=1,
            side_effect="mutating",
            variables={"email": "bot@example.test", "password": "from-secret-store"},
            precondition={"url_contains": "/login"},
            postcondition={"text_contains": "Dashboard"},
        ) as ctx:
            if ctx.hit:
                await ctx.replay()
            else:
                await agent.drive(session.bridge())

        return ctx.report
```

`scope_id`, a non-empty `precondition`, and a non-empty `postcondition` are
required for a context to enter or read the replay cache. A run lacking the
contract can still use normal browser tools; it finishes with
`report.status == "refused"` and is not cached.

Use only `url_contains`, `title_contains`, `text_contains`, and
`selector_exists` with non-empty string values in cacheable conditions. Changing a route, condition,
side-effect class, or other contract input requires a new `workflow_version`.

`TERX.type` is cacheable only for a named value supplied through `variables`.
That value is stored as `{{name}}`; direct free text is intentionally not
persisted for replay.

For destructive actions, pass a host verifier that atomically validates and
consumes an approval bound to TERX's replay identity:

```python
from terx import ApprovalDecision

async def verify_and_consume(request):
    verdict = await control_plane.consume_browser_approval(
        request.token,
        request.scope_hash,
        request.policy_fingerprint,
        request.structural_hash,
        request.command_digest,
    )
    return ApprovalDecision(verdict.approved, verdict.consumed, verdict.reason)

# session_for(..., approval_verifier=verify_and_consume)
await ctx.replay(approval_token=approval_from_your_control_plane)
```

Without a token and a verifier decision with both `approved=True` and
`consumed=True`, TERX refuses before any replay action runs.

## MCP server

```bash
terx-server
```

Add the server to an MCP client:

```json
{
  "mcpServers": {
    "terx": { "command": "terx-server" }
  }
}
```

Then use the task wrapper around supported browser actions:

```text
browser_task_start(
  task="log in to dashboard",
  scope_id="test:dashboard:service-account",
  precondition={"url_contains": "/login"},
  postcondition={"text_contains": "Dashboard"},
  variables={"email": "bot@example.test", "password": "..."}
)
browser_type(...)
browser_click(...)
browser_task_finish(success=true)
```

For `side_effect="destructive"`, `replay_approval` is only an opaque token.
The embedding application must construct `TERXServer` with an approval verifier
that consumes it; the standard `terx-server` command fails closed. Check each
result's `report`: it distinguishes `hit`, `miss`, `refused`, and `failed`.

Generate that exact config without installing anything into the client:

```bash
terx mcp-config --client cursor
terx mcp-config --client codex
```

## Verify locally

```bash
pytest tests/ -v
ruff check .
terx eval-local
```

The local eval starts a temporary local web page and headless Chrome. It proves
the supported semantic path only; it is not a claim that TERX can replay every
website or browser agent.
