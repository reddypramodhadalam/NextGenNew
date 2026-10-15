"""
service/ticketing/jira.py

Creates support tickets as Jira issues.

This is NextGen2's first WRITE to Jira. Everything before it — the source
adapter, the sync engine — only reads. Two consequences shape this file:

1. The description field on Jira Cloud is ADF (Atlassian Document Format), a
   nested JSON tree, not a string. service/jira_adapter.py flattens ADF on the
   way in; this builds it on the way out. Posting a plain string to
   /rest/api/3/issue fails with an unhelpful error, so _text_to_adf exists.

2. Support tickets must NOT land in the requirements project. The sync engine
   runs JQL over that project and treats what it finds as requirements: a
   support ticket filed there would be picked up and generate test cases from
   itself, and would appear in the traceability matrix an auditor reads. The
   project key therefore has no default, and _config() refuses to proceed if
   it matches the configured requirements project.

Credentials are shared with the source adapter — same Atlassian site, same
account — and read from settings.jira. The destination project and issue type
are separate, and come from settings.ticketing.
"""

from __future__ import annotations

import base64
import json
import logging
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any

from service.ticketing.base import TicketingError, TicketRef, TicketRequest

logger = logging.getLogger(__name__)

_TIMEOUT = 30

# Applied to every issue NextGen2 files, so support tickets raised from the
# product can be found, reported on, and excluded from other queries.
_BASE_LABEL = "NextGen2-support"


@dataclass
class _JiraTicketConfig:
    """Resolved configuration for the Jira write path.

    Attributes:
        base_url: Atlassian site URL, no trailing slash.
        email: Atlassian account email used for basic auth.
        api_token: Atlassian API token.
        project_key: Project support tickets are created in.
        issue_type: Issue type to create.
    """

    base_url: str
    email: str
    api_token: str
    project_key: str
    issue_type: str


class JiraTicketProvider:
    """
    Creates support tickets as issues in a Jira project.

    Holds no state — configuration is resolved per call, so a change to .env
    takes effect on restart without any cached client to invalidate.
    """

    name = "jira"

    def create_ticket(self, request: TicketRequest) -> TicketRef:
        """Create a Jira issue for a support request.

        Args:
            request: What the user is asking for, in NextGen2's terms.

        Returns:
            A reference carrying the Jira key and a browse URL.

        Raises:
            TicketingError: If Jira is not configured, the destination project
                is unsafe, or Jira rejected the request.
        """
        cfg = self._config()

        payload = {
            "fields": {
                "project": {"key": cfg.project_key},
                "issuetype": {"name": cfg.issue_type},
                "summary": self._summary(request),
                "description": _text_to_adf(self._body(request)),
                # The category is carried as a label rather than mapped onto a
                # Jira issue type. Mapping would mean assuming types like
                # 'Bug' and 'Service Request' exist in whichever project this
                # points at, and a missing type fails the create with a schema
                # error. A label always works, and the mapping can be made
                # explicit later once the destination project is fixed.
                "labels": [_BASE_LABEL, f"NextGen2-{_slug(request.category)}"],
            }
        }

        data = self._post(cfg, "/rest/api/3/issue", payload)

        key = data.get("key") or ""
        if not key:
            raise TicketingError(
                "Jira accepted the request but returned no issue key. "
                "The ticket may or may not have been created — check the "
                f"{cfg.project_key} project before retrying."
            )

        logger.info("Created Jira support ticket %s for %s", key, request.raised_by)
        return TicketRef(
            provider=self.name,
            external_ref=key,
            external_url=f"{cfg.base_url}/browse/{key}",
            raw=data,
        )

    def check_config(self) -> None:
        """Validate configuration without creating an issue.

        Raises:
            TicketingError: If credentials or the destination project are
                missing or unsafe.
        """
        self._config()

    # ── internals ────────────────────────────────────────────────────────

    @staticmethod
    def _config() -> _JiraTicketConfig:
        """Resolve and sanity-check the write configuration.

        Returns:
            The resolved configuration.

        Raises:
            TicketingError: If credentials are missing, the support project
                key is unset, or it matches the requirements project.
        """
        from core.settings import settings

        j, t = settings.jira, settings.ticketing

        if not (j.base_url and j.email and j.api_token):
            raise TicketingError(
                "Jira is not configured. Set JIRA_BASE_URL, JIRA_EMAIL and "
                "JIRA_API_TOKEN in .env."
            )

        if not t.jira_project_key:
            raise TicketingError(
                "No Jira project is configured for support tickets. Set "
                "JIRA_SUPPORT_PROJECT_KEY in .env to a project separate from "
                "the requirements project."
            )

        # The guard that matters. Filing support tickets into the project the
        # sync engine reads would turn them into requirements.
        if t.jira_project_key.strip().upper() == (j.project_key or "").strip().upper():
            raise TicketingError(
                f"JIRA_SUPPORT_PROJECT_KEY is set to '{t.jira_project_key}', which "
                f"is also the requirements project. Support tickets filed there "
                f"would be read back as requirements by the Jira sync. Use a "
                f"separate project."
            )

        return _JiraTicketConfig(
            base_url=j.base_url.rstrip("/"),
            email=j.email,
            api_token=j.api_token,
            project_key=t.jira_project_key.strip(),
            issue_type=t.jira_issue_type,
        )

    @staticmethod
    def _post(cfg: _JiraTicketConfig, path: str, payload: dict) -> dict:
        """POST JSON to Jira and return the decoded response.

        Args:
            cfg: Resolved configuration.
            path: API path beginning with '/'.
            payload: Request body.

        Returns:
            The decoded JSON response.

        Raises:
            TicketingError: On any HTTP or transport failure. Jira's own error
                messages are extracted and surfaced, because they name the
                actual problem ("issue type is required", "project not found")
                far better than a status code does.
        """
        req = urllib.request.Request(
            f"{cfg.base_url}{path}",
            method="POST",
            data=json.dumps(payload).encode("utf-8"),
            headers={
                "Authorization": _auth_header(cfg),
                "Content-Type": "application/json",
                "Accept": "application/json",
            },
        )
        try:
            with urllib.request.urlopen(req, timeout=_TIMEOUT) as resp:
                body = resp.read().decode("utf-8")
                return json.loads(body) if body else {}
        except urllib.error.HTTPError as e:
            raise TicketingError(_http_error_message(e, cfg)) from e
        except urllib.error.URLError as e:
            raise TicketingError(
                f"Could not reach Jira at {cfg.base_url}: {e.reason}"
            ) from e

    @staticmethod
    def _summary(request: TicketRequest) -> str:
        """Build the issue title.

        Args:
            request: The support request.

        Returns:
            The user's summary, prefixed so NextGen2 tickets are recognisable in
            a shared queue, and truncated to Jira's 255-character limit.
        """
        text = (request.summary or "").strip() or "NextGen2 support request"
        prefixed = f"[NextGen2] {text}"
        return prefixed[:255]

    @staticmethod
    def _body(request: TicketRequest) -> str:
        """Build the issue description.

        Args:
            request: The support request.

        Returns:
            The user's description followed by the captured technical context,
            so whoever picks the ticket up has what they need without asking.
        """
        parts = [(request.description or "").strip() or "(no description given)"]

        parts.append("")
        parts.append("--- Raised from NextGen2 ---")
        parts.append(f"Category: {request.category}")
        parts.append(f"Raised by: {request.raised_by}")

        if request.context:
            parts.append("")
            parts.append("Context:")
            for key in sorted(request.context):
                value = request.context[key]
                if value in (None, "", [], {}):
                    continue
                parts.append(f"- {key}: {value}")

        return "\n".join(parts)


