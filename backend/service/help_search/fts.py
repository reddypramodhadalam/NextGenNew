"""
service/help_search/fts.py

Postgres full-text search over help_chunks — the retrieval strategy in use
today.

Why this and not embeddings, for now: the corpus is small and its vocabulary
is largely the product's own (schedule, suite, requirement, sync, execution).
Keyword matching handles that well, needs no model, no extra service and no
extension, and runs against an index that already exists. Embeddings raise
recall on paraphrased questions, which matters as the corpus grows — that is a
separate strategy alongside this one, not a replacement. Production retrieval
usually keeps both and fuses the ranks, because exact terms like 'jira_sync'
or 'requirement_changed' are precisely where keyword search beats a vector.

Two passes, in order:

    precise  every term must appear (websearch_to_tsquery ANDs them)
    broad    terms ORed, run only if the precise pass found nothing

The broad pass exists because on a small corpus a user's phrasing routinely
shares few words with the article that answers them. It is a fallback, not a
peer: the precise pass runs first so a well-worded question is never diluted.

A caution that cost us a round of debugging, recorded here so it is not
rediscovered: ts_rank_cd is a cover-density measure, NOT a calibrated score.
Its magnitude depends on how many terms were required and how far apart they
sit, so the two passes land on completely different scales — a precise match
was measured at 0.009 while a loose broad match on the same corpus scored
0.500. Comparing a score from one pass against a score from the other, or
against any fixed threshold, is meaningless. That is why every Passage carries
match_type, and why filtering happens relative to the best result within a
single pass (see registry.py).
"""

from __future__ import annotations

import logging
import re

from core.db.connection import get_conn
from service.help_search.base import Passage

logger = logging.getLogger(__name__)


# One SELECT, parameterised on the tsquery expression. Written once and reused
# by both passes so the two cannot drift apart.
#
# The source is joined in so a passage can be cited without a second query,
# and so article state is filtered here rather than by the caller — a draft or
# superseded article must not reach the model by accident.
_SQL = """
SELECT
    c.id                AS chunk_id,
    c.source_type       AS source_type,
    c.heading           AS heading,
    c.body              AS body,
    a.id                AS article_id,
    a.slug              AS article_slug,
    a.title             AS article_title,
    a.version           AS article_version,
    d.id                AS document_id,
    d.original_filename AS document_filename,
    ts_rank_cd(c.search_vector, q.query) AS score
FROM help_chunks c
CROSS JOIN {tsquery_expr} AS q(query)
LEFT JOIN help_articles a ON a.id = c.article_id
LEFT JOIN documents     d ON d.id = c.document_id
WHERE c.is_stale = FALSE
  AND c.search_vector @@ q.query
  AND (
        c.source_type <> 'article'
        OR (a.is_stale = FALSE AND (NOT %(approved_only)s OR a.status = 'approved'))
      )
ORDER BY score DESC, c.chunk_index ASC
LIMIT %(limit)s
"""

# Precise pass. websearch_to_tsquery understands ordinary typed input — quoted
# phrases, 'or', a leading '-' to exclude — and never raises on punctuation,
# unlike to_tsquery. Terms are ANDed, so every word must appear.
_PRECISE = "websearch_to_tsquery('english'::regconfig, %(query)s)"

# Broad pass. Terms ORed, so a passage matching most of them still surfaces.
_BROAD = "to_tsquery('english'::regconfig, %(query)s)"

# Words carried by almost every question. They add no signal but do drag the
# broad pass toward irrelevant passages, so they are stripped before ORing.
_NOISE = {
    "how", "do", "does", "did", "i", "we", "you", "to", "the", "a", "an",
    "is", "are", "was", "were", "can", "could", "should", "would", "what",
    "which", "where", "when", "why", "who", "in", "on", "at", "of", "for",
    "and", "or", "my", "me", "it", "this", "that", "please", "help", "with",
    "from", "there", "any", "get", "got", "NextGen2",
}


