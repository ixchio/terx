"""Self-contained TERX browser replay eval suite."""

from __future__ import annotations

import asyncio
import base64
import json
import os
import shutil
import socket
import subprocess
import tempfile
import time
from collections.abc import Awaitable, Callable
from dataclasses import asdict, dataclass, field
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from threading import Thread
from typing import Any

from terx.cache import (
    ApprovalDecision,
    ApprovalVerifier,
    MemoryCache,
    ReplayApprovalRequest,
    session_for,
)
from terx.cache.policy import SideEffect
from terx.cdp.session import BrowserSession
from terx.dom.extractor import DOMExtractor
from terx.integrations.workflow import TerxActions, TerxWorkflow, WorkflowRunResult
from terx.server.mcp import TERXServer
from terx.tools import SavedTool


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

  <section>
    <h2>Vendor lookup</h2>
    <label>Account ID</label>
    <input aria-label="Account ID" id="account-id">
    <label>Order ID</label>
    <input aria-label="Order ID" id="order-id">
    <button aria-label="Check Order Status" id="check-order">Check status</button>
    <p id="order-status">No lookup yet</p>
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
    document.getElementById('check-order').addEventListener('click', function () {
      const account = document.getElementById('account-id').value;
      const order = document.getElementById('order-id').value;
      document.getElementById('order-status').textContent =
        'Account ' + account + ' — Order ' + order + ': status: delivered';
    });
  </script>
