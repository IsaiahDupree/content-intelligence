"""Real SQLite/HTTP contracts for active-model forecast coverage rechecks."""

from __future__ import annotations

import hashlib
import json
import threading
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

import pytest

from services.market_tape.collector import MarketTapeCollector
from services.market_tape.config import MarketTapeConfig
from services.market_tape.models import MarketContent, MetricCounters
from services.market_tape.predictor import (
    ENTRY_HORIZON,
    OBSERVATION_QUALITY_CONTRACT,
)
from services.market_tape.sources.local_research import LocalResearchSource
from services.market_tape.sources.youtube import YouTubeSource
from services.market_tape.store import MarketTapeStore


class RecheckProviderHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    requested_ids: list[list[str]] = []

    def do_GET(self):  # noqa: N802 - stdlib HTTP handler contract
        parsed = urlparse(self.path)
        if parsed.path != "/videos":
            self._json({"error": "not found"}, status=404)
            return
        ids = parse_qs(parsed.query).get("id", [""])[0].split(",")
        ids = [value for value in ids if value]
        self.__class__.requested_ids.append(ids)
        self._json({
            "items": [
                {
                    "id": external_id,
                    "snippet": {
                        "title": "Measured AI workflow adoption",
                        "description": "A real provider refresh for forecast coverage",
                        "publishedAt": "2026-08-22T12:00:00Z",
                        "channelId": f"channel-{external_id}",
                        "channelTitle": "Measured Systems Lab",
                    },
                    "statistics": {
                        "viewCount": "2400",
                        "likeCount": "120",
                        "commentCount": "18",
                        "shareCount": "9",
                    },
                    "contentDetails": {"duration": "PT45S"},
                }
                for external_id in ids
            ],
        })

    def log_message(self, *_args):
        return

    def _json(self, body, status=200):
        encoded = json.dumps(body).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)


