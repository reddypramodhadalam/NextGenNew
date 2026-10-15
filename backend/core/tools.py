"""
core/tools.py

Selenium-backed browser tool implementations.

This is the only file that calls Selenium directly. Agents never import
Selenium — they call these functions via the agent loop.

Error handling: every function returns a structured result dict rather
than raising. An agent that gets {"error": "..."} back can decide what
to do next (re-snapshot, try something else, call done) rather than
the whole process dying on an unexpected exception.
"""

import datetime
import logging
import time
from typing import Optional
from selenium.common.exceptions import (
    NoSuchElementException,
    StaleElementReferenceException,
    ElementNotInteractableException,
    ElementClickInterceptedException,
    TimeoutException,
    WebDriverException,
    NoAlertPresentException,
    NoSuchWindowException,
)
from selenium.webdriver.common.action_chains import ActionChains
from selenium.webdriver.common.by import By
from selenium.webdriver.common.keys import Keys
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.wait import WebDriverWait

# Phase 3: Import iframe helper for snapshot enhancement
from service.iframe_helper import explore_iframes, detect_iframes, resolve_jde_grid_cell_locator

logger = logging.getLogger(__name__)

INTERACTIVE_SELECTOR = (
    "button, a, input, textarea, select, "
    "[role='button'], [role='menuitem'], [role='menu'], [role='textbox'], "
    "[role='link'], [role='tab'], [role='checkbox'], [role='radio'], "
    # ARIA tree-widget roles (e.g. JD Edwards' accessible navigator menu
    # tree, JDE 9.2+): without these, a tree of role='treeitem' nodes is
    # invisible to get_snapshot even though it is fully clickable, which
    # pushes the agent toward whatever plain <a> links ARE captured (e.g.
    # the breadcrumb) instead of the actual navigation menu. Verify against
    # the real running app and adjust if JDE's tree does not use these roles.
    "[role='treeitem'], [role='tree'], "
    # JDE's menu bar (e.g. drop_mainmenu) uses plain, non-semantic <div>s
    # with a tabindex attribute instead of a button/role — confirmed via a
    # live debug session against the real app on 2026-09-09. tabindex="-1"
    # is excluded because that value is commonly used for containers meant
    # to be programmatically focusable only, not real click targets.
    "[tabindex]:not([tabindex='-1'])"
)

_element_cache: dict[str, object] = {}
# Active WebDriver reference — set by get_snapshot so tools that need the
# driver (navigate_to, switch_to_frame, handle_alert, etc.) can access it
# without requiring it as a parameter in every call.
_current_driver: object = None

# Set by click() on success, consumed (read then reset) by the very next
# get_snapshot() call. Lets get_snapshot give JDE's main content iframe extra
# time to finish rendering right after a click that may have navigated it,
# without adding that extra wait to every other snapshot. See
# service.iframe_helper.explore_iframes' just_clicked parameter.
_recently_clicked: bool = False

# Cache of the detected <iframe>/<frame> container list, keyed by the current
# top-level URL. In iframe-shell apps (e.g. JDE) the same set of iframe
# containers exists for as long as the URL doesn't change — only the CONTENT
# inside a couple of them changes as menus/screens are navigated. Re-querying
# the DOM for the iframe list itself on every single get_snapshot call is
# redundant. The cache is validated cheaply (one liveness check) before use
# and dropped in favor of a fresh detection the moment it proves stale — i.e.
# real evidence the page changed, not a fixed guess.
_iframe_list_cache: dict[str, list] = {}

# Raw main-document interactive-element count from the most recent
# get_snapshot() call. Used as the "before" baseline by
# _poll_for_stable_elements so it can tell the difference between "this
# click hasn't changed anything yet, keep waiting" and "nothing changed,
# stop waiting" — a plain stabilize-on-repeat check can't tell those apart
# when the DOM update hasn't even started within the first read.
_last_main_doc_count: Optional[int] = None


def _poll_for_stable_elements(driver, baseline_count: Optional[int], max_wait: float = 3.0, interval: float = 0.3) -> list:
    """
    Right after a click, the main document's interactive element count can
    still be changing for a brief moment (e.g. a JDE menu category expanding
    into its child items). Two phases, both bounded by a single max_wait
    deadline:
      1. Wait for the count to actually differ from baseline_count (the
         count captured just before the click) — skipped if no baseline
         is known yet.
      2. Once it has changed (or phase 1 timed out), wait for it to stop
         changing between consecutive reads before trusting it.
    """
    deadline = time.time() + max_wait
    elements = driver.find_elements(By.CSS_SELECTOR, INTERACTIVE_SELECTOR)

    if baseline_count is not None:
        while time.time() < deadline and len(elements) == baseline_count:
            time.sleep(interval)
            elements = driver.find_elements(By.CSS_SELECTOR, INTERACTIVE_SELECTOR)

    prev_count = len(elements)
    while time.time() < deadline:
        time.sleep(interval)
        elements = driver.find_elements(By.CSS_SELECTOR, INTERACTIVE_SELECTOR)
        count = len(elements)
        if count == prev_count:
            break
        prev_count = count
    return elements