# ── module helpers ───────────────────────────────────────────────────────

def _auth_header(cfg: _JiraTicketConfig) -> str:
    """Build the basic auth header.

    Args:
        cfg: Resolved configuration.

    Returns:
        A 'Basic ...' header value.
    """
    raw = f"{cfg.email}:{cfg.api_token}".encode("utf-8")
    return "Basic " + base64.b64encode(raw).decode("ascii")


def _text_to_adf(text: str) -> dict[str, Any]:
    """Convert plain text into an Atlassian Document Format document.

    Jira Cloud's v3 API will not accept a string for `description`; it expects
    an ADF tree. This produces the minimal valid shape: one paragraph per
    line, with blank lines preserved as empty paragraphs so the context block
    stays readable.

    Args:
        text: Plain text, newline-separated.

    Returns:
        An ADF document node suitable for the description field.
    """
    content: list[dict[str, Any]] = []
    for line in (text or "").split("\n"):
        if line.strip():
            content.append({
                "type": "paragraph",
                "content": [{"type": "text", "text": line}],
            })
        else:
            # An empty paragraph — ADF rejects a text node with an empty
            # string, so the paragraph carries no content at all.
            content.append({"type": "paragraph"})

    if not content:
        content = [{"type": "paragraph"}]

    return {"type": "doc", "version": 1, "content": content}


def _slug(value: str) -> str:
    """Reduce a category to a label-safe token.

    Jira labels cannot contain spaces, so a category like 'how to' would be
    rejected outright.

    Args:
        value: The plain-English category.

    Returns:
        A lowercase token containing only letters, digits and hyphens.
    """
    cleaned = "".join(c if c.isalnum() else "-" for c in (value or "").lower())
    while "--" in cleaned:
        cleaned = cleaned.replace("--", "-")
    return cleaned.strip("-") or "general"


def _http_error_message(e: urllib.error.HTTPError, cfg: _JiraTicketConfig) -> str:
    """Turn a Jira HTTP error into a message that names the actual problem.

    Args:
        e: The raised HTTPError.
        cfg: Resolved configuration, for naming the project in the message.

    Returns:
        A message fit to show a user.
    """
    detail = ""
    try:
        payload = json.loads(e.read().decode("utf-8"))
        messages = list(payload.get("errorMessages") or [])
        messages += [f"{k}: {v}" for k, v in (payload.get("errors") or {}).items()]
        detail = "; ".join(m for m in messages if m)
    except Exception:  # noqa: BLE001 - the body may not be JSON at all
        detail = ""

    if e.code in (401, 403):
        return (
            "Jira rejected the credentials or the account lacks permission to "
            f"create issues in {cfg.project_key}." + (f" {detail}" if detail else "")
        )
    if e.code == 404:
        return (
            f"Jira project '{cfg.project_key}' was not found, or the account "
            "cannot see it." + (f" {detail}" if detail else "")
        )
    if e.code == 400:
        return (
            f"Jira rejected the ticket. This is usually an issue type that "
            f"does not exist in {cfg.project_key} — currently configured as "
            f"'{cfg.issue_type}'." + (f" Jira said: {detail}" if detail else "")
        )
    return f"Jira returned HTTP {e.code}." + (f" {detail}" if detail else "")
