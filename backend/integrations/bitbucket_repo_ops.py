"""
integrations/bitbucket_repo_ops.py

Everything that talks to Bitbucket directly. Three jobs:
  1. Check a repository already exists (repo_exists)
  2. Save an approved test file into that repository (commit_test_script)
  3. Get a real, working copy of that repository onto this machine (sync_repo_locally)

Every failure here raises a typed error from core/errors.py, which
api/main.py's central handler turns into a specific error code and
logs with the full traceback - never a bare "INTERNAL_ERROR".
"""

import logging
import os
import subprocess

import requests

from core.errors import RepositoryNotFoundError, RepositorySyncError

logger = logging.getLogger(__name__)

WORKSPACE = "baxter"
BITBUCKET_USERNAME = os.environ["BITBUCKET_USERNAME"]
BITBUCKET_APP_PASSWORD = os.environ["BITBUCKET_APP_PASSWORD"]
AUTH = (BITBUCKET_USERNAME, BITBUCKET_APP_PASSWORD)

LOCAL_REPOS_ROOT = "local_repos"


def repo_exists(repo_slug: str) -> bool:
    """
    Checks whether a repository already exists in the NextGen2 QA Automation project.

    Args:
        repo_slug (str): the repository name, e.g. "qa-automation-fs".

    Returns:
        bool: True if the repository exists, False otherwise.

    Raises:
        RepositorySyncError: if Bitbucket could not be reached at all -
            deliberately not treated the same as "does not exist".
    """
    url = f"https://api.bitbucket.org/2.0/repositories/{WORKSPACE}/{repo_slug}"
    try:
        response = requests.get(url, auth=AUTH, timeout=15)
    except requests.exceptions.RequestException as e:
        logger.exception("Could not reach Bitbucket to check repository '%s'", repo_slug)
        raise RepositorySyncError(f"Could not reach Bitbucket to check repository '{repo_slug}'") from e

    exists = response.status_code == 200
    logger.info("repo_exists check for '%s': %s", repo_slug, exists)
    return exists


def commit_test_script(repo_slug: str, file_path: str, file_content: str, commit_message: str) -> None:
    """
    Saves a test file into an existing Bitbucket repository.

    Args:
        repo_slug (str): the repository to commit into.
        file_path (str): path inside the repository.
        file_content (str): the full contents of the file.
        commit_message (str): a human-readable commit message.

    Raises:
        RepositoryNotFoundError: if the repository does not exist yet.
        RepositorySyncError: if Bitbucket rejects the commit for any other reason.
    """
    if not repo_exists(repo_slug):
        logger.error("Commit rejected - repository '%s' does not exist", repo_slug)
        raise RepositoryNotFoundError(
            f"Repository '{repo_slug}' does not exist yet. "
            f"It needs to be created once, by hand, before this can run."
        )

    url = f"https://api.bitbucket.org/2.0/repositories/{WORKSPACE}/{repo_slug}/src"
    try:
        response = requests.post(
            url, auth=AUTH,
            data={file_path: file_content, "message": commit_message, "branch": "main"},
            timeout=30,
        )
        response.raise_for_status()
    except requests.exceptions.RequestException as e:
        logger.exception("Commit failed for '%s' in '%s'", file_path, repo_slug)
        raise RepositorySyncError(f"Could not commit '{file_path}' to '{repo_slug}'") from e

    logger.info("Committed '%s' to '%s'", file_path, repo_slug)


def sync_repo_locally(repo_slug: str) -> str:
    """
    Makes sure a real, up-to-date copy of the repository exists on this
    machine, and returns the folder path to it.

    Args:
        repo_slug (str): the repository to sync.

    Returns:
        str: local folder path containing a working copy of the repository.

    Raises:
        RepositorySyncError: if the clone or pull fails.
    """
    local_path = os.path.join(LOCAL_REPOS_ROOT, repo_slug)
    clone_url = (
        f"https://{BITBUCKET_USERNAME}:{BITBUCKET_APP_PASSWORD}"
        f"@bitbucket.org/{WORKSPACE}/{repo_slug}.git"
    )

    try:
        if os.path.isdir(local_path):
            subprocess.run(["git", "-C", local_path, "pull"], check=True, capture_output=True, text=True, timeout=120)
            logger.info("Pulled latest for '%s'", repo_slug)
        else:
            os.makedirs(LOCAL_REPOS_ROOT, exist_ok=True)
            subprocess.run(["git", "clone", clone_url, local_path], check=True, capture_output=True, text=True, timeout=120)
            logger.info("Cloned '%s'", repo_slug)
    except subprocess.CalledProcessError as e:
        logger.exception("Repository sync failed for '%s'", repo_slug)
        raise RepositorySyncError(f"Could not sync repository '{repo_slug}' onto this machine") from e
    except subprocess.TimeoutExpired as e:
        logger.exception("Repository sync timed out for '%s'", repo_slug)
        raise RepositorySyncError(f"Syncing repository '{repo_slug}' took too long and was stopped") from e

    return local_path
