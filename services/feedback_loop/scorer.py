"""
Post Scorer for Twitter Feedback Loop Agent.
Computes engagement scores and labels winners/losers.
"""

from typing import Dict, List, Optional, Tuple
from dataclasses import dataclass
from datetime import datetime
import statistics
from enum import Enum
from loguru import logger

from .models import (
    Score,
    MetricsSnapshot,
    Post,
    TemplateLeaderboard,
    ScoringMode,
    generate_id
)


class PostScorer:
    """
    Scores posts based on engagement metrics.
    Supports multiple scoring modes: followers, leads, purchases.
    """
    
    # Scoring weights by mode
    WEIGHTS = {
        ScoringMode.FOLLOWERS: {
            "reply_rate": 1.0,
            "repost_rate": 0.8,
            "profile_click_rate": 0.6,
            "like_rate": 0.4,
            "click_rate": 0.2
        },
        ScoringMode.LEADS: {
            "click_rate": 1.0,
            "reply_rate": 0.7,
            "profile_click_rate": 0.5,
            "repost_rate": 0.4,
            "like_rate": 0.2
        },
        ScoringMode.PURCHASES: {
            "conversion_rate": 1.0,
            "click_rate": 0.7,
            "reply_rate": 0.3,
            "profile_click_rate": 0.2,
            "like_rate": 0.1
        }
    }
    
    # Thresholds for labels
    WINNER_THRESHOLD = 1.5  # z-score above this = winner
    LOSER_THRESHOLD = -1.0  # z-score below this = loser
    HIGH_INTENT_CLICK_RATE = 0.02  # 2% click rate = high intent
    VIRAL_REPOST_RATE = 0.05  # 5% repost rate = viral
    
    def __init__(self, 
                 mode: ScoringMode = ScoringMode.LEADS,
                 historical_scores: Optional[List[float]] = None):
        """
        Initialize the scorer.
        
        Args:
            mode: Scoring mode (followers, leads, purchases)
            historical_scores: Previous scores for z-score calculation
        """
        self.mode = mode
        self.weights = self.WEIGHTS[mode]
        self.historical_scores = historical_scores or []
    
    def compute_rates(self, snapshot: MetricsSnapshot) -> Dict[str, float]:
        """
        Compute engagement rates from a metrics snapshot.
        
        Args:
            snapshot: MetricsSnapshot with raw counts
        
        Returns:
            Dict of rate names to values
        """
        metrics = snapshot.metrics
        impressions = metrics.get("impressions", 0)
        
        if impressions == 0:
            return {
                "like_rate": 0.0,
                "reply_rate": 0.0,
                "repost_rate": 0.0,
                "click_rate": 0.0,
                "profile_click_rate": 0.0,
                "follow_rate": 0.0,
                "conversion_rate": 0.0
            }
        
        # Compute rates
        rates = {
            "like_rate": metrics.get("likes", 0) / impressions,
            "reply_rate": metrics.get("replies", 0) / impressions,
            "repost_rate": metrics.get("reposts", 0) / impressions,
            "click_rate": metrics.get("url_clicks", 0) / impressions,
            "profile_click_rate": metrics.get("profile_clicks", 0) / impressions,
            "follow_rate": metrics.get("follows", 0) / impressions,
        }
        
        # Add conversion rate from link metrics
        link_metrics = snapshot.link_metrics
        clicks = link_metrics.get("shortlink_clicks", 0)
        if clicks > 0:
            rates["conversion_rate"] = link_metrics.get("conversions", 0) / clicks
        else:
            rates["conversion_rate"] = 0.0
        
        return rates
    
    def compute_raw_score(self, rates: Dict[str, float]) -> float:
        """
        Compute raw weighted score from rates.
        
        Args:
            rates: Dict of rate names to values
        
        Returns:
            Raw weighted score
        """
        score = 0.0
        for rate_name, weight in self.weights.items():
            rate_value = rates.get(rate_name, 0.0)
            score += weight * rate_value
        return score
    
    def compute_z_score(self, raw_score: float) -> float:
        """
        Compute z-score relative to historical scores.
        
        Args:
            raw_score: The raw score to normalize
        
        Returns:
            Z-score (0 if no history)
        """
        if len(self.historical_scores) < 3:
            return 0.0
        
        mean = statistics.mean(self.historical_scores)
        stdev = statistics.stdev(self.historical_scores)
        
        if stdev == 0:
            return 0.0
        
        return (raw_score - mean) / stdev
    
    def score_post(self, 
                   post: Post,
                   snapshots: List[MetricsSnapshot]) -> Score:
        """
        Score a post using all available snapshots.
        
        Args:
            post: The post to score
            snapshots: List of MetricsSnapshots at different times
        
        Returns:
            Score object with computed scores and labels
        """
        score = Score(
            post_id=post.post_id,
            scoring_mode=self.mode
        )
        
        # Sort snapshots by type
        snapshot_map = {s.snapshot_type: s for s in snapshots}
        
        # Compute score at each time horizon
        for snapshot_type, attr_name in [
            ("1h", "score_1h"),
            ("6h", "score_6h"),
            ("24h", "score_24h"),
            ("72h", "score_72h"),
            ("7d", "score_7d")
        ]:
            if snapshot_type in snapshot_map:
                rates = self.compute_rates(snapshot_map[snapshot_type])
                raw_score = self.compute_raw_score(rates)
                z_score = self.compute_z_score(raw_score)
                setattr(score, attr_name, z_score)
                
                # Update rates from latest snapshot
                if snapshot_type == "24h" or (snapshot_type == "7d" and score.score_24h == 0):
                    score.rates = rates
        
        # Final score is 7d if available, else 24h
        score.final_score = score.score_7d if score.score_7d != 0 else score.score_24h
        
        # Assign labels
        score.labels = self._assign_labels(score, score.rates)
        
        # Assign blame notes
        score.blame_notes = self._assign_blame(score.rates)
        
        return score
    
    def _assign_labels(self, score: Score, rates: Dict[str, float]) -> List[str]:
        """Assign labels based on score and rates."""
        labels = []
        
        # Winner/loser based on z-score
        if score.final_score >= self.WINNER_THRESHOLD:
            labels.append("winner")
        elif score.final_score <= self.LOSER_THRESHOLD:
            labels.append("loser")
        
        # High intent based on click rate
        if rates.get("click_rate", 0) >= self.HIGH_INTENT_CLICK_RATE:
            labels.append("high_intent")
        
        # Viral based on repost rate
        if rates.get("repost_rate", 0) >= self.VIRAL_REPOST_RATE:
            labels.append("viral")
        
        # Low engagement
        total_engagement = (
            rates.get("like_rate", 0) +
            rates.get("reply_rate", 0) +
            rates.get("repost_rate", 0)
        )
        if total_engagement < 0.005:  # < 0.5% total engagement
            labels.append("low_engagement")
        
        # High conversion
        if rates.get("conversion_rate", 0) >= 0.05:  # 5% conversion
            labels.append("high_conversion")
        
        return labels
    
    def _assign_blame(self, rates: Dict[str, float]) -> Dict[str, str]:
        """Assign blame notes to diagnose what worked/didn't."""
        blame = {}
        
        # Hook quality (early engagement = reply + like)
        early_engagement = rates.get("like_rate", 0) + rates.get("reply_rate", 0)
        if early_engagement >= 0.03:
            blame["hook"] = "strong"
        elif early_engagement >= 0.01:
            blame["hook"] = "neutral"
        else:
            blame["hook"] = "weak"
        
        # Value density (repost = people sharing value)
        if rates.get("repost_rate", 0) >= 0.02:
            blame["value_density"] = "high"
        elif rates.get("repost_rate", 0) >= 0.005:
            blame["value_density"] = "medium"
        else:
            blame["value_density"] = "low"
        
        # CTA clarity (click rate)
        if rates.get("click_rate", 0) >= 0.02:
            blame["cta_clarity"] = "good"
        elif rates.get("click_rate", 0) >= 0.005:
            blame["cta_clarity"] = "moderate"
        else:
            blame["cta_clarity"] = "poor"
        
        # Audience fit (profile clicks = curiosity about author)
        if rates.get("profile_click_rate", 0) >= 0.01:
            blame["audience_fit"] = "strong"
        elif rates.get("profile_click_rate", 0) >= 0.003:
            blame["audience_fit"] = "moderate"
        else:
            blame["audience_fit"] = "weak"
        
        return blame
    
    def update_template_leaderboard(self,
                                     template_id: str,
                                     scores: List[Score],
                                     existing: Optional[TemplateLeaderboard] = None) -> TemplateLeaderboard:
        """
        Update template leaderboard with new scores.
        
        Args:
            template_id: Template ID to update
            scores: New scores to incorporate
            existing: Existing leaderboard entry (or None for new)
        
        Returns:
            Updated TemplateLeaderboard
        """
        if existing:
            leaderboard = existing
        else:
            leaderboard = TemplateLeaderboard(template_id=template_id)
        
        # Update counts
        leaderboard.total_posts += len(scores)
        
        # Compute averages
        if scores:
            scores_24h = [s.score_24h for s in scores if s.score_24h != 0]
            scores_7d = [s.score_7d for s in scores if s.score_7d != 0]
            
            if scores_24h:
                # Rolling average
                old_avg = leaderboard.avg_score_24h
                new_avg = statistics.mean(scores_24h)
                leaderboard.avg_score_24h = (old_avg * 0.7) + (new_avg * 0.3)
            
            if scores_7d:
                old_avg = leaderboard.avg_score_7d
                new_avg = statistics.mean(scores_7d)
                leaderboard.avg_score_7d = (old_avg * 0.7) + (new_avg * 0.3)
            
            # Early velocity (1h + 6h scores)
            early_scores = [s.score_1h + s.score_6h for s in scores]
            if early_scores:
                leaderboard.early_velocity = statistics.mean(early_scores)
            
            # Variance
            if len(scores) >= 2:
                final_scores = [s.final_score for s in scores]
                leaderboard.variance = statistics.variance(final_scores)
            
            # Win rate
            winners = sum(1 for s in scores if "winner" in s.labels)
            leaderboard.win_rate = winners / len(scores)
        
        leaderboard.last_updated = datetime.now()
        return leaderboard
    
    def rank_templates(self, 
                       leaderboards: List[TemplateLeaderboard]) -> List[Tuple[str, float]]:
        """
        Rank templates by performance.
        
        Args:
            leaderboards: List of template leaderboards
        
        Returns:
            List of (template_id, score) tuples sorted by score desc
        """
        ranked = []
        
        for lb in leaderboards:
            # Composite score: avg_score_7d weighted by win_rate, penalized by variance
            composite = (
                lb.avg_score_7d * 0.6 +
                lb.avg_score_24h * 0.3 +
                lb.early_velocity * 0.1
            ) * (1 + lb.win_rate) / (1 + lb.variance * 0.5)
            
            ranked.append((lb.template_id, composite))
        
        return sorted(ranked, key=lambda x: x[1], reverse=True)
    
    def add_historical_score(self, score: float):
        """Add a score to historical scores for z-score calculation."""
        self.historical_scores.append(score)
        # Keep last 100 scores
        if len(self.historical_scores) > 100:
            self.historical_scores = self.historical_scores[-100:]
