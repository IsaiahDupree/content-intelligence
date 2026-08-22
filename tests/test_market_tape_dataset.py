"""Daily dataset certification uses real SQLite, gzip, and filesystem artifacts."""

from __future__ import annotations

import fcntl
import gzip
import hashlib
import json
import os
import sqlite3
import subprocess
import sys
import threading
import time
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest
from flask import Flask

from services.market_tape.api import register_market_tape_routes
from services.market_tape.config import MarketTapeConfig
from services.market_tape.dataset import (
    DatasetSnapshotIntegrityError,
    MarketTapeDatasetManager,
)
from services.market_tape.models import MarketContent, MetricCounters, QueryAttempt
from services.market_tape.store import MarketTapeStore


def test_certifier_reuses_recent_prior_day_manifest_via_real_loopback_api(tmp_path):
    target = (datetime.now(timezone.utc) - timedelta(days=1)).date().isoformat()
    manifest = tmp_path / "certification.json"
    manifest_payload = {
        "contract": "market_tape_daily_dataset_v1",
        "state": "partial",
        "dataset_date": target,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "certification_id": "recent-certification",
        "manifest_path": str(manifest),
    }
    manifest.write_text(json.dumps(manifest_payload), encoding="utf-8")
    calls = {"get": 0, "post": 0}

    class CertificationStatusHandler(BaseHTTPRequestHandler):
        def log_message(self, _format, *_args):
            return

        def do_GET(self):
            calls["get"] += 1
            payload = {
                **manifest_payload,
                "manifest_available": True,
            }
            body = json.dumps(payload).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_POST(self):
            calls["post"] += 1
            self.send_response(500)
            self.end_headers()

    server = ThreadingHTTPServer(("127.0.0.1", 0), CertificationStatusHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    receipt = tmp_path / "receipt.json"
    env = {
        **os.environ,
        "MARKET_TAPE_API_BASE_URL": f"http://127.0.0.1:{server.server_port}",
        "MARKET_TAPE_DATASET_RECEIPT_PATH": str(receipt),
        "MARKET_TAPE_DATASET_MIN_RECERTIFY_SECONDS": "21600",
        "MARKET_TAPE_DATASET_FORCE_RECERTIFY": "false",
        "MARKET_TAPE_PYTHON_BIN": sys.executable,
    }
    try:
        result = subprocess.run(
            ["/bin/zsh", "scripts/run_market_tape_certifier.sh"],
            cwd=Path(__file__).resolve().parents[1],
            env=env,
            capture_output=True,
            text=True,
            timeout=20,
            check=False,
        )
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)

    assert result.returncode == 0, result.stderr
    assert "certification skipped" in result.stdout
    assert calls == {"get": 1, "post": 0}
    assert json.loads(receipt.read_text(encoding="utf-8")) == {
        **manifest_payload,
        "manifest_available": True,
    }


def test_query_attempts_cover_empty_results_and_attributions_are_semantic(tmp_path):
    config = _config(tmp_path)
    store = MarketTapeStore(config)
    observed = datetime.now(timezone.utc) - timedelta(hours=1)
    store.start_run("query-run", "discovery")
    first = _content(observed, 100)
    second = _content(observed + timedelta(minutes=5), 180)
    first.discovery_context = {"query": "current market event", "surface": "search"}
    second.discovery_context = {"query": "current market event", "surface": "search"}
    assert store.ingest(first, "query-run")[0] is True
    assert store.ingest(second, "query-run")[0] is True
    inserted = store.save_query_attempts([
        QueryAttempt(
            run_id="query-run",
            source_id="browser-research",
            platform="youtube",
            query="current market event",
            attempted_at=observed,
            finished_at=observed + timedelta(seconds=5),
            state="completed",
            result_count=2,
        ),
        QueryAttempt(
            run_id="query-run",
            source_id="browser-research",
            platform="youtube",
            query="zero result market event",
            attempted_at=observed,
            finished_at=observed + timedelta(seconds=5),
            state="empty",
            result_count=0,
        ),
    ])
    store.finish_run("query-run")

    with store.connect() as connection:
        attribution_count = connection.execute(
            "SELECT COUNT(*) FROM mt_discovery_attributions"
        ).fetchone()[0]
    assert inserted == 2
    assert attribution_count == 1
    attempts = store.list_query_attempts(10)
    assert {attempt["state"] for attempt in attempts} == {"completed", "empty"}


