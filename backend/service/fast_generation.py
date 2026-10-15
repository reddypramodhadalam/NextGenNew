"""
service/fast_generation.py

Single-shot survey and script generation, for when the LLM is expensive
per call (a local model on modest hardware).

ARCHITECTURE — "explore once, generate many":
    1. Extract the page's real interactive elements ONCE per URL and
       cache the map.
    2. Propose scenarios in ONE call (element map + any uploaded BRD).
    3. Generate each script in ONE call, grounded in the cached map.

Versus the agent loop (agents/author_agent.py, agents/explorer_agent.py),
which interacts step-by-step and spends one LLM call per step. That loop
discovers state that only appears after interaction (modals, post-login
screens) and verifies each action as it goes — genuinely better coverage,
but N calls per script. On a local model at ~10s/call that is minutes per
script; here it is seconds.

Trade-off to be aware of: single-shot only sees the page as first loaded.
For flows behind a login or a modal, the agent loop remains the better
tool. Choose per application.

Enable with FAST_GENERATION=1 in .env (the orchestrator checks it).
"""

import ast
import json
import logging
import os
import shutil
import re
import time
from pathlib import Path
from typing import Optional

from llm.base import LLMProvider
# The one place the "N. action  →  expected" format is produced. Imported
# rather than reimplemented so a survey-written test case and an imported
# one are byte-identical to SCRIPT_PROMPT.
from service.test_case_import import flatten_steps
from service.generation_contract import (
    render_contract_for_prompt,
    validate_generated_script_navigation,
)

# Off by default: see the comment at the deterministic-compile call site in
# fast_survey(). The crawl trace faithfully records the crawl AGENT's own
# exploration (including backtracking/retries), never the SSO login steps
# (those run outside the agent loop), and captured element names are
# sometimes raw internal labels rather than business-friendly ones. Set
# DETERMINISTIC_TARGET_FLOW_STEPS=1 only once build_scenario_from_trace has
# been improved to filter exploration noise and reconcile names.
_DETERMINISTIC_TARGET_FLOW_STEPS = os.getenv("DETERMINISTIC_TARGET_FLOW_STEPS", "0") == "1"

logger = logging.getLogger(__name__)


# Cached element maps, keyed by URL — the "explore once" part.
_ELEMENT_CACHE: dict[str, list[dict]] = {}


def _wait_for_ready(driver, timeout: int = 15) -> None:
    """
    Wait for document.readyState == 'complete' instead of time.sleep().

    Safer than a fixed sleep: returns as soon as the page is ready and
    only burns the full timeout on genuinely slow pages. Never raises —
    callers proceed with whatever has rendered after the timeout.
    """
    from selenium.webdriver.support.wait import WebDriverWait
    try:
        WebDriverWait(driver, timeout).until(
            lambda d: d.execute_script("return document.readyState") == "complete"
        )
    except Exception as exc:
        logger.debug(
            "_wait_for_ready: readyState timeout after %ds (%s) — proceeding with current render",
            timeout, type(exc).__name__,
        )


EXTRACT_JS = r"""
const out = [];
const sel = [
  'input','textarea','button','a','select',
  '[role=button]','[role=menuitem]','[role=option]','[role=tab]','[role=link]',
  '[contenteditable=true]','li',
  '[class*="menu"] > *','[class*="Menu"] > *',
  '[class*="dropdown"] > *','[class*="Dropdown"] > *',
  '[class*="option"]','[class*="item"]'
].join(', ');

// A React dropdown keeps its items in the DOM permanently and hides them with
// CSS, so "does this element exist" cannot tell us which menu an item belongs
// to. Actual visibility can: after clicking Settings, only the Settings items
// become visible. Without this, "Upload file" and "Profile" looked identical
// and a generated test clicked the wrong menu.
function reallyVisible(el) {
  const s = getComputedStyle(el);
  if (s.visibility === 'hidden' || s.display === 'none') return false;
  if (parseFloat(s.opacity) < 0.05) return false;
  const r = el.getBoundingClientRect();
  if (r.width < 2 || r.height < 2) return false;

  // Ancestors may be the thing that is hidden or collapsed. A dropdown is
  // often kept in the DOM with max-height:0 / transform:scale(0), where the
  // item itself still reports opacity 1 and a non-null offsetParent.
  let p = el.parentElement, depth = 0;
  while (p && depth < 8) {
    const ps = getComputedStyle(p);
    if (ps.visibility === 'hidden' || ps.display === 'none') return false;
    if (parseFloat(ps.opacity) < 0.05) return false;
    const pr = p.getBoundingClientRect();
    if ((pr.height < 2 || pr.width < 2) && ps.overflow !== 'visible') return false;
    p = p.parentElement; depth++;
  }

  // Definitive test: is this element actually the thing at its own centre?
  const cx = r.left + r.width / 2, cy = r.top + r.height / 2;
  if (cx < 0 || cy < 0 || cx > innerWidth || cy > innerHeight) return false;
  const top = document.elementFromPoint(cx, cy);
  if (!top) return false;
  return el === top || el.contains(top) || top.contains(el);
}

const seen = new Set();
document.querySelectorAll(sel).forEach((el) => {
  const text = (el.innerText || '').trim().slice(0, 60) || null;
  const aria = el.getAttribute('aria-label');
  const dt   = el.getAttribute('data-test') || el.getAttribute('data-testid');
  if (!el.id && !aria && !dt && !text) return;
  if (text && el.querySelectorAll('button, a, input, [role=menuitem]').length > 1) return;
  const key = [el.tagName, el.id, aria, dt, text].join('|');
  if (seen.has(key)) return;
  seen.add(key);
  out.push({
    tag: el.tagName.toLowerCase(),
    id: el.id || null,
    name: el.getAttribute('name') || null,
    type: el.getAttribute('type') || null,
    role: el.getAttribute('role') || null,
    placeholder: el.getAttribute('placeholder') || null,
    aria_label: aria || null,
    data_test: dt || null,
    css_class: (el.className && typeof el.className === 'string')
                 ? el.className.split(/\s+/).slice(0, 3).join(' ') : null,
    text: text,
    visible: reallyVisible(el),
  });
});
return out.slice(0, 300);
"""


def extract_element_map(driver, url: str, use_cache: bool = True) -> list[dict]:
    """
    Return the page's visible interactive elements, caching per URL.

    Args:
        driver: A live Selenium WebDriver already on (or able to reach) the URL.
        url: The page URL, used as the cache key.
        use_cache: Reuse a previously extracted map for this URL.

    Returns:
        List of element descriptors (tag, id, name, placeholder, text, ...).
    """
    if use_cache and url in _ELEMENT_CACHE:
        cached = _ELEMENT_CACHE[url]
        n = sum(len(v) for v in cached.values()) if isinstance(cached, dict) else len(cached)
        logger.info("Using cached element map for %s (%d elements)", url, n)
        return _ELEMENT_CACHE[url]

    _wait_for_ready(driver)  # readyState wait replaces fixed time.sleep(2.0)
    try:
        elements = driver.execute_script(EXTRACT_JS) or []
    except Exception as exc:
        logger.error("extract_element_map: EXTRACT_JS failed for %s: %s", url, exc)
        elements = []
    elements = [{k: v for k, v in e.items() if v} for e in elements]
    _ELEMENT_CACHE[url] = elements
    logger.info("Extracted %d interactive elements from %s", len(elements), url)
    return elements




def _signature(el: dict) -> str:
    """Stable identity for an element, used to diff page states."""
    return "|".join(str(el.get(k) or "") for k in
                    ("tag", "id", "name", "aria_label", "data_test", "text", "css_class"))


def extract_multi_state(driver, url: str, max_probes: int = 6) -> dict:
    """
    Capture the page in several states, not just as first loaded.

    Menus, overlays and dropdowns only exist AFTER an interaction, so a
    single extraction misses them — and a model asked to test them will
    invent selectors it has never seen. This clicks the likely triggers
    and records what each one reveals.

    Costs no LLM calls; it is browser work only.

    Args:
        driver: Live WebDriver.
        url: Page URL (reloaded between probes to reset state).
        max_probes: How many buttons to try.

    Returns:
        {"initial": [...], "after clicking 'Settings'": [...], ...}
    """
    driver.get(url)
    _wait_for_ready(driver)
    raw = driver.execute_script(EXTRACT_JS) or []

    # Only what a user can actually see belongs to the initial state. Hidden
    # dropdown items are recorded separately, against the control that reveals
    # them — otherwise every menu item looks alike and a generated test opens
    # the wrong menu.
    initial = [{k: v for k, v in e.items() if v and k != "visible"}
               for e in raw if e.get("visible")]
    hidden = [e for e in raw if not e.get("visible")]
    logger.info("Extraction: %d element(s) visible, %d hidden%s",
                len(initial), len(hidden),
                (" (hidden: " + ", ".join(
                    (h.get("text") or h.get("aria_label") or "?") for h in hidden[:8]) + ")")
                if hidden else "")
    states = {"initial": initial}
    visible_now = {_signature(e) for e in initial}

    # Navigation triggers: not just buttons. ETQ's menu is entirely anchor
    # links; tabs use [role=tab]; menu items use [role=menuitem]. The original
    # 'button only' filter missed all of them.
    triggers = [
        e for e in initial
        if e.get("tag") in ("button", "a")
        or e.get("role") in ("tab", "menuitem", "link")
    ][:int(os.environ.get("SURVEY_MAX_PROBES", str(max_probes)))]

    for trig in triggers:
        label = trig.get("aria_label") or trig.get("text") or trig.get("css_class") or "button"
        try:
            driver.get(url)          # reset to a known state
            _wait_for_ready(driver, timeout=5)
            el = _find_element(driver, trig)
            if el is None:
                continue
            el.click()
            _wait_for_ready(driver, timeout=5)   # let the overlay render

            after_raw = driver.execute_script(EXTRACT_JS) or []
            after_visible = [{k: v for k, v in e.items() if v and k != "visible"}
                             for e in after_raw if e.get("visible")]

            # What this control made VISIBLE, not merely what exists.
            revealed = [e for e in after_visible if _signature(e) not in visible_now]
            if revealed:
                states[f"after clicking '{label}'"] = revealed
                logger.info("Probe '%s' revealed %d element(s): %s", label, len(revealed),
                            ", ".join((r.get("text") or r.get("aria_label") or "?")
                                      for r in revealed[:5]))
        except Exception as e:      # a probe failing must never break extraction
            logger.debug("Probe '%s' skipped: %s", label, e)

    total = sum(len(v) for v in states.values())
    logger.info("Multi-state extraction: %d states, %d elements total", len(states), total)
    return states


def _find_element(driver, desc: dict):
    """Locate an element from its descriptor, trying the most stable attribute first."""
    from selenium.webdriver.common.by import By
    attempts = []
    if desc.get("id"):
        attempts.append((By.ID, desc["id"]))
    if desc.get("data_test"):
        attempts.append((By.CSS_SELECTOR, f'[data-test="{desc["data_test"]}"]'))
    if desc.get("aria_label"):
        attempts.append((By.CSS_SELECTOR, f'[aria-label="{desc["aria_label"]}"]'))
    if desc.get("css_class"):
        first = desc["css_class"].split()[0]
        attempts.append((By.CSS_SELECTOR, f'.{first}'))
    for by, val in attempts:
        try:
            els = driver.find_elements(by, val)
            if els:
                logger.debug("_find_element: located %r via %s=%r", desc.get("text") or desc.get("aria_label"), by, val)
                return els[0]
        except Exception as exc:
            logger.debug("_find_element: attempt failed by=%s val=%r  %s", by, val, exc)
            continue
    logger.debug("_find_element: could not locate element %r", desc)
    return None


def clear_element_cache(url: Optional[str] = None) -> None:
    """Drop a cached map (or all of them) — used after a UI change."""
    if url:
        _ELEMENT_CACHE.pop(url, None)
    else:
        _ELEMENT_CACHE.clear()



def _get_map(driver, url: str, refresh: bool = False):
    """
    Return the multi-state element map for a URL.

    "Explore once, generate many": the map is extracted once and reused for
    every script, which is what makes generation cheap. A survey passes
    refresh=True so it always sees the page as it is now — a cached map from
    earlier in the process could otherwise outlive a change to the page or to
    the extractor itself.
    """
    if refresh:
        _ELEMENT_CACHE.pop(url, None)
    if url in _ELEMENT_CACHE:
        return _ELEMENT_CACHE[url]
    states = extract_multi_state(driver, url)
    _ELEMENT_CACHE[url] = states
    return states


# ─────────────────────────── survey (scenarios) ───────────────────────────

# How many requirement chunks a single survey will process. A cap rather
# than a time limit on purpose: a time cutoff would stop mid-document and
# drop whatever came after it silently, which is the one outcome this whole
# design exists to prevent. With a count cap we know exactly which
# requirements were not surveyed, and can say so.
SURVEY_MAX_CHUNKS = int(os.environ.get("SURVEY_MAX_CHUNKS", "40"))

# Roughly how much requirement text to put in one call. Sized for a 4096
# token context shared with the element map and the reply — see
# llm/ollama_provider.py for why that budget is what it is.
CHUNK_CHARS = int(os.environ.get("SURVEY_CHUNK_CHARS", "1200"))

# Token budget for each survey LLM call. 2000 was sized for 3-step test cases;
# 8 test cases with 8 steps each requires ~3500 tokens. Configurable via
# SURVEY_NUM_PREDICT for environments with different model context windows.
_SURVEY_NUM_PREDICT = int(os.environ.get("SURVEY_NUM_PREDICT", "3500"))

# Default test-case count for the no-BRD (screen-only) survey path.
# The BRD path writes as many cases as the requirement warrants — this only
# applies when no document is uploaded and the agent surveys the screen directly.
_SURVEY_MAX_CASES_DEFAULT = int(os.environ.get("SURVEY_MAX_CASES_PER_REQUIREMENT", "8"))

logger.debug(
    "fast_generation: survey settings  num_predict=%d  max_cases_no_brd=%d",
    _SURVEY_NUM_PREDICT, _SURVEY_MAX_CASES_DEFAULT,
)

# Requirement identifier regex — broad enough to match any application's format.
# Examples this must match: NET-020, REQ_14, BXU_RPT1_GEN5_V2.0, ISO-13485-REQ-001
#
# Pattern: uppercase letter start, then word chars / underscores / hyphens / dots,
# up to 50 chars total. A post-match digit check (in _split_requirements) filters
# out plain words like NOTE or SCOPE that match the shape but are not IDs.
_REQ_ID_RE = re.compile(
    r"^[ \t]*([A-Z][A-Z0-9_.\-]{2,50})(?=\s|$)",
    re.MULTILINE,
)

# Boilerplate section headings to strip before chunking.
# These sections (title page, glossary, change history etc.) contain no
# testable requirements. Sending them to the LLM wastes calls and produces
# nonsensical test cases like "Verify that the document follows change control".
#
# Pattern: optional heading number, then one of the skip headings, then the
# section body up to the next heading or end of text.
_SKIP_SECTION_RE = re.compile(
    r"(?:(?<=\n)|\A)[ \t]*(?:\d+\.?\s+)?"
    r"(?:scope|introduction|purpose|glossary|abbreviations|acronyms|"
    r"document[ \t]+control|change[ \t]+history|revision[ \t]+history|"
    r"table[ \t]+of[ \t]+contents|references|appendix|disclaimer|background)"
    r"[ \t]*\n"
    r".*?"
    r"(?=(?:\n[ \t]*(?:\d+\.?\s+)?\S)|\Z)",
    re.IGNORECASE | re.DOTALL,
)

# Numbered headings: "3. Search", "4.2.1 Export", "3 Search".
# The trailing dot must be optional AND not required to be followed by a
# digit — "1. Search" is the commonest heading style in a Word-authored BRD,
# and a regex that only accepted "1.1 Search" silently swallowed every
# top-level section into the one before it.
_HEADING_RE = re.compile(r"^[ \t]*(\d+(?:\.\d+)*)\.?[ \t]+(\S.*)$", re.M)


