"""Market Tape V1 integration tests use a real local HTTP server and SQLite."""

from __future__ import annotations

import json
import hashlib
import os
import sqlite3
import threading
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from tempfile import TemporaryDirectory
from urllib.parse import parse_qs, urlparse

import pytest
from flask import Flask

from services.market_tape.api import register_market_tape_routes
from services.market_tape.collector import MarketTapeCollector
from services.market_tape.config import MarketTapeConfig
from services.market_tape.math import age_bucket, concentration, counter_motion, log_velocity, poll_interval_seconds
from services.market_tape.migration import (
    MARKET_TAPE_TABLES,
    SupabaseMigrationManager,
    project_ref_from_url,
    validate_migration,
)
from services.market_tape.models import (
    MarketContent,
    MetricCounters,
    ProviderBatch,
    SourceReceipt,
    SourceState,
    stable_hash,
)
from services.market_tape.sources.base import sanitize
from services.market_tape.sources.local_research import LocalResearchSource
from services.market_tape.sources.youtube import YouTubeSource
from services.market_tape.sinks.supabase import ENTITY_SYNC_ORDER, ENTITY_TABLES, SupabaseSink
from services.market_tape.store import MarketTapeStore


class ProviderTestHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    received_posts = []
    received_gets = []
    remote_runs = set()
    research_jobs = {}

    def do_GET(self):  # noqa: N802 - HTTP handler contract
        parsed = urlparse(self.path)
        query = parse_qs(parsed.query)
        self.__class__.received_gets.append({"path": parsed.path, "query": query})
        if parsed.path == "/health":
            self._json({"status": "ok"})
            return
        if parsed.path == "/api/research/status":
            self._json({"currentJob": None, "recentJobs": []})
            return
        if parsed.path.startswith("/api/research/status/"):
            job_id = parsed.path.rsplit("/", 1)[-1]
            job = self.__class__.research_jobs.get(job_id)
            self._json(job or {"error": "not found"}, status=200 if job else 404)
            return
        if parsed.path == "/search":
            if query.get("q") == ["force-http-error"]:
                self._json({"error": "test provider unavailable"}, status=404)
                return
            if query.get("q") == ["paginated"]:
                page = int(query.get("pageToken", ["0"])[0])
                body = {
                    "items": [{"id": {"videoId": f"video-page-{page}"}, "snippet": {}}],
                }
                if page < 3:
                    body["nextPageToken"] = str(page + 1)
                self._json(body)
                return
            if query.get("q") == ["known-prefix"]:
                page = int(query.get("pageToken", ["0"])[0])
                body = {
                    "items": [{"id": {"videoId": f"known-prefix-{page}"}, "snippet": {}}],
                }
                if page < 3:
                    body["nextPageToken"] = str(page + 1)
                self._json(body)
                return
            if query.get("q") == ["quota-partial"]:
                page = int(query.get("pageToken", ["0"])[0])
                if page > 0:
                    self._json({"error": "search quota reached"}, status=429)
                    return
                self._json({
                    "items": [{"id": {"videoId": "quota-partial-0"}, "snippet": {}}],
                    "nextPageToken": "1",
                })
                return
            self._json({
                "items": [{"id": {"videoId": "video-search"}, "snippet": {"title": "AI agents in practice"}}],
            })
            return
        if parsed.path in {"/videos", "/videos:batchGetStats"}:
            if query.get("chart") == ["mostPopular"] and query.get("videoCategoryId") == ["404"]:
                self._json({"error": "category is unavailable in this region"}, status=404)
                return
            ids = query.get("id", ["video-chart"])[0].split(",")
            self._json({"items": [self._video(video_id) for video_id in ids if video_id]})
            return
        if parsed.path.removeprefix("/") in MARKET_TAPE_TABLES:
            self._json([])
            return
        self._json({"error": "not found"}, status=404)

    def do_POST(self):  # noqa: N802 - HTTP handler contract
        length = int(self.headers.get("Content-Length", "0"))
        body = json.loads(self.rfile.read(length) or b"[]")
        self.__class__.received_posts.append({"path": urlparse(self.path).path, "body": body})
        if isinstance(body, dict) and body.get("read_only") is True:
            if "row_count" in body.get("query", ""):
                self._json([
                    {"table_name": table, "row_count": index}
                    for index, table in enumerate(MARKET_TAPE_TABLES, start=1)
                ])
                return
            rows = []
            for table in MARKET_TAPE_TABLES:
                trigger_names = []
                if table == "actp_market_observations":
                    trigger_names.append("actp_market_observations_no_update")
                if table == "actp_market_discovery_attributions":
                    trigger_names.append("actp_market_discovery_attributions_no_update")
                if table == "actp_market_query_attempts":
                    trigger_names.append("actp_market_query_attempts_no_update")
                if table == "actp_trend_observations":
                    trigger_names.append("actp_trend_observations_no_update")
                rows.append({
                    "table_name": table,
                    "relation_exists": True,
                    "rls_enabled": True,
                    "policy_count": 0,
                    "trigger_names": trigger_names,
                })
            self._json(rows)
            return
        path = urlparse(self.path).path
        records = body if isinstance(body, list) else [body]
        if path == "/actp_market_collection_runs":
            self.__class__.remote_runs.update(
                record["run_id"] for record in records if isinstance(record, dict) and record.get("run_id")
            )
        if path == "/actp_market_source_receipts":
            missing = [
                record.get("run_id")
                for record in records
                if isinstance(record, dict) and record.get("run_id") not in self.__class__.remote_runs
            ]
            if missing:
                self._json({"code": "23503", "message": "missing parent run"}, status=409)
                return
        self._json({}, status=201)

    def log_message(self, *_):
        return

    def _video(self, video_id):
        index = 200 if video_id == "video-search" else 100
        return {
            "id": video_id,
            "snippet": {
                "title": "How AI automation changes marketing",
                "description": "Proof, systems, and results #aiautomation",
                "publishedAt": "2026-08-18T12:00:00Z",
                "channelId": "channel-1",
                "channelTitle": "Systems Lab",
                "defaultLanguage": "en",
                "tags": ["AI automation", "marketing"],
                "thumbnails": {"high": {"url": "https://example.test/thumb.jpg"}},
            },
            "statistics": {"viewCount": str(index), "likeCount": "20", "commentCount": "3"},
            "contentDetails": {"duration": "PT45S", "durationMillis": "45000"},
        }

    def _json(self, body, status=200):
        encoded = json.dumps(body).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)


