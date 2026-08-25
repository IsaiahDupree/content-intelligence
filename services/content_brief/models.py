"""
Enhanced Content Brief Models
=============================
Data models for enhanced content brief system.
"""

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Dict, Optional, Any, List
from uuid import UUID, uuid4


class BriefStatus(str, Enum):
    """Brief status."""
    DRAFT = "draft"
    SCORED = "scored"
    APPROVED = "approved"
    IN_PRODUCTION = "in_production"
    COMPLETED = "completed"
    REJECTED = "rejected"
    BLOCKED_QUALITY = "blocked_quality"


@dataclass
class BriefScore:
    """"Worth Covering" score breakdown (0-100)."""
    total: float  # 0-100
    velocity: float = 0.0  # 0-25: Views/hour growth, shares/saves rate, comment velocity
    intent: float = 0.0  # 0-20: "How do I...", "What tool...", "Template?", "Link?", "Price?"
    product_fit: float = 0.0  # 0-25: Can you point to service/product/lead magnet?
    differentiation: float = 0.0  # 0-15: Can you add unique lens?
    production_feasibility: float = 0.0  # 0-15: Can you produce it fast at quality bar?
    
    def is_worth_covering(self, threshold: float = 70.0) -> bool:
        """Check if score meets threshold."""
        return self.total >= threshold


@dataclass
class TrendCard:
    """Raw trend input card."""
    trend_id: str
    trend_type: str  # "hashtag", "sound", "topic", "cluster"
    trend_name: str
    platform: str  # "instagram", "tiktok", "youtube", etc.
    niche_tags: List[str] = field(default_factory=list)
    
    # Velocity signals
    views_growth: float = 0.0  # Views/hour growth
    likes_per_min: float = 0.0
    shares_save_rate: float = 0.0
    comment_rate: float = 0.0
    
    # Context
    what_people_achieve: Optional[str] = None
    what_people_stuck_on: Optional[str] = None
    
    # Evidence
    top_comments: List[str] = field(default_factory=list)
    repeated_questions: List[str] = field(default_factory=list)
    creator_hooks: List[str] = field(default_factory=list)
    
    # Format
    format: Optional[str] = None  # "talking_head", "screen_record", "meme_edit", "explainer", "listicle"
    
    # Metadata
    collected_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))


@dataclass
class TrendCluster:
    """Clustered trends (merged duplicates across platforms)."""
    cluster_id: str
    name: str
    trends: List[TrendCard] = field(default_factory=list)
    
    # Summary
    what_changed: Optional[str] = None
    why_people_care: Optional[str] = None
    what_debate: Optional[str] = None
    
    # Aggregated metrics
    total_views: int = 0
    avg_velocity: float = 0.0
    avg_intent_score: float = 0.0


@dataclass
class BriefAngle:
    """Content angle (niche convergence)."""
    angle_id: str
    cluster_id: str
    
    # Angle components
    audience_role: str  # "creator", "ecom_owner", "dev", "marketer", "student"
    intent: str  # "learn", "compare", "buy", "fix", "copy", "avoid"
    stakes: str  # "time", "money", "reputation", "speed", "simplicity"
    format: str  # "myth_bust", "teardown", "tutorial", "checklist", "story", "case_study"
    
    # Generated content
    promise: str  # Main promise of the angle
    unique_lens: str  # What makes this angle unique
    convergence_pattern: str  # e.g., "Problem × Tool", "Niche × Constraint"
    
    # Scoring
    score: Optional[BriefScore] = None


@dataclass
class ScriptBeat:
    """A beat in the script."""
    id: str
    t: str  # Time range, e.g., "0-2", "2-12"
    text: str  # Script text
    intent: str  # "hook", "problem", "solution", "proof", "cta", "example"
    on_screen: List[str] = field(default_factory=list)  # Keywords to show on screen
    visual_style: Optional[str] = None  # "big_text_punch_in", "diagram", "meme", etc.
    emphasis_words: List[str] = field(default_factory=list)  # Words to emphasize


@dataclass
class ScriptOutput:
    """Generated script.json output."""
    brief_id: str
    title: str
    hook: str
    segments: List[ScriptBeat] = field(default_factory=list)
    metadata: Dict[str, Any] = field(default_factory=dict)


@dataclass
class VisualPlan:
    """Visual plan for the video."""
    brief_id: str
    shots: List[Dict[str, Any]] = field(default_factory=list)  # Shot specifications


@dataclass
class EnhancedBrief:
    """Enhanced content brief with scoring and full pipeline support."""
    brief_id: str
    status: BriefStatus = BriefStatus.DRAFT
    
    # Source
    cluster: Optional[TrendCluster] = None
    angle: Optional[BriefAngle] = None
    
    # Scoring
    score: Optional[BriefScore] = None
    worth_covering: bool = False
    
    # Content
    title: Optional[str] = None
    hook: Optional[str] = None
    promise: Optional[str] = None
    unique_lens: Optional[str] = None
    
    # Video spec
    format: str = "shorts"  # "shorts", "reels", "tiktok", "longform"
    length_sec: int = 45
    hook_sec: float = 1.2
    pattern_interrupt_sec: float = 4.0
    
    # Script
    script_beats: List[ScriptBeat] = field(default_factory=list)
    script_json: Optional[ScriptOutput] = None
    
    # Visual plan
    visual_plan: Optional[VisualPlan] = None
    
    # CTA
    cta: Optional[Dict[str, Any]] = None
    
    # Metadata
    niche: Optional[str] = None
    platform: Optional[str] = None
    generated_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    expires_at: Optional[datetime] = None
    
    def __post_init__(self):
        """Generate brief_id if not provided."""
        if not self.brief_id:
            self.brief_id = str(uuid4())