def test_daily_passport_dataset_is_recoverable_and_prediction_scored(tmp_path):
    config = _config(tmp_path)
    store = MarketTapeStore(config)
    base = datetime.now(timezone.utc) - timedelta(hours=26)

    store.start_run("baseline-run", "discovery")
    first = _content(base, 100)
    second = _content(base + timedelta(minutes=5), 220)
    first.discovery_context = {"query": "measured trend"}
    second.discovery_context = {"query": "measured trend"}
    store.ingest(first, "baseline-run")
    store.ingest(second, "baseline-run")
    store.save_query_attempts([
        QueryAttempt(
            run_id="baseline-run",
            source_id="integration-provider",
            platform="youtube",
            query="measured trend",
            attempted_at=base,
            finished_at=base + timedelta(seconds=5),
            state="completed",
            result_count=1,
        ),
        QueryAttempt(
            run_id="baseline-run",
            source_id="integration-provider",
            platform="youtube",
            query="measured trend reaction",
            attempted_at=base,
            finished_at=base + timedelta(seconds=6),
            state="empty",
            result_count=0,
            metadata={"query_family": "measured trend"},
        ),
    ])
    store.aggregate_trends(observed_at=base + timedelta(minutes=5), run_id="baseline-run")
    assert store.create_predictions(
        "baseline-run", predicted_at=base + timedelta(minutes=5)
    ) > 0
    store.finish_run("baseline-run")

    store.aggregate_trends(observed_at=base + timedelta(hours=5, minutes=40))
    store.aggregate_trends(observed_at=base + timedelta(hours=6, minutes=5))
    store.start_run("follow-up-run", "recheck")
    store.ingest(_content(base + timedelta(hours=24, minutes=10), 1_500), "follow-up-run")
    store.finish_run("follow-up-run")

    evaluation = store.evaluate_predictions(datetime.now(timezone.utc))
    assert evaluation["newly_labeled"] >= 1
    assert evaluation["scored_labels"] >= 1

    manager = MarketTapeDatasetManager(config, store)
    result = manager.certify(base.date())
    assert result["state"] in {"partial", "certified"}
    assert result["quality"]["integrity"]["sqlite_quick_check"] == "ok"
    assert result["quality"]["integrity"]["raw_objects_missing"] == 0
    assert result["quality"]["query_coverage"]["coverage_ratio"] == 1.0
    assert result["quality"]["query_coverage"]["queries_attempted"] == 2
    assert result["quality"]["collection"]["acquired"] == 1
    raw_archive = result["artifacts"]["raw_archive"]
    assert raw_archive["verification_policy"] == "content-addressed-copy-once-v2"
    assert raw_archive["destination_deep_verified"] == raw_archive["registered"]
    assert raw_archive["destination_provenance_verified"] == 0

    repeated = manager.certify(base.date())
    repeated_raw = repeated["artifacts"]["raw_archive"]
    assert repeated_raw["copied"] == 0
    assert repeated_raw["destination_deep_verified"] == 0
    assert repeated_raw["destination_provenance_verified"] == repeated_raw["registered"]
    manager._record_local_status({
        "contract": "market_tape_daily_dataset_v1",
        "state": "blocked_storage",
        "dataset_date": base.date().isoformat(),
        "checked_at": datetime.now(timezone.utc).isoformat(),
        "storage": {"state": "preflight_timeout"},
    })
    blocked_status = manager.status()
    assert blocked_status["state"] == "blocked_storage"
    assert blocked_status["latest_success"]["certification_id"] == repeated["certification_id"]
    assert blocked_status["latest_success"]["manifest_available"] is True

    manifest_path = config.dataset_root / base.date().isoformat() / result["certification_id"] / "certification.json"
    snapshot_path = manifest_path.parent / "market-tape.sqlite3.gz"
    observations_path = manifest_path.parent / "tables" / "mt_market_observations.jsonl.gz"
    assert manifest_path.is_file()
    assert snapshot_path.is_file()
    assert observations_path.is_file()
    assert json.loads(manifest_path.read_text(encoding="utf-8"))["certification_id"] == result["certification_id"]
    with gzip.open(observations_path, "rt", encoding="utf-8") as handle:
        assert len(handle.readlines()) == 3
    restored = tmp_path / "restored.sqlite3"
    with gzip.open(snapshot_path, "rb") as source, restored.open("wb") as output:
        output.write(source.read())
    connection = sqlite3.connect(restored)
    try:
        assert connection.execute("PRAGMA quick_check").fetchone()[0] == "ok"
        assert connection.execute("SELECT COUNT(*) FROM mt_query_attempts").fetchone()[0] == 2
    finally:
        connection.close()


