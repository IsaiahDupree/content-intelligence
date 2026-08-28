from __future__ import annotations

import copy
import hashlib
import json

import pytest

from services.content_quality.api import create_content_quality_app
from services.content_quality.engine import QualityStore
from services.content_quality.reference_corpus import canonical_sha256
from services.content_quality.reference_script_binding import (
    BINDING_CONTRACT,
    ReferenceScriptQualityBinder,
    validate_reference_package,
)


def _reference_package() -> dict:
    request = {
        "contract": "reference_marketing_script_request_v1",
        "corpus_id": "reference-corpus-1",
        "title": "Remove the Wait",
        "topic": "Choosing a workflow to automate",
        "audience": "software founders",
        "objective": "educate",
        "content_role": "EDUCATE",
        "topic_distance_from_offer": 3,
        "topic_ladder_id": "automation-wait-v1",
        "angle": "contrast",
        "target_seconds": 30,
        "offer": {"offer_id": "", "name": ""},
        "narrative": {
            "hook": "Your lead waits",
            "problem": "The handoff is still manual",
            "stakes": "The customer feels the delay",
            "proof": {
                "statement": "A quote form records the incoming lead",
                "evidence_type": "worked_example",
                "source_receipt_ids": [],
                "independent_verification_receipts": [],
            },
            "reframe": "Choose the wait, not the demo",
            "steps": ["count arrivals", "measure wait", "check the record"],
            "takeaway": "A visible wait is measurable",
            "cta": {"text": "Score one task today", "action": "score", "destination": ""},
        },
    }
    beats = [
        {"node_id": "hook", "block": "hook", "purpose": "attention", "text": "Your lead waits.", "start_seconds": 0.0, "end_seconds": 3.0, "word_count": 3},
        {"node_id": "problem", "block": "problem", "purpose": "recognition", "text": "The handoff is still manual.", "start_seconds": 3.0, "end_seconds": 8.0, "word_count": 5},
        {"node_id": "stakes", "block": "stakes", "purpose": "stakes", "text": "The customer feels the delay.", "start_seconds": 8.0, "end_seconds": 12.0, "word_count": 5},
        {"node_id": "proof", "block": "proof", "purpose": "proof", "text": "A quote form records the incoming lead.", "start_seconds": 12.0, "end_seconds": 18.0, "word_count": 7},
        {"node_id": "takeaway", "block": "takeaway", "purpose": "payoff", "text": "Choose the wait you can measure.", "start_seconds": 18.0, "end_seconds": 25.0, "word_count": 6},
        {"node_id": "cta", "block": "call_to_action", "purpose": "action", "text": "Score one task today.", "start_seconds": 25.0, "end_seconds": 30.0, "word_count": 4},
    ]
    transcript = " ".join(item["text"] for item in beats)
    transcript_sha = hashlib.sha256(transcript.encode()).hexdigest()
    copy_gate = {
        "contract": "substantive_copy_provenance_gate_v1",
        "passed": True,
        "failure_codes": [],
        "provenance_gate": {
            "passed": True,
            "failure_codes": [],
            "candidate_sha256": transcript_sha,
            "provenance_sha256": "a" * 64,
            "source_material_usage": "abstract_patterns_only",
            "independent_verification_receipt_ids": [],
        },
    }
    audit_core = {
        "status": "pass",
        "contract": "content_creation_audit_v1",
        "audit_id": "refaudit-test-1",
        "corpus_id": "reference-corpus-1",
        "copy_gate": copy_gate,
        "rights": {
            "state": "public_reference_analysis_only",
            "direct_use_allowed": False,
            "identity_imitation_allowed": False,
            "likeness_imitation_allowed": False,
            "voice_imitation_allowed": False,
            "source_clip_use_allowed": False,
        },
        "created_at": "2026-08-28T12:00:00+00:00",
    }
    audit_result_sha = canonical_sha256(audit_core)
    audit = {
        **audit_core,
        "request_sha256": "b" * 64,
        "result_sha256": audit_result_sha,
    }
    request_sha = canonical_sha256(request)
    package = {
        "status": "approved",
        "contract": "reference_marketing_script_package_v1",
        "script_id": "refscript-test-1",
        "corpus_id": "reference-corpus-1",
        "context_id": "refctx-test-1",
        "request_contract": "reference_marketing_script_request_v1",
        "request_sha256": request_sha,
        "request": request,
        "marketing_logic": {
            "content_role": "EDUCATE",
            "topic_distance_from_offer": 3,
            "topic_ladder_id": "automation-wait-v1",
            "rhetorical_structure": {"structure_id": "contrast"},
        },
        "script": {
            "title": "Remove the Wait",
            "target_seconds": 30,
            "word_count": len(transcript.split()),
            "transcript": transcript,
            "beats": beats,
            "delivery_visual_plan": {"contract": "delivery_visual_plan_v1"},
        },
        "quality": {
            "status": "pass",
            "score": 99.0,
            "checks": {},
            "failed_checks": [],
            "owner_calibrated": {
                "contract": "owner_calibrated_script_quality_v1",
                "decision": "PASS",
                "score": 99.0,
                "failure_codes": [],
            },
        },
        "proof_evidence_gate": {
            "contract": "owned_evidence_validation_v1",
            "passed": True,
            "required": False,
            "receipt_ids": [],
            "failure_codes": [],
        },
        "corpus_audit": audit,
        "reference_context": {},
        "rights": {
            "state": "public_reference_analysis_only",
            "source_clips_used": False,
            "direct_use_allowed": False,
            "identity_imitation_allowed": False,
            "voice_imitation_allowed": False,
            "exact_draft_copy_gate_passed": True,
        },
        "revision": {"contract": "bounded_script_quality_rewrite_v1"},
        "lineage": {
            "request_sha256": request_sha,
            "audit_result_sha256": audit_result_sha,
        },
        "script_experiment_registration": {
            "status": "created",
            "experiment": {"brief_id": "refbrief-test-1"},
        },
        "created_at": "2026-08-28T12:00:00+00:00",
    }
    package["result_sha256"] = canonical_sha256(package)
    return package


