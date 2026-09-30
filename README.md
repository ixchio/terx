# TERX

**Local, policy-enforced replay for approved browser-agent workflows.**

TERX records a small, semantic workflow after a browser agent succeeds, then
replays it through Chrome DevTools Protocol without a TERX model call. It is
for repeatable workflows whose starting state, caller scope, and successful
outcome can be stated explicitly.

It is not a general browser automation framework, an autonomous fallback
agent, or a safe way to replay arbitrary JavaScript.

## When TERX earns its place

Use TERX when an agent has already completed a browser workflow and you want
the next run to be cheap, local, and constrained by evidence—not rediscovered
by another model call. It is a fit for repeatable internal tasks such as
logging into a known account, searching an operations dashboard, or filing an
approved action behind a control-plane approval.

On a cache hit, TERX re-checks the caller scope and starting conditions,
re-resolves every accessible target, executes only the semantic action set
below, then verifies the intended outcome. If any check is ambiguous or
false, it refuses instead of guessing.

## Replay contract

Every cacheable workflow must declare:

- a `scope_id` that binds it to a tenant, account, environment, or fixture;
- a `precondition` checked before replay;
- a `postcondition` checked after the first run and every replay;
- a route, workflow version, TTL, and side-effect class.

Conditions support `url_contains`, `title_contains`, `text_contains`, and
`selector_exists`; every value must be a non-empty string. Their full values participate in a policy fingerprint but
are not written to the cache. Change any part of a workflow contract and bump
`workflow_version` to intentionally create a new replay entry.

TERX persists only three semantic actions:

- `TERX.navigate` — an `http` or `https` origin-and-path navigation (no query,
  fragment, or embedded credential);
- `TERX.click` — an exact accessible role and label;
- `TERX.type` — an exact labelled input and a named value placeholder.

Raw CDP commands, coordinate clicks, key events, and arbitrary JavaScript still
run on the cold path but make the workflow non-cacheable. Ambiguous targets,
failed conditions, missing variables, expiry, and unapproved destructive work
produce a structured refusal; TERX does not silently invoke an LLM.

## Why it stays lightweight

TERX reuses the Chrome CDP connection your application already owns. Its core
runtime is an async CDP bridge, an accessibility-tree snapshot, and local
SQLite—no Playwright or Selenium runtime, browser fleet, background service,
or cloud account. The workflow adapter holds an accessibility snapshot only
while a cold path is running and discards it before the next workflow.

## Install

```bash
pip install terx
```

TERX needs a local Chrome or Chromium instance with remote debugging enabled:

```bash
google-chrome --remote-debugging-port=9222 --no-first-run \
  --user-data-dir=/tmp/terx-chrome
```

## Python quickstart

```python
from terx.cache.cache import MemoryCache, session_for
from terx.cdp.session import BrowserSession

cache = MemoryCache()

async with BrowserSession() as session:
    bridge = session.bridge()
    async with session_for(
        cache,
        bridge,
        "sign in to the billing dashboard",
        scope_id="acme-prod:billing-service-account",
        route_pattern="/login",
        workflow_version=1,
        side_effect="mutating",
        variables={"email": "bot@acme.test", "password": "from-secret-store"},
        precondition={"url_contains": "/login"},
        postcondition={"text_contains": "Billing overview"},
    ) as replay:
        if replay.hit:
            await replay.replay()
        else:
            # Drive bridge.send(...) from your agent here. Only labelled clicks
            # and named-variable text entry become replayable actions.
            await your_agent.run()

    print(replay.report.as_dict())
```

For a destructive workflow, require a host approval verifier that atomically
checks and consumes a fresh approval for this exact replay identity:

```python
from terx import ApprovalDecision

async def verify_and_consume(request):
    verdict = await control_plane.consume_browser_approval(
        token=request.token,
        scope_hash=request.scope_hash,
        task=request.task_description,
        workflow_version=request.workflow_version,
        policy_fingerprint=request.policy_fingerprint,
        structural_hash=request.structural_hash,
        command_digest=request.command_digest,
    )
    return ApprovalDecision(verdict.approved, verdict.consumed, verdict.reason)

# Pass approval_verifier=verify_and_consume when creating session_for(...).
await replay.replay(approval_token=approval_from_your_control_plane)
```

Without both a token and a verifier result with `approved=True` and
`consumed=True`, TERX refuses before running any replay action. The standalone
`terx-server` has no verifier by default and therefore fails closed for
destructive cache hits.

`ReplayReport.status` is one of `miss`, `hit`, `refused`, or `failed`. Treat a
refusal as a signal to run an approved cold-path agent flow or ask for input;
do not assume TERX completed the task.

## MCP

```bash
terx-server
```

```json
{
  "mcpServers": {
    "terx": { "command": "terx-server" }
  }
}
```

Start a cacheable task with a complete contract, run normal `browser_*` tools
on a miss, then finish it:

```text
browser_task_start(
  task="sign in to billing",
  scope_id="acme-prod:billing-service-account",
  precondition={"url_contains": "/login"},
  postcondition={"text_contains": "Billing overview"},
  variables={"email": "bot@acme.test", "password": "..."}
)
browser_task_finish(success=true)
```

