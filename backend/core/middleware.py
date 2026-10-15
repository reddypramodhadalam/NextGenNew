"""
core/middleware.py

Attaches one correlation id to every request, generated the moment it
arrives and carried through every log line written while handling it.
This is the piece that makes tracing actually possible: search the
logs for one id, see the whole story of one request - which
repository, which test file, which step, in what order.

The id is returned in the response header (X-Correlation-ID) and in
every error response body, so whoever hits an error can hand back
that exact id instead of a timestamp and a guess.
"""

import uuid
from contextvars import ContextVar

from starlette.middleware.base import BaseHTTPMiddleware

_correlation_id: ContextVar[str] = ContextVar("correlation_id", default="-")


def get_correlation_id() -> str:
    """Returns the current request's correlation id, or '-' if called outside a request."""
    return _correlation_id.get()


class CorrelationIdMiddleware(BaseHTTPMiddleware):
    """Generates a correlation id per request and attaches it to the response."""

    async def dispatch(self, request, call_next):
        correlation_id = str(uuid.uuid4())
        token = _correlation_id.set(correlation_id)
        try:
            response = await call_next(request)
            response.headers["X-Correlation-ID"] = correlation_id
            return response
        finally:
            _correlation_id.reset(token)
