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
self._id_counter += 1
cmd_id = self._id_counter
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
