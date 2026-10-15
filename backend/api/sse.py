"""
api/sse.py

Server-Sent Events helper used by every long-running endpoint:
survey, generate, execute, and batch.

The orchestrator calls the progress_callback with (event_type, data).
The callback puts the event on an asyncio Queue.
The queue_to_stream generator pulls events off the queue.
The event_stream generator serialises each event as an SSE chunk.
The StreamingResponse in each route consumes event_stream.

Flow: orchestrator -> callback -> queue -> queue_to_stream -> event_stream -> browser
"""
import asyncio
import base64
import datetime
import decimal
import json
import logging
import uuid
from typing import AsyncGenerator

logger = logging.getLogger(__name__)

# How often to emit a keep-alive when no real progress event has arrived.
# Real deployments (server behind a corporate proxy/VPN/load balancer) commonly
# have an idle-connection timeout of 60-120s. A single slow step -- e.g. an
# LLM call stuck in a multi-attempt retry loop against a gateway that is
# itself timing out -- can leave this stream silent for minutes with the
# orchestrator still working, since queue_to_stream previously only wrote a
# byte when a real event arrived. On localhost that silence is invisible
# (nothing on that path has an idle timeout); on a real deployment it gets
# read as the connection being dead, and the browser reports a network
# error even though the backend is still working and would have delivered
# the result. This value must stay comfortably under the shortest idle
# timeout in the real network path; 15s is a conservative default.
SSE_HEARTBEAT_SECONDS = float(__import__("os").environ.get("SSE_HEARTBEAT_SECONDS", "15"))

# Sentinel event type used internally between queue_to_stream and
# event_stream to signal "write a keep-alive", never surfaced to the caller
# of make_progress_queue and never seen by the orchestrator.
_KEEPALIVE = "__sse_keepalive__"


class _SafeEncoder(json.JSONEncoder):
    """
    Custom JSON encoder that handles every type PostgreSQL rows commonly
    return that the stdlib encoder rejects.

    Without this, a single datetime column in a proposal row crashes the
    entire SSE stream with a TypeError before the frontend receives anything.

    Handles:
        datetime / date / time  -> ISO 8601 string (e.g. "2026-07-09T21:56:53")
        Decimal                 -> float           (e.g. cost_usd columns)
        UUID                    -> string          (e.g. future ID fields)
        bytes                   -> base64 string   (e.g. binary columns)
        anything else           -> str(obj)        (never crashes, always streams)
    """

    def default(self, obj):
        if isinstance(obj, (datetime.datetime, datetime.date, datetime.time)):
            return obj.isoformat()
        if isinstance(obj, decimal.Decimal):
            return float(obj)
        if isinstance(obj, uuid.UUID):
            return str(obj)
        if isinstance(obj, bytes):
            return base64.b64encode(obj).decode()
        try:
            return super().default(obj)
        except TypeError:
            # Last resort — convert to string so the stream never dies
            logger.warning(
                "SSE encoder: unhandled type %s, converting to str", type(obj).__name__
            )
            return str(obj)


def _dumps(obj: object) -> str:
    """
    Serialises obj to a JSON string using _SafeEncoder.

    Args:
        obj: Any Python object. Typically a dict with "type" and "data" keys.

    Returns:
        A JSON string safe to embed in an SSE data line.
    """
    return json.dumps(obj, cls=_SafeEncoder)


