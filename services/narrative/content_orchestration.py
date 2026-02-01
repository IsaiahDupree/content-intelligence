"""
Content Orchestration Integration

Bridges the Narrative Scheduler with Video Orchestrator
to enable automated content creation from narrative goals.

Flow:
1. Narrative Goal → Content Brief
2. Content Brief → Video Script (via AI)
3. Video Script → ClipPlan (via Director)
4. ClipPlan → Provider Payloads (via SceneCrafter)
5. Generated Content → Scheduled Posts
"""

import os
import json
import logging
from typing import List, Dict, Optional, Any
from datetime import datetime, date
from dataclasses import dataclass, field
from uuid import uuid4

from .models import NarrativeGoal, NarrativePillar, WeeklyPlan, ScheduledSlot

logger = logging.getLogger(__name__)


@dataclass
class ContentBriefFromNarrative:
    """A content brief generated from narrative goals."""
    id: str = field(default_factory=lambda: str(uuid4()))
    narrative_goal_id: str = ""
    pillar: str = ""
    
    # Brief content
    topic: str = ""
    hook: str = ""
    key_points: List[str] = field(default_factory=list)
    call_to_action: str = ""
    target_duration_seconds: int = 30
    
    # Platform targeting
    target_platforms: List[str] = field(default_factory=list)
    
    # Style hints
    tone: str = "engaging"
    visual_style: str = "dynamic"
    
    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "narrative_goal_id": self.narrative_goal_id,
            "pillar": self.pillar,
            "topic": self.topic,
            "hook": self.hook,
            "key_points": self.key_points,
            "call_to_action": self.call_to_action,
            "target_duration_seconds": self.target_duration_seconds,
            "target_platforms": self.target_platforms,
            "tone": self.tone,
            "visual_style": self.visual_style,
        }


