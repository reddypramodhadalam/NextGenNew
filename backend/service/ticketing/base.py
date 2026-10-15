"""
service/ticketing/base.py

The contract every ticketing provider satisfies, and the shapes passed across
it.

This module is the seam that keeps NextGen2 independent of any one ticketing
system. Baxter runs ServiceNow; the demo runs Jira; a future deployment might
run neither. None of that reaches the routes, the database or the UI: they
deal in TicketRequest and TicketRef, and the provider is resolved at runtime
from configuration.

Nothing here imports urllib, a Jira client, or anything else
implementation-specific. That is deliberate — it is what makes the seam real
rather than nominal. If a provider-specific import ever appears above this
line, the abstraction has quietly stopped working, and test_ticketing.py
exists to catch exactly that.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional, Protocol


class TicketingError(RuntimeError):
    """
    Raised when a ticket cannot be created.

    Carries a message fit to show a user. The caller records the failure on
    the support_tickets row (status='failed', submit_error set) rather than
    discarding it, so a ticket that could not be filed is still visible
    afterwards instead of silently lost.
    """


@dataclass
class TicketRequest:
    """
    What the user is asking for, in NextGen2's own terms.

    Deliberately free of any provider's vocabulary. There is no issue type, no
    incident-versus-request, no priority scheme here — those are each
    provider's own model, and translating into them is the provider's job.
    The user picks a plain-English category and is never asked to understand
    ITIL or Jira's schema.

    Attributes:
        category: Plain-English kind of request, e.g. 'how-to', 'bug',
            'access', 'data'. Providers map this onto their own object model.
        summary: One-line title.
        description: The user's own account of the problem.
        raised_by: Who is asking. Currently supplied by the caller and
            defaulting to 'anonymous', matching how accepted_by works
            elsewhere in NextGen2; it becomes a real identity when SSO lands.
        context: Automatically captured technical detail — route,
            application_id, execution_id, browser, last error. Collected by
            the UI so the ticket is actionable without a follow-up
            conversation. Rendered into the ticket body by the provider.
    """

    category: str
    summary: str
    description: str = ""
    raised_by: str = "anonymous"
    context: dict[str, Any] = field(default_factory=dict)


@dataclass
class TicketRef:
    """
    Where the created ticket lives, in the external system.

    Intentionally opaque: an identifier and a link, with no assumption about
    their format. 'KVSUP-14' and 'INC0012345' are both just strings here,
    which is why moving between ticketing systems needs no schema migration.

    Attributes:
        provider: Which provider created it — matches a key in registry.py.
        external_ref: The external identifier, as that system displays it.
        external_url: Deep link to the ticket, or None if the provider cannot
            construct one.
        external_status: The external system's own status at creation time,
            if it reports one.
        raw: The provider's unmodified response, kept for diagnostics. Never
            parsed by callers — anything callers need is promoted to a field.
    """

    provider: str
    external_ref: str
    external_url: Optional[str] = None
    external_status: Optional[str] = None
    raw: dict[str, Any] = field(default_factory=dict)


class TicketProvider(Protocol):
    """
    The contract a ticketing implementation satisfies.

    Duck-typed via typing.Protocol, matching the pattern already used for
    scheduler jobs and help-search strategies: an implementation inherits
    nothing, it just provides these members. Adding ServiceNow is a new class
    plus one entry in registry.py.
    """

    name: str

    def create_ticket(self, request: TicketRequest) -> TicketRef:
        """Create a ticket in the external system.

        Args:
            request: What the user is asking for, in NextGen2's terms.

        Returns:
            A reference to the created ticket.

        Raises:
            TicketingError: If the provider is misconfigured or the external
                system rejected the request. The message is fit to show a
                user.
        """
        ...

    def check_config(self) -> None:
        """Validate configuration without creating anything.

        Lets a misconfiguration surface on a preflight check rather than at
        the moment a user is trying to report a problem — which is the worst
        possible time to discover that the project key is unset.

        Raises:
            TicketingError: If required configuration is missing or unusable.
        """
        ...
