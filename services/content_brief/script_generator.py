"""
Script Generator
================
Generates script.json from content briefs.
"""

import logging
from typing import List

from services.spoken_script_admission import admit_spoken_components
from .models import EnhancedBrief, ScriptBeat, ScriptOutput

logger = logging.getLogger(__name__)


class ScriptGenerator:
    """
    Generates script.json from content briefs.
    
    Output format matches Stage A of Media Factory pipeline.
    """
    
    def __init__(self):
        """Initialize script generator."""
        pass
    
    def generate_script(self, brief: EnhancedBrief) -> ScriptOutput:
        """
        Generate script.json from enhanced brief.
        
        Args:
            brief: Enhanced content brief
        
        Returns:
            ScriptOutput with script beats
        """
        # If brief already has script beats, use them
        if brief.script_beats:
            segments = brief.script_beats
        else:
            # Generate from brief content
            segments = self._generate_beats_from_brief(brief)
        
        admission = admit_spoken_components(
            self._quality_components(segments),
            family="reference_marketing",
            seed=brief.brief_id,
            target_seconds=max(1.0, float(brief.length_sec)),
            # This legacy path has no receipt-resolved evidence contract.
            evidence_phrases=(),
            preferred_structure=self._preferred_structure(brief),
        )
        admitted_segments = (
            self._segments_from_admission(admission)
            if admission["status"] == "ready"
            else []
        )

        # Blocked candidates stay reviewable in metadata but cannot render.
        total_duration = self._calculate_duration(admitted_segments)
        word_count = sum(len(beat.text.split()) for beat in admitted_segments)
        
        # Estimate TTS duration (rough: 150 words per minute)
        estimated_tts_duration = (word_count / 150) * 60
        
        return ScriptOutput(
            brief_id=brief.brief_id,
            title=brief.title or (brief.angle.promise if brief.angle else "Untitled"),
            hook=admitted_segments[0].text if admitted_segments else "",
            segments=admitted_segments,
            metadata={
                "status": admission["status"],
                "block_reason": admission["block_reason"],
                "total_duration_sec": total_duration,
                "word_count": word_count,
                "estimated_tts_duration": estimated_tts_duration,
                "format": brief.format,
                "platform": brief.platform,
                "rhetorical_structure": admission["rhetorical_structure"],
                "owner_quality": admission["owner_quality"],
                "claim_safety": admission["claim_safety"],
                "delivery_visual_plan": admission["delivery_visual_plan"],
                "quality_revision": admission["revision"],
                "blocked_candidate": (
                    {
                        "transcript": admission["transcript"],
                        "timeline": admission["timeline"],
                    }
                    if admission["status"] != "ready"
                    else None
                ),
                "rights": admission["rights"],
            }
        )
    
    def _generate_beats_from_brief(self, brief: EnhancedBrief) -> List[ScriptBeat]:
        """Generate script beats from brief content."""
        beats = []
        
        # Hook (0-2 seconds)
        if brief.hook:
            beats.append(ScriptBeat(
                id="seg_001",
                t="0-2",
                text=brief.hook,
                intent="hook",
                on_screen=[brief.hook.split()[0]] if brief.hook else [],
                visual_style="big_text_punch_in",
                emphasis_words=self._extract_emphasis_words(brief.hook)
            ))
        
        # Source-grounded context only.
        if brief.cluster and brief.cluster.what_changed:
            beats.append(ScriptBeat(
                id="seg_002",
                t="2-12",
                text=brief.cluster.what_changed,
                intent="problem",
                on_screen=["What changed"],
                visual_style="diagram",
                emphasis_words=self._extract_emphasis_words(brief.cluster.what_changed),
            ))
        
        if brief.cluster and brief.cluster.why_people_care:
            beats.append(ScriptBeat(
                id="seg_003",
                t="12-30",
                text=brief.cluster.why_people_care,
                intent="stakes",
                on_screen=["Why it matters"],
                visual_style="diagram",
                emphasis_words=self._extract_emphasis_words(brief.cluster.why_people_care),
            ))
        
        if brief.promise or (brief.angle and brief.angle.promise):
            promise = brief.promise or brief.angle.promise
            beats.append(ScriptBeat(
                id="seg_004",
                t="30-42",
                text=promise,
                intent="solution",
                on_screen=["Takeaway"],
                visual_style="diagram",
                emphasis_words=self._extract_emphasis_words(promise),
            ))

        if brief.unique_lens or (brief.angle and brief.angle.unique_lens):
            lens = brief.unique_lens or brief.angle.unique_lens
            beats.append(ScriptBeat(
                id="seg_005",
                t="30-42",
                text=lens,
                intent="method",
                on_screen=["Method"],
                visual_style="diagram",
                emphasis_words=self._extract_emphasis_words(lens),
            ))

        # Only use an explicitly supplied CTA.
        if brief.cta:
            cta_text = str(brief.cta.get("text") or "").strip()
            if cta_text:
                beats.append(ScriptBeat(
                    id="seg_006",
                    t="42-45",
                    text=cta_text,
                    intent="cta",
                    on_screen=[str(brief.cta.get("keyword") or "")],
                    visual_style="big_text",
                    emphasis_words=self._extract_emphasis_words(cta_text),
                ))
        
        return beats

    def _quality_components(self, segments: List[ScriptBeat]) -> dict:
        role_by_intent = {
            "hook": "hook",
            "problem": "stakes",
            "stakes": "stakes",
            "context": "context",
            "proof": "proof",
            "example": "proof",
            "solution": "claim",
            "claim": "claim",
            "method": "method",
            "takeaway": "payoff",
            "payoff": "payoff",
            "cta": "cta",
        }
        components = {}
        for beat in segments:
            role = role_by_intent.get(beat.intent, "context")
            components.setdefault(role, []).append({
                "node_id": beat.id,
                "source_beat": beat.intent,
                "text": beat.text,
                "on_screen": list(beat.on_screen),
                "emphasis_words": list(beat.emphasis_words),
            })
        return components

    def _segments_from_admission(self, admission: dict) -> List[ScriptBeat]:
        cues = admission["delivery_visual_plan"].get("cues", [])
        segments = []
        for index, item in enumerate(admission["timeline"]):
            start = float(item.get("start", item.get("start_seconds", 0.0)))
            end = float(item.get("end", item.get("end_seconds", start)))
            cue = cues[index] if index < len(cues) else {}
            visual = cue.get("visual", {}).get("mode", "owned_supporting_visual")
            segments.append(ScriptBeat(
                id=str(item.get("node_id") or f"seg_{index + 1:03d}"),
                t=f"{start:g}-{end:g}",
                text=str(item.get("text") or ""),
                intent=str(item.get("quality_role") or "context"),
                on_screen=list(item.get("on_screen") or []),
                visual_style=visual,
                emphasis_words=list(item.get("emphasis_words") or []),
            ))
        return segments

    def _preferred_structure(self, brief: EnhancedBrief) -> str | None:
        if not brief.angle:
            return None
        return {
            "myth_bust": "myth_turn",
            "tutorial": "proof_bridge",
            "checklist": "proof_bridge",
            "story": "stakes_then_method",
            "case_study": "stakes_then_method",
            "teardown": "contrast_reveal",
        }.get(brief.angle.format)
    
    def _calculate_duration(self, segments: List[ScriptBeat]) -> float:
        """Calculate total duration from segments."""
        max_end = 0.0
        for segment in segments:
            # Parse time range (e.g., "0-2" -> 2.0)
            if "-" in segment.t:
                try:
                    end_time = float(segment.t.split("-")[1])
                    max_end = max(max_end, end_time)
                except ValueError:
                    pass
        return max_end
    
    def _extract_emphasis_words(self, text: str) -> List[str]:
        """Extract words to emphasize from text."""
        # Simple: extract important words (not common words)
        common_words = {"the", "a", "an", "and", "or", "but", "in", "on", "at", "to", "for", "of", "with", "by"}
        words = text.lower().split()
        important = [w for w in words if w not in common_words and len(w) > 3]
        return important[:5]  # Top 5 important words
