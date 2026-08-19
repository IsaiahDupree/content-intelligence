#!/usr/bin/env python3
"""Run auditable signed-in YouTube searches and optionally ingest the results."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
INGEST_SCRIPT = REPOSITORY_ROOT / "scripts/ingest_yt_dlp_search.py"


@dataclass(frozen=True)
class QueryReceipt:
    query: str
    output_path: str
    output_sha256: str
    records: int
    elapsed_seconds: float
    command: list[str]
    state: str
    error: str = ""


def slugify(value: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")
    return slug[:96] or "query"


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def load_queries(values: list[str], query_file: Path | None) -> list[str]:
    queries = [value.strip() for value in values if value.strip()]
    if query_file:
        queries.extend(
            line.strip()
            for line in query_file.read_text(encoding="utf-8").splitlines()
            if line.strip() and not line.lstrip().startswith("#")
        )
    return list(dict.fromkeys(queries))


def classify_query_state(returncode: int, records: int) -> str:
    if returncode == 0:
        return "completed" if records else "empty"
    return "partial" if records else "failed"


def research_query(
    query: str,
    output_dir: Path,
    limit: int,
    candidate_multiplier: int,
    max_age_days: int,
    cookies_from_browser: str,
    timeout_seconds: int,
) -> QueryReceipt:
    output_path = output_dir / f"{slugify(query)}.jsonl"
    command = [
        "yt-dlp",
        "--cookies-from-browser",
        cookies_from_browser,
        "--skip-download",
        "--dump-json",
        "--no-warnings",
        "--socket-timeout",
        "15",
        "--retries",
        "1",
        "--extractor-retries",
        "1",
        "--dateafter",
        f"now-{max_age_days}days",
        f"ytsearch{limit * candidate_multiplier}:{query}",
    ]
    started = time.monotonic()
    try:
        result = subprocess.run(
            command,
            capture_output=True,
            text=True,
            timeout=timeout_seconds,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        return QueryReceipt(
            query=query,
            output_path=str(output_path),
            output_sha256="",
            records=0,
            elapsed_seconds=round(time.monotonic() - started, 3),
            command=command,
            state="timed_out",
            error=str(exc)[:500],
        )

    output = result.stdout.strip()
    if output:
        output_path.write_text(output + "\n", encoding="utf-8")
    records = len(output.splitlines()) if output else 0
    state = classify_query_state(result.returncode, records)
    return QueryReceipt(
        query=query,
        output_path=str(output_path),
        output_sha256=file_sha256(output_path) if output_path.is_file() else "",
        records=records,
        elapsed_seconds=round(time.monotonic() - started, 3),
        command=command,
        state=state,
        error=result.stderr.strip()[-500:] if state in {"failed", "partial"} else "",
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--query", action="append", default=[])
    parser.add_argument("--query-file", type=Path)
    parser.add_argument("--output-dir", required=True, type=Path)
    parser.add_argument("--manifest", type=Path)
    parser.add_argument("--limit", type=int, default=5)
    parser.add_argument("--candidate-multiplier", type=int, default=2)
    parser.add_argument("--max-age-days", type=int, default=3)
    parser.add_argument("--workers", type=int, default=2)
    parser.add_argument("--cookies-from-browser", default="chrome")
    parser.add_argument("--timeout-seconds", type=int, default=180)
    parser.add_argument("--no-ingest", action="store_true")
    args = parser.parse_args()

    queries = load_queries(args.query, args.query_file)
    if not queries:
        parser.error("provide at least one --query or --query-file")
    if not 1 <= args.limit <= 25:
        parser.error("--limit must be between 1 and 25")

    args.output_dir.mkdir(parents=True, exist_ok=True)
    started_at = datetime.now(timezone.utc)
    receipts: list[QueryReceipt] = []
    with ThreadPoolExecutor(max_workers=max(1, min(args.workers, 6))) as pool:
        futures = {
            pool.submit(
                research_query,
                query,
                args.output_dir,
                args.limit,
                max(1, min(args.candidate_multiplier, 5)),
                max(1, min(args.max_age_days, 30)),
                args.cookies_from_browser,
                args.timeout_seconds,
            ): query
            for query in queries
        }
        for future in as_completed(futures):
            receipt = future.result()
            receipts.append(receipt)
            print(
                f"[{receipt.state}] {receipt.query}: "
                f"{receipt.records} records in {receipt.elapsed_seconds}s",
                flush=True,
            )

    receipts.sort(key=lambda item: queries.index(item.query))
    completed = [receipt for receipt in receipts if receipt.state == "completed"]
    partial = [receipt for receipt in receipts if receipt.state == "partial"]
    ingestable = completed + partial
    successful = [
        receipt for receipt in receipts if receipt.state in {"completed", "empty"}
    ]
    ingest_result: dict[str, object] | None = None
    if ingestable and not args.no_ingest:
        command = [sys.executable, str(INGEST_SCRIPT)]
        for receipt in ingestable:
            command.extend(["--input", f"{receipt.query}={receipt.output_path}"])
        result = subprocess.run(command, capture_output=True, text=True, check=False)
        try:
            ingest_result = json.loads(result.stdout)
        except json.JSONDecodeError:
            ingest_result = {
                "state": "failed",
                "returncode": result.returncode,
                "error": (result.stderr or result.stdout)[-1000:],
            }

    payload = {
        "contract": "youtube_query_research_receipt_v1",
        "started_at": started_at.isoformat(),
        "finished_at": datetime.now(timezone.utc).isoformat(),
        "query_count": len(queries),
        "completed_query_count": len(successful),
        "empty_query_count": len(successful) - len(completed),
        "partial_query_count": len(partial),
        "failed_query_count": sum(receipt.state == "failed" for receipt in receipts),
        "timed_out_query_count": sum(receipt.state == "timed_out" for receipt in receipts),
        "record_count": sum(receipt.records for receipt in ingestable),
        "limit_per_query": args.limit,
        "cookies_from_browser": args.cookies_from_browser,
        "receipts": [asdict(receipt) for receipt in receipts],
        "ingest": ingest_result,
    }
    manifest = args.manifest or args.output_dir / "research-manifest.json"
    manifest.write_text(
        json.dumps(payload, indent=2, ensure_ascii=False, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    print(json.dumps({**payload, "manifest_path": str(manifest)}, indent=2))
    return 0 if len(successful) == len(queries) and (
        args.no_ingest
        or not ingestable
        or (ingest_result or {}).get("state") == "completed"
    ) else 1


if __name__ == "__main__":
    raise SystemExit(main())
