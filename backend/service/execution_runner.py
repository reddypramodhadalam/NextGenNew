"""
service/execution_runner.py
Execution Phase Runner
Owns run_execute_scripts() — the third and final step in the test-suite flow.
Responsibilities:
  - Optionally prioritise a gateway (SSO login) test to run first.
  - Run each generated script via pytest and record the result.
  - Block subsequent tests if the gateway test fails.
  - Write one executions row per run (additive, never overwrites history).
"""
import logging
import os
from typing import Callable

from service.workflow_control import checkpoint
from core.db.queries import applications as apps_db
from core.db.queries import executions as executions_db
from core.db.queries import generations as generations_db
from core.db.queries import proposals as proposals_db
from service.auth_gateway import (
    app_requires_sso_gateway, build_precheck_test_env, build_scenario_test_env, phase_is_gated,
)
from service.runner_utils import run_pytest

logger = logging.getLogger(__name__)


async def run_execute_scripts(
    application_id: int,
    generation_ids: list,
    triggered_by: str,
    progress_callback: Callable,
    headed: bool = False,
) -> dict:
    """
    Execute already-generated test scripts via pytest.
    Called separately from generation — only after the user has reviewed
    the generated scripts and explicitly chosen to execute.
    The gateway mechanism (EXECUTION_GATEWAY_MATCH / EXECUTION_GATEWAY_REQUIRED)
    is only active when the application requires SSO and the execute phase is
    gated. For non-SSO apps the env vars are ignored entirely.
    Args:
        application_id: FK to applications (used to find target_url and gateway flag).
        generation_ids: List of generation IDs whose scripts to execute.
        triggered_by:   Identity of who triggered this.
        progress_callback: async callable(event_type, data).
        headed:         Show the browser window while running.
    Returns:
        {"passed": int, "failed": int, "blocked": int, "results": list}
    """
    passed = 0
    failed = 0
    blocked = 0
    results = []
    gateway_match = (os.environ.get("EXECUTION_GATEWAY_MATCH") or "").strip().lower()
    gateway_required = (os.environ.get("EXECUTION_GATEWAY_REQUIRED", "0").strip() == "1")
    application = apps_db.find_by_id(application_id)
    target_url = (application or {}).get("base_url")
    test_env = build_precheck_test_env(application, triggered_by) if application else {}
    logger.info(
        "Executing %d script(s) against %s (headed=%s)",
        len(generation_ids), target_url, headed,
    )
    sso_required = bool(application) and app_requires_sso_gateway(application)
    execute_gated = sso_required and phase_is_gated("execute")
    if not execute_gated:
        gateway_match = ""
        gateway_required = False
    ordered_ids = list(generation_ids)
    gateway_generation_id = None
    if gateway_match:
        for gid in ordered_ids:
            g = generations_db.get(gid)
            path = (g or {}).get("script_path") or ""
            if gateway_match in path.lower():
                gateway_generation_id = gid
                break
        if gateway_generation_id and ordered_ids[0] != gateway_generation_id:
            ordered_ids.remove(gateway_generation_id)
            ordered_ids.insert(0, gateway_generation_id)
            logger.info(
                "Gateway test prioritized first: generation id=%s (match=%s)",
                gateway_generation_id, gateway_match,
            )
        elif gateway_required and gateway_generation_id is None:
            msg = (
                "Execution gateway is required but no selected script matched "
                f"EXECUTION_GATEWAY_MATCH='{gateway_match}'."
            )
            await progress_callback("error", {"message": msg})
            await progress_callback("complete", {
                "passed": 0,
                "failed": 0,
                "blocked": len(ordered_ids),
                "total": len(ordered_ids),
                "results": [],
                "message": msg,
            })
            return {"passed": 0, "failed": 0, "blocked": len(ordered_ids), "results": []}
    for i, generation_id in enumerate(ordered_ids):
        checkpoint()
        # Look up by generation ID, not proposal ID — using get_latest_for_proposal()
        # here was the root cause of "has no script" errors for existing generations.
        generation = generations_db.get(generation_id)
        if not generation or not generation.get("script_path"):
            logger.warning("Generation id=%s has no script, skipping", generation_id)
            continue
        await progress_callback("step", {
            "generation_id": generation_id,
            "current": i + 1,
            "total": len(ordered_ids),
            "message": f"Executing ({i+1}/{len(ordered_ids)})...",
        })
        proposal = proposals_db.get(generation["test_case_proposal_id"])
        # tags.target_flow (not the retired tags.test_data_file) is what
        # accept_proposals() actually stamps on a scenario — see
        # api/routes/run.py's accept_proposals() and _replace_existing().
        # test_data/loader.py resolves per-form overrides by this same
        # target_flow string (environment.forms.<target_flow>), so
        # build_scenario_test_env's third argument must be this value, not
        # a filename.
        target_flow = (proposal.get("tags") or {}).get("target_flow") if proposal else None
        scenario_test_env = build_scenario_test_env(
            application,
            None,  # execute has no environment_key param today; keeps "default"
            target_flow,
            triggered_by,
        )
        # Key names only — never log test-data values.
        logger.info(
            "Execution env for generation id=%s target_flow=%r keys=%s",
            generation_id, (target_flow or "")[:80], sorted(scenario_test_env.keys()),
        )
        exec_result = run_pytest(
            generation["script_path"],
            headed=headed,
            target_url=target_url,
            extra_env=scenario_test_env,
        )
        executions_db.create(
            generation_id=generation_id,
            # Keep this actual attempt without inventing a pass/fail verdict.
            # The existing schema supports skip; output records user cancellation.
            result="skip" if exec_result["result"] == "cancelled" else exec_result["result"],
            output=exec_result["output"],
            duration_ms=exec_result["duration_ms"],
            screenshot_path=exec_result.get("screenshot_dir"),
            triggered_by=triggered_by,
        )
        if exec_result["result"] == "cancelled":
            # Never classify user cancellation as a failed SSO gateway.
            checkpoint()
        if exec_result["result"] == "pass":
            passed += 1
        else:
            failed += 1
        results.append({
            "generation_id": generation_id,
            "result": exec_result["result"],
            "duration_ms": exec_result["duration_ms"],
        })
        if gateway_generation_id == generation_id and exec_result["result"] != "pass":
            remaining = ordered_ids[i + 1:]
            blocked = len(remaining)
            for blocked_id in remaining:
                results.append({
                    "generation_id": blocked_id,
                    "result": "blocked",
                    "duration_ms": 0,
                    "reason": (
                        f"Blocked by failed gateway test generation {gateway_generation_id}"
                    ),
                })
            logger.warning(
                "Gateway test failed (generation id=%s). Blocked %d subsequent execution(s).",
                gateway_generation_id, blocked,
            )
            break
        logger.info(
            "Executed generation id=%s result=%s duration_ms=%d",
            generation_id, exec_result["result"], exec_result["duration_ms"],
        )
    await progress_callback("complete", {
        "passed": passed,
        "failed": failed,
        "blocked": blocked,
        "total": len(generation_ids),
        "results": results,
    })
    return {"passed": passed, "failed": failed, "blocked": blocked, "results": results}
