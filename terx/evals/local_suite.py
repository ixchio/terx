"""Self-contained TERX browser replay eval suite."""

from __future__ import annotations

import asyncio
import json
import shutil
import socket
import subprocess
import tempfile
import time
from collections.abc import Awaitable, Callable
from dataclasses import asdict, dataclass
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from threading import Thread
from typing import Any

from terx.cache.cache import MemoryCache, session_for
from terx.cdp.session import BrowserSession
from terx.dom.extractor import DOMExtractor


HTML = """<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <title>TERX Local Eval</title>
  <style>
    body { font-family: system-ui, sans-serif; max-width: 720px; margin: 48px auto; }
    section { border-top: 1px solid #ddd; padding: 18px 0; }
    label { display: block; margin: 10px 0 4px; }
    input { width: 100%; max-width: 360px; padding: 9px; box-sizing: border-box; }
    button { margin-top: 12px; padding: 9px 14px; }
    #status, #results, #row-status { font-weight: 700; min-height: 24px; }
  </style>
</head>
<body>
  <h1>TERX Local Eval</h1>

  <section>
    <h2>Login</h2>
    <label>Email</label>
    <input aria-label="Email" id="email" type="email">
    <label>Password</label>
    <input aria-label="Password" id="password" type="password">
    <button aria-label="Login" id="login">Login</button>
    <p id="status">Waiting</p>
  </section>

  <section>
    <h2>Search</h2>
    <label>Search Query</label>
    <input aria-label="Search Query" id="query">
    <button aria-label="Run Search" id="search">Search</button>
    <p id="results">No results</p>
  </section>

  <section>
    <h2>Approvals</h2>
    <button aria-label="Approve Invoice" id="approve">Approve invoice</button>
    <p id="row-status">Invoice pending</p>
  </section>

  <script>
    document.getElementById('login').addEventListener('click', function () {
      document.getElementById('status').textContent =
        'Welcome ' + document.getElementById('email').value;
    });
    document.getElementById('search').addEventListener('click', function () {
      document.getElementById('results').textContent =
        'Results for ' + document.getElementById('query').value;
    });
    document.getElementById('approve').addEventListener('click', function () {
      document.getElementById('row-status').textContent = 'Invoice approved';
    });
  </script>
</body>
</html>"""


class EvalHandler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        self.send_response(200)
        self.send_header("Content-Type", "text/html")
        self.end_headers()
        self.wfile.write(HTML.encode())

    def log_message(self, *_: Any) -> None:
        return


@dataclass
class EvalCaseResult:
    task: str
    cold_ms: float
    warm_ms: float
    cache_hit: bool
    cold_commands: int
    warm_commands: int
    variables_used: list[str]
    redacted_fields: list[str]
    postcondition: Any


