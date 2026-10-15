"""
generate_for_netra.py

Tests whether the local model can generate WORKING Selenium tests for a
real application (Netra), end to end:

    1. Open the app with Selenium and extract its interactive elements
    2. Ask the local model for a pytest+Selenium script for a scenario,
       grounded ONLY in those real elements
    3. Save the script
    4. Optionally run it immediately and report pass/fail

Why single-shot instead of the agent loop:
The agent loop makes many LLM calls; on local hardware each call costs
seconds, so a script can take minutes. Extracting the elements once and
generating in a single call is far faster AND more reliable — the model
is given the real selectors, so it cannot invent one.

Usage (from the backend folder):
    python generate_for_netra.py --url http://localhost:3000 \
        --scenario "User can type a question into the ask box and submit it"

    python generate_for_netra.py --url http://localhost:3000 \
        --scenario "..." --run          # generate AND execute
    python generate_for_netra.py --url http://localhost:3000 --list-elements
"""

import argparse
import ast
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

from dotenv import load_dotenv

# Import the REAL extraction from the service. This tool previously carried
# its own copy, which drifted: the service was fixed and the tester kept
# reporting the old behaviour, so a genuine fix looked like it had failed.
from service.fast_generation import EXTRACT_JS, extract_multi_state
from selenium import webdriver
from selenium.webdriver.chrome.options import Options

load_dotenv()

OUT_DIR = Path("generated_tests")
OLLAMA_URL = os.environ.get("LLM_API_URL", "http://localhost:11434")
MODEL = os.environ.get("LLM_MODEL", "qwen2.5-coder:14b")


# ───────────────────────── element extraction ─────────────────────────



def make_driver(headed: bool):
    """Build a Chrome driver (Selenium Manager resolves the driver)."""
    o = Options()
    if not headed:
        o.add_argument("--headless=new")
    o.add_argument("--window-size=1400,1000")
    o.add_argument("--no-sandbox")
    o.add_argument("--disable-dev-shm-usage")
    o.add_experimental_option("prefs", {
        "credentials_enable_service": False,
        "profile.password_manager_enabled": False,
        "profile.password_manager_leak_detection": False,
    })
    return webdriver.Chrome(options=o)




def extract_states(url: str, headed: bool = False, max_probes: int = 6) -> dict:
    """
    Capture the page in several states, using the SAME code the product uses.

    Delegates to service.fast_generation.extract_multi_state so this tool can
    never report something different from what generation actually sees.
    """
    d = make_driver(headed)
    try:
        return extract_multi_state(d, url, max_probes=max_probes)
    finally:
        d.quit()



# ───────────────────────────── generation ─────────────────────────────

PROMPT = """You write Python UI tests using pytest and Selenium.

Below is the REAL list of interactive elements found on the page. You MUST
only use selectors that appear in this list — never invent an id, name or
class that is not present.

ELEMENTS (grouped by state — anything under "after clicking X" only
exists AFTER clicking X, so your test must click X first):
{elements}

PAGE URL: {url}

SELECTOR PREFERENCE (some classes are auto-generated and change on every
rebuild, so avoid them where possible):
  1. id
  2. [data-test] / [data-testid]
  3. [aria-label="..."]        <-- stable and semantic, prefer this
  4. visible text
  5. CSS class                 <-- least stable, last resort

SELECTING AN ELEMENT BY ITS VISIBLE TEXT — read this carefully:
CSS cannot select by text. NEVER write :contains(...) — that is jQuery,
not CSS, and Selenium raises InvalidSelectorException immediately.

If an element in the list has no id, no data-testid and no aria-label,
its only identity is its text, so you MUST use XPath:

    (By.XPATH, "//div[@role='menuitem' and normalize-space()='Profile']")
    (By.XPATH, "//*[normalize-space(text())='Chat history']")

Use By.CSS_SELECTOR only for id / class / attribute matches, for example:
    (By.CSS_SELECTOR, "[aria-label='Settings']")


SCENARIO TO TEST:
{scenario}

Write ONE complete Python file that:
- imports os, time, pytest, and the needed selenium modules
- reads the URL from os.environ.get("NETRA_URL", "{url}")
- runs headless unless os.environ.get("HEADED") == "1"
- adds these Chrome options: --window-size=1400,1000, --no-sandbox,
  --disable-dev-shm-usage
- defines exactly one test function whose name starts with test_
- prefers By.ID when the element has an id, else By.CSS_SELECTOR
- uses WebDriverWait with expected_conditions instead of bare sleeps
- always calls driver.quit() in a finally block

SCREENSHOTS — required for the audit trail:
After each meaningful step (page loaded, text entered, element clicked,
assertion point) save a screenshot. Use exactly this pattern:

    import os
    from pathlib import Path
    RUN_ID = os.environ.get("RUN_ID", "manual")
    SHOT_DIR = Path(os.environ.get("SCREENSHOT_DIR", "screenshots")) / RUN_ID
    SHOT_DIR.mkdir(parents=True, exist_ok=True)

    def _step(driver, name):
        driver.save_screenshot(str(SHOT_DIR / f"{name}.png"))

then call _step(driver, "01_loaded"), _step(driver, "02_clicked"), etc.
Also add a small time.sleep when os.environ.get("HEADED") == "1" so a
viewer can follow the run.


ASSERTIONS — this is the most important rule:
You know the page STRUCTURE (the elements above) but NOT how the
application behaves after an action. Never assume what happens after a
submit — do not assume the input clears, that a message bubble appears,
that a response is rendered, or that the URL changes.

Assert ONLY on things you can verify from the elements listed above:
  - an element is displayed / enabled
  - an input accepted the text you typed
    (element.get_attribute("value") == the text you sent)
  - an element is still present and clickable after the action
If the scenario implies an outcome you cannot verify from the element
list, assert the closest verifiable fact instead.

Output ONLY the Python code. No explanation, no markdown fences.
"""


