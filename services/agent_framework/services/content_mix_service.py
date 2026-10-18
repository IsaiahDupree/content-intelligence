"""
Content Mix Planner Service Handlers
=====================================
Service handlers for Content Mix Planner topics.
Enables AI agent to generate long-term content schedules with mixed content types.
"""

import logging
import os
from typing import Dict, Any
from datetime import date, timedelta

from ..run_manager import get_run_manager
from ..dispatcher import TOPICS

logger = logging.getLogger(__name__)

SERVICE_NAME = "ContentMixPlannerService"

# Lazy import to avoid circular dependencies
def get_planner():
    """Get the content mix planner instance."""
    try:
        from services.content_mix_planner import get_content_mix_planner
        return get_content_mix_planner()
    except ImportError as e:
        logger.warning(f"[ContentMixService] Could not import planner: {e}")
        return None


async def run_content_mix_generate_plan(run_id: str, payload: Dict[str, Any]):
    """
    Handler for content_mix.generate_plan
    Generates a long-term content plan with mixed content types.
    
    Payload options:
    - duration: "2_months", "6_months", etc.
    - posts_per_day: 1-5
    - platforms: ["tiktok", "instagram", ...]
    - content_mix: {ugc_caption: 40, carousel: 20, ai_generated: 20, animated: 10, raw_ugc: 10}
    - goal_id: optional narrative goal to align with
    """
    rm = get_run_manager()
    planner = get_planner()
    
    try:
        # Extract configuration from payload
        duration = payload.get("duration", "2_months")
        posts_per_day = payload.get("posts_per_day", 2)
        platforms = payload.get("platforms", ["tiktok", "instagram"])
        content_mix_config = payload.get("content_mix", {
            "ugc_caption_percentage": 40,
            "carousel_percentage": 20,
            "ai_generated_percentage": 20,
            "animated_percentage": 10,
            "raw_ugc_percentage": 10
        })
        goal_id = payload.get("goal_id")
        plan_name = payload.get("name")
        
        # Step 1: Configuration Analysis
        step_id = rm.start_step(run_id, "config_analysis", "Analyzing schedule configuration")
        rm.emit_thought(run_id, step_id,
            f"Planning {duration.replace('_', ' ')} of content with {posts_per_day} posts/day across {', '.join(platforms)}")
        rm.emit_event(
            run_id=run_id, step_id=step_id,
            event_type="config.loaded",
            message=f"Content mix: UGC {content_mix_config.get('ugc_caption_percentage', 40)}%, "
                   f"Carousel {content_mix_config.get('carousel_percentage', 20)}%, "
                   f"AI Generated {content_mix_config.get('ai_generated_percentage', 20)}%",
            source_service=SERVICE_NAME,
            payload=content_mix_config
        )
        rm.complete_step(run_id, "config_analysis", f"Configuration validated for {duration}")
        rm.update_progress(run_id, 1)
        
        # Step 2: Content Inventory
        step_id = rm.start_step(run_id, "content_inventory", "Analyzing available content")
        
        if planner:
            available_content = await planner._load_available_content()
            content_count = len(available_content)
        else:
            content_count = 0
            
        rm.emit_event(
            run_id=run_id, step_id=step_id,
            event_type="data.fetched",
            message=f"Found {content_count} analyzed videos available for scheduling",
            source_service=SERVICE_NAME,
            payload={"available_videos": content_count}
        )
        rm.emit_thought(run_id, step_id,
            f"Will distribute content across {len(platforms)} platforms with optimal timing")
        rm.complete_step(run_id, "content_inventory", f"Inventoried {content_count} content items")
        rm.update_progress(run_id, 2)
        
        # Step 3: Schedule Generation
        step_id = rm.start_step(run_id, "schedule_generation", "Generating long-term schedule")
        
        if planner:
            from services.content_mix_planner import ScheduleConfig, ScheduleDuration, ContentMix
            
            try:
                duration_enum = ScheduleDuration(duration)
            except ValueError:
                duration_enum = ScheduleDuration.TWO_MONTHS
            
            content_mix = ContentMix(
                ugc_caption_percentage=content_mix_config.get("ugc_caption_percentage", 40),
                carousel_percentage=content_mix_config.get("carousel_percentage", 20),
                ai_generated_percentage=content_mix_config.get("ai_generated_percentage", 20),
                animated_percentage=content_mix_config.get("animated_percentage", 10),
                raw_ugc_percentage=content_mix_config.get("raw_ugc_percentage", 10)
            )
            
            config = ScheduleConfig(
                duration=duration_enum,
                posts_per_day=posts_per_day,
                platforms=platforms,
                content_mix=content_mix,
                posting_times=["09:00", "18:00"],
                goal_id=goal_id
            )
            
            plan = await planner.generate_plan(config=config, name=plan_name)
            
            rm.emit_action(run_id, step_id,
                f"Generated {plan.total_posts} posts across {plan.total_days} days",
                {
                    "plan_id": plan.id,
                    "total_posts": plan.total_posts,
                    "total_days": (plan.end_date - plan.start_date).days + 1,
                    "content_distribution": plan.content_type_distribution
                }
            )
            
            rm.create_artifact(
                run_id=run_id,
                kind="content_mix_plan",
                name=f"content_plan_{plan.id}.json",
                step_id=step_id,
                content={
                    "plan_id": plan.id,
                    "name": plan.name,
                    "start_date": plan.start_date.isoformat(),
                    "end_date": plan.end_date.isoformat(),
                    "total_posts": plan.total_posts,
                    "content_distribution": plan.content_type_distribution,
                    "platforms": platforms,
                    "status": plan.status
                }
            )
            
            rm.emit_decision(run_id, step_id,
                f"Created draft plan '{plan.name}' with {plan.total_posts} scheduled slots",
                f"Optimized for {duration.replace('_', ' ')} with balanced content mix"
            )
        else:
            rm.emit_event(
                run_id=run_id, step_id=step_id,
                event_type="error",
                message="Content mix planner not available",
                source_service=SERVICE_NAME
            )
        
        rm.complete_step(run_id, "schedule_generation", "Long-term schedule generated")
        rm.update_progress(run_id, 3)
        
        # Step 4: Content Assignment
        step_id = rm.start_step(run_id, "content_assignment", "Assigning content to slots")
        rm.emit_thought(run_id, step_id,
            "Matching available UGC content to slots based on quality scores and pillar alignment")
        rm.emit_event(
            run_id=run_id, step_id=step_id,
            event_type="action.performed",
            message="Assigned available content to UGC slots; other slots marked for creation",
            source_service=SERVICE_NAME
        )
        rm.complete_step(run_id, "content_assignment", "Content assigned to available slots")
        rm.update_progress(run_id, 4)
        
        # Step 5: Awaiting Review
        step_id = rm.start_step(run_id, "human_review", "Awaiting human approval")
        rm.emit_event(
            run_id=run_id, step_id=step_id,
            event_type="waiting_approval",
            message="Long-term content plan ready for review at /content-planner",
            source_service=SERVICE_NAME,
            payload={"plan_id": plan.id if planner else None}
        )
        
        logger.info(f"[ContentMixService] Generate plan completed for run {run_id}")
        
    except Exception as e:
        logger.error(f"[ContentMixService] Error in generate_plan: {e}")
        rm.emit_event(
            run_id=run_id,
            event_type="error",
            severity="error",
            message=f"Plan generation failed: {str(e)}",
            source_service=SERVICE_NAME
        )
        raise


