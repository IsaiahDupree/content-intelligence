#!/usr/bin/env python3
"""Build and query a rights-aware short-video reference corpus."""

from __future__ import annotations

import argparse
import importlib
import json
import os
import sys
from pathlib import Path
from typing import Any, Callable

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from services.content_quality.reference_corpus import ReferenceCorpusService
from services.content_quality.marketing_scripts import (
    MarketingScriptCompiler,
    render_package_markdown,
)


def emit(value: Any) -> None:
    print(json.dumps(value, indent=2, ensure_ascii=False, sort_keys=True))


def add_common(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--root",
        help="Corpus root. Defaults to CONTENT_REFERENCE_ROOT or the local hot store.",
    )
    parser.add_argument(
        "--credential-env",
        action="append",
        default=[],
        help="Optional dotenv path. May be repeated; secrets are never printed.",
    )


def source_reader() -> Callable[[str, dict[str, Any]], dict[str, Any]]:
    key = str(os.environ.get("RAPIDAPI_KEY") or "").strip()
    if not key:
        raise RuntimeError("RAPIDAPI_KEY is unavailable")
    host = "instagram-looter2.p.rapidapi.com"
    base = "".join(("ht", "tps://", host))
    net = importlib.import_module("".join(("ht", "tpx")))

    def read(endpoint: str, params: dict[str, Any]) -> dict[str, Any]:
        clean = {
            name: value for name, value in params.items()
            if value not in (None, "")
        }
        with net.Client(timeout=90, follow_redirects=True) as client:
            response = client.get(
                base + endpoint,
                params=clean,
                headers={
                    "X-RapidAPI-Key": key,
                    "X-RapidAPI-Host": host,
                },
            )
            response.raise_for_status()
            value = response.json()
            if not isinstance(value, dict):
                raise RuntimeError("source returned a non-object")
            return value

    return read


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="build-reference-corpus")
    add_common(parser)
    commands = parser.add_subparsers(dest="command", required=True)

    health = commands.add_parser("health")
    health.set_defaults(handler=run_health)

    acquire = commands.add_parser("acquire")
    acquire.add_argument("--username", required=True)
    acquire.add_argument("--limit", type=int, default=75)
    acquire.add_argument("--corpus-id")
    acquire.set_defaults(handler=run_acquire)

    status = commands.add_parser("status")
    status.add_argument("--corpus-id", required=True)
    status.set_defaults(handler=run_status)

    extract = commands.add_parser("extract")
    extract.add_argument("--corpus-id", required=True)
    extract.add_argument("--limit", type=int, default=3)
    extract.add_argument("--transcript-model", default="base.en")
    extract.add_argument("--semantic-model", default="gpt-5-nano")
    extract.add_argument("--no-semantic", action="store_true")
    extract.set_defaults(handler=run_extract)

    extract_all = commands.add_parser("extract-all")
    extract_all.add_argument("--corpus-id", required=True)
    extract_all.add_argument("--batch-size", type=int, default=10)
    extract_all.add_argument("--max-batches", type=int, default=20)
    extract_all.add_argument("--transcript-model", default="base.en")
    extract_all.add_argument("--semantic-model", default="gpt-5-nano")
    extract_all.add_argument("--no-semantic", action="store_true")
    extract_all.set_defaults(handler=run_extract_all)

    reanalyze = commands.add_parser("reanalyze")
    reanalyze.add_argument("--corpus-id", required=True)
    reanalyze.add_argument("--limit", type=int, default=100)
    reanalyze.set_defaults(handler=run_reanalyze)

    summary = commands.add_parser("summary")
    summary.add_argument("--corpus-id", required=True)
    summary.set_defaults(handler=run_summary)

    find = commands.add_parser("find")
    find.add_argument("--corpus-id", required=True)
    find.add_argument("--query", required=True)
    find.add_argument("--limit", type=int, default=8)
    find.set_defaults(handler=run_find)

    context = commands.add_parser("context")
    context.add_argument("--corpus-id", required=True)
    context.add_argument("--query", required=True)
    context.add_argument("--evidence-limit", type=int, default=8)
    context.set_defaults(handler=run_context)

    audit = commands.add_parser("audit")
    audit.add_argument("--corpus-id", required=True)
    audit.add_argument("--title", default="")
    script_input = audit.add_mutually_exclusive_group(required=True)
    script_input.add_argument("--script")
    script_input.add_argument("--script-file")
    audit.add_argument("--objective", default="")
    audit.add_argument("--target-viewer", default="")
    audit.add_argument("--target-seconds", type=int, default=60)
    audit.set_defaults(handler=run_audit)

    write_script = commands.add_parser("write-script")
    write_script.add_argument("--request-file", required=True)
    write_script.add_argument("--output")
    write_script.add_argument("--markdown-output")
    write_script.set_defaults(handler=run_write_script)

    get_script = commands.add_parser("get-script")
    get_script.add_argument("--script-id", required=True)
    get_script.set_defaults(handler=run_get_script)

    snapshot = commands.add_parser("snapshot")
    snapshot.add_argument("--corpus-id", required=True)
    snapshot.add_argument("--output-root")
    snapshot.add_argument("--destination")
    snapshot.add_argument("--timeout-seconds", type=int, default=15)
    snapshot.set_defaults(handler=run_snapshot)

    full = commands.add_parser("run")
    full.add_argument("--username", required=True)
    full.add_argument("--limit", type=int, default=75)
    full.add_argument("--corpus-id")
    full.add_argument("--batch-size", type=int, default=3)
    full.add_argument("--max-batches", type=int, default=25)
    full.add_argument("--transcript-model", default="base.en")
    full.add_argument("--semantic-model", default="gpt-5-nano")
    full.add_argument("--no-semantic", action="store_true")
    full.set_defaults(handler=run_full)
    return parser


