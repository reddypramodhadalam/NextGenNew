"""
service/jira_adapter.py

Fetches Jira issues (Features / requirement-level work items) and flattens
them into BRD-equivalent requirement text for the survey/generation flow.

--------------------------------------------------------------------------
FIX HISTORY

(2026-08-18, fix #1) — two bugs that caused every real request to fail with
"Jira returned non-JSON content" against Baxter's Data Center instance:

1. `_auth_header` built a Basic-Auth header from an EMPTY username/password
   (`f":".encode()`), never actually using cfg.email / cfg.api_token. So no
   real credentials were ever sent — SSO/the reverse proxy then bounced the
   request to an HTML login page instead of the API.

2. The search endpoint was hardcoded to `/rest/api/3/search/jql`, which is
   Jira **Cloud**-only (v3 API + the newer /search/jql path). Jira **Server
   / Data Center** (e.g. jira-prod.aws.baxter.com) only has `/rest/api/2/`,
   and it authenticates with a **Personal Access Token as a Bearer token**,
   not Basic email+token.

This version auto-detects which flavour of Jira it's talking to:
  - If JIRA_EMAIL is set               -> Cloud: Basic auth, API v3.
  - If JIRA_EMAIL is NOT set (PAT only) -> Data Center: Bearer auth, API v2.
You can force either behaviour with JIRA_AUTH_MODE=basic|bearer and
JIRA_API_VERSION=2|3 if auto-detection ever guesses wrong.

(2026-08-18, fix #2) — CONFIG ERROR: JIRA_BASE_URL is not set, even with a
correctly filled-in .env file:

Python never loads .env files on its own. The rest of the Kavach backend
only got this for free because core/settings.py calls python-dotenv's
load_dotenv() at import time. This module previously read os.environ
directly WITHOUT loading .env itself, so anything that imports it without
first importing core.settings (e.g. test_jira.py run standalone) saw an
empty environment. Added a self-contained load_dotenv() call below so this
module works no matter what imports it or in what order.
--------------------------------------------------------------------------

Prerequisites — environment variables (set directly, or via backend/.env):
    Data Center / Server (e.g. Baxter's jira-prod):
        JIRA_BASE_URL   = "https://jira-prod.aws.baxter.com"
        JIRA_API_TOKEN  = "<Personal Access Token>"   # Profile -> Personal Access Tokens
        # JIRA_EMAIL must be UNSET/empty for Bearer/PAT auth to be selected.

    Jira Cloud:
        JIRA_BASE_URL   = "https://your-org.atlassian.net"
        JIRA_EMAIL      = "your-atlassian-email@example.com"
        JIRA_API_TOKEN  = "<API token>"               # id.atlassian.com/manage-profile/security/api-tokens

    Optional (either flavour):
        JIRA_PROJECT_KEY = "KAN"
        JIRA_ISSUE_TYPE  = "Feature"
        JIRA_AUTH_MODE   = "bearer" | "basic"   # override auto-detection
        JIRA_API_VERSION = "2" | "3"            # override auto-detection
"""

import base64
import json
import logging
import os
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Optional

logger = logging.getLogger(__name__)

# .env is never loaded by Python automatically. The rest of the backend gets
# this for free because core/settings.py calls load_dotenv() at import time —
# but test_jira.py (and anything else that imports this module directly,
# without going through core.settings first) never triggers that. Calling it
# here makes this module self-contained: JIRA_* values in backend/.env are
# picked up no matter what imports it, or in what order, or from what folder
# VS Code / a terminal happens to launch it from (python-dotenv walks up from
# the current working directory to find the nearest .env).
try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    # python-dotenv not installed — fall back to whatever is already in the
    # real process environment (e.g. set via PowerShell $env:...). Not fatal.
    pass


# ── Config ────────────────────────────────────────────────────────────────

@dataclass
class JiraConfig:
    base_url: str            # e.g. https://jira-prod.aws.baxter.com (no trailing slash)
    email: Optional[str]     # Atlassian account email (Cloud only). None/"" for Data Center PAT auth.
    api_token: str           # Cloud: API token.  Data Center: Personal Access Token (PAT).
    project_key: str = "KAN"
    issue_type: str = "Feature"     # this project's requirement-level type
    auth_mode: str = ""              # "basic" | "bearer" — resolved in load_config if not set
    api_version: str = ""            # "2" | "3" — resolved in load_config if not set

    @property
    def jql(self) -> str:
        return f'project = "{self.project_key}" AND issuetype = "{self.issue_type}" ORDER BY key ASC'


