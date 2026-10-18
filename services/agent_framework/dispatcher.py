"""
Topic Dispatcher - Routes Topics to Service Handlers
=====================================================
Central dispatcher that routes pub/sub topics to their service handlers.
"""

import logging
from typing import Dict, Any, Callable, Awaitable

logger = logging.getLogger(__name__)

# Topic constants (matching topic-registry.ts)
TOPICS = {
    # Shared
    "RUN_REQUESTED": "shared.run.requested",
    "RUN_QUEUED": "shared.run.queued",
    "RUN_STARTED": "shared.run.started",
    "RUN_COMPLETED": "shared.run.completed",
    "RUN_FAILED": "shared.run.failed",
    
    # Narrative
    "NARRATIVE_WEEKLY_GENERATE_PLAN": "narrative.weekly.generate_plan",
    "NARRATIVE_DAILY_EXECUTE_SCHEDULE": "narrative.daily.execute_schedule",
    "NARRATIVE_WEEKLY_REFLECT": "narrative.weekly.reflect",
    
    # Experiments
    "EXPERIMENTS_WEEKLY_PLAN": "experiments.weekly.plan_experiments",
    "EXPERIMENTS_DAILY_EXECUTE_VARIANTS": "experiments.daily.execute_variants",
    "EXPERIMENTS_DAILY_ANALYZE_RESULTS": "experiments.daily.analyze_results",
    "EXPERIMENTS_WINNER_DETECTION": "experiments.weekly.winner_detection",
    "EXPERIMENTS_PROMOTE_TO_NARRATIVE": "experiments.weekly.promote_to_narrative",
    
    # Content Mix Planner
    "CONTENT_MIX_GENERATE_PLAN": "content_mix.generate_plan",
    "CONTENT_MIX_ASSIGN_CONTENT": "content_mix.assign_content",
    "CONTENT_MIX_APPROVE_PLAN": "content_mix.approve_plan",
    "CONTENT_MIX_CREATE_CONTENT": "content_mix.create_content",
}

# Handler type
TopicHandler = Callable[[str, Dict[str, Any]], Awaitable[None]]

# Registry of topic handlers
_handlers: Dict[str, TopicHandler] = {}


def register_handler(topic: str, handler: TopicHandler):
    """Register a handler for a topic."""
    _handlers[topic] = handler
    logger.info(f"[Dispatcher] Registered handler for topic: {topic}")


async def dispatch_topic(topic: str, run_id: str, payload: Dict[str, Any]):
    """
    Dispatch a topic to its registered handler.
    
    Args:
        topic: The topic to dispatch (e.g., "narrative.weekly.generate_plan")
        run_id: The run ID for this execution
        payload: The payload to pass to the handler
    """
    logger.info(f"[Dispatcher] Dispatching topic: {topic} for run: {run_id}")
    
    handler = _handlers.get(topic)
    
    if handler:
        await handler(run_id, payload)
    else:
        # Try to find a handler dynamically
        handler = await _get_dynamic_handler(topic)
        if handler:
            await handler(run_id, payload)
        else:
            raise ValueError(f"No handler registered for topic: {topic}")


async def _get_dynamic_handler(topic: str) -> TopicHandler:
    """Get handler dynamically based on topic pattern."""
    
    # Narrative handlers
    if topic == TOPICS["NARRATIVE_WEEKLY_GENERATE_PLAN"]:
        from .services.narrative_service import run_narrative_generate_plan
        return run_narrative_generate_plan
    
    if topic == TOPICS["NARRATIVE_WEEKLY_REFLECT"]:
        from .services.narrative_service import run_narrative_reflect
        return run_narrative_reflect
    
    if topic == TOPICS["NARRATIVE_DAILY_EXECUTE_SCHEDULE"]:
        from .services.narrative_service import run_narrative_execute
        return run_narrative_execute
    
    # Experiments handlers
    if topic == TOPICS["EXPERIMENTS_WEEKLY_PLAN"]:
        from .services.experiments_service import run_experiments_plan
        return run_experiments_plan
    
    if topic == TOPICS["EXPERIMENTS_DAILY_ANALYZE_RESULTS"]:
        from .services.experiments_service import run_experiments_analyze
        return run_experiments_analyze
    
    if topic == TOPICS["EXPERIMENTS_PROMOTE_TO_NARRATIVE"]:
        from .services.experiments_service import run_experiments_promote
        return run_experiments_promote
    
    # Content Mix handlers
    if topic == TOPICS["CONTENT_MIX_GENERATE_PLAN"]:
        from .services.content_mix_service import run_content_mix_generate_plan
        return run_content_mix_generate_plan
    
    if topic == TOPICS["CONTENT_MIX_ASSIGN_CONTENT"]:
        from .services.content_mix_service import run_content_mix_assign_content
        return run_content_mix_assign_content
    
    if topic == TOPICS["CONTENT_MIX_APPROVE_PLAN"]:
        from .services.content_mix_service import run_content_mix_approve_plan
        return run_content_mix_approve_plan
    
    if topic == TOPICS["CONTENT_MIX_CREATE_CONTENT"]:
        from .services.content_mix_service import run_content_mix_create_content
        return run_content_mix_create_content
    
    return None


class TopicDispatcher:
    """
    Class-based dispatcher with dependency injection.
    """
    
    def __init__(self):
        self.handlers: Dict[str, TopicHandler] = {}
    
    def register(self, topic: str, handler: TopicHandler):
        """Register a handler."""
        self.handlers[topic] = handler
    
    async def dispatch(self, topic: str, run_id: str, payload: Dict[str, Any]):
        """Dispatch to handler."""
        handler = self.handlers.get(topic)
        if not handler:
            handler = await _get_dynamic_handler(topic)
        
        if handler:
            await handler(run_id, payload)
        else:
            raise ValueError(f"No handler for topic: {topic}")


# Global dispatcher instance
_dispatcher = TopicDispatcher()


def get_dispatcher() -> TopicDispatcher:
    """Get the global dispatcher."""
    return _dispatcher
