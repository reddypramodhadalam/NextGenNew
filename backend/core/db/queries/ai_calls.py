"""
core/db/queries/ai_calls.py

Records every LLM API call for cost and performance tracking.
The reporting dashboards calculate token usage and cost from this data.

Token counts come from the provider's response at the time of the call.
They are never estimated afterward.
"""

import logging
from decimal import Decimal, ROUND_HALF_UP
from typing import Optional

from core.db.connection import get_conn
from llm.usage_context import get_usage_context

logger = logging.getLogger(__name__)

# Prices are stored per 1 million tokens. Costs are rounded to 6 decimals,
# which matches the NUMERIC(14,6) database column.
_ONE_MILLION = Decimal("1000000")
_COST_PRECISION = Decimal("0.000001")

# Maps the purpose labels to the original related_to_type column,
# so existing code that reads related_to_type keeps working.
_PURPOSE_TO_RELATED_TYPE = {
    "test_case_generation": "survey",
    "script_generation": "generation",
    "healing": "heal",
    "chat": "chat",
    "crawl": "other",
    "other": "other",
}


def record(
    related_to_type: str,
    related_to_id: Optional[int],
    provider: str,
    model: str,
    input_tokens: Optional[int],
    output_tokens: Optional[int],
    cost_usd: Optional[Decimal],
    latency_ms: Optional[int],
    purpose: str = "other",
    application_id: Optional[int] = None,
    user_id: Optional[int] = None,
    pricing_found: bool = False,
    run_id: Optional[str] = None,
) -> None:
    """
    Inserts one row into ai_calls. Raises on database errors,
    so providers should call record_llm_call(), which never raises.
    """
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO ai_calls
                    (related_to_type, related_to_id, provider, model,
                     input_tokens, output_tokens, cost_usd, latency_ms,
                     purpose, application_id, user_id, pricing_found, run_id)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                """,
                (related_to_type, related_to_id, provider, model,
                 input_tokens, output_tokens, cost_usd, latency_ms,
                 purpose, application_id, user_id, pricing_found, run_id),
            )


def _lookup_pricing(provider, model):
    """Returns the current (input, output) price per 1M tokens, or None if no price is set."""
    # Takes the newest price row that is already in effect for this provider and model.
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT input_usd_per_1m, output_usd_per_1m
                FROM llm_model_pricing
                WHERE provider = %s AND model = %s AND effective_from <= NOW()
                ORDER BY effective_from DESC
                LIMIT 1
                """,
                (provider, model),
            )
            row = cur.fetchone()
    return (Decimal(row[0]), Decimal(row[1])) if row else None


def _calculate_cost(pricing, input_tokens, output_tokens):
    """Calculates the cost in USD with exact Decimal math. Returns None when no price is set."""
    # No price configured for this model: return None so the cost is saved as unknown, not $0.
    if pricing is None:
        return None
    input_price, output_price = pricing
    # Prices are per 1M tokens, so multiply the tokens by the price and divide by one million.
    cost = (
        Decimal(input_tokens or 0) * input_price
        + Decimal(output_tokens or 0) * output_price
    ) / _ONE_MILLION
    # Round to 6 decimals to match the NUMERIC(14,6) database column.
    return cost.quantize(_COST_PRECISION, rounding=ROUND_HALF_UP)


def record_llm_call(
    provider: str,
    model: str,
    input_tokens: Optional[int],
    output_tokens: Optional[int],
    latency_ms: Optional[int],
) -> None:
    """
    Called by every provider after a successful LLM response.
    Reads the active label, calculates the cost, and saves the row.
    Never raises, so a tracking failure can't break test or script generation.
    """
    try:
        ctx = get_usage_context()
        pricing = _lookup_pricing(provider, model)
        if pricing is None:
            # Logged so a missing price gets noticed. The call is still saved, with cost left empty.
            logger.warning("No pricing configured for %s/%s; cost recorded as NULL", provider, model)

        record(
            related_to_type=_PURPOSE_TO_RELATED_TYPE.get(ctx.purpose, "other"),
            related_to_id=ctx.related_to_id,
            provider=provider,
            model=model,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cost_usd=_calculate_cost(pricing, input_tokens, output_tokens),
            latency_ms=latency_ms,
            purpose=ctx.purpose,
            application_id=ctx.application_id,
            user_id=ctx.user_id,
            pricing_found=pricing is not None,
            run_id=ctx.run_id,
        )
    except Exception:
        logger.exception("Failed to record LLM usage (non-fatal) for %s/%s", provider, model)


def sum_cost_for_run(run_id: str) -> Decimal:
    """Total cost of every AI call in one survey run. Unpriced calls (NULL cost) count as zero."""
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT COALESCE(SUM(cost_usd), 0) FROM ai_calls WHERE run_id = %s", (run_id,))
            return Decimal(cur.fetchone()[0])


def get_script_costs(application_id: int) -> list:
    """
    Cost of each script generation for one application, as exact sums of the AI
    calls linked to that generation (including verify-and-repair attempts).
    Generations with no recorded calls, such as older ones, are left out rather than shown as $0.
    """
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT g.test_case_proposal_id, g.id,
                       COALESCE(SUM(c.cost_usd), 0) AS cost_usd,
                       COUNT(c.id)                  AS ai_calls
                FROM generations g
                JOIN test_case_proposals p ON p.id = g.test_case_proposal_id
                JOIN ai_calls c ON c.purpose = 'script_generation' AND c.related_to_id = g.id
                WHERE p.application_id = %s
                GROUP BY g.test_case_proposal_id, g.id
                ORDER BY g.id
                """,
                (application_id,),
            )
            rows = cur.fetchall()
    return [
        {"proposal_id": r[0], "generation_id": r[1], "cost_usd": str(r[2]), "ai_calls": r[3]}
        for r in rows
    ]


def save_survey_run(run_id, application_id, proposal_count, total_cost, per_case_cost):
    """
    Stores the total AI cost of one survey run and each test case's even share of it.
    Never raises: cost bookkeeping must not break the survey.
    """
    try:
        with get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO survey_runs
                        (run_id, application_id, proposal_count, total_cost_usd, per_case_cost_usd)
                    VALUES (%s, %s, %s, %s, %s)
                    ON CONFLICT (run_id) DO NOTHING
                    """,
                    (run_id, application_id, proposal_count, total_cost, per_case_cost),
                )
    except Exception:
        logger.exception("Could not save survey run cost (non-fatal)")


def get_survey_run(application_id, run_id=None):
    """
    Finds the survey run a test case's cost share comes from. Needs the run id:
    with none (file or ALM imports) it returns None, so no cost is attached.
    """
    if not run_id:
        return None
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT run_id, per_case_cost_usd FROM survey_runs WHERE run_id = %s AND application_id = %s",
                (run_id, application_id),
            )
            row = cur.fetchone()
    return {"run_id": row[0], "per_case_cost_usd": Decimal(row[1])} if row else None


def get_test_case_costs(application_id):
    """Estimated AI cost per test case of one application, as {proposal id (string): cost (string)}."""
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT id, ai_cost_usd FROM test_case_proposals
                WHERE application_id = %s AND ai_cost_usd IS NOT NULL AND status != 'archived'
                """,
                (application_id,),
            )
            rows = cur.fetchall()
    return {str(r[0]): str(r[1]) for r in rows}
