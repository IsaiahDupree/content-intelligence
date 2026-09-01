"""Direct, explicitly metered RapidAPI adapter for public Upwork demand data.

This adapter deliberately does not participate in browser automation.  It exposes
the provider's two documented POST operations and makes every credit-consuming
call opt-in at the call site.
"""

from __future__ import annotations

import math
import os
from typing import Any, Mapping
from urllib.parse import urlsplit

import httpx

from ..config import MarketTapeConfig
from .base import sanitize


DEFAULT_UPWORK_RAPIDAPI_HOST = "upwork-jobs-scraper-api.p.rapidapi.com"
UPWORK_RAPIDAPI_SOURCE_CONTRACT = "rapidapi_upwork_jobs_v1"


class UpworkAPIError(RuntimeError):
    """A credential, approval, transport, or provider-contract failure."""

    def __init__(
        self,
        message: str,
        *,
        code: str = "upstream_error",
        status_code: int = 502,
        request_id: str | None = None,
        retryable: bool = False,
        doc_url: str | None = None,
    ) -> None:
        self.code = code
        self.status_code = status_code
        self.message = sanitize(message)
        self.request_id = request_id
        self.retryable = retryable
        self.doc_url = doc_url
        super().__init__(self.message)

    def as_dict(self) -> dict[str, Any]:
        return {
            "ok": False,
            "error": {
                "code": self.code,
                "message": self.message,
                "status": self.status_code,
                "request_id": self.request_id,
                "retryable": self.retryable,
                "doc_url": self.doc_url,
            },
        }


