# TERX 1.0.0 release

Released 2026-11-14.

TERX 1.0 is the first release with a stable public API. From here on, TERX
follows [semantic versioning](versioning.md): no public API is removed without
a deprecation period and a major release, and a safety check is never loosened
in a minor or patch release.

## Highlights

**Measured on a hard eval.** `terx eval-hard` replays 15 scenarios against a
local site in headless Chrome and checks each page's real outcome itself,
independently of what TERX reports:

| Metric | v0.5.3 with the 1.0 fixes | 1.0.0 |
| --- | :---: | :---: |
| Replay rate | 40% | **100%** |
| False refusal rate | 16.7% | **0%** |
| Unsafe outcomes | 1 | **0** |
| Must-refuse pages refused | 2 / 2 | **5 / 5** |

CI runs it with `--gate`, which fails on any unsafe outcome or false refusal.
Per-scenario results are in [benchmarks](benchmarks.md). These are local
fixture results on pages built to exercise known failure modes, not a claim
about arbitrary production sites.

**Six replayable actions.** `TERX.check` sets a checkbox, switch, or radio on
or off without toggling it. `TERX.select` chooses a dropdown option.
`TERX.press` presses Enter, Tab, or Escape. They join navigate, click, and
type. Navigation paths can hold `{{variable}}` placeholders.

**Targets that drift safely.** Each target now records its position and a
`data-testid`-style attribute as well as its role and label. If the label and
the test id point at different elements, TERX refuses. A changed label is
tolerated only when the test id and position agree, and every such case is
listed in `ReplayReport.locator_notes`. Elements inside same-origin iframes
are supported.

**Safer by default.** This release rolls up the fixes merged since 0.5.3:

- An expired workflow can be re-recorded.
- Replay validates the whole sequence before acting.
- Replay waits for pages and targets to appear.
- The mutation guard survives navigation.
- Typing replaces a field's value instead of appending.
- A dropped connection reconnects the same tab.
- Scope digests are keyed (HMAC).
- MCP clients can only act on scopes they present.
- Screenshots are never stored.
- Arbitrary-script conditions and browser-internal pages are refused.

The [threat model](threat-model.md) documents what TERX protects against and
the risk that remains.

**Maintainable.** The 2,000-line cache module is split into focused modules,
the package passes `mypy --strict` and ships `py.typed`, and dead experimental
code is gone.

## Upgrading from 0.5.x

The cache database schema is unchanged (version 4): no migration runs and
existing workflows keep replaying. Check each item that applies to you.

### Python API

- **`MuscleMemorycache` was removed.** Use `MemoryCache`.
- **`terx.agent` (`SelfHealer`), `terx.vision`, and `terx.benchmarks` were
  removed,** along with the `healing`, `vision`, and `embeddings` extras.
  Remove them from imports and requirements; `pip install "terx[all]"` now
  installs `browser-use` and `dev` only.
- **Delete old screenshots.** 0.5.x saved an end-of-workflow screenshot for
  every recorded workflow to `.terx/screenshots/`, relative to the working
  directory where TERX ran. Those images can contain page data such as
  account details. TERX 1.0 never captures screenshots, so delete that
  folder wherever TERX has run.
- **Building a `SavedTool` by hand:** set `scope_hash=cache.scope_digest(scope_id)`
  instead of `ReplayPolicy(scope_id=...).scope_hash()`. The host-facing
  `ReplayApprovalRequest.scope_hash` is unchanged.
- **`MemoryCache.store()` returns `bool`** (whether a row was written).
- **Conditions with unknown keys** (including the old, undocumented `js`) now
  raise `ValueError` instead of running script.
- `terx.cache.cache` still re-exports every name it had in 0.5.x and will for
  all of 1.x. New code should import from `terx` or `terx.cache`.

### MCP server

- **`browser_tool_delete(name, scope_id)` and `cache_invalidate(domain,
  scope_id)` now require `scope_id`.** `cache_invalidate` clears only that
  scope; operators can still purge a whole domain with `terx purge <domain>`.
- **`browser_tool_save` refuses a manifest** unless a live recorded workflow
  matches its task, scope, version, and policy, and replacing a tool requires
  that tool's scope. Save tools after `browser_task_finish(success=true)`.
- **Navigation to `chrome:`, `devtools:`, `chrome-extension:`, `view-source:`,
  and `about:` pages other than `about:blank` is refused.**
- New tools: `browser_set_checked`, `browser_select_option`,
  `browser_press_key`.
- Startup output now goes to stderr, so stdio MCP clients no longer see stray
  text on stdout.

### Recorded workflows and cache data

- **Scope digests are re-keyed automatically.** The cache now stores an HMAC
  of each scope with a random key in `.terx/scope.key`. Entries from 0.5.x are
  upgraded the first time a caller presents their scope. Processes that share
  a cache must share that key file, or pass the same
  `MemoryCache(scope_key=...)`.
- **Re-record workflows whose URLs contain per-run values.** A workflow
  recorded in 0.5.x with, for example, `/orders/A-100` keeps that literal URL
  and will reopen A-100. Bump its `workflow_version` and record it again to get
  `/orders/{{order_id}}`.
- **Re-record to gain multi-signal targets.** Older targets have only role and
  label and keep resolving exactly as before. New recordings also store
  position and test id.

## Verification

```bash
pip install --upgrade terx
ruff check .
ruff format --check .
mypy
pytest tests/ -q
terx eval-local
terx eval-hard --gate
```

On the release commit: 101 tests pass, `mypy --strict` reports no issues in 28
source files, `terx eval-local` replays 5/5 tasks, and `terx eval-hard --gate`
reports the numbers above. CI runs all of these on Python 3.11, 3.12, and 3.13.

## Known gaps

- File uploads, multiple tabs, and cross-site (out-of-process) iframes are not
  supported; targets in them refuse.
- A secret in a URL path that was not supplied as a variable is stored as
  written. Pass tokens as variables.
- Workflows are not signed, and MCP clients are not individually
  authenticated. See the [threat model](threat-model.md).