def get_snapshot(driver) -> list[dict]:
    """
    Reads every interactive, visible element on screen.

    Includes elements from the main document AND all iframes (if present).
    Iframe elements are annotated with 'iframe_context' field to indicate
    which iframe they belong to.

    Args:
        driver: A live Selenium WebDriver instance.

    Returns:
        A list of element dicts, each with 'ref', 'role', and 'name'.
        Iframe elements additionally include 'iframe_context' field.
        If the page itself is unreachable, returns a single-item list
        containing {"error": "..."} so the agent can react rather than crash.
    """
    global _element_cache, _current_driver, _recently_clicked, _last_main_doc_count
    _element_cache = {}
    _current_driver = driver  # stored so driver-requiring tools can reach it

    # Consume the click flag now — only THIS snapshot gets the extra
    # patience below; any later snapshot without a fresh click reverts to
    # the normal quick poll.
    just_clicked = _recently_clicked
    _recently_clicked = False

    try:
        if just_clicked:
            elements = _poll_for_stable_elements(driver, _last_main_doc_count)
        else:
            elements = driver.find_elements(By.CSS_SELECTOR, INTERACTIVE_SELECTOR)
    except WebDriverException as e:
        logger.error("get_snapshot: could not read page — %s", e)
        return [{"error": f"Could not read the page: {e}"}]

    _last_main_doc_count = len(elements)

    snapshot = []
    for i, el in enumerate(elements):
        try:
            if not el.is_displayed():
                continue
            # aria-hidden=true means the element is intentionally hidden
            # from assistive technology — treat it as not present.
            if el.get_attribute("aria-hidden") == "true":
                continue

            ref = f"el-{i}"
            role = el.get_attribute("role") or el.tag_name
            # For submit/button/reset inputs the visible label is the 'value'
            # attribute (e.g. <input type="submit" value="Submit">).
            # Text inputs must NOT use value — that would capture the current
            # field content, not the field label.
            input_type = (el.get_attribute("type") or "").lower()
            value_label = (
                el.get_attribute("value")
                if input_type in ("submit", "button", "reset")
                else ""
            ) or ""

            # locator records WHICH real attribute actually produced the name
            # below (or the JDE grid recipe, for a cell with no real attribute
            # at all) — a generated script transcribes a selector straight
            # from this instead of re-guessing one from the flattened name
            # text. See service.trace_compiler. Preserves the exact original
            # name-precedence order: aria-label, placeholder, value (submit/
            # button/reset only), visible text, title.
            name = ""
            locator: Optional[dict] = None
            for attr, value in (
                ("aria-label", el.get_attribute("aria-label")),
                ("placeholder", el.get_attribute("placeholder")),
                ("value", value_label),
                ("text", el.text),
                ("title", el.get_attribute("title")),
            ):
                value = (value or "").strip()
                if value:
                    name = value
                    locator = {"strategy": "attribute", "attr": attr, "value": value}
                    break

            if not name and el.tag_name.lower() in ("input", "textarea", "select"):
                # See resolve_jde_grid_cell_locator — editable grid cells (e.g.
                # Item Number / Unit Price in the Sales Order Entry line-item
                # grid) carry no distinguishing name of their own; without
                # this every cell in the row looks identical to the agent.
                # locator here is a recipe (column header + row position),
                # never the grid's own DOM id — JDE regenerates that id every
                # page load, so nothing recorded here may depend on it.
                grid_locator = resolve_jde_grid_cell_locator(driver, el)
                if grid_locator:
                    name = grid_locator["column"]
                    locator = grid_locator

            if not name:
                # Some controls (e.g. JD Edwards' Navigator button) put the
                # human-readable label on a wrapping container while the
                # actual focusable/clickable leaf our selector matched (an
                # empty child div) has no attributes of its own at all.
                # Checking one parent level up recovers that label. Confirmed
                # against the real Navigator button's DOM on 2026-09-10 —
                # both the title and the onclick handler live on the
                # immediate parent, not the empty leaf. Bounded to a single
                # ancestor level to avoid picking up an unrelated label from
                # a large enclosing container.
                try:
                    parent = el.find_element(By.XPATH, "..")
                    parent_label = (parent.get_attribute("aria-label") or "").strip() \
                        or (parent.get_attribute("title") or "").strip()
                    if parent_label:
                        name = parent_label
                        locator = {"strategy": "ancestor_attribute",
                                   "attr": "aria-label" if parent.get_attribute("aria-label") else "title",
                                   "value": parent_label}
                        el = parent  # click the labeled parent, not the blank leaf
                except (NoSuchElementException, StaleElementReferenceException):
                    pass

            # Decorative ancestor containers (seen as role="menu") in
            # iframe-shell apps like JDE sometimes expose no real text of
            # their own — what leaks through as .text is the browser's
            # legacy no-iframe-support fallback message from a nested
            # <iframe> that hasn't rendered yet. This is never a real click
            # target. Left in, it was the ONLY element labeled role="menu"
            # on the page, so the agent repeatedly (and wrongly) treated it
            # as "the main menu" — every post-login click in a real run
            # targeted this one bogus element instead of the actual
            # navigator, which lives inside an iframe.
            if "iframe support is required" in name.lower():
                continue

            _element_cache[ref] = el
            snapshot.append({"ref": ref, "role": role, "name": name, "locator": locator})

        except StaleElementReferenceException:
            # Page re-rendered between find and read. Skip this element —
            # a slightly stale snapshot is more useful than no snapshot.
            continue

    # ── De-duplicate nested same-name candidates ────────────────────────
    # A single visual menu row can match INTERACTIVE_SELECTOR twice: once
    # for an outer wrapper (e.g. a tree row <table tabindex="0">) and once
    # for a real clickable target nested inside it (e.g. an <a href=
    # "javascript:RunNewApp(...)">). Both get the exact same name because
    # .text on the wrapper is just the inner element's text. Confirmed via
    # a real JD Edwards menu DOM on 2026-09-14: FOLDER rows' wrapper onclick
    # legitimately does the work (expands the tree), but LEAF/application
    # rows' wrapper onclick is a no-op — only the nested <a>'s href actually
    # navigates. Clicking the wrapper on a leaf row "succeeds" (no error)
    # but does nothing, which looks identical to a slow-loading page and
    # sends the agent hunting in iframes for content that was never there.
    # Clicking the nested element is always at least as effective as
    # clicking its ancestor (the click still bubbles up to the ancestor's
    # own handler), so when two same-named candidates are nested, only the
    # innermost one is kept. Only elements sharing an identical name string
    # are ever compared, so this is cheap — most snapshots have no
    # duplicate names at all.
    if len(snapshot) > 1:
        _by_name: dict[str, list[int]] = {}
        for _idx, _entry in enumerate(snapshot):
            _by_name.setdefault(_entry["name"], []).append(_idx)
        _drop_indices: set[int] = set()
        for _name, _idxs in _by_name.items():
            if len(_idxs) < 2:
                continue
            for _i in _idxs:
                if _i in _drop_indices:
                    continue
                _el_i = _element_cache.get(snapshot[_i]["ref"])
                if _el_i is None:
                    continue
                for _j in _idxs:
                    if _i == _j or _j in _drop_indices:
                        continue
                    _el_j = _element_cache.get(snapshot[_j]["ref"])
                    if _el_j is None:
                        continue
                    try:
                        _i_contains_j = driver.execute_script(
                            "return arguments[0] !== arguments[1] && "
                            "arguments[0].contains(arguments[1]);",
                            _el_i, _el_j,
                        )
                    except WebDriverException:
                        continue
                    if _i_contains_j:
                        _drop_indices.add(_i)  # _i is the ancestor — drop it
                        break
        if _drop_indices:
            logger.info(
                "get_snapshot: dropped %d ancestor-wrapper duplicate(s) in favor "
                "of their nested same-named element", len(_drop_indices),
            )
            for _idx in sorted(_drop_indices, reverse=True):
                _element_cache.pop(snapshot[_idx]["ref"], None)
                del snapshot[_idx]

    logger.debug("get_snapshot: found %d interactive elements in main document", len(snapshot))

    # ── Phase 3: Detect iframes once, then explore + cache containers ──────────
    # detect_iframes() used to be called twice per snapshot (once inside
    # explore_all_iframes, once again below for container caching) — each
    # call is a full DOM query. It now runs once and both passes below reuse
    # the same result.
    #
    # The list of iframe CONTAINERS itself is additionally cached per-URL
    # (see _iframe_list_cache above) — re-detecting all ~12 iframe elements
    # on every single get_snapshot call is redundant work when the page
    # hasn't actually reloaded. A cheap liveness probe on the first cached
    # element proves whether the cache is still good before trusting it.
    try:
        _url = driver.current_url
    except Exception:
        _url = None

    _iframes = None
    _cached = _iframe_list_cache.get(_url) if _url else None
    if _cached is not None:
        try:
            if _cached:
                _cached[0][2].is_enabled()  # canary — raises if the DOM moved on
            _iframes = _cached
        except (StaleElementReferenceException, WebDriverException, IndexError):
            logger.debug("get_snapshot: cached iframe list went stale for %r — re-detecting", _url)
            _iframes = None

    if _iframes is None:
        try:
            _iframes = detect_iframes(driver)
            if _url is not None:
                _iframe_list_cache[_url] = _iframes
        except Exception as e:
            logger.warning("get_snapshot: iframe detection failed (continuing): %s", e)
            _iframes = []

    if _iframes:
        try:
            iframe_elements = explore_iframes(driver, _iframes, INTERACTIVE_SELECTOR, just_clicked=just_clicked)
            if iframe_elements:
                snapshot.extend(iframe_elements)
                logger.info(
                    "get_snapshot: captured %d elements from iframes (total snapshot: %d)",
                    len(iframe_elements), len(snapshot)
                )
        except Exception as e:
            # Iframe exploration errors should not block main snapshot
            logger.warning("get_snapshot: iframe exploration failed (continuing): %s", e)

        # ── Cache iframe containers themselves ──────────────────────────────
        # The elements captured *inside* an iframe above (ifr-* refs) are
        # intentionally never cached — cross-context WebElement handles go stale
        # the moment we switch back to the main document. The iframe container
        # is different: it is a normal element of the main document, so it can
        # be cached and later handed to switch_to_frame(), which moves the
        # driver's actual context inside it. Once switched in, a fresh
        # get_snapshot() call finds that iframe's fields with normal (non-ifr-)
        # refs, and click/type_text work on them directly.
        try:
            for frame_id, frame_title, iframe_el in _iframes:
                frame_ref = f"frame-{frame_id}"
                _element_cache[frame_ref] = iframe_el
                snapshot.append({
                    "ref": frame_ref,
                    "role": "iframe",
                    "name": frame_title or frame_id,
                })
            logger.debug("get_snapshot: cached iframe container ref(s) for switch_to_frame")
        except Exception as e:
            # Container caching errors should not block the main snapshot either.
            logger.warning("get_snapshot: iframe container caching failed (continuing): %s", e)

    logger.debug("get_snapshot: final snapshot has %d elements (main + iframes)", len(snapshot))
    return snapshot


