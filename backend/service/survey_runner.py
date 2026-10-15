"""
service/survey_runner.py

Survey Phase Runner

Owns run_new_test_suite() — the first step in the new-test-suite flow.

Responsibilities:
  1. Create or find the application in the database.
  2. Run the SSO precheck if required.
  3. Extract and store the uploaded document if provided.
  4. Load requirements from Jira if that source is selected.
  5. Run the survey agent (fast or classic) in a worker thread.
  6. Build and return the in-memory proposal list for human review.

Generation does NOT happen here. Proposals are held in memory until a
reviewer accepts them via POST /v1/test-suites/accept-proposals.
"""

import asyncio
import logging
import uuid
from decimal import Decimal, ROUND_HALF_UP
from core.db.queries import ai_calls as ai_calls_db
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Optional

from selenium.common.exceptions import WebDriverException

from agents.author_agent import run_author
from core.db.queries import applications as apps_db
from core.db.queries import documents as documents_db
from llm.provider_factory import get_provider
from llm.usage_context import track_llm_usage  # TOKENS & COST: labels AI calls for the dashboard
from service.auth_gateway import (
    run_sso_precheck,
    augment_sso_scenarios,
    authenticate_driver,
    import_session_cookies,
)
from service.context_resolver import normalize_url
from service.document_extractor import extract_text_from_document
from service.fast_generation import fast_survey
from service.workflow_control import checkpoint
from service.runner_utils import (
    FAST_GENERATION,
    make_driver,
    sync_progress,
    sync_progress_status,
)

logger = logging.getLogger(__name__)


def _normalize_target_flow(text: Optional[str]) -> Optional[str]:
    """
    Normalise a target_flow string so trivial copy/paste differences do
    not silently defeat either:

      - the crawl reuse-gate below (an exact-text comparison against a
        previously stored map's target_flow — see run_new_test_suite's
        docstring for why an identical re-run should cost no further
        crawl-agent LLM calls once it has succeeded once), or
      - test_data.loader.load_test_data()'s "forms" match (exact or
        substring).

    Without this, pasting what LOOKS like the identical steps a second
    time could still silently trigger a brand new (paid) crawl, because
    the text is not byte-for-byte identical to what was stored the first
    time — e.g. Windows CRLF vs LF line endings, a trailing space picked
    up on one line, or an invisible zero-width space (U+200B) / BOM
    (U+FEFF) that rich-text copy/paste sometimes leaves behind and that
    looks completely identical on screen either way.

    Applied once, at the very top of run_new_test_suite, so every use of
    target_flow within this function (the reuse-gate check, the value
    stored into map_data by crawl_application, and the value stamped onto
    tags.target_flow at accept time in api/routes/run.py) is already
    working from the same normalised string — nothing downstream needs
    its own normalisation.

    Args:
        text: The raw target_flow as received from the caller, or None.

    Returns:
        The normalised string, or the original value unchanged if it was
        empty/None (nothing to normalise).
    """
    if not text:
        return text
    normalised = text.replace("\r\n", "\n").replace("\r", "\n")
    normalised = normalised.replace("\u200b", "").replace("\ufeff", "")
    lines = [line.strip() for line in normalised.split("\n")]
    return "\n".join(lines).strip()


