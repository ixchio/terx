"""`terx demo`: record a browser task once, replay it, and watch TERX refuse.

The demo is self-contained. It starts a throwaway headless browser and a page
served from this machine, then shows the three things TERX does:

1. **Record:** a scripted stand-in for an agent signs in. TERX keeps the
   actions only after the outcome is verified, with values stored as
   ``{{placeholders}}``.
2. **Replay:** the same task runs again for a different account with no agent
   and no model call, and the outcome is verified again.
3. **Refuse:** the page changes so a target is ambiguous. TERX refuses before
   touching the page and hands control back.

Every line marked as verified is checked against the real page or TERX's real
files during the run; the demo exits non-zero if any check fails.
"""

from __future__ import annotations

import asyncio
import json
import sys
import tempfile
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from threading import Thread
from typing import Any

from terx.cache import MemoryCache, ReplayRefused
from terx.cdp.bridge import CDPBridge
from terx.cdp.chrome import ChromeNotFoundError, launch_chrome, stop_chrome
from terx.cdp.session import BrowserSession
from terx.integrations.workflow import TerxActions, TerxWorkflow

TASK = "sign in to the demo dashboard"
SCOPE = "terx-demo:local"
FIRST = {"email": "ada@example.com", "password": "first-demo-secret"}
SECOND = {"email": "grace@example.com", "password": "second-demo-secret"}

_NAV = "".join(
    f'<a href="#{name.lower()}">{name}</a> '
    for name in ("Home", "Orders", "Reports", "Settings", "Help")
)
# Shown only after "the site changes": a second field with the same label.
_NEWSLETTER = "<footer><label>Email <input id=news type=email></label></footer>"


