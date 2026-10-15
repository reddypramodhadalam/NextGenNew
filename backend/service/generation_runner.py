"""
service/generation_runner.py

Generation Phase Runner

Owns run_generate_scripts() — the second step in the test-suite flow.

Responsibilities:
  - Run the SSO precheck before starting a generation batch.
  - For each approved proposal, run the Explorer Agent (or single-shot
    fast_generate_script) and write the resulting script to DB and disk.
  - Handle API quota exhaustion gracefully — mark the proposal incomplete
    and continue to the next one; never hang.
  - Verify + self-repair each generated script when VERIFY_GENERATED=1.
"""

import asyncio
import json
import logging
from pathlib import Path
from typing import Callable, Optional

from selenium.common.exceptions import WebDriverException

from agents.explorer_agent import run_explorer
from core.compiler import compile_trace
from core.db.queries import applications as apps_db
from core.db.queries import proposals as proposals_db
from core.db.queries import generations as generations_db
from core.settings import settings
from llm.base import LLMProviderError
from llm.provider_factory import get_provider
from llm.usage_context import set_related_id, track_llm_usage  # TOKENS & COST: labels AI calls for the dashboard
from service.auth_gateway import (
    app_requires_sso_gateway,
    build_precheck_test_env,
    build_scenario_test_env,
    run_sso_precheck,
)
from service.fast_generation import (
    extract_element_map,
    fast_generate_script,
    verify_and_repair,
)
from service.generation_contract import build_generation_contract
from service.map_formatter import format_map_for_scripts
from service.app_map_crawler import get_latest_map
from service.pytest_helpers import inject_pytest_mark, generate_conftest
from service.runner_utils import (
    FAST_GENERATION,
    GENERATED_SCRIPTS_DIR,
    PARALLEL_GENERATION_WORKERS,
    VERIFY_GENERATED,
    make_driver,
)

logger = logging.getLogger(__name__)


