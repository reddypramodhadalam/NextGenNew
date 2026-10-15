"""
api/routes/run.py
Execution + audit endpoints.
  POST /v1/run/script                    — run ONE script
  POST /v1/run/scenario                  — run ALL scripts under a scenario
  GET  /v1/generations/{id}/history      — full execution history (audit:
                                           how many times run, when, result,
                                           screenshot evidence per run)
  GET  /v1/screenshots/{run_id}/{name}   — serve a screenshot image
Execution uses pytest + Selenium. HEADED=1 shows Chrome visibly and runs
slowly (watchable). Each run captures screenshots as audit evidence.

TEST DATA — read this before touching _test_env_for / run/scenario:
Test data for one application lives in exactly ONE file:
    test_data/applications/app_<id>.json
under environments.<env>, plus an optional environments.<env>.forms block
keyed by target_flow (the plain text typed on the New Test Suite Configure
step, e.g. "Sales Order Entry") for values that must differ per flow (see
test_data/loader.py's target_flow parameter — it merges a matching
forms.<target_flow> entry on top of the flat values, resolving on a shared
key so e.g. "Sales Order Entry" and "Inventory Adjustment" no longer
collide on the same ItemNumber key). _test_env_for() below resolves this
by looking up which target_flow a scenario's own survey used
(tags.target_flow, stamped once at accept time — see accept_proposals),
so every caller in this file gets the right form-specific values
automatically and needs no changes of its own. There is deliberately no
second file and no per-scenario link to maintain.
"""
import ast
import json
import logging
import re
from datetime import datetime, timezone
import os
from pathlib import Path
from typing import Optional
from fastapi import APIRouter, Request
from fastapi.responses import FileResponse
from pydantic import BaseModel
from api.routes.workflows import WorkflowRoute
from service.workflow_control import checkpoint
from core.db.queries import generations as generations_db
from core.db.queries import executions as executions_db
from core.db.queries import proposals as proposals_db
from core.db.queries import users as users_db
from core.db.queries import app_credentials as app_credentials_db
from core.db.queries import ai_calls as ai_calls_db
from service.automation_readiness import evaluate_automation_readiness
from service.selenium_runner import run_script_file
from core.db.connection import get_conn
from test_data.loader import load_test_data
logger = logging.getLogger(__name__)
router = APIRouter(route_class=WorkflowRoute)
# Only used if an application has no base_url recorded.
DEFAULT_TARGET = os.environ.get("NETRA_URL", "https://www.saucedemo.com")
SCREENSHOT_ROOT = os.environ.get("SCREENSHOT_DIR", "screenshots")
def _target_for(generation_id: int, environment: Optional[str] = None,
                override_url: Optional[str] = None) -> tuple[str, str]:
    """
    Resolve where this script should run.
    A test case carries no URL — it is written once and run against dev,
    test, stage or production. The environment supplies the URL, and the
    name is returned so the execution records where it ran.
    Returns:
        (url, environment_label)
    """
    from api.routes.environments import resolve_url
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """SELECT p.application_id
                   FROM generations g
                   JOIN test_case_proposals p ON p.id = g.test_case_proposal_id
                   WHERE g.id = %s""",
                (generation_id,),
            )
            row = cur.fetchone()
    if not row:
        logger.warning("No application for generation %s", generation_id)
        return DEFAULT_TARGET, "unknown"
    url, label = resolve_url(row[0], environment, override_url)
    if not url:
        logger.warning("No URL configured for application %s; using %s",
                       row[0], DEFAULT_TARGET)
        return DEFAULT_TARGET, label
    return url, label
def _application_id_for_generation(generation_id: int) -> Optional[int]:
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """SELECT p.application_id
                   FROM generations g
                   JOIN test_case_proposals p ON p.id = g.test_case_proposal_id
                   WHERE g.id = %s""",
                (generation_id,),
            )
            row = cur.fetchone()
    return row[0] if row else None
def _target_flow_for_generation(generation_id: int) -> Optional[str]:
    """
    Look up which target_flow this scenario was surveyed under, so a
    single-script/single-scenario run resolves the same form-specific
    test data the New Test Suite wizard's batch generate/execute paths do
    (see module docstring).

    The value lives on test_case_proposals.tags.target_flow — stamped
    once, at accept time, from the same target_flow the user typed on the
    Configure step when surveying (see accept_proposals below). Absent
    means this scenario has no flow-specific override; load_test_data()
    then returns flat application-level data only, exactly as before this
    fix — nothing else to configure.
    """
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """SELECT p.tags
                   FROM generations g
                   JOIN test_case_proposals p ON p.id = g.test_case_proposal_id
                   WHERE g.id = %s""",
                (generation_id,),
            )
            row = cur.fetchone()
    if not row or not isinstance(row[0], dict):
        return None
    return row[0].get("target_flow")

def _scenario_text_for_generation(generation_id: int) -> Optional[str]:
    """
    Return the scenario's own title + description + step text.

    Used only as a fallback when tags.target_flow is absent or does not
    match any forms.<name> entry. Imported / Jira / ALM test cases are
    accepted with target_flow=None, so their form-specific test data was
    never resolved even when the steps clearly name the form
    (e.g. "Click on Sales Order Entry"). test_data/loader.py already
    supports matching a form name anywhere inside a longer procedure text.
    """
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """SELECT p.title, p.description, p.steps
                   FROM generations g
                   JOIN test_case_proposals p ON p.id = g.test_case_proposal_id
                   WHERE g.id = %s""",
                (generation_id,),
            )
            row = cur.fetchone()
    if not row:
        return None
    title, description, steps = row
    if isinstance(steps, str):
        try:
            steps = json.loads(steps)
        except ValueError:
            steps = None
    parts = [title or "", description or ""]
    for s in steps or []:
        if isinstance(s, dict):
            parts.append(str(s.get("step") or ""))
            parts.append(str(s.get("expected") or ""))
    text = "\n".join(p for p in parts if p.strip())
    return text or None