@pytest.fixture
def recheck_provider_server():
    RecheckProviderHandler.requested_ids = []
    server = ThreadingHTTPServer(("127.0.0.1", 0), RecheckProviderHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def test_forecast_window_preempts_normal_due_with_one_deduplicated_member(tmp_path):
    config = _config(tmp_path)
    store = MarketTapeStore(config)
    model_version = _write_active_model(config)
    selected_at = datetime(2026, 8, 22, 18, 0, tzinfo=timezone.utc)
    predicted_at = selected_at - timedelta(hours=5, minutes=45)
    forecast_video_id, normal_video_id, prediction_ids = _seed_open_forecasts(
        store,
        model_version=model_version,
        predicted_at=predicted_at,
        queue_now=selected_at,
    )

    plan = store.due_poll_plan(
        1,
        as_of=selected_at,
        forecast_capable_platforms={"youtube"},
    )

    assert list(plan["polls"]) == ["youtube"]
    selected = plan["polls"]["youtube"]
    assert len(selected) == 1
    assert selected[0]["video_id"] == forecast_video_id
    assert selected[0]["video_id"] != normal_video_id
    assert selected[0]["due_at"] > selected_at.isoformat()
    assert selected[0]["recheck_reason"] == (
        "active_model_forecast_terminal_coverage"
    )
    assert {
        obligation["prediction_id"]
        for obligation in selected[0]["forecast_coverage"]
    } == set(prediction_ids)
    receipt = plan["receipt"]
    assert receipt["coverage_state"] == "queued"
    assert receipt["coverage_predictions_due"] == 2
    assert receipt["coverage_trends_due"] == 2
    assert receipt["coverage_candidate_videos"] == 1
    assert receipt["coverage_predictions_selected"] == 2
    assert receipt["coverage_trends_selected"] == 2
    assert receipt["selected_forecast_coverage"] == 1
    assert receipt["selected_scheduled_due"] == 0
    assert len(receipt["selection_sha256"]) == 64

    incapable = store.due_poll_plan(
        1,
        as_of=selected_at,
        forecast_capable_platforms=set(),
    )
    assert incapable["receipt"]["coverage_state"] == (
        "no_refresh_capable_platform"
    )
    assert incapable["polls"]["youtube"][0]["video_id"] == normal_video_id
    assert incapable["polls"]["youtube"][0]["recheck_reason"] == (
        "scheduled_poll_due"
    )

    at_deadline = store.due_poll_plan(
        1,
        as_of=predicted_at + timedelta(hours=6),
        forecast_capable_platforms={"youtube"},
    )
    assert at_deadline["receipt"]["coverage_state"] == (
        "no_open_coverage_window"
    )
    assert at_deadline["polls"]["youtube"][0]["video_id"] == normal_video_id


def test_real_provider_recheck_scores_two_forecasts_with_one_budgeted_call(
    tmp_path,
    recheck_provider_server,
    monkeypatch,
):
    monkeypatch.setenv("YOUTUBE_API_KEY", "local-integration-key")
    config = _config(tmp_path)
    store = MarketTapeStore(config)
    model_version = _write_active_model(config)
    selected_at = datetime.now(timezone.utc)
    predicted_at = selected_at - timedelta(hours=5, minutes=45)
    forecast_video_id, normal_video_id, prediction_ids = _seed_open_forecasts(
        store,
        model_version=model_version,
        predicted_at=predicted_at,
        queue_now=selected_at,
    )

    def source_builder(resolved, run_id, budget_for):
        return [YouTubeSource(
            resolved,
            run_id,
            budget_for(
                YouTubeSource.source_id,
                resolved.request_limit_for("youtube"),
            ),
            base_url=recheck_provider_server,
        )]

    run_id = "forecast-coverage-recheck"
    store.start_run(run_id, "recheck")
    receipts = MarketTapeCollector(
        config,
        store,
        source_builder=source_builder,
    )._run_rechecks(run_id)

    planner = next(
        receipt
        for receipt in receipts
        if receipt["source_id"] == "market-tape-recheck-planner"
    )
    provider = next(
        receipt
        for receipt in receipts
        if receipt["source_id"] == YouTubeSource.source_id
    )
    assert planner["metadata"]["recheck_plan"]["coverage_state"] == "queued"
    assert planner["metadata"]["recheck_plan"][
        "coverage_predictions_selected"
    ] == 2
    assert provider["request_count"] == 1
    assert provider["accepted_count"] == 1
    assert provider["metadata"]["recheck_queue"]["reason_counts"] == {
        "active_model_forecast_terminal_coverage": 1,
    }
    assert provider["metadata"]["recheck_queue"][
        "coverage_prediction_ids"
    ] == prediction_ids
    assert provider["metadata"]["recheck_queue"]["coverage_trend_count"] == 2
    assert len(provider["metadata"]["recheck_queue"]["assignments_sha256"]) == 64
    assert RecheckProviderHandler.requested_ids == [[
        forecast_video_id.removeprefix("youtube:video:")
    ]]
    assert store.remaining_request_budget(YouTubeSource.source_id, 1) == 0

    with store.connect() as connection:
        refreshed = connection.execute(
            "SELECT video_id FROM mt_market_observations WHERE run_id = ?",
            (run_id,),
        ).fetchall()
        persisted_plan = json.loads(connection.execute(
            """SELECT metadata_json FROM mt_source_receipts
               WHERE run_id = ? AND source_id = 'market-tape-recheck-planner'""",
            (run_id,),
        ).fetchone()["metadata_json"])["recheck_plan"]
    assert [row["video_id"] for row in refreshed] == [forecast_video_id]
    assert normal_video_id not in {row["video_id"] for row in refreshed}
    assert persisted_plan["selection_sha256"] == planner["metadata"][
        "recheck_plan"
    ]["selection_sha256"]

    aggregate_at = datetime.now(timezone.utc)
    target_at = predicted_at + timedelta(hours=6)
    assert aggregate_at < target_at
    assert store.aggregate_trends(observed_at=aggregate_at, run_id=run_id) >= 2
    evaluation = store.evaluate_predictions(target_at)
    assert evaluation["newly_labeled"] == 2
    assert evaluation["newly_unscorable"] == 0
    outcomes = {
        row["prediction_id"]: row["outcome"]
        for row in store.list_predictions(10, "trend")
        if row["prediction_id"] in prediction_ids
    }
    assert set(outcomes) == set(prediction_ids)
    assert all(outcome["state"] == "scored" for outcome in outcomes.values())


def test_archive_replay_is_not_claimed_as_terminal_refresh_capability(tmp_path):
    config = _config(tmp_path)
    store = MarketTapeStore(config)
    model_version = _write_active_model(config)
    selected_at = datetime.now(timezone.utc)
    _seed_open_forecasts(
        store,
        model_version=model_version,
        predicted_at=selected_at - timedelta(hours=5, minutes=45),
        queue_now=selected_at,
    )

    def source_builder(resolved, run_id, budget_for):
        return [LocalResearchSource(
            resolved,
            run_id,
            budget_for("safari-local-research-youtube", 1),
            platform="youtube",
            api_platform="youtube",
            archive_root=tmp_path / "empty-archive",
            base_url="http://127.0.0.1:9",
        )]

    run_id = "archive-only-forecast-gap"
    store.start_run(run_id, "recheck")
    receipts = MarketTapeCollector(
        config,
        store,
        source_builder=source_builder,
    )._run_rechecks(run_id)

    planner = next(
        receipt
        for receipt in receipts
        if receipt["source_id"] == "market-tape-recheck-planner"
    )
    plan = planner["metadata"]["recheck_plan"]
    assert plan["coverage_state"] == "no_refresh_capable_platform"
    assert plan["coverage_predictions_due"] == 2
    assert plan["coverage_predictions_selected"] == 0
    assert plan["source_capability"] == [{
        "source_id": "safari-local-research-youtube",
        "platform": "youtube",
        "state": "archive_only_no_terminal_refresh",
        "request_budget_remaining": 1,
        "metered": False,
    }]


def _config(tmp_path):
    return MarketTapeConfig(
        db_path=tmp_path / "market.sqlite3",
        object_dir=tmp_path / "objects",
        heartbeat_path=tmp_path / "heartbeat.json",
        lock_path=tmp_path / "market.lock",
        local_research_state_path=tmp_path / "local-research-state.json",
        prediction_model_dir=tmp_path / "models",
        platforms=["youtube"],
        topics=["measured forecast coverage"],
        adaptive_topics_enabled=False,
        platform_daily_targets={"youtube": 10},
        provider_daily_request_limits={"youtube": 1},
        provider_cost_per_request_usd={"youtube": 0.01},
        max_daily_provider_cost_usd=1.0,
        max_due_rechecks_per_cycle=1,
        youtube_batch_stats=False,
        supabase_sync_enabled=False,
    )


def _write_active_model(config):
    model_version = "forecast-recheck-model-v1"
    artifact = {
        "contract": "market_tape_trend_predictor_v1",
        "status": "promoted",
        "model_family": "early-breakout-logistic-v3",
        "model_purpose": "early_breakout_entry",
        "model_version": model_version,
        "training_dataset_sha256": "b" * 64,
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


def _seed_open_forecasts(
    store,
    *,
    model_version,
    predicted_at,
    queue_now,
):
    store.start_run("forecast-recheck-seed", "discovery")
    forecast_item = _content(
        external_id="forecast-member",
        observed_at=predicted_at - timedelta(minutes=5),
        source_id=YouTubeSource.source_id,
    )
    normal_item = _content(
        external_id="ordinary-due",
        observed_at=predicted_at - timedelta(hours=1),
        source_id=YouTubeSource.source_id,
    )
    store.ingest(forecast_item, "forecast-recheck-seed")
    store.ingest(normal_item, "forecast-recheck-seed")
    prediction_ids = []
    with store.connect() as connection:
        connection.execute(
            "UPDATE mt_poll_queue SET due_at = ? WHERE video_id = ?",
            (
                (queue_now + timedelta(days=1)).isoformat(),
                forecast_item.video_id,
            ),
        )
        connection.execute(
            "UPDATE mt_poll_queue SET due_at = ? WHERE video_id = ?",
            (
                (queue_now - timedelta(hours=1)).isoformat(),
                normal_item.video_id,
            ),
        )
        for index in range(2):
            trend_id = f"trend:forecast-recheck:{index}"
            _insert_trend_observation(
                connection,
                trend_id=trend_id,
                observed_at=predicted_at,
            )
            connection.execute(
                """INSERT INTO mt_trend_memberships(
                       trend_id, video_id, confidence, evidence_json,
                       first_seen_at
                   ) VALUES(?, ?, 0.95, ?, ?)""",
                (
                    trend_id,
                    forecast_item.video_id,
                    json.dumps({"contract": "forecast-recheck-test-membership"}),
                    predicted_at.isoformat(),
                ),
            )
            cursor = connection.execute(
                """INSERT INTO mt_predictions(
                       subject_type, subject_id, model_version, predicted_at,
                       horizon, probability, expected_remaining_life_hours,
                       features_json
                   ) VALUES('trend', ?, ?, ?, ?, 0.6, 12.0, ?)""",
                (
                    trend_id,
                    model_version,
                    predicted_at.isoformat(),
                    ENTRY_HORIZON,
                    json.dumps({
                        "observation_quality_contract": (
                            OBSERVATION_QUALITY_CONTRACT
                        ),
                    }),
                ),
            )
            observation_id = int(connection.execute(
                """SELECT observation_id FROM mt_market_observations
                   WHERE observation_key = ?""",
                (forecast_item.observation_key,),
            ).fetchone()[0])
            connection.execute(
                """INSERT INTO mt_trend_membership_lineage(
                       trend_id, video_id, observation_id, linked_at, contract
                   ) VALUES(?, ?, ?, ?, ?)""",
                (
                    trend_id,
                    forecast_item.video_id,
                    observation_id,
                    predicted_at.isoformat(),
                    "market_tape_accepted_observation_evidence_v1",
                ),
            )
            prediction_ids.append(int(cursor.lastrowid))
    return forecast_item.video_id, normal_item.video_id, prediction_ids


def _content(*, external_id, observed_at, source_id):
    return MarketContent(
        platform="youtube",
        external_id=external_id,
        creator_external_id=f"creator-{external_id}",
        published_at=observed_at - timedelta(hours=2),
        observed_at=observed_at,
        source_id=source_id,
        metrics=MetricCounters(views=1000, likes=50, comments=5, shares=2),
        title=f"Measured forecast coverage {external_id}",
        url=f"https://youtube.com/watch?v={external_id}",
        raw_payload={"id": external_id, "views": 1000},
    )


def _insert_trend_observation(connection, *, trend_id, observed_at):
    connection.execute(
        """INSERT INTO mt_trends(
               trend_id, trend_type, canonical_key, display_name, status,
               first_seen_at, last_seen_at
           ) VALUES(?, 'topic', ?, ?, 'emerging', ?, ?)""",
        (
            trend_id,
            trend_id,
            trend_id,
            observed_at.isoformat(),
            observed_at.isoformat(),
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
               relative_strength, saturation, trend_strength, index_version,
               observation_quality_contract, state
           ) VALUES(?, ?, 2, 1, 2, 1, 1, 10000, 1000, 100, 50,
                    1000, 100, 10, 5, 1, 0.5, 1.0, 2.0, 0.8, 0.5,
                    0.5, 1.0, 1.0, 0.5, 1.0, 0.2, 60.0,
                    'trend-strength-v2', ?, 'emerging')""",
        (trend_id, observed_at.isoformat(), OBSERVATION_QUALITY_CONTRACT),
    )
