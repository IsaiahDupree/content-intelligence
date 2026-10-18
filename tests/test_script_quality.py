from __future__ import annotations

from services.content_quality.script_intelligence import (
    candidate_quality_failure_codes,
    is_retryable_quality_failure,
)
from services.content_quality.script_quality import (
    OWNER_QUALITY_CONTRACT,
    arrange_role_components,
    audit_owner_calibrated_quality,
    build_delivery_visual_plan,
    repair_owner_quality_text,
    retime_timeline,
    select_rhetorical_structure,
)


def test_structure_rotation_is_distinct_and_keeps_source_text_once() -> None:
    structures = [
        select_rhetorical_structure(
            "evidence_story", seed="stable-source", attempt=index
        )
        for index in range(4)
    ]
    assert len({item["structure_id"] for item in structures}) == 4

    source_text = "I missed a client reply while I was building the app."
    components = {
        "hook": [{"beat": "human_hook", "text": source_text}],
        "stakes": [{"beat": "stakes", "text": "The lead went cold."}],
        "context": [{"beat": "evidence_context", "text": "The source is saved."}],
        "proof": [{"beat": "proof", "text": "The reply was sent."}],
        "claim": [{"beat": "claim", "text": "A clear reply matters."}],
        "method": [{"beat": "method", "text": "Open, write, and send."}],
        "payoff": [{"beat": "payoff", "text": "The client gets an answer."}],
        "cta": [{"beat": "cta", "text": "Save this test."}],
    }
    arranged = arrange_role_components(components, structures[0])
    assert arranged[-1]["beat"] == "cta"
    assert [item["text"] for item in arranged].count(source_text) == 1


def test_owner_judge_allows_first_person_and_reports_five_judgments() -> None:
    text = (
        "I missed a client message while I was building the app, and the lead "
        "went cold. I opened the inbox, copied the question, wrote one clear "
        "reply, and sent it before the next call. That fixed the wait and gave "
        "the client a clear answer. Save this and test it on one message today."
    )
    report = audit_owner_calibrated_quality(text)

    assert report["contract"] == OWNER_QUALITY_CONTRACT
    assert report["decision"] == "PASS"
    assert report["judgments"]["spoken_naturalness"][
        "perspective_authorization_evaluated"
    ] is False
    assert set(report["judgments"]) == {
        "spoken_naturalness",
        "specificity",
        "tension_payoff",
        "technical_language_leakage",
        "repeated_phrasing",
    }


def test_tension_payoff_requires_both_sides_in_order() -> None:
    tension_only = audit_owner_calibrated_quality(
        "The client is stuck, the inbox is cold, and the delay gets worse."
    )
    payoff_only = audit_owner_calibrated_quality(
        "The answer is clear, the reply is sent, and the task is finished."
    )
    reverse_order = audit_owner_calibrated_quality(
        "The reply is sent and the task is finished before the client gets stuck."
    )
    reverse_with_role_labels = audit_owner_calibrated_quality(
        "The reply is sent and the task is finished. The client gets stuck.",
        timeline=[
            {"beat": "stakes", "text": "The reply is sent."},
            {"beat": "payoff", "text": "The client gets stuck."},
        ],
    )

    for report in (
        tension_only,
        payoff_only,
        reverse_order,
        reverse_with_role_labels,
    ):
        assert report["judgments"]["tension_payoff"]["passed"] is False
        assert "OWNER_TENSION_PAYOFF" in report["failure_codes"]


def test_topic_specific_science_turn_passes_without_keyword_filler() -> None:
    beats = [
        (
            "hook",
            "A glassfrog does not make its blood transparent. It moves most red "
            "blood cells into its reflective liver while resting.",
        ),
        (
            "problem",
            "Blood absorbs light, so visible circulation would expose the animal.",
        ),
        (
            "stakes",
            "Hiding those cells raises transparency but briefly limits oxygen transport.",
        ),
        (
            "proof",
            "A lab team measured about 89 percent of circulating red blood cells "
            "packed into the liver, raising transparency two to three times.",
        ),
        (
            "reframe",
            "The camouflage comes from relocating blood, not changing what blood is.",
        ),
        (
            "teaching_step",
            "Watch the frog at rest, when red cells leave the visible tissue.",
        ),
        (
            "teaching_step",
            "Follow the cells back into circulation when the frog becomes active.",
        ),
        (
            "takeaway",
            "A timed storage tradeoff lets a living animal become harder to see.",
        ),
        (
            "call_to_action",
            "Look for the hidden movement behind the visible change.",
        ),
    ]
    report = audit_owner_calibrated_quality(
        " ".join(text for _role, text in beats),
        timeline=[{"block": role, "text": text} for role, text in beats],
        protected_phrases=(beats[3][1],),
    )

    assert report["decision"] == "PASS"
    assert report["judgments"]["specificity"]["topic_anchor_hits"]
    assert report["judgments"]["tension_payoff"]["role_turn_supported"] is True
    assert report["judgments"]["repeated_phrasing"][
        "generic_quality_bridge_hits"
    ] == []


