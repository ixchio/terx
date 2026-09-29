# Stagehand integration status

TERX v0.4 does not ship a Stagehand adapter. Do not describe TERX as recording
arbitrary Stagehand `act`, `extract`, or `agent` workflows: those calls do not
automatically traverse the TERX CDP bridge and may use unsupported browser
gestures.

An experimental integration can use the TERX MCP server only when the workflow
is explicitly driven through its `browser_*` tools and satisfies the normal
v0.4 contract:

```json
{
  "task": "log in to billing dashboard",
  "scope_id": "test:billing:service-account",
  "precondition": {"url_contains": "/login"},
  "postcondition": {"text_contains": "Dashboard"},
  "variables": {"email": "bot@example.test", "password": "..."}
}
```

That is an MCP workflow using TERX, not a native Stagehand integration. A
production adapter needs a documented shared-browser contract, a typed mapping
to the semantic action IR, policy/approval propagation, and real integration
tests. Until those exist, Stagehand is outside the supported release surface.
