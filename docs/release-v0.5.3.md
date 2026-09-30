# TERX v0.5.3 release

Released 2026-10-01.

This release makes the v0.5 saved-tool release reproducible in GitHub Actions
without relaxing the real-browser test. It pins Ruff to the reviewed `0.15.11`
rule set and MCP to the compatible 1.x server API. It also makes the Chrome
evaluation start deterministically on hosted Linux.

The local evaluator now binds Chrome's CDP server to loopback explicitly,
avoids the small shared-memory mount common in CI, polls CDP readiness instead
of relying on a fixed sleep, and preserves the last Chrome startup stderr if
the browser exits early. The workflow still runs a real headless Chrome replay;
it is not replaced by a mock or skipped on CI.

No replay policy, saved-tool contract, or public compatibility claim changed.
See the [v0.5.0 feature release](release-v0.5.0.md) for the saved-tool
capability, local Chrome proof, and honest Browser Use cost observations.

## Verification

```bash
ruff format --check .
ruff check .
pytest tests/ -q
python3 -m terx.evals.local_suite
```
