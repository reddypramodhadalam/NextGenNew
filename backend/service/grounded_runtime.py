"""Standalone Selenium helpers embedded into map-grounded generated tests.
No application names, credentials, URLs, or business values belong here.
The generator replaces the empty catalog and adapter script with captured
recipes and the existing grid adapter. This module has no backend imports.

Includes _login_as() for multi-actor procedures that must run as more
than one identity within ONE continuous script (e.g. a requester creates
a record, an approver later approves it, an admin performs a final
override) — see its own docstring below.
"""
import os
import re
import time
from decimal import Decimal, InvalidOperation
from selenium.webdriver.common.by import By
from selenium.webdriver.common.keys import Keys
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.common.exceptions import (
    NoSuchElementException, StaleElementReferenceException, TimeoutException,
)
_TARGETS = {}
_GRID_INSPECT = ""
_CAPTURED_VALUES = {}

# ── Azure Entra ID (Microsoft Entra) — stable public selectors ─────────────
# Mirrors service/auth_gateway.py's constants of the same name. Duplicated
# here, not imported, because this module is intentionally standalone (see
# the module docstring: "has no backend imports") — it is embedded into
# every generated script verbatim via inspect.getsource() and must not
# depend on any backend module at runtime. If Microsoft ever changes these
# selectors, update them in both places. Used only by _login_as, below.
_ENTRA_REDIRECT_DOMAIN = "login.microsoftonline.com"
_ENTRA_USERNAME_SELECTOR = "input[name='loginfmt']"
_ENTRA_NEXT_BUTTON_ID = "idSIButton9"
_ENTRA_PASSWORD_SELECTOR = "input[name='passwd']"
_ENTRA_SUBMIT_BUTTON_ID = "idSIButton9"


def _record_event(driver, operation, **details):
    # Diagnostic metadata only: never include credentials or entered values.
    if not hasattr(driver, "_kavach_events"):
        driver._kavach_events = []
    driver._kavach_events.append({"operation": operation, **details})


def _normal_key(value):
    return "".join(c for c in str(value).casefold() if c.isalnum())


def get_data(key, *, required=True, default=""):
    """Accept naming variants only when they identify exactly one TEST_* key."""
    matches = [k for k in os.environ if k.startswith("TEST_")
               and _normal_key(k[5:]) == _normal_key(key)]
    if len(matches) > 1:
        raise ValueError("Ambiguous configured test-data key: " + str(key))
    value = os.environ[matches[0]] if matches else None
    if value in (None, ""):
        if required:
            raise RuntimeError("Missing required test-data key: " + str(key))
        return default
    return value


def _runtime_wait(driver, timeout=None):
    return WebDriverWait(
        driver, timeout if timeout is not None else float(os.environ.get("SELENIUM_TIMEOUT", "30")),
        ignored_exceptions=(NoSuchElementException, StaleElementReferenceException, TimeoutException),
    )


def _space(value):
    return " ".join(str(value or "").split())


def _xpath_literal(value):
    if "'" not in value:
        return "'" + value + "'"
    if '"' not in value:
        return '"' + value + '"'
    return "concat(" + ', "\'", '.join("'" + part + "'" for part in value.split("'")) + ")"


