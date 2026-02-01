"""
Data models for Trend Intelligence System
"""
from dataclasses import dataclass, field
from typing import Optional, List, Dict, Any
from datetime import datetime
from enum import Enum


class Platform(str, Enum):
    TIKTOK = "tiktok"
    INSTAGRAM = "instagram"
    YOUTUBE = "youtube"
    THREADS = "threads"
    TWITTER = "twitter"


class ClusterType(str, Enum):
    PHRASE = "phrase"
    TOPIC = "topic"
    SOUND = "sound"
    FORMAT = "format"
    HASHTAG = "hashtag"
    HOOK = "hook"


class TrendStatus(str, Enum):
    EMERGING = "emerging"
    RISING = "rising"
    PEAK = "peak"
    DECLINING = "declining"
    DEAD = "dead"


class BriefStatus(str, Enum):
    DRAFT = "draft"
    READY = "ready"
    USED = "used"
    ARCHIVED = "archived"


class RenderStatus(str, Enum):
    QUEUED = "queued"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"


@dataclass
class PostMetrics:
    views: int = 0
    likes: int = 0
    comments: int = 0
    shares: int = 0
    saves: int = 0
    
    @property
    def engagement(self) -> int:
        return self.likes + self.comments + self.shares + self.saves
    
    @property
    def engagement_rate(self) -> float:
        if self.views == 0:
            return 0.0
        return self.engagement / self.views
    
    def to_dict(self) -> Dict:
        return {
            "views": self.views,
            "likes": self.likes,
            "comments": self.comments,
            "shares": self.shares,
            "saves": self.saves,
            "engagement": self.engagement,
            "engagement_rate": self.engagement_rate,
        }


@dataclass
class AudioRef:
    sound_id: Optional[str] = None
    title: Optional[str] = None
    creator: Optional[str] = None
    is_original: bool = False
    
    def to_dict(self) -> Dict:
        return {
            "sound_id": self.sound_id,
            "title": self.title,
            "creator": self.creator,
            "is_original": self.is_original,
        }


@dataclass
class PostRaw:
    """Normalized post from any platform"""
    id: Optional[str] = None
    workspace_id: str = "00000000-0000-0000-0000-000000000001"
    platform: Platform = Platform.INSTAGRAM
    platform_post_id: str = ""
    author_handle: str = ""
    author_id: Optional[str] = None
    author_followers: int = 0
    posted_at: Optional[datetime] = None
    fetched_at: Optional[datetime] = None
    caption_text: str = ""
    hashtags: List[str] = field(default_factory=list)
    metrics: PostMetrics = field(default_factory=PostMetrics)
    audio_ref: Optional[AudioRef] = None
    media_type: str = "video"
    permalink: str = ""
    language: str = "en"
    extra: Dict[str, Any] = field(default_factory=dict)
    
    def to_dict(self) -> Dict:
        return {
            "id": self.id,
            "workspace_id": self.workspace_id,
            "platform": self.platform.value if isinstance(self.platform, Platform) else self.platform,
            "platform_post_id": self.platform_post_id,
            "author_handle": self.author_handle,
            "author_id": self.author_id,
            "author_followers": self.author_followers,
            "posted_at": self.posted_at.isoformat() if self.posted_at else None,
            "fetched_at": self.fetched_at.isoformat() if self.fetched_at else None,
            "caption_text": self.caption_text,
            "hashtags": self.hashtags,
            "metrics": self.metrics.to_dict() if isinstance(self.metrics, PostMetrics) else self.metrics,
            "audio_ref": self.audio_ref.to_dict() if self.audio_ref else None,
            "media_type": self.media_type,
            "permalink": self.permalink,
            "language": self.language,
            "extra": self.extra,
        }


@dataclass
class WorkspaceSource:
    """What to track for a workspace"""
    id: Optional[str] = None
    workspace_id: str = "00000000-0000-0000-0000-000000000001"
    platform: Platform = Platform.INSTAGRAM
    niche: str = ""
    seed_accounts: List[str] = field(default_factory=list)
    seed_keywords: List[str] = field(default_factory=list)
    seed_hashtags: List[str] = field(default_factory=list)
    is_enabled: bool = True
    last_synced_at: Optional[datetime] = None
    sync_frequency_hours: int = 24
    
    def to_dict(self) -> Dict:
        return {
            "id": self.id,
            "workspace_id": self.workspace_id,
            "platform": self.platform.value if isinstance(self.platform, Platform) else self.platform,
            "niche": self.niche,
            "seed_accounts": self.seed_accounts,
            "seed_keywords": self.seed_keywords,
            "seed_hashtags": self.seed_hashtags,
            "is_enabled": self.is_enabled,
            "last_synced_at": self.last_synced_at.isoformat() if self.last_synced_at else None,
            "sync_frequency_hours": self.sync_frequency_hours,
        }


@dataclass
class TrendCluster:
    """A cluster of related posts representing a trend"""
    id: Optional[str] = None
    workspace_id: str = "00000000-0000-0000-0000-000000000001"
    cluster_type: ClusterType = ClusterType.TOPIC
    title: str = ""
    description: str = ""
    platform: Optional[str] = None
    niche: Optional[str] = None
    status: TrendStatus = TrendStatus.EMERGING
    confidence: float = 0.5
    post_ids: List[str] = field(default_factory=list)
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None
    
    def to_dict(self) -> Dict:
        return {
            "id": self.id,
            "workspace_id": self.workspace_id,
            "cluster_type": self.cluster_type.value if isinstance(self.cluster_type, ClusterType) else self.cluster_type,
            "title": self.title,
            "description": self.description,
            "platform": self.platform,
            "niche": self.niche,
            "status": self.status.value if isinstance(self.status, TrendStatus) else self.status,
            "confidence": self.confidence,
            "post_ids": self.post_ids,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
        }


