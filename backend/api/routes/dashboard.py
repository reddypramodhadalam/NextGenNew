"""
api/routes/dashboard.py

Real dashboard metrics computed from the database — nothing hardcoded.
Serves both the Engineer and Leadership views.

  GET /v1/dashboard/engineer     — test-suite health (what's broken/flaky)

    Optional query params, read by the frontend's Range / Test Type /
    Application filter bar and applied to EVERY section below that bar
    on the page — Execution Trend, Results Distribution, Failing Tests,
    and Recent Runs all narrow together, never just the two charts:
      range_days      — lookback window in days (e.g. 7, 14, 30, 56, 90).
                         Omitted = no date bound at all (only the
                         page's very first, filter-less load behaves
                         this way — see EngineerView). Once the
                         frontend's filter bar is mounted it always
                         sends an explicit value (default: 7, "Last
                         week").
      test_type       — restrict to one test_case_proposals.test_type
                         ('functional' | 'regression' | 'smoke'). Omitted
                         or unrecognised = no restriction.
      application_id  — restrict to one applications.id. Omitted or not
                         a valid integer = no restriction.
      failing_limit   — max rows in "failing". Omitted = 12 (dashboard
                         card default). The "More" modal asks for more,
                         WITH the same range_days/test_type/
                         application_id already selected — "more" means
                         "the rest of what's currently shown", not a
                         different, unfiltered universe.
      recent_limit    — max rows in "recent_runs". Same idea as
                         failing_limit, for Recent Runs.

    kpis / by_type / scope always reflect the full, unfiltered dataset —
    those feed the cards ABOVE the filter bar (Summary cards, Critical
    Pass Rate / Flaky Tests / Avg Run Time), which must not shift just
    because someone changed a filter below them.

  GET /v1/dashboard/leadership   — coverage, trend, risk, ROI story

    Optional query params, read by the frontend's Range / Test Type /
    Application filter bar and applied to every section below that bar:
    Quality Trend, Automated Scripts by Application, AND Coverage by
    Application (including its per-module breakdown) all narrow
    together. range_days only affects Quality Trend — Automated Scripts
    by Application and Coverage by Application are snapshots of the
    current automation estate, not execution trends, so only test_type/
    application_id apply to them (see _scripts_by_app / _coverage_by_app
    for why). kpis / by_type / scope stay unfiltered — those feed the
    KPI row ABOVE the filter bar. risks is ALWAYS computed from the
    full, unfiltered dataset regardless of any filter, deliberately: a
    genuinely at-risk application must never disappear from risk
    detection just because the Application filter is set to a different
    app.

    coverage_by_app[] additionally carries a "modules" list per
    application — the same 3 metrics (scenarios/automated/incomplete/
    coverage), one level more granular, grouped by
    test_case_proposals.tags->>'scenario' (see _MODULE_TAG_EXPR). This
    is purely additive: every key that existed on a coverage_by_app
    entry before is unchanged; "modules" is new.

Flaky is DERIVED (a test with both pass and fail in its history), not a
stored flag. Coverage = scripts that have run / total scenarios.
"""

import logging
from typing import Optional

from fastapi import APIRouter, Query

from core.db.connection import get_conn

logger = logging.getLogger(__name__)
router = APIRouter()

# test_type values the chart filters accept on both endpoints. Mirrors the
# vocabulary already produced by the by_type breakdown query below. Kept
# as an explicit allow-list so an unexpected query-string value is
# ignored rather than reaching raw SQL.
_VALID_TEST_TYPES = {"functional", "regression", "smoke"}

# Default lookback window for the filtered trend charts when range_days
# is omitted — equivalent to the panels' original hardcoded "8 weeks".
_DEFAULT_RANGE_DAYS = 56

# Below this many days, bucket a trend by day instead of by week —
# grouping a "Last week" filter into a single weekly bar isn't useful.
_DAILY_BUCKET_THRESHOLD_DAYS = 14

# How a scenario's grouping ("module" in the Leadership dashboard) is
# read. test_case_proposals has no dedicated module column (see
# db/schema.sql) — the SAME grouping the Applications detail page already
# shows (app/applications/[id]/page.js groupScenarios()) is used here for
# consistency: tags->>'scenario', defaulting to 'General' when absent.
# That page's badge names like "Preferences Management" / "Docs
# Navigation" come from this exact key — using it here means the
# Dashboard's per-application breakdown always matches what a reviewer
# already sees when they open that application directly.
_MODULE_TAG_EXPR = "COALESCE(NULLIF(TRIM(p.tags->>'scenario'), ''), 'General')"


def _rows(cur):
    cols = [d[0] for d in cur.description]
    return [dict(zip(cols, r)) for r in cur.fetchall()]


def _resolve_test_type(test_type: Optional[str]) -> Optional[str]:
    """Validates a test_type filter, logging and dropping anything unrecognised."""
    if test_type and test_type not in _VALID_TEST_TYPES:
        logger.warning("Ignoring unrecognised test_type filter: %r", test_type)
        return None
    return test_type


