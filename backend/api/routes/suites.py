"""
api/routes/suites.py

Test suites — a named, curated selection of existing test cases.

  GET    /v1/applications/{id}/suites
  POST   /v1/applications/{id}/suites
  GET    /v1/suites/{suite_id}
  PATCH  /v1/suites/{suite_id}
  DELETE /v1/suites/{suite_id}
  PUT    /v1/suites/{suite_id}/members
  GET    /v1/applications/{id}/suite-candidates?kind=smoke

A suite selects tests that already exist; it never generates new ones.
"Smoke" and "regression" describe WHICH tests run and when, not a kind of
test — and the same test case belongs in both, so membership is
many-to-many.
"""

import logging
from typing import Optional

from fastapi import APIRouter, Request
from pydantic import BaseModel

from core.db.connection import get_conn

logger = logging.getLogger(__name__)
router = APIRouter()


class SuiteIn(BaseModel):
    name: str
    kind: str = "custom"
    description: Optional[str] = None
    member_ids: list[int] = []


class SuitePatch(BaseModel):
    name: Optional[str] = None
    description: Optional[str] = None


class MembersIn(BaseModel):
    member_ids: list[int]


def _rows(cur):
    cols = [d[0] for d in cur.description]
    return [dict(zip(cols, r)) for r in cur.fetchall()]


@router.get("/applications/{application_id}/suites")
def list_suites(application_id: int):
    """Suites for an application, with member and automation counts."""
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT s.*,
                       COUNT(DISTINCT m.test_case_proposal_id) AS member_count,
                       COUNT(DISTINCT m.test_case_proposal_id) FILTER (
                           WHERE EXISTS (
                               SELECT 1 FROM generations g
                               WHERE g.test_case_proposal_id = m.test_case_proposal_id
                                 AND g.script_path IS NOT NULL
                                 AND g.is_stale = FALSE
                           )
                       ) AS automated_count
                FROM test_suites s
                LEFT JOIN test_suite_members m ON m.suite_id = s.id
                WHERE s.application_id = %s
                GROUP BY s.id
                ORDER BY s.kind, s.name
                """,
                (application_id,),
            )
            return {"suites": _rows(cur)}


@router.get("/applications/{application_id}/suite-candidates")
def suite_candidates(application_id: int, kind: str = "custom"):
    """
    Every test case that could go in a suite, with a sensible pre-selection.

    Defaults are a starting point, not a decision:
        smoke       P0 and P1 that are automated — the critical path, minutes
        regression  everything automated — "did anything break"
        custom      nothing pre-selected

    Un-automated test cases are included deliberately. Choosing what belongs
    in a suite and choosing what to automate are separate decisions, and you
    should be able to put a test in the smoke suite and script it afterwards.
    """
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT p.id, p.title, p.priority, p.category, p.tags,
                       EXISTS (
                           SELECT 1 FROM generations g
                           WHERE g.test_case_proposal_id = p.id
                             AND g.script_path IS NOT NULL AND g.is_stale = FALSE
                       ) AS automated
                FROM test_case_proposals p
                WHERE p.application_id = %s AND p.status != 'archived'
                ORDER BY p.priority NULLS LAST, p.title
                """,
                (application_id,),
            )
            rows = _rows(cur)

    k = (kind or "custom").lower()
    for r in rows:
        if k == "smoke":
            r["suggested"] = bool(r["automated"]) and r.get("priority") in ("P0", "P1")
        elif k == "regression":
            r["suggested"] = bool(r["automated"])
        else:
            r["suggested"] = False

    return {
        "candidates": rows,
        "kind": k,
        "suggested_count": sum(1 for r in rows if r["suggested"]),
    }


