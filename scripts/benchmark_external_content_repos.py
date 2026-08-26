#!/usr/bin/env python3
"""Validate or execute the external content-repository transcript benchmark."""

from __future__ import annotations

import argparse
import json
import os
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date
from pathlib import Path

from dotenv import dotenv_values


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from services.content_quality.repo_benchmark import (  # noqa: E402
    DEFAULT_CORPUS_ID,
    DEFAULT_MODEL,
    ContentQualityClient,
    assemble_run,
    configured_ai_client,
    enforce_peer_diversity,
    load_registry,
    run_profile,
    verify_checkouts,
    write_artifacts,
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Run a clean-room comparison of content repository methods."
    )
    parser.add_argument(
        "--registry",
        type=Path,
        default=PROJECT_ROOT / "config" / "external_content_repo_benchmark.json",
    )
    parser.add_argument(
        "--source-root",
        type=Path,
        default=PROJECT_ROOT.parent / "content-factory-repo-lab" / "sources",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=(
            PROJECT_ROOT
            / "docs"
            / "benchmarks"
            / "external-content-repos"
            / date.today().isoformat()
        ),
    )
    parser.add_argument("--base-url", default="http://127.0.0.1:6010")
    parser.add_argument(
        "--runtime-env",
        type=Path,
        default=(
            Path.home()
            / "Library"
            / "Application Support"
            / "ContentQuality"
            / "runtime"
            / ".env.content-quality"
        ),
    )
    parser.add_argument("--corpus-id", default=DEFAULT_CORPUS_ID)
    parser.add_argument("--model", default=DEFAULT_MODEL)
    parser.add_argument("--profile", action="append", default=[])
    parser.add_argument("--max-repairs", type=int, default=3)
    parser.add_argument("--workers", type=int, default=3)
    parser.add_argument(
        "--execute",
        action="store_true",
        help="Generate scripts and call the live quality service.",
    )
    return parser


def load_runtime_credentials(path: Path) -> None:
    if not path.is_file():
        return
    values = dotenv_values(path)
    for name in (
        "CONTENT_QUALITY_CONTROL_TOKEN",
        "CONTENT_QUALITY_CREDENTIAL_ENV_FILE",
    ):
        value = str(values.get(name) or "").strip()
        if value:
            os.environ.setdefault(name, value)
    credential_path = Path(
        os.environ.get("CONTENT_QUALITY_CREDENTIAL_ENV_FILE", "")
    ).expanduser()
    if credential_path.is_file():
        credential_values = dotenv_values(credential_path)
        api_key = str(credential_values.get("OPENAI_API_KEY") or "").strip()
        if api_key:
            os.environ.setdefault("OPENAI_API_KEY", api_key)


def main() -> int:
    args = build_parser().parse_args()
    load_runtime_credentials(args.runtime_env)
    profiles = load_registry(args.registry)
    if args.profile:
        selected = set(args.profile)
        profiles = [item for item in profiles if item.profile_id in selected]
        missing = sorted(selected - {item.profile_id for item in profiles})
        if missing:
            raise ValueError(f"unknown profile IDs: {missing}")
    if not profiles:
        raise ValueError("no profiles selected")
    receipts = verify_checkouts(profiles, args.source_root)
    if not args.execute:
        print(json.dumps({
            "status": "validated",
            "profile_count": len(profiles),
            "checkout_receipts": receipts,
        }, indent=2, sort_keys=True))
        return 0

    if args.max_repairs < 0 or args.max_repairs > 3:
        raise ValueError("max-repairs must be between 0 and 3")
    if args.workers < 1 or args.workers > 4:
        raise ValueError("workers must be between 1 and 4")
    with ContentQualityClient(args.base_url) as quality:
        health = quality.health()
        if health.get("status") != "ok":
            raise RuntimeError(f"quality service is not healthy: {health}")
        status = quality.corpus_status(args.corpus_id)
        if int((status.get("counts") or {}).get("items") or 0) < 1:
            raise RuntimeError("reference corpus has no completed items")
        context_receipt = quality.context_receipt(args.corpus_id)

    def execute_profile(profile):
        with ContentQualityClient(args.base_url) as quality:
            return run_profile(
                profile=profile,
                ai_client=configured_ai_client(),
                quality_client=quality,
                corpus_id=args.corpus_id,
                model=args.model,
                max_repairs=args.max_repairs,
            )

    indexed_results = {}
    with ThreadPoolExecutor(max_workers=min(args.workers, len(profiles))) as pool:
        future_indexes = {}
        for index, profile in enumerate(profiles):
            print(f"Starting {profile.profile_id}...", flush=True)
            future_indexes[pool.submit(execute_profile, profile)] = index
        for future in as_completed(future_indexes):
            index = future_indexes[future]
            indexed_results[index] = future.result()
            print(
                f"Finished {profiles[index].profile_id} "
                f"({len(indexed_results)}/{len(profiles)})...",
                flush=True,
            )
    results = [indexed_results[index] for index in range(len(profiles))]
    with ContentQualityClient(args.base_url) as quality:
        peer_enforcement = enforce_peer_diversity(
            results=results,
            ai_client=configured_ai_client(),
            quality_client=quality,
            corpus_id=args.corpus_id,
            model=args.model,
            max_repairs=args.max_repairs,
        )
    run = assemble_run(
        results=results,
        checkout_receipts=receipts,
        model=args.model,
        corpus_id=args.corpus_id,
        context_receipt=context_receipt,
    )
    run["peer_diversity_enforcement"] = peer_enforcement
    paths = write_artifacts(args.output_dir, run)
    print(json.dumps({
        "status": "complete",
        "artifacts": paths,
        "summary": run["summary"],
    }, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
