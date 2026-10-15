"""
agents/crawl_agent.py

CRAWL AGENT

Purpose:
    Navigate a web application comprehensively — handling login, following
    navigation links, and recording every unique screen visited. The result
    is a structured map of all pages and their interactive elements.

    When a BRD is provided, the LLM uses it to navigate the screens relevant
    to those requirements (no page limit — it runs until the BRD is covered).
    Without a BRD, the agent explores up to MAX_SCREENS_NO_BRD screens from
    the base URL.

    Login is handled automatically: if the landing page contains a password
    field, the LLM will fill the credentials and submit — no hardcoded
    selectors, no dedicated login method.

Inputs:
    driver       — live Selenium WebDriver, already on the base URL.
    provider     — LLMProvider instance.
    credentials  — optional dict {"username": ..., "password": ...}.
    brd_text     — optional extracted BRD text. When present, guides which
                   screens to navigate to.
    on_step      — optional progress callback(step_number, step_dict).
    cancel_check — optional zero-arg callable returning True once the caller
                   (e.g. the original HTTP request) has gone away. Checked at
                   the top of every loop iteration and inside the retry-heavy
                   inner sections; when it returns True the crawl stops as
                   cleanly as a normal `done()` call, just without a model
                   turn to produce one. Never required — omitting it (the
                   default) reproduces the exact previous behaviour: the
                   crawl only stops via done(), the page/step limits, or the
                   existing no-progress hard stop.

Output:
    dict with keys:
        pages: list[dict]  — each page has url, title, and snapshot elements.
        trace: list[dict]  — full step trace for audit.
"""
import logging
import os
import re
import time
from pathlib import Path
from typing import Callable, Optional
from urllib.parse import urlparse

from llm.base import LLMProvider, LLMProviderError
from core import tools
from core.tools_registry import TOOL_DEFINITIONS

# Domains that the crawl agent is allowed to visit in addition to the
# application's own domain. Used to let SSO provider redirects through
# the hard domain boundary without disabling the boundary entirely.
# Configurable via SSO_ALLOWED_DOMAINS (comma-separated).
_SSO_ALLOWED_DOMAINS: frozenset = frozenset(
    d.strip().lower()
    for d in os.environ.get("SSO_ALLOWED_DOMAINS", "login.microsoftonline.com").split(",")
    if d.strip()
)

# Regex that matches URL path segments which look like record IDs.
# Rule: any segment that contains at least one digit is treated as an ID —
# this covers pure numbers (/users/42), ticket-style IDs (HP-2021-031, APT-01,
# JIRA-123), UUIDs, and any other app-specific record reference.
# Pure alphabetic segments (e.g. "login", "createIssue") are left untouched
# because they are structural route names, not data IDs.
_ID_SEGMENT_RE = re.compile(
    r"(?<=/)"
    r"[^/]*\d[^/]*"   # any segment that contains at least one digit
    r"(?=/|$|#)"
)


def _url_template(url: str) -> str:
    """Return a normalised URL with record-ID segments replaced by {id}."""
    # Strip query string and fragment before normalising
    base = url.split("?")[0].split("#")[0]
    return _ID_SEGMENT_RE.sub("{id}", base)


def _step_tool(step: str) -> Optional[str]:
    """Classify literal actions, not whether their business outcome passed.

    "launch"/"open"/"navigate"/"go" (e.g. "Launch the url") deliberately
    have NO entry here — they describe the browser already being on
    base_url before this loop starts (see run_crawl's docstring), not an
    action the agent performs, so no tool call could ever match one. A
    step-list's leading line using one of these verbs is instead skipped
    up front by _skip_leading_launch_step() below, so the step cursor
    starts on the first line the agent genuinely has to act on. Without
    that skip, this line's _step_tool() call would return None forever,
    the cursor would freeze at index 0 for the entire crawl, and every
    later step's wait-duration match, click-repeat allowance, and the
    final target_reached computation would be silently wrong for the
    whole run — even when the real navigation fully succeeds. Confirmed
    live: a 30-step list starting with "Launch the url" completed all
    steps correctly but still reported target_reached=False.
    """
    verb = step.strip().split(maxsplit=1)[0].lower() if step.strip() else ""
    return {
        "click": "click", "enter": "type_text", "type": "type_text",
        "press": "press_key", "wait": "wait_for_dynamic_content",
        "verify": "get_snapshot", "check": "get_snapshot",
    }.get(verb)


def _skip_leading_launch_step(lines: list[str]) -> int:
    """
    Return the index of the first line the agent must actually act on.

    Skips leading lines whose verb is launch/open/navigate/go — these
    describe the browser already being on base_url (true before
    run_crawl's main loop ever starts), not something the agent does. See
    _step_tool's docstring for why leaving such a line at index 0 silently
    breaks step-list tracking for the entire crawl.

    Args:
        lines: The parsed target_flow_lines (one manual step per entry).
    Returns:
        The index to start _step_cursor at (0 if line 1 is already a real
        action — this only skips genuine no-op openers, never a real step).
    """
    _launch_verbs = {"launch", "open", "navigate", "go"}
    idx = 0
    while idx < len(lines):
        verb = lines[idx].strip().split(maxsplit=1)[0].lower() if lines[idx].strip() else ""
        if verb not in _launch_verbs:
            break
        idx += 1
    return idx


def _click_matches_step(step: str, identity: Optional[tuple]) -> bool:
    """Match an explicitly requested control; never special-case an app/button."""
    if not identity or _step_tool(step) != "click":
        return False
    target = re.sub(r"^click\s+(?:on\s+)?(?:the\s+)?", "", step.strip(), flags=re.I)
    target = re.sub(r"\s+(?:button|field|menu(?:\s+item)?|link|tab)\.?$", "", target, flags=re.I)
    label = re.sub(r"\s*\((?:ctrl|alt|shift|cmd).*\)\s*$", "", identity[1], flags=re.I)
    normalise = lambda value: re.sub(r"\W+", "", value.casefold())
    return bool(normalise(target)) and normalise(target) == normalise(label)


def _find_forward_step_match(
    lines: list[str], start: int, name: str, identity: Optional[tuple], args: dict,
) -> Optional[int]:
    """
    Search target_flow_lines from `start` onward for the first line THIS
    action actually satisfies, so the step cursor can catch up when one or
    more leading lines were already satisfied BEFORE run_crawl's loop
    started — e.g. an SSO login agents.crawl_agent's caller completed via
    service/auth_gateway.py during the precheck/cookie-import phase, before
    crawl_application ever invoked run_crawl (confirmed live: a step list
    starting "Launch the url" / "Enter SSO Username" / ... / "Click Yes" /
    "Wait 10 seconds" never advances the cursor past those lines at all,
    because the crawl loop's FIRST real action is already the click that
    satisfies line 8 ("Click navigator menu") — the cursor, still parked
    on line 2, can never reach that line through ordinary same-index
    matching, so target_reached stays False for the entire run even
    though every real step actually succeeded).

    Deliberately NOT specific to SSO, login, or this one scenario: it does
    not look for any named precondition at all. It only asks "does this
    concrete action's own resolved identity satisfy some LATER line's own
    literal instruction text" — using the exact same strict matching rules
    already used for the current-index check (_click_matches_step's
    normalised label comparison for click, or the literal key name for
    press_key). That strictness is what keeps this generic and safe for
    ANY scenario: an unrelated action's identity will not spuriously equal
    a future line's specific wording by coincidence, so this only ever
    fires when the agent has done something that legitimately matches a
    step further down the list — regardless of what that precondition
    was, SSO or otherwise.

    Only click and press_key are supported (as with the current-index
    check above) — get_snapshot/type_text/wait_for_dynamic_content have no
    per-step identity to match on beyond tool-type alone, and matching
    those on tool-type this far ahead would be far too loose (most step
    lists reuse the same tool many times).

    Args:
        lines: target_flow_lines.
        start: Index to begin searching from (the cursor's current
            position + 1 — the current index itself is already checked
            by the caller before this function is used).
        name: The tool name actually being dispatched this turn.
        identity: For a click, the (iframe_context, name, role) identity
            of the ref being clicked; unused for press_key.
        args: The raw tool-call arguments (used for press_key's "key").
    Returns:
        The matched line's index, or None if nothing matches ahead.
    """
    if name not in ("click", "press_key"):
        return None
    for idx in range(start, len(lines)):
        candidate = lines[idx]
        if _step_tool(candidate) != name:
            continue
        if name == "click":
            if _click_matches_step(candidate, identity):
                return idx
        else:  # press_key
            key_match = re.match(r"press\s+(\w+)", candidate, re.I)
            if key_match and args.get("key", "").casefold() == key_match[1].casefold():
                return idx
    return None


