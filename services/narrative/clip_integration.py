"""
Clip Integration for Narrative Scheduler
=========================================
Connects extracted clips to the narrative scheduling system
for automated posting based on narrative goals and pillars.
"""

import os
import logging
from typing import List, Dict, Optional, Any
from datetime import datetime, date, timedelta
from dataclasses import dataclass, field
from uuid import uuid4

from sqlalchemy import create_engine, text

from .models import (
    NarrativeGoal,
    NarrativePillar,
    VideoCandidate,
    ScheduledSlot,
    WeeklyPlan
)
from .ai_classifier import AIContentClassifier

logger = logging.getLogger(__name__)

DATABASE_URL = os.getenv("DATABASE_URL", "postgresql://postgres:postgres@127.0.0.1:54322/postgres")


@dataclass
class ClipCandidate:
    """A clip ready for scheduling consideration."""
    id: str
    source_video_id: str
    clip_path: str
    start_time: float
    end_time: float
    duration: float
    text: str
    relevance_score: float
    reasoning: str
    
    # Classification
    pillar: Optional[str] = None
    pillar_confidence: float = 0.0
    topics: List[str] = field(default_factory=list)
    hooks: List[str] = field(default_factory=list)
    
    # Scheduling metadata
    suggested_platforms: List[str] = field(default_factory=list)
    suggested_time_slots: List[str] = field(default_factory=list)
    
    def to_video_candidate(self) -> VideoCandidate:
        """Convert to VideoCandidate for scheduling."""
        return VideoCandidate(
            id=self.id,
            title=self.text[:50] + "..." if len(self.text) > 50 else self.text,
            file_path=self.clip_path,
            duration_sec=self.duration,
            pre_social_score=int(self.relevance_score * 100),
            transcript=self.text,
            topics=self.topics,
            hooks=self.hooks
        )
    
    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "source_video_id": self.source_video_id,
            "clip_path": self.clip_path,
            "start_time": self.start_time,
            "end_time": self.end_time,
            "duration": self.duration,
            "text": self.text,
            "relevance_score": self.relevance_score,
            "pillar": self.pillar,
            "pillar_confidence": self.pillar_confidence,
            "topics": self.topics,
            "suggested_platforms": self.suggested_platforms
        }


