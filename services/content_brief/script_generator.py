"""
Script Generator
================
Generates script.json from content briefs.
"""

import logging
from typing import Dict, Any, Optional, List
from uuid import uuid4

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
        
        # Calculate metadata
        total_duration = self._calculate_duration(segments)
        word_count = sum(len(beat.text.split()) for beat in segments)
        
        # Estimate TTS duration (rough: 150 words per minute)
        estimated_tts_duration = (word_count / 150) * 60
        
        return ScriptOutput(
            brief_id=brief.brief_id,
            title=brief.title or brief.angle.promise if brief.angle else "Untitled",
            hook=brief.hook or segments[0].text if segments else "",
            segments=segments,
            metadata={
                "total_duration_sec": total_duration,
                "word_count": word_count,
                "estimated_tts_duration": estimated_tts_duration,
                "format": brief.format,
                "platform": brief.platform
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
        
        # Problem (2-12 seconds)
        if brief.angle and brief.angle.unique_lens:
            problem_text = f"Most people approach {brief.cluster.name if brief.cluster else 'this'} without considering {brief.angle.stakes}."
            beats.append(ScriptBeat(
                id="seg_002",
                t="2-12",
                text=problem_text,
                intent="problem",
                on_screen=["Problem", brief.angle.stakes],
                visual_style="diagram",
                emphasis_words=["most", "without", brief.angle.stakes]
            ))
        
        # Solution/Framework (12-30 seconds)
        if brief.angle:
            solution_text = brief.angle.promise or f"Here's a {brief.angle.format} approach that works."
            beats.append(ScriptBeat(
                id="seg_003",
                t="12-30",
                text=solution_text,
                intent="solution",
                on_screen=[brief.angle.format.upper(), "FRAMEWORK"],
                visual_style="diagram",
                emphasis_words=["here's", "approach", "works"]
            ))
        
        # Example/Proof (30-42 seconds)
        if brief.cluster:
            example_text = f"This {brief.cluster.name} trend scores high because comments ask for tools—that's buyer intent."
            beats.append(ScriptBeat(
                id="seg_004",
                t="30-42",
                text=example_text,
                intent="example",
                on_screen=["Example", "Buyer Intent"],
                visual_style="diagram",
                emphasis_words=["scores", "high", "buyer", "intent"]
            ))
        
        # CTA (42-45 seconds)
        if brief.cta:
            cta_text = brief.cta.get("text", "Comment BRIEF and I'll drop the template.")
            beats.append(ScriptBeat(
                id="seg_005",
                t="42-45",
                text=cta_text,
                intent="cta",
                on_screen=[brief.cta.get("keyword", "BRIEF")],
                visual_style="big_text",
                emphasis_words=[brief.cta.get("keyword", "BRIEF")]
            ))
        
        return beats
    
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