async def run_new_test_suite(
    application_name: str,
    base_url: Optional[str],
    requires_sso_gateway: Optional[bool],
    document_path: Optional[str],
    document_type: Optional[str],
    triggered_by: str,
    progress_callback: Callable,
    jira_source: bool = False,
    jira_project_key: Optional[str] = None,
    jira_issue_key: Optional[str] = None,
    include_sso_scenarios: bool = True,
    test_type: str = "functional",
    target_flow: Optional[str] = None,
    cancel_check: Optional[Callable[[], bool]] = None,
) -> dict:
    """
    Run the survey phase of the new-test-suite flow.

    Steps:
        1. Create or find the application in the database.
        2. Run the SSO precheck if the application requires it.
        3. Store the uploaded document and extract its text.
        4. Load requirements from Jira if jira_source is True.
        5. Run the survey agent against the live URL.
        6. Build the in-memory proposal list.
        7. Stream a 'complete' progress event with proposals.

    Args:
        application_name: Display name for the application.
        base_url:         Optional live URL for the survey agent.
        requires_sso_gateway: Whether SSO gating is required.
        document_path:    Optional path to an uploaded BRD/FRD file.
        document_type:    'requirements' or 'existing_test_cases'.
        triggered_by:     Identity of who triggered this.
        progress_callback: async callable(event_type, data).
        jira_source:      Read requirements from Jira instead of a file.
        jira_project_key: Jira project key (e.g. 'KAN').
        jira_issue_key:   Scope to a single Jira issue (e.g. 'KAN-5').
        include_sso_scenarios: When True (default) and the SSO precheck passes,
            inject extra SSO/authentication scenarios into the proposal list.
            Set to False when you want functional, regression, or smoke test
            proposals only — without authentication-focused additions.
        target_flow:      Optional plain-text navigation target (e.g. "Sales >
            Sales Order Entry > Standard Order SO"), OR a full multi-line
            ordered step list. Used only when no document is uploaded.

            REUSE — avoiding a repeat (paid) crawl for the same steps:
            If a prior crawl already reached this exact target for this
            application (map_data.target_flow == this target_flow AND
            map_data.target_reached is True), that saved map is reused
            instead of re-crawling — see the reuse-gate below. This means
            running the SAME target_flow text again, once it has
            previously succeeded end-to-end, costs no further crawl-agent
            LLM calls at all; only the (cheaper) authoring step
            (fast_survey) runs again. target_reached only becomes True
            once every step in the list was actually completed — a crawl
            that fails or stalls partway (e.g. because required test data
            was missing, so the underlying application itself rejected an
            action) does NOT get reused, and rightly so: reusing a failed
            attempt would silently ground later steps in an incomplete
            map. Get the crawl to succeed once (real test data in place,
            see below) and every identical re-run after that is free of
            crawl-agent cost.

            Normalised via _normalize_target_flow() immediately on entry
            to this function (see that helper) so trivial copy/paste
            differences — Windows CRLF vs LF, a trailing space on one
            line, an invisible zero-width character sometimes introduced
            by rich-text copy/paste — do not silently defeat the reuse-gate
            comparison above by making two visually-identical pastes
            compare as different strings.

            This same (normalised) string doubles as the key into this
            application's per-form test data overrides:
            test_data/applications/app_<id>.json can have an
            environment.forms.<form name> block (e.g. forms."Sales Order
            Entry".ItemNumber) holding real field values SPECIFIC TO this
            flow — set once, ahead of time, on the Application's Test Data
            screen. This is what lets two flows under the same application
            (e.g. "Sales Order Entry" and "Inventory Adjustment") each
            carry their own value for a field name they both use, instead
            of colliding on the one flat application-level value.
            test_data.loader.load_test_data()'s target_flow parameter
            resolves this automatically (including when target_flow is a
            full step list that merely MENTIONS the form name somewhere in
            its text — see test_data/loader.py's substring-match fallback)
            — nothing else to create or link. A target_flow with no
            matching "forms" entry (or none at all) simply uses the flat
            application-level values, unchanged.
        cancel_check:     Optional zero-arg callable, currently NOT forwarded
            to crawl_application() (see _run_crawl below) because
            service/app_map_crawler.py's crawl_application() does not accept
            a cancel_check parameter — passing it there raises
            "crawl_application() got an unexpected keyword argument
            'cancel_check'", which previously caused every crawl to fail and
            silently fall back to a shallow single-page survey (no menu
            navigation at all). Still checked directly inside this function
            at the points below, so a disconnected caller is still detected
            before/after the crawl — only mid-crawl cancellation is
            unavailable until app_map_crawler.py adds real support for it.

    Returns:
        {"application_id": int, "proposals": list}
    """
    provider = get_provider()
    checkpoint()

    # Normalise once, up front, so the reuse-gate comparison below, the
    # value stored into map_data by crawl_application, and tags.target_flow
    # stamped at accept time (api/routes/run.py) are all working from the
    # same string — see _normalize_target_flow's docstring for why this
    # matters for avoiding a repeat (paid) crawl on an already-successful
    # target_flow.
    target_flow = _normalize_target_flow(target_flow)

    # ── Step 1: Create or find the application ─────────────────────────
    await progress_callback("status", {"message": "Setting up application..."})

    normalized = normalize_url(base_url) if base_url else None
    application = None
    app_notice = None
    if normalized:
        application = apps_db.find_by_normalized_url(normalized)

    if not application:
        application = apps_db.create(
            name=application_name,
            base_url=base_url,
            normalized_url=normalized,
            application_type="web",
            owner=triggered_by,
            requires_sso_gateway=bool(requires_sso_gateway),
        )
        logger.info("Created application id=%s name=%s", application["id"], application_name)
        app_notice = f"Created application '{application_name}'."
        await progress_callback("status", {"message": app_notice})
        if requires_sso_gateway is not None:
            updated = apps_db.set_requires_sso_gateway(
                application["id"],
                bool(requires_sso_gateway),
            )
            if updated:
                application = updated
    else:
        logger.info("Found existing application id=%s for %s", application["id"], normalized)
        existing_name = application.get("name")
        app_notice = (
            f"Added to the existing application '{existing_name}' "
            f"({base_url}). Applications are identified by their URL, so no "
            f"new application was created."
        )
        if existing_name and existing_name != application_name:
            app_notice += (
                f" The name you entered ('{application_name}') was not used — "
                f"enter a different URL to create a separate application."
            )
        await progress_callback("status", {"message": app_notice})

    application_id = application["id"]
    # TOKENS & COST: one id for this whole survey run, so its AI calls can be totalled and shared across the test cases it produces.
    survey_run_id = uuid.uuid4().hex
    checkpoint()

    # ── Cancellation checkpoint: cheap to check, and worth checking right
    # after the (possibly slow) DB round-trips above, before any browser or
    # LLM work is spent on a client that may already be gone.
    if cancel_check is not None and cancel_check():
        logger.info("Survey cancelled by caller before SSO precheck (application_id=%s)", application_id)
        return {"application_id": application_id, "proposals": []}

    # ── Step 2: SSO precheck ───────────────────────────────────────────
    sso_precheck = run_sso_precheck(
        application=application,
        phase="survey",
        target_url=base_url,
        triggered_by=triggered_by,
    )
    checkpoint()
    if sso_precheck["enabled"]:
        await progress_callback("status", {"message": "Running SSO precheck before survey..."})
    if sso_precheck["enabled"] and sso_precheck["passed"]:
        await progress_callback("status", {"message": f"SSO precheck passed: {sso_precheck['message']}"})
    if not sso_precheck["passed"]:
        await progress_callback("error", {"message": sso_precheck["message"]})
        await progress_callback("complete", {
            "application_id": application_id,
            "proposals": [],
            "message": sso_precheck["message"],
            "notice": app_notice,
            "application_name": application.get("name"),
            "sso_precheck": sso_precheck,
        })
        return {"error": sso_precheck["message"], "application_id": application_id, "proposals": []}

    # ── Step 3: Store the uploaded document ────────────────────────────
    source_document_id = None
    document_context = None
    if document_path and document_type:
        await progress_callback("status", {"message": "Processing uploaded document..."})
        extracted_text = extract_text_from_document(document_path)
        checkpoint()
        doc = documents_db.create(
            document_type=document_type,
            original_filename=Path(document_path).name,
            storage_path=document_path,
            extracted_text=extracted_text,
            application_id=application_id,
        )
        source_document_id = doc["id"]
        document_context = extracted_text
        logger.info("Stored document id=%s type=%s", doc["id"], document_type)

    # ── Step 4: Load requirements from Jira ────────────────────────────
    if jira_source and not document_context:
        await progress_callback("status", {"message": "Reading requirements from Jira..."})
        try:
            from service.jira_requirements_reader import load_requirements_text
            document_context = load_requirements_text(
                project_key=jira_project_key,
                issue_key=jira_issue_key,
            )
            checkpoint()
            logger.info("Loaded %d chars of requirements from Jira%s",
                        len(document_context or ""),
                        f" (issue {jira_issue_key})" if jira_issue_key else "")
        except Exception as e:
            logger.exception("Jira read failed")
            await progress_callback("error", {"message": f"Could not read from Jira: {e}"})
            return {"error": f"Jira read failed: {e}"}
        if not document_context:
            await progress_callback("error", {
                "message": "No requirements found in Jira for that project/type."})
            return {"error": "No Jira requirements found."}

    # ── Step 5: Crawl + Survey the URL ────────────────────────────────
    proposed_test_cases = []
    requirement_report = {}
    if base_url:
        if cancel_check is not None and cancel_check():
            logger.info("Survey cancelled by caller before crawl (application_id=%s)", application_id)
            return {"application_id": application_id, "proposals": []}

        await progress_callback("status", {"message": f"Surveying {base_url}..."})
        loop = asyncio.get_running_loop()

        # ── Step 5a: Crawl the application ────────────────────────────
        # Run the Crawl Agent to build a full multi-page element map before
        # the survey LLM sees the app. This means generation is grounded in
        # real elements from all relevant screens, not just the landing page.
        await progress_callback("status", {"message": "Crawling application to map all screens..."})

        def _run_crawl():
            checkpoint()
            from service.app_map_crawler import crawl_application
            from test_data.loader import load_test_data

            def _crawl_progress(event_type: str, data: dict) -> None:
                sync_progress_status(loop, progress_callback, data.get("message", ""))

            # Load stored credentials for this application (if any).
            # test_data/applications/app_<id>.json holds per-app login details
            # plus any other real field values (e.g. SoldTo, ShipTo) used to
            # ground target_flow steps in real data instead of synthetic values.
            #
            # target_flow=target_flow resolves this flow's own
            # environment.forms.<form name> block on top of the
            # application-level data (flow values override on a shared key)
            # — see this function's docstring for why this matters: two
            # flows under the same application can now each carry their own
            # value for a field name they both use (e.g. ItemNumber),
            # instead of both reading the one application-level value and
            # colliding. Nothing to create or link — the same target_flow
            # text already typed on the Configure step is the whole key.
            try:
                td = load_test_data(
                    application_id=application_id,
                    environment="default",
                    target_flow=target_flow,
                )
            except Exception:
                td = {}
            # Login credentials come from the SAME single door the SSO precheck
            # used: _load_credentials() returns the signed-in Kavach user's own
            # saved credential for this application when they have one, and only
            # falls back to the shared file-based test data otherwise. Resolving
            # them here — instead of reading td["username"]/td["password"]
            # directly — means that when cookie replay doesn't take, the crawl's
            # login fallback authenticates with the real per-user credential the
            # precheck just used, never a placeholder like first_last@baxter.com.
            try:
                from service.auth_gateway import _load_credentials
                credentials = _load_credentials(application, triggered_by)
            except Exception:
                credentials = (
                    {"username": td["username"], "password": td["password"]}
                    if td.get("username") and td.get("password")
                    else None
                )
            # Pass precheck cookies so the crawl browser skips logging in
            # again (Issue 3 fix: one login per pipeline run, not three).
            _crawl_cookies = sso_precheck.get("session_cookies") or []
            return crawl_application(
                application_id=application_id,
                base_url=base_url,
                credentials=credentials,
                brd_text=document_context,
                target_flow=target_flow,
                progress=_crawl_progress,
                session_cookies=_crawl_cookies,
                test_data=td,
                # cancel_check is deliberately NOT passed here.
                # service/app_map_crawler.py's crawl_application() does not
                # accept a cancel_check parameter today, and passing one
                # raised "crawl_application() got an unexpected keyword
                # argument 'cancel_check'" on every single call — the crawl
                # never ran, and every survey silently fell back to a
                # shallow single-page read with no menu navigation at all.
                # Once app_map_crawler.py is updated to accept and forward
                # cancel_check through to agents.crawl_agent.run_crawl (which
                # already supports it), this can be reinstated.
            )

        # Reuse-gate: skip a fresh crawl only if a prior map for this app
        # already reached this exact target_flow. If it fell short last time
        # (or this is a new/different target), crawl fresh — reusing a failed
        # or unrelated map would silently ground the survey in the wrong screens.
        #
        # target_flow was already normalised at the top of this function
        # (see _normalize_target_flow), so this comparison is stable across
        # trivial copy/paste differences between runs — and safe to reuse
        # regardless of test data content, since target_flow is the only
        # key into environment.forms, so the same target_flow always
        # resolves the same form-specific values whether the map is fresh
        # or reused.
        _reused_map_id: Optional[int] = None
        if target_flow:
            try:
                from service.app_map_crawler import get_latest_map
                _existing_map = get_latest_map(application_id)
                _existing_data = (_existing_map or {}).get("map_data") or {}
                if (
                    _existing_map
                    and _existing_data.get("target_flow") == target_flow
                    and _existing_data.get("target_reached") is True
                ):
                    _reused_map_id = _existing_map["id"]
                    logger.info(
                        "Survey: reusing map_id=%d for target_flow=%r (already reached)",
                        _reused_map_id, target_flow,
                    )
            except Exception as reuse_err:
                logger.warning(
                    "Survey: could not check for a reusable map (%s) — crawling fresh",
                    reuse_err,
                )

        if _reused_map_id:
            map_id = _reused_map_id
        else:
            try:
                # TOKENS & COST: the crawl's AI calls exist only to produce test cases,
                # so they are counted as test case generation. Change to "crawl" to exclude them.
                with track_llm_usage("test_case_generation", application_id=application_id, related_to_id=application_id, run_id=survey_run_id):
                    map_id = await asyncio.to_thread(_run_crawl)
                logger.info("Survey: crawl complete  map_id=%d", map_id)
            except Exception as crawl_err:
                checkpoint()
                logger.warning("Survey: crawl failed (%s) — proceeding with single-page survey", crawl_err)
                map_id = None

        checkpoint()
        if cancel_check is not None and cancel_check():
            logger.info(
                "Survey cancelled by caller after crawl, before scenario authoring "
                "(application_id=%s)", application_id,
            )
            return {"application_id": application_id, "proposals": []}

        # ── Load crawled map and format for LLM consumption ───────────
        # Gives the survey agent the full multi-screen element map rather
        # than the landing page (typically a login screen with ~3 elements).
        app_map_context: str = ""
        app_map_identifiers: set = set()
        app_map_data: Optional[dict] = None
        if map_id:
            try:
                from service.app_map_crawler import get_latest_map
                from service.map_formatter import format_map_as_context, identifiers_from_map
                map_row = get_latest_map(application_id)
                if map_row and map_row.get("map_data", {}).get("pages"):
                    app_map_context = format_map_as_context(map_row["map_data"])
                    app_map_identifiers = identifiers_from_map(map_row["map_data"])
                    app_map_data = map_row["map_data"]
                    logger.info(
                        "Survey: map context ready  chars=%d  identifiers=%d",
                        len(app_map_context), len(app_map_identifiers),
                    )
                else:
                    logger.warning("Survey: map_id=%d exists but has no pages — survey will use live page", map_id)
            except Exception as fmt_err:
                logger.warning("Survey: could not load/format crawled map (%s) — falling back to live page", fmt_err)

        # ── Step 5b: Survey using the crawled map ─────────────────────
        def _run_survey():
            checkpoint()
            driver = make_driver()
            try:
                driver.get(base_url)

                # Authenticate the survey browser before element extraction.
                # Three paths in priority order (matches crawl_application logic):
                #   1. Cookie replay from precheck  — skips a full login (Issue 3).
                #   2. Kerberos                     — already handled by driver flags.
                #   3. Smart auth via authenticate_driver() — detects Entra/CAM/generic
                #      (Issues 1 & 2: no longer assumes every app uses Azure Entra).
                if sso_precheck.get("enabled") and sso_precheck.get("passed"):
                    from service.auth_gateway import _load_credentials
                    _sso_creds = _load_credentials(application, triggered_by)
                    _session_cookies = sso_precheck.get("session_cookies") or []

                    if _session_cookies:
                        logger.info(
                            "survey_runner: importing %d precheck cookie(s) into survey browser",
                            len(_session_cookies),
                        )
                        ok = import_session_cookies(driver, _session_cookies, base_url)
                        if not ok:
                            logger.warning(
                                "survey_runner: cookie import failed — falling back to credential auth"
                            )
                            if _sso_creds:
                                _sso_result = authenticate_driver(driver, _sso_creds, application)
                                if not _sso_result["passed"]:
                                    raise RuntimeError(
                                        f"SSO fallback auth failed for survey browser: {_sso_result['message']}"
                                    )
                                logger.info(
                                    "survey_runner: survey browser authenticated via %s (fallback)",
                                    _sso_result.get("method", "?"),
                                )
                        else:
                            logger.info("survey_runner: survey browser authenticated via cookie replay")
                    elif _sso_creds:
                        # No cookies — full credential-based login with smart form detection.
                        _sso_result = authenticate_driver(driver, _sso_creds, application)
                        if not _sso_result["passed"]:
                            raise RuntimeError(
                                f"SSO authentication failed for survey browser: {_sso_result['message']}"
                            )
                        logger.info(
                            "survey_runner: survey browser authenticated via %s",
                            _sso_result.get("method", "?"),
                        )
                checkpoint()

                if FAST_GENERATION:
                    logger.info("Survey: single-shot mode (FAST_GENERATION=1)")

                    def _chunk_progress(done, total, label):
                        sync_progress_status(
                            loop, progress_callback,
                            f"Requirement {done}/{total}: {label}",
                        )

                    # Reload the same scoped test data used for the crawl. `td`
                    # was loaded earlier inside _run_crawl()'s local scope only,
                    # and was passed to crawl_application() for login/form-fill
                    # during exploration — it never reached the scenario-authoring
                    # call below. That gap was the root cause of target_flow steps
                    # inventing their own field values instead of using real
                    # per-application data from test_data/applications/app_<id>.json.
                    #
                    # target_flow=target_flow here too, for the same reason
                    # as in _run_crawl() above: the authored steps must be
                    # grounded in the SAME flow-specific values the crawl
                    # itself just used to reach this screen, not fall back to
                    # application-level-only data at this later stage.
                    try:
                        from test_data.loader import load_test_data as _load_td_for_survey
                        _survey_test_data = _load_td_for_survey(
                            application_id=application_id, environment="default",
                            target_flow=target_flow,
                        )
                    except Exception:
                        _survey_test_data = {}

                    fast_result = fast_survey(
                        driver=driver,
                        url=base_url,
                        provider=provider,
                        document_context=document_context,
                        on_progress=_chunk_progress,
                        app_map_context=app_map_context or None,
                        app_map_identifiers=app_map_identifiers or None,
                        test_type=test_type,
                        target_flow=target_flow,
                        app_map_data=app_map_data,
                        test_data=_survey_test_data or None,
                    )
                    checkpoint()
                    if not (fast_result or {}).get("test_cases"):
                        sync_progress_status(
                            loop, progress_callback,
                            "Fast survey returned no scenarios. Retrying with classic survey...",
                        )
                        logger.warning(
                            "Fast survey returned 0 test cases for %s; falling back to run_author",
                            base_url,
                        )
                        # Prepend the map context to the author agent's context so it
                        # also sees all crawled screens, not just the current live page.
                        author_context = (
                            f"{app_map_context}\n\n{document_context}"
                            if app_map_context and document_context
                            else app_map_context or document_context
                        )
                        return run_author(
                            driver=driver,
                            provider=provider,
                            context=author_context,
                            on_step=lambda sn, step: sync_progress(progress_callback, sn, step),
                        )
                    return fast_result

                # Classic author agent path — prepend map context so the agent
                # is grounded in all crawled screens.
                author_context = (
                    f"{app_map_context}\n\n{document_context}"
                    if app_map_context and document_context
                    else app_map_context or document_context
                )
                return run_author(
                    driver=driver,
                    provider=provider,
                    context=author_context,
                    on_step=lambda sn, step: sync_progress(progress_callback, sn, step),
                )
            finally:
                driver.quit()

        try:
            # TOKENS & COST: labels every AI call in the survey, including the classic-survey
            # fallback, as test case generation. The label is copied into the worker thread.
            with track_llm_usage("test_case_generation", application_id=application_id, related_to_id=application_id, run_id=survey_run_id):
                result = await asyncio.to_thread(_run_survey)
            checkpoint()
            proposed_test_cases = result.get("test_cases", [])
            if sso_precheck["enabled"] and sso_precheck["passed"] and include_sso_scenarios:
                proposed_test_cases = augment_sso_scenarios(proposed_test_cases)
                await progress_callback("status", {
                    "message": "Added SSO-focused authentication scenarios to survey output."
                })
            elif sso_precheck["enabled"] and sso_precheck["passed"] and not include_sso_scenarios:
                logger.info(
                    "SSO precheck passed but include_sso_scenarios=False — "
                    "skipping SSO scenario injection as requested."
                )
            requirement_report = result.get("requirement_report") or {}
            logger.info("Author agent proposed %d scenarios", len(proposed_test_cases))
        except WebDriverException as e:
            raw = str(e)
            if any(k in raw for k in ("ERR_CONNECTION_REFUSED", "ERR_NAME_NOT_RESOLVED", "ERR_CONNECTION")):
                friendly = (
                    "Couldn't reach the URL you entered. Make sure the site is "
                    "running and the address is correct. "
                    "Nothing appears to be responding at that address."
                )
            elif any(k in raw.lower() for k in ("cannot find", "no such file", "executable")):
                friendly = "Google Chrome wasn't found on this machine. Please install Chrome and try again."
            elif "version" in raw.lower() and "chromedriver" in raw.lower():
                friendly = "ChromeDriver doesn't match your installed Chrome version. Update ChromeDriver and retry."
            else:
                friendly = "Couldn't open the browser to survey the page. Check the URL is reachable and Chrome is installed."
            await progress_callback("error", {"message": friendly})
            return {"error": friendly}

    # ── Step 6: Build the in-memory proposal list ──────────────────────
    checkpoint()
    await progress_callback("status", {"message": "Saving scenarios..."})

    saved_proposals = []
    existing_titles = set()
    existing_by_title = {}
    try:
        from core.db.connection import get_conn
        with get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT * FROM test_case_proposals "
                    "WHERE application_id = %s AND status != 'archived'",
                    (application_id,),
                )
                cols = [d[0] for d in cur.description]
                for row in cur.fetchall():
                    p = dict(zip(cols, row))
                    key = (p.get("title") or "").strip().lower()
                    existing_titles.add(key)
                    existing_by_title[key] = p
    except Exception as e:
        logger.warning("Could not load existing scenarios: %s", e)

    skipped_existing = 0
    for tc in proposed_test_cases:
        steps = None
        if isinstance(tc, dict):
            tc_text = tc.get("title", "")
            description = tc.get("description") or tc_text
            category = tc.get("category")
            priority = tc.get("priority")
            scenario_group = tc.get("scenario") or "General"
            requirement_id = tc.get("requirement_id")
            steps = tc.get("steps")
            preconditions = tc.get("preconditions") or []
        else:
            tc_text = str(tc)
            description = tc_text
            category = priority = requirement_id = None
            scenario_group = "General"
            preconditions = []
        if not tc_text:
            continue
        key = tc_text.strip().lower()
        prior = existing_by_title.get(key)
        if prior:
            skipped_existing += 1
        saved_proposals.append({
            "id": f"proposed_{len(saved_proposals) + 1}",
            "is_proposal": True,
            "title": tc_text[:200],
            "description": description,
            "steps": steps,
            "step_count": len(steps or []),
            "unmatched_steps": [s.get("step") for s in (steps or [])
                                if not s.get("matched", True)],
            "category": category,
            "priority": priority,
            "test_type": test_type,
            "tags": {"scenario": scenario_group, "requirement_id": requirement_id,
                     "preconditions": preconditions},
            "scenario": scenario_group,
            "requirement_id": requirement_id,
            "duplicate_of": (prior or {}).get("id"),
            "duplicate_title": (prior or {}).get("title"),
        })
      # TOKENS & COST: total AI cost of this survey run, split evenly across the test cases it produced.
    # This is an estimate per test case: the survey makes its AI calls for many test cases at once,
    # so an exact per-test-case cost cannot be measured.
    try:
        run_cost = ai_calls_db.sum_cost_for_run(survey_run_id)
    except Exception:
        logger.exception("Could not total survey cost (non-fatal)")
        run_cost = Decimal("0")
    per_case_cost = (
        (run_cost / len(saved_proposals)).quantize(Decimal("0.000001"), rounding=ROUND_HALF_UP)
        if saved_proposals else Decimal("0")
    )
    for p in saved_proposals:
        p["ai_cost_usd"] = str(per_case_cost)   # shown as an estimate in the UI
        p["survey_run_id"] = survey_run_id      # lets the accept step find this run
    # Saved so that accepting a test case later can store its share, even if the browser doesn't send the id.
    if saved_proposals:
        ai_calls_db.save_survey_run(survey_run_id, application_id, len(saved_proposals), run_cost, per_case_cost)

    # Record requirement coverage against the document for traceability.
    if source_document_id and requirement_report:
        try:
            import json as _json
            entry = dict(requirement_report)
            entry["at"] = datetime.now(timezone.utc).isoformat()
            from core.db.connection import get_conn
            with get_conn() as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        """UPDATE documents
                           SET requirement_index = jsonb_set(
                                   COALESCE(requirement_index, '{"runs": []}'::jsonb),
                                   '{runs}',
                                   COALESCE(requirement_index->'runs', '[]'::jsonb)
                                       || %s::jsonb)
                           WHERE id = %s""",
                        (_json.dumps([entry]), source_document_id),
                    )
                conn.commit()
        except Exception as e:
            logger.warning("Could not record requirement coverage for document %s: %s",
                           source_document_id, e)

    # Build the coverage gap note surfaced to the reviewer.
    zero = requirement_report.get("zero_coverage") or []
    unsurveyed = requirement_report.get("not_surveyed") or []
    gap_note = ""
    if zero:
        gap_note += (
            f" {len(zero)} requirement{'' if len(zero) == 1 else 's'} "
            f"produced no test case: {', '.join(zero[:8])}"
            f"{'…' if len(zero) > 8 else ''}."
        )
    if unsurveyed:
        gap_note += (
            f" {len(unsurveyed)} not surveyed (SURVEY_MAX_CHUNKS limit): "
            f"{', '.join(unsurveyed[:8])}"
            f"{'…' if len(unsurveyed) > 8 else ''}."
        )

    logger.info("Survey complete: %d scenarios", len(saved_proposals))
    await progress_callback("complete", {
        "application_id": application_id,
        "proposals": saved_proposals,
        "message": (
            f"{len(saved_proposals)} scenario"
            f"{'' if len(saved_proposals) == 1 else 's'} ready"
            + (f" ({len(saved_proposals) - skipped_existing} new, "
               f"{skipped_existing} already existed)"
               if skipped_existing else "")
            + ". Review and prioritise before automating."
            + gap_note
        ),
        "skipped_existing": skipped_existing,
        "requirement_report": requirement_report,
        "notice": app_notice,
        "application_name": application.get("name"),
    })
    return {"application_id": application_id, "proposals": saved_proposals}