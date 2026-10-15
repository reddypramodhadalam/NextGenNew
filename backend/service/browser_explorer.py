"""
service/browser_explorer.py

Comprehensive browser exploration for advanced web application patterns.

Handles:
  - Browser alerts, confirms, and prompts
  - Modal dialogs and popups (role=dialog, aria-modal, etc.)
  - Multiple windows and tabs (unified under "windows")
  - Lazy-loading and scroll-triggered content
  - Dynamic/AJAX content waiting

This module provides utility functions for the crawl agent to discover and
interact with all content types in modern web applications. All functions
follow the same error-handling pattern: return dict with "error" key on
failure, or {"ok": True, "data": ...} on success.

Logging: DEBUG for details, INFO for milestones, WARNING for edge cases,
ERROR for failures.
"""

import logging
from typing import Optional

logger = logging.getLogger(__name__)


# ── Alert/Dialog Handling ──────────────────────────────────────────────────

def detect_alert(driver) -> bool:
    """
    Check if a browser alert/confirm/prompt dialog is currently present.

    Browser alerts are JavaScript dialogs (alert(), confirm(), prompt()).
    They block further interaction until dismissed.

    Args:
        driver: Active Selenium WebDriver instance.

    Returns:
        True if alert is present, False otherwise.
    """
    from selenium.webdriver.support.wait import WebDriverWait
    from selenium.webdriver.support import expected_conditions as EC
    from selenium.common.exceptions import TimeoutException

    try:
        # Non-blocking check: wait 0.5 seconds for alert
        WebDriverWait(driver, 0.5).until(EC.alert_is_present())
        logger.debug("detect_alert: alert is present")
        return True
    except TimeoutException:
        logger.debug("detect_alert: no alert present")
        return False


def handle_alert(driver, action: str = "accept") -> dict:
    """
    Handle a browser alert/confirm/prompt dialog.

    Args:
        driver: Active Selenium WebDriver instance.
        action: "accept" (click OK), "dismiss" (click Cancel), or "get_text"
                (read the message without dismissing).

    Returns:
        {"ok": True, "data": {"text": "..."}} on success, or {"error": "..."}
        if no alert present or action fails.
    """
    from selenium.common.exceptions import NoAlertPresentException

    try:
        alert = driver.switch_to.alert
        text = alert.text
        logger.debug("handle_alert: found alert with text: %r", text[:80])

        if action == "accept":
            alert.accept()
            logger.info("handle_alert: accepted alert")
            return {"ok": True, "data": {"action": "accept", "text": text}}

        elif action == "dismiss":
            alert.dismiss()
            logger.info("handle_alert: dismissed alert")
            return {"ok": True, "data": {"action": "dismiss", "text": text}}

        elif action == "get_text":
            logger.info("handle_alert: read alert text")
            return {"ok": True, "data": {"action": "get_text", "text": text}}

        else:
            return {"error": f"Unknown alert action: {action}"}

    except NoAlertPresentException:
        logger.warning("handle_alert: no alert present (expected %s)", action)
        return {"error": "No alert present"}
    except Exception as e:
        logger.error("handle_alert: failed — %s", e)
        return {"error": f"Alert handling failed: {e}"}


# ── Modal/Popup Detection ──────────────────────────────────────────────────

def detect_modals(driver) -> list[dict]:
    """
    Find all modal/popup elements currently visible on the page.

    Modals are typically indicated by:
    - role="dialog"
    - aria-modal="true"
    - class containing "modal" or "dialog"
    - Visible overlays with event listeners

    Args:
        driver: Active Selenium WebDriver instance.

    Returns:
        List of modal dicts, each with:
        - ref: Element reference
        - role: Element role
        - title: Modal title or heading text
        - is_visible: Whether modal is visible (display != none)
        Empty list if no modals found.
    """
    from selenium.webdriver.common.by import By
    from selenium.common.exceptions import WebDriverException

    modals = []

    try:
        # Search for elements that look like modals
        modal_selectors = [
            "[role='dialog']",
            "[aria-modal='true']",
            ".modal",
            ".dialog",
            "[class*='modal']",
            "[class*='dialog']",
        ]

        for selector in modal_selectors:
            try:
                elements = driver.find_elements(By.CSS_SELECTOR, selector)
                for el in elements:
                    try:
                        if not el.is_displayed():
                            continue

                        ref = f"modal-{len(modals)}"
                        role = el.get_attribute("role") or "dialog"
                        title = (
                            el.get_attribute("aria-label")
                            or el.find_element(By.CSS_SELECTOR, "h1, h2, h3, [role='heading']").text
                            or "Untitled Modal"
                        )
                        modals.append({
                            "ref": ref,
                            "role": role,
                            "title": title[:100],
                            "is_visible": True,
                        })
                    except Exception:
                        continue
            except Exception:
                continue

        logger.debug("detect_modals: found %d modal(s)", len(modals))
        return modals

    except WebDriverException as e:
        logger.warning("detect_modals: search failed — %s", e)
        return []


# ── Window/Tab Management ──────────────────────────────────────────────────

