"""
Engagement Rate Scoring (OPS-004) & Reward Function Scorer (OPS-005)
====================================================================
Calculates engagement rates and composite reward scores for content posts.

Based on the Content Ops PRD scoring model:
- Uses RATES not raw counts (normalized by impressions)
- Composite reward function with weighted z-scores
- Classifies posts as winners/losers based on thresholds

Engagement Rates:
- like_rate = likes / impressions
- reply_rate = replies / impressions
- repost_rate = reposts / impressions
- click_rate = link_clicks / impressions

Reward Function (PRD):
score = 1.0 * z(click_rate) + 0.8 * z(reply_rate) + 0.6 * z(repost_rate) + 0.4 * z(like_rate)

Performance: <1ms per score calculation
"""

from typing import Dict, Optional, List
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from loguru import logger
import statistics


class PostLabel(Enum):
    """Post performance labels"""
    WINNER = "winner"       # Top 20% performers
    PROMISING = "promising"  # 40-80th percentile
    AVERAGE = "average"      # 20-40th percentile
    LOSER = "loser"          # Bottom 20%


@dataclass
class EngagementRates:
    """Engagement rates for a post"""
    like_rate: float
    reply_rate: float
    repost_rate: float
    click_rate: float
    impressions: int

    def to_dict(self) -> Dict:
        return {
            "like_rate": self.like_rate,
            "reply_rate": self.reply_rate,
            "repost_rate": self.repost_rate,
            "click_rate": self.click_rate,
            "impressions": self.impressions
        }


@dataclass
class RewardScore:
    """Composite reward score with breakdown"""
    score: float
    z_scores: Dict[str, float]
    label: PostLabel
    rates: EngagementRates

    def to_dict(self) -> Dict:
        return {
            "score": self.score,
            "z_scores": self.z_scores,
            "label": self.label.value,
            "rates": self.rates.to_dict()
        }


