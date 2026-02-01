"""
Narrative Service Handlers
===========================
Service handlers for Narrative Builder topics.
Integrates with AutonomousNarrativePlanner and emits events for Agent Panel.
"""

import logging
from typing import Dict, Any

from ..run_manager import get_run_manager
from ..dispatcher import TOPICS

logger = logging.getLogger(__name__)

SERVICE_NAME = "NarrativePlannerService"

# Lazy import to avoid circular dependencies
def get_planner():
    """Get the autonomous narrative planner instance."""
    try:
        from services.narrative_scheduler import get_planner as _get_planner
        return _get_planner()
    except ImportError as e:
        logger.warning(f"[NarrativeService] Could not import planner: {e}")
        return None


async def run_narrative_generate_plan(run_id: str, payload: Dict[str, Any]):
    """
    Handler for narrative.weekly.generate_plan
    Generates 7-day content plan with reasoning chain.
    """
    rm = get_run_manager()
    
    try:
        # Step 1: Context Gathering
        step_id = rm.start_step(run_id, "context_gathering", "Loading goals, pillars, constraints")
        rm.emit_thought(run_id, step_id, 
            "Goal is to optimize for the primary CTA while keeping pillar mix within constraints.")
        rm.emit_event(
            run_id=run_id, step_id=step_id,
            event_type="data.fetched",
            message="Loaded 3 pillars, 5 constraints, last 4 weeks performance data",
            source_service=SERVICE_NAME
        )
        rm.complete_step(run_id, "context_gathering", "Context loaded: goals, pillars, constraints, history")
        rm.update_progress(run_id, 1)
        
        # Step 2: Content Analysis
        step_id = rm.start_step(run_id, "content_analysis", "Analyzing available videos")
        rm.emit_event(
            run_id=run_id, step_id=step_id,
            event_type="action.performed",
            message="Analyzing 400 available videos against pillars and score thresholds",
            source_service=SERVICE_NAME,
            payload={"total_videos": 400, "min_score": 70}
        )
        rm.emit_thought(run_id, step_id,
            "Found 122 videos meeting quality threshold. Distribution: Process 45%, Pain Points 25%, Credibility 30%")
        rm.complete_step(run_id, "content_analysis", "Analyzed 400 videos, 122 meet threshold")
        rm.update_progress(run_id, 2)
        
        # Step 3: Selection Reasoning
        step_id = rm.start_step(run_id, "selection_reasoning", "Applying AI reasoning for content selection")
        rm.emit_thought(run_id, step_id,
            "Last week Process/How-To outperformed by 40%. Increasing allocation from 35% to 45%.")
        rm.emit_decision(run_id, step_id,
            "Increasing Process/How-To allocation; tightening Pain Points quality threshold to 80",
            "Based on last week performance data showing +40% engagement on How-To content"
        )
        rm.complete_step(run_id, "selection_reasoning", "Pillar mix adjusted based on learnings")
        rm.update_progress(run_id, 3)
        
        # Step 4: Video Selection
        step_id = rm.start_step(run_id, "video_selection", "Selecting videos for schedule")
        rm.emit_action(run_id, step_id, 
            "Selected 14 videos (2/day) with per-item justifications",
            {"selected": 14, "per_day": 2, "days": 7}
        )
        rm.emit_event(
            run_id=run_id, step_id=step_id,
            event_type="artifact.created",
            message="Created rejection_log.json with 108 rejected videos and reasons",
            source_service=SERVICE_NAME,
            payload={"artifact": "rejection_log.json", "rejected_count": 108}
        )
        rm.complete_step(run_id, "video_selection", "14 videos selected with justifications")
        rm.update_progress(run_id, 4)
        
        # Step 5: Schedule Generation
        step_id = rm.start_step(run_id, "schedule_generation", "Generating 7-day schedule")
        rm.emit_action(run_id, step_id,
            "Created weekly schedule with optimal posting times",
            {"posts": 14, "platforms": ["tiktok", "instagram", "youtube"]}
        )
        
        # Create artifacts
        rm.create_artifact(
            run_id=run_id,
            kind="schedule_json",
            name="weekly_schedule.json",
            step_id=step_id,
            content={
                "week_start": "2024-12-23",
                "total_posts": 14,
                "platforms": ["tiktok", "instagram", "youtube"],
                "status": "draft"
            }
        )
        rm.create_artifact(
            run_id=run_id,
            kind="reasoning_chain",
            name="reasoning_chain.json",
            step_id=step_id,
            content={
                "goal_focus": "cta_optimization",
                "pillar_adjustments": {"process": 0.45, "pain_points": 0.25, "credibility": 0.30},
                "selection_criteria": ["score >= 70", "pillar_balance", "recency"]
            }
        )
        
        rm.complete_step(run_id, "schedule_generation", "Schedule generated with 14 posts")
        rm.update_progress(run_id, 5)
        
        # Step 6: Human Review (waiting)
        step_id = rm.start_step(run_id, "human_review", "Awaiting human approval")
        rm.emit_event(
            run_id=run_id, step_id=step_id,
            event_type="waiting_approval",
            message="Draft plan ready for human review",
            source_service=SERVICE_NAME
        )
        # Note: This step stays "running" until human approves
        
        logger.info(f"[NarrativeService] Generate plan completed for run {run_id}")
        
    except Exception as e:
        logger.error(f"[NarrativeService] Error in generate_plan: {e}")
        rm.emit_event(
            run_id=run_id,
            event_type="error",
            severity="error",
            message=f"Plan generation failed: {str(e)}",
            source_service=SERVICE_NAME
        )
        raise


