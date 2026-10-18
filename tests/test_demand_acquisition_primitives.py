"""Reusable script-language demand acquisition primitives.

These tests use the real Market Tape/Transcript Bank implementations, real
temporary SQLite files, and a loopback HTTP provider.  No provider or database
client is mocked.
"""

from __future__ import annotations

import json
import threading
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

import pytest

from services.content_quality.transcript_bank import TranscriptBank
from services.market_tape.collector import MarketTapeCollector
from services.market_tape.config import MarketTapeConfig
from services.market_tape.full_pipeline import run_full_pipeline
from services.market_tape.models import MarketContent, MetricCounters
from services.market_tape.sources.youtube import YouTubeSource
from services.market_tape.store import MarketTapeStore


class DemandProviderHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    requests: list[dict] = []

    def do_GET(self):  # noqa: N802 - stdlib handler contract
        parsed = urlparse(self.path)
        query = parse_qs(parsed.query)
        self.__class__.requests.append({"path": parsed.path, "query": query})
        if parsed.path == "/search":
            requested = query.get("q", [""])[0]
            external_id = (
                "perfvideo01"
                if query.get("order") == ["viewCount"]
                else "scopedvid01"
            )
            self._json({
                "items": [{
                    "id": {"videoId": external_id},
                    "snippet": {"title": requested},
                }],
            })
            return
        if parsed.path == "/videos":
            if query.get("chart") == ["mostPopular"]:
                self._json({"items": []})
                return
            external_ids = query.get("id", [""])[0].split(",")
            self._json({
                "items": [
                    self._video(external_id)
                    for external_id in external_ids
                    if external_id
                ],
            })
            return
        self._json({"error": "not found"}, status=404)

    def log_message(self, *_):
        return

    @staticmethod
    def _video(external_id: str) -> dict:
        performance_lane = external_id == "perfvideo01"
        return {
            "id": external_id,
            "snippet": {
                "title": "Retention language demand breakdown",
                "description": "Retention language demand from observed creator speech.",
                "publishedAt": (
                    datetime.now(timezone.utc) - timedelta(hours=2)
                ).replace(microsecond=0).isoformat().replace("+00:00", "Z"),
                "channelId": f"channel-{external_id}",
                "channelTitle": "Evidence Creator",
            },
            "statistics": {
                "viewCount": "250000" if performance_lane else "100",
                "likeCount": "15000" if performance_lane else "1",
                "commentCount": "600" if performance_lane else "0",
            },
            "contentDetails": {"duration": "PT42S"},
        }

    def _json(self, body: dict, status: int = 200) -> None:
        encoded = json.dumps(body).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)


