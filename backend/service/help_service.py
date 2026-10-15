"""
service/help_service.py

The business logic behind Help & Support.

Four responsibilities, kept in one module because they share the versioning
rules and would drift if separated:

  1. Articles     Authored as markdown, seeded into help_articles, and treated
                  as controlled documents — never edited in place.
  2. Chunking     Turning an article or an uploaded training document into the
                  passages retrieval actually ranks.
  3. Training     Recording who read what, at which version, and who
                  acknowledged it.
  4. Search       Delegating to service/help_search and recording the misses.

The versioning rule is the part worth understanding, because it drives
everything else. Changing an article does not update a row. It inserts a new
row at version+1 and marks the previous one is_stale with superseded_by
pointing forward. That is the same soft-delete discipline used for test cases,
for the same reason: a training record saying "read v2 on 4 August" has to
still mean something after v3 is published, and an auditor has to be able to
see the text as it stood that day.

A consequence worth stating plainly: a new version arrives as a draft. NextGen2
AI will not cite it and it does not count as training material until a person
approves it. Content changing under an approval is exactly what the approval
was supposed to prevent.
"""

from __future__ import annotations

import logging
import re
from typing import Any, Optional

from core.db.connection import get_conn
from core.settings import settings
from service.help_search import search as retrieve

logger = logging.getLogger(__name__)


# Fields that define an article's content. If none of these differ, an upsert
# is a no-op rather than a new version — re-running the seed script must not
# manufacture version churn and invalidate every training record.
_CONTENT_FIELDS = ("title", "category", "summary", "body_md", "sort_order")


# ── articles ─────────────────────────────────────────────────────────────

def list_articles(category: Optional[str] = None,
                  include_drafts: bool = False) -> list[dict]:
    """List the live articles, ordered for reading rather than alphabetically.

    Args:
        category: Restrict to one category, or None for all.
        include_drafts: When True, unapproved articles are included. The Help
            screen passes True and marks them visibly as drafts; NextGen2 AI
            never does.

    Returns:
        Article rows without their body text, which is fetched per article.
    """
    sql = """
        SELECT id, slug, title, category, summary, version, status,
               author, approved_by, approved_at, sort_order, updated_at
        FROM help_articles
        WHERE is_stale = FALSE
          AND (%(include_drafts)s OR status = 'approved')
          AND (%(category)s IS NULL OR category = %(category)s)
        ORDER BY category ASC, sort_order ASC, title ASC
    """
    return _rows(sql, {"category": category, "include_drafts": include_drafts})


def get_article(slug: str, include_drafts: bool = True) -> Optional[dict]:
    """Fetch one live article by slug, including its body.

    Args:
        slug: The article's stable identifier.
        include_drafts: When False, an unapproved article reads as absent.

    Returns:
        The article row, or None if there is no live article with that slug.
    """
    sql = """
        SELECT *
        FROM help_articles
        WHERE slug = %(slug)s
          AND is_stale = FALSE
          AND (%(include_drafts)s OR status = 'approved')
    """
    rows = _rows(sql, {"slug": slug, "include_drafts": include_drafts})
    return rows[0] if rows else None


def get_article_by_id(article_id: int) -> Optional[dict]:
    """Fetch any article by id, including superseded versions.

    Used when resolving a citation recorded against an older version, which
    must still resolve after the article has moved on.

    Args:
        article_id: help_articles.id.

    Returns:
        The article row, or None.
    """
    rows = _rows("SELECT * FROM help_articles WHERE id = %(id)s", {"id": article_id})
    return rows[0] if rows else None


def list_categories(include_drafts: bool = False) -> list[dict]:
    """Count live articles per category, for the navigation.

    Args:
        include_drafts: Whether unapproved articles are counted.

    Returns:
        One row per category with an article_count, ordered by the lowest
        sort_order in each so the navigation follows a teaching order.
    """
    sql = """
        SELECT category,
               COUNT(*)          AS article_count,
               MIN(sort_order)   AS first_sort_order
        FROM help_articles
        WHERE is_stale = FALSE
          AND (%(include_drafts)s OR status = 'approved')
        GROUP BY category
        ORDER BY first_sort_order ASC, category ASC
    """
    return _rows(sql, {"include_drafts": include_drafts})


