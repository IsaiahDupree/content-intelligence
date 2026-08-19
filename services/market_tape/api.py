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

    @app.get("/api/market-tape/runs")
    def market_tape_runs():
        return jsonify({"runs": store.list_runs(_limit(request.args.get("limit"), 50))})

    @app.get("/api/market-tape/predictions")
    def market_tape_predictions():
        limit = _limit(request.args.get("limit"), 100)
        return jsonify({"predictions": store.list_predictions(limit, request.args.get("subject_type"))})

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

    @app.post("/api/market-tape/bootstrap-local")
    def market_tape_bootstrap_local():
        if not _authorized():
            return jsonify({"error": "local control token required"}), 401
        body: Any = request.get_json(silent=True) or {}
        limit = _limit(body.get("limit_per_platform"), 10000, maximum=100000)
        return run_exclusive(
            lambda: MarketTapeCollector(resolved, store).bootstrap_local_archive(limit)
        )

    @app.post("/api/market-tape/sync")
    def market_tape_sync():
        if not _authorized():
            return jsonify({"error": "local control token required"}), 401
        body: Any = request.get_json(silent=True) or {}
        def flush() -> Dict[str, Any]:
            if body.get("force") is True:
                store.make_outbox_due()
            sink = SupabaseSink(resolved, store)
            try:
                if body.get("drain") is True:
                    max_batches = _limit(body.get("max_batches"), 250, maximum=1000)
                    return sink.drain(max_batches)
                return sink.flush()
            finally:
                sink.close()

        return run_exclusive(flush)


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
