# Changelog

All notable changes to the TERX browser memory layer are documented in this file.

---

## [Unreleased]

### Added

- `.github/workflows/publish.yml` publishes a GitHub Release to PyPI, or an
  existing tag when run by hand. It refuses a tag that does not match the
  package version, uploads the files attached to the release (so PyPI gets the
  files whose checksums the release notes list), and authenticates with a
  `PYPI_API_TOKEN` secret or, without one, PyPI trusted publishing.

## [1.0.0] - 2026-11-14

TERX 1.0 commits to a stable public API under
[semantic versioning](versioning.md). It adds three replay actions, URL
templates, same-origin iframes, and multi-signal targets, hardens replay and
the MCP server, and removes experimental code. See the
[release notes](release-v1.0.0.md) for upgrade steps.

### Added

- Three replayable actions: `TERX.check` (set a checkbox, switch, or radio on
  or off without toggling), `TERX.select` (choose a dropdown option by label
  or variable), and `TERX.press` (`Enter`, `Tab`, or `Escape`). They are
  available as `TerxActions.set_checked`, `select_option`, and `press`, and as
  the MCP tools `browser_set_checked`, `browser_select_option`, and
  `browser_press_key`.
- URL templates: a navigation path may hold `{{variable}}` placeholders, and a
  path segment equal to a declared variable's value (3+ characters) is recorded
  as one. Values are percent-encoded as a single segment on replay, and a
  placeholder in the host is never replayable.
- Same-origin iframes: the snapshot includes elements inside same-process
  frames, tagged with their frame path, and every element action takes an
  optional `frame=`. Page identity still uses the main frame only.
- Multi-signal targets: new recordings also store the element's position among
  same-role elements and a `data-testid`-style attribute. A label match whose
  element has a different test id is refused; a changed label is tolerated
  only when the test id and position agree, and is listed in
  `ReplayReport.locator_notes`.
- `terx eval-hard`: 15 scenarios with independent outcome checks, reporting
  replay rate, false refusal rate, and unsafe outcomes. CI runs it with
  `--gate`. Replay rate went from 40% to 100%, false refusals from 16.7% to
  0%, and unsafe outcomes from 1 to 0 (see `docs/benchmarks.md`).
- `TerxWorkflow(replay_timeout=...)`.

### Fixed

- The local and hard evals now let Chrome choose its own DevTools port (read
  from `DevToolsActivePort`) instead of reserving one in advance, and wait up
  to 30 seconds for startup (`TERX_CHROME_STARTUP_TIMEOUT`). A fixed 10-second
  wait made CI fail when a runner started Chrome slowly.
- A variable in a URL path was frozen at record time, so replaying "open order
  B-200" reopened A-100 and still reported success.
- MCP recordings could not identify targets on any page after the first
  navigation; the server now gives the recorder a fresh snapshot before each
  action.
- `RecordingContext.record_command` now validates action parameters, not just
  the method name.

### Changed

- `terx/cache/cache.py` (about 2,000 lines) is split into focused modules:
  `storage`, `policy`, `recording`, `actions`, `conditions`, `redaction`,
  `guard`, `models`, and `errors`. `terx.cache.cache` still re-exports every
  name it defined, and stays importable throughout 1.x; new code should import
  from `terx` or `terx.cache`.
- Click and type logic shared by replay, the workflow adapter, and MCP now
  lives in `terx.cache.actions.click_element` and `replace_field_text`.

### Removed

- The experimental `SelfHealer` (`terx.agent`) and the `healing` extra. TERX no
  longer contains any model client.
- SSIM visual audit (`terx.vision`), `MemoryCache(visual_audit=...)`, and the
  `vision` extra. The SSIM score was only logged and never affected replay.
- The `embeddings` extra, which had no code behind it.
- The retired v0.3 benchmark runners (`terx.benchmarks`); their source remains
  at the `v0.5.3` tag.
- The `MuscleMemorycache` alias; use `MemoryCache`.
- Unused dev dependencies (`pytest-benchmark`, `rich`, `python-dotenv`) and the
  benchmark and LLM entries in `.env.example`.

### Security

- Scope digests are now HMAC-SHA256 with a random local key
  (`.terx/scope.key`, mode 0600, or `MemoryCache(scope_key=...)`). The unkeyed
  SHA-256 digest exposed by `browser_tool_list` and `terx inspect` could be
  reversed by guessing likely scope IDs. Legacy entries are re-keyed the first
  time a caller presents their scope. `ReplayApprovalRequest.scope_hash` is
  unchanged.
- MCP: `browser_tool_save` refuses a manifest without a matching live recorded
  workflow, and `replace=True` requires the existing tool's scope.
  `browser_tool_delete` and `cache_invalidate` now require `scope_id`, and
  `cache_invalidate` clears only that scope (`terx purge` still clears a whole
  domain). `browser_tool_run` is refused while a task is recording.
- End-of-workflow screenshots are no longer captured. 0.5.x saved one per
  recorded workflow to `.terx/screenshots/` relative to the working directory,
  named by page structure (so shared across tenants), and they could contain
  page data. Delete that folder after upgrading.