async def run_suite() -> dict[str, Any]:
    http_port = _free_port()
    cdp_port = _free_port()
    server = _start_server(http_port)
    user_data_dir = tempfile.TemporaryDirectory()
    cache_dir = tempfile.TemporaryDirectory()
    chrome = subprocess.Popen(
        [
            _chrome_binary(),
            "--headless=new",
            f"--remote-debugging-port={cdp_port}",
            f"--user-data-dir={user_data_dir.name}",
            "--disable-gpu",
            "--no-sandbox",
            "about:blank",
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    await asyncio.sleep(1.5)

    cache = MemoryCache(
        db_path=Path(cache_dir.name) / "cache.db",
        audit_dir=Path(cache_dir.name) / "audit",
    )
    base_url = f"http://127.0.0.1:{http_port}/"

    try:
        async with BrowserSession(port=cdp_port) as session:
            bridge = session.bridge()
            results = [
                await _run_case(
                    cache,
                    bridge,
                    base_url,
                    task="login to local eval app",
                    cold_variables={"email": "cold@example.com", "password": "cold-secret"},
                    warm_variables={"email": "warm@example.com", "password": "warm-secret"},
                    postcondition={"text_contains": "Welcome"},
                    runner=_fill_login,
                ),
                await _run_case(
                    cache,
                    bridge,
                    base_url,
                    task="search local eval records",
                    cold_variables={"query": "contracts"},
                    warm_variables={"query": "invoices"},
                    postcondition={"text_contains": "Results for"},
                    runner=_run_search,
                ),
                await _run_case(
                    cache,
                    bridge,
                    base_url,
                    task="approve local eval invoice",
                    cold_variables={},
                    warm_variables={},
                    postcondition={"text_contains": "Invoice approved"},
                    runner=_approve_invoice,
                ),
            ]
    finally:
        chrome.terminate()
        try:
            chrome.wait(timeout=5)
        except subprocess.TimeoutExpired:
            chrome.kill()
        server.shutdown()
        user_data_dir.cleanup()
        cache_dir.cleanup()

    hit_rate = sum(1 for result in results if result.cache_hit) / len(results)
    return {
        "suite": "local",
        "cases": [asdict(result) for result in results],
        "summary": {
            "tasks": len(results),
            "warm_cache_hit_rate": hit_rate,
            "cold_median_ms": _median([result.cold_ms for result in results]),
            "warm_median_ms": _median([result.warm_ms for result in results]),
            "commands_replayed": sum(result.warm_commands for result in results),
        },
    }


async def _run_case(
    cache: MemoryCache,
    bridge: Any,
    base_url: str,
    *,
    task: str,
    cold_variables: dict[str, str],
    warm_variables: dict[str, str],
    postcondition: dict[str, str],
    runner: Callable[[Any, dict[str, str]], Awaitable[None]],
) -> EvalCaseResult:
    await bridge.send("Page.navigate", {"url": base_url})
    await bridge.wait_for_load()
    cold_started = time.perf_counter()
    async with session_for(
        cache,
        bridge,
        task,
        variables=cold_variables,
        postcondition=postcondition,
    ) as cold_ctx:
        if cold_ctx.hit:
            raise RuntimeError(f"Unexpected cache hit on cold run: {task}")
        await runner(bridge, cold_variables)
    cold_ms = (time.perf_counter() - cold_started) * 1000

    await bridge.send("Page.navigate", {"url": base_url})
    await bridge.wait_for_load()
    warm_started = time.perf_counter()
    async with session_for(
        cache,
        bridge,
        task,
        variables=warm_variables,
        postcondition=postcondition,
    ) as warm_ctx:
        if not warm_ctx.hit:
            raise RuntimeError(f"Unexpected cache miss on warm run: {task}")
        await warm_ctx.replay()
    warm_ms = (time.perf_counter() - warm_started) * 1000

    warm_report = warm_ctx.report
    cold_report = cold_ctx.report
    return EvalCaseResult(
        task=task,
        cold_ms=round(cold_ms, 1),
        warm_ms=round(warm_ms, 1),
        cache_hit=warm_ctx.hit,
        cold_commands=cold_ctx.recorded_commands,
        warm_commands=warm_report.commands_replayed if warm_report else 0,
        variables_used=warm_report.variables_used if warm_report else [],
        redacted_fields=cold_report.redacted_fields if cold_report else [],
        postcondition=postcondition,
    )


async def _fill_login(bridge: Any, variables: dict[str, str]) -> None:
    await _focus_type(bridge, "Email", variables["email"])
    await _focus_type(bridge, "Password", variables["password"])
    await _click(bridge, "Login")


async def _run_search(bridge: Any, variables: dict[str, str]) -> None:
    await _focus_type(bridge, "Search Query", variables["query"])
    await _click(bridge, "Run Search")


async def _approve_invoice(bridge: Any, _: dict[str, str]) -> None:
    await _click(bridge, "Approve Invoice")


async def _focus_type(bridge: Any, label: str, text: str) -> None:
    snapshot = await DOMExtractor().snapshot(bridge)
    element = snapshot.find_by_label(label)
    if element is None:
        raise RuntimeError(f"Element not found: {label}")
    await bridge.send("DOM.focus", {"backendNodeId": element.backend_dom_id})
    await bridge.send("Input.insertText", {"text": text})


async def _click(bridge: Any, label: str) -> None:
    snapshot = await DOMExtractor().snapshot(bridge)
    element = snapshot.find_by_label(label)
    if element is None:
        raise RuntimeError(f"Element not found: {label}")
    resolved = await bridge.send("DOM.resolveNode", {"backendNodeId": element.backend_dom_id})
    object_id = resolved.get("object", {}).get("objectId")
    await bridge.send(
        "Runtime.callFunctionOn",
        {"objectId": object_id, "functionDeclaration": "function() { this.click(); }"},
    )
    await asyncio.sleep(0.1)


def _start_server(port: int) -> HTTPServer:
    server = HTTPServer(("127.0.0.1", port), EvalHandler)
    Thread(target=server.serve_forever, daemon=True).start()
    return server


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _chrome_binary() -> str:
    for name in ("google-chrome", "chromium", "chromium-browser"):
        binary = shutil.which(name)
        if binary:
            return binary
    raise RuntimeError("Chrome/Chromium not found")


def _median(values: list[float]) -> float:
    ordered = sorted(values)
    mid = len(ordered) // 2
    if len(ordered) % 2:
        return round(ordered[mid], 1)
    return round((ordered[mid - 1] + ordered[mid]) / 2, 1)


def main() -> None:
    print(json.dumps(asyncio.run(run_suite()), indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