@dataclass
class TrendScore:
    """Time-series scoring for a trend cluster"""
    id: Optional[str] = None
    cluster_id: str = ""
    time_window: str = "24h"  # 1h, 6h, 24h, 3d, 7d
    mentions: int = 0
    velocity: float = 0.0
    velocity_delta: float = 0.0
    engagement_sum: int = 0
    engagement_p50: float = 0.0
    creator_count: int = 0
    creator_diversity: float = 0.0
    saturation: float = 0.0
    score: float = 0.0
    computed_at: Optional[datetime] = None
    
    def to_dict(self) -> Dict:
        return {
            "id": self.id,
            "cluster_id": self.cluster_id,
            "time_window": self.time_window,
            "mentions": self.mentions,
            "velocity": self.velocity,
            "velocity_delta": self.velocity_delta,
            "engagement_sum": self.engagement_sum,
            "engagement_p50": self.engagement_p50,
            "creator_count": self.creator_count,
            "creator_diversity": self.creator_diversity,
            "saturation": self.saturation,
            "score": self.score,
            "computed_at": self.computed_at.isoformat() if self.computed_at else None,
        }


@dataclass
class ClusterLingo:
    """Language patterns and meaning for a trend"""
    cluster_id: str = ""
    key_phrases: List[str] = field(default_factory=list)
    hook_patterns: List[str] = field(default_factory=list)
    usage_notes: str = ""
    meaning: str = ""
    structure: Dict[str, Any] = field(default_factory=dict)  # setup→pivot→punchline
    tone: str = ""
    brand_safety_score: float = 0.5
    brand_safety_flags: List[str] = field(default_factory=list)
    example_captions: List[str] = field(default_factory=list)
    updated_at: Optional[datetime] = None
    
    def to_dict(self) -> Dict:
        return {
            "cluster_id": self.cluster_id,
            "key_phrases": self.key_phrases,
            "hook_patterns": self.hook_patterns,
            "usage_notes": self.usage_notes,
            "meaning": self.meaning,
            "structure": self.structure,
            "tone": self.tone,
            "brand_safety_score": self.brand_safety_score,
            "brand_safety_flags": self.brand_safety_flags,
            "example_captions": self.example_captions,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
        }


@dataclass
class Brief:
    """Content-ready pack generated from a trend"""
    id: Optional[str] = None
    workspace_id: str = "00000000-0000-0000-0000-000000000001"
    cluster_id: Optional[str] = None
    title: str = ""
    platform_target: str = "tiktok"
    format_type: str = "reel"
    tone: Dict[str, Any] = field(default_factory=dict)
    hooks: List[str] = field(default_factory=list)
    script_outline: Dict[str, Any] = field(default_factory=dict)
    caption_templates: List[str] = field(default_factory=list)
    angles: List[str] = field(default_factory=list)
    shotlist: List[Dict] = field(default_factory=list)
    cta: Dict[str, Any] = field(default_factory=dict)
    must_include: List[str] = field(default_factory=list)
    differentiation: str = ""
    status: BriefStatus = BriefStatus.DRAFT
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None
    
    def to_dict(self) -> Dict:
        return {
            "id": self.id,
            "workspace_id": self.workspace_id,
            "cluster_id": self.cluster_id,
            "title": self.title,
            "platform_target": self.platform_target,
            "format_type": self.format_type,
            "tone": self.tone,
            "hooks": self.hooks,
            "script_outline": self.script_outline,
            "caption_templates": self.caption_templates,
            "angles": self.angles,
            "shotlist": self.shotlist,
            "cta": self.cta,
            "must_include": self.must_include,
            "differentiation": self.differentiation,
            "status": self.status.value if isinstance(self.status, BriefStatus) else self.status,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
        }


@dataclass
class RenderJob:
    """Video render job"""
    id: Optional[str] = None
    workspace_id: str = "00000000-0000-0000-0000-000000000001"
    brief_id: Optional[str] = None
    format_template_id: Optional[str] = None
    engine: str = "remotion"
    status: RenderStatus = RenderStatus.QUEUED
    priority: int = 0
    input_payload: Dict[str, Any] = field(default_factory=dict)
    output: Optional[Dict[str, Any]] = None
    error: Optional[str] = None
    progress: float = 0.0
    created_at: Optional[datetime] = None
    started_at: Optional[datetime] = None
    finished_at: Optional[datetime] = None
    
    def to_dict(self) -> Dict:
        return {
            "id": self.id,
            "workspace_id": self.workspace_id,
            "brief_id": self.brief_id,
            "format_template_id": self.format_template_id,
            "engine": self.engine,
            "status": self.status.value if isinstance(self.status, RenderStatus) else self.status,
            "priority": self.priority,
            "input_payload": self.input_payload,
            "output": self.output,
            "error": self.error,
            "progress": self.progress,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "started_at": self.started_at.isoformat() if self.started_at else None,
            "finished_at": self.finished_at.isoformat() if self.finished_at else None,
        }