def upsert_article(slug: str, title: str, category: str, body_md: str,
                   summary: Optional[str] = None,
                   author: Optional[str] = None,
                   status: str = "draft",
                   sort_order: int = 100) -> dict:
    """Create an article, or supersede it with a new version if it changed.

    Never updates in place. If the live article's content is identical the
    call is a no-op, so re-seeding is safe and does not churn versions.

    The whole operation runs in one transaction because the unique index on
    live slugs means the old row must be marked stale before the new one is
    inserted; a failure between the two would leave the article missing.

    Args:
        slug: Stable identifier, shared across every version.
        title: Human title.
        category: Navigation grouping.
        body_md: Markdown body.
        summary: One-line description for the article list.
        author: Who wrote it.
        status: 'draft' or 'approved'. A new version defaults to draft — a
            change to approved content requires a person to approve it again.
        sort_order: Position within its category.

    Returns:
        The live article row, whether newly created, newly versioned, or
        unchanged.
    """
    incoming = {
        "title": title,
        "category": category,
        "summary": summary,
        "body_md": body_md,
        "sort_order": sort_order,
    }

    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT * FROM help_articles WHERE slug = %s AND is_stale = FALSE",
                (slug,),
            )
            existing = _one(cur)

            if existing and all(existing.get(f) == incoming[f] for f in _CONTENT_FIELDS):
                logger.debug("Help article '%s' unchanged at v%s", slug, existing["version"])
                return existing

            version = (existing["version"] + 1) if existing else 1

            if existing:
                # Free the unique live-slug index before inserting the
                # replacement, and take the old version's chunks out of
                # retrieval at the same time.
                cur.execute(
                    "UPDATE help_articles SET is_stale = TRUE, updated_at = now() "
                    "WHERE id = %s",
                    (existing["id"],),
                )
                cur.execute(
                    "UPDATE help_chunks SET is_stale = TRUE WHERE article_id = %s",
                    (existing["id"],),
                )

            cur.execute(
                """
                INSERT INTO help_articles
                    (slug, title, category, summary, body_md, version, status,
                     author, sort_order, effective_from)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s,
                        CASE WHEN %s = 'approved' THEN now() ELSE NULL END)
                RETURNING *
                """,
                (slug, title, category, summary, body_md, version, status,
                 author, sort_order, status),
            )
            created = _one(cur)

            if existing:
                cur.execute(
                    "UPDATE help_articles SET superseded_by = %s WHERE id = %s",
                    (created["id"], existing["id"]),
                )

            _write_article_chunks(cur, created["id"], title, body_md)

    logger.info("Help article '%s' saved at v%s (%s)", slug, created["version"], status)
    return created


def approve_article(article_id: int, approved_by: str) -> Optional[dict]:
    """Mark an article approved, making it citable and countable as training.

    Args:
        article_id: help_articles.id.
        approved_by: Who approved it.

    Returns:
        The updated row, or None if the article does not exist or is stale.
    """
    sql = """
        UPDATE help_articles
        SET status = 'approved',
            approved_by = %(approved_by)s,
            approved_at = now(),
            effective_from = COALESCE(effective_from, now()),
            updated_at = now()
        WHERE id = %(id)s AND is_stale = FALSE
        RETURNING *
    """
    rows = _rows(sql, {"id": article_id, "approved_by": approved_by})
    if rows:
        logger.info("Help article %s approved by %s", article_id, approved_by)
    return rows[0] if rows else None


# ── training documents ───────────────────────────────────────────────────

def list_training_documents() -> list[dict]:
    """List uploaded training documents with their indexed passage counts.

    Returns:
        One row per training document, newest first. chunk_count reveals a
        document whose text never extracted — a scanned PDF with no text
        layer indexes as zero passages and would otherwise look fine.
    """
    sql = """
        SELECT d.id, d.original_filename, d.storage_path, d.uploaded_at,
               length(d.extracted_text) AS text_length,
               COUNT(c.id) FILTER (WHERE c.is_stale = FALSE) AS chunk_count
        FROM documents d
        LEFT JOIN help_chunks c ON c.document_id = d.id
        WHERE d.document_type = 'training'
        GROUP BY d.id
        ORDER BY d.uploaded_at DESC
    """
    return _rows(sql, {})


