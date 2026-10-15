"""
diag_help_search.py

Diagnostic for the help search scoring behaviour. Not a test — it asserts
nothing. It seeds the same fixtures as test_help_search.py and prints the raw
ts_rank_cd scores from each pass, BEFORE the min_score floor is applied, so a
decision about that floor is made from measured numbers rather than a guess.

The distinction that matters: registry.search() applies the floor, whereas
get_strategy().search() does not. This calls the strategy directly.

Run from the backend directory:
    python diag_help_search.py

Cleans up everything it creates.
"""

from __future__ import annotations

import sys

from core.db.connection import get_conn, init_pool
from core.settings import settings
from service.help_search.fts import _BROAD, _PRECISE, PostgresFTS


MARKER = "zz-help-search-diag"

ARTICLES = [
    (
        f"{MARKER}-scheduling", "Scheduling a run", "approved",
        [
            ("Creating a schedule",
             "Open the Schedules screen and choose New schedule. Pick the test "
             "suite you want to run, the environment to run it against, and how "
             "often it should fire. Frequency can be once, hourly, daily, "
             "weekdays or weekly. A schedule set to once runs a single time and "
             "then stops, which is the safest default."),
            ("Pausing a schedule",
             "A schedule can be stopped without deleting it. Switch it off from "
             "the Schedules screen and it stays in place with all its settings, "
             "but will not fire again until it is switched back on. Nothing is "
             "removed, so the run history stays intact."),
        ],
    ),
    (
        f"{MARKER}-draft", "Draft article about environments", "draft",
        [
            ("Adding an environment",
             "Each application can be tested against several environments such "
             "as dev, test and stage. The URL belongs to the environment rather "
             "than to the test case, so one test runs anywhere."),
        ],
    ),
]

QUERIES = [
    "pausing a schedule",
    "how do I stop a run from firing every night",
    "adding an environment dev test stage",
    "quarterly payroll reconciliation in SAP",
    "how do I create a test suite and run it against stage",
]


def _seed() -> list[int]:
    """Insert the temporary articles and chunks.

    Returns:
        Ids of the articles created.
    """
    ids: list[int] = []
    with get_conn() as conn:
        with conn.cursor() as cur:
            for slug, title, status, chunks in ARTICLES:
                cur.execute(
                    """INSERT INTO help_articles
                           (slug, title, category, body_md, status, author)
                       VALUES (%s, %s, 'diag', %s, %s, 'diag') RETURNING id""",
                    (slug, title, "\n\n".join(b for _, b in chunks), status),
                )
                aid = cur.fetchone()[0]
                ids.append(aid)
                for i, (heading, body) in enumerate(chunks):
                    cur.execute(
                        """INSERT INTO help_chunks
                               (source_type, article_id, chunk_index, heading, body)
                           VALUES ('article', %s, %s, %s, %s)""",
                        (aid, i, heading, body),
                    )
    return ids


def _cleanup(ids: list[int]) -> None:
    """Delete everything created by _seed.

    Args:
        ids: Article ids; chunks cascade.
    """
    if not ids:
        return
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM help_articles WHERE id = ANY(%s)", (ids,))


def _report(query: str) -> None:
    """Print raw scores from both passes for one query.

    Args:
        query: The question to run.
    """
    strategy = PostgresFTS()
    print(f"\n  query: {query!r}")

    for label, expr, text in (
        ("precise (AND)", _PRECISE, query),
        ("broad   (OR) ", _BROAD, strategy._broaden(query)),
    ):
        if not text:
            print(f"    {label}: (no usable terms)")
            continue
        for approved_only in (True, False):
            rows = strategy._run(expr, text, limit=5, approved_only=approved_only)
            tag = "approved-only" if approved_only else "incl-drafts  "
            if not rows:
                print(f"    {label}  {tag}: (nothing)")
                continue
            for r in rows:
                below = "  <-- BELOW FLOOR" if r["score"] < settings.help.min_score else ""
                print(f"    {label}  {tag}: {r['score']:.6f}  "
                      f"{r['article_title']} / {r['heading']}{below}")


def main() -> int:
    """Run the diagnostic.

    Returns:
        0 always — this reports, it does not judge.
    """
    init_pool()
    print(f"min_score currently: {settings.help.min_score}")

    ids: list[int] = []
    try:
        ids = _seed()
        print(f"Seeded {len(ids)} temporary article(s).")
        for q in QUERIES:
            _report(q)
    finally:
        _cleanup(ids)
        print("\nCleaned up temporary rows.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
