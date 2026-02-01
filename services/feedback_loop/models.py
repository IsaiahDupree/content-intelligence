"""
Data models for Twitter Feedback Loop Agent.
Supports interchangeable Brands → Offers → ICPs structure.
"""

from dataclasses import dataclass, field
from typing import List, Dict, Optional, Any
from datetime import datetime, date
from enum import Enum
import uuid


class ScoringMode(Enum):
    FOLLOWERS = "followers"
    LEADS = "leads"
    PURCHASES = "purchases"


class SlotStatus(Enum):
    PLANNED = "planned"
    GENERATED = "generated"
    PUBLISHED = "published"
    ANALYZED = "analyzed"


class AllocationType(Enum):
    EXPLOIT = "exploit"      # 70% - use winning templates
    EXPLORE = "explore"      # 20% - promising but under-tested
    EXPERIMENT = "experiment" # 10% - new experiments


@dataclass
class CreatorProfile:
    """Personal brand voice used across all brands/offers."""
    creator_id: str
    name: str
    voice_rules: Dict[str, Any] = field(default_factory=dict)
    banned_phrases: List[str] = field(default_factory=list)
    tone_descriptors: List[str] = field(default_factory=list)
    proof_sources: List[str] = field(default_factory=list)
    
    def to_dict(self) -> Dict:
        return {
            "creator_id": self.creator_id,
            "name": self.name,
            "voice_rules": self.voice_rules,
            "banned_phrases": self.banned_phrases,
            "tone_descriptors": self.tone_descriptors,
            "proof_sources": self.proof_sources
        }


@dataclass
class ICP:
    """Ideal Customer Profile - audience segment tied to an offer."""
    icp_id: str
    offer_id: str
    name: str
    description: str = ""
    pains: List[str] = field(default_factory=list)
    desired_outcomes: List[str] = field(default_factory=list)
    objections: List[str] = field(default_factory=list)
    awareness_distribution: Dict[str, float] = field(default_factory=dict)
    language_to_use: List[str] = field(default_factory=list)
    language_to_avoid: List[str] = field(default_factory=list)
    example_hooks: List[str] = field(default_factory=list)
    
    def to_dict(self) -> Dict:
        return {
            "icp_id": self.icp_id,
            "offer_id": self.offer_id,
            "name": self.name,
            "description": self.description,
            "pains": self.pains,
            "desired_outcomes": self.desired_outcomes,
            "objections": self.objections,
            "awareness_distribution": self.awareness_distribution,
            "language_to_use": self.language_to_use,
            "language_to_avoid": self.language_to_avoid,
            "example_hooks": self.example_hooks
        }


@dataclass
class Offer:
    """Sellable unit: waitlist, subscription, trial, course, service, etc."""
    offer_id: str
    brand_id: str
    name: str
    offer_type: str  # trial, waitlist, subscription, service, course, product
    promise: str
    cta_primary: str
    landing_url: str
    cta_secondary: str = ""
    shortlink_domain: str = ""
    who_for: str = ""
    who_not_for: str = ""
    price: str = ""
    pricing_model: str = ""
    icps: List[ICP] = field(default_factory=list)
    
    def to_dict(self) -> Dict:
        return {
            "offer_id": self.offer_id,
            "brand_id": self.brand_id,
            "name": self.name,
            "offer_type": self.offer_type,
            "promise": self.promise,
            "cta_primary": self.cta_primary,
            "cta_secondary": self.cta_secondary,
            "landing_url": self.landing_url,
            "shortlink_domain": self.shortlink_domain,
            "who_for": self.who_for,
            "who_not_for": self.who_not_for,
            "price": self.price,
            "pricing_model": self.pricing_model,
            "icps": [icp.to_dict() for icp in self.icps]
        }


@dataclass
class Brand:
    """Business identity (e.g., EverReach, MatrixLoop, KeywordRadar, BlankLogo)."""
    brand_id: str
    name: str
    positioning: str
    tagline: str = ""
    allowed_topics: List[str] = field(default_factory=list)
    disallowed_topics: List[str] = field(default_factory=list)
    style_overrides: Dict[str, Any] = field(default_factory=dict)
    offers: List[Offer] = field(default_factory=list)
    
    def to_dict(self) -> Dict:
        return {
            "brand_id": self.brand_id,
            "name": self.name,
            "positioning": self.positioning,
            "tagline": self.tagline,
            "allowed_topics": self.allowed_topics,
            "disallowed_topics": self.disallowed_topics,
            "style_overrides": self.style_overrides,
            "offers": [offer.to_dict() for offer in self.offers]
        }


@dataclass
class Slot:
    """A planned content slot in the weekly content plan."""
    slot_id: str
    scheduled_time: datetime
    awareness_level: str
    fate_target: Dict[str, float] = field(default_factory=dict)
    template_id: Optional[str] = None
    target_offer_ids: List[str] = field(default_factory=list)
    target_icp_ids: List[str] = field(default_factory=list)
    allocation_type: AllocationType = AllocationType.EXPLOIT
    constraints: Dict[str, Any] = field(default_factory=dict)
    status: SlotStatus = SlotStatus.PLANNED
    
    def to_dict(self) -> Dict:
        return {
            "slot_id": self.slot_id,
            "scheduled_time": self.scheduled_time.isoformat(),
            "awareness_level": self.awareness_level,
            "fate_target": self.fate_target,
            "template_id": self.template_id,
            "target_offer_ids": self.target_offer_ids,
            "target_icp_ids": self.target_icp_ids,
            "allocation_type": self.allocation_type.value,
            "constraints": self.constraints,
            "status": self.status.value
        }


