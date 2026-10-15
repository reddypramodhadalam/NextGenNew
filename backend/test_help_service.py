"""
test_help_service.py

Gate test for service/help_service.py.

Creates its own temporary articles and a temporary training document, exercises
every path, then removes everything it created. The database is left exactly
as it was found, whether the test passes or fails.

Run from the backend directory:
    python test_help_service.py

What it checks:
    1. A new article is created at v1 with its chunks.
    2. Re-seeding identical content is a no-op — no version churn, so training
       records are not invalidated by running the seed script twice.
    3. Changed content supersedes: v2 live, v1 stale with superseded_by set,
       v1's chunks retired, and the new version arrives as a draft.
    4. A draft is invisible to search until approved.
    5. Views record the version that was on screen, and survive supersession.
    6. Acknowledgement is refused on someone else's behalf.
    7. A training document is chunked and becomes searchable alongside
       articles.
    8. A question that finds nothing is recorded as a documentation gap.
"""

from __future__ import annotations

import sys

from core.db.connection import get_conn, init_pool
from service import help_service as hs


MARKER = "zz-help-service-selftest"
USER = "selftest-user"

V1_BODY = """Kavach reads a BRD and proposes manual test cases for review.

## Uploading a requirements document

Open the application, choose Upload, and pick the document. Word and PDF are
both read, including text held inside tables.
"""

V2_BODY = V1_BODY + """
## Reviewing proposals

Every proposal is shown beside the requirement it came from. Nothing is
generated until you accept it.
"""

TRAINING_TEXT = """Kavach onboarding for new quality engineers.

The platform separates requirements from test cases and test cases from
scripts, with a person approving each step. Nothing moves forward on its own.

Zebra crossings and marmalade have nothing to do with this platform, and this
sentence exists only so a search for them finds this passage and nothing else.
"""


def _check(failures: list[str], label: str, condition: bool, detail: str = "") -> None:
    """Record the outcome of one assertion.

    Args:
        failures: Accumulator for failure descriptions.
        label: What was being checked.
        condition: Whether it held.
        detail: Extra context printed on failure.
    """
    print(f"    {'PASS' if condition else 'FAIL'}  {label}")
    if not condition:
        failures.append(f"{label}{f' - {detail}' if detail else ''}")


def _chunk_count(article_id: int, stale: bool = False) -> int:
    """Count an article's chunks.

    Args:
        article_id: help_articles.id.
        stale: Whether to count retired chunks instead of live ones.

    Returns:
        The number of matching chunks.
    """
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT COUNT(*) FROM help_chunks "
                "WHERE article_id = %s AND is_stale = %s",
                (article_id, stale),
            )
            return cur.fetchone()[0]


def _create_training_doc() -> int:
    """Insert a temporary training document row.

    Returns:
        The new documents.id.
    """
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """INSERT INTO documents
                       (document_type, original_filename, storage_path, extracted_text)
                   VALUES ('training', %s, %s, %s)
                   RETURNING id""",
                (f"{MARKER}-onboarding.pdf", f"/tmp/{MARKER}.pdf", TRAINING_TEXT),
            )
            return cur.fetchone()[0]


def _cleanup(doc_id: int | None) -> None:
    """Remove every row the test created.

    Args:
        doc_id: The training document id, if one was created.
    """
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "DELETE FROM help_views WHERE article_id IN "
                "(SELECT id FROM help_articles WHERE slug LIKE %s)",
                (f"{MARKER}%",),
            )
            if doc_id:
                cur.execute("DELETE FROM help_views WHERE document_id = %s", (doc_id,))
            # superseded_by is self-referential, so break the links first.
            cur.execute(
                "UPDATE help_articles SET superseded_by = NULL WHERE slug LIKE %s",
                (f"{MARKER}%",),
            )
            cur.execute("DELETE FROM help_articles WHERE slug LIKE %s", (f"{MARKER}%",))
            if doc_id:
                cur.execute("DELETE FROM documents WHERE id = %s", (doc_id,))
            cur.execute("DELETE FROM help_search_misses WHERE asked_by = %s", (USER,))


