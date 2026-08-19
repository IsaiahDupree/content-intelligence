"""Documented command surface for Market Tape V1."""

from __future__ import annotations

import argparse
import json
from typing import Any

from .collector import MarketTapeCollector
from .config import MarketTapeConfig
from .daemon import MarketTapeDaemon
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
    daemon = subparsers.add_parser("daemon")
    daemon.add_argument("--once", action="store_true")
    subparsers.add_parser("status")
    sync = subparsers.add_parser("sync")
    sync.add_argument("--force", action="store_true", help="Retry backed-off outbox rows immediately")
    subparsers.add_parser("doctor")
    videos = subparsers.add_parser("videos")
    videos.add_argument("--platform")
    videos.add_argument("--limit", type=int, default=100)
    trends = subparsers.add_parser("trends")
    trends.add_argument("--state")
    trends.add_argument("--limit", type=int, default=100)
    predictions = subparsers.add_parser("predictions")
    predictions.add_argument("--subject-type", choices=["video", "trend"])
    predictions.add_argument("--limit", type=int, default=100)
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
    if args.command == "daemon":
        MarketTapeDaemon(config).run(once=args.once)
        return 0
    if args.command == "status":
        return _print(store.status())
    if args.command == "sync":
        if args.force:
            store.make_outbox_due()
        sink = SupabaseSink(config, store)
        try:
            return _print(sink.flush())
        finally:
            sink.close()
    if args.command == "videos":
        return _print({"videos": store.list_videos(args.limit, args.platform)})
    if args.command == "trends":
        return _print({"trends": store.list_trends(args.limit, args.state)})
    if args.command == "predictions":
        return _print({"predictions": store.list_predictions(args.limit, args.subject_type)})
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
