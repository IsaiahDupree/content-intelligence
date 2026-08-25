from __future__ import annotations

import stat
import subprocess
import sys
from pathlib import Path

from dotenv import dotenv_values


REPO_ROOT = Path(__file__).resolve().parents[1]
BUILDER = REPO_ROOT / "scripts/build_content_quality_runtime_env.py"


def test_runtime_env_references_external_credential_without_copying_it(tmp_path):
    credential_dir = tmp_path / "external credentials"
    credential_dir.mkdir()
    credential_path = credential_dir / "provider.env"
    secret = "sk-test-runtime-credential-must-not-be-copied"
    credential_path.write_text(
        f"OPENAI_API_KEY={secret}\nUNRELATED_PROVIDER_VALUE=private\n",
        encoding="utf-8",
    )

    output = tmp_path / "runtime" / ".env.content-quality"
    output.parent.mkdir()
    preserved_token = "preserve-this-control-token"
    output.write_text(
        f"export CONTENT_QUALITY_CONTROL_TOKEN={preserved_token}\n",
        encoding="utf-8",
    )
    output.chmod(0o644)

    completed = subprocess.run(
        [
            sys.executable,
            str(BUILDER),
            "--output",
            str(output),
            "--credential-env",
            str(credential_path),
        ],
        cwd=REPO_ROOT,
        check=True,
        capture_output=True,
        text=True,
    )

    rendered = output.read_text(encoding="utf-8")
    parsed = dotenv_values(output)
    combined_process_output = completed.stdout + completed.stderr

    assert parsed["CONTENT_QUALITY_CONTROL_TOKEN"] == preserved_token
    assert parsed["CONTENT_QUALITY_CREDENTIAL_ENV_FILE"] == str(
        credential_path.resolve()
    )
    assert parsed["NARRATIVE_COHERENCE_LLM"] == "openai"
    assert parsed["NARRATIVE_JUDGE_MODEL"] == "gpt-5-nano"
    assert "OPENAI_API_KEY" not in rendered
    assert secret not in rendered
    assert secret not in combined_process_output
    assert stat.S_IMODE(output.stat().st_mode) == 0o600
    assert not output.with_suffix(output.suffix + ".tmp").exists()


def test_runtime_env_preserves_generated_control_token_on_rebuild(tmp_path):
    output = tmp_path / ".env.content-quality"

    first = subprocess.run(
        [sys.executable, str(BUILDER), "--output", str(output)],
        cwd=REPO_ROOT,
        check=True,
        capture_output=True,
        text=True,
    )
    first_token = str(dotenv_values(output)["CONTENT_QUALITY_CONTROL_TOKEN"])

    second = subprocess.run(
        [sys.executable, str(BUILDER), "--output", str(output)],
        cwd=REPO_ROOT,
        check=True,
        capture_output=True,
        text=True,
    )
    second_token = str(dotenv_values(output)["CONTENT_QUALITY_CONTROL_TOKEN"])

    assert first.returncode == second.returncode == 0
    assert len(first_token) >= 48
    assert second_token == first_token
    assert first_token not in first.stdout + first.stderr
    assert first_token not in second.stdout + second.stderr
    assert stat.S_IMODE(output.stat().st_mode) == 0o600