def _call_model(prompt: str) -> str:
    """Send one prompt to the local model and return cleaned code."""
    import requests
    started = time.time()
    r = requests.post(
        f"{OLLAMA_URL}/api/chat",
        json={
            "model": MODEL,
            "messages": [{"role": "user", "content": prompt}],
            "stream": False,
            "keep_alive": "60m",
            "options": {"temperature": 0.1, "num_ctx": 8192, "num_predict": 1200},
        },
        timeout=600,
    )
    r.raise_for_status()
    code = r.json().get("message", {}).get("content", "")
    print(f"  Model responded in {time.time() - started:.1f}s")
    code = re.sub(r"^```(?:python)?\s*", "", code.strip())
    return re.sub(r"\s*```$", "", code).strip()


REPAIR_PROMPT = """The test you wrote FAILED when it was run.

YOUR SCRIPT:
{code}

THE FAILURE:
{error}

Common causes, check these first:

1. InvalidSelectorException -> you used a CSS selector that isn't valid
   CSS, almost always :contains(). CSS CANNOT select by text. Use XPath:
       (By.XPATH, "//div[@role='menuitem' and normalize-space()='Profile']")

2. TimeoutException -> you waited for something that never appears,
   usually an assumed outcome. Assert only what is verifiable: an element
   is displayed/enabled, or an input holds the text you typed.

3. SyntaxError -> the code doesn't parse. Check try/except/finally blocks
   are complete and correctly indented.

Do not assume the input clears, that a response appears, or that the URL
changes.

Output ONLY the corrected Python code. No explanation, no fences.
"""


def repair_script(code: str, error: str) -> str:
    """Feed the real failure back to the model and ask for a fix."""
    print("  Test failed — sending the failure back to the model to repair...")
    return _call_model(REPAIR_PROMPT.format(code=code, error=error[:2500]))


def generate_script(url: str, scenario: str, elements: list[dict]) -> str:
    """
    Ask the local model for a Selenium script grounded in the real elements.

    Returns:
        The generated Python source.
    """
    prompt = PROMPT.format(
        elements=json.dumps(elements, indent=1),
        url=url,
        scenario=scenario,
    )

    print(f"  Asking {MODEL} to write the script (this can take 30-90s locally)...")
    return _call_model(prompt)


# ─────────────────────────────── main ───────────────────────────────


DIAGNOSE_JS = r"""
const target = arguments[0];
const out = [];
document.querySelectorAll('*').forEach(el => {
  const t = (el.innerText || '').trim();
  if (t !== target) return;
  if (el.children.length > 2) return;          // skip wrappers
  const chain = [];
  let n = el, depth = 0;
  while (n && depth < 6) {
    const s = getComputedStyle(n);
    const r = n.getBoundingClientRect();
    chain.push({
      tag: n.tagName.toLowerCase(),
      cls: (typeof n.className === 'string' ? n.className : '').slice(0, 60),
      display: s.display, visibility: s.visibility, opacity: s.opacity,
      maxHeight: s.maxHeight, height: s.height, overflow: s.overflow,
      transform: s.transform === 'none' ? 'none' : s.transform.slice(0, 40),
      pointerEvents: s.pointerEvents,
      clip: s.clipPath === 'none' ? 'none' : s.clipPath.slice(0, 30),
      rect: [Math.round(r.left), Math.round(r.top), Math.round(r.width), Math.round(r.height)],
      offsetParent: n.offsetParent ? n.offsetParent.tagName.toLowerCase() : null,
    });
    n = n.parentElement; depth++;
  }
  const r = el.getBoundingClientRect();
  const cx = r.left + r.width / 2, cy = r.top + r.height / 2;
  let atPoint = null;
  try { const e = document.elementFromPoint(cx, cy); atPoint = e ? e.tagName.toLowerCase() + '.' + String(e.className).slice(0,30) : null; } catch (e) {}
  out.push({ centre: [Math.round(cx), Math.round(cy)], elementAtCentre: atPoint, chain });
});
return out;
"""


