from __future__ import annotations

import json
import subprocess

import pytest
from pydantic import ValidationError

from services.content_quality.repo_benchmark import (
    BENCHMARK_CONTRACT,
    BenchmarkBeat,
    GeneratedBatch,
    GeneratedTranscript,
    RepositoryProfile,
    annotate_peer_overlaps,
    assemble_run,
    benchmark_briefs,
    build_generation_input,
    deterministic_owner_repair,
    longest_exact_word_run,
    normalize_factual_claims,
    peer_overlap_receipt,
    render_markdown_report,
    validate_batch_coverage,
    verify_checkouts,
)
from services.content_quality.script_quality import audit_owner_calibrated_quality


def profile(**changes) -> RepositoryProfile:
    value = {
        "profile_id": "sample_method",
        "label": "Sample Method",
        "repo_full_name": "owner/sample-method",
        "repo_url": "https://example.invalid/owner/sample-method",
        "checkout_dir": "owner__sample-method",
        "source_commit": None,
        "license_spdx": "MIT",
        "reuse_policy": "permissive_clean_room",
        "native_status": "working",
        "adapter_mode": "profile_adapted",
        "role": "viral_intelligence",
        "capabilities": ["Normalize results against a baseline."],
        "generation_principles": ["Open with the strongest observed contrast."],
        "limitations": ["No owned audience outcome is implied."],
        "evidence_paths": ["README.md", "src/score.py"],
    }
    value.update(changes)
    return RepositoryProfile.model_validate(value)


def transcript(brief_id: str) -> GeneratedTranscript:
    hook = "Your team keeps checking the same stuck task."
    cta = "Pick that task, record the delay, and test one smaller step today."
    middle = " ".join(["A narrow workflow makes the problem clear."] * 10)
    text = f"{hook} {middle} {cta}"
    return GeneratedTranscript(
        brief_id=brief_id,
        title="Fix the recurring delay",
        hook=hook,
        beats=[
            BenchmarkBeat(label="hook", text=hook),
            BenchmarkBeat(label="method", text=middle),
            BenchmarkBeat(label="cta", text=cta),
        ],
        transcript=text,
        cta=cta,
        word_count=len(text.split()),
        methodology_choices=["baseline comparison"],
        factual_claims_used=[],
    )


def test_prompt_projection_excludes_checkout_lineage() -> None:
    item = profile(source_commit="a" * 40)
    projection = item.prompt_projection()
    encoded = json.dumps(projection)
    assert "repo_url" not in projection
    assert "checkout_dir" not in projection
    assert "evidence_paths" not in projection
    assert "a" * 40 not in encoded


def test_unlicensed_profile_cannot_claim_permissive_reuse() -> None:
    with pytest.raises(ValidationError, match="evaluation-only"):
        profile(license_spdx="NOASSERTION")
    accepted = profile(
        license_spdx="NOASSERTION",
        reuse_policy="evaluation_only_no_license",
    )
    assert accepted.reuse_policy == "evaluation_only_no_license"


def test_quarantine_policy_requires_quarantine_state() -> None:
    with pytest.raises(ValidationError, match="must stay quarantined"):
        profile(
            license_spdx="NOASSERTION",
            reuse_policy="quarantined_security_review",
        )


def test_brief_set_and_generation_payload_are_fixed() -> None:
    briefs = benchmark_briefs()
    assert [item.brief_id for item in briefs] == [
        "control_plane", "automation_roi", "practical_ai"
    ]
    payload = json.loads(build_generation_input(profile(), briefs))
    assert payload["contract"] == "repo_profile_structured_output_v1"
    assert payload["profile"] == profile().prompt_projection()
    assert len(payload["briefs"]) == 3


def test_batch_coverage_rejects_duplicate_brief() -> None:
    briefs = benchmark_briefs()
    batch = GeneratedBatch(
        profile_id="sample_method",
        transcripts=[
            transcript("control_plane"),
            transcript("control_plane"),
            transcript("practical_ai"),
        ],
    )
    with pytest.raises(ValueError, match="coverage mismatch"):
        validate_batch_coverage(batch, profile(), briefs)


