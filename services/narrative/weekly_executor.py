"""
Weekly Cycle Executor (NAR-004)
=================================
Executes the 7-day narrative plan with daily checks, reflection phase, and learning accumulation.

This service:
1. Executes daily content slots from the weekly plan
2. Monitors performance throughout the week
3. Runs reflection phase at week end
4. Accumulates learnings for next cycle
5. Integrates with content selection and generation pipeline

Architecture:
- Main execution loop checks daily slots
- Each slot triggers content selection → generation → QA → publish
- Daily metrics collection and mid-week adjustments
- Sunday reflection synthesizes learnings
- Integrates with WeeklyPlanner for next cycle

Integration Points:
- NarrativeScheduler: Weekly plan data
- ContentSelector (NAR-005): Select UGC content
- ContentGeneration: Generate posts from templates
- QAGate: Quality checks before publishing
- MetricsSnapshot: Performance tracking
- ReflectionSystem: Weekly learnings
"""

import asyncio
import os
from datetime import datetime, timedelta, timezone, time
from typing import Optional, Dict, Any, List, Tuple
from uuid import uuid4
from loguru import logger
from sqlalchemy import create_engine, text
from dataclasses import dataclass, field
from enum import Enum

from services.event_bus import EventBus, Topics


class ExecutionState(Enum):
    """States of the weekly executor"""
    IDLE = "idle"
    EXECUTING_DAILY_SLOTS = "executing_daily_slots"
    MID_WEEK_REFLECTION = "mid_week_reflection"
    WEEKLY_REFLECTION = "weekly_reflection"
    APPLYING_LEARNINGS = "applying_learnings"
    ERROR = "error"


@dataclass
class DailySlotExecution:
    """Result of executing a daily slot"""
    slot_id: str
    scheduled_time: datetime
    content_selected: bool
    content_id: Optional[str]
    generation_status: str
    qa_passed: bool
    published: bool
    metrics: Dict[str, Any]
    errors: List[str] = field(default_factory=list)


@dataclass
class WeeklyCycleMetrics:
    """Metrics for the weekly cycle"""
    week_id: str
    start_date: datetime
    end_date: datetime
    total_slots: int
    executed_slots: int
    failed_slots: int
    total_engagements: int
    average_engagement_rate: float
    top_performing_pillar: Optional[str]
    learnings_count: int
    next_cycle_adjustments: List[str]


