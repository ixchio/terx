"""A deliberately hard local eval: realistic page behavior TERX must handle.

Each scenario records a workflow on one page variant, then replays it with new
variable values against a second variant. The harness checks the *actual page
outcome* independently of TERX's own report, so it can tell apart:

- ``replayed``: TERX replayed and the page shows the correct result;
- ``false_refusal``: a correct replay was possible but TERX refused or failed;
- ``wrong_result``: TERX reported a hit but the page shows the wrong result;
- ``correct_refusal``: the page changed in a way that must be refused, and was;
- ``unsafe_replay``: TERX replayed a page it should have refused;
- ``unsupported``: TERX cannot record the workflow at all.

Headline metrics:

- **replay rate**: share of replayable scenarios that replayed correctly;
- **false refusal rate**: share of recordable, replayable scenarios that TERX
  refused or failed;
- **unsafe outcomes**: ``wrong_result`` plus ``unsafe_replay``, which must be 0.

Run with ``terx eval-hard`` or ``python -m terx.evals.hard_suite [--gate]``.
"""

from __future__ import annotations

import argparse
import asyncio
import html
import json
import subprocess
import sys
import tempfile
import time
from collections.abc import Awaitable, Callable
from dataclasses import asdict, dataclass, field
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from threading import Thread
from typing import Any, Literal
from urllib.parse import parse_qs

from terx.cache import MemoryCache
from terx.cdp.bridge import CDPBridge
from terx.cdp.session import BrowserSession
from terx.evals.local_suite import _chrome_binary, _free_port, _wait_for_chrome_debugger
from terx.integrations.workflow import TerxActions, TerxWorkflow

Variant = Literal["cold", "warm"]
Expectation = Literal["replay", "refuse"]
ColdPath = Callable[[TerxActions, dict[str, str], str], Awaitable[None]]

# Which page variant each scenario currently serves. The harness flips this
# between the recording and the replay.
_VARIANTS: dict[str, Variant] = {}


@dataclass(frozen=True)
class Scenario:
    name: str
    description: str
    expect: Expectation
    page: Callable[[Variant], str]
    cold_path: ColdPath
    cold_variables: dict[str, str]
    warm_variables: dict[str, str]
    postcondition: dict[str, str]
    verify: Callable[[str, dict[str, str]], bool] = lambda text, _: True


@dataclass
class ScenarioResult:
    name: str
    description: str
    expect: Expectation
    outcome: str
    detail: str = ""
    warm_status: str | None = None
    commands_recorded: int = 0
    cold_ms: float = 0.0
    warm_ms: float = 0.0
    reasons: list[str] = field(default_factory=list)


# Real pages carry navigation and chrome around the part a workflow touches.
# Without it, changing one element on a one-element page would fail TERX's
# page-similarity check before target resolution is ever exercised.
_SITE_HEADER = (
    "<header><nav>"
    + "".join(
        f'<a href="#{name.lower()}">{name}</a> '
        for name in ("Home", "Orders", "Reports", "Customers", "Settings", "Help")
    )
    + "<label>Site search <input type=search name=site-search></label>"
    + "<button type=button>Notifications</button><button type=button>Profile</button>"
    "</nav></header>"
)


def _doc(body: str, script: str = "") -> str:
    return (
        "<!doctype html><html><head><meta charset=utf-8><title>TERX hard eval</title></head>"
        f"<body>{_SITE_HEADER}<main>{body}<p id=out></p></main><script>{script}</script>"
        "</body></html>"
    )


# ------------------------------------------------------------------ #
# Scenarios                                                             #
# ------------------------------------------------------------------ #


def _submit_navigates_page(_: Variant) -> str:
    return _doc(
        "<form method=post action=/hard/welcome>"
        "<label>Email <input name=email></label>"
        "<label>Password <input name=password type=password></label>"
        "<button>Sign in</button></form>"
    )