def test_long_export_releases_operation_lock_and_uses_one_snapshot(tmp_path):
    """A real WAL writer advances while Passport-like JSONL export is active."""
    config = _config(tmp_path)
    store = MarketTapeStore(config)
    initial_rows = 12_000
    with store.connect() as connection:
        connection.execute(
            "CREATE TABLE mt_concurrency_probe (id INTEGER PRIMARY KEY, payload TEXT NOT NULL)"
        )
        connection.executemany(
            "INSERT INTO mt_concurrency_probe(id, payload) VALUES (?, ?)",
            (
                (
                    index,
                    hashlib.shake_256(str(index).encode("utf-8")).hexdigest(1024),
                )
                for index in range(initial_rows)
            ),
        )

    manager = MarketTapeDatasetManager(config, store)
    operation_lock = threading.Lock()
    result_box = {}

    def certify() -> None:
        result_box["result"] = manager.certify(
            datetime.now(timezone.utc).date(),
            operation_lock=operation_lock,
        )

    worker = threading.Thread(target=certify, name="real-dataset-certification")
    worker.start()
    deadline = time.monotonic() + 30
    observed_unlocked_export = False
    while worker.is_alive() and time.monotonic() < deadline:
        if manager.local_status_path.is_file():
            status = json.loads(manager.local_status_path.read_text(encoding="utf-8"))
            if status.get("phase") in {
                "mirroring_raw_objects",
                "exporting_tables",
                "finalizing_manifest",
            }:
                assert operation_lock.acquire(blocking=False) is True
                operation_lock.release()
                with store.connect() as connection:
                    connection.execute(
                        "INSERT INTO mt_concurrency_probe(id, payload) VALUES (?, ?)",
                        (initial_rows, "arrived-after-snapshot-pin"),
                    )
                observed_unlocked_export = True
                break
        time.sleep(0.005)
    worker.join(timeout=30)

    assert worker.is_alive() is False
    assert observed_unlocked_export is True
    result = result_box["result"]
    assert result["state"] in {"partial", "certified"}
    assert result["consistency"] == {
        "contract": "market_tape_dataset_snapshot_consistency_v1",
        "snapshot_captured_at": result["artifacts"]["sqlite_snapshot"]["captured_at"],
        "sqlite_capture": "pinned_wal_read_transaction_online_backup",
        "destination_pragmas": {
            "journal_mode": "OFF",
            "synchronous": "OFF",
            "temp_store": "MEMORY",
        },
        "table_exports_source": "captured_sqlite_snapshot",
        "quality_report_source": "captured_sqlite_snapshot",
        "raw_registry_source": "captured_sqlite_snapshot",
        "model_files_captured_before_snapshot_pin": True,
        "long_exports_hold_live_operation_lock": False,
    }

    snapshot_path = Path(result["artifacts"]["sqlite_snapshot"]["path"])
    restored = tmp_path / "concurrency-restored.sqlite3"
    with gzip.open(snapshot_path, "rb") as source, restored.open("wb") as output:
        output.write(source.read())
    snapshot_connection = sqlite3.connect(restored)
    try:
        assert snapshot_connection.execute("PRAGMA quick_check").fetchone()[0] == "ok"
        assert snapshot_connection.execute(
            "SELECT COUNT(*) FROM mt_concurrency_probe"
        ).fetchone()[0] == initial_rows
    finally:
        snapshot_connection.close()

    export_path = snapshot_path.parent / "tables" / "mt_concurrency_probe.jsonl.gz"
    with gzip.open(export_path, "rt", encoding="utf-8") as handle:
        assert sum(1 for _ in handle) == initial_rows
    with store.connect() as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM mt_concurrency_probe"
        ).fetchone()[0] == initial_rows + 1