def _exec_filter_sql(test_type: Optional[str], application_id: Optional[int]):
    """
    Builds the JOIN/WHERE fragments shared by every chart that filters
    the `executions` table by test_type/application_id. Both those
    columns live on test_case_proposals, reached via
    executions -> generations -> test_case_proposals, so the same join is
    needed whichever (or both) filters are active — and skipped
    entirely when neither is, to avoid an unnecessary join on the
    unfiltered/default call.

    @returns: (join_sql: str, extra_sql: str, extra_params: list)
    """
    filter_by_type = test_type in _VALID_TEST_TYPES
    needs_join = filter_by_type or application_id is not None
    join_sql = (
        """
        JOIN generations g ON g.id = e.generation_id
        JOIN test_case_proposals p ON p.id = g.test_case_proposal_id
        """
        if needs_join
        else ""
    )
    extra_clauses = []
    extra_params = []
    if filter_by_type:
        extra_clauses.append("AND p.test_type = %s")
        extra_params.append(test_type)
    if application_id is not None:
        extra_clauses.append("AND p.application_id = %s")
        extra_params.append(application_id)
    return join_sql, " ".join(extra_clauses), extra_params


def _trend_and_distribution(cur, range_days: Optional[int], test_type: Optional[str],
                             application_id: Optional[int]):
    """
    Execution Trend + Results Distribution (Engineer view), both scoped to
    the same range_days/test_type/application_id filters.

    Isolated in one place because the two panels share identical
    join/filter logic and are the only Engineer-view sections these
    filters are allowed to touch — see the module docstring.

    @param cur: an open DB cursor (reused from the caller's connection).
    @param range_days: lookback window in days; None -> _DEFAULT_RANGE_DAYS.
    @param test_type: one of _VALID_TEST_TYPES, or None/unrecognised for
        no restriction.
    @param application_id: an applications.id to restrict to, or None for
        no restriction.
    @returns: (trend: list[dict], distribution: dict[str, int])
    """
    range_days = range_days or _DEFAULT_RANGE_DAYS
    bucket = "day" if range_days <= _DAILY_BUCKET_THRESHOLD_DAYS else "week"
    join_sql, extra_sql, extra_params = _exec_filter_sql(test_type, application_id)

    # ---- Execution trend: pass/fail/other per bucket, real dates ----
    cur.execute(
        f"""
        SELECT date_trunc(%s, e.executed_at)::date AS date,
               COUNT(*) FILTER (WHERE e.result = 'pass') AS passed,
               COUNT(*) FILTER (WHERE e.result = 'fail') AS failed,
               COUNT(*) FILTER (WHERE e.result IN ('skip', 'error')) AS other
        FROM executions e
        {join_sql}
        WHERE e.executed_at > now() - make_interval(days => %s)
        {extra_sql}
        GROUP BY 1 ORDER BY 1
        """,
        [bucket, range_days, *extra_params],
    )
    trend = _rows(cur)

    # ---- Results distribution over the same window/type/application ----
    cur.execute(
        f"""
        SELECT e.result, COUNT(*)
        FROM executions e
        {join_sql}
        WHERE e.executed_at > now() - make_interval(days => %s)
        {extra_sql}
        GROUP BY e.result
        """,
        [range_days, *extra_params],
    )
    distribution = {r[0]: r[1] for r in cur.fetchall()}

    return trend, distribution


def _quality_trend(cur, range_days: Optional[int], test_type: Optional[str],
                    application_id: Optional[int]):
    """
    Quality Trend (Leadership view): pass rate per bucket, scoped to the
    same range_days/test_type/application_id filters as the Engineer
    view's Execution Trend — same underlying `executions` data, just
    expressed as a single pass-rate percentage per bucket instead of
    passed/failed/other counts.

    Not applied to "Automated Scripts by Application": that chart is a
    snapshot of the current automation estate per application, not an
    execution trend, and `generations` has no verified created_at column
    to bucket by. See _scripts_by_app for the filters that DO apply there
    (test_type, application_id).

    @returns: trend: list[dict] with keys "date" and "pass_rate"
    """
    range_days = range_days or _DEFAULT_RANGE_DAYS
    bucket = "day" if range_days <= _DAILY_BUCKET_THRESHOLD_DAYS else "week"
    join_sql, extra_sql, extra_params = _exec_filter_sql(test_type, application_id)

    cur.execute(
        f"""
        SELECT date_trunc(%s, e.executed_at)::date AS date,
               ROUND(100.0 * COUNT(*) FILTER (WHERE e.result = 'pass')
                     / NULLIF(COUNT(*), 0)) AS pass_rate
        FROM executions e
        {join_sql}
        WHERE e.executed_at > now() - make_interval(days => %s)
        {extra_sql}
        GROUP BY 1 ORDER BY 1
        """,
        [bucket, range_days, *extra_params],
    )
    return _rows(cur)