def _matched_form_name(app_id: int, environment: Optional[str],
                       flow_text: Optional[str]) -> Optional[str]:
    """
    Return the forms.<name> key this flow text resolves to, or None.

    Uses the loader's own matching rules (exact first, then longest
    case-insensitive substring) so the result is identical to what
    load_test_data() will merge. Read-only; never decrypts anything.
    """
    if not flow_text:
        return None
    try:
        from test_data.loader import _read_app_file, _resolve_form_override, DEFAULT_ENV
        doc = _read_app_file(app_id)
        env_key = (environment or "").strip() or DEFAULT_ENV
        forms = ((doc.get("environments") or {}).get(env_key) or {}).get("forms")
        override = _resolve_form_override(forms, flow_text)
        if not override or not isinstance(forms, dict):
            return None
        for name, value in forms.items():
            if value is override:
                return name
    except Exception as e:  # diagnostic helper only — never block a run
        logger.debug("Form match check failed for app %s: %s", app_id, e)
    return None


def _per_user_credential_env(app_id: int, triggered_by: Optional[str]) -> dict:
    """Return {TEST_USERNAME, TEST_PASSWORD} from the triggering user's OWN
    saved credential for this application, or {} to leave file-based data in
    place.

    The caller's identity is their email (the X-Triggered-By header the
    frontend stamps on every request). Non-user triggers ('ui', 'scheduler',
    'anonymous', or a missing header) contain no '@' and are skipped, so this
    only ever engages for a real signed-in user. Resolution goes through the
    same single doors used everywhere else -- users.get_by_email then
    app_credentials.get_credential(application_id, user_id), scoped by BOTH
    keys -- so one user can never receive another user's credential. Any
    failure degrades silently to the existing file-based behaviour rather than
    breaking a run.
    """
    email = (triggered_by or "").strip()
    if "@" not in email:
        return {}
    try:
        user = users_db.get_by_email(email)
        if not user:
            return {}
        cred = app_credentials_db.get_credential(int(app_id), user["id"])
        if cred and cred.get("username") and cred.get("password"):
            return {"TEST_USERNAME": cred["username"], "TEST_PASSWORD": cred["password"]}
    except Exception as e:  # defensive: never let a credential lookup break a run
        logger.debug("per-user credential overlay skipped for app %s: %s", app_id, e)
    return {}


def _test_env_for(generation_id: int, environment: Optional[str],
                  triggered_by: Optional[str] = None) -> dict:
    """
    Build process env overrides from saved app/environment test data.

    Every key becomes TEST_<UPPER_KEY>. Form-specific values
    (environment.forms.<name>) are merged on top of the flat app-level
    data. The form is chosen by:
      1. tags.target_flow — unchanged, existing behaviour, always tried first.
      2. Fallback ONLY when (1) matches no form: the scenario's own
         title/description/step text. Covers imported / Jira / ALM test
         cases, which are accepted with target_flow=None.
    Apps without a "forms" block behave exactly as before.
    """
    app_id = _application_id_for_generation(generation_id)
    if app_id is None:
        logger.warning(
            "Generation %s has no linked application_id; scoped TEST_* data cannot be resolved.",
            generation_id,
        )
        return {}

    target_flow = _target_flow_for_generation(generation_id)
    flow_for_loader = target_flow
    form_source = "target_flow"
    form_name = _matched_form_name(app_id, environment, target_flow)

    if form_name is None:
        scenario_text = _scenario_text_for_generation(generation_id)
        fallback_form = _matched_form_name(app_id, environment, scenario_text)
        if fallback_form is not None:
            flow_for_loader = scenario_text
            form_name = fallback_form
            form_source = "scenario_text"
        else:
            form_source = "none"

    try:
        data = load_test_data(
            application_id=app_id,
            environment=environment,
            target_flow=flow_for_loader,
        )
    except Exception as e:
        logger.warning("Could not load scoped test data for generation %s: %s", generation_id, e)
        return {}

    out = {}
    for key, value in (data or {}).items():
        if value is None:
            continue
        norm = re.sub(r"[^A-Za-z0-9_]+", "_", str(key).strip().upper())
        norm = re.sub(r"_+", "_", norm).strip("_")
        if not norm:
            continue
        out[f"TEST_{norm}"] = str(value)

    # A per-user credential (saved on the "My login" page) OVERRIDES any
    # shared file-based username/password, but only for the real user who
    # triggered this run -- so each user signs in as themselves. Non-user
    # triggers (scheduler, or a run with no identity) carry no email and leave
    # the file-based values untouched.
    out.update(_per_user_credential_env(app_id, triggered_by))

    # Key names only — never log test-data values.
    logger.info(
        "Test env for generation %s: env=%r target_flow=%r form_source=%r form=%r keys=%s",
        generation_id, environment or "default",
        (target_flow or "")[:80] or None, form_source, form_name, sorted(out.keys()),
    )
    return out

    """
    Build process env overrides from saved app/environment test data.
    Scripts mostly read TEST_USERNAME / TEST_PASSWORD today; mapping all keys
    to TEST_<UPPER_KEY> keeps this generic for future fields.
    Merges this scenario's form-specific overrides (environment.forms.
    <target_flow>) on top of the flat application-level data — see the
    module docstring and _target_flow_for_generation() above. Previously
    this always called load_test_data() with no target_flow, which meant
    two scenarios under the same application sharing a key name (e.g.
    ItemNumber) collided, because only the flat application-level value
    was ever read.
    """
    app_id = _application_id_for_generation(generation_id)
    if app_id is None:
        logger.warning(
            "Generation %s has no linked application_id; scoped TEST_* data cannot be resolved.",
            generation_id,
        )
        return {}
    target_flow = _target_flow_for_generation(generation_id)
    try:
        data = load_test_data(
            application_id=app_id,
            environment=environment,
            target_flow=target_flow,
        )
    except Exception as e:
        logger.warning("Could not load scoped test data for generation %s: %s", generation_id, e)
        return {}
    out = {}
    for key, value in (data or {}).items():
        if value is None:
            continue
        norm = re.sub(r"[^A-Za-z0-9_]+", "_", str(key).strip().upper())
        norm = re.sub(r"_+", "_", norm).strip("_")
        if not norm:
            continue
        env_key = f"TEST_{norm}"
        out[env_key] = str(value)
    return out
class RunScriptRequest(BaseModel):
    generation_id: int
    headed: bool = False
    # Which environment to run against. Omit to use the application's
    # default; supply target_url to point at something one-off.
    environment: Optional[str] = None
    target_url: Optional[str] = None