def _page(changed: bool) -> str:
    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <title>TERX demo dashboard</title>
  <style>
    body {{ font-family: system-ui, sans-serif; max-width: 460px; margin: 48px auto; }}
    nav a {{ margin-right: 12px; }}
    label {{ display: block; margin: 14px 0 6px; }}
    input {{ width: 100%; padding: 10px; box-sizing: border-box; }}
    button {{ margin-top: 16px; padding: 10px 16px; }}
    #status {{ margin-top: 18px; font-weight: 700; }}
    footer {{ margin-top: 40px; border-top: 1px solid #ccc; }}
  </style>
</head>
<body>
  <nav>{_NAV}</nav>
  <h1>Sign in</h1>
  <label>Email <input id="email" type="email"></label>
  <label>Password <input id="password" type="password"></label>
  <button id="go" type="button">Sign in</button>
  <p id="status">Not signed in</p>
  {_NEWSLETTER if changed else ""}
  <script>
    document.getElementById('go').addEventListener('click', function () {{
      var email = document.getElementById('email').value;
      document.getElementById('status').textContent = 'Welcome, ' + email;
    }});
  </script>
</body>
</html>"""


class _DemoHandler(BaseHTTPRequestHandler):
    """Serves the sign-in page; ``changed`` flips it to the drifted version."""

    changed = False

    def do_GET(self) -> None:
        body = _page(type(self).changed).encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_: Any) -> None:
        return


class _Printer:
    """Prints the walkthrough and remembers whether any check failed."""

    def __init__(self) -> None:
        self.failed = False
        encoding = sys.stdout.encoding or "utf-8"
        try:
            "✓✗".encode(encoding)
            self._ok, self._bad = "✓", "✗"
        except (UnicodeEncodeError, LookupError):
            self._ok, self._bad = "OK", "FAILED"

    def line(self, text: str = "") -> None:
        print(text, flush=True)

    def check(self, passed: bool, text: str, problem: str = "") -> bool:
        if passed:
            self.line(f"   {self._ok} {text}")
        else:
            self.failed = True
            self.line(f"   {self._bad} {problem or text}")
        return passed


async def _evaluate(bridge: CDPBridge, expression: str) -> Any:
    result = await bridge.send_internal(
        "Runtime.evaluate", {"expression": expression, "returnByValue": True}
    )
    return result.get("result", {}).get("value")


async def _open(bridge: CDPBridge, url: str) -> None:
    await bridge.send_internal("Page.navigate", {"url": url})
    await bridge.wait_for_load(timeout=10.0)


async def _sign_in(browser: TerxActions) -> None:
    """The cold path: what an agent would do the first time."""
    await browser.type_into("textbox", "Email", "email")
    await browser.type_into("textbox", "Password", "password")
    await browser.click("button", "Sign in")
    await browser.wait_for({"text_contains": "Welcome"})


async def _must_not_run(_: TerxActions) -> None:
    raise RuntimeError("expected a replay, but TERX had no matching workflow")


def _stored_actions(audit_dir: Path) -> list[str]:
    """Describe the recorded actions from TERX's own audit record."""
    described: list[str] = []
    for audit_file in sorted(audit_dir.glob("*.jsonl"))[:1]:
        for raw in audit_file.read_text().splitlines():
            record = json.loads(raw)
            if record.get("type") != "frame":
                continue
            state = record["data"]["input_state"]
            params = state.get("cdp_params", {})
            target = params.get("target", {})
            where = f'{target.get("role", "")} "{target.get("label", "")}"'
            value = str(params.get("text", ""))
            described.append(f"{state.get('cdp_method', ''):<11} {where:<20} {value}".rstrip())
    return described


def _files_contain(folder: Path, needles: list[str]) -> bool:
    blobs = [path.read_bytes() for path in folder.rglob("*") if path.is_file()]
    return any(needle.encode() in blob for needle in needles for blob in blobs)


async def _walkthrough(out: _Printer, bridge: CDPBridge, url: str, data_dir: Path) -> None:
    cache = MemoryCache(db_path=data_dir / "cache.db", audit_dir=data_dir / "audit")
    workflow = TerxWorkflow(
        cache=cache,
        bridge=bridge,
        task=TASK,
        scope_id=SCOPE,
        variables=FIRST,
        precondition={"url_contains": url},
        postcondition={"text_contains": "Welcome"},
        replay_timeout=5.0,
    )

    out.line(f"1. Record   A stand-in for your agent signs in as {FIRST['email']}.")
    await _open(bridge, url)
    recorded = await workflow.run(_sign_in)
    status = await _evaluate(bridge, "document.getElementById('status').textContent")
    out.check(
        status == f"Welcome, {FIRST['email']}",
        f'Outcome verified: the page shows "{status}"',
        f"Unexpected page state after recording: {status!r}",
    )
    saved = cache.stats()["total_sequences"] == 1 and not recorded.cache_hit
    out.check(
        saved,
        f"Saved {recorded.commands_recorded} actions. "
        "The password is stored as {{password}}, never its value.",
        "The recording was not saved.",
    )
    out.line()

    out.line(f"2. Replay   Same task, different account: {SECOND['email']}. No agent, no model.")
    await _open(bridge, url)
    started = time.perf_counter()
    replayed = await workflow.run(_must_not_run, variables=SECOND)
    elapsed = time.perf_counter() - started
    report = replayed.report
    out.check(
        replayed.cache_hit and report is not None and report.status == "hit",
        f"Replayed {report.commands_replayed if report else 0} actions in {elapsed:.2f} s "
        "with 0 model calls",
        "The workflow was not replayed.",
    )
    status = await _evaluate(bridge, "document.getElementById('status').textContent")
    out.check(
        status == f"Welcome, {SECOND['email']}",
        f'Outcome verified: the page shows "{status}"',
        f"Unexpected page state after replay: {status!r}",
    )
    out.line()

    out.line('3. Refuse   The site changes: a second "Email" field appears.')
    _DemoHandler.changed = True
    await _open(bridge, url)
    refusal = ""
    try:
        await workflow.run(_must_not_run, variables=SECOND)
    except ReplayRefused as exc:
        refusal = exc.reason
    except RuntimeError as exc:
        refusal = ""
        out.check(False, "", f"Expected a refusal, got: {exc}")
    if refusal:
        out.check(True, f"TERX refused: {refusal}")
        untouched = await _evaluate(
            bridge,
            "document.getElementById('email').value === '' "
            "&& document.getElementById('news').value === '' "
            "&& document.getElementById('status').textContent === 'Not signed in'",
        )
        out.check(
            untouched is True,
            "Nothing was typed or clicked. Your agent takes over from here.",
            "The page was modified before the refusal.",
        )
    elif not out.failed:
        out.check(False, "", "TERX replayed a page it should have refused.")
    out.line()

    out.line("What TERX stored")
    for action in _stored_actions(data_dir / "audit"):
        out.line(f"   {action}")
    leaked = _files_contain(data_dir, [FIRST["password"], SECOND["password"]])
    out.check(
        not leaked,
        "Searched TERX's files for both passwords: not found.",
        "A password was found in TERX's files.",
    )


async def run_demo() -> int:
    """Run the walkthrough; return a process exit code."""
    out = _Printer()
    out.line("TERX demo: record a browser task once, then replay it without a model.")
    out.line("Runs in a throwaway headless browser against a page served from this machine.")
    out.line()

    profile = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
    data = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
    try:
        chrome, cdp_port = await launch_chrome(profile.name)
    except ChromeNotFoundError as exc:
        out.line(f"Cannot run the demo: {exc}")
        profile.cleanup()
        data.cleanup()
        return 1

    _DemoHandler.changed = False
    server = HTTPServer(("127.0.0.1", 0), _DemoHandler)
    Thread(target=server.serve_forever, daemon=True).start()
    url = f"http://127.0.0.1:{server.server_port}/"
    try:
        async with BrowserSession(port=cdp_port) as session:
            await _walkthrough(out, session.bridge(), url, Path(data.name))
    finally:
        stop_chrome(chrome)
        server.shutdown()
        server.server_close()
        profile.cleanup()
        data.cleanup()

    out.line()
    if out.failed:
        out.line("The demo did not behave as expected. Please report it:")
        out.line("https://github.com/ixchio/terx/issues")
        return 1
    out.line("Next: connect TERX to your agent with `terx mcp-config --client cursor`")
    out.line("Docs: https://github.com/ixchio/terx#readme")
    return 0


def main() -> int:
    return asyncio.run(run_demo())


if __name__ == "__main__":
    sys.exit(main())
