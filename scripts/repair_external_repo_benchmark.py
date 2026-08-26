#!/usr/bin/env python3
"""Repair rejected or cross-benchmark-overlapping scripts in an existing run."""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

from dotenv import dotenv_values


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from services.content_quality.repo_benchmark import (  # noqa: E402
    BENCHMARK_CONTRACT,
    DEFAULT_CORPUS_ID,
    DEFAULT_MODEL,
    PEER_EXACT_WORD_RUN_LIMIT,
    ContentQualityClient,
    GeneratedTranscript,
    RepositoryProfile,
    annotate_peer_overlaps,
    configured_ai_client,
    deterministic_owner_repair,
    enforce_peer_diversity,
    load_registry,
    quality_findings,
    repair_transcript,
    sha256_text,
    write_artifacts,
)


def parser() -> argparse.ArgumentParser:
    value = argparse.ArgumentParser(
        description=(
            "Repair rejected scripts and accepted scripts that fail the "
            "cross-benchmark peer-overlap gate."
        )
    )
    value.add_argument("--input", type=Path, required=True)
    value.add_argument("--output-dir", type=Path)
    value.add_argument(
        "--registry",
        type=Path,
        default=PROJECT_ROOT / "config" / "external_content_repo_benchmark.json",
    )
    value.add_argument("--base-url", default="http://127.0.0.1:6010")
    value.add_argument("--corpus-id", default=DEFAULT_CORPUS_ID)
    value.add_argument("--model", default=DEFAULT_MODEL)
    value.add_argument("--max-repairs", type=int, default=3)
    value.add_argument(
        "--peer-run-limit",
        type=int,
        default=PEER_EXACT_WORD_RUN_LIMIT,
        help="Reject exact candidate-to-candidate runs at or above this length.",
    )
    value.add_argument(
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
    value.add_argument("--execute", action="store_true")
    return value


def load_credentials(path: Path) -> None:
    if not path.is_file():
        return
    values = dotenv_values(path)
    for name in (
        "CONTENT_QUALITY_CONTROL_TOKEN",
        "CONTENT_QUALITY_CREDENTIAL_ENV_FILE",
    ):
        item = str(values.get(name) or "").strip()
        if item:
            os.environ.setdefault(name, item)
    credential_path = Path(
        os.environ.get("CONTENT_QUALITY_CREDENTIAL_ENV_FILE", "")
    ).expanduser()
    if credential_path.is_file():
        api_key = str(
            dotenv_values(credential_path).get("OPENAI_API_KEY") or ""
        ).strip()
        if api_key:
            os.environ.setdefault("OPENAI_API_KEY", api_key)


def repair_record(
    *,
    record: dict,
    profile: RepositoryProfile,
    brief,
    prior_texts: list[str],
    quality: ContentQualityClient,
    corpus_id: str,
    model: str,
    max_repairs: int,
) -> dict:
    item = GeneratedTranscript.model_validate(record["transcript"])
    old_digest = sha256_text(item.transcript)
    owner = record.get("owner_quality_within_batch") or {}
    item = deterministic_owner_repair(item, owner, attempt=1)
    ai_client = None
    candidates = []
    added_attempts = []
    next_repair_type = "canonical_literal_quality_repair"
    for step in range(max_repairs + 1):
        audit = quality.audit(
            corpus_id=corpus_id,
            title=item.title,
            script=item.transcript,
            objective=brief.objective,
            target_viewer=brief.audience,
        )
        findings, owner = quality_findings(
            item, audit, prior_texts=prior_texts
        )
        added_attempts.append({
            "attempt": len(record.get("attempts") or []) + step,
            "repair_phase": "post_run",
            "input_repair_type": next_repair_type,
            "script_sha256": sha256_text(item.transcript),
            "audit_id": audit.get("audit_id"),
            "audit_status": audit.get("status"),
            "overall_score": audit.get("overall_score"),
            "owner_decision": owner.get("decision"),
            "findings": findings,
        })
        candidates.append((item.model_copy(deep=True), audit, owner, findings))
        if not findings or step == max_repairs:
            break
        literal = deterministic_owner_repair(item, owner, attempt=step + 2)
        if literal.transcript != item.transcript:
            item = literal
            next_repair_type = "canonical_literal_quality_repair"
            continue
        if ai_client is None:
            ai_client = configured_ai_client()
        item, response_id = repair_transcript(
            ai_client,
            profile=profile,
            brief=brief,
            item=item,
            findings=findings,
            model=model,
        )
        added_attempts[-1]["repair_response_id"] = response_id
        next_repair_type = "structured_model_repair"

    item, audit, owner, findings = max(
        candidates,
        key=lambda value: (
            not bool(value[3]),
            -len(value[2].get("failure_codes", [])),
            float(value[1].get("overall_score") or 0.0),
        ),
    )
    accepted = not findings
    experiment = None
    if accepted:
        digest = sha256_text(item.transcript)
        script_id = (
            f"repo_bench_{profile.profile_id}_{brief.brief_id}_{digest[:12]}"
        )
        value = quality.register_experiment(
            brief_id=f"repo_benchmark_{brief.brief_id}",
            script_id=script_id,
            script_text=item.transcript,
            workflow_id=(
                f"{BENCHMARK_CONTRACT}:{profile.profile_id}:{brief.brief_id}"
            ),
            metadata={
                "profile_id": profile.profile_id,
                "adapter_mode": profile.adapter_mode,
                "native_status": profile.native_status,
                "source_commit": profile.source_commit,
                "audit_id": audit.get("audit_id"),
                "post_run_repair": True,
                "outcomes_measured": False,
            },
        )
        experiment = value.get("experiment", value)
    return {
        **record,
        "accepted": accepted,
        "transcript": item.model_dump(mode="json"),
        "audit": audit,
        "owner_quality_within_batch": owner,
        "attempts": list(record.get("attempts") or []) + added_attempts,
        "experiment": experiment,
        "post_run_repair": {
            "original_script_sha256": old_digest,
            "final_script_sha256": sha256_text(item.transcript),
            "changed": old_digest != sha256_text(item.transcript),
        },
    }


def main() -> int:
    args = parser().parse_args()
    if args.max_repairs < 0 or args.max_repairs > 3:
        raise ValueError("max-repairs must be between 0 and 3")
    if args.peer_run_limit < 5 or args.peer_run_limit > 100:
        raise ValueError("peer-run-limit must be between 5 and 100")
    run = json.loads(args.input.read_text(encoding="utf-8"))
    profiles = {item.profile_id: item for item in load_registry(args.registry)}
    rejected = sum(
        not bool(record.get("accepted"))
        for result in run.get("results", [])
        for record in result.get("transcripts", [])
    )
    if not args.execute:
        peer_summary = annotate_peer_overlaps(
            run.get("results", []),
            exact_word_run_limit=args.peer_run_limit,
        )
        print(json.dumps({
            "status": "validated",
            "rejected_count": rejected,
            "peer_overlap": peer_summary,
            "input": str(args.input),
        }, indent=2, sort_keys=True))
        return 0
    load_credentials(args.runtime_env)
    from services.content_quality.repo_benchmark import benchmark_briefs

    briefs = {item.brief_id: item for item in benchmark_briefs()}
    repaired_count = 0
    with ContentQualityClient(args.base_url) as quality:
        if quality.health().get("status") != "ok":
            raise RuntimeError("quality service is not healthy")
        for result in run["results"]:
            profile = profiles[result["profile"]["profile_id"]]
            prior_texts = []
            updated = []
            for record in result["transcripts"]:
                if record.get("accepted"):
                    updated.append(record)
                    prior_texts.append(record["transcript"]["transcript"])
                    continue
                print(
                    f"Repairing {profile.profile_id}/{record['brief_id']}...",
                    flush=True,
                )
                repaired = repair_record(
                    record=record,
                    profile=profile,
                    brief=briefs[record["brief_id"]],
                    prior_texts=prior_texts,
                    quality=quality,
                    corpus_id=args.corpus_id,
                    model=args.model,
                    max_repairs=args.max_repairs,
                )
                updated.append(repaired)
                if repaired["accepted"]:
                    repaired_count += 1
                    prior_texts.append(repaired["transcript"]["transcript"])
            result["transcripts"] = updated
            scores = [
                float(item["audit"].get("overall_score") or 0.0)
                for item in updated
            ]
            result["summary"] = {
                "generated_count": len(updated),
                "accepted_count": sum(
                    bool(item.get("accepted")) for item in updated
                ),
                "average_prepublication_quality": round(
                    sum(scores) / max(1, len(scores)), 3
                ),
                "outcomes_measured": False,
            }
        peer_enforcement = enforce_peer_diversity(
            results=run["results"],
            ai_client=configured_ai_client(),
            quality_client=quality,
            corpus_id=args.corpus_id,
            model=args.model,
            max_repairs=args.max_repairs,
            exact_word_run_limit=args.peer_run_limit,
        )
    run["updated_at"] = datetime.now(timezone.utc).isoformat()
    run["post_run_repair"] = {
        "input_sha256": sha256_text(args.input.read_text(encoding="utf-8")),
        "rejected_before": rejected,
        "newly_accepted": repaired_count,
        "peer_diversity": peer_enforcement,
    }
    run["summary"]["accepted_count"] = sum(
        int(result["summary"]["accepted_count"]) for result in run["results"]
    )
    run["summary"]["peer_overlap"] = {
        "exact_word_run_limit": peer_enforcement["exact_word_run_limit"],
        "maximum_exact_word_run": peer_enforcement["maximum_exact_word_run"],
        "failure_count": peer_enforcement["failure_count"],
        "passed": peer_enforcement["passed"],
    }
    output_dir = args.output_dir or args.input.parent
    paths = write_artifacts(output_dir, run)
    print(json.dumps({
        "status": "complete",
        "artifacts": paths,
        "accepted_count": run["summary"]["accepted_count"],
        "repaired_count": repaired_count,
        "peer_diversity": peer_enforcement,
    }, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