def _seed_evidence(store: QualityStore, *, distinct_creators: int = 5):
    receipt_ids = []
    for index in range(5):
        text = f"transcript evidence {index}"
        receipt = store.put_receipt(
            "viral_transcript_pattern",
            "youtube",
            f"video-{index}",
            f"https://www.youtube.com/watch?v=video-{index}",
            {
                "creator_id": f"creator-{index % distinct_creators}",
                "transcript_source": "local_whisper",
                "transcript_id": f"whisper-{index}",
                "transcript_sha256": hashlib.sha256(text.encode()).hexdigest(),
                "observation_key": hashlib.sha256(f"obs-{index}".encode()).hexdigest(),
                "performance_qualification": {
                    "audit_decision": "PASS",
                    "checks": {"artifact_verified": True},
                },
                "pattern": {"source_metrics": {"views": 30_000}},
            },
        )
        receipt_ids.append(receipt["receipt_id"])
    moment = {
        "moment_id": "moment-test-1",
        "situation": "if somebody fills out a quote form on your website",
        "audience": "software founders",
        "stakes": "if somebody fills out a quote form on your website",
        "source_transcript_id": "whisper-0",
        "source_observation_key": hashlib.sha256(b"obs-0").hexdigest(),
        "stakes_source_moment_id": "moment-test-1",
        "stakes_source_transcript_id": "whisper-0",
        "stakes_source_observation_key": hashlib.sha256(b"obs-0").hexdigest(),
    }
    moment_receipt = store.put_receipt(
        "audience_human_moments",
        "youtube",
        "audience-test-1",
        None,
        {"audience": "software founders", "moments": [moment]},
    )
    return receipt_ids, moment_receipt["receipt_id"], moment["moment_id"]


