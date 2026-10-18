"""
Background Task Scheduler for AI Agents
=========================================
Manages scheduled background tasks for Narrative Planner and Experiment Runner.
"""

import os
import asyncio
import logging
from typing import Dict, Optional, Callable, Any
from datetime import datetime, timedelta
from dataclasses import dataclass
from enum import Enum

from .event_bus import (
    get_event_bus, AgentEventEmitter, AgentType, EventType
)

logger = logging.getLogger(__name__)


class TaskStatus(Enum):
    PENDING = "pending"
    RUNNING = "running"
    PAUSED = "paused"
    STOPPED = "stopped"
    ERROR = "error"


@dataclass
class ScheduledTask:
    """A scheduled background task."""
    id: str
    name: str
    agent_type: AgentType
    interval_seconds: int
    status: TaskStatus = TaskStatus.PENDING
    last_run: Optional[datetime] = None
    next_run: Optional[datetime] = None
    run_count: int = 0
    error_count: int = 0
    last_error: Optional[str] = None


class AgentBackgroundScheduler:
    """
    Background scheduler for AI agent tasks.
    
    Manages:
    - Narrative Planner cycles (every 10 minutes)
    - Experiment Runner cycles (every 5 minutes)
    - Custom scheduled tasks
    """
    
    _instance: Optional['AgentBackgroundScheduler'] = None
    
    def __new__(cls):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
            cls._instance._initialized = False
        return cls._instance
    
    def __init__(self):
        if self._initialized:
            return
        
        self._initialized = True
        self._tasks: Dict[str, ScheduledTask] = {}
        self._task_handlers: Dict[str, Callable] = {}
        self._running = False
        self._loop_task: Optional[asyncio.Task] = None
        
        # Event emitters for each agent
        self._emitters: Dict[AgentType, AgentEventEmitter] = {}
        
        # Initialize default tasks
        self._init_default_tasks()
        
        logger.info("[Scheduler] Agent Background Scheduler initialized")
    
    def _init_default_tasks(self):
        """Initialize default agent tasks."""
        # Narrative Planner - runs every 10 minutes
        self._tasks["narrative_planner"] = ScheduledTask(
            id="narrative_planner",
            name="Narrative Planner Cycle",
            agent_type=AgentType.NARRATIVE_PLANNER,
            interval_seconds=600,  # 10 minutes
            next_run=datetime.now()
        )
        
        # Experiment Runner - runs every 5 minutes
        self._tasks["experiment_runner"] = ScheduledTask(
            id="experiment_runner",
            name="Experiment Runner Cycle",
            agent_type=AgentType.EXPERIMENT_RUNNER,
            interval_seconds=300,  # 5 minutes
            next_run=datetime.now()
        )
        
        # Register handlers
        self._task_handlers["narrative_planner"] = self._run_narrative_planner
        self._task_handlers["experiment_runner"] = self._run_experiment_runner
    
    def _get_emitter(self, agent_type: AgentType) -> AgentEventEmitter:
        """Get or create emitter for agent type."""
        if agent_type not in self._emitters:
            self._emitters[agent_type] = AgentEventEmitter(agent_type)
        return self._emitters[agent_type]
    
    async def start(self):
        """Start the background scheduler."""
        if self._running:
            logger.warning("[Scheduler] Already running")
            return
        
        self._running = True
        logger.info("[Scheduler] Starting background scheduler")
        
        # Emit start events for all agents
        for task in self._tasks.values():
            emitter = self._get_emitter(task.agent_type)
            await emitter.emit(
                EventType.AGENT_STARTED,
                f"{task.name} started",
                f"Interval: {task.interval_seconds}s"
            )
            task.status = TaskStatus.RUNNING
        
        # Start the main loop
        self._loop_task = asyncio.create_task(self._main_loop())
    
    async def stop(self):
        """Stop the background scheduler."""
        self._running = False
        
        if self._loop_task:
            self._loop_task.cancel()
            try:
                await self._loop_task
            except asyncio.CancelledError:
                pass
        
        # Emit stop events
        for task in self._tasks.values():
            emitter = self._get_emitter(task.agent_type)
            await emitter.emit(
                EventType.AGENT_STOPPED,
                f"{task.name} stopped"
            )
            task.status = TaskStatus.STOPPED
        
        logger.info("[Scheduler] Background scheduler stopped")
    
    async def _main_loop(self):
        """Main scheduler loop."""
        while self._running:
            try:
                now = datetime.now()
                
                for task_id, task in self._tasks.items():
                    if task.status != TaskStatus.RUNNING:
                        continue
                    
                    if task.next_run and now >= task.next_run:
                        # Run the task
                        await self._execute_task(task_id)
                
                # Sleep for a bit before checking again
                await asyncio.sleep(10)
                
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"[Scheduler] Loop error: {e}")
                await asyncio.sleep(30)
    
    async def _execute_task(self, task_id: str):
        """Execute a scheduled task."""
        task = self._tasks.get(task_id)
        if not task:
            return
        
        handler = self._task_handlers.get(task_id)
        if not handler:
            return
        
        emitter = self._get_emitter(task.agent_type)
        
        try:
            # Emit cycle start
            await emitter.emit(
                EventType.CYCLE_STARTED,
                f"Cycle #{task.run_count + 1} started",
                data={"cycle_number": task.run_count + 1}
            )
            
            # Run the handler
            await handler(emitter)
            
            # Update task stats
            task.last_run = datetime.now()
            task.next_run = task.last_run + timedelta(seconds=task.interval_seconds)
            task.run_count += 1
            
            # Emit cycle complete
            await emitter.emit(
                EventType.CYCLE_COMPLETED,
                f"Cycle #{task.run_count} completed",
                data={
                    "cycle_number": task.run_count,
                    "next_run": task.next_run.isoformat()
                }
            )
            
        except Exception as e:
            task.error_count += 1
            task.last_error = str(e)
            task.next_run = datetime.now() + timedelta(seconds=task.interval_seconds * 2)
            
            await emitter.error(f"Cycle error", str(e))
            logger.error(f"[Scheduler] Task {task_id} error: {e}")
    
    async def _run_narrative_planner(self, emitter: AgentEventEmitter):
        """Run narrative planner cycle."""
        from services.narrative_scheduler.autonomous_planner import get_planner
        
        planner = get_planner()
        
        # Emit thought about what we're doing
        await emitter.thought(
            "Checking content readiness",
            "Analyzing available videos and pillars for 7-day plan"
        )
        
        # Check readiness
        readiness = await planner.check_readiness()
        
        await emitter.emit(
            EventType.DATA_FETCHED,
            f"Readiness: {readiness.readiness_score:.0%}",
            f"Videos: {readiness.analyzed_videos}, High Performers: {readiness.high_performers}",
            data={
                "is_ready": readiness.is_ready,
                "score": readiness.readiness_score,
                "analyzed": readiness.analyzed_videos,
                "high_performers": readiness.high_performers,
                "pillars": readiness.active_pillars
            }
        )
        
        if not readiness.is_ready:
            await emitter.thought(
                "Not ready for plan generation",
                f"Missing: {', '.join(readiness.missing_requirements)}"
            )
            return
        
        # Run planner cycle
        await emitter.action("Generating 7-day plan draft")
        status = await planner.run_once()
        
        if planner.current_draft:
            await emitter.emit(
                EventType.PLAN_GENERATED,
                f"Draft plan created: {planner.current_draft.total_posts} posts",
                data={
                    "plan_id": planner.current_draft.id,
                    "total_posts": planner.current_draft.total_posts,
                    "platforms": planner.current_draft.platforms
                }
            )
            
            await emitter.emit(
                EventType.WAITING_APPROVAL,
                "Awaiting human approval",
                "Plan draft ready for review"
            )
    
    async def _run_experiment_runner(self, emitter: AgentEventEmitter):
        """Run experiment runner cycle."""
        from services.experiments_scheduler.autonomous_runner import get_runner
        
        runner = get_runner()
        
        await emitter.thought(
            "Scanning experiment backlog",
            "Looking for high-priority experiments to start"
        )
        
        # Get candidates
        candidates = await runner.scan_backlog()
        
        await emitter.emit(
            EventType.DATA_FETCHED,
            f"Found {len(candidates)} experiment candidates",
            data={"candidate_count": len(candidates)}
        )
        
        if not candidates:
            await emitter.thought("No experiments ready to start", "Backlog is empty")
            return
        
        # Check running count
        running = await runner._count_running_experiments()
        available = runner.max_concurrent - running
        
        await emitter.emit(
            EventType.REASONING,
            f"Running: {running}/{runner.max_concurrent}, Available slots: {available}",
            data={"running": running, "max": runner.max_concurrent, "available": available}
        )
        
        if available <= 0:
            await emitter.thought(
                "Max concurrent experiments reached",
                f"Waiting for {running} experiments to complete"
            )
            return
        
        # Auto-start if enabled
        if runner.auto_start_enabled:
            high_priority = [c for c in candidates if c.priority_score >= runner.min_priority]
            
            if high_priority:
                await emitter.action(
                    f"Auto-starting {min(available, len(high_priority))} experiments",
                    data={"to_start": min(available, len(high_priority))}
                )
                
                started = await runner._auto_start_experiments(high_priority, available)
                
                await emitter.emit(
                    EventType.EXPERIMENT_STARTED,
                    f"Started {started} experiments",
                    data={"started_count": started}
                )
    
    # =========================================================================
    # Control Methods
    # =========================================================================
    
    def pause_task(self, task_id: str):
        """Pause a specific task."""
        if task_id in self._tasks:
            self._tasks[task_id].status = TaskStatus.PAUSED
            logger.info(f"[Scheduler] Paused task: {task_id}")
    
    def resume_task(self, task_id: str):
        """Resume a paused task."""
        if task_id in self._tasks:
            self._tasks[task_id].status = TaskStatus.RUNNING
            self._tasks[task_id].next_run = datetime.now()
            logger.info(f"[Scheduler] Resumed task: {task_id}")
    
    def set_interval(self, task_id: str, interval_seconds: int):
        """Update task interval."""
        if task_id in self._tasks:
            self._tasks[task_id].interval_seconds = interval_seconds
            logger.info(f"[Scheduler] Updated {task_id} interval to {interval_seconds}s")
    
    async def run_task_now(self, task_id: str):
        """Immediately run a task."""
        if task_id in self._tasks and task_id in self._task_handlers:
            await self._execute_task(task_id)
    
    def get_task_status(self, task_id: str) -> Optional[Dict[str, Any]]:
        """Get status of a specific task."""
        task = self._tasks.get(task_id)
        if not task:
            return None
        
        return {
            "id": task.id,
            "name": task.name,
            "agent_type": task.agent_type.value,
            "status": task.status.value,
            "interval_seconds": task.interval_seconds,
            "last_run": task.last_run.isoformat() if task.last_run else None,
            "next_run": task.next_run.isoformat() if task.next_run else None,
            "run_count": task.run_count,
            "error_count": task.error_count,
            "last_error": task.last_error
        }
    
    def get_all_tasks(self) -> Dict[str, Dict[str, Any]]:
        """Get status of all tasks."""
        return {
            task_id: self.get_task_status(task_id)
            for task_id in self._tasks
        }


# Global scheduler instance
_scheduler: Optional[AgentBackgroundScheduler] = None


def get_scheduler() -> AgentBackgroundScheduler:
    """Get the global scheduler instance."""
    global _scheduler
    if _scheduler is None:
        _scheduler = AgentBackgroundScheduler()
    return _scheduler


async def start_background_agents():
    """Start all background agents."""
    scheduler = get_scheduler()
    await scheduler.start()


async def stop_background_agents():
    """Stop all background agents."""
    scheduler = get_scheduler()
    await scheduler.stop()
