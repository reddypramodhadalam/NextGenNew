"""
api/routes/reports.py

GET /v1/applications/{id}/report  — per-application coverage report
GET /v1/reports/leadership        — cross-application rollup

These endpoints are stubs — the query implementations are in
core/db/queries/reports.py (marked TODO there).
"""

import logging
from fastapi import APIRouter

logger = logging.getLogger(__name__)
router = APIRouter()


@router.get("/applications/{application_id}/report")
def get_application_report(application_id: int, environment: str = "default"):
    """
    Returns coverage numbers for one application/environment.
    TODO: Wire to core/db/queries/reports.get_application_coverage()
    """
    # PLACEHOLDER — will be replaced with real DB queries
    return {
        "application_id": application_id,
        "environment": environment,
        "coverage": {
            "total_proposed": 0,
            "successfully_generated": 0,
            "generation_coverage_pct": 0,
            "passed": 0,
            "effective_coverage_pct": 0,
        },
    }


@router.get("/reports/leadership")
def get_leadership_report():
    """
    Returns the cross-application coverage rollup for the leadership dashboard.
    TODO: Wire to core/db/queries/reports.get_leadership_rollup()
    """
    # PLACEHOLDER — will be replaced with real DB queries
    return {"applications": []}


# ── Report artefacts ─────────────────────────────────────────────────────
#
# Appended, not rewritten: the two endpoints above are in use by the
# dashboard and there is no reason to touch them.
#
# The distinction these draw is between a dashboard and a report. A
# dashboard answers "what is the state right now" and is consumed on screen.
# A report is parameterised — you choose the scope and the period — and is
# produced to be sent to someone. Every test management tool draws that
# line, and it is why this is not a second dashboard.

from datetime import datetime, timedelta          # noqa: E402
from typing import Optional                       # noqa: E402
from core.db.connection import get_conn           # noqa: E402


def _report_rows(cur):
    cols = [d[0] for d in cur.description]
    return [dict(zip(cols, r)) for r in cur.fetchall()]


@router.get("/reports/traceability")
def traceability(application_id: Optional[int] = None):
    """
    Requirement to test to result to evidence.

    The artefact a regulated environment asks for. Requirements come from the
    tags recorded when a scenario was ACCEPTED, so this reflects what a person
    approved rather than what a document claimed.

    Test cases with no requirement are listed separately rather than dropped.
    An untraced test is a finding, and hiding it would defeat the purpose.
    """
    where, params = ["p.status != 'archived'"], []
    if application_id:
        where.append("p.application_id = %s")
        params.append(application_id)
    clause = "WHERE " + " AND ".join(where)

    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                f"""
                SELECT p.id, p.title, p.priority,
                       p.tags->>'requirement_id' AS requirement_id,
                       p.tags->>'scenario'       AS scenario,
                       p.tags->>'accepted_by'    AS accepted_by,
                       p.tags->>'accepted_at'    AS accepted_at,
                       a.name AS application_name, a.id AS application_id,
                       (SELECT e.result FROM executions e
                         JOIN generations g ON g.id = e.generation_id
                         WHERE g.test_case_proposal_id = p.id
                         ORDER BY e.executed_at DESC LIMIT 1) AS last_result,
                       (SELECT e.executed_at FROM executions e
                         JOIN generations g2 ON g2.id = e.generation_id
                         WHERE g2.test_case_proposal_id = p.id
                         ORDER BY e.executed_at DESC LIMIT 1) AS last_run_at,
                       (SELECT e.environment FROM executions e
                         JOIN generations g3 ON g3.id = e.generation_id
                         WHERE g3.test_case_proposal_id = p.id
                         ORDER BY e.executed_at DESC LIMIT 1) AS environment,
                       (SELECT COUNT(*) FROM executions e
                         JOIN generations g4 ON g4.id = e.generation_id
                         WHERE g4.test_case_proposal_id = p.id) AS run_count,
                       EXISTS (SELECT 1 FROM generations g5
                                WHERE g5.test_case_proposal_id = p.id
                                  AND g5.script_path IS NOT NULL
                                  AND g5.is_stale = FALSE) AS automated
                FROM test_case_proposals p
                JOIN applications a ON a.id = p.application_id
                {clause}
                ORDER BY p.tags->>'requirement_id' NULLS LAST, p.title
                """,
                tuple(params),
            )
            rows = _report_rows(cur)

    traced = [r for r in rows if r["requirement_id"]]
    untraced = [r for r in rows if not r["requirement_id"]]

    grouped: dict = {}
    for r in traced:
        grouped.setdefault(r["requirement_id"], []).append(r)

    requirements = [
        {"requirement_id": req, "tests": tests,
         "verified": any(t["last_result"] == "pass" for t in tests)}
        for req, tests in sorted(grouped.items())
    ]

    return {
        "requirements": requirements,
        "untraced_tests": untraced,
        "counts": {
            "requirements": len(grouped),
            "verified": sum(1 for r in requirements if r["verified"]),
            "unverified": sum(1 for r in requirements if not r["verified"]),
            "tests_traced": len(traced),
            "tests_untraced": len(untraced),
        },
        "generated_at": datetime.now().isoformat(),
    }