def _frame_context(driver, context, *, timeout=None):
    """Enter captured frames from the top, never treating a title as a name."""
    driver.switch_to.default_content()
    for descriptor in context:
        def find_frame(browser):
            found = browser.execute_script("""
                const d=arguments[0], exact=d.attributes||{};
                return Array.from(document.querySelectorAll('iframe,frame')).filter(e=>{
                    const match=Object.keys(exact).length
                        ? Object.entries(exact).every(([k,v])=>e.getAttribute(k)===v)
                        : ((d.identifier && [e.id,e.name].includes(d.identifier))
                            || (d.title && e.getAttribute('title')===d.title));
                    // getClientRects().length is truthy even for a 0x0 box (a
                    // collapsed-but-not-display:none element still reports one
                    // zero-area rect), so it alone does not prove the frame is
                    // actually rendered with real, user-visible area. Shell
                    // applications with a persistent iframe set (confirmed
                    // live: JDE keeps ~12 iframes attached simultaneously,
                    // most collapsed rather than removed once their screen is
                    // no longer active) leave stale iframes that still pass
                    // this check, still get switched into, and can still
                    // contain leftover matching content from an earlier
                    // screen -- colliding with the one genuinely visible
                    // instance and causing a runtime "Ambiguous" failure for
                    // ANY captured target resolved without a pinned frame
                    // context, in ANY multi-iframe application, regardless of
                    // scenario. offsetWidth/offsetHeight are 0 for a
                    // collapsed element (unlike getClientRects().length),
                    // which is what actually distinguishes "still occupies
                    // real screen space" from "technically not display:none
                    // but rendered at zero size."
                    return match && e.getClientRects().length && getComputedStyle(e).visibility!=='hidden'
                        && e.offsetWidth > 0 && e.offsetHeight > 0;
                });
            """, descriptor)
            found = [frame for frame in found if frame.is_displayed()]
            if len(found) > 1:
                raise AssertionError("Ambiguous captured frame; refusing to choose one")
            return found[0] if found else False
        driver.switch_to.frame(_runtime_wait(driver, timeout).until(find_frame))


def _recipe_matches(driver, recipe):
    strategy = recipe.get("strategy")
    if strategy in {"attribute", "ancestor_attribute"}:
        attr, value = recipe["attr"], str(recipe["value"])
        if attr == "text":
            # Selenium's visible text excludes hidden descendants; XPath's
            # string-value does not. Compare visible text, then keep leaves.
            # One browser-side read instead of two remote calls per DOM node.
            # innerText is rendered text, unlike textContent/XPath string(.).
            #
            # Same reasoning as _frame_context's offsetWidth/offsetHeight
            # check above: getClientRects().length alone does not prove an
            # element occupies real, user-visible area (a collapsed-but-not-
            # display:none element still reports a truthy, zero-area rect
            # list). A stale/leftover element retaining matching text from an
            # earlier screen state can otherwise still be counted as a live
            # match here, for the same generic, app-agnostic reason -- not
            # specific to any one captured target or scenario.
            return driver.execute_script(r"""
                const normal=s=>(s||'').replace(/\s+/g,' ').trim();
                const text=normal(arguments[0]);
                const elements=Array.from(document.querySelectorAll('body *'));
                const matches=elements.filter(e=>e.getClientRects().length &&
                    getComputedStyle(e).visibility!=='hidden' &&
                    e.offsetWidth > 0 && e.offsetHeight > 0 && normal(e.innerText)===text);
                return matches.filter(e=>!matches.some(other=>other!==e && e.contains(other)));
            """, value)
        if not re.fullmatch(r"[A-Za-z_][\w:.-]*", attr):
            raise ValueError("Unsupported captured attribute")
        return driver.find_elements(By.XPATH, "//*[@" + attr + "=" + _xpath_literal(value) + "]")
    if strategy == "jde_grid_header":
        if not _GRID_INSPECT:
            raise ValueError("Captured grid recipe has no adapter")
        inspect = "var inspect=function(){" + _GRID_INSPECT + "}; var r=arguments[0];"
        return driver.execute_script(inspect + r"""
            return Array.from(document.querySelectorAll('input,textarea')).filter(e=>{
                const cell=inspect(e);
                return cell && (cell.headerText||'').replace(/\s+/g,' ').trim()===r.column
                    && cell.rowIndex===r.row_index;
            });
        """, recipe)
    raise ValueError("Unsupported captured locator strategy: " + str(strategy))