def click(ref: str) -> dict:
    """
    Clicks one element by its ref from the most recent snapshot.

    Args:
        ref: The element reference string from get_snapshot().

    Returns:
        {"ok": True} on success, or {"error": "..."} on failure.
    """
    el = _element_cache.get(ref)
    if el is None:
        # Check if this is an iframe element ref (ifr-*-el-*)
        # Iframe elements are not cached (cross-context issue), so we provide guidance
        if ref.startswith("ifr-"):
            return {
                "error": (
                    f"Cannot click iframe element '{ref}' during crawling. "
                    "Iframe elements are captured for test generation reference, but should not "
                    "be clicked during exploration. Focus on the main application workflow. "
                    "If the BRD requires testing an iframe workflow, call get_snapshot to re-read "
                    "the current screen state and provide more context."
                )
            }
        return {"error": f"Unknown ref '{ref}' — call get_snapshot() again first."}

    global _recently_clicked
    try:
        el.click()
        logger.info("click: ref=%s", ref)
        _recently_clicked = True
        return {"ok": True}
    except StaleElementReferenceException:
        return {"error": "Element no longer exists — screen changed. Call get_snapshot() again."}
    except ElementNotInteractableException:
        return {"error": "Element exists but is not clickable right now (hidden, disabled, or covered)."}
    except TimeoutException:
        return {"error": "Click timed out — page may be unresponsive."}
    except WebDriverException as e:
        return {"error": f"Click failed: {e}"}


def type_text(ref: str, text: str) -> dict:
    """
    Clears a text input and types the given text into it.

    Args:
        ref: The element reference string from get_snapshot().
        text: The exact text to type.

    Returns:
        {"ok": True} on success, or {"error": "..."} on failure.
    """
    el = _element_cache.get(ref)
    if el is None:
        # Check if this is an iframe element ref (ifr-*-el-*)
        # Iframe elements are not cached (cross-context issue), so we provide guidance
        if ref.startswith("ifr-"):
            return {
                "error": (
                    f"Cannot type into iframe element '{ref}' during crawling. "
                    "Iframe elements are captured for test generation reference, but should not "
                    "be interacted with during exploration. Focus on the main application workflow."
                )
            }
        return {"error": f"Unknown ref '{ref}' — call get_snapshot() again first."}

    try:
        # 0-F: React-safe input.  el.clear() sends a DOM CLEAR command that
        # bypasses React's synthetic onChange event — the component state keeps
        # the old value and the form submits old_value + new_text instead of
        # new_text alone.  Ctrl+A selects all existing text (triggering onChange
        # with an empty selection) and the following send_keys replaces the
        # selection, which correctly fires onChange with the new value.
        el.send_keys(Keys.CONTROL + 'a')
        el.send_keys(text)
        # Best-effort value verification — works for standard inputs/textareas.
        # Skipped for elements that don't support get_attribute("value") (e.g.
        # contentEditable divs) without raising.
        try:
            actual = el.get_attribute("value") or ""
            if actual and actual != text:
                logger.warning(
                    "type_text: value mismatch after input "
                    "(ref=%s, expected=%r, got=%r) — "
                    "page may have transformed or constrained the input",
                    ref, text[:40], actual[:40],
                )
        except WebDriverException:
            pass  # element doesn't support value attribute — not an error
        logger.debug("type_text: ref=%s text=%r", ref, text[:40])
        return {"ok": True}
    except StaleElementReferenceException:
        return {"error": "Element no longer exists — screen changed. Call get_snapshot() again."}
    except ElementNotInteractableException:
        return {"error": "Element exists but cannot accept text right now."}
    except WebDriverException as e:
        return {"error": f"Typing failed: {e}"}



def done(passed: bool, notes: str = "") -> dict:
    """
    The agent's signal that it has finished.

    Args:
        passed: True if the expected outcome was confirmed.
        notes: The agent's explanation.

    Returns:
        {"passed": bool, "notes": str}
    """
    return {"passed": passed, "notes": notes}


# ── Phase 1 tools: navigation, frames, dropdowns, waits, alerts ───────────────
# All of these read _current_driver, which is set by get_snapshot() each time
# the agent takes a snapshot. Agents must call get_snapshot first.


def navigate_to(url: str) -> dict:
    """
    Navigate the browser to a URL and wait for the page to finish loading.

    Args:
        url: The absolute URL to navigate to.

    Returns:
        {"ok": True, "url": final_url} on success, or {"error": "..."} on failure.
    """
    if _current_driver is None:
        return {"error": "No active browser session \u2014 call get_snapshot(driver) first."}
    if not url:
        return {"error": "navigate_to: url must not be empty."}
    logger.info("navigate_to: %s", url)
    try:
        _current_driver.get(url)
        WebDriverWait(_current_driver, 15).until(
            lambda d: d.execute_script("return document.readyState") == "complete"
        )
        final = _current_driver.current_url
        logger.info("navigate_to: loaded  final_url=%s", final)
        return {"ok": True, "url": final}
    except TimeoutException:
        # Page took >15 s — proceed with whatever is rendered
        final = _current_driver.current_url
        logger.warning("navigate_to: readyState timeout after 15 s  url=%s  current=%s", url, final)
        return {"ok": True, "url": final}
    except WebDriverException as exc:
        logger.error("navigate_to failed: url=%s  error=%s", url, exc)
        return {"error": f"Navigation failed: {exc}"}


