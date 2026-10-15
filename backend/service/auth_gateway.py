"""
service/auth_gateway.py

SSO / Authentication Gateway

All SSO-related logic lives here. Nothing else should import from this
module except survey_runner, generation_runner, execution_runner, and
app_map_crawler.

Responsibilities:
  - Determine whether an application requires an SSO precheck.
  - Authenticate a live Selenium browser through Azure Entra ID (pre-crawl).
  - Build the SSO login preamble injected into generated scripts.
  - Build TEST_* environment variables from stored app test data.
  - Augment survey proposals with authentication-focused scenarios.
  - Run the precheck and return a structured result.
"""

import logging
import os
import re
import time
from typing import Optional

from core.settings import settings
from test_data.loader import load_test_data
from core.db.queries import users as users_db
from core.db.queries import app_credentials as app_credentials_db

logger = logging.getLogger(__name__)


# ── Per-user application credentials (Stage 2b) ───────────────────────────────
# A run signs in as WHOEVER triggered it, using the credential that user saved
# for this application (api/routes/app_credentials.py). These "triggered_by"
# values are not real users, so they never map to a stored credential and must
# fall straight through to the shared, file-based test data as before.
_NON_USER_TRIGGERS = {"", "anonymous", "scheduler", "manual", "chat", "ui", "system"}


def _db_credential(application: dict, triggered_by: Optional[str]) -> Optional[dict]:
    """Return the initiator's OWN stored credential for this application, or
    None to fall back to the shared file-based test data.

    Resolves the triggering email to an EXISTING user (never creates one on a
    run) and reads only that user's row via the single door
    app_credentials.get_credential(application_id, user_id). Any failure here
    is deliberately swallowed to None so a credential-store hiccup degrades to
    the existing file-based behaviour rather than breaking a run.
    """
    if not triggered_by:
        return None
    email = str(triggered_by).strip()
    if not email or email.lower() in _NON_USER_TRIGGERS:
        return None
    app_id = application.get("id") if application else None
    if not app_id:
        return None
    try:
        user = users_db.get_by_email(email)
        if not user:
            return None
        return app_credentials_db.get_credential(int(app_id), user["id"])
    except Exception as exc:  # pragma: no cover - defensive fallback
        logger.debug(
            "auth_gateway: no DB credential for app=%s trigger=%s (%s)",
            app_id, email, exc,
        )
        return None


# ── Azure Entra ID (Microsoft Entra) — stable public selectors ────────────────
# Microsoft has not changed these selectors since at least 2019.
# If they ever change, update these four constants only.
_ENTRA_REDIRECT_DOMAIN   = "login.microsoftonline.com"
_ENTRA_USERNAME_SELECTOR = "input[name='loginfmt']"
_ENTRA_NEXT_BUTTON_ID    = "idSIButton9"
_ENTRA_PASSWORD_SELECTOR = "input[name='passwd']"
_ENTRA_SUBMIT_BUTTON_ID  = "idSIButton9"
# Wait time (seconds) for each Entra page transition
_ENTRA_STEP_TIMEOUT      = int(os.environ.get("SSO_ENTRA_STEP_TIMEOUT", "20"))

# ── Pre-Entra interstitial ("Sign in with Microsoft" style landing page) ──────
# Some applications do NOT redirect straight to login.microsoftonline.com —
# they first show their OWN branded landing page with a single sign-in
# button (e.g. "Sign in with Microsoft"), and only move to the real Entra
# page once that button is clicked (confirmed live: Netra's /login page).
# Detection is deliberately GENERIC (a broad text pattern on a clickable
# element), not a per-application flag or an exact label match — most
# applications have no such interstitial at all and go straight to Entra,
# which remains the default, unaffected path; this only engages when a
# matching button/link is actually found on the live page within a short
# bounded wait, so it can never block or misfire on an app that has none.
_SSO_INTERSTITIAL_TEXT_RE = re.compile(
    r"sign[ -]?in\s+with\b|continue\s+with\s+(?:microsoft|azure|entra|sso)\b|"
    r"\blog[ -]?in\s+with\b",
    re.I,
)
# How long (seconds) to fluent-wait for an interstitial button to render
# before concluding there isn't one. NOTE: this cost is paid ONLY by an
# application that has NEITHER a direct Entra redirect NOR a CAM/generic
# login form already present (see _detect_auth_state's ordering, and
# build_sso_preamble's _already_past_interstitial check) — i.e. one that
# is already authenticated via Kerberos or needs no login at all. A
# direct-to-Entra app (the common case) never reaches this wait and pays
# zero added cost. Default matches _ENTRA_STEP_TIMEOUT so a slow-
# rendering SPA landing page gets the same grace period Entra's own
# pages get.
_SSO_INTERSTITIAL_TIMEOUT = int(os.environ.get("SSO_INTERSTITIAL_TIMEOUT", str(_ENTRA_STEP_TIMEOUT)))


# ── Environment helpers ────────────────────────────────────────────────────────

def _csv_env(name: str) -> set[str]:
    raw = (os.environ.get(name) or "").strip()
    if not raw:
        return set()
    return {x.strip().lower() for x in raw.split(",") if x.strip()}


def _bool_env(name: str, default: bool = False) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return str(raw).strip().lower() in {"1", "true", "yes", "y", "on"}


def _app_config_entry(application: dict) -> Optional[dict]:
    app_name = (application.get("name") or "").strip().lower()
    app_configs = (settings.applications_config or {}).get("applications", {})
    for _, cfg in app_configs.items():
        display = str(cfg.get("display_name") or "").strip().lower()
        if display and display == app_name:
            return cfg
    return None


# ── SSO requirement detection ──────────────────────────────────────────────────

def app_requires_sso_gateway(application: dict) -> bool:
    """
    Return True if this application needs an SSO precheck before
    survey, generation, or execution.

    Checks (in order):
      1. The database flag requires_sso_gateway.
      2. The SSO_GATEWAY_ALL_APPS env var (applies to every app).
      3. SSO_REQUIRED_APPLICATIONS CSV (matches by name or id).
      4. The sso_gateway_script entry in config/applications.yaml.
    """
    if bool(application.get("requires_sso_gateway")):
        return True

    if _bool_env("SSO_GATEWAY_ALL_APPS", default=False):
        return True

    required_apps = _csv_env("SSO_REQUIRED_APPLICATIONS")
    app_name = (application.get("name") or "").strip().lower()
    app_id = str(application.get("id") or "").strip().lower()
    if app_name in required_apps or app_id in required_apps:
        return True

    cfg = _app_config_entry(application)
    if cfg and isinstance(cfg.get("requires_sso_gateway"), bool):
        return cfg.get("requires_sso_gateway") is True

    return False