@pytest.fixture
def provider_server():
    ProviderTestHandler.received_posts = []
    ProviderTestHandler.received_gets = []
    ProviderTestHandler.remote_runs = set()
    ProviderTestHandler.research_jobs = {}
    server = ThreadingHTTPServer(("127.0.0.1", 0), ProviderTestHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


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
        platforms=["youtube"],
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


def test_append_only_observations_motion_and_raw_archive(market_config):
    store = MarketTapeStore(market_config)
    store.start_run("run-1", "full")
    observed = datetime.now(timezone.utc) - timedelta(minutes=10)
    first = _content(observed, views=100)
    second = _content(observed + timedelta(minutes=5), views=250)

    assert store.ingest(first, "run-1") == (True, True)
    assert store.ingest(first, "run-1") == (False, False)
    assert store.ingest(second, "run-1") == (True, False)
    video = store.list_videos(1)[0]
    assert video["views"] == 250
    assert video["view_velocity"] > 0
    assert len(list(market_config.object_dir.rglob("*.json.gz"))) == 2
    assert store.aggregate_trends(run_id="run-1") > 0
    assert store.create_predictions("run-1") > 0
    predictions = store.list_predictions(100)
    assert {row["subject_type"] for row in predictions} == {"video", "trend"}
    assert all(0 <= row["probability"] <= 1 for row in predictions)
    candles = store.social_candles(window_minutes=60, limit=24)
    assert sum(row["new_views"] for row in candles) == 150

    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        with store.connect() as connection:
            connection.execute("UPDATE mt_market_observations SET views = 0")


def test_trend_activity_excludes_lifetime_views_from_old_posts_discovered_now(market_config):
    store = MarketTapeStore(market_config)
    observed = datetime.now(timezone.utc)
    store.start_run("crawler-expansion-run", "discovery")
    for index, views in enumerate((5_000_000, 3_000_000), start=1):
        store.ingest(
            _content_for_keyword(
                f"old-discovery-{index}",
                "Frank Beard memorial coverage",
                observed,
                views,
            ),
            "crawler-expansion-run",
        )
    store.aggregate_trends(run_id="crawler-expansion-run", observed_at=observed)

    with store.connect() as connection:
        first = connection.execute(
            """SELECT observation.* FROM mt_trend_observations observation
               JOIN mt_trends trend USING(trend_id)
               WHERE trend.canonical_key = 'frank beard memorial'
               ORDER BY observation.trend_observation_id DESC LIMIT 1"""
        ).fetchone()
    assert first["views_total"] == 8_000_000
    assert first["views_new_1h"] == 0
    assert first["counter_delta_videos"] == 0
    assert first["activity_coverage"] == 0
    assert first["momentum"] == 0
    assert first["state"] != "breakout"

    store.start_run("counter-growth-run", "recheck")
    later = observed + timedelta(minutes=10)
    for index, views in enumerate((5_010_000, 3_005_000), start=1):
        store.ingest(
            _content_for_keyword(
                f"old-discovery-{index}",
                "Frank Beard memorial coverage",
                later,
                views,
            ),
            "counter-growth-run",
        )
    store.aggregate_trends(run_id="counter-growth-run", observed_at=later)
    with store.connect() as connection:
        second = connection.execute(
            """SELECT observation.* FROM mt_trend_observations observation
               JOIN mt_trends trend USING(trend_id)
               WHERE trend.canonical_key = 'frank beard memorial'
               ORDER BY observation.trend_observation_id DESC LIMIT 1"""
        ).fetchone()
    assert second["views_new_1h"] == 15_000
    assert second["counter_delta_videos"] == 2
    assert second["activity_coverage"] == 1
    assert second["momentum"] > 0


def test_store_context_releases_sqlite_connection(market_config):
    store = MarketTapeStore(market_config)
    connection = store.connect()
    with connection:
        assert connection.execute("SELECT 1").fetchone()[0] == 1

    with pytest.raises(sqlite3.ProgrammingError, match="closed database"):
        connection.execute("SELECT 1")


def test_derivative_and_adaptive_polling_math():
    assert log_velocity(100, 200, 3600) > 0
    motion = counter_motion([
        {"observed_at": "2026-08-18T12:00:00+00:00", "views": 100},
        {"observed_at": "2026-08-18T12:05:00+00:00", "views": 200},
        {"observed_at": "2026-08-18T12:10:00+00:00", "views": 500},
    ])
    assert motion.velocity > 0
    assert motion.acceleration > 0
    assert poll_interval_seconds(1800) == 300
    assert poll_interval_seconds(1800, hot_mode=True) == 100
    assert poll_interval_seconds(10 * 86400) == 86400
    assert age_bucket(datetime.now(timezone.utc) - timedelta(minutes=10)) == "t+5-15m"
    assert concentration([90, 5, 5], 1) == pytest.approx(0.9)


def test_youtube_http_adapter_and_collector_receipts(provider_server, market_config, monkeypatch):
    monkeypatch.setenv("YOUTUBE_API_KEY", "integration-test-key")

    def source_builder(config, run_id, budget_for):
        return [YouTubeSource(
            config,
            run_id,
            budget_for(YouTubeSource.source_id, config.request_limit_for("youtube")),
            base_url=provider_server,
        )]

    store = MarketTapeStore(market_config)
    result = MarketTapeCollector(market_config, store, source_builder=source_builder).run_cycle("full")
    assert result["state"] == "completed"
    assert result["status"]["totals"]["videos"] == 2
    assert result["status"]["totals"]["observations"] == 2
    assert result["status"]["totals"]["trends"] > 0
    assert result["predictions_added"] > 0
    assert result["status"]["totals"]["predictions"] == result["predictions_added"]
    receipt = result["receipts"][0]
    assert receipt["accepted_count"] == 2
    assert receipt["request_count"] == 3
    assert receipt["estimated_cost_usd"] == pytest.approx(0.003)
    assert market_config.heartbeat_path.exists()
    assert store.remaining_request_budget(YouTubeSource.source_id, 20) == 17
    assert result["central_sync"]["state"] == "disabled"
    assert result["status"]["central_sync"]["pending"] > 0
    assert result["status"]["daemon"]["state"] in {"starting", "healthy"}


def test_source_receipts_count_only_requests_from_each_operation(
    provider_server,
    market_config,
    monkeypatch,
):
    monkeypatch.setenv("YOUTUBE_API_KEY", "integration-test-key")
    source = YouTubeSource(
        market_config,
        "sequential-source-run",
        20,
        base_url=provider_server,
    )
    try:
        discovery = source.discover(2)
        refresh = source.refresh([{"external_id": "video-search"}])
        total_requests = source.request_count
    finally:
        source.close()

    assert discovery.receipt.request_count > 0
    assert refresh.receipt.request_count == 1
    assert discovery.receipt.request_count + refresh.receipt.request_count == (
        total_requests
    )


def test_youtube_broad_charts_span_categories_and_regions_without_search(
    provider_server, market_config, monkeypatch
):
    monkeypatch.setenv("YOUTUBE_API_KEY", "integration-test-key")
    config = replace(
        market_config,
        regions=["US", "GB"],
        youtube_chart_categories=["all", "10", "20", "404"],
        youtube_search_daily_limit=0,
    )

    source = YouTubeSource(config, "chart-run", 20, base_url=provider_server)
    source.known_external_ids = lambda _: set()
    try:
        batch = source.discover(10)
    finally:
        source.close()

    chart_gets = [
        request for request in ProviderTestHandler.received_gets
        if request["path"] == "/videos" and request["query"].get("chart") == ["mostPopular"]
    ]
    assert batch.receipt.metadata["chart_requests"] == 6
    assert batch.receipt.metadata["search_requests"] == 0
    assert len(chart_gets) == 8
    assert batch.receipt.state == SourceState.READY
    assert len(batch.receipt.metadata["chart_category_errors"]) == 2
    assert {request["query"].get("regionCode", [""])[0] for request in chart_gets} == {"US", "GB"}
    assert {
        request["query"].get("videoCategoryId", ["all"])[0] for request in chart_gets
    } == {"all", "10", "20", "404"}


def test_keyword_frontier_finds_fresh_market_terms_and_drives_discovery(
    provider_server, market_config, monkeypatch
):
    monkeypatch.setenv("YOUTUBE_API_KEY", "integration-test-key")
    config = replace(
        market_config,
        adaptive_topics_enabled=True,
        adaptive_topic_limit=6,
        adaptive_topic_min_videos=2,
        adaptive_topic_exploration_fraction=0.2,
        youtube_search_daily_limit=1,
    )
    store = MarketTapeStore(config)
    now = datetime.now(timezone.utc)
    store.start_run("keyword-seed", "archive_bootstrap")
    for index, views in enumerate((2_000_000, 1_000_000, 500_000), start=1):
        store.ingest(
            _content_for_keyword(
                f"avengers-{index}",
                f"Avengers Doomsday trailer breakdown {index}",
                now - timedelta(hours=index),
                views,
            ),
            "keyword-seed",
        )
    store.finish_run("keyword-seed")

    signals = store.keyword_signals(limit=50, window_hours=168, min_videos=2)
    avengers = next(signal for signal in signals if signal["keyword"] == "avengers doomsday")
    assert avengers["videos_total"] == 3
    assert avengers["creators_total"] == 3
    assert avengers["views_total"] == 3_500_000
    assert avengers["query_ready"] is True

    observed_topics = []

    def source_builder(runtime_config, run_id, budget_for):
        observed_topics.extend(runtime_config.topics)
        return [YouTubeSource(
            runtime_config,
            run_id,
            budget_for(YouTubeSource.source_id, runtime_config.request_limit_for("youtube")),
            base_url=provider_server,
        )]

    result = MarketTapeCollector(config, store, source_builder=source_builder).run_cycle("discovery")
    assert result["discovery_topics"]["mode"] == "adaptive"
    assert any({"avengers", "doomsday"}.issubset(topic.split()) for topic in observed_topics)
    assert result["discovery_topics"]["adaptive_count"] >= 1
    assert result["receipts"][0]["metadata"]["queries_considered"] == observed_topics


def test_adaptive_frontier_suppresses_spelling_and_evidence_duplicates():
    candidates = [
        _keyword_signal("roblox", 90, 0.8, ["video-1", "video-2", "video-3"]),
        _keyword_signal("rblx", 100, 0.2, ["video-1", "video-2"]),
        _keyword_signal("trailers", 88, 0.7, ["video-4", "video-5"]),
        _keyword_signal("trailer", 87, 0.9, ["video-4", "video-5", "video-6"]),
        _keyword_signal("Kanye West", 86, 0.5, ["video-7", "video-8"]),
        _keyword_signal("Kanye", 85, 0.5, ["video-7", "video-8"]),
    ]

    ranked = sorted(candidates, key=MarketTapeCollector._discovery_priority, reverse=True)
    selected = MarketTapeCollector._diverse_keyword_signals(ranked, 10)

    assert [signal["keyword"] for signal in selected] == ["trailer", "roblox", "Kanye West"]


def test_discovery_queries_become_ranked_frontier_signals(market_config):
    store = MarketTapeStore(market_config)
    now = datetime.now(timezone.utc)
    store.start_run("query-attribution-run", "discovery")
    for index, views in enumerate((800_000, 500_000, 300_000), start=1):
        item = _content_for_keyword(
            f"query-video-{index}",
            f"Game recap number {index}",
            now - timedelta(minutes=index),
            views,
        )
        item.discovery_context = {
            "surface": "google_trends_to_youtube",
            "queries": ["Mariners vs Brewers"],
        }
        store.ingest(item, "query-attribution-run")
    store.finish_run("query-attribution-run")

    signals = store.keyword_signals(limit=100, window_hours=168, min_videos=2)
    signal = next(value for value in signals if value["keyword"] == "mariners vs brewers")

    assert signal["keyword_type"] == "query"
    assert signal["videos_total"] == 3
    assert signal["creators_total"] == 3
    assert signal["query_ready"] is True
    query_frontier = store.discovery_query_signals(limit=20, window_hours=168, min_videos=2)
    assert query_frontier[0]["keyword"] == "mariners vs brewers"
    collector = MarketTapeCollector(replace(
        market_config,
        adaptive_topics_enabled=True,
        adaptive_topic_limit=4,
        adaptive_topic_exploration_fraction=0,
    ), store, source_builder=lambda *_: [])
    collector._adaptive_discovery_config()
    assert "mariners vs brewers" in collector._last_discovery_topics["topics"]
    with store.connect() as connection:
        attributions = connection.execute(
            "SELECT query, surface FROM mt_discovery_attributions ORDER BY video_id"
        ).fetchall()
    assert len(attributions) == 3
    assert {row["query"] for row in attributions} == {"Mariners vs Brewers"}


def test_single_video_spike_is_visible_but_never_query_ready(market_config):
    store = MarketTapeStore(market_config)
    now = datetime.now(timezone.utc)
    store.start_run("single-spike-run", "discovery")
    store.ingest(
        _content_for_keyword("single-spike", "One enormous isolated spike", now, 90_000_000),
        "single-spike-run",
    )
    store.finish_run("single-spike-run")

    signals = store.keyword_signals(limit=100, window_hours=168, min_videos=1)
    signal = next(value for value in signals if value["keyword"] == "enormous isolated")
    assert signal["videos_total"] == 1
    assert signal["query_ready"] is False


def test_transactional_outbox_flushes_to_supabase_rest(provider_server, market_config, monkeypatch):
    store = MarketTapeStore(market_config)
    store.start_run("sync-run", "full")
    store.ingest(_content(datetime.now(timezone.utc), 100), "sync-run")
    now = datetime.now(timezone.utc)
    store.save_receipt(SourceReceipt(
        run_id="sync-run",
        source_id="integration-provider",
        platform="youtube",
        state=SourceState.READY,
        started_at=now,
        finished_at=now,
        accepted_count=1,
    ))
    store.finish_run("sync-run")
    assert store.enqueue_run_for_sync("sync-run") > 0

    monkeypatch.setenv("SUPABASE_URL", "https://project.supabase.co")
    monkeypatch.setenv("SUPABASE_SERVICE_KEY", "service-role-integration-key-1234567890")
    enabled = replace(market_config, supabase_sync_enabled=True)
    sink = SupabaseSink(enabled, store, rest_base_url=provider_server)
    try:
        result = sink.flush()
    finally:
        sink.close()
    assert result["state"] == "ready"
    assert result["synced"] > 0
    assert result["pending"] == 0
    assert any(post["path"] == "/actp_market_observations" for post in ProviderTestHandler.received_posts)
    assert any(post["path"] == "/actp_market_source_health" for post in ProviderTestHandler.received_posts)


def test_market_tape_migration_contract_matches_outbox_tables():
    validation = validate_migration()
    sink_tables = {definition[0] for definition in ENTITY_TABLES.values()}

    assert validation["state"] == "ready"
    assert validation["tables_expected"] == len(MARKET_TAPE_TABLES)
    assert set(MARKET_TAPE_TABLES) == sink_tables
    assert set(ENTITY_SYNC_ORDER) == set(ENTITY_TABLES)
    assert project_ref_from_url("https://ivhfuhxorppptyuofbgq.supabase.co") == "ivhfuhxorppptyuofbgq"
    assert project_ref_from_url("https://example.com") == ""


def test_market_tape_migration_applies_and_verifies_over_real_http(provider_server):
    project_ref = "abcdefghijklmnopqrst"
    posts_before = len(ProviderTestHandler.received_posts)
    gets_before = len(ProviderTestHandler.received_gets)
    manager = SupabaseMigrationManager(
        supabase_url=f"https://{project_ref}.supabase.co",
        service_role_key="service-role-integration-key-12345678901234567890",
        access_token="management-access-token-123456789012345678901234",
        management_api_url=provider_server,
        rest_base_url=provider_server,
    )
    try:
        result = manager.apply(project_ref, verify_delay_seconds=0)
    finally:
        manager.close()

    posts = ProviderTestHandler.received_posts[posts_before:]
    gets = ProviderTestHandler.received_gets[gets_before:]
    assert result["state"] == "applied", result
    assert result["inspection"]["tables_ready"] == len(MARKET_TAPE_TABLES)
    assert posts[0]["path"] == f"/v1/projects/{project_ref}/database/query"
    assert "create table if not exists public.actp_market_observations" in posts[0]["body"]["query"]
    assert posts[0]["body"]["read_only"] is False
    assert {request["path"].removeprefix("/") for request in gets} >= set(MARKET_TAPE_TABLES)


def test_market_tape_migration_rejects_target_mismatch(provider_server):
    manager = SupabaseMigrationManager(
        supabase_url="https://abcdefghijklmnopqrst.supabase.co",
        service_role_key="service-role-integration-key-12345678901234567890",
        access_token="management-access-token-123456789012345678901234",
        management_api_url=provider_server,
        rest_base_url=provider_server,
    )
    try:
        result = manager.apply("zyxwvutsrqponmlkjihg")
    finally:
        manager.close()

    assert result["state"] == "blocked_target_mismatch"


def test_market_tape_migration_verifies_security_invariants_over_real_http(provider_server):
    project_ref = "abcdefghijklmnopqrst"
    posts_before = len(ProviderTestHandler.received_posts)
    manager = SupabaseMigrationManager(
        supabase_url=f"https://{project_ref}.supabase.co",
        service_role_key="service-role-integration-key-12345678901234567890",
        access_token="management-access-token-123456789012345678901234",
        management_api_url=provider_server,
        rest_base_url=provider_server,
    )
    try:
        result = manager.verify_database(project_ref)
        counts = manager.remote_counts(project_ref)
    finally:
        manager.close()

    posts = ProviderTestHandler.received_posts[posts_before:]
    assert result["state"] == "ready", result
    assert result["tables_verified"] == len(MARKET_TAPE_TABLES)
    assert result["missing_tables"] == []
    assert result["rls_disabled"] == []
    assert result["unexpected_rls_policies"] == {}
    assert result["missing_append_only_triggers"] == []
    assert posts[0]["path"] == f"/v1/projects/{project_ref}/database/query"
    assert posts[0]["body"]["read_only"] is True
    assert "pg_catalog.pg_policies" in posts[0]["body"]["query"]
    assert counts["state"] == "ready", counts
    assert counts["tables_counted"] == len(MARKET_TAPE_TABLES)
    assert counts["total_rows"] == sum(range(1, len(MARKET_TAPE_TABLES) + 1))
    assert posts[1]["body"]["read_only"] is True
    assert "count(*)::bigint" in posts[1]["body"]["query"]


def test_transactional_outbox_drain_processes_multiple_batches(provider_server, market_config, monkeypatch):
    store = MarketTapeStore(market_config)
    store.start_run("drain-run", "full")
    observed = datetime.now(timezone.utc)
    for index in range(4):
        store.ingest(_content_for_id(f"drain-{index}", observed), "drain-run")
    store.finish_run("drain-run")
    assert store.enqueue_run_for_sync("drain-run") > 2

    monkeypatch.setenv("SUPABASE_URL", "https://project.supabase.co")
    monkeypatch.setenv("SUPABASE_SERVICE_KEY", "service-role-integration-key-1234567890")
    enabled = replace(
        market_config,
        supabase_sync_enabled=True,
        supabase_sync_batch_size=2,
    )
    sink = SupabaseSink(enabled, store, rest_base_url=provider_server)
    try:
        result = sink.drain(max_batches=100)
    finally:
        sink.close()

    assert result["state"] == "ready"
    assert result["batches"] > 1
    assert result["synced"] > 2
    assert result["pending"] == 0


def test_outbox_reconcile_queues_only_local_records_never_enqueued(
    provider_server, market_config, monkeypatch,
):
    store = MarketTapeStore(market_config)
    store.start_run("pre-outbox-run", "full")
    observed = datetime.now(timezone.utc)
    store.ingest(_content_for_id("pre-outbox-video", observed), "pre-outbox-run")
    store.finish_run("pre-outbox-run")

    queued = store.enqueue_missing_for_sync()
    assert queued > 0
    assert store.enqueue_missing_for_sync() == 0

    monkeypatch.setenv("SUPABASE_URL", "https://project.supabase.co")
    monkeypatch.setenv("SUPABASE_SERVICE_KEY", "service-role-integration-key-1234567890")
    enabled = replace(market_config, supabase_sync_enabled=True)
    sink = SupabaseSink(enabled, store, rest_base_url=provider_server)
    try:
        result = sink.drain(max_batches=100)
    finally:
        sink.close()

    assert result["state"] == "ready"
    assert result["synced"] == queued
    assert result["pending"] == 0
    paths = {post["path"] for post in ProviderTestHandler.received_posts}
    assert "/actp_market_videos" in paths
    assert "/actp_market_observations" in paths


def test_supabase_sink_orders_parent_run_before_earlier_receipt(provider_server, market_config, monkeypatch):
    store = MarketTapeStore(market_config)
    run_id = "dependency-run"
    store.start_run(run_id, "full")
    now = datetime.now(timezone.utc)
    store.save_receipt(SourceReceipt(
        run_id=run_id,
        source_id="dependency-provider",
        platform="youtube",
        state=SourceState.READY,
        started_at=now,
        finished_at=now,
        accepted_count=0,
    ))
    store.finish_run(run_id)
    store.enqueue_run_for_sync(run_id)
    with store.connect() as connection:
        run_outbox_id = connection.execute(
            "SELECT outbox_id FROM mt_sync_outbox WHERE entity_type = 'run' AND entity_key = ?",
            (run_id,),
        ).fetchone()[0]
        receipt_outbox_id = connection.execute(
            "SELECT outbox_id FROM mt_sync_outbox WHERE entity_type = 'receipt'",
        ).fetchone()[0]
        connection.execute(
            "UPDATE mt_sync_outbox SET outbox_id = ? WHERE outbox_id = ?",
            (receipt_outbox_id + 1000, run_outbox_id),
        )

    monkeypatch.setenv("SUPABASE_URL", "https://project.supabase.co")
    monkeypatch.setenv("SUPABASE_SERVICE_KEY", "service-role-integration-key-1234567890")
    enabled = replace(
        market_config,
        supabase_sync_enabled=True,
        supabase_sync_batch_size=1,
    )
    sink = SupabaseSink(enabled, store, rest_base_url=provider_server)
    try:
        result = sink.drain(max_batches=10)
    finally:
        sink.close()

    paths = [post["path"] for post in ProviderTestHandler.received_posts]
    assert result["state"] == "ready", result
    assert paths.index("/actp_market_collection_runs") < paths.index("/actp_market_source_receipts")


def test_supabase_sink_reports_unregistered_outbox_entity(provider_server, market_config, monkeypatch):
    store = MarketTapeStore(market_config)
    now = datetime.now(timezone.utc).isoformat()
    with store.connect() as connection:
        connection.execute(
            """INSERT INTO mt_sync_outbox(
                   entity_type, entity_key, payload_json, created_at, next_attempt_at
               ) VALUES('unknown_contract', 'unknown-1', '{}', ?, ?)""",
            (now, now),
        )

    monkeypatch.setenv("SUPABASE_URL", "https://project.supabase.co")
    monkeypatch.setenv("SUPABASE_SERVICE_KEY", "service-role-integration-key-1234567890")
    enabled = replace(market_config, supabase_sync_enabled=True)
    sink = SupabaseSink(enabled, store, rest_base_url=provider_server)
    try:
        result = sink.flush()
    finally:
        sink.close()

    assert result["state"] == "degraded"
    assert result["failed"] == 1
    assert result["pending"] == 1
    assert result["errors"] == ["unregistered outbox entity type: unknown_contract"]


def test_youtube_discovery_uses_quota_bounded_pagination(provider_server, market_config, monkeypatch):
    monkeypatch.setenv("YOUTUBE_API_KEY", "integration-test-key")
    config = replace(
        market_config,
        topics=["paginated"],
        youtube_search_daily_limit=4,
        provider_daily_request_limits={"youtube": 20},
    )

    def source_builder(runtime_config, run_id, budget_for):
        return [YouTubeSource(
            runtime_config,
            run_id,
            budget_for(YouTubeSource.source_id, runtime_config.request_limit_for("youtube")),
            base_url=provider_server,
        )]

    result = MarketTapeCollector(
        config,
        MarketTapeStore(config),
        source_builder=source_builder,
    ).run_cycle("discovery")

    assert result["receipts"][0]["metadata"]["search_requests"] == 4
    assert result["status"]["totals"]["videos"] == 5
    assert result["receipts"][0]["request_count"] == 9


def test_youtube_skips_known_pages_and_overflows_to_global_target(provider_server, market_config, monkeypatch):
    monkeypatch.setenv("YOUTUBE_API_KEY", "integration-test-key")
    config = replace(
        market_config,
        topics=["known-prefix"],
        daily_unique_target=5,
        platform_daily_targets={"youtube": 3},
        overflow_platforms=["youtube"],
        youtube_search_daily_limit=4,
        provider_daily_request_limits={"youtube": 20},
    )
    store = MarketTapeStore(config)
    store.start_run("seed-known", "archive_bootstrap")
    now = datetime.now(timezone.utc)
    for external_id in ("video-chart", "known-prefix-0", "known-prefix-1"):
        store.ingest(_content_for_id(external_id, now), "seed-known")
    store.finish_run("seed-known")

    def source_builder(runtime_config, run_id, budget_for):
        return [YouTubeSource(
            runtime_config,
            run_id,
            budget_for(YouTubeSource.source_id, runtime_config.request_limit_for("youtube")),
            base_url=provider_server,
        )]

    result = MarketTapeCollector(config, store, source_builder=source_builder).run_cycle("discovery")

    receipt = result["receipts"][0]
    assert receipt["accepted_count"] == 2
    assert receipt["metadata"]["known_ids_skipped"] == 3
    assert receipt["metadata"]["search_requests"] == 4
    assert receipt["request_count"] == 7
    assert result["status"]["daily"]["acquired"] == 5
    assert result["status"]["daily"]["platforms"]["youtube"] == {
        "target": 3,
        "acquired": 5,
        "remaining": 0,
    }


def test_youtube_preserves_partial_batch_when_search_quota_stops(provider_server, market_config, monkeypatch):
    monkeypatch.setenv("YOUTUBE_API_KEY", "integration-test-key")
    config = replace(
        market_config,
        topics=["quota-partial"],
        youtube_search_daily_limit=4,
    )

    def source_builder(runtime_config, run_id, budget_for):
        return [YouTubeSource(
            runtime_config,
            run_id,
            budget_for(YouTubeSource.source_id, runtime_config.request_limit_for("youtube")),
            base_url=provider_server,
        )]

    result = MarketTapeCollector(
        config,
        MarketTapeStore(config),
        source_builder=source_builder,
    ).run_cycle("discovery")

    receipt = result["receipts"][0]
    assert receipt["state"] == "ready"
    assert receipt["error_code"] == ""
    assert receipt["accepted_count"] == 2
    assert receipt["metadata"]["search_requests"] == 2
    assert receipt["metadata"]["terminated_by"] == "provider_rate_limited"
    assert receipt["metadata"]["search_lane_state"] == "blocked_quota"
    assert receipt["metadata"]["search_lane_error_code"] == "provider_rate_limited"
    assert result["status"]["totals"]["videos"] == 2


def test_youtube_search_bucket_is_persistent_across_cycles(provider_server, market_config, monkeypatch):
    monkeypatch.setenv("YOUTUBE_API_KEY", "integration-test-key")
    config = replace(
        market_config,
        topics=["paginated"],
        daily_unique_target=20,
        platform_daily_targets={"youtube": 20},
        youtube_search_daily_limit=2,
    )

    def source_builder(runtime_config, run_id, budget_for):
        return [YouTubeSource(
            runtime_config,
            run_id,
            budget_for(YouTubeSource.source_id, runtime_config.request_limit_for("youtube")),
            base_url=provider_server,
        )]

    store = MarketTapeStore(config)
    collector = MarketTapeCollector(config, store, source_builder=source_builder)
    first = collector.run_cycle("discovery")
    second = collector.run_cycle("discovery")

    assert first["receipts"][0]["metadata"]["search_requests"] == 2
    assert second["receipts"][0]["metadata"]["search_requests_used_before_run"] == 2
    assert second["receipts"][0]["metadata"]["search_requests"] == 0
    assert second["receipts"][0]["metadata"]["search_quota_remaining"] == 0


def test_provider_circuit_breaker_prevents_repeated_http_calls(provider_server, market_config, monkeypatch):
    monkeypatch.setenv("YOUTUBE_API_KEY", "integration-test-key")
    config = replace(
        market_config,
        topics=["force-http-error"],
        source_failure_backoff_seconds=3600,
    )

    def source_builder(runtime_config, run_id, budget_for):
        return [YouTubeSource(
            runtime_config,
            run_id,
            budget_for(YouTubeSource.source_id, runtime_config.request_limit_for("youtube")),
            base_url=provider_server,
        )]

    store = MarketTapeStore(config)
    collector = MarketTapeCollector(config, store, source_builder=source_builder)
    first = collector.run_cycle("discovery")
    requests_after_first = len(ProviderTestHandler.received_gets)
    second = collector.run_cycle("discovery")

    assert first["receipts"][0]["error_code"] == "provider_http_error"
    assert second["receipts"][0]["error_code"] == "circuit_open"
    assert len(ProviderTestHandler.received_gets) == requests_after_first
    retry = store.source_retry_status(YouTubeSource.source_id)
    assert retry["blocked"] is True
    assert retry["consecutive_failures"] == 1


def test_local_research_archive_source_normalizes_and_schedules(provider_server, market_config, tmp_path):
    archive = tmp_path / "research" / "twitter"
    archive.mkdir(parents=True)
    payload = {
        "results": [{
            "niche": "ai automation",
            "query": "ai agents",
            "collectionFinished": "2026-08-18T12:00:00Z",
            "tweets": [{
                "id": "1900000000000000001",
                "url": "https://x.com/systems/status/1900000000000000001",
                "author": "systems",
                "text": "AI agents need receipts #aiautomation",
                "views": 1200,
                "likes": 80,
                "replies": 7,
                "retweets": 12,
                "hasMedia": True,
                "collectedAt": "2026-08-18T12:00:00Z",
            }, {
                "id": "1900000000000000002",
                "url": "https://x.com/sports/status/1900000000000000002",
                "author": "sports",
                "text": "Unrelated basketball result carried over from an older browser page",
                "views": 9000,
                "collectedAt": "2026-08-18T12:00:00Z",
            }],
        }],
    }
    archive_file = archive / "twitter-research-niche.json"
    archive_file.write_text(json.dumps(payload), encoding="utf-8")
    old = datetime.now(timezone.utc).timestamp() - 7200
    os.utime(archive_file, (old, old))
    config = replace(
        market_config,
        platforms=["x"],
        local_research_dir=tmp_path / "research",
        local_research_trigger_enabled=True,
        local_research_refresh_seconds=60,
    )
    source = LocalResearchSource(
        config,
        "local-run",
        10,
        platform="x",
        api_platform="twitter",
        base_url=provider_server,
        archive_root=tmp_path / "research",
    )
    try:
        batch = source.discover(10)
    finally:
        source.close()
    assert len(batch.items) == 1
    assert batch.items[0].external_id == "1900000000000000001"
    assert batch.items[0].metrics.views == 1200
    assert batch.items[0].metrics.shares == 12
    assert batch.items[0].media_type == "video"
    assert batch.receipt.metadata["archive_qc"] == {
        "evaluated": 2,
        "accepted_relevant": 1,
        "rejected_irrelevant": 1,
        "unscoped": 0,
        "precision": 0.5,
        "policy": "niche-token-overlap-v1",
    }
    assert batch.receipt.metadata["scheduler"]["state"] == "triggered"
    assert len(batch.query_attempts) == 1
    assert batch.query_attempts[0].query == "ai agents"
    assert batch.query_attempts[0].result_count == 2
    assert any(post["path"] == "/api/research/twitter/full" for post in ProviderTestHandler.received_posts)


def test_local_research_cache_miss_does_not_poison_source_health(market_config, tmp_path):
    archive = tmp_path / "research" / "twitter"
    archive.mkdir(parents=True)
    observed = datetime.now(timezone.utc) - timedelta(hours=2)
    returned_id = "1900000000000000101"
    missing_id = "1900000000000000102"
    payload = {
        "results": [{
            "query": "live sports",
            "collectionFinished": observed.isoformat(),
            "tweets": [{
                "id": returned_id,
                "url": f"https://x.com/sports/status/{returned_id}",
                "author": "sportsdesk",
                "text": "Live sports result",
                "views": 1200,
                "likes": 80,
                "collectedAt": observed.isoformat(),
            }],
        }],
    }
    (archive / "twitter-research-final.json").write_text(json.dumps(payload), encoding="utf-8")
    config = replace(
        market_config,
        platforms=["x"],
        local_research_dir=tmp_path / "research",
        local_research_trigger_enabled=False,
        platform_daily_targets={"x": 10},
        provider_daily_request_limits={"x": 20},
        provider_cost_per_request_usd={"x": 0.0},
    )
    store = MarketTapeStore(config)
    source = LocalResearchSource(
        config,
        "seed-run",
        20,
        platform="x",
        api_platform="twitter",
        archive_root=tmp_path / "research",
    )
    source._reset_archive_qc()
    returned_item = source._load_archive(10)[0]
    source.close()
    missing_item = MarketContent(
        platform="x",
        external_id=missing_id,
        creator_external_id="missing-source-record",
        creator_handle="missing-source-record",
        published_at=observed - timedelta(minutes=10),
        observed_at=observed,
        source_id="safari-local-research-x",
        metrics=MetricCounters(views=400),
        caption="Record no longer present in the bounded fallback archive",
        url=f"https://x.com/missing/status/{missing_id}",
        media_type="post",
        raw_payload={"id": missing_id, "views": 400},
    )
    store.start_run("seed-run", "discovery")
    assert store.ingest(returned_item, "seed-run") == (True, True)
    assert store.ingest(missing_item, "seed-run") == (True, True)
    with store.connect() as connection:
        connection.execute(
            "UPDATE mt_poll_queue SET due_at = ? WHERE platform = 'x'",
            ((observed - timedelta(minutes=1)).isoformat(),),
        )

    store.start_run("recheck-run", "recheck")
    receipts = MarketTapeCollector(config, store)._run_rechecks("recheck-run")

    local_receipt = next(
        receipt for receipt in receipts
        if receipt["source_id"] == "safari-local-research-x"
    )
    assert local_receipt["state"] == "ready"
    assert local_receipt["error_code"] == ""
    assert local_receipt["accepted_count"] == 0
    assert local_receipt["duplicate_count"] == 1
    assert local_receipt["failed_count"] == 1
    assert local_receipt["metadata"]["tracked_count"] == 2
    assert local_receipt["metadata"]["returned_tracked_count"] == 1
    assert local_receipt["metadata"]["missing_tracked_count"] == 1
    assert local_receipt["metadata"]["item_failure_code"] == "provider_item_missing"
    with store.connect() as connection:
        health = connection.execute(
            "SELECT state, consecutive_failures, error_code FROM mt_source_health "
            "WHERE source_id = 'safari-local-research-x'"
        ).fetchone()
        missing_poll = connection.execute(
            "SELECT last_error_code FROM mt_poll_queue WHERE video_id = ?",
            (missing_item.video_id,),
        ).fetchone()
    assert dict(health) == {
        "state": "ready",
        "consecutive_failures": 0,
        "error_code": "",
    }
    assert missing_poll["last_error_code"] == "provider_item_missing"


def test_genuine_ingest_failure_remains_a_normalization_failure(market_config):
    store = MarketTapeStore(market_config)
    store.start_run("invalid-payload-run", "discovery")
    item = _content(datetime.now(timezone.utc), views=100)
    cyclic_payload = {}
    cyclic_payload["self"] = cyclic_payload
    item.raw_payload = cyclic_payload
    now = datetime.now(timezone.utc)
    batch = ProviderBatch(
        items=[item],
        receipt=SourceReceipt(
            run_id="invalid-payload-run",
            source_id="captured-provider",
            platform="youtube",
            state=SourceState.READY,
            started_at=now,
            finished_at=now,
        ),
    )

    MarketTapeCollector(market_config, store)._persist_batch(
        batch,
        "invalid-payload-run",
    )

    assert batch.receipt.state == SourceState.DEGRADED
    assert batch.receipt.error_code == "normalization_failed"
    assert batch.receipt.failed_count == 1
    assert batch.receipt.metadata["ingest_failure_count"] == 1
    assert batch.receipt.metadata["ingest_failure_types"] == {"ValueError": 1}


def test_local_research_dispatch_blocks_before_browser_when_disk_is_low(
    provider_server,
    market_config,
    tmp_path,
):
    archive = tmp_path / "research" / "twitter"
    archive.mkdir(parents=True)
    config = replace(
        market_config,
        platforms=["x"],
        local_research_dir=tmp_path / "research",
        local_research_trigger_enabled=True,
        local_research_min_free_bytes=2**63 - 1,
    )
    source = LocalResearchSource(
        config,
        "disk-pressure-run",
        10,
        platform="x",
        api_platform="twitter",
        base_url=provider_server,
        archive_root=tmp_path / "research",
    )
    try:
        batch = source.discover(10)
    finally:
        source.close()

    scheduler = batch.receipt.metadata["scheduler"]
    assert scheduler["state"] == "blocked_disk_pressure"
    assert scheduler["free_bytes"] < scheduler["minimum_free_bytes"]
    assert batch.receipt.state == SourceState.DEGRADED
    assert batch.receipt.error_code == "insufficient_temporary_disk"
    assert ProviderTestHandler.received_posts == []


def test_failed_cross_platform_job_cools_down_then_retries_only_failed_lanes(
    provider_server,
    market_config,
    tmp_path,
):
    archive = tmp_path / "research" / "tiktok"
    archive.mkdir(parents=True)
    now = datetime.now(timezone.utc)
    state_path = tmp_path / "local-research-state.json"
    state_path.write_text(json.dumps({
        "requested_at": now.isoformat(),
        "job_id": "failed-job",
        "query_hash": stable_hash(["superseded frontier topic"]),
        "query_count": 1,
        "platforms": ["tiktok", "twitter"],
    }), encoding="utf-8")
    ProviderTestHandler.research_jobs["failed-job"] = {
        "id": "failed-job",
        "status": "failed",
        "completedAt": now.isoformat(),
        "platformReceipts": [
            {"platform": "tiktok", "status": "completed"},
            {"platform": "twitter", "status": "failed"},
        ],
    }
    cooling_config = replace(
        market_config,
        platforms=["tiktok", "x"],
        topics=["live sports"],
        local_research_dir=tmp_path / "research",
        local_research_state_path=state_path,
        local_research_trigger_enabled=True,
        local_research_refresh_seconds=86400,
        local_research_failure_retry_seconds=3600,
    )

    cooling_source = LocalResearchSource(
        cooling_config,
        "cooling-run",
        10,
        platform="tiktok",
        base_url=provider_server,
        archive_root=tmp_path / "research",
    )
    try:
        cooling = cooling_source.discover(10)
    finally:
        cooling_source.close()
    assert cooling.receipt.metadata["scheduler"]["state"] == "failed_cooldown"
    assert ProviderTestHandler.received_posts == []

    retry_config = replace(cooling_config, local_research_failure_retry_seconds=0)
    retry_source = LocalResearchSource(
        retry_config,
        "retry-run",
        10,
        platform="tiktok",
        base_url=provider_server,
        archive_root=tmp_path / "research",
    )
    try:
        retry = retry_source.discover(10)
    finally:
        retry_source.close()

    scheduler = retry.receipt.metadata["scheduler"]
    assert scheduler["state"] == "triggered_all"
    assert scheduler["platforms"] == ["twitter"]
    assert scheduler["retry_of_job_id"] == "failed-job"
    assert ProviderTestHandler.received_posts[-1]["path"] == "/api/research/all/full"
    assert ProviderTestHandler.received_posts[-1]["body"]["platforms"] == ["twitter"]


def test_provider_restart_persists_missing_job_cooldown_then_retries_recorded_lanes(
    provider_server,
    market_config,
    tmp_path,
):
    archive = tmp_path / "research" / "tiktok"
    archive.mkdir(parents=True)
    now = datetime.now(timezone.utc)
    state_path = tmp_path / "local-research-state.json"
    state_path.write_text(json.dumps({
        "requested_at": (now - timedelta(hours=2)).isoformat(),
        "job_id": "forgotten-after-restart",
        "query_hash": stable_hash(["live sports"]),
        "query_count": 1,
        "platforms": ["tiktok", "twitter"],
    }), encoding="utf-8")
    cooling_config = replace(
        market_config,
        platforms=["tiktok", "x"],
        topics=["live sports"],
        local_research_dir=tmp_path / "research",
        local_research_state_path=state_path,
        local_research_trigger_enabled=True,
        local_research_refresh_seconds=86400,
        local_research_failure_retry_seconds=3600,
    )

    cooling_source = LocalResearchSource(
        cooling_config,
        "missing-job-cooling-run",
        10,
        platform="tiktok",
        base_url=provider_server,
        archive_root=tmp_path / "research",
    )
    try:
        cooling = cooling_source.discover(10)
    finally:
        cooling_source.close()
    scheduler = cooling.receipt.metadata["scheduler"]
    assert scheduler["state"] == "missing_job_cooldown"
    assert scheduler["job_status"] == "not_found"
    assert 3590 <= scheduler["retry_in_seconds"] <= 3600
    persisted = json.loads(state_path.read_text(encoding="utf-8"))
    assert persisted["missing_reason"] == "provider_job_not_found"
    assert persisted["missing_since"]
    assert ProviderTestHandler.received_posts == []

    retry_config = replace(cooling_config, local_research_failure_retry_seconds=0)
    retry_source = LocalResearchSource(
        retry_config,
        "missing-job-retry-run",
        10,
        platform="tiktok",
        base_url=provider_server,
        archive_root=tmp_path / "research",
    )
    try:
        retry = retry_source.discover(10)
    finally:
        retry_source.close()
    retried = retry.receipt.metadata["scheduler"]
    assert retried["state"] == "triggered_all"
    assert retried["platforms"] == ["tiktok", "twitter"]
    assert retried["retry_of_job_id"] == "forgotten-after-restart"
    assert ProviderTestHandler.received_posts[-1]["body"]["platforms"] == [
        "tiktok", "twitter",
    ]


def test_cumulative_browser_intermediates_preserve_partial_attempts_without_duplicates(
    market_config,
    tmp_path,
):
    archive = tmp_path / "research" / "tiktok"
    archive.mkdir(parents=True)
    live_sports = {
        "niche": "live sports",
        "query": "live sports",
        "videos": [{"id": "sports-1"}],
        "collectionStarted": "2026-08-20T04:41:05Z",
        "collectionFinished": "2026-08-20T04:51:30Z",
    }
    music = {
        "niche": "music releases",
        "query": "music releases",
        "videos": [],
        "collectionStarted": "2026-08-20T04:51:35Z",
        "collectionFinished": "2026-08-20T04:56:10Z",
    }
    (archive / "tiktok-research-intermediate-1.json").write_text(json.dumps({
        "metadata": {"generatedAt": "2026-08-20T04:51:30Z"},
        "results": [live_sports],
    }), encoding="utf-8")
    (archive / "tiktok-research-intermediate-2.json").write_text(json.dumps({
        "metadata": {"generatedAt": "2026-08-20T04:56:10Z"},
        "results": [live_sports, music],
    }), encoding="utf-8")
    config = replace(
        market_config,
        local_research_dir=tmp_path / "research",
        local_research_trigger_enabled=False,
    )
    source = LocalResearchSource(
        config,
        "partial-receipt-run",
        10,
        platform="tiktok",
        archive_root=tmp_path / "research",
    )
    try:
        attempts = source.archived_query_attempts()
    finally:
        source.close()

    assert [attempt.query for attempt in attempts] == [
        "live sports",
        "music releases",
    ]
    assert [attempt.state for attempt in attempts] == ["completed", "empty"]
    assert attempts[0].result_count == 1
    assert attempts[0].metadata["source_file"] == (
        "tiktok-research-intermediate-2.json"
    )


def test_query_attempt_backfill_verifies_youtube_and_deduplicates_safari_receipts(
    market_config,
    tmp_path,
):
    safari_dir = tmp_path / "research" / "twitter"
    safari_dir.mkdir(parents=True)
    safari_payload = {
        "metadata": {"generatedAt": "2026-08-19T12:00:00Z"},
        "results": [{"query": "summer recipes", "tweets": []}],
    }
    (safari_dir / "twitter-final.json").write_text(
        json.dumps(safari_payload),
        encoding="utf-8",
    )

    youtube_dir = tmp_path / "trend-frontier" / "2026-08-19" / "iteration-1"
    youtube_dir.mkdir(parents=True)
    artifact = youtube_dir / "movie-trailers.jsonl"
    artifact.write_text('{"id":"video-1"}\n', encoding="utf-8")
    artifact_sha = hashlib.sha256(artifact.read_bytes()).hexdigest()
    manifest = {
        "contract": "youtube_query_research_receipt_v1",
        "started_at": "2026-08-19T12:00:00Z",
        "finished_at": "2026-08-19T12:00:05Z",
        "receipts": [{
            "query": "movie trailers",
            "output_path": str(artifact),
            "output_sha256": artifact_sha,
            "records": 1,
            "state": "completed",
            "error": "",
        }],
    }
    (youtube_dir / "research-manifest.json").write_text(
        json.dumps(manifest),
        encoding="utf-8",
    )
    config = replace(
        market_config,
        local_research_dir=tmp_path / "research",
        youtube_research_dir=tmp_path / "trend-frontier",
    )
    store = MarketTapeStore(config)
    collector = MarketTapeCollector(config, store)

    first = collector.backfill_query_attempts()
    second = collector.backfill_query_attempts()

    assert first["state"] == "completed"
    assert first["attempts_inserted"] == 2
    assert second["attempts_inserted"] == 0
    first_run = next(run for run in store.list_runs(10) if run["run_id"] == first["run_id"])
    assert first_run["observations_added"] == 0
    attempts = store.list_query_attempts(10)
    assert {(attempt["platform"], attempt["query"]) for attempt in attempts} == {
        ("x", "summer recipes"),
        ("youtube", "movie trailers"),
    }
    youtube = next(attempt for attempt in attempts if attempt["platform"] == "youtube")
    assert youtube["metadata"]["artifact_verified"] is True
    assert youtube["artifact_sha256"] == artifact_sha


def test_discovery_context_trend_backfill_is_idempotent_and_provider_free(market_config):
    store = MarketTapeStore(market_config)
    observed = datetime.now(timezone.utc) - timedelta(hours=1)
    store.start_run("historical-context-run", "archive_bootstrap")
    item = _content_for_keyword(
        "historical-context-video",
        "A measured result from the field",
        observed,
        25_000,
    )
    store.ingest(item, "historical-context-run")
    store.finish_run("historical-context-run")
    context = {"niche": "live sports", "query": "live sports latest"}
    with store.connect() as connection:
        connection.execute(
            """INSERT INTO mt_discovery_attributions(
                   attribution_key, run_id, video_id, source_id, discovered_at,
                   surface, query, context_json
               ) VALUES(?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                "historical-live-sports-attribution",
                "historical-context-run",
                item.video_id,
                item.source_id,
                observed.isoformat(),
                "historical_archive",
                "live sports latest",
                json.dumps(context),
            ),
        )

    first = store.backfill_context_trends()
    second = store.backfill_context_trends()

    assert first["attributions_scanned"] == 1
    assert first["trends_inserted"] == 1
    assert first["memberships_inserted"] == 1
    assert len(first["affected_trend_ids"]) == 1
    assert second["trends_inserted"] == 0
    assert second["memberships_inserted"] == 0
    with store.connect() as connection:
        membership = connection.execute(
            """SELECT trend.display_name, membership.evidence_json
               FROM mt_trend_memberships membership
               JOIN mt_trends trend USING(trend_id)
               WHERE membership.video_id = ? AND trend.canonical_key = 'live sports'""",
            (item.video_id,),
        ).fetchone()
    assert membership["display_name"] == "Live Sports"
    assert json.loads(membership["evidence_json"])["contract"] == (
        "discovery-context-trend-backfill-v1"
    )


def test_recheck_cycle_promotes_new_safari_archive_without_external_calls(market_config, tmp_path):
    archive = tmp_path / "research" / "twitter"
    archive.mkdir(parents=True)
    archive.joinpath("new.json").write_text(json.dumps({
        "results": [{
            "collectionFinished": datetime.now(timezone.utc).isoformat(),
            "tweets": [{
                "id": "1900000000000000999",
                "author": "market-tape",
                "text": "New social market observation",
                "views": 41,
                "collectedAt": datetime.now(timezone.utc).isoformat(),
            }],
        }],
    }), encoding="utf-8")
    config = replace(
        market_config,
        platforms=["x"],
        platform_daily_targets={"x": 10},
        provider_daily_request_limits={"x": 10},
        local_research_dir=tmp_path / "research",
        local_research_trigger_enabled=False,
    )
    store = MarketTapeStore(config)
    result = MarketTapeCollector(config, store, source_builder=lambda *_: []).run_cycle("recheck")

    assert result["state"] == "completed"
    assert result["status"]["totals"]["videos"] == 1
    assert result["receipts"][0]["source_id"] == "safari-local-research-x"
    assert result["receipts"][0]["request_count"] == 0


def test_due_poll_selection_is_platform_fair_and_unchanged_snapshots_advance(market_config):
    config = replace(market_config, platforms=["youtube", "x"])
    store = MarketTapeStore(config)
    observed = datetime.now(timezone.utc) - timedelta(hours=2)
    store.start_run("fair-poll-run", "archive_bootstrap")
    for platform in config.platforms:
        for index in range(5):
            store.ingest(MarketContent(
                platform=platform,
                external_id=f"{platform}-fair-{index}",
                creator_external_id=f"{platform}-creator-{index}",
                published_at=observed - timedelta(hours=2),
                observed_at=observed,
                source_id=f"{platform}-source",
                metrics=MetricCounters(views=100 + index),
                title=f"Fair queue item {platform} {index}",
                raw_payload={"platform": platform, "index": index},
            ), "fair-poll-run")

    due = store.due_polls(4)

    assert {platform: len(rows) for platform, rows in due.items()} == {
        "youtube": 2,
        "x": 2,
    }
    deferred_id = due["youtube"][0]["video_id"]
    assert store.defer_unchanged_polls([deferred_id]) == 1
    remaining = {
        row["video_id"]
        for rows in store.due_polls(20).values()
        for row in rows
    }
    assert deferred_id not in remaining
    with store.connect() as connection:
        queue_row = connection.execute(
            "SELECT last_error_code FROM mt_poll_queue WHERE video_id = ?",
            (deferred_id,),
        ).fetchone()
    assert queue_row["last_error_code"] == "unchanged_source_snapshot"


def test_local_archive_ingest_bypasses_prior_scheduler_cooldown(market_config, tmp_path):
    archive = tmp_path / "research" / "twitter"
    archive.mkdir(parents=True)
    now = datetime.now(timezone.utc)
    archive.joinpath("after-scheduler-failure.json").write_text(json.dumps({
        "results": [{
            "collectionFinished": now.isoformat(),
            "tweets": [{
                "id": "1900000000000001888",
                "author": "archive-recovery",
                "text": "Archive ingestion must not wait for scheduler cooldown",
                "views": 88,
                "collectedAt": now.isoformat(),
            }],
        }],
    }), encoding="utf-8")
    config = replace(
        market_config,
        platforms=["x"],
        platform_daily_targets={"x": 10},
        provider_daily_request_limits={"x": 10},
        local_research_dir=tmp_path / "research",
        local_research_trigger_enabled=False,
    )
    store = MarketTapeStore(config)
    store.start_run("scheduler-failed", "discovery")
    store.save_receipt(SourceReceipt(
        run_id="scheduler-failed",
        source_id="safari-local-research-x",
        platform="x",
        state=SourceState.DEGRADED,
        started_at=now,
        finished_at=now,
        error_code="scheduler_unavailable",
        error_detail="Safari was unavailable before the archive arrived",
    ))
    store.finish_run("scheduler-failed")

    result = MarketTapeCollector(config, store, source_builder=lambda *_: []).run_cycle("recheck")

    assert result["state"] == "completed"
    assert result["receipts"][0]["accepted_count"] == 1
    assert result["receipts"][0]["error_code"] == ""
    assert result["status"]["daily"]["platforms"]["x"]["acquired"] == 1


def test_market_tape_api_is_readable_and_write_control_is_local(market_config):
    app = Flask(__name__)
    register_market_tape_routes(app, market_config)
    client = app.test_client()
    assert client.get("/api/market-tape/status").status_code == 200
    assert client.get("/api/market-tape/videos").status_code == 200
    assert client.get("/api/market-tape/trends").status_code == 200
    assert client.get("/api/market-tape/keywords").status_code == 200
    assert client.get("/api/market-tape/query-frontier").status_code == 200
    assert client.get("/api/market-tape/predictions").status_code == 200
    assert client.get("/api/market-tape/prediction-backtest").status_code == 200
    assert client.get("/api/market-tape/predictions/model").status_code == 200
    assert client.get("/api/market-tape/opportunities").status_code == 200
    assert client.get("/api/market-tape/query-attempts").status_code == 200
    assert client.get("/api/market-tape/datasets/status").status_code == 200
    assert client.get("/api/market-tape/candles?window_minutes=15").status_code == 200
    denied = client.post(
        "/api/market-tape/cycles",
        json={"mode": "recheck"},
        environ_overrides={"REMOTE_ADDR": "203.0.113.10"},
    )
    assert denied.status_code == 401


def test_dedicated_market_tape_app_health_and_security_headers(market_config):
    from market_tape_app import create_market_tape_app

    client = create_market_tape_app(market_config).test_client()
    response = client.get("/health")

    assert response.status_code == 200
    assert response.get_json()["service"] == "content-intelligence-market-tape"
    assert response.headers["X-Content-Type-Options"] == "nosniff"
    assert client.get("/api/market-tape/query-frontier").status_code == 200
    reindex = client.post(
        "/api/market-tape/trends/reindex",
        json={"forecast_limit": 100},
    )
    assert reindex.status_code == 200
    assert reindex.get_json()["provider_calls_made"] == 0


def test_market_tape_tick_selects_due_mode_without_external_calls(market_config):
    config = replace(market_config, platforms=[], discovery_interval_seconds=3600)
    app = Flask(__name__)
    register_market_tape_routes(app, config)
    client = app.test_client()

    first = client.post("/api/market-tape/tick", json={})
    second = client.post("/api/market-tape/tick", json={})

    assert first.status_code == 200
    assert first.get_json()["mode"] == "full"
    assert second.status_code == 200
    assert second.get_json()["mode"] == "recheck"


def test_secret_sanitization_and_scale_defaults():
    value = sanitize("https://provider.test/data?access_token=secret&api_key=also-secret")
    assert "secret" not in value
    config = MarketTapeConfig()
    assert config.daily_unique_target == 5000
    assert config.overflow_platforms == ["youtube"]
    assert len(config.topics) >= 20
    assert "live sports" in config.topics
    assert "ai automation" not in config.topics
    assert set(config.platforms) == {"youtube", "tiktok", "instagram", "x", "facebook", "threads"}


def _content(observed_at, views):
    return MarketContent(
        platform="youtube",
        external_id="video-1",
        creator_external_id="creator-1",
        creator_handle="systems-lab",
        published_at=observed_at - timedelta(minutes=10),
        observed_at=observed_at,
        source_id="integration-provider",
        metrics=MetricCounters(views=views, likes=10, comments=2),
        title="How AI automation works #aiautomation",
        duration_seconds=45,
        raw_payload={"id": "video-1", "views": views},
    )


def _content_for_id(external_id, observed_at):
    return MarketContent(
        platform="youtube",
        external_id=external_id,
        creator_external_id=f"creator-{external_id}",
        published_at=observed_at - timedelta(minutes=10),
        observed_at=observed_at,
        source_id="integration-provider",
        metrics=MetricCounters(views=100, likes=10, comments=2),
        title="Known content used to verify discovery pagination",
        raw_payload={"id": external_id, "views": 100},
    )


def _content_for_keyword(external_id, title, observed_at, views):
    return MarketContent(
        platform="youtube",
        external_id=external_id,
        creator_external_id=f"creator-{external_id}",
        published_at=observed_at - timedelta(hours=2),
        observed_at=observed_at,
        source_id="integration-provider",
        metrics=MetricCounters(
            views=views,
            likes=max(1, views // 20),
            comments=max(1, views // 1000),
        ),
        title=title,
        duration_seconds=45,
        raw_payload={"id": external_id, "views": views, "title": title},
    )


def _keyword_signal(keyword, score, confidence, video_ids):
    return {
        "keyword": keyword,
        "score": score,
        "confidence": confidence,
        "videos_total": len(video_ids),
        "creators_total": len(video_ids),
        "examples": [{"video_id": video_id} for video_id in video_ids],
    }