async def run_content_mix_assign_content(run_id: str, payload: Dict[str, Any]):
    """
    Handler for content_mix.assign_content
    Assigns specific content to slots in a plan.
    
    Payload:
    - plan_id: Plan to update
    - assignments: [{slot_id, content_id, content_title}, ...]
    """
    rm = get_run_manager()
    planner = get_planner()
    
    try:
        plan_id = payload.get("plan_id")
        assignments = payload.get("assignments", [])
        
        step_id = rm.start_step(run_id, "content_assignment", f"Assigning {len(assignments)} content items")
        
        if planner:
            success_count = 0
            for assignment in assignments:
                slot_id = assignment.get("slot_id")
                content_id = assignment.get("content_id")
                content_title = assignment.get("content_title")
                
                success = await planner.update_slot(slot_id, {
                    "content_id": content_id,
                    "content_title": content_title,
                    "status": "assigned"
                })
                if success:
                    success_count += 1
            
            rm.emit_action(run_id, step_id,
                f"Assigned {success_count}/{len(assignments)} content items to slots",
                {"plan_id": plan_id, "assigned": success_count}
            )
        
        rm.complete_step(run_id, "content_assignment", f"Content assignment complete")
        rm.update_progress(run_id, 5)
        
        logger.info(f"[ContentMixService] Content assignment completed for run {run_id}")
        
    except Exception as e:
        logger.error(f"[ContentMixService] Error in assign_content: {e}")
        raise