def get_current_url() -> dict:
    """
    Return the browser's current URL.

    Useful for verifying that a submit or navigation went to the expected page.

    Returns:
        {"url": current_url} or {"error": "..."}
    """
    if _current_driver is None:
        return {"error": "No active browser session \u2014 call get_snapshot(driver) first."}
    try:
        url = _current_driver.current_url
        logger.debug("get_current_url: %s", url)
        return {"url": url}
    except WebDriverException as exc:
        logger.error("get_current_url failed: %s", exc)
        return {"error": f"Could not read URL: {exc}"}


def switch_to_frame(frame_ref: str) -> dict:
    """
    Switch into an iframe so subsequent get_snapshot/click/type_text operate
    inside it. Pass 'main' to return to the top-level document.

    Args:
        frame_ref: A ref from the most recent get_snapshot, or the string 'main'.

    Returns:
        {"ok": True} or {"error": "..."}
    """
    global _recently_clicked
    if _current_driver is None:
        return {"error": "No active browser session \u2014 call get_snapshot(driver) first."}
    if frame_ref == "main":
        try:
            _current_driver.switch_to.default_content()
            logger.debug("switch_to_frame: returned to main document")
            return {"ok": True}
        except WebDriverException as exc:
            logger.error("switch_to_frame: default_content failed: %s", exc)
            return {"error": f"Switch to main content failed: {exc}"}
    el = _element_cache.get(frame_ref)
    if el is not None:
        try:
            _current_driver.switch_to.frame(el)
            logger.debug("switch_to_frame: switched to frame ref=%s", frame_ref)
            # A freshly-entered frame's content can still be loading (this is
            # exactly the e1menuAppIframe/Standard Order SO case — a plain
            # instant read after switching in caught it at 1 element,
            # mid-render). Reuse the same post-click stabilization polling
            # in the next get_snapshot() instead of trusting an instant read.
            _recently_clicked = True
            return {"ok": True}
        except WebDriverException as exc:
            # The cached handle is stale — most likely the page rebuilt its
            # DOM under the same URL (seen on JDE) since this ref was cached.
            # Fall through and try to re-locate the same iframe by id instead
            # of failing outright, so the model doesn't have to notice the
            # error, re-snapshot, and retry itself.
            logger.warning(
                "switch_to_frame: cached ref=%s went stale (%s) — re-locating", frame_ref, exc
            )
            el = None

    frame_id = frame_ref[len("frame-"):] if frame_ref.startswith("frame-") else frame_ref
    try:
        for fid, _title, iframe_el in detect_iframes(_current_driver):
            if fid != frame_id:
                continue
            _element_cache[frame_ref] = iframe_el
            try:
                _current_driver.switch_to.frame(iframe_el)
                logger.info(
                    "switch_to_frame: recovered ref=%s by re-detecting the iframe", frame_ref
                )
                _recently_clicked = True
                return {"ok": True}
            except WebDriverException as exc2:
                logger.error("switch_to_frame: retry failed ref=%s  error=%s", frame_ref, exc2)
                return {"error": f"Switch to frame failed after retry: {exc2}"}
    except WebDriverException as exc3:
        logger.error("switch_to_frame: re-detection failed ref=%s  error=%s", frame_ref, exc3)

    logger.warning("switch_to_frame: unknown ref '%s' — call get_snapshot() first", frame_ref)
    return {"error": f"Unknown ref '{frame_ref}' — call get_snapshot() again first."}


def select_option(ref: str, value: str) -> dict:
    """
    Select one option from a native <select> dropdown by its visible text.
    Falls back to matching by value attribute if the visible text is not found.

    Args:
        ref:   Ref of the <select> element from the most recent get_snapshot.
        value: The visible text of the option to select.

    Returns:
        {"ok": True} or {"error": "..."}
    """
    el = _element_cache.get(ref)
    if el is None:
        return {"error": f"Unknown ref '{ref}' \u2014 call get_snapshot() again first."}
    try:
        from selenium.webdriver.support.select import Select
        sel = Select(el)
        try:
            sel.select_by_visible_text(value)
        except Exception:
            sel.select_by_value(value)  # fallback: match by value attribute
        logger.debug("select_option: ref=%s value=%r", ref, value)
        return {"ok": True}
    except StaleElementReferenceException:
        return {"error": "Element no longer exists \u2014 call get_snapshot() again."}
    except WebDriverException as exc:
        return {"error": f"Could not select '{value}': {exc}"}


def scroll_into_view(ref: str) -> dict:
    """
    Scroll the viewport until the element is centred on screen.

    Elements below the fold return is_displayed()=False and are invisible to
    get_snapshot. Scrolling before interacting fixes this.

    Args:
        ref: Ref of the element to scroll to.

    Returns:
        {"ok": True} or {"error": "..."}
    """
    if _current_driver is None:
        return {"error": "No active browser session \u2014 call get_snapshot(driver) first."}
    el = _element_cache.get(ref)
    if el is None:
        return {"error": f"Unknown ref '{ref}' \u2014 call get_snapshot() again first."}
    try:
        _current_driver.execute_script(
            "arguments[0].scrollIntoView({block:'center', inline:'nearest'});", el
        )
        logger.debug("scroll_into_view: ref=%s", ref)
        return {"ok": True}
    except WebDriverException as exc:
        return {"error": f"Scroll failed: {exc}"}


def handle_alert(action: str = "accept") -> dict:
    """
    Accept or dismiss a browser alert/confirm/prompt dialog.

    Without this, window.confirm() and window.alert() freeze the page: every
    subsequent action fails with UnexpectedAlertPresentException.

    Args:
        action: 'accept' (click OK) or 'dismiss' (click Cancel).

    Returns:
        {"ok": True, "text": alert_text} or {"error": "..."}
    """
    if _current_driver is None:
        return {"error": "No active browser session \u2014 call get_snapshot(driver) first."}
    try:
        alert = WebDriverWait(_current_driver, 5).until(EC.alert_is_present())
        text = alert.text or ""
        if action == "accept":
            alert.accept()
        else:
            alert.dismiss()
        logger.debug("handle_alert: action=%s text=%r", action, text)
        return {"ok": True, "text": text}
    except TimeoutException:
        return {"error": "No browser alert is present (waited 5s)."}
    except WebDriverException as exc:
        return {"error": f"Alert handling failed: {exc}"}