# ── SSO authentication mode ───────────────────────────────────────────────────
# Each SSO application authenticates in one of two modes:
#
#   "credential" (DEFAULT) — the browser types a stored username/password into
#       the identity provider (Azure Entra, Cognos CAM, or a generic form).
#       This is exactly what every existing application does today, and it is
#       assumed whenever nothing is configured, so NO existing app changes
#       behaviour.
#
#   "integrated" — the browser authenticates SILENTLY using the host's own
#       Windows/Kerberos logon or Entra Primary Refresh Token (the same way a
#       domain-joined workstation opens JDE with no prompt). No stored
#       credentials are required or used for these applications; the runner
#       host's logged-in identity carries the browser in.
_VALID_SSO_MODES = {"credential", "integrated"}
_DEFAULT_SSO_MODE = "credential"


def app_sso_mode(application: dict) -> str:
    """
    Return the SSO authentication mode for this application.

    Resolution order (first match wins), deliberately mirroring
    app_requires_sso_gateway so configuration lives in the same places:
      1. The application row's own "sso_mode" field (DB-backed).
      2. The "sso_mode" entry in config/applications.yaml.
      3. The SSO_INTEGRATED_APPLICATIONS CSV (matches by name or id) — an
         env-level override, consistent with SSO_REQUIRED_APPLICATIONS.

    Falls back to "credential" (the existing, unchanged behaviour) whenever
    nothing is configured, the application is not a dict, or the configured
    value is not a recognised mode. This guarantees every existing
    application keeps its current credential-typed login untouched.
    """
    if not isinstance(application, dict):
        return _DEFAULT_SSO_MODE

    raw = str(application.get("sso_mode") or "").strip().lower()
    if raw in _VALID_SSO_MODES:
        return raw

    cfg = _app_config_entry(application)
    if cfg:
        cfg_mode = str(cfg.get("sso_mode") or "").strip().lower()
        if cfg_mode in _VALID_SSO_MODES:
            return cfg_mode

    integrated_apps = _csv_env("SSO_INTEGRATED_APPLICATIONS")
    app_name = (application.get("name") or "").strip().lower()
    app_id = str(application.get("id") or "").strip().lower()
    if app_name in integrated_apps or app_id in integrated_apps:
        return "integrated"

    return _DEFAULT_SSO_MODE


def app_uses_integrated_sso(application: dict) -> bool:
    """Convenience wrapper: True when this app authenticates silently
    (Windows/Kerberos/PRT) and must NOT require stored credentials."""
    return app_sso_mode(application) == "integrated"


def phase_is_gated(phase: str) -> bool:
    """
    Return True if the SSO precheck applies to this pipeline phase.

    Default: survey, generate, and execute are all gated.
    Override with SSO_PRECHECK_PHASES=survey,generate (comma-separated).
    """
    phases = _csv_env("SSO_PRECHECK_PHASES")
    if not phases:
        phases = {"survey", "generate", "execute"}
    return phase.strip().lower() in phases


# ── Script path resolution (DEPRECATED — kept for backwards compat) ───────────
# Per-app gateway scripts are replaced by authenticate_browser().
# These functions remain so existing configurations with sso_gateway_script
# still work during the transition period.

def _find_existing_script_path(configured: str) -> Optional[str]:
    from pathlib import Path
    configured = (configured or "").strip()
    if not configured:
        return None
    p = Path(configured)
    if p.is_absolute() and p.exists():
        return str(p)
    candidates = [
        Path.cwd() / configured,
        Path(__file__).resolve().parents[2] / configured,
        Path(__file__).resolve().parents[1] / configured,
    ]
    for c in candidates:
        if c.exists():
            return str(c)
    return None


def resolve_gateway_script_path(application: dict) -> Optional[str]:
    """Resolve per-app SSO gateway script. Deprecated — use authenticate_browser()."""
    app_id = application.get("id")
    cfg = _app_config_entry(application)
    if cfg and cfg.get("sso_gateway_script"):
        p = _find_existing_script_path(str(cfg.get("sso_gateway_script")))
        if p:
            return p
    if app_id:
        env_key = f"SSO_GATEWAY_SCRIPT_APP_{app_id}"
        p = _find_existing_script_path(os.environ.get(env_key, ""))
        if p:
            return p
    return None


# ── Azure Entra browser authentication ────────────────────────────────────────

# ── Auth state detection ───────────────────────────────────────────────────────

def _detect_auth_state(driver) -> str:
    """
    Inspect the current page to decide which authentication path applies.

    Called after driver.get(app_url) so we know where the browser actually
    landed before committing to a specific login flow.

    Returns one of:
        'authenticated'    — no login form visible; Kerberos or a prior
                             session handled auth silently.
        'entra'            — browser is on login.microsoftonline.com (Azure
                             Entra OIDC / SAML redirect).
        'cam'              — IBM Cognos Access Manager login form
                             (CAMUsername).
        'sso_interstitial' — the app's OWN branded landing page is shown,
                             with a single sign-in button (e.g. "Sign in
                             with Microsoft") and no redirect/password
                             field yet — see _find_interstitial_button.
        'generic'          — a login form is present but the type is
                             unknown.
        'unknown'          — cannot determine state (browser error, blank
                             page).
    """
    try:
        from selenium.webdriver.common.by import By
        current_url = (driver.current_url or "").lower()

        if _ENTRA_REDIRECT_DOMAIN in current_url:
            logger.debug("auth_gateway: _detect_auth_state → entra  url=%s", current_url)
            return "entra"

        # IBM Cognos CAM form — unique field names not found elsewhere.
        cam_fields = driver.find_elements(
            By.CSS_SELECTOR,
            "input[id='CAMUsername'], input[name='CAMUsername'], input[name='camUsername']",
        )
        if cam_fields:
            logger.debug("auth_gateway: _detect_auth_state → cam  url=%s", current_url)
            return "cam"

        # Generic login form: any visible password field.
        pass_fields = [
            f for f in driver.find_elements(By.CSS_SELECTOR, "input[type='password']")
            if f.is_displayed()
        ]
        if pass_fields:
            logger.debug("auth_gateway: _detect_auth_state → generic  url=%s", current_url)
            return "generic"

        # Pre-Entra interstitial: the app's own branded landing page with a
        # single "Sign in with Microsoft"-style button, not yet redirected
        # and with no password field of its own — see the constant's
        # docstring above. Checked LAST, after every other state has ruled
        # itself out, so it can never misclassify a page that already has
        # a real login form or is already on Entra.
        if _find_interstitial_button(driver) is not None:
            logger.debug("auth_gateway: _detect_auth_state → sso_interstitial  url=%s", current_url)
            return "sso_interstitial"

        logger.debug("auth_gateway: _detect_auth_state → authenticated  url=%s", current_url)
        return "authenticated"

    except Exception as exc:
        logger.warning("auth_gateway: _detect_auth_state error: %s", exc)
        return "unknown"


