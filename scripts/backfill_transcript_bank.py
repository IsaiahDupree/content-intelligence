#!/usr/bin/env python3
"""Run one resumable, performance-ranked local Whisper backfill batch."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPOSITORY_ROOT))

from services.content_quality.transcript_bank import TranscriptBank  # noqa: E402


DEFAULT_TAPE = Path.home() / "Library/Application Support/ContentIntelligence/data/market-tape.sqlite3"
DEFAULT_STORAGE = Path("/Volumes/My Passport/MarketTape/transcript-bank")


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Download and locally Whisper-transcribe the next highest-performing videos."
    )
    parser.add_argument("--limit", type=int, default=20)
    parser.add_argument(
        "--platform", action="append", choices=("youtube", "tiktok", "instagram", "facebook")
    )
    parser.add_argument("--model", default="base")
    parser.add_argument(
        "--topic",
        default="",
        help="Optional related-content filter; requires two metadata and transcript term matches.",
    )
    parser.add_argument("--cookies-from-browser")
    parser.add_argument("--tape", type=Path, default=DEFAULT_TAPE)
    parser.add_argument("--storage-root", type=Path, default=DEFAULT_STORAGE)
    args = parser.parse_args()

    bank = TranscriptBank(args.tape, args.storage_root)
    result = bank.run_backfill(
        limit=max(1, min(args.limit, 500)),
        platforms=args.platform or ("youtube", "tiktok", "instagram", "facebook"),
        model_name=args.model,
        topic=args.topic,
        cookies_from_browser=args.cookies_from_browser,
    )
    compact = {
        "run_id": result["run_id"],
        "status": result["status"],
        "candidate_count": result["candidate_count"],
        "artifact_count": result["artifact_count"],
        "passing_artifact_count": result["passing_artifact_count"],
        "failure_count": result["failure_count"],
        "manifest_path": result["manifest_path"],
    }
    print(json.dumps(compact, indent=2, sort_keys=True))
    return 0 if result["status"] == "completed" else 2


if __name__ == "__main__":
    raise SystemExit(main())
