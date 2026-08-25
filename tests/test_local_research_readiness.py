"""Local research readiness tests use real files and a loopback HTTP service."""

from __future__ import annotations

import json
import os
import threading
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse

import pytest

from services.market_tape.config import MarketTapeConfig
from services.market_tape.models import SourceState
from services.market_tape.sources.local_research import LocalResearchSource


class CoordinatorStatusHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    jobs = {}
    requests = []

    def do_GET(self):  # noqa: N802 - HTTP handler contract
        path = urlparse(self.path).path
        self.__class__.requests.append({"method": "GET", "path": path})
        if path.startswith("/api/research/status/"):
            job_id = path.rsplit("/", 1)[-1]
            job = self.__class__.jobs.get(job_id)
            self._json(job or {"error": "not found"}, status=200 if job else 404)
            return
        self._json({"error": "not found"}, status=404)

    def do_POST(self):  # noqa: N802 - HTTP handler contract
        path = urlparse(self.path).path
        self.__class__.requests.append({"method": "POST", "path": path})
        self._json({"error": "unexpected write"}, status=500)

    def _json(self, body, status=200):
        encoded = json.dumps(body).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)

    def log_message(self, *_):
        return


@pytest.fixture
def coordinator_server():
    CoordinatorStatusHandler.jobs = {}
    CoordinatorStatusHandler.requests = []
    server = ThreadingHTTPServer(("127.0.0.1", 0), CoordinatorStatusHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


@pytest.fixture
def readiness_config(tmp_path):
    return MarketTapeConfig(
        db_path=tmp_path / "market.sqlite3",
        object_dir=tmp_path / "objects",
        heartbeat_path=tmp_path / "heartbeat.json",
        lock_path=tmp_path / "market.lock",
        local_research_state_path=tmp_path / "local-research-state.json",
        local_research_dir=tmp_path / "research",
        prediction_model_dir=tmp_path / "models",
        platforms=["tiktok", "instagram", "x"],
        topics=["creator economy"],
        adaptive_topics_enabled=False,
        local_research_trigger_enabled=True,
        local_research_refresh_seconds=3600,
        local_research_failure_retry_seconds=1800,
        local_research_min_free_bytes=0,
        platform_daily_targets={"tiktok": 10, "instagram": 10, "x": 10},
        provider_daily_request_limits={"tiktok": 10, "instagram": 10, "x": 10},
        provider_cost_per_request_usd={"tiktok": 0.0, "instagram": 0.0, "x": 0.0},
        supabase_sync_enabled=False,
    )


def _write_instagram_archive(root, observed_at: datetime, *, touch_at: datetime | None = None):
    archive = root / "instagram"
    archive.mkdir(parents=True, exist_ok=True)
    path = archive / "instagram-creator-economy.json"
    path.write_text(json.dumps({
        "metadata": {"generatedAt": observed_at.isoformat()},
        "results": [{
            "niche": "creator economy",
            "query": "creator economy",
            "collectionFinished": observed_at.isoformat(),
            "posts": [{
                "id": "instagram-readiness-1",
                "author": "creator-lab",
                "description": "Creator economy revenue systems for creators",
                "collectedAt": observed_at.isoformat(),
                "views": 500,
                "likes": 50,
            }],
        }],
    }), encoding="utf-8")
    if touch_at is not None:
        timestamp = touch_at.timestamp()
        os.utime(path, (timestamp, timestamp))
    return path


def _write_x_archive(root, observed_at: datetime):
    archive = root / "twitter"
    archive.mkdir(parents=True, exist_ok=True)
    path = archive / "twitter-creator-economy.json"
    path.write_text(json.dumps({
        "results": [{
            "niche": "creator economy",
            "query": "creator economy",
            "collectionFinished": observed_at.isoformat(),
            "tweets": [{
                "id": "1900000000000000991",
                "author": "creator-lab",
                "text": "Creator economy revenue systems for creators",
                "collectedAt": observed_at.isoformat(),
                "views": 500,
            }],
        }],
    }), encoding="utf-8")
    return path


def _write_schedule_state(config, job_id: str, requested_at: datetime):
    config.local_research_state_path.write_text(json.dumps({
        "requested_at": requested_at.isoformat(),
        "job_id": job_id,
        "query_hash": "readiness-query-hash",
        "query_count": 1,
        "platforms": ["tiktok", "instagram", "twitter"],
    }), encoding="utf-8")


def _instagram_source(config, server_url, run_id):
    return LocalResearchSource(
        config,
        run_id,
        10,
        platform="instagram",
        api_platform="instagram",
        base_url=server_url,
        archive_root=config.local_research_dir,
    )


def test_archive_adapter_never_claims_live_query_or_terminal_metric_capability(
    coordinator_server,
    readiness_config,
):
    source = _instagram_source(
        readiness_config,
        coordinator_server,
        "archive-capability-contract",
    )
    try:
        assert source.adaptive_query_execution_capable() is False
        assert source.terminal_metrics_capable() is False
    finally:
        source.close()


def test_non_coordinator_propagates_exact_failed_lane_state(
    coordinator_server,
    readiness_config,
):
    now = datetime.now(timezone.utc)
    _write_instagram_archive(readiness_config.local_research_dir, now)
    _write_schedule_state(readiness_config, "shared-failed-job", now)
    CoordinatorStatusHandler.jobs["shared-failed-job"] = {
        "id": "shared-failed-job",
        "status": "failed",
        "completedAt": now.isoformat(),
        "platformReceipts": [
            {"platform": "tiktok", "status": "completed"},
            {"platform": "instagram", "status": "failed", "completedAt": now.isoformat()},
            {"platform": "twitter", "status": "failed", "completedAt": now.isoformat()},
        ],
    }
    source = _instagram_source(
        readiness_config,
        coordinator_server,
        "failed-lane-run",
    )
    try:
        batch = source.discover(10)
    finally:
        source.close()

    scheduler = batch.receipt.metadata["scheduler"]
    assert batch.receipt.state == SourceState.READY
    assert scheduler["state"] == "failed_cooldown"
    assert scheduler["coordinator"] == "tiktok"
    assert scheduler["job_status"] == "failed"
    assert scheduler["lane_status"] == "failed"
    assert scheduler["lane_platform"] == "instagram"
    assert scheduler["retry_in_seconds"] > 0
    assert batch.receipt.metadata["acquisition_state"] == "failed_cooldown"
    assert CoordinatorStatusHandler.requests == [{
        "method": "GET",
        "path": "/api/research/status/shared-failed-job",
    }]


def test_non_coordinator_uses_completed_lane_not_failed_overall_job(
    coordinator_server,
    readiness_config,
):
    now = datetime.now(timezone.utc)
    _write_instagram_archive(readiness_config.local_research_dir, now)
    _write_schedule_state(readiness_config, "shared-partial-job", now)
    CoordinatorStatusHandler.jobs["shared-partial-job"] = {
        "id": "shared-partial-job",
        "status": "failed",
        "completedAt": now.isoformat(),
        "platformReceipts": [
            {"platform": "tiktok", "status": "failed", "completedAt": now.isoformat()},
            {"platform": "instagram", "status": "completed", "completedAt": now.isoformat()},
        ],
    }
    source = _instagram_source(
        readiness_config,
        coordinator_server,
        "completed-lane-run",
    )
    try:
        batch = source.discover(10)
    finally:
        source.close()

    scheduler = batch.receipt.metadata["scheduler"]
    assert scheduler["job_status"] == "failed"
    assert scheduler["lane_status"] == "completed"
    assert scheduler["state"] == "recently_completed"
    assert batch.receipt.metadata["acquisition_state"] == "recently_completed"
    assert batch.receipt.metadata["archive_fresh"] is True


def test_x_lane_accepts_x_or_twitter_platform_receipt_names(
    coordinator_server,
    readiness_config,
):
    now = datetime.now(timezone.utc)
    _write_x_archive(readiness_config.local_research_dir, now)
    _write_schedule_state(readiness_config, "shared-x-alias-job", now)
    CoordinatorStatusHandler.jobs["shared-x-alias-job"] = {
        "id": "shared-x-alias-job",
        "status": "completed",
        "completedAt": now.isoformat(),
        "platformReceipts": [{
            "platform": "x",
            "status": "failed",
            "completedAt": now.isoformat(),
        }],
    }
    source = LocalResearchSource(
        readiness_config,
        "x-alias-run",
        10,
        platform="x",
        api_platform="twitter",
        base_url=coordinator_server,
        archive_root=readiness_config.local_research_dir,
    )
    try:
        batch = source.discover(10)
    finally:
        source.close()

    scheduler = batch.receipt.metadata["scheduler"]
    assert scheduler["job_status"] == "completed"
    assert scheduler["lane_status"] == "failed"
    assert scheduler["state"] == "failed_cooldown"
    assert batch.receipt.metadata["acquisition_state"] == "failed_cooldown"


def test_archive_freshness_uses_observation_time_not_recent_file_mtime(
    coordinator_server,
    readiness_config,
):
    now = datetime.now(timezone.utc)
    old = now - timedelta(hours=4)
    config = replace(readiness_config, local_research_refresh_seconds=60)
    _write_instagram_archive(
        config.local_research_dir,
        old,
        touch_at=now,
    )
    _write_schedule_state(config, "shared-stale-job", old)
    CoordinatorStatusHandler.jobs["shared-stale-job"] = {
        "id": "shared-stale-job",
        "status": "completed",
        "completedAt": old.isoformat(),
        "platformReceipts": [{
            "platform": "instagram",
            "status": "completed",
            "completedAt": old.isoformat(),
        }],
    }
    source = _instagram_source(config, coordinator_server, "stale-lane-run")
    try:
        batch = source.discover(10)
    finally:
        source.close()

    metadata = batch.receipt.metadata
    assert metadata["scheduler"]["state"] == "stale"
    assert metadata["acquisition_state"] == "stale"
    assert metadata["archive_freshness_basis"] == "latest_observed_at"
    assert metadata["archive_fresh"] is False
    assert metadata["archive_stale_after_seconds"] == 60
    assert metadata["archive_age_seconds"] > 3 * 3600
    assert metadata["artifact_age_seconds"] < 60
    assert metadata["latest_observed_at"].startswith(old.date().isoformat())
    assert metadata["latest_artifact_at"].startswith(now.date().isoformat())
