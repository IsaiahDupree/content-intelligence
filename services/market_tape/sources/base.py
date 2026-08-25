"""Provider contract, request ceilings, and secret-safe error handling."""

from __future__ import annotations

import hashlib
import os
import re
from abc import ABC, abstractmethod
from datetime import datetime
from typing import Any, Callable, Dict, List, Optional, Sequence, Set

import httpx

from ..config import MarketTapeConfig
from ..models import MarketContent, ProviderBatch, SourceReceipt, SourceState, utc_now


SECRET_RE = re.compile(
    r"(?i)(access_token|api[_-]?key|authorization|client_secret|key)=([^&\s]+)"
)


class SourceError(RuntimeError):
    code = "source_error"


class SourceCredentialError(SourceError):
    code = "credential_missing"


class SourceApprovalError(SourceError):
    code = "metered_reads_disabled"


class SourceQuotaError(SourceError):
    code = "request_budget_exhausted"


class SourceHTTPError(SourceError):
    code = "provider_http_error"

    def __init__(self, status_code: int, detail: str = ""):
        self.status_code = status_code
        if status_code == 429:
            self.code = "provider_rate_limited"
        elif status_code == 401:
            self.code = "provider_authentication_failed"
        elif status_code == 403:
            self.code = "provider_auth_or_quota"
        super().__init__(f"provider returned HTTP {status_code}: {sanitize(detail)[:300]}")


def sanitize(value: Any) -> str:
    return SECRET_RE.sub(lambda match: f"{match.group(1)}=<redacted>", str(value or ""))