class EngagementScorer:
    """
    Engagement Rate & Reward Function Scorer

    Calculates engagement rates and composite reward scores for posts.
    Maintains rolling statistics for z-score normalization.

    Usage:
        scorer = EngagementScorer.get_instance()

        # Calculate engagement rates
        rates = scorer.calculate_rates(
            likes=100, replies=20, reposts=30, clicks=50,
            impressions=10000
        )

        # Calculate reward score
        score = scorer.calculate_reward_score(rates)
        print(score.score)  # Composite score
        print(score.label)  # PostLabel.WINNER/PROMISING/AVERAGE/LOSER
    """

    _instance: Optional["EngagementScorer"] = None

    # Reward function weights (from PRD)
    CLICK_WEIGHT = 1.0
    REPLY_WEIGHT = 0.8
    REPOST_WEIGHT = 0.6
    LIKE_WEIGHT = 0.4

    # Performance label thresholds (percentiles)
    WINNER_THRESHOLD = 0.80   # Top 20%
    PROMISING_THRESHOLD = 0.40  # 40-80th percentile
    AVERAGE_THRESHOLD = 0.20    # 20-40th percentile

    def __init__(self):
        if EngagementScorer._instance is not None:
            raise RuntimeError("Use EngagementScorer.get_instance()")

        # Rolling statistics for z-score calculation
        self._historical_rates: Dict[str, List[float]] = {
            "like_rate": [],
            "reply_rate": [],
            "repost_rate": [],
            "click_rate": []
        }
        self._max_history = 1000  # Keep last 1000 posts
        self._historical_scores: List[float] = []

        logger.info("📊 Engagement Scorer initialized")

    @classmethod
    def get_instance(cls) -> "EngagementScorer":
        """Get singleton instance"""
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance

    def calculate_rates(
        self,
        likes: int,
        replies: int,
        reposts: int,
        clicks: int,
        impressions: int
    ) -> EngagementRates:
        """
        Calculate engagement rates from raw metrics

        Args:
            likes: Number of likes
            replies: Number of replies/comments
            reposts: Number of reposts/shares
            clicks: Number of link clicks
            impressions: Number of impressions (views)

        Returns:
            EngagementRates with normalized rates
        """
        if impressions <= 0:
            logger.warning("Impressions is 0 or negative, returning zero rates")
            return EngagementRates(
                like_rate=0.0,
                reply_rate=0.0,
                repost_rate=0.0,
                click_rate=0.0,
                impressions=impressions
            )

        return EngagementRates(
            like_rate=likes / impressions,
            reply_rate=replies / impressions,
            repost_rate=reposts / impressions,
            click_rate=clicks / impressions,
            impressions=impressions
        )

    def calculate_reward_score(
        self,
        rates: EngagementRates,
        update_history: bool = True
    ) -> RewardScore:
        """
        Calculate composite reward score using weighted z-scores

        Formula (from PRD):
        score = 1.0 * z(click_rate) + 0.8 * z(reply_rate) +
                0.6 * z(repost_rate) + 0.4 * z(like_rate)

        Args:
            rates: EngagementRates to score
            update_history: Add this post to historical statistics

        Returns:
            RewardScore with score, z-scores, and label
        """
        # Calculate z-scores for each rate
        z_scores = {
            "click_rate": self._calculate_z_score("click_rate", rates.click_rate),
            "reply_rate": self._calculate_z_score("reply_rate", rates.reply_rate),
            "repost_rate": self._calculate_z_score("repost_rate", rates.repost_rate),
            "like_rate": self._calculate_z_score("like_rate", rates.like_rate)
        }

        # Calculate weighted composite score
        score = (
            self.CLICK_WEIGHT * z_scores["click_rate"] +
            self.REPLY_WEIGHT * z_scores["reply_rate"] +
            self.REPOST_WEIGHT * z_scores["repost_rate"] +
            self.LIKE_WEIGHT * z_scores["like_rate"]
        )

        # Update historical data
        if update_history:
            self._update_history(rates, score)

        # Determine label based on score percentile
        label = self._determine_label(score)

        return RewardScore(
            score=score,
            z_scores=z_scores,
            label=label,
            rates=rates
        )

    def _calculate_z_score(self, rate_name: str, value: float) -> float:
        """
        Calculate z-score for a given rate

        Z-score = (value - mean) / stddev

        Args:
            rate_name: Name of the rate (like_rate, reply_rate, etc.)
            value: The rate value

        Returns:
            Z-score (0.0 if insufficient history)
        """
        history = self._historical_rates.get(rate_name, [])

        if len(history) < 2:
            # Insufficient data for z-score, return 0
            return 0.0

        try:
            mean = statistics.mean(history)
            stddev = statistics.stdev(history)

            if stddev == 0:
                # No variation, return 0
                return 0.0

            z_score = (value - mean) / stddev
            return z_score

        except Exception as e:
            logger.error(f"Error calculating z-score for {rate_name}: {e}")
            return 0.0

    def _update_history(self, rates: EngagementRates, score: float) -> None:
        """
        Update historical statistics with new post data

        Args:
            rates: Engagement rates to add
            score: Composite score to add
        """
        # Add rates to history
        self._historical_rates["like_rate"].append(rates.like_rate)
        self._historical_rates["reply_rate"].append(rates.reply_rate)
        self._historical_rates["repost_rate"].append(rates.repost_rate)
        self._historical_rates["click_rate"].append(rates.click_rate)

        # Trim to max history
        for rate_name in self._historical_rates:
            if len(self._historical_rates[rate_name]) > self._max_history:
                self._historical_rates[rate_name] = (
                    self._historical_rates[rate_name][-self._max_history:]
                )

        # Add score to history
        self._historical_scores.append(score)
        if len(self._historical_scores) > self._max_history:
            self._historical_scores = self._historical_scores[-self._max_history:]

    def _determine_label(self, score: float) -> PostLabel:
        """
        Determine performance label based on score percentile

        Args:
            score: Composite reward score

        Returns:
            PostLabel (WINNER/PROMISING/AVERAGE/LOSER)
        """
        if len(self._historical_scores) < 5:
            # Insufficient data, return AVERAGE
            return PostLabel.AVERAGE

        # Calculate percentile rank
        scores = sorted(self._historical_scores)
        rank = sum(1 for s in scores if s < score) / len(scores)

        if rank >= self.WINNER_THRESHOLD:
            return PostLabel.WINNER
        elif rank >= self.PROMISING_THRESHOLD:
            return PostLabel.PROMISING
        elif rank >= self.AVERAGE_THRESHOLD:
            return PostLabel.AVERAGE
        else:
            return PostLabel.LOSER

    def get_statistics(self) -> Dict:
        """
        Get current statistics for monitoring

        Returns:
            Dictionary with historical stats
        """
        stats = {
            "history_count": len(self._historical_scores),
            "rate_means": {},
            "rate_stddevs": {}
        }

        for rate_name, values in self._historical_rates.items():
            if len(values) >= 2:
                stats["rate_means"][rate_name] = statistics.mean(values)
                stats["rate_stddevs"][rate_name] = statistics.stdev(values)
            else:
                stats["rate_means"][rate_name] = 0.0
                stats["rate_stddevs"][rate_name] = 0.0

        if len(self._historical_scores) >= 2:
            stats["score_mean"] = statistics.mean(self._historical_scores)
            stats["score_stddev"] = statistics.stdev(self._historical_scores)
            stats["score_min"] = min(self._historical_scores)
            stats["score_max"] = max(self._historical_scores)
        else:
            stats["score_mean"] = 0.0
            stats["score_stddev"] = 0.0
            stats["score_min"] = 0.0
            stats["score_max"] = 0.0

        return stats

    def load_historical_data(
        self,
        historical_posts: List[Dict]
    ) -> None:
        """
        Load historical post data for z-score calibration

        Args:
            historical_posts: List of dicts with keys:
                - likes, replies, reposts, clicks, impressions
        """
        logger.info(f"Loading {len(historical_posts)} historical posts...")

        for post in historical_posts:
            rates = self.calculate_rates(
                likes=post.get("likes", 0),
                replies=post.get("replies", 0),
                reposts=post.get("reposts", 0),
                clicks=post.get("clicks", 0),
                impressions=post.get("impressions", 1)
            )

            # Calculate score and update history
            self.calculate_reward_score(rates, update_history=True)

        logger.success(
            f"✓ Loaded {len(historical_posts)} historical posts | "
            f"Score range: {min(self._historical_scores):.2f} to "
            f"{max(self._historical_scores):.2f}"
        )


# Singleton getter function
_scorer_instance: Optional[EngagementScorer] = None

def get_engagement_scorer() -> EngagementScorer:
    """Get singleton EngagementScorer instance"""
    global _scorer_instance
    if _scorer_instance is None:
        _scorer_instance = EngagementScorer.get_instance()
    return _scorer_instance
