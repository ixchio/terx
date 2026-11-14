# Project Structure

TERX keeps runtime library code, demos, docs, and generated artifacts separated
so the repository stays easy to audit.

```text
terx/
  cdp/             Raw Chrome DevTools Protocol bridge and browser sessions
  dom/             Accessibility-tree extraction and structural hashing
  cache/           Scoped replay, split by responsibility:
    storage.py       SQLite sequences, saved tools, keyed scope digests, audit files
    policy.py        Replay contract, replayable URLs, approval request/decision
    recording.py     RecordingContext / session_for: record, store, replay
    actions.py       Semantic actions (navigate, click, type) over CDP
    conditions.py    Declarative pre/postconditions and waits
    redaction.py     Placeholders, variables, secret redaction
    guard.py         Per-step DOM mutation guard
    models.py        Persisted actions, sequences, reports
    errors.py        Replay exceptions
    cache.py         Compatibility re-exports for `terx.cache.cache` (1.x)
  server/          MCP server and browser tools
  integrations/    Optional adapters; workflow.py is dependency-free
  evals/           Deterministic local eval suites
  tools.py         Saved tool manifests and live result extraction

examples/          Small integration examples
docs/              Public documentation and site assets
tests/             Unit and integration tests
```

## Keep Out of Git

These paths are local runtime output and must stay ignored:

```text
.terx/
.jsonl/
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
