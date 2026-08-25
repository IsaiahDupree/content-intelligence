"""Adaptive query feedback contracts using real SQLite and loopback HTTP."""

from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

import pytest

from services.market_tape.collector import MarketTapeCollector
from services.market_tape.config import MarketTapeConfig
from services.market_tape.dataset import MarketTapeDatasetManager
from services.market_tape.models import MarketContent, MetricCounters, QueryAttempt
from services.market_tape.predictor import (
    ENTRY_HORIZON,
    OBSERVATION_QUALITY_CONTRACT,
)
from services.market_tape.sources.youtube import YouTubeSource
from services.market_tape.store import MarketTapeStore


class QueryProviderHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    requests: list[dict] = []

    def do_GET(self):  # noqa: N802 - stdlib handler contract
        parsed = urlparse(self.path)
        query = parse_qs(parsed.query)
        self.__class__.requests.append({"path": parsed.path, "query": query})
        if parsed.path == "/search":
            family = query.get("q", [""])[0]
            self._json({
                "items": [{"id": {"videoId": f"search-{_slug(family)}"}}],
            })
            return
        if parsed.path == "/videos":
            ids = query.get("id", ["chart-current"])[0].split(",")
            self._json({"items": [self._video(video_id) for video_id in ids if video_id]})
            return
        self._json({"error": "not found"}, status=404)

    def log_message(self, *_):
        return

    def _video(self, video_id: str) -> dict:
        return {
            "id": video_id,
            "snippet": {
                "title": "Current market observation",
                "description": "Observed through a real loopback provider contract",
                "publishedAt": (
                    datetime.now(timezone.utc) - timedelta(minutes=20)
                ).replace(microsecond=0).isoformat().replace("+00:00", "Z"),
                "channelId": f"channel-{video_id}",
                "channelTitle": "Market Observer",
            },
            "statistics": {
                "viewCount": "12000",
                "likeCount": "800",
                "commentCount": "90",
                "shareCount": "45",
            },
            "contentDetails": {"duration": "PT42S"},
        }

    def _json(self, body: dict, status: int = 200) -> None:
        encoded = json.dumps(body).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)


