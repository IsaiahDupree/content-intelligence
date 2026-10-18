"""
Autonomous Experiment Runner
=============================
AI agent that automatically runs experiments from the backlog.
Prioritizes, starts, monitors, and learns from experiments autonomously.
"""

import os
import logging
import asyncio
from typing import List, Dict, Optional, Any
from datetime import datetime, timedelta
from dataclasses import dataclass
from enum import Enum

from sqlalchemy import create_engine, text

logger = logging.getLogger(__name__)

DATABASE_URL = os.getenv("DATABASE_URL", "postgresql://postgres:postgres@127.0.0.1:54322/postgres")


class AutoRunnerState(Enum):
    IDLE = "idle"
    SCANNING = "scanning"
    RUNNING = "running"
    PAUSED = "paused"


@dataclass
class ExperimentCandidate:
    """Experiment ready to be auto-started."""
    id: str
    hypothesis: str
    target_metric: str
    impact: str  # small, medium, large
    effort: str  # low, medium, high
    confidence: str  # low, medium, high
    priority_score: int
    status: str
    created_at: datetime


class AutonomousExperimentRunner:
    """
    AI Agent that autonomously manages experiment lifecycle.
    
    Responsibilities:
    - Scan experiment backlog for candidates
    - Prioritize based on impact, effort, confidence
    - Automatically start high-priority experiments
    - Monitor running experiments
    - Collect results and trigger learning
    - Manage resource allocation
    """
    
    def __init__(
        self,
        min_priority: int = 100,
        max_concurrent: int = 3,
        scan_interval_seconds: int = 300,
        auto_start_enabled: bool = True
    ):
        self.engine = create_engine(DATABASE_URL)
        self.min_priority = min_priority
        self.max_concurrent = max_concurrent
        self.scan_interval = scan_interval_seconds
        self.auto_start_enabled = auto_start_enabled
        
        self.state = AutoRunnerState.IDLE
        self.running_experiments: List[str] = []
        self.last_scan: Optional[datetime] = None
        self._running = False
    
    async def start(self):
        """Start the autonomous runner loop."""
        self._running = True
        self.state = AutoRunnerState.SCANNING
        logger.info("[AutoRunner] Starting autonomous experiment runner")
        
        while self._running:
            try:
                await self._run_cycle()
            except Exception as e:
                logger.error(f"[AutoRunner] Cycle error: {e}")
            
            await asyncio.sleep(self.scan_interval)
    
    async def stop(self):
        """Stop the autonomous runner."""
        self._running = False
        self.state = AutoRunnerState.PAUSED
        logger.info("[AutoRunner] Stopped autonomous experiment runner")
    
    async def _run_cycle(self):
        """Execute one autonomous cycle."""
        logger.info("[AutoRunner] Running cycle...")
        
        # 1. Scan backlog for candidates
        candidates = await self.scan_backlog()
        logger.info(f"[AutoRunner] Found {len(candidates)} experiment candidates")
        
        # 2. Check running experiments
        running_count = await self._count_running_experiments()
        available_slots = self.max_concurrent - running_count
        
        if available_slots <= 0:
            logger.info(f"[AutoRunner] Max concurrent experiments reached ({running_count}/{self.max_concurrent})")
            return
        
        # 3. Auto-start high priority experiments
        if self.auto_start_enabled:
            started = await self._auto_start_experiments(candidates, available_slots)
            logger.info(f"[AutoRunner] Auto-started {started} experiments")
        
        # 4. Check completed experiments for analysis
        completed = await self._check_completed_experiments()
        if completed:
            logger.info(f"[AutoRunner] Analyzed {len(completed)} completed experiments")
        
        self.last_scan = datetime.now()
    
    async def scan_backlog(self) -> List[ExperimentCandidate]:
        """Scan experiment backlog for auto-start candidates."""
        candidates = []
        
        with self.engine.connect() as conn:
            result = conn.execute(text("""
                SELECT id, hypothesis, primary_metric, 
                       COALESCE(notes, '') as notes,
                       status, created_at,
                       confidence
                FROM experiments
                WHERE status IN ('draft', 'pending', 'backlog')
                ORDER BY 
                    CASE 
                        WHEN confidence >= 0.8 THEN 3
                        WHEN confidence >= 0.5 THEN 2
                        ELSE 1
                    END DESC,
                    created_at ASC
                LIMIT 20
            """))
            
            for row in result:
                # Calculate priority score based on available data
                priority = self._calculate_priority(
                    confidence=float(row[6]) if row[6] else 0.5,
                    notes=row[3] or ""
                )
                
                if priority >= self.min_priority:
                    candidates.append(ExperimentCandidate(
                        id=str(row[0]),
                        hypothesis=row[1] or "",
                        target_metric=row[2] or "engagement_rate",
                        impact=self._extract_impact(row[3]),
                        effort=self._extract_effort(row[3]),
                        confidence=self._confidence_to_level(float(row[6]) if row[6] else 0.5),
                        priority_score=priority,
                        status=row[4],
                        created_at=row[5]
                    ))
        
        # Sort by priority
        candidates.sort(key=lambda x: x.priority_score, reverse=True)
        return candidates
    
    def _calculate_priority(self, confidence: float, notes: str) -> int:
        """Calculate priority score for an experiment."""
        base_score = 50
        
        # Confidence boost (0-100 points)
        confidence_boost = int(confidence * 100)
        
        # Impact boost from notes
        impact_boost = 0
        notes_lower = notes.lower()
        if "high impact" in notes_lower or "large" in notes_lower:
            impact_boost = 100
        elif "medium impact" in notes_lower or "medium" in notes_lower:
            impact_boost = 50
        
        # Effort penalty
        effort_penalty = 0
        if "high effort" in notes_lower:
            effort_penalty = 50
        elif "low effort" in notes_lower:
            effort_penalty = -25  # Low effort is a bonus
        
        return base_score + confidence_boost + impact_boost - effort_penalty
    
    def _extract_impact(self, notes: str) -> str:
        """Extract impact level from notes."""
        if not notes:
            return "medium"
        notes_lower = notes.lower()
        if "large" in notes_lower or "high impact" in notes_lower:
            return "large"
        elif "small" in notes_lower or "low impact" in notes_lower:
            return "small"
        return "medium"
    
    def _extract_effort(self, notes: str) -> str:
        """Extract effort level from notes."""
        if not notes:
            return "medium"
        notes_lower = notes.lower()
        if "high effort" in notes_lower:
            return "high"
        elif "low effort" in notes_lower:
            return "low"
        return "medium"
    
    def _confidence_to_level(self, confidence: float) -> str:
        """Convert confidence float to level."""
        if confidence >= 0.7:
            return "high"
        elif confidence >= 0.4:
            return "medium"
        return "low"
    
    async def _count_running_experiments(self) -> int:
        """Count currently running experiments."""
        with self.engine.connect() as conn:
            result = conn.execute(text("""
                SELECT COUNT(*) FROM experiments WHERE status = 'active'
            """)).scalar()
            return result or 0
    
    async def _auto_start_experiments(
        self,
        candidates: List[ExperimentCandidate],
        max_to_start: int
    ) -> int:
        """Auto-start high priority experiments."""
        started = 0
        
        for candidate in candidates[:max_to_start]:
            if candidate.priority_score >= self.min_priority:
                success = await self._start_experiment(candidate)
                if success:
                    started += 1
                    self.running_experiments.append(candidate.id)
        
        return started
    
    async def _start_experiment(self, candidate: ExperimentCandidate) -> bool:
        """Start a single experiment."""
        try:
            from .experiment_agent import ExperimentAgent, AgentAction, AgentActionType
            
            agent = ExperimentAgent()
            
            # Create action to start experiment
            action = AgentAction(
                experiment_id=candidate.id,
                action_type=AgentActionType.CREATE_HYPOTHESIS,
                action_params={
                    "hypothesis": candidate.hypothesis,
                    "target_metric": candidate.target_metric,
                    "auto_started": True
                }
            )
            
            # Execute the action
            await agent.execute_action(action)
            
            # Update experiment status
            with self.engine.connect() as conn:
                conn.execute(text("""
                    UPDATE experiments 
                    SET status = 'active', 
                        started_at = NOW(),
                        notes = COALESCE(notes, '') || ' [Auto-started by AI Agent]'
                    WHERE id = :id
                """), {"id": candidate.id})
                conn.commit()
            
            logger.info(f"[AutoRunner] Auto-started experiment: {candidate.id[:8]}... (priority: {candidate.priority_score})")
            return True
            
        except Exception as e:
            logger.error(f"[AutoRunner] Failed to start experiment {candidate.id}: {e}")
            return False
    
    async def _check_completed_experiments(self) -> List[str]:
        """Check for completed experiments and trigger analysis."""
        completed = []
        
        with self.engine.connect() as conn:
            # Find experiments that should be analyzed
            result = conn.execute(text("""
                SELECT e.id, e.hypothesis, e.started_at
                FROM experiments e
                WHERE e.status = 'active'
                AND e.started_at < NOW() - INTERVAL '7 days'
                AND NOT EXISTS (
                    SELECT 1 FROM schedule_performance sp 
                    WHERE sp.schedule_id::text = e.id::text
                )
                LIMIT 5
            """))
            
            for row in result:
                experiment_id = str(row[0])
                
                # Trigger analysis
                try:
                    from .scheduler import ExperimentsScheduler
                    scheduler = ExperimentsScheduler()
                    
                    experiment = await scheduler.get_experiment(experiment_id)
                    if experiment and experiment.hypotheses:
                        for hyp in experiment.hypotheses:
                            await scheduler.analyze_hypothesis(hyp.id)
                    
                    # Mark as completed
                    conn.execute(text("""
                        UPDATE experiments 
                        SET status = 'completed', completed_at = NOW()
                        WHERE id = :id
                    """), {"id": experiment_id})
                    conn.commit()
                    
                    completed.append(experiment_id)
                    logger.info(f"[AutoRunner] Completed experiment analysis: {experiment_id[:8]}...")
                    
                except Exception as e:
                    logger.error(f"[AutoRunner] Analysis failed for {experiment_id}: {e}")
        
        return completed
    
    async def get_status(self) -> Dict[str, Any]:
        """Get current runner status."""
        running_count = await self._count_running_experiments()
        candidates = await self.scan_backlog()
        
        return {
            "state": self.state.value,
            "auto_start_enabled": self.auto_start_enabled,
            "min_priority": self.min_priority,
            "max_concurrent": self.max_concurrent,
            "running_experiments": running_count,
            "available_slots": self.max_concurrent - running_count,
            "backlog_candidates": len(candidates),
            "high_priority_pending": len([c for c in candidates if c.priority_score >= 200]),
            "last_scan": self.last_scan.isoformat() if self.last_scan else None
        }
    
    async def run_once(self) -> Dict[str, Any]:
        """Run a single cycle manually (for testing/manual trigger)."""
        await self._run_cycle()
        return await self.get_status()
    
    def set_auto_start(self, enabled: bool):
        """Enable or disable auto-start."""
        self.auto_start_enabled = enabled
        logger.info(f"[AutoRunner] Auto-start {'enabled' if enabled else 'disabled'}")
    
    def set_min_priority(self, priority: int):
        """Set minimum priority for auto-start."""
        self.min_priority = priority
        logger.info(f"[AutoRunner] Min priority set to {priority}")


# Global runner instance
_runner: Optional[AutonomousExperimentRunner] = None


def get_runner() -> AutonomousExperimentRunner:
    """Get or create the global runner instance."""
    global _runner
    if _runner is None:
        _runner = AutonomousExperimentRunner()
    return _runner


async def start_autonomous_runner():
    """Start the global autonomous runner."""
    runner = get_runner()
    await runner.start()


async def stop_autonomous_runner():
    """Stop the global autonomous runner."""
    runner = get_runner()
    await runner.stop()
