"""Authenticated loopback client for Market Tape script-language demand.

Content Quality owns the decision that a requested script does not yet have
enough verified language evidence.  Market Tape owns discovery and transcript
acquisition.  This client is the deliberately narrow boundary between them:
it can enqueue one typed demand record, but it cannot execute arbitrary SQL or
reach a non-loopback service.
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from typing import Any
from urllib.parse import urlsplit


DEFAULT_MARKET_TAPE_API_URL = "http://127.0.0.1:6006"
LOOPBACK_HOSTS = {"127.0.0.1", "localhost", "::1"}


class DemandClientError(RuntimeError):
    """A safe, classified Market Tape demand submission failure."""

    def __init__(self, code: str, *, http_status: int | None = None) -> None:
        super().__init__(code)
        self.code = code
        self.http_status = http_status


class MarketTapeDemandClient:
    def __init__(
        self,
        *,
        base_url: str = DEFAULT_MARKET_TAPE_API_URL,
        control_token: str,
        timeout_seconds: float = 5.0,
    ) -> None:
        self.base_url = str(base_url).strip().rstrip("/")
        self.control_token = str(control_token).strip()
        self.timeout_seconds = max(0.25, min(float(timeout_seconds), 15.0))
        parsed = urlsplit(self.base_url)
        if parsed.scheme != "http" or parsed.hostname not in LOOPBACK_HOSTS:
            raise ValueError("Market Tape demand API must be an HTTP loopback URL")

    @classmethod
    def from_environment(cls) -> "MarketTapeDemandClient | None":
        token = str(os.getenv("MARKET_TAPE_CONTROL_TOKEN") or "").strip()
        if not token:
            return None
        return cls(
            base_url=(
                os.getenv("MARKET_TAPE_API_URL")
                or DEFAULT_MARKET_TAPE_API_URL
            ),
            control_token=token,
            timeout_seconds=float(
                os.getenv("MARKET_TAPE_DEMAND_TIMEOUT_SECONDS", "5")
            ),
        )

    def enqueue(self, payload: dict[str, Any]) -> dict[str, Any]:
        if not self.control_token:
            raise DemandClientError("MARKET_TAPE_CONTROL_TOKEN_NOT_CONFIGURED")
        body = json.dumps(
            payload, sort_keys=True, separators=(",", ":"), default=str
        ).encode("utf-8")
        request = urllib.request.Request(
            f"{self.base_url}/api/market-tape/script-language-demands",
            data=body,
            method="POST",
            headers={
                "Authorization": f"Bearer {self.control_token}",
                "Content-Type": "application/json",
                "Accept": "application/json",
                "X-Agent-Principal": "content-quality",
            },
        )
        try:
            with urllib.request.urlopen(
                request, timeout=self.timeout_seconds
            ) as response:
                status = int(response.status)
                response_body = response.read()
        except urllib.error.HTTPError as exc:
            raise DemandClientError(
                "MARKET_TAPE_DEMAND_REJECTED", http_status=int(exc.code)
            ) from exc
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise DemandClientError("MARKET_TAPE_DEMAND_UNAVAILABLE") from exc
        if not 200 <= status < 300:
            raise DemandClientError(
                "MARKET_TAPE_DEMAND_REJECTED", http_status=status
            )
        try:
            decoded = json.loads(response_body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise DemandClientError("MARKET_TAPE_DEMAND_INVALID_RESPONSE") from exc
        if not isinstance(decoded, dict) or not decoded.get("demand_id"):
            raise DemandClientError("MARKET_TAPE_DEMAND_INVALID_RESPONSE")
        return decoded


__all__ = ["DemandClientError", "MarketTapeDemandClient"]
