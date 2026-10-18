"""
Winner Detector
===============
Identifies high-performing content for narrative promotion.
"""

import os
import logging
from typing import List, Dict, Optional, Any
from datetime import datetime, timedelta
from dataclasses import dataclass

from sqlalchemy import create_engine, text

from .models import ExperimentWinner, WinnerType

logger = logging.getLogger(__name__)

DATABASE_URL = os.getenv("DATABASE_URL", "postgresql://postgres:postgres@127.0.0.1:54322/postgres")


@dataclass
class WinnerCriteria:
    """Criteria for identifying winners."""
    min_views: int = 5000
    min_engagement_rate: float = 0.04  # 4%
    min_watch_time_pct: float = 0.40   # 40%
    min_successful_tests: int = 2
    max_performance_variance: float = 0.30
    brand_safe_score: float = 0.85
    narrative_alignment: float = 0.60
    within_days: int = 30


class WinnerDetector:
    """
    Detects and tracks high-performing experiment content.
    
    Responsibilities:
    - Identify winners based on performance criteria
    - Track "winner of winners" for promotion
    - Manage promotion pipeline to narrative builder
    - Validate brand safety and narrative alignment
    """
    
    def __init__(self, criteria: Optional[WinnerCriteria] = None):
        self.criteria = criteria or WinnerCriteria()
        self.engine = create_engine(DATABASE_URL)
    
    async def detect_winners(
        self,
        experiment_id: Optional[str] = None
    ) -> List[ExperimentWinner]:
        """
        Detect winner content from experiments.
        
        Args:
            experiment_id: Optional filter for specific experiment
        
        Returns:
            List of detected winners
        """
        winners = []
        
        with self.engine.connect() as conn:
            query = """
                SELECT sp.id, sp.video_id, sp.experiment_id, sp.hypothesis_id,
                       sp.metrics, sp.posted_at
                FROM scheduled_posts sp
                WHERE sp.origin_type = 'experiments'
                AND sp.status = 'posted'
                AND sp.metrics IS NOT NULL
                AND sp.posted_at > NOW() - INTERVAL :days DAY
            """
            params = {"days": self.criteria.within_days}
            
            if experiment_id:
                query += " AND sp.experiment_id = :experiment_id"
                params["experiment_id"] = experiment_id
            
            result = conn.execute(text(query), params)
            
            for row in result:
                post_id = str(row[0])
                video_id = str(row[1]) if row[1] else ""
                exp_id = str(row[2]) if row[2] else ""
                hyp_id = str(row[3]) if row[3] else ""
                metrics = row[4] or {}
                
                # Check against criteria
                if self._meets_criteria(metrics):
                    ranking_score = self._calculate_ranking_score(metrics)
                    
                    winner = ExperimentWinner(
                        experiment_id=exp_id,
                        hypothesis_id=hyp_id,
                        post_id=post_id,
                        video_id=video_id,
                        performance_metrics=metrics,
                        ranking_score=ranking_score,
                        winner_type=WinnerType.WINNER
                    )
                    winners.append(winner)
        
        # Sort by ranking score
        winners.sort(key=lambda w: w.ranking_score, reverse=True)
        
        logger.info(f"[WinnerDetector] Detected {len(winners)} winners")
        
        return winners
    
    def _meets_criteria(self, metrics: Dict[str, Any]) -> bool:
        """Check if metrics meet winner criteria."""
        views = metrics.get("views", 0)
        engagement = metrics.get("engagement_rate", 0)
        watch_time = metrics.get("avg_watch_time_pct", 0)
        
        return (
            views >= self.criteria.min_views and
            engagement >= self.criteria.min_engagement_rate and
            watch_time >= self.criteria.min_watch_time_pct
        )
    
    def _calculate_ranking_score(self, metrics: Dict[str, Any]) -> float:
        """Calculate composite ranking score."""
        # Normalize each metric and weight
        views = metrics.get("views", 0)
        engagement = metrics.get("engagement_rate", 0)
        watch_time = metrics.get("avg_watch_time_pct", 0)
        
        # Weights
        view_weight = 0.3
        engagement_weight = 0.4
        watch_weight = 0.3
        
        # Normalize (simplified)
        view_score = min(views / 50000, 1.0)  # Cap at 50k
        engagement_score = min(engagement / 0.10, 1.0)  # Cap at 10%
        watch_score = min(watch_time / 0.80, 1.0)  # Cap at 80%
        
        return (
            view_weight * view_score +
            engagement_weight * engagement_score +
            watch_weight * watch_score
        )
    
    async def detect_winner_of_winners(self) -> List[ExperimentWinner]:
        """
        Identify the best performers across all experiments.
        
        These are candidates for promotion to narrative builder.
        """
        # Get all winners
        all_winners = await self.detect_winners()
        
        # Filter for consistent performers
        winner_of_winners = []
        
        for winner in all_winners:
            # Check if this video has performed well multiple times
            if await self._has_consistent_performance(winner.video_id):
                winner.winner_type = WinnerType.WINNER_OF_WINNERS
                winner_of_winners.append(winner)
        
        # Save to database
        await self._save_winners(winner_of_winners)
        
        logger.info(f"[WinnerDetector] Found {len(winner_of_winners)} winner of winners")
        
        return winner_of_winners
    
    async def _has_consistent_performance(self, video_id: str) -> bool:
        """Check if a video has consistent good performance."""
        with self.engine.connect() as conn:
            result = conn.execute(text("""
                SELECT COUNT(*) FROM scheduled_posts
                WHERE video_id = :video_id
                AND status = 'posted'
                AND (metrics->>'views')::int >= :min_views
            """), {
                "video_id": video_id,
                "min_views": self.criteria.min_views
            }).fetchone()
            
            count = result[0] if result else 0
            return count >= self.criteria.min_successful_tests
    
    async def _save_winners(self, winners: List[ExperimentWinner]):
        """Save winners to database."""
        with self.engine.connect() as conn:
            for winner in winners:
                conn.execute(text("""
                    INSERT INTO experiment_winners 
                        (id, experiment_id, hypothesis_id, post_id, video_id,
                         performance_metrics, ranking_score, winner_type)
                    VALUES 
                        (:id, :experiment_id, :hypothesis_id, :post_id, :video_id,
                         :metrics, :score, :type)
                    ON CONFLICT (id) DO UPDATE SET
                        ranking_score = :score,
                        winner_type = :type
                """), {
                    "id": winner.id,
                    "experiment_id": winner.experiment_id or None,
                    "hypothesis_id": winner.hypothesis_id or None,
                    "post_id": winner.post_id,
                    "video_id": winner.video_id,
                    "metrics": winner.performance_metrics,
                    "score": winner.ranking_score,
                    "type": winner.winner_type.value
                })
            conn.commit()
    
    async def promote_to_narrative(
        self,
        winner_id: str,
        narrative_goal_id: Optional[str] = None
    ) -> Dict[str, Any]:
        """
        Promote a winner to the narrative builder queue.
        
        Args:
            winner_id: ID of the winner to promote
            narrative_goal_id: Optional goal to align with
        
        Returns:
            Promotion result
        """
        with self.engine.connect() as conn:
            # Get winner details
            result = conn.execute(text("""
                SELECT video_id, performance_metrics, ranking_score
                FROM experiment_winners WHERE id = :id
            """), {"id": winner_id}).fetchone()
            
            if not result:
                return {"success": False, "error": "Winner not found"}
            
            video_id = str(result[0])
            metrics = result[1] or {}
            
            # Mark as promoted
            conn.execute(text("""
                UPDATE experiment_winners
                SET promoted_to_narrative = TRUE,
                    promoted_at = NOW(),
                    winner_type = 'promoted'
                WHERE id = :id
            """), {"id": winner_id})
            
            # Add to narrative schedule
            from uuid import uuid4
            post_id = str(uuid4())
            
            conn.execute(text("""
                INSERT INTO scheduled_posts 
                    (id, video_id, status, origin_type, 
                     narrative_goal_id, title, caption)
                VALUES 
                    (:id, :video_id, 'pending', 'narrative',
                     :goal_id, 'Promoted from experiments', 
                     'Top performing content from experiment testing')
            """), {
                "id": post_id,
                "video_id": video_id,
                "goal_id": narrative_goal_id
            })
            
            conn.commit()
        
        logger.info(f"[WinnerDetector] Promoted winner {winner_id} to narrative")
        
        return {
            "success": True,
            "winner_id": winner_id,
            "video_id": video_id,
            "narrative_post_id": post_id
        }
    
    async def get_promotion_candidates(
        self,
        limit: int = 10
    ) -> List[ExperimentWinner]:
        """Get winners ready for promotion."""
        with self.engine.connect() as conn:
            result = conn.execute(text("""
                SELECT id, experiment_id, hypothesis_id, post_id, video_id,
                       performance_metrics, ranking_score, winner_type
                FROM experiment_winners
                WHERE promoted_to_narrative = FALSE
                AND winner_type IN ('winner', 'winner_of_winners')
                ORDER BY ranking_score DESC
                LIMIT :limit
            """), {"limit": limit})
            
            candidates = []
            for row in result:
                winner = ExperimentWinner(
                    id=str(row[0]),
                    experiment_id=str(row[1]) if row[1] else "",
                    hypothesis_id=str(row[2]) if row[2] else "",
                    post_id=str(row[3]) if row[3] else "",
                    video_id=str(row[4]) if row[4] else "",
                    performance_metrics=row[5] or {},
                    ranking_score=float(row[6]) if row[6] else 0,
                    winner_type=WinnerType(row[7]) if row[7] else WinnerType.WINNER
                )
                candidates.append(winner)
        
        return candidates