def _target(driver, target_id, *, action="read", timeout=None):
    """Resolve a unique live target in its recorded frame; no heuristic clicks.

    Scope search order when a target has no pinned frame (frames=None,
    i.e. it was captured without a single definite context): the main
    document is checked FIRST, then each candidate frame in turn, and the
    FIRST scope that contains any match at all wins -- matches are no
    longer collected across every scope and merged into one combined list
    before deciding uniqueness.

    WHY: an iframe-shell application (e.g. JDE, with ~12 persistent
    iframes) very often has the SAME visible text/attribute value present
    in two structurally unrelated places at once -- e.g. a breadcrumb-
    style navigation link living in the main document, and an unrelated
    fragment of a compound heading (rendered as its own DOM node) living
    inside the active screen's content iframe. Both are fully real,
    fully visible, correctly-sized elements; neither is a stale leftover.
    Merging their matches together and raising "Ambiguous" the moment the
    combined count exceeds one was a structural bug for ANY unpinned
    target whose recipe happens to coincide between the shell/navigation
    layer and a screen's own content -- not specific to one captured
    target, application, or scenario. Confirmed live: a target capturing
    a "Sales Order Header" breadcrumb link (main document, role=link) and
    a same-text fragment of a "Standard Order SO - Sales Order Header"
    heading (inside the content iframe) were merged into one list of two,
    raising AssertionError("Ambiguous live target") on every attempt,
    with no way to ever resolve -- an unrecoverable, permanent failure
    for that step regardless of retries.

    A genuine collision -- two matches found WITHIN THE SAME scope -- is
    still treated as a real ambiguity and still raises; only matches from
    genuinely different, independent scopes are no longer forced to
    compete with each other.
    """
    if action not in {"read", "click", "type"}:
        raise ValueError("Unsupported target action")
    descriptor = _TARGETS[target_id]
    context = descriptor["frames"]
    scopes = [context] if context is not None else [[]] + [[frame] for frame in descriptor.get("candidate_frames", [])]
    def candidates_in_context(browser):
        candidates = []
        for element in _recipe_matches(browser, descriptor["locator"]):
            if not element.is_displayed():
                continue
            if action != "read" and not element.is_enabled():
                continue
            if action == "type":
                if element.tag_name not in ("input", "textarea"):
                    continue
                if element.get_property("readOnly"):
                    continue
                if element.get_dom_attribute("type") in {
                    "hidden", "button", "submit", "reset", "checkbox", "radio", "file",
                }:
                    continue
            # The capture's HTML input role disambiguates input vs label for
            # reading as well, without making business labels into rules.
            elif descriptor.get("role") in ("input", "textarea", "select"):
                if element.tag_name != descriptor["role"]:
                    continue
            candidates.append(element)
        return candidates
    def find(browser):
        for scope in scopes:
            try:
                _frame_context(browser, scope, timeout=0)
            except TimeoutException:
                continue
            candidates = candidates_in_context(browser)
            if not candidates:
                continue
            if len(candidates) > 1:
                # A genuine same-scope collision -- unlike a cross-scope
                # coincidence, this means the recipe itself does not
                # uniquely identify one element even within a single,
                # already-selected context. Still a real problem to
                # surface, not silently resolved.
                raise AssertionError("Ambiguous live target: " + target_id)
            _frame_context(browser, scope, timeout=0)
            return candidates[0]
        return False
    return _runtime_wait(driver, timeout).until(find)


def _click_target(driver, target_id):
    # Never retry a click: a first click may already have submitted data.
    _target(driver, target_id, action="click").click()
    _record_event(driver, "click", target=target_id)


def _same_value(actual, expected, numeric=False):
    if numeric:
        try:
            return Decimal(actual) == Decimal(expected)
        except (InvalidOperation, ValueError):
            return False
    return actual == expected