async def _submit_navigates(browser: TerxActions, _: dict[str, str], __: str) -> None:
    await browser.type_into("textbox", "Email", "email")
    await browser.type_into("textbox", "Password", "password")
    await browser.click("button", "Sign in")
    await browser.wait_for({"text_contains": "Welcome"})


def _late_render_page(_: Variant) -> str:
    return _doc(
        "<button id=load>Load orders</button><div id=list></div>",
        "document.getElementById('load').onclick = () => setTimeout(() => {"
        "  const b = document.createElement('button'); b.textContent = 'Open order A-1';"
        "  b.onclick = () => { document.getElementById('out').textContent = 'Order A-1 details'; };"
        "  document.getElementById('list').appendChild(b); }, 700);",
    )


async def _late_render(browser: TerxActions, _: dict[str, str], __: str) -> None:
    await browser.click("button", "Load orders")
    await browser.wait_for({"text_contains": "Open order A-1"})
    await browser.click("button", "Open order A-1")
    await browser.wait_for({"text_contains": "Order A-1 details"})


def _controlled_input_page(_: Variant) -> str:
    # Framework-style controlled input: state is the source of truth, the DOM
    # is re-rendered from it, and it starts with a saved draft.
    return _doc(
        "<label>Display name <input id=name></label><button id=save>Save</button>",
        "let state = 'draft text'; const input = document.getElementById('name');"
        "const render = () => { input.value = state; };"
        "input.addEventListener('input', (e) => { state = e.target.value; render(); });"
        "render();"
        "document.getElementById('save').onclick = () => {"
        "  document.getElementById('out').textContent = 'Saved: [' + state + ']'; };",
    )


async def _controlled_input(browser: TerxActions, _: dict[str, str], __: str) -> None:
    await browser.type_into("textbox", "Display name", "display_name")
    await browser.click("button", "Save")
    await browser.wait_for({"text_contains": "Saved:"})


def _shadow_dom_page(_: Variant) -> str:
    return _doc(
        "<x-confirm></x-confirm>",
        "customElements.define('x-confirm', class extends HTMLElement { connectedCallback() {"
        "  const root = this.attachShadow({mode: 'open'});"
        "  root.innerHTML = '<button>Confirm booking</button>';"
        "  root.querySelector('button').onclick = () => {"
        "    document.getElementById('out').textContent = 'Booking confirmed'; }; } });",
    )


async def _shadow_dom(browser: TerxActions, _: dict[str, str], __: str) -> None:
    await browser.click("button", "Confirm booking")
    await browser.wait_for({"text_contains": "Booking confirmed"})


def _iframe_page(_: Variant) -> str:
    inner = html.escape(
        "<label>Coupon code <input id=c></label><button id=a>Apply coupon</button>"
        "<script>document.getElementById('a').onclick = () => {"
        "parent.document.getElementById('out').textContent ="
        " 'Coupon applied: ' + document.getElementById('c').value; };</script>",
        quote=True,
    )
    return _doc(f'<iframe title="Checkout" srcdoc="{inner}"></iframe>')


async def _iframe(browser: TerxActions, _: dict[str, str], __: str) -> None:
    await browser.type_into("textbox", "Coupon code", "coupon")
    await browser.click("button", "Apply coupon")
    await browser.wait_for({"text_contains": "Coupon applied"})


def _translated_label_page(variant: Variant) -> str:
    label = "Sign up" if variant == "cold" else "Register"
    return _doc(
        f"<button data-testid=signup id=b>{label}</button>",
        "document.getElementById('b').onclick = () => {"
        "  document.getElementById('out').textContent = 'Account created'; };",
    )


async def _translated_label(browser: TerxActions, _: dict[str, str], __: str) -> None:
    await browser.click("button", "Sign up")
    await browser.wait_for({"text_contains": "Account created"})


def _duplicate_target_page(variant: Variant) -> str:
    buttons = "<button class=d>Delete draft</button>"
    if variant == "warm":
        buttons += "<button class=d>Delete draft</button>"
    return _doc(
        buttons,
        "document.querySelectorAll('.d').forEach((b) => { b.onclick = () => {"
        "  document.getElementById('out').textContent = 'Draft deleted'; }; });",
    )