class RunScenarioRequest(BaseModel):
    proposal_id: int
    headed: bool = False
    environment: Optional[str] = None
    target_url: Optional[str] = None
def _record(gen_id, r, triggered_by: str = "ui"):
    """
    Store an execution result with its evidence.
    Args:
        gen_id: The script that ran.
        r: The runner's result.
        triggered_by: Who or what started it — 'ui' for a person, 'scheduler'
            for a scheduled run. Previously hardcoded to 'ui', which meant
            every scheduled run was recorded as manual and the filter for
            them returned nothing.
    The environment is recorded too: "this test passed" is not an auditable
    statement without saying where it passed.
    """
    executions_db.create(
        generation_id=gen_id,
        result=r["result"],
        output=r["output"],
        duration_ms=r["duration_ms"],
        screenshot_path=r.get("screenshot_dir"),  # folder of per-step shots
        triggered_by=triggered_by,
        environment=r.get("environment"),
        step_results=r.get("step_results"),
    )
@router.post("/run/script")
def run_one(body: RunScriptRequest, request: Request):
    gen = generations_db.get(body.generation_id)
    if not gen or not gen.get("script_path"):
        return {"error": "No script found for this generation."}
    triggered_by = (request.headers.get("X-Triggered-By") or "ui").strip() or "ui"
    logger.info("Running script gen=%s headed=%s triggered_by=%s",
                body.generation_id, body.headed, triggered_by)
    url, env_label = _target_for(body.generation_id, body.environment, body.target_url)
    logger.info("Running generation %s against %s (%s)", body.generation_id, url, env_label)
    r = run_script_file(
        gen["script_path"],
        headed=body.headed,
        target_url=url,
        extra_env=_test_env_for(body.generation_id, body.environment, triggered_by),
    )
    r["environment"] = env_label
    r["target_url"] = url
    _record(body.generation_id, r, triggered_by)
    return r
@router.post("/run/scenario")
def run_scenario(body: RunScenarioRequest, request: Request):
    gens = generations_db.list_for_proposal(body.proposal_id)
    runnable = [g for g in gens if g.get("script_path") and not g.get("is_stale")]
    if not runnable:
        return {"error": "No scripts found for this scenario.", "results": []}
    triggered_by = (request.headers.get("X-Triggered-By") or "ui").strip() or "ui"
    results = []
    for g in runnable:
        url, env_label = _target_for(g["id"], body.environment, body.target_url)
        r = run_script_file(
            g["script_path"],
            headed=body.headed,
            target_url=url,
            extra_env=_test_env_for(g["id"], body.environment, triggered_by),
        )
        r["environment"] = env_label
        _record(g["id"], r, triggered_by)
        results.append({"generation_id": g["id"], **r})
    passed = sum(1 for x in results if x["result"] == "pass")
    return {"total": len(results), "passed": passed, "results": results}
@router.get("/proposals/{proposal_id}/scripts")
def list_scripts(proposal_id: int):
    logger.info("Listing scripts for proposal_id=%s", proposal_id)
    # Retired scripts stay in the database as a record, but showing them
    # beside the current one is confusing — after a regenerate you see two
    # identical-looking rows.
    gens = [g for g in generations_db.list_for_proposal(proposal_id)
            if not g.get("is_stale")]
    proposal = proposals_db.get(proposal_id) or {}
    category = proposal.get("category")
    priority = proposal.get("priority")
    tags = proposal.get("tags") or {}
    target_flow = tags.get("target_flow") if isinstance(tags, dict) else None
    payload = {
        "target_flow": target_flow,
        "scripts": [
            {
                "generation_id": g["id"],
                "title": proposal.get("title") or f"Script {g['id']}",
                "script_path": g.get("script_path"),
                "generation_status": g.get("generation_status"),
                "test_verdict": g.get("test_verdict"),
                "notes": g.get("notes"),
                "test_kind": "ui",
                "category": category,
                "priority": priority,
                "has_script": bool(g.get("script_path")),
            }
            for g in gens
        ]
    }
    logger.info("Listed %s non-stale scripts for proposal_id=%s", len(payload["scripts"]), proposal_id)
    return payload
@router.get("/generations/{generation_id}/history")
def execution_history(generation_id: int):
    """
    Full execution history for audit: every run of this script, newest
    first, with result, timestamp, duration, and its screenshot evidence.
    """
    rows = executions_db.list_for_generation(generation_id)
    history = []
    for row in rows:
        shot_dir = row.get("screenshot_path")
        shots = []
        if shot_dir and Path(shot_dir).exists():
            run_id = Path(shot_dir).name
            shots = [f"/v1/screenshots/{run_id}/{p.name}" for p in sorted(Path(shot_dir).glob("*.png"))]
        # Include the failure output. "failed" with no reason is not an
        # audit trail — the reviewer needs to see what actually happened.
        output = row.get("output") or ""
        history.append({
            "execution_id": row.get("id"),
            "result": row.get("result"),
            "executed_at": str(row.get("executed_at")),
            "duration_ms": row.get("duration_ms"),
            "environment": row.get("environment"),
            "screenshots": shots,
            "output": output[-4000:] if row.get("result") != "pass" else "",
        })
    return {"generation_id": generation_id, "run_count": len(history), "history": history}
@router.get("/screenshots/{run_id}/{name}")
def get_screenshot(run_id: str, name: str):
    """Serve a screenshot image (audit evidence)."""
    # basic path-safety: no traversal
    safe = Path(SCREENSHOT_ROOT) / Path(run_id).name / Path(name).name
    if not safe.exists():
        return {"error": "not found"}
    return FileResponse(str(safe), media_type="image/png")
class ScriptUpdate(BaseModel):
    script_content: str
@router.get("/generations/{generation_id}/script")
def get_script(generation_id: int):
    """
    Return a generated script's source so it can be viewed, edited or
    downloaded from anywhere in the app — not only inside the New Test
    Suite wizard.
    Prefers the file on disk (that is what pytest actually runs) and
    falls back to the copy stored in the database.
    """
    gen = generations_db.get(generation_id)
    if not gen:
        return {"error": "Generation not found."}
    content = gen.get("script_content") or ""
    path = gen.get("script_path")
    if path and Path(path).exists():
        try:
            content = Path(path).read_text(encoding="utf-8")
        except OSError as e:
            logger.warning("Could not read %s: %s", path, e)
    proposal = proposals_db.get(gen.get("test_case_proposal_id")) or {}
    return {
        "generation_id": generation_id,
        "title": proposal.get("title") or f"Script {generation_id}",
        "script_path": path,
        "script_content": content,
        "filename": Path(path).name if path else f"test_{generation_id}.py",
    }
