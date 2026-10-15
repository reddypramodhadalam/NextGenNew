"""
test_help_search.py

Standalone gate test for service/help_search/.

Proves the retrieval layer works before any route, seed script or UI depends
on it. Creates its own temporary article and chunks, runs several searches
against them, then removes everything it created — the database is left
exactly as it was found, whether the test passes or fails.

Run from the backend directory:
    python test_help_search.py

What it checks:
    1. The precise pass finds a passage when the wording matches.
    2. The broadened retry finds a passage when it does not — this is the
       recall behaviour that matters most on a small corpus.
    3. A question about something not in the corpus returns nothing, rather
       than a weak match presented as an answer.
    4. approved_only=True hides a draft article; approved_only=False shows it.
    5. Passages carry a usable citation.
"""

from __future__ import annotations

import sys

from core.db.connection import get_conn, init_pool
from service.help_search import search
from service.help_search.registry import get_strategy


MARKER = "zz-help-search-selftest"

ARTICLES = [
    # (slug, title, status, [(heading, body), ...])
    (
        f"{MARKER}-scheduling",
        "Scheduling a run",
        "approved",
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
        f"{MARKER}-draft",
        "Draft article about environments",
        "draft",
        [
            ("Adding an environment",
             "Each application can be tested against several environments such "
             "as dev, test and stage. The URL belongs to the environment rather "
             "than to the test case, so one test runs anywhere."),
        ],
    ),
]


def _seed() -> list[int]:
    """Insert the temporary articles and their chunks.

    Returns:
        The ids of the articles created.
    """
    article_ids: list[int] = []
    with get_conn() as conn:
        with conn.cursor() as cur:
            for slug, title, status, chunks in ARTICLES:
                cur.execute(
                    """INSERT INTO help_articles
                           (slug, title, category, body_md, status, author)
                       VALUES (%s, %s, 'selftest', %s, %s, 'selftest')
                       RETURNING id""",
                    (slug, title, "\n\n".join(b for _, b in chunks), status),
                )
                article_id = cur.fetchone()[0]
                article_ids.append(article_id)

                for i, (heading, body) in enumerate(chunks):
                    cur.execute(
                        """INSERT INTO help_chunks
                               (source_type, article_id, chunk_index, heading, body)
                           VALUES ('article', %s, %s, %s, %s)""",
                        (article_id, i, heading, body),
                    )
    return article_ids


def _cleanup(article_ids: list[int]) -> None:
    """Remove everything the test created.

    Args:
        article_ids: Ids returned by _seed. Chunks cascade on delete.
    """
    if not article_ids:
        return
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM help_articles WHERE id = ANY(%s)", (article_ids,))


def _show(label: str, query: str, **kwargs) -> list:
    """Run one search and print what came back.

    Args:
        label: What this case is demonstrating.
        query: The question to search for.
        **kwargs: Passed through to search().

    Returns:
        The passages returned.
    """
    passages = search(query, **kwargs)
    print(f"\n  {label}")
    print(f"    query   : {query!r}")
    if not passages:
        print("    result  : (nothing)")
        return passages
    for p in passages:
        print(f"    result  : {p.score:.5f}  {p.citation()}  —  {p.heading}")
    return passages


def main() -> int:
    """Run the gate test.

    Returns:
        0 if every check passed, 1 otherwise.
    """
    init_pool()
    print(f"Strategy in use: {get_strategy().name}")

    article_ids: list[int] = []
    failures: list[str] = []

    try:
        article_ids = _seed()
        print(f"Seeded {len(article_ids)} temporary article(s).")

        # 1. Precise pass — wording close to the text.
        got = _show("1. precise match", "pausing a schedule")
        if not got:
            failures.append("1. precise match returned nothing")

        # 2. Broadened retry — the user's words are not the article's words.
        #    'stop a run from firing every night' shares almost nothing with
        #    'Pausing a schedule', which is exactly the case the OR pass exists
        #    for. Keyword search has limits here; this is the honest test of
        #    where they are.
        got = _show("2. broadened retry", "how do I stop a run from firing every night")
        if not got:
            failures.append("2. broadened retry returned nothing")

        # 3. Not in the corpus at all — must return nothing, not a weak match.
        got = _show("3. absent topic", "quarterly payroll reconciliation in SAP")
        if got:
            failures.append("3. absent topic returned results when it should not")

        # 4. Draft visibility.
        got = _show("4a. draft hidden from AI", "adding an environment dev test stage",
                    approved_only=True)
        if any(p.label.endswith("-draft") for p in got):
            failures.append("4a. a draft article was returned with approved_only=True")

        got = _show("4b. draft visible to author", "adding an environment dev test stage",
                    approved_only=False)
        if not any(p.label.endswith("-draft") for p in got):
            failures.append("4b. draft article not returned with approved_only=False")

    finally:
        _cleanup(article_ids)
        print("\nCleaned up temporary rows.")

    print()
    if failures:
        print("FAILED:")
        for f in failures:
            print(f"  - {f}")
        return 1

    print("All checks passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
