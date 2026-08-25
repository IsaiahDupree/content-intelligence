#!/usr/bin/env python3
"""Explicitly backfill immutable payload snapshots for legacy transcripts."""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPOSITORY_ROOT))

from services.content_quality.transcript_bank import TranscriptBank  # noqa: E402


DEFAULT_TAPE = (
    Path.home()
    / "Library/Application Support/ContentIntelligence/data/market-tape.sqlite3"
)
DEFAULT_STORAGE = Path(
    os.environ.get("TRANSCRIPT_BANK_ROOT")
    or Path.home()
    / "Library/Application Support/ContentQuality/data/transcript-bank"
)


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(
        description=(
            "Read only an explicitly selected, bounded set of legacy transcript "
            "payload files and persist append-only SQLite hash snapshots. This "
            "command is never invoked by the script-generation path."
        )
    )
    selection = result.add_mutually_exclusive_group(required=True)
    selection.add_argument(
        "--transcript-id",
        action="append",
        default=[],
        help="Exact legacy transcript ID; repeat for a bounded set.",
    )
    selection.add_argument(
        "--cohort-manifest",
        type=Path,
        help="Exact cohort manifest whose member transcript IDs should be backfilled.",
    )
    result.add_argument("--limit", type=int, default=5)
    result.add_argument("--read-timeout-seconds", type=float, default=5.0)
    result.add_argument("--tape", type=Path, default=DEFAULT_TAPE)
    result.add_argument("--storage-root", type=Path, default=DEFAULT_STORAGE)
    return result


def transcript_ids_from_manifest(path: Path) -> list[str]:
    try:
        payload = json.loads(path.expanduser().resolve().read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError(
            f"cohort manifest is unreadable: {type(exc).__name__}"
        ) from exc
    if not isinstance(payload, dict) or not isinstance(payload.get("members"), list):
        raise ValueError("cohort manifest must contain a members array")
    transcript_ids = [
        str(member.get("transcript_id") or "").strip()
        for member in payload["members"]
        if isinstance(member, dict)
        and str(member.get("transcript_id") or "").strip()
    ]
    if not transcript_ids:
        raise ValueError("cohort manifest contains no transcript IDs")
    return transcript_ids


def main() -> int:
    args = parser().parse_args()
    if args.limit < 1 or args.limit > 100:
        raise SystemExit("--limit must be between 1 and 100")
    if args.read_timeout_seconds < 0.05 or args.read_timeout_seconds > 60:
        raise SystemExit("--read-timeout-seconds must be between 0.05 and 60")
    try:
        transcript_ids = (
            transcript_ids_from_manifest(args.cohort_manifest)
            if args.cohort_manifest else args.transcript_id
        )
        result = TranscriptBank(
            args.tape, args.storage_root
        ).backfill_transcript_payload_snapshots(
            transcript_ids=transcript_ids,
            limit=args.limit,
            read_timeout_seconds=args.read_timeout_seconds,
        )
    except (FileNotFoundError, RuntimeError, ValueError) as exc:
        print(json.dumps({
            "contract": "transcript_payload_snapshot_backfill_v1",
            "status": "failed",
            "error_type": type(exc).__name__,
            "error": str(exc),
        }, indent=2, sort_keys=True))
        return 2
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0 if result["status"] == "completed" else 2


if __name__ == "__main__":
    raise SystemExit(main())