def wait_for_element(selector: str, timeout: int = 10, poll_interval: float = 0.5) -> dict:
    """
    Wait for a CSS selector to be visible on screen.

    Useful after slow actions (form submit, async data load) before
    verifying the result. Removes the need for time.sleep() in scripts.

    WebDriverWait is a FluentWait subclass that polls the DOM at a fixed
    interval until the condition is met or the timeout expires.

    Args:
        selector:      CSS selector string.
        timeout:       Maximum seconds to wait (default 10).
        poll_interval: How often (in seconds) to re-check the DOM (default 0.5).
                       Lower values detect the element faster but add CPU load.
                       Raise it (e.g. 1.0) for slow server-rendered pages.

    Returns:
        {"ok": True} when visible, or {"error": "..."} on timeout.
    """
    if _current_driver is None:
        return {"error": "No active browser session — call get_snapshot(driver) first."}
    if poll_interval <= 0:
        return {"error": "poll_interval must be greater than 0."}
    logger.debug(
        "wait_for_element: selector=%r  timeout=%ds  poll=%.2fs",
        selector, timeout, poll_interval,
    )
    try:
        WebDriverWait(_current_driver, timeout, poll_frequency=poll_interval).until(
            EC.visibility_of_element_located((By.CSS_SELECTOR, selector))
        )
        logger.debug("wait_for_element: visible  selector=%r", selector)
        return {"ok": True}
    except TimeoutException:
        logger.warning(
            "wait_for_element: timed out after %ds  selector=%r", timeout, selector
        )
        return {"error": f"Element '{selector}' not visible after {timeout}s."}
    except WebDriverException as exc:
        logger.error("wait_for_element: error  selector=%r  %s", selector, exc)
        return {"error": f"Wait failed: {exc}"}


# ── Phase 2 tools: mouse gestures ─────────────────────────────────────────────


def hover(ref: str) -> dict:
    """Move the mouse over an element to trigger :hover styles, tooltips, or dropdown menus.
    Call get_snapshot after hovering to see newly revealed elements."""
    el = _element_cache.get(ref)
    if el is None:
        return {"error": f"Unknown ref '{ref}' — call get_snapshot() first."}
    if _current_driver is None:
        return {"error": "No active browser session — call get_snapshot(driver) first."}
    try:
        ActionChains(_current_driver).move_to_element(el).perform()
        logger.debug("hover: ref=%s", ref)
        return {"ok": True}
    except StaleElementReferenceException:
        return {"error": "Element no longer exists — call get_snapshot() again."}
    except WebDriverException as exc:
        logger.error("hover: failed ref=%s  %s", ref, exc)
        return {"error": f"Hover failed: {exc}"}


def double_click(ref: str) -> dict:
    """Double-click an element. Use for file open, inline rename, editable cells."""
    el = _element_cache.get(ref)
    if el is None:
        return {"error": f"Unknown ref '{ref}' — call get_snapshot() first."}
    if _current_driver is None:
        return {"error": "No active browser session — call get_snapshot(driver) first."}
    try:
        ActionChains(_current_driver).double_click(el).perform()
        logger.debug("double_click: ref=%s", ref)
        return {"ok": True}
    except StaleElementReferenceException:
        return {"error": "Element no longer exists — call get_snapshot() again."}
    except WebDriverException as exc:
        logger.error("double_click: failed ref=%s  %s", ref, exc)
        return {"error": f"Double-click failed: {exc}"}


def right_click(ref: str) -> dict:
    """Right-click (context-click) to open a context menu. Call get_snapshot after to see menu items."""
    el = _element_cache.get(ref)
    if el is None:
        return {"error": f"Unknown ref '{ref}' — call get_snapshot() first."}
    if _current_driver is None:
        return {"error": "No active browser session — call get_snapshot(driver) first."}
    try:
        ActionChains(_current_driver).context_click(el).perform()
        logger.debug("right_click: ref=%s", ref)
        return {"ok": True}
    except StaleElementReferenceException:
        return {"error": "Element no longer exists — call get_snapshot() again."}
    except WebDriverException as exc:
        logger.error("right_click: failed ref=%s  %s", ref, exc)
        return {"error": f"Right-click failed: {exc}"}


def drag_and_drop(source_ref: str, target_ref: str) -> dict:
    """Drag source element and drop it onto target. Use for reordering, kanban moves, resize."""
    source = _element_cache.get(source_ref)
    target = _element_cache.get(target_ref)
    if source is None:
        return {"error": f"Unknown source ref '{source_ref}' — call get_snapshot() first."}
    if target is None:
        return {"error": f"Unknown target ref '{target_ref}' — call get_snapshot() first."}
    if _current_driver is None:
        return {"error": "No active browser session — call get_snapshot(driver) first."}
    try:
        ActionChains(_current_driver).drag_and_drop(source, target).perform()
        logger.debug("drag_and_drop: %s -> %s", source_ref, target_ref)
        return {"ok": True}
    except StaleElementReferenceException:
        return {"error": "Source or target element no longer exists — call get_snapshot() again."}
    except WebDriverException as exc:
        logger.error("drag_and_drop: failed %s->%s  %s", source_ref, target_ref, exc)
        return {"error": f"Drag-and-drop failed: {exc}"}


# ── Phase 2 tools: keyboard ───────────────────────────────────────────────────

_KEY_MAP: dict[str, str] = {
    "enter": Keys.ENTER, "return": Keys.RETURN, "tab": Keys.TAB,
    "escape": Keys.ESCAPE, "esc": Keys.ESCAPE, "space": Keys.SPACE,
    "backspace": Keys.BACK_SPACE, "delete": Keys.DELETE,
    "home": Keys.HOME, "end": Keys.END,
    "pageup": Keys.PAGE_UP, "pagedown": Keys.PAGE_DOWN,
    "arrowup": Keys.ARROW_UP, "arrowdown": Keys.ARROW_DOWN,
    "arrowleft": Keys.ARROW_LEFT, "arrowright": Keys.ARROW_RIGHT,
    "f1": Keys.F1, "f2": Keys.F2, "f3": Keys.F3, "f4": Keys.F4,
    "f5": Keys.F5, "f6": Keys.F6, "f7": Keys.F7, "f8": Keys.F8,
    "f9": Keys.F9, "f10": Keys.F10, "f11": Keys.F11, "f12": Keys.F12,
}

_MOD_MAP: dict[str, str] = {
    "ctrl": Keys.CONTROL, "control": Keys.CONTROL,
    "shift": Keys.SHIFT, "alt": Keys.ALT, "meta": Keys.META,
}


def press_key(key: str) -> dict:
    """Press a keyboard key on the currently focused element or the page body.
    Accepts names: enter, tab, escape, space, backspace, delete, home, end,
    pageup, pagedown, arrowup/down/left/right, f1-f12, or any single character."""
    if _current_driver is None:
        return {"error": "No active browser session — call get_snapshot(driver) first."}
    if not key:
        return {"error": "key must not be empty."}
    resolved = _KEY_MAP.get(key.lower(), key)
    try:
        ActionChains(_current_driver).send_keys(resolved).perform()
        logger.debug("press_key: key=%r", key)
        return {"ok": True}
    except WebDriverException as exc:
        logger.error("press_key: failed key=%r  %s", key, exc)
        return {"error": f"Key press failed: {exc}"}


