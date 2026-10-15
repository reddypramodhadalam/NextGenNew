"""
test_support_routes.py

Gate test for api/routes/support.py.

Split deliberately into two parts, because they can only be tested different
ways:

  Part A, in-process. The handlers are called directly as Python functions.
      This is the only way to exercise the provider-failure path: switching
      providers means mutating the settings singleton, and a running uvicorn
      server is a different process, so a change made here would never reach
      it. Needs no server and no extra dependency.

  Part B, over HTTP. Confirms the routes are actually reachable on the running
      backend and that 'config' and 'categories' are not swallowed as ticket
      ids. Skipped automatically if the backend is not running.

Run from the backend directory:

    python test_support_routes.py           # nothing leaves the process
    python test_support_routes.py --live    # additionally files one real ticket

Every ticket created is deleted afterwards, and the configured provider is
restored whether or not the test passes.
"""

from __future__ import annotations

import json
import sys
import urllib.error
import urllib.request

from starlette.requests import Request as StarletteRequest

from api.routes import support as support_routes
from core.db.connection import get_conn, init_pool
from core.settings import settings


USER = "selftest-support"
BASE = f"http://127.0.0.1:{settings.app.port}/v1"


def _req(who: str = USER) -> StarletteRequest:
    """Build a minimal request carrying an identity header.

    Args:
        who: X-Triggered-By value.

    Returns:
        A Request the handlers can read headers from. Constructed directly
        rather than through a test client, so this test needs no HTTP stack.
    """
    return StarletteRequest({
        "type": "http", "method": "POST", "path": "/",
        "headers": [(b"x-triggered-by", who.encode())],
        "query_string": b"",
    })


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


def _http(method: str, path: str, body=None, who: str = USER):
    """Send one HTTP request to the running backend.

    Args:
        method: HTTP method.
        path: Path below /v1.
        body: JSON-serialisable body, or None.
        who: X-Triggered-By value.

    Returns:
        (status_code, decoded_body), or (None, reason) if the backend is not
        reachable.
    """
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(
        BASE + path, method=method, data=data,
        headers={"X-Triggered-By": who,
                 **({"Content-Type": "application/json"} if data else {})},
    )
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            text = r.read().decode()
            try:
                return r.status, json.loads(text)
            except json.JSONDecodeError:
                return r.status, text
    except urllib.error.HTTPError as e:
        text = e.read().decode()
        try:
            return e.code, json.loads(text)
        except json.JSONDecodeError:
            return e.code, text
    except urllib.error.URLError as e:
        return None, str(e.reason)


def _cleanup() -> None:
    """Delete every ticket this test created."""
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM support_tickets WHERE raised_by = %s", (USER,))