def _scripts_by_app(cur, test_type: Optional[str], application_id: Optional[int]):
    """
    Automated Scripts by Application (Leadership view): EVERY scenario
    for each application, split into exactly 3 mutually-exclusive
    buckets that always sum to that application's total scenario count:

      automated       — has at least one successful, non-stale
                        generation. Identical definition to
                        "automated" everywhere else on this dashboard
                        (coverage_fraction.automated, coverage_by_app[]
                        .automated, _automation_counts) — counts
                        DISTINCT scenarios, so a scenario regenerated
                        more than once (e.g. after a self-healing fix)
                        is still counted once here, same reconciliation
                        guarantee _automation_counts' docstring
                        describes.
      manual_only     — test_case_proposals.should_automate = FALSE: a
                        reviewer explicitly decided this scenario
                        should never be automated (e.g. it needs visual
                        judgement or a physical device), independent of
                        whether anyone has attempted automation on it.
      not_automated   — everything else: no successful script yet, and
                        not flagged manual-only. These are the
                        scenarios that COULD still be automated.

    Previously this returned a single "scripts" count per application
    (the automated slice only), feeding a single-series bar. The chart
    this feeds now renders a 3-series STACKED bar with a legend, so
    every scenario in the estate is visible here, not just the
    automated ones — nothing that was shown before is lost; "scripts"
    is simply renamed to "automated" and two new fields sit alongside
    it.

    test_type is applied inside the JOIN (not a WHERE clause) so an
    application with zero proposals of that type still appears with
    all-zero counts rather than silently disappearing from the chart —
    same convention as _coverage_by_app. application_id narrows to a
    single application's bar.

    @returns: list[dict] with keys name/scenarios/automated/manual_only/
        not_automated, ordered by scenarios desc.
    """
    filter_by_type = test_type in _VALID_TEST_TYPES
    inner_where = "WHERE p.test_type = %s" if filter_by_type else ""
    type_params = [test_type] if filter_by_type else []

    outer_where = "WHERE a.id = %s" if application_id is not None else ""
    app_params = [application_id] if application_id is not None else []

    cur.execute(
        f"""
        WITH proposal_status AS (
            SELECT p.id AS proposal_id,
                   p.application_id,
                   p.should_automate,
                   MAX(CASE WHEN g.script_path IS NOT NULL
                                 AND g.is_stale = FALSE
                                 AND g.generation_status = 'success'
                            THEN 1 ELSE 0 END) AS has_success
            FROM test_case_proposals p
            LEFT JOIN generations g ON g.test_case_proposal_id = p.id
            {inner_where}
            GROUP BY p.id, p.application_id, p.should_automate
        )
        SELECT a.name,
               COUNT(ps.proposal_id) AS scenarios,
               COUNT(*) FILTER (WHERE ps.has_success = 1) AS automated,
               COUNT(*) FILTER (
                   WHERE ps.has_success = 0 AND ps.should_automate = FALSE
               ) AS manual_only,
               COUNT(*) FILTER (
                   WHERE ps.has_success = 0
                     AND (ps.should_automate IS NULL OR ps.should_automate = TRUE)
               ) AS not_automated
        FROM applications a
        LEFT JOIN proposal_status ps ON ps.application_id = a.id
        {outer_where}
        GROUP BY a.name ORDER BY scenarios DESC
        """,
        [*type_params, *app_params],
    )
    return [
        {
            "name": name,
            "scenarios": scen or 0,
            "automated": auto or 0,
            "manual_only": manual or 0,
            "not_automated": not_auto or 0,
        }
        for name, scen, auto, manual, not_auto in cur.fetchall()
    ]


def _automation_counts(cur):
    """
    Distinct-SCENARIO automation counts — the single definition of
    "automated" / "incomplete" shared by both dashboards.

    A scenario counts as "automated" if it has AT LEAST ONE successful,
    non-stale generation, no matter how many times it was (re)generated
    (e.g. via the "Regenerate script" action after a self-healing fix) —
    regenerating an already-working scenario must not inflate this
    count. "incomplete" only counts a scenario that has NO successful
    generation but does have an incomplete one, so a scenario that was
    later fixed isn't miscounted as both.

    Previously the Engineer/Subscriber dashboard's "Automated Tests" KPI
    (total_scripts below) used a raw COUNT(*) over `generations` instead
    of this distinct-scenario count — the exact same bug _scripts_by_app
    used to have (see that function's docstring). A scenario regenerated
    more than once was counted once per successful generation there,
    which is why the Subscriber tab showed "25 automated" while the
    Leadership tab's coverage_fraction.automated (already
    scenario-distinct) showed "23" for the same data. Both dashboards
    now call this one function, so the two tabs cannot structurally
    diverge again — there is exactly one query that decides what counts
    as "automated".

    @returns: (automated: int, incomplete: int)
    """
    cur.execute(
        """
        WITH proposal_status AS (
            SELECT p.id AS proposal_id,
                   MAX(CASE WHEN g.script_path IS NOT NULL
                                 AND g.is_stale = FALSE
                                 AND g.generation_status = 'success'
                            THEN 1 ELSE 0 END) AS has_success,
                   MAX(CASE WHEN g.script_path IS NOT NULL
                                 AND g.is_stale = FALSE
                                 AND g.generation_status = 'incomplete'
                            THEN 1 ELSE 0 END) AS has_incomplete
            FROM test_case_proposals p
            LEFT JOIN generations g ON g.test_case_proposal_id = p.id
            GROUP BY p.id
        )
        SELECT COUNT(*) FILTER (WHERE has_success = 1) AS automated,
               COUNT(*) FILTER (WHERE has_success = 0 AND has_incomplete = 1) AS incomplete
        FROM proposal_status
        """
    )
    automated, incomplete = cur.fetchone()
    return automated or 0, incomplete or 0