def _store_reference_package(reference, package: dict) -> None:
    reference._upsert_corpus(
        corpus_id=package["corpus_id"],
        username="reference-test",
        target_count=1,
        state="complete",
        profile={"username": "reference-test"},
    )
    audit = package["corpus_audit"]
    with reference.connect() as connection:
        connection.execute(
            """INSERT INTO reference_audit_receipts(
                   audit_id, corpus_id, contract, request_sha256,
                   result_json, result_sha256, created_at
               ) VALUES(?,?,?,?,?,?,?)""",
            (
                audit["audit_id"], package["corpus_id"], audit["contract"],
                audit["request_sha256"], json.dumps(audit, sort_keys=True),
                audit["result_sha256"], audit["created_at"],
            ),
        )
        connection.commit()
    reference.put_script_package(package)


def _v2_reference_package() -> dict:
    package = copy.deepcopy(_reference_package())
    package["contract"] = "reference_marketing_script_package_v2"
    package["request_contract"] = "reference_marketing_script_request_v2"
    package["request"]["contract"] = "reference_marketing_script_request_v2"
    request_sha = canonical_sha256(package["request"])
    package["request_sha256"] = request_sha
    package["lineage"]["request_sha256"] = request_sha
    package["result_sha256"] = canonical_sha256(
        {key: value for key, value in package.items() if key != "result_sha256"}
    )
    return package


def test_validator_accepts_coherent_v1_and_v2_contract_pairs() -> None:
    assert validate_reference_package(_reference_package())["request_sha256"]
    assert validate_reference_package(_v2_reference_package())["request_sha256"]

    mismatched = _v2_reference_package()
    mismatched["request_contract"] = "reference_marketing_script_request_v1"
    mismatched["result_sha256"] = canonical_sha256(
        {key: value for key, value in mismatched.items() if key != "result_sha256"}
    )
    with pytest.raises(ValueError, match="request_contract is invalid"):
        validate_reference_package(mismatched)


def test_binds_package_and_is_idempotent(tmp_path):
    store = QualityStore(tmp_path / "quality.sqlite3")
    source_ids, moment_receipt_id, moment_id = _seed_evidence(store)
    binder = ReferenceScriptQualityBinder(store)

    first = binder.bind(
        _reference_package(),
        source_receipt_ids=source_ids,
        source_moment_receipt_id=moment_receipt_id,
        source_moment_id=moment_id,
    )
    second = binder.bind(
        _reference_package(),
        source_receipt_ids=source_ids,
        source_moment_receipt_id=moment_receipt_id,
        source_moment_id=moment_id,
    )

    assert first["status"] == "created"
    assert second["status"] == "idempotent_replay"
    assert first["contract"] == BINDING_CONTRACT
    assert first["script_sha256"] == second["script_sha256"]
    assert first["binding_receipt"]["receipt_id"] == second["binding_receipt"]["receipt_id"]
    assert first["owner_quality_audit"]["decision"] == "PASS"
    assert second["owner_quality_audit"]["audit_id"] == first["owner_quality_audit"]["audit_id"]
    assert store.script_gate_summary("refscript-test-1")["latest_audits"][
        "owner_calibrated_quality"
    ]["stored_script_binding_valid"] is True
    stored = store.script("refscript-test-1")
    assert stored["reference_package_binding"]["performance_cohort"] == {
        "accepted_transcript_count": 5,
        "creator_count": 5,
        "observed_views_snapshot": 150_000,
        "actual_audience_relatability_measured": False,
    }
    assert stored["human_moment"]["source_moment_receipt_id"] == moment_receipt_id
    assert [item["beat"] for item in stored["timeline"]] == [
        "human_hook", "human_problem", "stakes", "proof", "payoff", "cta"
    ]