@pytest.fixture
def demand_provider_server():
    DemandProviderHandler.requests = []
    server = ThreadingHTTPServer(("127.0.0.1", 0), DemandProviderHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def demand_config(tmp_path, **changes) -> MarketTapeConfig:
    base = MarketTapeConfig(
        db_path=tmp_path / "market.sqlite3",
        object_dir=tmp_path / "objects",
        heartbeat_path=tmp_path / "heartbeat.json",
        lock_path=tmp_path / "market.lock",
        local_research_dir=tmp_path / "local-research",
        local_research_state_path=tmp_path / "local-research-state.json",
        prediction_model_dir=tmp_path / "models",
        local_research_min_free_bytes=0,
        platforms=["youtube", "tiktok"],
        topics=["broad finance", "broad celebrity news"],
        adaptive_topics_enabled=True,
        regions=["US"],
        youtube_chart_categories=["all"],
        youtube_search_daily_limit=10,
        daily_unique_target=100,
        platform_daily_targets={"youtube": 100, "tiktok": 100},
        provider_daily_request_limits={"youtube": 20, "tiktok": 20},
        provider_cost_per_request_usd={"youtube": 0.0, "tiktok": 0.0},
        max_daily_provider_cost_usd=1.0,
        supabase_sync_enabled=False,
    )
    return replace(base, **changes) if changes else base


def test_performance_discovery_receipt_counts_items_as_discovered(
    tmp_path,
    demand_provider_server,
    monkeypatch,
):
    monkeypatch.setenv("YOUTUBE_API_KEY", "loopback-demand-key")
    config = demand_config(
        tmp_path,
        platforms=["youtube"],
        topics=["retention language demand"],
        adaptive_topics_enabled=False,
    )
    source = YouTubeSource(
        config,
        "performance-demand-run",
        request_budget=4,
        base_url=demand_provider_server,
    )
    try:
        batch = source.discover_performance(
            "retention language demand",
            max_items=5,
        )
    finally:
        source.close()

    assert len(batch.items) == 1
    assert batch.receipt.metadata["operation"] == "discover_performance"
    assert batch.receipt.discovered_count == 1
    assert batch.receipt.refreshed_count == 0
    assert len(batch.query_attempts) == 1
    assert batch.query_attempts[0].query == "retention language demand"


def test_backfill_selection_is_creator_diverse_and_honors_exclusions(tmp_path):
    config = demand_config(
        tmp_path,
        platforms=["youtube"],
        topics=["script language"],
        adaptive_topics_enabled=False,
    )
    store = MarketTapeStore(config)
    store.start_run("creator-diversity-seed", "integration")
    observed_at = datetime.now(timezone.utc)
    creator_ids: dict[str, str] = {}
    rows = (
        ("creator-a-top", "creator-a", 500_000, "Script language pattern one"),
        ("creator-a-second", "creator-a", 490_000, "Script language pattern two"),
        ("creator-b-top", "creator-b", 400_000, "Script language pattern three"),
        ("creator-c-top", "creator-c", 300_000, "Script language pattern four"),
        ("unrelated-top", "creator-d", 900_000, "Restaurant cooking demonstration"),
        ("low-engagement", "creator-e", 800_000, "Script language without response"),
    )
    for external_id, creator_external_id, views, title in rows:
        low_engagement = external_id == "low-engagement"
        item = MarketContent(
            platform="youtube",
            external_id=external_id,
            creator_external_id=creator_external_id,
            published_at=observed_at - timedelta(days=1),
            observed_at=observed_at,
            source_id="creator-diversity-integration",
            metrics=MetricCounters(
                views=views,
                likes=1 if low_engagement else max(10_000, views // 20),
                comments=0 if low_engagement else 500,
            ),
            title=title,
            description="A measured script language analysis." if "Script" in title else "Food recipe.",
            url=f"https://www.youtube.com/watch?v={external_id}",
            duration_seconds=45,
            raw_payload={"external_id": external_id},
        )
        creator_ids[creator_external_id] = item.creator_id
        store.ingest(item, "creator-diversity-seed")
    store.finish_run("creator-diversity-seed")

    bank = TranscriptBank(config.db_path, tmp_path / "transcript-bank")
    selected = bank.select_backfill_candidates(
        limit=3,
        platforms=["youtube"],
        topic="script language",
    )
    assert [candidate.external_id for candidate in selected] == [
        "creator-a-top",
        "creator-b-top",
        "creator-c-top",
    ]
    assert len({candidate.creator_id for candidate in selected}) == 3

    without_creator_a = bank.select_backfill_candidates(
        limit=5,
        platforms=["youtube"],
        topic="script language",
        exclude_creator_ids=[creator_ids["creator-a"]],
    )
    assert [candidate.external_id for candidate in without_creator_a] == [
        "creator-b-top",
        "creator-c-top",
    ]


def test_explicit_pipeline_topic_scopes_real_discovery_and_persists_query_attempt(
    tmp_path,
    demand_provider_server,
    monkeypatch,
):
    monkeypatch.setenv("YOUTUBE_API_KEY", "loopback-demand-key")
    config = demand_config(tmp_path)
    store = MarketTapeStore(config)

    def real_source_builder(runtime_config, run_id, budget_for):
        return [YouTubeSource(
            runtime_config,
            run_id,
            budget_for(
                YouTubeSource.source_id,
                runtime_config.request_limit_for("youtube"),
            ),
            base_url=demand_provider_server,
        )]

    broad_collector = MarketTapeCollector(
        config,
        store,
        source_builder=real_source_builder,
    )
    result = run_full_pipeline(
        config=config,
        store=store,
        collector=broad_collector,
        discovery_mode="discovery",
        transcript_limit=1,
        transcript_platforms=("youtube",),
        topic="retention language demand",
        transcript_storage_root=tmp_path / "transcript-bank",
    )

    search_queries = [
        request["query"].get("q", [""])[0]
        for request in DemandProviderHandler.requests
        if request["path"] == "/search"
    ]
    assert search_queries == ["retention language demand"]
    assert result["discovery"]["scope"] == {
        "topic": "retention language demand",
        "platforms": ["youtube"],
        "adaptive_topics_enabled": False,
        "lane": "standard_discovery",
    }
    attempts = store.list_query_attempts(limit=10, platform="youtube")
    assert len(attempts) == 1
    assert attempts[0]["query"] == "retention language demand"
    assert attempts[0]["run_id"] == result["discovery"]["run_id"]
    assert attempts[0]["state"] == "completed"
    assert result["transcription"]["candidate_count"] == 0


def test_demand_pipeline_uses_query_only_performance_lane_not_charts(
    tmp_path,
    demand_provider_server,
    monkeypatch,
):
    monkeypatch.setenv("YOUTUBE_API_KEY", "loopback-demand-key")
    config = demand_config(tmp_path)
    store = MarketTapeStore(config)

    def real_source_builder(runtime_config, run_id, budget_for):
        return [YouTubeSource(
            runtime_config,
            run_id,
            budget_for(
                YouTubeSource.source_id,
                runtime_config.request_limit_for("youtube"),
            ),
            base_url=demand_provider_server,
        )]

    collector = MarketTapeCollector(
        config, store, source_builder=real_source_builder
    )
    result = run_full_pipeline(
        config=config,
        store=store,
        collector=collector,
        discovery_mode="discovery",
        transcript_limit=0,
        transcript_platforms=("youtube",),
        topic="retention language demand",
        performance_discovery=True,
        transcript_storage_root=tmp_path / "transcript-bank",
    )

    assert result["state"] == "completed"
    assert result["discovery"]["videos_discovered"] == 1
    assert result["discovery"]["scope"]["lane"] == "performance_query"
    search_requests = [
        request for request in DemandProviderHandler.requests
        if request["path"] == "/search"
    ]
    assert len(search_requests) == 1
    assert search_requests[0]["query"]["q"] == ["retention language demand"]
    assert search_requests[0]["query"]["order"] == ["viewCount"]
    assert not any(
        request["query"].get("chart") == ["mostPopular"]
        for request in DemandProviderHandler.requests
    )
    attempts = store.list_query_attempts(limit=10, platform="youtube")
    assert len(attempts) == 1
    assert attempts[0]["metadata"]["lane"] == "performance_search"
