#!/usr/bin/env python3
"""Run a direct YouTube Data API performance search into Market Tape."""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPOSITORY_ROOT))

from services.market_tape.config import MarketTapeConfig  # noqa: E402
from services.market_tape.models import SourceState, new_run_id  # noqa: E402
from services.market_tape.sources.youtube import YouTubeSource  # noqa: E402
from services.market_tape.store import MarketTapeStore  # noqa: E402


DEFAULT_ENV = (
    Path.home()
    / "Library/Application Support/ContentIntelligence/runtime/.env.market-tape"
)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Discover high-view English YouTube shorts and persist exact API observations."
    )
    parser.add_argument("--query", required=True)
    parser.add_argument("--limit", type=int, default=25)
    parser.add_argument("--language", default="en")
    parser.add_argument("--region", default="US")
    parser.add_argument("--env-file", type=Path, default=DEFAULT_ENV)
    args = parser.parse_args()

    os.environ["MARKET_TAPE_ENV_FILES"] = str(args.env_file.expanduser())
    config = MarketTapeConfig.from_environment()
    store = MarketTapeStore(config)
    run_id = new_run_id()
    store.start_run(run_id, "youtube_performance_search")
    source = YouTubeSource(config, run_id, request_budget=4)
    accepted = 0
    duplicates = 0
    failed = 0
    try:
        batch = source.discover_performance(
            args.query,
            max_items=max(1, min(args.limit, 50)),
            relevance_language=args.language,
            region=args.region,
        )
        for item in batch.items:
            try:
                added, _ = store.ingest(item, run_id)
                if added:
                    accepted += 1
                else:
                    duplicates += 1
            except (TypeError, ValueError, KeyError):
                failed += 1
        batch.receipt.accepted_count = accepted
        batch.receipt.duplicate_count = duplicates
        batch.receipt.failed_count += failed
        if batch.receipt.state == SourceState.READY and failed and not accepted:
            batch.receipt.state = SourceState.DEGRADED
            batch.receipt.error_code = "normalization_failed"
        store.save_receipt(batch.receipt)
        state = "completed" if batch.receipt.state == SourceState.READY else "failed"
        store.finish_run(run_id, state=state, error_detail=batch.receipt.error_detail)
        result = {
            "run_id": run_id,
            "state": state,
            "receipt": batch.receipt.to_dict(),
            "videos": [
                {
                    "external_id": item.external_id,
                    "title": item.title,
                    "views": item.metrics.views,
                    "likes": item.metrics.likes,
                    "comments": item.metrics.comments,
                    "duration_seconds": item.duration_seconds,
                    "url": item.url,
                }
                for item in batch.items
            ],
        }
    finally:
        source.close()
    print(json.dumps(result, indent=2, ensure_ascii=False, sort_keys=True))
    return 0 if result["state"] == "completed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