For `side_effect="destructive"`, `replay_approval` is only an opaque token.
The application embedding `TERXServer` must configure a consume-once
`approval_verifier`; the standalone server refuses destructive cache hits by
default. The MCP server returns a refusal reason when it declines to act.

Print the local configuration for a client; this command only writes to stdout
and does not install a daemon or change client settings:

```bash
terx mcp-config --client cursor
terx mcp-config --client codex
```

## Lightweight application adapter

For code that already has a Chrome CDP bridge, use TERX's dependency-free
workflow adapter. It adds no Playwright/Selenium runtime, browser process, or
background worker. The cold path exposes only the three actions TERX can prove
and replay:

```python
from terx.integrations.workflow import TerxWorkflow

workflow = TerxWorkflow(
    cache=cache,
    bridge=bridge,
    task="sign in to billing",
    scope_id="acme-prod:billing-service-account",
    variables={"email": "bot@acme.test", "password": "from-secret-store"},
    precondition={"url_contains": "/login"},
    postcondition={"text_contains": "Billing overview"},
)

async def sign_in(browser):
    await browser.type_into("textbox", "Email", "email")
    await browser.type_into("textbox", "Password", "password")
    await browser.click("button", "Sign in")
    await browser.wait_for({"text_contains": "Billing overview"})

result = await workflow.run(sign_in)
```

On a warm hit, `sign_in` is never called. Direct mutating bridge calls are
intentionally excluded: use the adapter's semantic actions or TERX refuses to
call it a replayable workflow. See [integration guidance](https://github.com/ixchio/terx/blob/main/docs/integrations.md).

## v0.5: save a task as a tool

The MCP server can persist a reviewed workflow as a named, discoverable tool.
The manifest contains the task contract, input names, result schema, and a
scope digest—never input values or the raw scope. On a call, TERX verifies the
caller scope, replays only a matching approved workflow, then reads a small
declared set of **current** page fields. A miss, drift, changed scope, or
missing result field is a refusal; it never launches a hidden cold agent.

```text
# Agent A records a supported workflow, then promotes it.
browser_task_start(...)
browser_type(...)
browser_click(...)
browser_task_finish(success=true)
browser_tool_save(
  name="check_order_status",
  input_names=["order_id"],
  result_spec={
    "order_id": {"source": "input", "name": "order_id"},
    "status": {"source": "text", "selector": "[data-order-status]"}
  },
  ...the reviewed scope and replay policy...
)

# Any MCP client using that TERX server/cache can discover and call it later.
browser_tool_list()
browser_tool_run(name="check_order_status", inputs={"order_id": "A-101"}, scope_id="...")
```

Result fields are intentionally narrow: visible text from a CSS selector, page
title, page URL, or an echoed non-sensitive input. TERX does not save arbitrary
JavaScript or form values as a tool result.

Run the complete local `check_order_status(account_id, order_id)` example:

```bash
python3 examples/saved_tool_order_status.py
```

[Watch the reproducible 20-second demo](docs/assets/terx-v0.5-saved-tool-demo.mp4).
It records the approved local lookup, saves its contract, reconnects Chrome, and
returns a fresh status for a different account and order ID. The video shows
**zero TERX model calls during replay**; it does not claim the caller or browser
is free.

## Security model

- The cache is local SQLite under `.terx/`; protect that directory as
  application data.
- Named typed values are stored as `{{placeholders}}`; typed text without a
  named variable is intentionally not cacheable.
- A saved tool stores only its input names and scope digest. Its live result is
  returned to the current caller and is not written into the replay cache.
- Password, token, key, and similar named fields are redacted at cache and
  audit boundaries. Cached response payloads are not retained.
- Scope IDs are represented by a digest in the cache; scope is verified before
  lookup and replay.
- The experimental `SelfHealer` is disabled by default and is never called by
  a replay. Enabling it can send the supplied diagnostic request to a LiteLLM
  provider.

See [SECURITY.md](https://github.com/ixchio/terx/blob/main/SECURITY.md) for the
full boundary and reporting policy.

## Supported integration surface

Python, the dependency-free workflow adapter, saved MCP tools, and the built-in
MCP server are supported in v0.5. The Browser Use adapter is experimental: it can only record
agents that intentionally drive the TERX CDP bridge supplied to it. A normal
Browser Use session is not a drop-in capture source.

## Verification

```bash
pytest tests/ -v
ruff check .
terx eval-local
```

`terx eval-local` launches a temporary local page and headless Chrome to verify
cold recording and warm semantic replay. It does not establish compatibility
with arbitrary production websites.

## What TERX is not

TERX does not replace Playwright, Stagehand, Browser Use, or hosted browser
automation platforms. Those products provide broader automation, browser fleet,
observability, anti-bot, and agent capabilities. TERX's focused value is a
local replay gate: reuse a known-good, explicitly approved workflow only when
its scope and evidence still match.

## Development

```bash
git clone https://github.com/ixchio/terx.git
cd terx
pip install -e ".[dev]"
pytest tests/ -v
ruff format --check .
ruff check .
python -m terx.evals.local_suite
```

See the [quickstart](https://github.com/ixchio/terx/blob/main/docs/quickstart.md),
[development guide](https://github.com/ixchio/terx/blob/main/docs/development.md),
and [changelog](https://github.com/ixchio/terx/blob/main/docs/changelog.md).
