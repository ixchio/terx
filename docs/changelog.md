# Changelog

All notable changes to the TERX browser memory layer are documented in this file.

---

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
