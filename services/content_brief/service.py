"""
Enhanced Content Brief Service
================================
Main service for enhanced content brief generation with scoring, clustering, and script generation.
"""

import logging
from typing import List, Dict, Any, Optional
from datetime import datetime, timedelta, timezone
from uuid import uuid4

from services.agent_framework.event_bus import (
    AgentEvent,
    AgentEventBus,
    AgentType,
    EventType,
)

from .models import EnhancedBrief, BriefStatus, TrendCard, TrendCluster, BriefAngle, ScriptOutput
from .scoring import BriefScorer
from .clustering import TrendClusterer
from .angle_generator import AngleGenerator
from .script_generator import ScriptGenerator

logger = logging.getLogger(__name__)


class EnhancedBriefService:
    """
    Enhanced content brief service with:
    - Trend clustering
    - Angle generation
    - Scoring (Worth Covering)
    - Script generation
    """
    
    def __init__(self, event_bus: Optional[AgentEventBus] = None):
        """
        Initialize enhanced brief service.
        
        Args:
            event_bus: Event bus for publishing events
        """
        self.event_bus = event_bus or AgentEventBus()
        self.scorer = BriefScorer()
        self.clusterer = TrendClusterer()
        self.angle_generator = AngleGenerator()
        self.script_generator = ScriptGenerator()
    
    async def process_trends_to_briefs(
        self,
        trends: List[TrendCard],
        min_score: float = 70.0
    ) -> List[EnhancedBrief]:
        """
        Process trends through the full pipeline: cluster → angle → score → brief.
        
        Args:
            trends: List of trend cards
            min_score: Minimum score threshold
        
        Returns:
            List of enhanced briefs that meet the threshold
        """
        # Step 1: Cluster trends
        clusters = self.clusterer.cluster_trends(trends)
        logger.info(f"Clustered {len(trends)} trends into {len(clusters)} clusters")
        
        # Step 2: Generate angles for each cluster
        all_angles = []
        for cluster in clusters:
            angles = self.angle_generator.generate_angles(cluster, count=8)
            all_angles.extend(angles)
        
        logger.info(f"Generated {len(all_angles)} angles")
        
        # Step 3: Score angles and create briefs
        briefs = []
        for angle in all_angles:
            # Find cluster for angle
            cluster = next((c for c in clusters if c.cluster_id == angle.cluster_id), None)
            if not cluster:
                continue
            
            # Score the angle
            score = self.scorer.score_angle(angle, cluster)
            angle.score = score
            
            # Check if worth covering
            worth_covering = self.scorer.is_worth_covering(score, threshold=min_score)
            
            if worth_covering:
                # Create brief
                brief = EnhancedBrief(
                    brief_id=str(uuid4()),
                    status=BriefStatus.SCORED,
                    cluster=cluster,
                    angle=angle,
                    score=score,
                    worth_covering=True,
                    title=angle.promise,
                    hook=self._generate_hook(angle),
                    promise=angle.promise,
                    unique_lens=angle.unique_lens,
                    format="shorts",
                    length_sec=45,
                    niche=cluster.trends[0].niche_tags[0] if cluster.trends and cluster.trends[0].niche_tags else None,
                    platform="multi"
                )
                
                # Generate script
                brief.script_json = self.script_generator.generate_script(brief)
                brief.script_beats = brief.script_json.segments
                if brief.script_json.metadata.get("status") == "blocked_quality":
                    brief.status = BriefStatus.BLOCKED_QUALITY
                
                briefs.append(brief)
                
                # Emit event
                await self.event_bus.publish(
                    AgentEvent(
                        agent_type=AgentType.CONTENT_ANALYZER,
                        event_type=EventType.PLAN_GENERATED,
                        title="Content brief generated",
                        description=angle.promise,
                        data={
                            "brief_id": brief.brief_id,
                            "score": score.total,
                            "angle": angle.promise,
                            "correlation_id": brief.brief_id,
                            "script_status": brief.script_json.metadata.get("status"),
                        },
                    )
                )
        
        logger.info(f"Generated {len(briefs)} briefs above threshold {min_score}")
        return briefs
    
    def _generate_hook(self, angle: BriefAngle) -> str:
        """Generate hook from angle."""
        hooks = (
            f"{angle.audience_role}: what changes when {angle.stakes} is the constraint?",
            f"Before you {angle.intent}, check the {angle.stakes} tradeoff.",
            f"{angle.promise} Start with this lens: {angle.unique_lens}",
        )
        seed = sum(ord(character) for character in angle.angle_id)
        return hooks[seed % len(hooks)]
    
    async def generate_script_from_brief(self, brief: EnhancedBrief) -> ScriptOutput:
        """
        Generate script.json from an enhanced brief.
        
        Args:
            brief: Enhanced brief
        
        Returns:
            ScriptOutput
        """
        script = self.script_generator.generate_script(brief)
        brief.script_json = script
        brief.script_beats = script.segments
        if script.metadata.get("status") == "blocked_quality":
            brief.status = BriefStatus.BLOCKED_QUALITY
        
        return script
    
    def filter_by_score(
        self,
        briefs: List[EnhancedBrief],
        min_score: float = 70.0,
        strategic_threshold: float = 60.0,
        is_strategic: bool = False
    ) -> List[EnhancedBrief]:
        """
        Filter briefs by score threshold.
        
        Args:
            briefs: List of briefs
            min_score: Standard threshold
            strategic_threshold: Strategic threshold
            is_strategic: Whether to use strategic threshold
        
        Returns:
            Filtered list of briefs
        """
        filtered = []
        for brief in briefs:
            if brief.score:
                if self.scorer.is_worth_covering(
                    brief.score,
                    threshold=min_score,
                    strategic_threshold=strategic_threshold,
                    is_strategic=is_strategic
                ):
                    filtered.append(brief)
        return filtered
