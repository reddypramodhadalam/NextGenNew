"""
service/ticketing/registry.py

The bounded set of ticketing providers, and the one place a caller resolves
which is active.

Mirrors service/scheduler_jobs.py deliberately: an explicit dict rather than
an if/else, so what can run is enumerable and adding a provider touches this
file only. In a regulated environment that enumerability is a property worth
having — the set of external systems NextGen2 can write to is a short list
anyone can read, not something inferred from control flow.

Nothing above this line imports jira.py. That is what makes moving to
ServiceNow a configuration change:

    1. write service/ticketing/servicenow.py with a class exposing
       `name`, `create_ticket(request)` and `check_config()`
    2. add it to PROVIDERS below
    3. set TICKETING_PROVIDER=servicenow in .env

No schema migration, no route change, no UI change. support_tickets already
stores `provider` and an opaque `external_ref`, so existing rows stay valid
and readable.
"""

from __future__ import annotations

import logging

from core.settings import settings
from service.ticketing.base import (
    TicketingError,
    TicketProvider,
    TicketRef,
    TicketRequest,
)
from service.ticketing.fake import FakeTicketProvider
from service.ticketing.jira import JiraTicketProvider

logger = logging.getLogger(__name__)


# The explicit, bounded set. Keeping it a literal is itself a control: there
# is no path by which NextGen2 writes to a system not named here.
PROVIDERS: dict[str, TicketProvider] = {
    provider.name: provider
    for provider in (JiraTicketProvider(), FakeTicketProvider())
}


def get_provider(name: str | None = None) -> TicketProvider:
    """Resolve the active ticketing provider.

    Args:
        name: Provider key to use. Defaults to settings.ticketing.provider.

    Returns:
        The matching provider.

    Raises:
        TicketingError: If the configured name is not registered. Unlike help
            search — where an unknown strategy falls back to a working default
            — this raises. Silently filing a ticket somewhere other than where
            the operator configured it would be worse than not filing it, and
            the failure is recorded on the support_tickets row either way.
    """
    key = (name or settings.ticketing.provider or "").lower().strip()
    provider = PROVIDERS.get(key)
    if provider is None:
        raise TicketingError(
            f"Ticketing provider '{key}' is not registered. "
            f"Set TICKETING_PROVIDER to one of: {', '.join(sorted(PROVIDERS))}."
        )
    return provider


def create_ticket(request: TicketRequest,
                  provider_name: str | None = None) -> TicketRef:
    """Create a ticket using the configured provider.

    The single entry point callers use. Callers never import a provider
    module, never name Jira, and never branch on which system is in use.

    Args:
        request: What the user is asking for, in NextGen2's terms.
        provider_name: Override the configured provider. Used by tests; not
            exposed through the API, since letting a request choose where it
            is filed would defeat the point of configuring it.

    Returns:
        A reference to the created ticket.

    Raises:
        TicketingError: If the provider is unknown, misconfigured, or the
            external system rejected the request.
    """
    provider = get_provider(provider_name)
    logger.info(
        "Creating ticket via provider '%s' (category=%s)",
        provider.name, request.category,
    )
    return provider.create_ticket(request)


def check_config(provider_name: str | None = None) -> None:
    """Validate the active provider's configuration without creating anything.

    Intended for a preflight check, so a missing project key surfaces on
    startup or a health endpoint rather than at the moment a user is trying to
    report a problem.

    Args:
        provider_name: Override the configured provider.

    Raises:
        TicketingError: If the provider is unknown or misconfigured.
    """
    get_provider(provider_name).check_config()