def index_training_document(document_id: int) -> int:
    """Chunk a training document's extracted text so it becomes searchable.

    Uploading only stores the file and its text. Nothing is retrievable until
    it has been chunked, which is what this does. Safe to re-run: existing
    chunks for the document are retired first.

    Args:
        document_id: documents.id, which must be of type 'training'.

    Returns:
        The number of passages indexed. Zero means the document carried no
        extractable text — most often a scanned PDF with no text layer, which
        the caller should surface rather than treat as success.

    Raises:
        ValueError: If the document does not exist or is not a training
            document.
    """
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT id, document_type, original_filename, extracted_text "
                "FROM documents WHERE id = %s",
                (document_id,),
            )
            doc = _one(cur)
            if not doc:
                raise ValueError(f"Document {document_id} does not exist.")
            if doc["document_type"] != "training":
                raise ValueError(
                    f"Document {document_id} is of type '{doc['document_type']}', "
                    "not 'training'."
                )

            cur.execute(
                "UPDATE help_chunks SET is_stale = TRUE WHERE document_id = %s",
                (document_id,),
            )

            pieces = _split_text(doc["extracted_text"] or "")
            for i, (heading, body) in enumerate(pieces):
                cur.execute(
                    """INSERT INTO help_chunks
                           (source_type, document_id, chunk_index, heading, body)
                       VALUES ('training_doc', %s, %s, %s, %s)""",
                    (document_id, i, heading, body),
                )

    if not pieces:
        logger.warning(
            "Training document %s (%s) produced no passages — it may be a "
            "scanned PDF with no text layer.",
            document_id, doc["original_filename"],
        )
    else:
        logger.info("Indexed %d passage(s) from training document %s",
                    len(pieces), document_id)
    return len(pieces)


# ── training record ──────────────────────────────────────────────────────

def record_view(article_id: Optional[int] = None,
                document_id: Optional[int] = None,
                viewed_by: str = "anonymous") -> dict:
    """Record that someone opened an article or training document.

    The article's version is stored as a value rather than a reference, so the
    record still reads true after the article is superseded.

    Args:
        article_id: help_articles.id, if an article was opened.
        document_id: documents.id, if a training document was opened.
        viewed_by: Who opened it.

    Returns:
        The created help_views row.

    Raises:
        ValueError: If neither or both ids are given.
    """
    if (article_id is None) == (document_id is None):
        raise ValueError("Exactly one of article_id or document_id is required.")

    with get_conn() as conn:
        with conn.cursor() as cur:
            version = None
            if article_id is not None:
                cur.execute("SELECT version FROM help_articles WHERE id = %s",
                            (article_id,))
                row = cur.fetchone()
                version = row[0] if row else None

            cur.execute(
                """INSERT INTO help_views
                       (article_id, document_id, article_version, viewed_by)
                   VALUES (%s, %s, %s, %s)
                   RETURNING *""",
                (article_id, document_id, version, viewed_by),
            )
            return _one(cur)


def acknowledge(view_id: int, viewed_by: str = "anonymous") -> Optional[dict]:
    """Record an explicit "I have read and understood" against a view.

    A view alone is not training evidence — it says a page was opened. An
    acknowledgement is the assertion that carries weight in an audit, which is
    why it is a separate deliberate act rather than a side effect of reading.

    Args:
        view_id: help_views.id to acknowledge.
        viewed_by: Who is acknowledging. Must match the recorded viewer; an
            acknowledgement made on someone else's behalf would be worthless
            as evidence.

    Returns:
        The updated row, or None if no matching unacknowledged view exists.
    """
    sql = """
        UPDATE help_views
        SET acknowledged = TRUE, acknowledged_at = now()
        WHERE id = %(id)s AND viewed_by = %(viewed_by)s
        RETURNING *
    """
    rows = _rows(sql, {"id": view_id, "viewed_by": viewed_by})
    if not rows:
        logger.warning("Acknowledgement rejected for view %s by %s", view_id, viewed_by)
    return rows[0] if rows else None