def load_config(project_key: Optional[str] = None,
                issue_type: Optional[str] = None) -> JiraConfig:
    """
    Read Jira settings from environment variables (see module docstring).
    load_dotenv() above already merged backend/.env into os.environ, so this
    works whether the values came from .env, PowerShell $env:, or a real
    system environment variable.

    Auto-detects Cloud vs. Data Center:
      - JIRA_EMAIL present and non-empty  -> Cloud   -> Basic auth, API v3.
      - JIRA_EMAIL absent/empty           -> Data Center -> Bearer/PAT, API v2.
    Either can be overridden with JIRA_AUTH_MODE / JIRA_API_VERSION.
    """
    base_url = (os.environ.get("JIRA_BASE_URL") or "").strip().rstrip("/")
    email = (os.environ.get("JIRA_EMAIL") or "").strip() or None
    api_token = (os.environ.get("JIRA_API_TOKEN") or "").strip()

    if not base_url:
        raise RuntimeError(
            "JIRA_BASE_URL is not set. Checked the process environment and "
            "backend/.env (via load_dotenv()). If you just edited .env, make "
            "sure: (1) the file is named exactly '.env' in the 'backend' "
            "folder, (2) there's no leading '#' or stray quotes around the "
            "value, and (3) you're running this script with 'backend' as the "
            "current working directory."
        )
    if not api_token:
        raise RuntimeError("JIRA_API_TOKEN is not set (API token for Cloud, "
                            "or Personal Access Token for Data Center).")

    auth_mode = (os.environ.get("JIRA_AUTH_MODE") or "").strip().lower()
    if auth_mode not in ("basic", "bearer"):
        auth_mode = "basic" if email else "bearer"

    api_version = (os.environ.get("JIRA_API_VERSION") or "").strip()
    if api_version not in ("2", "3"):
        api_version = "3" if auth_mode == "basic" else "2"

    return JiraConfig(
        base_url=base_url,
        email=email,
        api_token=api_token,
        project_key=project_key or os.environ.get("JIRA_PROJECT_KEY", "KAN"),
        issue_type=issue_type or os.environ.get("JIRA_ISSUE_TYPE", "Feature"),
        auth_mode=auth_mode,
        api_version=api_version,
    )


# ── Auth ──────────────────────────────────────────────────────────────────

def _auth_header(cfg: JiraConfig) -> str:
    """
    Build the Authorization header value for this config.

    FIXED: previously encoded an empty ":" (no credentials at all), which is
    why every request silently failed auth and got bounced to an HTML page.
    """
    if cfg.auth_mode == "bearer":
        # Data Center / Server Personal Access Token.
        return f"Bearer {cfg.api_token}"
    # Cloud: Basic base64(email:api_token).
    raw = f"{cfg.email}:{cfg.api_token}".encode("utf-8")
    return "Basic " + base64.b64encode(raw).decode("ascii")


# ── ADF / plain-text description handling ──────────────────────────────────

def adf_to_text(node) -> str:
    """
    Flatten a description to plain text.

    Cloud (API v3) returns descriptions as Atlassian Document Format (a JSON
    tree). Data Center (API v2) returns plain wiki-markup text directly.
    This handles both so callers never need to care which flavour answered.
    """
    if node is None:
        return ""
    if isinstance(node, str):
        return node
    if isinstance(node, dict):
        parts = []
        if node.get("type") == "text":
            parts.append(node.get("text", ""))
        for child in node.get("content", []) or []:
            parts.append(adf_to_text(child))
        joined = " ".join(p for p in parts if p)
        if node.get("type") in ("paragraph", "heading"):
            joined += "\n"
        return joined
    if isinstance(node, list):
        return "\n".join(adf_to_text(n) for n in node)
    return str(node)


# ── Fetch ─────────────────────────────────────────────────────────────────

@dataclass
class JiraRequirement:
    key: str                       # e.g. KAN-4 / BHS-1 — the traceable source id
    summary: str                   # issue title
    description_text: str          # flattened body (ADF or plain text)
    issue_type: str = ""
    status: str = ""
    updated: str = ""              # issue's updated timestamp — change anchor

    def as_block(self) -> str:
        lines = [f"[{self.key}] {self.summary}"]
        if self.issue_type:
            lines.append(f"Type: {self.issue_type}")
        if self.status:
            lines.append(f"Status: {self.status}")
        if self.description_text:
            lines.append("")
            lines.append(self.description_text.strip())
        return "\n".join(lines)