@router.patch("/generations/{generation_id}/script")
def update_script(generation_id: int, body: ScriptUpdate):
    """
    Save an edited script.
    Writes BOTH the file on disk (so the next run uses the edit) and the
    database copy (the audit record of what the script contains).
    """
    gen = generations_db.get(generation_id)
    if not gen:
        return {"error": "Generation not found."}
    content = body.script_content or ""
    try:
        ast.parse(content)
    except SyntaxError as se:
        return {"error": f"Not valid Python — line {se.lineno}: {se.msg}"}
    path = gen.get("script_path")
    if path:
        try:
            Path(path).parent.mkdir(parents=True, exist_ok=True)
            Path(path).write_text(content, encoding="utf-8")
        except OSError as e:
            return {"error": f"Could not write the script file: {e}"}
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """UPDATE generations
                   SET script_content = %s, is_manually_edited = TRUE, edited_at = now()
                   WHERE id = %s""",
                (content, generation_id),
            )
        conn.commit()
    logger.info("Script for generation %s updated manually", generation_id)
    return {"saved": True, "generation_id": generation_id}
@router.post("/generations/{generation_id}/archive")
def archive_script(generation_id: int):
    """
    Archive a script: keep the row and its execution history, but mark it
    stale so it is no longer offered for running.
    Preferred over deletion in a regulated environment — the evidence of
    what ran, and when, is preserved.
    """
    gen = generations_db.get(generation_id)
    if not gen:
        return {"error": "Script not found."}
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("UPDATE generations SET is_stale = TRUE WHERE id = %s", (generation_id,))
        conn.commit()
    logger.info("Archived generation %s", generation_id)
    return {"archived": True, "generation_id": generation_id}
@router.post("/generations/{generation_id}/restore")
def restore_script(generation_id: int):
    """Bring an archived script back into use."""
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("UPDATE generations SET is_stale = FALSE WHERE id = %s", (generation_id,))
        conn.commit()
    return {"restored": True, "generation_id": generation_id}
@router.delete("/generations/{generation_id}")
def delete_script(generation_id: int):
    """
    Permanently delete a script AND its execution history.
    This destroys audit evidence, so the UI must confirm explicitly.
    Archiving is the safer option and is what the UI offers first.
    """
    gen = generations_db.get(generation_id)
    if not gen:
        return {"error": "Script not found."}
    # remove the file too, so a deleted script can't still be picked up
    path = gen.get("script_path")
    if path:
        try:
            Path(path).unlink(missing_ok=True)
        except OSError as e:
            logger.warning("Could not remove %s: %s", path, e)
    with get_conn() as conn:
        with conn.cursor() as cur:
            # executions reference the generation, so they go first
            cur.execute("DELETE FROM executions WHERE generation_id = %s", (generation_id,))
            cur.execute("DELETE FROM healing_proposals WHERE execution_id IN "
                        "(SELECT id FROM executions WHERE generation_id = %s)", (generation_id,))
            cur.execute("DELETE FROM generations WHERE id = %s", (generation_id,))
        conn.commit()
    logger.info("Permanently deleted generation %s and its executions", generation_id)
    return {"deleted": True, "generation_id": generation_id}
@router.post("/proposals/{proposal_id}/archive")
def archive_scenario(proposal_id: int):
    """Archive a scenario and stop offering it for automation."""
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE test_case_proposals SET status = 'archived', updated_at = now() WHERE id = %s",
                (proposal_id,),
            )
        conn.commit()
    logger.info("Archived proposal %s", proposal_id)
    return {"archived": True, "proposal_id": proposal_id}
@router.delete("/proposals/{proposal_id}")
def delete_scenario(proposal_id: int):
    """
    Permanently delete a scenario, every script under it, and all their
    execution history. Destructive — the UI confirms before calling this.
    """
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT id, script_path FROM generations WHERE test_case_proposal_id = %s",
                        (proposal_id,))
            gens = cur.fetchall()
            for _gid, spath in gens:
                if spath:
                    try:
                        Path(spath).unlink(missing_ok=True)
                    except OSError:
                        pass
            # Clear children in FK-safe order. Every table that references
            # test_case_proposals uses the column test_case_proposal_id:
            #   proposal_edits, generations, proposal_data_sets
            cur.execute("""DELETE FROM healing_proposals WHERE execution_id IN
                           (SELECT e.id FROM executions e
                            JOIN generations g ON g.id = e.generation_id
                            WHERE g.test_case_proposal_id = %s)""", (proposal_id,))
            cur.execute("""DELETE FROM executions WHERE generation_id IN
                           (SELECT id FROM generations WHERE test_case_proposal_id = %s)""",
                        (proposal_id,))
            cur.execute("DELETE FROM generations WHERE test_case_proposal_id = %s", (proposal_id,))
            cur.execute("DELETE FROM proposal_edits WHERE test_case_proposal_id = %s", (proposal_id,))
            cur.execute("DELETE FROM proposal_data_sets WHERE test_case_proposal_id = %s", (proposal_id,))
            cur.execute("DELETE FROM test_case_proposals WHERE id = %s", (proposal_id,))
        conn.commit()
    logger.info("Permanently deleted proposal %s with %d script(s)", proposal_id, len(gens))
    return {"deleted": True, "proposal_id": proposal_id, "scripts_removed": len(gens)}
@router.post("/applications/{application_id}/archive")
def archive_application(application_id: int):
    """
    Retire an application without destroying anything.
    Archived applications drop out of the active list but keep every
    scenario, script and execution record — which is what you want when
    a system is decommissioned but its test evidence must be retained.
    """
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("UPDATE applications SET is_archived = TRUE WHERE id = %s", (application_id,))
        conn.commit()
    logger.info("Archived application %s", application_id)
    return {"archived": True, "application_id": application_id}