def _find_interstitial_button(driver):
    """
    Return the first visible, enabled clickable element whose text matches
    the generic "sign in with ..." pattern, or None if nothing matches.

    Deliberately broad and label-agnostic (see _SSO_INTERSTITIAL_TEXT_RE) —
    this is NOT hardcoded to "Sign in with Microsoft" specifically, so it
    also matches an app's own differently-worded equivalent without any
    per-application configuration. Scans buttons, links and role=button
    elements; most applications will simply have none of these and this
    returns None immediately.
    """
    from selenium.webdriver.common.by import By
    try:
        candidates = driver.find_elements(
            By.CSS_SELECTOR, "button, a, [role='button'], input[type='submit'], input[type='button']",
        )
    except Exception:
        return None
    for element in candidates:
        try:
            if not element.is_displayed() or not element.is_enabled():
                continue
            text = (element.text or element.get_attribute("value") or "").strip()
            if text and _SSO_INTERSTITIAL_TEXT_RE.search(text):
                return element
        except Exception:
            continue
    return None


def _click_interstitial_and_proceed(driver, timeout: int) -> Optional[str]:
    """
    Click the detected interstitial sign-in button, then poll for the
    page to resolve to a DIFFERENT, known auth state.

    Deliberately does NOT call _detect_auth_state in this loop: that
    function's own interstitial check is itself a fluent wait of up to
    _SSO_INTERSTITIAL_TIMEOUT, and calling it repeatedly here could
    compound into a wait far longer than `timeout` if the page is slow to
    resolve. Instead this does its own cheap, instant checks each
    iteration (no nested waiting), polling at a fixed short interval and
    strictly bounded by `timeout` overall.

    Args:
        driver:  Live Selenium WebDriver, on the interstitial page.
        timeout: Maximum seconds to wait for the page to resolve after
                 the click.

    Returns:
        "entra" | "cam" | "generic" — the login form that appeared.
        "authenticated" — neither a login form nor the interstitial
            button is present any longer (likely a session/Kerberos-based
            IdP that completed silently after the click).
        "sso_interstitial" — the page never resolved within `timeout`;
            caller treats this as a failed click.
        None — the button could not be found or clicked at all.
    """
    from selenium.webdriver.common.by import By
    button = _find_interstitial_button(driver)
    if button is None:
        return None
    try:
        button.click()
    except Exception as exc:
        logger.warning("auth_gateway: could not click interstitial sign-in button: %s", exc)
        return None
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            current_url = (driver.current_url or "").lower()
            if _ENTRA_REDIRECT_DOMAIN in current_url:
                return "entra"
            if driver.find_elements(
                By.CSS_SELECTOR,
                "input[id='CAMUsername'], input[name='CAMUsername'], input[name='camUsername']",
            ):
                return "cam"
            if any(f.is_displayed() for f in driver.find_elements(By.CSS_SELECTOR, "input[type='password']")):
                return "generic"
            if _find_interstitial_button(driver) is None:
                # The button (or its container) is gone and none of the
                # known login forms appeared — most likely a session or
                # Kerberos-based identity provider that completed silently.
                return "authenticated"
        except Exception:
            pass
        time.sleep(0.3)
    return "sso_interstitial"


def _authenticate_cam(driver, credentials: dict) -> dict:
    """
    Fill an IBM Cognos Access Manager (CAMUsername / CAMPassword) login form.

    Used for Cognos Analytics instances that use the native CAM namespace
    rather than Azure Entra OIDC.

    Args:
        driver:      Live Selenium WebDriver, on the Cognos login page.
        credentials: {"username": ..., "password": ...}.

    Returns:
        {"passed": bool, "message": str}
    """
    from selenium.webdriver.common.by import By
    from selenium.webdriver.support.ui import WebDriverWait
    from selenium.webdriver.support import expected_conditions as EC
    from selenium.common.exceptions import TimeoutException, WebDriverException

    username = (credentials.get("username") or "").strip()
    password = (credentials.get("password") or "").strip()
    if not username or not password:
        return {
            "passed": False,
            "message": "CAM auth skipped: credentials missing.",
        }

    _cam_user_selectors = [
        "input[id='CAMUsername']",
        "input[name='CAMUsername']",
        "input[name='camUsername']",
    ]
    _cam_pass_selectors = [
        "input[id='CAMPassword']",
        "input[name='CAMPassword']",
        "input[name='camPassword']",
    ]

    try:
        wait = WebDriverWait(driver, _ENTRA_STEP_TIMEOUT)

        # Locate username field — try each selector in turn.
        user_field = None
        for sel in _cam_user_selectors:
            try:
                user_field = wait.until(EC.visibility_of_element_located((By.CSS_SELECTOR, sel)))
                break
            except TimeoutException:
                continue
        if not user_field:
            return {"passed": False, "message": "CAM auth: username field not found."}

        user_field.clear()
        user_field.send_keys(username)
        logger.info("auth_gateway: CAM username entered")

        # Locate password field.
        pass_field = None
        for sel in _cam_pass_selectors:
            try:
                pass_field = WebDriverWait(driver, 5).until(
                    EC.visibility_of_element_located((By.CSS_SELECTOR, sel))
                )
                break
            except TimeoutException:
                continue
        if not pass_field:
            return {"passed": False, "message": "CAM auth: password field not found."}

        pass_field.clear()
        pass_field.send_keys(password)
        logger.info("auth_gateway: CAM password entered")

        # Submit — look for a submit button.
        from selenium.webdriver.support import expected_conditions as _EC
        submitted = False
        for by, val in [
            (By.CSS_SELECTOR, "button[type='submit']"),
            (By.CSS_SELECTOR, "input[type='submit']"),
            (By.CSS_SELECTOR, "button"),
        ]:
            try:
                btn = WebDriverWait(driver, 5).until(_EC.element_to_be_clickable((by, val)))
                btn.click()
                submitted = True
                logger.info("auth_gateway: CAM submit clicked")
                break
            except Exception:
                continue

        if not submitted:
            return {"passed": False, "message": "CAM auth: no submit button found."}

        # Wait for the CAM login page to disappear.
        deadline = time.time() + _ENTRA_STEP_TIMEOUT
        while True:
            state = _detect_auth_state(driver)
            if state in ("authenticated", "unknown"):
                break
            if time.time() > deadline:
                return {"passed": False, "message": "CAM auth: timed out waiting for post-login page."}
            time.sleep(0.5)

        final_url = driver.current_url
        logger.info("auth_gateway: CAM authentication successful  final_url=%r", final_url)
        return {"passed": True, "message": f"CAM authentication successful. Redirected to: {final_url}"}

    except (TimeoutException, WebDriverException) as exc:
        logger.error("auth_gateway: CAM authentication error: %s", exc)
        return {"passed": False, "message": f"CAM authentication error: {exc}"}


