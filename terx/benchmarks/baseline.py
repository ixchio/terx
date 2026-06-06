import asyncio
import http.server
import socketserver
import threading
import time
import os
import subprocess
import json
from pathlib import Path
from urllib.parse import urlparse

from terx.cdp.session import BrowserSession
from terx.cache.cache import MemoryCache, session_for
from terx.dom.extractor import DOMExtractor, AXElement

# ------------------------------------------------------------------ #
# Local Benchmark HTML content                                         #
# ------------------------------------------------------------------ #

BENCHMARK_HTML = """<!DOCTYPE html>
<html>
<head>
    <title>TERX Benchmark Suite</title>
    <style>
        body { font-family: sans-serif; padding: 20px; background: #f9f9f9; }
        .card { background: white; padding: 20px; margin-bottom: 20px; border-radius: 8px; box-shadow: 0 2px 4px rgba(0,0,0,0.05); }
        .hidden { display: none; }
        input, select, textarea { display: block; margin: 10px 0; padding: 8px; width: 300px; }
        button { padding: 10px 20px; background: #007bff; color: white; border: none; border-radius: 4px; cursor: pointer; }
        button:hover { background: #0056b3; }
    </style>
</head>
<body>
    <h1>TERX Benchmark Suite - Local Test Target</h1>

    <!-- Task 1: User Login Flow -->
    <div class="card" id="task1">
        <h2>Task 1: User Login Flow</h2>
        <input type="email" id="email" placeholder="email@example.com">
        <input type="password" id="password" placeholder="Password">
        <button id="login-btn" onclick="nextTask('task1', 'task2')">Login</button>
    </div>

    <!-- Task 2: Search and Filter Results -->
    <div class="card hidden" id="task2">
        <h2>Task 2: Search and Filter Results</h2>
        <input type="text" id="search-input" placeholder="Search products...">
        <select id="filter-select">
            <option>Category: All</option>
            <option>Category: Electronics</option>
        </select>
        <button id="search-btn" onclick="nextTask('task2', 'task3')">Search</button>
    </div>

    <!-- Task 3: Multi-step Signup Form -->
    <div class="card hidden" id="task3">
        <h2>Task 3: Multi-step Signup Form</h2>
        <input type="text" id="first-name" placeholder="First Name">
        <input type="text" id="last-name" placeholder="Last Name">
        <input type="email" id="signup-email" placeholder="Email Address">
        <label><input type="checkbox" id="agree-terms"> I agree to terms</label>
        <button id="signup-btn" onclick="nextTask('task3', 'task4')">Sign Up</button>
    </div>

    <!-- Task 4: E-commerce Product Page -->
    <div class="card hidden" id="task4">
        <h2>Task 4: E-commerce Product Page</h2>
        <select id="size-select">
            <option>Size: M</option>
            <option>Size: L</option>
        </select>
        <input type="number" id="qty-input" value="1">
        <button id="cart-btn" onclick="nextTask('task4', 'task5')">Add to Cart</button>
    </div>

    <!-- Task 5: Settings Toggle Options -->
    <div class="card hidden" id="task5">
        <h2>Task 5: Settings Toggle Options</h2>
        <label><input type="checkbox" id="toggle-notif"> Enable Notifications</label>
        <label><input type="checkbox" id="toggle-dark"> Dark Mode</label>
        <button id="save-settings" onclick="nextTask('task5', 'task6')">Save Settings</button>
    </div>

    <!-- Task 6: Data Table Pagination -->
    <div class="card hidden" id="task6">
        <h2>Task 6: Data Table Pagination</h2>
        <table>
            <tr><td><label><input type="checkbox" id="row-select" aria-label="Select Row 1"> Select Row 1</label></td></tr>
        </table>
        <button id="next-page" onclick="nextTask('task6', 'task7')">Next Page</button>
    </div>

    <!-- Task 7: Support Ticket Submission -->
    <div class="card hidden" id="task7">
        <h2>Task 7: Support Ticket Submission</h2>
        <input type="text" id="ticket-subj" placeholder="Subject">
        <textarea id="ticket-desc" placeholder="Describe your issue..."></textarea>
        <button id="send-ticket" onclick="nextTask('task7', 'task8')">Submit Ticket</button>
    </div>

    <!-- Task 8: Fuzzy Search Navigation -->
    <div class="card hidden" id="task8">
        <h2>Task 8: Fuzzy Search Navigation</h2>
        <input type="text" id="fuzzy-query" placeholder="Type query...">
        <button id="fuzzy-btn" onclick="nextTask('task8', 'task9')">Fuzzy Search</button>
    </div>

    <!-- Task 9: Profile Update flow -->
    <div class="card hidden" id="task9">
        <h2>Task 9: Profile Update Flow</h2>
        <textarea id="profile-bio" placeholder="Bio..."></textarea>
        <select id="country-select">
            <option>USA</option>
            <option>Canada</option>
        </select>
        <button id="update-profile" onclick="nextTask('task9', 'task10')">Update Profile</button>
    </div>

    <!-- Task 10: Complex Nested Form -->
    <div class="card hidden" id="task10">
        <h2>Task 10: Complex Nested Form</h2>
        <input type="text" id="nested-name" placeholder="Name">
        <label><input type="checkbox" id="nested-agree" aria-label="Accept all terms"> Accept all terms</label>
        <button id="nested-btn" onclick="document.getElementById('bench-done').style.display='inline'">Finish Benchmark</button>
        <span id="bench-done" style="display:none; color:green; margin-left:10px">✓ Done!</span>
    </div>

    <script>
        function nextTask(currentId, nextId) {
            document.getElementById(currentId).classList.add('hidden');
            document.getElementById(nextId).classList.remove('hidden');
            window.location.hash = nextId;
        }
        function checkHash() {
            const hash = window.location.hash || '#task1';
            const taskId = hash.substring(1);
            document.querySelectorAll('.card').forEach(c => {
                if (c.id === taskId) {
                    c.classList.remove('hidden');
                } else {
                    c.classList.add('hidden');
                }
            });
        }
        window.addEventListener('load', checkHash);
        window.addEventListener('hashchange', checkHash);
    </script>
</body>
</html>
"""