@router.post("/applications/{application_id}/restore")
def restore_application(application_id: int):
    """Bring an archived application back into the active list."""
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("UPDATE applications SET is_archived = FALSE WHERE id = %s", (application_id,))
        conn.commit()
    return {"restored": True, "application_id": application_id}
@router.delete("/applications/{application_id}")
def delete_application(application_id: int):
    """
    Permanently delete an application and everything beneath it:
    scenarios, scripts, script files, executions and screenshots.
    Destructive and unrecoverable — the UI confirms explicitly and offers
    archiving first.
    """
    removed = {"scenarios": 0, "scripts": 0}
    with get_conn() as conn:
        with conn.cursor() as cur:
            # collect script files so they can be removed from disk too
            cur.execute("""SELECT g.script_path
                           FROM generations g
                           JOIN test_case_proposals p ON p.id = g.test_case_proposal_id
                           WHERE p.application_id = %s""", (application_id,))
            for (spath,) in cur.fetchall():
                if spath:
                    try:
                        Path(spath).unlink(missing_ok=True)
                    except OSError:
                        pass
            # children first — healing -> executions -> generations -> proposals
            cur.execute("""DELETE FROM healing_proposals WHERE execution_id IN
                           (SELECT e.id FROM executions e
                            JOIN generations g ON g.id = e.generation_id
                            JOIN test_case_proposals p ON p.id = g.test_case_proposal_id
                            WHERE p.application_id = %s)""", (application_id,))
            cur.execute("""DELETE FROM executions WHERE generation_id IN
                           (SELECT g.id FROM generations g
                            JOIN test_case_proposals p ON p.id = g.test_case_proposal_id
                            WHERE p.application_id = %s)""", (application_id,))
            cur.execute("""DELETE FROM executions WHERE batch_id IN
                           (SELECT id FROM batches WHERE application_id = %s)""", (application_id,))
            cur.execute("""DELETE FROM generations WHERE test_case_proposal_id IN
                           (SELECT id FROM test_case_proposals WHERE application_id = %s)""",
                        (application_id,))
            cur.execute("""DELETE FROM proposal_edits WHERE test_case_proposal_id IN
                           (SELECT id FROM test_case_proposals WHERE application_id = %s)""",
                        (application_id,))
            cur.execute("""DELETE FROM proposal_data_sets WHERE test_case_proposal_id IN
                           (SELECT id FROM test_case_proposals WHERE application_id = %s)""",
                        (application_id,))
            cur.execute("SELECT COUNT(*) FROM test_case_proposals WHERE application_id = %s",
                        (application_id,))
            removed["scenarios"] = cur.fetchone()[0]
            cur.execute("DELETE FROM test_case_proposals WHERE application_id = %s", (application_id,))
            cur.execute("DELETE FROM batches WHERE application_id = %s", (application_id,))
            cur.execute("DELETE FROM documents WHERE application_id = %s", (application_id,))
            cur.execute("DELETE FROM import_batches WHERE application_id = %s", (application_id,))
            cur.execute("DELETE FROM applications WHERE id = %s", (application_id,))
        conn.commit()
    logger.info("Permanently deleted application %s (%s scenarios)", application_id, removed["scenarios"])
    return {"deleted": True, "application_id": application_id, **removed}
# Deliberately NOT /applications/lookup — that collides with the existing
# /applications/{application_id} route, which tries to parse "lookup" as an
# integer id and returns 422.
@router.get("/application-lookup")
def lookup_application(url: str):
    """
    Check whether an application already exists for a URL, BEFORE a survey
    is started.
    Applications are identified by their normalised URL, so submitting a
    familiar URL with a new name silently reuses the existing application.
    The UI calls this first so it can ask the user what they want instead
    of deciding for them.
    Args:
        url: The URL typed into the Configure step.
    Returns:
        {"exists": bool, "application": {...} | None, "normalized": str}
    """
    from service.context_resolver import normalize_url
    normalized = normalize_url(url) if url else None
    if not normalized:
        return {"exists": False, "application": None, "normalized": None}
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """SELECT a.id, a.name, a.base_url, a.requires_sso_gateway,
                          (SELECT COUNT(*) FROM test_case_proposals p
                            WHERE p.application_id = a.id) AS scenario_count
                   FROM applications a
                   WHERE a.normalized_url = %s AND a.is_archived = FALSE""",
                (normalized,),
            )
            row = cur.fetchone()
    if not row:
        return {"exists": False, "application": None, "normalized": normalized}
    return {
        "exists": True,
        "normalized": normalized,
        "application": {
            "id": row[0], "name": row[1], "base_url": row[2],
            "requires_sso_gateway": row[3],
            "scenario_count": row[4],
        },
    }
class AcceptedStep(BaseModel):
    """
    One step of a test case: an action and what it should produce.
    `n` is deliberately absent — it is assigned server-side from array
    order, so a reviewer reordering steps in the UI cannot desynchronise
    the numbering from the sequence.
    """
    step: str
    expected: Optional[str] = None
    # False when the BRD asked for a control the page does not expose. The
    # step is still kept: a requirement with no matching element is a
    # coverage finding, not something to drop silently.
    matched: bool = True
    element_hint: Optional[str] = None
    # Business key for a typed value (e.g. "customer_po") — present on
    # type_text steps. Optional so it round-trips only when the survey (or
    # the deterministic trace compiler) actually produced one; previously
    # silently dropped here since this model didn't declare it, which
    # forced generation to reinvent test data from scratch. Kept alongside
    # element_hint/matched rather than as a new column, matching how the
    # rest of a step's shape already lives in this same model.
    data_input: Optional[str] = None
    wait_condition: Optional[str] = None
    # "click" | "type_text" | "verify" — set only on steps compiled
    # deterministically from a crawl trace (service.trace_compiler). Absent
    # on ordinary LLM-authored steps; when present it lets script generation
    # transcribe the step directly instead of re-deriving intent from prose.
    action: Optional[str] = None
    # Multi-actor procedures: the name of the identity (e.g. "requester",
    # "approver", "admin" — any name, no fixed list) the script must switch
    # to for this step onward, in the SAME continuous script/run. Present
    # ONLY on the one step that performs the switch; that step must contain
    # NOTHING else (see service/grounded_generation.py's validate_body and
    # service/grounded_runtime.py's _login_as). None for every ordinary
    # step and for the first identity of a script, which is still the
    # existing default username/password via the canonical SSO preamble.
    login_as: Optional[str] = None