def authenticate_driver(driver, credentials: dict, application: Optional[dict] = None) -> dict:
    """
    Authenticate a live Selenium WebDriver using whichever method the current
    page requires.

    This is the single entry-point for crawl and survey browser auth.
    It replaces the previous direct call to authenticate_browser() which
    assumed every app used Azure Entra OIDC — causing Cognos (CAM / Kerberos)
    and other non-Entra apps to time out waiting for input[name='loginfmt'].

    Detection order:
      1. Already authenticated (Kerberos handled it silently, or no auth
         needed) → return success immediately, no credentials consumed.
      2. Pre-Entra interstitial (the app's own "Sign in with Microsoft"
         style landing page) → click it, then re-dispatch on whatever
         state the page lands on next (entra/cam/generic/authenticated).
      3. Azure Entra OIDC page → use the Azure Entra flow.
      4. Cognos CAM page → use the CAM flow.
      5. Generic login form → log a warning; caller should fall back to
         live-page extraction or legacy script.
      6. Unknown state → return failure with context.

    Args:
        driver:      Live Selenium WebDriver, already on the app URL.
        credentials: {"username": ..., "password": ...}.  May be None for
                     Kerberos apps that need no explicit credentials.
        application: Optional application row dict — used only for logging.

    Returns:
        {"passed": bool, "message": str, "method": str}
    """
    app_label = (application or {}).get("name") or "app"
    state = _detect_auth_state(driver)
    logger.info(
        "auth_gateway: authenticate_driver  app=%r  state=%s  url=%s",
        app_label, state, driver.current_url,
    )

    if state == "authenticated":
        msg = f"Browser already authenticated (Kerberos or open session) for {app_label}."
        logger.info("auth_gateway: %s", msg)
        return {"passed": True, "method": "kerberos_or_session", "message": msg}

    if state == "sso_interstitial":
        logger.info(
            "auth_gateway: pre-Entra interstitial detected for %r — clicking through before the real login form",
            app_label,
        )
        new_state = _click_interstitial_and_proceed(driver, timeout=_ENTRA_STEP_TIMEOUT)
        if new_state is None:
            return {
                "passed": False,
                "message": f"Could not click the sign-in button for {app_label}.",
                "method": "sso_interstitial_failed",
            }
        if new_state == "sso_interstitial":
            return {
                "passed": False,
                "message": (
                    f"Clicked the sign-in button for {app_label} but the page did not "
                    f"proceed to a login form within {_ENTRA_STEP_TIMEOUT}s."
                ),
                "method": "sso_interstitial_timeout",
            }
        state = new_state
        if state == "authenticated":
            msg = f"Browser already authenticated after the sign-in click for {app_label}."
            logger.info("auth_gateway: %s", msg)
            return {"passed": True, "method": "kerberos_or_session", "message": msg}

    creds = credentials or {}
    username = (creds.get("username") or "").strip()
    password = (creds.get("password") or "").strip()
    if not username or not password:
        return {
            "passed": False,
            "message": (
                f"Authentication required for {app_label} but no credentials available. "
                "Store username and password in the application test data."
            ),
            "method": "none",
        }

    if state == "entra":
        result = authenticate_browser(driver, credentials)
        result["method"] = "entra"
        return result

    if state == "cam":
        result = _authenticate_cam(driver, credentials)
        result["method"] = "cam"
        return result

    if state == "generic":
        logger.warning(
            "auth_gateway: unknown login form for %r — attempting Entra flow as fallback",
            app_label,
        )
        result = authenticate_browser(driver, credentials)
        result["method"] = "entra_fallback"
        return result

    # unknown — browser error or blank page
    return {
        "passed": False,
        "message": f"Could not determine auth state for {app_label} — page may not have loaded.",
        "method": "unknown",
    }


# ── Session cookie sharing (Issue 3 — reduce logins per pipeline run) ─────────

def export_session_cookies(driver) -> list[dict]:
    """
    Export all cookies from an authenticated browser session.

    Called after a successful precheck so the crawl and survey browsers
    can import these cookies instead of logging in again — reducing three
    separate SSO logins per survey run down to one.

    Returns:
        List of cookie dicts as returned by driver.get_cookies(). Empty
        list on error (caller treats missing cookies as "no session to share").
    """
    try:
        cookies = driver.get_cookies()
        logger.info(
            "auth_gateway: exported %d session cookie(s) from precheck browser",
            len(cookies),
        )
        return cookies
    except Exception as exc:
        logger.warning("auth_gateway: could not export session cookies: %s", exc)
        return []


def import_session_cookies(driver, cookies: list[dict], base_url: str) -> bool:
    """
    Import session cookies into a fresh browser, replacing a full login.

    Selenium's add_cookie() only accepts cookies whose domain matches the
    page the browser is currently on. A fresh browser navigating to the app
    URL is immediately redirected to the IdP (Entra / CAM), so the browser
    lands on the wrong domain and every add_cookie() call is rejected.

    Fix: navigate to the bare application origin first (scheme + host, no
    path). This loads a simple page on the correct domain without triggering
    an auth redirect, puts the browser on the right domain for cookie
    insertion, then we navigate to the full URL to apply the session.

    Args:
        driver:   Fresh Selenium WebDriver (any URL).
        cookies:  Cookie list from export_session_cookies().
        base_url: Full application URL. The bare origin is derived from this.

    Returns:
        True if cookies were imported and the page looks authenticated,
        False if import failed or page still shows a login form.
    """
    if not cookies:
        logger.debug("auth_gateway: import_session_cookies called with empty list — skipping")
        return False

    # Derive the bare origin (e.g. "https://connect.baxter.com") so we can
    # land on the correct domain before adding cookies.
    from urllib.parse import urlparse
    parsed = urlparse(base_url)
    origin = f"{parsed.scheme}://{parsed.netloc}"

    logger.debug("auth_gateway: navigating to origin %r to prime cookie domain", origin)
    try:
        driver.get(origin)
    except Exception as exc:
        logger.warning("auth_gateway: could not navigate to origin %r: %s", origin, exc)
        return False

    imported = 0
    skipped = 0
    for cookie in cookies:
        try:
            # Selenium requires cookies to lack the 'expiry' key when it is
            # None; some drivers return it as None instead of omitting it.
            clean = {k: v for k, v in cookie.items() if v is not None}
            driver.add_cookie(clean)
            imported += 1
        except Exception as exc:
            logger.debug("auth_gateway: skipped cookie %r: %s", cookie.get("name"), exc)
            skipped += 1

    logger.info(
        "auth_gateway: imported %d cookie(s) (skipped %d) — navigating to app",
        imported, skipped,
    )

    if imported == 0:
        logger.warning("auth_gateway: no cookies were accepted — session sharing will not work")
        return False

    try:
        driver.get(base_url)
    except Exception as exc:
        logger.warning("auth_gateway: post-cookie navigate error: %s", exc)
        return False

    # Verify the cookies actually authenticated the session.
    state = _detect_auth_state(driver)
    if state == "authenticated":
        logger.info("auth_gateway: cookie import successful — browser is authenticated")
        return True

    logger.warning(
        "auth_gateway: cookie import did not authenticate browser — state=%s  url=%s",
        state, driver.current_url,
    )
    return False


