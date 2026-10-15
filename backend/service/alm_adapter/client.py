"""
service/alm_adapter/client.py

HTTP client for ALM REST calls.
"""

import logging
import time

import httpx

from core.settings import settings

logger = logging.getLogger(__name__)


class ALMClient:
    def __init__(self) -> None:
        self._client = httpx.Client(
            timeout=httpx.Timeout(
                connect=settings.alm.connect_timeout_sec,
                read=settings.alm.read_timeout_sec,
                write=settings.alm.read_timeout_sec,
                pool=settings.alm.read_timeout_sec,
            ),
            verify=settings.alm.ssl_verify,
        )

    def _request_with_retry(self, method: str, url: str, **kwargs) -> httpx.Response:
        attempts = max(1, settings.alm.max_retries)
        backoff_s = max(0.0, settings.alm.retry_backoff_base_ms / 1000.0)
        last_exc: Exception | None = None

        for attempt in range(1, attempts + 1):
            try:
                return self._client.request(method, url, **kwargs)
            except (httpx.ConnectError, httpx.ConnectTimeout, httpx.ReadTimeout, httpx.WriteTimeout) as exc:
                last_exc = exc
                logger.warning(
                    "alm_client: transient network error on %s %s (attempt %d/%d): %s",
                    method, url, attempt, attempts, type(exc).__name__,
                )
                if attempt < attempts and backoff_s > 0:
                    time.sleep(backoff_s * attempt)

        if last_exc:
            raise last_exc
        raise RuntimeError("ALM request retry exhausted without explicit exception")

    def get(self, url: str, headers: dict[str, str], params: dict | None = None) -> httpx.Response:
        return self._request_with_retry("GET", url, headers=headers, params=params)

    def post(self, url: str, headers: dict[str, str], json_body: dict | None = None) -> httpx.Response:
        return self._request_with_retry("POST", url, headers=headers, json=json_body)

    def post_xml(self, url: str, headers: dict[str, str], xml_body: str) -> httpx.Response:
        merged = {**headers, "Content-Type": "application/xml"}
        return self._request_with_retry("POST", url, headers=merged, content=xml_body.encode("utf-8"))
