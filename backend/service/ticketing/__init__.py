"""
service/ticketing/

Creating support tickets from inside NextGen2.

The public surface is deliberately provider-neutral:

    from service.ticketing import TicketRequest, create_ticket

    ref = create_ticket(TicketRequest(
        category="bug",
        summary="Execution page shows no results",
        description="...",
        raised_by="v.agarwal",
        context={"route": "/executions", "application_id": 3},
    ))

Callers never import a provider module and never name a ticketing system.
Which system receives the ticket is decided by TICKETING_PROVIDER, so moving
from Jira to ServiceNow is a new provider class plus a configuration change.
"""

from service.ticketing.base import (
    TicketingError,
    TicketProvider,
    TicketRef,
    TicketRequest,
)
from service.ticketing.registry import (
    PROVIDERS,
    check_config,
    create_ticket,
    get_provider,
)

__all__ = [
    "PROVIDERS",
    "TicketProvider",
    "TicketRef",
    "TicketRequest",
    "TicketingError",
    "check_config",
    "create_ticket",
    "get_provider",
]