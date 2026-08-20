"""Runtime configuration for the social market tape."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List

from dotenv import load_dotenv


REPO_ROOT = Path(__file__).resolve().parents[2]


def load_runtime_environment() -> None:
    """Load existing private env files without copying credentials into this repo."""
    configured = os.getenv("MARKET_TAPE_ENV_FILES", "")
    paths = [Path(value).expanduser() for value in configured.split(":") if value]
    paths.extend(
        [
            REPO_ROOT.parent / "actp-worker" / ".env",
            REPO_ROOT / ".env",
            REPO_ROOT / ".env.production",
            REPO_ROOT / ".env.market-tape",
        ]
    )
    for path in paths:
        if path.is_file():
            load_dotenv(path, override=False)


def _csv(name: str, default: str) -> List[str]:
    return [value.strip() for value in os.getenv(name, default).split(",") if value.strip()]


def _int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, str(default)))
    except ValueError:
        return default


def _float(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, str(default)))
    except ValueError:
        return default


def _bool(name: str, default: bool) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _platform_ints(prefix: str, defaults: Dict[str, int]) -> Dict[str, int]:
    return {
        platform: _int(f"{prefix}_{platform.upper()}", default)
        for platform, default in defaults.items()
    }


@dataclass(frozen=True)
class MarketTapeConfig:
    """All autonomous collection behavior is explicit and environment-overridable."""

    db_path: Path = field(default_factory=lambda: Path(os.getenv(
        "MARKET_TAPE_DB_PATH", str(REPO_ROOT / "data" / "market-tape.sqlite3")
    )).expanduser())
    object_dir: Path = field(default_factory=lambda: Path(os.getenv(
        "MARKET_TAPE_OBJECT_DIR", str(REPO_ROOT / "data" / "market-tape-objects")
    )).expanduser())
    heartbeat_path: Path = field(default_factory=lambda: Path(os.getenv(
        "MARKET_TAPE_HEARTBEAT_PATH", str(REPO_ROOT / "data" / "market-tape-heartbeat.json")
    )).expanduser())
    lock_path: Path = field(default_factory=lambda: Path(os.getenv(
        "MARKET_TAPE_LOCK_PATH", str(REPO_ROOT / "data" / "market-tape.lock")
    )).expanduser())
    local_research_dir: Path = field(default_factory=lambda: Path(os.getenv(
        "MARKET_TAPE_LOCAL_RESEARCH_DIR",
        "~/Library/Application Support/SafariAutomation/market-research-data",
    )).expanduser())
    local_research_state_path: Path = field(default_factory=lambda: Path(os.getenv(
        "MARKET_TAPE_LOCAL_RESEARCH_STATE_PATH",
        str(REPO_ROOT / "data" / "market-tape-local-research-state.json"),
    )).expanduser())
    passport_mount: Path = field(default_factory=lambda: Path(os.getenv(
        "MARKET_TAPE_PASSPORT_MOUNT", "/Volumes/My Passport"
    )).expanduser())
    dataset_root: Path = field(default_factory=lambda: Path(os.getenv(
        "MARKET_TAPE_DATASET_ROOT", "/Volumes/My Passport/MarketTape/datasets"
    )).expanduser())
    youtube_research_dir: Path = field(default_factory=lambda: Path(os.getenv(
        "MARKET_TAPE_YOUTUBE_RESEARCH_DIR",
        "/Volumes/My Passport/MarketTape/trend-frontier",
    )).expanduser())
    dataset_export_enabled: bool = field(default_factory=lambda: _bool(
        "MARKET_TAPE_DATASET_EXPORT_ENABLED", False
    ))
    dataset_require_mounted_volume: bool = field(default_factory=lambda: _bool(
        "MARKET_TAPE_DATASET_REQUIRE_MOUNTED_VOLUME", True
    ))
    dataset_storage_preflight_timeout_seconds: float = field(default_factory=lambda: _float(
        "MARKET_TAPE_DATASET_STORAGE_PREFLIGHT_TIMEOUT_SECONDS", 30.0
    ))
    prediction_min_backtest_labels: int = field(default_factory=lambda: _int(
        "MARKET_TAPE_PREDICTION_MIN_BACKTEST_LABELS", 100
    ))
    prediction_min_positive_labels: int = field(default_factory=lambda: _int(
        "MARKET_TAPE_PREDICTION_MIN_POSITIVE_LABELS", 10
    ))
    prediction_model_dir: Path = field(default_factory=lambda: Path(os.getenv(
        "MARKET_TAPE_PREDICTION_MODEL_DIR",
        str(REPO_ROOT / "data" / "market-tape-models"),
    )).expanduser())
    platforms: List[str] = field(default_factory=lambda: _csv(
        "MARKET_TAPE_PLATFORMS", "youtube,tiktok,instagram,x,facebook,threads"
    ))
    topics: List[str] = field(default_factory=lambda: _csv(
        "MARKET_TAPE_TOPICS",
        (
            "live sports,music releases,celebrity news,movie trailers,television,video games,"
            "breaking news,politics,weather,food,consumer products,finance,technology,science,"
            "health,travel,pets,comedy,relationships,true crime,fashion,cars"
        ),
    ))
    adaptive_topics_enabled: bool = field(default_factory=lambda: _bool(
        "MARKET_TAPE_ADAPTIVE_TOPICS_ENABLED", True
    ))
    adaptive_topic_limit: int = field(default_factory=lambda: _int(
        "MARKET_TAPE_ADAPTIVE_TOPIC_LIMIT", 30
    ))
    adaptive_topic_window_hours: int = field(default_factory=lambda: _int(
        "MARKET_TAPE_ADAPTIVE_TOPIC_WINDOW_HOURS", 168
    ))
    adaptive_topic_min_videos: int = field(default_factory=lambda: _int(
        "MARKET_TAPE_ADAPTIVE_TOPIC_MIN_VIDEOS", 2
    ))
    adaptive_topic_exploration_fraction: float = field(default_factory=lambda: _float(
        "MARKET_TAPE_ADAPTIVE_TOPIC_EXPLORATION_FRACTION", 0.2
    ))
    overflow_platforms: List[str] = field(default_factory=lambda: _csv(
        "MARKET_TAPE_OVERFLOW_PLATFORMS", "youtube"
    ))
    regions: List[str] = field(default_factory=lambda: _csv("MARKET_TAPE_REGIONS", "US"))
    languages: List[str] = field(default_factory=lambda: _csv("MARKET_TAPE_LANGUAGES", "en"))
    youtube_chart_categories: List[str] = field(default_factory=lambda: _csv(
        "MARKET_TAPE_YOUTUBE_CHART_CATEGORIES",
        "all,1,2,10,15,17,19,20,22,23,24,25,26,27,28",
    ))
    cycle_seconds: int = field(default_factory=lambda: _int("MARKET_TAPE_CYCLE_SECONDS", 900))
    discovery_interval_seconds: int = field(default_factory=lambda: _int(
        "MARKET_TAPE_DISCOVERY_INTERVAL_SECONDS", 14400
    ))
    request_timeout_seconds: float = field(default_factory=lambda: _float(
        "MARKET_TAPE_REQUEST_TIMEOUT_SECONDS", 30.0
    ))
    daily_unique_target: int = field(default_factory=lambda: _int(
        "MARKET_TAPE_DAILY_UNIQUE_TARGET", 5000
    ))
    platform_daily_targets: Dict[str, int] = field(default_factory=lambda: _platform_ints(
        "MARKET_TAPE_TARGET",
        {
            "youtube": 2500,
            "tiktok": 1000,
            "instagram": 750,
            "x": 500,
            "facebook": 125,
            "threads": 125,
        },
    ))
    provider_daily_request_limits: Dict[str, int] = field(default_factory=lambda: _platform_ints(
        "MARKET_TAPE_REQUEST_LIMIT",
        {
            "youtube": 180,
            "tiktok": 120,
            "instagram": 100,
            "x": 60,
            "facebook": 60,
            "threads": 60,
        },
    ))
    provider_cost_per_request_usd: Dict[str, float] = field(default_factory=lambda: {
        platform: _float(f"MARKET_TAPE_COST_PER_REQUEST_{platform.upper()}", 0.0)
        for platform in ("youtube", "tiktok", "instagram", "x", "facebook", "threads")
    })
    youtube_search_daily_limit: int = field(default_factory=lambda: _int(
        "MARKET_TAPE_YOUTUBE_SEARCH_DAILY_LIMIT", 80
    ))
    max_due_rechecks_per_cycle: int = field(default_factory=lambda: _int(
        "MARKET_TAPE_MAX_RECHECKS_PER_CYCLE", 1000
    ))
    max_discovery_items_per_source: int = field(default_factory=lambda: _int(
        "MARKET_TAPE_MAX_DISCOVERY_ITEMS_PER_SOURCE", 5000
    ))
    max_daily_provider_cost_usd: float = field(default_factory=lambda: _float(
        "MARKET_TAPE_MAX_DAILY_PROVIDER_COST_USD", 5.0
    ))
    allow_metered_reads: bool = field(default_factory=lambda: _bool(
        "MARKET_TAPE_ALLOW_METERED_READS", False
    ))
    archive_raw_payloads: bool = field(default_factory=lambda: _bool(
        "MARKET_TAPE_ARCHIVE_RAW_PAYLOADS", True
    ))
    youtube_batch_stats: bool = field(default_factory=lambda: _bool(
        "MARKET_TAPE_YOUTUBE_BATCH_STATS", True
    ))
    local_research_trigger_enabled: bool = field(default_factory=lambda: _bool(
        "MARKET_TAPE_LOCAL_RESEARCH_TRIGGER_ENABLED", True
    ))
    local_research_refresh_seconds: int = field(default_factory=lambda: _int(
        "MARKET_TAPE_LOCAL_RESEARCH_REFRESH_SECONDS", 86400
    ))
    local_research_failure_retry_seconds: int = field(default_factory=lambda: _int(
        "MARKET_TAPE_LOCAL_RESEARCH_FAILURE_RETRY_SECONDS", 3600
    ))
    local_research_min_free_bytes: int = field(default_factory=lambda: _int(
        "MARKET_TAPE_LOCAL_RESEARCH_MIN_FREE_BYTES", 5 * 1024 * 1024 * 1024
    ))
    source_failure_backoff_seconds: int = field(default_factory=lambda: _int(
        "MARKET_TAPE_SOURCE_FAILURE_BACKOFF_SECONDS", 3600
    ))
    source_auth_backoff_seconds: int = field(default_factory=lambda: _int(
        "MARKET_TAPE_SOURCE_AUTH_BACKOFF_SECONDS", 86400
    ))
    source_quota_backoff_seconds: int = field(default_factory=lambda: _int(
        "MARKET_TAPE_SOURCE_QUOTA_BACKOFF_SECONDS", 21600
    ))
    source_approval_backoff_seconds: int = field(default_factory=lambda: _int(
        "MARKET_TAPE_SOURCE_APPROVAL_BACKOFF_SECONDS", 900
    ))
    source_max_backoff_seconds: int = field(default_factory=lambda: _int(
        "MARKET_TAPE_SOURCE_MAX_BACKOFF_SECONDS", 86400
    ))
    supabase_sync_enabled: bool = field(default_factory=lambda: _bool(
        "MARKET_TAPE_SUPABASE_SYNC_ENABLED", True
    ))
    supabase_sync_batch_size: int = field(default_factory=lambda: _int(
        "MARKET_TAPE_SUPABASE_SYNC_BATCH_SIZE", 1000
    ))

    @classmethod
    def from_environment(cls) -> "MarketTapeConfig":
        load_runtime_environment()
        return cls()

    def target_for(self, platform: str) -> int:
        return max(0, self.platform_daily_targets.get(platform, 0))

    def request_limit_for(self, platform: str) -> int:
        return max(0, self.provider_daily_request_limits.get(platform, 0))

    def request_cost_for(self, platform: str) -> float:
        return max(0.0, self.provider_cost_per_request_usd.get(platform, 0.0))
