"""
service/help_search/

Retrieval for Help & Support.

The public surface is deliberately small:

    from service.help_search import search, Passage

    passages = search("how do I schedule a jira sync")

Callers never import a strategy module directly. Which implementation answers
is decided by HELP_SEARCH_STRATEGY, so retrieval can move from keyword
matching to embeddings without any caller changing.
"""

from service.help_search.base import Passage, SearchStrategy
from service.help_search.registry import STRATEGIES, get_strategy, search

__all__ = ["Passage", "SearchStrategy", "STRATEGIES", "get_strategy", "search"]