SURVEY_PROMPT = """You are a senior QA engineer writing test cases from a
requirements document.

THE REQUIREMENT DECIDES WHAT IS TESTED.
The element list below decides only whether a step can be AUTOMATED.
Never drop a requirement because the screen appears to lack the control for
it — write the test case anyway and mark the step that has no matching
element. A requirement nobody can test is a finding, not something to hide.

REQUIREMENT(S) UNDER TEST:
__REQUIREMENTS__

ELEMENTS AVAILABLE ON THE SCREEN (for writing runnable steps only).
Anything under "after clicking X" only exists once X has been clicked:
__ELEMENTS__

COVER THIS REQUIREMENT PROPERLY: the behaviour it states, AND the realistic
ways it can fail. Real requirements documents rarely spell out every
negative or edge case — a good tester reasons them out. Do that here, and be
honest about which cases came from the document versus your own reasoning.

  POSITIVE — always. Do what the requirement describes with valid input and
    confirm its acceptance criterion. If the requirement itself describes a
    "does nothing / is rejected" behaviour (e.g. "submitting an empty
    question has no effect"), verifying THAT behaviour is its positive case.

  NEGATIVE — how the behaviour fails on wrong or missing input. Reason from
    the TYPE of behaviour even when the requirement does not spell it out:
      submit / send  -> empty submit, whitespace only, submit while a prior
                        request is still pending
      text input     -> invalid characters, extremely long input
      login / auth   -> wrong credentials, locked or expired session
      file attach    -> unsupported type, oversized file
    Confirm the system fails GRACEFULLY (an error is shown or the action is
    blocked) — never assert the wrong input succeeds.

  EDGE / BOUNDARY — the limits of valid input: at the boundary and just past
    it (max length accepted, one over rejected), empty-but-allowed, special
    characters, very large values.

DERIVATION — label EVERY test case honestly:
  "stated"   — the requirement text or its acceptance criterion explicitly
               describes this case, including a limit written in the text
               ("up to 500 characters"). The positive case is almost always
               "stated".
  "inferred" — YOU reasoned this case from the behaviour type; the
               requirement did not state it. Most negatives and edges on a
               thin requirement are "inferred". Never mark an inferred case
               "stated".

Skip negatives and edges ONLY when the requirement has no input and no
failure mode at all — a pure "X is displayed" or "this control is present".
Those get one positive case (derivation "stated") and nothing else. Do not
manufacture failure modes for a display-only requirement.

Write as many test cases as the requirement needs to be properly covered —
one positive, plus the negatives and edges that realistically apply to its
behaviour. Do not pad with near-identical cases, but do not artificially
limit coverage either.

Each test case is ONE verifiable behaviour, carried out as an ordered list
of STEPS. Each step is one action and the result it should produce. Write
as many steps as the scenario requires — never compress multiple actions
into one step, and never pad with steps that add no new verification.
Set each test case's "category" to the angle it covers
(positive / negative / edge / boundary).

For each STEP:
  "step"            what a tester DOES. Imperative, one action.
  "expected"        what should then be TRUE. Never leave this blank.
  "element_hint"    the identifying attribute of the element the ACTION acts
                    on, copied EXACTLY from the list above (an id, an
                    aria-label, a placeholder, or the visible text). null only
                    if the action touches nothing ("wait 5 seconds").
  "matched"         false if the ACTION needs a control that is NOT in the
                    list above. Still write the step. Never invent a selector,
                    and never reword the requirement to fit what exists.
  "data_input"      the business key for the value to type or select, e.g.
                    "username", "password", "patient_id", "report_date".
                    null if the step types or selects nothing.
                    Never put a literal value here — always a key name.
  "wait_condition"  what to wait for after this action before the next step
                    can proceed. Exactly one of:
                    "url_change"       — the URL changes (submit, navigate)
                    "element_visible"  — a new element appears (modal, panel)
                    "none"             — observation or wait step; nothing to wait for

BOTH element_hint AND matched describe the ACTION, never the expected
result. "Click the Login button" acts on the login button, so its hint is
that button — even when the expected result is an error message that does
not appear anywhere in the list. An outcome you cannot see on the page is
normal and is not a missing control. Always fill in element_hint when the
action touches a real element.

SCENARIO: a SHORT feature name, 2-4 words. Not a sentence.
      good: "Settings menu"     bad: "Verify that clicking the settings..."
TITLE: SHORT and specific, under 60 characters. No "Verify that" prefix.
      good: "Profile is reachable from the settings menu"
category: exactly one of positive, negative, edge, boundary, security
derivation: exactly one of stated, inferred (stated = written in the
      requirement text; inferred = you reasoned it from the behaviour type)
priority: exactly one of P0, P1, P2, P3
      (P0 breaks the core purpose of the screen, P3 is cosmetic)
requirement_id: the ID of the requirement above, or null if it has none.
preconditions: list of strings — what must be true BEFORE step 1. Be
      specific and actionable. Always include authentication state and the
      starting screen. Include required data only when the scenario depends
      on it. Examples:
        "User is authenticated as a standard FAS user"
        "User is on the Initiate Escalation page"
        "An approved escalation record exists in the system"

Return ONLY JSON, no prose, no markdown fences:
[
 {
  "requirement_id": "NET-020",
  "scenario": "Settings menu",
  "test_cases": [
   {
    "title": "Profile is reachable from the settings menu",
    "category": "positive",
    "derivation": "stated",
    "priority": "P1",
    "preconditions": [
      "User is authenticated",
      "User is on the application home screen"
    ],
    "steps": [
     {"step": "Click the Settings button",
      "expected": "The settings menu opens",
      "element_hint": "aria-label='Settings'", "matched": true,
      "data_input": null, "wait_condition": "element_visible"},
     {"step": "Click Profile in the menu",
      "expected": "The profile view is shown",
      "element_hint": "Profile", "matched": true,
      "data_input": null, "wait_condition": "url_change"}
    ]
   }
  ]
 }
]
"""


def _split_requirements(text: str) -> list[dict]:
    """
    Break a requirements document into individually surveyable pieces.

    Three tiers, tried in order, because BRDs in the wild are written three
    different ways:

      1. Requirement IDs at the start of a line (NET-020, REQ_14). This is
         the tier that gives real traceability — the id travels with every
         test case produced from it.
      2. Numbered headings (3.1 Search). No id, but a real section boundary
         and a usable label.
      3. Paragraph windows. Guarantees termination on unstructured prose,
         which is what most "requirements" documents actually are.

    Replaces the old document_context[:4000] truncation, which silently
    discarded everything past the first few pages.

    Args:
        text: The extracted document text.

    Returns:
        [{"id": str|None, "label": str, "text": str}, ...] in document order.
    """
    if not text or not text.strip():
        return []

    # Strip boilerplate sections (glossary, change history, ToC etc.) before
    # any matching. These sections produce nonsensical test cases and waste
    # LLM calls. Log how much was stripped so the behaviour is auditable.
    original_len = len(text)
    text = _SKIP_SECTION_RE.sub("\n", text).strip()
    stripped = original_len - len(text)
    if stripped > 0:
        logger.debug(
            "_split_requirements: stripped %d char(s) of boilerplate sections",
            stripped,
        )

    def _cut(matches, label_of) -> list[dict]:
        out = []
        for i, m in enumerate(matches):
            start = m.start()
            end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
            body = text[start:end].strip()
            if body:
                out.append({"id": m.group(1), "label": label_of(m), "text": body})
        return out

    # Broad ID pattern: keep only matches that contain at least one digit so
    # plain words like NOTE or SCOPE (valid shape but not requirement IDs) are
    # filtered out. This supports any application's ID format without
    # hardcoding segment counts or separator characters.
    ids = [
        m for m in _REQ_ID_RE.finditer(text)
        if any(c.isdigit() for c in m.group(1))
    ]
    if ids:
        chunks = _cut(ids, lambda m: m.group(1))
        logger.info("BRD split by requirement id: %d requirement(s)", len(chunks))
        return chunks

    heads = list(_HEADING_RE.finditer(text))
    if heads:
        chunks = _cut(heads, lambda m: f"{m.group(1)} {m.group(2)[:50]}")
        for c in chunks:
            c["id"] = None          # a heading number is not a requirement id
        logger.info("BRD split by numbered heading: %d section(s)", len(chunks))
        return chunks

    # Paragraph windows — pack blank-line-separated blocks up to CHUNK_CHARS.
    paragraphs = []
    for para in re.split(r"\n\s*\n", text):
        para = para.strip()
        if not para:
            continue
        # A single paragraph can exceed the budget on its own — a wall-of-text
        # requirements section with no blank lines is common. Breaking only
        # BETWEEN paragraphs would emit a chunk too large for the context
        # window, and the model would silently see a truncated requirement.
        # Split on sentence ends where possible, mid-text only if it must.
        while len(para) > CHUNK_CHARS:
            cut = para.rfind(". ", 0, CHUNK_CHARS)
            cut = cut + 1 if cut > CHUNK_CHARS // 2 else CHUNK_CHARS
            paragraphs.append(para[:cut].strip())
            para = para[cut:].strip()
        if para:
            paragraphs.append(para)

    chunks, buf = [], ""
    for para in paragraphs:
        if buf and len(buf) + len(para) > CHUNK_CHARS:
            chunks.append({"id": None, "label": f"part {len(chunks) + 1}", "text": buf})
            buf = para
        else:
            buf = f"{buf}\n\n{para}" if buf else para
    if buf:
        chunks.append({"id": None, "label": f"part {len(chunks) + 1}", "text": buf})
    logger.info("BRD split into %d paragraph window(s)", len(chunks))
    return chunks


def _pack_chunks(chunks: list[dict], limit: int = CHUNK_CHARS) -> list[list[dict]]:
    """
    Group small adjacent requirements into one call, keeping long ones alone.

    One call per requirement is the safe default, but a document of one-line
    requirements would then cost dozens of calls to say very little. Packing
    keeps the call count proportional to the CONTENT rather than to how
    finely the document happens to be numbered.
    """
    batches, current, size = [], [], 0
    for c in chunks:
        n = len(c["text"])
        if current and size + n > limit:
            batches.append(current)
            current, size = [c], n
        else:
            current.append(c)
            size += n
    if current:
        batches.append(current)
    return batches


def _compact_elements(states) -> str:
    """
    Render the element map as one line per element instead of indented JSON.

    Not a cosmetic saving. json.dumps(states, indent=1) is by far the largest
    thing in the survey prompt, and at a 4096 token context it crowds out the
    requirement text — the model then writes test cases for a requirement it
    only half saw. Same information, roughly a fifth of the tokens.

    SCRIPT_PROMPT deliberately keeps the full JSON: it needs exact attribute
    fidelity to build selectors, and it only ever handles one scenario, so it
    has the budget to spare.
    """
    if isinstance(states, list):                  # pre-multi-state shape
        states = {"initial": states}

    lines = []
    for state, elements in (states or {}).items():
        lines.append(f"{state}:")
        for e in elements or []:
            bits = [str(e.get("tag") or "?")]
            if e.get("id"):
                bits.append(f"#{e['id']}")
            if e.get("data_test"):
                bits.append(f"data-test={e['data_test']}")
            if e.get("role"):
                bits.append(f"role={e['role']}")
            if e.get("aria_label"):
                bits.append(f'aria="{e["aria_label"]}"')
            if e.get("placeholder"):
                bits.append(f'placeholder="{e["placeholder"]}"')
            if e.get("name"):
                bits.append(f"name={e['name']}")
            if e.get("text"):
                bits.append(f'text="{e["text"][:40]}"')
            lines.append("  " + "  ".join(bits))
    return "\n".join(lines)


# Attribute names as they appear in a CSS selector, mapped to the key the
# element map stores them under.
_ATTR_KEYS = {
    "id": "id", "name": "name", "placeholder": "placeholder",
    "aria-label": "aria_label", "data-test": "data_test",
    "data-testid": "data_test", "class": "css_class",
}


def _validate_selectors(code: str, states) -> list[str]:
    """
    Find selectors in generated code that no element on the page could match.

    The prompt already forbids inventing attributes, with a worked example
    about aria-label, and a local model still writes
    [aria-label='Username'] for an input whose only identity is
    id=user-name / placeholder=Username. The instruction is not enough, so
    the claim is checked against the map instead of trusted.

    Only flags selectors that are definitely wrong — an attribute/value pair
    no element has. Anything it cannot interpret (XPath, compound CSS) is
    left alone, because a false accusation costs a repair call and can talk
    the model out of a selector that was right.

    Args:
        code: The generated script.
        states: The element map, keyed by page state. Can be:
            - A list of elements
            - A dict {state_name: [elements]} for multi-state
            - The full crawled map_data dict {"base_url": ..., "pages": [...]}

    Returns:
        Human-readable problems, each naming a better selector where one
        exists. Empty when nothing is provably wrong.
    """
    # SSO preamble selectors target Azure Entra pages, not the app DOM map.
    # Exclude that block from map-based selector checks to avoid false "impossible"
    # reports for loginfmt/passwd/idSIButton9.
    pre, _sso_block, post = _split_sso_preamble(code)
    code_to_scan = pre + post

    # Handle the full crawled map_data structure (same as _autofix_selectors)
    if isinstance(states, dict) and "pages" in states:
        all_elements = []
        for page in (states.get("pages") or []):
            page_elements = page.get("elements", [])
            if page_elements:
                all_elements.extend(page_elements)
            page_states = page.get("states", {})
            for state_elements in (page_states or {}).values():
                if state_elements:
                    all_elements.extend(state_elements)
        states = {"all": all_elements} if all_elements else {}
    elif isinstance(states, list):
        states = {"initial": states}
    elements = [e for group in (states or {}).values() for e in (group or [])]
    if not elements:
        return []

    problems = []
    seen = set()

    def _known(attr_key: str, value: str) -> bool:
        return any(str(e.get(attr_key) or "").strip().lower() == value.lower()
                   for e in elements)

    def _suggest(value: str) -> str:
        """
        A valid CSS selector for whatever element the value was pointing at.

        Returns CSS rather than a (By.X, "...") tuple so the result can be
        substituted straight into the existing string literal — the caller
        repairs the selector without having to rewrite the call around it.
        """
        for e in elements:
            for key in ("id", "data_test", "placeholder", "name", "aria_label", "text"):
                if str(e.get(key) or "").strip().lower() == value.lower():
                    if e.get("id"):
                        return f"#{e['id']}"
                    if e.get("data_test"):
                        return f"[data-test='{e['data_test']}']"
                    if e.get("placeholder"):
                        return f"[placeholder='{e['placeholder']}']"
                    if e.get("name"):
                        return f"[name='{e['name']}']"
        return ""

    for attr, value in re.findall(r"\[\s*([a-zA-Z_:-]+)\s*=\s*['\"]([^'\"]+)['\"]\s*\]", code_to_scan):
        key = _ATTR_KEYS.get(attr.lower())
        if not key or (attr.lower(), value.lower()) in seen:
            continue
        seen.add((attr.lower(), value.lower()))
        if not _known(key, value):
            better = _suggest(value)
            problems.append(
                f"[{attr}='{value}'] matches nothing on this page — no element "
                f"has {attr}='{value}'."
                + (f" Use {better} instead." if better else
                   " Pick an element from the list above.")
            )

    for value in re.findall(r"By\.ID\s*,\s*['\"]([^'\"]+)['\"]", code_to_scan):
        if ("id", value.lower()) in seen:
            continue
        seen.add(("id", value.lower()))
        if not _known("id", value):
            better = _suggest(value)
            problems.append(
                f"By.ID \"{value}\" matches nothing — no element has that id."
                + (f" Use {better} instead." if better else "")
            )

    return problems


def _autofix_selectors(code: str, states) -> tuple[str, list[str]]:
    """
    Correct provably-wrong selectors in place, without asking the model.

    When the checker can say both that a selector matches nothing AND which
    element the value was reaching for, the correction is a fact, not a
    judgement — [name='username'] on a page whose only 'username' is a
    data-test attribute can only have meant that input. Substituting it
    directly is deterministic and always succeeds, where a repair call has
    to be re-parsed, re-validated, and frequently comes back having dropped
    the step recording or introduced a new mistake.

    Only rewrites the string INSIDE the selector literal, so the
    find_element call around it is untouched and stays valid.

    Args:
        code: The generated script.
        states: The element map. Can be:
            - A list of elements
            - A dict {state_name: [elements]} for multi-state
            - The full crawled map_data dict {"base_url": ..., "pages": [...]}

    Returns:
        (code, descriptions of what was changed).
    """
    # Handle the full crawled map_data structure (when called from fast_generate_script
    # with app_map_data). Extract all elements from all pages and states.
    if isinstance(states, dict) and "pages" in states:
        all_elements = []
        for page in (states.get("pages") or []):
            # Single-state page
            page_elements = page.get("elements", [])
            if page_elements:
                all_elements.extend(page_elements)
            # Multi-state page
            page_states = page.get("states", {})
            for state_elements in (page_states or {}).values():
                if state_elements:
                    all_elements.extend(state_elements)
        states = {"all": all_elements} if all_elements else {}
    elif isinstance(states, list):
        states = {"initial": states}
    elements = [e for group in (states or {}).values() for e in (group or [])]
    if not elements:
        return code, []

    def _apply_selector_fixes(code_part: str) -> tuple[str, list[str]]:
        local_fixes = []
        for attr, value in set(re.findall(
                r"\[\s*([a-zA-Z_:-]+)\s*=\s*['\"]([^'\"]+)['\"]\s*\]", code_part)):
            key = _ATTR_KEYS.get(attr.lower())
            if not key:
                continue
            if any(str(e.get(key) or "").strip().lower() == value.lower() for e in elements):
                continue                       # the selector is fine
            replacement = ""
            for e in elements:
                for k in ("id", "data_test", "placeholder", "name", "aria_label", "text"):
                    if str(e.get(k) or "").strip().lower() == value.lower():
                        if e.get("id"):
                            replacement = f"#{e['id']}"
                        elif e.get("data_test"):
                            replacement = f"[data-test='{e['data_test']}']"
                        elif e.get("placeholder"):
                            replacement = f"[placeholder='{e['placeholder']}']"
                        break
                if replacement:
                    break
            if not replacement:
                continue                       # cannot tell what was meant; leave it
            for quoted in (f"[{attr}='{value}']", f'[{attr}="{value}"]'):
                if quoted in code_part:
                    code_part = code_part.replace(quoted, replacement)
                    local_fixes.append(f"{quoted} -> {replacement}")
        return code_part, local_fixes

    # Keep Azure Entra selector block intact: app-map autofix is not valid there.
    pre, sso_block, post = _split_sso_preamble(code)
    pre_fixed, fixes_pre = _apply_selector_fixes(pre)
    post_fixed, fixes_post = _apply_selector_fixes(post)
    return pre_fixed + sso_block + post_fixed, fixes_pre + fixes_post


def _element_identifiers(states) -> set:
    """
    Every string by which an element on this page can be identified.

    Used to check the model's `matched` claim against reality — see
    _apply_matched. Lowercased for comparison.
    """
    if isinstance(states, list):
        states = {"initial": states}
    out = set()
    for elements in (states or {}).values():
        for e in elements or []:
            for key in ("id", "data_test", "aria_label", "placeholder", "name", "text"):
                v = e.get(key)
                if v:
                    out.add(str(v).strip().lower())
    return out


# Verbs that mean the step drives a control. A step without one of these
# ("wait 5 seconds", "observe the total") needs no element, so the absence of
# a matching one is not a coverage gap.
_INTERACTION_VERBS = (
    "click", "enter", "type", "select", "choose", "navigate", "press", "tap",
    "upload", "toggle", "check", "uncheck", "drag", "submit", "open", "close",
    "sign in", "sign out", "log in", "log out", "search", "filter", "sort",
    "add", "remove", "scroll to", "expand", "collapse",
)

