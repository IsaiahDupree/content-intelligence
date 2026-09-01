"""Semantic control-plane tables are synced before legacy Market Tape rows."""

from __future__ import annotations

import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from services.market_tape.config import MarketTapeConfig
from services.market_tape.migration import (
    APPEND_ONLY_TABLES,
    MARKET_TAPE_TABLES,
    MIGRATION_PATHS,
    REQUIRED_INDEXES,
    migration_sql,
    validate_migration,
    verification_sql,
)
from services.market_tape.sinks.supabase import (
    ADAPTIVE_SPLIT_HTTP_STATUSES,
    ENTITY_SYNC_ORDER,
    ENTITY_TABLES,
    SupabaseSink,
)
from services.market_tape.store import MarketTapeStore


SEMANTIC_ENTITY_ORDER = (
    "semantic_graph_version",
    "semantic_topic_node",
    "semantic_topic_edge",
    "semantic_signal_candidate",
    "semantic_signal_binding",
    "semantic_resolution_run",
    "semantic_topic_observation",
    "semantic_atomic_selection",
    "semantic_atomic_selection_source",
    "semantic_evidence_receipt",
    "semantic_lineage_registration",
    "semantic_content_brief",
    "semantic_content_asset",
    "semantic_content_lineage",
)

UPWORK_ENTITY_ORDER = (
    "upwork_request_reservation",
    "upwork_scan_run",
    "upwork_job",
    "upwork_job_version",
    "upwork_query_observation",
    "upwork_job_observation",
    "upwork_demand_snapshot",
    "upwork_prediction",
    "upwork_prediction_outcome",
)