@router.post("/applications/{application_id}/suites")
def create_suite(application_id: int, body: SuiteIn, request: Request):
    """Create a suite and set its members."""
    name = (body.name or "").strip()
    if not name:
        return {"error": "A suite name is required."}

    who = request.headers.get("X-Triggered-By", "anonymous")
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT 1 FROM test_suites WHERE application_id=%s AND lower(name)=lower(%s)",
                (application_id, name),
            )
            if cur.fetchone():
                return {"error": f"A suite called '{name}' already exists."}

            cur.execute(
                """INSERT INTO test_suites (application_id, name, kind, description, created_by)
                   VALUES (%s, %s, %s, %s, %s) RETURNING *""",
                (application_id, name, body.kind, body.description, who),
            )
            suite = _rows(cur)[0]

            for pid in body.member_ids:
                cur.execute(
                    "INSERT INTO test_suite_members (suite_id, test_case_proposal_id) "
                    "VALUES (%s, %s) ON CONFLICT DO NOTHING",
                    (suite["id"], pid),
                )
        conn.commit()

    logger.info("Created %s suite '%s' with %d member(s) for application %s",
                body.kind, name, len(body.member_ids), application_id)
    suite["member_count"] = len(body.member_ids)
    return {"suite": suite}


@router.get("/suites/{suite_id}")
def get_suite(suite_id: int):
    """A suite with its members, each showing whether it is automated."""
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT * FROM test_suites WHERE id = %s", (suite_id,))
            suites = _rows(cur)
            if not suites:
                return {"error": "Suite not found."}
            suite = suites[0]

            cur.execute(
                """
                SELECT p.id, p.title, p.priority, p.category,
                       EXISTS (
                           SELECT 1 FROM generations g
                           WHERE g.test_case_proposal_id = p.id
                             AND g.script_path IS NOT NULL AND g.is_stale = FALSE
                       ) AS automated
                FROM test_suite_members m
                JOIN test_case_proposals p ON p.id = m.test_case_proposal_id
                WHERE m.suite_id = %s AND p.status != 'archived'
                ORDER BY p.priority NULLS LAST, p.title
                """,
                (suite_id,),
            )
            suite["members"] = _rows(cur)
    return {"suite": suite}


@router.patch("/suites/{suite_id}")
def update_suite(suite_id: int, body: SuitePatch):
    """Rename a suite or change its description."""
    sets, vals = [], []
    if body.name is not None:
        sets.append("name = %s"); vals.append(body.name.strip())
    if body.description is not None:
        sets.append("description = %s"); vals.append(body.description)
    if not sets:
        return {"error": "Nothing to update."}
    sets.append("updated_at = now()")
    vals.append(suite_id)

    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(f"UPDATE test_suites SET {', '.join(sets)} WHERE id = %s RETURNING *",
                        tuple(vals))
            rows = _rows(cur)
        conn.commit()
    return {"suite": rows[0]} if rows else {"error": "Suite not found."}


@router.put("/suites/{suite_id}/members")
def set_members(suite_id: int, body: MembersIn):
    """
    Replace a suite's membership.

    A whole-set update rather than add/remove calls: the UI edits the
    selection as one thing, so sending the final list avoids the two ever
    disagreeing.
    """
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM test_suite_members WHERE suite_id = %s", (suite_id,))
            for pid in body.member_ids:
                cur.execute(
                    "INSERT INTO test_suite_members (suite_id, test_case_proposal_id) "
                    "VALUES (%s, %s) ON CONFLICT DO NOTHING",
                    (suite_id, pid),
                )
            cur.execute("UPDATE test_suites SET updated_at = now() WHERE id = %s", (suite_id,))
        conn.commit()
    logger.info("Suite %s now has %d member(s)", suite_id, len(body.member_ids))
    return {"suite_id": suite_id, "member_count": len(body.member_ids)}