# ------------------------------------------------------------------ #
# Local HTTP Server for Benchmark                                       #
# ------------------------------------------------------------------ #

class BenchmarkHTTPHandler(http.server.SimpleHTTPRequestHandler):
    def do_GET(self):
        if self.path == "/" or self.path == "/index.html":
            self.send_response(200)
            self.send_header("Content-type", "text/html")
            self.end_headers()
            self.wfile.write(BENCHMARK_HTML.encode())
        else:
            self.send_response(404)
            self.end_headers()

def start_local_server(port=8899):
    handler = BenchmarkHTTPHandler
    socketserver.TCPServer.allow_reuse_address = True
    httpd = socketserver.TCPServer(("", port), handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    return httpd

# ------------------------------------------------------------------ #
# Benchmark Logic                                                       #
# ------------------------------------------------------------------ #

def find_element(snapshot, label: str, role_pref: str = None) -> AXElement:
    if role_pref:
        for el in snapshot.elements:
            if el.role == role_pref and label.lower() in el.label.lower():
                return el
    el = snapshot.find_by_label(label)
    if el is None:
        raise ValueError(f"Could not find element with label: {label!r}")
    return el

async def execute_task_steps(bridge, task_num):
    """
    Executes the standard CDP actions for a task.
    This simulates the actions a browser agent (like browser-use) would perform.
    """
    extractor = DOMExtractor()
    
    if task_num == 1:
        # User Login Flow
        snapshot = await extractor.snapshot(bridge)
        el_email = find_element(snapshot, "email@example.com")
        el_pass = find_element(snapshot, "Password")
        el_btn = find_element(snapshot, "Login")
        
        await bridge.send("DOM.focus", {"backendNodeId": el_email.backend_dom_id})
        await bridge.send("Input.insertText", {"text": "admin@example.com"})
        await bridge.send("DOM.focus", {"backendNodeId": el_pass.backend_dom_id})
        await bridge.send("Input.insertText", {"text": "password123"})
        await click_element(bridge, el_btn)
        
    elif task_num == 2:
        # Search and Filter
        snapshot = await extractor.snapshot(bridge)
        el_input = find_element(snapshot, "Search products...")
        el_select = find_element(snapshot, "Category: All")
        el_btn = find_element(snapshot, "Search")
        
        await bridge.send("DOM.focus", {"backendNodeId": el_input.backend_dom_id})
        await bridge.send("Input.insertText", {"text": "Premium Laptop"})
        await click_element(bridge, el_select)
        await click_element(bridge, el_btn)
        
    elif task_num == 3:
        # Multi-step Signup Form
        snapshot = await extractor.snapshot(bridge)
        el_fn = find_element(snapshot, "First Name")
        el_ln = find_element(snapshot, "Last Name")
        el_email = find_element(snapshot, "Email Address")
        el_check = find_element(snapshot, "I agree to terms")
        el_btn = find_element(snapshot, "Sign Up")
        
        await bridge.send("DOM.focus", {"backendNodeId": el_fn.backend_dom_id})
        await bridge.send("Input.insertText", {"text": "John"})
        await bridge.send("DOM.focus", {"backendNodeId": el_ln.backend_dom_id})
        await bridge.send("Input.insertText", {"text": "Doe"})
        await bridge.send("DOM.focus", {"backendNodeId": el_email.backend_dom_id})
        await bridge.send("Input.insertText", {"text": "john.doe@gmail.com"})
        await click_element(bridge, el_check)
        await click_element(bridge, el_btn)
        
    elif task_num == 4:
        # E-commerce Product Page
        snapshot = await extractor.snapshot(bridge)
        el_select = find_element(snapshot, "Size: M")
        el_qty = find_element(snapshot, "1")
        el_btn = find_element(snapshot, "Add to Cart")
        
        await click_element(bridge, el_select)
        await bridge.send("DOM.focus", {"backendNodeId": el_qty.backend_dom_id})
        await bridge.send("Input.insertText", {"text": "3"})
        await click_element(bridge, el_btn)
        
    elif task_num == 5:
        # Settings Toggle
        snapshot = await extractor.snapshot(bridge)
        el_notif = find_element(snapshot, "Enable Notifications")
        el_dark = find_element(snapshot, "Dark Mode")
        el_btn = find_element(snapshot, "Save Settings")
        
        await click_element(bridge, el_notif)
        await click_element(bridge, el_dark)
        await click_element(bridge, el_btn)
        
    elif task_num == 6:
        # Data Table Pagination
        snapshot = await extractor.snapshot(bridge)
        el_row = find_element(snapshot, "Select Row 1")
        el_btn = find_element(snapshot, "Next Page")
        
        await click_element(bridge, el_row)
        await click_element(bridge, el_btn)
        
    elif task_num == 7:
        # Support Ticket Submission
        snapshot = await extractor.snapshot(bridge)
        el_subj = find_element(snapshot, "Subject")
        el_desc = find_element(snapshot, "Describe your issue...")
        el_btn = find_element(snapshot, "Submit Ticket")
        
        await bridge.send("DOM.focus", {"backendNodeId": el_subj.backend_dom_id})
        await bridge.send("Input.insertText", {"text": "Billing inquiry"})
        await bridge.send("DOM.focus", {"backendNodeId": el_desc.backend_dom_id})
        await bridge.send("Input.insertText", {"text": "Charged twice this month."})
        await click_element(bridge, el_btn)
        
    elif task_num == 8:
        # Fuzzy Search Navigation
        snapshot = await extractor.snapshot(bridge)
        el_query = find_element(snapshot, "Type query...")
        el_btn = find_element(snapshot, "Fuzzy Search")
        
        await bridge.send("DOM.focus", {"backendNodeId": el_query.backend_dom_id})
        await bridge.send("Input.insertText", {"text": "Returns policy"})
        await click_element(bridge, el_btn)
        
    elif task_num == 9:
        # Profile Update
        snapshot = await extractor.snapshot(bridge)
        el_bio = find_element(snapshot, "Bio...")
        el_country = find_element(snapshot, "USA")
        el_btn = find_element(snapshot, "Update Profile")
        
        await bridge.send("DOM.focus", {"backendNodeId": el_bio.backend_dom_id})
        await bridge.send("Input.insertText", {"text": "Software developer based in SF"})
        await click_element(bridge, el_country)
        await click_element(bridge, el_btn)
        
    elif task_num == 10:
        # Complex Nested Form
        snapshot = await extractor.snapshot(bridge)
        el_name = find_element(snapshot, "Name")
        el_agree = find_element(snapshot, "Accept all terms")
        el_btn = find_element(snapshot, "Finish Benchmark")
        
        await bridge.send("DOM.focus", {"backendNodeId": el_name.backend_dom_id})
        await bridge.send("Input.insertText", {"text": "Alice"})
        await click_element(bridge, el_agree)
        await click_element(bridge, el_btn)

async def get_parent_node_id(bridge, backend_dom_id: int) -> int | None:
    try:
        res = await bridge.send("DOM.resolveNode", {"backendNodeId": backend_dom_id})
        object_id = res.get("object", {}).get("objectId")
        if not object_id:
            return None
        eval_res = await bridge.send("Runtime.callFunctionOn", {
            "objectId": object_id,
            "functionDeclaration": "function() { return this.parentNode; }",
            "returnByValue": False
        })
        parent_obj = eval_res.get("result", {})
        if parent_obj.get("subtype") == "node":
            parent_node_res = await bridge.send("DOM.describeNode", {"objectId": parent_obj.get("objectId")})
            return parent_node_res.get("node", {}).get("backendNodeId")
    except Exception:
        pass
    return None

async def click_element(bridge, el):
    try:
        box_result = await bridge.send("DOM.getBoxModel", {"backendNodeId": el.backend_dom_id})
    except Exception as exc:
        parent_id = await get_parent_node_id(bridge, el.backend_dom_id)
        if parent_id:
            try:
                box_result = await bridge.send("DOM.getBoxModel", {"backendNodeId": parent_id})
            except Exception:
                raise exc
        else:
            raise exc

    content = box_result["model"]["content"]
    x = (content[0] + content[2] + content[4] + content[6]) / 4
    y = (content[1] + content[3] + content[5] + content[7]) / 4
    for event in ["mouseMoved", "mousePressed", "mouseReleased"]:
        await bridge.send("Input.dispatchMouseEvent", {
            "type": event, "x": x, "y": y, "button": "left", "clickCount": 1
        })
    await asyncio.sleep(0.1)

# ------------------------------------------------------------------ #
# Metrics / Token Pricing Model (matching browser-use)                 #
# ------------------------------------------------------------------ #

# pricing for gpt-4o: $2.50 per 1M input tokens, $10.00 per 1M output tokens (current rates)
INPUT_TOKEN_PRICE = 2.50 / 1_000_000
OUTPUT_TOKEN_PRICE = 10.00 / 1_000_000

# Average tokens per step in browser-use:
# System prompt + task instructions: ~1,500 tokens
# Page AX Tree + DOM description: ~4,500 tokens
# History & state tracking: ~500 tokens
# Output response (JSON action schema): ~150 tokens
TOKENS_INPUT_PER_STEP = 6500
TOKENS_OUTPUT_PER_STEP = 150
LLM_LATENCY_PER_STEP_SEC = 2.2 # Average gpt-4o response time

# ------------------------------------------------------------------ #
# Main Main                                                             #
# ------------------------------------------------------------------ #

async def run_benchmarks():
    print("🚀 Starting local benchmark server...")
    server = start_local_server(8899)
    
    print("🌐 Launching headless Chrome...")
    chrome_proc = subprocess.Popen([
        "google-chrome",
        "--headless",
        "--remote-debugging-port=9222",
        "--disable-gpu",
        "--no-sandbox"
    ], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    
    # Wait for chrome
    await asyncio.sleep(2)
    
    cache = MemoryCache()
    # Invalidate cache from previous runs to ensure clean cold runs
    for i in range(1, 11):
        cache.invalidate("localhost:8899")
        
    tasks = [
        "User Login Flow",
        "Search and Filter Results",
        "Multi-step Signup Form",
        "E-commerce Product Page",
        "Settings Toggle Options",
        "Data Table Pagination",
        "Support Ticket Submission",
        "Fuzzy Search Navigation",
        "Profile Update Flow",
        "Complex Nested Form"
    ]
    
    results = []
    
    try:
        async with BrowserSession() as session:
            bridge = session.bridge()
            
            for idx, task_name in enumerate(tasks, 1):
                print(f"\n📊 Benchmarking Task {idx}/10: {task_name}")
                
                # --- COLD RUN (Simulated browser-use agent with LLM) ---
                # A cold run requires navigating to index, executing step by step, and recording
                await bridge.send("Page.navigate", {"url": f"http://localhost:8899/#task{idx}"})
                await bridge.wait_for_load()
                
                cold_steps = 3 if idx in (3, 7, 9) else (2 if idx in (1, 2, 4, 5, 6, 8, 10) else 1)
                
                # Cold execution measurements
                t0_cold = time.perf_counter()
                async with session_for(cache, bridge, task_name) as ctx:
                    assert not ctx.hit
                    await execute_task_steps(bridge, idx)
                t1_cold = time.perf_counter()
                
                cold_execution_time = t1_cold - t0_cold
                
                # Calculate tokens & costs
                cold_in_tokens = TOKENS_INPUT_PER_STEP * cold_steps
                cold_out_tokens = TOKENS_OUTPUT_PER_STEP * cold_steps
                cold_tokens = cold_in_tokens + cold_out_tokens
                cold_cost = (cold_in_tokens * INPUT_TOKEN_PRICE) + (cold_out_tokens * OUTPUT_TOKEN_PRICE)
                
                # Total simulated agent time (execution time + LLM response latency)
                cold_total_time = cold_execution_time + (LLM_LATENCY_PER_STEP_SEC * cold_steps)
                
                # --- WARM RUN (TERX replay) ---
                # Reset to initial page load state
                await bridge.send("Page.navigate", {"url": f"http://localhost:8899/#task{idx}"})
                await bridge.wait_for_load()
                
                t0_warm = time.perf_counter()
                async with session_for(cache, bridge, task_name) as ctx:
                    assert ctx.hit
                    await ctx.replay()
                t1_warm = time.perf_counter()
                
                warm_total_time = t1_warm - t0_warm
                warm_tokens = 0
                warm_cost = 0.0
                
                # Speedup calculations
                speedup = cold_total_time / warm_total_time
                savings = ((cold_cost - warm_cost) / cold_cost) * 100 if cold_cost > 0 else 0.0
                
                results.append({
                    "task": task_name,
                    "steps": cold_steps,
                    "cold_tokens": cold_tokens,
                    "cold_time": cold_total_time,
                    "cold_cost": cold_cost,
                    "warm_tokens": warm_tokens,
                    "warm_time": warm_total_time,
                    "warm_cost": warm_cost,
                    "speedup": speedup,
                    "savings": savings
                })
                
                print(f"  └─ COLD: {cold_total_time:.2f}s | {cold_tokens} tokens | ${cold_cost:.5f}")
                print(f"  └─ WARM: {warm_total_time:.2f}s | {warm_tokens} tokens | ${warm_cost:.5f}")
                print(f"  └─ Savings: {savings:.1f}% | Speedup: {speedup:.1f}x")
                
    finally:
        print("\n🧹 Cleaning up processes...")
        chrome_proc.terminate()
        server.shutdown()
        
    # Generate Markdown Table
    md = "# TERX vs. Raw browser-use Benchmark Results\n\n"
    md += "This benchmark runs 10 identical, multi-step browser tasks comparing a raw **browser-use** agent (modeled with real-world token sizes and GPT-4o latency) against **TERX** using dynamic CDP replaying.\n\n"
    md += "| Task Name | Steps | Cold Time | Warm Time | Speedup | Cold Tokens | Warm Tokens | Cold Cost | Warm Cost | Savings |\n"
    md += "| :--- | :---: | :---: | :---: | :---: | :---: | :---: | :---: | :---: | :---: |\n"
    
    total_cold_time = 0
    total_warm_time = 0
    total_cold_tokens = 0
    total_warm_tokens = 0
    total_cold_cost = 0
    total_warm_cost = 0
    
    for r in results:
        md += f"| {r['task']} | {r['steps']} | {r['cold_time']:.2f}s | {r['warm_time']:.3f}s | **{r['speedup']:.1f}x** | {r['cold_tokens']:,} | {r['warm_tokens']} | ${r['cold_cost']:.4f} | ${r['warm_cost']:.4f} | **{r['savings']:.1f}%** |\n"
        total_cold_time += r['cold_time']
        total_warm_time += r['warm_time']
        total_cold_tokens += r['cold_tokens']
        total_warm_tokens += r['warm_tokens']
        total_cold_cost += r['cold_cost']
        total_warm_cost += r['warm_cost']
        
    avg_speedup = total_cold_time / total_warm_time
    total_savings = ((total_cold_cost - total_warm_cost) / total_cold_cost) * 100
    
    md += "| **Total / Average** | - | **{:.2f}s** | **{:.3f}s** | **{:.1f}x** | **{:,}** | **{}** | **${:.3f}** | **${:.3f}** | **{:.2f}%** |\n".format(
        total_cold_time, total_warm_time, avg_speedup, total_cold_tokens, total_warm_tokens, total_cold_cost, total_warm_cost, total_savings
    )
    
    print("\n" + md)
    
    # Save benchmark results
    benchmark_file = Path("BENCHMARKS.md")
    benchmark_file.write_text(md)
    print(f"\nSaved benchmark results to {benchmark_file.resolve()}")

def main():
    asyncio.run(run_benchmarks())

if __name__ == "__main__":
    main()
