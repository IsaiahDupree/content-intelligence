from __future__ import annotations

import os
import stat
import subprocess
import sys
from pathlib import Path

from dotenv import dotenv_values


BUILDER = Path(__file__).resolve().parents[1] / "scripts/build_market_tape_runtime_env.py"
REPO_ROOT = Path(__file__).resolve().parents[1]


def test_market_tape_runtime_env_is_self_contained_and_preserves_credentials(tmp_path):
    repo = tmp_path / "content-intelligence"
    repo.mkdir()
    sibling_actp = tmp_path / "actp-worker"
    sibling_actp.mkdir()
    sibling_secret = "youtube-secret-that-must-not-be-imported"
    (sibling_actp / ".env").write_text(
        f"YOUTUBE_API_KEY={sibling_secret}\n",
        encoding="utf-8",
    )
    output = tmp_path / "runtime" / ".env.market-tape"
    output.parent.mkdir()
    owned_secret = "market-tape-owned-runtime-secret"
    output.write_text(
        f"export YOUTUBE_API_KEY={owned_secret}\n",
        encoding="utf-8",
    )

    clean_environment = {
        key: value
        for key, value in os.environ.items()
        if key not in {"YOUTUBE_API_KEY", "YOUTUBE_DATA_API_KEY"}
    }
    completed = subprocess.run(
        [
            sys.executable,
            str(BUILDER),
            "--repo-root",
            str(repo),
            "--runtime-base",
            str(tmp_path / "runtime-base"),
            "--output",
            str(output),
        ],
        check=True,
        capture_output=True,
        text=True,
        env=clean_environment,
    )

    rendered = output.read_text(encoding="utf-8")
    parsed = dotenv_values(output)
    assert parsed["YOUTUBE_API_KEY"] == owned_secret
    assert sibling_secret not in rendered
    assert sibling_secret not in completed.stdout + completed.stderr
    assert stat.S_IMODE(output.stat().st_mode) == 0o600
    assert parsed["MARKET_TAPE_DB_PATH"].endswith("data/market-tape.sqlite3")
    assert parsed["MARKET_TAPE_TRANSCRIPT_STORAGE_ROOT"] == str(
        Path.home()
        / "Library/Application Support/ContentQuality/data/transcript-bank"
    )
    assert parsed["MARKET_TAPE_SUPABASE_SYNC_BATCH_SIZE"] == "250"
    control_token = str(parsed["MARKET_TAPE_CONTROL_TOKEN"])
    assert len(control_token) >= 48
    assert control_token not in completed.stdout + completed.stderr

    rebuilt = subprocess.run(
        [
            sys.executable,
            str(BUILDER),
            "--repo-root",
            str(repo),
            "--runtime-base",
            str(tmp_path / "runtime-base"),
            "--output",
            str(output),
        ],
        check=True,
        capture_output=True,
        text=True,
        env=clean_environment,
    )
    assert dotenv_values(output)["MARKET_TAPE_CONTROL_TOKEN"] == control_token
    assert control_token not in rebuilt.stdout + rebuilt.stderr


def test_market_tape_runtime_entrypoints_have_no_actp_dependency():
    paths = (
        REPO_ROOT / "services/market_tape/config.py",
        REPO_ROOT / "scripts/run_market_tape_api.sh",
        REPO_ROOT / "scripts/run_market_tape_daemon.sh",
    )
    for path in paths:
        assert "actp-worker" not in path.read_text(encoding="utf-8")


def test_market_tape_runtime_control_token_can_be_rotated_without_disclosure(
    tmp_path,
):
    repo = tmp_path / "content-intelligence"
    repo.mkdir()
    output = tmp_path / "runtime" / ".env.market-tape"
    output.parent.mkdir()
    command = [
        sys.executable,
        str(BUILDER),
        "--repo-root",
        str(repo),
        "--runtime-base",
        str(tmp_path / "runtime-base"),
        "--output",
        str(output),
    ]
    initial = subprocess.run(
        command, check=True, capture_output=True, text=True,
    )
    first_token = str(dotenv_values(output)["MARKET_TAPE_CONTROL_TOKEN"])
    rotated = subprocess.run(
        [*command, "--rotate-control-token"],
        check=True,
        capture_output=True,
        text=True,
    )
    second_token = str(dotenv_values(output)["MARKET_TAPE_CONTROL_TOKEN"])

    assert len(first_token) >= 48
    assert len(second_token) >= 48
    assert first_token != second_token
    output_text = initial.stdout + initial.stderr + rotated.stdout + rotated.stderr
    assert first_token not in output_text
    assert second_token not in output_text