def training_records(viewed_by: Optional[str] = None,
                     acknowledged_only: bool = False) -> list[dict]:
    """The training record, joined to what was actually read.

    Args:
        viewed_by: Restrict to one person, or None for everyone.
        acknowledged_only: When True, only acknowledged views are returned —
            the subset that constitutes training evidence.

    Returns:
        One row per view, newest first, naming the article or document, the
        version read, and whether it was acknowledged.
    """
    sql = """
        SELECT v.id, v.viewed_by, v.viewed_at, v.acknowledged, v.acknowledged_at,
               v.article_version,
               COALESCE(a.title, d.original_filename) AS source_title,
               COALESCE(a.slug, d.original_filename)  AS source_label,
               CASE WHEN v.article_id IS NOT NULL THEN 'article'
                    ELSE 'training_doc' END           AS source_type,
               a.category                             AS category
        FROM help_views v
        LEFT JOIN help_articles a ON a.id = v.article_id
        LEFT JOIN documents     d ON d.id = v.document_id
        WHERE (%(viewed_by)s IS NULL OR v.viewed_by = %(viewed_by)s)
          AND (NOT %(acknowledged_only)s OR v.acknowledged = TRUE)
        ORDER BY v.viewed_at DESC
    """
    return _rows(sql, {"viewed_by": viewed_by, "acknowledged_only": acknowledged_only})


# ── search ───────────────────────────────────────────────────────────────

def search(query: str, asked_by: str = "anonymous", source: str = "help_search",
           approved_only: bool = True, limit: Optional[int] = None) -> list[dict]:
    """Search help content and record the question if nothing useful came back.

    The recording is the point as much as the search. A question that finds
    nothing is a documentation gap, and capturing it as data means the backlog
    of articles still to write is measured rather than guessed at.

    Args:
        query: The user's question, in their own words.
        asked_by: Who asked.
        source: 'help_search' or 'chat' — where the question came from.
        approved_only: When True, drafts are excluded. NextGen2 AI always passes
            True; an unapproved article is not authority.
        limit: Maximum passages. Defaults to settings.help.max_passages.

    Returns:
        Passages as plain dicts, best first. Empty means nothing matched.
    """
    passages = retrieve(query, limit=limit, approved_only=approved_only)

    if not passages:
        _log_miss(query, asked_by, source, 0)
        return []

    return [p.as_dict() for p in passages]


def search_misses(limit: int = 100) -> list[dict]:
    """The questions that found nothing, most frequent first.

    Args:
        limit: Maximum rows to return.

    Returns:
        Distinct queries with how often they were asked and when last asked —
        the ranked list of articles still to write.
    """
    sql = """
        SELECT lower(query)      AS query,
               COUNT(*)          AS times_asked,
               MAX(asked_at)     AS last_asked,
               COUNT(DISTINCT asked_by) AS distinct_askers
        FROM help_search_misses
        GROUP BY lower(query)
        ORDER BY times_asked DESC, last_asked DESC
        LIMIT %(limit)s
    """
    return _rows(sql, {"limit": limit})


def _log_miss(query: str, asked_by: str, source: str, result_count: int) -> None:
    """Record a question that found nothing useful.

    Failing to log must never fail the request: the user asked a question and
    is owed an answer, or an honest "I don't know", regardless of whether the
    gap was recorded.

    Args:
        query: The question asked.
        asked_by: Who asked.
        source: 'help_search' or 'chat'.
        result_count: How many results were returned, usually zero.
    """
    if source not in ("help_search", "chat"):
        source = "help_search"
    try:
        with get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """INSERT INTO help_search_misses
                           (query, source, result_count, asked_by)
                       VALUES (%s, %s, %s, %s)""",
                    (query[:1000], source, result_count, asked_by),
                )
        logger.info("Help search miss recorded: %r (from %s)", query, source)
    except Exception:  # noqa: BLE001 - logging a gap must not break the answer
        logger.exception("Failed to record help search miss for %r", query)


