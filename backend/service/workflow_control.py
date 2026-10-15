"""Single-process, cooperative cancellation for the test-suite wizard.

This is deliberately not a durable job queue or a user authentication system.
The random control key only controls the flow that issued it. No key, request
payload, application credentials or Entra token is logged or persisted here.
Workers are NEVER Task.cancel()'d: a thread must finish and clean up before its
operation is released, even if its HTTP client has disconnected.
"""

import asyncio
import contextvars
import logging
import os
import secrets
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Callable

logger = logging.getLogger(__name__)
_current = contextvars.ContextVar("wizard_operation", default=None)
_lock = threading.RLock()
_flows = {}
_tasks = set()


class FlowCancelled(BaseException):
    """Control signal, deliberately not swallowed by fallback/error handlers."""


@dataclass
class Flow:
    control_key: str
    cancel_requested: bool = False
    active: int = 0
    touched: float = field(default_factory=time.monotonic)
    failed_cleanup: list = field(default_factory=list)
    lock: threading.RLock = field(default_factory=threading.RLock)

    def snapshot(self):
        with self.lock:
            self.touched = time.monotonic()
            state = "active"
            if self.cancel_requested:
                state = "cancelling" if self.active else (
                    "cleanup_failed" if self.failed_cleanup else "cancelled"
                )
            return {"state": state, "active_operations": self.active}


def create_flow():
    with _lock:
        now = time.monotonic()
        retention = int(os.environ.get("WORKFLOW_IDLE_TTL_SECONDS", "3600"))
        for key, flow in list(_flows.items()):
            with flow.lock:
                if not flow.active and not flow.failed_cleanup and now - flow.touched > retention:
                    del _flows[key]
        if len(_flows) >= int(os.environ.get("WORKFLOW_MAX_TRACKED", "1024")):
            raise RuntimeError("Workflow capacity reached; retry later.")
        flow_id = secrets.token_urlsafe(24)
        flow = Flow(control_key=secrets.token_urlsafe(32))
        _flows[flow_id] = flow
        return {"flow_id": flow_id, "control_key": flow.control_key, **flow.snapshot()}


def find_flow(flow_id, control_key):
    with _lock:
        flow = _flows.get(flow_id)
        if (not flow or not isinstance(control_key, str) or not control_key
            or not secrets.compare_digest(flow.control_key.encode(), control_key.encode())):
            raise LookupError("Workflow unavailable; cancellation cannot be confirmed.")
        return flow


class Operation:
    def __init__(self, flow):
        self.flow = flow
        self.cleanups = []
        self.lock = threading.Lock()
        self.finished = False
        with flow.lock:
            if flow.cancel_requested:
                raise FlowCancelled()
            flow.active += 1

    def finish(self):
        # Called only after all actual work has returned (not at stream EOF).
        with self.lock:
            if self.finished:
                return
            self.finished = True
            cleanups = list(reversed(self.cleanups))
        failures = []
        for cleanup in cleanups:
            try:
                cleanup()
            except Exception:
                logger.exception("Workflow resource cleanup requires retry")
                failures.append(cleanup)
        with self.flow.lock:
            self.flow.failed_cleanup.extend(failures)
            self.flow.active -= 1
            self.flow.touched = time.monotonic()


def current_operation():
    return _current.get()


def cancellation_requested():
    operation = current_operation()
    return bool(operation and operation.flow.cancel_requested)


def checkpoint():
    if cancellation_requested():
        raise FlowCancelled()


@contextmanager
def completion_boundary():
    """Choose a single winner between cancellation and a final save."""
    operation = current_operation()
    if operation is None:
        yield
        return
    with operation.flow.lock:
        checkpoint()
        yield


def track_cleanup(callback: Callable):
    operation = current_operation()
    if operation:
        with operation.lock:
            operation.cleanups.append(callback)


def track_driver(driver):
    """Close only owned drivers; successful existing quit calls stay effective."""
    if current_operation() is None:
        return driver
    original_quit = driver.quit
    closed = False
    lock = threading.Lock()

    def quit_once():
        nonlocal closed
        with lock:
            if not closed:
                original_quit()
                closed = True

    driver.quit = quit_once
    track_cleanup(quit_once)
    return driver


def _retain(task):
    _tasks.add(task)
    task.add_done_callback(_tasks.discard)


def start_operation(run, callback):
    """Reserve before the HTTP handler returns; run blocking phases off-loop.

    No workflow context means the original detached-task behavior is retained.
    The worker owns its event loop; asyncio.run also joins its default executor.
    """
    parent = current_operation()
    if parent is None:
        _retain(asyncio.create_task(run()))
        return
    operation = Operation(parent.flow)

    def worker():
        token = _current.set(operation)
        try:
            try:
                asyncio.run(run())
            except FlowCancelled:
                asyncio.run(callback("cancelled", {"message": "Flow cancellation requested."}))
        finally:
            operation.finish()
            _current.reset(token)

    _retain(asyncio.create_task(asyncio.to_thread(worker)))


def request_cancel(flow):
    # New operation admission and this flag use the same lock.
    with flow.lock:
        flow.cancel_requested = True
        if not flow.active and flow.failed_cleanup:
            cleanups, flow.failed_cleanup = flow.failed_cleanup, []
            flow.active += 1

            def retry():
                failures = []
                for cleanup in cleanups:
                    try:
                        cleanup()
                    except Exception:
                        logger.exception("Workflow cleanup retry failed")
                        failures.append(cleanup)
                with flow.lock:
                    flow.failed_cleanup.extend(failures)
                    flow.active -= 1

            threading.Thread(target=retry, name="workflow-cleanup", daemon=True).start()
        return flow.snapshot()


async def tracked_request(flow, handler, request):
    """Keep ordinary saves tracked until their handler has really returned."""
    operation = Operation(flow)
    token = _current.set(operation)
    try:
        # Shield the handler against client disconnect cancellation. In
        # particular, abandoning a response cannot release a live save thread.
        task = asyncio.create_task(handler(request))
        disconnected = False
        while True:
            try:
                response = await asyncio.shield(task)
                break
            except asyncio.CancelledError:
                if task.cancelled():
                    raise
                disconnected = True
        if disconnected:
            raise asyncio.CancelledError()
        return response
    finally:
        cleanup = asyncio.create_task(asyncio.to_thread(operation.finish))
        try:
            while True:
                try:
                    await asyncio.shield(cleanup)
                    break
                except asyncio.CancelledError:
                    if cleanup.cancelled():
                        raise
        finally:
            _current.reset(token)