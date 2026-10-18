"""Durable promoted-forecast measurement contracts on real SQLite."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

from services.market_tape.config import MarketTapeConfig
from services.market_tape.predictor import (
    ENTRY_HORIZON,
    MODEL_FAMILY,
    OBSERVATION_QUALITY_CONTRACT,
)
from services.market_tape.store import (
    ACCEPTED_OBSERVATION_EVIDENCE_CONTRACT,
    MarketTapeStore,
    SCHEMA_VERSION,
)


SOURCE_ID = "youtube-data-api-v3"


def test_schema_v11_migrates_in_place_and_preserves_existing_rows(tmp_path):
    config = _config(tmp_path)
    config.db_path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(config.db_path)
    connection.executescript(
        """CREATE TABLE mt_meta(key TEXT PRIMARY KEY, value TEXT NOT NULL);
           INSERT INTO mt_meta(key, value) VALUES('schema_version', '9');
           CREATE TABLE mt_collection_runs(
               run_id TEXT PRIMARY KEY, mode TEXT NOT NULL,
               started_at TEXT NOT NULL, finished_at TEXT, state TEXT NOT NULL,
               items_seen INTEGER NOT NULL DEFAULT 0,
               observations_added INTEGER NOT NULL DEFAULT 0,
               unique_videos_added INTEGER NOT NULL DEFAULT 0,
               requests INTEGER NOT NULL DEFAULT 0,
               estimated_cost_usd REAL NOT NULL DEFAULT 0,
               error_detail TEXT NOT NULL DEFAULT ''
           );
           INSERT INTO mt_collection_runs(run_id, mode, started_at, state)
           VALUES('preserved-run', 'discovery',
                  '2026-08-22T12:00:00+00:00', 'running');"""
    )
    connection.commit()
    connection.close()

    migrated = MarketTapeStore(config)

    with migrated.connect() as connection:
        schema_version = connection.execute(
            "SELECT value FROM mt_meta WHERE key = 'schema_version'"
        ).fetchone()[0]
        tables = {
            row[0] for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            ).fetchall()
        }
        indexes = {
            row[0] for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'index'"
            ).fetchall()
        }
        preserved = connection.execute(
            "SELECT state FROM mt_collection_runs WHERE run_id = 'preserved-run'"
        ).fetchone()[0]
        assignment_foreign_keys = {
            row[2] for row in connection.execute(
                "PRAGMA foreign_key_list(mt_forecast_measurement_assignments)"
            ).fetchall()
        }
        foreign_key_errors = connection.execute(
            "PRAGMA foreign_key_check"
        ).fetchall()

    assert schema_version == str(SCHEMA_VERSION)
    assert {
        "mt_forecast_measurement_reservations",
        "mt_forecast_measurement_assignments",
    } <= tables
    assert {
        "mt_forecast_measurement_due_idx",
        "mt_forecast_measurement_usage_idx",
        "mt_forecast_measurement_cohort_idx",
        "mt_forecast_measurement_assignment_video_idx",
        "mt_forecast_measurement_assignment_trend_idx",
    } <= indexes
    assert assignment_foreign_keys == {
        "mt_forecast_measurement_reservations",
        "mt_predictions",
        "mt_trends",
        "mt_videos",
    }
    assert preserved == "running"
    assert foreign_key_errors == []


def test_validation_floor_requires_active_model_but_reservations_always_hold(
    tmp_path,
):
    store = MarketTapeStore(_config(tmp_path))
    predicted_at = datetime(2026, 8, 22, 12, tzinfo=timezone.utc)
    assert store.remaining_request_budget(
        SOURCE_ID, 10, purpose="discovery", as_of=predicted_at
    ) == 10
    assert store.remaining_request_budget(
        SOURCE_ID, 10, purpose="scheduled", as_of=predicted_at
    ) == 10

    _write_active_model(store.config)
    store.start_run("floor-run", "discovery")
    _seed_candidates(store, 1, predicted_at)
    forecast = store.forecast_active_trends(
        predicted_at,
        run_id="floor-run",
        measurement_sources=[_capability(daily_limit=10, units_per_batch=2)],
    )

    assert forecast["predictions_added"] == 1
    assert forecast["reservations"][0]["reserved_request_units"] == 2
    assert store.remaining_request_budget(
        SOURCE_ID, 10, purpose="discovery", as_of=predicted_at
    ) == 4
    assert store.remaining_request_budget(
        SOURCE_ID, 10, purpose="reservation", as_of=predicted_at
    ) == 8
    assert store.remaining_request_budget(
        SOURCE_ID, 10, purpose="forecast_terminal", as_of=predicted_at
    ) == 10

    (store.config.prediction_model_dir / "active.json").unlink()

    assert store.remaining_request_budget(
        SOURCE_ID, 10, purpose="discovery", as_of=predicted_at
    ) == 8
    assert store.remaining_request_budget(
        SOURCE_ID,
        10,
        purpose="discovery",
        as_of=predicted_at + timedelta(hours=6, seconds=1),
    ) == 10


def test_forecast_cohort_is_fail_closed_atomic_unique_and_capped_at_100(
    tmp_path,
):
    store = MarketTapeStore(_config(tmp_path))
    _write_active_model(store.config)
    predicted_at = datetime(2026, 8, 22, 12, tzinfo=timezone.utc)
    store.start_run("cohort-a", "discovery")
    store.start_run("cohort-b", "discovery")
    _seed_candidates(store, 105, predicted_at)
    _seed_candidates(
        store, 1, predicted_at, prefix="format", trend_type="format"
    )

    blocked = store.forecast_active_trends(
        predicted_at, run_id="cohort-a", measurement_sources=[]
    )
    missing_run = store.forecast_active_trends(
        predicted_at,
        run_id="missing-run",
        measurement_sources=[_capability()],
    )
    assert blocked["state"] == "blocked_measurement_capacity"
    assert blocked["predictions_added"] == 0
    assert missing_run["reason"] == "collection_run_not_found"
    assert missing_run["predictions_added"] == 0

    def admit(run_id):
        concurrent_store = MarketTapeStore(store.config)
        return concurrent_store.forecast_active_trends(
            predicted_at,
            run_id=run_id,
            measurement_sources=[_capability(
                daily_limit=10,
                batch_size=50,
                units_per_batch=2,
            )],
        )

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(admit, ("cohort-a", "cohort-b")))

    assert sum(result["predictions_added"] for result in results) == 100
    assert {result["state"] for result in results} == {
        "completed",
        "cohort_interval_active",
    }
    winner = next(result for result in results if result["state"] == "completed")
    assert winner["cohort_limit"] == 100
    assert winner["assignments_added"] == 100
    assert winner["reservations_added"] == 1
    assert winner["reservations"][0]["reserved_request_units"] == 4
    with store.connect() as connection:
        counts = dict(connection.execute(
            """SELECT
                   (SELECT COUNT(*) FROM mt_predictions
                    WHERE model_version = 'measurement-model-v1') AS predictions,
                   (SELECT COUNT(*) FROM mt_forecast_measurement_assignments)
                       AS assignments,
                   (SELECT COUNT(*) FROM mt_forecast_measurement_reservations)
                       AS reservations,
                   (SELECT COUNT(*)
                    FROM mt_forecast_measurement_assignments assignment
                    JOIN mt_trends trend ON trend.trend_id = assignment.trend_id
                    WHERE lower(trend.trend_type) = 'format')
                       AS format_assignments"""
        ).fetchone())
        reservation = dict(connection.execute(
            "SELECT * FROM mt_forecast_measurement_reservations"
        ).fetchone())
    assert counts == {
        "predictions": 100,
        "assignments": 100,
        "reservations": 1,
        "format_assignments": 0,
    }
    assert reservation["credential_fingerprint"] == "f" * 64
    assert reservation["state"] == "reserved"


def test_claim_plan_reclaim_and_complete_exact_assignments(tmp_path):
    config = _config(tmp_path, prediction_measurement_claim_ttl_seconds=120)
    store = MarketTapeStore(config)
    model_version = _write_active_model(config)
    predicted_at = datetime(2026, 8, 22, 12, tzinfo=timezone.utc)
    store.start_run("forecast-run", "discovery")
    store.start_run("claim-a", "recheck")
    store.start_run("claim-b", "recheck")
    exact_video_ids = _seed_candidates(store, 2, predicted_at)
    forecast = store.forecast_active_trends(
        predicted_at,
        run_id="forecast-run",
        measurement_sources=[_capability()],
    )
    assert forecast["predictions_added"] == 2

    legacy_trend_id = "legacy-trend"
    with store.connect() as connection:
        _insert_trend_observation(connection, legacy_trend_id, predicted_at)
        _insert_refreshable_member(
            connection, legacy_trend_id, predicted_at, "legacy"
        )
        legacy_prediction_id = int(connection.execute(
            """INSERT INTO mt_predictions(
                   subject_type, subject_id, model_version, predicted_at,
                   horizon, probability, expected_remaining_life_hours,
                   features_json
               ) VALUES('trend', ?, ?, ?, ?, 0.5, 12.0, ?)""",
            (
                legacy_trend_id,
                model_version,
                predicted_at.isoformat(),
                ENTRY_HORIZON,
                json.dumps({
                    "observation_quality_contract": (
                        OBSERVATION_QUALITY_CONTRACT
                    ),
                }),
            ),
        ).lastrowid)

    due_at = predicted_at + timedelta(hours=5, minutes=31)
    first_claim = store.claim_due_forecast_measurements(
        "claim-a", as_of=due_at, capable_source_ids=[SOURCE_ID]
    )
    competing_claim = store.claim_due_forecast_measurements(
        "claim-b", as_of=due_at, capable_source_ids=[SOURCE_ID]
    )
    assert first_claim["state"] == "claimed"
    assert first_claim["assignments_claimed"] == 2
    assert competing_claim["state"] == "nothing_due"

    plan = store.due_poll_plan(
        10,
        as_of=due_at,
        forecast_capable_platforms=["youtube"],
        forecast_capable_source_ids=[SOURCE_ID],
        claim_run_id="claim-a",
        phase="forecast_terminal",
    )
    rows = plan["polls"]["youtube"]
    assert [row["recheck_reason"] for row in rows[:2]] == [
        "reserved_active_model_forecast_terminal_coverage",
        "reserved_active_model_forecast_terminal_coverage",
    ]
    assert rows[2]["recheck_reason"] == (
        "active_model_forecast_terminal_coverage"
    )
    assert plan["receipt"]["reserved_assignments_selected"] == 2
    assert plan["receipt"]["legacy_assignments_selected"] == 1
    assert legacy_prediction_id in {
        prediction_id
        for assignment in plan["receipt"]["selected_assignments"]
        for prediction_id in assignment["coverage_prediction_ids"]
    }
    reserved_receipts = [
        assignment for assignment in plan["receipt"]["selected_assignments"]
        if assignment["recheck_reason"].startswith("reserved_")
    ]
    assert all(
        assignment["measurement_reservation_ids"]
        for assignment in reserved_receipts
    )

    reclaimed_at = due_at + timedelta(minutes=1)
    with store.connect() as connection:
        connection.execute(
            """UPDATE mt_forecast_measurement_reservations
               SET claim_expires_at = ? WHERE claim_run_id = 'claim-a'""",
            ((reclaimed_at - timedelta(seconds=1)).isoformat(),),
        )
    reclaimed = store.claim_due_forecast_measurements(
        "claim-b", as_of=reclaimed_at, capable_source_ids=[SOURCE_ID]
    )
    assert reclaimed["state"] == "claimed"
    assert reclaimed["assignments_claimed"] == 2

    completion = store.complete_forecast_measurements(
        "claim-b",
        SOURCE_ID,
        accepted_video_ids=[exact_video_ids[0]],
        error_code="provider_item_missing",
        completed_at=reclaimed_at + timedelta(seconds=10),
    )
    assert completion["state"] == "completed"
    assert completion["reservations_completed"] == 1
    assert completion["assignments_fulfilled"] == 1
    assert completion["assignments_failed"] == 1
    assert completion["reservations"][0]["state"] == "partial"
    with store.connect() as connection:
        reservation_state = connection.execute(
            "SELECT state FROM mt_forecast_measurement_reservations"
        ).fetchone()[0]
        assignment_states = dict(connection.execute(
            """SELECT state, COUNT(*)
               FROM mt_forecast_measurement_assignments GROUP BY state"""
        ).fetchall())
    assert reservation_state == "partial"
    assert assignment_states == {"failed": 1, "fulfilled": 1}


def test_reserved_exact_lane_deduplicates_a_shared_legacy_video(tmp_path):
    store = MarketTapeStore(_config(tmp_path))
    model_version = _write_active_model(store.config)
    predicted_at = datetime(2026, 8, 22, 12, tzinfo=timezone.utc)
    store.start_run("forecast-run", "discovery")
    store.start_run("claim-run", "recheck")
    shared_video_id = _seed_candidates(store, 1, predicted_at)[0]
    forecast = store.forecast_active_trends(
        predicted_at,
        run_id="forecast-run",
        measurement_sources=[_capability()],
    )
    assert forecast["predictions_added"] == 1

    legacy_trend_id = "legacy-shared-video-trend"
    with store.connect() as connection:
        _insert_trend_observation(
            connection,
            legacy_trend_id,
            predicted_at,
        )
        _insert_accepted_membership(
            connection,
            legacy_trend_id,
            shared_video_id,
            predicted_at,
        )
        legacy_prediction_id = int(connection.execute(
            """INSERT INTO mt_predictions(
                   subject_type, subject_id, model_version, predicted_at,
                   horizon, probability, expected_remaining_life_hours,
                   features_json
               ) VALUES('trend', ?, ?, ?, ?, 0.5, 12.0, ?)""",
            (
                legacy_trend_id,
                model_version,
                predicted_at.isoformat(),
                ENTRY_HORIZON,
                json.dumps({
                    "observation_quality_contract": (
                        OBSERVATION_QUALITY_CONTRACT
                    ),
                }),
            ),
        ).lastrowid)
        reserved_prediction_id = int(connection.execute(
            """SELECT prediction_id
               FROM mt_forecast_measurement_assignments"""
        ).fetchone()[0])

    plan = store.due_poll_plan(
        1,
        as_of=predicted_at + timedelta(hours=5, minutes=31),
        forecast_capable_platforms=["youtube"],
        forecast_capable_source_ids=[SOURCE_ID],
        claim_run_id="claim-run",
        phase="forecast_terminal",
    )

    rows = plan["polls"]["youtube"]
    assert len(rows) == 1
    assert rows[0]["video_id"] == shared_video_id
    assert rows[0]["preferred_source_id"] == SOURCE_ID
    assert rows[0]["recheck_reason"] == (
        "reserved_active_model_forecast_terminal_coverage"
    )
    assert {
        int(obligation["prediction_id"])
        for obligation in rows[0]["forecast_coverage"]
    } == {reserved_prediction_id, legacy_prediction_id}
    receipt = plan["receipt"]
    assert receipt["selected_total"] == 1
    assert receipt["selected_forecast_coverage"] == 1
    assert receipt["coverage_predictions_selected"] == 2
    assert receipt["reserved_assignments_selected"] == 1
    assert receipt["legacy_assignments_selected"] == 1
    assert receipt["measurement_claim"]["reserved_request_units"] == 1
    assert len(receipt["selected_assignments"]) == 1
    assert set(
        receipt["selected_assignments"][0]["coverage_prediction_ids"]
    ) == {reserved_prediction_id, legacy_prediction_id}


def test_exact_reservation_survives_active_model_replacement(tmp_path):
    store = MarketTapeStore(_config(tmp_path))
    _write_active_model(store.config)
    predicted_at = datetime(2026, 8, 22, 12, tzinfo=timezone.utc)
    store.start_run("forecast-run", "discovery")
    store.start_run("replacement-claim", "recheck")
    _seed_candidates(store, 1, predicted_at)
    forecast = store.forecast_active_trends(
        predicted_at,
        run_id="forecast-run",
        measurement_sources=[_capability()],
    )
    assert forecast["predictions_added"] == 1
    (store.config.prediction_model_dir / "active.json").unlink()

    plan = store.due_poll_plan(
        10,
        as_of=predicted_at + timedelta(hours=5, minutes=31),
        forecast_capable_platforms=["youtube"],
        forecast_capable_source_ids=[SOURCE_ID],
        claim_run_id="replacement-claim",
        phase="forecast_terminal",
    )

    assert plan["receipt"]["measurement_claim"]["state"] == "claimed"
    assert plan["receipt"]["coverage_state"] == "queued"
    assert plan["receipt"]["reserved_assignments_selected"] == 1
    assert plan["polls"]["youtube"][0]["recheck_reason"].startswith(
        "reserved_"
    )


def test_oversized_reservation_is_claimed_and_completed_in_bounded_batches(
    tmp_path,
):
    store = MarketTapeStore(_config(tmp_path))
    _write_active_model(store.config)
    predicted_at = datetime(2026, 8, 22, 12, tzinfo=timezone.utc)
    for run_id in ("forecast-run", "claim-a", "claim-b"):
        store.start_run(run_id, "recheck")
    _seed_candidates(store, 5, predicted_at)
    forecast = store.forecast_active_trends(
        predicted_at,
        run_id="forecast-run",
        measurement_sources=[_capability()],
    )
    assert forecast["predictions_added"] == 5
    assert forecast["reservations_added"] == 1
    due_at = predicted_at + timedelta(hours=5, minutes=31)

    first = store.claim_due_forecast_measurements(
        "claim-a",
        as_of=due_at,
        capable_source_ids=[SOURCE_ID],
        limit=2,
    )

    assert first["assignments_claimed"] == 2
    assert sum(
        len(assignment["video_ids"])
        for assignment in first["assignments"]
    ) == 2
    first_video_ids = first["assignments"][0]["video_ids"]
    completed = store.complete_forecast_measurements(
        "claim-a",
        SOURCE_ID,
        accepted_video_ids=first_video_ids,
        completed_at=due_at + timedelta(seconds=10),
    )
    assert completed["assignments_fulfilled"] == 2
    assert completed["reservations"][0]["state"] == "reserved"
    assert completed["reservations"][0]["assignments_remaining"] == 3
    with store.connect() as connection:
        reservation = dict(connection.execute(
            "SELECT * FROM mt_forecast_measurement_reservations"
        ).fetchone())
    assert reservation["state"] == "reserved"
    assert reservation["claim_run_id"] is None
    assert reservation["reserved_request_units"] == 1

    second = store.claim_due_forecast_measurements(
        "claim-b",
        as_of=due_at + timedelta(minutes=1),
        capable_source_ids=[SOURCE_ID],
        limit=2,
    )
    second_video_ids = second["assignments"][0]["video_ids"]
    assert second["assignments_claimed"] == 2
    assert set(first_video_ids).isdisjoint(second_video_ids)


def test_subject_cooldown_spans_model_versions_while_reservation_is_open(
    tmp_path,
):
    store = MarketTapeStore(_config(tmp_path))
    _write_active_model(store.config, "measurement-model-v1")
    predicted_at = datetime(2026, 8, 22, 12, tzinfo=timezone.utc)
    store.start_run("model-v1-run", "discovery")
    store.start_run("model-v2-run", "discovery")
    _seed_candidates(store, 1, predicted_at)
    first = store.forecast_active_trends(
        predicted_at,
        run_id="model-v1-run",
        measurement_sources=[_capability()],
    )
    assert first["predictions_added"] == 1

    _write_active_model(store.config, "measurement-model-v2")
    newer_at = predicted_at + timedelta(hours=1, minutes=2)
    with store.connect() as connection:
        _insert_trend_observation(
            connection,
            "candidate-trend-000",
            newer_at,
        )
    second = store.forecast_active_trends(
        newer_at + timedelta(minutes=1),
        run_id="model-v2-run",
        measurement_sources=[_capability()],
    )

    assert second["predictions_added"] == 0
    assert second["skipped_subject_cooldown"] == 1
    with store.connect() as connection:
        versions = {
            str(row[0]) for row in connection.execute(
                "SELECT model_version FROM mt_predictions"
            ).fetchall()
        }
    assert versions == {"measurement-model-v1"}


def test_future_day_capacity_ignores_an_undated_current_day_balance(tmp_path):
    predicted_at = datetime(2026, 8, 22, 23, tzinfo=timezone.utc)
    store = MarketTapeStore(_config(tmp_path / "future"))
    _write_active_model(store.config)
    store.start_run("future-run", "discovery")
    _seed_candidates(store, 1, predicted_at)

    future = store.forecast_active_trends(
        predicted_at,
        run_id="future-run",
        measurement_sources=[_capability(
            daily_limit=10,
            request_budget_remaining=0,
        )],
    )

    assert future["predictions_added"] == 1
    assert future["measurement_window_open_at"].startswith("2026-08-23")

    dated_store = MarketTapeStore(_config(tmp_path / "dated"))
    _write_active_model(dated_store.config)
    dated_store.start_run("dated-run", "discovery")
    _seed_candidates(dated_store, 1, predicted_at)
    dated = dated_store.forecast_active_trends(
        predicted_at,
        run_id="dated-run",
        measurement_sources=[_capability(
            daily_limit=10,
            request_budget_remaining=0,
            request_budget_date="2026-08-23",
        )],
    )
    assert dated["state"] == "blocked_measurement_capacity"
    assert dated["reason"] == "daily_measurement_capacity_unavailable"


def _config(tmp_path, **overrides):
    values = {
        "db_path": tmp_path / "market.sqlite3",
        "object_dir": tmp_path / "objects",
        "heartbeat_path": tmp_path / "heartbeat.json",
        "lock_path": tmp_path / "market.lock",
        "local_research_state_path": tmp_path / "local-research-state.json",
        "prediction_model_dir": tmp_path / "models",
        "prediction_min_backtest_labels": 20,
        "prediction_min_positive_labels": 2,
        "prediction_validation_cohort_limit": 100,
        "prediction_validation_interval_seconds": 3600,
        "prediction_validation_request_floor": 6,
        "platforms": ["youtube"],
        "topics": ["measured trend"],
        "supabase_sync_enabled": False,
    }
    values.update(overrides)
    return MarketTapeConfig(**values)


def _capability(
    *,
    daily_limit=200,
    batch_size=50,
    units_per_batch=1,
    request_budget_remaining=None,
    request_budget_date="",
):
    return {
        "state": "refresh_capable",
        "source_id": SOURCE_ID,
        "platform": "youtube",
        "daily_request_limit": daily_limit,
        "request_budget_remaining": (
            daily_limit
            if request_budget_remaining is None
            else request_budget_remaining
        ),
        "request_budget_date": request_budget_date,
        "refresh_batch_size": batch_size,
        "request_units_per_batch": units_per_batch,
        "credential_fingerprint": "f" * 64,
    }


def _write_active_model(config, model_version="measurement-model-v1"):
    artifact = {
        "contract": "market_tape_trend_predictor_v1",
        "status": "promoted",
        "model_family": MODEL_FAMILY,
        "model_purpose": "early_breakout_entry",
        "model_version": model_version,
        "training_dataset_sha256": "a" * 64,
        "training": {
            "index_version": "trend-strength-v2",
            "observation_quality_contract": OBSERVATION_QUALITY_CONTRACT,
        },
        "model": {
            "intercept": 0.0,
            "coefficients": [0.0] * 7,
            "means": [60.0, 1.0, 1.0, 0.5, 0.8, 0.5, 0.2],
            "standard_deviations": [10.0, 1.0, 1.0, 1.0, 0.2, 0.2, 0.2],
        },
    }
    config.prediction_model_dir.mkdir(parents=True, exist_ok=True)
    artifact_path = config.prediction_model_dir / f"{model_version}.json"
    artifact_path.write_text(
        json.dumps(artifact, sort_keys=True), encoding="utf-8"
    )
    artifact_sha = hashlib.sha256(artifact_path.read_bytes()).hexdigest()
    (config.prediction_model_dir / "active.json").write_text(
        json.dumps({
            "contract": "market_tape_active_predictor_v1",
            "model_version": model_version,
            "artifact_file": artifact_path.name,
            "artifact_sha256": artifact_sha,
        }, sort_keys=True),
        encoding="utf-8",
    )
    return model_version


def _seed_candidates(
    store,
    count,
    observed_at,
    *,
    prefix="candidate",
    trend_type="topic",
):
    video_ids = []
    with store.connect() as connection:
        for index in range(count):
            trend_id = f"{prefix}-trend-{index:03d}"
            _insert_trend_observation(
                connection,
                trend_id,
                observed_at - timedelta(minutes=1),
                trend_type=trend_type,
                strength=60.0 - index / 1000.0,
            )
            video_ids.append(_insert_refreshable_member(
                connection,
                trend_id,
                observed_at,
                f"{prefix}-{index:03d}",
            ))
    return video_ids


def _insert_trend_observation(
    connection,
    trend_id,
    observed_at,
    *,
    trend_type="topic",
    strength=60.0,
):
    timestamp = observed_at.isoformat()
    connection.execute(
        """INSERT OR IGNORE INTO mt_trends(
               trend_id, trend_type, canonical_key, display_name, status,
               first_seen_at, last_seen_at
           ) VALUES(?, ?, ?, ?, 'emerging', ?, ?)""",
        (trend_id, trend_type, trend_id, trend_id, timestamp, timestamp),
    )
    connection.execute(
        """INSERT INTO mt_trend_observations(
               trend_id, observed_at, videos_total, videos_new_1h,
               creators_total, creators_new_1h, platforms_total, views_total,
               likes_total, comments_total, shares_total, views_new_1h,
               likes_new_1h, comments_new_1h, shares_new_1h,
               counter_delta_videos, activity_coverage, median_video_velocity,
               p90_video_velocity, creator_breadth, platform_breadth,
               top1_concentration, top10_concentration, momentum, acceleration,
               relative_strength, saturation, trend_strength, index_version,
               observation_quality_contract, state
           ) VALUES(?, ?, 2, 2, 2, 2, 1, 10000, 1000, 100, 50,
                    1000, 100, 10, 5, 2, 0.8, 1.0, 2.0, 0.8, 0.5,
                    0.25, 0.75, 1.0, 0.5, 1.0, 0.2, ?,
                    'trend-strength-v2', ?, 'emerging')""",
        (trend_id, timestamp, strength, OBSERVATION_QUALITY_CONTRACT),
    )


def _insert_refreshable_member(
    connection,
    trend_id,
    observed_at,
    suffix,
):
    creator_id = f"youtube:creator:{suffix}"
    video_id = f"youtube:video:{suffix}"
    external_id = f"video-{suffix}"
    timestamp = observed_at.isoformat()
    connection.execute(
        """INSERT OR IGNORE INTO mt_creators(
               creator_id, platform, external_id, first_seen_at, last_seen_at
           ) VALUES(?, 'youtube', ?, ?, ?)""",
        (creator_id, f"creator-{suffix}", timestamp, timestamp),
    )
    connection.execute(
        """INSERT OR IGNORE INTO mt_videos(
               video_id, platform, external_id, creator_id, published_at,
               first_seen_at, last_seen_at, source_first_seen
           ) VALUES(?, 'youtube', ?, ?, ?, ?, ?, 'fixture')""",
        (
            video_id,
            external_id,
            creator_id,
            (observed_at - timedelta(hours=1)).isoformat(),
            timestamp,
            timestamp,
        ),
    )
    connection.execute(
        """INSERT OR REPLACE INTO mt_poll_queue(
               video_id, platform, external_id, preferred_source_id, due_at,
               last_observed_at
           ) VALUES(?, 'youtube', ?, ?, ?, ?)""",
        (
            video_id,
            external_id,
            SOURCE_ID,
            (observed_at + timedelta(days=1)).isoformat(),
            timestamp,
        ),
    )
    observation_key = f"fixture-observation:{suffix}:{timestamp}"
    raw_sha256 = hashlib.sha256(observation_key.encode("utf-8")).hexdigest()
    connection.execute(
        """INSERT OR IGNORE INTO mt_raw_objects(
               raw_sha256, object_path, bytes_compressed, first_seen_at,
               source_id
           ) VALUES(?, ?, 0, ?, ?)""",
        (
            raw_sha256,
            f"fixtures/{raw_sha256}.json",
            timestamp,
            SOURCE_ID,
        ),
    )
    connection.execute(
        """INSERT OR IGNORE INTO mt_market_observations(
               observation_key, run_id, observed_at, wall_clock_date,
               video_id, creator_id, platform, source_id, video_age_seconds,
               video_age_bucket, views, raw_sha256, source_confidence
           ) VALUES(?, 'fixture-run', ?, ?, ?, ?, 'youtube', ?, 3600,
                    '0-6h', 10000, ?, 1.0)""",
        (
            observation_key,
            timestamp,
            observed_at.date().isoformat(),
            video_id,
            creator_id,
            SOURCE_ID,
            raw_sha256,
        ),
    )
    observation_id = int(connection.execute(
        """SELECT observation_id FROM mt_market_observations
           WHERE observation_key = ?""",
        (observation_key,),
    ).fetchone()[0])
    evidence_id = hashlib.sha256(
        f"accepted:{observation_key}:full".encode("utf-8")
    ).hexdigest()
    connection.execute(
        """INSERT OR IGNORE INTO mt_accepted_observation_evidence(
               evidence_id, observation_id, observation_key, video_id,
               creator_id, accepted_at, contract, evidence_scope,
               published_at
           ) VALUES(?, ?, ?, ?, ?, ?, ?, 'full', ?)""",
        (
            evidence_id,
            observation_id,
            observation_key,
            video_id,
            creator_id,
            timestamp,
            ACCEPTED_OBSERVATION_EVIDENCE_CONTRACT,
            (observed_at - timedelta(hours=1)).isoformat(),
        ),
    )
    connection.execute(
        """INSERT OR IGNORE INTO mt_trend_memberships(
               trend_id, video_id, confidence, evidence_json, first_seen_at
           ) VALUES(?, ?, 1.0, '{}', ?)""",
        (trend_id, video_id, timestamp),
    )
    connection.execute(
        """INSERT OR IGNORE INTO mt_trend_membership_lineage(
               trend_id, video_id, observation_id, linked_at, contract
           ) VALUES(?, ?, ?, ?, ?)""",
        (
            trend_id,
            video_id,
            observation_id,
            timestamp,
            ACCEPTED_OBSERVATION_EVIDENCE_CONTRACT,
        ),
    )
    return video_id


def _insert_accepted_membership(
    connection,
    trend_id,
    video_id,
    observed_at,
):
    timestamp = observed_at.isoformat()
    evidence = connection.execute(
        """SELECT observation_id
           FROM mt_accepted_full_evidence_v1
           WHERE video_id = ?
           ORDER BY accepted_at DESC, observation_id DESC
           LIMIT 1""",
        (video_id,),
    ).fetchone()
    assert evidence is not None
    observation_id = int(evidence["observation_id"])
    connection.execute(
        """INSERT INTO mt_trend_memberships(
               trend_id, video_id, confidence, evidence_json, first_seen_at
           ) VALUES(?, ?, 1.0, '{}', ?)""",
        (trend_id, video_id, timestamp),
    )
    connection.execute(
        """INSERT INTO mt_trend_membership_lineage(
               trend_id, video_id, observation_id, linked_at, contract
           ) VALUES(?, ?, ?, ?, ?)""",
        (
            trend_id,
            video_id,
            observation_id,
            timestamp,
            ACCEPTED_OBSERVATION_EVIDENCE_CONTRACT,
        ),
    )
