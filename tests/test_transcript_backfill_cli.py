"""Real process and file-lock coverage for the transcript backfill CLI."""

from __future__ import annotations

import fcntl
import json
import subprocess
import sys
from pathlib import Path

from scripts import backfill_transcript_bank, transcribe_performance_cohort
from scripts.backfill_transcript_bank import exit_code_for_status, storage_mount_error
from services.content_quality.transcript_bank import model_progress_to_stderr


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]


def test_partial_batch_is_operationally_successful_but_failures_are_not():
    assert exit_code_for_status("completed") == 0
    assert exit_code_for_status("partial") == 0
    assert exit_code_for_status("failed") == 2
    assert exit_code_for_status("audit_failed") == 2
    assert exit_code_for_status("blocked_runtime") == 2


def test_passport_target_fails_closed_when_volume_is_only_a_directory(tmp_path):
    unmounted_volume = tmp_path / "My Passport"
    unmounted_volume.mkdir()
    target = unmounted_volume / "MarketTape" / "transcript-bank"
    assert storage_mount_error(target, passport_root=unmounted_volume) == (
        f"Passport storage is not a mounted filesystem: {unmounted_volume}"
    )
    assert storage_mount_error(tmp_path / "ordinary-local-test") == ""


def test_real_process_lock_emits_hashed_busy_receipt_without_opening_tape(tmp_path):
    storage_root = tmp_path / "transcript-bank"
    storage_root.mkdir()
    lock_path = storage_root / ".transcript-backfill.lock"
    with lock_path.open("a+", encoding="utf-8") as lock_handle:
        fcntl.flock(lock_handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        completed = subprocess.run(
            [
                sys.executable,
                str(REPOSITORY_ROOT / "scripts" / "backfill_transcript_bank.py"),
                "--tape",
                str(tmp_path / "does-not-exist.sqlite3"),
                "--storage-root",
                str(storage_root),
            ],
            cwd=REPOSITORY_ROOT,
            capture_output=True,
            text=True,
            timeout=30,
        )
    assert completed.returncode == 0, completed.stderr
    output = json.loads(completed.stdout)
    assert output["status"] == "busy_existing_worker"
    receipt_path = Path(output["receipt_path"])
    receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    assert receipt["contract"] == "transcript_backfill_singleton_busy_v1"
    assert receipt["status"] == "busy_existing_worker"
    assert len(receipt["receipt_sha256"]) == 64


def test_model_progress_is_routed_away_from_machine_json_stdout(capsys):
    with model_progress_to_stderr():
        print("model progress: 75%")
    print(json.dumps({"status": "completed"}))

    captured = capsys.readouterr()
    assert json.loads(captured.out) == {"status": "completed"}
    assert "model progress: 75%" in captured.err
    assert "model progress" not in captured.out


def test_acquisition_clis_default_internal_and_honor_explicit_storage(tmp_path):
    expected_internal = (
        Path.home()
        / "Library/Application Support/ContentQuality/data/transcript-bank"
    )
    explicit_storage = tmp_path / "explicit-legacy-recovery-root"

    backfill_default = backfill_transcript_bank.parser().parse_args([])
    cohort_default = transcribe_performance_cohort.parser().parse_args(
        ["--topic", "automation"]
    )
    assert backfill_default.storage_root == expected_internal
    assert cohort_default.storage_root == expected_internal

    backfill_explicit = backfill_transcript_bank.parser().parse_args(
        ["--storage-root", str(explicit_storage)]
    )
    cohort_explicit = transcribe_performance_cohort.parser().parse_args(
        [
            "--topic",
            "automation",
            "--storage-root",
            str(explicit_storage),
        ]
    )
    assert backfill_explicit.storage_root == explicit_storage
    assert cohort_explicit.storage_root == explicit_storage
