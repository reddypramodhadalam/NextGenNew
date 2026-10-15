"""
test_ticketing.py

Gate test for service/ticketing/.

Runs offline by default: it exercises the contract, the registry, the ADF
conversion and the safety guards without touching Atlassian. Nothing is
created anywhere unless --live is passed.

Run from the backend directory:

    python test_ticketing.py           # offline, safe, creates nothing
    python test_ticketing.py --live    # additionally files one real Jira ticket

What it checks:
    1. The fake provider satisfies the contract and records what it is sent.
    2. get_provider() resolves by configuration and rejects unknown names.
    3. The abstraction holds — no Jira-specific symbol is reachable through
       the public surface callers use.
    4. A support project key equal to the requirements project is refused.
    5. Missing configuration produces a clear message, not a stack trace.
    6. Plain text converts to valid ADF, including blank lines.
    7. Context is rendered into the ticket body.
    8. With --live: a real Jira issue is created and its key printed.
"""

from __future__ import annotations

import sys

from service.ticketing import (
    PROVIDERS,
    TicketingError,
    TicketRequest,
    create_ticket,
    get_provider,
)
from service.ticketing.fake import FakeTicketProvider
from service.ticketing.jira import JiraTicketProvider, _text_to_adf


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


def test_fake_provider(failures: list[str]) -> None:
    """The fake provider satisfies the contract and records requests."""
    print("\n  1. fake provider")
    fake = FakeTicketProvider()
    req = TicketRequest(
        category="how-to",
        summary="How do I schedule a suite",
        description="Cannot find the schedules screen.",
        raised_by="selftest",
        context={"route": "/schedules"},
    )
    ref = fake.create_ticket(req)

    _check(failures, "returns a reference with the provider name",
           ref.provider == "fake", ref.provider)
    _check(failures, "returns an external ref", bool(ref.external_ref))
    _check(failures, "records the request", fake.created == [req])
    _check(failures, "tolerates a provider with no URL", ref.external_url is None)


def test_registry(failures: list[str]) -> None:
    """The registry resolves by name and refuses unknown providers."""
    print("\n  2. registry")
    _check(failures, "jira is registered", "jira" in PROVIDERS)
    _check(failures, "fake is registered", "fake" in PROVIDERS)
    _check(failures, "resolves by explicit name",
           get_provider("fake").name == "fake")

    try:
        get_provider("servicenow")
        _check(failures, "unknown provider raises", False,
               "servicenow resolved but is not implemented")
    except TicketingError as e:
        _check(failures, "unknown provider raises a clear error",
               "not registered" in str(e), str(e))


def test_abstraction_holds(failures: list[str]) -> None:
    """No Jira-specific symbol leaks through the surface callers use.

    This is the check that matters most. If it fails, the ticketing layer is
    an abstraction in name only and a move to ServiceNow will not be a
    configuration change.
    """
    print("\n  3. abstraction")
    import service.ticketing as pkg

    leaked = [n for n in pkg.__all__ if "jira" in n.lower()]
    _check(failures, "public surface names no ticketing system",
           not leaked, f"leaked: {leaked}")

    # A caller must be able to file a ticket knowing only the neutral types.
    ref = create_ticket(
        TicketRequest(category="bug", summary="selftest", raised_by="selftest"),
        provider_name="fake",
    )
    _check(failures, "a ticket can be filed without naming a provider type",
           ref.external_ref.startswith("FAKE-"), ref.external_ref)

    # TicketRef carries nothing Jira-shaped that a caller could come to depend
    # on: the identifier is an opaque string, not a parsed key.
    _check(failures, "external_ref is an opaque string",
           isinstance(ref.external_ref, str))