# Words that carry no identifying weight — matching on these would make
# almost any step look present on almost any page.
_STOPWORDS = {
    "the", "a", "an", "and", "or", "of", "to", "in", "on", "for", "with",
    "from", "into", "then", "click", "enter", "type", "select", "choose",
    "navigate", "press", "tap", "submit", "open", "close", "button", "field",
    "link", "page", "screen", "section", "option", "menu", "icon", "valid",
    "invalid", "input", "box", "area", "value", "text", "his", "her", "their",
}


def _norm(s: str) -> str:
    """Reduce to comparable form: 'user-name' and 'User Name' both -> username."""
    return re.sub(r"[^a-z0-9]", "", str(s).lower())


def _resolve_from_text(step_text: str, identifiers: set) -> bool:
    """
    Decide whether a step with no element_hint acts on something real.

    Args:
        step_text: What the tester is told to do.
        identifiers: Every identifying string on the page.

    Returns:
        True if the step needs no control, or if the page appears to have
        one matching the words in the step. False when the step clearly
        drives a control that is nowhere on the page — a coverage gap.
    """
    low = (step_text or "").lower()
    if not any(v in low for v in _INTERACTION_VERBS):
        return True                        # nothing to locate

    norms = {_norm(i) for i in identifiers if i}
    norms.discard("")
    words = [w for w in re.findall(r"[a-zA-Z][a-zA-Z0-9]+", low)
             if w not in _STOPWORDS and len(w) > 2]
    if not words:
        return True                        # nothing distinctive to go on

    # Try the most specific reading first: adjacent word pairs ("order
    # history") before single words, so a step is not matched on a stray
    # common word that happens to appear in an unrelated element.
    pairs = [_norm(words[i] + words[i + 1]) for i in range(len(words) - 1)]
    for cand in pairs + [_norm(w) for w in words]:
        if cand and any(cand == n or cand in n or n in cand for n in norms):
            return True
    return False


def _apply_matched(steps: list[dict], identifiers: set) -> list[dict]:
    """
    Decide `matched` in Python rather than trusting the model's own claim.

    This is what makes "the BRD asked for something the page does not have"
    a reliable signal instead of a hopeful one. Asking a 14B coder model to
    notice that a control is absent is the least dependable thing in this
    pipeline, and both failure directions are costly:

      false "matched": true   -> a script gets an invented selector, exactly
                                 what SCRIPT_PROMPT works hardest to prevent
      false "matched": false  -> a perfectly automatable test case is parked
                                 as manual-only and never runs

    So the PAGE decides, in both directions. The model's own flag is
    discarded rather than treated as a veto, because in practice its false
    negatives are frequent and expensive: asked to test a login failure, it
    marks "Click the Login button" unmatched — reasoning about the error
    message it cannot find on a fresh page and letting that leak onto the
    click step, which has a perfectly good #login-button behind it.

    A step with NO hint cannot simply be trusted either way, because the
    model tends to omit the hint exactly when it could not find a control —
    which is the case we most need to detect. So the step TEXT is resolved
    against the page instead:

      "Click the Login button"          -> 'login' is on the page   -> matched
      "Navigate to Order History"       -> nothing resembling it    -> unmatched
      "Wait 5 seconds"                  -> no interaction at all    -> matched

    Without this, a BRD requirement for a feature the screen does not have
    (order history export, two-factor) produces steps that look perfectly
    automatable, and the coverage finding — the entire point of keeping the
    test case — never appears.
    """
    for s in steps:
        hint = (s.get("element_hint") or "").strip().lower()
        if not hint:
            s["matched"] = _resolve_from_text(s.get("step", ""), identifiers)
            continue

        # Extract element ID from parenthesized format like "el-33 (Issue Log Title input)"
        bare = hint
        if "(" in bare and ")" in bare:
            bare = bare.split("(")[0].strip()
        # Also handle aria-label='Settings' format
        elif "=" in bare:
            bare = bare.split("=", 1)[1]
        bare = bare.strip().strip("'\"[]")

        found = any(bare == i or bare in i or i in bare for i in identifiers if i)
        if not found:
            # The hint didn't literally match, but the hint is the model's
            # own (sometimes business-friendly, e.g. "Sold To") phrasing —
            # it can disagree with the page's raw captured name (e.g. "Long
            # Address Number") without the step actually being unautomatable.
            # Give the step the same second chance a hint-less step gets
            # before giving up on it.
            found = _resolve_from_text(s.get("step", ""), identifiers)
        if bool(s.get("matched", True)) != found:
            logger.info("Element check overrides model: hint=%r model=%s page=%s",
                        hint, s.get("matched", True), found)
        s["matched"] = found
    return steps


def fast_survey(
    driver,
    url: str,
    provider: LLMProvider,
    document_context: Optional[str] = None,
    count: int = _SURVEY_MAX_CASES_DEFAULT,
    on_progress=None,
    app_map_context: Optional[str] = None,
    app_map_identifiers: Optional[set] = None,
    test_type: str = "functional",
    target_flow: Optional[str] = None,
    app_map_data: Optional[dict] = None,
    test_data: Optional[dict] = None,
) -> dict:
    """
    Write test cases from the requirements, grounded in the real page.

    One LLM call per requirement chunk rather than one call for the whole
    document. Slower, and deliberately so: the previous single call could
    only ever see the first 4000 characters, so a long BRD lost most of its
    requirements with nothing to say they had gone.

    Matches the shape agents.author_agent.run_author returns, so the
    orchestrator can use either.

    Args:
        driver: Live WebDriver already on the page.
        url: Page URL (cache key).
        provider: LLM provider.
        document_context: Extracted BRD/FRD text, if uploaded.
        count: Test cases to request per call when there is no document.
        on_progress: Optional callable(done, total, label) for live progress.
            A per-requirement survey takes minutes; without this the UI sits
            silent and looks hung.
        app_map_context: Pre-formatted multi-screen element map from the crawl
            agent (service.map_formatter.format_map_as_context). When provided,
            replaces the live single-page snapshot in __ELEMENTS__ so the LLM
            sees the full application rather than just the landing page.
        app_map_identifiers: Set of element name strings from the crawled map
            (service.map_formatter.identifiers_from_map). Replaces the
            single-page identifier set used for matched validation.
        target_flow: Optional plain-text navigation target (e.g. "Sales >
            Sales Order Entry > Standard Order SO"). Only used when
            document_context is not provided. When set, replaces the
            generic single-screen survey call with one dedicated call that
            writes exactly one end-to-end scenario grounded in the crawl's
            recorded navigation path (see _generate_target_flow_scenario).
        app_map_data: The raw crawled map dict ({"pages": [...], "trace":
            [...]}), as stored on application_maps.map_data. When provided
            together with target_flow, the scenario's steps are compiled
            deterministically from the trace itself first (see
            service.trace_compiler.build_scenario_from_trace) — falling
            back to _generate_target_flow_scenario only when the trace has
            nothing usable to compile.

    Returns:
        {"test_cases": [...],
         "requirement_report": {"requirements": [...], "zero_coverage": [...],
                                "not_surveyed": [...], "chunks": int,
                                "truncated": bool}}
    """
    if app_map_context:
        # Use the pre-crawled multi-screen map instead of the live page snapshot.
        # The live page is likely the login screen (few elements); the crawled
        # map covers all relevant screens with their real interactive elements.
        compact = app_map_context
        identifiers = app_map_identifiers or set()
        logger.info(
            "fast_survey: using pre-crawled map context  chars=%d  identifiers=%d",
            len(compact), len(identifiers),
        )
    else:
        elements = _get_map(driver, url, refresh=True)   # survey always re-reads the page
        compact = _compact_elements(elements)
        identifiers = _element_identifiers(elements)
        logger.info("Survey element map: %d chars compacted (was %d as JSON)",
                    len(compact), len(json.dumps(elements, indent=1)))

    chunks = _split_requirements(document_context or "")
    report = {"requirements": [], "zero_coverage": [], "not_surveyed": [],
              "chunks": 0, "truncated": False}

    if not chunks and target_flow:
        # No document, but a specific navigation target was given: skip the
        # generic "what does this screen offer" call entirely. That call
        # produces several small, disconnected observations (inconsistent
        # steps — sometimes checking a breadcrumb instead of the click that
        # actually got there) rather than one coherent flow. Generate
        # exactly one end-to-end scenario grounded in the crawl's recorded
        # navigation path instead.
        #
        # Optionally try compiling the scenario directly from the crawl's
        # own trace first — no LLM call, so nothing to hallucinate. OFF by
        # default (see _DETERMINISTIC_TARGET_FLOW_STEPS below): the trace is
        # a faithful recording of how the crawl AGENT explored — including
        # its own backtracking/retries (duplicate clicks, "Cancel" clicks
        # to back out of a wrong screen) — and it never includes the SSO
        # login steps (those run in a separate, already-authenticated
        # browser flow outside the agent loop). Transcribing it verbatim
        # produced a worse test case than the LLM writer: missing the
        # login narrative, exploration noise baked in as real steps, and
        # raw internal field names (e.g. "Long Address Number") instead of
        # the business names ("Sold To") the target_flow text itself uses.
        # Re-enable only once build_scenario_from_trace filters exploration
        # noise and reconciles names against target_flow's own wording.
        target_produced: list = []
        if app_map_data and _DETERMINISTIC_TARGET_FLOW_STEPS:
            try:
                from service.trace_compiler import build_scenario_from_trace
                target_produced = build_scenario_from_trace(app_map_data, target_flow) or []
                if target_produced:
                    logger.info(
                        "Target flow scenario compiled deterministically from trace "
                        "(%d step(s)) for target_flow=%r",
                        len(target_produced[0].get("steps", [])), target_flow,
                    )
            except Exception as _tc_err:
                logger.warning("Deterministic trace compilation failed (falling back to LLM): %s", _tc_err)
                target_produced = []

        if not target_produced:
            tool = provider.build_tool([])
            try:
                target_produced = _generate_target_flow_scenario(
                    provider=provider,
                    target_flow=target_flow,
                    compact=compact,
                    identifiers=identifiers,
                    test_type=test_type,
                    tool=tool,
                    test_data=test_data,
                )
            except Exception as _tf_err:
                logger.warning("Target flow scenario call failed: %s", _tf_err)
                target_produced = []
        report["chunks"] = 1
        logger.info(
            "Survey complete (target flow mode): %d test case(s) for target_flow=%r",
            len(target_produced), target_flow,
        )
        return {"test_cases": target_produced, "requirement_report": report}

    if not chunks:
        # No document, no target flow: survey the screen itself. One call, as before.
        batches = [[{"id": None, "label": "the screen",
                     "text": "No requirements document was provided. Base the "
                             "test cases on what the screen below offers."}]]
    else:
        # One requirement per call. Packing several requirements into one
        # 4096-token call made the model cover only ~2 of them and drop the
        # rest; a single requirement per call fits the budget and guarantees
        # every requirement produces its test case.
        batches = [[c] for c in chunks]
        if len(batches) > SURVEY_MAX_CHUNKS:
            dropped = [c for b in batches[SURVEY_MAX_CHUNKS:] for c in b]
            report["not_surveyed"] = [c["id"] or c["label"] for c in dropped]
            report["truncated"] = True
            batches = batches[:SURVEY_MAX_CHUNKS]
            logger.warning(
                "BRD has more requirement chunks than SURVEY_MAX_CHUNKS=%d — "
                "%d not surveyed: %s",
                SURVEY_MAX_CHUNKS, len(dropped), ", ".join(report["not_surveyed"][:10]),
            )
        report["requirements"] = [c["id"] or c["label"] for b in batches for c in b]

    report["chunks"] = len(batches)
    tool = provider.build_tool([])
    scenarios: list[dict] = []

    for i, batch in enumerate(batches, 1):
        label = ", ".join(c["id"] or c["label"] for c in batch)
        if on_progress:
            on_progress(i, len(batches), label)

        requirement_text = "\n\n".join(c["text"] for c in batch)
        prompt = (
            SURVEY_PROMPT
            .replace("__REQUIREMENTS__", requirement_text[:CHUNK_CHARS * 2])
            .replace("__ELEMENTS__", compact)
            .replace("__TEST_TYPE__", test_type)
        )

        started = time.time()
        try:
            response = provider.call_model(
                "You reply with JSON only.", tool,
                [provider.user_text_content(prompt)],
                options={"num_predict": _SURVEY_NUM_PREDICT},
            )
            text = " ".join(
                p.text for p in response.candidates[0].content.parts
                if getattr(p, "text", "")
            ).strip()
        except Exception as e:
            # One bad chunk must not lose the rest of the document.
            logger.warning("Survey chunk %d/%d (%s) failed: %s", i, len(batches), label, e)
            report["zero_coverage"] += [c["id"] or c["label"] for c in batch]
            continue

        produced = _parse_scenarios(text, identifiers)
        logger.info("Survey chunk %d/%d (%s): %d test case(s) in %.1fs",
                    i, len(batches), label, len(produced), time.time() - started)

        if not produced:
            # Distinguish "model wrote nothing usable" from "reply was cut
            # off mid-JSON" — the second is a num_predict problem and the
            # generic 'could not parse' message sends people prompt-hunting.
            if text.lstrip().startswith("[") and not text.rstrip().endswith("]"):
                logger.warning("Survey chunk %s: reply truncated mid-JSON — "
                               "raise num_predict or lower SURVEY_CHUNK_CHARS", label)
            else:
                logger.warning("Survey chunk %s produced nothing usable: %s",
                               label, text[:200])
            report["zero_coverage"] += [c["id"] or c["label"] for c in batch]
            continue

        # A chunk can answer for some of its requirements and not others.
        answered = {(s.get("requirement_id") or "").strip().lower() for s in produced}
        for c in batch:
            if c["id"] and c["id"].strip().lower() not in answered:
                report["zero_coverage"].append(c["id"])

        scenarios += produced

    # ── E2E scenario (BRD-only, one call after all chunks) ────────────────────
    # The per-requirement calls cover individual behaviours. This final call
    # produces exactly one end-to-end scenario that walks the complete workflow
    # described in the BRD from start to final acceptance criterion. Without
    # this, the suite has depth but no integration coverage.
    if chunks and document_context:
        try:
            e2e_produced = _generate_e2e_scenario(
                provider=provider,
                document_context=document_context,
                compact=compact,
                identifiers=identifiers,
                test_type=test_type,
                tool=tool,
            )
            if e2e_produced:
                scenarios += e2e_produced
                logger.info("E2E scenario added (%d test case(s))", len(e2e_produced))
            else:
                logger.warning("E2E call produced nothing usable")
        except Exception as _e2e_err:
            logger.warning("E2E scenario call failed (non-fatal): %s", _e2e_err)

    logger.info(
        "Survey complete: %d test case(s) from %d chunk(s); %d requirement(s) "
        "produced nothing; %d not surveyed",
        len(scenarios), len(batches), len(report["zero_coverage"]),
        len(report["not_surveyed"]),
    )
    return {"test_cases": scenarios, "requirement_report": report}


# ──────────────────────────── E2E scenario ─────────────────────────────────

E2E_PROMPT = """You are a senior QA engineer.

Below is a requirements document describing a user workflow.

REQUIREMENTS DOCUMENT:
----------------------
__DOCUMENT__
----------------------

ELEMENTS AVAILABLE ON THE SCREEN:
__ELEMENTS__

Write EXACTLY ONE end-to-end test case that walks the COMPLETE workflow
described in the document from the very first user action to the final
acceptance criterion. This is the integration scenario — it must cover
the full happy path in sequence, not just one requirement. Every step
in the workflow must appear as a numbered step in this single test case.

Use the same JSON format as your other test cases:
[
  {
    "scenario": "<2-4 word feature name>",
    "title": "<one sentence describing the full workflow>",
    "description": "<numbered steps — one action + expected result each>",
    "category": "positive",
    "derivation": "stated",
    "requirement_id": "E2E",
    "steps": [
      {"step": "...", "expected": "...", "element_hint": "...", "matched": true}
    ]
  }
]

TEST TYPE: __TEST_TYPE__
Reply with JSON only. No explanation, no markdown fences."""


def _generate_e2e_scenario(
    provider: LLMProvider,
    document_context: str,
    compact: str,
    identifiers: set,
    test_type: str,
    tool: list,
) -> list[dict]:
    """
    Generate exactly one end-to-end scenario covering the full BRD workflow.

    Called once after all per-requirement chunks have been processed.
    Returns a list of parsed scenario dicts (normally just one), or an
    empty list if the model returns nothing usable.

    Args:
        provider:         LLM provider.
        document_context: Full BRD/FRD text.
        compact:          Formatted element map for the __ELEMENTS__ slot.
        identifiers:      Element name set for matched-validation.
        test_type:        'functional' or 'regression'.
        tool:             Pre-built empty tool list (no tool calls needed).
    """
    prompt = (
        E2E_PROMPT
        .replace("__DOCUMENT__", document_context[:CHUNK_CHARS * 4])
        .replace("__ELEMENTS__", compact)
        .replace("__TEST_TYPE__", test_type)
    )
    response = provider.call_model(
        "You reply with JSON only.", tool,
        [provider.user_text_content(prompt)],
        options={"num_predict": _SURVEY_NUM_PREDICT},
    )
    text = " ".join(
        p.text for p in response.candidates[0].content.parts
        if getattr(p, "text", "")
    ).strip()
    produced = _parse_scenarios(text, identifiers)
    if produced:
        for s in produced:
            s.setdefault("requirement_id", "E2E")
            s["tags"] = {**(s.get("tags") or {}), "e2e": True}
    return produced


# ──────────────────────────── target-flow scenario ─────────────────────────