EXPECTED_ADVISOR_INDEXES = {
    "actp_semantic_atomic_sources_observation_idx": (
        "actp_semantic_atomic_selection_sources",
        "topic_observation_key",
    ),
    "actp_semantic_atomic_sources_signal_idx": (
        "actp_semantic_atomic_selection_sources",
        "signal_id",
    ),
    "actp_semantic_assets_graph_atomic_idx": (
        "actp_semantic_content_assets",
        "graph_version_id,atomic_topic_id",
    ),
    "actp_semantic_assets_parent_idx": (
        "actp_semantic_content_assets",
        "parent_asset_id",
    ),
    "actp_semantic_briefs_selection_idx": (
        "actp_semantic_content_briefs",
        "atomic_selection_id",
    ),
    "actp_semantic_briefs_registration_idx": (
        "actp_semantic_content_briefs",
        "registration_id",
    ),
    "actp_semantic_lineage_graph_atomic_idx": (
        "actp_semantic_content_lineage",
        "graph_version_id,atomic_topic_id",
    ),
    "actp_semantic_lineage_signal_graph_idx": (
        "actp_semantic_content_lineage",
        "signal_id,graph_version_id",
    ),
    "actp_semantic_lineage_observation_idx": (
        "actp_semantic_content_lineage",
        "topic_observation_key",
    ),
    "actp_semantic_resolution_graph_selected_idx": (
        "actp_semantic_resolution_runs",
        "graph_version_id,selected_topic_id",
    ),
    "actp_semantic_resolution_signal_graph_idx": (
        "actp_semantic_resolution_runs",
        "signal_id,graph_version_id",
    ),
    "actp_semantic_bindings_signal_graph_idx": (
        "actp_semantic_signal_bindings",
        "signal_id,graph_version_id",
    ),
    "actp_semantic_signals_source_trend_idx": (
        "actp_semantic_signal_candidates",
        "source_trend_id",
    ),
    "actp_semantic_observations_binding_fk_idx": (
        "actp_semantic_topic_observations",
        "binding_id,graph_version_id,signal_id,topic_id",
    ),
    "actp_semantic_observations_signal_graph_idx": (
        "actp_semantic_topic_observations",
        "signal_id,graph_version_id",
    ),
    "actp_upwork_reservations_usage_idx": (
        "actp_upwork_request_reservations",
        "usage_date,reserved_at",
    ),
    "actp_upwork_scans_observed_idx": (
        "actp_upwork_scan_runs",
        "observed_atdesc,scan_run_id",
    ),
    "actp_upwork_versions_job_time_idx": (
        "actp_upwork_job_versions",
        "job_id,observed_atdesc,job_version_id",
    ),
    "actp_upwork_queries_query_time_idx": (
        "actp_upwork_query_observations",
        "normalized_query,observed_atdesc,query_observation_id",
    ),
    "actp_upwork_query_observations_scan_idx": (
        "actp_upwork_query_observations",
        "scan_run_id",
    ),
    "actp_upwork_job_observations_job_time_idx": (
        "actp_upwork_job_observations",
        "job_id,observed_atdesc,job_observation_id",
    ),
    "actp_upwork_job_observations_query_idx": (
        "actp_upwork_job_observations",
        "query_observation_id",
    ),
    "actp_upwork_job_observations_version_idx": (
        "actp_upwork_job_observations",
        "job_version_id",
    ),
    "actp_upwork_snapshots_cohort_time_idx": (
        "actp_upwork_demand_snapshots",
        "cohort_type,cohort_key,observed_atdesc,demand_snapshot_id",
    ),
    "actp_upwork_predictions_cohort_time_idx": (
        "actp_upwork_predictions",
        "cohort_type,cohort_key,as_ofdesc",
    ),
    "actp_upwork_outcomes_evaluated_idx": (
        "actp_upwork_prediction_outcomes",
        "evaluated_atdesc",
    ),
    "actp_upwork_prediction_outcomes_snapshot_idx": (
        "actp_upwork_prediction_outcomes",
        "observed_snapshot_id",
    ),
    "actp_upwork_semantic_links_signal_idx": (
        "actp_upwork_semantic_links",
        "graph_version_id,signal_id,created_atdesc",
    ),
    "actp_upwork_semantic_links_signal_graph_idx": (
        "actp_upwork_semantic_links",
        "signal_id,graph_version_id",
    ),
    "actp_market_rapid_triggers_detected_idx": (
        "actp_market_rapid_trend_triggers",
        "detected_atdesc,trigger_id",
    ),
    "actp_market_rapid_triggers_trend_idx": (
        "actp_market_rapid_trend_triggers",
        "trend_id,detected_atdesc,trigger_id",
    ),
    "actp_market_rapid_triggers_baseline_idx": (
        "actp_market_rapid_trend_triggers",
        "baseline_trend_observation_key",
    ),
    "actp_market_rapid_triggers_expiry_idx": (
        "actp_market_rapid_trend_triggers",
        "expires_at,trigger_id",
    ),
    "actp_market_rapid_events_trigger_time_idx": (
        "actp_market_rapid_trend_trigger_events",
        "trigger_id,created_at,event_id",
    ),
    "actp_market_rapid_events_type_time_idx": (
        "actp_market_rapid_trend_trigger_events",
        "event_type,created_atdesc,trigger_id",
    ),
}


def test_semantic_migration_and_sink_registry_cover_the_same_control_plane():
    validation = validate_migration()
    semantic_tables = {
        table for table in MARKET_TAPE_TABLES if table.startswith("actp_semantic_")
    }

    assert validation["state"] == "ready", validation
    assert validation["tables_expected"] == len(MARKET_TAPE_TABLES)
    assert set(MARKET_TAPE_TABLES) == {
        definition[0] for definition in ENTITY_TABLES.values()
    }
    assert semantic_tables <= APPEND_ONLY_TABLES
    assert all(table in verification_sql() for table in semantic_tables)


def test_upwork_remote_snapshot_schema_uses_explicit_usd_metric_fields():
    source = MIGRATION_PATHS[-2].read_text(encoding="utf-8")

    assert "public.actp_upwork_market_jobs" in source
    assert "create table if not exists public.actp_upwork_jobs" not in source
    assert "fixed_budget_usd_coverage double precision" in source
    assert "median_fixed_budget_usd double precision" in source
    assert "hourly_rate_usd_coverage double precision" in source
    assert "median_hourly_rate_usd double precision" in source
    assert "\n  budget_coverage double precision" not in source
    assert "\n  median_budget double precision" not in source