def test_project_guard(failures: list[str]) -> None:
    """A support project equal to the requirements project is refused."""
    print("\n  4. requirements-project guard")
    from core.settings import settings

    j, t = settings.jira, settings.ticketing
    original = t.jira_project_key
    try:
        # Point support at the requirements project and confirm it is refused.
        t.jira_project_key = j.project_key or "KAN"
        try:
            JiraTicketProvider().check_config()
            _check(failures, "refuses the requirements project", False,
                   f"accepted '{t.jira_project_key}' as the support project")
        except TicketingError as e:
            msg = str(e)
            # Missing credentials would raise first and mask the guard.
            if "not configured" in msg:
                print("    SKIP  refuses the requirements project "
                      "(Jira credentials not set)")
            else:
                _check(failures, "refuses the requirements project",
                       "read back as requirements" in msg, msg)
    finally:
        t.jira_project_key = original


def test_missing_config(failures: list[str]) -> None:
    """Absent configuration produces a clear message."""
    print("\n  5. missing configuration")
    from core.settings import settings

    t = settings.ticketing
    original = t.jira_project_key
    try:
        t.jira_project_key = ""
        try:
            JiraTicketProvider().check_config()
            _check(failures, "missing project key is rejected", False)
        except TicketingError as e:
            msg = str(e)
            expected = "JIRA_SUPPORT_PROJECT_KEY" in msg or "not configured" in msg
            _check(failures, "missing project key gives an actionable message",
                   expected, msg)
    finally:
        t.jira_project_key = original


def test_adf(failures: list[str]) -> None:
    """Plain text converts to a valid ADF document."""
    print("\n  6. ADF conversion")
    doc = _text_to_adf("first line\n\nafter a blank")

    _check(failures, "is a doc node", doc.get("type") == "doc")
    _check(failures, "declares version 1", doc.get("version") == 1)
    _check(failures, "produces one node per line", len(doc["content"]) == 3)

    blank = doc["content"][1]
    _check(failures, "blank line has no text node", "content" not in blank)

    first = doc["content"][0]["content"][0]
    _check(failures, "text is carried through", first.get("text") == "first line")

    empty = _text_to_adf("")
    _check(failures, "empty input still yields a valid document",
           empty["content"] == [{"type": "paragraph"}])


def test_body_rendering(failures: list[str]) -> None:
    """Captured context appears in the ticket body."""
    print("\n  7. body rendering")
    body = JiraTicketProvider._body(TicketRequest(
        category="bug",
        summary="s",
        description="Executions page is empty.",
        raised_by="v.agarwal",
        context={"route": "/executions", "application_id": 3, "browser": ""},
    ))

    _check(failures, "includes the description", "Executions page is empty." in body)
    _check(failures, "includes who raised it", "v.agarwal" in body)
    _check(failures, "includes context values", "/executions" in body)
    _check(failures, "omits empty context values", "browser" not in body)


def test_live(failures: list[str]) -> None:
    """Create one real Jira issue. Only runs with --live."""
    print("\n  8. live Jira")
    try:
        ref = create_ticket(
            TicketRequest(
                category="how-to",
                summary="Kavach ticketing self-test - safe to close",
                description=(
                    "Created by test_ticketing.py to verify the Jira write "
                    "path. No action needed; please close."
                ),
                raised_by="selftest",
                context={"route": "/help", "source": "test_ticketing.py"},
            ),
            provider_name="jira",
        )
        print(f"    created: {ref.external_ref}")
        print(f"    url    : {ref.external_url}")
        _check(failures, "live ticket created", bool(ref.external_ref))
    except TicketingError as e:
        _check(failures, "live ticket created", False, str(e))


def main() -> int:
    """Run the gate test.

    Returns:
        0 if every check passed, 1 otherwise.
    """
    live = "--live" in sys.argv
    from core.settings import settings

    print(f"Configured provider: {settings.ticketing.provider}")
    print(f"Registered providers: {', '.join(sorted(PROVIDERS))}")
    print(f"Support project: {settings.ticketing.jira_project_key or '(unset)'}")
    print(f"Requirements project: {settings.jira.project_key or '(unset)'}")

    failures: list[str] = []
    test_fake_provider(failures)
    test_registry(failures)
    test_abstraction_holds(failures)
    test_project_guard(failures)
    test_missing_config(failures)
    test_adf(failures)
    test_body_rendering(failures)

    if live:
        test_live(failures)
    else:
        print("\n  8. live Jira - skipped (pass --live to create a real ticket)")

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