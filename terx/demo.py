"""Production-style TERX demo: local page, variables, redaction, replay check."""

from __future__ import annotations

import asyncio
import shutil
import subprocess
import tempfile
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from threading import Thread

from terx.cache.cache import MemoryCache, session_for
from terx.cdp.session import BrowserSession
from terx.dom.extractor import DOMExtractor

PORT = 8897
CDP_PORT = 9333

HTML = """<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <title>TERX Demo Login</title>
  <style>
    body { font-family: system-ui, sans-serif; max-width: 420px; margin: 64px auto; }
    label { display: block; margin: 14px 0 6px; }
    input { width: 100%; padding: 10px; box-sizing: border-box; }
    button { margin-top: 16px; padding: 10px 16px; }
    #status { margin-top: 18px; font-weight: 700; }
  </style>
</head>
<body>
  <h1>TERX demo</h1>
  <label>Email</label>
  <input aria-label="Email" id="email" type="email">
  <label>Password</label>
  <input aria-label="Password" id="password" type="password">
  <button aria-label="Login" id="login">Login</button>
  <p id="status">Waiting</p>
  <script>
    document.getElementById('login').addEventListener('click', function () {
      var email = document.getElementById('email').value;
      document.getElementById('status').textContent = 'Welcome ' + email;
    });
  </script>
</body>
</html>"""


class DemoHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/html")
        self.end_headers()
        self.wfile.write(HTML.encode())

    def log_message(self, *_):
        return


def _start_server() -> HTTPServer:
    server = HTTPServer(("127.0.0.1", PORT), DemoHandler)
    Thread(target=server.serve_forever, daemon=True).start()
    return server


def _chrome_binary() -> str:
    for name in ("google-chrome", "chromium", "chromium-browser"):
        binary = shutil.which(name)
        if binary:
            return binary
    raise RuntimeError("Chrome/Chromium not found")


async def _fill_login(bridge, email: str, password: str) -> None:
    snapshot = await DOMExtractor().snapshot(bridge)
    email_el = snapshot.find_by_label("Email")
    password_el = snapshot.find_by_label("Password")
    login_el = snapshot.find_by_label("Login")
    if not email_el or not password_el or not login_el:
        raise RuntimeError("Demo page elements were not found")

    await bridge.send("DOM.focus", {"backendNodeId": email_el.backend_dom_id})
    await bridge.send("Input.insertText", {"text": email})
    await bridge.send("DOM.focus", {"backendNodeId": password_el.backend_dom_id})
    await bridge.send("Input.insertText", {"text": password})
    resolved = await bridge.send("DOM.resolveNode", {"backendNodeId": login_el.backend_dom_id})
    object_id = resolved.get("object", {}).get("objectId")
    await bridge.send(
        "Runtime.callFunctionOn",
        {"objectId": object_id, "functionDeclaration": "function() { this.click(); }"},
    )


async def run_demo() -> None:
    server = _start_server()
    user_data_dir = tempfile.TemporaryDirectory()
    chrome = subprocess.Popen(
        [
            _chrome_binary(),
            "--headless=new",
            f"--remote-debugging-port={CDP_PORT}",
            f"--user-data-dir={user_data_dir.name}",
            "--disable-gpu",
            "--no-sandbox",
            "about:blank",
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    await asyncio.sleep(1.5)

    cache_dir = tempfile.TemporaryDirectory()
    cache = MemoryCache(
        db_path=Path(cache_dir.name) / "cache.db",
        vcr_dir=Path(cache_dir.name) / "vcr",
    )
    url = f"http://127.0.0.1:{PORT}/"

    try:
        async with BrowserSession(port=CDP_PORT) as session:
            bridge = session.bridge()

            for run_no, variables in enumerate(
                [
                    {"email": "demo@example.com", "password": "first-secret"},
                    {"email": "replay@example.com", "password": "second-secret"},
                ],
                1,
            ):
                await bridge.send("Page.navigate", {"url": url})
                await bridge.wait_for_load()

                started = time.perf_counter()
                async with session_for(
                    cache,
                    bridge,
                    "login to demo dashboard",
                    variables=variables,
                    postcondition={"text_contains": "Welcome"},
                ) as ctx:
                    if ctx.hit:
                        await ctx.replay()
                    else:
                        await _fill_login(bridge, variables["email"], variables["password"])

                elapsed_ms = (time.perf_counter() - started) * 1000
                state = "HIT" if ctx.hit else "MISS"
                print(f"run {run_no}: cache {state} in {elapsed_ms:.1f}ms")
                if ctx.ledger:
                    print(ctx.ledger)

        stats = cache.stats()
        print(f"cache stats: {stats}")
        print(f"vcr files: {len(list((Path(cache_dir.name) / 'vcr').glob('*.vcr')))}")
    finally:
        chrome.terminate()
        try:
            chrome.wait(timeout=5)
        except subprocess.TimeoutExpired:
            chrome.kill()
        server.shutdown()
        user_data_dir.cleanup()
        cache_dir.cleanup()


def main() -> None:
    asyncio.run(run_demo())


if __name__ == "__main__":
    main()
