"""
Weekly Automation for Narrative Scheduling

Handles automated weekly tasks:
1. Sunday evening: Generate reflection on past week
2. Sunday evening: Apply learnings to next week's plan
3. Monday morning: Generate and notify about new 7-day plan
"""

import os
import logging
import asyncio
from typing import Optional, Dict, Any, List
from datetime import datetime, date, timedelta
from dataclasses import dataclass

from sqlalchemy import create_engine, text

from .scheduler import NarrativeScheduler
from .reflection_system import ReflectionSystem
from .models import WeeklyPlan, Learning

logger = logging.getLogger(__name__)

DATABASE_URL = os.getenv("DATABASE_URL", "postgresql://postgres:postgres@127.0.0.1:54322/postgres")


@dataclass
class AutomationConfig:
    """Configuration for weekly automation."""
    reflection_day: int = 6  # Sunday (0=Monday, 6=Sunday)
    reflection_hour: int = 20  # 8 PM
    plan_generation_day: int = 0  # Monday
    plan_generation_hour: int = 6  # 6 AM
    apply_learnings: bool = True
    auto_approve_plans: bool = False
    notification_webhook: Optional[str] = None


class WeeklyAutomation:
    """
    Manages automated weekly narrative scheduling tasks.
    """
    
    def __init__(self, config: Optional[AutomationConfig] = None):
        self.config = config or AutomationConfig()
        self.engine = create_engine(DATABASE_URL)
        self.scheduler = NarrativeScheduler()
        self.reflection_system = ReflectionSystem()
        self._running = False
    
    async def run_weekly_reflection(self, schedule_id: Optional[str] = None) -> Dict[str, Any]:
        """
        Run weekly reflection on the most recent completed schedule.
        
        Returns reflection results and generated learnings.
        """
        logger.info("[WeeklyAutomation] Starting weekly reflection...")
        
        # Find the most recent completed schedule if not provided
        if not schedule_id:
            schedule_id = await self._find_last_completed_schedule()
        
        if not schedule_id:
            logger.warning("[WeeklyAutomation] No completed schedule found for reflection")
            return {"success": False, "reason": "No completed schedule found"}
        
        try:
            # Generate reflection
            reflection = await self.reflection_system.generate_weekly_reflection(schedule_id)
            
            result = {
                "success": True,
                "schedule_id": schedule_id,
                "reflection": reflection.to_dict(),
                "learnings_generated": len(reflection.learnings),
                "recommendations": reflection.recommendations
            }
            
            logger.info(f"[WeeklyAutomation] Reflection complete: {len(reflection.learnings)} learnings generated")
            
            # Send notification if configured
            if self.config.notification_webhook:
                await self._send_notification("weekly_reflection", result)
            
            return result
            
        except Exception as e:
            logger.error(f"[WeeklyAutomation] Reflection failed: {e}")
            return {"success": False, "error": str(e)}
    
    async def generate_next_week_plan(
        self,
        goal_id: Optional[str] = None,
        apply_learnings: bool = True
    ) -> Dict[str, Any]:
        """
        Generate next week's plan with optional learning application.
        """
        logger.info("[WeeklyAutomation] Generating next week's plan...")
        
        try:
            # Load learnings if enabled
            learnings = []
            if apply_learnings:
                learnings = await self.reflection_system.get_accumulated_learnings(
                    goal_id=goal_id,
                    min_confidence=0.7,
                    unapplied_only=True
                )
                logger.info(f"[WeeklyAutomation] Applying {len(learnings)} learnings")
            
            # Generate plan
            plan = await self.scheduler.generate_7_day_plan(
                goal_id=goal_id,
                use_defaults=True
            )
            
            # Mark learnings as applied
            if learnings:
                await self._mark_learnings_applied([l.id for l in learnings])
            
            result = {
                "success": True,
                "plan_id": plan.id,
                "total_posts": plan.total_posts,
                "pillar_distribution": plan.pillar_distribution,
                "platform_distribution": plan.platform_distribution,
                "learnings_applied": len(learnings),
                "status": plan.status
            }
            
            # Auto-approve if configured
            if self.config.auto_approve_plans:
                approval = await self.scheduler.approve_and_schedule(plan.id)
                result["auto_approved"] = True
                result["posts_scheduled"] = approval.get("posts_scheduled", 0)
            
            logger.info(f"[WeeklyAutomation] Plan generated: {plan.total_posts} posts")
            
            # Send notification
            if self.config.notification_webhook:
                await self._send_notification("plan_generated", result)
            
            return result
            
        except Exception as e:
            logger.error(f"[WeeklyAutomation] Plan generation failed: {e}")
            return {"success": False, "error": str(e)}
    
    async def run_full_weekly_cycle(self, goal_id: Optional[str] = None) -> Dict[str, Any]:
        """
        Run the complete weekly automation cycle:
        1. Reflect on past week
        2. Generate learnings
        3. Create new plan with learnings applied
        """
        logger.info("[WeeklyAutomation] Starting full weekly cycle...")
        
        results = {
            "reflection": None,
            "plan": None,
            "timestamp": datetime.now().isoformat()
        }
        
        # Step 1: Run reflection
        reflection_result = await self.run_weekly_reflection()
        results["reflection"] = reflection_result
        
        # Step 2: Generate new plan (learnings applied automatically)
        plan_result = await self.generate_next_week_plan(
            goal_id=goal_id,
            apply_learnings=self.config.apply_learnings
        )
        results["plan"] = plan_result
        
        # Summary
        results["success"] = reflection_result.get("success", False) or plan_result.get("success", False)
        results["summary"] = {
            "learnings_generated": reflection_result.get("learnings_generated", 0),
            "learnings_applied": plan_result.get("learnings_applied", 0),
            "posts_planned": plan_result.get("total_posts", 0)
        }
        
        logger.info(f"[WeeklyAutomation] Weekly cycle complete: {results['summary']}")
        
        return results
    
    async def _find_last_completed_schedule(self) -> Optional[str]:
        """Find the most recent completed or executing schedule."""
        with self.engine.connect() as conn:
            result = conn.execute(text("""
                SELECT id FROM weekly_schedules 
                WHERE status IN ('approved', 'executing', 'completed')
                ORDER BY week_end DESC LIMIT 1
            """))
            row = result.fetchone()
            return str(row[0]) if row else None
    
    async def _mark_learnings_applied(self, learning_ids: List[str]):
        """Mark learnings as applied."""
        with self.engine.connect() as conn:
            for learning_id in learning_ids:
                conn.execute(text("""
                    UPDATE learnings SET applied = TRUE WHERE id = :id
                """), {"id": learning_id})
            conn.commit()
    
    async def _send_notification(self, event_type: str, data: Dict[str, Any]):
        """Send webhook notification."""
        if not self.config.notification_webhook:
            return
        
        try:
            import httpx
            async with httpx.AsyncClient() as client:
                await client.post(
                    self.config.notification_webhook,
                    json={
                        "event": event_type,
                        "data": data,
                        "timestamp": datetime.now().isoformat()
                    },
                    timeout=10
                )
        except Exception as e:
            logger.error(f"[WeeklyAutomation] Notification failed: {e}")
    
    def should_run_reflection(self) -> bool:
        """Check if reflection should run based on schedule."""
        now = datetime.now()
        return (
            now.weekday() == self.config.reflection_day and
            now.hour == self.config.reflection_hour
        )
    
    def should_run_plan_generation(self) -> bool:
        """Check if plan generation should run based on schedule."""
        now = datetime.now()
        return (
            now.weekday() == self.config.plan_generation_day and
            now.hour == self.config.plan_generation_hour
        )
    
    async def start_scheduler(self, check_interval_minutes: int = 30):
        """Start the automation scheduler loop."""
        self._running = True
        logger.info("[WeeklyAutomation] Scheduler started")
        
        while self._running:
            try:
                if self.should_run_reflection():
                    await self.run_weekly_reflection()
                
                if self.should_run_plan_generation():
                    await self.generate_next_week_plan(apply_learnings=True)
                
            except Exception as e:
                logger.error(f"[WeeklyAutomation] Scheduler error: {e}")
            
            await asyncio.sleep(check_interval_minutes * 60)
    
    def stop_scheduler(self):
        """Stop the automation scheduler."""
        self._running = False
        logger.info("[WeeklyAutomation] Scheduler stopped")


# Convenience function for manual triggering
async def trigger_weekly_cycle(goal_id: Optional[str] = None) -> Dict[str, Any]:
    """Manually trigger a full weekly cycle."""
    automation = WeeklyAutomation()
    return await automation.run_full_weekly_cycle(goal_id)
