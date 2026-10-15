"""
integrations/app_repo_map.py

The one place that says which application's tests live in which
Bitbucket repository. When a new application gets onboarded - and its
repository has been created by hand, as agreed - add one line here.
Nothing else needs to change.
"""

import logging

from core.errors import ApplicationNotMappedError

logger = logging.getLogger(__name__)

APP_REPO_MAP = {
    "FS": "qa-automation-fs",
    "JDE": "qa-automation-jde",
    "BAXTER_COM": "qa-automation-baxtercom",
    "MDM": "qa-automation-mdm",
    "BOUTIQUE": "qa-automation-boutique",
}


def get_repo_slug(app_name: str) -> str:
    """
    Looks up which Bitbucket repository holds the tests for a given application.

    Args:
        app_name (str): the application name as selected in the UI, e.g. "FS".

    Returns:
        str: the Bitbucket repository slug, e.g. "qa-automation-fs".

    Raises:
        ApplicationNotMappedError: if the application has not been
            added to APP_REPO_MAP yet.
    """
    key = app_name.upper()
    if key not in APP_REPO_MAP:
        logger.error("Application not mapped: %s", app_name)
        raise ApplicationNotMappedError(
            f"No repository is mapped for '{app_name}'. "
            f"Add it to APP_REPO_MAP once its repository exists in Bitbucket."
        )
    return APP_REPO_MAP[key]