TARGET_FLOW_PROMPT = """You are a senior QA engineer.

A specific navigation target was given instead of a requirements document.

TARGET FLOW: __TARGET_FLOW__

Below is the map of every screen the crawl visited, including a NAVIGATION
FLOW section (if present) recording the exact click/input sequence that
reached the target screen:

__ELEMENTS__

Write EXACTLY ONE end-to-end test case that reaches the target screen AND
completes the action it describes (e.g. if the target is "Sales Order
Creation", the test must actually create a sales order — not just arrive
at the screen).

TEST DATA available for this application (business keys only — real values
are resolved at execution time from secure storage, never shown to you
here):
__TEST_DATA_KEYS__

GROUNDING RULES — read carefully:
1. Use the NAVIGATION FLOW section as the authoritative click path. Every
   click and text-entry action listed there must appear as its own
   numbered step, IN ORDER, from Screen 1 through the destination screen.
   Never skip a recorded click. Never substitute a breadcrumb or
   observation step for a click that NAVIGATION FLOW records as
   necessary — a breadcrumb is supporting evidence, not a replacement for
   the click that got you there.
2. Once you reach the destination screen, use its element list to write
   the steps that complete the target action, then submit. If NAVIGATION
   FLOW shows the crawl already filled and submitted a form on that
   screen, reflect that same sequence of FIELDS (which fields, in what
   order) — but see rule 3 for what VALUE to put in each one. Fill EVERY
   field NAVIGATION FLOW shows being filled on that screen before the
   submit action — do not stop partway through a multi-field form.
3. DATA VALUES — priority order, per field:
   a. If the field's business name matches one of the TEST DATA keys
      above (e.g. field "Item Number" ~ key "ItemNumber"), set that
      step's "data_input" to "{{ key }}" using the EXACT key name from
      TEST DATA — never the literal value, never a value copied from
      NAVIGATION FLOW. The real value is substituted later from this
      application's stored test data.
   b. Only when a field has NO matching TEST DATA key, fall back to the
      value NAVIGATION FLOW recorded for it (the crawl agent's own
      exploration input) as a literal string in "data_input" — a
      fallback for incidental fields only, never for a field that has a
      real TEST DATA key.
   c. Never reuse one field's value/key for a different field. If two
      fields share the same captured name, assign values in the order
      they were entered, first-to-first.
   d. Never invent a value that is neither a TEST DATA key nor present in
      NAVIGATION FLOW.
4. Never substitute a guessed keyboard shortcut or a "well-known" button
   name for a click NAVIGATION FLOW records. If NAVIGATION FLOW shows a
   click on a bare ref (e.g. "el-403") rather than a readable name,
   describe the step using that ref, not an invented label.
5. GRID CELLS — an element line marked "[JDE GRID CELL] column=... row_index=..."
   is an editable cell inside a data grid (e.g. line-item fields such as
   Item Number, Quantity, Lot/Serial). It has no name/id/title of its own
   in the real page — set that step's element_hint to the exact column
   text shown (e.g. "Item Number"), never a guessed attribute value. The
   script generator resolves grid cells by column header + row position,
   not by CSS attribute, so the hint only needs to name the column
   correctly.
6. After submission, add a step that verifies the outcome (confirmation
   message, updated list, or new record reference) using an element from
   the map if one is available; otherwise describe the expected result
   even without a backing element.
7. Write only this one test case. Do not propose separate small tests for
   individual screens.

Use the same JSON format as other test cases:
[
  {
    "scenario": "<2-4 word feature name>",
    "title": "<one sentence describing the full workflow>",
    "description": "<numbered steps — one action + expected result each>",
    "category": "positive",
    "derivation": "stated",
    "requirement_id": "TARGET_FLOW",
    "steps": [
      {"step": "Click the Add button", "expected": "A blank entry form opens",
       "element_hint": "Add (Ctrl+Alt+A)", "matched": true,
       "data_input": null, "wait_condition": "element_visible"},
      {"step": "Enter {{ ItemNumber }} into the Item Number field",
       "expected": "The item number is accepted",
       "element_hint": "Item Number", "matched": true,
       "data_input": "{{ ItemNumber }}", "wait_condition": "none"}
    ]
  }
]

TEST TYPE: __TEST_TYPE__
Reply with JSON only. No explanation, no markdown fences."""


def _generate_target_flow_scenario(
    provider: LLMProvider,
    target_flow: str,
    compact: str,
    identifiers: set,
    test_type: str,
    tool: list,
    test_data: Optional[dict] = None,
) -> list[dict]:
    """
    Generate exactly one end-to-end scenario that completes the given
    target flow.

    Mirrors _generate_e2e_scenario, but grounded in a target_flow string
    plus the crawl's recorded NAVIGATION FLOW trace instead of a BRD. Used
    when a user supplies a target flow with no requirements document —
    replaces the generic per-screen survey call so the result is always
    one coherent, fully-grounded scenario rather than several disconnected
    observations.

    Args:
        provider:    LLM provider.
        target_flow: Plain-text navigation target (e.g. "Sales > Sales
                     Order Entry > Standard Order SO").
        compact:     Formatted element map (+ NAVIGATION FLOW section) for
                     the __ELEMENTS__ slot.
        identifiers: Element name set for matched-validation.
        test_type:   'functional' or 'regression'.
        tool:        Pre-built empty tool list (no tool calls needed).
        test_data:   Optional dict of real per-application field values
                     (test_data/applications/app_<id>.json), excluding
                     username/password. Any field this target flow needs
                     to fill whose business name matches one of these keys
                     must be written as "{{ key }}" in data_input (resolved
                     to a real stored value via get_data() at execution
                     time), instead of copying whatever the crawl agent
                     typed in while exploring the form.
    """
    _TEST_DATA_EXCLUDED_KEYS = {"username", "password", "password_env"}
    _td_items = {
        k: v for k, v in (test_data or {}).items()
        if k not in _TEST_DATA_EXCLUDED_KEYS and v not in (None, "")
    }
    test_data_section = (
        "\n".join(f"  - {k}" for k in _td_items.keys())
        if _td_items
        else "(none stored for this application)"
    )

    prompt = (
        TARGET_FLOW_PROMPT
        # Was target_flow[:500] — silently dropped everything past step ~22
        # on longer literal step-list flows (a JDE flow this size runs well
        # past 500 chars). CHUNK_CHARS is generous enough for a full manual
        # test case description without re-introducing an unbounded prompt.
        .replace("__TARGET_FLOW__", target_flow[:CHUNK_CHARS * 4])
        .replace("__TEST_DATA_KEYS__", test_data_section)
        .replace("__ELEMENTS__", compact)
        .replace("__TEST_TYPE__", test_type)
    )
    response = provider.call_model(
        "You reply with JSON only.", tool,
        [provider.user_text_content(prompt)],
        options={"num_predict": _SURVEY_NUM_PREDICT},
    )
    text = " ".join(
        p.text for p in response.candidates[0].content.parts
        if getattr(p, "text", "")
    ).strip()
    produced = _parse_scenarios(text, identifiers)
    if produced:
        for s in produced:
            s.setdefault("requirement_id", "TARGET_FLOW")
            s["tags"] = {**(s.get("tags") or {}), "target_flow": True}
    return produced


# ──────────────────────────── script generation ────────────────────────────

