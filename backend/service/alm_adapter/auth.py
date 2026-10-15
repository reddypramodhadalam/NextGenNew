"""
service/alm_adapter/auth.py

ALM session authentication (username/password per run).
"""

import base64
import logging

from service.alm_adapter.client import ALMClient
from service.alm_adapter.models import ALMContext

logger = logging.getLogger(__name__)

_SESSION_XML = "<session-parameters><client-type>REST</client-type></session-parameters>"


def _normalize_base_url(base_url: str) -> str:
    url = (base_url or "").strip().rstrip("/")
    if not url:
        raise ValueError("ALM base URL is required")
    if not (url.startswith("http://") or url.startswith("https://")):
        raise ValueError("ALM base URL must start with http:// or https://")
    return url


def _project_session_url(base_url: str, domain: str, project: str) -> str:
    return f"{base_url}/rest/domains/{domain}/projects/{project}/sessions"


def _site_session_url(base_url: str) -> str:
    return f"{base_url}/rest/site-session"


def login(base_url: str, username: str, password: str, domain: str, project: str) -> ALMContext:
    base_url = _normalize_base_url(base_url)
    domain = (domain or "").strip()
    project = (project or "").strip()
    username = (username or "").strip()

    if not domain:
        raise ValueError("ALM domain is required")
    if not project:
        raise ValueError("ALM project is required")
    if not username:
        raise ValueError("ALM username is required")
    if not password:
        raise ValueError("ALM password is required")

    client = ALMClient()
    basic = base64.b64encode(f"{username}:{password}".encode("utf-8")).decode("utf-8")
    auth_headers = {
        "Authorization": f"Basic {basic}",
        "Accept": "application/json",
    }

    auth_resp = client.post(f"{base_url}/authentication-point/authenticate", headers=auth_headers)
    if auth_resp.status_code == 401:
        raise ValueError("Invalid ALM credentials")
    if auth_resp.status_code == 403:
        raise ValueError("ALM access denied for this user")
    if auth_resp.status_code not in (200, 201):
        raise RuntimeError(f"ALM authentication failed with HTTP {auth_resp.status_code}")

    lwsso = auth_resp.cookies.get("LWSSO_COOKIE_KEY", "")
    if not lwsso:
        raise ValueError("ALM did not return LWSSO_COOKIE_KEY")

    base_headers = {
        "Cookie": f"LWSSO_COOKIE_KEY={lwsso}",
        "Accept": "application/json",
    }

    qc_session = ""
    project_url = _project_session_url(base_url, domain, project)

    for method_name in ("post_xml", "get"):
        try:
            if method_name == "post_xml":
                resp = client.post_xml(project_url, headers=base_headers, xml_body=_SESSION_XML)
            else:
                resp = client.get(project_url, headers=base_headers)
            if resp.status_code in (200, 201):
                qc_session = resp.cookies.get("QCSession", "") or qc_session
                if qc_session:
                    logger.info("alm_auth: QCSession established for domain=%s project=%s", domain, project)
                    break
        except Exception as exc:
            logger.debug("alm_auth: project session %s failed: %s", method_name, type(exc).__name__)

    if not qc_session:
        site_url = _site_session_url(base_url)
        for method_name in ("post_xml", "get"):
            try:
                if method_name == "post_xml":
                    resp = client.post_xml(site_url, headers=base_headers, xml_body=_SESSION_XML)
                else:
                    resp = client.get(site_url, headers=base_headers)
                if resp.status_code in (200, 201):
                    qc_session = resp.cookies.get("QCSession", "") or qc_session
                    if qc_session:
                        logger.info("alm_auth: QCSession established via site session")
                        break
            except Exception as exc:
                logger.debug("alm_auth: site session %s failed: %s", method_name, type(exc).__name__)

    if not qc_session:
        logger.warning(
            "alm_auth: authenticated but QCSession not established for domain=%s project=%s",
            domain, project,
        )

    return ALMContext(
        base_url=base_url,
        domain=domain,
        project=project,
        lwsso_cookie_key=lwsso,
        qc_session=qc_session,
    )