def part_a(failures: list[str]) -> None:
    """In-process checks: creation, validation, and the failure path.

    Args:
        failures: Accumulator for failure descriptions.
    """
    original = settings.ticketing.provider
    try:
        # ── created through the fake provider: nothing leaves the process ──
        print("\n  A1. raising a ticket (fake provider)")
        settings.ticketing.provider = "fake"

        out = support_routes.raise_ticket(
            support_routes.TicketIn(
                category="bug",
                summary="Executions page shows no results",
                description="Ran the smoke suite, the page stayed empty.",
                context={"route": "/executions", "application_id": 3,
                         "browser": "Edge 126", "last_error": "TimeoutException"},
            ),
            _req(),
        )
        _check(failures, "ticket created", "error" not in out, json.dumps(out, default=str)[:200])
        ticket_id = out.get("id")
        _check(failures, "status is submitted", out.get("status") == "submitted",
               str(out.get("status")))
        _check(failures, "external reference recorded", bool(out.get("external_ref")),
               str(out.get("external_ref")))
        _check(failures, "provider recorded on the row", out.get("provider") == "fake",
               str(out.get("provider")))
        _check(failures, "identity taken from the header", out.get("raised_by") == USER,
               str(out.get("raised_by")))
        _check(failures, "context stored",
               (out.get("context") or {}).get("last_error") == "TimeoutException",
               json.dumps(out.get("context"), default=str)[:120])

        one = support_routes.get_ticket(ticket_id)
        _check(failures, "retrievable by id", one.get("id") == ticket_id)

        mine = support_routes.list_tickets(raised_by=USER)
        _check(failures, "appears in the list", any(t["id"] == ticket_id for t in mine))

        # ── validation ──
        print("\n  A2. validation")
        out = support_routes.raise_ticket(
            support_routes.TicketIn(category="bug", summary="   "), _req())
        _check(failures, "empty summary refused", "error" in out,
               json.dumps(out, default=str)[:120])

        out = support_routes.raise_ticket(
            support_routes.TicketIn(category="not-a-real-category",
                                    summary="Filed anyway"), _req())
        _check(failures, "unknown category is filed, not rejected",
               out.get("status") == "submitted", json.dumps(out, default=str)[:150])
        _check(failures, "unknown category falls back to a known one",
               out.get("category") == "how-to", str(out.get("category")))

        # ── the property that matters most ──
        print("\n  A3. a provider failure loses nothing")
        settings.ticketing.provider = "no-such-provider"
        out = support_routes.raise_ticket(
            support_routes.TicketIn(
                category="bug",
                summary="Raised while the provider was unreachable",
                description="This must survive the failure."),
            _req(),
        )
        _check(failures, "response still carries the ticket", bool(out.get("id")),
               json.dumps(out, default=str)[:200])
        _check(failures, "status is failed", out.get("status") == "failed",
               str(out.get("status")))
        _check(failures, "reason recorded", bool(out.get("submit_error")),
               str(out.get("submit_error"))[:120])
        _check(failures, "user is told what happened", "error" in out)

        stored = support_routes.get_ticket(out["id"])
        _check(failures, "the failed ticket is still stored and readable",
               str(stored.get("summary", "")).startswith("Raised while"),
               json.dumps(stored, default=str)[:150])

        # ── preflight ──
        print("\n  A4. preflight")
        settings.ticketing.provider = "fake"
        cfg = support_routes.support_config()
        _check(failures, "reports a usable provider as configured",
               cfg.get("configured") is True, json.dumps(cfg)[:120])

        settings.ticketing.provider = "no-such-provider"
        cfg = support_routes.support_config()
        _check(failures, "reports an unusable provider with a reason",
               cfg.get("configured") is False and bool(cfg.get("reason")),
               json.dumps(cfg)[:160])

    finally:
        settings.ticketing.provider = original


def part_b(failures: list[str], live: bool) -> None:
    """HTTP checks against the running backend.

    Args:
        failures: Accumulator for failure descriptions.
        live: Whether to file one real ticket through the configured provider.
    """
    print("\n  B1. route resolution over HTTP")
    status, body = _http("GET", "/support/config")
    if status is None:
        print(f"    SKIP  backend not reachable at {BASE} ({body})")
        print("          start the backend to check routing")
        return

    _check(failures, "GET /support/config -> 200", status == 200, str(body)[:120])
    print(f"      provider={body.get('provider')} configured={body.get('configured')}"
          f"{' reason=' + str(body.get('reason')) if body.get('reason') else ''}")

    status, cats = _http("GET", "/support/categories")
    _check(failures, "GET /support/categories -> 200", status == 200, str(cats)[:120])
    _check(failures, "categories are enumerable and not read as a ticket id",
           isinstance(cats, list) and len(cats) >= 3, str(cats)[:120])

    status, tickets = _http("GET", "/support/tickets")
    _check(failures, "GET /support/tickets -> 200", status == 200, str(tickets)[:120])

    status, out = _http("GET", "/support/tickets/999999")
    _check(failures, "unknown ticket id returns an error, not a crash",
           status == 200 and "error" in out, str(out)[:120])

    if live:
        print("\n  B2. live provider over HTTP")
        status, out = _http("POST", "/support/tickets", {
            "category": "how-to",
            "summary": "Kavach support route self-test - safe to close",
            "description": "Created by test_support_routes.py. Please close.",
            "context": {"route": "/help", "source": "test_support_routes.py"},
        })
        if isinstance(out, dict) and out.get("status") == "submitted":
            print(f"      created: {out.get('external_ref')}")
            print(f"      url    : {out.get('external_url')}")
        _check(failures, "live ticket filed",
               isinstance(out, dict) and out.get("status") == "submitted",
               str(out)[:200])
    else:
        print("\n  B2. live provider - skipped (pass --live to file a real ticket)")


def main() -> int:
    """Run the gate test.

    Returns:
        0 if every check passed, 1 otherwise.
    """
    init_pool()
    _cleanup()
    live = "--live" in sys.argv
    failures: list[str] = []

    print(f"Configured provider: {settings.ticketing.provider}")
    print(f"Backend for HTTP checks: {BASE}")

    try:
        part_a(failures)
        part_b(failures, live)
    finally:
        _cleanup()
        print("\n  Cleaned up test tickets.")

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