def test_rejects_package_tampering_and_underdiverse_evidence(tmp_path):
    store = QualityStore(tmp_path / "quality.sqlite3")
    source_ids, moment_receipt_id, moment_id = _seed_evidence(
        store, distinct_creators=1
    )
    binder = ReferenceScriptQualityBinder(store)
    with pytest.raises(ValueError, match="at least three creators"):
        binder.bind(
            _reference_package(),
            source_receipt_ids=source_ids,
            source_moment_receipt_id=moment_receipt_id,
            source_moment_id=moment_id,
        )

    diverse_store = QualityStore(tmp_path / "diverse.sqlite3")
    source_ids, moment_receipt_id, moment_id = _seed_evidence(diverse_store)
    package = copy.deepcopy(_reference_package())
    package["script"]["beats"][0]["text"] = "A different opening."
    package["script"]["transcript"] = " ".join(
        item["text"] for item in package["script"]["beats"]
    )
    package["result_sha256"] = canonical_sha256(
        {key: value for key, value in package.items() if key != "result_sha256"}
    )
    with pytest.raises(ValueError, match="copy provenance"):
        ReferenceScriptQualityBinder(diverse_store).bind(
            package,
            source_receipt_ids=source_ids,
            source_moment_receipt_id=moment_receipt_id,
            source_moment_id=moment_id,
        )

    malformed = copy.deepcopy(_reference_package())
    malformed["script"]["beats"][0]["start_seconds"] = "not-a-number"
    malformed["result_sha256"] = canonical_sha256(
        {key: value for key, value in malformed.items() if key != "result_sha256"}
    )
    with pytest.raises(ValueError, match="timing must be numeric"):
        ReferenceScriptQualityBinder(diverse_store).bind(
            malformed,
            source_receipt_ids=source_ids,
            source_moment_receipt_id=moment_receipt_id,
            source_moment_id=moment_id,
        )


def test_authenticated_api_binds_then_audits_exact_stored_script(tmp_path):
    app = create_content_quality_app({
        "TESTING": True,
        "CONTENT_QUALITY_CONTROL_TOKEN": "test-control-token",
        "CONTENT_REFERENCE_ROOT": str(tmp_path / "reference"),
        "CONTENT_QUALITY_DB": str(tmp_path / "quality.sqlite3"),
        "MARKET_TAPE_DB": str(tmp_path / "market-tape.sqlite3"),
        "NARRATIVE_COHERENCE_LLM": "off",
        "RELATABILITY_JUDGE": "off",
    })
    package = _reference_package()
    _store_reference_package(app.extensions["reference_corpus"], package)
    engine = app.extensions["content_quality_engine"]
    source_ids, moment_receipt_id, moment_id = _seed_evidence(engine.store)
    body = {
        "source_receipt_ids": source_ids,
        "source_moment_receipt_id": moment_receipt_id,
        "source_moment_id": moment_id,
    }
    client = app.test_client()

    denied = client.post(
        "/api/reference-corpus/scripts/refscript-test-1/bind-quality", json=body
    )
    assert denied.status_code == 401
    response = client.post(
        "/api/reference-corpus/scripts/refscript-test-1/bind-quality",
        json=body,
        headers={"Authorization": "Bearer test-control-token"},
    )
    assert response.status_code == 200
    assert response.get_json()["script_id"] == "refscript-test-1"

    attention = engine.attention.script_audit({"script_id": "refscript-test-1"})
    assert attention["decision"] == "PASS"
    assert attention["findings"]["input_binding"]["stored_script_bound"] is True
    assert attention["findings"]["input_binding"]["script_sha256"] == response.get_json()["script_sha256"]

    narrative = client.post(
        "/api/narrative-coherence/audit", json={"script_id": "refscript-test-1"}
    )
    assert narrative.status_code == 200
    assert narrative.get_json()["decision"] == "PASS"
    assert narrative.get_json()["findings"]["input_binding"] == {
        "contract": "stored_script_audit_binding_v1",
        "stored_script_bound": True,
        "script_id": "refscript-test-1",
        "script_sha256": response.get_json()["script_sha256"],
    }