- Removed the undocumented `js` condition, which evaluated arbitrary script on
  the cold path. Unknown condition keys now raise `ValueError`.
- MCP navigation now blocks `chrome:`, `chrome-extension:`, `devtools:`,
  `view-source:`, and `about:` pages other than `about:blank` (Chrome maps
  `about:flags` and similar to `chrome://`).

### Fixed

- Typing now replaces a field's value instead of appending to it. Replay, the
  workflow adapter, and MCP `browser_type` select the field's existing contents
  (autofill or prefilled values) before inserting text, through
  `replace_field_text`.
- The heartbeat reconnects the same tab in place (`CDPBridge.reconnect()`), so
  callers' bridge objects and recorders stay valid. It previously reconnected
  to whichever tab Chrome listed first and left callers holding a dead bridge.
  If the active tab is gone, its bridge is closed and `bridge()` raises instead
  of silently moving to another tab.
- Audit files are never overwritten: default session IDs include a random
  suffix, an existing file gets a new name, and caller-supplied session IDs are
  sanitized so they cannot write outside the audit directory.
- `CDPBridge` bounds its event queue (`max_queued_events`, default 1000, oldest
  dropped) and no longer leaks a pending request when a socket send fails.
- MCP `browser_task_start`, `browser_task_finish`, and `browser_tool_run`
  refuse a concurrent call instead of letting two task starts both enter.
- An expired workflow, or one whose policy changed without a
  `workflow_version` bump, is now replaced by the next successful recording.
  Previously `INSERT OR IGNORE` kept the dead row, so the workflow could never
  be cached again. `MemoryCache.store()` now returns whether it wrote, and
  `browser_task_finish` reports `cached` from that result.
- Replay validates every cached action and every required variable before the
  first action runs and before a destructive approval is consumed, instead of
  refusing after earlier steps already changed the page.
- Replay now waits: a missing semantic target is polled (an ambiguous one still
  refuses at once), a click waits for any started load, and the postcondition is
  polled. The limit is `replay_timeout` (default 10s) on `session_for`.
- The mutation guard is checked and reinstalled after every replay step, so it
  survives navigation and stops the sequence before the next action instead of
  only alarming after all actions ran. Drift and postcondition failures now set
  `status="failed"` with a report.
- `terx-server` writes its startup banner and logs to stderr, keeping stdout
  clean for the stdio JSON-RPC stream, and no longer configures logging on
  import.

## [0.5.3] - 2026-10-01

### Fixed

- Pinned the CI and development Ruff version to `0.15.11`, the reviewed
  project rule set, so an unreviewed linter upgrade cannot fail a release with
  new default rules. Reformatted the remaining source files under that rule
  set.
- Pinned the MCP dependency to the compatible 1.x API. MCP 2.x removes the
  `FastMCP` server API used by TERX, so the prior unbounded dependency made CI
  install an incompatible runtime.
- Hardened the real-Chrome evaluation launcher for hosted Linux: it now waits
  for the loopback CDP listener instead of sleeping a fixed interval, uses a
  dedicated debug address and shared-memory-safe flag, and reports Chrome's
  startup stderr when the browser exits early.

---

## [0.5.0] - 2026-10-01

### Added

- Added data-only saved tool manifests and MCP commands to save, discover, run,
  and delete a named approved workflow. A tool call validates exact input names,
  scope, policy, semantic targets, and postcondition before returning fresh
  declared page output.
- Added a constrained live-result schema for visible selector text, page title,
  page URL, and echoed non-sensitive input. Result payloads are never cached.
- Added per-run variable overrides and optional fresh result readers to
  `TerxWorkflow.run()`, so a warm replay can return current structured data.
- Added regression coverage for changed inputs, browser-session restart, scope
  refusal, manifest redaction, and fresh result extraction.

### Changed

- Replaced the action-count-derived `estimated_llm_calls_saved` display with
  `model_calls_during_replay`. TERX reports zero model calls made by TERX
  during replay; provider-side savings must come from host usage records.
- Migrated the local SQLite schema to v4 for compact saved tool manifests.

### Security

- Saved manifests retain only a scope digest and input names, never raw scope
  identifiers or input values. Sensitive input names cannot be echoed into a
  tool result.
- Named tool calls have no implicit cold-agent or self-healing fallback. A
  missing matching replay, drift, wrong scope, or unreadable result is explicit.

---

## [0.4.0] - 2026-09-29

### Changed

- Added the dependency-free `TerxWorkflow` adapter and copy-paste MCP client
  config generator. Neither launches a browser, starts a background worker, or
  adds a framework dependency.
- Reject empty condition values instead of treating them as verified checks.
- Replaced raw-CDP cache persistence with a small semantic replay IR:
  `TERX.navigate`, `TERX.click`, and `TERX.type`.
- Made replay policy-bound: cacheable workflows now need `scope_id`, non-empty
  precondition and postcondition dictionaries, route identity, version, and TTL.
- Added structured replay decisions (`hit`, `miss`, `refused`, `failed`) to
  reports and MCP results.
- Marked the Browser Use adapter experimental. It captures only agents that
  intentionally drive the supplied TERX CDP bridge.