def _enter_text(driver, target_id, data_key, *, numeric=False):
    value = get_data(data_key)
    element = _target(driver, target_id, action="type")
    element.click()
    modifier = Keys.COMMAND if str(driver.capabilities.get("platformName", "")).lower() == "mac" else Keys.CONTROL
    element.send_keys(modifier, "a")
    element.send_keys(value)
    # Re-find after reactive rendering. Do not print actual or expected values.
    _runtime_wait(driver).until(lambda d: _same_value(
        _target(d, target_id, action="type", timeout=0).get_property("value"), value, numeric))
    _record_event(driver, "input_value_verified", target=target_id)


def _press_tab(driver, target_id):
    _target(driver, target_id, action="type").send_keys(Keys.TAB)
    _record_event(driver, "tab", target=target_id)


def _assert_value(driver, target_id, data_key, *, numeric=False):
    expected = get_data(data_key)
    _runtime_wait(driver).until(lambda d: _same_value(
        _target(d, target_id, timeout=0).get_property("value"), expected, numeric))
    _record_event(driver, "retained_value_verified", target=target_id)


def _assert_nonempty(driver, target_id):
    _runtime_wait(driver).until(lambda d: bool(_space(
        _target(d, target_id, timeout=0).get_property("value"))))
    _record_event(driver, "nonempty_value_verified", target=target_id)


def _read_current_value(element):
    """Read whatever a captured control currently shows: an editable
    field's value, or a non-input control's visible text (e.g. a
    system-generated reference number rendered as a link, a grid cell,
    or a label rather than an input)."""
    if element.tag_name in ("input", "textarea", "select"):
        return element.get_property("value")
    return _space(element.text)


def _capture_value(driver, target_id, variable_name):
    """Read a captured control's CURRENT live value and hold it in memory
    under variable_name, for later steps in this SAME test run to reuse
    or compare against (see _enter_captured, _assert_contains_captured,
    _assert_value_delta). Never persisted beyond this one test function
    call, never written to disk, and never printed via _record_event.
    Typical use: a system-generated identifier (an order number, a
    document number) that only exists after a save, or a numeric field
    (an on-hand quantity) that must be read BEFORE a change so a later
    step can verify the change actually took effect.
    """
    value = _read_current_value(_target(driver, target_id))
    if not _space(value):
        raise AssertionError(
            "Captured control has no value to record for variable: " + str(variable_name)
        )
    _CAPTURED_VALUES[variable_name] = value
    _record_event(driver, "value_captured", target=target_id, variable=variable_name)
    return value


def _get_captured(variable_name):
    if variable_name not in _CAPTURED_VALUES:
        raise RuntimeError("No value captured for variable: " + str(variable_name))
    return _CAPTURED_VALUES[variable_name]


def _enter_captured(driver, target_id, variable_name):
    """Type a previously captured value into a field -- e.g. re-enter an
    order number captured right after it was created into a later
    'search by order number' field. Same keyboard-replace-and-verify
    behaviour as _enter_text, but the source value comes from an earlier
    _capture_value call in this same run, not from stored test data."""
    value = _get_captured(variable_name)
    element = _target(driver, target_id, action="type")
    element.click()
    modifier = Keys.COMMAND if str(driver.capabilities.get("platformName", "")).lower() == "mac" else Keys.CONTROL
    element.send_keys(modifier, "a")
    element.send_keys(value)
    _runtime_wait(driver).until(lambda d: _same_value(
        _target(d, target_id, action="type", timeout=0).get_property("value"), value))
    _record_event(driver, "captured_value_entered", target=target_id, variable=variable_name)


def _assert_contains_captured(driver, target_id, variable_name):
    """Verify a control's current value/text CONTAINS a previously
    captured value -- e.g. confirm the order number captured right after
    creation now appears in a results grid row, without requiring an
    exact full-string match (the row may show it alongside other text)."""
    expected = _get_captured(variable_name)
    _runtime_wait(driver).until(lambda d: expected in _space(
        _read_current_value(_target(d, target_id, timeout=0))))
    _record_event(driver, "captured_value_verified_present", target=target_id, variable=variable_name)


