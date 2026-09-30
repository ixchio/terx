# Benchmark status

The large speedup and token-saving tables published for TERX v0.3 measured a
different product: raw CDP command replay against a local benchmark page. They
are retained in Git history for provenance but are **not v0.5 performance
claims** and must not be used to compare this release with other tools.

v0.5 adds named tool manifests and fresh result reads, on top of mandatory
scope, precondition, postcondition, target uniqueness, expiry, and
destructive-action checks. It also refuses raw mouse, keyboard, and JavaScript
command streams. These protections change both the supported workload and its
timing, so the prior benchmark runner is no longer exposed as a package command.

## What v0.5 verifies now

```bash
terx eval-local
```

This self-contained suite launches a deliberately slow local web page and
headless Chrome. It records and replays login, search, destructive approval,
and adapter workflows. It also promotes `check_order_status(account_id,
order_id)` to a saved tool, reconnects to a fresh browser CDP session, invokes
it with a different account and order ID, reads the current status, and checks
that an ambiguous target is refused.

On the 2026-10-01 release machine, one run reported five warm hits, a 166.4 ms
cold median, and a 159.2 ms warm median. The order-status run returned its new
live value after the reconnect and made **0 TERX model calls during replay**.
This is a local fixture result, not a production latency claim, a provider-cost
measurement, or a competitor comparison.

The suite proves only that TERX's supported local semantic replay path works.
It does not measure provider billing, establish production-site compatibility,
or establish a competitive winner.

## Observed Browser Use Cloud cost smoke

This is a real, intentionally small Browser Use Cloud v4 run recorded on
2026-10-01. It is **not** a like-for-like TERX comparison and must not be used
to claim that either tool is faster or more reliable.

- Task: open `https://example.com`, click `Learn more`, return the final host.
- Configuration: `gpt-6-luna`, low reasoning, US proxy, recording disabled,
  `$0.25` maximum cost.
- API result: `completed`, result `www.iana.org`, 48,492 input tokens, 218
  output tokens, `$0.002516` total cost; client wall time 16.017 seconds.

The v4 response did not include a separate judged-success boolean, browser RSS,
or cost line items, so none are invented here. The request returned the expected
host. Reproduce only on an account you control and retain the raw run ID and
provider record with any public comparison.

## Observed Browser Use native rerun smoke

Browser Use `0.13.10` was also measured locally with its native
`Agent.rerun_history` API after a Chrome restart. This is the same public
`example.com` task as the Cloud smoke, but it is still **not** the TERX local
order-status task and is not a product winner claim.

- Provider/model: Groq `qwen/qwen3.8-27b`; Browser Use flash mode with vision,
  judging, and planning disabled.
- Cold run: succeeded with `iana.org` in 7.204 seconds; 1,944 prompt tokens,
  106 completion tokens, 2,050 total tokens, and `$0.00113448` reported by
  Browser Use from the provider response.
- Native rerun: no action errors after the browser restart, 4.032 seconds;
  950 prompt tokens, 83 completion tokens, 1,033 total tokens, and
  `$0.000648` reported cost. The non-zero use comes from Browser Use's rerun
  summary, so it must not be labelled a zero-model-call replay.
- Peak local Python-plus-child-Chrome process-tree RSS: 1,447.88 MiB. This is
  an environment measurement, not a Browser Use memory guarantee.

Those are one-run smoke observations, not p50/p95 results. Keep the raw output
and failures with any future benchmark corpus rather than rounding them into a
marketing number.

## Comparative benchmark contract

The next public comparison must run the **same authorized task** in four modes:

1. the original browser agent with its provider usage record;
2. Browser Use's current `rerun_history`/`load_and_rerun` path;
3. the relevant framework's native caching path, configured according to its
   current documentation; and
4. a small conventional script, plus TERX's saved tool.

For every run, publish raw completion, incorrect result, refusal, p50/p95
latency, browser/profile memory, wall-clock model usage from the provider or
framework (input/output/cached tokens and cost where supplied), and every
excluded case. Include changed input, changed account, browser reconnect, slow
load, scope mismatch, condition failure, and ambiguous target. A replayed
command count is not a model-call estimate; TERX deliberately reports only
`model_calls_during_replay=0` for its own replay layer.

Browser Use and Stagehand already have their own reuse mechanisms. The public
comparison must run their current native reuse paths on the same authorized
task, not a configuration snippet or a one-off Cloud smoke. Stagehand's hosted
cache path still needs a Browserbase account and an authorized target before a
real Stagehand cost record can be published.