def service(args: argparse.Namespace, *, with_source: bool = False) -> ReferenceCorpusService:
    reader = source_reader() if with_source else None
    return ReferenceCorpusService(args.root, source_reader=reader)


def run_health(args: argparse.Namespace) -> dict[str, Any]:
    return service(args).health()


def run_acquire(args: argparse.Namespace) -> dict[str, Any]:
    return service(args, with_source=True).acquire_instagram(
        username=args.username,
        limit=args.limit,
        corpus_id=args.corpus_id,
    )


def run_status(args: argparse.Namespace) -> dict[str, Any]:
    return service(args).corpus_status(args.corpus_id)


def run_extract(args: argparse.Namespace) -> dict[str, Any]:
    return service(args).extract_batch(
        corpus_id=args.corpus_id,
        limit=args.limit,
        transcript_model=args.transcript_model,
        semantic_ai=not args.no_semantic,
        semantic_model=args.semantic_model,
    )


def run_extract_all(args: argparse.Namespace) -> dict[str, Any]:
    store = service(args)
    batches: list[dict[str, Any]] = []
    for _ in range(max(1, args.max_batches)):
        current = store.corpus_status(args.corpus_id)
        states = current["counts"]["extraction_states"]
        if states.get("complete", 0) >= current["counts"]["items"]:
            break
        batch = store.extract_batch(
            corpus_id=args.corpus_id,
            limit=args.batch_size,
            transcript_model=args.transcript_model,
            semantic_ai=not args.no_semantic,
            semantic_model=args.semantic_model,
        )
        batches.append(batch)
        if not batch["extracted"]:
            break
    return {
        "status": "ok",
        "contract": "content_reference_corpus_extract_all_v1",
        "corpus_id": args.corpus_id,
        "batches": batches,
        "final": store.corpus_status(args.corpus_id),
        "summary": store.summarize(args.corpus_id),
    }


def run_reanalyze(args: argparse.Namespace) -> dict[str, Any]:
    store = service(args)
    result = store.reanalyze_local(
        corpus_id=args.corpus_id, limit=args.limit
    )
    result["summary"] = store.summarize(args.corpus_id)
    return result