def test_semantic_advisor_indexes_are_checked_and_cover_expected_columns():
    compact_sql = "".join(migration_sql().lower().split())

    assert REQUIRED_INDEXES == {
        index_name: table
        for index_name, (table, _columns) in EXPECTED_ADVISOR_INDEXES.items()
    }
    assert len(REQUIRED_INDEXES) == 35
    for index_name, (table, columns) in EXPECTED_ADVISOR_INDEXES.items():
        expected = (
            f"createindexifnotexists{index_name}"
            f"onpublic.{table}({columns})"
        )
        assert expected in compact_sql

    missing_name = "actp_semantic_assets_parent_idx"
    broken_sql = migration_sql().replace(missing_name, f"{missing_name}_missing", 1)
    validation = validate_migration(broken_sql)
    assert validation["state"] == "invalid"
    assert validation["missing_required_indexes"] == [missing_name]


def test_semantic_outbox_entities_are_dependency_ordered_ahead_of_legacy(tmp_path):
    config = MarketTapeConfig(
        db_path=tmp_path / "market.sqlite3",
        object_dir=tmp_path / "objects",
        heartbeat_path=tmp_path / "heartbeat.json",
        lock_path=tmp_path / "market.lock",
        local_research_state_path=tmp_path / "local-research-state.json",
        prediction_model_dir=tmp_path / "models",
    )
    store = MarketTapeStore(config)
    due_at = "2000-01-01T00:00:00Z"
    inserted_order = ("source_health", "creator", *reversed(SEMANTIC_ENTITY_ORDER))
    with store.connect() as connection:
        for index, entity_type in enumerate(inserted_order):
            connection.execute(
                """INSERT INTO mt_sync_outbox(
                       entity_type, entity_key, payload_json,
                       created_at, next_attempt_at
                   ) VALUES(?, ?, ?, ?, ?)""",
                (
                    entity_type,
                    f"record-{index}",
                    json.dumps({"record": index}),
                    due_at,
                    due_at,
                ),
            )

    pending = store.pending_outbox(100, entity_order=ENTITY_SYNC_ORDER)
    pending_types = tuple(row["entity_type"] for row in pending)

    assert ENTITY_SYNC_ORDER[: len(UPWORK_ENTITY_ORDER)] == UPWORK_ENTITY_ORDER
    semantic_start = len(UPWORK_ENTITY_ORDER)
    assert (
        ENTITY_SYNC_ORDER[
            semantic_start: semantic_start + len(SEMANTIC_ENTITY_ORDER)
        ]
        == SEMANTIC_ENTITY_ORDER
    )
    assert pending_types[: len(SEMANTIC_ENTITY_ORDER)] == SEMANTIC_ENTITY_ORDER
    assert pending_types[-2:] == ("creator", "source_health")
    assert set(ENTITY_SYNC_ORDER) == set(ENTITY_TABLES)
    assert all(ENTITY_TABLES[entity_type][2] is False for entity_type in SEMANTIC_ENTITY_ORDER)


