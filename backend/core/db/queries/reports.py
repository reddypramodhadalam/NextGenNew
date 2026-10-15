"""
core/db/queries/reports.py

Read-only queries that power the reporting dashboards.
These never write. They aggregate across multiple tables.
"""

import logging
from decimal import Decimal
from typing import Optional

from core.db.connection import get_conn

logger = logging.getLogger(__name__)


def get_application_coverage(application_id: int, environment: str) -> dict:
    """
    Computes the four F4 coverage numbers for one application/environment.

    Returns:
        A dict with keys: total_proposed, successfully_generated,
        generation_coverage_pct, total_executed, passed, execution_pass_rate_pct,
        effectively_passing, effective_coverage_pct.
    """
    # TODO: Implement the four coverage queries validated against real
    # Postgres during the spec sessions.
    raise NotImplementedError


def get_leadership_rollup() -> list[dict]:
    """
    Returns one summary row per application for the leadership dashboard.

    Returns:
        List of dicts with application name, environment, coverage numbers,
        last run timestamp, and status.
    """
    # TODO: Implement cross-application rollup
    raise NotImplementedError


def get_ai_usage_summary(days: int, application_id: Optional[int] = None) -> dict:
    """
    Totals token usage and cost for each AI activity over the last `days` days,
    plus how many test cases were generated and how many tests were automated.
    Powers the dashboard's Tokens & Cost card.
    """
    with get_conn() as conn:
        with conn.cursor() as cur:
            # Script calls are only counted when the script they belong to was generated successfully,
            # so the Tests automated row shows the tokens and cost of working tests only. Calls from
            # failed or incomplete scripts, and old calls not linked to a script, are left out.
            # Every other activity (test case generation, etc.) is counted as before.
            # unpriced_calls counts calls whose model had no price set.
            cur.execute(
                """
                SELECT c.purpose,
                       COUNT(*)                                     AS calls,
                       COALESCE(SUM(c.input_tokens), 0)             AS input_tokens,
                       COALESCE(SUM(c.output_tokens), 0)            AS output_tokens,
                       COALESCE(SUM(c.cost_usd), 0)                 AS cost_usd,
                       COUNT(*) FILTER (WHERE NOT c.pricing_found)  AS unpriced_calls
                FROM ai_calls c
                LEFT JOIN generations g
                       ON c.purpose = 'script_generation' AND g.id = c.related_to_id
                WHERE c.called_at >= NOW() - make_interval(days => %s)
                  AND (%s::int IS NULL OR c.application_id = %s)
                  AND (c.purpose <> 'script_generation' OR g.generation_status = 'success')
                GROUP BY c.purpose
                ORDER BY c.purpose
                """,
                (days, application_id, application_id),
            )
            rows = cur.fetchall()

            # Test cases generated: how many test cases the survey runs in this period produced.
            cur.execute(
                """
                SELECT COALESCE(SUM(proposal_count), 0)
                FROM survey_runs
                WHERE created_at >= NOW() - make_interval(days => %s)
                  AND (%s::int IS NULL OR application_id = %s)
                """,
                (days, application_id, application_id),
            )
            test_cases_generated = int(cur.fetchone()[0])

            # Tests automated: test cases with a successfully generated script in this period.
            # A test case is counted once, however many times its script was regenerated.
            cur.execute(
                """
                SELECT COUNT(DISTINCT g.test_case_proposal_id)
                FROM ai_calls c
                JOIN generations g ON g.id = c.related_to_id
                WHERE c.purpose = 'script_generation'
                  AND g.generation_status = 'success'
                  AND c.called_at >= NOW() - make_interval(days => %s)
                  AND (%s::int IS NULL OR c.application_id = %s)
                """,
                (days, application_id, application_id),
            )
            tests_automated = int(cur.fetchone()[0])

    # Cost is sent as a string so the exact Decimal value reaches the frontend unchanged.
    by_purpose = [
        {
            "purpose": r[0],
            "calls": r[1],
            "input_tokens": int(r[2]),
            "output_tokens": int(r[3]),
            "cost_usd": str(r[4]),
            "unpriced_calls": r[5],
        }
        for r in rows
    ]

    # Grand totals across all activities, for the card's Total row.
    totals = {
        "calls": sum(p["calls"] for p in by_purpose),
        "input_tokens": sum(p["input_tokens"] for p in by_purpose),
        "output_tokens": sum(p["output_tokens"] for p in by_purpose),
        "cost_usd": str(sum((Decimal(p["cost_usd"]) for p in by_purpose), Decimal("0"))),
        "unpriced_calls": sum(p["unpriced_calls"] for p in by_purpose),
    }
    return {
        "period_days": days,
        "application_id": application_id,
        "by_purpose": by_purpose,
        "totals": totals,
        "counts": {
            "test_cases_generated": test_cases_generated,
            "tests_automated": tests_automated,
        },
    }


def get_ai_cost_summary(application_id: int) -> dict:
    """Kept for existing callers. Returns all-time usage for one application."""
    return get_ai_usage_summary(days=3650, application_id=application_id)