SCRIPT_PROMPT = """You write Python UI tests using pytest and Selenium.

PRODUCE EXACTLY THIS FILE SHAPE. Copy it and fill in the marked parts.
Do not restructure it, do not add a main block, do not add extra functions.

    import os
    import time
    from selenium.webdriver.common.by import By
    from selenium.webdriver.support.ui import WebDriverWait
    from selenium.webdriver.support import expected_conditions as EC
    from selenium.common.exceptions import NoSuchElementException, StaleElementReferenceException
    from NextGen2_evidence import step, note_skipped

    APP_URL = os.environ.get("APP_URL") or os.environ.get("NETRA_URL")
    if not APP_URL:
        raise RuntimeError("APP_URL is not set")

    def get_data(key: str, *, required: bool = True, default: str = "") -> str:
        env_key = f"TEST_{str(key).strip().upper().replace('-', '_').replace(' ', '_')}"
        value = os.environ.get(env_key)
        if value is None or value == "":
            if required:
                raise RuntimeError(f"Missing required test data: {env_key}")
            return default
        return value

    def _wait(driver, timeout=20):
        \"\"\"Fluent wait: polls every 500 ms, ignores transient DOM churn.\"\"\"
        return WebDriverWait(
            driver, timeout,
            poll_frequency=0.5,
            ignored_exceptions=[NoSuchElementException, StaleElementReferenceException],
        )

    def _visible(driver, locator, timeout=20):
        \"\"\"Wait for visible element with actionable diagnostics on timeout.\"\"\"
        try:
            return _wait(driver, timeout).until(
                EC.visibility_of_element_located(locator)
            )
        except Exception as exc:
            by, value = locator
            in_dom = bool(driver.find_elements(by, value))
            if in_dom:
                raise AssertionError(
                    f\"Element exists in DOM but is not visible for locator=({by}, {value!r}). \"
                    \"It may be inside a collapsed/hidden section; expand the related container first.\"
                ) from exc
            raise AssertionError(
                f\"Element not found in DOM for locator=({by}, {value!r}). Selector may be incorrect.\"
            ) from exc

    def _resolve_interactable(driver, locator, *, target_label=\"\", timeout=20):
        \"\"\"Return the best visible+enabled match. Searches the main document
        AND every iframe on the page (an element tagged '[in iframe: X]' in
        the ELEMENTS list only exists inside that nested iframe), then
        expands the nearest collapsed ancestor if needed. You do not need to
        call switch_to.frame() yourself for tagged elements -- this helper
        does it for you and leaves the driver positioned in the correct
        context for whatever it returns.\"\"\"
        by, value = locator

        def _score(el):
            score = 0
            try:
                if el.is_displayed():
                    score += 100
                if el.is_enabled():
                    score += 40
            except Exception:
                return -1
            txt = (el.text or \"\").strip().lower()
            href = (el.get_attribute(\"href\") or \"\").strip().lower()
            if target_label:
                needle = target_label.strip().lower()
                if needle and needle in txt:
                    score += 15
                if needle and needle in href:
                    score += 15
            if href:
                score += 5
            return score

        def _rank_here():
            candidates = driver.find_elements(by, value)
            if not candidates:
                return None, []
            ranked = sorted(candidates, key=_score, reverse=True)
            return ranked[0], candidates

        def _best_current():
            driver.switch_to.default_content()
            best, candidates = _rank_here()
            if candidates:
                return best, candidates
            try:
                frame_count = len(driver.find_elements(By.TAG_NAME, \"iframe\"))
            except Exception:
                frame_count = 0
            for idx in range(frame_count):
                try:
                    frames = driver.find_elements(By.TAG_NAME, \"iframe\")
                    driver.switch_to.frame(frames[idx])
                except Exception:
                    driver.switch_to.default_content()
                    continue
                best, candidates = _rank_here()
                if candidates:
                    return best, candidates
                driver.switch_to.default_content()
            return None, []

        best, candidates = _best_current()
        if not candidates:
            raise AssertionError(
                f\"Element not found in DOM for locator=({by}, {value!r}) in the main \"
                \"document or any iframe. Selector may be incorrect.\"
            )

        if best is not None and best.is_displayed() and best.is_enabled():
            return best

        # Expand the closest collapsed ancestor for each candidate when possible.
        expanded_any = False
        for cand in candidates:
            try:
                ancestors = cand.find_elements(
                    By.XPATH,
                    \"./ancestor::*[@aria-expanded='false' or @hidden or contains(@class,'collapsed')][1]\",
                )
                if not ancestors:
                    continue
                container = ancestors[0]
                toggles = container.find_elements(
                    By.XPATH,
                    \".//button[@aria-expanded='false' or @aria-controls] | .//*[@role='button' and @aria-expanded='false'] | .//summary\",
                )
                if toggles:
                    driver.execute_script(\"arguments[0].scrollIntoView({block: 'center'});\", toggles[0])
                    toggles[0].click()
                    expanded_any = True
                    continue
                if container.is_displayed() and container.is_enabled():
                    driver.execute_script(\"arguments[0].scrollIntoView({block: 'center'});\", container)
                    container.click()
                    expanded_any = True
            except Exception:
                continue

        # Re-check until an interactable candidate appears.
        try:
            return _wait(driver, timeout).until(
                lambda _d: next(
                    (
                        el for el in sorted(driver.find_elements(by, value), key=_score, reverse=True)
                        if el.is_displayed() and el.is_enabled()
                    ),
                    False,
                )
            )
        except Exception as exc:
            if driver.find_elements(by, value):
                suffix = \" after expanding nearest collapsed ancestor\" if expanded_any else \"\"
                raise AssertionError(
                    f\"Element exists in DOM but is not interactable for locator=({by}, {value!r}){suffix}.\"
                ) from exc
            raise AssertionError(
                f\"Element disappeared from DOM while waiting for locator=({by}, {value!r}).\"
            ) from exc

    def _resolve_jde_grid_cell(driver, column, *, row_index=0, timeout=20):
        \"\"\"Find an editable JDE grid cell by its column header text and row
        position. These cells (e.g. Item Number, Quantity, Lot/Serial in a
        line-item grid) have no name/id/aria-label/title of their own --
        every cell in the row is otherwise identical. Searches the main
        document and every iframe on the page, the same way
        _resolve_interactable does. Use this INSTEAD of a CSS/XPath selector
        whenever the ELEMENTS list marks a field '[JDE GRID CELL]'.\"\"\"
        inspect_js = r\"\"\"
        var td = el.closest('td[colindex]');
        if (!td) { return null; }
        var colindex = td.getAttribute('colindex');
        var table = el.closest('table[id^=\"jdeGridData\"]');
        if (!table || !table.id) { return null; }
        var m = table.id.match(/^jdeGridData(.+)\\.\\d+$/);
        if (!m) { return null; }
        var gridId = m[1];
        var header = document.getElementById('GC' + gridId + '.' + colindex);
        if (!header) { return null; }
        var headerText = (header.innerText || header.textContent || '');
        var rowEl = el.closest('tr[realrow]');
        var rowIndex = 0;
        if (rowEl && rowEl.getAttribute('realrow') !== null) {
            rowIndex = parseInt(rowEl.getAttribute('realrow'), 10);
            if (isNaN(rowIndex)) { rowIndex = 0; }
        } else {
            var rows = Array.from(table.querySelectorAll('td[colindex=\"' + colindex + '\"]'));
            rowIndex = rows.indexOf(td);
        }
        return {headerText: headerText, rowIndex: rowIndex};
        \"\"\"

        def _matches(el):
            try:
                if not el.is_displayed():
                    return False
                info = driver.execute_script(\"return (function(el){\" + inspect_js + \"})(arguments[0]);\", el)
            except Exception:
                return False
            if not info or not info.get(\"headerText\"):
                return False
            header_text = \" \".join(info[\"headerText\"].split())
            return header_text == column and info.get(\"rowIndex\", -1) == row_index

        def _search_here():
            for el in driver.find_elements(By.CSS_SELECTOR, \"input, textarea\"):
                if _matches(el):
                    return el
            return None

        def _search_all_contexts():
            driver.switch_to.default_content()
            found = _search_here()
            if found is not None:
                return found
            try:
                frame_count = len(driver.find_elements(By.TAG_NAME, \"iframe\"))
            except Exception:
                frame_count = 0
            for idx in range(frame_count):
                try:
                    frames = driver.find_elements(By.TAG_NAME, \"iframe\")
                    driver.switch_to.frame(frames[idx])
                except Exception:
                    driver.switch_to.default_content()
                    continue
                found = _search_here()
                if found is not None:
                    return found
                driver.switch_to.default_content()
            return None

        try:
            return _wait(driver, timeout).until(lambda _d: _search_all_contexts() or False)
        except Exception as exc:
            raise AssertionError(
                f\"JDE grid cell not found for column={column!r} row_index={row_index}. \"
                \"The grid/row may not have rendered yet, or the column header text \"
                \"does not match exactly.\"
            ) from exc

    @pytest.mark.<SEVERITY>  # See PYTEST MARKS section below
    def test_<short_snake_case_name>(driver):
        \"\"\"Test case description.
        
        This test receives the 'driver' fixture from conftest.py.
        Do NOT create a WebDriver here - it is automatically provided
        and pooled across all tests in the suite.
        \"\"\"
        driver.get(APP_URL)          # ALWAYS first. Keep this line.

        with step(driver, 1, \"<text of step 1>\", \"<expected result 1>\"):
            <selenium code carrying out step 1>

        with step(driver, 2, \"<text of step 2>\", \"<expected result 2>\"):
            <selenium code carrying out step 2>

        # ... one `with step(...)` block per numbered step, in order
        # NOTE: No try/finally or driver.quit() - cleanup is handled by conftest.py

PYTEST MARKS ---------
Each test function must have exactly ONE severity mark. The mark indicates
whether this is a smoke test, high-priority, medium-priority, or low-priority:

@pytest.mark.smoke  - Critical path, must pass before release (login, checkout, etc.)
@pytest.mark.P0     - High priority, blocking other tests (core CRUD operations)
@pytest.mark.P1     - Medium priority, normal test case (optional workflows)
@pytest.mark.P2     - Low priority, nice-to-have features (advanced options)

Choose the mark based on the business importance of the test scenario:
  - Does it block a release? Use @pytest.mark.smoke
  - Does it block other work? Use @pytest.mark.P0
  - Is it a normal workflow? Use @pytest.mark.P1
  - Is it optional? Use @pytest.mark.P2

Example:
    @pytest.mark.smoke
    def test_user_login(driver):
        ...

    @pytest.mark.P0
    def test_create_order(driver):
        ...

Run only smoke tests: pytest -m smoke
Run P0 and P1 tests:  pytest -m "P0 or P1"
Skip low-priority:    pytest -m "not P2"

FIXTURES ---------
Do NOT create a WebDriver in your test function. The 'driver' parameter is
automatically provided by conftest.py and is reused across ALL tests in the
suite. This reduces test execution time from minutes to seconds (1 driver
startup + N tests vs N driver startups).

What conftest.py provides:
  - driver: WebDriver instance (Chrome) configured and ready
  - Window size: 1920x1080
  - Headless mode: Auto-enabled (disable with HEADED=1 environment variable)
  - Cleanup: Auto-cleanup on test failure (screenshots), on session end (quit)
  - Credentials: Read from environment (TEST_USERNAME, TEST_PASSWORD)

What you must NOT do:
  - Do NOT call webdriver.Chrome(...) - driver is provided
  - Do NOT call driver.quit() - cleanup is automatic
  - Do NOT create new Options() or set window size - it's pre-configured
  - Do NOT import from selenium.webdriver.chrome.options - not needed

Your function signature:
    def test_my_scenario(driver):  # <-- driver comes from conftest
        # Use driver directly, no setup needed

The `driver.get(APP_URL)` line is not optional and is not a step. Without
it the browser is still on a blank page and EVERY find_element raises
NoSuchElementException. If the first numbered step is itself "open the
page", keep the driver.get where it is and let that step assert what the
loaded page shows.

THE `with step(...)` BLOCKS ARE MANDATORY. They time each step, record
whether it passed, and capture a dated screenshot. A script without them
produces no evidence and will be rejected. The number and the text come
from the numbered step list below — copy the wording as given.

Put the assertions for a step INSIDE its own `with` block, so a failure is
recorded against the step that caused it.

For a step that cannot be automated because no matching element exists,
record it instead of dropping it:

    note_skipped(3, "Export order history to PDF", "no matching element")

Below is the REAL list of interactive elements on the page. You MUST only
use selectors that appear in this list — never invent an id, name or class
that is not present.

ELEMENTS (grouped by state).

CRITICAL — read the state headings:
Anything listed under "after clicking X" is HIDDEN until X is clicked. It is
still present in the page's HTML, so find_element WILL succeed even when the
menu is shut — and is_displayed() will then return False and the test fails.

So for any element under "after clicking X":
  1. click X first
  2. wait for the element to become visible
       WebDriverWait(driver, 10).until(
           EC.visibility_of_element_located((By.XPATH, "...")))
  3. only then assert

Click the control named in ITS OWN state heading. Do not assume every menu
item lives under the same control — "Profile" and "Upload image" are opened
by different buttons.

IFRAME ELEMENTS — an element line ending in "[in iframe: X]" only exists
inside a nested <iframe>, not in the main document. X is the iframe's real
"name" or "id" HTML attribute, copied EXACTLY as captured — it is NOT the
iframe's visible title or any other display text (a crawled map may show a
different, friendlier-looking title for the same iframe elsewhere; that
title is NOT what "name" resolves to in the DOM and MUST NOT be used in a
selector). You do not need to write manual switch_to.frame() calls for
these elements at all: ALWAYS call _resolve_interactable() for them — it
already searches the main document and every iframe on the page on its own
and leaves the driver correctly positioned. Do not write your own iframe-
switching helper function, and do not guess an iframe's name/id from its
title — both are unnecessary and error-prone since _resolve_interactable
already handles this.

JDE GRID CELLS — an element line marked "[JDE GRID CELL] column=\"X\"
row_index=N" is an editable cell inside a data grid (e.g. line-item fields
such as Item Number, Quantity, Lot/Serial in an inventory or order entry
grid). These cells have NO name, id, aria-label, placeholder or title of
their own in the real DOM — every cell in the row is otherwise identical.
Never guess a CSS selector like input[name='X'] or input[title='X'] for
one of these — it will not match anything. Instead, call the injected
helper:
    _resolve_jde_grid_cell(driver, "X", row_index=N, timeout=20)
which finds the cell live by its actual column header text and row
position (the same mechanism the crawl agent itself used to identify it),
searching all iframes automatically. Use it exactly like
_resolve_interactable() — it returns the live WebElement to click, read,
or type into.
__ELEMENTS__

PAGE CRAWLED (for context only — do NOT put this in the script): __URL__

SELECTOR PREFERENCE (important — some classes are auto-generated and
change on every rebuild):
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

    (By.XPATH, "//div[@role='menuitem' and normalize-space()='THE_TEXT']")
    (By.XPATH, "//*[normalize-space(text())='THE_TEXT']")

THE_TEXT above is a placeholder. Substitute the real text from the element
list. Never copy a value out of these instructions into your script.

Use By.CSS_SELECTOR only for id / class / attribute matches, for example:
    (By.CSS_SELECTOR, "[aria-label='THE_ARIA_LABEL']")

WAITING FOR ELEMENTS — always use helpers, never bare WebDriverWait:
    _visible(driver, (By.CSS_SELECTOR, "[aria-label='Submit']"))
    _wait(driver).until(EC.element_to_be_clickable((By.ID, "save-btn")))
    submit_btn = _resolve_interactable(driver, (By.CSS_SELECTOR, "[name='Submit']"),
                                       target_label="Submit", timeout=20)
    submit_btn.click()
_wait() uses a 20-second timeout, polls every 500 ms, and ignores transient
NoSuchElementException and StaleElementReferenceException automatically.
Never write WebDriverWait(driver, N) directly in your script.

IMPORTANT: VISIBILITY AND HIDDEN ELEMENTS
If multiple elements match your selector, Selenium returns the FIRST in DOM order.
Some elements may be hidden (display:none, visibility:hidden, off-screen).
Always use _visible(...) or element_to_be_clickable in your
waits — never bare find_element, which may hit a hidden element.
For links/buttons that may be under expandable UI, use
_resolve_interactable(...), then click the returned element.
If a selector could match both hidden and visible elements, either:
  - Use XPath: //button[@aria-label='Save' and not(contains(@style, 'display'))]
  - Or add explicit visibility checks before interaction
Each precondition must be satisfied in code before the numbered steps begin.
Rules:
- If authentication is required: generate a full login sequence using
  get_data('username') and get_data('password') to fill the login form,
  submit it, and wait for the home screen. Do this BEFORE any scenario step.
- If a specific screen is required: after login (if needed), navigate to
  that screen and wait for it to load before step 1.
- If an existing data record is required: use get_data('<record_key>',
  required=False) to read the identifier from the environment. If the value
  is empty, call note_skipped for every step that depends on it.
- Precondition code is NOT wrapped in step() blocks — it is setup, not
  evidence. Place it directly before the first with step(...): block.

SSO PREAMBLE (insert verbatim before the preconditions block; omit if blank):
__SSO_PREAMBLE__

PRECONDITIONS FOR THIS SCENARIO (implement each one in code before step 1):
__PRECONDITIONS__

STRUCTURED SCENARIO CONTRACT (must be satisfied):
__SCENARIO_CONTRACT__

If authentication_required=true, you MUST use get_data("username") and
get_data("password") in setup before numbered scenario steps.
If needs_navigation_context=true, include concrete navigation to the
target screen before validating field-level behavior.

SCENARIO TO TEST:
__SCENARIO__

THE SCENARIO IS USUALLY A NUMBERED LIST OF STEPS.
Implement EVERY step, in order, inside ONE test function. Do not write a
separate test function per step, and do not stop at the first assertion —
the whole numbered list is one test case.

RECORDING EACH STEP — required, and the reason the file has one shape:
Wrap every numbered step in the step() context manager. It times the step,
records whether it passed, and captures a dated screenshot when the screen
actually changed or when the step fails. Do NOT call save_screenshot
yourself and do NOT write your own screenshot helper — the recording is
fixed code so that evidence cannot vary between runs.

    from NextGen2_evidence import step, note_skipped

    with step(driver, 1, "Click the Login button", "The catalogue opens"):
        driver.find_element(By.ID, "login-button").click()

    with step(driver, 2, "Verify the catalogue is shown", "Items are listed"):
        _wait(driver).until(
            EC.visibility_of_element_located((By.CLASS_NAME, "inventory_list")))

The first two arguments after driver are the step NUMBER and the step text,
copied from the numbered list. Assertions go INSIDE the with block, so a
failure is recorded against the step that caused it.

If a step cannot be automated because no matching element exists, record it
rather than dropping it:

    note_skipped(3, "Export order history to PDF", "no matching element")

WHEN THE SCENARIO CONTAINS NUMBERED STEPS (an imported or generated
manual test case):
- The quoted names in a step are what a HUMAN calls the field, not a
  selector. A step saying 'the "Search" box' is how a tester refers to it;
  the actual element may expose that text as a placeholder, a label, or
  visible text.
- Find that element IN THE LIST ABOVE and use ITS real attributes. Never
  build a selector out of the quoted text directly — writing an aria-label
  selector for a string the list shows as a PLACEHOLDER produces a selector
  that matches nothing, or worse, matches something hidden.
- Follow the steps in order, and assert what the Expected column states.
- If a step refers to something not in the list at all, skip that step
  rather than inventing a selector for it.

SELECTOR MUST MATCH THE ATTRIBUTE IT CAME FROM.
The names below are placeholders — never copy them into your script:

    the element lists  placeholder: "SOME_TEXT"
      correct          [placeholder='SOME_TEXT']
      WRONG            [aria-label='SOME_TEXT']     <- it has no aria-label
    the element lists  aria_label: "SOME_TEXT"
      correct          [aria-label='SOME_TEXT']
    the element lists  title: "SOME_TEXT"
      correct          [title='SOME_TEXT']
      WRONG            [aria-label='SOME_TEXT']     <- it has no aria-label
    the element lists  id: "SOME_ID"
      correct          (By.ID, "SOME_ID")           <- prefer this whenever
                                                       the element has an id
    the element lists  text: "SOME_TEXT" and role: "menuitem"
      correct          //div[@role='menuitem' and normalize-space()='SOME_TEXT']

If an element in the list has an id, USE THE ID. Reaching for aria-label on
an element whose listed attributes are id, name and placeholder produces a
selector that matches nothing, and the test fails on the first step.

The skeleton above already handles the imports, the environment URL, the
Chrome options, the single test function and driver.quit(). Keep them as
written. NEVER hard-code a URL: the same test case runs against dev, test,
stage and production, and the environment supplies the address at run time.

TEST DATA CONTRACT (required):
- Read scenario data using get_data("field_name") where field_name is the
    business key from saved test data (examples: username, password,
    patient_id, order_number, account_no).
- Credentials must come from get_data("username") / get_data("password").
- Never use fallback credential defaults like
    os.environ.get("TEST_USERNAME", "testuser") or
    os.environ.get("TEST_PASSWORD", "testpassword").
- Never hard-code credentials in send_keys(...). Use values from get_data.
- If required data is absent, fail fast using the helper (do not continue
    with made-up values).

Inside the step blocks:
- prefer By.ID when the element has an id, otherwise By.CSS_SELECTOR
- use WebDriverWait with expected_conditions rather than bare sleeps
- import anything else you use. A missing import is valid Python until that
  line runs, so it survives review and fails during a real run.

SCREENSHOTS — do not write any screenshot code.
Evidence capture belongs to the step() context manager described above. It
already decides when an image is worth taking and stamps each one with the
date it was captured. A script that calls save_screenshot itself produces
undated images alongside the dated ones and makes the run harder to review,
not easier.

Add a small time.sleep when os.environ.get("HEADED") == "1" so a viewer can
follow the run.


ASSERTIONS — the most important rule:
You know the page STRUCTURE (the elements above) but NOT how the
application behaves after an action. Never assume what happens after a
submit — do not assume the input clears, that a message appears, that a
response renders, or that the URL changes.

Assert ONLY what is verifiable from the elements listed above:
  - an element is displayed / enabled
  - an input accepted the text you typed
    (element.get_attribute("value") == the text you sent)
  - an element is still present and clickable after the action
If the scenario implies an outcome you cannot verify, assert the closest
verifiable fact instead.

POST-ACTION VERIFICATION SAFETY (required):
- When verifying an element may disappear (for example, login button after
    successful login), do NOT call find_element(...) directly inside a
    WebDriverWait lambda; that raises when absent and causes false timeouts.
- Prefer expected_conditions like invisibility_of_element_located, or use
    find_elements(...) and check length/visibility safely.
- A valid predicate must remain True/False-only, not exception-driven.

Output ONLY the Python code. No explanation, no markdown fences.
"""