def key_combo(ref: str, keys: list) -> dict:
    """Send a keyboard shortcut to an element (e.g. Ctrl+S). Modifiers: ctrl, shift, alt, meta.
    Use ref='body' to target the page itself. Example: key_combo('el-3', ['ctrl', 's'])"""
    if _current_driver is None:
        return {"error": "No active browser session — call get_snapshot(driver) first."}
    if not keys:
        return {"error": "keys list must not be empty."}
    resolved = [_MOD_MAP.get(k.lower()) or _KEY_MAP.get(k.lower(), k) for k in keys]
    try:
        if ref == "body":
            el = _current_driver.find_element(By.TAG_NAME, "body")
        else:
            el = _element_cache.get(ref)
            if el is None:
                return {"error": f"Unknown ref '{ref}' — call get_snapshot() first."}
        chain = ActionChains(_current_driver)
        chain.click(el)
        for k in resolved[:-1]:
            chain.key_down(k)
        chain.send_keys(resolved[-1])
        for k in reversed(resolved[:-1]):
            chain.key_up(k)
        chain.perform()
        logger.debug("key_combo: ref=%s  keys=%s", ref, keys)
        return {"ok": True}
    except StaleElementReferenceException:
        return {"error": "Element no longer exists — call get_snapshot() again."}
    except WebDriverException as exc:
        logger.error("key_combo: failed ref=%s  keys=%s  %s", ref, keys, exc)
        return {"error": f"Key combo failed: {exc}"}


# ── Phase 2 tools: form helpers ───────────────────────────────────────────────


def check(ref: str) -> dict:
    """Ensure a checkbox is checked. No-op if already checked.
    Returns was_already_checked so callers can log the state change."""
    el = _element_cache.get(ref)
    if el is None:
        return {"error": f"Unknown ref '{ref}' — call get_snapshot() first."}
    try:
        already = el.is_selected()
        if not already:
            el.click()
            logger.debug("check: ref=%s  clicked to check", ref)
        else:
            logger.debug("check: ref=%s  already checked — no-op", ref)
        return {"ok": True, "was_already_checked": already}
    except StaleElementReferenceException:
        return {"error": "Element no longer exists — call get_snapshot() again."}
    except WebDriverException as exc:
        logger.error("check: failed ref=%s  %s", ref, exc)
        return {"error": f"Check failed: {exc}"}


def uncheck(ref: str) -> dict:
    """Ensure a checkbox is unchecked. No-op if already unchecked.
    Returns was_already_unchecked so callers can log the state change."""
    el = _element_cache.get(ref)
    if el is None:
        return {"error": f"Unknown ref '{ref}' — call get_snapshot() first."}
    try:
        already_off = not el.is_selected()
        if not already_off:
            el.click()
            logger.debug("uncheck: ref=%s  clicked to uncheck", ref)
        else:
            logger.debug("uncheck: ref=%s  already unchecked — no-op", ref)
        return {"ok": True, "was_already_unchecked": already_off}
    except StaleElementReferenceException:
        return {"error": "Element no longer exists — call get_snapshot() again."}
    except WebDriverException as exc:
        logger.error("uncheck: failed ref=%s  %s", ref, exc)
        return {"error": f"Uncheck failed: {exc}"}


def upload_file(ref: str, file_path: str) -> dict:
    """Upload a file by sending its absolute path to a <input type='file'> element.
    No file-picker dialog interaction needed."""
    el = _element_cache.get(ref)
    if el is None:
        return {"error": f"Unknown ref '{ref}' — call get_snapshot() first."}
    if not file_path:
        return {"error": "file_path must not be empty."}
    try:
        el.send_keys(file_path)
        logger.info("upload_file: ref=%s  path=%s", ref, file_path)
        return {"ok": True}
    except StaleElementReferenceException:
        return {"error": "Element no longer exists — call get_snapshot() again."}
    except WebDriverException as exc:
        logger.error("upload_file: failed ref=%s  %s", ref, exc)
        return {"error": f"File upload failed: {exc}"}


def clear_input(ref: str) -> dict:
    """Clear a text input using Ctrl+A then Delete (React-safe — triggers onChange correctly)."""
    el = _element_cache.get(ref)
    if el is None:
        return {"error": f"Unknown ref '{ref}' — call get_snapshot() first."}
    try:
        el.send_keys(Keys.CONTROL + 'a')
        el.send_keys(Keys.DELETE)
        logger.debug("clear_input: ref=%s", ref)
        return {"ok": True}
    except StaleElementReferenceException:
        return {"error": "Element no longer exists — call get_snapshot() again."}
    except WebDriverException as exc:
        logger.error("clear_input: failed ref=%s  %s", ref, exc)
        return {"error": f"Clear failed: {exc}"}


# ── Phase 2 tools: element inspection ────────────────────────────────────────


def is_visible(ref: str) -> dict:
    """Check whether an element is currently visible on screen.
    Returns {visible: bool} — never raises, so agents can branch on it safely."""
    el = _element_cache.get(ref)
    if el is None:
        return {"error": f"Unknown ref '{ref}' — call get_snapshot() first."}
    try:
        visible = el.is_displayed()
        logger.debug("is_visible: ref=%s  visible=%s", ref, visible)
        return {"visible": visible}
    except StaleElementReferenceException:
        return {"visible": False}
    except WebDriverException as exc:
        logger.error("is_visible: failed ref=%s  %s", ref, exc)
        return {"error": f"Visibility check failed: {exc}"}


def get_element_text(ref: str) -> dict:
    """Return the current visible text (or value) of an element.
    Use for reading dynamic values — prices, statuses, error messages — after an action."""
    el = _element_cache.get(ref)
    if el is None:
        return {"error": f"Unknown ref '{ref}' — call get_snapshot() first."}
    try:
        text = el.text or el.get_attribute("value") or ""
        logger.debug("get_element_text: ref=%s  text=%r", ref, text[:60])
        return {"text": text}
    except StaleElementReferenceException:
        return {"error": "Element no longer exists — call get_snapshot() again."}
    except WebDriverException as exc:
        logger.error("get_element_text: failed ref=%s  %s", ref, exc)
        return {"error": f"Text read failed: {exc}"}


def get_attribute(ref: str, attribute: str) -> dict:
    """Return the value of any HTML attribute (aria-checked, href, data-*, disabled, value, ...).
    Use when get_snapshot doesn't surface the property you need to assert."""
    el = _element_cache.get(ref)
    if el is None:
        return {"error": f"Unknown ref '{ref}' — call get_snapshot() first."}
    if not attribute:
        return {"error": "attribute must not be empty."}
    try:
        value = el.get_attribute(attribute)
        logger.debug("get_attribute: ref=%s  attribute=%r  value=%r", ref, attribute, value)
        return {"value": value}
    except StaleElementReferenceException:
        return {"error": "Element no longer exists — call get_snapshot() again."}
    except WebDriverException as exc:
        logger.error("get_attribute: failed ref=%s  attr=%s  %s", ref, attribute, exc)
        return {"error": f"Attribute read failed: {exc}"}


# ── Phase 2 tools: window and tab management ──────────────────────────────────


