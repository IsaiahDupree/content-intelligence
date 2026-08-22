"""End-to-end pipeline orchestration: discovery chained into transcript
acquisition, exercised with real Store/Collector/TranscriptBank objects
against real temp SQLite -- no mocked internals. The download leg genuinely
invokes the real yt-dlp binary; against an unreachable localhost URL it
fails for real, and this proves the pipeline records that failure honestly
rather than swallowing or faking a transcript."""

from __future__ import annotations

from dataclasses import replace
from datetime import timezone
from pathlib import Path

import pytest
from flask import Flask

from services.market_tape.api import register_market_tape_routes
from services.market_tape.collector import MarketTapeCollector
from services.market_tape.config import MarketTapeConfig
from services.market_tape.full_pipeline import pipeline_state, run_full_pipeline
from services.market_tape.models import MarketContent, MetricCounters, utc_now
from services.market_tape.store import MarketTapeStore


@pytest.fixture
def market_config(tmp_path):
    return MarketTapeConfig(
        db_path=tmp_path / "market.sqlite3",
        object_dir=tmp_path / "objects",
        heartbeat_path=tmp_path / "heartbeat.json",
        lock_path=tmp_path / "market.lock",
        local_research_state_path=tmp_path / "local-research-state.json",
        prediction_model_dir=tmp_path / "models",
        local_research_min_free_bytes=0,
        platforms=[],
        topics=["ai automation"],
        adaptive_topics_enabled=False,
        regions=["US"],
        youtube_chart_categories=["all"],
        daily_unique_target=5000,
        platform_daily_targets={"youtube": 10},
        provider_daily_request_limits={"youtube": 20},
        provider_cost_per_request_usd={"youtube": 0.001},
        max_daily_provider_cost_usd=1.0,
        supabase_sync_enabled=False,
    )