async def event_stream(generator: AsyncGenerator) -> AsyncGenerator[str, None]:
    """
    Converts an async generator of (event_type, data) tuples into a stream
    of SSE-formatted text chunks ready for a StreamingResponse.

    Each chunk looks like:
        data: {"type": "status", "data": {"message": "Surveying..."}}\n\n

    A keep-alive tuple (see _KEEPALIVE) is written as a bare SSE comment
    line (": ...") instead of a data event — comment lines are valid SSE
    syntax, are silently ignored by the browser's EventSource/fetch-stream
    parser, and exist purely to keep bytes flowing so nothing on the network
    path (proxy, VPN gateway, load balancer) treats the connection as idle
    and drops it while the orchestrator is still legitimately working.

    Args:
        generator: An async generator yielding (event_type, data) tuples.
            event_type: Short string — "status", "step", "agent_step",
                        "complete", "error", or the internal keep-alive
                        sentinel.
            data:       A dict. May contain datetime/Decimal values from
                        Postgres rows — _dumps handles those safely.

    Yields:
        SSE-formatted strings, each ending with a double newline as required
        by the SSE spec so the browser flushes the event immediately.
    """
    async for event_type, data in generator:
        if event_type == _KEEPALIVE:
            logger.debug("SSE keep-alive sent")
            yield ": keep-alive\n\n"
            continue
        payload = _dumps({"type": event_type, "data": data})
        logger.debug("SSE event: type=%s payload_len=%d", event_type, len(payload))
        yield f"data: {payload}\n\n"


def make_progress_queue():
    """
    Creates an asyncio Queue and an async callback function together.

    The callback is passed to the orchestrator, which calls it with
    (event_type, data) after each meaningful step. The queue is passed
    to queue_to_stream, which feeds the SSE generator.

    Using a queue decouples the orchestrator (which runs in a background
    asyncio task) from the SSE response (which runs in the request task).

    Returns:
        Tuple of (queue, callback):
            queue:    asyncio.Queue — pass to queue_to_stream().
            callback: async function(event_type, data) — pass to orchestrator.
    """
    queue: asyncio.Queue = asyncio.Queue()
    owner_loop = asyncio.get_running_loop()

    async def callback(event_type: str, data: dict) -> None:
        """
        Puts one event on the queue. Called by the orchestrator.

        Args:
            event_type: "status", "step", "agent_step", "complete", or "error".
            data:       Dict of event-specific fields.
        """
        # Wizard workers have their own event loop so blocking work does not
        # prevent cancellation requests. Queue wakeups must stay on its owner.
        if asyncio.get_running_loop() is owner_loop:
            await queue.put((event_type, data))
        else:
            future = asyncio.run_coroutine_threadsafe(queue.put((event_type, data)), owner_loop)
            await asyncio.wrap_future(future)

    return queue, callback


async def queue_to_stream(queue: asyncio.Queue) -> AsyncGenerator:
    """
    Pulls events off the queue and yields them one by one.

    Stops automatically when it receives a "complete" or "error" event,
    which signals that the orchestrator has finished (successfully or not).
    Without this stop condition the SSE connection would hang open forever.

    While waiting for the next real event, this now yields a keep-alive
    (see _KEEPALIVE) at most once every SSE_HEARTBEAT_SECONDS. Previously
    this was a bare `await queue.get()` with no timeout, so a single slow
    orchestrator step (e.g. one LLM call stuck retrying against a slow
    gateway) produced total silence on the stream for however long that
    step took — invisible on localhost, but long enough on a real
    deployment (behind a proxy/VPN/load balancer with its own idle-timeout)
    to have the connection dropped and reported to the user as a network
    error, even though the backend was still working correctly.

    Args:
        queue: The asyncio.Queue returned by make_progress_queue().

    Yields:
        (event_type, data) tuples — consumed by event_stream(). A keep-alive
        is yielded as (_KEEPALIVE, None) and is never a terminal event.
    """
    while True:
        try:
            event_type, data = await asyncio.wait_for(
                queue.get(), timeout=SSE_HEARTBEAT_SECONDS
            )
        except asyncio.TimeoutError:
            yield _KEEPALIVE, None
            continue
        logger.debug("SSE queue: dequeued event_type=%s", event_type)
        yield event_type, data
        if event_type in ("complete", "error", "cancelled"):
            logger.info("SSE stream closing: received terminal event type=%s", event_type)
            break