def fast_generate_script(
    url: str,
    scenario: str,
    provider: LLMProvider,
    driver=None,
    app_map_context: Optional[str] = None,
    app_map_data: Optional[dict] = None,
    preconditions: Optional[list] = None,
    scenario_contract: Optional[dict] = None,
    sso_required: bool = False,
    sso_integrated: bool = False,
    proposal_steps: Optional[list] = None,
    final_ui_verification: Optional[dict] = None,
    diagnostics: Optional[list] = None,
) -> Optional[str]:
    """
    Generate a complete Selenium script in a single LLM call.

    Uses the cached element map for the URL, so repeated generations for
    the same application cost one call each with no re-crawling.

    Args:
        url: Application URL.
        scenario: Plain-English scenario to test.
        provider: LLM provider.
        driver: Optional live driver, used only if the map isn't cached and
                app_map_context is not provided.
        app_map_context: Pre-formatted element map from a prior crawl (from
                         map_formatter.format_map_for_scripts). When provided,
                         skips the live-page element extraction entirely so the
                         script is grounded in the full crawled app, not just
                         the login page.
        app_map_data: Original dict from the crawled map. Required when
                      app_map_context is provided, used by _autofix_selectors
                      for post-processing. If None and app_map_context is set,
                      autofix is skipped.
        preconditions: List of setup actions to run before scenario steps.
        scenario_contract: Structured setup/navigation contract derived from
                   accepted proposal steps + preconditions.
        sso_required: When True, injects Azure Entra SSO authentication code
                      before the preconditions block. The application must have
                      requires_sso_gateway set and valid stored credentials.
        proposal_steps: Accepted structured actions and expectations. Supported
                  mapped procedures use the constrained generator with the
                  shared production browser fixture. Unsupported procedure
                  types retain the established generation path. A rejected
                  constrained candidate never silently falls back.
        final_ui_verification: Optional bounded UI observation supplied by the
              caller. The production runner derives it from original steps;
              standalone diagnostics may supply an explicitly reviewed scope.
        diagnostics: Optional list populated with constrained rejection reasons.

    Returns:
        Python source, or None if the model produced nothing usable.
    """
    from service.generation_policy import supports_grounded_generation
    if supports_grounded_generation(proposal_steps, app_map_data, preconditions):
        from service.grounded_generation import generate_grounded_script
        from service.auth_gateway import build_sso_preamble
        # A rejection is incomplete, not permission to silently bypass the
        # approved steps and target constraints via a less restricted generator.
        return generate_grounded_script(
            map_data=app_map_data, steps=proposal_steps, scenario=scenario, provider=provider,
            sso_preamble=build_sso_preamble(url, integrated=sso_integrated) if sso_required else '',
            final_ui_verification=final_ui_verification, use_shared_driver=True,
            diagnostics=diagnostics,
        )
    if final_ui_verification is not None:
        raise ValueError('UI verification is not supported for this procedure; review the original steps and captured controls.')

    # Try compiling this script directly from the crawl's own trace first —
    # no LLM call, so nothing to hallucinate. Only scenarios with structured,
    # trace-backed steps AND no extra preconditions (beyond what the SSO
    # preamble above already covers deterministically) qualify; anything
    # else falls through to the existing LLM-driven path unchanged.
    if proposal_steps and app_map_data and not preconditions:
        try:
            from service.trace_compiler import compile_script_from_trace
            deterministic_code = compile_script_from_trace(
                map_data=app_map_data,
                proposal_steps=proposal_steps,
                scenario_title=scenario[:80] if scenario else "Generated scenario",
                url=url,
                sso_required=sso_required,
            )
            if deterministic_code:
                logger.info(
                    "fast_generate_script: compiled deterministically from trace "
                    "(%d step(s)) — skipping LLM call", len(proposal_steps),
                )
                return deterministic_code
        except Exception as _tc_err:
            logger.warning(
                "Deterministic script compilation failed (falling back to LLM): %s", _tc_err
            )

    # Resolve the element context: prefer the pre-crawled map (covers the full
    # app, not just the login page); fall back to the live-page cache or a
    # fresh extraction when the crawled map is not available.
    if app_map_context:
        elements_text = app_map_context
        elements = app_map_data  # May be None; _autofix_selectors will skip if so
        logger.info(
            "fast_generate_script: using pre-crawled map context (%d chars)", len(elements_text)
        )
    else:
        elements = _ELEMENT_CACHE.get(url)
        if elements is None:
            if driver is None:
                logger.error("No cached element map for %s and no driver supplied", url)
                return None
            elements = _get_map(driver, url)
        elements_text = json.dumps(elements, indent=1)

    # Format preconditions for injection into the prompt.
    if preconditions:
        preconditions_text = "\n".join(f"- {p}" for p in preconditions)
    else:
        preconditions_text = "(none — test starts from APP_URL)"

    # Build the SSO preamble block to inject into the script template.
    # This code handles Azure Entra authentication before any test step runs.
    if sso_required:
        from service.auth_gateway import build_sso_preamble
        sso_preamble_text = build_sso_preamble(url, integrated=sso_integrated)
        logger.info("fast_generate_script: SSO preamble injected for url=%r", url)
    else:
        sso_preamble_text = "# No SSO required — script starts from APP_URL directly"

    # NOT .format(): the prompt contains literal Python (f"{name}.png",
    # {RUN_ID} ...) which .format() would treat as placeholders and raise
    # KeyError on. Explicit replacement is safe whatever the prompt holds.
    prompt = (
        SCRIPT_PROMPT
        .replace("__ELEMENTS__", elements_text)
        .replace("__URL__", url)
        .replace("__SSO_PREAMBLE__", sso_preamble_text)
        .replace("__PRECONDITIONS__", preconditions_text)
        .replace("__SCENARIO_CONTRACT__", render_contract_for_prompt(scenario_contract))
        .replace("__SCENARIO__", scenario)
    )

    started = time.time()
    tool = provider.build_tool([])
    contents = [provider.user_text_content(prompt)]
    response = provider.call_model("You reply with Python code only.", tool, contents)
    code = " ".join(

        p.text for p in response.candidates[0].content.parts if getattr(p, "text", "")
    ).strip()
    logger.info("fast_generate_script: model replied in %.1fs", time.time() - started)

    code = _strip_fences(code)
    if "def test_" not in code:
        logger.warning("Generated output has no test function: %s", code[:300])
        return None

    def _ensure_sso_preamble(code_text: str) -> str:
        """Inject/normalize Azure Entra preamble for SSO apps.

        The preamble uses ``driver`` (a pytest fixture / function parameter) so
        it MUST live inside the test function body at 4-space indent.  This
        function always:

        1. Removes any existing SSO block from wherever the model placed it.
        2. Re-inserts the canonical preamble at 4-space indent immediately
           after ``driver.get(APP_URL)`` so it is always inside the function.
        """
        if not sso_required:
            return code_text

        from service.auth_gateway import build_sso_preamble
        preamble_raw = build_sso_preamble(url, integrated=sso_integrated)

        # Indent every non-blank line by 4 spaces so the preamble sits inside
        # the test function body and ``driver`` is in scope.
        preamble_indented = "\n".join(
            ("    " + line) if line.strip() else line
            for line in preamble_raw.splitlines()
        )

        # Step 1 — remove any existing SSO block (model-authored or a prior
        # canonical copy that may be at the wrong indent level).
        existing = _extract_sso_preamble(code_text)
        if existing:
            if existing.strip() != preamble_raw.strip():
                logger.warning(
                    "fast_generate_script: replacing model-authored SSO preamble with canonical gateway preamble"
                )
            else:
                logger.info(
                    "fast_generate_script: re-indenting canonical SSO preamble to function-body level"
                )
            code_text = code_text.replace(existing, "", 1)
            # Collapse any triple-blank gaps left by the removal.
            code_text = re.sub(r"\n{3,}", "\n\n", code_text)

        # Step 2 — insert the 4-space-indented preamble right after
        # driver.get(APP_URL), which is always inside the test function.
        after_get = re.search(r"(?m)^[ \t]*driver\.get\(APP_URL\).*$", code_text)
        if after_get:
            pos = after_get.end()
            return code_text[:pos] + "\n\n" + preamble_indented + "\n" + code_text[pos:]

        # Fallback: insert before the first step block (also inside function).
        before_step = re.search(r"(?m)^[ \t]*with step\(driver,", code_text)
        if before_step:
            logger.warning(
                "fast_generate_script: driver.get(APP_URL) not found; inserting SSO preamble before first step block"
            )
            pos = before_step.start()
            return code_text[:pos] + preamble_indented + "\n\n" + code_text[pos:]

        logger.warning(
            "fast_generate_script: no suitable insertion point found; prepending SSO preamble as fallback"
        )
        return preamble_indented + "\n\n" + code_text

    def _ensure_visibility_wait_diagnostics(code_text: str) -> str:
        """Normalize visibility waits to emit clear hidden-vs-missing errors."""
        helper = '''
def _visible(driver, locator, timeout=20):
    """Wait for visible element with actionable diagnostics on timeout."""
    try:
        return _wait(driver, timeout).until(
            EC.visibility_of_element_located(locator)
        )
    except Exception as exc:
        by, value = locator
        in_dom = bool(driver.find_elements(by, value))
        if in_dom:
            raise AssertionError(
                f"Element exists in DOM but is not visible for locator=({by}, {value!r}). "
                "It may be inside a collapsed/hidden section; expand the related container first."
            ) from exc
        raise AssertionError(
            f"Element not found in DOM for locator=({by}, {value!r}). Selector may be incorrect."
        ) from exc
'''
        if "def _visible(driver, locator, timeout=20):" not in code_text:
            wait_block = re.search(
                r"(?ms)^def _wait\(driver, timeout=20\):\n(?:^[ \t].*\n)+",
                code_text,
            )
            if wait_block:
                pos = wait_block.end()
                code_text = code_text[:pos] + "\n" + helper + "\n" + code_text[pos:]

        # Rewrite common visibility wait patterns to the diagnostic helper.
        code_text = re.sub(
            r"_wait\(\s*driver\s*\)\.until\(\s*EC\.visibility_of_element_located\(\s*(\([^\n]+\))\s*\)\s*\)",
            r"_visible(driver, \1)",
            code_text,
        )
        code_text = re.sub(
            r"_wait\(\s*driver\s*,\s*([0-9]+)\s*\)\.until\(\s*EC\.visibility_of_element_located\(\s*(\([^\n]+\))\s*\)\s*\)",
            r"_visible(driver, \2, timeout=\1)",
            code_text,
        )
        return code_text

    def _ensure_no_manual_iframe_switching(code_text: str) -> str:
        """
        Deterministically strip LLM-invented manual iframe-switching code
        that duplicates (and often breaks) what _resolve_interactable
        already does internally.

        Two concrete failure patterns this removes, confirmed against a
        real generated script:

        1. A custom helper function like:
               def _switch_to_main_app_iframe(driver):
                   driver.switch_to.default_content()
                   main_iframe = _wait(driver, 20).until(
                       EC.presence_of_element_located(
                           (By.CSS_SELECTOR, "iframe[name='Main Application']"))
                   )
                   driver.switch_to.frame(main_iframe)
           The model guessed the iframe's visible TITLE ("Main Application")
           instead of its real name/id attribute — the crawl's own map
           always captures the real name/id (see service/iframe_helper.py),
           and SCRIPT_PROMPT's "[in iframe: X]" section now documents this
           distinction explicitly. Before that prompt fix, and as a
           deterministic safety net even after it, this function always
           timed out because no iframe truly has that name. Removed
           entirely (definition + every call site) — _resolve_interactable
           already walks every iframe on the page itself and needs no such
           helper.

        2. A standalone "wait for iframe by name" block not assigned to
           anything used later, e.g.:
               _wait(driver, 30).until(
                   EC.presence_of_element_located(
                       (By.CSS_SELECTOR, "iframe[name='Main Application']"))
               )
           Same wrong-attribute risk as (1) — if the guessed name is wrong,
           this raises TimeoutException and fails the step before
           _resolve_interactable ever gets a chance to look for the real
           element (which needs no such wait — it retries/searches iframes
           on its own).

        Both patterns are safe to remove unconditionally: neither one's
        return value is ever used by later code, so deleting them changes
        nothing about what the test actually verifies.

        This is a best-effort, name-heuristic-based safety net (matches
        function names containing "switch"/"frame"/"iframe"), not a
        guarantee against every possible way a model could hand-roll frame
        logic — the prompt-level fix (documenting "[in iframe: X]" and
        instructing the model to prefer _resolve_interactable) is the
        primary defense; this is the deterministic backstop for when the
        model doesn't fully comply. Works on either LF or CRLF input.
        """
        had_crlf = "\r\n" in code_text
        if had_crlf:
            code_text = code_text.replace("\r\n", "\n")

        removed_names = []
        helper_def_pattern = re.compile(
            r"(?m)^def\s+(_[a-zA-Z0-9_]*(?:switch|frame|iframe)[a-zA-Z0-9_]*)\s*\("
            r"[^\)\n]*\)\s*:\n"
            r"(?:[ \t]+[^\n]*\n|\n)*?"
            r"(?=^def\s|^@|^class\s|\Z)"
        )

        def _capture_and_drop(m):
            removed_names.append(m.group(1))
            return ""

        code_text = helper_def_pattern.sub(_capture_and_drop, code_text)
        removed_names = [
            n for n in removed_names
            if n not in ("_resolve_interactable", "_resolve_jde_grid_cell", "_visible", "_wait")
        ]

        for name in removed_names:
            call_pattern = re.compile(
                rf"(?m)^[ \t]*{re.escape(name)}\s*\([^\)\n]*\)\s*\n"
            )
            code_text = call_pattern.sub("", code_text)

        dead_iframe_wait_pattern = re.compile(
            r"(?m)^[ \t]*_wait\(\s*driver\s*,\s*[0-9]+\s*\)\.until\(\s*\n?"
            r"[ \t]*EC\.presence_of_element_located\(\s*\(\s*By\.CSS_SELECTOR\s*,\s*"
            r"[\"']iframe\[(?:name|title)=[^\]\n]*\][^\"'\n]*[\"']\s*\)\s*\n?"
            r"[ \t]*\)\s*\n?"
            r"[ \t]*\)\s*\n"
        )
        code_text = dead_iframe_wait_pattern.sub("", code_text)

        if removed_names:
            logger.info(
                "fast_generate_script: removed model-invented iframe-switching "
                "helper(s) %s and their call sites — _resolve_interactable "
                "already handles this",
                removed_names,
            )

        if had_crlf:
            code_text = code_text.replace("\n", "\r\n")
        return code_text

    def _ensure_resilient_interactable_helper(code_text: str) -> str:
        """Inject resilient interactable helper and normalize common click waits."""
        code_text = _ensure_no_manual_iframe_switching(code_text)
        helper = '''
def _resolve_interactable(driver, locator, *, target_label="", timeout=20):
    """Return the best visible+enabled match. Searches the main document AND
    every iframe on the page (crawled elements tagged '[in iframe: X]' only
    exist inside a nested iframe and were never reachable with a plain
    top-level find_element), then expands the nearest collapsed ancestor if
    needed. Leaves the driver positioned in the correct frame context for
    whatever it returns."""
    by, value = locator

    def _score(el):
        score = 0
        try:
            if el.is_displayed():
                score += 100
            if el.is_enabled():
                score += 40
        except Exception:
            return -1
        txt = (el.text or "").strip().lower()
        href = (el.get_attribute("href") or "").strip().lower()
        if target_label:
            needle = target_label.strip().lower()
            if needle and needle in txt:
                score += 15
            if needle and needle in href:
                score += 15
        if href:
            score += 5
        return score

    def _rank_here():
        candidates = driver.find_elements(by, value)
        if not candidates:
            return None, []
        ranked = sorted(candidates, key=_score, reverse=True)
        return ranked[0], candidates

    def _best_current():
        driver.switch_to.default_content()
        best, candidates = _rank_here()
        if candidates:
            return best, candidates
        try:
            frame_count = len(driver.find_elements(By.TAG_NAME, "iframe"))
        except Exception:
            frame_count = 0
        for idx in range(frame_count):
            try:
                frames = driver.find_elements(By.TAG_NAME, "iframe")
                driver.switch_to.frame(frames[idx])
            except Exception:
                driver.switch_to.default_content()
                continue
            best, candidates = _rank_here()
            if candidates:
                return best, candidates
            driver.switch_to.default_content()
        return None, []

    best, candidates = _best_current()
    if not candidates:
        raise AssertionError(
            f"Element not found in DOM for locator=({by}, {value!r}) in the main document "
            "or any iframe. Selector may be incorrect."
        )

    if best is not None and best.is_displayed() and best.is_enabled():
        return best

    expanded_any = False
    for cand in candidates:
        try:
            ancestors = cand.find_elements(
                By.XPATH,
                "./ancestor::*[@aria-expanded='false' or @hidden or contains(@class,'collapsed')][1]",
            )
            if not ancestors:
                continue
            container = ancestors[0]
            toggles = container.find_elements(
                By.XPATH,
                ".//button[@aria-expanded='false' or @aria-controls] | .//*[@role='button' and @aria-expanded='false'] | .//summary",
            )
            if toggles:
                driver.execute_script("arguments[0].scrollIntoView({block: 'center'});", toggles[0])
                toggles[0].click()
                expanded_any = True
                continue
            if container.is_displayed() and container.is_enabled():
                driver.execute_script("arguments[0].scrollIntoView({block: 'center'});", container)
                container.click()
                expanded_any = True
        except Exception:
            continue

    try:
        return _wait(driver, timeout).until(
            lambda _d: next(
                (
                    el for el in sorted(driver.find_elements(by, value), key=_score, reverse=True)
                    if el.is_displayed() and el.is_enabled()
                ),
                False,
            )
        )
    except Exception as exc:
        if driver.find_elements(by, value):
            suffix = " after expanding nearest collapsed ancestor" if expanded_any else ""
            raise AssertionError(
                f"Element exists in DOM but is not interactable for locator=({by}, {value!r}){suffix}."
            ) from exc
        raise AssertionError(
            f"Element disappeared from DOM while waiting for locator=({by}, {value!r})."
        ) from exc


def _resolve_jde_grid_cell(driver, column, *, row_index=0, timeout=20):
    """Find an editable JDE grid cell by its column header text and row
    position. These cells (e.g. Item Number, Quantity, Lot/Serial in a
    line-item grid) have no name/id/aria-label/title of their own -- every
    cell in the row is otherwise identical in the DOM. Searches the main
    document and every iframe on the page, the same way
    _resolve_interactable does."""
    inspect_js = r"""
    var td = el.closest('td[colindex]');
    if (!td) { return null; }
    var colindex = td.getAttribute('colindex');
    var table = el.closest('table[id^="jdeGridData"]');
    if (!table || !table.id) { return null; }
    var m = table.id.match(/^jdeGridData(.+)\\.\\d+$/);
    if (!m) { return null; }
    var gridId = m[1];
    var header = document.getElementById('GC' + gridId + '.' + colindex);
    if (!header) { return null; }
    var headerText = (header.innerText || header.textContent || '');
    var rowEl = el.closest('tr[realrow]');
    var rowIndex = 0;
    if (rowEl && rowEl.getAttribute('realrow') !== null) {
        rowIndex = parseInt(rowEl.getAttribute('realrow'), 10);
        if (isNaN(rowIndex)) { rowIndex = 0; }
    } else {
        var rows = Array.from(table.querySelectorAll('td[colindex="' + colindex + '"]'));
        rowIndex = rows.indexOf(td);
    }
    return {headerText: headerText, rowIndex: rowIndex};
    """

    def _matches(el):
        try:
            if not el.is_displayed():
                return False
            info = driver.execute_script("return (function(el){" + inspect_js + "})(arguments[0]);", el)
        except Exception:
            return False
        if not info or not info.get("headerText"):
            return False
        header_text = " ".join(info["headerText"].split())
        return header_text == column and info.get("rowIndex", -1) == row_index

    def _search_here():
        for el in driver.find_elements(By.CSS_SELECTOR, "input, textarea"):
            if _matches(el):
                return el
        return None

    def _search_all_contexts():
        driver.switch_to.default_content()
        found = _search_here()
        if found is not None:
            return found
        try:
            frame_count = len(driver.find_elements(By.TAG_NAME, "iframe"))
        except Exception:
            frame_count = 0
        for idx in range(frame_count):
            try:
                frames = driver.find_elements(By.TAG_NAME, "iframe")
                driver.switch_to.frame(frames[idx])
            except Exception:
                driver.switch_to.default_content()
                continue
            found = _search_here()
            if found is not None:
                return found
            driver.switch_to.default_content()
        return None

    try:
        return _wait(driver, timeout).until(lambda _d: _search_all_contexts() or False)
    except Exception as exc:
        raise AssertionError(
            f"JDE grid cell not found for column={column!r} row_index={row_index}. "
            "The grid/row may not have rendered yet, or the column header text "
            "does not match exactly."
        ) from exc
'''

        if "def _resolve_interactable(driver, locator, *, target_label=\"\", timeout=20):" not in code_text:
            visible_block = re.search(
                r"(?ms)^def _visible\(driver, locator, timeout=20\):\n(?:^[ \t].*\n)+",
                code_text,
            )
            if visible_block:
                pos = visible_block.end()
                code_text = code_text[:pos] + "\n" + helper + "\n" + code_text[pos:]

        # Normalize common "wait for clickable then click" assignment patterns.
        def _repl_clickable_assign(m: re.Match) -> str:
            indent = m.group(1)
            var_name = m.group(2)
            timeout = m.group(3) or "20"
            locator = m.group(4)
            return (
                f"{indent}{var_name} = _resolve_interactable(driver, {locator}, "
                f"target_label={var_name!r}, timeout={timeout})"
            )

        code_text, rewrites_clickable = re.subn(
            r"(?ms)^([ \t]*)([A-Za-z_][A-Za-z0-9_]*)\s*=\s*_wait\(\s*driver\s*(?:,\s*(?:timeout\s*=\s*)?([0-9]+)\s*)?\)"
            r"\s*\.until\(\s*EC\.element_to_be_clickable\(\s*(\([^\n]+\))\s*\)\s*\)\s*$",
            _repl_clickable_assign,
            code_text,
        )

        # Normalize common "visibility wait assigned then clicked" patterns.
        def _repl_visible_assign(m: re.Match) -> str:
            indent = m.group(1)
            var_name = m.group(2)
            locator = m.group(3)
            timeout = m.group(4) or "20"
            return (
                f"{indent}{var_name} = _resolve_interactable(driver, {locator}, "
                f"target_label={var_name!r}, timeout={timeout})"
            )

        code_text, rewrites_visible = re.subn(
            r"(?ms)^([ \t]*)([A-Za-z_][A-Za-z0-9_]*)\s*=\s*_visible\(\s*driver\s*,\s*(\([^\n]+\))\s*(?:,\s*timeout\s*=\s*([0-9]+)\s*)?\)\s*$",
            _repl_visible_assign,
            code_text,
        )

        if rewrites_clickable or rewrites_visible:
            logger.info(
                "fast_generate_script: normalized %d clickable and %d visible assignment(s) to _resolve_interactable",
                rewrites_clickable,
                rewrites_visible,
            )
        return code_text

    code = _ensure_sso_preamble(code)
    code = _ensure_visibility_wait_diagnostics(code)
    code = _ensure_resilient_interactable_helper(code)

    # Enforce non-negotiable script contract before spending repair budget.
    contract_errors = _validate_script_contract(code)
    if contract_errors:
        logger.warning("Generated script violates contract (%d issue(s)) — repairing",
                       len(contract_errors))
        fixed = repair_script(
            code,
            "Script contract violations:\n  "
            + "\n  ".join(contract_errors)
            + "\n\nFix ONLY these contract issues while preserving behavior and step() blocks.",
            provider,
        )
        if fixed:
            code = fixed
            code = _ensure_sso_preamble(code)
            code = _ensure_visibility_wait_diagnostics(code)
            code = _ensure_resilient_interactable_helper(code)
            contract_errors = _validate_script_contract(code)
    if contract_errors:
        logger.error("Script still violates contract after repair — rejecting")
        for err in contract_errors:
            logger.error("  %s", err)
        return None

    nav_issues = validate_generated_script_navigation(scenario_contract, code)
    if nav_issues:
        logger.warning(
            "Generated script violates navigation/setup contract (%d issue(s)) — repairing",
            len(nav_issues),
        )
        fixed = repair_script(
            code,
            "Navigation/setup contract issues:\n  "
            + "\n  ".join(nav_issues)
            + "\n\nFix ONLY these issues while preserving behavior and step() blocks.",
            provider,
        )
        if fixed:
            code = fixed
            code = _ensure_sso_preamble(code)
            code = _ensure_visibility_wait_diagnostics(code)
            code = _ensure_resilient_interactable_helper(code)
            nav_issues = validate_generated_script_navigation(scenario_contract, code)
    if nav_issues:
        logger.error("Script still violates navigation/setup contract after repair — rejecting")
        for err in nav_issues:
            logger.error("  %s", err)
        return None

    # Evidence recording is not optional, so it is not left to whether the
    # model happened to follow the instruction. It frequently does not: a
    # 14B model given a page and a scenario writes the Selenium it knows and
    # drops the wrapper. Checking here costs one repair call; not checking
    # produces a script that runs green and stores no step ledger at all,
    # which is only discovered when someone goes looking for the evidence.
    if "NextGen2_evidence" not in code:
        logger.warning("Generated script does not record steps — repairing")
        fixed = repair_script(
            code,
            "The script does not record its steps. Every numbered step must be "
            "wrapped in the step() context manager so the run produces evidence:\n"
            "    from NextGen2_evidence import step, note_skipped\n"
            "    with step(driver, 1, \"<step text>\", \"<expected>\"):\n"
            "        <the selenium code for that step>\n"
            "Rewrite the SAME test, unchanged in behaviour, with one "
            "`with step(...)` block per numbered step and the assertions for "
            "each step inside its own block. Keep the import at the top.",
            provider,
        )
        if fixed and "NextGen2_evidence" in fixed:
            code = fixed
            code = _ensure_sso_preamble(code)
            code = _ensure_visibility_wait_diagnostics(code)
            code = _ensure_resilient_interactable_helper(code)
        else:
            # Better a recorded failure than a script that silently produces
            # no evidence — the caller marks the generation incomplete.
            logger.error("Script still records no steps after repair — rejecting")
            return None

    # Selectors are checked against the page rather than taken on trust. An
    # invented attribute costs a whole run to discover otherwise, and the
    # failure looks like a broken application rather than a bad script.
    # Correct what can be corrected outright, before spending a repair call.
    # (Skipped if we don't have the original element dict.)
    if elements:
        if _has_sso_preamble(code):
            logger.info(
                "Selector checks: excluding SSO preamble block from app-map validation"
            )
        code, applied = _autofix_selectors(code, elements)
        for f in applied:
            logger.info("Selector corrected from the element map: %s", f)

        problems = _validate_selectors(code, elements)
        if problems:
            logger.warning("Generated script uses %d impossible selector(s) — repairing",
                           len(problems))
            for p in problems[:5]:
                logger.warning("   %s", p)
            fixed = repair_script(
                code,
                "These selectors cannot match anything on the page:\n  "
                + "\n  ".join(problems)
                + "\n\nUse only attributes the element actually has, exactly as "
                  "listed. Change nothing else — keep the step() blocks, the "
                  "step numbers and the step text as they are.",
                provider,
            )
            if fixed and "NextGen2_evidence" in fixed:
                remaining = _validate_selectors(fixed, elements)
                if len(remaining) < len(problems):
                    code = fixed
                    code = _ensure_sso_preamble(code)
                    code = _ensure_visibility_wait_diagnostics(code)
                    code = _ensure_resilient_interactable_helper(code)
                    if remaining:
                        logger.warning("Still %d impossible selector(s) after repair",
                                       len(remaining))
    else:
        logger.info(
            "Skipping selector autofix and validation (no element dict provided)"
        )

    # Reject code that won't even parse — better to record a clear failure
    # than to write a broken file that pytest can't collect.
    # A malformed try/finally is a common model slip. Repairing costs one
    # call; giving up costs the whole scenario.
    for _ in range(2):
        try:
            ast.parse(code)
            missing = find_undefined_names(code)
            if missing:
                # A missing import is mechanical: add the line, touch nothing
                # else, call no model. Regenerating the file to fix one import
                # was reverting correct code — notably the selectors already
                # checked against the element map.
                code, unresolved = _add_missing_imports(code, missing)
                if unresolved:
                    code, unresolved, applied_aliases = _fix_common_undefined_aliases(
                        code, unresolved
                    )
                    for alias_fix in applied_aliases:
                        logger.info("Applied deterministic alias fix: %s", alias_fix)
                if unresolved:
                    logger.warning(
                        "Script uses name(s) with no known import: %s — leaving it "
                        "as generated. Rewriting the whole file to chase an unknown "
                        "name changes working code and hides the real defect.",
                        ", ".join(unresolved),
                    )
                    return _ensure_resilient_interactable_helper(
                        _ensure_visibility_wait_diagnostics(_ensure_sso_preamble(code))
                    )
                imported_names = [name for name in missing if name in _KNOWN_IMPORTS]
                if imported_names:
                    logger.info("Added missing import(s) for: %s", ", ".join(imported_names))
                continue
            return _ensure_resilient_interactable_helper(
                _ensure_visibility_wait_diagnostics(_ensure_sso_preamble(code))
            )
        except SyntaxError as se:
            logger.warning("Generated script has a syntax error (line %s: %s) — repairing",
                           se.lineno, se.msg)
            fixed = repair_script(code, f"SyntaxError line {se.lineno}: {se.msg}", provider)
            if not fixed:
                return None
            code = fixed
            code = _ensure_sso_preamble(code)
            code = _ensure_visibility_wait_diagnostics(code)
            code = _ensure_resilient_interactable_helper(code)

    logger.warning("Could not obtain syntactically valid code")
    return None


