# Threat model

This document states what TERX protects, against whom, and where its
protection ends. It covers the Python library, the workflow adapter, and the
`terx-server` MCP server. Report vulnerabilities as described in
[SECURITY.md](../SECURITY.md).

## What TERX is

TERX records a browser workflow once and later replays it over the Chrome
DevTools Protocol (CDP) without a model call. A replay runs only when the
caller's scope, the starting page, the policy contract, and the recorded
semantic actions all match, and it must prove its outcome afterwards. When any
check fails, TERX refuses and returns the decision to its host.

TERX is a **replay gate, not a sandbox**. It narrows what a *replay* may do. It
does not limit what a live agent can do with the browser on the cold path.

## Assets

| Asset | Where it lives |
| --- | --- |
| The browser profile's signed-in sessions | Chrome, reachable by anyone who holds the CDP connection |
| Recorded workflows | `.terx/cache.db` (`sequences` table) |
| Saved tool manifests | `.terx/cache.db` (`saved_tools` table) |
| The scope key | `.terx/scope.key` (mode 0600) |
| Audit records | `.terx/audit/*.jsonl` |
| Variable values (passwords, IDs) | Process memory only, supplied per run |
| Approval tokens | Supplied per replay; verified and consumed by the host |

## Actors

- **Host application:** the code embedding TERX. Trusted.
- **Agent:** an LLM agent driving the browser, directly or through MCP. Treated
  as fallible and possibly prompt-injected by page content.
- **Other MCP clients:** separate agents connected to the same `terx-server`
  and cache. Not trusted with each other's scopes.
- **Websites:** the pages being automated. Their content is untrusted input.
- **Local users:** anyone with read or write access to `.terx/` or the CDP port.

## Trust boundaries

1. **Host ↔ TERX:** in-process; the host is trusted.
2. **MCP client ↔ `terx-server`:** stdio JSON-RPC. Clients are not
   authenticated; isolation between them is by scope.
3. **TERX ↔ Chrome:** CDP over a local WebSocket with the profile's full
   authority.
4. **Chrome ↔ websites:** page DOM, text, and script are untrusted.
5. **TERX ↔ disk:** `.terx/` is trusted application data.

## Threats and mitigations

### T1. Replaying a workflow in the wrong context

*Example:* a workflow recorded for tenant A runs while signed in as tenant B.

- **Mitigations:** every cache row is keyed by an HMAC scope digest, domain,
  origin, route pattern, workflow version, and policy fingerprint. A
  precondition is checked before replay, and the whole sequence is validated
  before any action runs.
- **Residual risk:** a scope ID is asserted by the caller, not authenticated.
  The precondition is the only check of what the page actually shows, so it
  must be specific to the account or environment.

### T2. The page changed and replay acts on the wrong element

- **Mitigations:** targets must match an exact accessible role and name (in
  the recorded iframe), and more than one match is refused. When a test id was
  recorded, a name match whose element now has a different test id is refused
  as a signal conflict. A changed name is tolerated only when the test id and
  the element's position among same-role elements both still agree, and the
  report lists it in `locator_notes`. A missing target is waited for and then
  refused. The DOM mutation guard is checked after every step and stops the
  sequence. A postcondition must hold at the end, or the replay reports
  `failed`. `terx eval-hard` checks five must-refuse pages in CI.
- **Residual risk:** an element with the same role, name, and test id but a
  different meaning (for example a "Delete" button now attached to a different
  record) is indistinguishable. Each step's own side effects happen before the
  guard reads them.

### T3. A destructive action runs without consent

- **Mitigations:** `side_effect="destructive"` replays need an approval token,
  and a host verifier must return a decision that is both approved and
  consumed. The request binds task, scope, policy fingerprint, structural hash,
  and a digest of the exact command sequence. Missing variables and malformed
  actions are refused before the approval is consumed. The standalone
  `terx-server` has no verifier, so it refuses every destructive hit.
- **Residual risk:** correctness depends on the host's verifier. A target on a
  later page that never appears still refuses after earlier steps ran.

### T4. Secrets are written to disk

- **Mitigations:** typed values become `{{placeholders}}`, and text without a
  named variable is not cacheable. Password, token, key, and similar fields are
  redacted at every persistence boundary. CDP responses are never stored, and
  TERX captures no screenshots. Scope IDs are stored only as keyed digests.
- **Residual risk:** a path segment equal to a declared variable is stored as a
  `{{placeholder}}`, but a secret in a URL path that was *not* supplied as a
  variable (`/reset/<token>`) is stored in plain text. Pass such tokens as
  variables. Task descriptions, labels, and condition summaries written by the
  caller are stored as given.

### T5. A prompt-injected agent misuses the browser

- **Mitigations:** MCP navigation allows only `http`, `https`, and
  `about:blank`, which blocks `chrome:`, `devtools:`, extension, `file:`,
  `data:`, and `javascript:` pages. Screenshots are returned by reference
  rather than inline.
- **Residual risk:** on the cold path the agent can click, type, and navigate
  anywhere on the web with the profile's sessions. Use a dedicated browser
  profile signed in only to what the agent needs.

### T6. One MCP client interferes with another

- **Mitigations:** replacing or deleting a saved tool, and invalidating cached
  workflows, require the target's own scope ID. A tool can be saved only when a
  matching live workflow exists. Task start, finish, and saved-tool runs refuse
  concurrent calls.
- **Residual risk:** the first client to save a tool writes its description,
  which other agents read; treat descriptions as untrusted text. Any client
  that knows a scope ID can act in that scope.

### T7. The cache is tampered with on disk

- **Mitigations:** replay executes only the semantic action set, with URL and
  target checks, whatever `commands_json` contains. Destructive approvals bind
  the command digest, so a verifier sees a changed sequence.
- **Residual risk:** write access to `.terx/` lets an attacker change
  non-destructive workflows and saved tools. Workflows are not signed yet.

### T8. A hostile page fakes evidence

- **Mitigations:** none beyond refusing ambiguity. TERX assumes the site being
  automated is the site the user intends to use.
- **Residual risk:** page script can relabel elements or render text that
  satisfies a `text_contains` check. Conditions are evidence, not proof.

### T9. The CDP port is exposed

- **Mitigations:** documentation only. TERX connects to `localhost` by default.
- **Residual risk:** anyone who can reach the debugging port controls the
  browser. Never bind it to a non-loopback interface.

### T10. Resource exhaustion and unsafe file paths

- **Mitigations:** the CDP event queue is bounded, and every CDP command and
  replay step has a timeout. Audit file names are sanitized and never
  overwrite an earlier record.

## Out of scope

- A compromised host process, Python environment, or Chrome binary.
- Isolation between operating-system users who share `.terx/` or a CDP port.
- Websites that deliberately detect and sabotage automation.
- Guarantees about the outcome of a cold-path agent run, beyond the
  postcondition check before it is cached.

## Known gaps and planned work

- Secrets in URL paths that are not variables (T4): detection of token-like
  segments.
- Unsigned workflows (T7): signed, reviewable workflow files.
- Unauthenticated MCP clients (T6): per-client credentials and capabilities.
- Cross-site iframes: elements in an out-of-process frame are not visible to
  TERX, so targets there refuse.
