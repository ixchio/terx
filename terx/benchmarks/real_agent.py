"""
TERX Real Agent Benchmark
=========================
Runs a genuine LLM agent loop (Groq → openai/gpt-oss-120b) against the same
10 tasks as baseline.py. Measures real wall-clock time and real token counts
from API response headers — no modeled constants.

Usage:
    # Option 1: .env file (recommended)
    cp .env.example .env          # fill in GROQ_API_KEY
    python -m terx.benchmarks.real_agent

    # Option 2: inline env var
    GROQ_API_KEY=gsk_... python -m terx.benchmarks.real_agent

    # Option 3: entry point
    terx-bench-real

Get a free Groq API key at https://console.groq.com
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import socket
import subprocess
import tempfile
import time
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from threading import Thread
from typing import Any

# Load .env if present (pip install python-dotenv, or ignored if not installed)
try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

try:
    from groq import Groq
except ImportError:
    Groq = None

from terx.cdp.session import BrowserSession
from terx.cache.cache import MemoryCache, session_for
from terx.dom.extractor import DOMExtractor


# ------------------------------------------------------------------ #
# Config                                                               #
# ------------------------------------------------------------------ #

GROQ_MODEL    = "openai/gpt-oss-120b"
PORT          = 8898
MAX_STEPS     = 12
DEBUG         = os.environ.get("TERX_DEBUG") == "1"

# Groq pricing for openai/gpt-oss-120b (per 1M tokens, June 2026)
PRICE_INPUT_PER_M  = 2.50
PRICE_OUTPUT_PER_M = 10.00

# This is a reasoning model — it uses thinking tokens internally.
# max_completion_tokens must be large enough for reasoning + JSON output.
MAX_TOKENS_PER_CALL = 1024

# ------------------------------------------------------------------ #
# Benchmark HTML (same tasks as baseline.py)                           #
# ------------------------------------------------------------------ #

HTML = r"""<!DOCTYPE html>
<html lang="en">
<head><meta charset="UTF-8"><title>TERX Real Agent Benchmark</title>
<style>
  body { font-family: sans-serif; padding: 24px; background: #f5f5f5; }
  .card { background: white; border-radius: 8px; padding: 24px; margin: 0 auto;
          max-width: 480px; box-shadow: 0 2px 8px rgba(0,0,0,.08); }
  .hidden { display: none; }
  input, select, textarea { display: block; width: 100%; margin: 8px 0 16px;
    padding: 8px; border: 1px solid #ddd; border-radius: 4px; box-sizing: border-box; }
  button { padding: 10px 20px; background: #0070f3; color: white;
           border: none; border-radius: 4px; cursor: pointer; }
  button:disabled { background: #ccc; cursor: default; }
  label { display: block; margin: 8px 0; }
  .success { color: #0a0; font-weight: bold; margin-top: 12px; display: none; }
</style>
</head>
<body>
<div class="card hidden" id="task1">
  <h2>Task 1: User Login</h2>
  <input type="email" placeholder="email@example.com" aria-label="email@example.com">
  <input type="password" placeholder="Password" aria-label="Password">
  <button id="btn1" onclick="done('btn1','ok1')">Login</button>
  <p class="success" id="ok1">✓ Logged in</p>
</div>
<div class="card hidden" id="task2">
  <h2>Task 2: Search and Filter</h2>
  <input type="text" placeholder="Search products..." aria-label="Search products...">
  <select aria-label="Category: All">
    <option>Category: All</option><option>Category: Electronics</option>
  </select>
  <button id="btn2" onclick="done('btn2','ok2')">Search</button>
  <p class="success" id="ok2">✓ Search complete</p>
</div>
<div class="card hidden" id="task3">
  <h2>Task 3: Multi-step Signup</h2>
  <input type="text" placeholder="First Name" aria-label="First Name">
  <input type="text" placeholder="Last Name" aria-label="Last Name">
  <input type="email" placeholder="Email Address" aria-label="Email Address">
  <label><input type="checkbox" aria-label="I agree to terms"> I agree to terms</label>
  <button id="btn3" onclick="done('btn3','ok3')">Sign Up</button>
  <p class="success" id="ok3">✓ Account created</p>
</div>
<div class="card hidden" id="task4">
  <h2>Task 4: E-commerce Product</h2>
  <select aria-label="Size: M"><option>Size: M</option><option>Size: L</option></select>
  <input type="number" min="1" max="99" value="1" aria-label="Quantity">
  <button id="btn4" onclick="done('btn4','ok4')">Add to Cart</button>
  <p class="success" id="ok4">✓ Added to cart</p>
</div>
<div class="card hidden" id="task5">
  <h2>Task 5: Settings Toggles</h2>
  <label><input type="checkbox" aria-label="Enable Notifications"> Enable Notifications</label>
  <label><input type="checkbox" aria-label="Dark Mode"> Dark Mode</label>
  <button id="btn5" onclick="done('btn5','ok5')">Save Settings</button>
  <p class="success" id="ok5">✓ Settings saved</p>
</div>
<div class="card hidden" id="task6">
  <h2>Task 6: Data Table Pagination</h2>
  <label><input type="checkbox" aria-label="Select Row 1"> Select Row 1</label>
  <button id="btn6" onclick="done('btn6','ok6')">Next Page</button>
  <p class="success" id="ok6">✓ Page advanced</p>
</div>
<div class="card hidden" id="task7">
  <h2>Task 7: Support Ticket</h2>
  <input type="text" placeholder="Subject" aria-label="Subject">
  <textarea placeholder="Describe your issue..." aria-label="Describe your issue..."></textarea>
  <button id="btn7" onclick="done('btn7','ok7')">Submit Ticket</button>
  <p class="success" id="ok7">✓ Ticket submitted</p>
</div>
<div class="card hidden" id="task8">
  <h2>Task 8: Fuzzy Search</h2>
  <input type="text" placeholder="Type query..." aria-label="Type query...">
  <button id="btn8" onclick="done('btn8','ok8')">Fuzzy Search</button>
  <p class="success" id="ok8">✓ Search done</p>
</div>
<div class="card hidden" id="task9">
  <h2>Task 9: Profile Update</h2>
  <textarea placeholder="Bio..." aria-label="Bio..."></textarea>
  <select aria-label="USA"><option>USA</option><option>UK</option></select>
  <button id="btn9" onclick="done('btn9','ok9')">Update Profile</button>
  <p class="success" id="ok9">✓ Profile updated</p>
</div>
<div class="card hidden" id="task10">
  <h2>Task 10: Complex Form</h2>
  <input type="text" placeholder="Name" aria-label="Name">
  <label><input type="checkbox" aria-label="Accept all terms"> Accept all terms</label>
  <button id="btn10" onclick="done('btn10','ok10')">Finish Benchmark</button>
  <p class="success" id="ok10">✓ Done!</p>
</div>
<script>
  function done(btnId, okId) {
    document.getElementById(btnId).disabled = true;
    document.getElementById(okId).style.display = 'block';
  }
  function checkHash() {
    var h = window.location.hash.replace('#','') || 'task1';
    document.querySelectorAll('.card').forEach(function(c){ c.classList.add('hidden'); });
    var el = document.getElementById(h);
    if(el) el.classList.remove('hidden');
  }
  window.addEventListener('load', checkHash);
  window.addEventListener('hashchange', checkHash);
</script>
</body></html>"""

TASKS = [
    ("User Login Flow", 1, ["email@example.com", "Password", "Login"], "Logged in"),
    ("Search and Filter", 2, ["Search products...", "Category: All", "Search"], "Search complete"),
    (
        "Multi-step Signup Form",
        3,
        ["First Name", "Last Name", "Email Address", "I agree to terms", "Sign Up"],
        "Account created",
    ),
    ("E-commerce Product Page", 4, ["Size: M", "Quantity", "Add to Cart"], "Added to cart"),
    (
        "Settings Toggle Options",
        5,
        ["Enable Notifications", "Dark Mode", "Save Settings"],
        "Settings saved",
    ),
    ("Data Table Pagination", 6, ["Select Row 1", "Next Page"], "Page advanced"),
    ("Support Ticket Submit", 7, ["Subject", "Describe your issue...", "Submit Ticket"], "Ticket submitted"),
    ("Fuzzy Search Navigation", 8, ["Type query...", "Fuzzy Search"], "Search done"),
    ("Profile Update Flow", 9, ["Bio...", "USA", "Update Profile"], "Profile updated"),
    ("Complex Nested Form", 10, ["Name", "Accept all terms", "Finish Benchmark"], "Done!"),
]

# ------------------------------------------------------------------ #
# Local server                                                          #
# ------------------------------------------------------------------ #

class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/html")
        self.end_headers()
        self.wfile.write(HTML.encode())
    def log_message(self, *_): pass

def start_server(port: int) -> HTTPServer:
    srv = HTTPServer(("127.0.0.1", port), Handler)
    srv.socket.setsockopt(1, 2, 1)   # SO_REUSEADDR
    Thread(target=srv.serve_forever, daemon=True).start()
    return srv

# ------------------------------------------------------------------ #
# LLM Agent                                                            #
# ------------------------------------------------------------------ #

SYSTEM_PROMPT = """\
You are a browser automation agent. Complete the given task step by step.
Each message you receive shows the current page elements. Respond with ONE JSON action.

Available actions:
{"action":"type","element_id":<int>,"text":<str>}   -- type text into an input/textarea
{"action":"click","element_id":<int>}               -- click a button or checkbox
{"action":"done"}                                    -- ONLY when the primary submit button has been clicked

RULES:
- You MUST fill inputs and check checkboxes BEFORE clicking submit.
- Only call {"action":"done"} after you have clicked the final submit button.
- Provide ONLY the raw JSON object. No markdown. No explanation."""


@dataclass
class AgentRun:
    task_name: str
    wall_time_s: float
    input_tokens: int
    output_tokens: int
    steps: int
    cost_usd: float
    success: bool
    error: str = ""


def _cost(in_tok: int, out_tok: int) -> float:
    return (in_tok / 1_000_000) * PRICE_INPUT_PER_M + (out_tok / 1_000_000) * PRICE_OUTPUT_PER_M


async def run_llm_agent(
    bridge,
    client: Any,
    task_name: str,
    task_idx: int,
    url: str,
    expected_text: str,
) -> AgentRun:
    """Real LLM agent loop with conversation history. Measures actual API token counts.
    NOTE: caller must navigate to `url` BEFORE entering session_for, so TERX
    snapshots the correct page on entry."""
    extractor = DOMExtractor()
    # Page is already loaded — caller navigated before session_for entry

    total_input_tokens  = 0
    total_output_tokens = 0
    steps               = 0
    t0 = time.perf_counter()

    # Conversation history so the model sees what it has already done
    history: list[dict] = []

    try:
        for iteration in range(MAX_STEPS):
            # Snapshot current AX tree
            snapshot = await extractor.snapshot(bridge)
            elements_desc = "\n".join(
                f"  id={el.id} role={el.role} label={el.label!r}"
                for el in snapshot.elements
            )

            user_content = (
                f"Task: {task_name}\n"
                f"Step: {iteration + 1}/{MAX_STEPS}\n\n"
                f"Page elements:\n{elements_desc}\n\n"
                f"What is your next action? "
                f"Remember: fill ALL inputs first, then click submit, then say done."
            )

            messages = [
                {"role": "system", "content": SYSTEM_PROMPT},
                *history,
                {"role": "user", "content": user_content},
            ]

            # Real LLM call
            resp = client.chat.completions.create(
                model=GROQ_MODEL,
                messages=messages,
                temperature=1,          # required by this model
                max_completion_tokens=MAX_TOKENS_PER_CALL,
                reasoning_effort="medium",
                stream=False,
            )

            steps += 1
            total_input_tokens  += resp.usage.prompt_tokens
            total_output_tokens += resp.usage.completion_tokens

            content = (resp.choices[0].message.content or "").strip()
            if DEBUG:
                print(f"      [LLM step {steps}] → {content!r}")

            # Add to history so model tracks its own actions
            history.append({"role": "user",      "content": user_content})
            history.append({"role": "assistant",  "content": content})

            # Parse action
            try:
                if content.startswith("```"):
                    content = content.split("\n", 1)[1].rsplit("```", 1)[0].strip()
                action = json.loads(content)
            except Exception:
                break  # unparseable — stop

            act    = action.get("action", "done")
            el_id  = action.get("element_id")

            if act == "done":
                break

            if el_id is None:
                break

            el = snapshot.find_by_id(el_id)
            if el is None:
                break

            if act == "type":
                text = action.get("text", "test")
                await bridge.send("DOM.focus", {"backendNodeId": el.backend_dom_id})
                await asyncio.sleep(0.05)
                await bridge.send("Input.insertText", {"text": text})

            elif act == "click":
                try:
                    box = await bridge.send("DOM.getBoxModel", {"backendNodeId": el.backend_dom_id})
                except Exception:
                    box = None
                if box:
                    c = box["model"]["content"]
                    x = (c[0] + c[2] + c[4] + c[6]) / 4
                    y = (c[1] + c[3] + c[5] + c[7]) / 4
                    for ev in ["mouseMoved", "mousePressed", "mouseReleased"]:
                        await bridge.send("Input.dispatchMouseEvent", {
                            "type": ev, "x": x, "y": y,
                            "button": "left", "clickCount": 1,
                        })
                await asyncio.sleep(0.15)

    except Exception as exc:
        elapsed = time.perf_counter() - t0
        cost    = _cost(total_input_tokens, total_output_tokens)
        return AgentRun(
            task_name=task_name, wall_time_s=elapsed,
            input_tokens=total_input_tokens, output_tokens=total_output_tokens,
            steps=steps, cost_usd=cost, success=False, error=str(exc),
        )

    elapsed = time.perf_counter() - t0
    cost    = _cost(total_input_tokens, total_output_tokens)
    postcondition_ok = await _page_contains(bridge, expected_text)
    return AgentRun(
        task_name=task_name, wall_time_s=elapsed,
        input_tokens=total_input_tokens, output_tokens=total_output_tokens,
        steps=steps, cost_usd=cost, success=postcondition_ok,
        error="" if postcondition_ok else f"Postcondition failed: {expected_text}",
    )


async def _page_contains(bridge, text: str) -> bool:
    result = await bridge.send_internal(
        "Runtime.evaluate",
        {"expression": "document.body?.innerText || ''", "returnByValue": True},
    )
    return text in result.get("result", {}).get("value", "")



# ------------------------------------------------------------------ #
# Warm replay (TERX)                                                   #
# ------------------------------------------------------------------ #

@dataclass
class ReplayRun:
    task_name: str
    wall_time_s: float
    hit: bool


async def run_terx_replay(
    bridge,
    cache: MemoryCache,
    task_name: str,
    url: str,
    expected_text: str,
) -> ReplayRun:
    # Hard reload to get a clean DOM — the cold run may have left buttons disabled
    # or success messages visible, which would change the structural hash.
    await bridge.send("Page.navigate", {"url": "about:blank"})
    await asyncio.sleep(0.1)
    await bridge.send("Page.navigate", {"url": url})
    await bridge.wait_for_load()
    await asyncio.sleep(0.1)  # Let JS hashchange handler run

    t0 = time.perf_counter()
    async with session_for(
        cache,
        bridge,
        task_name,
        postcondition={"text_contains": expected_text},
        redact_secrets=False,
    ) as ctx:
        if ctx.hit:
            await ctx.replay()
    t1 = time.perf_counter()
    return ReplayRun(task_name=task_name, wall_time_s=t1 - t0, hit=ctx.hit)


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


# ------------------------------------------------------------------ #
# Main benchmark loop                                                   #
# ------------------------------------------------------------------ #

async def run():
    if Groq is None:
        raise SystemExit('Install benchmark extras first: pip install "terx[benchmark]"')

    api_key = os.environ.get("GROQ_API_KEY", "").strip()
    if not api_key:
        raise SystemExit("Set GROQ_API_KEY environment variable.")

    client = Groq(api_key=api_key)
    port = int(os.environ.get("TERX_BENCH_PORT") or _free_port())
    cdp_port = int(os.environ.get("TERX_CDP_PORT") or _free_port())
    chrome_binary = _chrome_binary()
    task_limit = int(os.environ.get("TERX_BENCH_TASK_LIMIT") or len(TASKS))
    selected_tasks = TASKS[: max(1, min(task_limit, len(TASKS)))]

    print(f"🤖 Model:  {GROQ_MODEL}")
    print(f"📋 Tasks:  {len(selected_tasks)}")
    print("=" * 64)

    print("\n🚀 Starting local benchmark server...")
    server = start_server(port)

    print("🌐 Launching headless Chrome...")
    user_data_dir = tempfile.TemporaryDirectory()
    cache_dir = tempfile.TemporaryDirectory()
    chrome = subprocess.Popen(
        [
            chrome_binary,
            "--headless=new",
            f"--remote-debugging-port={cdp_port}",
            f"--user-data-dir={user_data_dir.name}",
            "--disable-gpu",
            "--no-sandbox",
            "about:blank",
        ],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    await asyncio.sleep(2)

    cache = MemoryCache(
        db_path=Path(cache_dir.name) / "cache.db",
        vcr_dir=Path(cache_dir.name) / "vcr",
    )

    agent_results: list[AgentRun]  = []
    replay_results: list[ReplayRun] = []

    try:
        async with BrowserSession(port=cdp_port) as session:
            bridge = session.bridge()

            for case_no, (task_name, task_idx, _, expected_text) in enumerate(selected_tasks, 1):
                url = f"http://localhost:{port}/#task{task_idx}"
                print(f"\n▶ [{case_no:02d}/{len(selected_tasks):02d}] {task_name}")

                # --- PHASE 1: Real LLM agent ---
                # CRITICAL: navigate BEFORE session_for so TERX snapshots the
                # correct page DOM on entry (that hash is what gets cached).
                print("   🧠 LLM agent running...", end="", flush=True)

                await bridge.send("Page.navigate", {"url": url})
                await bridge.wait_for_load()

                agent_t0 = time.perf_counter()
                async with session_for(
                    cache,
                    bridge,
                    task_name,
                    postcondition={"text_contains": expected_text},
                    redact_secrets=False,
                ) as _cold_ctx:
                    agent_run = await run_llm_agent(
                        bridge, client, task_name, task_idx, url, expected_text
                    )
                    if not agent_run.success:
                        raise RuntimeError(agent_run.error)
                agent_elapsed = time.perf_counter() - agent_t0

                agent_results.append(agent_run)
                status = "✓" if agent_run.success else "✗"
                print(
                    f" {status} {agent_elapsed:.2f}s | "
                    f"{agent_run.input_tokens + agent_run.output_tokens:,} tokens | "
                    f"${agent_run.cost_usd:.4f} | {agent_run.steps} LLM steps"
                )
                if agent_run.error:
                    print(f"   ⚠ {agent_run.error}")

                # --- PHASE 2: TERX warm replay ---
                print("   ⚡ TERX replay...", end="", flush=True)
                replay = await run_terx_replay(bridge, cache, task_name, url, expected_text)
                replay_results.append(replay)
                hit_str = "HIT ✓" if replay.hit else "MISS ✗"
                print(f" {hit_str} {replay.wall_time_s:.3f}s | 0 tokens | $0.0000")

    finally:
        chrome.terminate()
        try:
            chrome.wait(timeout=5)
        except subprocess.TimeoutExpired:
            chrome.kill()
        server.shutdown()
        user_data_dir.cleanup()
        cache_dir.cleanup()

    # ------------------------------------------------------------------ #
    # Results table                                                        #
    # ------------------------------------------------------------------ #

    print("\n" + "=" * 80)
    print("TERX REAL BENCHMARK RESULTS")
    print(f"Model: {GROQ_MODEL} via Groq API")
    print("=" * 80)

    total_agent_tokens = 0
    total_agent_cost   = 0.0
    total_agent_time   = 0.0
    total_replay_time  = 0.0
    hits = 0

    rows = []
    for a, r in zip(agent_results, replay_results):
        tok  = a.input_tokens + a.output_tokens
        spd  = round(a.wall_time_s / r.wall_time_s, 1) if r.wall_time_s > 0 else float("inf")
        rows.append((a.task_name, a.steps, a.wall_time_s, r.wall_time_s, spd,
                     tok, a.cost_usd, r.hit))
        total_agent_tokens += tok
        total_agent_cost   += a.cost_usd
        total_agent_time   += a.wall_time_s
        total_replay_time  += r.wall_time_s
        if r.hit:
            hits += 1

    avg_spd = round(total_agent_time / total_replay_time, 1) if total_replay_time > 0 else 0

    hdr = (
        f"{'Task':<32} {'Steps':>5} {'Agent':>8} {'Replay':>8} "
        f"{'Speedup':>8} {'Tokens':>8} {'Cost':>9} {'Hit':>4}"
    )
    print(hdr)
    print("-" * len(hdr))
    for task_name, steps, at, rt, spd, tok, cost, hit in rows:
        h = "✓" if hit else "✗"
        print(f"{task_name:<32} {steps:>5} {at:>7.2f}s {rt:>7.3f}s "
              f"{spd:>7.1f}x {tok:>8,} ${cost:>7.4f} {h:>4}")
    print("-" * len(hdr))
    print(f"{'TOTAL / AVERAGE':<32} {'—':>5} {total_agent_time:>7.2f}s "
          f"{total_replay_time:>7.3f}s {avg_spd:>7.1f}x "
          f"{total_agent_tokens:>8,} ${total_agent_cost:>7.4f} "
          f"{hits}/{len(rows)}")

    # Write local benchmark artifact. The canonical public benchmark document is
    # docs/benchmarks.md; local runs should not create a second public file.
    md = _generate_md(rows, total_agent_time, total_replay_time, avg_spd,
                      total_agent_tokens, total_agent_cost, hits)
    benchmark_file = Path(".benchmarks/real_agent_latest.md")
    benchmark_file.parent.mkdir(parents=True, exist_ok=True)
    benchmark_file.write_text(md)
    print(f"\n✅ Saved → {benchmark_file}")
    print(f"   Hit rate: {hits}/{len(rows)} tasks cached")
    print(f"   Total savings: ${total_agent_cost:.4f} → $0.0000 per repeat run")


def _generate_md(rows, total_agent_time, total_replay_time, avg_spd,
                 total_agent_tokens, total_agent_cost, hits):
    lines = [
        "# TERX Real Benchmark Results",
        "",
        "## Methodology",
        "",
        "**This is a real measurement — no modeled constants.**",
        "",
        f"- **LLM agent**: `{GROQ_MODEL}` via Groq API. Real API calls, real token counts from response headers.",
        "- **TERX replay**: Real Chrome CDP replay. Measured wall-clock time end-to-end.",
        "- **Benchmark pages**: Local HTTP server (127.0.0.1). Tasks are form fills, clicks, searches.",
        "- **Cache**: Fresh SQLite per run. LLM agent records on first run, TERX replays on second.",
        "",
        "## Results",
        "",
        "| Task | Steps | LLM Agent | TERX Replay | Speedup | Tokens | Cost | Cache |",
        "|:-----|:-----:|----------:|------------:|--------:|-------:|-----:|:-----:|",
    ]

    for task_name, steps, at, rt, spd, tok, cost, hit in rows:
        h = "✓" if hit else "✗"
        lines.append(
            f"| {task_name} | {steps} | {at:.2f}s | {rt:.3f}s | "
            f"**{spd:.1f}x** | {tok:,} | ${cost:.4f} | {h} |"
        )

    lines += [
        f"| **Total / Average** | — | **{total_agent_time:.2f}s** | **{total_replay_time:.3f}s** | "
        f"**{avg_spd:.1f}x** | **{total_agent_tokens:,}** | **${total_agent_cost:.4f}** | "
        f"**{hits}/{len(rows)}** |",
        "",
        "## Pricing",
        "",
        f"Model: `{GROQ_MODEL}` — ${PRICE_INPUT_PER_M}/1M input, ${PRICE_OUTPUT_PER_M}/1M output.",
        "TERX replay: **$0.0000** — zero LLM calls.",
        "",
        "## Reproduce",
        "",
        "```bash",
        "pip install terx",
        "export GROQ_API_KEY=your_key",
        "python -m terx.benchmarks.real_agent",
        "```",
        "",
        "Source: [`terx/benchmarks/real_agent.py`](terx/benchmarks/real_agent.py)",
    ]
    return "\n".join(lines) + "\n"


def main():
    asyncio.run(run())


if __name__ == "__main__":
    main()
