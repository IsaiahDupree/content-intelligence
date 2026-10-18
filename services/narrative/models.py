"""
Data models for the AI Narrative Scheduling System
"""

from dataclasses import dataclass, field
from typing import List, Dict, Optional, Any
from datetime import datetime, date
from enum import Enum
import uuid


class PillarType(Enum):
    VALUE = "value"
    PROOF = "proof"
    CTA = "cta"


class TimeHorizon(Enum):
    TODAY = "today"
    NEXT_7_DAYS = "next_7_days"
    NEXT_30_DAYS = "next_30_days"


class PrimaryCTA(Enum):
    SUBSCRIBE = "subscribe"
    WAITLIST = "waitlist"
    DM_KEYWORD = "dm_keyword"
    LINK_CLICK = "link_click"
    PURCHASE = "purchase"
    FOLLOW = "follow"
    SAVE = "save"
    SHARE = "share"


@dataclass
class NarrativeGoal:
    """Defines the overarching narrative and objectives"""
    id: str = field(default_factory=lambda: str(uuid.uuid4()))
    goal_statement: str = ""
    primary_cta: str = "follow"
    target_audience: str = ""
    time_horizon: str = "next_7_days"
    start_date: Optional[date] = None
    end_date: Optional[date] = None
    
    # Success metrics
    target_followers: Optional[int] = None
    target_engagement_rate: Optional[float] = None
    target_conversions: Optional[int] = None
    
    status: str = "active"
    created_at: datetime = field(default_factory=datetime.now)
    
    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "goal_statement": self.goal_statement,
            "primary_cta": self.primary_cta,
            "target_audience": self.target_audience,
            "time_horizon": self.time_horizon,
            "start_date": self.start_date.isoformat() if self.start_date else None,
            "end_date": self.end_date.isoformat() if self.end_date else None,
            "target_followers": self.target_followers,
            "target_engagement_rate": self.target_engagement_rate,
            "target_conversions": self.target_conversions,
            "status": self.status,
        }


@dataclass
class NarrativePillar:
    """Defines a content theme/pillar"""
    id: str = field(default_factory=lambda: str(uuid.uuid4()))
    name: str = ""
    description: str = ""
    pillar_type: str = "value"  # value, proof, cta
    color: str = "#3b82f6"
    keywords: List[str] = field(default_factory=list)
    target_percentage: float = 20.0
    min_posts_per_week: int = 1
    max_posts_per_week: int = 5
    priority: int = 5
    is_active: bool = True
    
    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "description": self.description,
            "pillar_type": self.pillar_type,
            "color": self.color,
            "keywords": self.keywords,
            "target_percentage": self.target_percentage,
            "min_posts_per_week": self.min_posts_per_week,
            "max_posts_per_week": self.max_posts_per_week,
            "priority": self.priority,
            "is_active": self.is_active,
        }


@dataclass
class SchedulingConstraints:
    """Defines scheduling boundaries and rules"""
    id: str = field(default_factory=lambda: str(uuid.uuid4()))
    enabled_platforms: List[str] = field(default_factory=lambda: ["tiktok", "instagram"])
    max_posts_per_day: int = 3
    min_posts_per_day: int = 1
    max_posts_per_platform_per_day: int = 2
    posting_windows: Dict[str, List[str]] = field(default_factory=dict)
    blackout_dates: List[date] = field(default_factory=list)
    timezone: str = "America/New_York"
    min_pre_social_score: int = 60
    require_analysis: bool = True
    max_same_pillar_consecutive: int = 2
    
    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "enabled_platforms": self.enabled_platforms,
            "max_posts_per_day": self.max_posts_per_day,
            "min_posts_per_day": self.min_posts_per_day,
            "max_posts_per_platform_per_day": self.max_posts_per_platform_per_day,
            "posting_windows": self.posting_windows,
            "blackout_dates": [d.isoformat() for d in self.blackout_dates],
            "timezone": self.timezone,
            "min_pre_social_score": self.min_pre_social_score,
            "require_analysis": self.require_analysis,
            "max_same_pillar_consecutive": self.max_same_pillar_consecutive,
        }


@dataclass
class VideoCandidate:
    """A video being considered for scheduling"""
    id: str
    title: str
    file_path: str
    thumbnail_path: Optional[str] = None
    duration_sec: Optional[float] = None
    pre_social_score: Optional[int] = None
    
    # Analysis data
    transcript: Optional[str] = None
    topics: Optional[List[str]] = None
    hooks: Optional[List[str]] = None
    tone: Optional[str] = None
    
    # Pillar classification
    primary_pillar: Optional[str] = None
    secondary_pillar: Optional[str] = None
    pillar_confidence: float = 0.0
    
    # Selection metadata
    selection_reason: Optional[str] = None
    rejection_reason: Optional[str] = None
    is_selected: bool = False
    
    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "title": self.title,
            "file_path": self.file_path,
            "thumbnail_path": self.thumbnail_path,
            "duration_sec": self.duration_sec,
            "pre_social_score": self.pre_social_score,
            "primary_pillar": self.primary_pillar,
            "secondary_pillar": self.secondary_pillar,
            "pillar_confidence": self.pillar_confidence,
            "selection_reason": self.selection_reason,
            "rejection_reason": self.rejection_reason,
            "is_selected": self.is_selected,
        }