class NarrativeContentOrchestrator:
    """
    Orchestrates content creation based on narrative goals.
    
    Integrates:
    - Narrative Scheduler (goals, pillars, scheduling)
    - Video Orchestrator (clip plans, scene crafting)
    - AI Providers (script generation)
    """
    
    def __init__(self, openai_api_key: Optional[str] = None):
        self.openai_api_key = openai_api_key or os.getenv("OPENAI_API_KEY")
    
    async def generate_content_briefs_from_goal(
        self,
        goal: NarrativeGoal,
        pillars: List[NarrativePillar],
        count: int = 7
    ) -> List[ContentBriefFromNarrative]:
        """
        Generate content briefs from a narrative goal.
        
        Creates briefs distributed across pillars to match target percentages.
        """
        briefs = []
        
        # Calculate briefs per pillar based on target percentages
        pillar_counts = {}
        for pillar in pillars:
            target_count = max(1, int((pillar.target_percentage / 100) * count))
            pillar_counts[pillar.name] = target_count
        
        # Generate briefs for each pillar
        for pillar in pillars:
            pillar_brief_count = pillar_counts.get(pillar.name, 1)
            
            for i in range(pillar_brief_count):
                brief = await self._generate_brief_for_pillar(
                    goal=goal,
                    pillar=pillar,
                    index=i
                )
                briefs.append(brief)
                
                if len(briefs) >= count:
                    break
            
            if len(briefs) >= count:
                break
        
        logger.info(f"[ContentOrchestrator] Generated {len(briefs)} briefs from goal")
        return briefs[:count]
    
    async def _generate_brief_for_pillar(
        self,
        goal: NarrativeGoal,
        pillar: NarrativePillar,
        index: int = 0
    ) -> ContentBriefFromNarrative:
        """Generate a single content brief for a pillar using AI."""
        
        # Try AI-powered generation first
        if self.openai_api_key:
            try:
                return await self._generate_brief_with_ai(goal, pillar, index)
            except Exception as e:
                logger.warning(f"[ContentOrchestrator] AI brief generation failed: {e}")
        
        # Fallback to templates
        hook, topic, key_points = self._get_pillar_content_template(
            pillar_name=pillar.name,
            pillar_type=pillar.pillar_type,
            goal_statement=goal.goal_statement,
            target_audience=goal.target_audience,
            index=index
        )
        
        cta = self._get_cta_for_goal(goal.primary_cta)
        
        brief = ContentBriefFromNarrative(
            narrative_goal_id=goal.id,
            pillar=pillar.name,
            topic=topic,
            hook=hook,
            key_points=key_points,
            call_to_action=cta,
            target_duration_seconds=30,
            target_platforms=["tiktok", "instagram"],
            tone=self._get_tone_for_pillar(pillar.pillar_type),
            visual_style="dynamic"
        )
        
        return brief
    
    async def _generate_brief_with_ai(
        self,
        goal: NarrativeGoal,
        pillar: NarrativePillar,
        index: int = 0
    ) -> ContentBriefFromNarrative:
        """Use real OpenAI to generate personalized content briefs."""
        from openai import OpenAI
        client = OpenAI(api_key=self.openai_api_key)
        
        prompt = f"""You are a viral content strategist creating a content brief for short-form video.

NARRATIVE GOAL: {goal.goal_statement}
TARGET AUDIENCE: {goal.target_audience}
PRIMARY CTA: {goal.primary_cta}
CONTENT PILLAR: {pillar.name} ({pillar.pillar_type})
PILLAR DESCRIPTION: {pillar.description}

Create a unique, engaging content brief for a 30-second video. Be specific and creative.

Respond in JSON:
{{
    "hook": "An attention-grabbing opening line (first 3 seconds)",
    "topic": "Specific topic/angle for this video",
    "key_points": ["point 1", "point 2", "point 3"],
    "call_to_action": "Specific CTA aligned with the goal",
    "tone": "emotional tone (e.g., energetic, inspirational, educational)",
    "visual_style": "visual approach suggestion",
    "target_duration": 30
}}

Make the hook scroll-stopping and the content valuable for the target audience."""

        response = client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[
                {"role": "system", "content": "You are a viral content expert. Create briefs that drive engagement."},
                {"role": "user", "content": prompt}
            ],
            temperature=0.7,  # Higher creativity for content
            response_format={"type": "json_object"}
        )
        
        result = json.loads(response.choices[0].message.content)
        
        brief = ContentBriefFromNarrative(
            narrative_goal_id=goal.id,
            pillar=pillar.name,
            topic=result.get("topic", "Content topic"),
            hook=result.get("hook", "Check this out..."),
            key_points=result.get("key_points", ["Deliver value"]),
            call_to_action=result.get("call_to_action", self._get_cta_for_goal(goal.primary_cta)),
            target_duration_seconds=result.get("target_duration", 30),
            target_platforms=["tiktok", "instagram"],
            tone=result.get("tone", "engaging"),
            visual_style=result.get("visual_style", "dynamic")
        )
        
        logger.info(f"[AI Brief] Generated brief for {pillar.name}: {brief.hook[:50]}...")
        return brief
    
    def _get_pillar_content_template(
        self,
        pillar_name: str,
        pillar_type: str,
        goal_statement: str,
        target_audience: str,
        index: int
    ) -> tuple[str, str, List[str]]:
        """Get content template for a pillar type."""
        
        templates = {
            "Process/How-To": {
                "hooks": [
                    "Here's how to do this in 30 seconds...",
                    "The easiest way to get this done...",
                    "Stop doing it the hard way...",
                    "This trick will save you hours...",
                ],
                "topics": [
                    "Quick tutorial on essential technique",
                    "Step-by-step breakdown",
                    "Pro tip demonstration",
                    "Efficiency hack walkthrough",
                ],
                "key_points": ["Show the problem", "Reveal the solution", "Demonstrate results"]
            },
            "Pain Points": {
                "hooks": [
                    "Are you still struggling with this?",
                    "This is why you're stuck...",
                    "The biggest mistake people make...",
                    "If this sounds familiar...",
                ],
                "topics": [
                    "Common problem and solution",
                    "Why most people fail at this",
                    "The hidden obstacle holding you back",
                ],
                "key_points": ["Identify the pain", "Show empathy", "Hint at solution"]
            },
            "Social Proof": {
                "hooks": [
                    "Here's what happened when...",
                    "The results speak for themselves...",
                    "From struggling to succeeding...",
                ],
                "topics": [
                    "Success story showcase",
                    "Before and after transformation",
                    "Real results demonstration",
                ],
                "key_points": ["Show the before", "Reveal the after", "Share the method"]
            },
            "Personality": {
                "hooks": [
                    "POV: A day in my life...",
                    "Something people don't know about me...",
                    "Behind the scenes of...",
                ],
                "topics": [
                    "Authentic moment share",
                    "Behind the scenes look",
                    "Personal story time",
                ],
                "key_points": ["Be authentic", "Share vulnerability", "Connect emotionally"]
            },
            "Promotion/CTA": {
                "hooks": [
                    "If you've been waiting for a sign...",
                    "This is your chance to...",
                    "Don't miss out on...",
                ],
                "topics": [
                    "Direct offer presentation",
                    "Limited opportunity announcement",
                    "Call to action focused",
                ],
                "key_points": ["Create urgency", "Show value", "Clear next step"]
            },
        }
        
        # Default template
        default = {
            "hooks": ["Check this out..."],
            "topics": ["Valuable content share"],
            "key_points": ["Deliver value", "Engage audience", "Call to action"]
        }
        
        template = templates.get(pillar_name, default)
        
        hook = template["hooks"][index % len(template["hooks"])]
        topic = template["topics"][index % len(template["topics"])]
        key_points = template["key_points"]
        
        return hook, topic, key_points
    
    def _get_cta_for_goal(self, primary_cta: str) -> str:
        """Get call-to-action text based on goal CTA type."""
        cta_templates = {
            "follow": "Follow for more tips like this!",
            "subscribe": "Subscribe to stay updated!",
            "waitlist": "Join the waitlist - link in bio!",
            "purchase": "Get yours now - link in bio!",
            "dm_keyword": "DM me 'INFO' to learn more!",
            "link_click": "Click the link in bio!",
            "save": "Save this for later!",
            "share": "Share this with someone who needs it!",
        }
        return cta_templates.get(primary_cta, "Follow for more!")
    
    def _get_tone_for_pillar(self, pillar_type: str) -> str:
        """Get tone based on pillar type."""
        tones = {
            "value": "educational and engaging",
            "proof": "confident and inspiring",
            "cta": "urgent and compelling",
        }
        return tones.get(pillar_type, "engaging")
    
    async def convert_brief_to_script(
        self,
        brief: ContentBriefFromNarrative
    ) -> str:
        """Convert a content brief to a video script using AI."""
        
        if not self.openai_api_key:
            # Fallback: generate basic script from brief
            return self._generate_basic_script(brief)
        
        try:
            import openai
            client = openai.OpenAI(api_key=self.openai_api_key)
            
            prompt = f"""Write a short-form video script (30 seconds, ~75 words) based on this brief:

Topic: {brief.topic}
Hook: {brief.hook}
Key Points: {', '.join(brief.key_points)}
Call to Action: {brief.call_to_action}
Tone: {brief.tone}
Pillar: {brief.pillar}

Write a natural, conversational script that:
1. Opens with an attention-grabbing hook
2. Delivers value quickly
3. Ends with a clear call-to-action

Format: Just the script text, no labels or timestamps."""

            response = client.chat.completions.create(
                model="gpt-4o-mini",
                messages=[
                    {"role": "system", "content": "You write engaging short-form video scripts for social media."},
                    {"role": "user", "content": prompt}
                ],
                temperature=0.7,
                max_tokens=200
            )
            
            return response.choices[0].message.content.strip()
            
        except Exception as e:
            logger.error(f"AI script generation failed: {e}")
            return self._generate_basic_script(brief)
    
    def _generate_basic_script(self, brief: ContentBriefFromNarrative) -> str:
        """Generate a basic script from brief without AI."""
        lines = [
            brief.hook,
            "",
            f"Today I want to share something about {brief.topic.lower()}.",
            "",
        ]
        
        for i, point in enumerate(brief.key_points[:3], 1):
            lines.append(f"{point}.")
        
        lines.extend([
            "",
            brief.call_to_action
        ])
        
        return "\n".join(lines)
    
    async def create_clip_plan_from_brief(
        self,
        brief: ContentBriefFromNarrative,
        script: str
    ) -> Dict[str, Any]:
        """Create a ClipPlan using the Director service."""
        try:
            from services.video_orchestrator.director import DirectorService
            
            director = DirectorService()
            
            # Create clip plan from script
            clip_plan = director.create_plan_from_script(
                script_text=script,
                plan_name=f"Narrative: {brief.topic[:30]}",
                platform_hint=brief.target_platforms[0] if brief.target_platforms else "tiktok"
            )
            
            return clip_plan.to_dict() if hasattr(clip_plan, 'to_dict') else {"status": "created"}
            
        except ImportError:
            logger.warning("Director service not available")
            return {"status": "director_unavailable", "script": script}
        except Exception as e:
            logger.error(f"Clip plan creation failed: {e}")
            return {"status": "error", "error": str(e)}
    
    async def orchestrate_content_for_week(
        self,
        goal: NarrativeGoal,
        pillars: List[NarrativePillar],
        plan: WeeklyPlan
    ) -> Dict[str, Any]:
        """
        Full orchestration: generate briefs, scripts, and clip plans
        for a weekly schedule.
        """
        results = {
            "briefs_generated": 0,
            "scripts_generated": 0,
            "clip_plans_created": 0,
            "content": []
        }
        
        # Generate briefs for the week
        briefs = await self.generate_content_briefs_from_goal(
            goal=goal,
            pillars=pillars,
            count=plan.total_posts
        )
        results["briefs_generated"] = len(briefs)
        
        # Convert each brief to script and clip plan
        for brief in briefs:
            script = await self.convert_brief_to_script(brief)
            results["scripts_generated"] += 1
            
            clip_plan = await self.create_clip_plan_from_brief(brief, script)
            if clip_plan.get("status") != "error":
                results["clip_plans_created"] += 1
            
            results["content"].append({
                "brief": brief.to_dict(),
                "script": script,
                "clip_plan": clip_plan
            })
        
        logger.info(f"[ContentOrchestrator] Week orchestration complete: {results['briefs_generated']} briefs, {results['scripts_generated']} scripts")
        
        return results
