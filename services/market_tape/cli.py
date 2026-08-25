"""Documented command surface for Market Tape V1."""

from __future__ import annotations

import argparse
import json
from typing import Any

from .collector import MarketTapeCollector
from .config import MarketTapeConfig
from .daemon import MarketTapeDaemon
from .dataset import MarketTapeDatasetManager
from .intelligence import build_intelligence_snapshot
from .predictor import MarketTapePredictor
from .sources import build_sources
from .sinks import SupabaseSink
from .store import MarketTapeStore


def main() -> int:
    parser = argparse.ArgumentParser(prog="market-tape", description="Autonomous cross-platform social market tape")
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("init")
    cycle = subparsers.add_parser("cycle")
    cycle.add_argument("--mode", choices=["full", "discovery", "recheck"], default="full")
    bootstrap = subparsers.add_parser("bootstrap-local")
    bootstrap.add_argument("--limit-per-platform", type=int, default=10000)
    subparsers.add_parser("backfill-query-attempts")
    reindex = subparsers.add_parser("reindex-trends")
    reindex.add_argument("--forecast-limit", type=int, default=50000)
    daemon = subparsers.add_parser("daemon")
    daemon.add_argument("--once", action="store_true")
    subparsers.add_parser("status")
    intelligence = subparsers.add_parser("intelligence")
    intelligence.add_argument("--limit", type=int, default=25)
    intelligence.add_argument("--window-hours", type=int, default=168)
    intelligence.add_argument("--min-videos", type=int, default=2)
    sync = subparsers.add_parser("sync")
    sync.add_argument("--force", action="store_true", help="Retry backed-off outbox rows immediately")
    sync.add_argument("--reconcile", action="store_true", help="Queue local records missing from the outbox")
    sync.add_argument("--drain", action="store_true", help="Drain bounded batches until empty or blocked")
    sync.add_argument("--max-batches", type=int, default=250)
    subparsers.add_parser("doctor")
    videos = subparsers.add_parser("videos")
    videos.add_argument("--platform")
    videos.add_argument("--limit", type=int, default=100)
    trends = subparsers.add_parser("trends")
    trends.add_argument("--state")
    trends.add_argument("--limit", type=int, default=100)
    keywords = subparsers.add_parser("keywords")
    keywords.add_argument("--limit", type=int, default=100)
    keywords.add_argument("--window-hours", type=int, default=168)
    keywords.add_argument("--min-videos", type=int, default=1)
    query_frontier = subparsers.add_parser("query-frontier")
    query_frontier.add_argument("--limit", type=int, default=100)
    query_frontier.add_argument("--window-hours", type=int, default=168)
    query_frontier.add_argument("--min-videos", type=int, default=2)
    predictions = subparsers.add_parser("predictions")
    predictions.add_argument("--subject-type", choices=["video", "trend"])
    predictions.add_argument("--limit", type=int, default=100)
    query_attempts = subparsers.add_parser("query-attempts")
    query_attempts.add_argument("--platform")
    query_attempts.add_argument("--limit", type=int, default=100)
    subparsers.add_parser("prediction-backtest")
    subparsers.add_parser("evaluate-predictions")
    subparsers.add_parser("train-predictor")
    subparsers.add_parser("predictor-status")
    forecast = subparsers.add_parser("forecast-trends")
    forecast.add_argument("--limit", type=int, default=5000)
    opportunities = subparsers.add_parser("opportunities")
    opportunities.add_argument("--limit", type=int, default=100)
    opportunities.add_argument("--max-saturation", type=float, default=0.75)
    opportunities.add_argument("--min-videos", type=int, default=2)
    opportunities.add_argument("--min-measured-videos", type=int, default=2)
    certify = subparsers.add_parser("certify-dataset")
    certify.add_argument("--date")
    subparsers.add_parser("dataset-status")
    candles = subparsers.add_parser("candles")
    candles.add_argument("--platform")
    candles.add_argument("--window-minutes", type=int, default=15)
    candles.add_argument("--limit", type=int, default=96)
    args = parser.parse_args()

    config = MarketTapeConfig.from_environment()
    store = MarketTapeStore(config)
    if args.command == "init":
        return _print({"state": "initialized", "status": store.status()})
    if args.command == "cycle":
        return _print(MarketTapeCollector(config, store).run_cycle(args.mode))
    if args.command == "bootstrap-local":
        return _print(MarketTapeCollector(config, store).bootstrap_local_archive(args.limit_per_platform))
    if args.command == "backfill-query-attempts":
        return _print(MarketTapeCollector(config, store).backfill_query_attempts())
    if args.command == "reindex-trends":
        return _print(MarketTapeCollector(config, store).reindex_trends(args.forecast_limit))
    if args.command == "daemon":
        MarketTapeDaemon(config).run(once=args.once)
        return 0
    if args.command == "status":
        return _print(store.status())
    if args.command == "intelligence":
        return _print(build_intelligence_snapshot(
            config,
            store,
            limit=args.limit,
            window_hours=args.window_hours,
            minimum_videos=args.min_videos,
        ))
    if args.command == "sync":
        reconciled = store.enqueue_missing_for_sync() if args.reconcile else 0
        if args.force:
            store.make_outbox_due()
        sink = SupabaseSink(config, store)
        try:
            result = sink.drain(args.max_batches) if args.drain else sink.flush()
            result["reconciled_records"] = reconciled
            return _print(result)
        finally:
            sink.close()
    if args.command == "videos":
        return _print({"videos": store.list_videos(args.limit, args.platform)})
    if args.command == "trends":
        return _print({"trends": store.list_trends(args.limit, args.state)})
    if args.command == "keywords":
        return _print({
            "keywords": store.keyword_signals(
                args.limit,
                args.window_hours,
                args.min_videos,
            )
        })
    if args.command == "query-frontier":
        return _print({
            "queries": store.discovery_query_signals(
                args.limit,
                args.window_hours,
                args.min_videos,
            )
        })
    if args.command == "predictions":
        return _print({"predictions": store.list_predictions(args.limit, args.subject_type)})
    if args.command == "query-attempts":
        return _print({"attempts": store.list_query_attempts(args.limit, args.platform)})
    if args.command == "prediction-backtest":
        return _print(store.prediction_backtest())
    if args.command == "evaluate-predictions":
        return _print(store.evaluate_predictions())
    if args.command == "train-predictor":
        return _print(MarketTapePredictor(config, store).train())
    if args.command == "predictor-status":
        return _print(MarketTapePredictor(config, store).status())
    if args.command == "forecast-trends":
        return _print(MarketTapeCollector(
            config,
            store,
        ).reserve_validation_forecasts(limit=args.limit))
    if args.command == "opportunities":
        return _print(store.trend_opportunities(
            limit=args.limit,
            max_saturation=args.max_saturation,
            min_videos=args.min_videos,
            min_measured_videos=args.min_measured_videos,
        ))
    if args.command == "certify-dataset":
        return _print(MarketTapeDatasetManager(config, store).certify(args.date))
    if args.command == "dataset-status":
        return _print(MarketTapeDatasetManager(config, store).status())
    if args.command == "candles":
        return _print({"candles": store.social_candles(args.window_minutes, args.limit, args.platform)})
    if args.command == "doctor":
        sources = build_sources(config, "doctor", store.remaining_request_budget)
        try:
            result = {
                "database": str(config.db_path),
                "object_store": str(config.object_dir),
                "daily_unique_target": config.daily_unique_target,
                "metered_reads_approved": config.allow_metered_reads,
                "supabase_sync_enabled": config.supabase_sync_enabled,
                "sources": [
                    {
                        "source_id": source.source_id,
                        "platform": source.platform,
                        "credential_ready": source.credentials_available(),
                        "missing": source.missing_credentials(),
                        "request_budget_remaining": source.request_budget,
                        "metered": source.metered,
                    }
                    for source in sources
                ],
            }
        finally:
            for source in sources:
                source.close()
        return _print(result)
    return 1


def _print(value: Any) -> int:
    print(json.dumps(value, indent=2, sort_keys=True, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
