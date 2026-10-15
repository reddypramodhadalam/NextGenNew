"""
api/routes/test_suites.py

Routes for the new test suite flow.

Three separate operations, each streaming progress via SSE:

    POST /v1/test-suites/survey    — survey URL/document, propose scenarios
                                     Returns for human review before anything
                                     is generated. No Explorer Agent here.

    POST /v1/test-suites/generate  — generate scripts for approved proposals.
                                     Does NOT execute. If API quota runs out,
                                     marks remaining proposals incomplete and
                                     returns partial results immediately —
                                     never hangs.

    POST /v1/test-suites/execute   — execute already-generated scripts via
                                     pytest. Separate explicit user action.

Client-disconnect handling: every long-running operation below is dispatched
as a detached asyncio.create_task(run()), decoupled from the HTTP request by
design (see api/sse.py) so that progress can be streamed via SSE while the
work continues in the background. Previously nothing in that detached task
had any way to learn that the original client had gone away (browser tab
closed, "network error" from a dropped connection, VPN blip, proxy idle
timeout, etc.) — the task simply ran to its own natural completion (the
crawl finishing, hitting its page/step limits, or a provider failure),
silently continuing to make paid LLM calls with no one able to see the
result. Each handler below now starts a lightweight background watcher that
polls request.is_disconnected() and sets a threading.Event the moment the
client is gone; that event's plain, thread-safe `.is_set` method is passed
down as `cancel_check` to the orchestrator, which threads it through to the
one place expensive, repeated work actually happens (agents/crawl_agent.py's
step loop). Passing None (the previous default everywhere) reproduces the
exact old behaviour — this is purely additive.
"""
import asyncio
import logging
import os
import shutil
import tempfile
import threading
from typing import Optional

from fastapi import APIRouter, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import StreamingResponse

from api.sse import event_stream, make_progress_queue, queue_to_stream
from api.routes.workflows import WorkflowRoute, router as workflow_router
from service import jira_sync
from service.orchestrator import (
    run_new_test_suite,
    run_generate_scripts,
    run_execute_scripts,
)

logger = logging.getLogger(__name__)

router = APIRouter(route_class=WorkflowRoute)
router.include_router(workflow_router)

# How often the disconnect watcher polls request.is_disconnected(). Kept
# short relative to typical LLM call durations so a dropped client is
# noticed promptly, without adding meaningful overhead (is_disconnected()
# is a cheap check against the ASGI transport, not a network round trip).
_DISCONNECT_POLL_SECONDS = float(os.environ.get("DISCONNECT_POLL_SECONDS", "2"))


def _start_disconnect_watcher(request: Request) -> tuple[threading.Event, "asyncio.Task"]:
    """
    Starts a background task that watches for the client disconnecting and
    signals a threading.Event the moment it does.

    threading.Event (not asyncio.Event) is used deliberately: the orchestrator
    work this feeds into runs partly inside worker threads (via
    asyncio.to_thread — see service/survey_runner.py), and threading.Event is
    the primitive explicitly designed to be read/set safely across threads.
    Its .is_set method takes no arguments and never raises, making it a safe
    cancel_check callable to hand to code that has no other knowledge of
    HTTP requests, event loops, or async at all (e.g. agents/crawl_agent.py).

    Args:
        request: The incoming Request for this operation.

    Returns:
        (cancelled_event, watcher_task) — the caller should pass
        cancelled_event.is_set as cancel_check to the orchestrator, and
        should cancel watcher_task once the operation's own run() coroutine
        has finished (successfully or not), so the watcher does not outlive
        the request it was created for.
    """
    cancelled = threading.Event()

    async def _watch():
        try:
            while not cancelled.is_set():
                if await request.is_disconnected():
                    cancelled.set()
                    logger.info(
                        "Client disconnected for %s %s — signalling background work to stop",
                        request.method, request.url.path,
                    )
                    break
                await asyncio.sleep(_DISCONNECT_POLL_SECONDS)
        except Exception as e:
            # A broken watcher must never affect the operation itself —
            # worst case, cancel_check simply never fires and behaviour
            # reverts to exactly what it was before this feature existed.
            logger.debug("Disconnect watcher ended (non-fatal): %s", e)

    return cancelled, asyncio.create_task(_watch())