def _execution_distribution(cur):
    """
    Always-unfiltered pass/fail/skip/error counts across ALL executions —
    feeds the Leadership dashboard's "Executions" summary card ONLY (the
    donut-with-legend KPI card, not the filterable Quality Trend or
    Automated Scripts by Application panels further down the page).

    Deliberately NOT scoped by range_days/test_type/application_id like
    _trend_and_distribution's distribution (that one feeds the
    Engineer/Subscriber view's filterable Results Distribution chart,
    positioned below that view's filter bar). This is a top-of-page KPI
    number, and every other number in that same summary row (scope,
    kpis.coverage_pct, kpis.pass_rate, etc.) is deliberately unfiltered
    too — a chart filter picked further down the page must not make a
    KPI card above it move.

    @returns: dict[str, int] keyed by result ('pass'/'fail'/'skip'/
        'error'). A result with zero executions is simply absent from
        the dict — callers use .get(key, 0).
    """
    cur.execute("SELECT result, COUNT(*) FROM executions GROUP BY result")
    return {r[0]: r[1] for r in cur.fetchall()}


def _proposal_filters(range_days: Optional[int], test_type: Optional[str],
                       application_id: Optional[int], date_column: str = "e.executed_at"):
    """
    Shared WHERE-clause builder for Failing Tests / Recent Runs: both
    query `executions e` joined to `generations g` / `test_case_proposals
    p`, and both accept the identical range_days/test_type/
    application_id filters as Execution Trend / Results Distribution —
    this is what makes "pick a filter, it applies to every card below"
    actually true instead of just the two charts.

    range_days is treated differently here than in
    _trend_and_distribution: a bucketed trend always needs SOME window
    to group by, so that function defaults range_days=None to 56 days.
    These two list-style cards have no such requirement — when
    range_days is None, no date bound is applied at all (their original,
    always-unbounded behaviour), which only actually happens on the
    page's very first, filter-less load (see EngineerView) before the
    filter bar's effect has fired even once.

    @returns: (where_sql: str, params: list) — where_sql is either ""
        or a "WHERE ..." clause ready to embed directly in the query.
    """
    clauses = []
    params = []
    if range_days is not None:
        clauses.append(f"{date_column} > now() - make_interval(days => %s)")
        params.append(range_days)
    if test_type in _VALID_TEST_TYPES:
        clauses.append("p.test_type = %s")
        params.append(test_type)
    if application_id is not None:
        clauses.append("p.application_id = %s")
        params.append(application_id)
    where_sql = ("WHERE " + " AND ".join(clauses)) if clauses else ""
    return where_sql, params


def _failing_tests(cur, range_days: Optional[int], test_type: Optional[str],
                    application_id: Optional[int], limit: int):
    """
    Failing Tests (Engineer/Subscriber view): latest run per script that
    failed, P0 first — scoped by the same range_days/test_type/
    application_id filters as Execution Trend / Results Distribution
    (see _proposal_filters and the module docstring's "applied to EVERY
    section below that bar" note).

    range_days bounds which execution counts as "latest": with a filter
    active, only runs within that window are considered, so a test that
    last failed outside the window drops off this list even if nobody
    has re-run it since. This is intentional — the whole point of the
    filter applying here too is that "Last week" means the failures
    that matter THIS week.

    @returns: list[dict] of failing rows (P0 first), already capped at
        `limit` — the caller does not need to slice this again.
    """
    where_sql, params = _proposal_filters(range_days, test_type, application_id)
    cur.execute(
        f"""
        SELECT DISTINCT ON (g.id)
               g.id AS generation_id, p.title, p.priority, p.category, p.test_type,
               e.result, e.executed_at
        FROM executions e
        JOIN generations g ON g.id = e.generation_id
        JOIN test_case_proposals p ON p.id = g.test_case_proposal_id
        {where_sql}
        ORDER BY g.id, e.executed_at DESC
        """,
        params,
    )
    latest = _rows(cur)
    failing = [r for r in latest if r["result"] in ("fail", "error")]
    prio_order = {"P0": 0, "P1": 1, "P2": 2, "P3": 3, None: 4}
    failing.sort(key=lambda r: prio_order.get(r["priority"], 4))
    return failing[:limit]


def _recent_runs(cur, range_days: Optional[int], test_type: Optional[str],
                  application_id: Optional[int], limit: int):
    """
    Recent Runs (Engineer/Subscriber view): most recent executions —
    scoped by the same range_days/test_type/application_id filters as
    Execution Trend / Results Distribution / Failing Tests (see
    _proposal_filters). range_days=None (only the page's very first,
    filter-less load) preserves this card's original, always-unbounded
    "most recent N runs" behaviour.

    @returns: list[dict], already capped at `limit`.
    """
    where_sql, params = _proposal_filters(range_days, test_type, application_id)
    cur.execute(
        f"""
        SELECT e.result, e.executed_at, e.duration_ms, p.title, a.name AS app
        FROM executions e
        JOIN generations g ON g.id = e.generation_id
        JOIN test_case_proposals p ON p.id = g.test_case_proposal_id
        JOIN applications a ON a.id = p.application_id
        {where_sql}
        ORDER BY e.executed_at DESC LIMIT %s
        """,
        [*params, limit],
    )
    return _rows(cur)


