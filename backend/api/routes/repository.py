"""
api/routes/repository.py

The test repository, across every application.

  GET /v1/repository/test-cases    filtered list plus the counts behind it
  GET /v1/repository/summary       just the counts

The application page answers "tell me about Netra". This answers "tell me
about our testing" — how much of the estate is automated, which P1s have no
script, what came from an import versus what the AI proposed. Those are
questions you cannot answer by clicking through four applications one at a
time.

Read-only. Acting on a selection reuses the endpoints that already exist —
adding to a suite, generating scripts — so there is no second way to do
anything.
"""

import logging
from typing import Optional

from fastapi import APIRouter

from core.db.connection import get_conn

logger = logging.getLogger(__name__)
router = APIRouter()


def _rows(cur):
    cols = [d[0] for d in cur.description]
    return [dict(zip(cols, r)) for r in cur.fetchall()]


@router.get("/repository/test-cases")
def list_test_cases(
    application_id: Optional[int] = None,
    priority: Optional[str] = None,
    automated: Optional[str] = None,      # "yes" | "no"
    source: Optional[str] = None,         # author_agent | bulk_import | brd | crawl | jira | alm
    scenario: Optional[str] = None,
    search: Optional[str] = None,
    limit: int = 500,
):
    """
    Every test case that matches, with enough context to be acted on.

    Args:
        application_id: Restrict to one application.
        priority: P0-P3.
        automated: "yes" for those with a current script, "no" for those without.
        source: Where the test case came from.
        scenario: The feature grouping it belongs to.
        search: Matches the title.
        limit: Safety cap; the UI shows the count so a truncated list is visible.

    Returns:
        {"test_cases": [...], "total": int, "counts": {...}}
    """
    where, params = [], []
    if application_id:
        where.append("p.application_id = %s"); params.append(application_id)
    if priority:
        where.append("p.priority = %s"); params.append(priority)
    if source:
        # Backward compatible filter:
        # - ingestion source: author_agent | bulk_import (column p.source)
        # - provenance source: brd | crawl | jira | alm (column p.origin)
        if source in {"brd", "crawl", "jira", "alm"}:
            where.append("p.origin = %s"); params.append(source)
        else:
            where.append("p.source = %s"); params.append(source)
    if scenario:
        where.append("p.tags->>'scenario' = %s"); params.append(scenario)
    if search:
        where.append("p.title ILIKE %s"); params.append(f"%{search}%")
    if automated == "yes":
        where.append("EXISTS (SELECT 1 FROM generations g WHERE g.test_case_proposal_id = p.id "
                     "AND g.script_path IS NOT NULL AND g.is_stale = FALSE)")
    elif automated == "no":
        where.append("NOT EXISTS (SELECT 1 FROM generations g WHERE g.test_case_proposal_id = p.id "
                     "AND g.script_path IS NOT NULL AND g.is_stale = FALSE)")

    where.append("p.status != 'archived'")
    clause = "WHERE " + " AND ".join(where)

    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                f"""SELECT p.id, p.title, p.priority, p.category, p.source, p.origin,
                           p.tags->>'scenario'       AS scenario,
                           p.tags->>'requirement_id' AS requirement_id,
                           a.id   AS application_id,
                           a.name AS application_name,
                           EXISTS (SELECT 1 FROM generations g
                                    WHERE g.test_case_proposal_id = p.id
                                      AND g.script_path IS NOT NULL
                                      AND g.is_stale = FALSE) AS automated,
                           (SELECT COUNT(*) FROM executions e
                             JOIN generations g2 ON g2.id = e.generation_id
                             WHERE g2.test_case_proposal_id = p.id) AS run_count,
                           (SELECT e.result FROM executions e
                             JOIN generations g3 ON g3.id = e.generation_id
                             WHERE g3.test_case_proposal_id = p.id
                             ORDER BY e.executed_at DESC LIMIT 1) AS last_result
                    FROM test_case_proposals p
                    JOIN applications a ON a.id = p.application_id
                    {clause}
                    ORDER BY a.name, p.priority NULLS LAST, p.title
                    LIMIT %s""",
                tuple(params) + (limit,),
            )
            cases = _rows(cur)

            cur.execute(
                f"""SELECT COUNT(*) FROM test_case_proposals p
                    JOIN applications a ON a.id = p.application_id
                    {clause}""",
                tuple(params),
            )
            total = cur.fetchone()[0]

    return {"test_cases": cases, "total": total, "shown": len(cases)}


@router.get("/repository/summary")
def summary():
    """
    The estate in numbers.

    Deliberately the same shape the dashboard would want: coverage is only
    meaningful next to what is missing, so the automation gap is reported
    rather than left to be worked out from two other figures.
    """
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """SELECT COUNT(*) AS total,
                          COUNT(*) FILTER (
                              WHERE EXISTS (SELECT 1 FROM generations g
                                             WHERE g.test_case_proposal_id = p.id
                                               AND g.script_path IS NOT NULL
                                               AND g.is_stale = FALSE)) AS automated,
                          COUNT(*) FILTER (WHERE p.source = 'bulk_import') AS imported,
                          COUNT(*) FILTER (
                              WHERE p.priority IN ('P0','P1')
                                AND NOT EXISTS (SELECT 1 FROM generations g
                                                 WHERE g.test_case_proposal_id = p.id
                                                   AND g.script_path IS NOT NULL
                                                   AND g.is_stale = FALSE)) AS critical_gap
                   FROM test_case_proposals p
                   WHERE p.status != 'archived'"""
            )
            total, automated, imported, gap = cur.fetchone()

            cur.execute(
                """SELECT a.id, a.name, COUNT(p.id) AS test_cases,
                          COUNT(p.id) FILTER (
                              WHERE EXISTS (SELECT 1 FROM generations g
                                             WHERE g.test_case_proposal_id = p.id
                                               AND g.script_path IS NOT NULL
                                               AND g.is_stale = FALSE)) AS automated
                   FROM applications a
                   LEFT JOIN test_case_proposals p
                          ON p.application_id = a.id AND p.status != 'archived'
                   WHERE a.is_archived = FALSE
                   GROUP BY a.id, a.name
                   ORDER BY a.name"""
            )
            per_app = _rows(cur)

            cur.execute(
                """SELECT DISTINCT p.tags->>'scenario' AS scenario
                   FROM test_case_proposals p
                   WHERE p.status != 'archived' AND p.tags->>'scenario' IS NOT NULL
                   ORDER BY 1"""
            )
            scenarios = [r[0] for r in cur.fetchall()]

    return {
        "total": total,
        "automated": automated,
        "imported": imported,
        "critical_gap": gap,          # P0/P1 with no script — the number that matters
        "coverage_pct": round(100 * automated / total) if total else 0,
        "applications": per_app,
        "scenarios": scenarios,
    }
