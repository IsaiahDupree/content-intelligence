#!/usr/bin/env python3
"""Audit a persisted script against an immutable transcript cohort, without HTTP."""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from contextlib import closing
from pathlib import Path


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPOSITORY_ROOT))

from services.content_quality.engine import QualityStore  # noqa: E402
from services.content_quality.transcript_bank import TranscriptBank  # noqa: E402


DEFAULT_TAPE = Path.home() / "Library/Application Support/ContentIntelligence/data/market-tape.sqlite3"
DEFAULT_QUALITY = Path.home() / "Library/Application Support/ContentQuality/data/content-quality.sqlite3"
DEFAULT_STORAGE = Path("/Volumes/My Passport/MarketTape/transcript-bank")


def latest_old_audit(quality_path: Path, script_id: str) -> str | None:
    with closing(sqlite3.connect(quality_path)) as connection:
        row = connection.execute(
            """
            SELECT audit_id FROM cq_audits
            WHERE subject_id=? AND audit_type='relatability_script' AND decision='PASS'
            ORDER BY created_at DESC LIMIT 1
            """,
            (script_id,),
        ).fetchone()
    return str(row[0]) if row else None


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Fail-closed script relatability audit backed by local Whisper transcripts."
    )
    parser.add_argument("--script-id", required=True)
    parser.add_argument("--cohort-manifest", type=Path, required=True)
    parser.add_argument("--quality-db", type=Path, default=DEFAULT_QUALITY)
    parser.add_argument("--tape", type=Path, default=DEFAULT_TAPE)
    parser.add_argument("--storage-root", type=Path, default=DEFAULT_STORAGE)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()

    quality = QualityStore(args.quality_db)
    script = quality.script(args.script_id)
    if not script:
        raise SystemExit(f"script not found: {args.script_id}")
    previous_audit_id = latest_old_audit(args.quality_db, args.script_id)
    bank = TranscriptBank(args.tape, args.storage_root)
    evidence_audit = bank.audit_script_against_cohort(
        script_id=args.script_id,
        script_text=str(script.get("text") or ""),
        cohort_manifest_path=args.cohort_manifest,
    )
    gate_decision = (
        "PASS" if evidence_audit["decision"] == "PASS_PREDICTED_RELATABILITY"
        else "REJECT_NOT_RELATABLE"
    )
    findings = {
        **evidence_audit["findings"],
        "evidence_audit_id": evidence_audit["audit_id"],
        "evidence_receipt_path": evidence_audit["receipt_path"],
        "cohort_manifest_path": str(args.cohort_manifest.expanduser().resolve()),
        "supersedes_audit_id": previous_audit_id,
        "supersession_reason": (
            "Prior token/beat-label audit did not validate source transcript performance, "
            "artifact integrity, cohort sufficiency, or script-to-transcript alignment."
        ),
    }
    quality_audit = quality.put_audit(
        "relatability_script",
        args.script_id,
        gate_decision,
        float(evidence_audit["score"]),
        findings,
    )
    result = {
        "script_id": args.script_id,
        "decision": gate_decision,
        "score": evidence_audit["score"],
        "evidence_audit": evidence_audit,
        "quality_gate_audit": quality_audit,
        "gate_summary": quality.script_gate_summary(args.script_id),
    }
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(
            json.dumps(result, indent=2, ensure_ascii=False, sort_keys=True) + "\n",
            encoding="utf-8",
        )
    print(json.dumps(result, indent=2, ensure_ascii=False, sort_keys=True))
    return 0 if gate_decision == "PASS" else 2


if __name__ == "__main__":
    raise SystemExit(main())