def _coverage_by_app(cur, test_type: Optional[str], application_id: Optional[int]):
    """
    Coverage by Application (Leadership view): scenarios/automated/
    incomplete/coverage per application — the "Coverage by Application"
    table's app-level rows.

    Optionally scoped by test_type/application_id, mirroring exactly how
    _scripts_by_app scopes its chart: test_type narrows which scenarios
    count (an app with none of that type still appears, with zero
    counts, rather than disappearing — the type filter is applied
    inside the JOIN, not as an outer WHERE); application_id narrows the
    result to that one application's row only. NOT scoped by
    range_days: like _scripts_by_app, this is a snapshot of the current
    automation estate, not an execution trend — see that function's
    docstring for the same reasoning.

    Uses the exact same distinct-scenario has_success/has_incomplete
    definition as _automation_counts, just grouped per application
    instead of totalled — so calling this with no filters and summing
    every row's "automated"/"incomplete" always reconciles with
    _automation_counts' totals.

    @returns: list[dict] with keys app/scenarios/automated/incomplete/
        coverage, ordered by application name.
    """
    filter_by_type = test_type in _VALID_TEST_TYPES
    inner_where = "WHERE p.test_type = %s" if filter_by_type else ""
    type_params = [test_type] if filter_by_type else []

    outer_where = "WHERE a.id = %s" if application_id is not None else ""
    app_params = [application_id] if application_id is not None else []

    cur.execute(
        f"""
        WITH proposal_status AS (
            SELECT p.id AS proposal_id,
                   p.application_id,
                   MAX(CASE WHEN g.script_path IS NOT NULL
                                 AND g.is_stale = FALSE
                                 AND g.generation_status = 'success'
                            THEN 1 ELSE 0 END) AS has_success,
                   MAX(CASE WHEN g.script_path IS NOT NULL
                                 AND g.is_stale = FALSE
                                 AND g.generation_status = 'incomplete'
                            THEN 1 ELSE 0 END) AS has_incomplete
            FROM test_case_proposals p
            LEFT JOIN generations g ON g.test_case_proposal_id = p.id
            {inner_where}
            GROUP BY p.id, p.application_id
        )
        SELECT a.name,
               COUNT(ps.proposal_id) AS scenarios,
               COUNT(*) FILTER (WHERE ps.has_success = 1) AS automated,
               COUNT(*) FILTER (WHERE ps.has_success = 0 AND ps.has_incomplete = 1) AS incomplete
        FROM applications a
        LEFT JOIN proposal_status ps ON ps.application_id = a.id
        {outer_where}
        GROUP BY a.name ORDER BY a.name
        """,
        [*type_params, *app_params],
    )
    cov_by_app = []
    for name, scen, auto, inc in cur.fetchall():
        pct = round(100 * (auto or 0) / (scen or 1)) if scen else 0
        cov_by_app.append(
            {
                "app": name,
                "scenarios": scen or 0,
                "automated": auto or 0,
                "incomplete": inc or 0,
                "coverage": pct,
            }
        )
    return cov_by_app


def _coverage_by_module(cur, test_type: Optional[str] = None, application_id: Optional[int] = None):
    """
    Coverage broken down by (application, module) — the same 3 derived
    metrics as the app-level coverage_by_app query (scenarios/automated/
    incomplete/coverage), one level more granular.

    Deliberately a SEPARATE query from the existing app-level
    coverage_by_app computation in leadership_dashboard(), rather than
    changing that query's GROUP BY to include module: this way the
    per-application totals leadership already sees cannot be affected by
    this addition even if module data is missing, malformed, or
    inconsistent for some scenarios. The two queries are reconciled by
    construction (both derive "automated"/"incomplete" from the exact
    same has_success/has_incomplete CASE logic), so a module's scenario
    counts for an app always sum to that app's existing "scenarios"
    total in coverage_by_app — nothing here can make the numbers
    disagree.

    Module grouping matches the Applications detail page's own
    groupScenarios() exactly — tags->>'scenario', defaulting to
    'General' — so the same named groups shown there (e.g. "Preferences
    Management", "Docs Navigation") appear here too. See _MODULE_TAG_EXPR.

    Optionally scoped by test_type/application_id — same convention as
    _coverage_by_app, so a module's drill-down under an app row always
    matches whatever filter that row itself is currently scoped to. Not
    scoped by range_days, for the same "snapshot, not a trend" reason as
    _coverage_by_app / _scripts_by_app.

    @returns: dict[str, list[dict]] keyed by application name, each value
        a list of {"module","scenarios","automated","incomplete","coverage"}
        sorted by scenarios desc. Applications with zero scenarios are
        simply absent from the dict (an empty list is substituted by the
        caller), matching how they'd have no rows to show either way.
    """
    filter_by_type = test_type in _VALID_TEST_TYPES
    clauses = []
    params = []
    if filter_by_type:
        clauses.append("p.test_type = %s")
        params.append(test_type)
    if application_id is not None:
        clauses.append("p.application_id = %s")
        params.append(application_id)
    where_sql = ("WHERE " + " AND ".join(clauses)) if clauses else ""

    cur.execute(
        f"""
        WITH proposal_status AS (
            SELECT p.id AS proposal_id,
                   p.application_id,
                   {_MODULE_TAG_EXPR} AS module,
                   MAX(CASE WHEN g.script_path IS NOT NULL
                                 AND g.is_stale = FALSE
                                 AND g.generation_status = 'success'
                            THEN 1 ELSE 0 END) AS has_success,
                   MAX(CASE WHEN g.script_path IS NOT NULL
                                 AND g.is_stale = FALSE
                                 AND g.generation_status = 'incomplete'
                            THEN 1 ELSE 0 END) AS has_incomplete
            FROM test_case_proposals p
            LEFT JOIN generations g ON g.test_case_proposal_id = p.id
            {where_sql}
            GROUP BY p.id, p.application_id, {_MODULE_TAG_EXPR}
        )
        SELECT a.name,
               ps.module,
               COUNT(ps.proposal_id) AS scenarios,
               COUNT(*) FILTER (WHERE ps.has_success = 1) AS automated,
               COUNT(*) FILTER (WHERE ps.has_success = 0 AND ps.has_incomplete = 1) AS incomplete
        FROM applications a
        JOIN proposal_status ps ON ps.application_id = a.id
        GROUP BY a.name, ps.module
        ORDER BY a.name, scenarios DESC
        """,
        params,
    )
    by_app = {}
    for name, module, scen, auto, inc in cur.fetchall():
        pct = round(100 * (auto or 0) / (scen or 1)) if scen else 0
        by_app.setdefault(name, []).append(
            {
                "module": module,
                "scenarios": scen or 0,
                "automated": auto or 0,
                "incomplete": inc or 0,
                "coverage": pct,
            }
        )
    return by_app


