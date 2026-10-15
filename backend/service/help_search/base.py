"""
service/help_search/base.py

The contract every help-search implementation satisfies, and the shape of a
result.

This module is the seam. Retrieval today is Postgres full-text search over
help_chunks. It will later be embeddings in a vector store (Qdrant or
otherwise), and most likely both at once with the ranks fused. None of that is
visible above this line: the route, the chat tool and the UI call
`search(query, limit)` and receive a list of Passage. Changing how matching
works means adding a file next to fts.py and changing one setting.

Nothing here imports psycopg2, a vector client, or anything else
implementation-specific. That is deliberate — it is what keeps the seam real
rather than nominal.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional, Protocol


@dataclass
class Passage:
    """
    One retrieved passage, with everything needed to cite it.

    Citation is not decoration here. An answer from NextGen2 AI is advisory; the
    approved article or training document is the authority. So every passage
    carries enough identity for the reader to go and check the source, and for
    an auditor to see which version of which document an answer came from.

    Attributes:
        chunk_id: help_chunks.id — the retrieval unit's primary key. This is
            also the join key a future vector store would carry in its point
            payload, so a passage retrieved by either method is the same row.
        source_type: 'article' or 'training_doc'.
        source_id: help_articles.id or documents.id, per source_type.
        label: Stable identifier shown to the user — an article slug, or a
            training document's original filename.
        title: The source's human title.
        heading: Nearest heading within the source, or None.
        body: The passage text itself. This is what the model is given.
        version: Article version, or None for a training document.
        score: Relevance from the pass that produced it. Comparable ONLY
            against passages from the same pass — see match_type. For
            full-text search this is ts_rank_cd, a cover-density measure whose
            magnitude depends on how many terms were required and how far
            apart they sit, so it carries no absolute meaning and is never
            compared against a fixed threshold.
        match_type: Which pass produced this passage: 'precise' when every
            term had to be present, 'broad' when the terms were ORed after the
            precise pass found nothing. Recorded because the two run on
            different numeric scales — a 0.009 precise hit is a better match
            than a 0.500 broad one — and because a future hybrid strategy
            needs to know which retriever produced a result in order to fuse
            ranks.
    """

    chunk_id: int
    source_type: str
    source_id: int
    label: str
    title: str
    heading: Optional[str]
    body: str
    version: Optional[int]
    score: float
    match_type: str = "precise"

    def citation(self) -> str:
        """Build the short source reference shown alongside an answer.

        Returns:
            A string such as 'Scheduling a run (v2)' for an article, or
            'NextGen2 onboarding.pdf' for a training document.
        """
        if self.source_type == "article":
            return f"{self.title} (v{self.version})" if self.version else self.title
        return self.label or self.title

    def as_dict(self) -> dict:
        """Serialise for an API response or a chat tool result.

        Returns:
            A plain dict; no database types, safe to hand to FastAPI or to
            embed in a tool result string.
        """
        return {
            "chunk_id": self.chunk_id,
            "source_type": self.source_type,
            "source_id": self.source_id,
            "label": self.label,
            "title": self.title,
            "heading": self.heading,
            "body": self.body,
            "version": self.version,
            "score": round(self.score, 5),
            "match_type": self.match_type,
            "citation": self.citation(),
        }


class SearchStrategy(Protocol):
    """
    The contract a retrieval implementation satisfies.

    Duck-typed via typing.Protocol, matching the pattern already used for
    scheduler jobs: a strategy class inherits nothing, it just provides these
    members. Adding vector or hybrid retrieval is a new class plus one entry
    in registry.py.
    """

    name: str

    def search(self, query: str, limit: int = 3,
               approved_only: bool = True) -> list[Passage]:
        """Return the most relevant passages for a query, best first.

        Args:
            query: The user's question, in their own words.
            limit: Maximum number of passages to return.
            approved_only: When True, draft and retired articles are excluded.
                NextGen2 AI always calls with True — an unapproved article is
                not authority and must never be cited as if it were. The Help
                screen may pass False so an author can find their own draft.

        Returns:
            Passages ordered by descending relevance, already filtered to
            those the strategy considers good enough to answer from. An empty
            list means nothing matched well enough, which the caller records
            as a documentation gap rather than answering anyway.

            Relevance gating belongs to the strategy, not to its callers: what
            counts as a good enough match is specific to how the strategy
            matches, and a threshold that suits keyword ranking is meaningless
            for cosine distance.
        """
        ...