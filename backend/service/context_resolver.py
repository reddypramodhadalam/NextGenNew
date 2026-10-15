"""
service/context_resolver.py

CONTEXT RESOLVER

Resolves a user's selection (application name + environment name) into
everything actually needed to act: the real URL, which credential env
vars to read, and whether production confirmation is required.

Fully deterministic — identical input always produces identical output.
Never calls an LLM. All values come from config/applications.yaml.
"""

import logging
import os
from dataclasses import dataclass
from typing import Optional

from core.settings import settings

logger = logging.getLogger(__name__)


@dataclass
class ResolvedContext:
    """
    Everything needed to interact with one application environment.

    Attributes:
        application_key: The config key, e.g. 'netra'.
        environment_key: The environment key, e.g. 'default'.
        display_name: Human-readable application name.
        url: The resolved URL for this environment.
        service_user: Service account username, or None if not configured.
        service_password: Service account password, or None if not configured.
        requires_production_confirmation: True when this environment is
            production and generation must be explicitly confirmed.
    """
    application_key: str
    environment_key: str
    display_name: str
    url: str
    service_user: Optional[str]
    service_password: Optional[str]
    requires_production_confirmation: bool


class UnknownApplicationError(ValueError):
    """Raised when the application key is not in config/applications.yaml."""
    pass


class UnknownEnvironmentError(ValueError):
    """Raised when the environment key is not defined for that application."""
    pass


def resolve(application_key: str, environment_key: str) -> ResolvedContext:
    """
    Resolves an application + environment selection into a ResolvedContext.

    Args:
        application_key: The application's config key, e.g. 'netra'.
            Must match a key in config/applications.yaml.
        environment_key: The environment's config key, e.g. 'default'.
            Must be defined under that application in config.

    Returns:
        A ResolvedContext with all values needed to act.

    Raises:
        UnknownApplicationError: application_key not in config.
        UnknownEnvironmentError: environment_key not found for that application.
    """
    apps = settings.applications_config.get("applications", {})

    app_config = apps.get(application_key)
    if not app_config:
        raise UnknownApplicationError(
            f"Application '{application_key}' is not configured. "
            f"Known applications: {list(apps.keys())}. "
            f"Add it to config/applications.yaml."
        )

    envs = app_config.get("environments", {})
    env_config = envs.get(environment_key)
    if not env_config:
        raise UnknownEnvironmentError(
            f"Environment '{environment_key}' is not configured for "
            f"application '{application_key}'. "
            f"Known environments: {list(envs.keys())}."
        )

    url = env_config.get("url", "")
    requires_prod = env_config.get("requires_production_confirmation", False)

    # Credentials are read from environment variables, never from config files.
    service_user = None
    service_password = None
    user_env = env_config.get("service_user_env_var")
    password_env = env_config.get("service_password_env_var")
    if user_env:
        service_user = os.environ.get(user_env)
    if password_env:
        service_password = os.environ.get(password_env)

    logger.info(
        "Resolved context: app=%s env=%s url=%s prod_confirmation=%s",
        application_key, environment_key, url, requires_prod,
    )

    return ResolvedContext(
        application_key=application_key,
        environment_key=environment_key,
        display_name=app_config.get("display_name", application_key),
        url=url,
        service_user=service_user,
        service_password=service_password,
        requires_production_confirmation=requires_prod,
    )


def normalize_url(url: str) -> str:
    """
    Normalises a URL for deduplication: lowercase host, strip trailing slash,
    strip default ports (80 for http, 443 for https).

    Args:
        url: The raw URL as provided by the user.

    Returns:
        The normalised URL string.
    """
    from urllib.parse import urlparse, urlunparse

    parsed = urlparse(url.strip().lower())
    host = parsed.hostname or ""
    port = parsed.port

    # Strip default ports
    if (parsed.scheme == "http" and port == 80) or (parsed.scheme == "https" and port == 443):
        netloc = host
    elif port:
        netloc = f"{host}:{port}"
    else:
        netloc = host

    path = parsed.path.rstrip("/")
    return urlunparse((parsed.scheme, netloc, path, "", "", ""))
