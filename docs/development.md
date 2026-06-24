# TERX Developer Guide

This document covers local development setup, architectural internals, code conventions, and testing protocols.

---

## 🏗️ Architecture Blueprint

TERX operates as a lightweight, modular middle-layer. Unlike heavy automation frameworks (e.g., Playwright) that spawn multi-layered browser runtimes, TERX communicates with Chrome directly via a single WebSocket per tab using the **Chrome DevTools Protocol (CDP)**.

```
                  ┌─────────────────────────────────┐
                  │           AI Agent /            │
                  │        MCP Client App           │
                  └────────────────┬────────────────┘
                                   │ (MCP Protocol)
                                   ▼
                  ┌─────────────────────────────────┐
                  │         TERX MCP Server         │
                  │         (FastMCP tools)         │
                  └────────────────┬────────────────┘
                                   │ (Direct method calls)
                                   ▼
                  ┌─────────────────────────────────┐
                  │         BrowserSession          │
                  │      (Tab Manager / Heartbeat)  │
                  └──────┬───────────────────┬──────┘
                         │                   │
                         ▼                   ▼
                ┌───────────────┐   ┌───────────────┐
                │   CDPBridge   │   │   CDPBridge   │
                │    (Tab 1)    │   │    (Tab 2)    │
                └───────┬───────┘   └───────┬───────┘
                        │ (JSON WebSocket)  │
                        ▼                   ▼
              ┌───────────────────────────────────────┐
              │           Google Chrome               │
              │  (Remote Debugging Port: 9222)        │
              └───────────────────────────────────────┘
```

---

## 🛠️ Development Setup

### 1. Prerequisites
- **Python:** 3.11 or 3.12 (standard packages work).
- **Chrome/Chromium:** Required for debugging.
- **Dependencies:** `websockets`, `aiohttp`, `mcp`, `mmh3`. (Optional: `sentence-transformers` for semantic matching).

### 2. Sandbox Setup
Clone the repository and perform an editable, developmental installation:

```bash
git clone https://github.com/ixchio/terx.git
cd terx

# Create and activate virtual environment
python3 -m venv .venv
source .venv/bin/activate

# Install all development dependencies in editable mode
pip install -e ".[all]"
```

### 3. Launching Chrome with Debugging enabled
Chrome must expose the debugger websocket interface. Make sure all Chrome windows are closed first, then launch:

**Linux:**
```bash
google-chrome --remote-debugging-port=9222 --no-first-run --user-data-dir=~/.config/chrome-dev
```

**macOS:**
```bash
/Applications/Google\ Chrome.app/Contents/MacOS/Google\ Chrome --remote-debugging-port=9222 --no-first-run --user-data-dir=/tmp/chrome-dev
```

**Windows (PowerShell):**
```powershell
Start-Process "chrome.exe" -ArgumentList "--remote-debugging-port=9222", "--no-first-run", "--user-data-dir=$env:TEMP\chrome-dev"
```

Verify connection by visiting `http://localhost:9222/json/list` in any browser.

---

## 🔬 Core Systems Implementation

### 1. Direct CDP Bridge (`terx.cdp.bridge`)
The bridge does not use intermediate abstractions. It wraps a raw WebSocket connection.

To bypass loop attachment exceptions (`RuntimeError: Event loop is closed` / `attached to a different loop`), the WebSocket listener executes as a background task spawned on the running loop (`asyncio.get_running_loop()`). Command sequences match incoming frames using incremental transaction IDs:

```python
cmd_id = next(self._id_counter)
future = asyncio.get_running_loop().create_future()
self._pending[cmd_id] = future

# Sent as JSON frame
await self._ws.send(json.dumps({"id": cmd_id, "method": method, "params": params}))
```

### 2. Fuzzy Structural Hasher (`terx.dom.extractor`)
Instead of parsing raw HTML strings (which causes context bloat and token waste), the DOM Extractor retrieves Chrome's Accessibility Tree (AXTree).

Fuzzy matching uses token-level **Levenshtein Distance** over the compiled role sequences. When the user requests a cached navigation target, the system matches the sequence against database records using this calculation:

$$\text{Similarity} = 1.0 - \frac{\text{LevenshteinDistance}(S_{\text{active}}, S_{\text{cached}})}{\max(|S_{\text{active}}|, |S_{\text{cached}}|)}$$

If the similarity is $\ge 0.85$, the cache yields a hit.

### 3. Parametric Replay and Redaction

`session_for(..., variables={...})` replaces matching typed values with stable
placeholders before caching:

```python
await bridge.send("Input.insertText", {"text": "user@example.com"})
# stored as {"text": "{{email}}"} when variables["email"] == "user@example.com"
```

Sensitive fields whose AX label includes terms such as `password`, `token`, or
`api key` are redacted by default even when the caller forgot to pass variables.
Replay then raises `MissingReplayVariable` until the required value is supplied.

### 4. Replay Postconditions

A cache replay can execute technically and still land on the wrong page. TERX
therefore supports postconditions:

```python
postcondition={
    "url_contains": "/dashboard",
    "text_contains": "Welcome",
    "selector_exists": "#account-menu",
}
```

Failed postconditions raise `PostconditionFailed` and do not count as cache hits.

### 5. Replay Reports

Every recording context exposes `ctx.report`, a structured `ReplayReport` used
by the Python API, MCP tools, Browser Use-style adapter, and CLI-facing demos.
It includes cache hit state, command counts, variable placeholders, redacted
fields, postcondition metadata, latency, run number, and mutation guard stats.

### 6. Mutation Guard

Warm replays can be suspicious even when individual CDP commands succeed. TERX
therefore injects a temporary page-scoped `MutationObserver` before replay and
reads the mutation count after the sequence. If the count exceeds the configured
threshold, replay raises `MutationDriftError` instead of silently accepting a
workflow that likely landed on a changed UI.

```python
async with session_for(
    cache,
    bridge,
    "approve invoice",
    mutation_guard=True,
    mutation_threshold=20,
) as ctx:
    if ctx.hit:
        await ctx.replay()
```

### 7. Operator CLI

`terx` is the human/operator command surface:

```bash
terx doctor
terx stats
terx inspect --domain app.example.com
terx purge app.example.com
terx demo
terx eval-local
```

`inspect` reads SQLite directly in read-only mode and summarizes cached
sequences without printing recorded secret values.

---

## 🧪 Testing Guidelines

Verify modifications against unit tests before committing:

```bash
# Run basic tests
pytest tests/ -v

# Run performance benchmarks with pytest-benchmark
pytest tests/ -v --benchmark-only
```

Write test cases inside the `tests/` folder. When verifying browser interactions, mock the `CDPBridge` or spin up a headless Chrome instance with temporary profile directories.
