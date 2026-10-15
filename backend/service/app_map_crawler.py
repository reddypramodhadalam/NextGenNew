"""
service/app_map_crawler.py

App Map crawler — orchestrator that drives the Crawl Agent and persists
the result in the application_maps table.

Architecture
------------
1. Create a Chrome WebDriver and navigate to base_url.
2. Run agents/crawl_agent.py (LLM-driven):
   - Agent calls get_snapshot, click, type_text, navigate_to tools.
   - Handles login automatically (LLM sees login form in snapshot, fills it).
   - With BRD: navigates all BRD-relevant screens (no screen limit).
   - Without BRD: explores up to CRAWL_MAX_SCREENS screens (default 5).
3. Store the resulting map in application_maps table.
4. Return map_id so callers (API route, survey, healer) can reference it.

Progress events emitted via the sync callback(event_type, data):
    status   {"message": "..."}
    page     {"url": ..., "title": ..., "element_count": N, "page_number": N}
    complete {"map_id": N, "page_count": N, "element_count": N, "message": "..."}
    error    {"message": "..."}
"""

import json
import logging
from datetime import datetime, timezone
from typing import Callable, Optional

from core.settings import settings

logger = logging.getLogger(__name__)


# ── Browser helper ────────────────────────────────────────────────────────────

def _make_driver(headed: bool = False):
    """Create a Chrome WebDriver for the crawl agent.

    Kerberos / Windows Integrated Authentication:
    If KERBEROS_AUTH_DOMAINS is set (e.g. "*.baxter.com"), Chrome is
    configured to negotiate Kerberos tickets for matching domains.  Required
    for apps like Cognos Analytics that use Windows Integrated Authentication.
    """
    import os as _os
    from selenium import webdriver
    from selenium.webdriver.chrome.options import Options

    opts = Options()
    if not headed:
        opts.add_argument("--headless=new")
    opts.add_argument("--no-sandbox")
    opts.add_argument("--disable-dev-shm-usage")
    opts.add_argument("--window-size=1440,900")
    opts.add_argument("--disable-extensions")
    _kerberos_domains = _os.environ.get("KERBEROS_AUTH_DOMAINS", "").strip()
    if _kerberos_domains:
        opts.add_argument(f"--auth-server-whitelist={_kerberos_domains}")
        opts.add_argument(f"--auth-negotiate-delegate-whitelist={_kerberos_domains}")
        logger.debug("_make_driver: Kerberos auth enabled for domains: %s", _kerberos_domains)
    driver = webdriver.Chrome(options=opts)
    from service.workflow_control import track_driver
    track_driver(driver)
    logger.info(
        "_make_driver: Chrome created  headed=%s  version=%s  kerberos=%s",
        headed, driver.capabilities.get("browserVersion", "unknown"),
        bool(_kerberos_domains),
    )
    return driver


# ── Database persistence ──────────────────────────────────────────────────────

def _insert_map_running(application_id: int) -> int:
    from core.db.connection import get_conn
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """INSERT INTO application_maps (application_id, status, map_data)
                   VALUES (%s, 'running', '{}') RETURNING id""",
                (application_id,),
            )
            map_id = cur.fetchone()[0]
    logger.info("_insert_map_running: map_id=%d  application_id=%d", map_id, application_id)
    return map_id


def _finalise_map(map_id: int, map_data: dict, was_authenticated: bool) -> None:
    page_count    = len(map_data.get("pages", []))
    element_count = sum(len(p.get("elements", [])) for p in map_data.get("pages", []))
    logger.info(
        "_finalise_map: map_id=%d  pages=%d  elements=%d  authenticated=%s",
        map_id, page_count, element_count, was_authenticated,
    )
    from core.db.connection import get_conn
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """UPDATE application_maps
                   SET status='complete', map_data=%s,
                       page_count=%s, element_count=%s,
                       was_authenticated=%s, crawled_at=now()
                   WHERE id=%s""",
                (json.dumps(map_data), page_count, element_count,
                 was_authenticated, map_id),
            )