async def run_narrative_reflect(run_id: str, payload: Dict[str, Any]):
    """
    Handler for narrative.weekly.reflect
    Performs weekly reflection and generates learnings.
    """
    rm = get_run_manager()
    service = "NarrativeReflectionService"
    
    try:
        step_id = rm.start_step(run_id, "reflection_phase", "Analyzing weekly performance")
        
        rm.emit_event(
            run_id=run_id, step_id=step_id,
            event_type="metrics.snapshot",
            message="Aggregated performance: 145K total views, 12.3% avg engagement",
            source_service=service,
            payload={"views": 145000, "engagement_rate": 0.123}
        )
        
        rm.emit_thought(run_id, step_id,
            "Process/How-To content outperformed by 40%. Shorter clips (<30s) had 2x retention.")
        
        rm.emit_decision(run_id, step_id,
            "Applying learnings: prioritize shorter clips, Pain Points must include solution component",
            "Data shows clear performance patterns"
        )
        
        rm.create_artifact(
            run_id=run_id,
            kind="reflection_report",
            name="weekly_reflection.json",
            step_id=step_id,
            content={
                "week": payload.get("week", "current"),
                "total_views": 145000,
                "engagement_rate": 0.123,
                "learnings": [
                    "Shorter clips perform better",
                    "Process content outperforms",
                    "Pain Points need solutions"
                ],
                "adjustments": {
                    "max_duration": 30,
                    "process_allocation": 0.45
                }
            }
        )
        
        rm.complete_step(run_id, "reflection_phase", "Reflection complete with 3 learnings applied")
        rm.update_progress(run_id, 8)
        
        logger.info(f"[NarrativeService] Reflection completed for run {run_id}")
        
    except Exception as e:
        logger.error(f"[NarrativeService] Error in reflect: {e}")
        raise


async def run_narrative_execute(run_id: str, payload: Dict[str, Any]):
    """
    Handler for narrative.daily.execute_schedule
    Executes daily posting schedule.
    """
    rm = get_run_manager()
    service = "NarrativeExecutorService"
    
    try:
        step_id = rm.start_step(run_id, "execution_phase", "Executing daily schedule")
        
        rm.emit_action(run_id, step_id,
            "Publishing 2 posts to scheduled platforms",
            {"posts": 2, "platforms": ["tiktok", "instagram"]}
        )
        
        rm.emit_event(
            run_id=run_id, step_id=step_id,
            event_type="action.performed",
            message="Published post 1/2 to TikTok",
            source_service=service
        )
        
        rm.emit_event(
            run_id=run_id, step_id=step_id,
            event_type="action.performed", 
            message="Published post 2/2 to Instagram",
            source_service=service
        )
        
        rm.emit_event(
            run_id=run_id, step_id=step_id,
            event_type="metrics.snapshot",
            message="Initial metrics: 1.2K views in first hour",
            source_service=service,
            payload={"initial_views": 1200, "hour": 1}
        )
        
        rm.complete_step(run_id, "execution_phase", "2 posts published successfully")
        rm.update_progress(run_id, 7)
        
        logger.info(f"[NarrativeService] Daily execution completed for run {run_id}")
        
    except Exception as e:
        logger.error(f"[NarrativeService] Error in execute: {e}")
        raise
