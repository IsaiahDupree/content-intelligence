"""Real-SQLite contract for the unified read-only intelligence surface."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timedelta, timezone
from time import perf_counter

from flask import Flask

from services.market_tape.api import register_market_tape_routes
from services.market_tape.config import MarketTapeConfig
from services.market_tape.intelligence import build_intelligence_snapshot
from services.market_tape.predictor import ENTRY_HORIZON
from services.market_tape.store import MarketTapeStore


def test_empty_tape_snapshot_is_observed_only_and_lineage_complete(tmp_path):
    config = MarketTapeConfig(
        db_path=tmp_path / "market.sqlite3",
        object_dir=tmp_path / "objects",
        heartbeat_path=tmp_path / "heartbeat.json",
        lock_path=tmp_path / "market.lock",
        local_research_state_path=tmp_path / "local-research-state.json",
        prediction_model_dir=tmp_path / "models",
        dataset_root=tmp_path / "passport-datasets",
        passport_mount=tmp_path,
        platforms=["youtube"],
        topics=["measured trend"],
        supabase_sync_enabled=False,
    )
    store = MarketTapeStore(config)

    snapshot = build_intelligence_snapshot(
        config,
        store,
        limit=5,
        window_hours=24,
        minimum_videos=2,
    )

    assert snapshot["contract"] == "market_tape_intelligence_snapshot_v1"
    assert snapshot["state"] == "observed_only"
    assert snapshot["read_only"] is True
    assert snapshot["parameters"] == {
        "limit": 5,
        "window_hours": 24,
        "minimum_videos": 2,
    }
    assert snapshot["lineage"]["live_database_path"] == str(config.db_path)
    assert snapshot["lineage"]["passport_dataset"]["state"] == "not_run"
    assert snapshot["keywords"]["derived_terms"] == []
    assert snapshot["keywords"]["exact_discovery_queries"] == []
    assert snapshot["forecast"]["probability_admitted"] is False
    assert snapshot["forecast"]["admission"]["admission_reason"] == (
        "no_active_model"
    )
    assert snapshot["trends"]["score_is_probability"] is False
    assert snapshot["performance"]["contract"] == (
        "market_tape_intelligence_performance_v1"
    )
    assert snapshot["performance"]["bounded_inputs"] == {
        "keyword_source_rows": 5000,
        "opportunity_candidate_rows": 500,
    }


def test_intelligence_endpoint_is_bounded_with_active_prediction_history(tmp_path):
    config = _config(tmp_path)
    store = MarketTapeStore(config)
    model_version = _write_active_model(config)
    observed = datetime.now(timezone.utc).isoformat()
    trend_count = 2_500
    with store.connect() as connection:
        connection.executemany(
            """INSERT INTO mt_trends(
                   trend_id, trend_type, canonical_key, display_name, status,
                   first_seen_at, last_seen_at
               ) VALUES(?, 'topic', ?, ?, 'emerging', ?, ?)""",
            [
                (
                    f"scale-trend-{index}",
                    f"scale-trend-{index}",
                    f"Measured workflow {index} adoption",
                    observed,
                    observed,
                )
                for index in range(trend_count)
            ],
        )
        connection.executemany(
            """INSERT INTO mt_trend_observations(
                   trend_id, observed_at, videos_total, videos_new_1h,
                   creators_total, creators_new_1h, platforms_total, views_total,
                   likes_total, comments_total, shares_total, views_new_1h,
                   likes_new_1h, comments_new_1h, shares_new_1h,
                   counter_delta_videos, activity_coverage,
                   median_video_velocity, p90_video_velocity, creator_breadth,
                   platform_breadth, top1_concentration, top10_concentration,
                   momentum, acceleration, relative_strength, saturation,
                   trend_strength, index_version, state
               ) VALUES(?, ?, 3, 2, 3, 2, 1, 10000, 1000, 100, 50,
                        500, 50, 5, 2, 2, 0.8, 1.0, 2.0, 0.8, 0.4,
                        0.4, 0.7, 1.0, 0.5, 1.2, 0.2, ?,
                        'trend-strength-v2', 'emerging')""",
            [
                (f"scale-trend-{index}", observed, 60.0)
                for index in range(trend_count)
            ],
        )
        connection.executemany(
            """INSERT INTO mt_predictions(
                   subject_type, subject_id, model_version, predicted_at,
                   horizon, probability, expected_peak_at,
                   expected_remaining_life_hours, features_json
               ) VALUES('trend', ?, ?, ?, ?, ?, ?, 12.0, '{}')""",
            [
                (
                    f"scale-trend-{index}",
                    model_version,
                    observed,
                    ENTRY_HORIZON,
                    0.99 if index % 2 else 0.01,
                    observed,
                )
                for index in range(trend_count)
            ],
        )

    app = Flask(__name__)
    register_market_tape_routes(app, config)
    started = perf_counter()
    response = app.test_client().get(
        "/api/market-tape/intelligence?limit=5&window_hours=24&min_videos=2"
    )
    elapsed = perf_counter() - started

    assert response.status_code == 200
    payload = response.get_json()
    assert payload["contract"] == "market_tape_intelligence_snapshot_v1"
    assert payload["lineage"]["live_totals"]["trends"] == trend_count
    assert payload["trends"]["candidates_considered"] == trend_count
    assert payload["trends"]["coarse_eligible_candidates"] == trend_count
    assert payload["trends"]["candidate_rows_loaded"] == 500
    assert payload["trends"]["candidate_scan_truncated"] is True
    expected_loaded = sorted(
        (f"scale-trend-{index}" for index in range(trend_count))
    )[:500]
    assert payload["trends"]["candidate_preselection"] == {
        "model_neutral": True,
        "order": [
            "trend_strength_desc",
            "videos_total_desc",
            "observed_at_desc",
            "trend_id_asc",
        ],
        "maximum_loaded_trend_strength": 60.0,
        "minimum_loaded_trend_strength": 60.0,
        "first_loaded_trend_id": expected_loaded[0],
        "last_loaded_trend_id": expected_loaded[-1],
    }
    assert payload["trends"]["ranking_scope"] == (
        "bounded_top_strength_candidates"
    )
    assert payload["trends"]["filters"]["candidate_scan_limit"] == 500
    assert payload["performance"]["elapsed_ms"] <= elapsed * 1000.0
    # The regression runs from Passport-backed TMPDIR on low-disk hosts, so
    # retain a cold-cache margin while staying safely below the 30s API timeout.
    assert payload["performance"]["elapsed_ms"] < 20_000
    assert elapsed < 20.0


def test_latest_ineligible_observation_never_falls_back_to_older_eligible_row(
    tmp_path,
):
    config = _config(tmp_path)
    store = MarketTapeStore(config)
    observed = datetime.now(timezone.utc)
    with store.connect() as connection:
        connection.execute(
            """INSERT INTO mt_trends(
                   trend_id, trend_type, canonical_key, display_name, status,
                   first_seen_at, last_seen_at
               ) VALUES('latest-state-trend', 'topic', 'latest-state-trend',
                        'Measured workflow adoption', 'emerging', ?, ?)""",
            (observed.isoformat(), observed.isoformat()),
        )
        for observed_at, saturation, strength in (
            (observed - timedelta(minutes=10), 0.2, 65.0),
            (observed - timedelta(minutes=5), 0.95, 68.0),
        ):
            connection.execute(
                """INSERT INTO mt_trend_observations(
                       trend_id, observed_at, videos_total, videos_new_1h,
                       creators_total, creators_new_1h, platforms_total,
                       views_total, likes_total, comments_total, shares_total,
                       views_new_1h, likes_new_1h, comments_new_1h,
                       shares_new_1h, counter_delta_videos, activity_coverage,
                       median_video_velocity, p90_video_velocity,
                       creator_breadth, platform_breadth, top1_concentration,
                       top10_concentration, momentum, acceleration,
                       relative_strength, saturation, trend_strength,
                       index_version, state
                   ) VALUES('latest-state-trend', ?, 3, 2, 3, 2, 1,
                            10000, 1000, 100, 50, 500, 50, 5, 2, 2, 0.8,
                            1.0, 2.0, 0.8, 0.4, 0.4, 0.7, 1.0, 0.5,
                            1.2, ?, ?, 'trend-strength-v2', 'emerging')""",
                (observed_at.isoformat(), saturation, strength),
            )

    result = store.trend_opportunities(
        limit=5,
        max_saturation=0.75,
        min_videos=2,
        min_measured_videos=2,
    )

    assert result["candidates_considered"] == 1
    assert result["coarse_eligible_candidates"] == 0
    assert result["candidate_rows_loaded"] == 0
    assert result["candidate_scan_truncated"] is False
    assert result["suppressed_by_reason"]["above_saturation_ceiling"] == 1
    assert result["opportunities"] == []


def _config(tmp_path):
    return MarketTapeConfig(
        db_path=tmp_path / "market.sqlite3",
        object_dir=tmp_path / "objects",
        heartbeat_path=tmp_path / "heartbeat.json",
        lock_path=tmp_path / "market.lock",
        local_research_state_path=tmp_path / "local-research-state.json",
        prediction_model_dir=tmp_path / "models",
        dataset_root=tmp_path / "passport-datasets",
        passport_mount=tmp_path,
        prediction_min_backtest_labels=20,
        prediction_min_positive_labels=2,
        platforms=["youtube"],
        topics=["measured trend"],
        supabase_sync_enabled=False,
    )


def _write_active_model(config):
    model_version = "intelligence-latency-model-v1"
    artifact = {
        "contract": "market_tape_trend_predictor_v1",
        "status": "promoted",
        "model_family": "early-breakout-logistic-v3",
        "model_purpose": "early_breakout_entry",
        "model_version": model_version,
        "training_dataset_sha256": "b" * 64,
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
