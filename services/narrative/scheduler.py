"""
Narrative Scheduler Service

Orchestrates the AI reasoning engine with database operations
to create, execute, and track content schedules.
"""

import os
import json
import logging
from typing import List, Dict, Optional, Any
from datetime import datetime, date, timedelta
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

from .models import (
    NarrativeGoal,
    NarrativePillar,
    SchedulingConstraints,
    VideoCandidate,
    WeeklyPlan,
    PerformanceMetrics,
    Learning,
)
from .reasoning_engine import NarrativeReasoningEngine

logger = logging.getLogger(__name__)

DATABASE_URL = os.getenv("DATABASE_URL", "postgresql://postgres:postgres@127.0.0.1:54322/postgres")


class NarrativeScheduler:
    """
    Main service for narrative-based content scheduling.
    
    Handles:
    - Loading goals, pillars, constraints from database
    - Fetching available videos with analysis
    - Orchestrating AI reasoning
    - Saving schedules and tracking performance
    """
    
    def __init__(self):
        self.engine = create_engine(DATABASE_URL)
        self.reasoning_engine = NarrativeReasoningEngine()
    
    async def generate_7_day_plan(
        self,
        goal_id: Optional[str] = None,
        use_defaults: bool = True
    ) -> WeeklyPlan:
        """
        Generate a complete 7-day content plan.
        
        Args:
            goal_id: Specific goal ID to use, or None for active goal
            use_defaults: Use default pillars/constraints if not configured
        
        Returns:
            WeeklyPlan with full reasoning chain
        """
        logger.info("[NarrativeScheduler] Starting 7-day plan generation...")
        
        # Load goal
        goal = await self._load_goal(goal_id)
        if not goal:
            if use_defaults:
                goal = self._get_default_goal()
            else:
                raise ValueError("No active narrative goal found")
        
        # Load pillars
        pillars = await self._load_pillars(goal.id)
        if not pillars and use_defaults:
            pillars = self._get_default_pillars()
        
        # Load constraints
        constraints = await self._load_constraints(goal.id)
        if not constraints:
            constraints = self._get_default_constraints()
        
        # Load available videos with analysis
        videos = await self._load_analyzed_videos(constraints)
        
        logger.info(f"[NarrativeScheduler] Loaded: goal='{goal.goal_statement[:30]}...', {len(pillars)} pillars, {len(videos)} videos")
        
        # Load previous performance (if exists)
        previous_performance = await self._load_previous_performance(goal.id)
        
        # Load accumulated learnings
        learnings = await self._load_learnings(goal.id)
        
        # Generate plan with AI reasoning
        plan = await self.reasoning_engine.generate_weekly_plan(
            goal=goal,
            pillars=pillars,
            constraints=constraints,
            available_videos=videos,
            previous_performance=previous_performance,
            learnings=learnings
        )
        
        # Save plan to database
        await self._save_plan(plan)
        
        logger.info(f"[NarrativeScheduler] Plan generated: {plan.total_posts} posts, status={plan.status}")
        
        return plan
    
    async def _load_goal(self, goal_id: Optional[str] = None) -> Optional[NarrativeGoal]:
        """Load narrative goal from database"""
        with self.engine.connect() as conn:
            if goal_id:
                result = conn.execute(text("""
                    SELECT id, goal_statement, primary_cta, target_audience, 
                           time_horizon
                    FROM narrative_goals WHERE id = :id
                """), {"id": goal_id})
            else:
                result = conn.execute(text("""
                    SELECT id, goal_statement, primary_cta, target_audience,
                           time_horizon
                    FROM narrative_goals WHERE status = 'active' ORDER BY created_at DESC LIMIT 1
                """))
            
            row = result.fetchone()
            
            if row:
                return NarrativeGoal(
                    id=str(row[0]),
                    goal_statement=row[1] or "",
                    primary_cta=row[2] or "follow",
                    target_audience=row[3] or "",
                    time_horizon=row[4] or "next_7_days",
                    target_followers=100,
                    target_engagement_rate=4.0,
                    target_conversions=None
                )
        
        return None
    
    async def _load_pillars(self, goal_id: str) -> List[NarrativePillar]:
        """Load narrative pillars for a goal"""
        pillars = []
        
        with self.engine.connect() as conn:
            result = conn.execute(text("""
                SELECT id, name, description, pillar_type, color, keywords,
                       target_percentage, min_posts_per_week, max_posts_per_week, is_active
                FROM narrative_pillars WHERE goal_id = :goal_id AND is_active = TRUE
                ORDER BY created_at DESC
            """), {"goal_id": goal_id})
            
            for row in result:
                pillars.append(NarrativePillar(
                    id=str(row[0]),
                    name=row[1],
                    description=row[2] or "",
                    pillar_type=row[3] or "value",
                    color=row[4] or "#3b82f6",
                    keywords=list(row[5]) if row[5] else [],
                    target_percentage=float(row[6]) if row[6] else 20.0,
                    min_posts_per_week=int(row[7]) if row[7] else 1,
                    max_posts_per_week=int(row[8]) if row[8] else 5,
                    priority=5,  # Default priority since column doesn't exist
                    is_active=bool(row[9]) if row[9] is not None else True
                ))
        
        return pillars
    
    async def _load_constraints(self, goal_id: str) -> Optional[SchedulingConstraints]:
        """Load scheduling constraints for a goal"""
        with self.engine.connect() as conn:
            result = conn.execute(text("""
                SELECT id, enabled_platforms, max_posts_per_day, min_posts_per_day,
                       posting_windows, timezone, min_pre_social_score, max_same_pillar_consecutive
                FROM scheduling_constraints WHERE goal_id = :goal_id LIMIT 1
            """), {"goal_id": goal_id})
            
            row = result.fetchone()
            
            if row:
                return SchedulingConstraints(
                    id=str(row[0]),
                    enabled_platforms=row[1] or ["tiktok", "instagram"],
                    max_posts_per_day=row[2] or 3,
                    min_posts_per_day=row[3] or 1,
                    posting_windows=row[4] or {},
                    timezone=row[5] or "America/New_York",
                    min_pre_social_score=row[6] or 60,
                    max_same_pillar_consecutive=row[7] or 2
                )
        
        return None
    
    async def _load_analyzed_videos(
        self, 
        constraints: SchedulingConstraints
    ) -> List[VideoCandidate]:
        """Load videos that have been analyzed, approved, and not already scheduled/posted"""
        videos = []
        
        with self.engine.connect() as conn:
            # Load APPROVED videos with score threshold, excluding already scheduled/posted
            result = conn.execute(text("""
                SELECT v.id, v.file_name, v.source_uri, v.thumbnail_path, v.duration_sec,
                       va.pre_social_score, va.transcript, va.topics, va.hooks, va.tone
                FROM videos v
                JOIN video_analysis va ON va.video_id = v.id
                WHERE va.pre_social_score >= :min_score
                  AND va.curation_status = 'approved'
                  AND NOT EXISTS (
                      SELECT 1 FROM scheduled_posts sp 
                      WHERE (sp.content_id = v.id::text OR sp.clip_id = v.id)
                        AND sp.status IN ('scheduled', 'publishing', 'posted', 'published')
                  )
                ORDER BY va.pre_social_score DESC
                LIMIT 500
            """), {"min_score": constraints.min_pre_social_score})
            
            rows = list(result)
            
            # If no approved videos meet threshold, get approved videos with lower scores
            if not rows:
                logger.info(f"[NarrativeScheduler] No approved videos with score >= {constraints.min_pre_social_score}, loading all approved videos")
                result = conn.execute(text("""
                    SELECT v.id, v.file_name, v.source_uri, v.thumbnail_path, v.duration_sec,
                           COALESCE(va.pre_social_score, 50) as pre_social_score, 
                           va.transcript, va.topics, va.hooks, va.tone
                    FROM videos v
                    JOIN video_analysis va ON va.video_id = v.id
                    WHERE va.curation_status = 'approved'
                      AND NOT EXISTS (
                          SELECT 1 FROM scheduled_posts sp 
                          WHERE (sp.content_id = v.id::text OR sp.clip_id = v.id)
                            AND sp.status IN ('scheduled', 'publishing', 'posted', 'published')
                      )
                    ORDER BY va.pre_social_score DESC NULLS LAST
                    LIMIT 100
                """))
                rows = list(result)
            
            for row in rows:
                # Convert Decimal to int/float for compatibility
                score = int(row[5]) if row[5] else None
                duration = float(row[4]) if row[4] else None
                
                videos.append(VideoCandidate(
                    id=str(row[0]),
                    title=row[1] or "Untitled",
                    file_path=row[2] or "",
                    thumbnail_path=row[3],
                    duration_sec=duration,
                    pre_social_score=score,
                    transcript=row[6],
                    topics=row[7] if isinstance(row[7], list) else [],
                    hooks=row[8] if isinstance(row[8], list) else [],
                    tone=row[9]
                ))
        
        return videos
    
    async def _load_previous_performance(self, goal_id: str) -> Optional[PerformanceMetrics]:
        """Load last week's performance metrics"""
        # For now, return None - will be implemented when we have performance tracking
        return None
    
    async def _load_learnings(self, goal_id: str) -> List[Learning]:
        """Load accumulated learnings for a goal"""
        # For now, return empty - will be implemented with learning system
        return []
    
    async def _save_plan(self, plan: WeeklyPlan):
        """Save weekly plan to database"""
        with self.engine.connect() as conn:
            # Check if goal_id exists in narrative_goals (skip if using defaults)
            goal_id_to_save = None
            if plan.goal_id:
                result = conn.execute(text("SELECT 1 FROM narrative_goals WHERE id = :id"), {"id": plan.goal_id})
                if result.fetchone():
                    goal_id_to_save = plan.goal_id
            
            # Insert plan
            conn.execute(text("""
                INSERT INTO weekly_schedules (id, goal_id, week_start, week_end, 
                    total_posts, pillar_distribution, platform_distribution,
                    reasoning_chain, justification, status, created_at)
                VALUES (:id, :goal_id, :week_start, :week_end,
                    :total_posts, :pillar_dist, :platform_dist,
                    :reasoning, :justification, :status, NOW())
                ON CONFLICT (id) DO UPDATE SET
                    total_posts = EXCLUDED.total_posts,
                    pillar_distribution = EXCLUDED.pillar_distribution,
                    platform_distribution = EXCLUDED.platform_distribution,
                    reasoning_chain = EXCLUDED.reasoning_chain,
                    justification = EXCLUDED.justification,
                    status = EXCLUDED.status,
                    updated_at = NOW()
            """), {
                "id": plan.id,
                "goal_id": goal_id_to_save,
                "week_start": plan.week_start,
                "week_end": plan.week_end,
                "total_posts": plan.total_posts,
                "pillar_dist": json.dumps(plan.pillar_distribution),
                "platform_dist": json.dumps(plan.platform_distribution),
                "reasoning": json.dumps([r.to_dict() for r in plan.reasoning_chain]),
                "justification": json.dumps({"text": plan.justification_summary}) if plan.justification_summary else None,
                "status": plan.status
            })
            
            # Insert scheduled slots
            for slot in plan.scheduled_slots:
                conn.execute(text("""
                    INSERT INTO schedule_slots (id, schedule_id, video_id, video_title,
                        platform, scheduled_date, scheduled_time, pillar, selection_reason)
                    VALUES (:id, :schedule_id, :video_id, :video_title,
                        :platform, :scheduled_date, :scheduled_time, :pillar, :selection_reason)
                """), {
                    "id": slot.id,
                    "schedule_id": plan.id,
                    "video_id": slot.video_id,
                    "video_title": slot.video_title,
                    "platform": slot.platform,
                    "scheduled_date": slot.scheduled_date,
                    "scheduled_time": slot.scheduled_time,
                    "pillar": slot.pillar,
                    "selection_reason": slot.selection_reason
                })
            
            conn.commit()
    
    def _get_default_goal(self) -> NarrativeGoal:
        """Get default goal if none configured"""
        return NarrativeGoal(
            goal_statement="Build audience engagement and grow following",
            primary_cta="follow",
            target_audience="General audience interested in content",
            time_horizon="next_7_days",
            target_followers=100,
            target_engagement_rate=4.0
        )
    
    def _get_default_pillars(self) -> List[NarrativePillar]:
        """Get default pillars if none configured"""
        return [
            NarrativePillar(
                name="Process/How-To",
                description="Educational content showing expertise",
                pillar_type="value",
                color="#3b82f6",
                keywords=["how to", "tutorial", "step by step", "guide", "tips"],
                target_percentage=30.0
            ),
            NarrativePillar(
                name="Pain Points",
                description="Address audience struggles",
                pillar_type="value",
                color="#ef4444",
                keywords=["problem", "struggle", "frustrated", "challenge"],
                target_percentage=20.0
            ),
            NarrativePillar(
                name="Social Proof",
                description="Testimonials and results",
                pillar_type="proof",
                color="#f59e0b",
                keywords=["results", "success", "testimonial", "transformation"],
                target_percentage=20.0
            ),
            NarrativePillar(
                name="Personality",
                description="Behind-the-scenes content",
                pillar_type="value",
                color="#a855f7",
                keywords=["behind the scenes", "day in life", "personal", "story"],
                target_percentage=20.0
            ),
            NarrativePillar(
                name="Promotion/CTA",
                description="Calls-to-action",
                pillar_type="cta",
                color="#10b981",
                keywords=["sign up", "subscribe", "join", "download", "link"],
                target_percentage=10.0
            ),
        ]
    
    def _get_default_constraints(self) -> SchedulingConstraints:
        """Get default constraints if none configured"""
        return SchedulingConstraints(
            enabled_platforms=["tiktok", "instagram"],
            max_posts_per_day=3,
            min_posts_per_day=2,
            posting_windows={
                "tiktok": ["12:00", "18:00", "21:00"],
                "instagram": ["09:00", "17:00"]
            },
            timezone="America/New_York",
            min_pre_social_score=60,
            max_same_pillar_consecutive=2
        )
    
    async def approve_and_schedule(self, plan_id: str) -> Dict[str, Any]:
        """Approve a plan and create actual scheduled posts"""
        logger.info(f"[NarrativeScheduler] Approving plan {plan_id}...")
        
        with self.engine.connect() as conn:
            # Get plan slots
            result = conn.execute(text("""
                SELECT ss.video_id, ss.video_title, ss.platform, ss.scheduled_date, ss.scheduled_time, ss.pillar
                FROM schedule_slots ss
                WHERE ss.schedule_id = :plan_id
            """), {"plan_id": plan_id})
            
            slots = list(result)
            created = 0
            
            # Get goal_id from plan for narrative tracking
            goal_result = conn.execute(text("""
                SELECT goal_id FROM weekly_schedules WHERE id = :plan_id
            """), {"plan_id": plan_id}).fetchone()
            goal_id = str(goal_result[0]) if goal_result and goal_result[0] else None
            
            for slot in slots:
                video_id, title, platform, sched_date, sched_time, pillar = slot
                
                # Create scheduled post with narrative tracking
                scheduled_at = datetime.combine(sched_date, datetime.strptime(sched_time, "%H:%M").time())
                
                # Store pillar info in recommendation_reasoning as JSON for now
                # (We can add a dedicated pillar_tags column later if needed)
                pillar_info = json.dumps({"pillar": pillar, "narrative_goal_id": goal_id}) if pillar else None
                
                conn.execute(text("""
                    INSERT INTO scheduled_posts (
                        clip_id, title, platform, scheduled_time, scheduled_at, status, source,
                        recommendation_reasoning, is_ai_recommended
                    ) VALUES (
                        CAST(:content_id AS uuid), :title, :platform, :scheduled_time, :scheduled_at, 'scheduled', 'narrative_builder',
                        :pillar_info, TRUE
                    )
                """), {
                    "content_id": video_id,
                    "title": title,
                    "platform": platform,
                    "scheduled_time": scheduled_at,
                    "scheduled_at": scheduled_at,
                    "pillar_info": pillar_info
                })
                created += 1
            
            # Update plan status
            conn.execute(text("""
                UPDATE weekly_schedules SET status = 'approved', updated_at = NOW()
                WHERE id = :plan_id
            """), {"plan_id": plan_id})
            
            conn.commit()
        
        logger.info(f"[NarrativeScheduler] Plan approved: {created} posts scheduled")
        
        return {"approved": True, "posts_scheduled": created}
    
    async def get_plan_reasoning(self, plan_id: str) -> Dict[str, Any]:
        """Get the full reasoning chain for a plan"""
        with self.engine.connect() as conn:
            result = conn.execute(text("""
                SELECT reasoning_chain, justification, pillar_distribution, platform_distribution
                FROM weekly_schedules WHERE id = :plan_id
            """), {"plan_id": plan_id})
            
            row = result.fetchone()
            
            if row:
                return {
                    "reasoning_chain": json.loads(row[0]) if row[0] else [],
                    "justification": row[1],
                    "pillar_distribution": json.loads(row[2]) if row[2] else {},
                    "platform_distribution": json.loads(row[3]) if row[3] else {}
                }
        
        return {}
