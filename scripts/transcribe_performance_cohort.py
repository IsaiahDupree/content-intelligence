#!/usr/bin/env python3
"""Download, Whisper-transcribe, bind, and audit a Market Tape cohort."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPOSITORY_ROOT))

from services.content_quality.transcript_bank import (  # noqa: E402
    model_progress_to_stderr,
    transcribe_cohort,
)


DEFAULT_TAPE = (
    Path.home()
    / "Library/Application Support/ContentIntelligence/data/market-tape.sqlite3"
)
PASSPORT_ROOT = Path("/Volumes/My Passport/MarketTape/transcript-bank")
DEFAULT_STORAGE = (
    PASSPORT_ROOT
    if PASSPORT_ROOT.parent.parent.exists()
    else Path.home()
    / "Library/Application Support/ContentIntelligence/data/transcript-bank"
)


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(
        description=(
            "Read real Market Tape performance observations, download source audio, "
            "transcribe locally with Whisper, and persist immutable audit receipts."
        )
    )
    result.add_argument("--topic", required=True)
    result.add_argument(
        "--video-id",
        action="append",
        default=[],
        help="Exact platform external ID; repeat for a curated cohort.",
    )
    result.add_argument(
        "--platform",
        action="append",
        default=[],
        choices=("youtube", "tiktok", "instagram", "facebook"),
    )
    result.add_argument("--limit", type=int, default=12)
    result.add_argument("--model", default="base")
    result.add_argument("--target-language", default="en")
    result.add_argument("--minimum-topic-matches", type=int, default=1)
    result.add_argument("--tape", type=Path, default=DEFAULT_TAPE)
    result.add_argument("--storage-root", type=Path, default=DEFAULT_STORAGE)
    result.add_argument("--cookies-from-browser")
    result.add_argument("--force", action="store_true")
    result.add_argument("--output", type=Path)
    return result


def main() -> int:
    args = parser().parse_args()
    with model_progress_to_stderr():
        result = transcribe_cohort(
            tape_path=args.tape,
            storage_root=args.storage_root,
            topic=args.topic,
            external_ids=args.video_id,
            platforms=args.platform or (
                "youtube", "tiktok", "instagram", "facebook"
            ),
            limit=max(1, min(args.limit, 100)),
            model_name=args.model,
            minimum_topic_matches=max(1, args.minimum_topic_matches),
            force=args.force,
            cookies_from_browser=args.cookies_from_browser,
            target_language=args.target_language,
        )
    rendered = json.dumps(result, indent=2, ensure_ascii=False, sort_keys=True)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(rendered + "\n", encoding="utf-8")
    print(rendered)
    return 0 if result.get("decision") == "PASS" else 2


if __name__ == "__main__":
    raise SystemExit(main())
