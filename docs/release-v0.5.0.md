# TERX v0.5.0 release

Released 2026-10-01.

TERX v0.5 turns an already approved semantic browser workflow into a local,
named MCP tool. This is not a generic agent builder: every call either finds a
matching policy-bound replay, rechecks its evidence, and returns declared live
page data—or refuses.

## What shipped

- `browser_tool_save`, `browser_tool_list`, `browser_tool_run`, and
  `browser_tool_delete` for local MCP tool manifests.
- Exact string input schemas and fresh output fields limited to visible CSS
  selector text, page title, page URL, and non-sensitive echoed inputs.
- Per-run variable values and optional fresh result readers in `TerxWorkflow`.
- SQLite schema v4 manifest storage. It retains a scope digest and input names,
  never raw scope identifiers, input values, or result payloads.
- A real headless-Chrome local golden path for a saved
  `check_order_status(account_id, order_id)` tool. It reconnects the browser
  CDP session, changes both inputs, returns the new status, exercises a slow
  page load, and refuses an ambiguous target.
- A [reproducible 20-second demo](assets/terx-v0.5-saved-tool-demo.mp4) built
  from the unaltered frames of that local golden path. Run
  `python3 examples/capture_saved_tool_demo.py --output /tmp/terx-demo-frames`
  followed by `bash docs/assets/render-v0.5-demo.sh /tmp/terx-demo-frames` to
  reproduce it.

## Cost and benchmark boundary

`ReplayCostLedger` no longer infers LLM savings from action count. A TERX replay
reports the model calls made by TERX itself (`0`); provider tokens, calling
assistant cost, and browser cost require host usage records. The included local
evaluation is a fixture proof, not a Browser Use, Stagehand, or production-site
comparison. See [benchmark protocol](benchmarks.md).

One separate Browser Use Cloud v4 smoke run was measured using `gpt-6-luna`,
low reasoning, a US browser, and a `$0.25` cap. It returned the expected final
host (`www.iana.org`) in 16.017 seconds, consuming 48,492 input tokens and 218
output tokens for `$0.002516`. It is a real provider record, but a different
public task—not a TERX-versus-Browser-Use performance claim.

The release notes also include a separate native Browser Use `rerun_history`
smoke record: after a Chrome restart it completed its stored public-page actions
in 4.032 seconds, but consumed 1,033 Groq tokens for the rerun summary. TERX
does not use that fact to claim superiority; it documents the architectural
difference between a framework rerun and TERX's constrained replay layer.

## Deliberate limits

- Chromium/CDP only; no cross-browser or cloud browser fleet.
- No proxy/stealth/CAPTCHA stack, credential vault, visual builder, or hosted
  operations layer.
- Saved tools share the local TERX cache/server. They are not a hosted registry.
- Browser Use remains experimental: normal Browser Use action traffic is not
  safely captured by TERX's semantic recorder yet.
- There is no automatic replay self-healing. Refusal is the intended safety
  outcome when the contract no longer matches.

## Verification performed

```bash
ruff check .
pytest tests/ -q
python3 -m terx.evals.local_suite
python3 -m build
python3 -m twine check dist/*
```

The release workflow reruns this suite under Python 3.11, 3.12, and 3.13.