def test_checkout_verification_uses_real_git_state(tmp_path) -> None:
    checkout = tmp_path / "owner__sample-method"
    checkout.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=checkout, check=True)
    subprocess.run(
        ["git", "config", "user.email", "test@example.invalid"],
        cwd=checkout,
        check=True,
    )
    subprocess.run(
        ["git", "config", "user.name", "Test User"], cwd=checkout, check=True
    )
    (checkout / "README.md").write_text("sample\n", encoding="utf-8")
    subprocess.run(["git", "add", "README.md"], cwd=checkout, check=True)
    subprocess.run(["git", "commit", "-qm", "initial"], cwd=checkout, check=True)
    subprocess.run(
        ["git", "remote", "add", "origin", "https://example.invalid/sample.git"],
        cwd=checkout,
        check=True,
    )
    head = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=checkout,
        text=True,
        capture_output=True,
        check=True,
    ).stdout.strip()
    receipt = verify_checkouts(
        [profile(source_commit=head)], tmp_path
    )[0]
    assert receipt["verified"] is True
    assert receipt["commit"] == head


def test_report_states_score_limit() -> None:
    item = profile().model_dump(mode="json")
    record = transcript("control_plane").model_dump(mode="json")
    result = {
        "profile": item,
        "summary": {
            "generated_count": 1,
            "accepted_count": 1,
            "average_prepublication_quality": 88.0,
            "outcomes_measured": False,
        },
        "transcripts": [{
            "brief_id": "control_plane",
            "accepted": True,
            "transcript": record,
            "audit": {
                "overall_score": 88.0,
                "audit_id": "audit_1",
                "copy_gate": {"passed": True},
            },
            "owner_quality_within_batch": {"decision": "PASS"},
        }],
    }
    run = assemble_run(
        results=[result],
        checkout_receipts=[],
        model="model-a",
        corpus_id="corpus-a",
    )
    rendered = render_markdown_report(run)
    assert run["contract"] == BENCHMARK_CONTRACT
    assert run["summary"]["peer_overlap"]["passed"] is True
    assert "not predictions of audience outcomes" in rendered
    assert "peer gate `True`" in rendered
    assert "Fix the recurring delay" in rendered


def test_peer_overlap_uses_separate_twenty_word_gate() -> None:
    shared = " ".join(f"word{index}" for index in range(20))
    left = f"alpha begins {shared} left ending"
    right = f"beta starts {shared} right ending"
    run_length, phrase = longest_exact_word_run(left, right)
    receipt = peer_overlap_receipt(left, [("prior/script", right)])
    assert run_length == 20
    assert phrase == shared
    assert receipt["passed"] is False
    assert receipt["exact_word_run_limit"] == 20
    assert receipt["nearest_script_id"] == "prior/script"


def test_peer_annotation_is_deterministic_in_run_order() -> None:
    shared = " ".join(f"term{index}" for index in range(20))
    first = transcript("control_plane").model_dump(mode="json")
    second = transcript("automation_roi").model_dump(mode="json")
    first["transcript"] = f"Alpha begins {shared} first close"
    second["transcript"] = f"Beta starts {shared} second close"
    results = [
        {
            "profile": profile(profile_id="one").model_dump(mode="json"),
            "transcripts": [{"brief_id": "control_plane", "transcript": first}],
        },
        {
            "profile": profile(profile_id="two").model_dump(mode="json"),
            "transcripts": [{"brief_id": "automation_roi", "transcript": second}],
        },
    ]
    summary = annotate_peer_overlaps(results)
    assert summary["failure_count"] == 1
    assert summary["maximum_exact_word_run"] == 20
    assert results[1]["transcripts"][0]["peer_overlap"]["passed"] is False


def test_factual_claim_metadata_keeps_only_canonical_brief_facts() -> None:
    brief = benchmark_briefs()[2]
    item = transcript(brief.brief_id)
    stale = "The team should own the destination action and recovery path."
    item.factual_claims_used = [brief.allowed_facts[0], stale]
    removed = normalize_factual_claims(item, brief)
    assert removed == [stale]
    assert item.factual_claims_used == [brief.allowed_facts[0]]


def test_deterministic_repair_updates_script_hook_and_beats() -> None:
    item = transcript("practical_ai")
    item.hook = "Your team checks the queue and gets stuck."
    item.transcript = (
        f"{item.hook} The recovery path is unclear. "
        f"{item.transcript}"
    )
    item.beats[0].text = item.hook
    item.word_count = len(item.transcript.split())
    report = audit_owner_calibrated_quality(item.transcript)
    repaired = deterministic_owner_repair(item, report)
    assert "queue" not in repaired.hook.casefold()
    assert "queue" not in repaired.beats[0].text.casefold()
    assert "recovery path" not in repaired.transcript.casefold()
    assert "way back" in repaired.transcript.casefold()
    assert repaired.word_count == len(repaired.transcript.split())
