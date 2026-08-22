"""Read APIs and a loopback/token-protected cycle trigger."""

from __future__ import annotations

import hmac
import os
import threading
from collections.abc import Callable
from typing import Any

from flask import Flask, jsonify, request

from .collector import MarketTapeCollector
from .config import MarketTapeConfig
from .dataset import MarketTapeDatasetManager
from .full_pipeline import run_full_pipeline
from .intelligence import build_intelligence_snapshot
from .predictor import MarketTapePredictor
from .store import MarketTapeStore
from .sinks import SupabaseSink


def register_market_tape_routes(app: Flask, config: MarketTapeConfig | None = None) -> None:
    resolved = config or MarketTapeConfig.from_environment()
    store = MarketTapeStore(resolved)
    operation_lock = threading.Lock()

    def run_exclusive(operation: Callable[[], Dict[str, Any]]):
        if not operation_lock.acquire(blocking=False):
            return jsonify({
                "error": "market tape operation already running",
                "state": "busy",
            }), 409
        try:
            return jsonify(operation())
        finally:
            operation_lock.release()

    @app.get("/api/market-tape/status")
    def market_tape_status():
        return jsonify(store.status())

    @app.get("/api/market-tape/intelligence")
    def market_tape_intelligence():
        limit = _limit(request.args.get("limit"), 25, maximum=100)
        window = _limit(request.args.get("window_hours"), 168, maximum=24 * 90)
        minimum = _limit(request.args.get("min_videos"), 2, maximum=1000)
        return jsonify(build_intelligence_snapshot(
            resolved,
            store,
            limit=limit,
            window_hours=window,
            minimum_videos=minimum,
        ))

    @app.get("/api/market-tape/sources")
    def market_tape_sources():
        return jsonify({"sources": store.status()["sources"]})

    @app.get("/api/market-tape/videos")
    def market_tape_videos():
        limit = _limit(request.args.get("limit"), 100)
        return jsonify({"videos": store.list_videos(limit, request.args.get("platform"))})

    @app.get("/api/market-tape/trends")
    def market_tape_trends():
        limit = _limit(request.args.get("limit"), 100)
        return jsonify({"trends": store.list_trends(limit, request.args.get("state"))})

    @app.get("/api/market-tape/keywords")
    def market_tape_keywords():
        limit = _limit(request.args.get("limit"), 100)
        window = _limit(request.args.get("window_hours"), 168, maximum=24 * 90)
        minimum = _limit(request.args.get("min_videos"), 1, maximum=1000)
        return jsonify({
            "keywords": store.keyword_signals(limit, window, minimum),
            "window_hours": window,
            "min_videos": minimum,
        })

    @app.get("/api/market-tape/query-frontier")
    def market_tape_query_frontier():
        limit = _limit(request.args.get("limit"), 100)
        window = _limit(request.args.get("window_hours"), 168, maximum=24 * 90)
        minimum = _limit(request.args.get("min_videos"), 2, maximum=1000)
        return jsonify({
            "queries": store.discovery_query_signals(limit, window, minimum),
            "window_hours": window,
            "min_videos": minimum,
        })

    @app.get("/api/market-tape/runs")
    def market_tape_runs():
        return jsonify({"runs": store.list_runs(_limit(request.args.get("limit"), 50))})

    @app.get("/api/market-tape/predictions")
    def market_tape_predictions():
        limit = _limit(request.args.get("limit"), 100)
        return jsonify({"predictions": store.list_predictions(limit, request.args.get("subject_type"))})

    @app.get("/api/market-tape/prediction-backtest")
    def market_tape_prediction_backtest():
        return jsonify(store.prediction_backtest())

    @app.get("/api/market-tape/calibration")
    def market_tape_calibration_history():
        limit = _limit(request.args.get("limit"), 50, maximum=500)
        return jsonify({"calibration": store.calibration_history(limit)})

    @app.post("/api/market-tape/calibration/record")
    def market_tape_record_calibration():
        if not _authorized():
            return jsonify({"error": "local control token required"}), 401
        return jsonify(store.record_calibration())

    @app.get("/api/market-tape/opportunities")
    def market_tape_opportunities():
        limit = _limit(request.args.get("limit"), 100, maximum=500)
        max_saturation = _bounded_float(
            request.args.get("max_saturation"), 0.75, minimum=0.0, maximum=1.0
        )
        min_videos = _limit(request.args.get("min_videos"), 2, maximum=10000)
        min_measured_videos = _limit(
            request.args.get("min_measured_videos"),
            2,
            maximum=10000,
        )
        return jsonify(store.trend_opportunities(
            limit=limit,
            max_saturation=max_saturation,
            min_videos=min_videos,
            min_measured_videos=min_measured_videos,
        ))

    @app.get("/api/market-tape/predictions/model")
    def market_tape_predictor_status():
        return jsonify(MarketTapePredictor(resolved, store).status())

    @app.get("/api/market-tape/query-attempts")
    def market_tape_query_attempts():
        limit = _limit(request.args.get("limit"), 100, maximum=5000)
        return jsonify({
            "attempts": store.list_query_attempts(limit, request.args.get("platform")),
        })

    @app.get("/api/market-tape/datasets/status")
    def market_tape_dataset_status():
        return jsonify(MarketTapeDatasetManager(resolved, store).status())

    @app.get("/api/market-tape/candles")
    def market_tape_candles():
        limit = _limit(request.args.get("limit"), 96)
        window = _limit(request.args.get("window_minutes"), 15)
        return jsonify({
            "candles": store.social_candles(window, limit, request.args.get("platform")),
        })

    @app.post("/api/market-tape/cycles")
    def market_tape_cycle():
        if not _authorized():
            return jsonify({"error": "local control token required"}), 401
        body: Any = request.get_json(silent=True) or {}
        mode = str(body.get("mode", "full"))
        if mode not in {"full", "discovery", "recheck"}:
            return jsonify({"error": "mode must be full, discovery, or recheck"}), 400
        return run_exclusive(lambda: MarketTapeCollector(resolved, store).run_cycle(mode))

    @app.post("/api/market-tape/tick")
    def market_tape_tick():
        """Run the next due autonomous cycle without delegating policy to launchd."""
        if not _authorized():
            return jsonify({"error": "local control token required"}), 401
        elapsed = store.seconds_since_discovery()
        mode = (
            "full"
            if elapsed is None or elapsed >= resolved.discovery_interval_seconds
            else "recheck"
        )
        return run_exclusive(lambda: MarketTapeCollector(resolved, store).run_cycle(mode))

    @app.post("/api/market-tape/full-pipeline")
    def market_tape_full_pipeline():
        """Discover candidate videos, then download, Whisper-transcribe, and
        vet the highest-performing untranscribed ones -- in one call."""
        if not _authorized():
            return jsonify({"error": "local control token required"}), 401
        body: Any = request.get_json(silent=True) or {}
        mode = str(body.get("discovery_mode", "full"))
        if mode not in {"full", "discovery", "recheck"}:
            return jsonify({"error": "discovery_mode must be full, discovery, or recheck"}), 400
        platforms = body.get("platforms") or ["youtube", "tiktok", "instagram", "facebook"]
        limit = _limit(body.get("limit"), 5, maximum=200)
        topic = str(body.get("topic", ""))
        model = str(body.get("model", "base"))
        trend_ids = body.get("trend_ids") or []
        if not isinstance(trend_ids, list) or any(
            not isinstance(value, str) or not value.strip()
            for value in trend_ids
        ) or len(trend_ids) > 25:
            return jsonify({"error": "trend_ids must be an array of at most 25 non-empty strings"}), 400
        return run_exclusive(lambda: run_full_pipeline(
            config=resolved,
            store=store,
            collector=MarketTapeCollector(resolved, store),
            discovery_mode=mode,
            transcript_limit=limit,
            transcript_platforms=platforms,
            transcript_model=model,
            topic=topic,
            transcript_trend_ids=trend_ids,
        ))

    @app.post("/api/market-tape/bootstrap-local")
    def market_tape_bootstrap_local():
        if not _authorized():
            return jsonify({"error": "local control token required"}), 401
        body: Any = request.get_json(silent=True) or {}
        limit = _limit(body.get("limit_per_platform"), 10000, maximum=100000)
        return run_exclusive(
            lambda: MarketTapeCollector(resolved, store).bootstrap_local_archive(limit)
        )

    @app.post("/api/market-tape/query-attempts/backfill")
    def market_tape_backfill_query_attempts():
        if not _authorized():
            return jsonify({"error": "local control token required"}), 401
        return run_exclusive(
            lambda: MarketTapeCollector(resolved, store).backfill_query_attempts()
        )

    @app.post("/api/market-tape/trends/reindex")
    def market_tape_reindex_trends():
        if not _authorized():
            return jsonify({"error": "local control token required"}), 401
        body: Any = request.get_json(silent=True) or {}
        limit = _limit(body.get("forecast_limit"), 50000, maximum=100000)
        return run_exclusive(
            lambda: MarketTapeCollector(resolved, store).reindex_trends(limit)
        )

    @app.post("/api/market-tape/sync")
    def market_tape_sync():
        if not _authorized():
            return jsonify({"error": "local control token required"}), 401
        body: Any = request.get_json(silent=True) or {}
        def flush() -> Dict[str, Any]:
            reconciled = store.enqueue_missing_for_sync() if body.get("reconcile") is True else 0
            if body.get("force") is True:
                store.make_outbox_due()
            sink = SupabaseSink(resolved, store)
            try:
                if body.get("drain") is True:
                    max_batches = _limit(body.get("max_batches"), 250, maximum=1000)
                    result = sink.drain(max_batches)
                else:
                    result = sink.flush()
                result["reconciled_records"] = reconciled
                return result
            finally:
                sink.close()

        return run_exclusive(flush)

    @app.post("/api/market-tape/predictions/evaluate")
    def market_tape_evaluate_predictions():
        if not _authorized():
            return jsonify({"error": "local control token required"}), 401
        return run_exclusive(store.evaluate_predictions)

    @app.post("/api/market-tape/predictions/train")
    def market_tape_train_predictor():
        if not _authorized():
            return jsonify({"error": "local control token required"}), 401
        return run_exclusive(
            lambda: MarketTapePredictor(resolved, store).train()
        )

    @app.post("/api/market-tape/predictions/forecast")
    def market_tape_forecast_trends():
        if not _authorized():
            return jsonify({"error": "local control token required"}), 401
        body: Any = request.get_json(silent=True) or {}
        limit = _limit(body.get("limit"), 5000, maximum=20000)
        return run_exclusive(lambda: store.forecast_active_trends(limit=limit))

    @app.post("/api/market-tape/datasets/certify")
    def market_tape_certify_dataset():
        if not _authorized():
            return jsonify({"error": "local control token required"}), 401
        body: Any = request.get_json(silent=True) or {}
        target_date = body.get("date")
        return run_exclusive(
            lambda: MarketTapeDatasetManager(resolved, store).certify(target_date)
        )


def _authorized() -> bool:
    configured = os.getenv("MARKET_TAPE_CONTROL_TOKEN", "").strip()
    if configured:
        supplied = request.headers.get("Authorization", "").removeprefix("Bearer ").strip()
        return hmac.compare_digest(configured, supplied)
    return request.remote_addr in {"127.0.0.1", "::1", "localhost"}


def _limit(value: Any, default: int, maximum: int = 1000) -> int:
    try:
        return min(maximum, max(1, int(value)))
    except (TypeError, ValueError):
        return default


def _bounded_float(
    value: Any,
    default: float,
    minimum: float,
    maximum: float,
) -> float:
    try:
        return min(maximum, max(minimum, float(value)))
    except (TypeError, ValueError):
        return default