def test_supabase_sink_splits_read_timeouts_without_losing_outbox_rows(
    tmp_path,
    monkeypatch,
):
    assert {502, 503} <= ADAPTIVE_SPLIT_HTTP_STATUSES
    successful_ids: set[str] = set()
    request_sizes: list[int] = []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):  # noqa: N802
            size = int(self.headers.get("Content-Length", "0"))
            payload = json.loads(self.rfile.read(size))
            request_sizes.append(len(payload))
            if len(payload) > 2:
                time.sleep(0.12)
            else:
                successful_ids.update(
                    str(row["request_reservation_id"]) for row in payload
                )
            try:
                self.send_response(201)
                self.send_header("Content-Length", "0")
                self.end_headers()
            except OSError:
                pass

        def log_message(self, _format, *_args):
            return

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    server.daemon_threads = True
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    monkeypatch.setenv("SUPABASE_URL", "https://example.supabase.co")
    monkeypatch.setenv("SUPABASE_SERVICE_KEY", "s" * 80)
    config = MarketTapeConfig(
        db_path=tmp_path / "market.sqlite3",
        object_dir=tmp_path / "objects",
        heartbeat_path=tmp_path / "heartbeat.json",
        lock_path=tmp_path / "market.lock",
        local_research_state_path=tmp_path / "local-research-state.json",
        prediction_model_dir=tmp_path / "models",
        request_timeout_seconds=0.03,
        supabase_sync_post_batch_size=50,
    )
    store = MarketTapeStore(config)
    due_at = "2000-01-01T00:00:00Z"
    with store.connect() as connection:
        for index in range(5):
            reservation_id = f"reservation-{index}"
            connection.execute(
                """INSERT INTO mt_sync_outbox(
                       entity_type, entity_key, payload_json,
                       created_at, next_attempt_at
                   ) VALUES('upwork_request_reservation', ?, ?, ?, ?)""",
                (
                    reservation_id,
                    json.dumps({"request_reservation_id": reservation_id}),
                    due_at,
                    due_at,
                ),
            )

    sink = SupabaseSink(
        config,
        store,
        rest_base_url=f"http://127.0.0.1:{server.server_port}",
    )
    try:
        result = sink.flush(limit=10)
    finally:
        sink.close()
        server.shutdown()
        server.server_close()
        thread.join(timeout=1)

    assert result["state"] == "ready"
    assert result["synced"] == 5
    assert result["failed"] == 0
    assert result["pending"] == 0
    assert result["dependency_blocked_by"] == ""
    assert max(request_sizes) == 5
    assert successful_ids == {f"reservation-{index}" for index in range(5)}


def test_supabase_sink_defers_child_entities_after_parent_leaf_failure(
    tmp_path,
    monkeypatch,
):
    requested_paths: list[str] = []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):  # noqa: N802
            size = int(self.headers.get("Content-Length", "0"))
            self.rfile.read(size)
            requested_paths.append(self.path)
            status = (
                503
                if "/actp_upwork_request_reservations" in self.path
                else 201
            )
            self.send_response(status)
            self.send_header("Content-Length", "0")
            self.end_headers()

        def log_message(self, _format, *_args):
            return

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    server.daemon_threads = True
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    monkeypatch.setenv("SUPABASE_URL", "https://example.supabase.co")
    monkeypatch.setenv("SUPABASE_SERVICE_KEY", "s" * 80)
    config = MarketTapeConfig(
        db_path=tmp_path / "market.sqlite3",
        object_dir=tmp_path / "objects",
        heartbeat_path=tmp_path / "heartbeat.json",
        lock_path=tmp_path / "market.lock",
        local_research_state_path=tmp_path / "local-research-state.json",
        prediction_model_dir=tmp_path / "models",
    )
    store = MarketTapeStore(config)
    due_at = "2000-01-01T00:00:00Z"
    with store.connect() as connection:
        for entity_type, entity_key, payload in (
            (
                "upwork_request_reservation",
                "reservation-1",
                {"request_reservation_id": "reservation-1"},
            ),
            (
                "upwork_scan_run",
                "scan-1",
                {
                    "scan_run_id": "scan-1",
                    "request_reservation_id": "reservation-1",
                },
            ),
            ("creator", "creator-1", {"creator_id": "creator-1"}),
        ):
            connection.execute(
                """INSERT INTO mt_sync_outbox(
                       entity_type, entity_key, payload_json,
                       created_at, next_attempt_at
                   ) VALUES(?, ?, ?, ?, ?)""",
                (
                    entity_type,
                    entity_key,
                    json.dumps(payload),
                    due_at,
                    due_at,
                ),
            )

    sink = SupabaseSink(
        config,
        store,
        rest_base_url=f"http://127.0.0.1:{server.server_port}",
    )
    try:
        result = sink.flush(limit=10)
    finally:
        sink.close()
        server.shutdown()
        server.server_close()
        thread.join(timeout=1)

    assert result["state"] == "degraded"
    assert result["synced"] == 1
    assert result["failed"] == 1
    assert result["deferred"] == 1
    assert result["pending"] == 2
    assert result["dependency_blocked_by"] == "upwork_request_reservation"
    assert result["dependency_deferred_entities"] == ["upwork_scan_run"]
    assert len(requested_paths) == 2
    assert any(
        "/actp_upwork_request_reservations" in path
        for path in requested_paths
    )
    assert not any("/actp_upwork_scan_runs" in path for path in requested_paths)
    assert any("/actp_market_creators" in path for path in requested_paths)
    with store.connect() as connection:
        attempts = [
            int(row[0])
            for row in connection.execute(
                "SELECT attempts FROM mt_sync_outbox ORDER BY outbox_id"
            ).fetchall()
        ]
        due_times = [
            str(row[0])
            for row in connection.execute(
                "SELECT next_attempt_at FROM mt_sync_outbox ORDER BY outbox_id"
            ).fetchall()
        ]
    assert attempts == [1, 0, 0]
    assert due_times[1] >= due_times[0]


