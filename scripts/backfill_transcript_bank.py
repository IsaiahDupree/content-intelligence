#!/usr/bin/env python3
"""Run one resumable, performance-ranked local Whisper backfill batch."""

from __future__ import annotations

import argparse
import fcntl
import json
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPOSITORY_ROOT))

from services.content_quality.transcript_bank import (  # noqa: E402
    TranscriptBank,
    atomic_write_json,
    canonical_sha256,
    model_progress_to_stderr,
    storage_mount_error,
)


DEFAULT_TAPE = Path.home() / "Library/Application Support/ContentIntelligence/data/market-tape.sqlite3"
DEFAULT_STORAGE = Path("/Volumes/My Passport/MarketTape/transcript-bank")


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _write_busy_receipt(storage_root: Path, lock_path: Path) -> Path:
    run_id = f"transcript_busy_{uuid.uuid4().hex}"
    receipt = {
        "contract": "transcript_backfill_singleton_busy_v1",
        "run_id": run_id,
        "status": "busy_existing_worker",
        "lock_path": str(lock_path),
        "observed_at": _utc_now(),
    }
    receipt["receipt_sha256"] = canonical_sha256(receipt)
    receipt_root = storage_root / "runs"
    receipt_root.mkdir(parents=True, exist_ok=True)
    receipt_path = receipt_root / f"{run_id}.json"
    atomic_write_json(receipt_path, receipt)
    return receipt_path


def exit_code_for_status(status: str) -> int:
    return 0 if status in {"completed", "partial"} else 2


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

    mount_error = storage_mount_error(args.storage_root)
    if mount_error:
        print(json.dumps({
            "status": "blocked_storage_not_mounted",
            "error": mount_error,
        }, indent=2, sort_keys=True))
        return 2
    args.storage_root.mkdir(parents=True, exist_ok=True)
    lock_path = args.storage_root / ".transcript-backfill.lock"
    lock_handle = lock_path.open("a+", encoding="utf-8")
    try:
        try:
            fcntl.flock(lock_handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            receipt_path = _write_busy_receipt(args.storage_root, lock_path)
            print(json.dumps({
                "status": "busy_existing_worker",
                "receipt_path": str(receipt_path),
            }, indent=2, sort_keys=True))
            return 0
        bank = TranscriptBank(args.tape, args.storage_root)
        with model_progress_to_stderr():
            result = bank.run_backfill(
                limit=max(1, min(args.limit, 500)),
                platforms=args.platform or (
                    "youtube", "tiktok", "instagram", "facebook"
                ),
                model_name=args.model,
                topic=args.topic,
                cookies_from_browser=args.cookies_from_browser,
            )
    finally:
        try:
            fcntl.flock(lock_handle.fileno(), fcntl.LOCK_UN)
        finally:
            lock_handle.close()
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
    return exit_code_for_status(result["status"])


if __name__ == "__main__":
    raise SystemExit(main())