class UpworkRapidAPIClient:
    """Small direct client for the RapidAPI Upwork ``/jobs`` contract.

    ``health`` is intentionally local and credit-free.  ``search_jobs`` and
    ``job_detail`` require ``execute_metered_reads=True`` on every invocation;
    enabling metered reads in process configuration alone is not sufficient.
    """

    source_id = "rapidapi_upwork"
    contract = UPWORK_RAPIDAPI_SOURCE_CONTRACT
    request_units_per_call = 1

    def __init__(
        self,
        config: MarketTapeConfig,
        *,
        client: httpx.Client | None = None,
        test_base_url: str | None = None,
        allow_loopback_test_transport: bool = False,
    ) -> None:
        self.config = config
        self.api_key = (
            os.getenv("UPWORK_SCRAPER_RAPIDAPI_KEY", "").strip()
            or os.getenv("RAPIDAPI_KEY", "").strip()
        )
        self.host = os.getenv(
            "UPWORK_SCRAPER_HOST", DEFAULT_UPWORK_RAPIDAPI_HOST
        ).strip().lower()
        configured_base_url = (
            os.getenv("UPWORK_SCRAPER_BASE_URL", "").strip()
            or f"https://{self.host}"
        )
        self.base_url = self._validated_base_url(
            configured_base_url,
            test_base_url=test_base_url,
            allow_loopback_test_transport=allow_loopback_test_transport,
        )
        self.request_timeout_seconds = float(
            config.upwork_request_timeout_seconds
        )
        if (
            not math.isfinite(self.request_timeout_seconds)
            or self.request_timeout_seconds <= 0
        ):
            raise ValueError(
                "MARKET_TAPE_UPWORK_REQUEST_TIMEOUT_SECONDS must be positive"
            )
        if client is not None and client.follow_redirects:
            raise ValueError("injected Upwork HTTP client must disable redirects")
        self._client = client or httpx.Client(
            timeout=self.request_timeout_seconds,
            follow_redirects=False,
        )
        self._owns_client = client is None

    def _validated_base_url(
        self,
        configured_base_url: str,
        *,
        test_base_url: str | None,
        allow_loopback_test_transport: bool,
    ) -> str:
        if bool(test_base_url) != bool(allow_loopback_test_transport):
            raise ValueError(
                "loopback test transport requires both explicit constructor arguments"
            )
        if test_base_url:
            parsed = urlsplit(test_base_url)
            if parsed.hostname not in {"127.0.0.1", "localhost", "::1"}:
                raise ValueError("test transport must target loopback")
            if parsed.scheme not in {"http", "https"}:
                raise ValueError("test transport must use HTTP or HTTPS")
            if parsed.username or parsed.password or parsed.query or parsed.fragment:
                raise ValueError("test transport URL cannot contain credentials/query/fragment")
            return test_base_url.rstrip("/")
        if not self.host.endswith(".p.rapidapi.com") or any(
            character in self.host for character in "/:@?#"
        ):
            raise ValueError("UPWORK_SCRAPER_HOST must be a RapidAPI hostname")
        parsed = urlsplit(configured_base_url)
        if parsed.scheme != "https":
            raise ValueError("UPWORK_SCRAPER_BASE_URL must use HTTPS")
        if parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise ValueError(
                "UPWORK_SCRAPER_BASE_URL cannot contain credentials/query/fragment"
            )
        if parsed.hostname != self.host or parsed.port not in {None, 443}:
            raise ValueError(
                "UPWORK_SCRAPER_BASE_URL hostname must match X-RapidAPI-Host"
            )
        return configured_base_url.rstrip("/")

    def close(self) -> None:
        if self._owns_client:
            self._client.close()

    def __enter__(self) -> UpworkRapidAPIClient:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def health(self) -> dict[str, Any]:
        """Return local readiness without making a provider request."""

        configured = bool(self.api_key and self.host and self.base_url)
        return {
            "source_id": self.source_id,
            "contract": self.contract,
            "status": "ready" if configured else "blocked_credential",
            "configured": configured,
            "metered": True,
            "request_units_per_call": self.request_units_per_call,
            "request_timeout_seconds": self.request_timeout_seconds,
            "execute_metered_reads_default": False,
            "base_url": self.base_url,
            "host": self.host,
            "credential_source": (
                "UPWORK_SCRAPER_RAPIDAPI_KEY"
                if os.getenv("UPWORK_SCRAPER_RAPIDAPI_KEY", "").strip()
                else "RAPIDAPI_KEY"
                if os.getenv("RAPIDAPI_KEY", "").strip()
                else ""
            ),
        }

    def search_jobs(
        self,
        *,
        keyword: str | None = None,
        search_url: str | None = None,
        sort: str | None = None,
        execute_metered_reads: bool = False,
    ) -> dict[str, Any]:
        """Execute one one-credit ``POST /jobs`` request."""

        normalized_keyword = str(keyword or "").strip()
        normalized_url = str(search_url or "").strip()
        if bool(normalized_keyword) == bool(normalized_url):
            raise ValueError("provide exactly one of keyword or search_url")
        payload: dict[str, Any] = (
            {"keyword": normalized_keyword}
            if normalized_keyword
            else {"searchUrl": normalized_url}
        )
        if str(sort or "").strip():
            payload["sort"] = str(sort).strip()
        response = self._metered_post(
            "/jobs",
            payload,
            execute_metered_reads=execute_metered_reads,
        )
        data = response.get("data")
        if not isinstance(data, Mapping) or not isinstance(data.get("jobs"), list):
            raise UpworkAPIError(
                "unexpected response shape (data.jobs must be a list)",
                code="unexpected_shape",
                status_code=502,
            )
        meta = response.get("meta") or {}
        if not isinstance(meta, Mapping):
            meta = {}
        return {
            "ok": True,
            "query": {
                "keyword": normalized_keyword or None,
                "search_url": normalized_url or None,
                "sort": str(sort).strip() if str(sort or "").strip() else None,
            },
            # Preserve every list element. The demand ledger performs typed
            # normalization and must account for malformed elements as
            # rejected/partial evidence instead of silently dropping them.
            "jobs": list(data["jobs"]),
            "count": data.get("count"),
            "estimated_total": data.get("estimatedTotal"),
            "truncated": data.get("truncated"),
            "partial": data.get("partial"),
            "credits_used": meta.get("creditsUsed"),
            "request_id": meta.get("requestId"),
            "tool": meta.get("tool"),
        }

    def job_detail(
        self,
        *,
        job_id: str | None = None,
        job_url: str | None = None,
        execute_metered_reads: bool = False,
    ) -> dict[str, Any]:
        """Execute one one-credit ``POST /job-detail`` request."""

        normalized_id = str(job_id or "").strip()
        normalized_url = str(job_url or "").strip()
        if bool(normalized_id) == bool(normalized_url):
            raise ValueError("provide exactly one of job_id or job_url")
        payload = (
            {"jobId": normalized_id}
            if normalized_id
            else {"jobUrl": normalized_url}
        )
        response = self._metered_post(
            "/job-detail",
            payload,
            execute_metered_reads=execute_metered_reads,
        )
        data = response.get("data") or {}
        if not isinstance(data, Mapping):
            data = {}
        job = data.get("job")
        if not isinstance(job, Mapping):
            raise UpworkAPIError(
                "unexpected response shape (data.job must be an object)",
                code="unexpected_shape",
                status_code=502,
            )
        meta = response.get("meta") or {}
        if not isinstance(meta, Mapping):
            meta = {}
        return {
            "ok": True,
            "job": dict(job),
            "credits_used": meta.get("creditsUsed"),
            "request_id": meta.get("requestId"),
            "tool": meta.get("tool"),
        }

    def _metered_post(
        self,
        path: str,
        payload: Mapping[str, Any],
        *,
        execute_metered_reads: bool,
    ) -> dict[str, Any]:
        if not execute_metered_reads:
            raise UpworkAPIError(
                "metered RapidAPI reads require execute_metered_reads=true",
                code="metered_reads_disabled",
                status_code=403,
            )
        if not self.config.allow_metered_reads:
            raise UpworkAPIError(
                "MARKET_TAPE_ALLOW_METERED_READS is disabled",
                code="metered_reads_not_configured",
                status_code=403,
            )
        if not self.api_key:
            raise UpworkAPIError(
                "missing UPWORK_SCRAPER_RAPIDAPI_KEY or RAPIDAPI_KEY",
                code="not_configured",
                status_code=503,
            )
        try:
            response = self._client.post(
                f"{self.base_url}{path}",
                headers={
                    "X-RapidAPI-Key": self.api_key,
                    "X-RapidAPI-Host": self.host,
                    "Accept": "application/json",
                    "Content-Type": "application/json",
                },
                json=dict(payload),
            )
        except httpx.HTTPError as error:
            raise UpworkAPIError(
                f"upstream unreachable: {error.__class__.__name__}",
                code="upstream_unreachable",
                status_code=502,
                retryable=True,
            ) from error
        try:
            body = response.json()
        except ValueError as error:
            raise UpworkAPIError(
                f"non-JSON response (HTTP {response.status_code})",
                code="non_json_response",
                status_code=response.status_code if response.status_code >= 400 else 502,
                retryable=response.status_code >= 500,
            ) from error
        if not isinstance(body, dict):
            raise UpworkAPIError(
                "unexpected response shape (no 'data' field)",
                code="unexpected_shape",
                status_code=502,
            )
        is_error = response.status_code >= 400 or (
            "data" not in body and bool(body.get("code"))
        )
        if is_error:
            fallback_status = response.status_code if response.status_code >= 400 else 502
            try:
                upstream_status = int(body.get("status", fallback_status))
            except (TypeError, ValueError):
                upstream_status = fallback_status
            raise UpworkAPIError(
                str(body.get("message") or f"HTTP {response.status_code}"),
                code=str(body.get("code") or "upstream_error"),
                status_code=upstream_status,
                request_id=(str(body["requestId"]) if body.get("requestId") else None),
                retryable=bool(body.get("retryable", fallback_status >= 500)),
                doc_url=(str(body["docUrl"]) if body.get("docUrl") else None),
            )
        if "data" not in body:
            raise UpworkAPIError(
                "unexpected response shape (no 'data' field)",
                code="unexpected_shape",
                status_code=502,
            )
        return body
