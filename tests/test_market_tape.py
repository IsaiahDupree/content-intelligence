"""Market Tape V1 integration tests use a real local HTTP server and SQLite."""

from __future__ import annotations

import json
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
from services.market_tape.models import MarketContent, MetricCounters, SourceReceipt, SourceState
from services.market_tape.sources.base import sanitize
from services.market_tape.sources.local_research import LocalResearchSource
from services.market_tape.sources.youtube import YouTubeSource
from services.market_tape.sinks.supabase import SupabaseSink
from services.market_tape.store import MarketTapeStore


class ProviderTestHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    received_posts = []
    received_gets = []

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
            ids = query.get("id", ["video-chart"])[0].split(",")
            self._json({"items": [self._video(video_id) for video_id in ids if video_id]})
            return
        self._json({"error": "not found"}, status=404)

    def do_POST(self):  # noqa: N802 - HTTP handler contract
        length = int(self.headers.get("Content-Length", "0"))
        body = json.loads(self.rfile.read(length) or b"[]")
        self.__class__.received_posts.append({"path": urlparse(self.path).path, "body": body})
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
        platforms=["youtube"],
        topics=["ai automation"],
        regions=["US"],
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
    observed = datetime(2026, 8, 18, 12, 10, tzinfo=timezone.utc)
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
    assert receipt["state"] == "blocked_quota"
    assert receipt["error_code"] == "provider_rate_limited"
    assert receipt["accepted_count"] == 2
    assert receipt["metadata"]["search_requests"] == 2
    assert receipt["metadata"]["terminated_by"] == "provider_rate_limited"
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
    assert batch.receipt.metadata["scheduler"]["state"] == "triggered"
    assert any(post["path"] == "/api/research/twitter/full" for post in ProviderTestHandler.received_posts)


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
    assert client.get("/api/market-tape/predictions").status_code == 200
    assert client.get("/api/market-tape/candles?window_minutes=15").status_code == 200
    denied = client.post(
        "/api/market-tape/cycles",
        json={"mode": "recheck"},
        environ_overrides={"REMOTE_ADDR": "203.0.113.10"},
    )
    assert denied.status_code == 401


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
    assert len(config.topics) == 15
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
