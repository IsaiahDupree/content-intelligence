"""Real-SQLite regressions for immutable script gate identity."""
from __future__ import annotations

from copy import deepcopy
from pathlib import Path

import pytest

from services.content_quality.engine import (
    AttentionService,
    QualityStore,
    RelatabilityService,
)
from services.content_quality.narrative_coherence import NarrativeCoherenceService


def stored_script(store: QualityStore) -> dict:
    timeline = [
        {"start": 0.0, "end": 3.0, "beat": "human_hook", "text": "You feel stuck."},
        {"start": 3.0, "end": 8.0, "beat": "stakes", "text": "It matters because time is lost."},
        {"start": 8.0, "end": 15.0, "beat": "claim", "text": "Start with the human problem."},
        {"start": 15.0, "end": 23.0, "beat": "proof", "text": "The evidence shows the pattern."},
        {"start": 23.0, "end": 31.0, "beat": "method", "text": "Remove one pressure today."},
        {"start": 31.0, "end": 38.0, "beat": "payoff", "text": "The work feels possible again."},
        {"start": 38.0, "end": 43.0, "beat": "cta", "text": "Which part feels hardest?"},
    ]
    script = {
        "script_id": "script-gate-integrity",
        "topic": "AI automation",
        "audience": "software founders",
        "objective": "qualified_attention",
        "brief_id": "brief-gate-integrity",
        "source_receipt_ids": [],
        "human_moment": {"situation": "You feel stuck.", "stakes": "Time is lost."},
        "evidence_summary": {"creator_count": 3},
        "timeline": timeline,
        "text": " ".join(beat["text"] for beat in timeline),
        "status": "generated_pending_gates",
        "created_at": "2026-08-24T00:00:00+00:00",
    }
    return store.put_script(script)


@pytest.mark.parametrize(
    ("service_name", "method_name", "mutation"),
    (
        ("narrative", "audit", "timeline"),
        ("relatability", "audit", "text"),
        ("attention", "script_audit", "text"),
        ("attention", "video_preflight", "timeline"),
    ),
)
def test_stored_script_gate_rejects_caller_mutation_before_audit(
    tmp_path: Path,
    service_name: str,
    method_name: str,
    mutation: str,
):
    store = QualityStore(tmp_path / "quality.sqlite3")
    script = stored_script(store)
    services = {
        "narrative": NarrativeCoherenceService(store),
        "relatability": RelatabilityService(store),
        "attention": AttentionService(store),
    }
    payload = deepcopy(script)
    if mutation == "text":
        payload["text"] = "Caller-controlled replacement text."
    else:
        payload["timeline"][0]["text"] = "Caller-controlled replacement hook."

    with pytest.raises(ValueError, match=f"{mutation} does not match"):
        getattr(services[service_name], method_name)(payload)

    assert store.script_gate_summary(script["script_id"])["latest_audits"] == {}


def test_same_script_id_cannot_alias_different_immutable_content(tmp_path: Path):
    store = QualityStore(tmp_path / "quality.sqlite3")
    script = stored_script(store)
    conflicting = {**script, "audience": "independent creators"}

    with pytest.raises(ValueError, match="different immutable content"):
        store.put_script(conflicting)

    assert store.script(script["script_id"])["audience"] == "software founders"


def test_render_summary_requires_seven_current_hash_bound_gates(tmp_path: Path):
    store = QualityStore(tmp_path / "quality.sqlite3")
    script = stored_script(store)
    binding = {
        "contract": "stored_script_audit_binding_v1",
        "stored_script_bound": True,
        "script_id": script["script_id"],
        "script_sha256": store.script_audit_sha256(script),
    }
    decisions = {
        "narrative_coherence": "PASS",
        "relatability_script": "PASS",
        "relatability_ai_qualitative": "PASS_NON_AI",
        "relatability_transcript_cohort": "PASS",
        "transcript_style_fit": "PASS",
        "attention_script": "PASS",
        "attention_video_preflight": "PASS",
    }
    for audit_type, decision in decisions.items():
        store.put_audit(
            audit_type, script["script_id"], decision, 100.0,
            {"input_binding": binding},
        )

    assert store.script_gate_summary(script["script_id"])["ready_for_render"] is True

    store.put_audit(
        "attention_video_preflight", script["script_id"], "PASS", 100.0,
        {"input_binding": {**binding, "script_sha256": "0" * 64}},
    )
    summary = store.script_gate_summary(script["script_id"])
    assert summary["ready_for_render"] is False
    assert summary["latest_audits"]["attention_video_preflight"][
        "stored_script_binding_valid"
    ] is False