@router.get("/dashboard/engineer")
def engineer_dashboard(
    range_days: Optional[int] = Query(
        None, ge=1, le=365,
        description="Lookback window in days, applied to Execution Trend, "
                     "Results Distribution, Failing Tests, AND Recent Runs "
                     "alike. Omitted = no date bound (only the page's very "
                     "first, filter-less load — see EngineerView).",
    ),
    test_type: Optional[str] = Query(
        None,
        description="Restrict Execution Trend, Results Distribution, "
                     "Failing Tests, AND Recent Runs to one test_type "
                     "(functional/regression/smoke). Omitted = all.",
    ),
    application_id: Optional[int] = Query(
        None,
        description="Restrict Execution Trend, Results Distribution, "
                     "Failing Tests, AND Recent Runs to one application's "
                     "id. Omitted = all applications.",
    ),
    failing_limit: Optional[int] = Query(
        None, ge=1, le=500,
        description="Max rows to return in 'failing'. Omitted = 12 "
                     "(dashboard card default). Frontend's 'More' modal "
                     "requests a higher number for the full list.",
    ),
    recent_limit: Optional[int] = Query(
        None, ge=1, le=500,
        description="Max rows to return in 'recent_runs'. Omitted = 10 "
                     "(dashboard card default). Frontend's 'More' modal "
                     "requests a higher number for the full list.",
    ),
):
    logger.info(
        "Building engineer dashboard metrics (range_days=%s, test_type=%s, "
        "application_id=%s, failing_limit=%s, recent_limit=%s)",
        range_days, test_type, application_id, failing_limit, recent_limit,
    )
    test_type = _resolve_test_type(test_type)

    with get_conn() as conn:
        with conn.cursor() as cur:
            # ---- KPIs ----
            cur.execute("SELECT COUNT(*) FROM test_case_proposals")
            total_scenarios = cur.fetchone()[0]
            # Distinct-scenario counts — see _automation_counts docstring
            # for why this replaced a raw COUNT(*) over `generations`
            # (that double-counted any scenario regenerated more than
            # once, which is what caused this KPI to disagree with the
            # Leadership tab's "23 of 208 scenarios").
            total_scripts, incomplete_scripts = _automation_counts(cur)
            cur.execute("SELECT COUNT(*) FROM applications WHERE is_archived = FALSE")
            total_apps = cur.fetchone()[0]
            cur.execute("SELECT COUNT(*) FROM executions")
            total_runs_all = cur.fetchone()[0]

            # P0/critical pass rate — the honest "are the important tests green?"
            cur.execute("""
                SELECT COUNT(*) FILTER (WHERE e.result='pass'), COUNT(*)
                FROM executions e
                JOIN generations g ON g.id=e.generation_id
                JOIN test_case_proposals p ON p.id=g.test_case_proposal_id
                WHERE p.priority IN ('P0','P1')""")
            p0p, p0t = cur.fetchone()
            p0_pass_rate = round(100 * (p0p or 0) / (p0t or 1))

            # pass rate over the most recent runs (last 200 executions)
            cur.execute("""SELECT result, COUNT(*) FROM (
                             SELECT result FROM executions ORDER BY executed_at DESC LIMIT 200
                           ) x GROUP BY result""")
            recent = {r[0]: r[1] for r in cur.fetchall()}
            recent_total = sum(recent.values()) or 1
            pass_rate = round(100 * recent.get("pass", 0) / recent_total)

            # avg execution time
            cur.execute("SELECT COALESCE(AVG(duration_ms),0) FROM executions")
            avg_ms = int(cur.fetchone()[0])

            # flaky: tests (generations) that have BOTH a pass and a fail
            cur.execute("""SELECT COUNT(*) FROM (
                             SELECT generation_id
                             FROM executions
                             GROUP BY generation_id
                             HAVING COUNT(*) FILTER (WHERE result='pass') > 0
                                AND COUNT(*) FILTER (WHERE result='fail') > 0
                           ) f""")
            flaky_count = cur.fetchone()[0]

            # ---- Execution trend + Results distribution ----
            # Both scoped to range_days/test_type/application_id (see
            # _trend_and_distribution); everything else in this endpoint
            # stays on the full dataset.
            trend, dist = _trend_and_distribution(cur, range_days, test_type, application_id)

            # ---- Failing tests (latest run per script that failed), P0 first ----
            # Scoped by range_days/test_type/application_id — see
            # _failing_tests and the module docstring's "applied to
            # EVERY section below that bar" note.
            failing = _failing_tests(cur, range_days, test_type, application_id, failing_limit or 12)

            # ---- Test-type breakdown ----
            cur.execute("SELECT test_type, COUNT(*) FROM test_case_proposals GROUP BY test_type")
            by_type = {r[0]: r[1] for r in cur.fetchall()}

            # ---- Recent runs ----
            # Same filter scoping as Failing Tests above — see _recent_runs.
            recent_runs = _recent_runs(cur, range_days, test_type, application_id, recent_limit or 10)

    return {
        "scope": {"apps": total_apps, "scenarios": total_scenarios, "runs": total_runs_all},
        "kpis": {
            "total_tests": total_scripts,
            "incomplete_tests": incomplete_scripts,
            "total_scenarios": total_scenarios,
            "pass_rate": pass_rate,
            "p0_pass_rate": p0_pass_rate,
            "flaky": flaky_count,
            "avg_ms": avg_ms,
        },
        "distribution": {
            "pass": dist.get("pass", 0), "fail": dist.get("fail", 0),
            "skip": dist.get("skip", 0), "error": dist.get("error", 0),
        },
        "trend": trend,
        "failing": [
            {"title": r["title"], "priority": r["priority"], "category": r["category"],
             "test_type": r["test_type"], "last_run": str(r["executed_at"])}
            for r in failing
        ],
        "by_type": by_type,
        "recent_runs": [
            {"result": r["result"], "executed_at": str(r["executed_at"]),
             "duration_ms": r["duration_ms"], "title": r["title"], "app": r["app"]}
            for r in recent_runs
        ],
    }