class AcceptedProposal(BaseModel):
    title: str
    description: Optional[str] = None
    # Optional so that every existing caller — the import path, and any
    # frontend bundle predating this change — keeps working untouched.
    # When present it is authoritative and description is derived from it.
    steps: Optional[list[AcceptedStep]] = None
    category: Optional[str] = None
    # Whether the requirement STATED this case or the AI INFERRED it from the
    # behaviour type. Optional so older frontend bundles keep working.
    derivation: Optional[str] = None
    priority: Optional[str] = None
    test_type: Optional[str] = "functional"
    scenario: Optional[str] = None
    requirement_id: Optional[str] = None
    # What must be true before step 1 — carried from the survey and
    # persisted in tags so the generator can inject them as preconditions.
    preconditions: Optional[list] = None
    # Whether to generate a script now. A test case is valid whether or not
    # it is automated yet, so this is separate from being accepted.
    should_automate: bool = True
    # What to do when a test case with this title already exists.
    # 'skip'    — leave the existing one alone (default)
    # 'replace' — update it and retire its old script, so a bad earlier
    #             generation can be corrected without losing the test case
    #             or its run history
    on_duplicate: str = "skip"
        # TOKENS & COST: optional id of the survey run these test cases came from. Only used as a key;
    # the cost itself is always looked up on the server and is never taken from the browser.
    survey_run_id: Optional[str] = None
class AcceptProposalsRequest(BaseModel):
    application_id: int
    proposals: list[AcceptedProposal]
    source_document_id: Optional[int] = None
    accepted_by: Optional[str] = "anonymous"
    # When these proposals came from Jira, origin is recorded as 'jira' and
    # each test case captures its source issue's `updated` timestamp
    # (source_updated_at) — the baseline the Stage 3 change-detection poll
    # compares against. The frontend sets these on a Jira-sourced run.
    jira_source: bool = False
    jira_project_key: Optional[str] = None
    # ALM-sourced accepts mirror Jira provenance capture: origin='alm' and
    # source reference data is recorded for audit traceability.
    alm_source: bool = False
    alm_domain: Optional[str] = None
    alm_project: Optional[str] = None
    # The same target_flow string the user typed on the Configure step
    # when surveying (e.g. "Sales Order Entry"). Stamped onto every
    # proposal created from this accept call (tags.target_flow), so a
    # later single-script Run and the batch generate/execute path both
    # resolve the same form-specific test data from
    # test_data/applications/app_<id>.json's "forms" block — with no
    # separate file and no manual linking step. None for imports / Jira /
    # ALM-sourced accepts, which have no target_flow.
    target_flow: Optional[str] = None
def _steps_and_description(p, title: str) -> tuple[Optional[list], str]:
    """
    Normalise a proposal's steps and derive its description from them.
    The rule this enforces: when steps are present, description is a
    PROJECTION of them, never an independent field. A client that sends both
    has its description ignored — otherwise the two could disagree, and
    generation reads description, so it would silently act on the stale half.
    Step numbers are assigned here from array order rather than trusted from
    the client, so reordering in the UI cannot produce gaps or duplicates.
    Args:
        p: An AcceptedProposal.
        title: The cleaned title, used as the description fallback.
    Returns:
        (steps or None, description). steps is None for a one-line test
        case, which is what every pre-006 row is.
    """
    from service.test_case_import import flatten_steps
    raw = [s.model_dump() for s in (p.steps or [])]
    steps = [s for s in raw if (s.get("step") or "").strip()]
    if not steps:
        return None, (p.description or title)
    for i, s in enumerate(steps, 1):
        s["n"] = i
    return steps, (flatten_steps(steps) or title)
def _replace_existing(application_id: int, title: str, p,
                      accepted_by: str = "anonymous",
                      target_flow: Optional[str] = None) -> Optional[dict]:
    """
    Update an existing test case rather than creating a duplicate.
    Used when an earlier generation produced a wrong script: the test case
    and its execution history are kept (that history is the audit evidence),
    its metadata is refreshed, and any existing scripts are marked stale so
    a fresh one can be generated.
    Args:
        accepted_by: Who approved the replacement. Previously hardcoded to
            "anonymous" here, which meant replacing a test case quietly
            erased the attribution that accepting one records — the wrong
            way round, since a replacement is the more consequential act.
        target_flow: See AcceptProposalsRequest.target_flow. Used here ONLY
            as a fallback — if this scenario already has its own
            tags.target_flow (from an earlier accept), that existing value
            is preserved and this parameter is ignored; a re-survey should
            not silently overwrite a value already linked. Only applied
            when no prior value exists.
    Returns:
        The updated proposal row, or None if it could not be found.
    """
    import json as _json
    from datetime import datetime as _dt, timezone as _tz
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """SELECT * FROM test_case_proposals
                   WHERE application_id = %s AND lower(trim(title)) = lower(trim(%s))
                     AND status != 'archived'
                   ORDER BY id DESC LIMIT 1""",
                (application_id, title),
            )
            row = cur.fetchone()
            if not row:
                return None
            cols = [d[0] for d in cur.description]
            proposal = dict(zip(cols, row))
            steps, description = _steps_and_description(p, title)
            preconditions = getattr(p, "preconditions", None) or []
            requested_automation = bool(p.should_automate)
            readiness = evaluate_automation_readiness(
                title=title,
                steps=steps,
                preconditions=preconditions,
                requested=requested_automation,
            )
            should_automate = requested_automation and readiness["eligible_for_automation"]
            if requested_automation and not should_automate:
                logger.warning(
                    "Replacing scenario %s with automation disabled: %s",
                    proposal["id"],
                    "; ".join(readiness["blocked_reasons"]),
                )
            # Preserve an existing target_flow link across a replace —
            # this is metadata about the scenario's identity, not something
            # a re-survey/re-accept should silently drop, unless this call
            # supplies a new one.
            existing_tags = proposal.get("tags") or {}
            existing_target_flow = (
                existing_tags.get("target_flow")
                if isinstance(existing_tags, dict) else None
            )
            tags = {
                "scenario": p.scenario or "General",
                "requirement_id": p.requirement_id,
                "accepted_by": accepted_by,
                "accepted_at": _dt.now(_tz.utc).isoformat(),
                "replaced_at": _dt.now(_tz.utc).isoformat(),
                "target_flow": target_flow or existing_target_flow,
                "step_count": len(steps or []),
                "preconditions": preconditions,
                "automation_readiness": {
                    "eligible_for_automation": readiness["eligible_for_automation"],
                    "blocked_reasons": readiness["blocked_reasons"],
                    "warnings": readiness["warnings"],
                    "metrics": readiness["metrics"],
                },
            }
            cur.execute(
                """UPDATE test_case_proposals
                   SET description = %s, steps = %s, category = %s, tags = %s,
                       should_automate = %s, status = 'proposed', updated_at = now()
                   WHERE id = %s""",
                (description, _json.dumps(steps) if steps else None,
                 p.category, _json.dumps(tags),
                 should_automate, proposal["id"]),
            )
            if p.priority:
                cur.execute(
                    "UPDATE test_case_proposals SET priority = %s WHERE id = %s",
                    (p.priority, proposal["id"]),
                )
            # Retire the old script but keep it and its executions on record.
            cur.execute(
                "UPDATE generations SET is_stale = TRUE WHERE test_case_proposal_id = %s",
                (proposal["id"],),
            )
        conn.commit()
    proposal.update({
        "description": description,
        "steps": steps,
        "category": p.category,
        "priority": p.priority or proposal.get("priority"),
        "tags": tags,
        "should_automate": should_automate,
        "was_replaced": True,
    })
    return proposal