def authenticate_browser(driver, credentials: dict) -> dict:
    """
    Authenticate a live Selenium browser through Azure Entra ID.

    Called before the crawl agent starts, so the crawl session is already
    authenticated and every screen captured reflects the logged-in state.

    The browser must already be on the application URL (or its SSO redirect)
    before this function is called.

    Args:
        driver:      Live Selenium WebDriver, already navigated to the app URL.
        credentials: {"username": ..., "password": ...} from stored test data.

    Returns:
        {"passed": bool, "message": str}
    """
    from selenium.webdriver.common.by import By
    from selenium.webdriver.support.ui import WebDriverWait
    from selenium.webdriver.support import expected_conditions as EC
    from selenium.common.exceptions import (
        TimeoutException,
        WebDriverException,
        StaleElementReferenceException,
    )

    username = (credentials.get("username") or "").strip()
    password = (credentials.get("password") or "").strip()
    if not username or not password:
        return {
            "passed": False,
            "message": "SSO authentication skipped: credentials missing (username or password empty).",
        }

    try:
        wait = WebDriverWait(driver, _ENTRA_STEP_TIMEOUT)

        def _click_entra_button(button_id: str, label: str, timeout: int = 8, retries: int = 2) -> None:
            """Click an Entra button with a small stale-safe retry loop."""
            last_exc = None
            for attempt in range(1, retries + 1):
                try:
                    WebDriverWait(driver, timeout).until(
                        EC.element_to_be_clickable((By.ID, button_id))
                    ).click()
                    return
                except StaleElementReferenceException as exc:
                    last_exc = exc
                    logger.debug(
                        "auth_gateway: %s button went stale on attempt %d/%d; retrying",
                        label,
                        attempt,
                        retries,
                    )
                    time.sleep(0.2)
            if last_exc:
                raise last_exc

        # Step 1 — wait for the Azure Entra username field
        logger.info("auth_gateway: waiting for Azure Entra username field")
        username_field = wait.until(
            EC.visibility_of_element_located((By.CSS_SELECTOR, _ENTRA_USERNAME_SELECTOR))
        )
        username_field.clear()
        username_field.send_keys(username)
        logger.info("auth_gateway: username entered")

        # Step 2 — click Next
        _click_entra_button(_ENTRA_NEXT_BUTTON_ID, "Next", timeout=_ENTRA_STEP_TIMEOUT, retries=3)
        logger.info("auth_gateway: Next clicked, waiting for password field")

        # Step 3 — wait for password field (may go through org picker first)
        password_field = wait.until(
            EC.visibility_of_element_located((By.CSS_SELECTOR, _ENTRA_PASSWORD_SELECTOR))
        )
        password_field.clear()
        password_field.send_keys(password)
        logger.info("auth_gateway: password entered")

        # Step 4 — click Sign in
        _click_entra_button(_ENTRA_SUBMIT_BUTTON_ID, "Sign in", timeout=_ENTRA_STEP_TIMEOUT, retries=3)
        logger.info("auth_gateway: Sign in clicked, waiting for redirect back to app")

        # Step 4.5 — handle optional "Stay signed in?" prompt (KMSI page).
        # This page keeps the URL on login.microsoftonline.com, so the poll
        # below would time out if we don't dismiss it first. The Yes button
        # reuses idSIButton9; the No button is idBtn_Back. We click Yes so
        # the session cookie is persisted and this prompt doesn't repeat.
        try:
            _click_entra_button(_ENTRA_NEXT_BUTTON_ID, "Stay signed in", timeout=8, retries=3)
            logger.info("auth_gateway: 'Stay signed in?' prompt dismissed (clicked Yes)")
        except TimeoutException:
            logger.debug("auth_gateway: no 'Stay signed in?' prompt appeared, continuing")
        except StaleElementReferenceException:
            logger.debug(
                "auth_gateway: 'Stay signed in?' prompt re-rendered during click; continuing"
            )

        # Step 5 — poll until the URL moves away from Entra.
        # WebDriverWait has no built-in url_not_contains, so we poll manually.
        deadline = time.time() + _ENTRA_STEP_TIMEOUT
        while _ENTRA_REDIRECT_DOMAIN in driver.current_url:
            if time.time() > deadline:
                raise TimeoutException("Timed out waiting for redirect away from Azure Entra")
            time.sleep(0.5)

        final_url = driver.current_url
        logger.info("auth_gateway: SSO authentication successful  final_url=%r", final_url)
        return {"passed": True, "message": f"Azure Entra SSO authentication successful. Redirected to: {final_url}"}

    except TimeoutException as e:
        logger.error("auth_gateway: SSO authentication timed out: %s", e)
        return {"passed": False, "message": f"Azure Entra SSO timed out: {e}"}
    except WebDriverException as e:
        logger.error("auth_gateway: SSO authentication browser error: %s", e)
        return {"passed": False, "message": f"Azure Entra SSO browser error: {e}"}


