"""
Twitter Feedback Loop Agent
AI-driven content generation with Awareness × FATE framework.
"""

from .templates import (
    Template,
    TEMPLATE_LIBRARY,
    AwarenessLevel,
    CTAStrength,
    FATEWeights,
    get_templates_by_awareness,
    get_template_by_id,
)
from .models import (
    Brand,
    Offer,
    ICP,
    CreatorProfile,
    ContentPlan,
    Slot,
    PromptRun,
    Post,
    MetricsSnapshot,
    Score,
)
from .generator import ContentGenerator
from .scorer import PostScorer, ScoringMode

__all__ = [
    "Template",
    "TEMPLATE_LIBRARY",
    "AwarenessLevel",
    "CTAStrength",
    "FATEWeights",
    "get_templates_by_awareness",
    "get_template_by_id",
    "Brand",
    "Offer",
    "ICP",
    "CreatorProfile",
    "ContentPlan",
    "Slot",
    "PromptRun",
    "Post",
    "MetricsSnapshot",
    "Score",
    "ContentGenerator",
    "PostScorer",
    "ScoringMode",
]