def main() -> int:
    """Run the gate test.

    Returns:
        0 if every check passed, 1 otherwise.
    """
    init_pool()
    failures: list[str] = []
    doc_id: int | None = None
    slug = f"{MARKER}-brd"

    try:
        # 1. create
        print("\n  1. create")
        v1 = hs.upsert_article(
            slug=slug, title="Uploading a BRD", category="Getting started",
            body_md=V1_BODY, summary="How requirements enter Kavach",
            author=USER, status="approved", sort_order=10,
        )
        _check(failures, "created at version 1", v1["version"] == 1, str(v1.get("version")))
        _check(failures, "is live", v1["is_stale"] is False)
        _check(failures, "chunks written", _chunk_count(v1["id"]) >= 2,
               str(_chunk_count(v1["id"])))

        # 2. idempotent re-seed
        print("\n  2. re-seeding identical content")
        again = hs.upsert_article(
            slug=slug, title="Uploading a BRD", category="Getting started",
            body_md=V1_BODY, summary="How requirements enter Kavach",
            author=USER, status="approved", sort_order=10,
        )
        _check(failures, "no new version created", again["version"] == 1,
               str(again["version"]))
        _check(failures, "same row returned", again["id"] == v1["id"])

        # 5a. view recorded against v1, before it is superseded
        print("\n  3. view recorded")
        view = hs.record_view(article_id=v1["id"], viewed_by=USER)
        _check(failures, "records the version on screen", view["article_version"] == 1,
               str(view.get("article_version")))

        # 3. supersede
        print("\n  4. changed content supersedes")
        v2 = hs.upsert_article(
            slug=slug, title="Uploading a BRD", category="Getting started",
            body_md=V2_BODY, summary="How requirements enter Kavach",
            author=USER, sort_order=10,
        )
        _check(failures, "new version is 2", v2["version"] == 2, str(v2["version"]))
        _check(failures, "new version arrives as a draft", v2["status"] == "draft",
               v2["status"])

        old = hs.get_article_by_id(v1["id"])
        _check(failures, "previous version marked stale", old["is_stale"] is True)
        _check(failures, "previous version points forward",
               old["superseded_by"] == v2["id"], str(old.get("superseded_by")))
        _check(failures, "previous version's chunks retired",
               _chunk_count(v1["id"]) == 0 and _chunk_count(v1["id"], stale=True) > 0)
        _check(failures, "only one live article for the slug",
               len([a for a in hs.list_articles(include_drafts=True)
                    if a["slug"] == slug]) == 1)

        # 5b. the older view still reads true
        recs = [r for r in hs.training_records(viewed_by=USER)
                if r["source_label"] == slug]
        _check(failures, "training record still shows v1 after supersession",
               bool(recs) and recs[0]["article_version"] == 1,
               str(recs[0]["article_version"]) if recs else "no record")

        # 4. draft invisible to search until approved
        print("\n  5. approval gates retrieval")
        hits = hs.search("reviewing proposals accept requirement",
                         asked_by=USER, approved_only=True)
        _check(failures, "draft is not returned to Kavach AI",
               not any(h["label"] == slug for h in hits))

        drafts = hs.search("reviewing proposals accept requirement",
                           asked_by=USER, approved_only=False)
        _check(failures, "draft is visible to an author",
               any(h["label"] == slug for h in drafts))

        approved = hs.approve_article(v2["id"], approved_by=USER)
        _check(failures, "approval records who and when",
               approved["approved_by"] == USER and approved["approved_at"] is not None)

        hits = hs.search("reviewing proposals accept requirement",
                         asked_by=USER, approved_only=True)
        _check(failures, "approved article is now returned",
               any(h["label"] == slug for h in hits))

        # 6. acknowledgement
        print("\n  6. acknowledgement")
        wrong = hs.acknowledge(view["id"], viewed_by="someone-else")
        _check(failures, "cannot acknowledge on another person's behalf", wrong is None)

        ack = hs.acknowledge(view["id"], viewed_by=USER)
        _check(failures, "acknowledgement recorded",
               bool(ack) and ack["acknowledged"] is True)
        _check(failures, "acknowledged-only filter works",
               any(r["id"] == view["id"]
                   for r in hs.training_records(viewed_by=USER, acknowledged_only=True)))

        # 7. training document
        print("\n  7. training document")
        doc_id = _create_training_doc()
        count = hs.index_training_document(doc_id)
        _check(failures, "document chunked", count >= 1, str(count))

        listed = [d for d in hs.list_training_documents() if d["id"] == doc_id]
        _check(failures, "appears in the training list", bool(listed))
        _check(failures, "reports its passage count",
               bool(listed) and listed[0]["chunk_count"] == count)

        found = hs.search("zebra crossings marmalade", asked_by=USER)
        _check(failures, "training document is searchable",
               any(h["source_type"] == "training_doc" for h in found),
               str([h["source_type"] for h in found]))

        # 8. miss logging
        print("\n  8. documentation gaps")
        before = len(hs.search_misses())
        empty = hs.search("quarterly payroll reconciliation in SAP", asked_by=USER)
        _check(failures, "absent topic returns nothing", empty == [])
        _check(failures, "the gap is recorded", len(hs.search_misses()) > before)
        _check(failures, "the recorded query is retrievable",
               any("payroll" in m["query"] for m in hs.search_misses()))

    finally:
        _cleanup(doc_id)
        print("\n  Cleaned up temporary rows.")

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