def seed_real_candidate(
    store: MarketTapeStore,
    run_id: str,
    *,
    unreachable_url: str,
    external_id: str = "unreachable000",
    views: int = 50_000,
    title: str = "Automating invoicing workflows for busy startup founders",
) -> str:
    """Insert one real, policy-qualifying video via the real ingest() path --
    same code every live discovery cycle uses -- so the transcript stage has
    a genuine untranscribed candidate to select."""
    item = MarketContent(
        platform="youtube",
        external_id=external_id,
        creator_external_id=f"creator-{external_id}",
        published_at=None,
        observed_at=utc_now(),
        source_id="test-seed",
        metrics=MetricCounters(views=views, likes=max(1_000, views // 20), comments=200, shares=50, saves=10),
        title=title,
        caption="",
        description="A walkthrough of automating invoicing workflows for founders.",
        url=unreachable_url,
        duration_seconds=120.0,
        media_type="video",
    )
    store.ingest(item, run_id)
    return item.video_id


class TestFullPipeline:
    def test_non_provider_runtime_and_audit_failures_fail_closed(self):
        assert pipeline_state("completed", "blocked_runtime") == "failed"
        assert pipeline_state("completed", "audit_failed") == "failed"
        assert pipeline_state("completed", "partial") == "partial"
        assert pipeline_state("completed", "completed") == "completed"

    def test_empty_discovery_and_empty_backfill_completes_cleanly(self, market_config, tmp_path):
        """Zero registered sources, zero matching backfill candidates: both
        real stages run and complete without any external call."""
        store = MarketTapeStore(market_config)
        collector = MarketTapeCollector(market_config, store, source_builder=lambda *_: [])

        result = run_full_pipeline(
            config=market_config,
            store=store,
            collector=collector,
            transcript_platforms=("youtube",),
            transcript_storage_root=tmp_path / "transcript-bank",
            topic="a topic phrase nothing in the tape will ever match",
        )

        assert result["state"] == "completed"
        assert result["discovery"]["videos_discovered"] == 0
        assert result["transcription"]["candidate_count"] == 0
        assert result["transcription"]["artifact_count"] == 0
        assert result["fully_vetted_transcript_ids"] == []

    def test_real_candidate_triggers_real_download_and_records_honest_failure(
        self, market_config, tmp_path
    ):
        """A real, policy-qualifying candidate exists in the tape (no bound
        transcript). The pipeline must select it and genuinely invoke yt-dlp;
        against an address nothing listens on, that download really fails,
        and the failure must show up in the receipt -- never a fabricated
        transcript, never a silently dropped candidate."""
        store = MarketTapeStore(market_config)
        target_video_id = seed_real_candidate(
            store,
            "seed-run",
            unreachable_url="http://127.0.0.1:1/target-does-not-exist.mp4",
            external_id="target000",
            views=50_000,
        )
        seed_real_candidate(
            store,
            "seed-run",
            unreachable_url="http://127.0.0.1:1/higher-does-not-exist.mp4",
            external_id="higher000",
            views=500_000,
            title="A higher-ranked unrelated creator story",
        )
        now = utc_now().isoformat()
        with store.connect() as connection:
            connection.execute(
                """
                INSERT INTO mt_trends(
                    trend_id, trend_type, canonical_key, display_name,
                    status, first_seen_at, last_seen_at
                ) VALUES(?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    "trend:test:explicit-target",
                    "topic",
                    "explicit target",
                    "Explicit Target",
                    "active",
                    now,
                    now,
                ),
            )
            connection.execute(
                """
                INSERT INTO mt_trend_memberships(
                    trend_id, video_id, confidence, evidence_json, first_seen_at
                ) VALUES(?, ?, ?, ?, ?)
                """,
                ("trend:test:explicit-target", target_video_id, 1.0, "{}", now),
            )
            connection.commit()
        collector = MarketTapeCollector(market_config, store, source_builder=lambda *_: [])

        result = run_full_pipeline(
            config=market_config,
            store=store,
            collector=collector,
            transcript_platforms=("youtube",),
            transcript_storage_root=tmp_path / "transcript-bank",
            transcript_limit=5,
            topic="explicit target",
        )

        assert result["transcription"]["candidate_count"] == 1
        assert result["transcription"]["artifact_count"] == 0
        assert result["transcription"]["failure_count"] == 1
        failure = result["transcription"]["failures"][0]
        assert failure["video_id"] == target_video_id
        assert failure["error"]
        assert result["transcription"]["trend_ids"] == ["trend:test:explicit-target"]
        assert result["fully_vetted_transcript_ids"] == []
        assert result["state"] == "failed"

    def test_api_route_chains_both_stages_and_is_auth_gated(self, market_config, monkeypatch):
        """The Flask route wires the real orchestrator, honors the loopback/
        token auth gate every other market-tape write route uses, and does
        not accept an invalid discovery_mode."""
        config = replace(market_config, platforms=[])
        app = Flask(__name__)
        register_market_tape_routes(app, config)
        client = app.test_client()

        bad_mode = client.post(
            "/api/market-tape/full-pipeline", json={"discovery_mode": "nonsense"}
        )
        assert bad_mode.status_code == 400

        bad_trends = client.post(
            "/api/market-tape/full-pipeline", json={"trend_ids": "not-an-array"}
        )
        assert bad_trends.status_code == 400

        ok = client.post(
            "/api/market-tape/full-pipeline",
            json={"discovery_mode": "discovery", "platforms": ["youtube"], "limit": 1,
                  "topic": "a topic phrase nothing in the tape will ever match",
                  "trend_ids": ["trend:does-not-exist"]},
        )
        assert ok.status_code == 200
        body = ok.get_json()
        assert "discovery" in body and "transcription" in body
        assert body["transcription"]["trend_ids"] == ["trend:does-not-exist"]
        assert body["state"] == "completed"

        monkeypatch.setenv("MARKET_TAPE_CONTROL_TOKEN", "secret-token")
        try:
            unauthorized = client.post("/api/market-tape/full-pipeline", json={})
            assert unauthorized.status_code == 401
        finally:
            monkeypatch.delenv("MARKET_TAPE_CONTROL_TOKEN", raising=False)
