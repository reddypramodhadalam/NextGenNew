"""
integrations/git_provider.py

Picks which git host (GitHub or Bitbucket) is used for saving and
syncing test scripts. Same pattern as the LLM provider factory: one
config value decides, no .py file is edited to switch.

The single switch is GIT_PROVIDER (environment variable):
  - "github"    -> GitHubProvider
  - "bitbucket" -> BitbucketProvider

Both providers do the same three things behind one common interface,
so the rest of the app (run_tests.py) never knows or cares which host
is actually in use:
  - repo_exists(repo_name)
  - commit_test_script(repo_name, file_path, content, message)
  - sync_repo_locally(repo_name) -> local folder path
"""

import logging
import os
import subprocess
from abc import ABC, abstractmethod

import requests

from core.errors import RepositoryNotFoundError, RepositorySyncError

logger = logging.getLogger(__name__)

LOCAL_REPOS_ROOT = os.environ.get("LOCAL_REPOS_ROOT", "local_repos")


class GitProvider(ABC):
    """Common interface every git host implementation must provide."""

    @abstractmethod
    def repo_exists(self, repo_name: str) -> bool:
        """Returns True if the repository exists on the host."""
        raise NotImplementedError

    @abstractmethod
    def commit_test_script(self, repo_name: str, file_path: str, content: str, message: str) -> None:
        """Saves one file into the repository."""
        raise NotImplementedError

    @abstractmethod
    def sync_repo_locally(self, repo_name: str) -> str:
        """Clones or pulls the repo onto this machine, returns the local path."""
        raise NotImplementedError


class GitHubProvider(GitProvider):
    """
    GitHub implementation. Uses a personal access token for auth.

    Reads from environment:
      - GITHUB_OWNER: the user or org that owns the repos, e.g. "panks"
      - GITHUB_TOKEN: a personal access token with 'repo' scope
    """

    def __init__(self):
        self.owner = os.environ["GITHUB_OWNER"]
        self.token = os.environ["GITHUB_TOKEN"]

    def repo_exists(self, repo_name: str) -> bool:
        url = f"https://api.github.com/repos/{self.owner}/{repo_name}"
        try:
            response = requests.get(url, headers=self._headers(), timeout=15)
        except requests.exceptions.RequestException as e:
            logger.exception("Could not reach GitHub to check '%s'", repo_name)
            raise RepositorySyncError(f"Could not reach GitHub to check '{repo_name}'") from e
        return response.status_code == 200

    def commit_test_script(self, repo_name: str, file_path: str, content: str, message: str) -> None:
        import base64

        if not self.repo_exists(repo_name):
            raise RepositoryNotFoundError(
                f"GitHub repository '{repo_name}' does not exist yet. Create it once by hand first."
            )

        url = f"https://api.github.com/repos/{self.owner}/{repo_name}/contents/{file_path}"

        # GitHub's contents API needs the existing file's SHA to update it.
        # If the file doesn't exist yet, there's no SHA - that's a create.
        sha = None
        try:
            existing = requests.get(url, headers=self._headers(), timeout=15)
            if existing.status_code == 200:
                sha = existing.json().get("sha")
        except requests.exceptions.RequestException:
            pass

        payload = {
            "message": message,
            "content": base64.b64encode(content.encode()).decode(),
            "branch": "main",
        }
        if sha:
            payload["sha"] = sha

        try:
            response = requests.put(url, headers=self._headers(), json=payload, timeout=30)
            response.raise_for_status()
        except requests.exceptions.RequestException as e:
            logger.exception("GitHub commit failed for '%s' in '%s'", file_path, repo_name)
            raise RepositorySyncError(f"Could not commit '{file_path}' to '{repo_name}'") from e

        logger.info("Committed '%s' to GitHub repo '%s'", file_path, repo_name)

    def sync_repo_locally(self, repo_name: str) -> str:
        local_path = os.path.join(LOCAL_REPOS_ROOT, repo_name)
        clone_url = f"https://{self.token}@github.com/{self.owner}/{repo_name}.git"
        return _clone_or_pull(local_path, clone_url, repo_name)

    def _headers(self) -> dict:
        return {
            "Authorization": f"Bearer {self.token}",
            "Accept": "application/vnd.github+json",
        }