@router.delete("/suites/{suite_id}")
def delete_suite(suite_id: int):
    """
    Delete a suite.

    Only the grouping is removed — the test cases, their scripts and their
    run history are untouched. A suite is a view over tests, not their owner.
    """
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM test_suite_members WHERE suite_id = %s", (suite_id,))
            cur.execute("DELETE FROM test_suites WHERE id = %s", (suite_id,))
            deleted = cur.rowcount
        conn.commit()
    if not deleted:
        return {"error": "Suite not found."}
    return {"deleted": True, "suite_id": suite_id}


class RunSuiteRequest(BaseModel):
    headed: bool = False
    environment: Optional[str] = None
    target_url: Optional[str] = None
    triggered_by: Optional[str] = "ui"


@router.post("/suites/{suite_id}/run")
def run_suite(suite_id: int, body: RunSuiteRequest):
    """
    Run every automated test case in a suite.

    This is the ONE path a suite is executed by — the UI's Run button and the
    scheduler both call it. Keeping them on the same code means what you
    demonstrate by hand is exactly what runs at 6am.

    Members without a script are reported as skipped rather than ignored: a
    suite that silently drops half its tests is worse than one that says so.
    """
    from api.routes.environments import resolve_url
    from api.routes.run import _record, _test_env_for
    from service.selenium_runner import run_script_file

    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """SELECT s.name, s.kind, s.application_id, a.name AS app_name
                   FROM test_suites s JOIN applications a ON a.id = s.application_id
                   WHERE s.id = %s""",
                (suite_id,),
            )
            row = cur.fetchone()
            if not row:
                return {"error": "Suite not found."}
            suite_name, kind, application_id, app_name = row

            cur.execute(
                """
                SELECT p.id, p.title,
                       (SELECT g.id FROM generations g
                         WHERE g.test_case_proposal_id = p.id
                           AND g.script_path IS NOT NULL AND g.is_stale = FALSE
                         ORDER BY g.id DESC LIMIT 1) AS generation_id,
                       (SELECT g.script_path FROM generations g
                         WHERE g.test_case_proposal_id = p.id
                           AND g.script_path IS NOT NULL AND g.is_stale = FALSE
                         ORDER BY g.id DESC LIMIT 1) AS script_path
                FROM test_suite_members m
                JOIN test_case_proposals p ON p.id = m.test_case_proposal_id
                WHERE m.suite_id = %s AND p.status != 'archived'
                ORDER BY p.priority NULLS LAST, p.title
                """,
                (suite_id,),
            )
            members = _rows(cur)

    url, env_label = resolve_url(application_id, body.environment, body.target_url)
    logger.info("Running suite '%s' (%d member(s)) against %s [%s]",
                suite_name, len(members), url, env_label)

    results, passed, failed, skipped = [], 0, 0, []

    for m in members:
        if not m["generation_id"] or not m["script_path"]:
            skipped.append(m["title"])
            results.append({"test": m["title"], "result": "skipped",
                            "reason": "no script generated yet"})
            continue

        r = run_script_file(
            m["script_path"],
            headed=body.headed,
            target_url=url,
            extra_env=_test_env_for(m["generation_id"], body.environment),
        )
        r["environment"] = env_label
        try:
            _record(m["generation_id"], r)
        except Exception as e:
            logger.warning("Could not record execution for %s: %s", m["title"], e)

        if r.get("result") == "pass":
            passed += 1
        else:
            failed += 1
        results.append({
            "test": m["title"],
            "result": r.get("result"),
            "duration_ms": r.get("duration_ms"),
            "output": (r.get("output") or "")[-1200:] if r.get("result") != "pass" else "",
        })

    summary = {
        "suite_id": suite_id, "suite": suite_name, "kind": kind,
        "application": app_name, "application_id": application_id,
        "environment": env_label, "target_url": url,
        "total": len(members), "passed": passed, "failed": failed,
        "skipped": len(skipped), "skipped_tests": skipped,
        "results": results,
        "triggered_by": body.triggered_by,
    }
    logger.info("Suite '%s' finished: %d passed, %d failed, %d skipped",
                suite_name, passed, failed, len(skipped))
    return summary
