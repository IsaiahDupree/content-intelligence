from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlparse


UTC = timezone.utc
PUBLICATION_CONTRACT = "owned_publication_receipt_v1"
PUBLICATION_PLATFORMS = (
    "facebook", "instagram", "linkedin", "threads", "tiktok", "x", "youtube",
)
TERMINAL_PROVIDER_STATES = {"completed", "posted", "published", "success"}
NONTERMINAL_PROVIDER_STATES = {
    "accepted", "pending", "processing", "publishing", "queued", "scheduled",
    "submitted",
}


class OwnedPublicationAttributionService:
    """Verifies immutable semantic-asset-to-provider-post receipts."""

    PLATFORM_ALIASES = {
        "facebook_reels": "facebook",
        "fb": "facebook",
        "ig": "instagram",
        "instagram_reels": "instagram",
        "twitter": "x",
        "twitter_x": "x",
        "youtube_shorts": "youtube",
        "yt": "youtube",
    }

    def __init__(self, store: Any, tape: Any):
        self.store = store
        self.tape = tape

    @staticmethod
    def _required_text(
        payload: dict[str, Any], field: str, *, maximum: int = 4_000
    ) -> str:
        value = payload.get(field)
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"{field} is required")
        result = value.strip()
        if len(result) > maximum:
            raise ValueError(f"{field} must be at most {maximum} characters")
        return result

    @classmethod
    def _sha256(cls, payload: dict[str, Any], field: str) -> str:
        value = cls._required_text(payload, field, maximum=64).lower()
        if not re.fullmatch(r"[0-9a-f]{64}", value):
            raise ValueError(f"{field} must be a lowercase SHA-256 hex digest")
        return value

    @classmethod
    def _timestamp(cls, payload: dict[str, Any], field: str) -> str:
        value = cls._required_text(payload, field, maximum=80)
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError as exc:
            raise ValueError(f"{field} must be an ISO-8601 timestamp") from exc
        if parsed.tzinfo is None or parsed.utcoffset() is None:
            raise ValueError(f"{field} must include a timezone")
        return parsed.astimezone(UTC).isoformat()

    @classmethod
    def _platform(cls, payload: dict[str, Any]) -> str:
        raw = cls._required_text(payload, "source_platform", maximum=80)
        normalized = re.sub(r"[^a-z0-9]+", "_", raw.lower()).strip("_")
        canonical = cls.PLATFORM_ALIASES.get(normalized, normalized)
        if canonical not in PUBLICATION_PLATFORMS:
            raise ValueError(
                "source_platform must be one of: "
                + ", ".join(PUBLICATION_PLATFORMS)
            )
        return canonical

    @classmethod
    def _https_url(cls, payload: dict[str, Any], field: str) -> str:
        value = cls._required_text(payload, field, maximum=4_000)
        parsed = urlparse(value)
        if parsed.scheme.lower() != "https" or not parsed.netloc:
            raise ValueError(f"{field} must be an absolute HTTPS URL")
        if (parsed.hostname or "").lower() in {"localhost", "127.0.0.1", "::1"}:
            raise ValueError(f"{field} must identify a public provider post")
        return value

    @staticmethod
    def _canonical_receipt(payload: dict[str, Any]) -> tuple[dict[str, Any], str]:
        receipt = payload.get("provider_receipt")
        if not isinstance(receipt, dict) or not receipt:
            raise ValueError("provider_receipt must be a non-empty object")
        try:
            encoded = json.dumps(
                receipt, sort_keys=True, separators=(",", ":"), ensure_ascii=True
            )
        except (TypeError, ValueError) as exc:
            raise ValueError("provider_receipt must be JSON serializable") from exc
        if len(encoded.encode("utf-8")) > 262_144:
            raise ValueError("provider_receipt must be at most 262144 encoded bytes")
        return receipt, hashlib.sha256(encoded.encode("utf-8")).hexdigest()

    @staticmethod
    def _receipt_contains(receipt: Any, expected: str) -> bool:
        if isinstance(receipt, dict):
            return any(
                OwnedPublicationAttributionService._receipt_contains(value, expected)
                for value in receipt.values()
            )
        if isinstance(receipt, list):
            return any(
                OwnedPublicationAttributionService._receipt_contains(value, expected)
                for value in receipt
            )
        return str(receipt) == expected

    @staticmethod
    def _receipt_values_for_keys(receipt: Any, keys: set[str]) -> list[str]:
        values: list[str] = []
        if isinstance(receipt, dict):
            for key, value in receipt.items():
                normalized = re.sub(r"[^a-z0-9]+", "_", str(key).lower()).strip("_")
                if normalized in keys and not isinstance(value, (dict, list)):
                    values.append(str(value))
                values.extend(
                    OwnedPublicationAttributionService._receipt_values_for_keys(
                        value, keys
                    )
                )
        elif isinstance(receipt, list):
            for value in receipt:
                values.extend(
                    OwnedPublicationAttributionService._receipt_values_for_keys(
                        value, keys
                    )
                )
        return values

    @classmethod
    def _assert_terminal_provider_receipt(
        cls,
        receipt: dict[str, Any],
        *,
        platform: str,
        provider_post_id: str,
        provider_post_url: str,
    ) -> None:
        states = {
            value.strip().lower()
            for value in cls._receipt_values_for_keys(receipt, {"state", "status"})
            if value.strip()
        }
        if states & NONTERMINAL_PROVIDER_STATES:
            raise ValueError(
                "provider_receipt is non-terminal; a submission or processing "
                "receipt is not a published post"
            )
        if not states & TERMINAL_PROVIDER_STATES:
            raise ValueError(
                "provider_receipt must contain an explicit terminal published state"
            )
        native_ids = set(cls._receipt_values_for_keys(
            receipt,
            {
                "id", "native_post_id", "platform_post_id", "post_id",
                "provider_post_id", "video_id",
            },
        ))
        if provider_post_id not in native_ids:
            raise ValueError(
                "provider_post_id must be present in a native post identity field"
            )
        hosts = {
            "facebook": ("facebook.com", "fb.watch"),
            "instagram": ("instagram.com",),
            "linkedin": ("linkedin.com",),
            "threads": ("threads.net",),
            "tiktok": ("tiktok.com",),
            "x": ("x.com", "twitter.com"),
            "youtube": ("youtube.com", "youtu.be"),
        }[platform]
        hostname = (urlparse(provider_post_url).hostname or "").lower()
        if not any(hostname == host or hostname.endswith(f".{host}") for host in hosts):
            raise ValueError(
                "provider_post_url must identify the declared platform's public post"
            )

    @staticmethod
    def _file_sha256(path: Path) -> tuple[str, int]:
        digest = hashlib.sha256()
        size = 0
        with path.open("rb") as source:
            for chunk in iter(lambda: source.read(1024 * 1024), b""):
                digest.update(chunk)
                size += len(chunk)
        return digest.hexdigest(), size

    @staticmethod
    def _metadata(payload: dict[str, Any]) -> dict[str, Any]:
        metadata = payload.get("metadata") or {}
        if not isinstance(metadata, dict):
            raise ValueError("metadata must be an object")
        encoded = json.dumps(metadata, sort_keys=True, separators=(",", ":"))
        if len(encoded.encode("utf-8")) > 32_768:
            raise ValueError("metadata must be at most 32768 encoded bytes")
        return metadata

    def register(self, payload: dict[str, Any]) -> dict[str, Any]:
        if not isinstance(payload, dict):
            raise ValueError("JSON body must be an object")
        content_id = self._required_text(payload, "content_id", maximum=500)
        semantic_asset = self.tape.semantic_content_asset(content_id)
        if semantic_asset is None:
            raise ValueError(
                "content_id is not registered in the semantic content lineage"
            )

        local_asset_path = Path(
            self._required_text(payload, "local_asset_path", maximum=4_000)
        ).expanduser().resolve()
        if not local_asset_path.is_file():
            raise ValueError("local_asset_path must identify an existing file")
        expected_asset_sha256 = self._sha256(payload, "local_asset_sha256")
        observed_asset_sha256, asset_bytes = self._file_sha256(local_asset_path)
        if observed_asset_sha256 != expected_asset_sha256:
            raise ValueError(
                "local_asset_sha256 does not match the bytes at local_asset_path"
            )
        if asset_bytes <= 0:
            raise ValueError("local asset must not be empty")

        provider_receipt, observed_provider_receipt_sha256 = (
            self._canonical_receipt(payload)
        )
        expected_provider_receipt_sha256 = self._sha256(
            payload, "provider_receipt_sha256"
        )
        if observed_provider_receipt_sha256 != expected_provider_receipt_sha256:
            raise ValueError(
                "provider_receipt_sha256 does not match provider_receipt"
            )
        provider_receipt_id = self._required_text(
            payload, "provider_receipt_id", maximum=500
        )
        provider_post_id = self._required_text(
            payload, "provider_post_id", maximum=1_000
        )
        provider_post_url = self._https_url(payload, "provider_post_url")
        for field, value in (
            ("provider_receipt_id", provider_receipt_id),
            ("provider_post_id", provider_post_id),
            ("provider_post_url", provider_post_url),
        ):
            if not self._receipt_contains(provider_receipt, value):
                raise ValueError(
                    f"{field} is not present in the terminal provider_receipt"
                )

        source_platform = self._platform(payload)
        self._assert_terminal_provider_receipt(
            provider_receipt,
            platform=source_platform,
            provider_post_id=provider_post_id,
            provider_post_url=provider_post_url,
        )

        publication = {
            "contract": PUBLICATION_CONTRACT,
            "idempotency_key": self._required_text(
                payload, "idempotency_key", maximum=500
            ),
            "content_id": content_id,
            "campaign_id": self._required_text(
                payload, "campaign_id", maximum=500
            ),
            "offer_id": self._required_text(payload, "offer_id", maximum=500),
            "semantic_asset_id": str(semantic_asset["asset_id"]),
            "semantic_asset_sha256": str(semantic_asset["asset_sha256"]),
            "local_asset_path": str(local_asset_path),
            "local_asset_sha256": observed_asset_sha256,
            "local_asset_bytes": asset_bytes,
            "source_platform": source_platform,
            "account_id": self._required_text(
                payload, "account_id", maximum=500
            ),
            "publisher": self._required_text(
                payload, "publisher", maximum=100
            ).lower(),
            "provider_post_id": provider_post_id,
            "provider_post_url": provider_post_url,
            "published_at": self._timestamp(payload, "published_at"),
            "provider_receipt_id": provider_receipt_id,
            "provider_receipt_sha256": observed_provider_receipt_sha256,
            "provider_receipt": provider_receipt,
            "metadata": self._metadata(payload),
        }
        stored, created = self.store.put_owned_publication(publication)
        return {
            "status": "created" if created else "idempotent_replay",
            "created": created,
            "publication": stored,
        }

    def binding(self, payload: dict[str, Any]) -> dict[str, Any]:
        publication_id = self._required_text(
            payload, "publication_id", maximum=500
        )
        receipt_sha256 = self._sha256(
            payload, "publication_receipt_sha256"
        )
        publication = self.store.owned_publication(
            publication_id=publication_id,
            publication_receipt_sha256=receipt_sha256,
        )
        if publication is None:
            raise ValueError(
                "publication_id and publication_receipt_sha256 do not identify "
                "a registered publication receipt"
            )
        return publication

    def readiness(self, payload: dict[str, Any]) -> dict[str, Any]:
        publication = self.binding(payload)
        exact = publication["attribution"]
        events = [
            event for event in self.store.owned_outcome_events(exact, limit=500)
            if (event.get("publication_binding") or {}).get("publication_id")
                == publication["publication_id"]
        ]
        samples = [
            sample
            for sample in self.store.owned_retention_samples(exact, limit=2_000)
            if (sample.get("publication_binding") or {}).get("publication_id")
                == publication["publication_id"]
        ]
        stages = {
            name: sum(event["event_type"] == name for event in events)
            for name in ("click", "install", "trial", "purchase")
        }
        observed = bool(events or samples)
        return {
            "status": (
                "owned_evidence_observed"
                if observed else "awaiting_owned_evidence"
            ),
            "contract": "owned_publication_outcome_readiness_v1",
            "read_only": True,
            "publication": publication,
            "strictly_bound_event_count": len(events),
            "strictly_bound_retention_sample_count": len(samples),
            "event_stage_counts": stages,
            "first_owned_evidence_observed": observed,
            "outcome_claim": (
                "observed_first_party_facts_only" if observed else "none"
            ),
            "missing_stages": (
                [] if observed else ["owned_outcome_or_retention"]
            ),
            "causal_claim": False,
        }