def test_semantic_role_credit_requires_exact_timeline_projection() -> None:
    text = (
        "Glassfrog blood absorbs light through transparent tissue. "
        "Reflective crystals surround the liver during sleep."
    )
    report = audit_owner_calibrated_quality(
        text,
        timeline=[
            {"block": "problem", "text": "Words absent from the script."},
            {"block": "takeaway", "text": "A fabricated resolution."},
        ],
    )

    judgment = report["judgments"]["tension_payoff"]
    assert judgment["passed"] is False
    assert judgment["role_turn_supported"] is False
    assert judgment["timeline_binding"]["exact_ordered_projection"] is False
    assert judgment["timeline_binding"]["failure_codes"] == [
        "timeline_not_exact_ordered_script_projection"
    ]


def test_generic_quality_bridge_family_fails_owner_judgment() -> None:
    text = " ".join((
        "The problem is hard to see, so you look at the result, then you compare "
        "the evidence and check the clear answer.",
        "The risk is missing the cause, so you record the result, then you "
        "compare it and check which answer works.",
        "A wrong guess creates friction, so you show the result, then you compare "
        "the proof and check the answer.",
    ))
    report = audit_owner_calibrated_quality(text)

    assert report["decision"] == "REVISE"
    assert "OWNER_REPEATED_PHRASING" in report["failure_codes"]
    assert report["judgments"]["specificity"]["passed"] is False
    assert len(report["judgments"]["repeated_phrasing"][
        "generic_quality_bridge_hits"
    ]) == 3


def test_corpus_count_narration_is_a_quality_failure() -> None:
    report = audit_owner_calibrated_quality(
        "Across 8 public creator videos, the same problem kept coming up. "
        "A client message went cold. Open the inbox, write one clear reply, "
        "send it, and save the answer before the next call."
    )

    leakage = report["judgments"]["technical_language_leakage"]
    assert leakage["passed"] is False
    assert leakage["corpus_count_narration_hits"] == [
        "Across 8 public creator videos"
    ]
    assert "OWNER_TECHNICAL_LANGUAGE_LEAKAGE" in report["failure_codes"]


def test_literal_repair_keeps_protected_claim_and_number_exact() -> None:
    protected = "I used the workflow receipt for 17 client replies"
    text = (
        protected
        + ". The content machine needs a typed contract and routing rules. "
        "The visible result is a finished reply. First, open the inbox. "
        "First, open the inbox."
    )
    report = audit_owner_calibrated_quality(
        text, protected_phrases=(protected,)
    )
    repaired = repair_owner_quality_text(
        text, report, protected_phrases=(protected,)
    )

    assert protected in repaired
    assert "17" in repaired
    assert "content machine" not in repaired.casefold()
    assert "typed contract" not in repaired.casefold()
    assert "The visible result is" not in repaired
    assert audit_owner_calibrated_quality(
        repaired, protected_phrases=(protected,)
    )["judgments"]["technical_language_leakage"]["passed"] is True


def test_delivery_plan_is_timed_and_reference_safe() -> None:
    timeline = retime_timeline(
        [
            {"beat": "human_hook", "text": "A client message went cold."},
            {"beat": "proof", "text": "The saved reply shows what changed."},
            {"beat": "payoff", "text": "The client gets a clear answer."},
            {"beat": "cta", "text": "Save this test."},
        ],
        target_seconds=30.0,
    )
    plan = build_delivery_visual_plan(
        timeline, structure_id="proof_then_turn"
    )

    assert plan["cue_count"] > len(timeline)
    assert plan["cues"][0]["start_seconds"] == 0.0
    assert plan["cues"][-1]["end_seconds"] == 30.0
    assert plan["maximum_visual_interrupt_gap_seconds"] == 3.0
    assert plan["actual_maximum_visual_interrupt_gap_seconds"] <= 3.0
    assert any(
        cue["visual"]["interrupt"] == "intra_beat_reset"
        for cue in plan["cues"]
    )
    assert plan["asset_policy"] == {
        "owned_or_licensed_assets_required": True,
        "reference_clips_used": False,
        "reference_identity_likeness_or_voice_used": False,
    }
    assert all(
        cue["visual"]["reference_clip_allowed"] is False
        and cue["visual"]["reference_identity_or_voice_allowed"] is False
        for cue in plan["cues"]
    )


def test_retry_policy_never_retries_evidence_rights_or_judge_outages() -> None:
    decisions = {
        "narrative": True,
        "owner_quality": False,
        "relatability": True,
        "qualitative_relatability": True,
        "cohort_integrity": True,
        "transcript_style": True,
        "attention": True,
        "video_preflight": True,
    }
    audits = {
        "owner_calibrated_quality": {
            "findings": {
                "quality": {"failure_codes": ["OWNER_SPECIFICITY"]}
            }
        },
        "qualitative_relatability": {"decision": "PASS"},
        "transcript_style": {"findings": {"copy_gate": {"passed": True}}},
    }
    assert is_retryable_quality_failure(decisions, audits) is True
    assert candidate_quality_failure_codes(decisions, audits) == [
        "OWNER_SPECIFICITY"
    ]

    decisions["cohort_integrity"] = False
    assert is_retryable_quality_failure(decisions, audits) is False
    decisions["cohort_integrity"] = True
    audits["transcript_style"]["findings"]["copy_gate"]["passed"] = False
    assert is_retryable_quality_failure(decisions, audits) is False
    audits["transcript_style"]["findings"]["copy_gate"]["passed"] = True
    audits["qualitative_relatability"]["decision"] = "JUDGE_UNAVAILABLE"
    assert is_retryable_quality_failure(decisions, audits) is False
