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

## Promote a recorded task to an MCP tool

v0.5 adds a small shared tool registry to the MCP server. It is designed for
the path that matters in practice: one agent completes a supported task, a
reviewer names its inputs and outcome, and a later MCP client calls that same
approved workflow with new values.

First record the task normally and finish it successfully. Then save a
manifest. The raw `scope_id` is used only to make a digest; it is supplied and
checked again on every invocation.

```text
browser_tool_save(
  name="check_order_status",
  description="Read a current order status from the vendor portal.",
  task="check order status in vendor portal",
  input_names=["order_id"],
  scope_id="acme-prod:vendor-portal:orders",
  precondition={"url_contains": "/orders"},
  postcondition={"selector_exists": "[data-order-status]"},
  route_pattern="/orders",
  workflow_version=1,
  side_effect="read_only",
  result_spec={
    "order_id": {"source": "input", "name": "order_id"},
    "status": {"source": "text", "selector": "[data-order-status]"},
    "page_url": {"source": "url"}
  }
)
```

Use `browser_tool_list()` to discover the input and result schemas, then call
it through `browser_tool_run(name, inputs, scope_id)`. The run never starts an
agent on a miss: it replays a matching approved semantic workflow and returns
fresh, declared page output, or returns a refusal. `browser_tool_delete(name)`
removes the manifest without deleting the historical replay record.

The result schema is deliberately limited to visible text from a CSS selector,
the page title, the page URL, and a non-sensitive echoed input. There is no
arbitrary JavaScript result extractor, form-value reading, or persisted result
payload. A tool manifest is local SQLite data shared by MCP clients that use
the same TERX server/cache; it is not a cloud registry.

### Fresh results from the Python adapter

`TerxWorkflow.run()` accepts per-run values for variables declared at
construction and an optional host-owned `result_reader`. The reader runs after
both a cold success and a warm replay, and its return value is not persisted:

```python
async def read_status(bridge):
    # Use your application's read-only page query here.
    return {"status": await read_visible_status(bridge)}

result = await workflow.run(
    cold_path,
    variables={"order_id": "A-101"},
    result_reader=read_status,
)
assert result.value == {"status": "Delivered"}
```

The host reader is intentionally outside TERX's replay IR. Do not use it for
mutations; use the MCP result schema above when a constrained, declarative
reader is sufficient.

## Browser Use and Stagehand

The Browser Use adapter remains experimental because normal Browser Use runs do
not expose the exact TERX CDP bridge required for safe semantic capture. The
Stagehand guide describes an MCP workflow, not a native adapter. Do not present
either as a drop-in integration until it has the same explicit policy and
replay guarantees as `TerxWorkflow`.