# ────────────────────────────── helpers ──────────────────────────────


def find_undefined_names(code: str) -> list[str]:
    """
    Report names the script uses but never imports or defines.

    Why this exists: a missing import is syntactically valid, so ast.parse()
    accepts it. If the offending line sits in a branch that only runs when
    HEADED=1 — a screenshot pause, for example — verification (which runs
    headless) passes and the script only fails later, in front of someone.
    That is exactly how "time.sleep(1)" without "import time" reached a live
    run.

    Args:
        code: Generated Python source.

    Returns:
        Sorted list of names that appear to be undefined.
    """
    import builtins

    try:
        tree = ast.parse(code)
    except SyntaxError:
        return []

    defined: set[str] = set(dir(builtins))
    used: dict[str, int] = {}

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for a in node.names:
                defined.add((a.asname or a.name).split(".")[0])
        elif isinstance(node, ast.ImportFrom):
            for a in node.names:
                defined.add(a.asname or a.name)
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            defined.add(node.name)
            args = node.args
            for a in (list(args.args) + list(args.posonlyargs) + list(args.kwonlyargs)):
                defined.add(a.arg)
            if args.vararg:
                defined.add(args.vararg.arg)
            if args.kwarg:
                defined.add(args.kwarg.arg)
        elif isinstance(node, ast.ClassDef):
            defined.add(node.name)
        elif isinstance(node, ast.Name) and isinstance(node.ctx, (ast.Store, ast.Del)):
            defined.add(node.id)
        elif isinstance(node, ast.ExceptHandler) and node.name:
            defined.add(node.name)
        elif isinstance(node, (ast.comprehension,)):
            for t in ast.walk(node.target):
                if isinstance(t, ast.Name):
                    defined.add(t.id)
        elif isinstance(node, ast.Global):
            defined.update(node.names)

    for node in ast.walk(tree):
        if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load):
            used.setdefault(node.id, getattr(node, "lineno", 0))

    return sorted(n for n in used if n not in defined)


# Names a generated Selenium test can use, and the import each one needs.
# Every entry is unambiguous — there is exactly one import that makes the
# name valid in this kind of script, which is what allows the fix to be
# mechanical rather than a question for the model.
_KNOWN_IMPORTS = {
    "os": "import os",
    "re": "import re",
    "sys": "import sys",
    "time": "import time",
    "json": "import json",
    "random": "import random",
    "subprocess": "import subprocess",
    "pytest": "import pytest",
    "datetime": "from datetime import datetime",
    "Path": "from pathlib import Path",
    "webdriver": "from selenium import webdriver",
    "By": "from selenium.webdriver.common.by import By",
    "Keys": "from selenium.webdriver.common.keys import Keys",
    "Options": "from selenium.webdriver.chrome.options import Options",
    "WebDriverWait": "from selenium.webdriver.support.ui import WebDriverWait",
    "Select": "from selenium.webdriver.support.ui import Select",
    "EC": "from selenium.webdriver.support import expected_conditions as EC",
    "ActionChains": "from selenium.webdriver.common.action_chains import ActionChains",
    "TimeoutException": "from selenium.common.exceptions import TimeoutException",
    "NoSuchElementException": "from selenium.common.exceptions import NoSuchElementException",
    "StaleElementReferenceException":
        "from selenium.common.exceptions import StaleElementReferenceException",
    "ElementNotInteractableException":
        "from selenium.common.exceptions import ElementNotInteractableException",
    "WebDriverException": "from selenium.common.exceptions import WebDriverException",
    "step": "from NextGen2_evidence import step, note_skipped",
    "note_skipped": "from NextGen2_evidence import step, note_skipped",
}


def _add_missing_imports(code: str, missing: list) -> tuple[str, list]:
    """
    Insert the import lines a script needs, without regenerating it.

    A missing import is a one-line, mechanical defect with exactly one
    correct fix. Sending the whole file to the model to have that line added
    means the model rewrites all forty lines — and it does not come back the
    same. That is how corrected selectors were being reverted: a missing
    `import time` triggered a full regeneration through a prompt written for
    runtime failures, which then reintroduced the selectors the element map
    had already ruled out.

    Args:
        code: The generated script.
        missing: Names reported undefined by find_undefined_names().

    Returns:
        (code with the resolvable imports added, names that could not be
        resolved). A non-empty second element means the caller should leave
        the script alone rather than reach for the model — an unknown name
        is a real defect in the script, and regenerating it hides that
        behind a fresh set of changes.
    """
    unresolved = [n for n in missing if n not in _KNOWN_IMPORTS]
    statements = []
    for name in missing:
        stmt = _KNOWN_IMPORTS.get(name)
        if stmt and stmt not in code and stmt not in statements:
            statements.append(stmt)

    if not statements:
        return code, unresolved

    # Insert after the existing imports so the file keeps a conventional
    # shape, and after a module docstring if there is one — putting an
    # import above it would silently demote the docstring to a bare string.
    insert_at = 0
    try:
        tree = ast.parse(code)
        for node in tree.body:
            if isinstance(node, (ast.Import, ast.ImportFrom)):
                insert_at = max(insert_at, node.end_lineno or 0)
        if insert_at == 0 and tree.body:
            first = tree.body[0]
            if isinstance(first, ast.Expr) and isinstance(first.value, ast.Constant) \
                    and isinstance(first.value.value, str):
                insert_at = first.end_lineno or 0
    except SyntaxError:
        return code, unresolved

    lines = code.splitlines()
    lines[insert_at:insert_at] = statements
    return "\n".join(lines), unresolved


def _fix_common_undefined_aliases(code: str, unresolved: list[str]) -> tuple[str, list[str], list[str]]:
    """
    Apply safe, deterministic alias fixes for known model shorthand names.

    Today this handles the common case where the model uses `d` as a
    shorthand for the Selenium `driver` fixture without defining it.
    """
    if not unresolved:
        return code, unresolved, []

    remaining = list(unresolved)
    applied: list[str] = []

    if "d" in remaining:
        fixed, changed = _insert_alias_in_test_function(code, alias="d", target="driver")
        if changed:
            code = fixed
            remaining = [n for n in remaining if n != "d"]
            applied.append("d = driver")

    return code, remaining, applied


def _insert_alias_in_test_function(code: str, alias: str, target: str) -> tuple[str, bool]:
    """Insert `alias = target` into the first test function that has target in args."""
    try:
        tree = ast.parse(code)
    except SyntaxError:
        return code, False

    lines = code.splitlines()
    for node in tree.body:
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        if not node.name.startswith("test_"):
            continue

        args = node.args
        arg_names = {a.arg for a in (list(args.args) + list(args.posonlyargs) + list(args.kwonlyargs))}
        if args.vararg:
            arg_names.add(args.vararg.arg)
        if args.kwarg:
            arg_names.add(args.kwarg.arg)
        if target not in arg_names:
            continue

        fn_start = max(1, node.lineno)
        fn_end = max(fn_start, node.end_lineno or fn_start)
        fn_text = "\n".join(lines[fn_start - 1:fn_end])
        if re.search(rf"(?m)^\s*{re.escape(alias)}\s*=\s*{re.escape(target)}\s*$", fn_text):
            return code, False

        if not node.body:
            continue

        insert_at = node.body[0].lineno
        first_stmt = node.body[0]
        if (
            isinstance(first_stmt, ast.Expr)
            and isinstance(first_stmt.value, ast.Constant)
            and isinstance(first_stmt.value.value, str)
        ):
            insert_at = (first_stmt.end_lineno or first_stmt.lineno) + 1

        # Reuse existing indentation from the first statement in the body.
        body_line = lines[max(0, node.body[0].lineno - 1)]
        indent_match = re.match(r"\s*", body_line)
        indent = indent_match.group(0) if indent_match else "    "

        alias_line = f"{indent}{alias} = {target}"
        lines.insert(max(0, insert_at - 1), alias_line)
        return "\n".join(lines), True

    return code, False


def _strip_fences(text: str) -> str:
    """Remove ```python fences the model may add despite instructions."""
    text = re.sub(r"^```(?:python)?\s*", "", text.strip())
    return re.sub(r"\s*```$", "", text).strip()


def _validate_script_contract(code: str) -> list[str]:
    """
    Validate non-negotiable generation contract for runtime data usage.

    Returns a list of actionable violations. Empty list means contract holds.
    """
    issues: list[str] = []

    if "APP_URL = os.environ.get(\"APP_URL\") or os.environ.get(\"NETRA_URL\")" not in code:
        issues.append("Missing APP_URL/NETRA_URL runtime URL pattern.")
    if "driver.get(APP_URL)" not in code:
        issues.append("Missing driver.get(APP_URL); URL must come from runtime env.")
    if re.search(r"driver\.get\(\s*['\"]https?://", code):
        issues.append("Hard-coded URL in driver.get(...). Use APP_URL only.")

    if "def get_data(" not in code:
        issues.append("Missing get_data(...) helper for generic TEST_* lookup.")

    if re.search(r"os\.environ\.get\(\s*['\"]TEST_USERNAME['\"]\s*,", code):
        issues.append("Fallback default for TEST_USERNAME is not allowed.")
    if re.search(r"os\.environ\.get\(\s*['\"]TEST_PASSWORD['\"]\s*,", code):
        issues.append("Fallback default for TEST_PASSWORD is not allowed.")

    if re.search(r"send_keys\(\s*['\"][^'\"]+['\"]\s*\)", code):
        issues.append("Literal value passed to send_keys(...). Use get_data(...) for test inputs.")

    if "NextGen2_evidence" not in code or "with step(driver," not in code:
        issues.append("Missing evidence wrapper (NextGen2_evidence + with step blocks).")

    # Unsafe wait predicates are a common source of false timeouts in
    # post-login checks where elements may disappear from the DOM.
    if re.search(r"WebDriverWait\([^\)]*\)\.until\(\s*lambda[\s\S]*?find_element\(", code):
        issues.append(
            "Unsafe WebDriverWait lambda uses find_element(...). "
            "Use expected_conditions or find_elements(...) for optional/vanishing elements."
        )

    return issues




VALID_CATEGORIES = {"positive", "negative", "edge", "boundary", "security"}
VALID_PRIORITIES = {"P0", "P1", "P2", "P3"}


def _parse_steps(raw, identifiers: Optional[set] = None) -> Optional[list]:
    """
    Turn the model's "steps" array into validated, densely numbered steps.

    Defensive throughout: a malformed or missing steps array must degrade to
    None — a one-line test case, exactly what this system produced before —
    and never raise. A local 14B model will sometimes return a string, a
    dict, or nothing at all where an array was asked for.

    Args:
        raw: Whatever the model put in "steps".
        identifiers: Every identifying string on the page, for the matched
            check. None skips the check (the model's claim stands).

    Returns:
        List of step dicts, or None if there is nothing usable.
    """
    if not isinstance(raw, list):
        return None

    steps = []
    for item in raw:
        if isinstance(item, str):
            step_text, expected, hint, claimed = item.strip(), "", None, True
            data_input, wait_condition = None, "none"
        elif isinstance(item, dict):
            step_text = str(item.get("step") or "").strip()
            expected = str(item.get("expected") or "").strip()
            hint = item.get("element_hint")
            hint = str(hint).strip()[:120] if hint else None
            claimed = item.get("matched", True)
            claimed = claimed if isinstance(claimed, bool) else str(claimed).lower() != "false"
            data_input = item.get("data_input")
            data_input = str(data_input).strip()[:80] if data_input else None
            wait_condition = item.get("wait_condition")
            wait_condition = str(wait_condition).strip().lower() if wait_condition else "none"
            if wait_condition not in {"url_change", "element_visible", "none"}:
                wait_condition = "none"
        else:
            continue
        if not step_text:
            continue
        steps.append({
            # Numbered below, from position — never from the model, which
            # skips and repeats numbers.
            "step": step_text[:300],
            "expected": expected[:300],
            "element_hint": hint,
            "matched": claimed,
            "data_input": data_input,
            "wait_condition": wait_condition,
        })

    if not steps:
        return None
    if identifiers is not None:
        steps = _apply_matched(steps, identifiers)
    for i, s in enumerate(steps, 1):
        s["n"] = i
    return steps