class ClipSchedulingIntegration:
    """
    Integrates extracted clips with the narrative scheduler.
    
    Pipeline:
    1. Load extracted clips from database
    2. Classify clips into narrative pillars
    3. Score and rank clips for scheduling
    4. Generate schedule slots for clips
    5. Add to weekly plan
    """
    
    def __init__(self):
        self.engine = create_engine(DATABASE_URL)
        self.classifier = AIContentClassifier()
    
    async def load_unscheduled_clips(
        self,
        min_relevance: float = 0.5,
        limit: int = 50
    ) -> List[ClipCandidate]:
        """Load extracted clips that haven't been scheduled yet."""
        clips = []
        
        with self.engine.connect() as conn:
            result = conn.execute(text("""
                SELECT vc.id, vc.video_id, vc.rendered_url, 
                       vc.start_time, vc.end_time, vc.ai_reasoning
                FROM video_clips vc
                WHERE vc.clip_type = 'extracted'
                AND NOT EXISTS (
                    SELECT 1 FROM scheduled_posts sp 
                    WHERE sp.clip_id = vc.id
                )
                ORDER BY vc.ai_score DESC NULLS LAST
                LIMIT :limit
            """), {"limit": limit})
            
            for row in result:
                ai_reasoning = row[5] or ""
                # Use a default relevance score since we're filtering by ai_score
                relevance = 0.7
                
                if relevance >= min_relevance:
                    start_time = float(row[3]) if row[3] else 0
                    end_time = float(row[4]) if row[4] else 30
                    clip = ClipCandidate(
                        id=str(row[0]),
                        source_video_id=str(row[1]) if row[1] else "",
                        clip_path=row[2] or "",
                        start_time=start_time,
                        end_time=end_time,
                        duration=end_time - start_time if end_time > start_time else 30,
                        text=ai_reasoning[:200] if ai_reasoning else "",
                        relevance_score=relevance,
                        reasoning=ai_reasoning
                    )
                    clips.append(clip)
        
        logger.info(f"[ClipIntegration] Loaded {len(clips)} unscheduled clips")
        return clips
    
    async def classify_clips_into_pillars(
        self,
        clips: List[ClipCandidate],
        pillars: List[NarrativePillar]
    ) -> List[ClipCandidate]:
        """Classify each clip into narrative pillars."""
        pillar_dicts = [p.to_dict() for p in pillars]
        
        for clip in clips:
            result = await self.classifier.classify_video(
                video_title=clip.text[:100],
                transcript=clip.text,
                topics=clip.topics,
                hooks=clip.hooks,
                pillars=pillar_dicts
            )
            
            clip.pillar = result.primary_pillar
            clip.pillar_confidence = result.confidence
            clip.topics = result.topics_detected
            clip.hooks = result.suggested_hooks
        
        logger.info(f"[ClipIntegration] Classified {len(clips)} clips into pillars")
        return clips
    
    async def rank_clips_for_goal(
        self,
        clips: List[ClipCandidate],
        goal: NarrativeGoal,
        pillars: List[NarrativePillar]
    ) -> List[ClipCandidate]:
        """
        Rank clips based on goal alignment and pillar targets.
        
        Scoring factors:
        - Relevance score from extraction
        - Pillar alignment with target distribution
        - CTA alignment with goal
        - Hooks and topics match
        """
        # Build pillar target map
        pillar_targets = {p.name: p.target_percentage for p in pillars}
        
        for clip in clips:
            score = clip.relevance_score * 0.5  # Base relevance
            
            # Pillar bonus
            if clip.pillar in pillar_targets:
                target_pct = pillar_targets[clip.pillar]
                score += (target_pct / 100) * 0.3
            
            # Hook bonus
            if clip.hooks:
                score += 0.1
            
            # Pillar confidence
            score += clip.pillar_confidence * 0.1
            
            # Update score
            clip.relevance_score = min(score, 1.0)
        
        # Sort by score
        clips.sort(key=lambda c: c.relevance_score, reverse=True)
        
        return clips
    
    async def generate_schedule_from_clips(
        self,
        clips: List[ClipCandidate],
        goal: NarrativeGoal,
        pillars: List[NarrativePillar],
        posts_per_day: int = 2,
        days: int = 7,
        platforms: List[str] = None
    ) -> List[ScheduledSlot]:
        """Generate schedule slots from ranked clips."""
        platforms = platforms or ["tiktok", "instagram"]
        slots = []
        
        today = date.today()
        
        # Distribute clips across days
        total_slots = posts_per_day * days
        selected_clips = clips[:total_slots]
        
        clip_index = 0
        for day_offset in range(days):
            schedule_date = today + timedelta(days=day_offset)
            
            for slot_num in range(posts_per_day):
                if clip_index >= len(selected_clips):
                    break
                
                clip = selected_clips[clip_index]
                
                # Determine time slot
                time_slots = ["09:00", "12:00", "15:00", "18:00", "20:00"]
                scheduled_time = time_slots[slot_num % len(time_slots)]
                
                # Determine platform (alternate)
                platform = platforms[slot_num % len(platforms)]
                
                slot = ScheduledSlot(
                    video_id=clip.id,
                    video_title=clip.text[:50],
                    platform=platform,
                    scheduled_date=schedule_date,
                    scheduled_time=scheduled_time,
                    pillar=clip.pillar or "Uncategorized",
                    selection_reason=f"Clip extracted with {clip.relevance_score:.0%} relevance. {clip.reasoning}"
                )
                
                slots.append(slot)
                clip_index += 1
        
        logger.info(f"[ClipIntegration] Generated {len(slots)} schedule slots from clips")
        return slots
    
    async def add_clips_to_weekly_plan(
        self,
        plan_id: str,
        clips: List[ClipCandidate]
    ) -> Dict[str, Any]:
        """Add classified clips to an existing weekly plan."""
        with self.engine.connect() as conn:
            added = 0
            
            for clip in clips:
                # Get plan's week dates
                plan_result = conn.execute(text("""
                    SELECT week_start, week_end FROM weekly_schedules WHERE id = :id
                """), {"id": plan_id}).fetchone()
                
                if not plan_result:
                    return {"error": "Plan not found", "plan_id": plan_id}
                
                # Add slot
                conn.execute(text("""
                    INSERT INTO schedule_slots (id, schedule_id, video_id, video_title,
                        platform, scheduled_date, scheduled_time, pillar, selection_reason)
                    VALUES (:id, :plan_id, :video_id, :title, :platform, :date, :time, :pillar, :reason)
                """), {
                    "id": str(uuid4()),
                    "plan_id": plan_id,
                    "video_id": clip.id,
                    "title": clip.text[:50],
                    "platform": "tiktok",
                    "date": plan_result[0],
                    "time": "12:00",
                    "pillar": clip.pillar or "Uncategorized",
                    "reason": f"Extracted clip: {clip.reasoning}"
                })
                added += 1
            
            conn.commit()
        
        return {"added": added, "plan_id": plan_id}
    
    async def auto_schedule_clips(
        self,
        goal_id: Optional[str] = None
    ) -> Dict[str, Any]:
        """
        Fully automated clip scheduling:
        1. Load unscheduled clips
        2. Load goal and pillars
        3. Classify and rank
        4. Generate schedule
        5. Save to database
        """
        from .scheduler import NarrativeScheduler
        
        scheduler = NarrativeScheduler()
        
        # Load goal and pillars
        goal = await scheduler._load_goal(goal_id)
        if not goal:
            goal = scheduler._get_default_goal()
        
        pillars = await scheduler._load_pillars(goal.id) if goal_id else []
        if not pillars:
            pillars = scheduler._get_default_pillars()
        
        # Load clips
        clips = await self.load_unscheduled_clips(min_relevance=0.5, limit=20)
        
        if not clips:
            return {"success": False, "reason": "No unscheduled clips found"}
        
        # Classify
        clips = await self.classify_clips_into_pillars(clips, pillars)
        
        # Rank
        clips = await self.rank_clips_for_goal(clips, goal, pillars)
        
        # Generate schedule
        slots = await self.generate_schedule_from_clips(
            clips=clips,
            goal=goal,
            pillars=pillars,
            posts_per_day=2,
            days=7
        )
        
        # Save slots to scheduled_posts
        with self.engine.connect() as conn:
            scheduled = 0
            for slot in slots:
                try:
                    conn.execute(text("""
                        INSERT INTO scheduled_posts (id, clip_id, title, caption, platform,
                            scheduled_at, status, origin)
                        VALUES (:id, :clip_id, :title, :caption, :platform, :scheduled_at, 'pending', 'NARRATIVE_CLIP')
                    """), {
                        "id": str(uuid4()),
                        "clip_id": slot.video_id,
                        "title": slot.video_title,
                        "caption": slot.selection_reason,
                        "platform": slot.platform,
                        "scheduled_at": datetime.combine(slot.scheduled_date, datetime.strptime(slot.scheduled_time, "%H:%M").time())
                    })
                    scheduled += 1
                except Exception as e:
                    logger.warning(f"Failed to schedule clip: {e}")
            
            conn.commit()
        
        return {
            "success": True,
            "clips_loaded": len(clips),
            "clips_scheduled": scheduled,
            "goal": goal.goal_statement,
            "pillars_used": [p.name for p in pillars]
        }