def diagnose_element(url: str, text: str, headed: bool = False):
    """
    Print exactly how the page hides (or shows) an element.

    Written because repeated guesses at the hiding mechanism were wrong: a
    dropdown can be hidden by display, visibility, opacity, max-height,
    transform, clip-path or by being covered. This reports the real computed
    values so the visibility check can be based on fact.
    """
    d = make_driver(headed)
    try:
        d.get(url)
        time.sleep(2.5)
        print(f"\n=== how '{text}' is rendered BEFORE any click ===")
        for hit in (d.execute_script(DIAGNOSE_JS, text) or []):
            print(f"  centre={hit['centre']}  elementAtCentre={hit['elementAtCentre']}")
            for i, n in enumerate(hit["chain"]):
                print(f"    {'  '*i}{n['tag']}.{n['cls'][:34]}")
                print(f"    {'  '*i}   display={n['display']} visibility={n['visibility']} "
                      f"opacity={n['opacity']} maxH={n['maxHeight']} h={n['height']} "
                      f"overflow={n['overflow']}")
                print(f"    {'  '*i}   transform={n['transform']} pointer={n['pointerEvents']} "
                      f"clip={n['clip']} rect={n['rect']} offsetParent={n['offsetParent']}")
    finally:
        d.quit()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", default="http://localhost:3000")
    ap.add_argument("--scenario", help="What to test, in plain English")
    ap.add_argument("--name", help="Output filename (default derived from scenario)")
    ap.add_argument("--run", action="store_true", help="Execute the script after generating")
    ap.add_argument("--headed", action="store_true", help="Show the browser")
    ap.add_argument("--list-elements", action="store_true", help="Only print the elements found")
    ap.add_argument("--debug-element", help="Show exactly how the page hides/shows an element, by its text")
    ap.add_argument("--max-repairs", type=int, default=2,
                    help="How many times to feed a failure back to the model (default 2)")
    args = ap.parse_args()

    if args.debug_element:
        diagnose_element(args.url, args.debug_element, headed=args.headed)
        return

    print(f"\n1. Opening {args.url} and extracting elements (probing menus/overlays)...")
    try:
        states = extract_states(args.url, headed=args.headed)
        elements = states
    except Exception as e:
        print(f"\n   Could not open the page: {e}")
        print("   Is the app running at that URL, and is Chrome installed?")
        sys.exit(1)

    total = sum(len(v) for v in states.values())
    print(f"   Found {total} elements across {len(states)} state(s).")
    if args.list_elements or not args.scenario:
        for state, els in states.items():
            print(f"\n   [{state}]")
            for e in els:
                print("   -", json.dumps(e))
        if not args.scenario:
            print("\n   Pass --scenario \"...\" to generate a test.")
        return

    print("\n2. Generating the test script...")
    code = generate_script(args.url, args.scenario, elements)

    # Syntax-check before running. A malformed try/finally is a common model
    # slip, and catching it here costs one call instead of a whole pytest run.
    for i in range(2):
        try:
            ast.parse(code)
            break
        except SyntaxError as se:
            print(f"   Generated code has a syntax error (line {se.lineno}: {se.msg}) — asking the model to fix it...")
            code = repair_script(code, f"SyntaxError on line {se.lineno}: {se.msg}\n{se.text or ''}")
    else:
        print("   Could not get syntactically valid code after 2 attempts.")

    if "def test_" not in code:
        print("\n   The model did not return a usable test function. Raw output:\n")
        print(code[:1500])
        sys.exit(1)

    OUT_DIR.mkdir(exist_ok=True)
    slug = args.name or ("test_netra_" + re.sub(r"[^a-z0-9]+", "_", args.scenario.lower())[:40].strip("_") + ".py")
    if not slug.endswith(".py"):
        slug += ".py"
    path = OUT_DIR / slug
    path.write_text(code, encoding="utf-8")
    print(f"   Saved: {path}")

    if not args.run:
        print(f"\n3. To run it:\n   pytest {path} -v")
        return

    print("\n3. Running it with pytest...")
    env = os.environ.copy()
    env["NETRA_URL"] = args.url
    if args.headed:
        env["HEADED"] = "1"

    def run_once():
        return subprocess.run(["pytest", str(path), "-v", "--tb=short"],
                              capture_output=True, text=True, timeout=300, env=env)

    proc = run_once()
    print(proc.stdout[-2500:])

    # Generate -> verify -> repair. The model knows the page structure but not
    # how the app behaves, so its first assertion is often a guess. Feeding the
    # real failure back usually fixes it in one pass.
    attempt = 1
    while proc.returncode != 0 and attempt <= args.max_repairs:
        print(f"\n   Repair attempt {attempt}/{args.max_repairs}...")
        code = repair_script(path.read_text(encoding="utf-8"), proc.stdout[-2500:])
        if "def test_" not in code:
            print("   Repair did not return a usable test — stopping.")
            break
        path.write_text(code, encoding="utf-8")
        proc = run_once()
        print(proc.stdout[-2500:])
        attempt += 1

    print("\n   RESULT:", "PASSED — the model generated a working test."
          if proc.returncode == 0 else "FAILED — see the output above.")
    print(f"   Script: {path}")


if __name__ == "__main__":
    main()