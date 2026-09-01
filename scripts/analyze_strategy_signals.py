#!/usr/bin/env python3
"""Analyze and export resumable strategy signals from reference captions."""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from services.content_quality.strategy_signal_bank import (  # noqa: E402
    OpenAIStrategySignalAnalyzer,
    StrategySignalBank,
)


DEFAULT_CREATORS = ["kenda.laney", "kallawaymarketing", "beau_norton"]
DEFAULT_OUTPUT = (
    Path.home()
    / "Library/Application Support/ContentReferenceCorpus/exports/strategy-reference-snapshot.json"
)
OPENAI_KEY_PATHS = (
    ROOT.parent / "marketing-video-foundry/.env",
    ROOT.parent / "yt-second-brain/.env",
    ROOT.parent / "actp-worker/.env",
    ROOT.parent / "MediaPoster/Backend/.env",
    ROOT.parent / "everreach_backend_2/.env",
)


def emit(value: Any) -> None:
    print(json.dumps(value, ensure_ascii=False, sort_keys=True), flush=True)


def add_common(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--root")
    parser.add_argument(
        "--creator",
        action="append",
        dest="creators",
        help="Creator handle. Repeat for multiple creators.",
    )
    parser.add_argument(
        "--credential-env",
        action="append",
        default=[],
        help="Optional dotenv path. May be repeated; secrets are never printed.",
    )


def creators(args: argparse.Namespace) -> list[str]:
    return args.creators or DEFAULT_CREATORS


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="analyze-strategy-signals")
    add_common(parser)
    commands = parser.add_subparsers(dest="command", required=True)

    status = commands.add_parser("status")
    status.set_defaults(handler=run_status)

    analyze = commands.add_parser("analyze-all")
    analyze.add_argument("--batch-size", type=int, default=24)
    analyze.add_argument("--max-batches", type=int, default=100)
    analyze.add_argument("--model", default="gpt-5-nano")
    analyze.set_defaults(handler=run_analyze_all)

    export = commands.add_parser("export")
    export.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    export.set_defaults(handler=run_export)
    return parser


def load_credentials(paths: list[str]) -> None:
    for value in paths:
        load_dotenv(Path(value).expanduser(), override=False)


def resolve_openai_key() -> str:
    def usable(candidate: str) -> bool:
        value = str(candidate or "").strip().strip('"').strip("'")
        upper = value.upper()
        return (
            value.startswith("sk-")
            and len(value) >= 30
            and "BLOCKED" not in upper
            and "REDACT" not in upper
        )

    current = str(os.environ.get("OPENAI_API_KEY") or "").strip()
    if usable(current):
        return current
    from dotenv import dotenv_values

    for path in OPENAI_KEY_PATHS:
        candidate = str(dotenv_values(path).get("OPENAI_API_KEY") or "").strip()
        if usable(candidate):
            return candidate
    return ""


def run_status(args: argparse.Namespace) -> dict[str, Any]:
    return StrategySignalBank(args.root).status(creators(args))


def run_analyze_all(args: argparse.Namespace) -> dict[str, Any]:
    batch_size = max(1, min(24, int(args.batch_size)))
    bank = StrategySignalBank(
        args.root,
        analyzer=OpenAIStrategySignalAnalyzer(resolve_openai_key()),
    )
    totals = {
        "processed_count": 0,
        "eligible_count": 0,
        "metadata_only_count": 0,
        "evidence_states": {},
    }
    for batch_number in range(1, max(1, int(args.max_batches)) + 1):
        if not bank.pending_items(creators(args), limit=1):
            break
        result = bank.analyze_pending(
            creators(args),
            limit=batch_size,
            model=args.model,
        )
        for key in ("processed_count", "eligible_count", "metadata_only_count"):
            totals[key] += int(result[key])
        for state, count in result["evidence_states"].items():
            totals["evidence_states"][state] = (
                totals["evidence_states"].get(state, 0) + int(count)
            )
        emit({
            "status": "progress",
            "batch": batch_number,
            "processed": totals["processed_count"],
            "batch_evidence_states": result["evidence_states"],
        })
    return {
        "status": "ok",
        "contract": "reference_strategy_signal_sweep_v1",
        "model": args.model,
        **totals,
        "final": bank.status(creators(args)),
    }


def run_export(args: argparse.Namespace) -> dict[str, Any]:
    return StrategySignalBank(args.root).export_snapshot(
        creators(args), args.output
    )


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()
    load_credentials(args.credential_env)
    try:
        emit(args.handler(args))
    except Exception as error:
        emit({
            "status": "error",
            "error_type": type(error).__name__,
            "error": str(error),
        })
        raise SystemExit(1)


if __name__ == "__main__":
    main()