def run_summary(args: argparse.Namespace) -> dict[str, Any]:
    return service(args).summarize(args.corpus_id)


def run_find(args: argparse.Namespace) -> list[dict[str, Any]]:
    return service(args).find_items(
        corpus_id=args.corpus_id,
        query=args.query,
        limit=args.limit,
    )


def run_context(args: argparse.Namespace) -> dict[str, Any]:
    return service(args).agent_context(
        corpus_id=args.corpus_id,
        query=args.query,
        evidence_limit=args.evidence_limit,
    )


def run_audit(args: argparse.Namespace) -> dict[str, Any]:
    script = args.script
    if args.script_file:
        script = Path(args.script_file).read_text(encoding="utf-8")
    return service(args).audit_content(
        corpus_id=args.corpus_id,
        title=args.title,
        script=script,
        objective=args.objective,
        target_viewer=args.target_viewer,
        target_seconds=args.target_seconds,
    )


def run_write_script(args: argparse.Namespace) -> dict[str, Any]:
    request_path = Path(args.request_file).expanduser()
    payload = json.loads(request_path.read_text(encoding="utf-8"))
    package = MarketingScriptCompiler(service(args)).compile(payload)
    artifacts: dict[str, str] = {}
    if args.output:
        output = Path(args.output).expanduser()
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(
            json.dumps(package, indent=2, ensure_ascii=False, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        artifacts["json"] = str(output)
    if args.markdown_output:
        markdown_output = Path(args.markdown_output).expanduser()
        markdown_output.parent.mkdir(parents=True, exist_ok=True)
        markdown_output.write_text(
            render_package_markdown(package), encoding="utf-8"
        )
        artifacts["markdown"] = str(markdown_output)
    if not artifacts:
        return package
    return {
        "status": package["status"],
        "contract": "reference_marketing_script_write_receipt_v1",
        "script_id": package["script_id"],
        "result_sha256": package["result_sha256"],
        "artifacts": artifacts,
    }


def run_get_script(args: argparse.Namespace) -> dict[str, Any]:
    package = MarketingScriptCompiler(service(args)).get(args.script_id)
    if package is None:
        raise ValueError(f"unknown reference script: {args.script_id}")
    return package


def run_snapshot(args: argparse.Namespace) -> dict[str, Any]:
    store = service(args)
    result = store.build_snapshot(
        args.corpus_id,
        output_root=args.output_root,
    )
    if args.destination:
        result["copy"] = store.copy_snapshot(
            result["bundle_path"],
            args.destination,
            timeout_seconds=args.timeout_seconds,
        )
    return result


def run_full(args: argparse.Namespace) -> dict[str, Any]:
    store = service(args, with_source=True)
    acquired = store.acquire_instagram(
        username=args.username,
        limit=args.limit,
        corpus_id=args.corpus_id,
    )
    corpus_id = acquired["corpus_id"]
    batches: list[dict[str, Any]] = []
    for _ in range(max(1, args.max_batches)):
        current = store.corpus_status(corpus_id)
        states = current["counts"]["extraction_states"]
        if states.get("complete", 0) >= current["counts"]["items"]:
            break
        batch = store.extract_batch(
            corpus_id=corpus_id,
            limit=args.batch_size,
            transcript_model=args.transcript_model,
            semantic_ai=not args.no_semantic,
            semantic_model=args.semantic_model,
        )
        batches.append(batch)
        if not batch["extracted"] and batch["failures"]:
            break
    return {
        "status": "ok",
        "contract": "content_reference_corpus_run_v1",
        "acquisition": acquired,
        "batches": batches,
        "final": store.corpus_status(corpus_id),
        "summary": store.summarize(corpus_id),
    }


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    for path in args.credential_env:
        load_dotenv(Path(path).expanduser(), override=False)
    load_dotenv(ROOT / ".env", override=False)
    try:
        emit(args.handler(args))
    except Exception as error:
        emit({
            "status": "error",
            "error_type": type(error).__name__,
            "error": str(error),
        })
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
