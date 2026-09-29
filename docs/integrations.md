# Integrations

TERX is a replay gate, not another browser runtime. Integrations use the
existing Chrome DevTools Protocol connection and must not add a second browser,
resident agent process, cloud service, or framework dependency merely to cache
three semantic actions.

## MCP clients

TERX speaks MCP over stdio. Print a client-ready snippet and paste it into the
client's MCP configuration:

```bash
terx mcp-config --client cursor
terx mcp-config --client claude-desktop
terx mcp-config --client windsurf
terx mcp-config --client codex
```

The command is deliberately read-only: it prints configuration and never edits
an MCP client file. `terx-server` starts only when the MCP client invokes it.

## Python semantic workflow adapter

`TerxWorkflow` is the supported SDK integration for applications that already
use a TERX `CDPBridge`. It is framework-free and has no idle state: during a
run it uses the existing bridge, short-lived accessibility snapshots, and the
existing local SQLite database.

Navigate the browser to the proven starting state before opening the workflow.
That navigation belongs to the host application's setup; the workflow's
precondition must verify the state that is actually replayable.

```python
from terx.cache.cache import MemoryCache
from terx.cdp.session import BrowserSession
from terx.integrations.workflow import TerxWorkflow


async def run_login():
    cache = MemoryCache()
    async with BrowserSession() as session:
        bridge = session.bridge()
        await bridge.send_internal(
            "Page.navigate", {"url": "https://billing.example.test/login"}
        )
        await bridge.wait_for_load()

        workflow = TerxWorkflow(
            cache=cache,
            bridge=bridge,
            task="sign in to billing",
            scope_id="acme-prod:billing-service-account",
            route_pattern="/login",
            workflow_version=1,
            variables={
                "email": "bot@acme.test",
                "password": "from-secret-store",
            },
            precondition={"url_contains": "/login"},
            postcondition={"text_contains": "Billing overview"},
        )

        async def cold_path(browser):
            await browser.type_into("textbox", "Email", "email")
            await browser.type_into("textbox", "Password", "password")
            await browser.click("button", "Sign in")
            await browser.wait_for({"text_contains": "Billing overview"})

        result = await workflow.run(cold_path)
        return result.report
```

On the first successful run, TERX stores only semantic actions. On a matching
warm run, it skips `cold_path` and replays those actions using the same CDP
bridge. Literal values are sent to Chrome only on the cold path and are stored
as named placeholders such as `{{password}}`.

### Contract

`TerxActions` intentionally has only these mutating methods:

- `navigate(url)` for credential-, query-, and fragment-free HTTP(S) URLs;
- `click(role, label)` when exactly one accessibility-tree target matches;
- `type_into(role, label, variable)` for a declared lower-snake-case variable.

`wait_for(condition)` is available for safe URL, title, text, or selector
checks. It does not become a replay action.

Do not mix direct mutating `bridge.send(...)` calls with the adapter. Those
actions are outside its proof boundary and may cause TERX to refuse caching.

For `side_effect="destructive"`, configure a host verifier that atomically
consumes an approval token bound to the task, scope hash, workflow version, and
policy fingerprint, selected DOM structural hash, and semantic command digest.
TERX accepts only an `ApprovalDecision` whose `approved` and `consumed` values
are both true:

```python
from terx import ApprovalDecision

async def verify_and_consume(request):
    verdict = await control_plane.consume_browser_approval(request)
    return ApprovalDecision(verdict.approved, verdict.consumed, verdict.reason)

workflow = TerxWorkflow(..., side_effect="destructive", approval_verifier=verify_and_consume)
result = await workflow.run(cold_path, approval_token=approval_from_control_plane)
```

The standalone MCP server deliberately has no verifier and therefore refuses
destructive cache hits. An application that embeds `TERXServer` must provide
the same `approval_verifier` contract.

## Browser Use and Stagehand

The Browser Use adapter remains experimental because normal Browser Use runs do
not expose the exact TERX CDP bridge required for safe semantic capture. The
Stagehand guide describes an MCP workflow, not a native adapter. Do not present
either as a drop-in integration until it has the same explicit policy and
replay guarantees as `TerxWorkflow`.