def _assert_value_delta(driver, target_id, before_variable, delta):
    """Verify a control's CURRENT numeric value equals an earlier captured
    value plus/minus a known change -- e.g. confirm on-hand quantity now
    equals the quantity captured before an adjustment, plus the
    adjustment amount. delta is a plain number (5, "-3", "1.5"), or
    another captured variable name prefixed with '$' (e.g. "$Quantity")
    when the change amount was itself entered/captured earlier in the
    same run rather than known in advance."""
    before = Decimal(_get_captured(before_variable))
    if isinstance(delta, str) and delta.startswith("$"):
        change = Decimal(_get_captured(delta[1:]))
    else:
        change = Decimal(str(delta))
    expected = before + change
    def check(d):
        try:
            return Decimal(_read_current_value(_target(d, target_id, timeout=0))) == expected
        except (InvalidOperation, ValueError):
            return False
    _runtime_wait(driver).until(check)
    _record_event(driver, "value_delta_verified", target=target_id, before=before_variable)


def _assert_screen(driver, target_id, *, timeout=None):
    """Prove destination using a non-shared recipe AND its recorded heading.

    The control and the heading do not have to be in the same frame. In a
    frame-shell application the control used as proof (e.g. a breadcrumb
    link in the main document or a breadcrumb frame) and the screen heading
    (inside the content frame) often live in different documents. The
    heading is therefore searched in the main document, the target's pinned
    frame(s) and every captured candidate frame -- never only the frame
    where the control happened to be found. Stale/collapsed frames are still
    excluded by _frame_context, and both the control AND the exact captured
    heading must be visible for the check to pass.
    """
    descriptor = _TARGETS[target_id]
    title = descriptor.get("transition_title")
    if not title:
        raise ValueError("Destination control is not unique to a captured screen: " + target_id)
    recipe = {"strategy": "attribute", "attr": "text", "value": title}
    heading_scopes = [[]]
    for frame in list(descriptor.get("frames") or []) + list(descriptor.get("candidate_frames") or []):
        if [frame] not in heading_scopes:
            heading_scopes.append([frame])
    def heading_visible(browser):
        for scope in heading_scopes:
            try:
                _frame_context(browser, scope, timeout=0)
            except (TimeoutException, AssertionError):
                # Frame absent/collapsed or ambiguous: it cannot hold the heading.
                continue
            if any(element.is_displayed() for element in _recipe_matches(browser, recipe)):
                return True
        return False
    def arrived(browser):
        try:
            _target(browser, target_id, timeout=0)
        except TimeoutException:
            return False
        return heading_visible(browser)
    _runtime_wait(driver, timeout).until(arrived)
    _record_event(driver, "screen_verified", target=target_id, screen=title)
    return True


def _optional_click(driver, target_id, next_target_id, *, timeout=None):
    """Only absence is optional. Click errors propagate; never suppress them."""
    try:
        _assert_screen(driver, next_target_id, timeout=1)
        _record_event(driver, "conditional_confirmation", branch="not_needed", target=target_id, successor=next_target_id)
        return "not_needed"
    except TimeoutException:
        pass
    _target(driver, target_id, action="click", timeout=timeout).click()
    _record_event(driver, "conditional_confirmation", branch="clicked", target=target_id, successor=next_target_id)
    _assert_screen(driver, next_target_id, timeout=timeout)
    return "clicked"