@router.get("/dashboard/leadership")
def leadership_dashboard(
    range_days: Optional[int] = Query(
        None, ge=1, le=365,
        description="Lookback window in days for Quality Trend only. "
                     "Omitted = 56 days (~8 weeks). Not applied to "
                     "Automated Scripts by Application or Coverage by "
                     "Application — both are snapshots, not trends.",
    ),
    test_type: Optional[str] = Query(
        None,
        description="Restrict Quality Trend, Automated Scripts by "
                     "Application, AND Coverage by Application (incl. its "
                     "per-module breakdown) to one test_type "
                     "(functional/regression/smoke). Omitted = all.",
    ),
    application_id: Optional[int] = Query(
        None,
        description="Restrict Quality Trend, Automated Scripts by "
                     "Application, AND Coverage by Application to one "
                     "application's id. Omitted = all.",
    ),
):
    logger.info(
        "Building leadership dashboard metrics (range_days=%s, test_type=%s, application_id=%s)",
        range_days, test_type, application_id,
    )
    test_type = _resolve_test_type(test_type)

    with get_conn() as conn:
        with conn.cursor() as cur:
            # automation coverage: scenarios with SUCCESSFUL automation vs total.
            # Incomplete scripts are tracked separately so coverage isn't inflated.
            cur.execute("SELECT COUNT(*) FROM test_case_proposals")
            total_scen = cur.fetchone()[0]
            # Same distinct-scenario definition of "automated"/"incomplete"
            # the Engineer/Subscriber dashboard now uses — see
            # _automation_counts docstring. Previously this endpoint had
            # its own copy of this query; now there is exactly one.
            automated, incomplete = _automation_counts(cur)
            coverage_pct = round(100 * (automated or 0) / (total_scen or 1))

            # overall pass rate
            cur.execute("SELECT COUNT(*) FILTER (WHERE result='pass'), COUNT(*) FROM executions")
            p, t = cur.fetchone()
            pass_rate = round(100 * (p or 0) / (t or 1))

            # P0/critical pass rate — defensible, replaces vague readiness
            cur.execute("""
                SELECT COUNT(*) FILTER (WHERE e.result='pass'), COUNT(*)
                FROM executions e
                JOIN generations g ON g.id=e.generation_id
                JOIN test_case_proposals p ON p.id=g.test_case_proposal_id
                WHERE p.priority IN ('P0','P1')""")
            p0p, p0t = cur.fetchone()
            p0_pass_rate = round(100 * (p0p or 0) / (p0t or 1))

            # scope counts
            cur.execute("SELECT COUNT(*) FROM applications WHERE is_archived = FALSE")
            total_apps = cur.fetchone()[0]
            cur.execute("SELECT COUNT(*) FROM executions")
            total_runs = cur.fetchone()[0]

            # execution_distribution: always-unfiltered pass/fail/skip/error
            # breakdown for the "Executions" summary card's donut. See
            # _execution_distribution docstring for why this stays
            # unfiltered rather than reusing _trend_and_distribution's
            # (filterable) version.
            execution_distribution = _execution_distribution(cur)

            # ---- Coverage by Application (+ per-module breakdown) ----
            # Scoped by test_type/application_id — see _coverage_by_app
            # and the module docstring's "applied to EVERY section below
            # that bar" note. NOT scoped by range_days (snapshot, not a
            # trend — same reasoning as _scripts_by_app).
            cov_by_app = _coverage_by_app(cur, test_type, application_id)
            modules_by_app = _coverage_by_module(cur, test_type, application_id)
            for app in cov_by_app:
                app["modules"] = modules_by_app.get(app["app"], [])

            # ---- Quality Trend + Automated Scripts by Application ----
            # Both scoped to test_type/application_id; Quality Trend also
            # takes range_days (see _quality_trend / _scripts_by_app for
            # why range_days isn't applied to the scripts-by-app snapshot).
            quality_trend = _quality_trend(cur, range_days, test_type, application_id)
            scripts_by_app = _scripts_by_app(cur, test_type, application_id)

            # test-type distribution
            cur.execute("SELECT test_type, COUNT(*) FROM test_case_proposals GROUP BY test_type")
            by_type = {r[0]: r[1] for r in cur.fetchall()}

            # ---- Always-unfiltered application-level coverage ----
            # Deliberately computed once here, independent of the possibly
            # filtered cov_by_app above — used for two things that must
            # never move just because a chart filter is active:
            #   1. risks: a genuinely at-risk application must never
            #      disappear from risk detection just because the
            #      Application filter is currently set to a different app.
            #   2. apps_automation: the "Applications" summary card's donut
            #      (how many apps have >=1 automated scenario vs none) is a
            #      top-of-page KPI, same unfiltered convention as scope/kpis.
            # Only re-queries when a filter is actually active; the
            # unfiltered case reuses cov_by_app as-is (no redundant query).
            risk_source = cov_by_app if (test_type is None and application_id is None) \
                else _coverage_by_app(cur, None, None)

            apps_with_automation = sum(1 for a in risk_source if a["automated"] > 0)
            apps_automation = {
                "with_automation": apps_with_automation,
                "without_automation": len(risk_source) - apps_with_automation,
                "total": len(risk_source),
            }

            risks = []
            for app in risk_source:
                if app["coverage"] < 40:
                    risks.append({"app": app["app"], "issue": f"Low coverage ({app['coverage']}%)", "severity": "high"})
            cur.execute("""
                SELECT a.name, COUNT(*) AS p0_fails
                FROM executions e
                JOIN generations g ON g.id=e.generation_id
                JOIN test_case_proposals p ON p.id=g.test_case_proposal_id
                JOIN applications a ON a.id=p.application_id
                WHERE p.priority='P0' AND e.result IN ('fail','error')
                  AND e.executed_at > now() - interval '2 weeks'
                GROUP BY a.name""")
            for name, cnt in cur.fetchall():
                risks.append({"app": name, "issue": f"{cnt} P0 failure(s) recently", "severity": "critical"})

    return {
        "scope": {"apps": total_apps, "scenarios": total_scen, "runs": total_runs},
        "kpis": {
            "coverage_pct": coverage_pct,
            "coverage_fraction": {
                "automated": automated or 0,
                "incomplete": incomplete or 0,
                "total": total_scen,
            },
            "pass_rate": pass_rate,
            "p0_pass_rate": p0_pass_rate,
            "p0_total": p0t,
            # ---- Additive fields for the Leadership summary row's 4 donut
            # KPI cards (Applications / Total Scenarios / Executions / Pass
            # Rate). Every field above this comment is unchanged and still
            # populated exactly as before — nothing existing was removed,
            # this only adds the raw counts those donuts need to render
            # their slices + legends. ----
            "apps_automation": apps_automation,
            "execution_distribution": {
                "pass": execution_distribution.get("pass", 0),
                "fail": execution_distribution.get("fail", 0),
                "skip": execution_distribution.get("skip", 0),
                "error": execution_distribution.get("error", 0),
            },
            "pass_counts": {
                # "overall" / "critical" here are the SAME executions
                # pass_rate/p0_pass_rate above already summarise as a
                # rounded percentage — these are the raw pass/total counts
                # behind those two percentages, so the Pass Rate donut's
                # slice sizes and its displayed percentage are always
                # derived from one consistent source.
                "overall": {"pass": p or 0, "total": t or 0},
                "critical": {"pass": p0p or 0, "total": p0t or 0},
            },
        },
        "coverage_by_app": cov_by_app,
        "quality_trend": quality_trend,
        "scripts_by_app": scripts_by_app,
        "by_type": by_type,
        "risks": risks,
    }