# TERX + Stagehand

Stagehand and TERX solve adjacent problems:

- Stagehand gives browser agents high-level primitives like `act`, `extract`, `observe`, and `agent`.
- TERX records successful browser workflows and replays the CDP commands with variables, redaction, and postconditions.

## Recommended Path Today

Use TERX as the memory layer around repeated Stagehand workflows when your automation stack can route actions through the same Chrome DevTools target:

1. Start Chrome with remote debugging enabled.
2. Start `terx-server` and connect it to that Chrome instance.
3. Wrap repeated workflows with `browser_task_start` and `browser_task_finish`.
4. Keep task descriptions stable and pass variables for user-specific values.

Example MCP flow:

```json
{"tool": "browser_task_start", "arguments": {
  "task": "log in to billing dashboard",
  "variables": {"email": "owner@example.com", "password": "secret"},
  "postcondition": {"text_contains": "Dashboard"}
}}
```

Then run the Stagehand or agent actions that perform the workflow. Finish with:

```json
{"tool": "browser_task_finish", "arguments": {"success": true}}
```

On the next matching DOM/task, TERX can replay the cached CDP command sequence and return a replay report.

## What TERX Does Not Fake

TERX does not currently ship a native Stagehand adapter that monkey-patches Stagehand internals. That would be fragile unless the adapter owns a documented shared-browser contract.

The production-grade native adapter should:

- Accept an existing Stagehand page/browser session.
- Start a TERX recording context before `act`/`agent` workflows.
- Capture only mutating browser actions, not observations or extraction calls.
- Pass `variables`, `postcondition`, `mutation_guard`, and `redact_secrets` through to TERX.
- Return the TERX `ReplayReport` beside the Stagehand result.

Until that adapter exists, MCP wrapping is the safer integration story.
