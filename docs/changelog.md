# Changelog

All notable changes to the TERX browser memory layer are documented in this file.

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
- **VCR Integration:** Automatically writes `.vcr` files outputting JSONL frames that are fully backward compatible with Agent VCR playbacks.
- **Target-Specific React inputs:** Framework-adaptive text injection using JS object resolving and `Runtime.callFunctionOn` instead of fragile focus actions.

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