logger = logging.getLogger(__name__)

# When no BRD is provided, limit exploration to this many unique screens.
MAX_SCREENS_NO_BRD = int(__import__("os").environ.get("CRAWL_MAX_SCREENS", "5"))

_SYSTEM_PROMPT_TEMPLATE = (
    Path(__file__).parent.parent / "prompts" / "crawl_system_prompt.txt"
).read_text(encoding="utf-8")

CRAWL_TOOLS = [
    TOOL_DEFINITIONS["get_snapshot"],
    TOOL_DEFINITIONS["click"],
    TOOL_DEFINITIONS["type_text"],
    TOOL_DEFINITIONS["navigate_to"],
    # ── Phase 3.0-Extended: Advanced browser exploration ──────────────────────
    TOOL_DEFINITIONS["handle_alert"],
    TOOL_DEFINITIONS["switch_to_window"],
    TOOL_DEFINITIONS["get_all_windows"],
    TOOL_DEFINITIONS["scroll_page"],
    TOOL_DEFINITIONS["wait_for_dynamic_content"],
    TOOL_DEFINITIONS["detect_modals"],
    # ── Crawl-specific: hallucination-safe form filler ──────────────────────
    TOOL_DEFINITIONS["fill_form_for_exploration"],
    # ── Precise field-by-field entry: needed to follow an ordered manual
    # test case literally (real per-field values, Tab-triggered validation,
    # iframe-embedded fields) instead of only bulk-filling with synthetic data.
    TOOL_DEFINITIONS["press_key"],
    TOOL_DEFINITIONS["switch_to_frame"],
    TOOL_DEFINITIONS["done"],
]


