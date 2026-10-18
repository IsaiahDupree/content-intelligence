"""Typed canonical records shared by all market data providers."""

from __future__ import annotations

import hashlib
import json
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Dict, List, Optional


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def isoformat(value: Optional[datetime]) -> Optional[str]:
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).isoformat()


def parse_datetime(value: Any) -> Optional[datetime]:
    if value in (None, ""):
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(value, tz=timezone.utc)
    text = str(value).strip().replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(text)
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def canonical_id(platform: str, kind: str, external_id: str) -> str:
    clean = str(external_id).strip()
    if not clean:
        raise ValueError(f"{kind} external_id is required")
    return f"{platform.lower()}:{kind}:{clean}"


def stable_hash(value: Any) -> str:
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


class SourceState(str, Enum):
    READY = "ready"
    RUNNING = "running"
    DEGRADED = "degraded"
    BLOCKED_CREDENTIAL = "blocked_credential"
    BLOCKED_APPROVAL = "blocked_approval"
    BLOCKED_QUOTA = "blocked_quota"
    DISABLED = "disabled"


@dataclass(frozen=True)
class MetricCounters:
    views: int = 0
    likes: int = 0
    comments: int = 0
    shares: int = 0
    saves: int = 0

    def __post_init__(self) -> None:
        for name, value in asdict(self).items():
            if int(value) < 0:
                raise ValueError(f"{name} must be non-negative")

    @classmethod
    def from_values(cls, **values: Any) -> "MetricCounters":
        def count(name: str) -> int:
            try:
                return max(0, int(values.get(name) or 0))
            except (TypeError, ValueError):
                return 0

        return cls(**{name: count(name) for name in ("views", "likes", "comments", "shares", "saves")})


@dataclass
class MarketContent:
    platform: str
    external_id: str
    creator_external_id: str
    published_at: Optional[datetime]
    observed_at: datetime
    source_id: str
    metrics: MetricCounters = field(default_factory=MetricCounters)
    creator_handle: str = ""
    creator_name: str = ""
    creator_followers: int = 0
    title: str = ""
    caption: str = ""
    description: str = ""
    language: str = ""
    url: str = ""
    thumbnail_url: str = ""
    media_type: str = "video"
    duration_seconds: Optional[float] = None
    hashtags: List[str] = field(default_factory=list)
    audio_id: str = ""
    audio_title: str = ""
    raw_payload: Dict[str, Any] = field(default_factory=dict)
    discovery_context: Dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        self.platform = self.platform.lower().strip()
        self.external_id = str(self.external_id).strip()
        self.creator_external_id = str(self.creator_external_id or self.creator_handle or "unknown").strip()
        if not self.platform or not self.external_id:
            raise ValueError("platform and external_id are required")
        self.published_at = parse_datetime(self.published_at)
        self.observed_at = parse_datetime(self.observed_at) or utc_now()
        self.creator_followers = max(0, int(self.creator_followers or 0))
        self.hashtags = sorted({str(tag).strip().lstrip("#").lower() for tag in self.hashtags if str(tag).strip()})

    @property
    def video_id(self) -> str:
        return canonical_id(self.platform, "video", self.external_id)

    @property
    def creator_id(self) -> str:
        return canonical_id(self.platform, "creator", self.creator_external_id)

    @property
    def raw_sha256(self) -> str:
        return stable_hash(self.raw_payload)

    @property
    def observation_key(self) -> str:
        return stable_hash({
            "video_id": self.video_id,
            "observed_at": isoformat(self.observed_at),
            "source_id": self.source_id,
            "metrics": asdict(self.metrics),
            "raw_sha256": self.raw_sha256,
        })


@dataclass
class SourceReceipt:
    run_id: str
    source_id: str
    platform: str
    state: SourceState
    started_at: datetime
    finished_at: datetime
    request_count: int = 0
    discovered_count: int = 0
    refreshed_count: int = 0
    accepted_count: int = 0
    duplicate_count: int = 0
    failed_count: int = 0
    quota_remaining: Optional[int] = None
    estimated_cost_usd: float = 0.0
    error_code: str = ""
    error_detail: str = ""
    cursor: str = ""
    metadata: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        data = asdict(self)
        data["state"] = self.state.value
        data["started_at"] = isoformat(self.started_at)
        data["finished_at"] = isoformat(self.finished_at)
        return data


@dataclass(frozen=True)
class QueryAttempt:
    """Immutable proof that one source actually attempted one query."""

    run_id: str
    source_id: str
    platform: str
    query: str
    attempted_at: datetime
    finished_at: datetime
    state: str
    result_count: int = 0
    request_count: int = 1
    error_code: str = ""
    error_detail: str = ""
    artifact_path: str = ""
    artifact_sha256: str = ""
    metadata: Dict[str, Any] = field(default_factory=dict)

    @property
    def attempt_key(self) -> str:
        artifact_identity = str(self.metadata.get("attempt_identity") or "").strip()
        if not artifact_identity:
            artifact_identity = self.artifact_sha256 or isoformat(self.attempted_at)
        return stable_hash({
            "source_id": self.source_id,
            "platform": self.platform.casefold(),
            "query": " ".join(self.query.casefold().split()),
            "artifact_identity": artifact_identity,
        })

    def to_dict(self) -> Dict[str, Any]:
        data = asdict(self)
        data["attempt_key"] = self.attempt_key
        data["attempted_at"] = isoformat(self.attempted_at)
        data["finished_at"] = isoformat(self.finished_at)
        return data


@dataclass
class ProviderBatch:
    items: List[MarketContent]
    receipt: SourceReceipt
    query_attempts: List[QueryAttempt] = field(default_factory=list)


def new_run_id() -> str:
    return f"mt-run-{uuid.uuid4()}"
