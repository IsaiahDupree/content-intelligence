"""
Autonomous Narrative Planner
=============================
AI agent that autonomously builds narrative plans until a complete
7-day schedule is ready, then waits for human approval before scheduling.
"""

import os
import logging
import asyncio
from typing import List, Dict, Optional, Any
from datetime import datetime, timedelta
from dataclasses import dataclass, field
from enum import Enum
from uuid import uuid4

from sqlalchemy import create_engine, text

logger = logging.getLogger(__name__)

DATABASE_URL = os.getenv("DATABASE_URL", "postgresql://postgres:postgres@127.0.0.1:54322/postgres")


class PlannerState(Enum):
    IDLE = "idle"
    ANALYZING = "analyzing"
    BUILDING_PILLARS = "building_pillars"
    GENERATING_PLAN = "generating_plan"
    AWAITING_APPROVAL = "awaiting_approval"
    APPROVED = "approved"
    SCHEDULING = "scheduling"


@dataclass
class PlanRequirements:
    """Requirements for a complete 7-day plan."""
    min_posts_per_day: int = 2
    max_posts_per_day: int = 5
    required_days: int = 7
    min_pillars: int = 3
    min_candidates_per_pillar: int = 5
    min_high_performers: int = 10


@dataclass
class PlanReadiness:
    """Readiness status for plan generation."""
    is_ready: bool = False
    analyzed_videos: int = 0
    high_performers: int = 0
    active_pillars: int = 0
    candidates_available: int = 0
    missing_requirements: List[str] = field(default_factory=list)
    readiness_score: float = 0.0


@dataclass
class DraftPlan:
    """Draft 7-day plan awaiting approval."""
    id: str
    created_at: datetime
    days: List[Dict[str, Any]]
    total_posts: int
    platforms: List[str]
    pillars_used: List[str]
    reasoning_chain: List[Dict[str, str]]
    estimated_reach: int
    status: str  # draft, approved, rejected, scheduled