def _fail_map(map_id: int, error_message: str) -> None:
    logger.error("_fail_map: map_id=%d  error=%s", map_id, error_message[:200])
    from core.db.connection import get_conn
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE application_maps SET status='failed', error_message=%s WHERE id=%s",
                (error_message[:500], map_id),
            )


def get_latest_map(application_id: int) -> Optional[dict]:
    """Return the most recent complete map for this application, or None."""
    from core.db.connection import get_conn
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """SELECT id, crawled_at, page_count, element_count,
                          was_authenticated, map_data
                   FROM application_maps
                   WHERE application_id=%s AND status='complete'
                   ORDER BY crawled_at DESC LIMIT 1""",
                (application_id,),
            )
            row = cur.fetchone()
            if not row:
                return None
            cols = [d[0] for d in cur.description]
            result = dict(zip(cols, row))
    logger.info(
        "get_latest_map: application_id=%d  map_id=%d  pages=%d  elements=%d",
        application_id, result["id"], result["page_count"], result["element_count"],
    )
    return result


# ── Main crawl function ───────────────────────────────────────────────────────

def crawl_application(
    application_id: int,
    base_url: str,
    credentials: Optional[dict] = None,
    brd_text: Optional[str] = None,
    target_flow: Optional[str] = None,
    progress: Optional[Callable] = None,
    session_cookies: Optional[list] = None,
    test_data: Optional[dict] = None,
) -> int:
    """
    Run the Crawl Agent against base_url and persist the resulting map.

    The Crawl Agent (agents/crawl_agent.py) is LLM-driven — it calls
    get_snapshot, click, type_text, and navigate_to to explore the app.
    Authentication is handled in priority order:
      1. session_cookies — imported from the precheck browser so no fresh
         login is needed (Issue 3 fix: one login per pipeline run).
      2. Kerberos / Windows Integrated Auth — Chrome flags in _make_driver()
         negotiate authentication silently at the HTTP level.
      3. authenticate_driver() — detects Entra / CAM / generic form and fills
         it accordingly (Issues 1 & 2 fix).

    Args:
        application_id:  ID in the applications table.
        base_url:        Starting URL.
        credentials:     Optional {"username": ..., "password": ...} for login.
        brd_text:        Optional BRD text to guide which screens to visit.
                         Without this, the agent explores up to CRAWL_MAX_SCREENS.
        target_flow:     Optional plain-text navigation target (e.g. "Sales Order
                         Entry > Standard Order SO"), used only when brd_text is
                         not provided. See agents.crawl_agent.run_crawl for details.
        progress:        Optional sync callback(event_type, data) for live events.
        session_cookies: Optional cookie list from run_sso_precheck(). When set
                         the crawler imports them instead of logging in again.
        test_data:       Optional dict of real field values for this application
                         (e.g. {"SoldTo": "48166329"}), as returned by
                         test_data.loader.load_test_data(). Passed through to
                         agents.crawl_agent.run_crawl so target_flow steps can
                         use real values instead of only synthetic ones.

    Returns:
        map_id (primary key in application_maps).
    """
    from agents.crawl_agent import run_crawl
    from llm.provider_factory import get_provider

    def _emit(event_type: str, data: dict) -> None:
        if progress:
            try:
                progress(event_type, data)
            except Exception as cb_err:
                logger.debug("Progress callback error: %s", cb_err)

    map_id = _insert_map_running(application_id)
    driver = None

    try:
        provider = get_provider()
        # Respect the shared SELENIUM_HEADED setting (same one auth_gateway.py
        # and runner_utils.py use) instead of forcing headless — defaults to
        # headed (visible Chrome) so the user can watch the crawl happen.
        driver = _make_driver(headed=settings.app.selenium_headed)

        _emit("status", {"message": f"Opening {base_url}"})
        driver.get(base_url)

        # Authenticate before the crawl begins so the agent starts on an
        # already-authenticated session.  Three paths in priority order:
        #   1. Cookie replay  — fastest; avoids a full login round-trip.
        #   2. Kerberos       — silent; nothing to do here (flags on driver).
        #   3. Smart auth     — detect form type and fill credentials.
        from core.db.queries import applications as _apps_db
        from service.auth_gateway import (
            app_requires_sso_gateway,
            authenticate_driver,
            import_session_cookies,
        )
        _app_record = _apps_db.find_by_id(application_id) or {}
        if session_cookies:
            logger.info(
                "crawl_application: importing %d session cookie(s) for app %d — skipping login",
                len(session_cookies), application_id,
            )
            _emit("status", {"message": "Applying precheck session cookies..."})
            ok = import_session_cookies(driver, session_cookies, base_url)
            if not ok:
                # Cookies didn't authenticate — fall through to explicit auth.
                logger.warning(
                    "crawl_application: cookie import did not authenticate for app %d — falling back",
                    application_id,
                )
                _emit("status", {"message": "Cookie import ineffective — authenticating via credentials"})
                if credentials and app_requires_sso_gateway(_app_record):
                    sso_result = authenticate_driver(driver, credentials, _app_record)
                    if not sso_result["passed"]:
                        logger.error(
                            "crawl_application: SSO fallback auth failed for app %d: %s",
                            application_id, sso_result["message"],
                        )
                        _fail_map(map_id, sso_result["message"])
                        _emit("error", {"message": sso_result["message"]})
                        return map_id
            else:
                _emit("status", {"message": "Session cookies applied — crawler is authenticated"})
        elif credentials and app_requires_sso_gateway(_app_record):
            logger.info(
                "crawl_application: SSO required for app %d — authenticating (method auto-detected)",
                application_id,
            )
            _emit("status", {"message": "Authenticating via SSO..."})
            sso_result = authenticate_driver(driver, credentials, _app_record)
            if not sso_result["passed"]:
                logger.error(
                    "crawl_application: SSO authentication failed for app %d: %s",
                    application_id, sso_result["message"],
                )
                _fail_map(map_id, sso_result["message"])
                _emit("error", {"message": sso_result["message"]})
                return map_id
            logger.info(
                "crawl_application: SSO authentication successful for app %d (method=%s)",
                application_id, sso_result.get("method", "?"),
            )
            _emit("status", {"message": "SSO authentication successful"})

        def _on_step(page_num: int, step: dict) -> None:
            if step.get("action") == "page_recorded":
                inp = step.get("input", {})
                _emit("page", {
                    "url":           inp.get("url", ""),
                    "title":         inp.get("title", ""),
                    "element_count": inp.get("element_count", 0),
                    "page_number":   page_num,
                })
                _emit("status", {
                    "message": f"Recorded page {page_num}: {inp.get('title', inp.get('url', ''))}"
                })

        _emit("status", {"message": "Crawl agent starting..."})
        result = run_crawl(
            driver=driver,
            provider=provider,
            credentials=credentials,
            brd_text=brd_text,
            target_flow=target_flow,
            on_step=_on_step,
            base_url=base_url,
            test_data=test_data,
        )

        pages = result.get("pages", [])
        was_authenticated = credentials is not None and any(
            "login" not in p.get("url", "").lower() for p in pages
        )

        map_data = {
            "base_url":       base_url,
            "crawled_at":     datetime.now(timezone.utc).isoformat(),
            "page_count":     len(pages),
            "pages":          pages,
            "target_flow":    target_flow,
            "target_reached": result.get("target_reached"),
            "trace":          result.get("trace", []),
        }

        _finalise_map(map_id, map_data, was_authenticated)

        total_elements = sum(len(p.get("elements", [])) for p in pages)
        _emit("complete", {
            "map_id":        map_id,
            "page_count":    len(pages),
            "element_count": total_elements,
            "message":       f"Crawl complete — {len(pages)} pages, {total_elements} elements",
        })
        logger.info(
            "crawl_application: done  app=%d map_id=%d pages=%d elements=%d",
            application_id, map_id, len(pages), total_elements,
        )
        return map_id

    except Exception as exc:
        logger.exception("crawl_application: failed for app %d", application_id)
        _fail_map(map_id, str(exc))
        _emit("error", {"message": f"Crawl failed: {exc}"})
        raise

    finally:
        if driver:
            try:
                driver.quit()
            except Exception:
                pass