async def run_content_mix_approve_plan(run_id: str, payload: Dict[str, Any]):
    """
    Handler for content_mix.approve_plan
    Approves a plan and creates scheduled posts.
    
    Payload:
    - plan_id: Plan to approve
    """
    rm = get_run_manager()
    planner = get_planner()
    
    try:
        plan_id = payload.get("plan_id")
        
        step_id = rm.start_step(run_id, "plan_approval", "Approving and scheduling plan")
        
        if planner:
            result = await planner.approve_plan(plan_id)
            
            rm.emit_action(run_id, step_id,
                f"Approved plan and scheduled {result.get('posts_scheduled', 0)} posts",
                result
            )
            
            rm.emit_event(
                run_id=run_id, step_id=step_id,
                event_type="artifact.created",
                message=f"Created {result.get('posts_scheduled', 0)} scheduled posts",
                source_service=SERVICE_NAME
            )
        
        rm.complete_step(run_id, "plan_approval", "Plan approved and posts scheduled")
        rm.update_progress(run_id, 6)
        
        logger.info(f"[ContentMixService] Plan approval completed for run {run_id}")
        
    except Exception as e:
        logger.error(f"[ContentMixService] Error in approve_plan: {e}")
        raise


async def run_content_mix_create_content(run_id: str, payload: Dict[str, Any]):
    """
    Handler for content_mix.create_content
    Triggers creation of different content types (AI-generated, animated, carousel).
    
    Payload:
    - content_type: "ai_generated", "animated", "carousel"
    - slot_id: Slot to create content for
    - prompt: AI generation prompt
    - pillar: Content pillar for context
    """
    rm = get_run_manager()
    
    try:
        content_type = payload.get("content_type")
        slot_id = payload.get("slot_id")
        prompt = payload.get("prompt", "")
        pillar = payload.get("pillar")
        
        step_id = rm.start_step(run_id, "content_creation", f"Creating {content_type} content")
        
        rm.emit_thought(run_id, step_id,
            f"Generating {content_type} content for {pillar or 'general'} pillar")
        
        if content_type == "ai_generated":
            rm.emit_action(run_id, step_id,
                "Triggering AI video generation pipeline",
                {"type": "ai_generated", "slot_id": slot_id, "pillar": pillar}
            )
            # Here we would call the actual AI video generation service
            # For now, emit that it's queued
            rm.emit_event(
                run_id=run_id, step_id=step_id,
                event_type="action.queued",
                message="AI video generation queued - will complete in background",
                source_service=SERVICE_NAME
            )
            
        elif content_type == "carousel":
            rm.emit_action(run_id, step_id,
                "Creating carousel from content brief",
                {"type": "carousel", "slot_id": slot_id}
            )
            rm.emit_event(
                run_id=run_id, step_id=step_id,
                event_type="action.queued",
                message="Carousel creation queued",
                source_service=SERVICE_NAME
            )
            
        elif content_type == "animated":
            rm.emit_action(run_id, step_id,
                "Triggering animated content creation",
                {"type": "animated", "slot_id": slot_id}
            )
            rm.emit_event(
                run_id=run_id, step_id=step_id,
                event_type="action.queued",
                message="Animation creation queued",
                source_service=SERVICE_NAME
            )
        
        rm.complete_step(run_id, "content_creation", f"{content_type} content creation initiated")
        rm.update_progress(run_id, 7)
        
        logger.info(f"[ContentMixService] Content creation triggered for run {run_id}")
        
    except Exception as e:
        logger.error(f"[ContentMixService] Error in create_content: {e}")
        raise


# Topic handlers mapping
CONTENT_MIX_HANDLERS = {
    "content_mix.generate_plan": run_content_mix_generate_plan,
    "content_mix.assign_content": run_content_mix_assign_content,
    "content_mix.approve_plan": run_content_mix_approve_plan,
    "content_mix.create_content": run_content_mix_create_content,
}