def _login_as(driver, persona, *, timeout=None):
    """Switch the live browser session to a different named identity.

    For a multi-actor procedure that must run as ONE continuous script
    (e.g. a requester creates a record, an approver later approves it, an
    admin performs a final override) rather than three separate scripts:
    a single Selenium session can only be authenticated as one identity at
    a time, so moving on to the next actor's part means fully ending the
    current session's authentication and starting a fresh one, not
    holding two identities at once.

    `persona` is a plain string naming the role (e.g. "requester",
    "approver", "admin", or any other name a scenario defines) -- there is
    NO fixed list of supported roles anywhere in this function or
    anywhere else in the generated script. It is used only to look up two
    already-externalised test-data keys: "<persona>_username" and
    "<persona>_password" (see test_data/loader.py's personas flattening
    and service/auth_gateway.py's existing generic TEST_* export) -- so a
    procedure needing a new role is purely a test-data change, never a
    code change.

    This is for every identity switch AFTER the first. The first login of
    a script is still handled entirely by the existing canonical preamble
    inside _open_application()/_sso_step() (using the application's
    flat, default username/password) -- _login_as is never used for step 1.

    Scoped to Azure Entra ID (Microsoft Entra) applications only, same as
    the existing SSO preamble this mirrors; other login types (CAM,
    generic forms) are not supported by this runtime helper.

    Steps:
      1. Clear cookies and local/session storage for the current origin
         so the previous identity's session cannot silently persist
         through the re-navigation below.
      2. Re-navigate to APP_URL, which (for an SSO-gated app) triggers a
         fresh Azure Entra redirect since there is no valid session left.
      3. Complete the Entra username/password/stay-signed-in flow using
         THIS persona's own credentials.
    """
    try:
        driver.delete_all_cookies()
    except Exception:
        pass
    try:
        driver.execute_script("window.localStorage.clear(); window.sessionStorage.clear();")
    except Exception:
        pass
    app_url = os.environ.get("APP_URL") or os.environ.get("NETRA_URL")
    if not app_url:
        raise RuntimeError("APP_URL is not configured")
    driver.get(app_url)
    username = get_data(f"{persona}_username")
    password = get_data(f"{persona}_password")
    step_timeout = timeout if timeout is not None else float(os.environ.get("SELENIUM_TIMEOUT", "30"))
    wait = _runtime_wait(driver, step_timeout)
    deadline = time.time() + step_timeout
    needs_interactive = _ENTRA_REDIRECT_DOMAIN in (driver.current_url or "")
    while not needs_interactive and time.time() < deadline:
        current = driver.current_url or ""
        if _ENTRA_REDIRECT_DOMAIN in current:
            needs_interactive = True
            break
        if driver.find_elements(By.CSS_SELECTOR, _ENTRA_USERNAME_SELECTOR):
            needs_interactive = True
            break
        time.sleep(0.25)
    if needs_interactive:
        wait.until(EC.visibility_of_element_located(
            (By.CSS_SELECTOR, _ENTRA_USERNAME_SELECTOR))).send_keys(username)
        wait.until(EC.element_to_be_clickable((By.ID, _ENTRA_NEXT_BUTTON_ID))).click()
        wait.until(EC.visibility_of_element_located(
            (By.CSS_SELECTOR, _ENTRA_PASSWORD_SELECTOR))).send_keys(password)
        wait.until(EC.element_to_be_clickable((By.ID, _ENTRA_SUBMIT_BUTTON_ID))).click()
        # Optional "Stay signed in?" prompt (KMSI) — same button id as Sign in.
        kmsi_deadline = time.time() + 8
        while _ENTRA_REDIRECT_DOMAIN in (driver.current_url or "") and time.time() < kmsi_deadline:
            try:
                kmsi_btn = driver.find_elements(By.ID, _ENTRA_SUBMIT_BUTTON_ID)
                if kmsi_btn and kmsi_btn[0].is_displayed() and kmsi_btn[0].is_enabled():
                    kmsi_btn[0].click()
                    break
            except Exception:
                pass
            time.sleep(0.3)
        redirect_deadline = time.time() + step_timeout
        while _ENTRA_REDIRECT_DOMAIN in (driver.current_url or ""):
            if time.time() > redirect_deadline:
                raise TimeoutException(
                    "Timed out waiting for Azure Entra redirect back to the "
                    "application while logging in as persona: " + str(persona)
                )
            time.sleep(0.5)
    _record_event(driver, "persona_login", persona=persona)