async def _delete_draft(browser: TerxActions, _: dict[str, str], __: str) -> None:
    await browser.click("button", "Delete draft")
    await browser.wait_for({"text_contains": "Draft deleted"})


def _target_removed_page(variant: Variant) -> str:
    body = "<button id=b>Delete draft</button>" if variant == "cold" else "<p>Nothing here</p>"
    return _doc(
        body,
        "const b = document.getElementById('b'); if (b) { b.onclick = () => {"
        "  document.getElementById('out').textContent = 'Draft deleted'; }; }",
    )


def _checkbox_page(variant: Variant) -> str:
    checked = " checked" if variant == "warm" else ""
    return _doc(
        f"<label><input type=checkbox id=c{checked}> Email me updates</label>"
        "<button id=s>Save preferences</button>",
        "document.getElementById('s').onclick = () => {"
        "  document.getElementById('out').textContent = 'Preferences saved: updates='"
        "  + (document.getElementById('c').checked ? 'on' : 'off'); };",
    )


async def _checkbox(browser: TerxActions, _: dict[str, str], __: str) -> None:
    await browser.set_checked("checkbox", "Email me updates", True)
    await browser.click("button", "Save preferences")
    await browser.wait_for({"text_contains": "Preferences saved"})


def _select_page(_: Variant) -> str:
    return _doc(
        "<label>Country <select id=s><option>Choose one</option><option>Canada</option>"
        "<option>Mexico</option></select></label><button id=n>Continue</button>",
        "document.getElementById('n').onclick = () => {"
        "  document.getElementById('out').textContent ="
        "  'Country: ' + document.getElementById('s').value; };",
    )


async def _select(browser: TerxActions, _: dict[str, str], __: str) -> None:
    await browser.select_option("combobox", "Country", "country")
    await browser.click("button", "Continue")
    await browser.wait_for({"text_contains": "Country:"})


def _enter_page(_: Variant) -> str:
    return _doc(
        "<form id=f><label>Search records <input id=q></label></form>",
        "document.getElementById('f').onsubmit = (e) => { e.preventDefault();"
        "  document.getElementById('out').textContent ="
        "  'Results for ' + document.getElementById('q').value; };",
    )


async def _enter(browser: TerxActions, _: dict[str, str], __: str) -> None:
    await browser.type_into("textbox", "Search records", "query")
    await browser.press("textbox", "Search records", "Enter")
    await browser.wait_for({"text_contains": "Results for"})


def _url_template_page(_: Variant) -> str:
    return _doc("<p>Order lookup</p>")


async def _url_template(browser: TerxActions, variables: dict[str, str], base: str) -> None:
    # What a caller naturally writes today: a literal URL built from the value.
    await browser.navigate(f"{base}/hard/orders/{variables['order_id']}")
    await browser.wait_for({"text_contains": "Order "})


def _conflicting_signals_page(variant: Variant) -> str:
    # Warm variant: the "Archive" label now sits on the element whose test id
    # used to be "delete". Name and test id point at different buttons.
    archive_id, delete_id = ("archive", "delete") if variant == "cold" else ("delete", "archive")
    return _doc(
        f"<button data-testid={archive_id} class=x>Archive</button>"
        f"<button data-testid={delete_id} class=x>Delete</button>",
        "document.querySelectorAll('.x').forEach((b) => { b.onclick = () => {"
        "  document.getElementById('out').textContent = 'Done: ' + b.textContent; }; });",
    )


async def _conflicting_signals(browser: TerxActions, _: dict[str, str], __: str) -> None:
    await browser.click("button", "Archive")
    await browser.wait_for({"text_contains": "Done:"})


