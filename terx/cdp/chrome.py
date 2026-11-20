"""Find and launch a local Chrome-family browser for demos and evals.

TERX normally attaches to a Chrome that the host application already runs.
These helpers exist so `terx demo` and the eval suites can start their own
throwaway headless browser on any developer machine.
"""

from __future__ import annotations

import asyncio
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

CHROME_PATH_ENV = "TERX_CHROME"

# Checked in order on PATH. Edge is Chromium-based and speaks the same protocol.
_PATH_NAMES = (
    "google-chrome",
    "google-chrome-stable",
    "chromium",
    "chromium-browser",
    "chrome",
    "microsoft-edge",
    "microsoft-edge-stable",
    "msedge",
)


class ChromeNotFoundError(RuntimeError):
    """No Chrome, Chromium, or Edge executable could be located."""


def _install_locations() -> list[Path]:
    """Standard install paths that are usually not on PATH (macOS, Windows)."""
    if sys.platform == "darwin":
        apps = (
            "Google Chrome.app/Contents/MacOS/Google Chrome",
            "Chromium.app/Contents/MacOS/Chromium",
            "Microsoft Edge.app/Contents/MacOS/Microsoft Edge",
        )
        roots = (Path("/Applications"), Path.home() / "Applications")
        return [root / app for app in apps for root in roots]
    if sys.platform.startswith("win"):
        programs = (
            "Google/Chrome/Application/chrome.exe",
            "Chromium/Application/chrome.exe",
            "Microsoft/Edge/Application/msedge.exe",
        )
        roots = [
            Path(value)
            for name in ("PROGRAMFILES", "PROGRAMFILES(X86)", "LOCALAPPDATA")
            if (value := os.environ.get(name))
        ]
        return [root / program for program in programs for root in roots]
    return []


def find_chrome() -> str:
    """Return the path of a Chrome-family browser, or raise ChromeNotFoundError.

    ``TERX_CHROME`` overrides the search with an explicit path or command name.
    """
    override = os.environ.get(CHROME_PATH_ENV, "").strip()
    if override:
        resolved = shutil.which(override)
        if resolved:
            return resolved
        if Path(override).is_file():
            return override
        raise ChromeNotFoundError(
            f"{CHROME_PATH_ENV} is set to {override!r}, which is not an executable file"
        )
    for name in _PATH_NAMES:
        binary = shutil.which(name)
        if binary:
            return binary
    for candidate in _install_locations():
        if candidate.is_file():
            return str(candidate)
    raise ChromeNotFoundError(
        "Chrome, Chromium, or Edge was not found. Install one of them, or set "
        f"{CHROME_PATH_ENV} to the browser's path."
    )


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
            find_chrome(),
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
