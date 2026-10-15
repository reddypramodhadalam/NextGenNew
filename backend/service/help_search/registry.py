"""
service/help_search/registry.py

The bounded set of retrieval strategies, and the one place a caller resolves
which is active.

Mirrors service/scheduler_jobs.py deliberately: an explicit dict rather than
an if/else, so what can run is enumerable and adding a strategy touches this
file only. Nothing above this line imports fts directly — that is what makes
a later move to vector retrieval a configuration change.

Adding vector search later:
    1. write service/help_search/qdrant.py with a class exposing
       `name` and `search(query, limit, approved_only)`
    2. add it to STRATEGIES below
    3. set HELP_SEARCH_STRATEGY=qdrant in .env
No route, service, chat tool or screen changes.
"""

from __future__ import annotations

import logging

from core.settings import settings
from service.help_search.base import Passage, SearchStrategy
from service.help_search.fts import PostgresFTS

logger = logging.getLogger(__name__)


# The explicit, bounded set. Keeping it a literal is itself a control: no
# retrieval path exists that is not listed here.
STRATEGIES: dict[str, SearchStrategy] = {
    strategy.name: strategy
    for strategy in (PostgresFTS(),)
}

# What to fall back to if the configured name is not registered — the one
# strategy with no external dependency, so help search degrades to working
# rather than to broken.
_DEFAULT = PostgresFTS.name


def get_strategy(name: str | None = None) -> SearchStrategy:
    """Resolve the active retrieval strategy.

    Args:
        name: Strategy key to use. Defaults to settings.help.search_strategy.

    Returns:
        The matching strategy. If the configured name is not registered, a
        warning is logged and the default is returned rather than raising —
        a mistyped setting should not take Help & Support offline.
    """
    key = (name or settings.help.search_strategy or _DEFAULT).lower()
    strategy = STRATEGIES.get(key)
    if strategy is None:
        logger.warning(
            "Help search strategy '%s' is not registered; falling back to '%s'. "
            "Registered strategies: %s",
            key, _DEFAULT, ", ".join(sorted(STRATEGIES)),
        )
        return STRATEGIES[_DEFAULT]
    return strategy


def search(query: str, limit: int | None = None,
           approved_only: bool = True) -> list[Passage]:
    """Search help content using the configured strategy.

    The single entry point every caller uses.

    Two filters are applied to what the strategy returns, in this order:

    1. min_score — an absolute floor. Zero by default, because full-text
       ranking is not calibrated: ts_rank_cd measures cover density, so a
       precise all-terms-required match scores an order of magnitude LOWER
       than a loose any-term match on the same corpus. Comparing either to a
       fixed number discards good results. The setting is retained because a
       vector strategy's cosine similarity IS calibrated 0-1, and there an
       absolute floor is exactly the right instrument.

    2. relative_floor — a proportion of the best result in this set. Within a
       single pass the scores ARE comparable, so a passage scoring a fraction
       of the top hit matched on one weak term and is noise. Dropping it
       matters: a local model given two good passages and one irrelevant one
       answers worse than a model given two.

    Args:
        query: The user's question, in their own words.
        limit: Maximum passages to return. Defaults to
            settings.help.max_passages.
        approved_only: When True, draft and retired articles are excluded.

    Returns:
        Passages ordered by descending relevance. Empty means nothing matched,
        which the caller records as a documentation gap rather than answering
        anyway.
    """
    strategy = get_strategy()
    passages = strategy.search(
        query,
        limit=limit or settings.help.max_passages,
        approved_only=approved_only,
    )
    if not passages:
        return []

    floor = settings.help.min_score
    kept = [p for p in passages if p.score >= floor]
    if not kept:
        logger.info(
            "Help search: %d result(s) for %r all scored below min_score %.4f "
            "(best %.6f) - treating as no answer",
            len(passages), query, floor, passages[0].score,
        )
        return []

    kept = _drop_weak_tail(kept, settings.help.relative_floor)

    logger.debug(
        "Help search %r -> %d passage(s), match_type=%s, top score %.6f",
        query, len(kept), kept[0].match_type, kept[0].score,
    )
    return kept


def _drop_weak_tail(passages: list[Passage], relative_floor: float) -> list[Passage]:
    """Remove passages scoring far below the best one in the same pass.

    Only compares passages sharing a match_type, since the precise and broad
    passes score on different scales and a cross-pass ratio would be
    meaningless. In practice a strategy returns one pass's results at a time,
    so this is a guard against a future strategy that mixes them.

    Args:
        passages: Results ordered by descending score.
        relative_floor: Fraction of the top score below which a passage is
            treated as noise. 0.0 disables the filter entirely.

    Returns:
        The passages worth showing, order preserved. The best result is always
        kept, whatever its absolute score.
    """
    if relative_floor <= 0 or not passages:
        return passages

    best_by_type: dict[str, float] = {}
    for p in passages:
        if p.score > best_by_type.get(p.match_type, 0.0):
            best_by_type[p.match_type] = p.score

    kept = [
        p for p in passages
        if p.score >= best_by_type[p.match_type] * relative_floor
    ]

    dropped = len(passages) - len(kept)
    if dropped:
        logger.debug(
            "Help search dropped %d weak passage(s) below %.0f%% of the top score",
            dropped, relative_floor * 100,
        )
    return kept