@router.get("/reports/executions")
def execution_report(application_id: Optional[int] = None, days: int = 30,
                     environment: Optional[str] = None):
    """
    Everything that ran in a period, with where and what started it.

    The report a team sends after a release.
    """
    since = datetime.now() - timedelta(days=days)
    where, params = ["e.executed_at >= %s"], [since]
    if application_id:
        where.append("p.application_id = %s"); params.append(application_id)
    if environment:
        where.append("e.environment = %s"); params.append(environment)
    clause = "WHERE " + " AND ".join(where)

    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                f"""SELECT e.id, e.result, e.executed_at, e.duration_ms,
                           e.triggered_by, e.environment,
                           p.title AS test_title, p.priority,
                           p.tags->>'requirement_id' AS requirement_id,
                           p.tags->>'scenario' AS scenario,
                           a.name AS application_name
                    FROM executions e
                    JOIN generations g ON g.id = e.generation_id
                    JOIN test_case_proposals p ON p.id = g.test_case_proposal_id
                    JOIN applications a ON a.id = p.application_id
                    {clause}
                    ORDER BY e.executed_at DESC""",
                tuple(params),
            )
            runs = _report_rows(cur)

    passed = sum(1 for r in runs if r["result"] == "pass")
    scheduled = sum(1 for r in runs if r["triggered_by"] == "scheduler")
    return {
        "runs": runs,
        "period": {"days": days, "from": since.isoformat(),
                   "to": datetime.now().isoformat()},
        "counts": {"total": len(runs), "passed": passed,
                   "failed": len(runs) - passed,
                   "pass_rate_pct": round(100 * passed / len(runs)) if runs else None,
                   "scheduled": scheduled, "manual": len(runs) - scheduled},
        "generated_at": datetime.now().isoformat(),
    }


@router.get("/reports/coverage")
def coverage_report():
    """
    Coverage per application, with the automation gap stated beside it.

    A coverage percentage alone invites the wrong conclusion. How many
    critical tests have no script is the number worth acting on, so it is
    reported rather than left to be inferred.
    """
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT a.id, a.name, a.base_url,
                       COUNT(p.id) AS test_cases,
                       COUNT(p.id) FILTER (
                           WHERE EXISTS (SELECT 1 FROM generations g
                                          WHERE g.test_case_proposal_id = p.id
                                            AND g.script_path IS NOT NULL
                                            AND g.is_stale = FALSE)) AS automated,
                       COUNT(p.id) FILTER (
                           WHERE p.priority IN ('P0','P1')
                             AND NOT EXISTS (SELECT 1 FROM generations g
                                              WHERE g.test_case_proposal_id = p.id
                                                AND g.script_path IS NOT NULL
                                                AND g.is_stale = FALSE)) AS critical_gap,
                       COUNT(p.id) FILTER (WHERE p.source = 'bulk_import') AS imported,
                       COUNT(DISTINCT p.tags->>'requirement_id')
                           FILTER (WHERE p.tags->>'requirement_id' IS NOT NULL) AS requirements
                FROM applications a
                LEFT JOIN test_case_proposals p
                       ON p.application_id = a.id AND p.status != 'archived'
                WHERE a.is_archived = FALSE
                GROUP BY a.id, a.name, a.base_url
                ORDER BY a.name
                """
            )
            apps = _report_rows(cur)

    for a in apps:
        a["coverage_pct"] = (round(100 * a["automated"] / a["test_cases"])
                             if a["test_cases"] else 0)
    return {
        "applications": apps,
        "totals": {"test_cases": sum(a["test_cases"] for a in apps),
                   "automated": sum(a["automated"] for a in apps),
                   "critical_gap": sum(a["critical_gap"] for a in apps),
                   "imported": sum(a["imported"] for a in apps)},
        "generated_at": datetime.now().isoformat(),
    }


# ── AI token usage and cost ──────────────────────────────────────────────
# Feeds the dashboard's Tokens & Cost card and the cost labels on the Applications page.
from decimal import Decimal                                  # noqa: E402
from fastapi import Query                                    # noqa: E402
from core.db.queries import ai_calls as ai_calls_db          # noqa: E402
from core.db.queries import reports as reports_db            # noqa: E402


@router.get("/reports/ai-usage")
def ai_usage_report(
    days: int = Query(30, ge=1, le=365, description="Look-back window in days"),
    application_id: Optional[int] = Query(None, ge=1, description="Limit to one application"),
):
    """Token usage (input/output) and cost for each AI activity."""
    return reports_db.get_ai_usage_summary(days=days, application_id=application_id)


@router.get("/reports/ai-cost/items")
def ai_cost_items(application_id: int = Query(..., ge=1, description="Application to price")):
    """
    test_cases: estimated share of the survey run that produced each test case (keyed by proposal id).
    scripts: exact cost of generating each test case's script, including repairs and regenerations.
    Items generated before cost tracking are left out so the UI can show "-" instead of a false $0.
    """
    test_cases = {
        pid: {"cost_usd": cost, "estimated": True}
        for pid, cost in ai_calls_db.get_test_case_costs(application_id).items()
    }

    # Adds up every generation of the same test case, so regenerating a script shows the running total.
    scripts = {}
    for r in ai_calls_db.get_script_costs(application_id):
        entry = scripts.setdefault(str(r["proposal_id"]), {"cost_usd": Decimal("0"), "generations": []})
        entry["cost_usd"] += Decimal(r["cost_usd"])
        entry["generations"].append({"generation_id": r["generation_id"], "cost_usd": r["cost_usd"]})
    for entry in scripts.values():
        entry["cost_usd"] = str(entry["cost_usd"])   # string keeps the exact value

    return {"application_id": application_id, "test_cases": test_cases, "scripts": scripts}