@dataclass
class ScheduledSlot:
    """A single scheduled post slot"""
    id: str = field(default_factory=lambda: str(uuid.uuid4()))
    video_id: str = ""
    video_title: str = ""
    platform: str = ""
    scheduled_date: date = None
    scheduled_time: str = ""
    pillar: str = ""
    selection_reason: str = ""
    expected_engagement: Optional[float] = None
    
    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "video_id": self.video_id,
            "video_title": self.video_title,
            "platform": self.platform,
            "scheduled_date": self.scheduled_date.isoformat() if self.scheduled_date else None,
            "scheduled_time": self.scheduled_time,
            "pillar": self.pillar,
            "selection_reason": self.selection_reason,
            "expected_engagement": self.expected_engagement,
        }


@dataclass
class ReasoningStep:
    """A single step in the AI reasoning chain"""
    step_number: int
    thought: str
    decision: str
    confidence: float = 0.8
    data_referenced: Optional[Dict[str, Any]] = None
    
    def to_dict(self) -> Dict[str, Any]:
        return {
            "step": self.step_number,
            "thought": self.thought,
            "decision": self.decision,
            "confidence": self.confidence,
            "data_referenced": self.data_referenced,
        }


@dataclass
class WeeklyPlan:
    """A complete 7-day content plan"""
    id: str = field(default_factory=lambda: str(uuid.uuid4()))
    goal_id: str = ""
    week_start: date = None
    week_end: date = None
    
    # Schedule
    scheduled_slots: List[ScheduledSlot] = field(default_factory=list)
    
    # Reasoning
    reasoning_chain: List[ReasoningStep] = field(default_factory=list)
    
    # Statistics
    total_posts: int = 0
    pillar_distribution: Dict[str, int] = field(default_factory=dict)
    platform_distribution: Dict[str, int] = field(default_factory=dict)
    
    # Justification
    justification_summary: str = ""
    
    # Status
    status: str = "draft"  # draft, approved, executing, completed
    created_at: datetime = field(default_factory=datetime.now)
    
    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "goal_id": self.goal_id,
            "week_start": self.week_start.isoformat() if self.week_start else None,
            "week_end": self.week_end.isoformat() if self.week_end else None,
            "scheduled_slots": [s.to_dict() for s in self.scheduled_slots],
            "reasoning_chain": [r.to_dict() for r in self.reasoning_chain],
            "total_posts": self.total_posts,
            "pillar_distribution": self.pillar_distribution,
            "platform_distribution": self.platform_distribution,
            "justification_summary": self.justification_summary,
            "status": self.status,
            "created_at": self.created_at.isoformat(),
        }


@dataclass
class PerformanceMetrics:
    """Performance data for a completed schedule"""
    schedule_id: str
    week_start: date
    week_end: date
    
    # Aggregate metrics
    total_posts: int = 0
    total_views: int = 0
    total_likes: int = 0
    total_comments: int = 0
    total_shares: int = 0
    avg_engagement_rate: float = 0.0
    
    # Goal progress
    followers_gained: int = 0
    conversions: int = 0
    goal_progress_pct: float = 0.0
    
    # Pillar performance
    pillar_performance: Dict[str, Dict[str, Any]] = field(default_factory=dict)
    
    def to_dict(self) -> Dict[str, Any]:
        return {
            "schedule_id": self.schedule_id,
            "week_start": self.week_start.isoformat(),
            "week_end": self.week_end.isoformat(),
            "total_posts": self.total_posts,
            "total_views": self.total_views,
            "total_likes": self.total_likes,
            "total_comments": self.total_comments,
            "total_shares": self.total_shares,
            "avg_engagement_rate": self.avg_engagement_rate,
            "followers_gained": self.followers_gained,
            "conversions": self.conversions,
            "goal_progress_pct": self.goal_progress_pct,
            "pillar_performance": self.pillar_performance,
        }


@dataclass
class Learning:
    """A learning derived from performance analysis"""
    id: str = field(default_factory=lambda: str(uuid.uuid4()))
    learning_type: str = ""  # content_format, pillar_performance, timing, platform
    insight: str = ""
    confidence: float = 0.0
    action: str = ""
    source_schedule_id: str = ""
    applied: bool = False
    created_at: datetime = field(default_factory=datetime.now)
    
    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "learning_type": self.learning_type,
            "insight": self.insight,
            "confidence": self.confidence,
            "action": self.action,
            "source_schedule_id": self.source_schedule_id,
            "applied": self.applied,
            "created_at": self.created_at.isoformat(),
        }