def _drift_and_move_page(variant: Variant) -> str:
    # Warm variant: the label changed AND a new button now precedes it, so the
    # test id alone is not corroborated by the element's position.
    # A realistic toolbar keeps the page similar enough to be the same cached
    # page, so the refusal must come from target resolution, not a cache miss.
    toolbar = "".join(
        f"<button type=button>{n}</button>" for n in ("Bold", "Italic", "Link", "Undo")
    )
    body = (
        f"<button data-testid=signup id=b>Sign up</button>{toolbar}"
        if variant == "cold"
        else f"<button>Learn more</button><button data-testid=signup id=b>Register</button>{toolbar}"
    )
    return _doc(
        body,
        "document.getElementById('b').onclick = () => {"
        "  document.getElementById('out').textContent = 'Account created'; };",
    )


SCENARIOS: list[Scenario] = [
    Scenario(
        "submit_navigates",
        "Form submit loads a new page after a server delay",
        "replay",
        _submit_navigates_page,
        _submit_navigates,
        {"email": "cold@example.com", "password": "cold-secret"},
        {"email": "warm@example.com", "password": "warm-secret"},
        {"text_contains": "Welcome"},
        lambda text, v: f"Welcome, {v['email']}" in text,
    ),
    Scenario(
        "late_render",
        "The next target renders 700 ms after a click",
        "replay",
        _late_render_page,
        _late_render,
        {},
        {},
        {"text_contains": "Order A-1 details"},
    ),
    Scenario(
        "controlled_input",
        "Framework-controlled input that starts with a saved draft",
        "replay",
        _controlled_input_page,
        _controlled_input,
        {"display_name": "Cold Name"},
        {"display_name": "Warm Name"},
        {"text_contains": "Saved:"},
        lambda text, v: f"Saved: [{v['display_name']}]" in text,
    ),
    Scenario(
        "shadow_dom",
        "Button inside an open shadow root",
        "replay",
        _shadow_dom_page,
        _shadow_dom,
        {},
        {},
        {"text_contains": "Booking confirmed"},
    ),
    Scenario(
        "iframe_form",
        "Form inside a same-origin iframe",
        "replay",
        _iframe_page,
        _iframe,
        {"coupon": "COLD10"},
        {"coupon": "WARM20"},
        {"text_contains": "Coupon applied"},
        lambda text, v: f"Coupon applied: {v['coupon']}" in text,
    ),
    Scenario(
        "translated_label",
        "Button label changes; its test id and position do not",
        "replay",
        _translated_label_page,
        _translated_label,
        {},
        {},
        {"text_contains": "Account created"},
    ),
    Scenario(
        "duplicate_target",
        "A second identical button appears: must refuse",
        "refuse",
        _duplicate_target_page,
        _delete_draft,
        {},
        {},
        {"text_contains": "Draft deleted"},
    ),
    Scenario(
        "target_removed",
        "The target disappears: must refuse",
        "refuse",
        _target_removed_page,
        _delete_draft,
        {},
        {},
        {"text_contains": "Draft deleted"},
    ),
    Scenario(
        "checkbox_state",
        "Ensure a checkbox is on when it may already be on",
        "replay",
        _checkbox_page,
        _checkbox,
        {},
        {},
        {"text_contains": "Preferences saved"},
        lambda text, _: "updates=on" in text,
    ),
    Scenario(
        "select_option",
        "Choose a dropdown option from a variable",
        "replay",
        _select_page,
        _select,
        {"country": "Canada"},
        {"country": "Mexico"},
        {"text_contains": "Country:"},
        lambda text, v: f"Country: {v['country']}" in text,
    ),
    Scenario(
        "enter_to_submit",
        "Search submits on Enter; there is no button",
        "replay",
        _enter_page,
        _enter,
        {"query": "contracts"},
        {"query": "invoices"},
        {"text_contains": "Results for"},
        lambda text, v: f"Results for {v['query']}" in text,
    ),
    Scenario(
        "conflicting_signals",
        "Label and test id now point at different buttons: must refuse",
        "refuse",
        _conflicting_signals_page,
        _conflicting_signals,
        {},
        {},
        {"text_contains": "Done:"},
    ),
    Scenario(
        "label_drift_and_move",
        "Label changed and the element moved: must refuse",
        "refuse",
        _drift_and_move_page,
        _translated_label,
        {},
        {},
        {"text_contains": "Account created"},
    ),
    Scenario(
        "select_missing_option",
        "The requested option does not exist: must refuse",
        "refuse",
        _select_page,
        _select,
        {"country": "Canada"},
        {"country": "Atlantis"},
        {"text_contains": "Country:"},
    ),
    Scenario(
        "url_template",
        "Navigate to a URL whose path contains a variable",
        "replay",
        _url_template_page,
        _url_template,
        {"order_id": "A-100"},
        {"order_id": "B-200"},
        {"text_contains": "Order "},
        lambda text, v: f"Order {v['order_id']}: shipped" in text,
    ),
]
_BY_NAME = {scenario.name: scenario for scenario in SCENARIOS}


