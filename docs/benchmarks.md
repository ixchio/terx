# Benchmark status

The large speedup and token-saving tables published for TERX v0.3 measured a
different product: raw CDP command replay against a local benchmark page. They
are retained in Git history for provenance but are **not v0.4 performance
claims** and must not be used to compare this release with other tools.

v0.4 adds mandatory scope, precondition, postcondition, target uniqueness,
expiry, and destructive-action checks. It also refuses raw mouse, keyboard,
and JavaScript command streams. These protections change both the supported
workload and its timing, so the prior benchmark runner is no longer exposed as
a package command.

## What v0.4 verifies now

```bash
terx eval-local
```

This self-contained suite launches a local web page and headless Chrome. It
records and replays three supported semantic workflows—login, search, and a
destructive invoice approval—with named variables, preconditions,
postconditions, and a consume-once per-hit approval verifier. It reports cold and warm timings for that
machine.

The suite proves only that TERX's supported local semantic replay path works.
It does not measure LLM token savings, provide an independent competitive
comparison, or establish production-site compatibility.

## Before publishing a new benchmark

Publish the exact TERX version and commit, browser version, hardware, workflow
policy, raw results, cold agent model/provider, number of runs, median and
tail latency, hit/refusal rate, and every excluded workflow. Include negative
cases such as scope mismatch, condition failure, ambiguous targets, and denied
destructive replay. A benchmark that hides those refusals measures only speed,
not trustworthy automation.