def test_supabase_sink_preflights_parent_backoff_not_selected_in_batch(
    tmp_path,
    monkeypatch,
):
    requested_paths: list[str] = []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):  # noqa: N802
            size = int(self.headers.get("Content-Length", "0"))
            self.rfile.read(size)
            requested_paths.append(self.path)
            self.send_response(201)
            self.send_header("Content-Length", "0")
            self.end_headers()

        def log_message(self, _format, *_args):
            return

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    server.daemon_threads = True
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    monkeypatch.setenv("SUPABASE_URL", "https://example.supabase.co")
    monkeypatch.setenv("SUPABASE_SERVICE_KEY", "s" * 80)
    config = MarketTapeConfig(
        db_path=tmp_path / "market.sqlite3",
        object_dir=tmp_path / "objects",
        heartbeat_path=tmp_path / "heartbeat.json",
        lock_path=tmp_path / "market.lock",
        local_research_state_path=tmp_path / "local-research-state.json",
        prediction_model_dir=tmp_path / "models",
    )
    store = MarketTapeStore(config)
    due_at = "2000-01-01T00:00:00Z"
    parent_retry_at = "2099-01-01T00:00:00Z"
    with store.connect() as connection:
        for entity_type, entity_key, payload, next_attempt_at in (
            (
                "upwork_request_reservation",
                "reservation-1",
                {"request_reservation_id": "reservation-1"},
                parent_retry_at,
            ),
            (
                "upwork_scan_run",
                "scan-1",
                {
                    "scan_run_id": "scan-1",
                    "request_reservation_id": "reservation-1",
                },
                due_at,
            ),
            (
                "upwork_scan_run",
                "scan-2",
                {
                    "scan_run_id": "scan-2",
                    "request_reservation_id": "reservation-2",
                },
                due_at,
            ),
            ("creator", "creator-1", {"creator_id": "creator-1"}, due_at),
        ):
            connection.execute(
                """INSERT INTO mt_sync_outbox(
                       entity_type, entity_key, payload_json,
                       created_at, next_attempt_at
                   ) VALUES(?, ?, ?, ?, ?)""",
                (
                    entity_type,
                    entity_key,
                    json.dumps(payload),
                    due_at,
                    next_attempt_at,
                ),
            )

    sink = SupabaseSink(
        config,
        store,
        rest_base_url=f"http://127.0.0.1:{server.server_port}",
    )
    try:
        result = sink.flush(limit=10)
    finally:
        sink.close()
        server.shutdown()
        server.server_close()
        thread.join(timeout=1)

    assert result["state"] == "deferred"
    assert result["synced"] == 2
    assert result["failed"] == 0
    assert result["deferred"] == 1
    assert result["dependency_blocked_by"] == "upwork_request_reservation"
    assert result["dependency_deferred_entities"] == ["upwork_scan_run"]
    assert not any(
        "/actp_upwork_request_reservations" in path for path in requested_paths
    )
    assert any("/actp_upwork_scan_runs" in path for path in requested_paths)
    assert any("/actp_market_creators" in path for path in requested_paths)
    with store.connect() as connection:
        scans = connection.execute(
            """SELECT entity_key, attempts, next_attempt_at, synced_at
               FROM mt_sync_outbox WHERE entity_type = 'upwork_scan_run'
               ORDER BY entity_key"""
        ).fetchall()
    assert int(scans[0]["attempts"]) == 0
    assert str(scans[0]["next_attempt_at"]) >= parent_retry_at
    assert scans[0]["synced_at"] is None
    assert scans[1]["synced_at"] is not None
