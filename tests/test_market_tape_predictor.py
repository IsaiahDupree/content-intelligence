"""Trend model training uses measured labels and durable model receipts."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

from services.market_tape.config import MarketTapeConfig
from services.market_tape.models import MarketContent, MetricCounters
from services.market_tape.predictor import (
    ENTRY_HORIZON,
    MarketTapePredictor,
    load_active_model,
    predict_probability,
)
from services.market_tape.store import MarketTapeStore


def test_grouped_logistic_candidate_is_promoted_and_reproducible(tmp_path):
    config = _config(tmp_path)
    store = MarketTapeStore(config)
    _insert_labeled_predictions(store, 120, positive_every=10)

    first = MarketTapePredictor(config, store).train()
    second = MarketTapePredictor(config, store).train()
    active = load_active_model(config)

    assert first["status"] == "promoted"
    assert first["model_purpose"] == "early_breakout_entry"
    assert first["training"]["horizon"] == ENTRY_HORIZON
    assert first["cross_validation"]["brier_skill_score"] > 0.05
    assert first["cross_validation"]["roc_auc"] >= 0.65
    assert second["operation"] == "unchanged"
    assert active is not None
    assert active["model_version"] == first["model_version"]
    assert predict_probability(active, _positive_features()) > predict_probability(
        active,
        _negative_features(),
    )
    status = MarketTapePredictor(config, store).status()
    assert status["state"] == "active"
    assert len(status["models"]) == 1

    observed = datetime.now(timezone.utc)
    store.start_run("live-run", "discovery")
    store.ingest(MarketContent(
        platform="youtube",
        external_id="live-video",
        creator_external_id="live-creator",
        published_at=observed - timedelta(hours=1),
        observed_at=observed,
        source_id="integration-provider",
        metrics=MetricCounters(views=1000, likes=100, comments=20, shares=10),
        title="Measured trend breakout evidence",
        raw_payload={"id": "live-video", "views": 1000},
    ), "live-run")
    store.aggregate_trends(run_id="live-run")
    store.create_predictions("live-run")
    assert any(
        prediction["model_version"] == active["model_version"]
        for prediction in store.list_predictions(100, "trend")
    )
    forecast = store.forecast_active_trends(observed, limit=100)
    assert forecast["state"] == "completed"
    assert forecast["predictions_added"] > 0
    assert forecast["outbox_records"] == forecast["predictions_added"]

    _insert_opportunity_fixture(store, active["model_version"], observed)
    opportunities = store.trend_opportunities(
        limit=20,
        min_videos=1,
        min_measured_videos=1,
    )
    assert opportunities["state"] == "ready"
    assert opportunities["score_is_probability"] is False
    assert round(sum(opportunities["ranking_weights"].values()), 6) == 1.0
    assert "evidence_reliability" in (
        opportunities["opportunities"][0]["ranking_components"]
    )
    assert opportunities["opportunities"][0]["platform_distribution"] == {
        "youtube": 1
    }
    assert opportunities["opportunities"][0]["representative_content"][0][
        "external_id"
    ] == "live-video"
    assert opportunities["opportunities"][0]["display_name"] == "Caitlin Clark comeback"
    assert all(
        row["display_name"] not in {"#fyp", "Video short"}
        for row in opportunities["opportunities"]
    )
    assert opportunities["suppressed_by_reason"]["format_aggregate"] >= 1
    assert opportunities["suppressed_by_reason"]["generic_distribution_label"] >= 1


def test_insufficient_predictor_remains_retestable_without_promotion(tmp_path):
    config = _config(tmp_path)
    store = MarketTapeStore(config)
    _insert_labeled_predictions(store, 20, positive_every=10)

    result = MarketTapePredictor(config, store).train()

    assert result["status"] == "collecting_labels"
    assert result["retestable"] is True
    assert load_active_model(config) is None
    assert (config.prediction_model_dir / f"{result['model_version']}.json").is_file()


def test_opportunity_ranker_remains_available_without_a_promoted_model(tmp_path):
    config = _config(tmp_path)
    store = MarketTapeStore(config)
    observed = datetime.now(timezone.utc)
    store.start_run("deterministic-opportunity-run", "discovery")
    store.ingest(MarketContent(
        platform="youtube",
        external_id="deterministic-opportunity-video",
        creator_external_id="deterministic-opportunity-creator",
        published_at=observed - timedelta(minutes=20),
        observed_at=observed,
        source_id="integration-provider",
        metrics=MetricCounters(views=5000, likes=400, comments=40, shares=20),
        title="Caitlin Clark comeback analysis",
        raw_payload={"id": "deterministic-opportunity-video", "views": 5000},
    ), "deterministic-opportunity-run")
    store.aggregate_trends(run_id="deterministic-opportunity-run")

    opportunities = store.trend_opportunities(
        limit=20,
        min_videos=1,
        min_measured_videos=1,
    )

    assert opportunities["state"] == "ready"
    assert opportunities["active_model"] is None
    assert opportunities["ranking_weights"]["model_probability"] == 0
    assert opportunities["opportunities"]
    assert all(
        row["prediction"] is None for row in opportunities["opportunities"]
    )


def test_early_entry_horizon_rejects_already_hot_baselines(tmp_path):
    config = _config(tmp_path)
    store = MarketTapeStore(config)
    base = datetime(2026, 8, 10, tzinfo=timezone.utc)
    with store.connect() as connection:
        _insert_trend_observation(
            connection, "already-hot", "Already hot", "topic", base,
            state="breakout", strength=82.0, saturation=0.2, videos=12,
        )
        _insert_trend_observation(
            connection, "already-hot", "Already hot", "topic",
            base + timedelta(hours=6), state="breakout", strength=85.0,
            saturation=0.3, videos=18,
        )
        _insert_trend_observation(
            connection, "new-entry", "New entry", "topic", base,
            state="emerging", strength=58.0, saturation=0.1, videos=4,
        )
        _insert_trend_observation(
            connection, "new-entry", "New entry", "topic",
            base + timedelta(hours=6), state="breakout", strength=76.0,
            saturation=0.2, videos=15,
        )
        for trend_id in ("already-hot", "new-entry"):
            connection.execute(
                """INSERT INTO mt_predictions(
                       subject_type, subject_id, model_version, predicted_at,
                       horizon, probability, expected_peak_at,
                       expected_remaining_life_hours, features_json
                   ) VALUES('trend', ?, 'transparent-entry-baseline-v2', ?, ?,
                            0.5, ?, 12.0, ?)""",
                (
                    trend_id,
                    base.isoformat(),
                    ENTRY_HORIZON,
                    (base + timedelta(hours=6)).isoformat(),
                    json.dumps({"state": "emerging", "trend_strength": 58.0}),
                ),
            )

    result = store.evaluate_predictions(base + timedelta(hours=7))
    predictions = {
        row["subject_id"]: row for row in store.list_predictions(10, "trend")
    }

    assert result["newly_labeled"] == 2
    assert predictions["already-hot"]["outcome"]["state"] == "unscorable"
    assert predictions["already-hot"]["outcome"]["reason"] == (
        "already_breakout_at_prediction"
    )
    assert predictions["new-entry"]["outcome"]["state"] == "scored"
    assert predictions["new-entry"]["outcome"]["actual"] == 1


def _config(tmp_path):
    return MarketTapeConfig(
        db_path=tmp_path / "market.sqlite3",
        object_dir=tmp_path / "objects",
        heartbeat_path=tmp_path / "heartbeat.json",
        lock_path=tmp_path / "market.lock",
        local_research_state_path=tmp_path / "local-research-state.json",
        prediction_model_dir=tmp_path / "models",
        prediction_min_backtest_labels=100,
        prediction_min_positive_labels=10,
        platforms=["youtube"],
        topics=["measured trend"],
        supabase_sync_enabled=False,
    )


def _insert_labeled_predictions(store, count, positive_every):
    started = datetime(2026, 8, 1, tzinfo=timezone.utc)
    with store.connect() as connection:
        for index in range(count):
            actual = int(index % positive_every == 0)
            features = _positive_features() if actual else _negative_features()
            predicted_at = (started + timedelta(minutes=index)).isoformat()
            connection.execute(
                """INSERT INTO mt_predictions(
                       subject_type, subject_id, model_version, predicted_at,
                       horizon, probability, expected_peak_at,
                       expected_remaining_life_hours, features_json, outcome_json
                   ) VALUES('trend', ?, 'transparent-baseline-v1', ?,
                            'reaches_breakout_within_6h', 0.5, ?, 12.0, ?, ?)""",
                (
                    f"trend-{index}",
                    predicted_at,
                    (started + timedelta(hours=6, minutes=index)).isoformat(),
                    json.dumps(features, sort_keys=True),
                    json.dumps({"state": "scored", "actual": actual}, sort_keys=True),
                ),
            )


def _insert_opportunity_fixture(store, model_version, observed):
    with store.connect() as connection:
        source_video_id = connection.execute(
            "SELECT video_id FROM mt_videos WHERE external_id = 'live-video'"
        ).fetchone()[0]
        fixtures = (
            ("specific-opportunity", "Caitlin Clark comeback", "topic", 0.82),
            ("generic-opportunity", "#fyp", "hashtag", 0.99),
            ("format-opportunity", "Video short", "format", 0.99),
        )
        for trend_id, display_name, trend_type, probability in fixtures:
            _insert_trend_observation(
                connection,
                trend_id,
                display_name,
                trend_type,
                observed,
                state="emerging",
                strength=62.0,
                saturation=0.2,
                videos=10,
            )
            connection.execute(
                """INSERT INTO mt_predictions(
                       subject_type, subject_id, model_version, predicted_at,
                       horizon, probability, expected_peak_at,
                       expected_remaining_life_hours, features_json
                   ) VALUES('trend', ?, ?, ?, ?, ?, ?, 12.0, '{}')""",
                (
                    trend_id,
                    model_version,
                    observed.isoformat(),
                    ENTRY_HORIZON,
                    probability,
                    (observed + timedelta(hours=2)).isoformat(),
                ),
            )
            if trend_id == "specific-opportunity":
                connection.execute(
                    """INSERT INTO mt_trend_memberships(
                           trend_id, video_id, confidence, evidence_json,
                           first_seen_at
                       ) VALUES(?, ?, 0.95, '{}', ?)""",
                    (trend_id, source_video_id, observed.isoformat()),
                )


def _insert_trend_observation(
    connection,
    trend_id,
    display_name,
    trend_type,
    observed,
    *,
    state,
    strength,
    saturation,
    videos,
):
    connection.execute(
        """INSERT OR IGNORE INTO mt_trends(
               trend_id, trend_type, canonical_key, display_name, status,
               first_seen_at, last_seen_at
           ) VALUES(?, ?, ?, ?, ?, ?, ?)""",
        (
            trend_id,
            trend_type,
            trend_id,
            display_name,
            state,
            observed.isoformat(),
            observed.isoformat(),
        ),
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
               relative_strength, saturation, trend_strength, index_version, state
           ) VALUES(?, ?, ?, 2, 8, 2, 3, 100000, 10000, 1000, 500,
                    1000, 100, 10, 5, 2, 0.5,
                    2.0, 4.0, 0.8, 0.5, 0.2, 0.6, 1.2, 1.0, 1.5,
                    ?, ?, 'trend-strength-v2', ?)""",
        (
            trend_id,
            observed.isoformat(),
            videos,
            saturation,
            strength,
            state,
        ),
    )


def _positive_features():
    return {
        "index_version": "trend-strength-v2",
        "trend_strength": 66.0,
        "relative_strength": 2.5,
        "momentum": 2.0,
        "acceleration": 1.5,
        "creator_breadth": 0.85,
        "platform_breadth": 0.8,
        "saturation": 0.55,
    }


def _negative_features():
    return {
        "index_version": "trend-strength-v2",
        "trend_strength": 40.0,
        "relative_strength": 0.2,
        "momentum": 0.1,
        "acceleration": 0.0,
        "creator_breadth": 0.5,
        "platform_breadth": 0.2,
        "saturation": 0.01,
    }