def build_sso_preamble(app_url: str, integrated: bool = False) -> str:
    """
    Return a Python code block that handles Azure Entra SSO authentication.

    Injected into generated test scripts as the SSO precondition preamble.
    The generated script calls this code before the first test step so every
    script is self-contained and can authenticate independently.

    Args:
        app_url: The application base URL (used in the redirect-back check).
        integrated: When True, emit the CREDENTIAL-FREE preamble for silent
            Windows/Kerberos/PRT single sign-on. The generated block types no
            username or password and never calls get_data(); it clicks any
            "Sign in with Microsoft" interstitial, then waits for the host's
            own logon session to carry the browser back to the application.
            If an Entra login form actually appears, it raises a clear error
            (the runner host is not providing a silent SSO session) rather
            than silently hanging. Default False preserves the existing
            credential-typed behaviour for every current application.

    Returns:
        Python source string, indented for direct injection into a function body.
    """
    if integrated:
        return build_integrated_sso_preamble(app_url)
    return f"""\
# SSO PREAMBLE — Azure Entra ID authentication
# Navigates through the Azure Entra login flow before the test steps begin.
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.common.exceptions import (
    TimeoutException as _TimeoutException,
    StaleElementReferenceException as _StaleElementReferenceException,
    NoSuchElementException as _NoSuchElementException,
)

_sso_wait = WebDriverWait(driver, {_ENTRA_STEP_TIMEOUT})

# Pre-Entra interstitial: some applications do NOT redirect straight to
# Entra -- they first show their OWN branded landing page with a single
# sign-in button (e.g. "Sign in with Microsoft"), and only redirect once
# that button is clicked (confirmed live: Netra's /login page). Detection
# is a broad, generic text pattern on a clickable element -- NOT hardcoded
# to one application's exact button label.
#
# COST FOR A DIRECT-TO-ENTRA APP (the common case, no interstitial): the
# check immediately below is a single, instant, one-shot scan -- if the
# browser is ALREADY on the Entra domain or the username field is ALREADY
# present, the fluent wait is skipped entirely and this falls straight
# through to the existing Entra-redirect wait beneath it, completely
# unchanged from before interstitial support existed.
#
# COST FOR AN APP WITH THE INTERSTITIAL: the fluent wait below (a real
# WebDriverWait.until(), NOT a fixed sleep or manual polling loop) returns
# the INSTANT the button renders -- so a button that appears in 0.5s costs
# 0.5s, not a fixed delay.
import re as _re
_sso_interstitial_re = _re.compile(
    r"sign[ -]?in\\s+with\\b|continue\\s+with\\s+(?:microsoft|azure|entra|sso)\\b|"
    r"\\blog[ -]?in\\s+with\\b", _re.I,
)

def _find_sso_interstitial_button(_d):
    for _el in _d.find_elements(By.CSS_SELECTOR, "button, a, [role='button'], input[type='submit'], input[type='button']"):
        try:
            if not _el.is_displayed() or not _el.is_enabled():
                continue
            _txt = (_el.text or _el.get_attribute("value") or "").strip()
            if _txt and _sso_interstitial_re.search(_txt):
                return _el
        except Exception:
            continue
    return None

import time as _time
_already_past_interstitial = (
    "{_ENTRA_REDIRECT_DOMAIN}" in (driver.current_url or "")
    or bool(driver.find_elements(By.CSS_SELECTOR, "{_ENTRA_USERNAME_SELECTOR}"))
)
if not _already_past_interstitial:
    try:
        _interstitial_btn = WebDriverWait(
            driver, {_SSO_INTERSTITIAL_TIMEOUT}, poll_frequency=0.3,
            ignored_exceptions=(_NoSuchElementException, _StaleElementReferenceException),
        ).until(lambda d: _find_sso_interstitial_button(d))
        _interstitial_btn.click()
    except _TimeoutException:
        pass

# Some environments redirect to Entra a moment after driver.get(APP_URL).
# Wait briefly before deciding that interactive SSO is unnecessary.
_sso_deadline = _time.time() + {_ENTRA_STEP_TIMEOUT}
_needs_interactive_sso = "{_ENTRA_REDIRECT_DOMAIN}" in (driver.current_url or "")

while not _needs_interactive_sso and _time.time() < _sso_deadline:
    _cur = driver.current_url or ""
    if "{_ENTRA_REDIRECT_DOMAIN}" in _cur:
        _needs_interactive_sso = True
        break
    # URL can lag while DOM already swapped to Entra login form.
    if driver.find_elements(By.CSS_SELECTOR, "{_ENTRA_USERNAME_SELECTOR}"):
        _needs_interactive_sso = True
        break
    _time.sleep(0.25)

# If session cookies already authenticated the browser, skip interactive login.
if _needs_interactive_sso:
    # Enter username and click Next
    _sso_wait.until(EC.visibility_of_element_located(
        (By.CSS_SELECTOR, "{_ENTRA_USERNAME_SELECTOR}")
    )).send_keys(get_data("username"))
    _sso_wait.until(EC.element_to_be_clickable(
        (By.ID, "{_ENTRA_NEXT_BUTTON_ID}")
    )).click()

    # Enter password and click Sign in
    _sso_wait.until(EC.visibility_of_element_located(
        (By.CSS_SELECTOR, "{_ENTRA_PASSWORD_SELECTOR}")
    )).send_keys(get_data("password"))
    _sso_wait.until(EC.element_to_be_clickable(
        (By.ID, "{_ENTRA_SUBMIT_BUTTON_ID}")
    )).click()

    # Handle optional "Stay signed in?" prompt (KMSI).
    # Entra shows this between Sign in and the redirect — it reuses idSIButton9
    # for the Yes button. We click Yes if it appears within 8 seconds.
    _kmsi_deadline = _time.time() + 8
    while "{_ENTRA_REDIRECT_DOMAIN}" in (driver.current_url or "") and _time.time() < _kmsi_deadline:
        try:
            _kmsi_btn = driver.find_elements(By.ID, "{_ENTRA_SUBMIT_BUTTON_ID}")
            if _kmsi_btn and _kmsi_btn[0].is_displayed() and _kmsi_btn[0].is_enabled():
                _kmsi_btn[0].click()
                break
        except Exception:
            pass
        _time.sleep(0.3)

    # Wait for redirect back to the application
    _deadline = _time.time() + {_ENTRA_STEP_TIMEOUT}
    while "{_ENTRA_REDIRECT_DOMAIN}" in driver.current_url:
        if _time.time() > _deadline:
            raise TimeoutError("Timed out waiting for Azure Entra redirect back to the application")
        _time.sleep(0.5)
# End SSO preamble
"""