</body>
</html>"""


class EvalHandler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        if self.path.startswith("/slow"):
            time.sleep(0.25)
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
    details: dict[str, Any] = field(default_factory=dict)


async def run_suite(capture_dir: Path | None = None) -> dict[str, Any]:
    server = _start_server(0)
    http_port = server.server_port
    user_data_dir = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
    cache_dir = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
    chrome: subprocess.Popen[str] | None = None
    try:
        chrome, cdp_port = await launch_chrome(user_data_dir.name)

        cache = MemoryCache(
            db_path=Path(cache_dir.name) / "cache.db",
            audit_dir=Path(cache_dir.name) / "audit",
        )
        # A deliberate server-side delay exercises the CDP load wait without
        # pretending it is a production-site benchmark.
        base_url = f"http://127.0.0.1:{http_port}/slow"

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
                    side_effect="mutating",
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
                    side_effect="read_only",
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
                    side_effect="destructive",
                    runner=_approve_invoice,
                ),
                await _run_workflow_adapter_case(cache, bridge, base_url),
            ]
        results.append(await _run_saved_tool_case(cache, base_url, cdp_port, capture_dir))
    finally:
        if chrome is not None:
            stop_chrome(chrome)
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
    side_effect: SideEffect,
    runner: Callable[[Any, dict[str, str]], Awaitable[None]],
) -> EvalCaseResult:
    approval_verifier = _local_approval_verifier() if side_effect == "destructive" else None
    await bridge.send("Page.navigate", {"url": base_url})
    await bridge.wait_for_load()
    cold_started = time.perf_counter()
    async with session_for(
        cache,
        bridge,
        task,
        variables=cold_variables,
        scope_id=f"local-eval:{task}",
        precondition={"url_contains": base_url},
        postcondition=postcondition,
        side_effect=side_effect,
        approval_verifier=approval_verifier,
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
        scope_id=f"local-eval:{task}",
        precondition={"url_contains": base_url},
        postcondition=postcondition,
        side_effect=side_effect,
        approval_verifier=approval_verifier,
    ) as warm_ctx:
        if not warm_ctx.hit:
            raise RuntimeError(f"Unexpected cache miss on warm run: {task}")
        await warm_ctx.replay(
            approval_token="local-eval-approved" if side_effect == "destructive" else None
        )
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


async def _run_workflow_adapter_case(
    cache: MemoryCache, bridge: Any, base_url: str
) -> EvalCaseResult:
    """Exercise the public dependency-free adapter against real Chrome."""
    task = "login through TERX workflow adapter"
    postcondition = {"text_contains": "Welcome"}

    async def run_once(variables: dict[str, str]) -> tuple[WorkflowRunResult, float]:
        await bridge.send_internal("Page.navigate", {"url": base_url})
        await bridge.wait_for_load()
        workflow = TerxWorkflow(
            cache=cache,
            bridge=bridge,
            task=task,
            scope_id=f"local-eval:{task}",
            variables=variables,
            precondition={"url_contains": base_url},
            postcondition=postcondition,
        )

        async def cold_path(browser: TerxActions) -> None:
            await browser.type_into("textbox", "Email", "email")
            await browser.type_into("textbox", "Password", "password")
            await browser.click("button", "Login")
            await browser.wait_for(postcondition)

        started = time.perf_counter()
        result = await workflow.run(cold_path)
        return result, (time.perf_counter() - started) * 1000

    cold_result, cold_ms = await run_once(
        {"email": "adapter-cold@example.com", "password": "adapter-cold-secret"}
    )
    if cold_result.cache_hit:
        raise RuntimeError("Unexpected workflow-adapter cache hit on cold run")

    warm_result, warm_ms = await run_once(
        {"email": "adapter-warm@example.com", "password": "adapter-warm-secret"}
    )
    if not warm_result.cache_hit:
        raise RuntimeError("Unexpected workflow-adapter cache miss on warm run")

    report = warm_result.report
    return EvalCaseResult(
        task=task,
        cold_ms=round(cold_ms, 1),
        warm_ms=round(warm_ms, 1),
        cache_hit=warm_result.cache_hit,
        cold_commands=cold_result.commands_recorded,
        warm_commands=report.commands_replayed if report else 0,
        variables_used=report.variables_used if report else [],
        redacted_fields=cold_result.report.redacted_fields if cold_result.report else [],
        postcondition=postcondition,
    )


async def _run_saved_tool_case(
    cache: MemoryCache,
    base_url: str,
    cdp_port: int,
    capture_dir: Path | None = None,
) -> EvalCaseResult:
    """Prove the public saved-tool path across a fresh browser CDP session."""
    task = "check local vendor order status"
    scope_id = "local-eval:vendor-orders:account-a"
    postcondition = {"text_contains": ": status:"}
    cold_inputs = {"account_id": "account-a", "order_id": "A-100"}
    warm_inputs = {"account_id": "account-b", "order_id": "A-101"}

    async with BrowserSession(port=cdp_port) as recording_session:
        bridge = recording_session.bridge()
        await bridge.send_internal("Page.navigate", {"url": base_url})
        await bridge.wait_for_load()
        if capture_dir is not None:
            await _capture_frame(bridge, capture_dir / "01-task-ready.png")
        workflow = TerxWorkflow(
            cache=cache,
            bridge=bridge,
            task=task,
            scope_id=scope_id,
            variables=cold_inputs,
            precondition={"url_contains": base_url},
            postcondition=postcondition,
            route_pattern="/slow",
            workflow_version=1,
            side_effect="read_only",
        )

        async def cold_path(browser: TerxActions) -> None:
            await browser.type_into("textbox", "Account ID", "account_id")
            await browser.type_into("textbox", "Order ID", "order_id")
            await browser.click("button", "Check Order Status")
            await browser.wait_for(postcondition)

        cold_started = time.perf_counter()
        cold_result = await workflow.run(cold_path)
        cold_ms = (time.perf_counter() - cold_started) * 1000
        if cold_result.cache_hit:
            raise RuntimeError("Unexpected saved-tool cache hit on cold run")
        if capture_dir is not None:
            await _capture_frame(bridge, capture_dir / "02-task-recorded.png")

    cache.save_tool(
        SavedTool(
            name="check_order_status",
            description="Return the local vendor order status for an account and order ID.",
            task=task,
            input_names=("account_id", "order_id"),
            scope_hash=cache.scope_digest(scope_id),
            precondition={"url_contains": base_url},
            postcondition=postcondition,
            route_pattern="/slow",
            workflow_version=1,
            side_effect="read_only",
            ttl_seconds=86_400,
            mutation_guard=True,
            mutation_threshold=20,
            result_spec={
                "account_id": {"source": "input", "name": "account_id"},
                "order_id": {"source": "input", "name": "order_id"},
                "status": {"source": "text", "selector": "#order-status"},
            },
        )
    )

    async with BrowserSession(port=cdp_port) as replay_session:
        bridge = replay_session.bridge()
        await bridge.send_internal("Page.navigate", {"url": base_url})
        await bridge.wait_for_load()
        server = TERXServer(cache=cache)
        server._session = replay_session
        warm_started = time.perf_counter()
        warm_result = await server._run_saved_tool("check_order_status", warm_inputs, scope_id)
        warm_ms = (time.perf_counter() - warm_started) * 1000
        if not warm_result.get("success") or not warm_result.get("cache_hit"):
            raise RuntimeError(f"Saved tool did not replay: {warm_result}")
        expected_status = "Account account-b — Order A-101: status: delivered"
        if warm_result["result"]["status"] != expected_status:
            raise RuntimeError(f"Saved tool returned stale output: {warm_result['result']}")
        if capture_dir is not None:
            await _capture_frame(bridge, capture_dir / "03-tool-replayed.png")

        # The same contract must refuse an ambiguous live target rather than
        # guessing which input to use.
        await bridge.send_internal("Page.navigate", {"url": base_url})
        await bridge.wait_for_load()
        await bridge.send_internal(
            "Runtime.evaluate",
            {
                "expression": (
                    "document.querySelector('#order-id').insertAdjacentHTML("
                    "'afterend', '<input aria-label=\"Order ID\">')"
                )
            },
        )
        ambiguous = await server._run_saved_tool("check_order_status", warm_inputs, scope_id)
        if not ambiguous.get("refused"):
            raise RuntimeError(f"Ambiguous saved-tool target was not refused: {ambiguous}")

    report = warm_result.get("report") or {}
    return EvalCaseResult(
        task=task,
        cold_ms=round(cold_ms, 1),
        warm_ms=round(warm_ms, 1),
        cache_hit=True,
        cold_commands=cold_result.commands_recorded,
        warm_commands=report.get("commands_replayed", 0),
        variables_used=report.get("variables_used", []),
        redacted_fields=(cold_result.report.redacted_fields if cold_result.report else []),
        postcondition=postcondition,
        details={
            "fresh_result": warm_result["result"],
            "changed_input": True,
            "changed_account": True,
            "browser_session_restarted": True,
            "slow_page_load": True,
            "ambiguous_target_refused": True,
            "terx_model_calls_during_replay": 0,
        },
    )


async def _capture_frame(bridge: Any, path: Path) -> None:
    """Save an unaltered browser frame for the reproducible local demo."""
    path.parent.mkdir(parents=True, exist_ok=True)
    result = await bridge.send_internal(
        "Page.captureScreenshot",
        {"format": "png", "captureBeyondViewport": True},
    )
    data = result.get("data")
    if not isinstance(data, str) or not data:
        raise RuntimeError("Chrome did not return a screenshot for the demo frame")
    path.write_bytes(base64.b64decode(data))


async def _fill_login(bridge: Any, variables: dict[str, str]) -> None:
    await _focus_type(bridge, "Email", variables["email"])
    await _focus_type(bridge, "Password", variables["password"])
    await _click(bridge, "Login")


async def _run_search(bridge: Any, variables: dict[str, str]) -> None:
    await _focus_type(bridge, "Search Query", variables["query"])
    await _click(bridge, "Run Search")


async def _approve_invoice(bridge: Any, _: dict[str, str]) -> None:
    await _click(bridge, "Approve Invoice")


def _local_approval_verifier() -> ApprovalVerifier:
    """Test-only consume-once approval authority for the destructive eval."""
    consumed: set[str] = set()

    def verify_and_consume(request: ReplayApprovalRequest) -> ApprovalDecision:
        if request.token != "local-eval-approved":
            return ApprovalDecision(False, False, "unknown local approval")
        if request.token in consumed:
            return ApprovalDecision(False, False, "local approval already consumed")
        consumed.add(request.token)
        return ApprovalDecision(True, True)

    return verify_and_consume


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


CHROME_STARTUP_TIMEOUT_ENV = "TERX_CHROME_STARTUP_TIMEOUT"
DEFAULT_CHROME_STARTUP_TIMEOUT = 30.0


def _chrome_startup_timeout() -> float:
    raw = os.environ.get(CHROME_STARTUP_TIMEOUT_ENV, "")
    try:
        value = float(raw) if raw else DEFAULT_CHROME_STARTUP_TIMEOUT
    except ValueError:
        value = DEFAULT_CHROME_STARTUP_TIMEOUT
    return value if value > 0 else DEFAULT_CHROME_STARTUP_TIMEOUT


async def launch_chrome(user_data_dir: str) -> tuple[subprocess.Popen[str], int]:
    """Start headless Chrome on a port it chooses itself and wait for CDP.

    ``--remote-debugging-port=0`` lets Chrome bind a free port atomically and
    record it in ``DevToolsActivePort`` inside the profile, so there is no
    window in which another process can take a port picked in advance. Cold
    CI runners can be slow to start Chrome; the wait defaults to 30 seconds
    and can be changed with ``TERX_CHROME_STARTUP_TIMEOUT``.
    """
    chrome = subprocess.Popen(
        [
            _chrome_binary(),
            "--headless=new",
            "--remote-debugging-address=127.0.0.1",
            "--remote-debugging-port=0",
            f"--user-data-dir={user_data_dir}",
            "--disable-gpu",
            "--disable-dev-shm-usage",
            "--no-first-run",
            "--no-sandbox",
            "about:blank",
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        port = await _wait_for_devtools_port(chrome, Path(user_data_dir), _chrome_startup_timeout())
    except BaseException:
        stop_chrome(chrome)
        raise
    return chrome, port


async def _wait_for_devtools_port(
    chrome: subprocess.Popen[str], profile: Path, timeout_seconds: float
) -> int:
    """Read the port Chrome chose from DevToolsActivePort, then confirm it listens."""
    deadline = time.monotonic() + timeout_seconds
    active_port_file = profile / "DevToolsActivePort"
    while time.monotonic() < deadline:
        if chrome.poll() is not None:
            stderr = chrome.stderr.read() if chrome.stderr is not None else ""
            detail = stderr.strip()[-2_000:] or "no Chrome stderr was produced"
            raise RuntimeError(
                f"Chrome exited before CDP was ready (exit {chrome.returncode}): {detail}"
            )
        try:
            first_line = active_port_file.read_text().splitlines()[0]
            port = int(first_line)
        except (OSError, ValueError, IndexError):
            await asyncio.sleep(0.1)
            continue
        remaining = max(deadline - time.monotonic(), 0.1)
        await _wait_for_chrome_debugger(chrome, port, timeout_seconds=remaining)
        return port
    raise RuntimeError(
        f"Chrome did not report a DevTools port within {timeout_seconds:.1f}s "
        f"(set {CHROME_STARTUP_TIMEOUT_ENV} to allow longer)"
    )


def stop_chrome(chrome: subprocess.Popen[str]) -> None:
    """Terminate Chrome, escalating to kill, and close its stderr pipe."""
    if chrome.poll() is None:
        chrome.terminate()
        try:
            chrome.wait(timeout=5)
        except subprocess.TimeoutExpired:
            chrome.kill()
            chrome.wait(timeout=5)
    if chrome.stderr is not None:
        chrome.stderr.close()


async def _wait_for_chrome_debugger(
    chrome: subprocess.Popen[str], port: int, *, timeout_seconds: float = 10.0
) -> None:
    """Wait for Chrome's loopback CDP listener and keep early-exit diagnostics."""
    deadline = time.monotonic() + timeout_seconds
    last_error = "no connection attempt made"
    while time.monotonic() < deadline:
        if chrome.poll() is not None:
            stderr = chrome.stderr.read() if chrome.stderr is not None else ""
            detail = stderr.strip()[-2_000:] or "no Chrome stderr was produced"
            raise RuntimeError(
                f"Chrome exited before CDP was ready (exit {chrome.returncode}): {detail}"
            )
        try:
            _, writer = await asyncio.open_connection("127.0.0.1", port)
        except OSError as error:
            last_error = str(error)
            await asyncio.sleep(0.1)
            continue
        writer.close()
        await writer.wait_closed()
        return
    raise RuntimeError(
        f"Chrome did not expose CDP on 127.0.0.1:{port} within {timeout_seconds:.1f}s "
        f"({last_error})"
    )


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
