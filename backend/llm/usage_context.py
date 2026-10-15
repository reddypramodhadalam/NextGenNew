"""
llm/usage_context.py

Labels LLM calls with what they were for (test case generation, script
generation, etc.) without changing any provider or agent signatures.

The calling code wraps its work in track_llm_usage(...). Each provider reads
the active context when it records token usage.
"""

import contextvars
import dataclasses
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Iterator, Optional

# The only purposes the database accepts. Must match ai_calls_purpose_check in migration 018.
PURPOSES = frozenset({
    "test_case_generation",
    "script_generation",
    "healing",
    "chat",
    "crawl",
    "other",
})


@dataclass(frozen=True)
class UsageContext:
    """Attribution details for LLM calls made inside a track_llm_usage() block."""
    purpose: str = "other"
    application_id: Optional[int] = None
    related_to_id: Optional[int] = None   # e.g. the generation id for script generation
    user_id: Optional[int] = None
    run_id: Optional[str] = None          # groups all calls of one survey run


# Holds the label separately for each request, so requests running at the same
# time never mix labels. Defaults to "other", so unlabelled calls are still recorded.
_current: contextvars.ContextVar[UsageContext] = contextvars.ContextVar(
    "llm_usage_context", default=UsageContext()
)


def get_usage_context() -> UsageContext:
    """Returns the label for the current call. Providers use this when saving usage."""
    return _current.get()


def set_related_id(related_to_id: Optional[int]) -> None:
    """
    Updates the related id of the active label. Used when the id only becomes
    known partway through, e.g. a generation row created after the label was set.
    The enclosing track_llm_usage() block still restores the previous label on exit.
    """
    _current.set(dataclasses.replace(_current.get(), related_to_id=related_to_id))


@contextmanager
def track_llm_usage(
    purpose: str,
    application_id: Optional[int] = None,
    related_to_id: Optional[int] = None,
    user_id: Optional[int] = None,
    run_id: Optional[str] = None,
) -> Iterator[None]:
    """
    Labels every LLM call made inside this block.

    Example:
        with track_llm_usage("script_generation", application_id=app_id):
            generate_scripts(...)
    """
    # Rejects unknown labels right away, instead of failing later in the database.
    if purpose not in PURPOSES:
        raise ValueError(f"Unknown LLM usage purpose '{purpose}'. Allowed: {sorted(PURPOSES)}")

    token = _current.set(UsageContext(purpose, application_id, related_to_id, user_id, run_id))
    try:
        yield
    finally:
        # Always restores the previous label, even if the block raised an error.
        _current.reset(token)