class PostgresFTS:
    """
    Keyword retrieval over help_chunks using Postgres full-text search.

    Satisfies the SearchStrategy protocol. Holds no state, so the single
    instance in the registry is safe to share across requests.
    """

    name = "fts"

    def search(self, query: str, limit: int = 3,
               approved_only: bool = True) -> list[Passage]:
        """Return the most relevant passages for a query, best first.

        Args:
            query: The user's question, in their own words.
            limit: Maximum number of passages to return.
            approved_only: When True, draft and retired articles are excluded.

        Returns:
            Passages ordered by descending relevance, each stamped with the
            pass that produced it. Empty means neither pass matched, which the
            caller records as a documentation gap rather than answering anyway.
        """
        q = (query or "").strip()
        if not q:
            return []

        rows = self._run(_PRECISE, q, limit, approved_only)
        if rows:
            return [self._to_passage(r, "precise") for r in rows]

        broad = self._broaden(q)
        if not broad:
            logger.info("Help search: %r contained no usable terms", q)
            return []

        rows = self._run(_BROAD, broad, limit, approved_only)
        if not rows:
            logger.info("Help search found nothing for %r (broadened to %r)", q, broad)
        return [self._to_passage(r, "broad") for r in rows]

    def _run(self, tsquery_expr: str, query: str, limit: int,
             approved_only: bool) -> list[dict]:
        """Execute one search pass.

        Args:
            tsquery_expr: SQL fragment producing the tsquery — _PRECISE or
                _BROAD. A module constant, never user input, so it is safe to
                format into the statement; the query text itself is always
                bound as a parameter.
            query: Text bound into the tsquery function.
            limit: Row limit.
            approved_only: Whether to exclude non-approved articles.

        Returns:
            Rows as dicts, ordered by descending score.
        """
        sql = _SQL.format(tsquery_expr=tsquery_expr)
        params = {"query": query, "limit": limit, "approved_only": approved_only}

        with get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute(sql, params)
                cols = [d[0] for d in cur.description]
                return [dict(zip(cols, r)) for r in cur.fetchall()]

    @staticmethod
    def _broaden(query: str) -> str:
        """Turn a question into an OR-joined tsquery expression.

        Strips punctuation and common question words, then joins what remains
        with '|'. Only word characters survive, so nothing the user types can
        reach to_tsquery as syntax — a stray ':' or '&' would otherwise raise.

        Args:
            query: The user's raw question.

        Returns:
            An expression such as 'schedule | jira | sync', or '' if the
            question carried no usable words at all.
        """
        words = re.findall(r"[A-Za-z0-9_]+", query.lower())
        terms = [w for w in words if w not in _NOISE and len(w) > 2]
        if not terms:
            # Everything was filtered out — fall back to the raw words rather
            # than giving up on a question made entirely of common ones.
            terms = [w for w in words if len(w) > 2]

        seen: set[str] = set()
        ordered: list[str] = []
        for t in terms:
            if t not in seen:
                seen.add(t)
                ordered.append(t)
        return " | ".join(ordered)

    @staticmethod
    def _to_passage(row: dict, match_type: str) -> Passage:
        """Map one result row onto a Passage.

        Args:
            row: A row from _run, with the source columns joined in.
            match_type: 'precise' or 'broad' — which pass produced the row.
                Carried through because the two passes score on different
                scales and must never be compared against one another.

        Returns:
            A Passage carrying enough identity to cite its source.
        """
        is_article = row["source_type"] == "article"
        return Passage(
            chunk_id=row["chunk_id"],
            source_type=row["source_type"],
            source_id=(row["article_id"] if is_article else row["document_id"]) or 0,
            label=(row["article_slug"] if is_article else row["document_filename"]) or "",
            title=(row["article_title"] if is_article else row["document_filename"]) or "",
            heading=row["heading"],
            body=row["body"] or "",
            version=row["article_version"] if is_article else None,
            score=float(row["score"] or 0.0),
            match_type=match_type,
        )