# ── chunking ─────────────────────────────────────────────────────────────

def _write_article_chunks(cur, article_id: int, title: str, body_md: str) -> None:
    """Split an article body into passages and insert them.

    Args:
        cur: An open cursor, so chunking joins the caller's transaction — an
            article must never exist without its chunks.
        article_id: The article the chunks belong to.
        title: Used as the heading for text appearing before the first
            markdown heading, so an intro paragraph is still citable.
        body_md: The markdown body.
    """
    for i, (heading, body) in enumerate(_split_markdown(body_md, title)):
        cur.execute(
            """INSERT INTO help_chunks
                   (source_type, article_id, chunk_index, heading, body)
               VALUES ('article', %s, %s, %s, %s)""",
            (article_id, i, heading, body),
        )


def _split_markdown(body_md: str, default_heading: str) -> list[tuple[str, str]]:
    """Split markdown into passages, one per section, subdividing long ones.

    Sections are the natural retrieval unit: a heading states what the text
    below answers, which is precisely the signal a question needs to match
    against. The heading is stored with each passage and indexed alongside it.

    Args:
        body_md: The markdown body.
        default_heading: Heading for content before the first '#' heading.

    Returns:
        (heading, body) pairs in document order.
    """
    if not (body_md or "").strip():
        return []

    sections: list[tuple[str, list[str]]] = [(default_heading, [])]
    for line in body_md.split("\n"):
        match = re.match(r"^\s{0,3}(#{1,6})\s+(.*\S)\s*$", line)
        if match:
            sections.append((match.group(2).strip(), []))
        else:
            sections[-1][1].append(line)

    out: list[tuple[str, str]] = []
    for heading, lines in sections:
        text = "\n".join(lines).strip()
        if not text:
            continue
        for part in _subdivide(text):
            out.append((heading, part))
    return out


def _split_text(text: str) -> list[tuple[Optional[str], str]]:
    """Split unstructured extracted text into passages.

    Used for training documents, whose text comes out of a PDF or DOCX with no
    reliable heading structure. Paragraphs are accumulated to the target size
    instead.

    Args:
        text: Extracted plain text.

    Returns:
        (heading, body) pairs, heading always None.
    """
    if not (text or "").strip():
        return []
    return [(None, part) for part in _subdivide(text)]


def _subdivide(text: str) -> list[str]:
    """Break text into pieces of roughly the configured passage size.

    Splits on blank lines and accumulates paragraphs until the target word
    count is reached, so a passage never ends mid-sentence. A single paragraph
    longer than the target is left whole rather than cut arbitrarily —
    truncating one would produce a passage that answers half a question.

    Args:
        text: The text to divide.

    Returns:
        Non-empty pieces in order.
    """
    target = max(50, settings.help.chunk_target_words)
    paragraphs = [p.strip() for p in re.split(r"\n\s*\n", text) if p.strip()]
    if not paragraphs:
        return []

    pieces: list[str] = []
    buffer: list[str] = []
    words = 0

    for para in paragraphs:
        para_words = len(para.split())
        if buffer and words + para_words > target:
            pieces.append("\n\n".join(buffer))
            buffer, words = [], 0
        buffer.append(para)
        words += para_words

    if buffer:
        pieces.append("\n\n".join(buffer))
    return pieces


# ── row helpers ──────────────────────────────────────────────────────────

def _rows(sql: str, params: dict[str, Any]) -> list[dict]:
    """Run a query and return its rows as dicts.

    Args:
        sql: SQL with named placeholders.
        params: Values for those placeholders.

    Returns:
        Rows as dicts, empty if the statement returned none.
    """
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(sql, params)
            if cur.description is None:
                return []
            cols = [d[0] for d in cur.description]
            return [dict(zip(cols, r)) for r in cur.fetchall()]


def _one(cur) -> dict:
    """Read a single row from an open cursor as a dict.

    Args:
        cur: A cursor positioned after a statement returning at most one row.

    Returns:
        The row as a dict, or an empty dict if there was none.
    """
    row = cur.fetchone()
    if row is None:
        return {}
    return dict(zip([d[0] for d in cur.description], row))