def build_integrated_sso_preamble(app_url: str) -> str:
    """
    Return a CREDENTIAL-FREE Python code block for silent (integrated) SSO.

    This is the generated-script counterpart to integrated mode: the runner
    host is domain/Entra-joined and already logged in as a corporate user, so
    the browser authenticates silently via Kerberos / Entra Primary Refresh
    Token — exactly like a human opening JDE with no prompt. The block types
    NO username or password and never calls get_data(); it only:

      1. Clicks any "Sign in with Microsoft" interstitial button, if present.
      2. Waits for the host's own session to carry the browser back off the
         Entra domain to the application.
      3. If an Entra login form actually appears (silent SSO did not happen),
         raises a clear, actionable error instead of hanging or leaking a
         credential prompt — the correct signal that the runner host is not
         providing an integrated SSO session.
    """
    return f"""\
# SSO PREAMBLE — Integrated (silent Windows/Kerberos/PRT) authentication
# No credentials are typed: the runner host's own logon session authenticates
# the browser the same way it signs a human in to JDE with no prompt.
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.common.exceptions import (
    TimeoutException as _TimeoutException,
    StaleElementReferenceException as _StaleElementReferenceException,
    NoSuchElementException as _NoSuchElementException,
)
import re as _re
import time as _time

# Pre-Entra interstitial: some apps show their own branded landing page with a
# single "Sign in with Microsoft" button and only redirect once it is clicked.
# Clicking it is safe for integrated SSO — the Entra page that follows
# authenticates silently from the host session. Detection is a broad, generic
# text pattern, NOT hardcoded to any one application's button label.
_sso_interstitial_re = _re.compile(
    r"sign[ -]?in\\s+with\\b|continue\\s+with\\s+(?:microsoft|azure|entra|sso)\\b|"
    r"\\blog[ -]?in\\s+with\\b", _re.I,
)

def _find_sso_interstitial_button(_d):
    for _el in _d.find_elements(By.CSS_SELECTOR, "button, a, [role='button'], input[type='submit'], input[type='button']"):
        try:
            if not _el.is_displayed() or not _el.is_enabled():
                continue
            _txt = (_el.text or _el.get_attribute("value") or "").strip()
            if _txt and _sso_interstitial_re.search(_txt):
                return _el
        except Exception:
            continue
    return None

_already_past_interstitial = (
    "{_ENTRA_REDIRECT_DOMAIN}" in (driver.current_url or "")
    or bool(driver.find_elements(By.CSS_SELECTOR, "{_ENTRA_USERNAME_SELECTOR}"))
)
if not _already_past_interstitial:
    try:
        _interstitial_btn = WebDriverWait(
            driver, {_SSO_INTERSTITIAL_TIMEOUT}, poll_frequency=0.3,
            ignored_exceptions=(_NoSuchElementException, _StaleElementReferenceException),
        ).until(lambda d: _find_sso_interstitial_button(d))
        _interstitial_btn.click()
    except _TimeoutException:
        pass

# Wait for the host's silent session to carry the browser back to the app.
# No username/password is ever entered. If an Entra login FORM appears, silent
# SSO did not happen on this host — fail fast with a clear, actionable message.
_deadline = _time.time() + {_ENTRA_STEP_TIMEOUT}
while _time.time() < _deadline:
    _cur = driver.current_url or ""
    _on_entra = "{_ENTRA_REDIRECT_DOMAIN}" in _cur
    _login_form = bool(driver.find_elements(By.CSS_SELECTOR, "{_ENTRA_USERNAME_SELECTOR}"))
    if not _on_entra and not _login_form:
        break
    if _login_form:
        raise RuntimeError(
            "Integrated SSO expected a silent Windows/Kerberos/PRT login, but an "
            "Azure Entra sign-in form appeared. The runner host is not providing a "
            "silent SSO session (it must be domain/Entra-joined and running as a "
            "signed-in corporate user, with the app host on the browser's "
            "AuthServerAllowlist). No credentials were entered."
        )
    _time.sleep(0.5)
else:
    if "{_ENTRA_REDIRECT_DOMAIN}" in (driver.current_url or ""):
        raise TimeoutError(
            "Timed out waiting for integrated SSO to redirect back to the application."
        )
# End SSO preamble
"""


def _load_credentials(application: dict, triggered_by: Optional[str] = None) -> Optional[dict]:
    """
    Load the username and password this run should sign in with.

    When a real user triggered the run and has saved their own credential for
    this application, that per-user credential is used. Otherwise this falls
    back to the shared, file-based test data exactly as before.

    Returns {"username": str, "password": str} or None if not found.
    """
    app_id = application.get("id")
    if not app_id:
        return None
    db_cred = _db_credential(application, triggered_by)
    if db_cred and db_cred.get("username") and db_cred.get("password"):
        return {"username": db_cred["username"], "password": db_cred["password"]}
    try:
        data = load_test_data(application_id=int(app_id), environment="default")
        username = data.get("username") or ""
        password = data.get("password") or ""
        if username and password:
            return {"username": username, "password": password}
    except Exception as exc:
        logger.debug("auth_gateway: could not load credentials for app %s: %s", app_id, exc)
    return None


def run_sso_precheck(application: dict, phase: str, target_url: Optional[str],
                     triggered_by: Optional[str] = None) -> dict:
    """
    Run the SSO authentication check for SSO-enabled applications.

    Uses authenticate_browser() with a headless Selenium driver — no
    per-app pytest scripts required. Falls back to a legacy per-app script
    if one is configured (backwards compatibility).

    Returns:
        {
            "enabled": bool,   whether precheck applied at all
            "passed":  bool,   whether authentication succeeded
            "message": str,    human-readable status for the UI
        }
    """
    if not app_requires_sso_gateway(application):
        return {
            "enabled": False,
            "passed": True,
            "message": "SSO precheck not enabled for this application.",
        }

    if not phase_is_gated(phase):
        return {
            "enabled": False,
            "passed": True,
            "message": f"SSO precheck disabled for phase '{phase}'.",
        }

    url = target_url or ""
    if not url:
        return {
            "enabled": True,
            "passed": False,
            "message": "SSO precheck could not run: no target URL provided.",
        }

    integrated = app_uses_integrated_sso(application)
    credentials = _load_credentials(application, triggered_by)
    if not credentials and not integrated:
        app_name = application.get("name") or "(unnamed)"
        return {
            "enabled": True,
            "passed": False,
            "message": (
                f"SSO precheck for '{app_name}' failed: no credentials found. "
                "Store username and password in the application test data."
            ),
        }
    # Integrated (Windows/Kerberos/PRT) apps authenticate silently with no
    # stored credentials. We proceed with credentials=None: authenticate_driver
    # returns success on the 'authenticated' state and fails fast (never typing
    # a password) if a login form unexpectedly appears — which is the correct
    # signal that the runner host is not providing a silent SSO session.

    # Fall back to legacy per-app script if one is configured.
    script_path = resolve_gateway_script_path(application)
    if script_path:
        logger.info(
            "auth_gateway: using legacy gateway script for app %s: %s",
            application.get("id"), script_path,
        )
        from service.runner_utils import run_pytest
        from core.settings import settings as _settings
        r = run_pytest(
            script_path=script_path,
            headed=_settings.app.selenium_headed,
            target_url=url,
            extra_env=build_precheck_test_env(application, triggered_by),
        )
        if r.get("result") == "pass":
            return {"enabled": True, "passed": True, "message": f"SSO precheck passed ({phase}) via gateway script."}
        snippet = (r.get("output") or "").strip()[:240]
        return {
            "enabled": True,
            "passed": False,
            "message": f"SSO precheck failed during {phase} via gateway script. {snippet}",
        }

    # Generic Azure Entra authentication via live browser.
    # make_driver() reads settings.app.selenium_headed (SELENIUM_HEADED env var),
    # so headed/headless is controlled by the same single setting as every other
    # browser in the system — no separate SSO_PRECHECK_HEADED env var needed.
    from service.runner_utils import make_driver
    driver = None
    session_cookies: list = []
    try:
        driver = make_driver()
        driver.get(url)
        result = authenticate_driver(driver, credentials, application)
        if result["passed"]:
            session_cookies = export_session_cookies(driver)
    except Exception as exc:
        logger.exception("auth_gateway: SSO precheck browser error for app %s", application.get("id"))
        result = {"passed": False, "message": f"SSO precheck browser error: {exc}"}
        if driver:
            try:
                driver.quit()
            except Exception:
                pass
        driver = None

    if not result["passed"] and driver:
        try:
            driver.quit()
        except Exception:
            pass
        driver = None

    # NOTE: When result["passed"] is True, the driver is intentionally NOT quit
    # here. The caller (generation_runner) keeps it alive so verification can
    # reuse the authenticated session without opening a second browser.
    # The caller is responsible for calling driver.quit() when done.
    return {
        "enabled": True,
        "passed": result["passed"],
        "message": result["message"],
        "session_cookies": session_cookies,
        # Live authenticated driver — only present on success, None otherwise.
        "precheck_driver": driver,
    }


