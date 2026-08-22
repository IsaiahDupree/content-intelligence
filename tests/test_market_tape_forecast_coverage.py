"""Real-SQLite contracts for forecast lineage and outcome coverage."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timedelta, timezone

from services.market_tape.config import MarketTapeConfig
from services.market_tape.predictor import ENTRY_HORIZON
from services.market_tape.store import MarketTapeStore


def test_active_forecast_requires_fresh_snapshot_and_deduplicates_lineage(tmp_path):
    store = MarketTapeStore(_config(tmp_path))
    _write_active_model(store.config)
    predicted_at = datetime(2026, 8, 22, 12, 0, tzinfo=timezone.utc)
    with store.connect() as connection:
        fresh_id = _insert_trend_observation(
            connection,
            trend_id="fresh-trend",
            observed_at=predicted_at - timedelta(minutes=5),
        )
        _insert_trend_observation(
            connection,
            trend_id="stale-trend",
            observed_at=predicted_at - timedelta(minutes=31),
        )
        _insert_trend_observation(
            connection,
            trend_id="future-trend",
            observed_at=predicted_at + timedelta(minutes=10),
        )
        _insert_trend_observation(
            connection,
            trend_id="singleton-trend",
            observed_at=predicted_at - timedelta(minutes=5),
            videos=1,
            creators=1,
        )

    first = store.forecast_active_trends(predicted_at, limit=20)

    assert first["state"] == "completed"
    assert first["predictions_added"] == 1
    assert first["skipped_stale"] == 1
    assert first["skipped_insufficient_support"] == 1
    assert first["skipped_duplicate"] == 0
    assert first["source_freshness_policy"] == {
        "maximum_age_seconds": 1800.0,
        "prediction_horizon_hours": 6.0,
        "coverage_tolerance_seconds": 1800.0,
        "future_observations_allowed": False,
        "minimum_videos": 2,
        "minimum_creators": 2,
    }
    predictions = store.list_predictions(20, "trend")
    assert len(predictions) == 1
    features = predictions[0]["features"]
    assert features["source_observation_type"] == "trend_observation"
    assert features["source_observation_id"] == fresh_id
    assert features["source_observed_at"] == (
        predicted_at - timedelta(minutes=5)
    ).isoformat()
    assert features["source_observation_age_seconds"] == 300.0

    duplicate = store.forecast_active_trends(predicted_at, limit=20)

    assert duplicate["predictions_added"] == 0
    assert duplicate["skipped_stale"] == 1
    assert duplicate["skipped_insufficient_support"] == 1
    assert duplicate["skipped_duplicate"] == 1
    assert len(store.list_predictions(20, "trend")) == 1

    with store.connect() as connection:
        newer_id = _insert_trend_observation(
            connection,
            trend_id="fresh-trend",
            observed_at=predicted_at + timedelta(minutes=2),
        )
    newer = store.forecast_active_trends(
        predicted_at + timedelta(minutes=3),
        limit=20,
    )

    assert newer["predictions_added"] == 1
    assert newer["skipped_duplicate"] == 0
    assert store.list_predictions(1, "trend")[0]["features"][
        "source_observation_id"
    ] == newer_id


def test_missing_trend_coverage_closes_unscorable_after_grace(tmp_path):
    store = MarketTapeStore(_config(tmp_path))
    predicted_at = datetime(2026, 8, 20, 8, 0, tzinfo=timezone.utc)
    with store.connect() as connection:
        _insert_trend_observation(
            connection,
            trend_id="coverage-gap",
            observed_at=predicted_at,
        )
        connection.execute(
            """INSERT INTO mt_predictions(
                   subject_type, subject_id, model_version, predicted_at,
                   horizon, probability, expected_remaining_life_hours,
                   features_json
               ) VALUES('trend', 'coverage-gap', 'coverage-test-v1', ?, ?,
                        0.6, 12.0, '{}')""",
            (predicted_at.isoformat(), ENTRY_HORIZON),
        )

    during_grace = store.evaluate_predictions(
        predicted_at + timedelta(hours=7, minutes=59)
    )

    assert during_grace["pending_due"] == 1
    assert during_grace["newly_missing_future_trend_coverage"] == 0
    assert store.list_predictions(1, "trend")[0].get("outcome") is None

    closed = store.evaluate_predictions(predicted_at + timedelta(hours=8))

    assert closed["pending_due"] == 0
    assert closed["newly_unscorable"] == 1
    assert closed["newly_missing_future_trend_coverage"] == 1
    assert closed["trend_coverage_grace_hours"] == 2.0
    outcome = store.list_predictions(1, "trend")[0]["outcome"]
    assert outcome["state"] == "unscorable"
    assert outcome["reason"] == "missing_future_trend_coverage"
    assert "actual" not in outcome
    assert outcome["coverage_grace_closed_at"] == (
        predicted_at + timedelta(hours=8)
    ).isoformat()
    assert closed["scored_labels"] == 0
    assert closed["unscorable_by_reason"] == {
        "missing_future_trend_coverage": 1
    }
    assert closed["calibration"]["recorded"] == 0
    assert store.calibration_history() == []


def test_opportunity_probability_requires_exact_prospective_admission_and_freshness(
    tmp_path,
):
    store = MarketTapeStore(_config(tmp_path))
    model_version = _write_active_model(store.config)
    now = datetime.now(timezone.utc)
    with store.connect() as connection:
        _insert_trend_observation(
            connection,
            trend_id="ai-content-workflow",
            observed_at=now,
            display_name="AI content workflow adoption",
        )

    no_labels = store.trend_opportunities(limit=10, min_videos=1)

    assert no_labels["model_admission"]["admission_reason"] == (
        "no_prospective_labels"
    )
    assert no_labels["model_admission"]["admitted_for_ranking"] is False
    assert no_labels["ranking_weights"]["model_probability"] == 0.0

    historical = now - timedelta(days=2)
    with store.connect() as connection:
        for index in range(20):
            actual = int(index % 5 == 0)
            probability = 0.9 if actual else 0.1
            connection.execute(
                """INSERT INTO mt_predictions(
                       subject_type, subject_id, model_version, predicted_at,
                       horizon, probability, expected_remaining_life_hours,
                       features_json, outcome_json
                   ) VALUES('trend', ?, ?, ?, ?, ?, 12.0, '{}', ?)""",
                (
                    f"historical-{index}",
                    model_version,
                    (historical + timedelta(hours=index)).isoformat(),
                    ENTRY_HORIZON,
                    probability,
                    json.dumps({"state": "scored", "actual": actual}),
                ),
            )

    expired = store.trend_opportunities(limit=10, min_videos=1)

    assert expired["model_admission"]["prospective_validation_passed"] is True
    assert expired["model_admission"]["admission_reason"] == (
        "no_unexpired_predictions"
    )
    assert expired["model_admission"]["unexpired_predictions"] == 0
    assert expired["ranking_weights"]["model_probability"] == 0.0

    with store.connect() as connection:
        connection.execute(
            """INSERT INTO mt_predictions(
                   subject_type, subject_id, model_version, predicted_at,
                   horizon, probability, expected_remaining_life_hours,
                   features_json
               ) VALUES('trend', 'ai-content-workflow', ?, ?, ?, 0.7,
                        12.0, '{}')""",
            (model_version, now.isoformat(), ENTRY_HORIZON),
        )

    admitted = store.trend_opportunities(limit=10, min_videos=1)

    assert admitted["model_admission"]["admission_reason"] == (
        "prospective_validation_passed"
    )
    assert admitted["model_admission"]["admitted_for_ranking"] is True
    assert admitted["model_admission"]["prospective_metrics"]["labels"] == 20
    assert admitted["model_admission"]["prospective_metrics"][
        "unique_subjects"
    ] == 20
    assert admitted["model_admission"]["prospective_metrics"][
        "forecast_time_batches"
    ] == 20
    assert admitted["ranking_weights"]["model_probability"] == 0.25
    assert admitted["opportunities"][0]["prediction"]["probability"] == 0.7


def _config(tmp_path):
    return MarketTapeConfig(
        db_path=tmp_path / "market.sqlite3",
        object_dir=tmp_path / "objects",
        heartbeat_path=tmp_path / "heartbeat.json",
        lock_path=tmp_path / "market.lock",
        local_research_state_path=tmp_path / "local-research-state.json",
        prediction_model_dir=tmp_path / "models",
        prediction_min_backtest_labels=20,
        prediction_min_positive_labels=2,
        platforms=["youtube"],
        topics=["measured trend"],
        supabase_sync_enabled=False,
    )


def _write_active_model(config):
    model_version = "forecast-lineage-model-v1"
    artifact = {
        "contract": "market_tape_trend_predictor_v1",
        "status": "promoted",
        "model_family": "early-breakout-logistic-v3",
        "model_purpose": "early_breakout_entry",
        "model_version": model_version,
        "training_dataset_sha256": "a" * 64,
        "training": {"index_version": "trend-strength-v2"},
        "model": {
            "intercept": 0.0,
            "coefficients": [0.0] * 7,
            "means": [60.0, 1.0, 1.0, 0.5, 0.8, 0.5, 0.2],
            "standard_deviations": [10.0, 1.0, 1.0, 1.0, 0.2, 0.2, 0.2],
        },
    }
    config.prediction_model_dir.mkdir(parents=True, exist_ok=True)
    artifact_path = config.prediction_model_dir / f"{model_version}.json"
    artifact_path.write_text(json.dumps(artifact, sort_keys=True), encoding="utf-8")
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


def _insert_trend_observation(
    connection,
    *,
    trend_id,
    observed_at,
    display_name="Measured workflow adoption",
    videos=4,
    creators=4,
):
    connection.execute(
        """INSERT OR IGNORE INTO mt_trends(
               trend_id, trend_type, canonical_key, display_name, status,
               first_seen_at, last_seen_at
           ) VALUES(?, 'topic', ?, ?, 'emerging', ?, ?)""",
        (
            trend_id,
            trend_id,
            display_name,
            observed_at.isoformat(),
            observed_at.isoformat(),
        ),
    )
    cursor = connection.execute(
        """INSERT INTO mt_trend_observations(
               trend_id, observed_at, videos_total, videos_new_1h,
               creators_total, creators_new_1h, platforms_total, views_total,
               likes_total, comments_total, shares_total, views_new_1h,
               likes_new_1h, comments_new_1h, shares_new_1h,
               counter_delta_videos, activity_coverage, median_video_velocity,
               p90_video_velocity, creator_breadth, platform_breadth,
               top1_concentration, top10_concentration, momentum, acceleration,
               relative_strength, saturation, trend_strength, index_version,
               state
           ) VALUES(?, ?, ?, 2, ?, 2, 1, 10000, 1000, 100, 50,
                    1000, 100, 10, 5, 2, 0.8, 1.0, 2.0, 0.8, 0.5,
                    0.25, 0.75, 1.0, 0.5, 1.0, 0.2, 60.0,
                    'trend-strength-v2', 'emerging')""",
        (trend_id, observed_at.isoformat(), videos, creators),
    )
    return int(cursor.lastrowid)