class BitbucketProvider(GitProvider):
    """
    Bitbucket implementation. Uses username + app password for auth.

    Reads from environment:
      - BITBUCKET_WORKSPACE: the workspace, e.g. "baxter"
      - BITBUCKET_USERNAME
      - BITBUCKET_APP_PASSWORD
    """

    def __init__(self):
        self.workspace = os.environ.get("BITBUCKET_WORKSPACE", "baxter")
        self.username = os.environ["BITBUCKET_USERNAME"]
        self.app_password = os.environ["BITBUCKET_APP_PASSWORD"]

    def _auth(self):
        return (self.username, self.app_password)

    def repo_exists(self, repo_name: str) -> bool:
        url = f"https://api.bitbucket.org/2.0/repositories/{self.workspace}/{repo_name}"
        try:
            response = requests.get(url, auth=self._auth(), timeout=15)
        except requests.exceptions.RequestException as e:
            logger.exception("Could not reach Bitbucket to check '%s'", repo_name)
            raise RepositorySyncError(f"Could not reach Bitbucket to check '{repo_name}'") from e
        return response.status_code == 200

    def commit_test_script(self, repo_name: str, file_path: str, content: str, message: str) -> None:
        if not self.repo_exists(repo_name):
            raise RepositoryNotFoundError(
                f"Bitbucket repository '{repo_name}' does not exist yet. Create it once by hand first."
            )
        url = f"https://api.bitbucket.org/2.0/repositories/{self.workspace}/{repo_name}/src"
        try:
            response = requests.post(
                url, auth=self._auth(),
                data={file_path: content, "message": message, "branch": "main"},
                timeout=30,
            )
            response.raise_for_status()
        except requests.exceptions.RequestException as e:
            logger.exception("Bitbucket commit failed for '%s' in '%s'", file_path, repo_name)
            raise RepositorySyncError(f"Could not commit '{file_path}' to '{repo_name}'") from e
        logger.info("Committed '%s' to Bitbucket repo '%s'", file_path, repo_name)

    def sync_repo_locally(self, repo_name: str) -> str:
        local_path = os.path.join(LOCAL_REPOS_ROOT, repo_name)
        clone_url = (
            f"https://{self.username}:{self.app_password}"
            f"@bitbucket.org/{self.workspace}/{repo_name}.git"
        )
        return _clone_or_pull(local_path, clone_url, repo_name)


def _clone_or_pull(local_path: str, clone_url: str, repo_name: str) -> str:
    """Shared clone-or-pull logic - identical for both hosts."""
    try:
        if os.path.isdir(local_path):
            subprocess.run(["git", "-C", local_path, "pull"], check=True, capture_output=True, text=True, timeout=120)
            logger.info("Pulled latest for '%s'", repo_name)
        else:
            os.makedirs(LOCAL_REPOS_ROOT, exist_ok=True)
            subprocess.run(["git", "clone", clone_url, local_path], check=True, capture_output=True, text=True, timeout=120)
            logger.info("Cloned '%s'", repo_name)
    except subprocess.CalledProcessError as e:
        logger.exception("Repository sync failed for '%s'", repo_name)
        raise RepositorySyncError(f"Could not sync repository '{repo_name}'") from e
    except subprocess.TimeoutExpired as e:
        logger.exception("Repository sync timed out for '%s'", repo_name)
        raise RepositorySyncError(f"Syncing '{repo_name}' took too long and was stopped") from e
    return local_path


# Supported values for the GIT_PROVIDER config switch
SUPPORTED_GIT_PROVIDERS = ("github", "bitbucket")


def get_git_provider() -> GitProvider:
    """
    Returns the configured git provider instance, chosen by the
    GIT_PROVIDER environment variable.

    Returns:
        GitProvider: a ready-to-use GitHub or Bitbucket provider.

    Raises:
        ValueError: if GIT_PROVIDER is not one of the supported values.
    """
    provider = os.environ.get("GIT_PROVIDER", "github").lower().strip()

    if provider not in SUPPORTED_GIT_PROVIDERS:
        raise ValueError(
            f"Unknown GIT_PROVIDER '{provider}'. Supported: {', '.join(SUPPORTED_GIT_PROVIDERS)}"
        )

    logger.info("Using git provider: %s", provider)

    if provider == "github":
        return GitHubProvider()
    return BitbucketProvider()