@dataclass
class ContentPlan:
    """Weekly content plan with slots."""
    content_plan_id: str
    week_start: date
    week_end: date
    slots: List[Slot] = field(default_factory=list)
    allocation: Dict[str, float] = field(default_factory=lambda: {
        "problem_solution_aware": 0.4,
        "authority": 0.3,
        "tribe_identity": 0.2,
        "direct_offer": 0.1
    })
    
    def to_dict(self) -> Dict:
        return {
            "content_plan_id": self.content_plan_id,
            "week_start": self.week_start.isoformat(),
            "week_end": self.week_end.isoformat(),
            "slots": [slot.to_dict() for slot in self.slots],
            "allocation": self.allocation
        }


@dataclass
class PromptRun:
    """Record of a single AI generation run."""
    prompt_run_id: str
    template_id: str
    slot_id: Optional[str] = None
    inputs: Dict[str, str] = field(default_factory=dict)
    model_info: Dict[str, Any] = field(default_factory=lambda: {
        "model": "gpt-4o",
        "temperature": 0.7,
        "max_tokens": 500
    })
    generated_text: str = ""
    variants: List[str] = field(default_factory=list)
    quality_checks: Dict[str, Any] = field(default_factory=dict)
    created_at: datetime = field(default_factory=datetime.now)
    
    def to_dict(self) -> Dict:
        return {
            "prompt_run_id": self.prompt_run_id,
            "template_id": self.template_id,
            "slot_id": self.slot_id,
            "inputs": self.inputs,
            "model_info": self.model_info,
            "generated_text": self.generated_text,
            "variants": self.variants,
            "quality_checks": self.quality_checks,
            "created_at": self.created_at.isoformat()
        }


@dataclass
class Post:
    """Published post with full attribution."""
    post_id: str
    post_url: str
    prompt_run_id: str
    published_at: datetime
    brand_id: str
    offer_id: str
    icp_id: str
    shortlink_id: str = ""
    shortlink_url: str = ""
    post_text: str = ""
    is_thread: bool = False
    thread_ids: List[str] = field(default_factory=list)
    
    def to_dict(self) -> Dict:
        return {
            "post_id": self.post_id,
            "post_url": self.post_url,
            "prompt_run_id": self.prompt_run_id,
            "published_at": self.published_at.isoformat(),
            "brand_id": self.brand_id,
            "offer_id": self.offer_id,
            "icp_id": self.icp_id,
            "shortlink_id": self.shortlink_id,
            "shortlink_url": self.shortlink_url,
            "post_text": self.post_text,
            "is_thread": self.is_thread,
            "thread_ids": self.thread_ids
        }


@dataclass
class MetricsSnapshot:
    """Engagement metrics at a point in time."""
    snapshot_id: str
    post_id: str
    pulled_at: datetime
    snapshot_type: str  # 1h, 6h, 24h, 72h, 7d
    metrics: Dict[str, int] = field(default_factory=lambda: {
        "impressions": 0,
        "likes": 0,
        "replies": 0,
        "reposts": 0,
        "quotes": 0,
        "profile_clicks": 0,
        "url_clicks": 0,
        "follows": 0
    })
    link_metrics: Dict[str, int] = field(default_factory=lambda: {
        "shortlink_clicks": 0,
        "landing_views": 0,
        "conversions": 0
    })
    
    def to_dict(self) -> Dict:
        return {
            "snapshot_id": self.snapshot_id,
            "post_id": self.post_id,
            "pulled_at": self.pulled_at.isoformat(),
            "snapshot_type": self.snapshot_type,
            "metrics": self.metrics,
            "link_metrics": self.link_metrics
        }


@dataclass
class Score:
    """Computed score for a post."""
    post_id: str
    scoring_mode: ScoringMode = ScoringMode.LEADS
    score_1h: float = 0.0
    score_6h: float = 0.0
    score_24h: float = 0.0
    score_72h: float = 0.0
    score_7d: float = 0.0
    final_score: float = 0.0
    rates: Dict[str, float] = field(default_factory=lambda: {
        "like_rate": 0.0,
        "reply_rate": 0.0,
        "repost_rate": 0.0,
        "click_rate": 0.0,
        "follow_rate": 0.0
    })
    labels: List[str] = field(default_factory=list)
    blame_notes: Dict[str, str] = field(default_factory=dict)
    
    def to_dict(self) -> Dict:
        return {
            "post_id": self.post_id,
            "scoring_mode": self.scoring_mode.value,
            "score_1h": self.score_1h,
            "score_6h": self.score_6h,
            "score_24h": self.score_24h,
            "score_72h": self.score_72h,
            "score_7d": self.score_7d,
            "final_score": self.final_score,
            "rates": self.rates,
            "labels": self.labels,
            "blame_notes": self.blame_notes
        }


@dataclass
class TemplateLeaderboard:
    """Performance stats for a template."""
    template_id: str
    total_posts: int = 0
    avg_score_24h: float = 0.0
    avg_score_7d: float = 0.0
    early_velocity: float = 0.0
    variance: float = 0.0
    best_offers: List[str] = field(default_factory=list)
    best_icps: List[str] = field(default_factory=list)
    win_rate: float = 0.0
    last_updated: datetime = field(default_factory=datetime.now)
    
    def to_dict(self) -> Dict:
        return {
            "template_id": self.template_id,
            "total_posts": self.total_posts,
            "avg_score_24h": self.avg_score_24h,
            "avg_score_7d": self.avg_score_7d,
            "early_velocity": self.early_velocity,
            "variance": self.variance,
            "best_offers": self.best_offers,
            "best_icps": self.best_icps,
            "win_rate": self.win_rate,
            "last_updated": self.last_updated.isoformat()
        }


def generate_id(prefix: str = "") -> str:
    """Generate a unique ID with optional prefix."""
    return f"{prefix}{uuid.uuid4().hex[:12]}"
