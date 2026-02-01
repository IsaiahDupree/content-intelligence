"""
Metrics Service for PRD2
Handles polling external data, tracking comments, and scoring performance
"""
from datetime import datetime
from uuid import UUID, uuid4
from typing import List, Dict, Any, Optional

from models.supabase_models import (
    PostingMetrics, Comment, SentimentLabel, Platform
)

class MetricsService:
    """Service to poll metrics and calculate insights"""

    def poll_metrics(self, metrics_row: PostingMetrics, external_id: str, platform: Platform):
        """
        Poll external platform for metrics
        updates the metrics_row in place (simulating update)
        """
        data = self._fetch_external_data(external_id, platform)
        
        # Update fields
        metrics_row.views = data.get("views", 0)
        metrics_row.likes = data.get("likes", 0)
        metrics_row.comments_count = data.get("comments_count", 0)
        metrics_row.shares = data.get("shares", 0)
        metrics_row.watch_time_sec = data.get("watch_time", 0.0)
        metrics_row.ctr = data.get("ctr", 0.0)
        metrics_row.raw_payload = data
        
        # Calculate Post-Social Score
        metrics_row.post_social_score = self._calculate_post_social_score(metrics_row)

    def process_comments(self, schedule_id: UUID, raw_comments: List[Dict[str, Any]]) -> List[Comment]:
        """
        Process raw comments from platform
        """
        comments = []
        for rc in raw_comments:
            sentiment = self._analyze_sentiment(rc.get("text", ""))
            
            c = Comment(
                id=uuid4(),
                schedule_id=schedule_id,
                platform_comment_id=rc.get("id", "unknown"),
                author_handle=rc.get("author", "anon"),
                text=rc.get("text", ""),
                like_count=rc.get("likes", 0),
                sentiment_score=sentiment["score"],
                sentiment_label=sentiment["label"],
                topic_tags=sentiment["topics"]
            )
            comments.append(c)
        return comments

    def _fetch_external_data(self, external_id: str, platform: Platform) -> Dict[str, Any]:
        """Mock RapidAPI/TikTok API call"""
        # Return dummy data based on ID for deterministic testing
        if "viral" in external_id:
            return {
                "views": 1000000, "likes": 100000, "comments_count": 5000,
                "shares": 10000, "watch_time": 15.5, "ctr": 5.2
            }
        return {
            "views": 100, "likes": 1, "comments_count": 0,
            "shares": 0, "watch_time": 3.0, "ctr": 1.1
        }

    def _calculate_post_social_score(self, metrics: PostingMetrics) -> float:
        """
        Calculate score 0-100 based on engagement rate and retention
        Simple formula:
        EngRate = (Likes + Comments + Shares) / Views
        Score = min(100, EngRate * 1000)
        """
        if metrics.views == 0:
            return 0.0
            
        engagement = metrics.likes + metrics.comments_count + metrics.shares
        rate = engagement / metrics.views
        
        # Normalize: say 10% engagement is 100 score
        score = rate * 1000  # 0.1 * 1000 = 100
        return min(100.0, score)

    def _analyze_sentiment(self, text: str) -> Dict[str, Any]:
        """Mock sentiment analysis"""
        text_lower = text.lower()
        if "love" in text_lower or "amazing" in text_lower:
            return {"score": 0.9, "label": SentimentLabel.POSITIVE, "topics": ["praise"]}
        if "hate" in text_lower or "worst" in text_lower:
            return {"score": -0.9, "label": SentimentLabel.NEGATIVE, "topics": ["complaint"]}
            
        return {"score": 0.0, "label": SentimentLabel.NEUTRAL, "topics": []}
