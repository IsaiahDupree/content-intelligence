"""Real-SQLite contracts for forecast lineage and outcome coverage."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timedelta, timezone

from services.market_tape.config import MarketTapeConfig
from services.market_tape.models import MarketContent, MetricCounters
from services.market_tape.predictor import (
    ENTRY_HORIZON,
    OBSERVATION_QUALITY_CONTRACT,
)
from services.market_tape.store import MarketTapeStore


def test_active_forecast_requires_fresh_snapshot_and_deduplicates_lineage(tmp_path):
    store = MarketTapeStore(_config(tmp_path))
    _write_active_model(store.config)
    predicted_at = datetime(2026, 8, 22, 12, 0, tzinfo=timezone.utc)
    run_id = "forecast-lineage-run"
    store.start_run(run_id, "discovery")
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
    _insert_refreshable_member(
        store,
        run_id=run_id,
        trend_id="fresh-trend",
        observed_at=predicted_at,
    )

    first = store.forecast_active_trends(
        predicted_at,
        limit=20,
        run_id=run_id,
        measurement_sources=[_measurement_capability()],
    )

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

    duplicate = store.forecast_active_trends(
        predicted_at,
        limit=20,
        run_id=run_id,
        measurement_sources=[_measurement_capability()],
    )

    assert duplicate["predictions_added"] == 0
    assert duplicate["state"] == "cohort_interval_active"
    assert len(store.list_predictions(20, "trend")) == 1

    with store.connect() as connection:
        newer_id = _insert_trend_observation(
            connection,
            trend_id="fresh-trend",
            observed_at=predicted_at + timedelta(hours=1, minutes=2),
        )
    newer = store.forecast_active_trends(
        predicted_at + timedelta(hours=1, minutes=3),
        limit=20,
        run_id=run_id,
        measurement_sources=[_measurement_capability()],
    )

    assert newer["predictions_added"] == 0
    assert newer["skipped_duplicate"] == 0
    assert newer["skipped_subject_cooldown"] == 1

    with store.connect() as connection:
        newer_id = _insert_trend_observation(
            connection,
            trend_id="fresh-trend",
            observed_at=predicted_at + timedelta(hours=6, minutes=4),
        )
    after_cooldown = store.forecast_active_trends(
        predicted_at + timedelta(hours=6, minutes=5),
        limit=20,
        run_id=run_id,
        measurement_sources=[_measurement_capability()],
    )

    assert after_cooldown["predictions_added"] == 1
    assert store.list_predictions(1, "trend")[0]["features"][
        "source_observation_id"
    ] == newer_id


def test_promoted_model_has_one_explicit_non_format_writer(tmp_path):
    store = MarketTapeStore(_config(tmp_path))
    model_version = _write_active_model(store.config)
    observed_at = datetime.now(timezone.utc) - timedelta(minutes=2)
    run_id = "active-writer-containment"
    store.start_run(run_id, "discovery")
    for index in range(2):
        store.ingest(MarketContent(
            platform="youtube",
            external_id=f"containment-video-{index}",
            creator_external_id=f"containment-creator-{index}",
            published_at=observed_at - timedelta(hours=1),
            observed_at=observed_at,
            source_id="integration-provider",
            metrics=MetricCounters(
                views=10_000 + index * 1_000,
                likes=1_000,
                comments=100,
                shares=50,
            ),
            title="Measured workflow adoption",
            duration_seconds=30,
            raw_payload={
                "id": f"containment-video-{index}",
                "views": 10_000 + index * 1_000,
            },
        ), run_id)
    store.aggregate_trends(observed_at=observed_at, run_id=run_id)
    with store.connect() as connection:
        _insert_trend_observation(
            connection,
            trend_id="eligible-active-control",
            observed_at=observed_at,
            display_name="Eligible active control",
        )
        video_rows = [dict(row) for row in connection.execute(
            """SELECT video.video_id, MAX(observation.observation_id)
                          AS observation_id
               FROM mt_videos video
               JOIN mt_accepted_full_evidence_v1 evidence
                 ON evidence.video_id = video.video_id
               JOIN mt_market_observations observation
                 ON observation.observation_id = evidence.observation_id
               GROUP BY video.video_id
               ORDER BY video.video_id"""
        ).fetchall()]
        connection.executemany(
            """INSERT INTO mt_trend_memberships(
                   trend_id, video_id, confidence, evidence_json, first_seen_at
               ) VALUES('eligible-active-control', ?, 1.0, '{}', ?)""",
            [
                (row["video_id"], observed_at.isoformat())
                for row in video_rows
            ],
        )
        connection.executemany(
            """INSERT INTO mt_trend_membership_lineage(
                   trend_id, video_id, observation_id, linked_at, contract
               ) VALUES('eligible-active-control', ?, ?, ?, ?)""",
            [
                (
                    row["video_id"],
                    row["observation_id"],
                    observed_at.isoformat(),
                    "market_tape_accepted_observation_evidence_v1",
                )
                for row in video_rows
            ],
        )

    baseline_count = store.create_predictions(
        run_id,
        predicted_at=observed_at + timedelta(seconds=30),
    )

    assert baseline_count > 0
    with store.connect() as connection:
        ungated = connection.execute(
            "SELECT COUNT(*) FROM mt_predictions WHERE model_version = ?",
            (model_version,),
        ).fetchone()[0]
        eligible_format_rows = connection.execute(
            """SELECT COUNT(*)
               FROM mt_trend_observations observation
               JOIN mt_trends trend ON trend.trend_id = observation.trend_id
               WHERE lower(trend.trend_type) = 'format'
                 AND observation.videos_total >= 2
                 AND observation.creators_total >= 2"""
        ).fetchone()[0]
    assert ungated == 0
    assert eligible_format_rows > 0

    forecast = store.forecast_active_trends(
        observed_at + timedelta(minutes=1),
        limit=100,
        run_id=run_id,
        measurement_sources=[_measurement_capability()],
    )

    assert forecast["predictions_added"] > 0
    with store.connect() as connection:
        active_rows = [dict(row) for row in connection.execute(
            """SELECT prediction.features_json, trend.trend_type
               FROM mt_predictions prediction
               JOIN mt_trends trend ON trend.trend_id = prediction.subject_id
               WHERE prediction.model_version = ?""",
            (model_version,),
        ).fetchall()]
    assert active_rows
    assert all(row["trend_type"] != "format" for row in active_rows)
    assert all(
        json.loads(row["features_json"])["forecast_source"]
        == "active_trend_snapshot"
        for row in active_rows
    )


def test_format_labels_do_not_contribute_to_prospective_admission(tmp_path):
    store = MarketTapeStore(_config(tmp_path))
    model_version = _write_active_model(store.config)
    now = datetime.now(timezone.utc)
    historical = now - timedelta(days=3)
    with store.connect() as connection:
        _insert_trend_observation(
            connection,
            trend_id="actionable-control",
            observed_at=now,
            display_name="Actionable control trend",
        )
        for index in range(20):
            trend_id = f"format-label-{index}"
            predicted_at = historical + timedelta(hours=index)
            _insert_trend_observation(
                connection,
                trend_id=trend_id,
                observed_at=predicted_at,
                display_name=f"Format label {index}",
                trend_type="format",
            )
            actual = int(index % 5 == 0)
            connection.execute(
                """INSERT INTO mt_predictions(
                       subject_type, subject_id, model_version, predicted_at,
                       horizon, probability, expected_remaining_life_hours,
                       features_json, outcome_json
                   ) VALUES('trend', ?, ?, ?, ?, ?, 12.0, ?, ?)""",
                (
                    trend_id,
                    model_version,
                    predicted_at.isoformat(),
                    ENTRY_HORIZON,
                    0.9 if actual else 0.1,
                    json.dumps({
                        "observation_quality_contract": (
                            OBSERVATION_QUALITY_CONTRACT
                        ),
                    }),
                    json.dumps({"state": "scored", "actual": actual}),
                ),
            )

    opportunities = store.trend_opportunities(limit=10, min_videos=1)

    admission = opportunities["model_admission"]
    assert admission["admission_reason"] == "no_prospective_labels"
    assert admission["prospective_validation_passed"] is False
    assert admission["prospective_metrics"]["labels"] == 0
    assert admission["prospective_metrics"]["positives"] == 0


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
                        0.6, 12.0, ?)""",
            (
                predicted_at.isoformat(),
                ENTRY_HORIZON,
                json.dumps({
                    "observation_quality_contract": (
                        OBSERVATION_QUALITY_CONTRACT
                    ),
                }),
            ),
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
                   ) VALUES('trend', ?, ?, ?, ?, ?, 12.0, ?, ?)""",
                (
                    f"historical-{index}",
                    model_version,
                    (historical + timedelta(hours=index)).isoformat(),
                    ENTRY_HORIZON,
                    probability,
                    json.dumps({
                        "observation_quality_contract": (
                            OBSERVATION_QUALITY_CONTRACT
                        ),
                    }),
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
                        12.0, ?)""",
            (
                model_version,
                now.isoformat(),
                ENTRY_HORIZON,
                json.dumps({
                    "observation_quality_contract": (
                        OBSERVATION_QUALITY_CONTRACT
                    ),
                }),
            ),
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
    trend_type="topic",
    videos=4,
    creators=4,
):
    connection.execute(
        """INSERT OR IGNORE INTO mt_trends(
               trend_id, trend_type, canonical_key, display_name, status,
               first_seen_at, last_seen_at
           ) VALUES(?, ?, ?, ?, 'emerging', ?, ?)""",
        (
            trend_id,
            trend_type,
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
               observation_quality_contract, state
           ) VALUES(?, ?, ?, 2, ?, 2, 1, 10000, 1000, 100, 50,
                    1000, 100, 10, 5, 2, 0.8, 1.0, 2.0, 0.8, 0.5,
                    0.25, 0.75, 1.0, 0.5, 1.0, 0.2, 60.0,
                    'trend-strength-v2', ?, 'emerging')""",
        (
            trend_id,
            observed_at.isoformat(),
            videos,
            creators,
            OBSERVATION_QUALITY_CONTRACT,
        ),
    )
    return int(cursor.lastrowid)


def _measurement_capability(
    source_id="integration-provider",
    *,
    daily_limit=200,
    batch_size=50,
    units_per_batch=1,
):
    return {
        "state": "refresh_capable",
        "source_id": source_id,
        "platform": "youtube",
        "daily_request_limit": daily_limit,
        "request_budget_remaining": daily_limit,
        "refresh_batch_size": batch_size,
        "request_units_per_batch": units_per_batch,
        "credential_fingerprint": "a" * 64,
    }


def _insert_refreshable_member(
    store,
    *,
    run_id,
    trend_id,
    observed_at,
    suffix="fresh",
    source_id="integration-provider",
):
    item = MarketContent(
        platform="youtube",
        external_id=f"video-{suffix}",
        creator_external_id=f"creator-{suffix}",
        published_at=observed_at - timedelta(hours=1),
        observed_at=observed_at,
        source_id=source_id,
        metrics=MetricCounters(views=10_000, likes=1_000, comments=100, shares=50),
        title="Measured workflow adoption",
        duration_seconds=30,
        raw_payload={"id": f"video-{suffix}", "views": 10_000},
    )
    store.ingest(item, run_id)
    timestamp = observed_at.isoformat()
    with store.connect() as connection:
        observation_id = int(connection.execute(
            """SELECT observation_id FROM mt_market_observations
               WHERE observation_key = ?""",
            (item.observation_key,),
        ).fetchone()[0])
        connection.execute(
            """INSERT OR IGNORE INTO mt_trend_memberships(
                   trend_id, video_id, confidence, evidence_json, first_seen_at
               ) VALUES(?, ?, 1.0, '{}', ?)""",
            (trend_id, item.video_id, timestamp),
        )
        connection.execute(
            """INSERT OR IGNORE INTO mt_trend_membership_lineage(
                   trend_id, video_id, observation_id, linked_at, contract
               ) VALUES(?, ?, ?, ?, ?)""",
            (
                trend_id,
                item.video_id,
                observation_id,
                timestamp,
                "market_tape_accepted_observation_evidence_v1",
            ),
        )
    return item.video_id