class WeeklyCycleExecutor:
    """
    Weekly Cycle Executor (NAR-004)

    Executes the 7-day narrative plan with continuous monitoring,
    mid-week adjustments, and end-of-week reflection.

    Daily Workflow:
    1. Check today's slots
    2. For each slot:
       a. Select content (NAR-005)
       b. Generate post from template
       c. Run QA gate
       d. Publish if approved
       e. Track metrics
    3. Log execution results

    Weekly Workflow:
    1. Sunday evening: Run reflection
    2. Synthesize learnings
    3. Pass to WeeklyPlanner for next cycle

    Usage:
        executor = WeeklyCycleExecutor.get_instance()
        await executor.start()

        # Manually trigger execution
        result = await executor.execute_daily_slots()

        # Run reflection
        reflection = await executor.run_weekly_reflection()
    """

    _instance: Optional["WeeklyCycleExecutor"] = None

    def __init__(self):
        """Initialize weekly cycle executor"""
        if WeeklyCycleExecutor._instance is not None:
            raise RuntimeError("Use WeeklyCycleExecutor.get_instance()")

        self.event_bus = EventBus.get_instance()
        self.event_bus.set_source("weekly-cycle-executor")

        # Database connection
        self.database_url = os.getenv(
            "DATABASE_URL",
            "postgresql://postgres:postgres@localhost:54322/postgres"
        )
        self.engine = create_engine(self.database_url)

        # State
        self.state = ExecutionState.IDLE
        self._is_running = False
        self._task: Optional[asyncio.Task] = None
        self.current_week_id: Optional[str] = None
        self.execution_history: List[DailySlotExecution] = []

        # Configuration
        self.check_interval = int(os.getenv("WEEKLY_EXECUTOR_CHECK_INTERVAL", "3600"))  # 1 hour
        self.reflection_day = 6  # Sunday (0=Monday, 6=Sunday)
        self.reflection_hour = 20  # 8 PM
        self.mid_week_check_day = 3  # Thursday

        WeeklyCycleExecutor._instance = self
        logger.info("📅 WeeklyCycleExecutor initialized")

    @classmethod
    def get_instance(cls) -> "WeeklyCycleExecutor":
        """Get singleton instance"""
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance

    async def start(self) -> None:
        """Start the weekly cycle executor"""
        if self._is_running:
            logger.warning("⚠️  WeeklyCycleExecutor already running")
            return

        self._is_running = True

        # Ensure tables exist
        await self._ensure_tables_exist()

        # Start execution loop
        self._task = asyncio.create_task(self._execution_loop())

        logger.success("🚀 WeeklyCycleExecutor started")

    async def stop(self) -> None:
        """Stop the weekly cycle executor"""
        self._is_running = False

        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass

        logger.info("🛑 WeeklyCycleExecutor stopped")

    async def _execution_loop(self) -> None:
        """Main execution loop - checks for daily slots and runs reflection"""
        while self._is_running:
            try:
                now = datetime.now(timezone.utc)

                # Check if we need to run reflection (Sunday evening)
                if now.weekday() == self.reflection_day and now.hour >= self.reflection_hour:
                    if self.state != ExecutionState.WEEKLY_REFLECTION:
                        await self._run_weekly_reflection_cycle()

                # Check if we need to run mid-week adjustment (Thursday)
                elif now.weekday() == self.mid_week_check_day and self.state != ExecutionState.MID_WEEK_REFLECTION:
                    await self._run_mid_week_check()

                # Execute daily slots
                else:
                    await self.execute_daily_slots()

                # Sleep until next check
                await asyncio.sleep(self.check_interval)

            except Exception as e:
                logger.error(f"❌ Error in execution loop: {e}")
                self.state = ExecutionState.ERROR
                await asyncio.sleep(60)  # Wait before retry

    async def execute_daily_slots(self) -> List[DailySlotExecution]:
        """
        Execute today's content slots

        Returns:
            List of execution results for each slot
        """
        self.state = ExecutionState.EXECUTING_DAILY_SLOTS

        try:
            # Get current active weekly plan
            week_plan = await self._get_current_week_plan()
            if not week_plan:
                logger.info("ℹ️  No active weekly plan found")
                self.state = ExecutionState.IDLE
                return []

            self.current_week_id = week_plan["id"]

            # Get today's slots
            today_slots = await self._get_today_slots(week_plan["id"])
            if not today_slots:
                logger.info("ℹ️  No slots scheduled for today")
                self.state = ExecutionState.IDLE
                return []

            logger.info(f"📋 Executing {len(today_slots)} slots for today")

            results = []
            for slot in today_slots:
                result = await self._execute_slot(slot)
                results.append(result)
                self.execution_history.append(result)

                # Publish event
                await self.event_bus.publish(
                    Topics.CONTENT_PUBLISHED if result.published else "content.slot.failed",
                    {
                        "slot_id": result.slot_id,
                        "week_id": self.current_week_id,
                        "published": result.published,
                        "content_id": result.content_id,
                        "errors": result.errors
                    }
                )

            self.state = ExecutionState.IDLE
            return results

        except Exception as e:
            logger.error(f"❌ Error executing daily slots: {e}")
            self.state = ExecutionState.ERROR
            raise

    async def _execute_slot(self, slot: Dict[str, Any]) -> DailySlotExecution:
        """
        Execute a single content slot

        Workflow:
        1. Select content using NAR-005 content selector
        2. Generate post from template
        3. Run QA gate
        4. Publish if approved
        5. Track initial metrics

        Args:
            slot: Slot data with pillar, template, time, etc.

        Returns:
            Execution result
        """
        slot_id = slot.get("id") or str(uuid4())
        errors = []

        logger.info(f"🎯 Executing slot {slot_id} - Pillar: {slot.get('pillar_name')}")

        result = DailySlotExecution(
            slot_id=slot_id,
            scheduled_time=slot.get("scheduled_time"),
            content_selected=False,
            content_id=None,
            generation_status="pending",
            qa_passed=False,
            published=False,
            metrics={},
            errors=errors
        )

        try:
            # Step 1: Select content (NAR-005)
            from services.narrative_scheduler.content_selector import ContentSelector
            selector = ContentSelector.get_instance()

            selected_content = await selector.select_content_for_slot(slot)
            if not selected_content:
                errors.append("No suitable content found for slot")
                logger.warning(f"⚠️  No content selected for slot {slot_id}")
                return result

            result.content_selected = True
            result.content_id = selected_content["content_id"]

            # Step 2: Generate post from template
            from services.content_generation_pipeline import ContentGenerationPipeline
            gen_service = ContentGenerationPipeline()

            generated_post = await gen_service.generate_from_template(
                template_id=slot.get("template_id"),
                context={
                    "pillar": slot.get("pillar_name"),
                    "goal": slot.get("goal_statement"),
                    "content": selected_content
                }
            )

            if not generated_post:
                errors.append("Content generation failed")
                result.generation_status = "failed"
                return result

            result.generation_status = "success"

            # Step 3: QA Gate
            from services.qa_gate_service import QAGateService
            qa_service = QAGateService.get_instance()

            qa_result = await qa_service.check_content(generated_post["text"])
            result.qa_passed = qa_result["approved"]

            if not qa_result["approved"]:
                errors.append(f"QA failed: {qa_result.get('reason')}")
                logger.warning(f"⚠️  QA failed for slot {slot_id}: {qa_result.get('reason')}")
                return result

            # Step 4: Publish (Note: Publishing is typically done via events, this is a placeholder)
            # For now, we'll emit an event for publishing rather than direct publishing
            # In production, this would integrate with the publishing queue/workers
            publish_result = {"success": True}  # Placeholder

            # TODO: Integrate with actual publishing system
            # from services.publisher_service import PublisherService
            # pub_service = PublisherService(db)
            # publish_result = await pub_service.publish_post(
            #     platform=slot.get("platform", "tiktok"),
            #     account_id=slot.get("account_id"),
            #     content=generated_post,
            #     scheduled_time=slot.get("scheduled_time")
            # )

            result.published = publish_result.get("success", False)

            if result.published:
                logger.success(f"✅ Slot {slot_id} published successfully")
            else:
                errors.append(f"Publishing failed: {publish_result.get('error')}")

            # Step 5: Track initial metrics
            result.metrics = {
                "published_at": datetime.now(timezone.utc).isoformat(),
                "platform": slot.get("platform"),
                "pillar": slot.get("pillar_name")
            }

        except Exception as e:
            logger.error(f"❌ Error executing slot {slot_id}: {e}")
            errors.append(str(e))

        return result

    async def _run_weekly_reflection_cycle(self) -> Dict[str, Any]:
        """
        Run end-of-week reflection and learning accumulation

        Returns:
            Reflection results with learnings
        """
        self.state = ExecutionState.WEEKLY_REFLECTION
        logger.info("🔍 Starting weekly reflection cycle...")

        try:
            # Import reflection system
            from services.narrative_scheduler.reflection_system import ReflectionSystem
            reflection_system = ReflectionSystem()

            # Get current week data
            week_plan = await self._get_current_week_plan()
            if not week_plan:
                logger.warning("⚠️  No active week plan for reflection")
                return {"success": False, "reason": "No active plan"}

            # Generate reflection
            reflection = await reflection_system.generate_weekly_reflection(
                schedule_id=week_plan["id"]
            )

            # Extract learnings
            learnings = reflection.learnings if hasattr(reflection, 'learnings') else []

            # Store learnings for next cycle
            await self._store_learnings(week_plan["id"], learnings)

            # Publish reflection event
            await self.event_bus.publish(
                "narrative.reflection.completed",
                {
                    "week_id": week_plan["id"],
                    "learnings_count": len(learnings),
                    "top_performing_pillar": reflection.best_performing_pillar if hasattr(reflection, 'best_performing_pillar') else None,
                    "recommendations": reflection.recommendations if hasattr(reflection, 'recommendations') else []
                }
            )

            logger.success(f"✅ Weekly reflection complete: {len(learnings)} learnings generated")

            self.state = ExecutionState.IDLE
            return {
                "success": True,
                "week_id": week_plan["id"],
                "learnings": [l.to_dict() if hasattr(l, 'to_dict') else l for l in learnings],
                "reflection": reflection.to_dict() if hasattr(reflection, 'to_dict') else {}
            }

        except Exception as e:
            logger.error(f"❌ Error in weekly reflection: {e}")
            self.state = ExecutionState.ERROR
            return {"success": False, "error": str(e)}

    async def _run_mid_week_check(self) -> None:
        """Run mid-week performance check and minor adjustments"""
        self.state = ExecutionState.MID_WEEK_REFLECTION
        logger.info("📊 Running mid-week performance check...")

        try:
            # Get week-to-date performance
            week_plan = await self._get_current_week_plan()
            if not week_plan:
                return

            # Analyze performance
            performance = await self._analyze_week_performance(week_plan["id"])

            # Check if any pillar is severely underperforming
            underperforming_pillars = [
                p for p in performance.get("pillar_performance", [])
                if p["engagement_rate"] < performance.get("average_engagement_rate", 0) * 0.5
            ]

            if underperforming_pillars:
                logger.warning(f"⚠️  {len(underperforming_pillars)} underperforming pillars detected")

                # Log for next week's planning
                await self._log_mid_week_insight(
                    week_plan["id"],
                    {
                        "type": "underperformance",
                        "pillars": underperforming_pillars,
                        "recommendation": "Consider reducing allocation for next week"
                    }
                )

            self.state = ExecutionState.IDLE

        except Exception as e:
            logger.error(f"❌ Error in mid-week check: {e}")
            self.state = ExecutionState.ERROR

    async def get_cycle_metrics(self, week_id: Optional[str] = None) -> WeeklyCycleMetrics:
        """
        Get metrics for the current or specified week cycle

        Args:
            week_id: Optional week plan ID (defaults to current week)

        Returns:
            Metrics summary
        """
        if not week_id:
            week_plan = await self._get_current_week_plan()
            week_id = week_plan["id"] if week_plan else None

        if not week_id:
            raise ValueError("No active week plan")

        # Get execution history for this week
        week_executions = [e for e in self.execution_history if e.slot_id.startswith(week_id[:8])]

        # Calculate metrics
        total_slots = len(week_executions)
        executed = sum(1 for e in week_executions if e.published)
        failed = sum(1 for e in week_executions if not e.published)

        # Get learnings
        learnings = await self._get_learnings(week_id)

        return WeeklyCycleMetrics(
            week_id=week_id,
            start_date=datetime.now(timezone.utc) - timedelta(days=7),  # Placeholder
            end_date=datetime.now(timezone.utc),
            total_slots=total_slots,
            executed_slots=executed,
            failed_slots=failed,
            total_engagements=0,  # TODO: Aggregate from metrics
            average_engagement_rate=0.0,  # TODO: Calculate
            top_performing_pillar=None,  # TODO: Analyze
            learnings_count=len(learnings),
            next_cycle_adjustments=[]  # TODO: Generate from learnings
        )

    # Helper methods

    async def _ensure_tables_exist(self) -> None:
        """Ensure required database tables exist"""
        with self.engine.connect() as conn:
            # Weekly execution tracking
            conn.execute(text("""
                CREATE TABLE IF NOT EXISTS weekly_cycle_executions (
                    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
                    week_id UUID NOT NULL,
                    slot_id TEXT NOT NULL,
                    scheduled_time TIMESTAMPTZ,
                    content_id TEXT,
                    published BOOLEAN DEFAULT FALSE,
                    qa_passed BOOLEAN DEFAULT FALSE,
                    errors JSONB,
                    metrics JSONB,
                    created_at TIMESTAMPTZ DEFAULT NOW()
                );
            """))

            # Weekly learnings
            conn.execute(text("""
                CREATE TABLE IF NOT EXISTS weekly_learnings (
                    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
                    week_id UUID NOT NULL,
                    learning_type TEXT,
                    insight TEXT,
                    confidence FLOAT,
                    recommendation TEXT,
                    created_at TIMESTAMPTZ DEFAULT NOW()
                );
            """))

            conn.commit()

    async def _get_current_week_plan(self) -> Optional[Dict[str, Any]]:
        """Get the currently active weekly plan"""
        with self.engine.connect() as conn:
            result = conn.execute(text("""
                SELECT id, goal_id, start_date, end_date, status, plan_data
                FROM narrative_schedules
                WHERE start_date <= NOW()::date
                  AND end_date >= NOW()::date
                  AND status = 'active'
                ORDER BY created_at DESC
                LIMIT 1
            """))
            row = result.fetchone()
            if row:
                return {
                    "id": str(row[0]),
                    "goal_id": str(row[1]),
                    "start_date": row[2],
                    "end_date": row[3],
                    "status": row[4],
                    "plan_data": row[5]
                }
            return None

    async def _get_today_slots(self, week_id: str) -> List[Dict[str, Any]]:
        """Get slots scheduled for today"""
        today = datetime.now(timezone.utc).date()

        with self.engine.connect() as conn:
            result = conn.execute(text("""
                SELECT id, schedule_id, day_of_week, time_slot, pillar_id, template_id, platform, account_id
                FROM narrative_schedule_slots
                WHERE schedule_id = :week_id
                  AND day_of_week = :today
                  AND status = 'pending'
                ORDER BY time_slot
            """), {"week_id": week_id, "today": today.isoformat()})

            slots = []
            for row in result:
                slots.append({
                    "id": str(row[0]),
                    "schedule_id": str(row[1]),
                    "day_of_week": row[2],
                    "scheduled_time": row[3],
                    "pillar_id": str(row[4]) if row[4] else None,
                    "template_id": str(row[5]) if row[5] else None,
                    "platform": row[6],
                    "account_id": str(row[7]) if row[7] else None
                })

            return slots

    async def _store_learnings(self, week_id: str, learnings: List[Any]) -> None:
        """Store weekly learnings in database"""
        with self.engine.connect() as conn:
            for learning in learnings:
                conn.execute(text("""
                    INSERT INTO weekly_learnings (week_id, learning_type, insight, confidence, recommendation)
                    VALUES (:week_id, :type, :insight, :confidence, :recommendation)
                """), {
                    "week_id": week_id,
                    "type": learning.category if hasattr(learning, 'category') else "general",
                    "insight": learning.insight if hasattr(learning, 'insight') else str(learning),
                    "confidence": learning.confidence if hasattr(learning, 'confidence') else 0.5,
                    "recommendation": learning.action if hasattr(learning, 'action') else ""
                })
            conn.commit()

    async def _get_learnings(self, week_id: str) -> List[Dict[str, Any]]:
        """Get learnings for a week"""
        with self.engine.connect() as conn:
            result = conn.execute(text("""
                SELECT learning_type, insight, confidence, recommendation
                FROM weekly_learnings
                WHERE week_id = :week_id
                ORDER BY confidence DESC
            """), {"week_id": week_id})

            return [
                {
                    "type": row[0],
                    "insight": row[1],
                    "confidence": row[2],
                    "recommendation": row[3]
                }
                for row in result
            ]

    async def _analyze_week_performance(self, week_id: str) -> Dict[str, Any]:
        """Analyze performance for the week so far"""
        # TODO: Implement actual performance analysis
        return {
            "average_engagement_rate": 0.05,
            "pillar_performance": []
        }

    async def _log_mid_week_insight(self, week_id: str, insight: Dict[str, Any]) -> None:
        """Log a mid-week insight for future planning"""
        with self.engine.connect() as conn:
            conn.execute(text("""
                INSERT INTO weekly_learnings (week_id, learning_type, insight, recommendation)
                VALUES (:week_id, :type, :insight, :recommendation)
            """), {
                "week_id": week_id,
                "type": "mid_week_" + insight["type"],
                "insight": str(insight.get("pillars", [])),
                "recommendation": insight.get("recommendation", "")
            })
            conn.commit()
