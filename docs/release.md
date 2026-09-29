# TERX v0.4.0 release

Released 2026-09-29.

TERX v0.4 is a local replay gate for browser-agent workflows that have already
succeeded once. It reuses only a small semantic sequence when the caller scope,
starting state, accessible targets, and outcome proof still match.

## What shipped

- A narrow replay IR: `TERX.navigate`, `TERX.click`, and `TERX.type` only.
  Raw CDP scripts, coordinate input, keyboard events, and arbitrary JavaScript
  stay cold-path-only and are never cached for replay.
- Required replay policy: scope, route, workflow version, side-effect class,
  non-empty precondition, non-empty postcondition, and TTL.
- Exact accessible role-and-name target resolution on every replay. Missing or
  ambiguous targets refuse before an action runs.
- Destructive replay hardening: a host must atomically validate and consume an
  approval for the task, scope, policy, selected DOM structural hash, and
  semantic command digest. A token by itself has no authority.
- A dependency-free `TerxWorkflow` adapter for code that already owns a TERX
  CDP bridge, plus `terx mcp-config` for copy-paste MCP setup.
- Fail-closed standalone MCP behavior for destructive cache hits. Embed
  `TERXServer` with an approval verifier when an application needs that path.

## What did not ship

v0.4 is Chromium/CDP-focused. It does not provide a cross-browser fleet,
stealth or proxy layer, CAPTCHA handling, credential vault, visual workflow
builder, hosted trace UI, or automatic production self-healing. Browser Use is
experimental and Stagehand has no native TERX adapter.

## Verification performed

The release candidate passed:

- 57 unit and integration tests;
- Ruff formatting and static checks;
- the four-workflow local headless-Chrome evaluation, including destructive
  replay with a consume-once approval verifier and the workflow adapter;
- source-distribution and wheel build plus current Twine metadata/README
  validation;
- fresh-wheel installation and import smoke tests; and
- a final Bugbot review with no actionable P1, P2, or P3 findings.

The local evaluation proves TERX's supported replay path on its temporary test
application. It is not a benchmark claim or proof of compatibility with an
arbitrary production website.

## Install and verify

```bash
pip install --upgrade terx==0.4.0
terx eval-local
```

See the [quick start](quickstart.md) for the policy contract and
[integrations](integrations.md) for the adapter and MCP surfaces.

## Release links

- [PyPI 0.4.0](https://pypi.org/project/terx/0.4.0/)
- [GitHub release](https://github.com/ixchio/terx/releases/tag/v0.4.0)
- [Changelog](changelog.md)
