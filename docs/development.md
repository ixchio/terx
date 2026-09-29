# TERX developer guide

## Local setup

```bash
git clone https://github.com/ixchio/terx.git
cd terx
python3 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
pytest tests/ -v
ruff format --check .
ruff check .
python -m terx.evals.local_suite
```

The local eval launches its own temporary headless Chrome. For manual MCP work,
start a dedicated Chrome profile with `--remote-debugging-port=9222`.

## Replay architecture

```text
agent or MCP client
        |
        v
RecordingContext + ReplayPolicy
        |                    |
 cold path CDP          scoped cache lookup
        |                    |
 semantic action IR <--- hit -> precondition -> replay -> postcondition
        |                                      |
 SQLite + redacted audit                         refusal or report
```

`RecordingContext` observes regular bridge commands only on a cold path and
translates this narrow sequence into a persisted action IR:

| Source CDP gesture | Persisted action |
| --- | --- |
| `Page.navigate` to query-free `http` or `https` | `TERX.navigate` |
| `DOM.resolveNode` plus exact `this.click()` | `TERX.click` |
| labelled `DOM.focus` plus `Input.insertText` | `TERX.type` |

Any raw mouse/keyboard event, coordinate click, or arbitrary
`Runtime.evaluate`/`Runtime.callFunctionOn` marks the cold run non-cacheable.
It is important that a new feature preserves this fail-closed behavior instead
of adding raw CDP serialization.

## Replay policy

`ReplayPolicy` requires a scope ID, precondition dictionary, postcondition
dictionary, positive workflow version, and valid side-effect class. The scope
digest, origin, route pattern, workflow version, and DOM similarity are all
part of matching. The complete policy also has a fingerprint, so a caller
cannot replay a workflow under altered conditions or a looser side-effect
class. Only non-empty URL, title, text, and selector conditions are cacheable; arbitrary
JavaScript conditions remain cold-path-only. A cache entry also has a TTL.

Replay re-resolves each target from the live accessibility tree using exact
role plus accessible name. Zero or multiple matches cause `ReplayRefused`.
There is no automatic LLM fallback. A `destructive` replay requires a host
`approval_verifier` to return an `ApprovalDecision` with both `approved` and
`consumed` true for each hit. The verifier must atomically consume the opaque
token and bind it to the request's task, scope hash, version, policy
fingerprint, selected structural hash, and semantic command digest.

## Data boundaries

Keep variable values out of every persisted representation. `TERX.type` must
contain a `{{placeholder}}`; cached result payloads are always empty. Both the
SQLite cache and JSONL audit writer call the same storage sanitizer. Do not add
a diagnostic log or an adapter that serializes raw params/results without a
redaction review.

The experimental `SelfHealer` is manual-only and opt-in. It must never be
called from the normal replay path.

## Test requirements

Any cache or integration change needs unit coverage for the intended replay and
the failure mode. At minimum, preserve coverage for:

- scope mismatch and TTL miss;
- failed precondition and failed postcondition;
- destructive replay without an approval verifier, denied approval, and reused approval;
- missing/ambiguous semantic target;
- no plaintext typed value or raw JavaScript in cache/audit;
- a headless Chrome cold-to-warm flow in `terx.evals.local_suite`.

`pytest` and Ruff validate code shape. The local eval validates the supported
browser path; neither proves compatibility with production sites.