def _parse_scenarios(text: str, identifiers: Optional[set] = None) -> list[dict]:
    """
    Parse the model's reply into a flat list of test cases, each carrying
    the scenario it belongs to and the requirement it traces to.

    Expected shape (grouped):
        [{"scenario": "Settings menu", "requirement_id": "NET-020",
          "test_cases": [{"title": ..., "steps": [{"step", "expected"}]}]}]

    Also accepts the older flat shapes (list of objects, list of strings)
    so nothing breaks if the model ignores the grouping instruction. Those
    paths produce steps=None, which is a one-line test case — the same thing
    this function returned before steps existed.

    Args:
        text: The model's raw reply.
        identifiers: Page element identifiers, for the matched check.

    Returns:
        List of dicts: title, description, steps, category, priority,
        scenario, requirement_id.
    """
    text = _strip_fences(text)
    data = None
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        m = re.search(r"\[.*\]", text, re.S)
        if m:
            try:
                data = json.loads(m.group(0))
            except json.JSONDecodeError:
                data = None

    out: list[dict] = []
    if isinstance(data, list):
        for item in data:
            # grouped shape
            if isinstance(item, dict) and isinstance(item.get("test_cases"), list):
                scenario = str(item.get("scenario") or "General").strip()[:80]
                req = item.get("requirement_id")
                req = str(req).strip()[:40] if req else None
                for tc in item["test_cases"]:
                    steps = None
                    if isinstance(tc, dict):
                        title = str(tc.get("title") or "").strip()
                        cat = str(tc.get("category") or "").lower().strip()
                        pri = str(tc.get("priority") or "").upper().strip()
                        deriv = str(tc.get("derivation") or "").lower().strip()
                        steps = _parse_steps(tc.get("steps"), identifiers)
                        preconditions = [
                            str(p).strip() for p in (tc.get("preconditions") or [])
                            if str(p).strip()
                        ]
                    elif isinstance(tc, str):
                        title, cat, pri, deriv, preconditions = tc.strip(), "", "", "", []
                    else:
                        continue
                    if not title:
                        continue
                    out.append({
                        "title": title[:120],
                        "description": flatten_steps(steps) if steps else title,
                        "steps": steps,
                        "preconditions": preconditions,
                        "category": cat if cat in VALID_CATEGORIES else None,
                        # The local model often omits derivation even when it
                        # classifies the category. Fall back to the category:
                        # a positive confirms the stated criterion ("stated");
                        # negatives/edges are reasoned from the behaviour type
                        # ("inferred") — SURVEY_PROMPT's own guidance. Only when
                        # the model gave nothing usable at all is it left null.
                        "derivation": (
                            deriv if deriv in ("stated", "inferred")
                            else ("stated" if cat == "positive"
                                  else ("inferred" if cat in VALID_CATEGORIES else None))
                        ),
                        "priority": pri if pri in VALID_PRIORITIES else None,
                        "scenario": scenario,
                        "requirement_id": req,
                    })
            # flat object shape
            elif isinstance(item, dict) and item.get("title"):
                title = str(item["title"]).strip()
                cat = str(item.get("category") or "").lower().strip()
                pri = str(item.get("priority") or "").upper().strip()
                preconditions = [
                    str(p).strip() for p in (item.get("preconditions") or [])
                    if str(p).strip()
                ]
                out.append({
                    "title": title[:120],
                    "description": title,
                    "steps": _parse_steps(item.get("steps"), identifiers),
                    "preconditions": preconditions,
                    "category": cat if cat in VALID_CATEGORIES else None,
                    "priority": pri if pri in VALID_PRIORITIES else None,
                    "scenario": str(item.get("scenario") or "General")[:80],
                    "requirement_id": item.get("requirement_id"),
                })
            # plain string
            elif isinstance(item, str) and item.strip():
                out.append({
                    "title": item.strip()[:120], "description": item.strip(),
                    "steps": None, "preconditions": [],
                    "category": None, "priority": None,
                    "scenario": "General", "requirement_id": None,
                })
    if out:
        return out

    return [{
        "title": s[:120], "description": s, "steps": None, "preconditions": [],
        "category": None, "priority": None,
        "scenario": "General", "requirement_id": None,
    } for s in _parse_string_array(text)]

def _parse_string_array(text: str) -> list[str]:
    """
    Parse a JSON array of strings from the model's reply, tolerating
    surrounding prose or fences.
    """
    text = _strip_fences(text)
    try:
        data = json.loads(text)
        if isinstance(data, list):
            return [str(x).strip() for x in data if str(x).strip()]
    except json.JSONDecodeError:
        pass

    # fall back to the first [...] block in the text
    m = re.search(r"\[.*\]", text, re.S)
    if m:
        try:
            data = json.loads(m.group(0))
            if isinstance(data, list):
                return [str(x).strip() for x in data if str(x).strip()]
        except json.JSONDecodeError:
            pass

    # last resort: bullet or numbered lines
    lines = [
        re.sub(r'^\s*(?:[-*\d.)\]]+)\s*', '', ln).strip(' "\'')
        for ln in text.splitlines()
        if ln.strip() and len(ln.strip()) > 15
    ]
    return [ln for ln in lines if ln][:10]


# ──────────────────────────── repair ────────────────────────────

REPAIR_PROMPT = """The Selenium test below did not work.

SCRIPT:
__CODE__

PROBLEM:
__ERROR__

Common causes — check these first:

1. InvalidSelectorException -> a CSS selector that isn't valid CSS,
   almost always :contains(). CSS CANNOT select by text. Use XPath:
       (By.XPATH, "//div[@role='menuitem' and normalize-space()='Profile']")

2. TimeoutException -> waiting for something that never appears, usually
   an assumed outcome. Assert only what is verifiable: an element is
   displayed/enabled, or an input holds the text you typed.

3. SyntaxError -> the code doesn't parse. Check that try/except/finally
   blocks are complete and correctly indented.

Do not assume the input clears, that a response appears, or that the URL
changes.

KEEP THE STEP RECORDING EXACTLY AS IT IS.
If the script imports NextGen2_evidence and wraps its steps in
`with step(driver, n, "...", "..."):` blocks, the corrected script MUST
still do so — same import, same blocks, same step numbers and text. Fix
the code INSIDE the blocks. Removing the wrapper produces a test that
appears to work and records no evidence at all, which is worse than the
failure you are being asked to fix.

WAIT PREDICATE SAFETY:
- Do not use find_element(...) directly inside WebDriverWait(...).until(lambda ...)
    when the element may be absent after success.
- Use expected_conditions (for example invisibility_of_element_located) or
    use find_elements(...) and derive a boolean without exceptions.

Output ONLY the corrected Python code. No explanation, no fences.
"""


def repair_script(code: str, error: str, provider: LLMProvider) -> Optional[str]:
    """
    Send a failing script and its error back to the model for a fix.

    Args:
        code: The script that failed.
        error: Syntax error or pytest output.
        provider: LLM provider.

    Returns:
        Corrected source, or None if the model didn't return usable code.
    """
    prompt = (REPAIR_PROMPT
              .replace("__CODE__", code)
              .replace("__ERROR__", str(error)[:2500]))
    tool = provider.build_tool([])
    contents = [provider.user_text_content(prompt)]
    response = provider.call_model("You reply with Python code only.", tool, contents)
    fixed = " ".join(
        p.text for p in response.candidates[0].content.parts if getattr(p, "text", "")
    ).strip()
    fixed = _strip_fences(fixed)
    if "def test_" not in fixed:
        return None
    return fixed


_SSO_PREAMBLE_START = "# SSO PREAMBLE — Azure Entra ID authentication"
_SSO_PREAMBLE_END = "# End SSO preamble"


def _has_sso_preamble(code: str) -> bool:
    return _SSO_PREAMBLE_START in code and _SSO_PREAMBLE_END in code


def _extract_sso_preamble(code: str) -> Optional[str]:
    m = re.search(
        rf"(?ms)^[ \t]*{re.escape(_SSO_PREAMBLE_START)}.*?^[ \t]*{re.escape(_SSO_PREAMBLE_END)}\s*$",
        code,
    )
    return m.group(0) if m else None


def _split_sso_preamble(code: str) -> tuple[str, str, str]:
    """Split code into (before_sso, sso_block, after_sso)."""
    m = re.search(
        rf"(?ms)^[ \t]*{re.escape(_SSO_PREAMBLE_START)}.*?^[ \t]*{re.escape(_SSO_PREAMBLE_END)}\s*$",
        code,
    )
    if not m:
        return code, "", ""
    return code[:m.start()], m.group(0), code[m.end():]


def _restore_sso_preamble(original_code: str, repaired_code: str) -> tuple[str, bool]:
    """Reinsert original SSO preamble into repaired code when it was dropped."""
    preamble = _extract_sso_preamble(original_code)
    if not preamble:
        return repaired_code, False

    after_get = re.search(r"(?m)^[ \t]*driver\.get\(APP_URL\).*$", repaired_code)
    if after_get:
        pos = after_get.end()
        return repaired_code[:pos] + "\n\n" + preamble + repaired_code[pos:], True

    before_step = re.search(r"(?m)^[ \t]*with step\(driver,", repaired_code)
    if before_step:
        pos = before_step.start()
        return repaired_code[:pos] + preamble + "\n\n" + repaired_code[pos:], True

    return repaired_code, False


def verify_and_repair(
    script_path: str,
    code: str,
    provider: LLMProvider,
    target_url: str,
    max_repairs: int = 1,
    elements=None,
    extra_env: Optional[dict] = None,
    enforce_sso_preamble: bool = False,
) -> tuple[str, bool, Optional[str]]:
    """
    Run the generated script and, if it fails, feed the real failure back
    to the model and try again.

    This is the generate -> verify -> repair loop. Without it a first-shot
    mistake (a wrong assertion, an invalid selector) ships as a broken
    test; with it, most are corrected automatically.

    Args:
        script_path: The script's eventual home. Used for logging only —
            verification runs entirely in a temp directory. Writing here
            mid-loop restarted `uvicorn --reload` and killed the pipeline,
            so only the caller writes to this path, once, at the end.
        code: The generated source.
        provider: LLM provider.
        target_url: Passed to the test as NETRA_URL.
        max_repairs: How many repair attempts to make.

    Returns:
        (final_code, passed, reason_code)
    """
    import os
    import subprocess
    import tempfile

    env = os.environ.copy()
    env["NETRA_URL"] = target_url
    env["HEADED"] = "0"          # verification always runs headless
    if extra_env:
        env.update({str(k): str(v) for k, v in extra_env.items() if v is not None})

    # Fast fail diagnostics: if the script asks for credentials but they are
    # absent from the verify environment, repairs cannot make it pass.
    needs_username = bool(re.search(r'get_data\(\s*["\']username["\']', code))
    needs_password = bool(re.search(r'get_data\(\s*["\']password["\']', code))
    if needs_username and not env.get("TEST_USERNAME"):
        logger.warning(
            "verify_and_repair: script expects TEST_USERNAME but it is missing from env"
        )
    if needs_password and not env.get("TEST_PASSWORD"):
        logger.warning(
            "verify_and_repair: script expects TEST_PASSWORD but it is missing from env"
        )

    def _classify_verification_failure(output: str) -> str:
        text = (output or "").lower()

        if "nameerror" in text:
            return "script_name_error"

        if "fixture 'driver' not found" in text or "fixture \"driver\" not found" in text:
            return "fixture_missing"

        if (
            "missing required test data: test_username" in text
            or "missing required test data: test_password" in text
            or (needs_username and not env.get("TEST_USERNAME"))
            or (needs_password and not env.get("TEST_PASSWORD"))
        ):
            return "auth_context_missing"

        if (
            "azure entra" in text
            or "login.microsoftonline.com" in text
            or "timed out waiting for azure entra redirect" in text
            or "authentication" in text and "failed" in text
        ):
            return "auth_login_failed"

        if (
            "assert" in text
            and (
                "current_url" in text
                or "url" in text and "change" in text
                or "redirect" in text
                or "navigate" in text
            )
        ):
            return "flow_navigation_missing"

        if "nosuchelementexception" in text or "elementnotinteractableexception" in text:
            return "selector_unstable_after_repair"

        if "timed out" in text or "timeoutexception" in text:
            return "verify_timeout"

        return "verify_failed"

    # Generated scripts import NextGen2_evidence, which lives in
    # generated_tests/. Verification deliberately runs in a temp dir, where
    # pytest only puts the candidate's own directory on sys.path — so every
    # evidence-using script failed collection with ModuleNotFoundError, was
    # sent for repair, and came back with the evidence block stripped. The
    # temp dir is still right; it just needs to see that one module.
    env["PYTHONPATH"] = os.pathsep.join(
        p for p in [str(Path(__file__).parent.parent / "generated_tests"),
                    env.get("PYTHONPATH", "")] if p
    )

    # Verify in a temp directory, NOT in generated_tests/. Writing there
    # while `uvicorn --reload` is watching restarts the backend mid-run and
    # kills the pipeline. Only the final, verified script is written to the
    # real path by the caller.
    tmp_dir = tempfile.mkdtemp(prefix="NextGen2_verify_")
    tmp_script = os.path.join(tmp_dir, "test_candidate.py")

    # Generated tests rely on a shared driver fixture from conftest.py.
    # Verification runs in an isolated temp directory, so write a local
    # conftest there to keep fixture resolution identical to normal suite runs.
    try:
        from service.pytest_helpers import generate_conftest

        if not generate_conftest(Path(tmp_dir)):
            logger.warning(
                "verify_and_repair: could not generate temp conftest; "
                "driver fixture may be unavailable during verification"
            )
    except Exception as exc:
        logger.warning("verify_and_repair: temp conftest setup failed: %s", exc)

    # What the generator produced, before any repair. It has already been
    # checked against the element map, so its selectors are known to be
    # possible. A repair is a fresh rewrite from the model's own idea of the
    # page and can come back WORSE — swapping a verified By.ID for an
    # invented attribute selector. If no repair actually verifies, this is
    # what ships, rather than the last and least-checked attempt.
    original_code = code
    require_sso_block = enforce_sso_preamble and _has_sso_preamble(original_code)
    original_sso_preamble = _extract_sso_preamble(original_code) if require_sso_block else None
    if enforce_sso_preamble and not require_sso_block:
        logger.warning(
            "verify_and_repair: SSO enforcement requested but generated code has no SSO preamble"
        )

    for attempt in range(max_repairs + 1):
        Path(tmp_script).write_text(code, encoding="utf-8")
        try:
            proc = subprocess.run(
                ["pytest", tmp_script, "-q", "--tb=short", "--no-header"],
                capture_output=True, text=True, timeout=180, env=env,
            )
        except Exception as e:
            logger.warning("Could not run verification: %s", e)
            return original_code, False, "verify_subprocess_error"

        combined_output = f"{proc.stdout or ''}\n{proc.stderr or ''}"

        if proc.returncode == 0:
            logger.info("Generated script verified: PASSED%s",
                        f" after {attempt} repair(s)" if attempt else "")
            shutil.rmtree(tmp_dir, ignore_errors=True)
            return code, True, None

        if attempt >= max_repairs:
            failure_reason = _classify_verification_failure(combined_output)
            logger.warning("Script still failing after %d repair(s)", max_repairs)
            logger.warning("verify_and_repair: final failure reason=%s", failure_reason)
            if combined_output.strip():
                logger.warning(
                    "verify_and_repair: final pytest output tail:\n%s",
                    combined_output[-1500:],
                )
            shutil.rmtree(tmp_dir, ignore_errors=True)
            return original_code, False, failure_reason

        logger.info("Generated script failed — repairing (attempt %d/%d) for %s",
                    attempt + 1, max_repairs, Path(script_path).name)
        fixed = repair_script(code, combined_output[-2500:], provider)
        if not fixed:
            return original_code, False, "repair_unavailable"

        # SSO-enabled apps must keep their auth bootstrap. Repairs can rewrite
        # the file and accidentally drop it; restore from the original script
        # before validation.
        if require_sso_block and not _has_sso_preamble(fixed):
            restored, ok = _restore_sso_preamble(original_code, fixed)
            if ok:
                logger.warning(
                    "Repair removed SSO preamble — restored original SSO block before re-verify"
                )
                fixed = restored
            else:
                logger.warning(
                    "Repair removed SSO preamble and restoration failed — keeping original"
                )
                return original_code, False, "repair_removed_sso_preamble"

        # A repair may keep markers but rewrite SSO internals. Preserve the
        # original canonical preamble to keep auth flow deterministic.
        if require_sso_block and original_sso_preamble:
            repaired_sso_preamble = _extract_sso_preamble(fixed)
            if repaired_sso_preamble and repaired_sso_preamble.strip() != original_sso_preamble.strip():
                fixed = fixed.replace(repaired_sso_preamble, original_sso_preamble, 1)
                logger.warning(
                    "Repair rewrote SSO preamble internals — restored canonical original SSO block"
                )

        try:
            ast.parse(fixed)
        except SyntaxError:
            return original_code, False, "repair_syntax_invalid"

        # A repair that drops the step recording is not a repair. The model
        # rewrites the script from what it knows a Selenium test looks like
        # and quietly discards the unfamiliar wrapper — which is how a
        # verified-looking script ended up storing no evidence at all.
        # Keep the original rather than accept a fix that costs the ledger.
        if "NextGen2_evidence" in code and "NextGen2_evidence" not in fixed:
            logger.warning("Repair removed the step recording — keeping the "
                           "original script and reporting it unverified")
            return original_code, False, "repair_removed_evidence"

        # Re-apply the deterministic selector corrections. The repair call
        # rewrites the whole file from the model's own idea of the page, so
        # it reintroduces the very selectors that were corrected before
        # verification — the fix has to be applied AFTER the last rewrite,
        # not only before the first.
        if elements:
            fixed, reapplied = _autofix_selectors(fixed, elements)
            for f in reapplied:
                logger.info("Selector re-corrected after repair: %s", f)

        code = fixed

    return original_code, False, "verify_failed"