class AutonomousNarrativePlanner:
    """
    AI Agent that autonomously plans content until a 7-day schedule is ready.
    
    Workflow:
    1. Analyze available content inventory
    2. Build/update narrative pillars based on content
    3. Generate candidate selections
    4. Create 7-day plan draft
    5. STOP and wait for human approval
    6. Only schedule after explicit approval
    """
    
    def __init__(
        self,
        requirements: Optional[PlanRequirements] = None,
        auto_analyze: bool = True,
        scan_interval_seconds: int = 600
    ):
        self.engine = create_engine(DATABASE_URL)
        self.requirements = requirements or PlanRequirements()
        self.auto_analyze = auto_analyze
        self.scan_interval = scan_interval_seconds
        
        self.state = PlannerState.IDLE
        self.current_draft: Optional[DraftPlan] = None
        self.reasoning_chain: List[Dict[str, str]] = []
        self._running = False
    
    async def start(self):
        """Start the autonomous planner loop."""
        self._running = True
        logger.info("[NarrativePlanner] Starting autonomous narrative planner")
        
        while self._running:
            try:
                await self._run_cycle()
            except Exception as e:
                logger.error(f"[NarrativePlanner] Cycle error: {e}")
            
            # Only continue cycling if not awaiting approval
            if self.state == PlannerState.AWAITING_APPROVAL:
                logger.info("[NarrativePlanner] Plan ready - awaiting human approval")
                await asyncio.sleep(60)  # Check less frequently when waiting
            else:
                await asyncio.sleep(self.scan_interval)
    
    async def stop(self):
        """Stop the autonomous planner."""
        self._running = False
        logger.info("[NarrativePlanner] Stopped autonomous narrative planner")
    
    async def _run_cycle(self):
        """Execute one autonomous planning cycle."""
        self.reasoning_chain = []
        
        # Step 1: Check readiness
        self._add_reasoning("Checking plan readiness", "Analyzing content inventory and requirements")
        readiness = await self.check_readiness()
        
        if not readiness.is_ready:
            self.state = PlannerState.ANALYZING
            self._add_reasoning(
                f"Not ready yet (score: {readiness.readiness_score:.0%})",
                f"Missing: {', '.join(readiness.missing_requirements)}"
            )
            
            # Try to improve readiness
            if self.auto_analyze:
                await self._improve_readiness(readiness)
            return
        
        # Step 2: Generate plan if ready and no current draft
        if self.current_draft is None or self.current_draft.status == 'rejected':
            self._add_reasoning("Readiness confirmed", "Generating 7-day plan draft")
            self.state = PlannerState.GENERATING_PLAN
            
            draft = await self._generate_plan_draft()
            if draft:
                self.current_draft = draft
                self.state = PlannerState.AWAITING_APPROVAL
                self._add_reasoning(
                    f"Plan draft created with {draft.total_posts} posts",
                    "Waiting for human approval before scheduling"
                )
                
                # Store draft in database
                await self._save_draft(draft)
    
    async def check_readiness(self) -> PlanReadiness:
        """Check if we have enough content for a 7-day plan."""
        readiness = PlanReadiness()
        missing = []
        
        with self.engine.connect() as conn:
            # Count analyzed videos
            readiness.analyzed_videos = conn.execute(text(
                "SELECT COUNT(*) FROM video_analysis"
            )).scalar() or 0
            
            # Count high performers
            readiness.high_performers = conn.execute(text(
                "SELECT COUNT(*) FROM video_analysis WHERE pre_social_score >= 70"
            )).scalar() or 0
            
            # Count active pillars
            readiness.active_pillars = conn.execute(text(
                "SELECT COUNT(*) FROM narrative_pillars WHERE is_active = TRUE"
            )).scalar() or 0
            
            # Count available candidates
            readiness.candidates_available = conn.execute(text("""
                SELECT COUNT(*) FROM videos v
                WHERE NOT EXISTS (
                    SELECT 1 FROM scheduled_posts sp 
                    WHERE sp.content_id = v.id::text
                )
            """)).scalar() or 0
        
        # Check requirements
        req = self.requirements
        
        if readiness.high_performers < req.min_high_performers:
            missing.append(f"Need {req.min_high_performers - readiness.high_performers} more high performers")
        
        if readiness.active_pillars < req.min_pillars:
            missing.append(f"Need {req.min_pillars - readiness.active_pillars} more pillars")
        
        min_candidates = req.min_posts_per_day * req.required_days
        if readiness.candidates_available < min_candidates:
            missing.append(f"Need {min_candidates - readiness.candidates_available} more candidate videos")
        
        readiness.missing_requirements = missing
        readiness.is_ready = len(missing) == 0
        
        # Calculate readiness score
        scores = [
            min(readiness.high_performers / req.min_high_performers, 1.0),
            min(readiness.active_pillars / req.min_pillars, 1.0),
            min(readiness.candidates_available / min_candidates, 1.0)
        ]
        readiness.readiness_score = sum(scores) / len(scores)
        
        return readiness
    
    async def _improve_readiness(self, readiness: PlanReadiness):
        """Try to improve readiness by analyzing more content."""
        logger.info("[NarrativePlanner] Attempting to improve readiness...")
        
        # If we need more pillars, try to create them from content
        if readiness.active_pillars < self.requirements.min_pillars:
            await self._auto_create_pillars()
        
        # If we need more analyzed videos, trigger analysis
        if readiness.high_performers < self.requirements.min_high_performers:
            await self._trigger_content_analysis()
    
    async def _auto_create_pillars(self):
        """Auto-create narrative pillars using AI theme clustering."""
        self._add_reasoning("Creating pillars", "Using AI to discover content themes and create narrative pillars")
        
        with self.engine.connect() as conn:
            # Get content data for AI clustering
            content_data = conn.execute(text("""
                SELECT 
                    va.topics,
                    va.tone,
                    va.hooks,
                    v.file_name
                FROM video_analysis va
                JOIN videos v ON v.id = va.video_id
                WHERE va.topics IS NOT NULL
                LIMIT 50
            """))
            
            content_list = []
            for row in content_data:
                content_list.append({
                    "topics": row[0],
                    "tone": row[1],
                    "hooks": row[2],
                    "filename": row[3]
                })
            
            if not content_list:
                logger.warning("[NarrativePlanner] No analyzed content for pillar discovery")
                return
            
            # Use AI to discover themes and create pillars
            pillars = await self._discover_pillars_with_ai(content_list)
            
            for pillar in pillars:
                # Check if pillar exists
                exists = conn.execute(text("""
                    SELECT 1 FROM narrative_pillars 
                    WHERE LOWER(name) LIKE :theme
                """), {"theme": f"%{pillar['name'].lower()}%"}).scalar()
                
                if not exists:
                    conn.execute(text("""
                        INSERT INTO narrative_pillars (id, name, description, is_active, target_percentage)
                        VALUES (:id, :name, :desc, TRUE, :pct)
                    """), {
                        "id": str(uuid4()),
                        "name": pillar["name"],
                        "desc": pillar["description"],
                        "pct": pillar.get("target_percentage", 20)
                    })
                    conn.commit()
                    logger.info(f"[NarrativePlanner] AI Created pillar: {pillar['name']}")
    
    async def _discover_pillars_with_ai(self, content_list: List[Dict]) -> List[Dict]:
        """Use OpenAI to discover content themes and suggest pillars."""
        try:
            from openai import OpenAI
            
            client = OpenAI()
            
            # Prepare content summary for AI
            content_summary = []
            for c in content_list[:30]:  # Limit to avoid token overflow
                topics = c.get("topics", [])
                if isinstance(topics, list):
                    topics = ", ".join(topics[:5])
                content_summary.append(f"- Topics: {topics}, Tone: {c.get('tone', 'unknown')}")
            
            prompt = f"""Analyze this content library and discover 3-5 distinct narrative pillars (content themes).
Each pillar should represent a recurring theme that appears across multiple videos.

Content Library:
{chr(10).join(content_summary)}

Return a JSON array of pillars with this structure:
[
  {{
    "name": "Short catchy pillar name (2-4 words)",
    "description": "One sentence describing this content theme",
    "target_percentage": 20  // Suggested % of content for this pillar
  }}
]

Focus on themes that:
1. Appear multiple times in the content
2. Would resonate with a social media audience
3. Are distinct from each other
4. Could be used for content planning

Return ONLY valid JSON, no explanation."""

            response = client.chat.completions.create(
                model="gpt-4o-mini",
                messages=[{"role": "user", "content": prompt}],
                temperature=0.7,
                max_tokens=500
            )
            
            import json
            result = response.choices[0].message.content.strip()
            # Clean up response
            if result.startswith("```"):
                result = result.split("```")[1]
                if result.startswith("json"):
                    result = result[4:]
            
            pillars = json.loads(result)
            logger.info(f"[NarrativePlanner] AI discovered {len(pillars)} pillars")
            return pillars
            
        except Exception as e:
            logger.error(f"[NarrativePlanner] AI pillar discovery failed: {e}")
            # Fallback to basic theme extraction
            return [
                {"name": "Lifestyle", "description": "Day-to-day life content", "target_percentage": 30},
                {"name": "Educational", "description": "Tips and tutorials", "target_percentage": 25},
                {"name": "Entertainment", "description": "Fun and engaging content", "target_percentage": 25},
                {"name": "Personal", "description": "Behind the scenes and stories", "target_percentage": 20}
            ]
    
    async def _trigger_content_analysis(self):
        """Trigger analysis for unanalyzed videos."""
        self._add_reasoning("Triggering analysis", "Analyzing unanalyzed high-quality videos")
        
        # This would call the video analysis service
        # For now, log the intent
        logger.info("[NarrativePlanner] Would trigger content analysis for unanalyzed videos")
    
    async def _generate_plan_draft(self) -> Optional[DraftPlan]:
        """Generate a 7-day plan draft."""
        self.state = PlannerState.GENERATING_PLAN
        
        days = []
        total_posts = 0
        platforms_used = set()
        pillars_used = set()
        
        with self.engine.connect() as conn:
            # Get available high-quality candidates
            candidates = conn.execute(text("""
                SELECT v.id, v.title, va.pre_social_score, va.content_type,
                       va.recommended_platforms
                FROM videos v
                JOIN video_analysis va ON v.id = va.video_id
                WHERE va.pre_social_score >= 60
                AND NOT EXISTS (
                    SELECT 1 FROM scheduled_posts sp 
                    WHERE sp.content_id = v.id::text
                )
                ORDER BY va.pre_social_score DESC
                LIMIT 50
            """))
            
            candidate_list = list(candidates)
            
            # Get active pillars
            pillars = conn.execute(text("""
                SELECT id, name, target_percentage 
                FROM narrative_pillars 
                WHERE is_active = TRUE
            """))
            pillar_list = list(pillars)
        
        if len(candidate_list) < self.requirements.min_posts_per_day * 7:
            self._add_reasoning("Insufficient candidates", "Not enough high-quality videos for 7-day plan")
            return None
        
        # Generate 7 days of content
        candidate_idx = 0
        start_date = datetime.now() + timedelta(days=1)
        
        for day_num in range(7):
            day_date = start_date + timedelta(days=day_num)
            day_posts = []
            posts_today = min(
                self.requirements.max_posts_per_day,
                len(candidate_list) - candidate_idx
            )
            
            for _ in range(max(self.requirements.min_posts_per_day, posts_today)):
                if candidate_idx >= len(candidate_list):
                    break
                    
                candidate = candidate_list[candidate_idx]
                candidate_idx += 1
                
                # Determine platform
                platforms = candidate[4] if candidate[4] else ["instagram"]
                platform = platforms[0] if isinstance(platforms, list) else "instagram"
                platforms_used.add(platform)
                
                # Determine pillar
                content_type = candidate[3] or "general"
                pillars_used.add(content_type)
                
                day_posts.append({
                    "video_id": str(candidate[0]),
                    "title": candidate[1],
                    "score": float(candidate[2]),
                    "platform": platform,
                    "pillar": content_type,
                    "scheduled_time": f"{day_date.strftime('%Y-%m-%d')}T{9 + len(day_posts) * 3}:00:00"
                })
                total_posts += 1
            
            days.append({
                "date": day_date.strftime("%Y-%m-%d"),
                "day_name": day_date.strftime("%A"),
                "posts": day_posts
            })
        
        self._add_reasoning(
            f"Generated {total_posts} posts across {len(pillars_used)} pillars",
            f"Platforms: {', '.join(platforms_used)}"
        )
        
        return DraftPlan(
            id=str(uuid4()),
            created_at=datetime.now(),
            days=days,
            total_posts=total_posts,
            platforms=list(platforms_used),
            pillars_used=list(pillars_used),
            reasoning_chain=self.reasoning_chain.copy(),
            estimated_reach=total_posts * 1500,  # Rough estimate
            status="draft"
        )
    
    async def _save_draft(self, draft: DraftPlan):
        """Save draft plan to database."""
        with self.engine.connect() as conn:
            conn.execute(text("""
                INSERT INTO weekly_schedules (
                    id, week_start, status, created_at, schedule_data
                ) VALUES (
                    :id, :week_start, 'draft', :created_at, :schedule_data
                )
                ON CONFLICT (id) DO UPDATE SET
                    schedule_data = :schedule_data,
                    status = 'draft'
            """), {
                "id": draft.id,
                "week_start": draft.days[0]["date"] if draft.days else datetime.now().strftime("%Y-%m-%d"),
                "created_at": draft.created_at,
                "schedule_data": str({
                    "days": draft.days,
                    "reasoning": draft.reasoning_chain,
                    "total_posts": draft.total_posts,
                    "platforms": draft.platforms,
                    "pillars": draft.pillars_used
                })
            })
            conn.commit()
    
    def _add_reasoning(self, thought: str, action: str):
        """Add a step to the reasoning chain."""
        self.reasoning_chain.append({
            "step": len(self.reasoning_chain) + 1,
            "thought": thought,
            "action": action,
            "timestamp": datetime.now().isoformat()
        })
    
    # =========================================================================
    # HUMAN APPROVAL INTERFACE
    # =========================================================================
    
    async def approve_plan(self, plan_id: Optional[str] = None) -> Dict[str, Any]:
        """
        Human approves the current draft plan.
        Only after approval will scheduling begin.
        """
        if self.current_draft is None:
            return {"success": False, "error": "No draft plan to approve"}
        
        if plan_id and self.current_draft.id != plan_id:
            return {"success": False, "error": "Plan ID mismatch"}
        
        self.current_draft.status = "approved"
        self.state = PlannerState.APPROVED
        
        self._add_reasoning("Human approval received", "Proceeding to schedule posts")
        
        # Update database
        with self.engine.connect() as conn:
            conn.execute(text("""
                UPDATE weekly_schedules 
                SET status = 'approved' 
                WHERE id = :id
            """), {"id": self.current_draft.id})
            conn.commit()
        
        logger.info(f"[NarrativePlanner] Plan {self.current_draft.id} approved by human")
        
        return {
            "success": True,
            "plan_id": self.current_draft.id,
            "message": "Plan approved - ready for scheduling",
            "next_step": "Call /schedule-approved to begin scheduling"
        }
    
    async def reject_plan(self, plan_id: Optional[str] = None, reason: str = "") -> Dict[str, Any]:
        """Human rejects the current draft plan."""
        if self.current_draft is None:
            return {"success": False, "error": "No draft plan to reject"}
        
        self.current_draft.status = "rejected"
        self.state = PlannerState.IDLE
        
        self._add_reasoning(f"Human rejected plan: {reason}", "Will generate new plan")
        
        # Update database
        with self.engine.connect() as conn:
            conn.execute(text("""
                UPDATE weekly_schedules 
                SET status = 'rejected' 
                WHERE id = :id
            """), {"id": self.current_draft.id})
            conn.commit()
        
        old_draft = self.current_draft
        self.current_draft = None
        
        logger.info(f"[NarrativePlanner] Plan {old_draft.id} rejected: {reason}")
        
        return {
            "success": True,
            "plan_id": old_draft.id,
            "message": "Plan rejected - will regenerate",
            "reason": reason
        }
    
    async def schedule_approved_plan(self) -> Dict[str, Any]:
        """
        Schedule the approved plan.
        This is the ONLY way posts get scheduled - after human approval.
        """
        if self.current_draft is None:
            return {"success": False, "error": "No plan to schedule"}
        
        if self.current_draft.status != "approved":
            return {
                "success": False,
                "error": f"Plan must be approved first (current status: {self.current_draft.status})"
            }
        
        self.state = PlannerState.SCHEDULING
        scheduled_count = 0
        
        with self.engine.connect() as conn:
            for day in self.current_draft.days:
                for post in day["posts"]:
                    # Create scheduled post
                    conn.execute(text("""
                        INSERT INTO scheduled_posts (
                            id, content_id, platform, scheduled_time, status,
                            origin_type, origin_id
                        ) VALUES (
                            :id, :content_id, :platform, :scheduled_time, 'pending',
                            'narrative', :origin_id
                        )
                    """), {
                        "id": str(uuid4()),
                        "content_id": post["video_id"],
                        "platform": post["platform"],
                        "scheduled_time": post["scheduled_time"],
                        "origin_id": self.current_draft.id
                    })
                    scheduled_count += 1
            
            # Update weekly schedule status
            conn.execute(text("""
                UPDATE weekly_schedules 
                SET status = 'active' 
                WHERE id = :id
            """), {"id": self.current_draft.id})
            
            conn.commit()
        
        self.current_draft.status = "scheduled"
        self.state = PlannerState.IDLE
        
        logger.info(f"[NarrativePlanner] Scheduled {scheduled_count} posts from approved plan")
        
        return {
            "success": True,
            "plan_id": self.current_draft.id,
            "scheduled_posts": scheduled_count,
            "message": f"Successfully scheduled {scheduled_count} posts"
        }
    
    async def get_status(self) -> Dict[str, Any]:
        """Get current planner status."""
        readiness = await self.check_readiness()
        
        return {
            "state": self.state.value,
            "readiness": {
                "is_ready": readiness.is_ready,
                "score": readiness.readiness_score,
                "analyzed_videos": readiness.analyzed_videos,
                "high_performers": readiness.high_performers,
                "active_pillars": readiness.active_pillars,
                "candidates_available": readiness.candidates_available,
                "missing": readiness.missing_requirements
            },
            "current_draft": {
                "id": self.current_draft.id,
                "status": self.current_draft.status,
                "total_posts": self.current_draft.total_posts,
                "platforms": self.current_draft.platforms,
                "pillars": self.current_draft.pillars_used,
                "reasoning": self.current_draft.reasoning_chain
            } if self.current_draft else None,
            "reasoning_chain": self.reasoning_chain
        }
    
    async def get_draft_plan(self) -> Optional[Dict[str, Any]]:
        """Get the current draft plan for review."""
        if self.current_draft is None:
            return None
        
        return {
            "id": self.current_draft.id,
            "created_at": self.current_draft.created_at.isoformat(),
            "status": self.current_draft.status,
            "days": self.current_draft.days,
            "total_posts": self.current_draft.total_posts,
            "platforms": self.current_draft.platforms,
            "pillars_used": self.current_draft.pillars_used,
            "reasoning_chain": self.current_draft.reasoning_chain,
            "estimated_reach": self.current_draft.estimated_reach
        }
    
    async def run_once(self) -> Dict[str, Any]:
        """Run a single planning cycle manually."""
        await self._run_cycle()
        return await self.get_status()


# Global planner instance
_planner: Optional[AutonomousNarrativePlanner] = None


def get_planner() -> AutonomousNarrativePlanner:
    """Get or create the global planner instance."""
    global _planner
    if _planner is None:
        _planner = AutonomousNarrativePlanner()
    return _planner