class HardEvalHandler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        path = self.path.split("?", 1)[0]
        if path.startswith("/hard/orders/"):
            order_id = html.escape(path.rsplit("/", 1)[-1])
            self._send(_doc(f"<h1>Order {order_id}: shipped</h1>"))
            return
        name = path.removeprefix("/hard/")
        scenario = _BY_NAME.get(name)
        if scenario is None:
            self.send_error(404)
            return
        self._send(scenario.page(_VARIANTS.get(name, "cold")))

    def do_POST(self) -> None:
        length = int(self.headers.get("Content-Length", "0"))
        form = parse_qs(self.rfile.read(length).decode())
        time.sleep(0.3)  # A realistic server round trip before the new page.
        email = html.escape(form.get("email", [""])[0])
        self._send(_doc(f"<h1>Welcome, {email}</h1>"))

    def _send(self, body: str) -> None:
        data = body.encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *_: Any) -> None:
        return


# ------------------------------------------------------------------ #
# Harness                                                               #
# ------------------------------------------------------------------ #


async def _page_text(bridge: CDPBridge) -> str:
    result = await bridge.send_internal(
        "Runtime.evaluate", {"expression": "document.body?.innerText || ''", "returnByValue": True}
    )
    value = result.get("result", {}).get("value", "")
    return value if isinstance(value, str) else ""


async def _open(bridge: CDPBridge, url: str) -> None:
    await bridge.send_internal("Page.navigate", {"url": url})
    await bridge.wait_for_load(timeout=10.0)


async def _run_scenario(
    scenario: Scenario, cache: MemoryCache, bridge: CDPBridge, base: str
) -> ScenarioResult:
    result = ScenarioResult(scenario.name, scenario.description, scenario.expect, "unsupported")
    start_url = f"{base}/hard/{scenario.name}"
    workflow = TerxWorkflow(
        cache=cache,
        bridge=bridge,
        task=f"hard eval: {scenario.name}",
        scope_id="hard-eval",
        variables=scenario.cold_variables,
        precondition={"url_contains": f"/hard/{scenario.name}"},
        postcondition=scenario.postcondition,
        replay_timeout=3.0,
    )

    async def cold(browser: TerxActions) -> None:
        await scenario.cold_path(browser, scenario.cold_variables, base)

    _VARIANTS[scenario.name] = "cold"
    await _open(bridge, start_url)
    started = time.perf_counter()
    try:
        recorded = await workflow.run(cold)
    except Exception as exc:  # TERX cannot express or complete this workflow.
        result.detail = f"cold path: {type(exc).__name__}: {exc}"
        return result
    result.cold_ms = round((time.perf_counter() - started) * 1000, 1)
    result.commands_recorded = recorded.commands_recorded
    if recorded.report is None or recorded.report.status != "miss":
        result.reasons = list(recorded.report.refusal_reasons) if recorded.report else []
        result.detail = "cold run was not cacheable"
        return result

    async def must_not_run(_: TerxActions) -> None:
        raise RuntimeError("cache miss on the warm run")

    _VARIANTS[scenario.name] = "warm"
    await _open(bridge, start_url)
    started = time.perf_counter()
    hit = False
    try:
        replayed = await workflow.run(must_not_run, variables=scenario.warm_variables)
        hit = replayed.cache_hit
        result.warm_status = replayed.report.status if replayed.report else None
    except Exception as exc:
        result.detail = f"warm run: {type(exc).__name__}: {exc}"
        result.warm_status = "refused_or_failed"
    result.warm_ms = round((time.perf_counter() - started) * 1000, 1)

    if scenario.expect == "refuse":
        result.outcome = "unsafe_replay" if hit else "correct_refusal"
        return result
    if not hit:
        result.outcome = "false_refusal"
        return result
    text = await _page_text(bridge)
    if scenario.verify(text, scenario.warm_variables):
        result.outcome = "replayed"
    else:
        result.outcome = "wrong_result"
        result.detail = f"page after replay: {text.strip()[:160]!r}"
    return result


