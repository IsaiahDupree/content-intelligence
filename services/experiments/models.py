"""
Experiments Scheduler Models
============================
Data models for the experimentation system.
"""

from dataclasses import dataclass, field
from datetime import datetime
from typing import List, Dict, Optional, Any
from uuid import uuid4
from enum import Enum


class ExperimentStatus(str, Enum):
    DRAFT = "draft"
    ACTIVE = "active"
    PAUSED = "paused"
    COMPLETED = "completed"
    CANCELLED = "cancelled"


class HypothesisStatus(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    PASSED = "passed"
    FAILED = "failed"
    INCONCLUSIVE = "inconclusive"


class OriginType(str, Enum):
    NARRATIVE = "narrative"
    EXPERIMENTS = "experiments"
    USER = "user"


class WinnerType(str, Enum):
    WINNER = "winner"
    WINNER_OF_WINNERS = "winner_of_winners"
    PROMOTED = "promoted"


@dataclass
class PostOrigin:
    """Origin tracking for all scheduled posts."""
    origin_type: OriginType
    
    # Narrative Builder fields
    narrative_goal_id: Optional[str] = None
    pillar: Optional[str] = None
    
    # Experiments fields
    experiment_id: Optional[str] = None
    hypothesis_id: Optional[str] = None
    variant: Optional[str] = None  # 'control' | 'variant_a' | 'variant_b'
    
    # User fields
    user_id: Optional[str] = None
    manual_reason: Optional[str] = None
    
    # Common
    scheduled_at: datetime = field(default_factory=datetime.now)
    scheduled_by: str = "system"  # 'ai_narrative' | 'ai_experiments' | 'user'
    
    def to_dict(self) -> Dict[str, Any]:
        return {
            "origin_type": self.origin_type.value,
            "narrative_goal_id": self.narrative_goal_id,
            "pillar": self.pillar,
            "experiment_id": self.experiment_id,
            "hypothesis_id": self.hypothesis_id,
            "variant": self.variant,
            "user_id": self.user_id,
            "manual_reason": self.manual_reason,
            "scheduled_at": self.scheduled_at.isoformat(),
            "scheduled_by": self.scheduled_by
        }


@dataclass
class Hypothesis:
    """A testable hypothesis for content experimentation."""
    id: str = field(default_factory=lambda: str(uuid4()))
    experiment_id: str = ""
    
    # The hypothesis statement
    statement: str = ""  # e.g., "Videos with questions in first 2 seconds get 40% more views"
    
    # Variables being tested
    independent_variable: str = ""  # What we're changing
    dependent_variable: str = ""    # What we're measuring
    control_description: str = ""   # Baseline approach
    variant_description: str = ""   # Test approach
    
    # Success criteria
    success_metric: str = "view_count"  # e.g., "view_count", "engagement_rate"
    success_threshold: float = 1.2      # e.g., 1.2 (20% improvement)
    min_sample_size: int = 10           # Minimum posts before conclusion
    
    # Results
    status: HypothesisStatus = HypothesisStatus.PENDING
    confidence_level: float = 0.0
    actual_improvement: float = 0.0
    control_avg: float = 0.0
    variant_avg: float = 0.0
    p_value: float = 1.0
    learnings: str = ""
    
    # Tracking
    control_posts: List[str] = field(default_factory=list)
    variant_posts: List[str] = field(default_factory=list)
    
    created_at: datetime = field(default_factory=datetime.now)
    
    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "experiment_id": self.experiment_id,
            "statement": self.statement,
            "independent_variable": self.independent_variable,
            "dependent_variable": self.dependent_variable,
            "control_description": self.control_description,
            "variant_description": self.variant_description,
            "success_metric": self.success_metric,
            "success_threshold": self.success_threshold,
            "min_sample_size": self.min_sample_size,
            "status": self.status.value,
            "confidence_level": self.confidence_level,
            "actual_improvement": self.actual_improvement,
            "control_avg": self.control_avg,
            "variant_avg": self.variant_avg,
            "learnings": self.learnings,
            "control_posts_count": len(self.control_posts),
            "variant_posts_count": len(self.variant_posts)
        }