# TOKENS & COST: this was originally named _generate_one_blocking. It is renamed to
# _generate_one_blocking_impl and wrapped by _generate_one_blocking (defined below,
# above run_generate_scripts) so the wrapper can label its AI calls. Logic unchanged.
def _generate_one_blocking_impl(
    proposal: dict,
    target_url: str,
    provider,
    script_map_context: Optional[str],
    script_map_data: Optional[dict],
    triggered_by: str,
    loop: asyncio.AbstractEventLoop,
    progress_callback,
    verify_test_env: Optional[dict] = None,
    sso_required: bool = False,
    precheck_driver=None,
    replace_existing: bool = False,
    candidate_maps: Optional[list] = None,
    map_load_error: Optional[str] = None,
) -> dict:
    """
    Run one script generation synchronously (blocking).

    Called via asyncio.to_thread so it can run in a thread pool without
    blocking the event loop. All DB writes, driver operations, and LLM
    calls happen here. Step-level progress (explorer mode only) is emitted
    thread-safely via asyncio.run_coroutine_threadsafe.

    Returns a result dict: {result, proposal_id, generation_id, notes,
    script_path, test_verdict, script_content, message}.
    """
    proposal_id = proposal["id"]
    from service.workflow_control import (
        FlowCancelled, checkpoint, cancellation_requested, completion_boundary,
    )
    checkpoint()
    from service.generation_policy import (
        _supports_action_grammar, infer_final_ui_observation, is_grounded_script,
        select_generation_map, steps_fingerprint,
    )
    # Historical settings tags remain untouched; they no longer control generation.
    generation_context = {"steps_sha256": steps_fingerprint(proposal.get("steps"))}
    final_ui_observation = None
    selection_error = None
    diagnostics = []
    try:
        preconditions = (proposal.get("tags") or {}).get("preconditions") or []
        if FAST_GENERATION and _supports_action_grammar(proposal.get("steps"), preconditions):
            if map_load_error:
                raise ValueError(map_load_error)
            maps = candidate_maps
            if maps is None:
                # Compatibility for direct callers that already supply a map.
                maps = [{"id": script_map_data.get("map_id") or script_map_data.get("id"),
                         "map_data": script_map_data}] if script_map_data else []
            selected = select_generation_map(proposal["steps"], maps, preconditions, sso_required)
            script_map_data = {**selected["map_data"], "map_id": selected["id"]}
            script_map_context = format_map_for_scripts(script_map_data)
            final_ui_observation = infer_final_ui_observation(proposal["steps"], script_map_data)
            generation_context.update(app_map_id=selected["id"],
                                      map_selection=selected["selection_summary"],
                                      final_ui_observation=final_ui_observation)
    except ValueError as exc:
        selection_error = str(exc)
    except Exception:
        logger.exception("Cannot select generation map for proposal id=%s", proposal_id)
        selection_error = "Captured controls could not be evaluated. No fallback map was used; retry after checking the application capture."
    if selection_error:
        script_map_data = None
        diagnostics.append(selection_error)

    proposals_db.update_status(proposal_id, "generating")

    # Resolve the app_map_id from the map data so the healer can later
    # retrieve the exact element snapshot used to generate this script.
    _app_map_id: Optional[int] = None
    if script_map_data and isinstance(script_map_data, dict):
        _app_map_id = script_map_data.get("id") or script_map_data.get("map_id")

    generation = generations_db.create(
        test_case_proposal_id=proposal_id,
        provider="ollama",
        model=settings.llm.model,
        triggered_by=triggered_by,
        app_map_id=_app_map_id,
    )
    generation_id = generation["id"]
    # TOKENS & COST: from here on, every AI call (script writing and verify-and-repair) is linked to this generation.
    set_related_id(generation_id)
    logger.info("Started generation id=%s for proposal id=%s", generation_id, proposal_id)

    def cancelled_result():
        # No usable replacement was completed: keep prior scripts and verdicts.
        generations_db.complete(
            generation_id=generation_id, generation_status="incomplete",
            test_verdict=None, script_content=None, script_path=None,
            steps_taken=0, trace={"cancelled": True, "generation_context": generation_context},
            notes="Cancelled by user before generation completed.",
            **({"replace_existing": True} if replace_existing else {}),
        )
        if not replace_existing:
            proposals_db.update_status(proposal_id, proposal.get("status") or "queued")
        return {"result": "cancelled", "proposal_id": proposal_id,
                "generation_id": generation_id, "message": "Generation cancelled."}

    trace = None
    failure_notes = None
    fast_code = None
    last_step = None
    verify_note = None
    try:
        if cancellation_requested():
            return cancelled_result()
        if selection_error:
            raise ValueError(selection_error)
        driver = make_driver()
        try:
            driver.get(target_url)
            if cancellation_requested():
                return cancelled_result()
            if FAST_GENERATION:
                logger.info("Generation: single-shot mode for proposal id=%s", proposal_id)
                if not script_map_context:
                    extract_element_map(driver, target_url)
                preconditions = (proposal.get("tags") or {}).get("preconditions") or []
                scenario_contract = build_generation_contract(
                    title=proposal.get("title") or "",
                    steps=proposal.get("steps"),
                    preconditions=preconditions,
                )
                logger.debug(
                    "Proposal %s generation contract: auth=%s nav=%s targets=%s",
                    proposal_id,
                    scenario_contract.get("authentication_required"),
                    scenario_contract.get("needs_navigation_context"),
                    scenario_contract.get("navigation_targets"),
                )
                fast_code = fast_generate_script(
                    url=target_url,
                    scenario=proposal["description"] or proposal["title"],
                    provider=provider,
                    driver=driver,
                    app_map_context=script_map_context,
                    app_map_data=script_map_data,
                    preconditions=preconditions,
                    scenario_contract=scenario_contract,
                    sso_required=sso_required,
                    proposal_steps=proposal.get("steps"),
                    final_ui_verification=final_ui_observation,
                    diagnostics=diagnostics,
                )
                trace = None
                if fast_code is None:
                    failure_notes = "Generation did not produce a script satisfying the original procedure. " + (
                        " ".join(dict.fromkeys(diagnostics)) if diagnostics else
                        "The generator returned no usable script; check provider and validation logs. No test steps were bypassed."
                    )
            else:
                def _on_step(sn, step):
                    asyncio.run_coroutine_threadsafe(
                        progress_callback("agent_step", {
                            "step_num": sn,
                            "action": step.get("action"),
                            "message": f"Step {sn}: {step.get('action', '')}",
                        }),
                        loop,
                    )
                trace = run_explorer(
                    driver=driver,
                    test_case=proposal["description"],
                    provider=provider,
                    on_step=_on_step,
                )
        finally:
            driver.quit()
    except FlowCancelled:
        return cancelled_result()
    except WebDriverException as e:
        failure_notes = f"Browser error: {e}"
        logger.error("Browser failure for proposal id=%s: %s", proposal_id, e)
    except LLMProviderError as e:
        failure_notes = f"LLM provider error (quota or connectivity): {e}"
        logger.warning("LLM failure for proposal id=%s (continuing): %s", proposal_id, e)
    except Exception as e:
        failure_notes = f"Unexpected error: {e}"
        logger.exception("Unexpected failure for proposal id=%s", proposal_id)

    if cancellation_requested():
        return cancelled_result()

    # ── Agent failed ───────────────────────────────────────────────────
    if failure_notes or (trace is None and not (FAST_GENERATION and fast_code)):
        generations_db.complete(
            generation_id=generation_id,
            generation_status="incomplete",
            test_verdict=None,
            script_content=None,
            script_path=None,
            steps_taken=0,
            trace={"steps": None, "generation_context": generation_context,
                   "diagnostics": diagnostics or [failure_notes or "No trace produced"]},
            notes=failure_notes or "No trace produced",
            **({"replace_existing": True} if replace_existing else {}),
        )
        if not replace_existing:
            proposals_db.update_status(proposal_id, "queued")
        return {
            "result": "incomplete",
            "proposal_id": proposal_id,
            "generation_id": generation_id,
            "notes": failure_notes,
            "message": f"Incomplete: {(failure_notes or '')[:80]}",
        }

    # ── Determine generation status ────────────────────────────────────
    if FAST_GENERATION and fast_code:
        gen_status = "success"
        test_verdict = None
    else:
        last_step = trace[-1]
        gen_status = (
            "success"
            if last_step["input"].get("source") == "model"
            else "incomplete"
        )
        test_verdict = None
        if gen_status == "success":
            test_verdict = "pass" if last_step["input"].get("passed") else "fail"

    safe_title = "".join(
        c if c.isalnum() else "_" for c in proposal["title"][:40]
    ).strip("_") or "test"
    # Verification also writes this path: isolate every attempt before it runs.
    script_filename = f"test_{proposal_id}_{safe_title}_generation_{generation_id}.py"
    script_path = str(GENERATED_SCRIPTS_DIR / script_filename)
    script_content = (
        fast_code
        if (FAST_GENERATION and fast_code)
        else compile_trace(trace, f"proposal_{proposal_id}", target_url)
    )

    # ── Phase 3-E: Inject pytest mark based on proposal priority ──────────
    priority = (proposal.get("tags") or {}).get("priority") or "P1"
    script_content = inject_pytest_mark(script_content, severity=priority)
    logger.debug(
        "Injected @pytest.mark.%s to proposal_id=%s", priority, proposal_id
    )

    # ── Verify + self-repair (FAST_GENERATION only) ────────────────────
    if FAST_GENERATION and fast_code and VERIFY_GENERATED:
        if sso_required and precheck_driver:
            # The precheck browser is already authenticated on the app page.
            # Launching a second browser for verification would require a
            # fresh SSO login that reliably fails in a headless subprocess.
            # Static generation checks and auth reachability do not execute
            # the procedure. Keep generation quality and runtime verdict separate.
            logger.info(
                "Proposal %s: SSO app — skipping subprocess verification, "
                "using precheck driver session as proof of auth reachability",
                proposal_id,
            )
            gen_status = "success"
            test_verdict = None
            verify_note = (
                "[runtime_unverified] Generated with static checks only. "
                "SSO precheck confirmed login, not execution of this test."
            )
        else:
            try:
                from service.fast_generation import _ELEMENT_CACHE
                script_content, verified, verify_reason = verify_and_repair(
                    script_path=script_path,
                    code=script_content,
                    provider=provider,
                    target_url=target_url,
                    elements=_ELEMENT_CACHE.get(target_url),
                    extra_env=verify_test_env,
                    enforce_sso_preamble=sso_required,
                    **({'max_repairs': 0} if is_grounded_script(script_content) else {}),
                )
                test_verdict = "pass" if verified else None
                if not verified:
                    gen_status = "incomplete"
                    verify_note = (
                        f"[{verify_reason or 'verify_failed'}] "
                        "Generated but failed verification after its repair budget. "
                        "The script is kept for inspection but is not marked as a working test."
                    )
                    logger.warning(
                        "Proposal %s: script failed verification reason=%s",
                        proposal_id,
                        verify_reason or "verify_failed",
                    )
            except FlowCancelled:
                return cancelled_result()
            except Exception as e:
                if replace_existing:
                    gen_status = "incomplete"
                    test_verdict = None
                    verify_note = f"[verify_error] Replacement verification failed: {e}"
                    logger.exception("Replacement verification failed for proposal id=%s", proposal_id)
                else:
                    logger.warning("Verification step skipped: %s", e)

    if cancellation_requested():
        return cancelled_result()

    if is_grounded_script(script_content):
        scope_note = 'Generated from structured approved steps and captured targets.'
        if final_ui_observation:
            scope_note += ' Final verification repeats the original UI-only field observation; business persistence is not independently verified.'
        verify_note = '\n'.join(note for note in (verify_note, scope_note) if note)

    try:
        Path(script_path).write_text(script_content, encoding="utf-8")
    except Exception as e:
        if not replace_existing:
            raise
        logger.exception("Replacement script write failed for proposal id=%s", proposal_id)
        gen_status = "incomplete"
        test_verdict = None
        script_path = None
        verify_note = "\n".join(note for note in (
            verify_note, f"[script_write_error] Replacement script could not be saved: {e}"
        ) if note)

    try:
        with completion_boundary():
            generations_db.complete(
                generation_id=generation_id,
                generation_status=gen_status,
                test_verdict=test_verdict,
                script_content=script_content,
                script_path=script_path,
                steps_taken=len(trace) if trace else 0,
                trace={"steps": trace, "generation_context": generation_context, "diagnostics": diagnostics},
                notes=(verify_note or
                       (last_step["input"].get("notes", "") if last_step
                        else "Generated in single-shot mode")),
                **({"replace_existing": True} if replace_existing else {}),
            )
            if not replace_existing:
                proposals_db.update_status(
                    proposal_id,
                    "generated" if gen_status == "success" else "queued",
                )
    except FlowCancelled:
        return cancelled_result()

    return {
        "result": "generated" if gen_status == "success" else "incomplete",
        "proposal_id": proposal_id,
        "generation_id": generation_id,
        "script_path": script_path if gen_status == "success" else None,
        "test_verdict": test_verdict,
        "script_content": script_content if gen_status == "success" else None,
        "notes": verify_note or (last_step["input"].get("notes", "") if last_step else ""),
        "message": f"{'Generated' if gen_status == 'success' else 'Incomplete'}: {proposal['title'][:60]}",
    }