def build_precheck_test_env(application: dict, triggered_by: Optional[str] = None) -> dict:
    """
    Build TEST_* environment variables from the stored app test data.

    Used to supply credentials to the SSO gateway script without
    hardcoding them anywhere. When a real user triggered the run and has
    saved their own login for this application, their username/password
    override the shared file values; every other field still comes from the
    shared test data.
    """
    app_id = application.get("id")
    if not app_id:
        return {}

    try:
        data = load_test_data(application_id=int(app_id), environment="default")
    except Exception as e:
        logger.warning("Could not load SSO precheck test data for app %s: %s", app_id, e)
        data = {}

    data = dict(data or {})
    db_cred = _db_credential(application, triggered_by)
    if db_cred:
        data["username"] = db_cred["username"]
        data["password"] = db_cred["password"]

    out = {}
    for key, value in data.items():
        if value is None:
            continue
        norm = re.sub(r"[^A-Za-z0-9_]+", "_", str(key).strip().upper())
        norm = re.sub(r"_+", "_", norm).strip("_")
        if not norm:
            continue
        out[f"TEST_{norm}"] = str(value)
    return out


# ── Scenario augmentation ──────────────────────────────────────────────────────

def augment_sso_scenarios(proposed_test_cases: list) -> list:
    """
    Add explicit SSO authentication scenarios to a survey's proposals.

    The precheck proves login works; this ensures the test repository also
    contains auditable scenarios for the authentication paths and their
    failure modes.

    Only adds scenarios that are not already present (title-match check).
    """
    if not proposed_test_cases:
        proposed_test_cases = []

    def _title(tc) -> str:
        if isinstance(tc, dict):
            return str(tc.get("title") or "")
        return str(tc or "")

    existing_titles = {
        _title(tc).strip().lower() for tc in proposed_test_cases if _title(tc).strip()
    }

    required = [
        {
            "title": "Validate SSO login reaches the application home page after sign-in prompts",
            "description": (
                "User enters corporate credentials, completes sign-in prompts "
                "(including optional stay signed in), and lands in the authenticated app shell."
            ),
            "category": "authentication",
            "priority": "high",
            "scenario": "SSO Gateway",
            "steps": [
                {
                    "step": "Open the application URL and start enterprise SSO sign-in.",
                    "expected": "The SSO identity provider sign-in form is displayed.",
                },
                {
                    "step": "Enter a valid corporate username and password, then continue through prompts.",
                    "expected": "Authentication succeeds and user is redirected back to the application.",
                },
                {
                    "step": "Complete any optional stay-signed-in confirmation and continue.",
                    "expected": "The authenticated application landing page is visible.",
                },
            ],
        },
        {
            "title": "Validate invalid SSO credentials are rejected without granting application access",
            "description": (
                "A wrong password must not authenticate and the app must remain inaccessible."
            ),
            "category": "authentication",
            "priority": "high",
            "scenario": "SSO Gateway",
            "steps": [
                {
                    "step": "Open enterprise sign-in and enter a valid corporate username with an invalid password.",
                    "expected": "SSO rejects the credentials and shows an authentication error.",
                },
                {
                    "step": "Attempt to proceed after the failure.",
                    "expected": "User stays on SSO error/sign-in screen and is not logged into the application.",
                },
            ],
        },
    ]

    for scenario in required:
        key = (scenario.get("title") or "").strip().lower()
        if key and key not in existing_titles:
            proposed_test_cases.append(scenario)
            existing_titles.add(key)

    return proposed_test_cases

def build_scenario_test_env(
    application: dict,
    environment: Optional[str] = None,
    test_data_file: Optional[str] = None,
    triggered_by: Optional[str] = None,
) -> dict:
    """
    Build TEST_* environment variables for ONE scenario's script run.

    Same purpose as build_precheck_test_env, but additionally resolves a
    scenario's own per-form overrides on top of the app-level environment
    data (form values win on key collision), via
    test_data.loader.load_test_data's target_flow parameter — see
    test_data/loader.py.

    NOTE on the test_data_file parameter name: test_data/loader.py no
    longer has a scenario_file/test_data_file concept at all — test data
    for one application lives in exactly one file
    (test_data/applications/app_<id>.json), and a value that needs to
    differ per flow is resolved via an environment.forms.<target_flow>
    block, keyed by the SAME target_flow text a scenario was surveyed
    under (see api/routes/run.py's tags.target_flow and
    service/survey_runner.py). This function's own parameter name was kept
    as test_data_file, unchanged, so every existing caller of this
    function (not modified here — its callers were not available to
    verify in this change) keeps working without needing its own update;
    only the value it is CALLED WITH now needs to be that same target_flow
    string rather than a filename, for the merge below to actually resolve
    anything. Passing None (the previous no-op value) still reproduces
    build_precheck_test_env's app-level-only behaviour exactly.

    Previously this called load_test_data(scenario_file=test_data_file,
    ...), which raised "load_test_data() got an unexpected keyword
    argument 'scenario_file'" on every call — caught by the try/except
    below, so this silently returned {} (app-level-only data) every time,
    without ever surfacing the failure beyond a warning log line.
    """
    app_id = application.get("id")
    if not app_id:
        return {}
    try:
        data = load_test_data(
            target_flow=test_data_file,
            application_id=int(app_id),
            environment=environment or "default",
        )
    except Exception as e:
        logger.warning(
            "Could not load scenario test data for app %s target_flow=%s: %s",
            app_id, test_data_file, e,
        )
        data = {}

    data = dict(data or {})
    db_cred = _db_credential(application, triggered_by)
    if db_cred:
        data["username"] = db_cred["username"]
        data["password"] = db_cred["password"]

    out = {}
    for key, value in data.items():
        if value is None:
            continue
        norm = re.sub(r"[^A-Za-z0-9_]+", "_", str(key).strip().upper())
        norm = re.sub(r"_+", "_", norm).strip("_")
        if not norm:
            continue
        out[f"TEST_{norm}"] = str(value)
    return out