"""
service/ticketing/fake.py

An in-memory ticketing provider.

Not a mock bolted on beside the code — a real implementation of the same
contract, registered alongside Jira. It earns its place twice:

1. It is the test that the abstraction is genuine. If any Jira-specific
   import, field or assumption has leaked upward into the route or the
   service layer, raising a ticket with TICKETING_PROVIDER=fake breaks, and
   the leak is found immediately rather than during a ServiceNow migration.

2. It makes the support flow demonstrable and testable with no Atlassian
   account, no network, and no risk of littering a real project with test
   issues.

It is safe to leave registered in production: selecting it requires setting
TICKETING_PROVIDER=fake explicitly, and it logs loudly that nothing left the
process.
"""

from __future__ import annotations

import logging
from itertools import count

from service.ticketing.base import TicketRef, TicketRequest

logger = logging.getLogger(__name__)


class FakeTicketProvider:
    """
    Records tickets in memory and returns plausible references.

    Attributes:
        created: Every request handled by this instance, in order. Tests read
            it to assert what the caller actually sent.
    """

    name = "fake"

    def __init__(self) -> None:
        """Initialise an empty in-memory ticket store."""
        self.created: list[TicketRequest] = []
        self._counter = count(1)

    def create_ticket(self, request: TicketRequest) -> TicketRef:
        """Record a ticket without contacting anything.

        Args:
            request: What the user is asking for, in NextGen2's terms.

        Returns:
            A reference with a synthetic identifier. external_url is None,
            because there is nothing to link to — callers must cope with a
            missing link, and this is what proves they do.
        """
        self.created.append(request)
        ref = f"FAKE-{next(self._counter)}"
        logger.warning(
            "FakeTicketProvider recorded %s in memory (category=%s, raised_by=%s). "
            "No ticket was created in any external system.",
            ref, request.category, request.raised_by,
        )
        return TicketRef(
            provider=self.name,
            external_ref=ref,
            external_url=None,
            external_status="recorded",
            raw={"summary": request.summary, "context": request.context},
        )

    def check_config(self) -> None:
        """Validate configuration. There is none, so this always succeeds."""
        return None

    def reset(self) -> None:
        """Discard recorded tickets, so one test cannot affect the next."""
        self.created.clear()