# TOKENS & COST: thin wrapper added so every AI call made while generating one script
# (including verify-and-repair) is labelled "script_generation" on the dashboard.
# run_generate_scripts() still calls this name, so nothing else needed to change.
def _generate_one_blocking(proposal: dict, *args, **kwargs) -> dict:
    """
    Labels every AI call made while generating one script, including the
    verify-and-repair attempts, as script generation, then runs the real work.
    Kept as a thin wrapper so the Tokens & Cost dashboard can attribute cost
    without changing the generation logic or its callers.
    """
    with track_llm_usage("script_generation", application_id=proposal.get("application_id")):
        return _generate_one_blocking_impl(proposal, *args, **kwargs)


async def run_generate_scripts(
    application_id: int,
    proposal_ids: list,
    environment_key: str,
    triggered_by: str,
    progress_callback: Callable,
    replace_existing: bool = False,
) -> dict:
    """
    Generate automated test scripts for approved, prioritised proposals.

    Does NOT execute. Returns when all proposals are either:
        - generated successfully (script written to DB and disk), or
        - marked incomplete (API quota exhausted, browser failure, etc.)

    Key behaviour on API quota exhaustion:
        When the LLM free tier limit is hit, the retry logic waits and
        retries up to max_retries times. If all retries are exhausted, the
        proposal is marked 'incomplete' and generation continues with the
        next proposal. The batch never hangs indefinitely.
        Partial results are immediately usable — proposals that succeeded
        before the quota ran out have real, downloadable scripts.

    Args:
        application_id: FK to applications.
        proposal_ids:   List of proposal IDs approved for automation.
        environment_key: Which environment to run against.
        triggered_by:   Identity of who triggered this.
        progress_callback: async callable(event_type, data).
        replace_existing: Keep prior scripts active unless replacement succeeds.

    Returns:
        {"generated": int, "incomplete": int, "results": list}
    """
    from service.workflow_control import checkpoint, cancellation_requested
    checkpoint()
    provider = get_provider()
    application = apps_db.find_by_id(application_id)
    if not application:
        await progress_callback("error", {
            "message": f"Application {application_id} not found."
        })
        return {"error": f"Application {application_id} not found."}

    # Resolve the target URL (base_url wins over config for named envs).
    from service.runner_utils import resolve_target_url
    target_url = resolve_target_url(application, environment_key)
    base_url = (application.get("base_url") or "").strip()
    if base_url and target_url and target_url != base_url:
        logger.warning(
            "URL MISMATCH: generating against %s but application '%s' base_url "
            "is %s. Scripts will be written for the first and run against the "
            "second. Check config/applications.yaml.",
            target_url, application["name"], base_url,
        )
    logger.info(
        "Starting generation: application=%s (%s) env=%s target_url=%s "
        "base_url=%s proposals=%d",
        application_id, application["name"], environment_key, target_url,
        base_url or "(none)", len(proposal_ids),
    )

    sso_precheck = run_sso_precheck(
        application=application,
        phase="generate",
        target_url=target_url,
        triggered_by=triggered_by,
    )
    checkpoint()
    if sso_precheck["enabled"]:
        await progress_callback("status", {"message": "Running SSO precheck before generation..."})
    if sso_precheck["enabled"] and sso_precheck["passed"]:
        await progress_callback("status", {"message": f"SSO precheck passed: {sso_precheck['message']}"})
    if not sso_precheck["passed"]:
        blocked_results = [{
            "proposal_id": pid,
            "result": "blocked",
            "notes": sso_precheck["message"],
        } for pid in proposal_ids]
        await progress_callback("error", {"message": sso_precheck["message"]})
        await progress_callback("complete", {
            "generated": 0,
            "incomplete": 0,
            "blocked": len(proposal_ids),
            "total": len(proposal_ids),
            "results": blocked_results,
            "message": sso_precheck["message"],
        })
        return {
            "generated": 0,
            "incomplete": 0,
            "blocked": len(proposal_ids),
            "results": blocked_results,
        }

    GENERATED_SCRIPTS_DIR.mkdir(exist_ok=True)

    # Determine once whether this application uses SSO — passed to every
    # script generation so the Azure Entra preamble is injected when needed.
    sso_required: bool = app_requires_sso_gateway(application)
    verify_test_env = build_precheck_test_env(application, triggered_by)
    # Keep the live precheck driver for same-browser verification.
    precheck_driver = sso_precheck.get("precheck_driver") if sso_required else None
    if precheck_driver:
        logger.info(
            "generation_runner: precheck driver will be reused for SSO verification (no second login)"
        )
    if sso_required and (
        not verify_test_env.get("TEST_USERNAME")
        or not verify_test_env.get("TEST_PASSWORD")
    ):
        logger.warning(
            "generation_runner: app=%s requires SSO but TEST_USERNAME/"
            "TEST_PASSWORD are missing from verification env",
            application_id,
        )
    logger.info("generation_runner: sso_required=%s for app=%s", sso_required, application_id)

    # Load the pre-crawled element map once for the whole batch.  Script
    # generation uses this instead of scraping the login page, so every
    # generated script sees the full application element set.
    script_map_context: Optional[str] = None
    script_map_data: Optional[dict] = None
    try:
        latest_map = get_latest_map(application_id)
        if latest_map:
            raw_map = latest_map.get("map_data") or latest_map
            script_map_context = format_map_for_scripts(raw_map)
            script_map_data = {**raw_map, 'map_id': latest_map.get('id')}
            logger.info(
                "generation_runner: loaded crawled map for app=%s  chars=%d",
                application_id, len(script_map_context or ""),
            )
        else:
            logger.warning(
                "generation_runner: no crawled map found for app=%s — "
                "script generator will fall back to live-page extraction",
                application_id,
            )
    except Exception as _map_err:  # noqa: BLE001
        logger.warning(
            "generation_runner: failed to load crawled map for app=%s: %s — continuing",
            application_id, _map_err,
        )

    generated = 0
    incomplete = 0
    results = []

    # Pre-fetch all proposals once so each concurrent task has its data.
    all_proposals = proposals_db.list_for_application(application_id)
    proposals_to_run = [
        p for p in all_proposals if p["id"] in set(proposal_ids)
    ]
    # Preserve the caller's ordering.
    id_order = {pid: idx for idx, pid in enumerate(proposal_ids)}
    proposals_to_run.sort(key=lambda p: id_order.get(p["id"], 999))
    from service.generation_policy import _supports_action_grammar
    mapped_proposal_ids = {
        p["id"] for p in proposals_to_run
        if FAST_GENERATION and _supports_action_grammar(p.get("steps"), (p.get("tags") or {}).get("preconditions"))
    }
    candidate_maps = None
    map_load_error = None
    if mapped_proposal_ids:
        try:
            from core.db.queries.generation_maps import list_complete
            candidate_maps = list_complete(application_id)
        except Exception:
            logger.exception("Cannot load complete maps for application=%s", application_id)
            candidate_maps = []
            map_load_error = "Application captures could not be loaded. Retry after checking the capture storage; no fallback map was used."

    total = len(proposals_to_run)
    sem = asyncio.Semaphore(PARALLEL_GENERATION_WORKERS)
    logger.info(
        "generation_runner: running %d proposals with concurrency=%d",
        total, PARALLEL_GENERATION_WORKERS,
    )

    async def _do_one(i: int, proposal: dict) -> None:
        nonlocal generated, incomplete
        async with sem:
            if cancellation_requested():
                return
            proposal_id = proposal["id"]
            await progress_callback("step", {
                "proposal_id": proposal_id,
                "current": i + 1,
                "total": total,
                "message": f"Generating ({i+1}/{total}): {proposal['title'][:60]}",
            })

            # tags.target_flow (not the retired tags.test_data_file) is what
            # accept_proposals() actually stamps on a scenario — see
            # api/routes/run.py's accept_proposals() and _replace_existing().
            # test_data/loader.py resolves per-form overrides by this same
            # target_flow string (environment.forms.<target_flow>), so
            # build_scenario_test_env's third argument must be this value,
            # not a filename. Reading the old tags.test_data_file key here
            # always returned None for any proposal accepted after that
            # migration — not a crash, just a silent fallback to flat
            # app-level data only, for every scenario, regardless of which
            # form it belongs to.
            scenario_test_env = build_scenario_test_env(
                application,
                environment_key,
                (proposal.get("tags") or {}).get("target_flow"),
                triggered_by,
            )
            result = await asyncio.to_thread(
                _generate_one_blocking,
                proposal,
                target_url,
                provider,
                script_map_context,
                script_map_data,
                triggered_by,
                asyncio.get_running_loop(),
                progress_callback,
                scenario_test_env,
                sso_required,
                precheck_driver,
                **({"replace_existing": True} if replace_existing else {}),
                **({"candidate_maps": candidate_maps, "map_load_error": map_load_error}
                   if proposal_id in mapped_proposal_ids else {}),
            )
            if result["result"] == "cancelled":
                results.append(result)
                return
            if result["result"] == "generated":
                generated += 1
                logger.info(
                    "Generated script for proposal id=%s verdict=%s",
                    proposal_id, result.get("test_verdict"),
                )
            else:
                incomplete += 1
            results.append(result)
            await progress_callback("step", {
                "proposal_id": proposal_id,
                "current": i + 1,
                "total": total,
                "message": result.get("message", ""),
            })

    # Await every started worker before stream/cleanup completion, including
    # when a sibling fails. Never mistake a cancelled await for a stopped thread.
    outcomes = await asyncio.gather(
        *[_do_one(i, p) for i, p in enumerate(proposals_to_run)],
        return_exceptions=True,
    )

    # Quit the precheck driver now that all proposals are done.
    if precheck_driver:
        try:
            precheck_driver.quit()
            logger.info("generation_runner: precheck driver closed after batch")
        except Exception as _dq:
            logger.warning("generation_runner: error closing precheck driver: %s", _dq)

    # ── Phase 3-E: Generate conftest.py for the suite ───────────────────
    checkpoint()
    for outcome in outcomes:
        if isinstance(outcome, BaseException):
            raise outcome
    if generated > 0:
        conftest_ok = generate_conftest(GENERATED_SCRIPTS_DIR)
        if conftest_ok:
            await progress_callback("status", {
                "message": "Generated conftest.py for WebDriver pooling"
            })
        else:
            logger.warning(
                "Failed to generate conftest.py; tests may run without pooling"
            )

    summary_message = f"{generated} of {total} test cases generated successfully."
    if incomplete > 0:
        summary_message += (
            f" {incomplete} could not be generated "
            "— see each test case's failure reason before retrying."
        )
    await progress_callback("complete", {
        "generated": generated,
        "incomplete": incomplete,
        "total": total,
        "results": results,
        "message": summary_message,
    })
    return {"generated": generated, "incomplete": incomplete, "results": results}