def _attach_survey_cost(proposal: dict, application_id: int, run_id: Optional[str]) -> None:
    """
    Stores this test case's share of its survey run's AI cost.
    Never raises: cost bookkeeping must not block accepting a test case.
    """
    try:
        run = ai_calls_db.get_survey_run(application_id, run_id)
        if not run:
            return
        proposals_db.set_ai_cost(proposal["id"], run["per_case_cost_usd"], run["run_id"])
        proposal["ai_cost_usd"] = str(run["per_case_cost_usd"])
        proposal["survey_run_id"] = run["run_id"]
    except Exception:
        logger.exception("Could not attach survey cost to proposal %s (non-fatal)", proposal.get("id"))

@router.post("/test-suites/accept-proposals")
def accept_proposals(body: AcceptProposalsRequest):
    # For a Jira-sourced run, read each requirement's current `updated`
    # timestamp once, keyed by Jira key, to stamp source_updated_at below.
    # Best-effort: if Jira is unreachable, timestamps are left null and the
    # Stage 3 poll falls back to created_at — the accept must not fail over
    # its own sync bookkeeping.
    _jira_updated_map = {}
    if body.jira_source:
        try:
            from service import jira_adapter
            _cfg = jira_adapter.load_config(project_key=body.jira_project_key)
            _reqs = jira_adapter.fetch_requirements(_cfg)
            _jira_updated_map = jira_adapter.updated_map(_reqs)
        except Exception as _e:
            logger.warning("Could not read Jira updated timestamps at accept: %s", _e)
    """
    Write reviewer-accepted scenarios into the test repository.
    The survey no longer inserts anything: it proposes. This endpoint is
    the single point at which AI output becomes a real test case, and it
    only runs on an explicit human action.
    Why it matters beyond tidiness: in a regulated environment every test
    in the repository must be attributable to a person who accepted it.
    Auto-persisting model output removes that control point.
    Args:
        body: application_id, the accepted proposals, and who accepted them.
    Returns:
        {"created": [proposal rows], "count": int}
    """
    created = []
    skipped = []
    replaced = []
    automation_warnings = []
    # Guard at the point of insert. The Review dialog flags likely duplicates
    # for the reviewer, but accepting twice (two survey runs, both accepted)
    # would still create identical rows. This is the last line of defence.
    existing = set()
    try:
        with get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT lower(trim(title)) FROM test_case_proposals "
                    "WHERE application_id = %s AND status != 'archived'",
                    (body.application_id,),
                )
                existing = {r[0] for r in cur.fetchall()}
    except Exception as e:
        logger.warning("Could not load existing titles for dedup: %s", e)
    for p in body.proposals:
        # Finish a proposal's existing save/audit writes before stopping. Never
        # interrupt between its creation and accepted_by/accepted_at stamp.
        checkpoint()
        title = (p.title or "").strip()
        if not title:
            continue
        if title.lower() in existing:
            if (p.on_duplicate or "skip").lower() != "replace":
                skipped.append(title)
                logger.info("Not accepting duplicate scenario: %s", title[:70])
                continue
            # Replace: keep the test case (and its history) but refresh its
            # metadata and retire the old script, so the next generation
            # produces a clean one. Deleting and recreating would throw away
            # the run history, which is the evidence.
            replaced_row = _replace_existing(body.application_id, title, p,
                                             accepted_by=body.accepted_by or "anonymous",
                                             target_flow=body.target_flow)
            if replaced_row:
                replaced.append(title)
                created.append(replaced_row)
                logger.info("Replacing existing scenario: %s", title[:70])
                continue
            skipped.append(title)
            continue
        existing.add(title.lower())
        steps, description = _steps_and_description(p, title)
        preconditions = getattr(p, "preconditions", None) or []
        requested_automation = bool(p.should_automate)
        readiness = evaluate_automation_readiness(
            title=title,
            steps=steps,
            preconditions=preconditions,
            requested=requested_automation,
        )
        should_automate = requested_automation and readiness["eligible_for_automation"]
        if requested_automation and not should_automate:
            automation_warnings.append({
                "title": title,
                "requirement_id": p.requirement_id,
                "blocked_reasons": readiness["blocked_reasons"],
            })
            logger.warning(
                "Accepted scenario with automation disabled title=%r reasons=%s",
                title[:120],
                "; ".join(readiness["blocked_reasons"]),
            )
        # Jira-sourced runs: origin is 'jira' and each test case records the
        # source issue's `updated` timestamp, keyed by requirement_id (the Jira
        # key). That timestamp is the baseline the Stage 3 poll compares
        # against to detect a changed requirement.
        if body.jira_source:
            _origin = "jira"
            _source_updated_at = _jira_updated_map.get((p.requirement_id or "").strip())
        elif body.alm_source:
            _origin = "alm"
            _source_updated_at = None
        else:
            _origin = "brd" if body.source_document_id else "crawl"
            _source_updated_at = None
        proposal = proposals_db.create(
            application_id=body.application_id,
            title=title[:200],
            description=description,
            source="author_agent",
            test_type=p.test_type or "functional",
            category=p.category,
            source_document_id=body.source_document_id,
            steps=steps,
            # Provenance: the AI's stated/inferred judgment travels from the
            # survey; origin is the real-world source (BRD / crawl / jira).
            derivation=(p.derivation if p.derivation in ("stated", "inferred") else None),
            origin=_origin,
            source_version=None,
            source_updated_at=_source_updated_at,
        )
        if not proposal:
            continue
        # persist the automate decision on the test case
        try:
            with get_conn() as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        "UPDATE test_case_proposals SET should_automate = %s WHERE id = %s",
                        (should_automate, proposal["id"]),
                    )
                conn.commit()
            proposal["should_automate"] = should_automate
        except Exception as e:
            logger.warning("Could not set should_automate on %s: %s", proposal["id"], e)
        if p.priority:
            try:
                proposals_db.update_priority(proposal["id"], p.priority)
                proposal["priority"] = p.priority
            except Exception as e:
                logger.warning("Could not set priority on %s: %s", proposal["id"], e)
        tags = {
            "scenario": p.scenario or "General",
            "requirement_id": p.requirement_id,
            "source": _origin,
            "source_ref": ({
                "type": "alm",
                "domain": (body.alm_domain or "").strip() or None,
                "project": (body.alm_project or "").strip() or None,
            } if body.alm_source else ({
                "type": "jira",
                "project_key": (body.jira_project_key or "").strip() or None,
            } if body.jira_source else None)),
            # Recorded so the audit trail can answer "who put this test in
            # the repository, and when".
            "accepted_by": body.accepted_by,
            "accepted_at": datetime.now(timezone.utc).isoformat(),
            # The flow this scenario was surveyed under (see
            # AcceptProposalsRequest.target_flow) — lets a later Run/Execute
            # resolve the same form-specific test data
            # (test_data/applications/app_<id>.json's "forms" block) the
            # crawl itself already used, with no separate file or manual
            # linking step.
            "target_flow": body.target_flow,
            # Surfaced in tags so list endpoints can show "6 steps" without
            # fetching and parsing the JSONB column for every row.
            "step_count": len(steps or []),
            # Preconditions from the survey — what must be true before step 1.
            # Preserved here so the generator receives them via proposal["tags"].
            "preconditions": preconditions,
            "automation_readiness": {
                "eligible_for_automation": readiness["eligible_for_automation"],
                "blocked_reasons": readiness["blocked_reasons"],
                "warnings": readiness["warnings"],
                "metrics": readiness["metrics"],
            },
        }
        try:
            with get_conn() as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        "UPDATE test_case_proposals SET tags = %s WHERE id = %s",
                        (json.dumps(tags), proposal["id"]),
                    )
                conn.commit()
            proposal["tags"] = tags
        except Exception as e:
            logger.warning("Could not tag proposal %s: %s", proposal["id"], e)
        # TOKENS & COST: attach this test case's share of its survey run's AI cost. ALM imports have no survey run.
        if not body.alm_source:
            _attach_survey_cost(proposal, body.application_id, p.survey_run_id)
        created.append(proposal)
    # Jira-sourced accepts RESOLVE the requirement change: once a human has
    # reviewed and accepted tests for a Jira requirement (via Regenerate, or a
    # scoped/whole survey), its 'requirement_changed' flag is cleared — the
    # review loop's "waiting for a human" condition is now satisfied. This is
    # what makes Regenerate reliably clear the flag regardless of whether the
    # re-surveyed titles happened to match the old ones.
    if body.jira_source:
        req_keys = sorted({(p.requirement_id or "").strip() for p in body.proposals
                           if (p.requirement_id or "").strip()})
        if req_keys:
            try:
                with get_conn() as conn:
                    with conn.cursor() as cur:
                        cur.execute(
                            """UPDATE test_case_proposals
                               SET source_sync_status = 'current', updated_at = now()
                               WHERE application_id = %s AND origin = 'jira'
                                 AND source_sync_status = 'requirement_changed'
                                 AND status != 'archived'
                                 AND tags->>'requirement_id' = ANY(%s)""",
                            (body.application_id, req_keys),
                        )
                        cleared = cur.rowcount
                    conn.commit()
                if cleared:
                    logger.info("Cleared requirement_changed on %d test case(s) for %s",
                                cleared, req_keys)
            except Exception as e:
                logger.warning("Could not clear requirement_changed flags: %s", e)
    logger.info("Accepted %d proposal(s) into application %s by %s (%d duplicate(s) skipped)",
                len(created), body.application_id, body.accepted_by, len(skipped))
    return {
        "created": created,
        "count": len(created),
        "skipped_duplicates": skipped,
        "replaced": replaced,
        "automation_warnings": automation_warnings,
    }
@router.post("/proposals/{proposal_id}/retire-scripts")
def retire_scripts(proposal_id: int):
    """
    Mark every current script for a test case as retired.
    Called before regenerating. The rows and their executions are kept — the
    run history is the audit evidence, and a regenerate should not erase what
    previously ran — but they stop being offered as the current script.
    """
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE generations SET is_stale = TRUE "
                "WHERE test_case_proposal_id = %s AND is_stale = FALSE",
                (proposal_id,),
            )
            retired = cur.rowcount
        conn.commit()
    logger.info("Retired %d script(s) for proposal %s ahead of regeneration",
                retired, proposal_id)
    return {"retired": retired, "proposal_id": proposal_id}

@router.post("/proposals/{proposal_id}/archive")
def archive_proposal(proposal_id: int):
    """
    Retire a test case without destroying anything.
    Archived proposals drop out of the active list but keep every script and
    execution record — which is what you want when a requirement is
    deprecated but its test evidence must be retained.
    """
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("UPDATE test_case_proposals SET status = 'archived' WHERE id = %s",
                        (proposal_id,))
        conn.commit()
    logger.info("Archived proposal %s", proposal_id)    