#!/usr/bin/env python3
"""Ingest signed-in yt-dlp search results as audited Market Tape observations."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPOSITORY_ROOT))

from services.market_tape.config import MarketTapeConfig  # noqa: E402
from services.market_tape.models import (  # noqa: E402
    MarketContent,
    MetricCounters,
    SourceReceipt,
    SourceState,
    new_run_id,
    utc_now,
)
from services.market_tape.store import MarketTapeStore  # noqa: E402
from services.market_tape.sinks import SupabaseSink  # noqa: E402


DEFAULT_ENV = (
    Path.home()
    / "Library/Application Support/ContentIntelligence/runtime/.env.market-tape"
)
SOURCE_ID = "youtube-yt-dlp-signed-search"


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def published_at(record: dict[str, Any]) -> datetime | None:
    timestamp = record.get("timestamp")
    if timestamp is not None:
        try:
            return datetime.fromtimestamp(float(timestamp), tz=timezone.utc)
        except (TypeError, ValueError, OSError):
            pass
    value = str(record.get("upload_date") or "")
    if len(value) == 8 and value.isdigit():
        return datetime.strptime(value, "%Y%m%d").replace(tzinfo=timezone.utc)
    return None


def safe_record(record: dict[str, Any], queries: list[str]) -> dict[str, Any]:
    """Archive useful source evidence without cookies, formats, or transient URLs."""

    allowed = (
        "id", "title", "description", "view_count", "like_count", "comment_count",
        "repost_count", "duration", "channel_id", "channel", "channel_follower_count",
        "uploader_id", "uploader", "upload_date", "timestamp", "language",
        "webpage_url", "original_url", "thumbnail", "availability", "live_status",
        "extractor", "extractor_key",
    )
    payload = {key: record.get(key) for key in allowed if record.get(key) is not None}
    payload["discovery_queries"] = sorted(set(queries))
    payload["ingestion_contract"] = "signed_in_yt_dlp_search_v1"
    return payload


def parse_input(value: str) -> tuple[str, Path]:
    if "=" not in value:
        raise argparse.ArgumentTypeError("input must use QUERY=/absolute/path.jsonl")
    query, raw_path = value.split("=", 1)
    path = Path(raw_path).expanduser().resolve()
    if not query.strip() or not path.is_file():
        raise argparse.ArgumentTypeError(f"invalid query or missing JSONL file: {value}")
    return query.strip(), path


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Persist signed-in YouTube search observations with query/file provenance."
    )
    parser.add_argument(
        "--input",
        action="append",
        required=True,
        type=parse_input,
        help="Repeatable QUERY=/absolute/path.jsonl input.",
    )
    parser.add_argument("--env-file", type=Path, default=DEFAULT_ENV)
    parser.add_argument("--manifest", type=Path)
    args = parser.parse_args()

    os.environ["MARKET_TAPE_ENV_FILES"] = str(args.env_file.expanduser())
    config = MarketTapeConfig.from_environment()
    store = MarketTapeStore(config)
    run_id = new_run_id()
    started_at = utc_now()
    store.start_run(run_id, "youtube_signed_search_ingest")

    source_files: list[dict[str, Any]] = []
    merged: dict[str, dict[str, Any]] = {}
    failed_lines: list[dict[str, Any]] = []
    for query, path in args.input:
        source_files.append({
            "query": query,
            "path": str(path),
            "sha256": file_sha256(path),
            "bytes": path.stat().st_size,
        })
        for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            try:
                record = json.loads(line)
            except json.JSONDecodeError as exc:
                failed_lines.append({
                    "path": str(path), "line": line_number, "error": str(exc),
                })
                continue
            external_id = str(record.get("id") or "").strip()
            if not external_id:
                failed_lines.append({
                    "path": str(path), "line": line_number, "error": "missing id",
                })
                continue
            entry = merged.setdefault(external_id, {"record": record, "queries": []})
            entry["queries"].append(query)
            if int(record.get("view_count") or 0) > int(entry["record"].get("view_count") or 0):
                entry["record"] = record

    accepted = 0
    duplicate_observations = 0
    unique_videos = 0
    ingestion_failures: list[dict[str, str]] = []
    for external_id, entry in merged.items():
        record = entry["record"]
        queries = sorted(set(entry["queries"]))
        raw_payload = safe_record(record, queries)
        item = MarketContent(
            platform="youtube",
            external_id=external_id,
            creator_external_id=str(
                record.get("channel_id") or record.get("uploader_id") or "unknown"
            ),
            creator_handle=str(record.get("uploader_id") or ""),
            creator_name=str(record.get("channel") or record.get("uploader") or ""),
            creator_followers=int(record.get("channel_follower_count") or 0),
            published_at=published_at(record),
            observed_at=started_at,
            source_id=SOURCE_ID,
            metrics=MetricCounters.from_values(
                views=record.get("view_count"),
                likes=record.get("like_count"),
                comments=record.get("comment_count"),
                shares=record.get("repost_count"),
            ),
            title=str(record.get("title") or ""),
            description=str(record.get("description") or ""),
            language=str(record.get("language") or ""),
            url=str(
                record.get("webpage_url")
                or record.get("original_url")
                or f"https://www.youtube.com/watch?v={external_id}"
            ),
            thumbnail_url=str(record.get("thumbnail") or ""),
            duration_seconds=float(record.get("duration") or 0.0) or None,
            raw_payload=raw_payload,
            discovery_context={
                "surface": "youtube_signed_in_search",
                "queries": queries,
                "source_id": SOURCE_ID,
            },
        )
        try:
            added, unique = store.ingest(item, run_id)
            accepted += int(added)
            duplicate_observations += int(not added)
            unique_videos += int(unique)
        except (KeyError, TypeError, ValueError, OSError) as exc:
            ingestion_failures.append({
                "external_id": external_id,
                "error_type": type(exc).__name__,
                "error": str(exc)[:500],
            })

    finished_at = utc_now()
    failed_count = len(failed_lines) + len(ingestion_failures)
    receipt = SourceReceipt(
        run_id=run_id,
        source_id=SOURCE_ID,
        platform="youtube",
        state=SourceState.READY if accepted and not failed_count else SourceState.DEGRADED,
        started_at=started_at,
        finished_at=finished_at,
        request_count=len(source_files),
        discovered_count=sum(
            1 for _, path in args.input for line in path.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ),
        accepted_count=accepted,
        duplicate_count=duplicate_observations,
        failed_count=failed_count,
        error_code="" if not failed_count else "partial_jsonl_ingestion_failure",
        error_detail="" if not failed_count else f"{failed_count} records failed validation/ingestion",
        metadata={
            "contract": "signed_in_yt_dlp_search_receipt_v1",
            "source_files": source_files,
            "deduplicated_video_count": len(merged),
            "unique_videos_added": unique_videos,
            "failed_lines": failed_lines[:25],
            "ingestion_failures": ingestion_failures[:25],
        },
    )
    store.save_receipt(receipt)
    final_state = "completed" if receipt.state == SourceState.READY else "degraded"
    store.finish_run(run_id, final_state, receipt.error_detail)
    outbox_records = store.enqueue_run_for_sync(run_id)
    sink = SupabaseSink(config, store)
    try:
        central_sync = sink.flush()
    finally:
        sink.close()

    result = {
        "run_id": run_id,
        "state": final_state,
        "source_id": SOURCE_ID,
        "input_record_count": receipt.discovered_count,
        "deduplicated_video_count": len(merged),
        "observations_added": accepted,
        "unique_videos_added": unique_videos,
        "duplicate_observations": duplicate_observations,
        "failed_count": failed_count,
        "outbox_records": outbox_records,
        "central_sync": central_sync,
        "source_files": source_files,
    }
    manifest = args.manifest or (
        Path("/Volumes/My Passport/MarketTape/transcript-bank/runs")
        / f"{run_id}-signed-search-ingest.json"
    )
    manifest.parent.mkdir(parents=True, exist_ok=True)
    manifest.write_text(
        json.dumps(result, indent=2, ensure_ascii=False, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    print(json.dumps({**result, "manifest_path": str(manifest)}, indent=2, sort_keys=True))
    return 0 if final_state == "completed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