class MarketSource(ABC):
    source_id = "source"
    platform = "unknown"
    metered = False
    credential_names: Sequence[str] = ()

    def __init__(
        self,
        config: MarketTapeConfig,
        run_id: str,
        request_budget: int,
        *,
        client: Optional[httpx.Client] = None,
    ):
        self.config = config
        self.run_id = run_id
        self.request_budget = max(0, request_budget)
        self.request_count = 0
        self._receipted_request_count = 0
        self.client = client or httpx.Client(timeout=config.request_timeout_seconds, follow_redirects=True)
        self._owns_client = client is None
        self.known_external_ids: Callable[[Sequence[str]], Set[str]] = lambda _: set()
        self.recent_metadata_total: Callable[[str], int] = lambda _: 0

    def close(self) -> None:
        if self._owns_client:
            self.client.close()

    def credentials_available(self) -> bool:
        return all(bool(os.getenv(name, "").strip()) for name in self.credential_names)

    def missing_credentials(self) -> List[str]:
        return [name for name in self.credential_names if not os.getenv(name, "").strip()]

    def credential_material(self) -> Sequence[str]:
        """Resolved secret material used only to derive a non-reversible digest."""

        return tuple(os.getenv(name, "").strip() for name in self.credential_names)

    def credential_fingerprint(self) -> str:
        material = tuple(self.credential_material())
        if not any(material):
            return ""
        digest = hashlib.sha256()
        for value in (self.source_id, *material):
            encoded = value.encode("utf-8")
            digest.update(len(encoded).to_bytes(8, "big"))
            digest.update(encoded)
        return digest.hexdigest()

    def terminal_metrics_capable(self) -> bool:
        """Whether refreshes can produce authoritative engagement counters.

        Discovery-only and metadata-only adapters override this so they can
        never be admitted as terminal forecast measurement sources.
        """

        return True

    def adaptive_query_execution_capable(self) -> bool:
        """Whether this cycle can execute the supplied query against a provider."""

        return True

    def measurement_refresh_batch_size(self) -> int:
        """Maximum tracked items covered by one terminal refresh request."""

        return 1

    def measurement_request_units_per_batch(self) -> int:
        """Conservative request units reserved for one terminal batch."""

        return 1

    def preflight(self) -> None:
        if self.platform not in self.config.platforms:
            raise SourceError(f"platform {self.platform} is disabled")
        if not self.credentials_available():
            raise SourceCredentialError("missing " + ", ".join(self.missing_credentials()))
        if self.metered and not self.config.allow_metered_reads:
            raise SourceApprovalError("metered provider reads are disabled")
        if self.request_budget <= 0:
            raise SourceQuotaError("daily request ceiling reached")

    def request_json(
        self,
        method: str,
        url: str,
        *,
        headers: Optional[Dict[str, str]] = None,
        params: Optional[Dict[str, Any]] = None,
        json_body: Optional[Dict[str, Any]] = None,
        form: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        if self.request_count >= self.request_budget:
            raise SourceQuotaError("request budget exhausted during collection")
        self.request_count += 1
        try:
            response = self.client.request(
                method,
                url,
                headers=headers,
                params=params,
                json=json_body,
                data=form,
            )
        except httpx.HTTPError as error:
            raise SourceError(sanitize(error.__class__.__name__)) from error
        if response.status_code >= 400:
            detail = ""
            try:
                body = response.json()
                detail = body.get("error", body.get("message", "")) if isinstance(body, dict) else ""
            except ValueError:
                detail = response.text[:300]
            raise SourceHTTPError(response.status_code, detail)
        try:
            body = response.json()
        except ValueError as error:
            raise SourceError("provider returned invalid JSON") from error
        if not isinstance(body, dict):
            raise SourceError("provider returned a non-object response")
        return body

    def blocked_batch(self, started_at: datetime, error: Exception) -> ProviderBatch:
        operation_requests = self._operation_request_count()
        if isinstance(error, SourceCredentialError) or getattr(error, "code", "") == "provider_authentication_failed":
            state = SourceState.BLOCKED_CREDENTIAL
        elif isinstance(error, SourceApprovalError):
            state = SourceState.BLOCKED_APPROVAL
        elif isinstance(error, (SourceQuotaError, SourceHTTPError)) and getattr(error, "code", "") in {
            "request_budget_exhausted", "provider_rate_limited", "provider_auth_or_quota"
        }:
            state = SourceState.BLOCKED_QUOTA
        else:
            state = SourceState.DEGRADED
        return ProviderBatch([], SourceReceipt(
            run_id=self.run_id,
            source_id=self.source_id,
            platform=self.platform,
            state=state,
            started_at=started_at,
            finished_at=utc_now(),
            request_count=operation_requests,
            estimated_cost_usd=operation_requests * self.config.request_cost_for(self.platform),
            quota_remaining=max(0, self.request_budget - self.request_count),
            error_code=getattr(error, "code", "source_error"),
            error_detail=sanitize(error),
            metadata={
                "metered": self.metered,
                "credential_fingerprint": self.credential_fingerprint(),
            },
        ))

    def success_batch(
        self,
        started_at: datetime,
        items: List[MarketContent],
        *,
        operation: str,
        cursor: str = "",
        metadata: Optional[Dict[str, Any]] = None,
    ) -> ProviderBatch:
        operation_requests = self._operation_request_count()
        return ProviderBatch(items, SourceReceipt(
            run_id=self.run_id,
            source_id=self.source_id,
            platform=self.platform,
            state=SourceState.READY,
            started_at=started_at,
            finished_at=utc_now(),
            request_count=operation_requests,
            estimated_cost_usd=operation_requests * self.config.request_cost_for(self.platform),
            discovered_count=(
                len(items)
                if operation in {"discover", "discover_performance"}
                else 0
            ),
            refreshed_count=len(items) if operation == "refresh" else 0,
            quota_remaining=max(0, self.request_budget - self.request_count),
            cursor=cursor,
            metadata={
                "metered": self.metered,
                "operation": operation,
                **(metadata or {}),
                "credential_fingerprint": self.credential_fingerprint(),
            },
        ))

    def _operation_request_count(self) -> int:
        count = max(0, self.request_count - self._receipted_request_count)
        self._receipted_request_count = self.request_count
        return count

    @abstractmethod
    def discover(self, max_items: int) -> ProviderBatch:
        raise NotImplementedError

    @abstractmethod
    def refresh(self, tracked: Sequence[Dict[str, Any]]) -> ProviderBatch:
        raise NotImplementedError