def _stop_disconnect_watcher(watcher_task: "asyncio.Task") -> None:
    """Cancels the watcher task; safe to call even if it already finished."""
    if not watcher_task.done():
        watcher_task.cancel()


@router.post("/test-suites/survey")
async def survey(
    request: Request,
    application_name: str = Form(..., description="Display name for the application"),
    base_url: Optional[str] = Form(None, description="Live URL to survey"),
    requires_sso_gateway: Optional[bool] = Form(
        None, description="Whether this application requires SSO gateway prechecks."
    ),
    include_sso_scenarios: bool = Form(
        True,
        description=(
            "When True (default) and SSO is enabled, inject extra SSO/authentication "
            "scenarios into the survey output. Set False for functional/regression "
            "surveys that should not include authentication test cases."
        ),
    ),
    test_type: Optional[str] = Form(
        "functional",
        description="Type of test cases to generate: 'functional' or 'regression'.",
    ),
    document: Optional[UploadFile] = File(None, description="PRD/BRD/FRD document"),
    document_type: Optional[str] = Form(
        None, description="'requirements' or 'existing_test_cases'"
    ),
    target_flow: Optional[str] = Form(
        None,
        description=(
            "Optional specific navigation target to crawl toward (e.g. "
            "'Sales > Sales Order Entry > Standard Order SO'), used only when "
            "no document is uploaded. Without this, the crawl explores broadly."
        ),
    ),
):
    """
    Surveys a URL and/or processes an uploaded document to propose scenarios.

    Streams live progress via SSE while the Author Agent runs.
    Returns proposed scenarios for human review and prioritisation.
    Generation does NOT happen here — this is step 1 only.

    The final SSE event is type='complete' with:
        application_id: int
        proposals: list of proposal dicts
        message: summary string

    Returns:
        SSE stream (text/event-stream)

    Raises:
        400: If neither a URL nor a document is provided.
    """
    if not base_url and not document:
        raise HTTPException(
            status_code=400,
            detail={
                "code": "MISSING_INPUT",
                "message": "Provide at least a URL or a document to survey.",
            },
        )

    triggered_by = request.headers.get("X-Triggered-By", "anonymous")
    logger.info(
        "Survey request: application=%s url=%s triggered_by=%s",
        application_name, base_url, triggered_by,
    )

    # Save the uploaded document to a temp file if provided.
    # The temp dir is cleaned up after the stream closes.
    document_path = None
    temp_dir = None
    if document:
        temp_dir = tempfile.mkdtemp()
        document_path = os.path.join(temp_dir, document.filename)
        with open(document_path, "wb") as f:
            shutil.copyfileobj(document.file, f)
        logger.info(
            "Saved uploaded document: name=%s type=%s path=%s",
            document.filename, document_type, document_path,
        )

    queue, progress_callback = make_progress_queue()
    cancelled_event, watcher_task = _start_disconnect_watcher(request)

    async def run():
        try:
            if temp_dir:
                from service.workflow_control import track_cleanup
                track_cleanup(lambda: shutil.rmtree(temp_dir) if os.path.exists(temp_dir) else None)
            await run_new_test_suite(
                application_name=application_name,
                base_url=base_url,
                requires_sso_gateway=requires_sso_gateway,
                include_sso_scenarios=include_sso_scenarios,
                test_type=test_type or "functional",
                document_path=document_path,
                document_type=document_type,
                triggered_by=triggered_by,
                progress_callback=progress_callback,
                target_flow=target_flow,
                cancel_check=cancelled_event.is_set,
            )
        except Exception as e:
            logger.exception("Survey pipeline failed")
            await progress_callback("error", {"message": str(e)})
        finally:
            _stop_disconnect_watcher(watcher_task)
            if temp_dir:
                shutil.rmtree(temp_dir, ignore_errors=True)
                logger.debug("Cleaned up temp dir: %s", temp_dir)

    from service.workflow_control import current_operation, start_operation
    if current_operation():
        try:
            start_operation(run, progress_callback)
        except BaseException:
            _stop_disconnect_watcher(watcher_task)
            if temp_dir:
                shutil.rmtree(temp_dir, ignore_errors=True)
            raise
    else:
        asyncio.create_task(run())

    return StreamingResponse(
        event_stream(queue_to_stream(queue)),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@router.post("/test-suites/generate")
async def generate_scripts(
    request: Request,
    application_id: int = Form(..., description="Application to generate tests for"),
    proposal_ids: str = Form(
        ..., description="Comma-separated proposal IDs approved for automation"
    ),
    environment_key: str = Form(
        default="default", description="Environment to run the Explorer Agent against"
    ),
    replace_existing: bool = Form(
        False, description="Keep existing scripts until a replacement succeeds"
    ),
):
    """
    Generates automated test scripts for approved, prioritised proposals.

    Does NOT execute — scripts are written to the database and disk,
    then returned for the user to review before execution.

    If the API quota runs out mid-batch, proposals that failed are marked
    'incomplete' and the endpoint returns partial results immediately.
    Already-generated scripts remain usable and downloadable.

    The final SSE event is type='complete' with:
        generated: int — successfully generated
        incomplete: int — failed (quota, browser error, etc.)
        results: list of per-proposal result dicts

    Returns:
        SSE stream (text/event-stream)

    Raises:
        400: If proposal_ids cannot be parsed as integers.
        400: If no proposal IDs are provided.
    """
    triggered_by = request.headers.get("X-Triggered-By", "anonymous")
    try:
        ids = [int(x.strip()) for x in proposal_ids.split(",") if x.strip()]
    except ValueError:
        # A temporary id ("proposed_1") means the caller skipped the accept
        # step. Survey now only PROPOSES; scenarios must be accepted via
        # /v1/test-suites/accept-proposals before they can be generated.
        # Say so explicitly rather than returning a bare 400 that gives the
        # user nothing to act on.
        looks_unaccepted = "proposed_" in proposal_ids
        raise HTTPException(
            status_code=400,
            detail={
                "code": "UNACCEPTED_PROPOSALS" if looks_unaccepted else "INVALID_IDS",
                "message": (
                    "These scenarios have not been saved yet. The survey only "
                    "proposes them; they must be accepted first "
                    "(POST /v1/test-suites/accept-proposals) before scripts can "
                    "be generated. If you are seeing this in the UI, the "
                    "frontend is out of date — copy the latest "
                    "app/new-test-suite/page.js."
                    if looks_unaccepted else
                    "proposal_ids must be comma-separated integers."
                ),
            },
        )
    if not ids:
        raise HTTPException(
            status_code=400,
            detail={"code": "NO_PROPOSALS", "message": "No proposal IDs provided."},
        )
    logger.info(
        "Generate request: application=%s proposals=%s env=%s triggered_by=%s",
        application_id, ids, environment_key, triggered_by,
    )
    queue, progress_callback = make_progress_queue()
    cancelled_event, watcher_task = _start_disconnect_watcher(request)

    async def run():
        try:
            # NOTE: cancel_check is intentionally NOT passed to
            # run_generate_scripts here. Unlike run_new_test_suite
            # (survey_runner.py, patched alongside this file), the
            # generation pipeline lives in service/generation_runner.py,
            # which was not available to verify/patch in this change. Adding
            # an unsupported keyword argument to a function whose current
            # signature is unknown risks a hard TypeError on every generate
            # call -- worse than the problem being fixed. The disconnect
            # watcher above still runs (harmless on its own) so wiring this
            # through is a single-line change once that file is available.
            await run_generate_scripts(
                application_id=application_id,
                proposal_ids=ids,
                environment_key=environment_key,
                triggered_by=triggered_by,
                progress_callback=progress_callback,
                **({"replace_existing": True} if replace_existing is True else {}),
            )
        except Exception as e:
            logger.exception("Generation pipeline failed")
            await progress_callback("error", {"message": str(e)})
        finally:
            _stop_disconnect_watcher(watcher_task)

    from service.workflow_control import current_operation, start_operation
    if current_operation():
        start_operation(run, progress_callback)
    else:
        asyncio.create_task(run())

    return StreamingResponse(
        event_stream(queue_to_stream(queue)),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@router.post("/test-suites/execute")
async def execute_scripts(
    request: Request,
    application_id: int = Form(...),
    generation_ids: str = Form(
        ..., description="Comma-separated generation IDs to execute"
    ),
    headed: bool = Form(
        False, description="Show the browser while the tests run"
    ),
):
    """
    Executes already-generated test scripts via pytest.

    This is a separate, explicit user action — called only after the user
    has reviewed the generated scripts and chosen to proceed.

    The final SSE event is type='complete' with:
        passed: int
        failed: int
        results: list of per-execution dicts

    Returns:
        SSE stream (text/event-stream)

    Raises:
        400: If generation_ids cannot be parsed.
    """
    triggered_by = request.headers.get("X-Triggered-By", "anonymous")
    try:
        ids = [int(x.strip()) for x in generation_ids.split(",") if x.strip()]
    except ValueError:
        raise HTTPException(
            status_code=400,
            detail={
                "code": "INVALID_IDS",
                "message": "generation_ids must be comma-separated integers.",
            },
        )
    if not ids:
        raise HTTPException(
            status_code=400,
            detail={"code": "NO_GENERATIONS", "message": "No generation IDs provided."},
        )
    logger.info(
        "Execute request: application=%s generations=%s triggered_by=%s",
        application_id, ids, triggered_by,
    )
    queue, progress_callback = make_progress_queue()
    cancelled_event, watcher_task = _start_disconnect_watcher(request)

    async def run():
        try:
            # NOTE: cancel_check is intentionally NOT passed here either --
            # same reason as /test-suites/generate above: execution_runner.py
            # (service/execution_runner.py) was not available to verify/patch
            # in this change, so its current run_execute_scripts signature is
            # unknown. The disconnect watcher still runs; wiring cancel_check
            # through is a one-line addition once that file is provided.
            await run_execute_scripts(
                application_id=application_id,
                generation_ids=ids,
                triggered_by=triggered_by,
                progress_callback=progress_callback,
                headed=headed,
            )
        except Exception as e:
            logger.exception("Execution pipeline failed")
            await progress_callback("error", {"message": str(e)})
        finally:
            _stop_disconnect_watcher(watcher_task)

    from service.workflow_control import current_operation, start_operation
    if current_operation():
        start_operation(run, progress_callback)
    else:
        asyncio.create_task(run())

    return StreamingResponse(
        event_stream(queue_to_stream(queue)),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@router.post("/test-suites/survey-from-jira")
async def survey_from_jira(
    request: Request,
    application_name: str = Form(..., description="Display name for the application"),
    base_url: str = Form(..., description="Live URL to survey the requirements against"),
    requires_sso_gateway: Optional[bool] = Form(
        None, description="Whether this application requires SSO gateway prechecks."
    ),
    jira_project_key: Optional[str] = Form(
        None, description="Jira project key. Defaults to JIRA_PROJECT_KEY from settings."
    ),
    jira_issue_key: Optional[str] = Form(
        None, description="Scope the survey to a single Jira issue (e.g. KAN-5) "
                          "instead of the whole project. Used by Regenerate / "
                          "Generate-tests for one requirement."
    ),
):
    """
    Reads requirements from Jira and surveys them against a live URL to
    propose scenarios — the Jira equivalent of /test-suites/survey.

    Requirements are pulled from Jira by the orchestrator (jira_source=True),
    flattened to the same BRD-equivalent text an uploaded document produces,
    and run through the identical survey/proposal pipeline. Generation does
    NOT happen here — this is step 1 only, exactly like the document survey.

    Streams live progress via SSE. The final event is type='complete' with
    application_id, proposals, and a summary message.

    Returns:
        SSE stream (text/event-stream)

    Raises:
        400: If no URL is provided (a URL is still required to survey against).
    """
    if not base_url:
        raise HTTPException(
            status_code=400,
            detail={
                "code": "MISSING_INPUT",
                "message": "Provide a URL to survey the Jira requirements against.",
            },
        )
    triggered_by = request.headers.get("X-Triggered-By", "anonymous")
    queue, progress_callback = make_progress_queue()
    cancelled_event, watcher_task = _start_disconnect_watcher(request)

    async def run():
        try:
            await run_new_test_suite(
                application_name=application_name,
                base_url=base_url,
                requires_sso_gateway=requires_sso_gateway,
                document_path=None,
                document_type=None,
                triggered_by=triggered_by,
                progress_callback=progress_callback,
                jira_source=True,
                jira_project_key=jira_project_key,
                jira_issue_key=jira_issue_key,
                cancel_check=cancelled_event.is_set,
            )
        except Exception as e:
            logger.exception("Jira survey pipeline failed")
            await progress_callback("error", {"message": str(e)})
        finally:
            _stop_disconnect_watcher(watcher_task)

    from service.workflow_control import current_operation, start_operation
    if current_operation():
        start_operation(run, progress_callback)
    else:
        asyncio.create_task(run())

    return StreamingResponse(
        event_stream(queue_to_stream(queue)),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@router.post("/jira/sync")
async def jira_sync_now(
    request: Request,
    application_id: int = Form(..., description="Application whose Jira-sourced tests to check"),
    jira_project_key: Optional[str] = Form(
        None, description="Jira project key. Defaults to JIRA_PROJECT_KEY from settings."
    ),
):
    """
    Run one Jira change-detection pass on demand (the engine a nightly
    scheduler will call). Queries Jira for requirements updated since the last
    sync, flags the linked test cases as 'requirement_changed' (flag-only —
    never regenerated), and reports new requirements that have no tests yet.

    Returns a JSON summary with: project_key, since, checked_at,
    changed_requirements, flagged_cases (id+title), and new_requirements.
    """
    try:
        return jira_sync.sync_application(
            application_id=application_id,
            project_key=jira_project_key,
        )
    except RuntimeError as e:
        raise HTTPException(
            status_code=400,
            detail={"code": "JIRA_NOT_CONFIGURED", "message": str(e)},
        )
    except Exception as e:
        logger.exception("Jira sync failed")
        raise HTTPException(
            status_code=502,
            detail={"code": "JIRA_SYNC_FAILED", "message": str(e)},
        )


@router.get("/jira/pending")
def jira_pending(application_id: int, jira_project_key: Optional[str] = None):
    """
    The standing Jira review backlog for an application.

    Every test case still flagged 'requirement_changed' (grouped by its Jira
    requirement, with titles), plus Jira requirements that still have no tests.
    This is the CURRENT state, not a single check's delta — a flag persists here
    until a human clears it via /test-cases/{id}/review — so the review screen
    shows what genuinely still needs attention, and a change cannot appear once
    and then silently vanish on the next check.

    Returns a JSON object with: application_id, changed, new, changed_count,
    new_count, jira_error.
    """
    try:
        return jira_sync.list_pending(application_id, project_key=jira_project_key)
    except RuntimeError as e:
        raise HTTPException(
            status_code=400,
            detail={"code": "JIRA_NOT_CONFIGURED", "message": str(e)},
        )
    except Exception as e:
        logger.exception("Jira pending lookup failed")
        raise HTTPException(
            status_code=502,
            detail={"code": "JIRA_PENDING_FAILED", "message": str(e)},
        )


@router.post("/test-cases/{proposal_id}/review")
def review_test_case(proposal_id: int):
    """
    Mark a flagged test case as reviewed by a human.

    Clears its 'requirement_changed' flag (source_sync_status back to 'current')
    and removes it from the review backlog. This is the one human action that
    resolves a detected change; the sync never clears a flag on its own, so
    nothing leaves the backlog without a person acting.

    Returns: {"reviewed": bool, "id": int, "title": str | None}.
    """
    return jira_sync.mark_reviewed(proposal_id)