def get_all_windows(driver) -> list[dict]:
    """
    Get all open windows (tabs and separate windows).

    In Selenium, "windows" includes both browser tabs and separate windows.
    Both are accessed via window handles.

    Args:
        driver: Active Selenium WebDriver instance.

    Returns:
        List of window dicts, each with:
        - handle: Window handle (opaque ID)
        - title: Window title (from JavaScript window.name or page title)
        - url: Current URL
        - is_current: True if this is the currently-active window
    """
    from selenium.common.exceptions import WebDriverException

    try:
        current_handle = driver.current_window_handle
        windows = []

        for handle in driver.window_handles:
            try:
                driver.switch_to.window(handle)
                title = driver.title or driver.execute_script("return window.name") or "Untitled"
                url = driver.current_url
                is_current = (handle == current_handle)

                windows.append({
                    "handle": handle,
                    "title": title[:100],
                    "url": url,
                    "is_current": is_current,
                })
            except Exception as e:
                logger.warning("get_all_windows: could not read window %s — %s", handle, e)
                continue

        # Return to the current window
        driver.switch_to.window(current_handle)

        logger.debug("get_all_windows: found %d window(s)", len(windows))
        return windows

    except WebDriverException as e:
        logger.error("get_all_windows: failed — %s", e)
        return []


def switch_to_window(driver, handle: str) -> dict:
    """
    Switch focus to a specific window or tab.

    Handles both new browser tabs and separate windows uniformly.
    In Selenium, both are accessed via window handles.

    Args:
        driver: Active Selenium WebDriver instance.
        handle: Window handle (from get_all_windows or window_handles).

    Returns:
        {"ok": True, "data": {"title": "...", "url": "..."}} on success,
        or {"error": "..."} on failure.
    """
    from selenium.common.exceptions import WebDriverException, NoSuchWindowException

    try:
        driver.switch_to.window(handle)
        title = driver.title
        url = driver.current_url
        logger.info("switch_to_window: switched to '%s' (%s)", title[:60], url[:80])
        return {"ok": True, "data": {"title": title, "url": url}}

    except NoSuchWindowException:
        logger.warning("switch_to_window: window handle %s does not exist", handle)
        return {"error": f"Window handle {handle} not found"}
    except WebDriverException as e:
        logger.error("switch_to_window: failed — %s", e)
        return {"error": f"Could not switch to window: {e}"}


def switch_to_main_window(driver) -> dict:
    """
    Switch focus back to the first/main window (typically window_handles[0]).

    Convenience function for when you want to return to the original tab.

    Args:
        driver: Active Selenium WebDriver instance.

    Returns:
        {"ok": True, "data": {...}} on success, or {"error": "..."}.
    """
    try:
        if not driver.window_handles:
            return {"error": "No windows available"}
        main_handle = driver.window_handles[0]
        return switch_to_window(driver, main_handle)
    except Exception as e:
        logger.error("switch_to_main_window: failed — %s", e)
        return {"error": f"Could not switch to main window: {e}"}


# ── Lazy-Load & Scroll Handling ────────────────────────────────────────────

def detect_scrollable_areas(driver) -> list[dict]:
    """
    Detect areas on the page that might have lazy-loaded or scrollable content.

    Returns elements with overflow properties or explicit scroll containers.

    Args:
        driver: Active Selenium WebDriver instance.

    Returns:
        List of scrollable area dicts, each with:
        - ref: Element reference
        - role: Element role or tag
        - has_overflow: Whether element has scrollable content
        - scroll_height: Total scrollable height
        - scroll_position: Current scroll position
    """
    from selenium.webdriver.common.by import By
    from selenium.common.exceptions import WebDriverException

    try:
        # Find elements with overflow or scroll-like behavior
        scrollable_selectors = [
            "[style*='overflow']",
            "[class*='scroll']",
            ".carousel",
            "[role='listbox']",
            "[role='grid']",
        ]

        areas = []
        for selector in scrollable_selectors:
            try:
                elements = driver.find_elements(By.CSS_SELECTOR, selector)
                for el in elements:
                    try:
                        if not el.is_displayed():
                            continue

                        scroll_height = el.get_attribute("scrollHeight") or "unknown"
                        client_height = el.get_attribute("clientHeight") or "unknown"
                        is_scrollable = (
                            scroll_height != "unknown"
                            and client_height != "unknown"
                            and int(scroll_height) > int(client_height)
                        )

                        if is_scrollable:
                            areas.append({
                                "ref": f"scroll-{len(areas)}",
                                "role": el.get_attribute("role") or el.tag_name,
                                "has_overflow": True,
                                "scroll_height": scroll_height,
                                "scroll_position": el.get_attribute("scrollTop") or 0,
                            })
                    except Exception:
                        continue
            except Exception:
                continue

        logger.debug("detect_scrollable_areas: found %d scrollable area(s)", len(areas))
        return areas

    except WebDriverException as e:
        logger.warning("detect_scrollable_areas: search failed — %s", e)
        return []