def list_tabs() -> dict:
    """Return all open browser tabs with their index, title, and handle.
    Use the index with switch_tab()."""
    if _current_driver is None:
        return {"error": "No active browser session — call get_snapshot(driver) first."}
    try:
        current = _current_driver.current_window_handle
        tabs = []
        for i, handle in enumerate(_current_driver.window_handles):
            _current_driver.switch_to.window(handle)
            tabs.append({"index": i, "title": _current_driver.title, "handle": handle})
        _current_driver.switch_to.window(current)
        logger.debug("list_tabs: %d tab(s)", len(tabs))
        return {"tabs": tabs}
    except NoSuchWindowException as exc:
        return {"error": f"Window handle no longer valid: {exc}"}
    except WebDriverException as exc:
        logger.error("list_tabs: failed  %s", exc)
        return {"error": f"Tab list failed: {exc}"}


def new_tab(url: str = "") -> dict:
    """Open a new browser tab and optionally navigate to a URL. New tab becomes active."""
    if _current_driver is None:
        return {"error": "No active browser session — call get_snapshot(driver) first."}
    try:
        _current_driver.execute_script("window.open(arguments[0], '_blank');", url or "about:blank")
        handles = _current_driver.window_handles
        _current_driver.switch_to.window(handles[-1])
        index = len(handles) - 1
        logger.info("new_tab: index=%d  url=%s", index, url or "about:blank")
        return {"ok": True, "index": index}
    except WebDriverException as exc:
        logger.error("new_tab: failed  %s", exc)
        return {"error": f"Open tab failed: {exc}"}


def switch_tab(index: int) -> dict:
    """Switch focus to a browser tab by its zero-based index (from list_tabs).
    Call get_snapshot after switching to see the new tab's elements."""
    if _current_driver is None:
        return {"error": "No active browser session — call get_snapshot(driver) first."}
    try:
        handles = _current_driver.window_handles
        if index < 0 or index >= len(handles):
            return {"error": f"Tab index {index} out of range (0-{len(handles) - 1})."}
        _current_driver.switch_to.window(handles[index])
        title = _current_driver.title
        logger.info("switch_tab: index=%d  title=%r", index, title)
        return {"ok": True, "title": title}
    except NoSuchWindowException:
        return {"error": f"Tab {index} no longer exists."}
    except WebDriverException as exc:
        logger.error("switch_tab: failed index=%d  %s", index, exc)
        return {"error": f"Switch tab failed: {exc}"}


def close_tab() -> dict:
    """Close the currently active tab. Switches focus to the last remaining tab.
    Returns an error if this is the last tab (would end the session)."""
    if _current_driver is None:
        return {"error": "No active browser session — call get_snapshot(driver) first."}
    try:
        handles = _current_driver.window_handles
        if len(handles) == 1:
            return {"error": "Cannot close the last tab — the browser session would end."}
        _current_driver.close()
        remaining = _current_driver.window_handles
        _current_driver.switch_to.window(remaining[-1])
        logger.info("close_tab: closed  remaining=%d", len(remaining))
        return {"ok": True, "remaining_tabs": len(remaining)}
    except NoSuchWindowException:
        return {"error": "Window handle no longer valid."}
    except WebDriverException as exc:
        logger.error("close_tab: failed  %s", exc)
        return {"error": f"Close tab failed: {exc}"}


# ── Phase 2 tools: browser control ───────────────────────────────────────────


def resize_window(width: int, height: int) -> dict:
    """Resize the browser viewport. Common sizes: 1440x900 desktop, 768x1024 tablet, 375x812 mobile."""
    if _current_driver is None:
        return {"error": "No active browser session — call get_snapshot(driver) first."}
    try:
        _current_driver.set_window_size(width, height)
        logger.info("resize_window: %dx%d", width, height)
        return {"ok": True}
    except WebDriverException as exc:
        logger.error("resize_window: failed %dx%d  %s", width, height, exc)
        return {"error": f"Resize failed: {exc}"}


def evaluate_js(script: str) -> dict:
    """Execute arbitrary JavaScript in the browser and return the result.
    Use 'return' in the script to send a value back. Result must be JSON-serialisable."""
    if _current_driver is None:
        return {"error": "No active browser session — call get_snapshot(driver) first."}
    if not script:
        return {"error": "script must not be empty."}
    try:
        result = _current_driver.execute_script(script)
        logger.debug("evaluate_js: result=%r", str(result)[:120])
        return {"result": result}
    except WebDriverException as exc:
        logger.error("evaluate_js: failed  %s", exc)
        return {"error": f"JS evaluation failed: {exc}"}


def get_console_messages() -> dict:
    """Return browser JavaScript console messages (errors, warnings, info logs).
    Returns empty list with a warning if browser logging was not enabled at driver startup."""
    if _current_driver is None:
        return {"error": "No active browser session — call get_snapshot(driver) first."}
    try:
        raw = _current_driver.get_log("browser")
        messages = [
            {"level": e.get("level", ""), "message": e.get("message", ""),
             "timestamp": e.get("timestamp", 0)}
            for e in (raw or [])
        ]
        logger.debug("get_console_messages: %d message(s)", len(messages))
        return {"messages": messages}
    except WebDriverException as exc:
        logger.warning("get_console_messages: unavailable — %s", exc)
        return {"messages": [], "warning": "Browser logging may not be enabled at driver startup."}


def get_network_requests() -> dict:
    """Return resource timing data for all network requests (XHR, fetch, scripts, images).
    Uses the browser Performance API — no proxy or CDP needed. Does not include headers or bodies."""
    if _current_driver is None:
        return {"error": "No active browser session — call get_snapshot(driver) first."}
    try:
        entries = _current_driver.execute_script(
            "return (window.performance.getEntriesByType('resource') || []).map(e => "
            "({name: e.name, duration_ms: Math.round(e.duration), type: e.initiatorType}));"
        ) or []
        logger.debug("get_network_requests: %d resource(s)", len(entries))
        return {"requests": entries}
    except WebDriverException as exc:
        logger.error("get_network_requests: failed  %s", exc)
        return {"error": f"Network request read failed: {exc}"}


# ── Crawl-specific tool: exploration form filler ──────────────────────────────
#
# The crawl agent must never use real, BRD-sourced, or domain-specific data to
# fill forms. Doing so causes hallucination (the LLM invents realistic values
# from training data instead of the actual application domain). This tool fills
# every visible form field with the minimum synthetic value that satisfies
# HTML5 client-side validation so Submit can be clicked — nothing more.

_EXPLORATION_VALUES: dict[str, str] = {
    # Maps HTML input type to a minimal synthetic value that passes HTML5
    # client-side validation without using any real or domain-specific data.
    "text":     "x",
    "search":   "x",
    "email":    "a@b.com",
    "number":   "1",
    "tel":      "0000000000",
    "url":      "https://example.com",
    "time":     "00:00",
}

# Input types we must never touch during exploration.
_SKIP_INPUT_TYPES: frozenset = frozenset({
    "password", "hidden", "file", "submit", "button", "reset", "image",
})


