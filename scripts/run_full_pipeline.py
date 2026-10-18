#!/usr/bin/env python3
"""Run the entire pipeline in one call: discover candidate videos, then
download, Whisper-transcribe, and vet the highest-performing untranscribed
ones. Chains the discovery cron (run_market_tape_scheduler.sh) and the
transcript backfill cron (backfill_transcript_bank.py) into a single,
on-demand, inspectable run."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPOSITORY_ROOT))

from services.market_tape.full_pipeline import (  # noqa: E402
    DEFAULT_TRANSCRIPT_STORAGE_ROOT,
    run_full_pipeline,
)
from services.content_quality.transcript_bank import (  # noqa: E402
    model_progress_to_stderr,
)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--discovery-mode", choices=("full", "discovery", "recheck"), default="full")
    parser.add_argument("--limit", type=int, default=5, help="Transcript backfill batch size.")
    parser.add_argument(
        "--platform", action="append", choices=("youtube", "tiktok", "instagram", "facebook")
    )
    parser.add_argument("--model", default="base")
    parser.add_argument(
        "--topic", default="",
        help="Optional related-content filter shared by discovery and transcript selection.",
    )
    parser.add_argument("--cookies-from-browser")
    parser.add_argument("--storage-root", type=Path, default=DEFAULT_TRANSCRIPT_STORAGE_ROOT)
    args = parser.parse_args()

    with model_progress_to_stderr():
        result = run_full_pipeline(
            discovery_mode=args.discovery_mode,
            transcript_limit=max(1, min(args.limit, 500)),
            transcript_platforms=args.platform or (
                "youtube", "tiktok", "instagram", "facebook"
            ),
            transcript_model=args.model,
            topic=args.topic,
            transcript_storage_root=args.storage_root,
            cookies_from_browser=args.cookies_from_browser,
        )
    print(json.dumps(result, indent=2, sort_keys=True, default=str))
    return 0 if result["state"] == "completed" else (1 if result["state"] == "failed" else 2)


if __name__ == "__main__":
    raise SystemExit(main())