### Security

- Stopped persisting raw CDP parameters and result payloads to SQLite or audit
  JSONL; typed replay values must be named variables and are stored as placeholders.
- Bound lookup to origin, route pattern, scope digest, and workflow version.
- Replaced token-only destructive approval with a host-supplied, consume-once
  approval verifier bound to the task, scope digest, workflow version, and
  policy fingerprint, selected DOM structural hash, and semantic command
  digest.
- Removed automatic LLM self-healing from replay. The diagnostic helper is now
  explicit opt-in (`TERX_ENABLE_EXPERIMENTAL_HEALING=1`).
- Refuse ambiguous targets, failed preconditions, unsupported actions, and
  uncacheable typed text instead of guessing or falling back.

### Verification

- Added negative tests for scope mismatch, precondition failure, destructive
  approval, ambiguous targets, and raw-script/secret persistence.
- Added the real local Chrome replay eval to CI.

## [0.3.0] - 2026-06-24

### Fixed
- Fixed SQLite cache deadlocks caused by nested cache locking.
- Fixed CDP request ID generation in the normal `send()` path.
- Prevented internal DOM snapshots from being recorded as user actions.
- Restored compact stable element IDs for MCP/tool use.

### Added
- Added explicit MCP task wrappers: `browser_task_start` and `browser_task_finish`.
- Added `browser_click_at` as a physical-click fallback while keeping `browser_click` replay-friendly.
- Added tracked MIT license and quickstart documentation.
- Added parametric replay with `{{variable}}` interpolation.
- Added default redaction for password/token/API-key typed fields.
- Added replay postcondition checks for URL, title, body text, selector presence, and custom JS.
- Added Browser Use-style `wrap_browser_use()` adapter.
- Added installed `terx-demo` and `terx demo` commands that run a local Chrome replay demo.
- Added structured `ReplayReport` objects across Python, MCP, and Browser Use-style surfaces.
- Added `TERX_REDACT_ALL_TEXT` and `TERX_REDACT_FIELDS` secret policy controls.
- Added MutationObserver replay drift guard with `MutationDriftError`.
- Added `terx` CLI with `doctor`, `stats`, `inspect`, `purge`, `demo`, and `eval-local`.
- Added local Chrome eval suite and Stagehand integration guidance.

---

## [0.1.0] - 2026-06-01

This is the initial alpha release of TERX, featuring a bare-metal CDP bridge, a fuzzy accessibility snapshot indexer, SQLite muscle memory storage, and an MCP server.

### Added
- **Direct CDP Bridge:** Fully asynchronous Chrome DevTools Protocol client using standard `websockets` library. Bypasses Playwright/Selenium overhead.
- **Heartbeat Supervisor:** Keeps tabs alive, detects browser restarts, and automatically reconnects with exponential backoff.
- **AX Tree DOM Extractor:** Filters inaccessible noise, extracting only interactable nodes (`button`, `link`, `textbox`, etc.).
- **Fuzzy Levenshtein Matcher:** Computes structural similarity on serialized element sequences so UI cache hits survive CSS changes.
- **Muscle Memory Cache:** SQLite state tracking in WAL mode. Matches, stores, and replays raw CDP command flows.
- **FastMCP Protocol Integration:** Exposes `browser_*` and `cache_*` tools for agentic integration (e.g. Cursor, Claude Desktop).
- **Audit Files:** Automatically writes JSONL audit files for replay inspection and debugging.
- **Target-Specific React inputs:** Framework-adaptive text injection using JS object resolving and `Runtime.callFunctionOn` instead of fragile focus actions.
- **LLM-Powered Self-Healing:** Seamless integration with `litellm`. When a replay fails due to DOM shift, the memory layer evaluates the new state and derives updated parameters dynamically without dropping back to a full agent evaluation.
- **Visual Auditing with SSIM:** Verifies cache replays by taking screenshots pre- and post-cache hit and generating structural similarity indexes (SSIM) to alert agents to silent visual UI drift.

### Fixed (V0.1.0 Alpha Hardening)
- **Deprecation warnings:** Replaced `asyncio.get_event_loop()` with `asyncio.get_running_loop()` to prevent crashes on Python 3.12+.
- **Broken Hash Sim:** Fixed similarity logic that compared binary hexadecimal representations directly. Replaced with raw token Levenshtein comparisons.
- **Target boundaries:** Fixed `DOM.getBoxModel` coordinate center calculation. Now averages all 8 coordinates from the four corners.
- **Startup deadlocks:** Fixed double event loop conflicts by turning `BrowserSession` initialization into a lazy-eval startup hook inside FastMCP.
- **Memory leaks:** Swapped standard dict caches for a bounded `LRUScreenshotStore` capped at 20 image buffers.
- **Collision fixes:** Appended task descriptions hash tags to the cache lookup key to stop task mismatch overlaps.
- **Navigation parameters:** Fixed target detail lookup calling `Target.getTargetInfo` by replacing it with a clean `Runtime.evaluate` metadata check.

---

## [0.0.1] - 2026-05-20
- Initial design draft and protocol specifications.