def run_crawl(
    driver,
    provider: LLMProvider,
    credentials: Optional[dict] = None,
    brd_text: Optional[str] = None,
    on_step: Optional[Callable] = None,
    base_url: Optional[str] = None,
    target_flow: Optional[str] = None,
    test_data: Optional[dict] = None,
    cancel_check: Optional[Callable[[], bool]] = None,
) -> dict:
    """
    Runs the Crawl Agent loop.

    Args:
        driver:      Live Selenium WebDriver on the application base URL.
        provider:    LLM provider from provider_factory.get_provider().
        credentials: Optional {"username": ..., "password": ...} for login.
        brd_text:    Optional BRD/FRD text. Guides which screens to visit.
        on_step:     Optional callback(step_number, step_dict).
        base_url:    Optional base URL of the application. When provided, the
                     agent is restricted to URLs whose path starts with the
                     base URL's path prefix (e.g. '/fas/'). This prevents the
                     agent from navigating to the bare domain root or external
                     paths that redirect to unrelated login pages.
        target_flow: Optional plain-text description of a specific navigation
                     target (e.g. "Sales > Sales Order Entry > Standard Order
                     SO"), OR a multi-line ordered list of literal manual test
                     steps. Only used when brd_text is not provided. A single
                     line is treated as a destination to navigate toward; more
                     than one line is treated as an ordered procedure to follow
                     literally (see _is_step_list below). Ignored when brd_text
                     is set.
        test_data:   Optional dict of real field values for this application
                     (e.g. {"SoldTo": "48166329", "ShipTo": "48166371"}), as
                     returned by test_data.loader.load_test_data(). When a form
                     field's label matches a key here, the agent uses this real
                     value with type_text instead of a synthetic placeholder.
                     username/password are not repeated here — they already
                     appear in the CREDENTIALS section.
        cancel_check: Optional zero-arg callable returning True once this
                     crawl should stop because its caller has gone away
                     (e.g. the originating HTTP request disconnected).
                     Checked at the top of every loop iteration; when it
                     returns True the crawl stops immediately, the same way
                     it would if the model itself had called done(), and
                     returns whatever pages/trace were captured so far. Never
                     required: omitted (the default, None), this function's
                     behaviour is identical to before this parameter existed.

    Returns:
        {"pages": [...], "trace": [...], "target_reached": bool | None}
        target_reached is None unless target_flow was provided.
    """
    # Extract the application domain from base_url when provided (user-supplied),
    # falling back to driver.current_url. We always prefer base_url so the
    # boundary is anchored to exactly what the user entered, not to wherever
    # the browser happens to be at call time.
    try:
        _domain_source = base_url or driver.current_url
        _app_domain = urlparse(_domain_source).netloc  # e.g. test-fas.aws.baxter.com
    except Exception:
        _app_domain = None
    logger.info("crawl_agent: domain boundary set to %r (from %r)", _app_domain, _domain_source)

    # Derive a path prefix from base_url so the agent cannot wander to the
    # bare domain root or paths outside the application's own subtree.
    # Example: base_url='https://host/fas/login#!' → _base_path_prefix='/fas/'
    _base_path_prefix: Optional[str] = None
    _base_url_display = base_url or (driver.current_url if driver else None)
    if base_url:
        try:
            _parsed_base = urlparse(base_url)
            raw_path = _parsed_base.path.rstrip("/")
            parts = [p for p in raw_path.split("/") if p]
            if parts:
                _base_path_prefix = "/" + parts[0] + "/"  # e.g. '/fas/'
            else:
                _base_path_prefix = "/"
            logger.info(
                "crawl_agent: base path prefix restricted to %r (from base_url=%r)",
                _base_path_prefix, base_url,
            )
        except Exception as _bp_err:
            logger.warning("crawl_agent: could not derive base path prefix: %s", _bp_err)

    # ── Build system prompt ────────────────────────────────────────────
    cred_section = (
        f"Username: {credentials.get('username', '')}\n"
        f"Password: {credentials.get('password', '')}"
        if credentials
        else "No credentials provided — skip login if a form is present."
    )

    # Real per-field data (e.g. SoldTo, ShipTo, ItemNumber) for this
    # application, excluding credentials (already shown above) and any
    # empty values. Lets the agent use real values on the target form
    # instead of only synthetic fill_form_for_exploration placeholders.
    _TEST_DATA_EXCLUDED_KEYS = {"username", "password", "password_env"}
    _test_data_items = {
        k: v for k, v in (test_data or {}).items()
        if k not in _TEST_DATA_EXCLUDED_KEYS and v not in (None, "")
    }
    test_data_section = (
        "\n".join(f"  - {k}: {v}" for k, v in _test_data_items.items())
        if _test_data_items
        else "No additional test data provided."
    )

    # BRD mode: visit only screens needed for the stated requirements.
    # Even in BRD mode we cap at CRAWL_MAX_SCREENS_BRD (default 20) to avoid
    # runaway crawls on large apps.
    MAX_SCREENS_BRD = int(__import__("os").environ.get("CRAWL_MAX_SCREENS_BRD", "20"))
    # Default; set True below only when target_flow is a multi-line literal
    # step list (see _is_step_list). Referenced later in the main loop to
    # decide whether to inject the flow-adherence reminder.
    _is_step_list = False
    if brd_text:
        brd_section = (
            "A requirements document has been provided. Read it carefully before you do anything else.\n\n"
            "REQUIREMENTS DOCUMENT:\n"
            "----------------------\n"
            f"{brd_text[:4000]}\n"
            "----------------------\n\n"
            "YOUR TASK:\n"
            "1. From the document above, identify the ONE main user workflow it describes "
            "(e.g. 'create an escalation issue', 'submit a report', 'approve a request').\n"
            "2. Navigate ONLY the screens that a user must pass through to complete that "
            "workflow from start to finish. Nothing else.\n"
            "3. Before clicking any element or navigating to any screen, ask yourself: "
            "'Does the requirements document explicitly require this screen in order to "
            "test the stated workflow?' If the answer is no, do not navigate there.\n"
            "4. Do NOT open Admin pages, Settings, Reports, User Management, or any other "
            "section of the application unless the requirements document explicitly "
            "mentions them as part of this workflow.\n"
            "5. Do NOT click into existing data records (e.g. list rows, tickets, "
            "cases) unless the requirements document explicitly states the workflow "
            "involves viewing or editing an existing record. If the workflow is about "
            "creating something new, only navigate the creation path.\n"
            "6. Once you have walked through the complete workflow end-to-end, call done. "
            "Do not keep exploring after the workflow is complete.\n"
            f"7. Hard limit: stop and call done after {MAX_SCREENS_BRD} screens regardless.\n\n"
            "FORM SUBMISSION RULE (applies when the workflow ends with creating or submitting a record):\n"
            "a. After navigating to the form screen, call get_snapshot to read all fields.\n"
            "b. Call fill_form_for_exploration (no arguments, or pass a CSS form selector if "
            "multiple forms exist). This fills every visible field with the minimum synthetic "
            "value that satisfies HTML5 validation — no real, BRD-sourced, or domain-specific "
            "data. Do NOT use type_text to enter field values yourself.\n"
            "c. For any electronic signature, username, or password field the tool skips "
            "(it always skips password inputs), use the credentials from the CREDENTIALS "
            "section above with type_text — credentials are the only exception.\n"
            "d. Click the submit button to complete the submission.\n"
            "e. After submission, immediately call get_snapshot on every screen that appears: "
            "authentication dialogs, confirmation pages, success banners, updated list views. "
            "Record all of them.\n"
            "f. Only call done AFTER you have captured the post-submission screens. "
            "The post-submission elements are the most important part of end-to-end coverage."
        )
    elif target_flow:
        # A single line ('Sales > Sales Order Entry') is a destination to
        # navigate toward. More than one line is an ordered manual test case
        # to follow literally — avoids the ambiguity of guessing whether a
        # lookup/search screen with a matching name counts as "arrived".
        _target_flow_lines = [
            ln.strip() for ln in target_flow.strip().splitlines() if ln.strip()
        ]
        _is_step_list = len(_target_flow_lines) > 1
        if _is_step_list:
            _numbered_steps = "\n".join(
                f"  {i}. {ln}" for i, ln in enumerate(_target_flow_lines, start=1)
            )
            brd_section = (
                "You have been given the EXACT ordered steps of a manual test case "
                "to execute, instead of a requirements document or a short navigation "
                "target. Follow them literally, in order — do not judge whether you "
                "have 'arrived' by screen name alone; the step list itself defines "
                "when the workflow is actually complete.\n\n"
                f"STEPS TO FOLLOW, IN ORDER:\n{_numbered_steps}\n\n"
                "YOUR TASK:\n"
                "1. Execute each step above in order. Call get_snapshot before acting "
                "on a step if you have not already seen the current screen.\n"
                "2. A step naming a menu item, link, tab, or button is a click step — "
                "find the matching element in the snapshot and click it.\n"
                "3. A step naming a field and a value is a data-entry step — find that "
                "field in the snapshot and use type_text. Check the TEST DATA section "
                "above for a value matching the field's label/name before typing; use "
                "that real value if one is found. Only use fill_form_for_exploration's "
                "synthetic values for a field the step list and TEST DATA both say "
                "nothing about.\n"
                "4. A step that says to press Tab, or that describes a validation "
                "message appearing after leaving a field, is a press_key step — call "
                "press_key with key='tab'.\n"
                "5. If the field for a step is inside an iframe (shown in the snapshot "
                "with a '[in iframe: ...]' label and an 'ifr-' ref), call switch_to_frame "
                "with that iframe's container ref first (e.g. 'frame-frame_1'), call "
                "get_snapshot again to get a directly-usable ref, then interact with the "
                "field. Call switch_to_frame with frame_ref='main' before moving to a "
                "step that is outside that iframe.\n"
                "6. A step that says to wait, or describes a screen appearing after an "
                "action, is a wait_for_dynamic_content step.\n"
                "7. A step describing an expected result with no action of its own (e.g. "
                "a confirmation message or a new record reference) is verified by calling "
                "get_snapshot and recording what is shown — no click or type is needed.\n"
                f"8. Only call done after the LAST step above has been completed and its "
                f"result captured with get_snapshot, or after {MAX_SCREENS_NO_BRD} screens, "
                f"whichever comes first."
            )
        else:
            brd_section = (
                "A specific navigation target has been provided instead of a requirements "
                "document.\n\n"
                f"TARGET FLOW: {target_flow}\n\n"
                "YOUR TASK:\n"
                "1. Navigate the menus/links most likely to lead toward this target, one "
                "step at a time, using click and get_snapshot as normal.\n"
                "2. Call get_snapshot after every navigation so each screen along the way "
                "is recorded, not just the final one.\n"
                "3. If a menu path does not seem to lead toward the target, use the menu "
                "or breadcrumb (not the browser back button) to try a different path.\n"
                "4. Reaching a screen whose name matches the target is not the same as "
                "completing it — if the target implies creating, submitting, or updating "
                "something (rather than just viewing a list or search screen), look for "
                "an Add/New/Create action on that screen and use it before treating the "
                "target as reached.\n"
                f"5. Stop and call done once you have completed the target action "
                f"described above, or after {MAX_SCREENS_NO_BRD} screens, whichever comes "
                f"first.\n\n"
                "FORM SUBMISSION RULE (applies once you reach the target screen, if it is "
                "a form):\n"
                "a. Call get_snapshot to read all fields.\n"
                "b. Check the TEST DATA section above for a value matching each field's "
                "label/name; use type_text with the real value where one is found.\n"
                "c. Call fill_form_for_exploration (no arguments, or a CSS form selector "
                "if multiple forms exist) to fill any remaining visible fields with the "
                "minimum synthetic value that satisfies HTML5 validation. Do NOT use "
                "type_text for a field TEST DATA already covered.\n"
                "d. For any electronic signature, username, or password field the tool "
                "skips, use the credentials from the CREDENTIALS section above with "
                "type_text — credentials are the only exception.\n"
                "e. If a field is inside an iframe, call switch_to_frame with its "
                "container ref, call get_snapshot again, then interact with it; call "
                "switch_to_frame with frame_ref='main' afterward.\n"
                "f. Click the submit button to complete the submission.\n"
                "g. After submission, immediately call get_snapshot on every screen that "
                "appears (confirmation pages, success banners, updated list views). "
                "Record all of them.\n"
                "h. Only call done AFTER you have captured the post-submission screens."
            )
    else:
        brd_section = (
            f"No requirements document provided. Explore the application broadly, "
            f"visiting up to {MAX_SCREENS_NO_BRD} screens. Follow the main navigation "
            f"links and menu items to understand the application's structure.\n"
            "When a URL contains a record identifier (e.g. HP-2021-031, APT-01, "
            "a UUID, or a long number), visit only ONE example of that page template "
            "and then move on."
        )

    system_prompt = (
        _SYSTEM_PROMPT_TEMPLATE
        .replace("__CREDENTIALS__", cred_section)
        .replace("__TEST_DATA__", test_data_section)
        .replace("__BRD_SECTION__", brd_section)
        .replace("__APP_DOMAIN__", _app_domain or "the application domain")
        .replace("__BASE_URL__", _base_url_display or "the application base URL")
        .replace("__BASE_PATH_PREFIX__", _base_path_prefix or "/")
    )

    tool = provider.build_tool(CRAWL_TOOLS)
    trace: list[dict] = []
    pages: list[dict] = []
    visited_urls: set[str] = set()       # exact URLs already recorded
    visited_templates: set[str] = set()  # normalised URL templates already recorded

    # Per-page tool-call budget.  Resets each time a new page is recorded.
    # Prevents the agent from spending unlimited tool calls on a single form.
    # Configurable via CRAWL_MAX_TOOLS_PER_PAGE env var (default 15).
    _CRAWL_MAX_TOOLS_PER_PAGE = int(
        os.environ.get("CRAWL_MAX_TOOLS_PER_PAGE", "15")
    )
    # Only direct interaction tools count toward the budget.
    # Navigation support tools (wait_for_dynamic_content, switch_to_window,
    # get_all_windows) are exempt so the agent is never blocked from moving
    # to the next page after budget exhaustion.
    _BUDGETED_TOOLS: frozenset = frozenset({
        "click", "type_text", "fill_form_for_exploration",
        "scroll_page", "detect_modals",
    })
    # Mutable dict so nested _record_current_page can reset without nonlocal.
    # budget starts at the configured base; _record_current_page raises it
    # to max(base, element_count) so complex pages get proportionally more
    # tool calls before the hard-stop fires.
    _page_state: dict = {"tool_calls": 0, "fill_used": False, "budget": _CRAWL_MAX_TOOLS_PER_PAGE}

    # URLs the agent has actually observed from page elements (anchor hrefs).
    # Populated after every get_snapshot call. Used to warn when the LLM
    # constructs a URL from the BRD rather than following a real link.
    _discovered_urls: set = {base_url} if base_url else set()

    contents = [provider.user_text_content(
        "Start exploring the application. Call get_snapshot first to see the current screen."
    )]

    # Content fingerprint per recorded page (indexed to match `pages`), used
    # to tell a genuinely new screen apart from a duplicate view when the
    # URL alone can't (see _record_current_page below).
    _page_fingerprints: dict[int, frozenset] = {}

    # Target-flow tracking: when target_flow is given (no BRD), record
    # whether any captured page actually matched the destination, so the
    # caller can decide whether this crawl is safe to reuse later. Mutable
    # dict so nested _append_page can update it without nonlocal.
    _target_keyword = (
        re.split(r"->|\u2192", target_flow)[-1].strip().lower() if target_flow else ""
    )
    _target_state: dict = {"reached": False}

    # Flow-adherence tracking: counts consecutive get_snapshot calls whose
    # content fingerprint is IDENTICAL to the previous one — i.e. the screen
    # genuinely did not change. This is deliberately based on real content,
    # not on whether the click/switch_to_frame call in between merely
    # executed without throwing: a click that lands on the same tree node
    # (or a re-render that swaps ref numbers without changing the screen)
    # still "succeeds" with no error, so a success-based counter never grows
    # and never catches a thrashing loop. A high streak means the agent is
    # re-checking the same screen instead of progressing.
    #
    # Originally this tracking only fed a hard-stop for step-list mode
    # (_is_step_list). A single-line destination target (e.g.
    # "Navigator -> Baxter Main TaskView -> AP -> Baxter -> Inquiries ->
    # Supplier Master", written as ONE line with "->" separators rather than
    # one line per step) produces _is_step_list=False -- and had NO hard
    # stop at all beyond the page/step ceilings, since every reminder and
    # every hard-stop check below was gated on `if _is_step_list:`. A crawl
    # against a menu-heavy shell app (JDE) that gets stuck oscillating
    # between a few top-level menu entries in this mode could burn through
    # many minutes and many LLM calls with no circuit breaker at all, other
    # than eventually hitting max_steps=200 or a provider-level failure
    # (e.g. a Bedrock gateway timeout). Fixed below: the same no-progress
    # detection now also triggers a (differently worded, since there is no
    # per-step cursor to name) stop for single-line destination targets.
    # Broad exploration (no target_flow, no BRD) is intentionally left
    # untouched -- there is no defined path to be "stuck" on, and the
    # existing MAX_SCREENS_NO_BRD limit already bounds that case.
    _NO_PROGRESS_WARN_THRESHOLD = 4
    _HARD_STOP_STREAK = _NO_PROGRESS_WARN_THRESHOLD * 2
    _flow_state: dict = {
        "no_progress_streak": 0,
        "last_snapshot_fp": None,
    }

    # Explicit step cursor (step-list mode only): the model is handed the
    # FULL numbered list only once, at the very start, in the system prompt.
    # Across dozens of tool calls it has no reliable way to remember which
    # line it's currently on. Advance on successful matching actions, not on
    # incidental screen changes. This tracks dispatched actions; it is NOT
    # an assertion that a business transaction or verification has passed.
    #
    # Starts past any leading launch/open/navigate/go line (see
    # _skip_leading_launch_step) rather than always at 0 — otherwise a step
    # list beginning "Launch the url" / "Open the application" freezes this
    # cursor at index 0 for the whole crawl (see _step_tool's docstring),
    # silently breaking every later step's wait-duration match, click-repeat
    # allowance, and the final target_reached computation even when the
    # real navigation fully succeeds. Safe to reference _target_flow_lines
    # here: it is only ever unset when _is_step_list is False, in which case
    # the conditional short-circuits to 0 without evaluating it.
    _step_cursor: dict = {
        "index": _skip_leading_launch_step(_target_flow_lines) if _is_step_list else 0
    }
    _snapshot_serial = 0
    _last_click_snapshot: dict = {}
    _completed_step_clicks: set = set()

    # Guardrails for two failure modes seen in production crawls:
    #  - the model re-clicking something it already successfully clicked on
    #    this same screen instead of picking the newly-revealed item it
    #    actually wants (_clicked_identities_this_page). Tracks every element
    #    IDENTITY (iframe_context, name, role) clicked on the current screen —
    #    not the raw ref string, since refs are assigned by position in each
    #    fresh snapshot ("el-23" means "whatever is 23rd right now") and a
    #    tree/menu expanding can reshuffle that position onto a completely
    #    different, never-clicked element. Tracking by identity instead of
    #    position number avoids blocking that different element by mistake.
    #    Not just the immediately previous click either, since an intervening
    #    get_snapshot/switch_to_frame would otherwise reset a single-slot
    #    tracker and let the same wrong click through again.
    #  - a get_snapshot() coming back almost empty while still switched into
    #    an iframe from an earlier switch_to_frame call, which used to get
    #    wrongly recorded as a brand-new "page" (_frame_state)
    _clicked_identities_this_page: set = set()

    # ref -> (iframe_context, name, role) identity, refreshed on every
    # get_snapshot so click() can resolve what a ref actually refers to
    # RIGHT NOW before deciding whether it was already clicked.
    _ref_identity: dict = {}

    # empty_switch_count: how many consecutive switch_to_frame + get_snapshot
    # cycles on THIS page have come back almost empty (<=2 elements). Without
    # a cap, a page that is simply still loading looks like "try a different
    # iframe" to the model, and it cycles through every iframe container one
    # by one (observed live: e1MTreeIframe -> e1menuAppIframe -> e1MTreeIframe1
    # -> defaultFrame -> e1menuAppIframe again), wasting tool calls/tokens
    # without ever finding content because the real fix is to wait, not to
    # keep switching. Reset to 0 the moment real content is seen again.
    _frame_state: dict = {"in_frame": False, "ref": None, "empty_switch_count": 0}

    # Main-document snapshots are not ranked by their historical size.
    # A smaller form is valid content, not evidence that it is still loading.
    # Set the moment a main-page click succeeds; cleared the moment a fresh
    # main-document get_snapshot is actually taken. While set, switch_to_frame
    # to anything other than 'main' is blocked — the model has been observed
    # skipping the main-doc re-check entirely and guessing at iframes instead
    # (the hint on the click result alone was not reliably followed).
    _needs_main_recheck: dict = {"flag": False}

    def _content_fingerprint(snapshot: list[dict]) -> frozenset:
        """Lightweight content signature for a snapshot.

        Iframe-shell apps (e.g. JDE) can swap the entire visible screen via
        AJAX inside an iframe without ever changing driver.current_url, so
        URL alone cannot tell two different screens apart. This captures
        which elements are present (by iframe + name + role) so a
        meaningfully different screen is still recognised at the same URL.
        """
        return frozenset(
            (el.get("iframe_context") or "main", el.get("name") or "", el.get("role") or "")
            for el in snapshot
            if isinstance(el, dict)
        )

    def _append_page(url: str, title: str, snapshot: list[dict], fingerprint: frozenset) -> None:
        """Add a new entry to `pages`, reset the per-page budget, and log/notify."""
        pages.append({"url": url, "title": title, "elements": snapshot})
        _page_fingerprints[len(pages) - 1] = fingerprint
        # Reset per-page counters and recalculate budget from actual element count.
        # A page with 45 elements needs more exploration room than one with 5.
        element_count = len(snapshot) if isinstance(snapshot, list) else 0
        _page_state["tool_calls"] = 0
        _page_state["fill_used"] = False
        # A genuinely new screen — forget what was clicked on the previous one.
        _clicked_identities_this_page.clear()
        _needs_main_recheck["flag"] = False
        _page_state["budget"] = max(_CRAWL_MAX_TOOLS_PER_PAGE, element_count)
        logger.info(
            "crawl_agent: recorded page %d  url=%r  elements=%d  page_budget=%d",
            len(pages), url, element_count, _page_state["budget"],
        )
        if on_step:
            on_step(len(pages), {
                "action": "page_recorded",
                "input": {"url": url, "title": title, "element_count": element_count},
            })
        if _target_keyword and not _target_state["reached"]:
            _haystack = (title or "").lower()
            if _target_keyword in _haystack or any(
                _target_keyword in (el.get("name") or "").lower()
                for el in snapshot if isinstance(el, dict)
            ):
                _target_state["reached"] = True
                logger.info(
                    "crawl_agent: target flow reached  target=%r  page=%d",
                    target_flow, len(pages),
                )

    def _record_current_page(snapshot: list[dict]) -> None:
        """Store the current page and its snapshot into the pages list.

        Iframe-heavy shell apps (e.g. JDE) keep the same top-level URL while
        swapping content inside an iframe, so URL alone can't distinguish
        screens there. When the URL exactly matches a page we've already
        recorded, a content fingerprint decides what happened:
          - same content, more elements  -> upgrade the stored page in place
            (e.g. iframe content that finished loading after we first looked)
          - same content, no more elements -> true duplicate, ignore
          - different content            -> a distinct screen, record it too
        URLs that only match by *template* (e.g. a different record ID) are
        still skipped entirely, unchanged from prior behaviour.
        """
        try:
            url = driver.current_url
            title = driver.title or url
        except Exception:
            url = "unknown"
            title = "unknown"

        element_count = len(snapshot) if isinstance(snapshot, list) else 0
        fingerprint = _content_fingerprint(snapshot)
        template = _url_template(url)

        if url in visited_urls:
            for idx, existing in enumerate(pages):
                if existing["url"] != url:
                    continue
                existing_fp = _page_fingerprints.get(idx, frozenset())
                if fingerprint <= existing_fp or existing_fp <= fingerprint:
                    prev_count = len(existing.get("elements") or [])
                    if element_count > prev_count:
                        existing["elements"] = snapshot
                        _page_fingerprints[idx] = fingerprint
                        logger.info(
                            "crawl_agent: upgraded page url=%r  elements %d -> %d",
                            url, prev_count, element_count,
                        )
                    else:
                        logger.debug(
                            "crawl_agent: already visited %r — new snapshot not richer "
                            "(%d <= %d elements)", url, element_count, prev_count,
                        )
                    return
            logger.info(
                "crawl_agent: same URL %r but new content detected — recording as "
                "an additional screen (%d elements)", url, element_count,
            )
            _append_page(url, title, snapshot, fingerprint)
            return

        if template in visited_templates:
            logger.debug(
                "crawl_agent: URL template already seen %r (%r) — skipping", template, url
            )
            return

        visited_urls.add(url)
        visited_templates.add(template)
        _append_page(url, title, snapshot, fingerprint)

    max_steps = 200  # safety ceiling — avoids infinite loops on pathological apps
    _cancelled = False
    try:
        for step_num in range(1, max_steps + 1):
            # Stop as soon as the caller has gone away (e.g. the original
            # HTTP request that started this crawl disconnected). Checked
            # first, before spending another LLM call, so a dropped client
            # does not leave the crawl running unattended to its own natural
            # end condition. cancel_check is optional and side-effect free
            # by contract (see run_crawl's docstring); a caller that never
            # passes one gets byte-for-byte the same behaviour as before
            # this parameter existed.
            if cancel_check is not None:
                try:
                    if cancel_check():
                        _cancelled = True
                        logger.info(
                            "crawl_agent: cancel_check signalled stop — ending crawl "
                            "at step %d (pages=%d)", step_num, len(pages),
                        )
                        break
                except Exception as _cc_err:
                    # A broken cancel_check must never take down the crawl —
                    # log once and keep going as if it had returned False.
                    logger.warning("crawl_agent: cancel_check raised (ignored): %s", _cc_err)

            response = provider.call_model(system_prompt, tool, contents)
            name, args = provider.extract_function_call(response)
            if name is None:
                logger.warning("crawl_agent: model returned no tool call at step %d — stopping", step_num)
                break
            contents.append(response.candidates[0].content)

            # ── Per-page budget check ──────────────────────────────────────────
            # Count exploration tool calls and hard-stop when the budget is
            # exceeded, returning a message that tells the LLM to move on.
            # get_snapshot, navigate_to, handle_alert, and done are exempt.
            if name in _BUDGETED_TOOLS:
                _page_state["tool_calls"] += 1
                page_budget = _page_state["budget"]
                if _page_state["tool_calls"] > page_budget:
                    logger.warning(
                        "crawl_agent: page budget exhausted "
                        "(limit=%d, step=%d, tool=%s, page=%d)",
                        page_budget, step_num, name, len(pages),
                    )
                    result = {
                        "error": (
                            "Page exploration budget exhausted (%d tool calls on this page). "
                            "Stop exploring this page now. Call navigate_to to move to the "
                            "next unvisited screen."
                        ) % page_budget
                    }
                    step = {"action": name, "input": args, "page_index": len(pages)}
                    trace.append(step)
                    if on_step:
                        on_step(step_num, step)
                    contents.append(provider.function_response_content(name, result))
                    continue

            # ── Execute tool ───────────────────────────────────────────
            _current_step = (
                _target_flow_lines[_step_cursor["index"]]
                if _is_step_list and _step_cursor["index"] < len(_target_flow_lines)
                else ""
            )
            _step_identity = _ref_identity.get(args.get("ref"))
            _expected_tool = _step_tool(_current_step)
            _matches_step = bool(_current_step) and name == _expected_tool
            if _matches_step and name == "click":
                _matches_step = _click_matches_step(_current_step, _step_identity)
                # Field-focus labels may differ from the user's business label.
                # Permit the model's resolved editable field, but do not use this
                # fallback to grant repeated clicks on an already-clicked control.
                if not _matches_step and re.search(r"\bfield\.?$", _current_step, re.I):
                    _matches_step = bool(_step_identity and _step_identity[2] in {
                        "input", "textarea", "select", "textbox", "combobox",
                    })
            elif _matches_step and name == "press_key":
                _key_match = re.match(r"press\s+(\w+)", _current_step, re.I)
                _matches_step = bool(_key_match and args.get("key", "").casefold() == _key_match[1].casefold())

            # Catch the cursor up when this action doesn't match the CURRENT
            # line but does match a LATER one — see _find_forward_step_match's
            # docstring for why this is a generic fix (not SSO-specific) for
            # one or more leading lines having already been satisfied before
            # this loop started. Only attempted once same-index matching has
            # already failed, and only while there is still a current line to
            # be behind (an empty _current_step means the cursor is already
            # at/past the end — nothing to catch up to).
            if _is_step_list and _current_step and not _matches_step:
                _forward_idx = _find_forward_step_match(
                    _target_flow_lines, _step_cursor["index"] + 1, name, _step_identity, args,
                )
                if _forward_idx is not None:
                    logger.info(
                        "crawl_agent: step cursor caught up %d -> %d — this action matches "
                        "a later line, so earlier line(s) were evidently already satisfied "
                        "before this crawl started",
                        _step_cursor["index"], _forward_idx,
                    )
                    _step_cursor["index"] = _forward_idx
                    _current_step = _target_flow_lines[_forward_idx]
                    _matches_step = True

            if name == "get_snapshot":
                result = tools.get_snapshot(driver)
                _snapshot_valid = isinstance(result, list) and not any(
                    not isinstance(el, dict) or el.get("error") for el in result
                )
                if _snapshot_valid:
                    _snapshot_serial += 1

                # Refresh ref -> identity map from this snapshot BEFORE any
                # error-message substitution below can happen, so click()'s
                # already-clicked check always resolves against current refs.
                if isinstance(result, list):
                    _ref_identity.clear()
                    for _el in result:
                        if isinstance(_el, dict) and _el.get("ref"):
                            _ref_identity[_el["ref"]] = (
                                _el.get("iframe_context") or "main",
                                _el.get("name") or "",
                                _el.get("role") or "",
                            )

                _snap_count = len(result) if isinstance(result, list) else 0
                if not _frame_state["in_frame"] and _snapshot_valid:
                    _needs_main_recheck["flag"] = False

                if not _snapshot_valid:
                    # Preserve the actual browser error instead of disguising it
                    # as a loading delay, and never record it as a screen.
                    pass
                elif _frame_state["in_frame"] and _snap_count <= 2:
                    _frame_state["empty_switch_count"] += 1
                    logger.warning(
                        "crawl_agent: snapshot while switched into frame ref=%s "
                        "returned only %d element(s) — not recording as a new page "
                        "(step=%d, empty_switch_count=%d)",
                        _frame_state["ref"], _snap_count, step_num, _frame_state["empty_switch_count"],
                    )
                    if _frame_state["empty_switch_count"] >= 2:
                        # Already tried at least two frames (or the same frame
                        # twice) on this page with nothing to show — stop
                        # iframe-hunting and point at the actual fix: the page
                        # itself likely hasn't finished loading yet.
                        result = {
                            "error": (
                                f"This snapshot only found {_snap_count} element(s) while "
                                f"switched into frame '{_frame_state['ref']}'. This is the "
                                f"{_frame_state['empty_switch_count']}th near-empty frame "
                                "read in a row on this page — do NOT try switching into "
                                "another iframe. Call switch_to_frame with frame_ref='main', "
                                "then call wait_for_dynamic_content, then call get_snapshot "
                                "again before deciding what to do next."
                            )
                        }
                    else:
                        result = {
                            "error": (
                                f"This snapshot only found {_snap_count} element(s) while "
                                f"you are still switched into frame '{_frame_state['ref']}'. "
                                "If you meant to return to the main page, call "
                                "switch_to_frame with frame_ref='main' first, then call "
                                "get_snapshot again."
                            )
                        }
                else:
                    _frame_state["empty_switch_count"] = 0
                    if result:
                        _record_current_page(result)

                # Discover all anchor hrefs on the page so the navigate_to
                # check can warn when the LLM constructs a URL from the BRD
                # rather than following a link that actually exists in the UI.
                try:
                    hrefs = driver.execute_script(
                        "return Array.from(document.querySelectorAll('a[href]'))"
                        ".map(function(a){return a.href;})"
                        ".filter(function(h){return h && h.indexOf('javascript:')<0;});"
                    ) or []
                    _discovered_urls.update(hrefs)
                    _discovered_urls.add(driver.current_url)
                    logger.debug(
                        "crawl_agent: discovered %d href(s) on page (total known: %d)",
                        len(hrefs), len(_discovered_urls),
                    )
                except Exception as _de:
                    logger.debug("crawl_agent: link discovery failed (non-fatal): %s", _de)

                # Enforce screen limit
                limit = MAX_SCREENS_BRD if brd_text else MAX_SCREENS_NO_BRD
                if len(pages) >= limit:
                    logger.info(
                        "crawl_agent: reached %d-screen limit (%s) — stopping",
                        limit, "BRD mode" if brd_text else "no BRD",
                    )
                    break

            elif name == "click":
                _click_ref = args["ref"]
                _click_identity = _ref_identity.get(_click_ref)
                _step_click_key = (_step_cursor["index"], _click_identity)
                _requested_repeat = (
                    _is_step_list and _click_matches_step(_current_step, _click_identity)
                    and _step_click_key not in _completed_step_clicks
                    and _snapshot_serial > _last_click_snapshot.get(_click_identity, -1)
                )
                if (_click_identity is not None
                    and _click_identity in _clicked_identities_this_page
                    and not _requested_repeat):
                    logger.warning(
                        "crawl_agent: blocked repeated click on already-clicked "
                        "ref=%s identity=%r on this screen (step=%d)",
                        _click_ref, _click_identity, step_num,
                    )
                    result = {
                        "error": (
                            "You already clicked this element on this screen. A repeat "
                            "requires a fresh snapshot and a separate current step "
                            "explicitly naming this control. Do not repeat it to guess "
                            "at progress. Look at the elements already visible "
                            "in the last snapshot for a DIFFERENT item you have not yet "
                            "clicked, and click that instead. If you are unsure what is "
                            "currently visible, call get_snapshot — do not switch frames "
                            "or scroll to search for it."
                        )
                    }
                else:
                    result = tools.click(_click_ref)
                    logger.warning(
                        "crawl_agent: click ref=%s identity=%r -> %s (step=%d)",
                        _click_ref, _click_identity,
                        "ok" if result.get("ok") else f"error: {result.get('error')}",
                        step_num,
                    )
                    if result.get("ok") and _click_identity is not None:
                        _clicked_identities_this_page.add(_click_identity)
                        _last_click_snapshot[_click_identity] = _snapshot_serial
                        if _matches_step:
                            _completed_step_clicks.add(_step_click_key)
                        # The clicked element lived in the main page, not an
                        # iframe — whatever it revealed (e.g. a tree/menu
                        # item expanding into child rows) will appear on the
                        # main page too. Say so explicitly instead of letting
                        # the model guess and go hunting through iframes that
                        # have nothing to do with this click.
                        #
                        # "not _frame_state['in_frame']" is the fix here.
                        # _click_identity[0] is reported by tools.get_snapshot
                        # relative to Selenium's CURRENT active document — so
                        # if the agent is already switched into an iframe
                        # (_frame_state["in_frame"] is True) and clicks a
                        # field there (e.g. Sold To inside e1menuAppIframe),
                        # that click is STILL labeled "main", because from
                        # Selenium's perspective the iframe IS the active
                        # document right now. Without this guard, that
                        # mislabeled click wrongly armed
                        # _needs_main_recheck["flag"], which can only be
                        # cleared by "not _frame_state['in_frame']" being
                        # True — but the agent genuinely IS still in the
                        # iframe, so that condition never fires, and the
                        # flag stays stuck True for the rest of the crawl.
                        # Every later switch_to_frame call then gets blocked
                        # ("Your last click was on the main page...") no
                        # matter how many valid get_snapshot calls happen in
                        # between, and the crawl silently dies mid-form.
                        # Confirmed live: a stale-ref click failure on Ship
                        # To, right after a real (but mislabeled) main-page
                        # hint from the Sold To click, triggered exactly
                        # this permanent lock — 5 consecutive blocked
                        # switch_to_frame attempts with no recovery.
                        if _click_identity[0] == "main" and not _frame_state["in_frame"]:
                            result["hint"] = (
                                "This element was on the main page (not inside an "
                                "iframe). Call get_snapshot now and look at the main "
                                "page again for newly revealed items — do NOT call "
                                "switch_to_frame unless a get_snapshot result itself "
                                "tells you the new content is inside a frame."
                            )
                            _needs_main_recheck["flag"] = True

            elif name == "type_text":
                result = tools.type_text(args["ref"], args["text"])

            elif name == "press_key":
                result = tools.press_key(args["key"])

            elif name == "switch_to_frame":
                _frame_ref = args["frame_ref"]
                if _frame_ref != "main" and _needs_main_recheck["flag"]:
                    # The last click was on the main page and no main-doc
                    # get_snapshot has happened since — refuse to let the
                    # model guess at iframes before it has actually looked.
                    logger.warning(
                        "crawl_agent: blocked switch_to_frame(%r) before a fresh "
                        "main-doc get_snapshot following a main-page click (step=%d)",
                        _frame_ref, step_num,
                    )
                    result = {
                        "error": (
                            "Your last click was on the main page and you have not "
                            "called get_snapshot since. Call get_snapshot now and look "
                            "at the main page for newly revealed items before switching "
                            "into any iframe."
                        )
                    }
                else:
                    result = tools.switch_to_frame(_frame_ref)
                    if result.get("ok"):
                        if _frame_ref == "main":
                            _frame_state["in_frame"] = False
                            _frame_state["ref"] = None
                        else:
                            _frame_state["in_frame"] = True
                            _frame_state["ref"] = _frame_ref

            elif name == "navigate_to":
                nav_url = args.get("url", "")
                nav_domain = urlparse(nav_url).netloc if nav_url else ""
                nav_path  = urlparse(nav_url).path  if nav_url else ""

                # Block navigation outside the application's domain
                if _app_domain and nav_domain and nav_domain != _app_domain \
                        and nav_domain not in _SSO_ALLOWED_DOMAINS:
                    logger.warning(
                        "crawl_agent: blocked navigate_to outside app domain "
                        "(target=%r, allowed=%r, sso_allowed=%r)", nav_domain, _app_domain, _SSO_ALLOWED_DOMAINS
                    )
                    result = {
                        "error": (
                            f"Navigation blocked: '{nav_url}' is outside the application "
                            f"domain '{_app_domain}'. Only navigate within this domain. "
                            "Use click or get_snapshot to proceed."
                        )
                    }
                # Block navigation to bare domain root or paths outside the base path prefix
                elif _base_path_prefix and nav_path and not nav_path.startswith(_base_path_prefix):
                    logger.warning(
                        "crawl_agent: blocked navigate_to outside base path prefix "
                        "(target=%r, required_prefix=%r)", nav_url, _base_path_prefix
                    )
                    result = {
                        "error": (
                            f"Navigation blocked: '{nav_url}' is outside the application "
                            f"base path '{_base_path_prefix}'. "
                            f"Only navigate to URLs that start with {_base_path_prefix}. "
                            "Do not navigate to the bare domain root."
                        )
                    }
                else:
                    # Block navigation to any URL the agent has not actually
                    # seen on a page.  This prevents the LLM from constructing
                    # paths from BRD content or training knowledge.
                    # Exception: _discovered_urls is empty before the first
                    # get_snapshot (login page), so we only enforce once it is
                    # populated.  base_url is seeded at startup so the initial
                    # navigation is always allowed.
                    _nav_norm = nav_url.rstrip("/").split("#")[0]
                    _url_known = not _discovered_urls or any(
                        u.rstrip("/").split("#")[0] == _nav_norm
                        for u in _discovered_urls
                    )
                    if not _url_known:
                        logger.warning(
                            "crawl_agent: blocked navigate_to — URL not discovered "
                            "from any page element (url=%r). Constructed from BRD or "
                            "training data.",
                            nav_url,
                        )
                        result = {
                            "error": (
                                f"Navigation blocked: '{nav_url}' was not found as a "
                                "link on any page you have visited. Do NOT construct "
                                "or guess URLs from the requirements document or your "
                                "own knowledge. Find a visible link or button on the "
                                "current page and use click instead."
                            )
                        }
                    else:
                        result = tools.navigate_to(nav_url)
                        # Reset page budget when navigation succeeds so tools on
                        # the new page are not blocked by the previous page's counter.
                        if result.get("ok"):
                            _page_state["tool_calls"] = 0
                            _page_state["fill_used"] = False
                            _frame_state["in_frame"] = False
                            _frame_state["ref"] = None
                            _frame_state["empty_switch_count"] = 0
                            _clicked_identities_this_page.clear()
                            logger.debug(
                                "crawl_agent: page state reset after navigate_to %r", nav_url
                            )

            # ── Phase 3.0-Extended: Advanced browser exploration ──────────────────
            elif name == "handle_alert":
                from service.browser_explorer import handle_alert
                result = handle_alert(driver, action=args.get("action", "accept"))

            elif name == "switch_to_window":
                from service.browser_explorer import switch_to_window, switch_to_main_window
                window_handle = args.get("handle", "main")
                if window_handle == "main":
                    result = switch_to_main_window(driver)
                else:
                    result = switch_to_window(driver, window_handle)

            elif name == "get_all_windows":
                from service.browser_explorer import get_all_windows
                windows = get_all_windows(driver)
                result = {"ok": True, "data": {"windows": windows}}

            elif name == "scroll_page":
                from service.browser_explorer import scroll_page
                direction = args.get("direction", "down")
                amount = args.get("amount", 3)
                result = scroll_page(driver, direction=direction, amount=amount)

            elif name == "wait_for_dynamic_content":
                from service.browser_explorer import wait_for_dynamic_content
                timeout = args.get("timeout", 10)
                _fixed_wait = re.fullmatch(
                    r"wait\s+(?:for\s+)?(\d+(?:\.\d+)?)\s+seconds?\.?",
                    _current_step, re.I,
                ) if _matches_step else None
                if _fixed_wait:
                    # A literal pause is not a spinner-readiness timeout. Honor
                    # the requested duration once, without repeated model calls.
                    _wait_started = time.monotonic()
                    time.sleep(float(_fixed_wait[1]))
                    result = {"ok": True, "data": {
                        "waited_seconds": time.monotonic() - _wait_started,
                    }}
                else:
                    result = wait_for_dynamic_content(driver, timeout=timeout)

            elif name == "detect_modals":
                from service.browser_explorer import detect_modals
                modals = detect_modals(driver)
                result = {"ok": True, "data": {"modals": modals}}

            elif name == "fill_form_for_exploration":
                # Dedup: block a second call on the same page — the form fields
                # do not change between calls and a repeat wastes time.
                if _page_state["fill_used"]:
                    logger.warning(
                        "crawl_agent: fill_form_for_exploration already called on "
                        "this page (step=%d) — blocked to prevent duplicate filling",
                        step_num,
                    )
                    result = {
                        "error": (
                            "fill_form_for_exploration already called on this page. "
                            "The form has been filled with synthetic values. "
                            "Click the submit button to proceed, or navigate to the next screen."
                        )
                    }
                else:
                    _page_state["fill_used"] = True
                    result = tools.fill_form_for_exploration(
                        form_selector=args.get("form_selector"),
                    )
                    logger.info(
                        "crawl_agent: fill_form_for_exploration filled=%d skipped=%d",
                        len((result.get("filled") or [])),
                        len((result.get("skipped") or [])),
                    )

            elif name == "done":
                step = {"action": "done", "input": args, "page_index": len(pages)}
                trace.append(step)
                if on_step:
                    on_step(step_num, step)
                logger.info(
                    "crawl_agent: done called — pages=%d  passed=%s  notes=%r",
                    len(pages), args.get("passed"), args.get("notes"),
                )
                break

            else:
                result = {"error": f"Unknown tool: {name}"}

            # ── Literal action progress, separate from screen recording ──
            _action_ok = isinstance(result, dict) and result.get("ok") is True and not result.get("error")
            _observation_ok = (
                name == "get_snapshot" and isinstance(result, list) and bool(result)
                and all(isinstance(el, dict) and not el.get("error") for el in result)
            )
            if _matches_step and (_action_ok or _observation_ok):
                logger.warning(
                    "crawl_agent: flow step %d/%d %s via %s (tool_step=%d)",
                    _step_cursor["index"] + 1, len(_target_flow_lines),
                    "observed (not asserted)" if name == "get_snapshot" else "action executed",
                    name, step_num,
                )
                _step_cursor["index"] += 1
                _flow_state["no_progress_streak"] = 0

            if name == "get_snapshot":
                _fp = _content_fingerprint(result if isinstance(result, list) else [])
                if (_flow_state["last_snapshot_fp"] is not None
                    and _fp == _flow_state["last_snapshot_fp"]
                    and not (_matches_step and _observation_ok)):
                    _flow_state["no_progress_streak"] += 1
                else:
                    _flow_state["no_progress_streak"] = 0
                _flow_state["last_snapshot_fp"] = _fp

            if _is_step_list:
                _cur_i = _step_cursor["index"]
                _reminder = (
                    f"CURRENT STEP ({_cur_i + 1} of {len(_target_flow_lines)}): "
                    f"{_target_flow_lines[_cur_i]}\n"
                    "Do this step next. Do not skip ahead and do not deviate to a menu "
                    "path, tab, or link that is not named in this step (e.g. do not use "
                    "the breadcrumb as a substitute for the navigator menu unless this "
                    "step says to)."
                ) if _cur_i < len(_target_flow_lines) else (
                    "All requested actions have been dispatched. Capture the final "
                    "screen with get_snapshot, then call done. Action success and "
                    "captured snapshots alone do not prove business assertions passed; "
                    "report any unverified expected results explicitly."
                )
                if (_cur_i < len(_target_flow_lines)
                    and _flow_state["no_progress_streak"] >= _NO_PROGRESS_WARN_THRESHOLD):
                    _reminder += (
                        f" STOP: the screen has not changed for {_flow_state['no_progress_streak']} "
                        f"snapshot(s) in a row while you were on this step. Do not keep "
                        "re-clicking or try an alternate path. State that you are blocked "
                        f"on step {_cur_i + 1} ('{_target_flow_lines[_cur_i]}') and why, then stop."
                    )
                if isinstance(result, dict):
                    result = {**result, "_flow_reminder": _reminder}
                elif isinstance(result, list):
                    # get_snapshot returns a bare list[dict] of elements — wrap it
                    # so the reminder can still reach the model. Previously this
                    # branch was skipped entirely for get_snapshot results because
                    # isinstance(result, dict) was False, so the model never saw
                    # the reminder (or the STOP escalation) on most turns.
                    result = {"elements": result, "_flow_reminder": _reminder}
            elif target_flow:
                # Destination-style single-line target: no per-step cursor to
                # reference, so unlike step-list mode this does NOT annotate
                # every turn — only intervenes once the crawl is plausibly
                # stuck, so normal/working destination crawls see no change
                # in behaviour at all (nothing is added to their results).
                if _flow_state["no_progress_streak"] >= _NO_PROGRESS_WARN_THRESHOLD:
                    _reminder = (
                        f"STOP: the screen has not changed for "
                        f"{_flow_state['no_progress_streak']} snapshot(s) in a row while "
                        f"trying to reach the target flow '{target_flow}'. Do not keep "
                        "re-clicking the same control or wandering to unrelated menu "
                        "items (e.g. Home, Notifications, unrelated Open Applications "
                        "entries) hoping something changes. If you cannot make further "
                        "progress toward the target from here, call done now and "
                        "explain exactly what is blocking you."
                    )
                    if isinstance(result, dict):
                        result = {**result, "_flow_reminder": _reminder}
                    elif isinstance(result, list):
                        result = {"elements": result, "_flow_reminder": _reminder}

            step = {"action": name, "input": args, "page_index": len(pages)}
            trace.append(step)
            if on_step:
                on_step(step_num, step)
            contents.append(provider.function_response_content(name, result))

            # Hard backstop: don't trust the model to honor the STOP
            # instruction indefinitely. If the screen still hasn't changed
            # after repeated warnings, end the crawl now rather than
            # burning more minutes (and LLM calls) on a loop that isn't
            # going anywhere. Applies to BOTH step-list mode and single-line
            # destination-style target_flow mode -- previously only the
            # former was covered, which is why a target_flow written as one
            # "A -> B -> C" line (rather than one line per step) had no
            # circuit breaker at all beyond the page/step ceilings.
            _stuck_in_step_list = _is_step_list and _step_cursor["index"] < len(_target_flow_lines)
            _stuck_in_destination = bool(target_flow) and not _is_step_list
            if ((_stuck_in_step_list or _stuck_in_destination)
                and _flow_state["no_progress_streak"] >= _HARD_STOP_STREAK):
                _blocked_step = (
                    _target_flow_lines[_step_cursor["index"]]
                    if _stuck_in_step_list else target_flow
                )
                logger.warning(
                    "crawl_agent: hard-stopping — no screen change for %d consecutive "
                    "snapshots, blocked on %s (%r)",
                    _flow_state["no_progress_streak"],
                    f"step {_step_cursor['index'] + 1}/{len(_target_flow_lines)}" if _stuck_in_step_list
                    else "the target flow",
                    _blocked_step,
                )
                if on_step:
                    on_step(step_num, {
                        "action": "blocked",
                        "input": {
                            "reason": "no_progress",
                            "streak": _flow_state["no_progress_streak"],
                            "step_index": _step_cursor["index"] + 1 if _stuck_in_step_list else None,
                            "step_total": len(_target_flow_lines) if _stuck_in_step_list else None,
                            "step_text": _blocked_step,
                        },
                        "page_index": len(pages),
                    })
                break

    except LLMProviderError as e:
        logger.error("crawl_agent: LLM failure: %s", e)
    except Exception:
        logger.exception("crawl_agent: unexpected error")

    if _is_step_list:
        # Step-list flows have no single short "destination" phrase for
        # _target_keyword to search for — it degenerates to the whole flow
        # text, which can never match a page title or element name. Instead,
        # completion is measured the same way progress already is: the step
        # cursor only advances (see the get_snapshot handling above) on a
        # verified real screen change following an actual action, so reaching
        # the final line means every step was actually completed in order.
        _target_state["reached"] = _step_cursor["index"] >= len(_target_flow_lines) - 1

    logger.info(
        "crawl_agent: finished  pages=%d  steps=%d  target_reached=%s%s",
        len(pages), len(trace), _target_state["reached"] if target_flow else "n/a",
        "  (cancelled)" if _cancelled else "",
    )
    return {
        "pages": pages,
        "trace": trace,
        "target_reached": _target_state["reached"] if target_flow else None,
    }