@pytest.fixture
def query_provider_server():
    QueryProviderHandler.requests = []
    server = ThreadingHTTPServer(("127.0.0.1", 0), QueryProviderHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def test_feedback_portfolio_reserves_baseline_and_direct_current_lanes(tmp_path):
    config = _config(
        tmp_path,
        topics=["market baseline", "broad culture", "consumer pulse"],
        adaptive_topic_limit=6,
        adaptive_topic_exploration_fraction=0.34,
        adaptive_topic_direct_query_fraction=0.34,
    )
    store = MarketTapeStore(config)
    now = datetime.now(timezone.utc)
    store.start_run("evidence-run", "archive_bootstrap")
    _ingest_family(
        store,
        "evidence-run",
        now,
        family="Mars Sample Return",
        title="Watch today",
        prefix="mars",
        query=True,
    )
    _ingest_family(
        store,
        "evidence-run",
        now,
        family="quantum battery breakthrough",
        title="Quantum battery breakthrough",
        prefix="battery",
        query=False,
    )
    store.finish_run("evidence-run")

    collector = MarketTapeCollector(config, store)
    runtime = collector._adaptive_discovery_config()
    plan = collector._last_discovery_topics

    assert plan["contract"] == "market_tape_adaptive_query_feedback_v1"
    assert plan["mode"] == "adaptive"
    assert plan["direct_current_count"] >= 1
    assert plan["derived_feedback_count"] >= 1
    assert len(plan["baseline_topics"]) >= 2
    assert runtime.topics == plan["topics"]
    assert plan["budgets"]["daily_feedback_limit"] == 20
    assert plan["budgets"]["daily_feedback_admitted"] == len(plan["signals"])
    assert plan["budgets"]["daily_feedback_remaining_after_selection"] == (
        20 - len(plan["signals"])
    )
    direct = next(
        signal for signal in plan["signals"]
        if signal["selection_lane"] == "direct_current_query"
    )
    assert direct["keyword"] == "mars sample return"
    assert direct["evidence_source"] == "mt_discovery_attributions"
    assert len(direct["evidence_video_ids"]) >= 2
    assert len(direct["evidence_urls"]) >= 2
    assert direct["videos_total"] >= 2
    assert direct["creators_total"] >= 2


def test_one_slot_portfolio_cannot_displace_configured_baseline(tmp_path):
    config = _config(
        tmp_path,
        topics=["market baseline"],
        adaptive_topic_limit=1,
        adaptive_topic_exploration_fraction=0,
    )
    store = MarketTapeStore(config)
    now = datetime.now(timezone.utc)
    store.start_run("one-slot-evidence", "archive_bootstrap")
    _ingest_family(
        store,
        "one-slot-evidence",
        now,
        family="Mars Sample Return",
        title="Watch today",
        prefix="one-slot",
        query=True,
    )
    store.finish_run("one-slot-evidence")

    collector = MarketTapeCollector(config, store)
    collector._adaptive_discovery_config()
    plan = collector._last_discovery_topics
    assert plan["topics"] == ["market baseline"]
    assert plan["baseline_topics"] == ["market baseline"]
    assert plan["signals"] == []
    assert plan["budgets"]["daily_feedback_admitted"] == 0


def test_planner_receipt_enforces_family_cooldown_and_daily_budget(tmp_path, monkeypatch):
    monkeypatch.setenv("YOUTUBE_API_KEY", "local-ledger-test-key")
    config = _config(
        tmp_path,
        topics=["market baseline"],
        adaptive_topic_limit=3,
        adaptive_topic_exploration_fraction=0.34,
        adaptive_topic_daily_feedback_limit=1,
        adaptive_topic_family_daily_limit=1,
        adaptive_topic_cooldown_hours=24,
    )
    store = MarketTapeStore(config)
    now = datetime.now(timezone.utc)
    store.start_run("budget-evidence", "archive_bootstrap")
    _ingest_family(
        store,
        "budget-evidence",
        now,
        family="Mars Sample Return",
        title="Watch today",
        prefix="mars-budget",
        query=True,
    )
    _ingest_family(
        store,
        "budget-evidence",
        now,
        family="Ocean Carbon Capture",
        title="Watch today",
        prefix="ocean-budget",
        query=True,
    )
    store.finish_run("budget-evidence")

    first = MarketTapeCollector(config, store)
    runtime = first._adaptive_discovery_config()
    assert len(first._last_discovery_topics["signals"]) == 1
    admitted_keyword = first._last_discovery_topics["signals"][0]["keyword"]
    store.start_run("planner-admission", "discovery")
    source = YouTubeSource(runtime, "planner-admission", 10, base_url="http://127.0.0.1:9")
    try:
        receipt = first._save_adaptive_query_plan("planner-admission", [source])
    finally:
        source.close()
    store.finish_run("planner-admission")
    assert receipt is not None
    assert receipt.request_count == 0
    assert receipt.metadata["adaptive_query_selection"]["execution_admitted"] is True

    usage = store.adaptive_query_feedback_usage(
        datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
    )
    assert usage["planner_receipts"] == 1
    assert usage["feedback_selections"] == 1
    assert usage["families"][admitted_keyword]["selection_count"] == 1
    assert usage["query_attempts"] == 0

    second = MarketTapeCollector(config, store)
    second._adaptive_discovery_config()
    second_plan = second._last_discovery_topics
    assert second_plan["mode"] == "configured_fallback"
    assert second_plan["signals"] == []
    excluded = {
        row["keyword"]: set(row["reasons"])
        for row in second_plan["excluded_candidates"]
    }
    assert "adaptive_daily_budget_exhausted" in excluded[admitted_keyword]
    assert "query_family_daily_budget_exhausted" in excluded[admitted_keyword]
    assert "query_family_cooldown_active" in excluded[admitted_keyword]
    other_keyword = next(keyword for keyword in excluded if keyword != admitted_keyword)
    assert "adaptive_daily_budget_exhausted" in excluded[other_keyword]


def test_actual_query_attempt_cools_family_without_prior_planner_receipt(tmp_path):
    config = _config(
        tmp_path,
        topics=["market baseline"],
        adaptive_topic_limit=3,
        adaptive_topic_exploration_fraction=0.34,
        adaptive_topic_daily_feedback_limit=5,
        adaptive_topic_cooldown_hours=24,
    )
    store = MarketTapeStore(config)
    now = datetime.now(timezone.utc)
    store.start_run("attempt-evidence", "archive_bootstrap")
    _ingest_family(
        store,
        "attempt-evidence",
        now,
        family="Mars Sample Return",
        title="Watch today",
        prefix="mars-attempt",
        query=True,
    )
    store.finish_run("attempt-evidence")
    store.save_query_attempts([QueryAttempt(
        run_id="historical-provider-run",
        source_id="youtube-data-api-v3",
        platform="youtube",
        query="Mars Sample Return",
        attempted_at=now - timedelta(hours=1),
        finished_at=now - timedelta(hours=1),
        state="empty",
        result_count=0,
        request_count=1,
        metadata={"query_family": "Mars Sample Return"},
    )])

    collector = MarketTapeCollector(config, store)
    collector._adaptive_discovery_config()
    plan = collector._last_discovery_topics
    assert plan["signals"] == []
    exclusion = next(
        row for row in plan["excluded_candidates"]
        if row["keyword"] == "mars sample return"
    )
    assert exclusion["reasons"] == ["query_family_cooldown_active"]
    usage = store.adaptive_query_feedback_usage(now - timedelta(hours=2))
    assert usage["feedback_selections"] == 0
    assert usage["families"]["mars sample return"]["attempt_count"] == 1


def test_loopback_provider_persists_planner_and_per_attempt_lineage(
    tmp_path,
    query_provider_server,
    monkeypatch,
):
    monkeypatch.setenv("YOUTUBE_API_KEY", "loopback-integration-key")
    config = _config(
        tmp_path,
        topics=["market baseline", "broad culture"],
        adaptive_topic_limit=3,
        adaptive_topic_exploration_fraction=0.34,
        adaptive_topic_daily_feedback_limit=3,
        youtube_search_daily_limit=3,
    )
    store = MarketTapeStore(config)
    now = datetime.now(timezone.utc)
    store.start_run("lineage-evidence", "archive_bootstrap")
    _ingest_family(
        store,
        "lineage-evidence",
        now,
        family="Mars Sample Return",
        title="Watch today",
        prefix="mars-lineage",
        query=True,
    )
    store.finish_run("lineage-evidence")

    def source_builder(runtime_config, run_id, budget_for):
        return [YouTubeSource(
            runtime_config,
            run_id,
            budget_for(YouTubeSource.source_id, runtime_config.request_limit_for("youtube")),
            base_url=query_provider_server,
        )]

    result = MarketTapeCollector(config, store, source_builder=source_builder).run_cycle("discovery")
    assert result["state"] == "completed"
    provider_receipt = result["receipts"][0]
    planner_receipt = result["receipts"][-1]
    assert provider_receipt["source_id"] == YouTubeSource.source_id
    assert planner_receipt["source_id"] == "market-tape-adaptive-query-planner"
    plan_pointer = provider_receipt["metadata"]["adaptive_query_plan"]
    plan = planner_receipt["metadata"]["adaptive_query_selection"]
    assert plan_pointer["selection_sha256"] == plan["selection_sha256"]
    assert plan["execution_admitted"] is True
    assert plan["admitted_feedback_signals"][0]["evidence_video_ids"]
    unhashed = {**plan, "selection_sha256": ""}
    assert plan["selection_sha256"] == hashlib.sha256(json.dumps(
        unhashed,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")).hexdigest()

    attempts = store.list_query_attempts(limit=100, platform="youtube")
    direct = next(row for row in attempts if row["query"] == "mars sample return")
    direct_lineage = direct["metadata"]["adaptive_query_lineage"]
    assert direct_lineage["selection_lane"] == "direct_current_query"
    assert direct_lineage["evidence_source"] == "mt_discovery_attributions"
    assert direct_lineage["planner_run_id"] == result["run_id"]
    assert direct_lineage["selection_sha256"] == plan["selection_sha256"]
    baseline = next(row for row in attempts if row["query"] in plan["baseline_topics"])
    assert baseline["metadata"]["adaptive_query_lineage"]["selection_lane"] == "configured_baseline"

    usage = store.adaptive_query_feedback_usage(now - timedelta(hours=1))
    assert usage["feedback_selections"] == 1
    assert usage["families"]["mars sample return"]["attempt_count"] == 1
    assert usage["families"]["mars sample return"]["request_count"] == 1
    searched = {
        request["query"].get("q", [""])[0]
        for request in QueryProviderHandler.requests
        if request["path"] == "/search"
    }
    assert "mars sample return" in searched


def test_full_cycle_reserves_single_provider_request_for_terminal_forecast(
    tmp_path,
    query_provider_server,
    monkeypatch,
):
    monkeypatch.setenv("YOUTUBE_API_KEY", "terminal-priority-loopback-key")
    config = _config(
        tmp_path,
        topics=["adaptive discovery baseline"],
        adaptive_topic_limit=3,
        provider_daily_request_limits={"youtube": 1},
        youtube_search_daily_limit=1,
        youtube_chart_categories=["all"],
        youtube_batch_stats=False,
        max_due_rechecks_per_cycle=1,
    )
    store = MarketTapeStore(config)
    model_version = _write_active_model(config)
    selected_at = datetime.now(timezone.utc)
    predicted_at = selected_at - timedelta(hours=5, minutes=45)
    forecast_video_id, prediction_id = _seed_terminal_forecast(
        store,
        model_version=model_version,
        predicted_at=predicted_at,
        queue_now=selected_at,
    )

    def source_builder(runtime_config, run_id, budget_for):
        return [YouTubeSource(
            runtime_config,
            run_id,
            budget_for(YouTubeSource.source_id, runtime_config.request_limit_for("youtube")),
            base_url=query_provider_server,
        )]

    result = MarketTapeCollector(config, store, source_builder=source_builder).run_cycle("full")
    assert result["state"] == "completed"
    refresh_receipt = next(
        receipt for receipt in result["receipts"]
        if receipt["source_id"] == YouTubeSource.source_id
        and "recheck_queue" in receipt["metadata"]
    )
    discovery_receipt = next(
        receipt for receipt in result["receipts"]
        if receipt["source_id"] == YouTubeSource.source_id
        and "adaptive_query_plan" in receipt["metadata"]
    )
    adaptive_planner = next(
        receipt for receipt in result["receipts"]
        if receipt["source_id"] == "market-tape-adaptive-query-planner"
    )
    assert refresh_receipt["request_count"] == 1
    assert refresh_receipt["accepted_count"] == 1
    assert refresh_receipt["metadata"]["recheck_queue"]["reason_counts"] == {
        "active_model_forecast_terminal_coverage": 1,
    }
    assert refresh_receipt["metadata"]["recheck_queue"]["planner_phase"] == (
        "forecast_terminal"
    )
    assert refresh_receipt["metadata"]["recheck_queue"]["selection_lane"] == (
        "forecast_terminal"
    )
    assert discovery_receipt["request_count"] == 0
    assert discovery_receipt["error_code"] == "request_budget_exhausted"
    assert adaptive_planner["metadata"]["adaptive_query_selection"][
        "execution_admitted"
    ] is False
    assert store.status()["totals"]["adaptive_query_admissions"] == 0
    assert store.remaining_request_budget(YouTubeSource.source_id, 1) == 0
    http_paths = [request["path"] for request in QueryProviderHandler.requests]
    assert http_paths == ["/videos"]
    assert QueryProviderHandler.requests[0]["query"]["id"] == [
        forecast_video_id.removeprefix("youtube:video:")
    ]
    terminal_planner = next(
        receipt for receipt in result["receipts"]
        if receipt["source_id"] == "market-tape-recheck-planner-terminal"
    )["metadata"]["recheck_plan"]
    scheduled_planner = next(
        receipt for receipt in result["receipts"]
        if receipt["source_id"] == "market-tape-recheck-planner-scheduled"
    )["metadata"]["recheck_plan"]
    assert terminal_planner["phase"] == "forecast_terminal"
    assert terminal_planner["coverage_evaluated"] is True
    assert terminal_planner["selected_scheduled_due"] == 0
    assert scheduled_planner["phase"] == "scheduled"
    assert scheduled_planner["coverage_evaluated"] is False
    assert scheduled_planner["coverage_state"] == (
        "not_evaluated_in_scheduled_phase"
    )
    assert scheduled_planner["coverage_predictions_due"] == 0

    evaluation = store.evaluate_predictions(predicted_at + timedelta(hours=6))
    assert evaluation["newly_labeled"] == 1
    outcome = next(
        row["outcome"] for row in store.list_predictions(20, "trend")
        if row["prediction_id"] == prediction_id
    )
    assert outcome["state"] == "scored"


def test_full_cycle_gives_discovery_priority_over_ordinary_due_recheck(
    tmp_path,
    query_provider_server,
    monkeypatch,
):
    monkeypatch.setenv("YOUTUBE_API_KEY", "ordinary-phase-loopback-key")
    config = _config(
        tmp_path,
        topics=["adaptive discovery baseline"],
        adaptive_topic_limit=3,
        provider_daily_request_limits={"youtube": 1},
        youtube_search_daily_limit=1,
        youtube_chart_categories=["all"],
        youtube_batch_stats=False,
        max_due_rechecks_per_cycle=1,
    )
    store = MarketTapeStore(config)
    ordinary_video_id = _seed_ordinary_due_poll(
        store,
        queue_now=datetime.now(timezone.utc),
    )

    def source_builder(runtime_config, run_id, budget_for):
        return [YouTubeSource(
            runtime_config,
            run_id,
            budget_for(
                YouTubeSource.source_id,
                runtime_config.request_limit_for("youtube"),
            ),
            base_url=query_provider_server,
        )]

    result = MarketTapeCollector(
        config,
        store,
        source_builder=source_builder,
    ).run_cycle("full")
    assert result["state"] == "completed"
    terminal_plan = next(
        receipt["metadata"]["recheck_plan"]
        for receipt in result["receipts"]
        if receipt["source_id"] == "market-tape-recheck-planner-terminal"
    )
    scheduled_plan = next(
        receipt["metadata"]["recheck_plan"]
        for receipt in result["receipts"]
        if receipt["source_id"] == "market-tape-recheck-planner-scheduled"
    )
    discovery_receipt = next(
        receipt for receipt in result["receipts"]
        if receipt["source_id"] == YouTubeSource.source_id
        and "adaptive_query_plan" in receipt["metadata"]
    )
    scheduled_receipt = next(
        receipt for receipt in result["receipts"]
        if receipt["source_id"] == YouTubeSource.source_id
        and "recheck_queue" in receipt["metadata"]
    )

    assert terminal_plan["phase"] == "forecast_terminal"
    assert terminal_plan["coverage_evaluated"] is True
    assert terminal_plan["coverage_state"] == "no_active_model"
    assert terminal_plan["selected_total"] == 0
    assert terminal_plan["selected_scheduled_due"] == 0
    assert discovery_receipt["request_count"] == 1
    assert discovery_receipt["accepted_count"] == 1
    assert scheduled_plan["phase"] == "scheduled"
    assert scheduled_plan["coverage_evaluated"] is False
    assert scheduled_plan["coverage_state"] == (
        "not_evaluated_in_scheduled_phase"
    )
    assert scheduled_plan["selected_forecast_coverage"] == 0
    assert scheduled_plan["selected_scheduled_due"] == 1
    assert scheduled_receipt["request_count"] == 0
    assert scheduled_receipt["error_code"] == "request_budget_exhausted"
    assert scheduled_receipt["metadata"]["recheck_queue"]["planner_phase"] == (
        "scheduled"
    )
    assert scheduled_receipt["metadata"]["recheck_queue"]["selection_lane"] == (
        "scheduled"
    )
    assert scheduled_receipt["metadata"]["recheck_queue"]["assignments"][0][
        "video_id"
    ] == ordinary_video_id
    assert len(QueryProviderHandler.requests) == 1
    assert QueryProviderHandler.requests[0]["path"] == "/videos"
    assert QueryProviderHandler.requests[0]["query"]["chart"] == [
        "mostPopular"
    ]
    assert "id" not in QueryProviderHandler.requests[0]["query"]


def test_atomic_admission_ledger_blocks_concurrent_collectors(
    tmp_path,
    query_provider_server,
    monkeypatch,
):
    monkeypatch.setenv("YOUTUBE_API_KEY", "atomic-ledger-loopback-key")
    config = _config(
        tmp_path,
        topics=["market baseline"],
        adaptive_topic_limit=3,
        adaptive_topic_exploration_fraction=0.34,
        adaptive_topic_daily_feedback_limit=1,
        adaptive_topic_family_daily_limit=1,
        adaptive_topic_cooldown_hours=0,
    )
    seed_store = MarketTapeStore(config)
    now = datetime.now(timezone.utc)
    seed_store.start_run("atomic-evidence", "archive_bootstrap")
    _ingest_family(
        seed_store,
        "atomic-evidence",
        now,
        family="Mars Sample Return",
        title="Watch today",
        prefix="atomic-mars",
        query=True,
    )
    seed_store.finish_run("atomic-evidence")

    stores = [MarketTapeStore(config), MarketTapeStore(config)]
    collectors = [MarketTapeCollector(config, store) for store in stores]
    run_ids = ["concurrent-planner-a", "concurrent-planner-b"]
    sources = []
    for store, collector, run_id in zip(stores, collectors, run_ids):
        store.start_run(run_id, "discovery")
        runtime = collector._adaptive_discovery_config()
        assert [signal["keyword"] for signal in collector._last_discovery_topics["signals"]] == [
            "mars sample return"
        ]
        sources.append(YouTubeSource(
            runtime,
            run_id,
            10,
            base_url=query_provider_server,
        ))

    barrier = threading.Barrier(2)

    def reserve(index: int):
        barrier.wait(timeout=5)
        return collectors[index]._save_adaptive_query_plan(run_ids[index], [sources[index]])

    try:
        with ThreadPoolExecutor(max_workers=2) as executor:
            receipts = list(executor.map(reserve, (0, 1)))
    finally:
        for source in sources:
            source.close()
        for store, run_id in zip(stores, run_ids):
            store.finish_run(run_id)

    plans = [receipt.metadata["adaptive_query_selection"] for receipt in receipts]
    assert sorted(len(plan["admitted_feedback_signals"]) for plan in plans) == [0, 1]
    assert sorted(plan["atomic_admission"]["new_admissions"] for plan in plans) == [0, 1]
    loser = next(plan for plan in plans if not plan["admitted_feedback_signals"])
    assert loser["atomic_admission"]["rejected"][0]["reasons"] == [
        "adaptive_daily_budget_exhausted_atomic",
        "query_family_daily_budget_exhausted_atomic",
    ]
    assert loser["budgets"]["daily_feedback_used_before_selection"] == 1
    assert loser["budgets"]["daily_feedback_remaining_before_selection"] == 0
    assert loser["budgets"]["daily_feedback_used_after_selection"] == 1
    assert loser["budgets"]["daily_feedback_remaining_after_selection"] == 0
    with seed_store.connect() as connection:
        rows = connection.execute(
            "SELECT * FROM mt_adaptive_query_admissions"
        ).fetchall()
    assert len(rows) == 1
    assert rows[0]["query_family"] == "mars sample return"
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        with seed_store.connect() as connection:
            connection.execute(
                "UPDATE mt_adaptive_query_admissions SET keyword = 'changed'"
            )

    winner = next(plan for plan in plans if plan["admitted_feedback_signals"])
    winner_run = winner["atomic_admission"]["run_id"]
    seed_store.enqueue_run_for_sync(winner_run)
    with seed_store.connect() as connection:
        outbox_receipts = [
            json.loads(row["payload_json"])
            for row in connection.execute(
                "SELECT payload_json FROM mt_sync_outbox WHERE entity_type = 'receipt'"
            ).fetchall()
        ]
    planner_payload = next(
        payload for payload in outbox_receipts
        if payload["run_id"] == winner_run
        and payload["source_id"] == "market-tape-adaptive-query-planner"
    )
    persisted_plan = json.loads(planner_payload["metadata_json"])[
        "adaptive_query_selection"
    ]
    assert persisted_plan["atomic_admission"]["admitted"][0]["admission_key"] == rows[0][
        "admission_key"
    ]
    assert planner_payload["request_count"] == 0
    assert seed_store.list_query_attempts(10) == []

    artifacts = MarketTapeDatasetManager(config, seed_store)._export_tables(
        tmp_path / "dataset-tables",
        seed_store,
    )
    ledger_artifact = next(
        artifact for artifact in artifacts
        if artifact["table"] == "mt_adaptive_query_admissions"
    )
    assert ledger_artifact["rows"] == 1
    assert ledger_artifact["sha256"]


@pytest.mark.parametrize(
    ("attempt_query", "attempt_metadata"),
    [
        (
            "provider-specific expansion syntax",
            {"query_family": "Mars Sample Return"},
        ),
        ("Mars Sample Return", {}),
    ],
    ids=("metadata-query-family", "query-fallback"),
)
def test_atomic_reservation_rechecks_attempt_cooldown_after_planner_preflight(
    tmp_path,
    monkeypatch,
    attempt_query,
    attempt_metadata,
):
    monkeypatch.setenv("YOUTUBE_API_KEY", "cooldown-toctou-test-key")
    config = _config(
        tmp_path,
        topics=["market baseline"],
        adaptive_topic_limit=3,
        adaptive_topic_exploration_fraction=0.34,
        adaptive_topic_daily_feedback_limit=5,
        adaptive_topic_family_daily_limit=1,
        adaptive_topic_cooldown_hours=24,
    )
    evidence_store = MarketTapeStore(config)
    now = datetime.now(timezone.utc)
    evidence_store.start_run("cooldown-race-evidence", "archive_bootstrap")
    _ingest_family(
        evidence_store,
        "cooldown-race-evidence",
        now,
        family="Mars Sample Return",
        title="Watch today",
        prefix="cooldown-race",
        query=True,
    )
    evidence_store.finish_run("cooldown-race-evidence")

    reservation_store = MarketTapeStore(config)
    attempt_store = MarketTapeStore(config)
    collector = MarketTapeCollector(config, reservation_store)
    runtime = collector._adaptive_discovery_config()
    proposed_plan = collector._last_discovery_topics
    assert [signal["keyword"] for signal in proposed_plan["signals"]] == [
        "mars sample return"
    ]

    reservation_store.start_run("cooldown-race-planner", "discovery")
    attempt_time = datetime.now(timezone.utc)
    assert attempt_time > datetime.fromisoformat(proposed_plan["selected_at"])
    assert attempt_store.save_query_attempts([QueryAttempt(
        run_id="provider-attempt-after-preflight",
        source_id="youtube-data-api-v3",
        platform="youtube",
        query=attempt_query,
        attempted_at=attempt_time,
        finished_at=attempt_time,
        state="empty",
        result_count=0,
        request_count=1,
        metadata=attempt_metadata,
    )]) == 1

    source = YouTubeSource(
        runtime,
        "cooldown-race-planner",
        10,
        base_url="http://127.0.0.1:9",
    )
    try:
        receipt = collector._save_adaptive_query_plan(
            "cooldown-race-planner",
            [source],
        )
    finally:
        source.close()
        reservation_store.finish_run("cooldown-race-planner")

    assert receipt is not None
    plan = receipt.metadata["adaptive_query_selection"]
    atomic = plan["atomic_admission"]
    assert plan["execution_admitted"] is True
    assert plan["admitted_feedback_signals"] == []
    assert atomic["new_admissions"] == 0
    assert atomic["rejected"][0]["reasons"] == [
        "query_family_cooldown_active_atomic"
    ]
    assert atomic["rejected"][0]["cooldown_sources"] == ["query_attempt"]
    assert atomic["rejected"][0]["latest_cooldown_activity_at"] == attempt_time.isoformat()
    assert datetime.fromisoformat(atomic["cooldown_boundary"]) < attempt_time
    assert reservation_store.status()["totals"]["adaptive_query_admissions"] == 0
    exclusion = next(
        row for row in plan["excluded_candidates"]
        if row["keyword"] == "mars sample return"
        and row["keyword_type"] == "atomic_admission"
    )
    assert exclusion["reasons"] == ["query_family_cooldown_active_atomic"]
    assert exclusion["latest_activity_at"] == attempt_time.isoformat()
    assert exclusion["latest_cooldown_activity_at"] == attempt_time.isoformat()
    assert exclusion["cooldown_sources"] == ["query_attempt"]
    assert exclusion["cooldown_hours"] == 24
    assert exclusion["cooldown_boundary"] == atomic["cooldown_boundary"]
    assert exclusion["requested_cooldown_boundary"] == atomic[
        "requested_cooldown_boundary"
    ]


def test_atomic_rolling_cooldown_serializes_admissions_across_utc_day_boundary(
    tmp_path,
):
    config = _config(
        tmp_path,
        adaptive_topic_daily_feedback_limit=1,
        adaptive_topic_family_daily_limit=1,
        adaptive_topic_cooldown_hours=24,
    )
    stores = [MarketTapeStore(config), MarketTapeStore(config)]
    run_ids = ["utc-boundary-a", "utc-boundary-b"]
    for store, run_id in zip(stores, run_ids):
        store.start_run(run_id, "discovery")
    admission_times = [
        datetime(2026, 8, 22, 23, 59, 59, tzinfo=timezone.utc),
        datetime(2026, 8, 23, 0, 0, 1, tzinfo=timezone.utc),
    ]
    candidate = {
        "keyword": "Mars Sample Return",
        "selection_lane": "direct_current_query",
        "evidence_video_ids": ["youtube:video:a", "youtube:video:b"],
    }
    barrier = threading.Barrier(2)

    def reserve(index: int):
        barrier.wait(timeout=5)
        admitted_at = admission_times[index]
        return stores[index].reserve_adaptive_query_admissions(
            run_id=run_ids[index],
            admitted_at=admitted_at,
            candidates=[candidate],
            daily_limit=1,
            family_daily_limit=1,
            cooldown_boundary=admitted_at - timedelta(hours=24),
            cooldown_hours=24,
            proposal_sha256=f"proposal-{index}",
        )

    try:
        with ThreadPoolExecutor(max_workers=2) as executor:
            results = list(executor.map(reserve, (0, 1)))
    finally:
        for store, run_id in zip(stores, run_ids):
            store.finish_run(run_id)

    assert sorted(result["new_admissions"] for result in results) == [0, 1]
    loser = next(result for result in results if result["new_admissions"] == 0)
    assert loser["rejected"][0]["reasons"] == [
        "query_family_cooldown_active_atomic"
    ]
    assert loser["rejected"][0]["cooldown_sources"] == [
        "adaptive_admission"
    ]
    with stores[0].connect() as connection:
        rows = connection.execute(
            "SELECT utc_day, query_family FROM mt_adaptive_query_admissions"
        ).fetchall()
    assert len(rows) == 1
    assert rows[0]["query_family"] == "mars sample return"


def test_complete_exclusion_decision_is_persisted_and_hashed(tmp_path):
    config = _config(
        tmp_path,
        topics=["market baseline"],
        adaptive_topic_limit=30,
        adaptive_topic_daily_feedback_limit=0,
        adaptive_topic_family_daily_limit=1,
    )
    store = MarketTapeStore(config)
    now = datetime.now(timezone.utc)
    queries = [f"measured current query {index:03d}" for index in range(120)]
    store.start_run("many-exclusions-evidence", "archive_bootstrap")
    _ingest_many_queries(store, "many-exclusions-evidence", now, queries)
    store.finish_run("many-exclusions-evidence")

    collector = MarketTapeCollector(config, store)
    collector._adaptive_discovery_config()
    assert collector._last_discovery_topics["excluded_candidates_total"] >= 120
    store.start_run("many-exclusions-plan", "discovery")
    receipt = collector._save_adaptive_query_plan("many-exclusions-plan", [])
    store.finish_run("many-exclusions-plan")
    assert receipt is not None
    plan = receipt.metadata["adaptive_query_selection"]
    assert len(plan["excluded_candidates"]) == plan["excluded_candidates_total"]
    assert len(plan["excluded_candidates"]) >= 120
    assert len(plan["excluded_candidates_preview"]) == 100
    assert plan["excluded_candidates"][100:] != []
    unhashed = {**plan, "selection_sha256": ""}
    assert plan["selection_sha256"] == hashlib.sha256(json.dumps(
        unhashed,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")).hexdigest()
    with store.connect() as connection:
        persisted = json.loads(connection.execute(
            """SELECT metadata_json FROM mt_source_receipts
               WHERE run_id = ? AND source_id = 'market-tape-adaptive-query-planner'""",
            ("many-exclusions-plan",),
        ).fetchone()["metadata_json"])["adaptive_query_selection"]
    assert persisted["excluded_candidates"] == plan["excluded_candidates"]
    assert persisted["selection_sha256"] == plan["selection_sha256"]


def _config(tmp_path, **changes) -> MarketTapeConfig:
    base = MarketTapeConfig(
        db_path=tmp_path / "market.sqlite3",
        object_dir=tmp_path / "objects",
        heartbeat_path=tmp_path / "heartbeat.json",
        lock_path=tmp_path / "market.lock",
        local_research_state_path=tmp_path / "local-research.json",
        prediction_model_dir=tmp_path / "models",
        platforms=["youtube"],
        topics=["market baseline"],
        adaptive_topics_enabled=True,
        adaptive_topic_limit=4,
        adaptive_topic_window_hours=168,
        adaptive_topic_min_videos=2,
        adaptive_topic_exploration_fraction=0.25,
        adaptive_topic_direct_query_fraction=0.25,
        adaptive_topic_cooldown_hours=24,
        adaptive_topic_daily_feedback_limit=20,
        adaptive_topic_family_daily_limit=1,
        regions=["US"],
        youtube_chart_categories=["all"],
        daily_unique_target=100,
        platform_daily_targets={"youtube": 100},
        provider_daily_request_limits={"youtube": 40},
        provider_cost_per_request_usd={"youtube": 0.0},
        youtube_search_daily_limit=10,
        max_daily_provider_cost_usd=1.0,
        supabase_sync_enabled=False,
    )
    return replace(base, **changes)


def _ingest_family(
    store: MarketTapeStore,
    run_id: str,
    now: datetime,
    *,
    family: str,
    title: str,
    prefix: str,
    query: bool,
) -> None:
    for index, views in enumerate((900_000, 650_000, 400_000), start=1):
        discovery_context = (
            {"surface": "measured_external_query", "queries": [family]}
            if query
            else {}
        )
        store.ingest(MarketContent(
            platform="youtube",
            external_id=f"{prefix}-{index}",
            creator_external_id=f"creator-{prefix}-{index}",
            creator_handle=f"creator_{prefix}_{index}",
            published_at=now - timedelta(hours=index),
            observed_at=now - timedelta(minutes=index),
            source_id="real-archive-fixture",
            metrics=MetricCounters.from_values(
                views=views,
                likes=views // 20,
                comments=views // 200,
                shares=views // 500,
            ),
            title=title,
            url=f"https://www.youtube.com/watch?v={prefix}-{index}",
            raw_payload={
                "external_id": f"{prefix}-{index}",
                "observed_at": (now - timedelta(minutes=index)).isoformat(),
            },
            discovery_context=discovery_context,
        ), run_id)


def _ingest_many_queries(
    store: MarketTapeStore,
    run_id: str,
    now: datetime,
    queries: list[str],
) -> None:
    for index, views in enumerate((900_000, 650_000), start=1):
        store.ingest(MarketContent(
            platform="youtube",
            external_id=f"many-query-video-{index}",
            creator_external_id=f"many-query-creator-{index}",
            creator_handle=f"many_query_creator_{index}",
            published_at=now - timedelta(hours=index),
            observed_at=now - timedelta(minutes=index),
            source_id="real-archive-fixture",
            metrics=MetricCounters.from_values(
                views=views,
                likes=views // 20,
                comments=views // 200,
                shares=views // 500,
            ),
            title="Watch today",
            url=f"https://www.youtube.com/watch?v=many-query-video-{index}",
            raw_payload={"external_id": f"many-query-video-{index}"},
            discovery_context={
                "surface": "measured_external_query",
                "queries": queries,
            },
        ), run_id)


def _write_active_model(config: MarketTapeConfig) -> str:
    model_version = "adaptive-full-cycle-forecast-v1"
    artifact = {
        "contract": "market_tape_trend_predictor_v1",
        "status": "promoted",
        "model_family": "early-breakout-logistic-v3",
        "model_purpose": "early_breakout_entry",
        "model_version": model_version,
        "training_dataset_sha256": "c" * 64,
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
    (config.prediction_model_dir / "active.json").write_text(json.dumps({
        "contract": "market_tape_active_predictor_v1",
        "model_version": model_version,
        "artifact_file": artifact_path.name,
        "artifact_sha256": artifact_sha,
    }, sort_keys=True), encoding="utf-8")
    return model_version


def _seed_ordinary_due_poll(
    store: MarketTapeStore,
    *,
    queue_now: datetime,
) -> str:
    store.start_run("ordinary-due-seed", "archive_bootstrap")
    item = MarketContent(
        platform="youtube",
        external_id="ordinary-due-video",
        creator_external_id="creator-ordinary-due-video",
        published_at=queue_now - timedelta(hours=2),
        observed_at=queue_now - timedelta(hours=1),
        source_id=YouTubeSource.source_id,
        metrics=MetricCounters(views=900, likes=40, comments=4, shares=2),
        title="Measured ordinary due video",
        url="https://youtube.com/watch?v=ordinary-due-video",
        raw_payload={"id": "ordinary-due-video", "views": 900},
    )
    store.ingest(item, "ordinary-due-seed")
    with store.connect() as connection:
        connection.execute(
            "UPDATE mt_poll_queue SET due_at = ? WHERE video_id = ?",
            (
                (queue_now - timedelta(minutes=1)).isoformat(),
                item.video_id,
            ),
        )
    store.finish_run("ordinary-due-seed")
    return item.video_id


def _seed_terminal_forecast(
    store: MarketTapeStore,
    *,
    model_version: str,
    predicted_at: datetime,
    queue_now: datetime,
) -> tuple[str, int]:
    store.start_run("terminal-priority-seed", "discovery")
    item = MarketContent(
        platform="youtube",
        external_id="terminal-forecast-member",
        creator_external_id="creator-terminal-forecast-member",
        published_at=predicted_at - timedelta(hours=2),
        observed_at=predicted_at - timedelta(minutes=5),
        source_id=YouTubeSource.source_id,
        metrics=MetricCounters(views=1000, likes=50, comments=5, shares=2),
        title="Measured terminal forecast member",
        url="https://youtube.com/watch?v=terminal-forecast-member",
        raw_payload={"id": "terminal-forecast-member", "views": 1000},
    )
    store.ingest(item, "terminal-priority-seed")
    trend_id = "trend:terminal-priority"
    with store.connect() as connection:
        connection.execute(
            "UPDATE mt_poll_queue SET due_at = ? WHERE video_id = ?",
            ((queue_now + timedelta(days=1)).isoformat(), item.video_id),
        )
        _insert_test_trend_observation(
            connection,
            trend_id=trend_id,
            observed_at=predicted_at,
        )
        connection.execute(
            """INSERT INTO mt_trend_memberships(
                   trend_id, video_id, confidence, evidence_json, first_seen_at
               ) VALUES(?, ?, 0.95, ?, ?)""",
            (
                trend_id,
                item.video_id,
                json.dumps({"contract": "terminal-priority-test-membership"}),
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
        prediction_id = int(cursor.lastrowid)
        observation_id = int(connection.execute(
            """SELECT observation_id FROM mt_market_observations
               WHERE observation_key = ?""",
            (item.observation_key,),
        ).fetchone()[0])
        connection.execute(
            """INSERT INTO mt_trend_membership_lineage(
                   trend_id, video_id, observation_id, linked_at, contract
               ) VALUES(?, ?, ?, ?, ?)""",
            (
                trend_id,
                item.video_id,
                observation_id,
                predicted_at.isoformat(),
                "market_tape_accepted_observation_evidence_v1",
            ),
        )
    store.finish_run("terminal-priority-seed")
    return item.video_id, prediction_id


def _insert_test_trend_observation(connection, *, trend_id: str, observed_at: datetime) -> None:
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


def _slug(value: str) -> str:
    return "-".join(value.casefold().split()) or "empty"
