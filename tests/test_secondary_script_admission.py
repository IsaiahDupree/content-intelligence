import asyncio
import importlib.util
import sys
from pathlib import Path

import pytest

from services.content_brief import EnhancedBriefService
from services.content_brief.models import EnhancedBrief, ScriptBeat as BriefScriptBeat
from services.content_brief.script_generator import ScriptGenerator
from services.narrative.content_orchestration import (
    ContentBriefFromNarrative,
    NarrativeContentOrchestrator,
)
from services.spoken_script_admission import (
    admit_spoken_components,
    audit_claim_safety,
)
from services.trend_flash.flash_generator import FlashGenerator
from services.trend_flash.trend_radar import TrendCluster


_REEL_MODULE_NAME = "secondary_reeltrends_service"
_REEL_SPEC = importlib.util.spec_from_file_location(
    _REEL_MODULE_NAME,
    Path(__file__).parents[1]
    / "services"
    / "trend_intelligence"
    / "reeltrends_service.py",
)
assert _REEL_SPEC and _REEL_SPEC.loader
_REEL_MODULE = importlib.util.module_from_spec(_REEL_SPEC)
sys.modules[_REEL_MODULE_NAME] = _REEL_MODULE
_REEL_SPEC.loader.exec_module(_REEL_MODULE)
ReelTrendsService = _REEL_MODULE.ReelTrendsService
ReelScriptBeat = _REEL_MODULE.ScriptBeat


PASSING_COMPONENTS = {
    "hook": [{
        "node_id": "hook",
        "text": "A client inbox is stuck, and the delay gets worse.",
    }],
    "stakes": [{
        "node_id": "stakes",
        "text": "The missing answer stops the call and creates another wait.",
    }],
    "method": [{
        "node_id": "method",
        "text": "Open the account, check each message, answer the question, and save the reply.",
    }],
    "payoff": [{
        "node_id": "payoff",
        "text": "Then book the call. The result is clear: the problem is fixed without another delay.",
    }],
    "cta": [{
        "node_id": "cta",
        "text": "Test this on one email today.",
    }],
}


def test_secondary_admission_pass_has_bounded_interrupt_plan():
    result = admit_spoken_components(
        PASSING_COMPONENTS,
        family="evidence_story",
        seed="deterministic-pass",
        target_seconds=30,
    )

    assert result["status"] == "ready"
    assert result["revision"]["attempt_count"] <= 3
    assert result["owner_quality"]["decision"] == "PASS"
    plan = result["delivery_visual_plan"]
    assert plan["maximum_interrupt_gap_seconds"] == 3.0
    times = [item["at_seconds"] for item in plan["interrupt_schedule"]]
    assert max((later - earlier for earlier, later in zip(times, times[1:])), default=0) <= 3
    assert plan["asset_policy"]["reference_clips_used"] is False
    assert plan["asset_policy"]["reference_identity_likeness_or_voice_used"] is False


@pytest.mark.parametrize(
    "text",
    (
        "I lost three hours fixing the inbox.",
        "My inbox was chaos.",
        "We built a tool that always works.",
        "Send the result to me.",
        "Our client saved the day.",
    ),
)
def test_claim_safety_rejects_every_unreceipted_first_person_token(text):
    report = audit_claim_safety(text)
    assert report["decision"] == "REVISE"
    assert "UNSUPPORTED_FIRST_PARTY_ASSERTION" in report["failure_codes"]


def test_evidence_cannot_bypass_claim_checks_without_receipt_ids():
    with pytest.raises(ValueError, match="receipt-resolved"):
        admit_spoken_components(
            PASSING_COMPONENTS,
            family="evidence_story",
            seed="unreceipted",
            target_seconds=30,
            evidence_phrases=("I built this.",),
        )


def test_content_brief_writer_returns_no_renderable_beats_when_blocked():
    assert EnhancedBriefService.__name__ == "EnhancedBriefService"
    brief = EnhancedBrief(
        brief_id="brief-first-person",
        title="Inbox",
        script_beats=[BriefScriptBeat(
            id="seg_1",
            t="0-5",
            text="My inbox was chaos.",
            intent="hook",
        )],
    )

    result = ScriptGenerator().generate_script(brief)

    assert result.metadata["status"] == "blocked_quality"
    assert result.segments == []
    assert result.hook == ""
    assert result.metadata["quality_revision"]["attempt_count"] == 3
    assert "UNSUPPORTED_FIRST_PARTY_ASSERTION" in result.metadata[
        "claim_safety"
    ]["failure_codes"]


def test_trend_flash_provider_unavailable_never_uses_a_template_fallback():
    generator = FlashGenerator.__new__(FlashGenerator)
    generator.client = None
    cluster = TrendCluster(
        id="trend-1",
        topic="inbox delays",
        summary="A missing answer can stop a client call.",
        top_questions=["Why is the inbox stuck?"],
    )

    candidate = asyncio.run(generator._generate_script(cluster, "educational"))

    assert candidate["_generation_error"] == "provider_unavailable"
    assert candidate["hook"] == cluster.top_questions[0]
    assert candidate["context"] == cluster.summary
    assert candidate["take"] == candidate["action"] == candidate["cta"] == ""
    assert "consistency over perfection" not in " ".join(candidate.values())


def test_reeltrends_actual_writer_admits_only_audited_beats():
    service = ReelTrendsService(api_key="")
    beats = [
        ReelScriptBeat(
            name="build_up",
            duration_seconds=10,
            script=(
                "A client inbox is stuck, and the delay gets worse. "
                "The missing answer stops the call and creates another wait."
            ),
            visual_notes="unused source note",
            word_count=18,
        ),
        ReelScriptBeat(
            name="punchline",
            duration_seconds=15,
            script=(
                "Open the account, check each message, answer the question, and save the reply. "
                "Then book the call. The result is clear: the problem is fixed without another delay."
            ),
            visual_notes="unused source note",
            word_count=28,
        ),
        ReelScriptBeat(
            name="cta",
            duration_seconds=5,
            script="Test this on one email today.",
            visual_notes="unused source note",
            word_count=6,
        ),
    ]

    result = service._admit_script("inbox delays", beats, 30)

    assert result["status"] == "ready"
    assert result["owner_quality"]["decision"] == "PASS"
    assert result["rights"]["source_identity_likeness_or_voice_used"] is False


def test_narrative_fallback_removes_fabricated_intro_and_blocks_first_person():
    orchestrator = NarrativeContentOrchestrator(openai_api_key="")
    brief = ContentBriefFromNarrative(
        id="narrative-1",
        topic="inbox delays",
        hook="My inbox was chaos.",
        key_points=["Open the account", "Check the message", "Answer the question"],
        call_to_action="Save the reply.",
        target_duration_seconds=30,
    )

    assembled = orchestrator._generate_basic_script(brief)
    package = asyncio.run(orchestrator.convert_brief_to_script(brief))

    assert "Today I want" not in assembled
    assert package["status"] == "blocked_quality"
    assert package["transcript"] == ""
    assert "UNSUPPORTED_FIRST_PARTY_ASSERTION" in package[
        "claim_safety"
    ]["failure_codes"]
