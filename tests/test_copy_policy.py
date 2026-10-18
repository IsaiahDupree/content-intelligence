from __future__ import annotations

from services.content_quality.copy_policy import (
    audit_substantive_copy,
    build_independent_verification_receipt,
    build_script_only_provenance,
    sha256_text,
)


def test_substantive_expression_fails_without_a_word_count_cutoff() -> None:
    source = (
        "Atomic clocks keep a steady beat. "
        "Earth rotates unevenly, so their answers drift apart."
    )
    candidate = (
        "Atomic clocks keep a steady beat. "
        "That creates an engineering problem worth explaining."
    )
    receipt = audit_substantive_copy(
        candidate,
        [{"source_id": "source-1", "text": source}],
        provenance=build_script_only_provenance(candidate),
    )

    assert receipt["passed"] is False
    assert "COPIED_EXPRESSION" in receipt["failure_codes"]
    assert receipt["policy"]["fixed_matching_word_limit_applied"] is False
    assert "maximum_five_word_overlap" not in receipt


def test_ordered_expression_and_structure_are_separate_blockers() -> None:
    source = (
        "Why does the clock look wrong? "
        "The problem appears when two time standards drift. "
        "Use the evidence to check the final timestamp."
    )
    candidate = source + " Then explain the result in plain language."
    receipt = audit_substantive_copy(
        candidate,
        [{"source_id": "source-2", "text": source}],
        provenance=build_script_only_provenance(candidate),
    )

    assert receipt["passed"] is False
    assert "COPIED_SEQUENCE" in receipt["failure_codes"]
    assert receipt["substantive_copy"]["source_findings"][0][
        "copied_structure"
    ] is True


def test_independently_verified_fact_can_pass_when_expression_is_original() -> None:
    source = (
        "Atomic clocks keep a steady beat, but Earth rotates unevenly. "
        "Timekeepers sometimes add a leap second."
    )
    claim = "Earth does not rotate at one perfectly uniform rate."
    candidate = (
        f"{claim} That mismatch creates occasional coordination work for "
        "the people maintaining civil time."
    )
    verification = build_independent_verification_receipt(
        receipt_id="verify-earth-rotation-1",
        claim=claim,
        source_url="https" + "://www.iers.org/IERS/EN/Science/EarthRotation/EarthRotation.html",
        source_kind="official_source",
        source_sha256=sha256_text("captured official source bytes"),
        verified_at="2026-08-25T20:00:00Z",
    )
    provenance = build_script_only_provenance(
        candidate,
        source_material_usage="facts_or_general_ideas_only",
        independent_verification_receipts=[verification],
    )
    receipt = audit_substantive_copy(
        candidate,
        [{"source_id": "source-3", "text": source}],
        provenance=provenance,
    )

    assert receipt["passed"] is True
    assert receipt["provenance_gate"]["passed"] is True
    assert receipt["substantive_copy"]["copied_expression"] is False


def test_missing_or_forbidden_provenance_fails_closed() -> None:
    candidate = "A fresh explanation with no source expression."
    missing = audit_substantive_copy(candidate, [], provenance=None)
    forbidden = build_script_only_provenance(candidate)
    forbidden["creator_voice_used"] = True
    forbidden["source_clip_ids"] = ["clip-1"]
    rejected = audit_substantive_copy(candidate, [], provenance=forbidden)

    assert missing["passed"] is False
    assert missing["failure_codes"] == ["MISSING_COPY_PROVENANCE"]
    assert rejected["passed"] is False
    assert "COPY_PROVENANCE_HASH_MISMATCH" in rejected["failure_codes"]
    assert "FORBIDDEN_CREATOR_VOICE_USED" in rejected["failure_codes"]
    assert "FORBIDDEN_SOURCE_CLIP_IDS" in rejected["failure_codes"]


def test_fact_mode_rejects_missing_or_tampered_verification() -> None:
    claim = "The measured value changed after the official adjustment."
    missing = build_script_only_provenance(
        claim, source_material_usage="facts_or_general_ideas_only"
    )
    missing_result = audit_substantive_copy(claim, [], provenance=missing)

    receipt = build_independent_verification_receipt(
        receipt_id="verify-1",
        claim=claim,
        source_url="https" + "://www.bipm.org/en/time-frequency",
        source_kind="primary_source",
        source_sha256=sha256_text("measurement bytes"),
        verified_at="2026-08-25T20:00:00Z",
    )
    receipt["claim"] = "A different claim."
    tampered = build_script_only_provenance(
        claim,
        source_material_usage="facts_or_general_ideas_only",
        independent_verification_receipts=[receipt],
    )
    tampered_result = audit_substantive_copy(claim, [], provenance=tampered)

    assert "INDEPENDENT_VERIFICATION_REQUIRED" in missing_result["failure_codes"]
    assert tampered_result["passed"] is False
    assert any(
        code.endswith("RECEIPT_HASH_MISMATCH")
        for code in tampered_result["failure_codes"]
    )
    assert tampered_result["provenance_gate"][
        "independent_verification_receipt_ids"
    ] == []


def test_provenance_rejects_invalid_reference_ids_and_url_shape() -> None:
    claim = "The primary record reports a changed measurement."
    verification = build_independent_verification_receipt(
        receipt_id="verify-url-shape",
        claim=claim,
        source_url="https:official.example/measurement",
        source_kind="primary_source",
        source_sha256=sha256_text("primary record bytes"),
        verified_at="2026-08-25T20:00:00Z",
    )
    provenance = build_script_only_provenance(
        claim,
        source_material_usage="facts_or_general_ideas_only",
        independent_verification_receipts=[verification],
    )
    provenance["reference_item_ids"] = "source-1"
    result = audit_substantive_copy(claim, [], provenance=provenance)

    assert result["passed"] is False
    assert "INVALID_REFERENCE_ITEM_IDS" in result["failure_codes"]
    assert "INDEPENDENT_VERIFICATION_0_SOURCE_URL_INVALID" in result["failure_codes"]
    assert result["provenance_gate"]["independent_verification_receipt_ids"] == []


def test_creator_identity_reference_fails_even_when_declaration_says_unused() -> None:
    candidate = "The explanation from @clockmaker-lab uses a clear example."
    result = audit_substantive_copy(
        candidate,
        [{
            "source_id": "source-identity",
            "text": "A different discussion of timekeeping.",
            "creator_identifiers": ["clockmaker-lab"],
        }],
        provenance=build_script_only_provenance(candidate),
    )

    assert result["passed"] is False
    assert "CREATOR_IDENTITY_REFERENCE" in result["failure_codes"]
    assert result["substantive_copy"]["creator_identity_references"] == [{
        "source_id": "source-identity",
        "identifier": "clockmaker-lab",
    }]
