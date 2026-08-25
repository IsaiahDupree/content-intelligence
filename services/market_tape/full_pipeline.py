"""End-to-end orchestration from discovery to a fully vetted Whisper transcript.

Discovery (``MarketTapeCollector.run_cycle``) and transcript acquisition
(``TranscriptBank.run_backfill``) previously only ran independently, on
separate cron schedules, sharing the Market Tape SQLite database as their
sole hand-off point. This module chains both stages behind one call so the
whole pipeline -- discover candidate videos, select the highest-performing
untranscribed ones, download their audio, transcribe it locally with
Whisper, vet the result, and persist it -- can be triggered and inspected
as a single unit, on demand.
"""

from __future__ import annotations

import os
from contextlib import closing
from dataclasses import replace
from pathlib import Path
from typing import Any, Sequence

from .collector import MarketTapeCollector
from .config import MarketTapeConfig
from .store import MarketTapeStore

DEFAULT_TRANSCRIPT_STORAGE_ROOT = Path(
    os.getenv(
        "MARKET_TAPE_TRANSCRIPT_STORAGE_ROOT",
        str(
            Path.home()
            / "Library/Application Support/ContentQuality/data/transcript-bank"
        ),
    )
).expanduser()
TRANSCRIPT_FAILURE_STATES = {"failed", "blocked_runtime", "audit_failed"}


def pipeline_state(discovery_state: str, transcript_status: str) -> str:
    if discovery_state != "completed" or transcript_status in TRANSCRIPT_FAILURE_STATES:
        return "failed"
    if transcript_status == "partial":
        return "partial"
    return "completed"


def matching_trend_ids(
    store: MarketTapeStore,
    topic: str,
    *,
    limit: int = 25,
) -> list[str]:
    """Resolve a requested topic to exact Market Tape trend objects."""

    normalized = str(topic or "").strip().lower()
    if not normalized:
        return []
    with closing(store.connect()) as connection:
        rows = connection.execute(
            """
            SELECT trend_id
            FROM mt_trends
            WHERE LOWER(display_name) LIKE ? OR LOWER(canonical_key) LIKE ?
            ORDER BY last_seen_at DESC
            LIMIT ?
            """,
            (f"%{normalized}%", f"%{normalized}%", max(1, min(100, int(limit)))),
        ).fetchall()
    return [str(row["trend_id"]) for row in rows]


def run_full_pipeline(
    *,
    config: MarketTapeConfig | None = None,
    store: MarketTapeStore | None = None,
    collector: MarketTapeCollector | None = None,
    discovery_mode: str = "full",
    transcript_limit: int = 5,
    transcript_platforms: Sequence[str] = ("youtube", "tiktok", "instagram", "facebook"),
    transcript_model: str = "base",
    topic: str = "",
    transcript_trend_ids: Sequence[str] = (),
    exclude_creator_ids: Sequence[str] = (),
    performance_discovery: bool = False,
    transcript_storage_root: Path | None = None,
    cookies_from_browser: str | None = None,
    bank_factory: Any = None,
) -> dict[str, Any]:
    """Run discovery, then run a Whisper backfill batch, and return one receipt.

    ``bank_factory`` lets a caller substitute a ``TranscriptBank`` subclass or
    a compatible constructor (used by tests to point the transcript stage at
    a temp storage root); it defaults to the real class, which does a real
    ``yt-dlp`` download and a real local ``whisper.transcribe`` call.
    """
    resolved_config = config or MarketTapeConfig.from_environment()
    normalized_topic = str(topic or "").strip()
    requested_platforms = tuple(dict.fromkeys(
        str(value).strip().lower()
        for value in transcript_platforms
        if str(value).strip()
    ))
    if not requested_platforms:
        raise ValueError("transcript_platforms must include at least one platform")

    discovery_config = resolved_config
    if normalized_topic:
        enabled_platforms = set(resolved_config.platforms)
        discovery_config = replace(
            resolved_config,
            topics=[normalized_topic],
            platforms=[
                platform for platform in requested_platforms
                if platform in enabled_platforms
            ],
            adaptive_topics_enabled=False,
        )

    resolved_store = store or MarketTapeStore(discovery_config)
    if collector is None:
        resolved_collector = MarketTapeCollector(discovery_config, resolved_store)
    elif discovery_config == resolved_config:
        resolved_collector = collector
    else:
        # Preserve an injected real-source builder while ensuring a caller-
        # supplied collector cannot retain its broad/adaptive configuration.
        resolved_collector = MarketTapeCollector(
            discovery_config,
            resolved_store,
            source_builder=collector.source_builder,
        )

    if performance_discovery:
        if not normalized_topic:
            raise ValueError("performance_discovery requires topic")
        if "youtube" not in discovery_config.platforms:
            raise ValueError(
                "performance_discovery requires youtube in transcript_platforms"
            )
        discovery = resolved_collector.run_topic_performance_discovery(
            normalized_topic,
            max_items=discovery_config.max_discovery_items_per_source,
        )
    else:
        discovery = resolved_collector.run_cycle(discovery_mode)

    if bank_factory is None:
        from services.content_quality.transcript_bank import TranscriptBank as bank_factory  # noqa: N813

    storage_root = transcript_storage_root or DEFAULT_TRANSCRIPT_STORAGE_ROOT
    bank = bank_factory(discovery_config.db_path, storage_root)
    target_trend_ids = list(dict.fromkeys(
        str(value) for value in transcript_trend_ids if str(value)
    ))
    if not target_trend_ids and normalized_topic:
        target_trend_ids = matching_trend_ids(resolved_store, normalized_topic)
    backfill = bank.run_backfill(
        limit=transcript_limit,
        platforms=requested_platforms,
        model_name=transcript_model,
        topic=normalized_topic,
        trend_ids=target_trend_ids,
        exclude_creator_ids=exclude_creator_ids,
        cookies_from_browser=cookies_from_browser,
    )

    vetted_transcript_ids = [
        item["transcript_id"] for item in backfill["artifacts"] if item["decision"] == "PASS"
    ]
    state = pipeline_state(discovery["state"], backfill["status"])

    return {
        "state": state,
        "discovery": {
            "run_id": discovery["run_id"],
            "mode": discovery["mode"],
            "state": discovery["state"],
            "error_detail": discovery["error_detail"],
            "videos_discovered": sum(
                int(receipt.get("accepted_count", 0) or 0) for receipt in discovery["receipts"]
            ),
            "receipts": discovery["receipts"],
            "scope": {
                "topic": normalized_topic,
                "platforms": list(discovery_config.platforms),
                "adaptive_topics_enabled": discovery_config.adaptive_topics_enabled,
                "lane": (
                    "performance_query"
                    if performance_discovery else "standard_discovery"
                ),
            },
        },
        "transcription": {
            "run_id": backfill["run_id"],
            "status": backfill["status"],
            "candidate_count": backfill["candidate_count"],
            "artifact_count": backfill["artifact_count"],
            "passing_artifact_count": backfill["passing_artifact_count"],
            "failure_count": backfill["failure_count"],
            "failures": backfill["failures"],
            "manifest_path": backfill["manifest_path"],
            "trend_ids": target_trend_ids,
            "excluded_creator_ids": sorted({
                str(value).strip()
                for value in exclude_creator_ids
                if str(value).strip()
            }),
        },
        "fully_vetted_transcript_ids": vetted_transcript_ids,
    }


__all__ = ["matching_trend_ids", "pipeline_state", "run_full_pipeline"]