def _request(cfg: JiraConfig, path: str, payload: dict) -> dict:
    """POST one JQL search request and return the parsed JSON response."""
    url = f"{cfg.base_url}{path}"
    logger.debug(
        "Jira request: POST %s  auth_mode=%s api_version=%s startAt=%s maxResults=%s",
        url, cfg.auth_mode, cfg.api_version,
        payload.get("startAt", 0), payload.get("maxResults"),
    )
    body = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=body,
        method="POST",
        headers={
            "Authorization": _auth_header(cfg),
            "Accept": "application/json",
            "Content-Type": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            content_type = resp.headers.get("Content-Type", "")
            raw = resp.read()
    except urllib.error.HTTPError as e:
        content_type = e.headers.get("Content-Type", "") if e.headers else ""
        raw = e.read()
        if "application/json" not in content_type:
            snippet = raw.decode("utf-8", errors="replace")[:300]
            logger.error(
                "Jira HTTP %d non-JSON response for POST %s  content_type=%s  body_start=%r",
                e.code, url, content_type, snippet,
            )
            raise RuntimeError(
                f"Jira returned {e.code} non-JSON content ({content_type}) for "
                f"POST {url}. Body starts: {snippet!r}"
            ) from e
        # A JSON error body — surface Jira's own message.
        try:
            err_json = json.loads(raw)
            logger.error("Jira HTTP %d error for POST %s: %s", e.code, url, err_json)
            raise RuntimeError(f"Jira returned {e.code}: {err_json}") from e
        except json.JSONDecodeError:
            logger.error("Jira HTTP %d non-parseable body for POST %s: %r", e.code, url, raw[:200])
            raise RuntimeError(f"Jira returned {e.code}: {raw!r}") from e
    except urllib.error.URLError as e:
        logger.error("Jira network error for POST %s: %s", url, e.reason)
        raise RuntimeError(f"Jira network error: {e.reason}") from e

    if "application/json" not in content_type:
        snippet = raw.decode("utf-8", errors="replace")[:300]
        logger.error(
            "Jira returned non-JSON 200 for POST %s  content_type=%s  body_start=%r",
            url, content_type, snippet,
        )
        raise RuntimeError(
            f"Jira returned non-JSON content ({content_type}) for POST {url}.\n"
            f"If this is a 410/404, your site's API version may differ — "
            f"currently using /rest/api/{cfg.api_version}/. "
            f"If it's an SSO login page, re-check auth_mode "
            f"(currently '{cfg.auth_mode}') and the token.\n"
            f"Body starts: {snippet!r}"
        )
    result = json.loads(raw)
    logger.debug("Jira response: %d bytes parsed OK from %s", len(raw), url)
    return result


def _search(
    cfg: JiraConfig,
    jql: str,
    max_results: int,
    start_at: int = 0,
) -> tuple[list[JiraRequirement], int]:
    """
    Run one page of a JQL search.

    Returns (issues, total) where total is the Jira-reported total count across
    all pages — not just this page.  Callers use total to decide whether to
    fetch the next page.
    """
    logger.debug(
        "_search: jql=%r  start_at=%d  max_results=%d",
        jql[:120], start_at, max_results,
    )
    fields = ["summary", "description", "issuetype", "status", "updated"]

    if cfg.api_version == "3":
        # Jira Cloud v3 — modern search endpoint.
        path = "/rest/api/3/search/jql"
    else:
        # Jira Server / Data Center v2 — classic endpoint (no /jql suffix).
        path = "/rest/api/2/search"

    payload = {
        "jql": jql,
        "startAt": start_at,
        "maxResults": max_results,
        "fields": fields,
    }
    data = _request(cfg, path, payload)
    total = int(data.get("total", 0))

    out: list[JiraRequirement] = []
    for issue in data.get("issues", []):
        f = issue.get("fields", {}) or {}
        out.append(JiraRequirement(
            key=issue.get("key", ""),
            summary=f.get("summary") or "",
            description_text=adf_to_text(f.get("description")),
            issue_type=(f.get("issuetype") or {}).get("name", ""),
            status=(f.get("status") or {}).get("name", ""),
            updated=f.get("updated") or "",
        ))
    logger.debug(
        "_search page: fetched %d issue(s)  total=%d  start_at=%d",
        len(out), total, start_at,
    )
    return out, total


def fetch_requirements(cfg: JiraConfig, page_size: int = 50) -> list[JiraRequirement]:
    """
    Fetch ALL requirements from Jira, paginating through every page.

    Previously made a single request capped at max_results (50-100).  A project
    with 200 requirements silently returned 50.  Now loops until startAt >= total
    so every issue is fetched regardless of project size.
    """
    logger.info(
        "fetch_requirements: project=%s type=%s page_size=%d",
        cfg.project_key, cfg.issue_type, page_size,
    )
    all_reqs: list[JiraRequirement] = []
    start_at = 0
    page_num = 0
    while True:
        page_num += 1
        page, total = _search(cfg, cfg.jql, max_results=page_size, start_at=start_at)
        all_reqs.extend(page)
        start_at += len(page)
        logger.debug("fetch_requirements: page %d — %d/%d fetched", page_num, start_at, total)
        if start_at >= total or not page:
            break
    logger.info(
        "fetch_requirements: done — %d requirement(s) fetched in %d page(s)",
        len(all_reqs), page_num,
    )
    return all_reqs


def fetch_requirements_updated_since(
    cfg: JiraConfig,
    since_iso: Optional[str],
    page_size: int = 50,
) -> list[JiraRequirement]:
    """
    Fetch requirement issues UPDATED at or after since_iso — the query the
    change-detection poll runs. When since_iso is None (never synced), this is
    equivalent to fetch_requirements (returns all), which seeds the baseline.
    Paginated: fetches all matching issues, not just the first page.
    """
    if not since_iso:
        logger.info("fetch_requirements_updated_since: no since_iso — fetching all")
        return fetch_requirements(cfg, page_size=page_size)
    logger.info(
        "fetch_requirements_updated_since: since=%s  project=%s",
        since_iso, cfg.project_key,
    )
    jql = f'{cfg.jql} AND updated >= "{_to_jql_time(since_iso)}"'
    all_reqs: list[JiraRequirement] = []
    start_at = 0
    page_num = 0
    while True:
        page_num += 1
        page, total = _search(cfg, jql, max_results=page_size, start_at=start_at)
        all_reqs.extend(page)
        start_at += len(page)
        if start_at >= total or not page:
            break
    logger.info(
        "fetch_requirements_updated_since: %d updated requirement(s) fetched",
        len(all_reqs),
    )
    return all_reqs


def fetch_current_keys(cfg: JiraConfig, page_size: int = 50) -> set:
    """
    All requirement issue keys currently in the project. Used to spot NEW
    requirements (keys in Jira with no test cases yet) and, later, deletions
    (stored keys no longer present). Paginated: returns all keys.
    """
    logger.info("fetch_current_keys: project=%s", cfg.project_key)
    keys = {r.key for r in fetch_requirements(cfg, page_size=page_size) if r.key}
    logger.info("fetch_current_keys: %d key(s) found", len(keys))
    return keys


def _to_jql_time(iso: str) -> str:
    """
    Convert an ISO timestamp (e.g. Jira's '2026-08-02T15:43:39.129+0530') to the
    'yyyy-mm-dd hh:mm' form JQL accepts. Falls back to the date if parsing fails.
    """
    from datetime import datetime
    s = (iso or "").strip()
    for fmt in ("%Y-%m-%dT%H:%M:%S.%f%z", "%Y-%m-%dT%H:%M:%S%z",
                "%Y-%m-%dT%H:%M:%S.%f", "%Y-%m-%dT%H:%M:%S"):
        try:
            return datetime.strptime(s, fmt).strftime("%Y-%m-%d %H:%M")
        except Exception:
            continue
    return s[:10]  # date only, as a safe fallback


def build_requirements_text(reqs: list[JiraRequirement]) -> str:
    """
    Concatenate issue blocks into a single BRD-equivalent string — the exact
    kind of text _extract_text_from_document returns for a document, so it
    can be handed to the existing survey as document_context untouched.
    """
    return "\n\n".join(r.as_block() for r in reqs if r.as_block().strip())


def updated_map(reqs: list[JiraRequirement]) -> dict:
    """Key -> updated timestamp for every fetched issue."""
    return {r.key: r.updated for r in reqs if r.key}


def load_requirements_text(project_key: Optional[str] = None,
                            issue_type: Optional[str] = None) -> str:
    """
    Convenience: config -> fetch -> BRD-equivalent text, in one call.
    """
    cfg = load_config(project_key=project_key, issue_type=issue_type)
    reqs = fetch_requirements(cfg)
    return build_requirements_text(reqs)


def load_requirements_with_meta(project_key: Optional[str] = None,
                                 issue_type: Optional[str] = None) -> tuple[str, dict]:
    """
    Like load_requirements_text, but also returns the updated_map so the
    caller can record source_updated_at on the resulting test cases.
    Returns (requirements_text, updated_map).
    """
    cfg = load_config(project_key=project_key, issue_type=issue_type)
    reqs = fetch_requirements(cfg)
    return build_requirements_text(reqs), updated_map(reqs)


def load_requirements_text_for_key(issue_key: str,
                                    project_key: Optional[str] = None,
                                    issue_type: Optional[str] = None) -> str:
    """BRD-equivalent text for ONE Jira issue, by key."""
    cfg = load_config(project_key=project_key, issue_type=issue_type)
    jql = f'key = "{issue_key}"'
    reqs, _ = _search(cfg, jql, 1)  # single-key lookup — no pagination needed
    return build_requirements_text(reqs)