def test_certification_lock_returns_http_409_without_clobbering_status(tmp_path):
    config = _config(tmp_path)
    manager = MarketTapeDatasetManager(config, MarketTapeStore(config))
    manager.local_status_path.write_text(json.dumps({
        "contract": "market_tape_daily_dataset_v1",
        "state": "running",
        "phase": "exporting_tables",
    }), encoding="utf-8")
    manager.certification_lock_path.parent.mkdir(parents=True, exist_ok=True)
    lock_handle = manager.certification_lock_path.open("a+", encoding="utf-8")
    fcntl.flock(lock_handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    try:
        app = Flask(__name__)
        register_market_tape_routes(app, config)
        response = app.test_client().post(
            "/api/market-tape/datasets/certify",
            json={"date": datetime.now(timezone.utc).date().isoformat()},
        )
    finally:
        fcntl.flock(lock_handle.fileno(), fcntl.LOCK_UN)
        lock_handle.close()

    assert response.status_code == 409
    assert response.get_json()["state"] == "busy"
    assert response.get_json()["busy_scope"] == "dataset_certification"
    assert json.loads(manager.local_status_path.read_text(encoding="utf-8"))["phase"] == (
        "exporting_tables"
    )


def test_unsafe_staging_pragmas_require_a_recoverable_snapshot(tmp_path):
    config = _config(tmp_path)
    store = MarketTapeStore(config)
    manager = MarketTapeDatasetManager(config, store)
    staging = config.dataset_root / ".staging" / "pragma-integrity-test"
    staging.mkdir(parents=True)
    source, captured_at = manager._pin_source_snapshot()
    try:
        capture = manager._copy_pinned_snapshot(staging, source, captured_at)
    finally:
        source.rollback()
        source.close()

    assert capture["destination_pragmas"] == {
        "journal_mode": "OFF",
        "synchronous": "OFF",
        "temp_store": "MEMORY",
    }
    assert manager._validate_snapshot(Path(capture["source_path"])) == {
        "quick_check": "ok",
        "foreign_key_errors": 0,
    }
    assert not Path(str(capture["source_path"]) + "-journal").exists()

    corrupt = staging / "corrupt.sqlite3"
    corrupt.write_bytes(b"not a sqlite database")
    with pytest.raises(DatasetSnapshotIntegrityError):
        manager._validate_snapshot(corrupt)


def _config(tmp_path):
    mount = tmp_path / "passport"
    mount.mkdir()
    return MarketTapeConfig(
        db_path=tmp_path / "market.sqlite3",
        object_dir=tmp_path / "objects",
        heartbeat_path=tmp_path / "heartbeat.json",
        lock_path=tmp_path / "market.lock",
        local_research_state_path=tmp_path / "local-research-state.json",
        prediction_model_dir=tmp_path / "models",
        passport_mount=mount,
        dataset_root=mount / "MarketTape" / "datasets",
        dataset_export_enabled=True,
        dataset_require_mounted_volume=False,
        prediction_min_backtest_labels=1,
        platforms=["youtube"],
        topics=["measured trend"],
        adaptive_topics_enabled=False,
        daily_unique_target=1,
        platform_daily_targets={"youtube": 1},
        provider_daily_request_limits={"youtube": 10},
        supabase_sync_enabled=False,
    )


def _content(observed_at, views):
    return MarketContent(
        platform="youtube",
        external_id="measured-video",
        creator_external_id="measured-creator",
        creator_handle="measured-creator",
        creator_followers=100,
        published_at=observed_at - timedelta(hours=1),
        observed_at=observed_at,
        source_id="integration-provider",
        metrics=MetricCounters(
            views=views,
            likes=max(1, views // 10),
            comments=max(1, views // 100),
            shares=max(1, views // 50),
        ),
        title="Measured trend with repeated observations",
        duration_seconds=45,
        raw_payload={"id": "measured-video", "views": views},
    )