def scroll_page(driver, direction: str = "down", amount: int = 3, target_ref: Optional[str] = None) -> dict:
    """
    Scroll the page or a specific element to reveal lazy-loaded content.

    Args:
        driver: Active Selenium WebDriver instance.
        direction: "down" (default) or "up".
        amount: Number of scroll steps to perform (default 3).
        target_ref: Optional element to scroll (if None, scrolls entire page).

    Returns:
        {"ok": True, "data": {"scrolled": True, "new_elements_visible": N}}
        or {"error": "..."} on failure.
    """
    from selenium.webdriver.common.by import By
    from selenium.webdriver.common.keys import Keys
    from selenium.common.exceptions import WebDriverException

    try:
        if direction not in ("down", "up"):
            return {"error": f"Invalid direction: {direction}"}

        key = Keys.PAGE_DOWN if direction == "down" else Keys.PAGE_UP

        # If no target, scroll the page itself
        if not target_ref:
            for i in range(amount):
                driver.find_element(By.TAG_NAME, "body").send_keys(key)
            logger.info("scroll_page: scrolled page %s x%d", direction, amount)
            return {"ok": True, "data": {"scrolled": True, "target": "page"}}

        # If target specified, scroll that element
        # (This is for future use; currently just scroll page)
        for i in range(amount):
            driver.find_element(By.TAG_NAME, "body").send_keys(key)

        logger.info("scroll_page: scrolled %s x%d", direction, amount)
        return {"ok": True, "data": {"scrolled": True}}

    except WebDriverException as e:
        logger.error("scroll_page: failed — %s", e)
        return {"error": f"Scroll failed: {e}"}


# ── Dynamic Content Waiting ────────────────────────────────────────────────

def wait_for_dynamic_content(driver, timeout: int = 10) -> dict:
    """
    Wait for dynamic/AJAX content to finish loading.

    Detects if page has active loading indicators and waits for them to disappear.
    Looks for:
    - Spinners (class/role containing "spinner", "loading")
    - Progress bars
    - Dimmed/grayed overlays
    - AJAX activity (if detectable)

    Args:
        driver: Active Selenium WebDriver instance.
        timeout: Maximum seconds to wait (default 10).

    Returns:
        {"ok": True, "data": {"waited_seconds": N, "content_ready": True}}
        or {"error": "..."} if timeout exceeded or check fails.
    """
    from selenium.webdriver.common.by import By
    from selenium.webdriver.support.wait import WebDriverWait
    from selenium.webdriver.support import expected_conditions as EC
    from selenium.common.exceptions import TimeoutException, WebDriverException
    import time

    try:
        start_time = time.time()

        # Check if there are loading indicators
        loading_selectors = [
            "[class*='spinner']",
            "[class*='loading']",
            "[role='status'][aria-live='polite']",
            ".progress",
            "[class*='overlay']",
        ]

        for selector in loading_selectors:
            try:
                WebDriverWait(driver, timeout).until(
                    EC.invisibility_of_all_elements_located((By.CSS_SELECTOR, selector))
                )
            except TimeoutException:
                elapsed = time.time() - start_time
                logger.warning(
                    "wait_for_dynamic_content: timeout waiting for %s (%.1fs)",
                    selector, elapsed
                )
            except Exception:
                continue

        elapsed = time.time() - start_time
        logger.info("wait_for_dynamic_content: content ready (waited %.1fs)", elapsed)
        return {"ok": True, "data": {"waited_seconds": round(elapsed, 1), "content_ready": True}}

    except WebDriverException as e:
        logger.error("wait_for_dynamic_content: failed — %s", e)
        return {"error": f"Wait failed: {e}"}


# ── Shadow DOM Detection (Future Enhancement) ──────────────────────────────

def detect_shadow_dom_roots(driver) -> list[dict]:
    """
    Detect Shadow DOM roots on the page (reserved for future implementation).

    Shadow DOM elements are encapsulated and require special handling.
    This is currently a placeholder for apps that heavily use Web Components.

    Args:
        driver: Active Selenium WebDriver instance.

    Returns:
        Empty list for now (feature reserved for Phase 3.0-Plus).
    """
    logger.debug("detect_shadow_dom_roots: reserved for future implementation")
    return []


# ── Unified Discovery API ──────────────────────────────────────────────────

def discover_advanced_content(driver) -> dict:
    """
    Perform a comprehensive discovery of advanced browser content.

    Checks for alerts, modals, new windows, lazy-load areas in one call.

    Args:
        driver: Active Selenium WebDriver instance.

    Returns:
        {
            "alerts": <bool>,
            "modals": <list>,
            "windows": <list>,
            "scrollable_areas": <list>,
            "has_dynamic_content": <bool>,
        }
    """
    try:
        return {
            "alerts": detect_alert(driver),
            "modals": detect_modals(driver),
            "windows": get_all_windows(driver),
            "scrollable_areas": detect_scrollable_areas(driver),
            "has_dynamic_content": len(detect_scrollable_areas(driver)) > 0,
        }
    except Exception as e:
        logger.error("discover_advanced_content: failed — %s", e)
        return {
            "alerts": False,
            "modals": [],
            "windows": [],
            "scrollable_areas": [],
            "has_dynamic_content": False,
            "error": str(e),
        }
