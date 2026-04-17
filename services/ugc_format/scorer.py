"""Performance Scoring System - calculates engagement and performance metrics."""
from typing import Dict, Any, Optional
from dataclasses import dataclass
import math
from datetime import datetime, timezone


@dataclass
class PerformanceScore:
    overall_score: float  # 0-100
    engagement_score: float  # 0-100
    retention_score: float  # 0-100
    conversion_score: float  # 0-100
    metrics_breakdown: Dict[str, float]
    timestamp: str


class PerformanceScorer:
    """Calculates normalized performance scores from engagement metrics."""

    def score(self, metrics: Dict[str, Any]) -> PerformanceScore:
        """Calculate performance score from metrics.

        Expected metrics:
        - views: int
        - likes: int
        - comments: int
        - shares: int
        - watch_time_avg: float (0-1)
        - completion_rate: float (0-1)
        - click_through_rate: float (0-1)
        - conversions: int
        """

        # Extract and normalize metrics
        views = max(0, metrics.get("views", 0))
        likes = max(0, metrics.get("likes", 0))
        comments = max(0, metrics.get("comments", 0))
        shares = max(0, metrics.get("shares", 0))
        watch_time_avg = max(0, min(1.0, metrics.get("watch_time_avg", 0)))
        completion_rate = max(0, min(1.0, metrics.get("completion_rate", 0)))
        click_through_rate = max(0, min(1.0, metrics.get("click_through_rate", 0)))
        conversions = max(0, metrics.get("conversions", 0))

        # Calculate engagement score (0-100)
        engagement_score = self._calculate_engagement_score(
            views, likes, comments, shares
        )

        # Calculate retention score (0-100)
        retention_score = self._calculate_retention_score(
            watch_time_avg, completion_rate
        )

        # Calculate conversion score (0-100)
        conversion_score = self._calculate_conversion_score(
            click_through_rate, conversions, views
        )

        # Overall = weighted average
        overall_score = (
            engagement_score * 0.4 +
            retention_score * 0.3 +
            conversion_score * 0.3
        )

        breakdown = {
            "views": min(100, (views / 10000) * 100) if views > 0 else 0,
            "engagement_rate": (
                ((likes + comments + shares) / max(views, 1)) * 100
            ) if views > 0 else 0,
            "watch_time_avg": watch_time_avg * 100,
            "completion_rate": completion_rate * 100,
            "ctr": click_through_rate * 100,
            "conversions": min(100, conversions * 10),
        }

        timestamp = datetime.now(timezone.utc).isoformat()

        return PerformanceScore(
            overall_score=min(100, overall_score),
            engagement_score=min(100, engagement_score),
            retention_score=min(100, retention_score),
            conversion_score=min(100, conversion_score),
            metrics_breakdown=breakdown,
            timestamp=timestamp
        )

    def _calculate_engagement_score(self, views: float, likes: float,
                                  comments: float, shares: float) -> float:
        """Calculate engagement score (0-100)."""
        if views == 0:
            return 0

        # Engagement rate components
        like_rate = (likes / views) * 100
        comment_rate = (comments / views) * 100
        share_rate = (shares / views) * 100

        # Weighted formula: shares are most valuable, then comments, then likes
        # Typical good engagement: 3% likes, 0.5% comments, 0.2% shares
        weighted_score = (
            min(like_rate / 3.0, 1.0) * 0.3 +      # Like rate (compare to 3%)
            min(comment_rate / 0.5, 1.0) * 0.5 +    # Comment rate (compare to 0.5%)
            min(share_rate / 0.2, 1.0) * 0.8        # Share rate (compare to 0.2%)
        ) * 100

        return min(weighted_score, 100)

    def _calculate_retention_score(self, watch_time_avg: float,
                                  completion_rate: float) -> float:
        """Calculate retention score (0-100)."""
        # Equal weight to watch time and completion
        score = ((watch_time_avg + completion_rate) / 2) * 100
        return min(score, 100)

    def _calculate_conversion_score(self, ctr: float, conversions: float,
                                  views: float) -> float:
        """Calculate conversion score (0-100)."""
        if views == 0:
            return 0

        # CTR is primary signal
        ctr_score = ctr * 100

        # Conversions add to the score
        conversion_rate = conversions / max(views, 1)
        conversion_score = conversion_rate * 100

        # Weighted blend
        score = (ctr_score * 0.6) + (conversion_score * 0.4)
        return min(score, 100)
