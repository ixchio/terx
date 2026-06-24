# Project Structure

TERX keeps runtime library code, demos, docs, and generated artifacts separated
so the repository stays easy to audit.

```text
terx/
  cdp/             Raw Chrome DevTools Protocol bridge and browser sessions
  dom/             Accessibility-tree extraction and structural hashing
  cache/           Recording, replay, redaction, reports, and drift guards
  server/          MCP server and browser tools
  agent/           Optional self-healing helpers
  integrations/    Third-party adapter surfaces
  evals/           Deterministic local eval suites
  benchmarks/      Baseline and real LLM benchmark runners
  vision/          Optional visual audit helpers

examples/          Small integration examples
docs/              Public documentation and site assets
tests/             Unit and integration tests
```

## Keep Out of Git

These paths are local runtime output and must stay ignored:

```text
.terx/
.vcr/
dist/
build/
.venv/
.pytest_cache/
.ruff_cache/
.mypy_cache/
__pycache__/
```

`docs/assets/terx-demo.gif` is the only committed demo animation. Old generated
assets and one-off bug reports should be moved into docs or deleted instead of
living at the repository root.