@dataclass
class Experiment:
    """An experiment containing one or more hypotheses."""
    id: str = field(default_factory=lambda: str(uuid4()))
    name: str = ""
    description: str = ""
    goal: str = ""  # What we're trying to learn/achieve
    
    # Status
    status: ExperimentStatus = ExperimentStatus.DRAFT
    
    # Timing
    start_date: Optional[datetime] = None
    end_date: Optional[datetime] = None
    
    # Configuration
    success_criteria: Dict[str, Any] = field(default_factory=dict)
    target_accounts: List[str] = field(default_factory=list)
    resource_types: List[str] = field(default_factory=list)  # ugc, ai_generated, edited
    
    # Hypotheses
    hypotheses: List[Hypothesis] = field(default_factory=list)
    
    # Results
    results: Dict[str, Any] = field(default_factory=dict)
    learnings: str = ""
    winner_video_ids: List[str] = field(default_factory=list)
    
    created_at: datetime = field(default_factory=datetime.now)
    updated_at: datetime = field(default_factory=datetime.now)
    
    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "description": self.description,
            "goal": self.goal,
            "status": self.status.value,
            "start_date": self.start_date.isoformat() if self.start_date else None,
            "end_date": self.end_date.isoformat() if self.end_date else None,
            "success_criteria": self.success_criteria,
            "target_accounts": self.target_accounts,
            "resource_types": self.resource_types,
            "hypotheses": [h.to_dict() for h in self.hypotheses],
            "results": self.results,
            "learnings": self.learnings,
            "winner_video_ids": self.winner_video_ids,
            "created_at": self.created_at.isoformat()
        }


@dataclass
class ContentPattern:
    """A learned pattern from experiments."""
    id: str = field(default_factory=lambda: str(uuid4()))
    pattern_type: str = ""  # hook, format, timing, caption, audio, angle
    category: str = ""
    name: str = ""
    description: str = ""
    
    # Performance metrics
    success_rate: float = 0.0
    avg_improvement: float = 0.0
    confidence: float = 0.0
    
    # Evidence
    supporting_experiments: List[str] = field(default_factory=list)
    sample_size: int = 0
    
    # Application guidance
    when_to_use: str = ""
    when_to_avoid: str = ""
    best_for_pillars: List[str] = field(default_factory=list)
    
    # Evolution
    first_discovered: datetime = field(default_factory=datetime.now)
    last_validated: Optional[datetime] = None
    times_applied: int = 0
    times_successful: int = 0
    
    is_active: bool = True
    
    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "pattern_type": self.pattern_type,
            "category": self.category,
            "name": self.name,
            "description": self.description,
            "success_rate": self.success_rate,
            "avg_improvement": self.avg_improvement,
            "confidence": self.confidence,
            "supporting_experiments": self.supporting_experiments,
            "sample_size": self.sample_size,
            "when_to_use": self.when_to_use,
            "when_to_avoid": self.when_to_avoid,
            "best_for_pillars": self.best_for_pillars,
            "times_applied": self.times_applied,
            "is_active": self.is_active
        }


@dataclass
class ContentFramework:
    """A proven framework for content creation."""
    id: str = field(default_factory=lambda: str(uuid4()))
    name: str = ""
    description: str = ""
    
    # Structure
    structure: List[Dict[str, str]] = field(default_factory=list)  # Step-by-step
    
    # Application
    best_for: List[str] = field(default_factory=list)
    pillars: List[str] = field(default_factory=list)
    platforms: List[str] = field(default_factory=list)
    
    # Performance
    avg_performance_lift: float = 0.0
    times_validated: int = 0
    success_rate: float = 0.0
    
    # Source
    source_patterns: List[str] = field(default_factory=list)
    
    is_active: bool = True
    
    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "description": self.description,
            "structure": self.structure,
            "best_for": self.best_for,
            "pillars": self.pillars,
            "platforms": self.platforms,
            "avg_performance_lift": self.avg_performance_lift,
            "times_validated": self.times_validated,
            "success_rate": self.success_rate,
            "is_active": self.is_active
        }


@dataclass
class ExperimentWinner:
    """A high-performing content piece from experiments."""
    id: str = field(default_factory=lambda: str(uuid4()))
    experiment_id: str = ""
    hypothesis_id: str = ""
    post_id: str = ""
    video_id: str = ""
    
    # Performance
    performance_metrics: Dict[str, Any] = field(default_factory=dict)
    ranking_score: float = 0.0
    
    # Status
    winner_type: WinnerType = WinnerType.WINNER
    promoted_to_narrative: bool = False
    promoted_at: Optional[datetime] = None
    
    # After promotion
    narrative_performance: Dict[str, Any] = field(default_factory=dict)
    
    detected_at: datetime = field(default_factory=datetime.now)
    
    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "experiment_id": self.experiment_id,
            "hypothesis_id": self.hypothesis_id,
            "post_id": self.post_id,
            "video_id": self.video_id,
            "performance_metrics": self.performance_metrics,
            "ranking_score": self.ranking_score,
            "winner_type": self.winner_type.value,
            "promoted_to_narrative": self.promoted_to_narrative,
            "promoted_at": self.promoted_at.isoformat() if self.promoted_at else None,
            "detected_at": self.detected_at.isoformat()
        }