def summarize(results: list[ScenarioResult]) -> dict[str, Any]:
    """Compute the headline metrics from scenario outcomes."""
    replayable = [r for r in results if r.expect == "replay"]
    supported_replayable = [r for r in replayable if r.outcome != "unsupported"]
    count = {o: sum(1 for r in results if r.outcome == o) for o in _OUTCOMES}
    replayed = count["replayed"]
    false_refusals = count["false_refusal"]
    return {
        "scenarios": len(results),
        "supported": sum(1 for r in results if r.outcome != "unsupported"),
        "replay_rate": round(replayed / len(replayable), 3) if replayable else None,
        "false_refusal_rate": (
            round(false_refusals / len(supported_replayable), 3) if supported_replayable else None
        ),
        "unsafe_outcomes": count["wrong_result"] + count["unsafe_replay"],
        "outcomes": count,
    }


_OUTCOMES = (
    "replayed",
    "false_refusal",
    "wrong_result",
    "correct_refusal",
    "unsafe_replay",
    "unsupported",
)


async def run_suite(only: list[str] | None = None) -> dict[str, Any]:
    scenarios = [s for s in SCENARIOS if not only or s.name in only]
    http_port, cdp_port = _free_port(), _free_port()
    server = HTTPServer(("127.0.0.1", http_port), HardEvalHandler)
    Thread(target=server.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{http_port}"
    profile = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
    cache_dir = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
    chrome: subprocess.Popen[str] | None = None
    results: list[ScenarioResult] = []
    try:
        chrome = subprocess.Popen(
            [
                _chrome_binary(),
                "--headless=new",
                "--remote-debugging-address=127.0.0.1",
                f"--remote-debugging-port={cdp_port}",
                f"--user-data-dir={profile.name}",
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
        await _wait_for_chrome_debugger(chrome, cdp_port)
        cache = MemoryCache(
            db_path=Path(cache_dir.name) / "cache.db", audit_dir=Path(cache_dir.name) / "audit"
        )
        async with BrowserSession(port=cdp_port) as session:
            bridge = session.bridge()
            for scenario in scenarios:
                results.append(await _run_scenario(scenario, cache, bridge, base))
    finally:
        if chrome is not None:
            if chrome.poll() is None:
                chrome.terminate()
                try:
                    chrome.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    chrome.kill()
            if chrome.stderr is not None:
                chrome.stderr.close()
        server.shutdown()
        profile.cleanup()
        cache_dir.cleanup()
    return {
        "suite": "hard",
        "summary": summarize(results),
        "scenarios": [asdict(result) for result in results],
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="terx eval-hard", description=__doc__.split("\n")[0])
    parser.add_argument("--only", nargs="*", help="Run only these scenario names")
    parser.add_argument(
        "--gate",
        action="store_true",
        help="Exit non-zero on any unsafe outcome or any false refusal",
    )
    args = parser.parse_args(argv)
    report = asyncio.run(run_suite(args.only))
    print(json.dumps(report, indent=2))
    summary = report["summary"]
    if args.gate and (summary["unsafe_outcomes"] or summary["outcomes"]["false_refusal"]):
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