def _get_synthetic_value(input_type: str, tag: str):
    """
    Return the minimal synthetic value for a given HTML input type / tag name,
    or None if the element should be skipped entirely.

    This is a pure function with no Selenium calls or side effects. Keeping it
    separate makes the value-selection logic unit-testable without a browser.

    Date/time types are computed from today so the value is always valid.

    Args:
        input_type: Lower-cased HTML input type attribute (e.g. 'text', 'email').
        tag:        Lower-cased HTML tag name (e.g. 'input', 'textarea', 'select').

    Returns:
        A string to type into the field, a sentinel string for special handling
        ('__select_first_option__', '__check__', '__radio__'), or None to skip.
    """
    if tag == "select":
        return "__select_first_option__"
    if tag == "textarea":
        return "x"
    if input_type in _SKIP_INPUT_TYPES:
        return None
    if input_type == "checkbox":
        return "__check__"
    if input_type == "radio":
        return "__radio__"
    today = datetime.date.today()
    if input_type == "date":
        return today.isoformat()              # YYYY-MM-DD
    if input_type == "datetime-local":
        return today.isoformat() + "T00:00"  # YYYY-MM-DDTHH:MM
    if input_type == "month":
        return today.strftime("%Y-%m")        # YYYY-MM
    if input_type == "week":
        return today.strftime("%Y-W%W")       # YYYY-Www
    return _EXPLORATION_VALUES.get(input_type, "x")


def fill_form_for_exploration(form_selector: str = None) -> dict:
    """
    Fill all visible form inputs with minimal synthetic values derived solely
    from each field's HTML attributes (type, tag). No real, BRD-sourced, or
    domain-specific data is ever used — this prevents the crawl agent from
    hallucinating realistic field values.

    The sole purpose is to satisfy client-side HTML5 validation so that a
    Submit button can be clicked, allowing the crawl agent to observe what
    the application does next (dialogs, redirects, validation errors) without
    needing any knowledge of the application's domain.

    Performance: visible/enabled filtering is done with a single JavaScript
    call instead of one Selenium round-trip per element. This reduces N
    network calls to 1 for the initial query.

    Password, hidden, file, submit, button, reset, and image inputs are
    always skipped.

    For radio/checkbox inputs covered by custom UI overlays (e.g. jQuery UI),
    a JavaScript click fallback is used when a direct click is intercepted.

    Args:
        form_selector: Optional CSS selector scoping the fill to a specific
                       form (e.g. 'form#escalation-form'). When None, all
                       visible fillable inputs on the page are targeted.

    Returns:
        {"ok": True, "filled": [...], "skipped": [...]} where each entry
        names the field and the value used (or the skip reason), or
        {"error": "..."} on a DOM-level failure.
    """
    if _current_driver is None:
        return {"error": "No active browser session — call get_snapshot(driver) first."}

    from selenium.webdriver.support.select import Select

    # Single JS round-trip: return only visible, enabled, non-readonly elements.
    # This replaces N individual is_displayed() + is_enabled() Selenium calls
    # (one per DOM element) with one execute_script call that filters in the
    # browser process itself.
    _JS_VISIBLE_INPUTS = (
        "var scope = arguments[0];"
        "return Array.from(scope.querySelectorAll('input, textarea, select'))"
        ".filter(function(el) {"
        "  if (el.disabled || el.readOnly === true) return false;"
        "  var s = window.getComputedStyle(el);"
        "  if (s.display === 'none' || s.visibility === 'hidden' || s.opacity === '0') return false;"
        "  if (el.offsetWidth === 0 && el.offsetHeight === 0) return false;"
        "  return true;"
        "});"
    )

    try:
        if form_selector:
            scope_el = _current_driver.find_element(By.CSS_SELECTOR, form_selector)
        else:
            scope_el = _current_driver.find_element(By.TAG_NAME, "body")
        elements = _current_driver.execute_script(_JS_VISIBLE_INPUTS, scope_el) or []
    except WebDriverException as exc:
        logger.error("fill_form_for_exploration: JS pre-filter failed — %s", exc)
        return {"error": "Cannot query form fields: %s" % exc}

    logger.debug(
        "fill_form_for_exploration: JS pre-filter returned %d visible elements", len(elements)
    )

    filled: list = []
    skipped: list = []

    for el in elements:
        label = "unknown"  # initialise before attribute reads so except blocks can reference it
        try:
            tag = (el.tag_name or "").lower()
            input_type = (el.get_attribute("type") or "text").lower()
            label = (
                el.get_attribute("aria-label")
                or el.get_attribute("placeholder")
                or el.get_attribute("name")
                or "%s[type=%s]" % (tag, input_type)
            )

            synthetic = _get_synthetic_value(input_type, tag)

            if synthetic is None:
                skipped.append({"field": label, "reason": "type '%s' skipped by policy" % input_type})
                logger.debug(
                    "fill_form_for_exploration: skipped field=%r type=%s (policy)",
                    label, input_type,
                )
                continue

            if synthetic == "__select_first_option__":
                sel_widget = Select(el)
                first_opt = next(
                    (opt for opt in sel_widget.options
                     if (opt.get_attribute("value") or "").strip()),
                    None,
                )
                if first_opt:
                    sel_widget.select_by_value(first_opt.get_attribute("value"))
                    filled.append({"field": label, "value": first_opt.text})
                    logger.debug(
                        "fill_form_for_exploration: select field=%r option=%r",
                        label, first_opt.text,
                    )
                else:
                    skipped.append({"field": label, "reason": "select has no non-empty options"})
                continue

            if synthetic == "__check__":
                if not el.is_selected():
                    try:
                        el.click()
                    except ElementClickInterceptedException:
                        # Custom UI overlay (e.g. jQuery UI) is covering the real input.
                        # JS click fires directly on the element, bypassing the overlay.
                        _current_driver.execute_script("arguments[0].click()", el)
                        logger.debug(
                            "fill_form_for_exploration: JS click fallback for checkbox field=%r",
                            label,
                        )
                filled.append({"field": label, "value": "checked"})
                logger.debug("fill_form_for_exploration: checkbox field=%r checked", label)
                continue

            if synthetic == "__radio__":
                if not el.is_selected():
                    try:
                        el.click()
                    except ElementClickInterceptedException:
                        _current_driver.execute_script("arguments[0].click()", el)
                        logger.debug(
                            "fill_form_for_exploration: JS click fallback for radio field=%r",
                            label,
                        )
                filled.append({"field": label, "value": "selected"})
                logger.debug("fill_form_for_exploration: radio field=%r selected", label)
                continue

            # Text-like input — Ctrl+A to clear (React-safe), then send synthetic value.
            el.send_keys(Keys.CONTROL + "a")
            el.send_keys(synthetic)
            filled.append({"field": label, "value": synthetic})
            logger.debug(
                "fill_form_for_exploration: filled field=%r value=%r", label, synthetic
            )

        except StaleElementReferenceException:
            skipped.append({"field": label, "reason": "element became stale — page re-rendered"})
        except ElementNotInteractableException:
            skipped.append({"field": label, "reason": "not interactable"})
        except WebDriverException as exc:
            skipped.append({"field": label, "reason": str(exc)})
            logger.warning("fill_form_for_exploration: field error — %s", exc)

    logger.info(
        "fill_form_for_exploration: filled=%d skipped=%d form_selector=%r",
        len(filled), len(skipped), form_selector,
    )
    return {"ok": True, "filled": filled, "skipped": skipped}
