"""Real provider and SQLite coverage for TikTok transcript URL admission."""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone

import pytest

from services.content_quality.transcript_bank import TranscriptBank
from services.market_tape.config import MarketTapeConfig
from services.market_tape.models import MarketContent, MetricCounters
from services.market_tape.sources.local_research import LocalResearchSource
from services.market_tape.sources.social import TikTokResearchSource
from services.market_tape.store import MarketTapeStore


def _config(tmp_path) -> MarketTapeConfig:
    return MarketTapeConfig(
        db_path=tmp_path / "market.sqlite3",
        object_dir=tmp_path / "objects",
        heartbeat_path=tmp_path / "heartbeat.json",
        lock_path=tmp_path / "market.lock",
        local_research_dir=tmp_path / "research",
        platforms=["tiktok"],
        topics=["retention attention"],
        supabase_sync_enabled=False,
        local_research_trigger_enabled=False,
    )


def test_research_provider_mapping_is_never_stringified_into_tiktok_url(tmp_path):
    source = TikTokResearchSource(_config(tmp_path), "provider-shape-run", 1)
    observed = datetime.now(timezone.utc)
    try:
        malformed = source._normalize(
            {
                "id": "7770000000000000001",
                "username": {"12": {"views": 900_000}},
                "video_description": "Retention attention analysis",
                "view_count": 900_000,
            },
            observed,
            {},
        )
        nested_identity = source._normalize(
            {
                "id": "7770000000000000002",
                "username": {"unique_id": "valid.creator"},
                "video_description": "Retention attention analysis",
                "view_count": 900_000,
            },
            observed,
            {},
        )
    finally:
        source.close()

    assert malformed.creator_handle == ""
    assert malformed.creator_external_id == "unknown"
    assert malformed.url == ""
    assert "{" not in malformed.url and "%7B" not in malformed.url
    assert nested_identity.creator_handle == "valid.creator"
    assert nested_identity.url == (
        "https://www.tiktok.com/@valid.creator/video/7770000000000000002"
    )


def test_local_archive_uses_canonical_url_handle_instead_of_author_mapping(tmp_path):
    config = _config(tmp_path)
    source = LocalResearchSource(
        config,
        "archive-shape-run",
        1,
        platform="tiktok",
        archive_root=tmp_path / "research",
    )
    observed = datetime.now(timezone.utc)
    source._reset_archive_qc()
    try:
        item = source._normalize(
            {
                "id": "7770000000000000003",
                "url": "https://www.tiktok.com/@archive.creator/video/7770000000000000003",
                "author": {"12": {"views": 800_000}},
                "description": "Retention attention breakdown",
                "views": 800_000,
            },
            {},
            tmp_path / "archive.json",
            observed,
        )
    finally:
        source.close()

    assert item is not None
    assert item.creator_handle == "archive.creator"
    assert item.creator_external_id == "archive.creator"
    assert item.url == (
        "https://www.tiktok.com/@archive.creator/video/7770000000000000003"
    )


def test_transcript_backfill_skips_legacy_object_handle_url_before_ytdlp(tmp_path):
    config = _config(tmp_path)
    store = MarketTapeStore(config)
    store.start_run("legacy-url-run", "integration")
    observed = datetime.now(timezone.utc)
    malformed_url = (
        "https://www.tiktok.com/@%7B%2712%27%3A%20%27sports%27%7D/video/"
        "7770000000000000004"
    )
    for external_id, views, url in (
        ("7770000000000000004", 900_000, malformed_url),
        (
            "7770000000000000005",
            500_000,
            "https://www.tiktok.com/@tested.creator/video/7770000000000000005",
        ),
    ):
        store.ingest(
            MarketContent(
                platform="tiktok",
                external_id=external_id,
                creator_external_id=f"creator-{external_id}",
                creator_handle="tested.creator",
                published_at=observed - timedelta(days=1),
                observed_at=observed,
                source_id="tiktok-url-guard-integration",
                metrics=MetricCounters(
                    views=views,
                    likes=30_000,
                    comments=2_000,
                    shares=1_000,
                ),
                title="Retention attention systems",
                caption="A measured retention attention breakdown",
                url=url,
                duration_seconds=45,
                raw_payload={"external_id": external_id, "url": url},
            ),
            "legacy-url-run",
        )
    store.finish_run("legacy-url-run")

    bank = TranscriptBank(config.db_path, tmp_path / "transcript-bank")
    candidates = bank.select_backfill_candidates(limit=5, platforms=["tiktok"])

    assert [candidate.external_id for candidate in candidates] == [
        "7770000000000000005"
    ]
    invalid_candidate = replace(candidates[0], source_url=malformed_url)
    with pytest.raises(RuntimeError, match="refusing unusable source URL"):
        bank.transcribe(invalid_candidate, model=None, model_name="base")
    assert not (
        tmp_path
        / "transcript-bank"
        / "videos"
        / "tiktok"
        / invalid_candidate.external_id
    ).exists()
