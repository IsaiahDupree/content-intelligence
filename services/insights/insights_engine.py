"""
Insights Engine - Pattern Detection and Recommendations
Analyzes content performance to generate actionable insights
"""
import logging
from typing import Dict, List, Optional, Any
from datetime import datetime, timedelta
from sqlalchemy.orm import Session
from sqlalchemy import func, and_
from database.models import (
    PlatformPost, PlatformCheckback, PostComment,
    AnalyzedVideo, VideoSegment, VideoFrame,
    ContentInsight
)
import uuid

logger = logging.getLogger(__name__)


class InsightsEngine:
    """Generates insights and recommendations from content data"""
    
    def __init__(self, db: Session):
        """
        Initialize insights engine
        
        Args:
            db: Database session
        """
        self.db = db
    
    def detect_hook_patterns(
        self,
        min_sample_size: int = 10,
        lookback_days: int = 30
    ) -> List[Dict[str, Any]]:
        """
        Detect which hook types perform best
        
        Args:
            min_sample_size: Minimum posts needed for pattern
            lookback_days: Days to analyze
            
        Returns:
            List of hook insights
        """
        cutoff_date = datetime.now() - timedelta(days=lookback_days)
        
        # Get all posts with analyzed videos
        posts_with_hooks = self.db.query(
            VideoSegment.hook_type,
            func.avg(PlatformCheckback.avg_watch_pct).label('avg_retention'),
            func.avg(PlatformCheckback.like_rate).label('avg_like_rate'),
            func.count(PlatformPost.id).label('sample_size')
        ).join(
            AnalyzedVideo, VideoSegment.video_id == AnalyzedVideo.id
        ).join(
            PlatformPost, AnalyzedVideo.id == PlatformPost.id  # Simplified - needs proper linking
        ).join(
            PlatformCheckback, PlatformPost.id == PlatformCheckback.platform_post_id
        ).filter(
            VideoSegment.segment_type == 'hook',
            VideoSegment.hook_type.isnot(None),
            PlatformPost.published_at >= cutoff_date
        ).group_by(
            VideoSegment.hook_type
        ).having(
            func.count(PlatformPost.id) >= min_sample_size
        ).all()
        
        insights = []
        
        for hook_type, avg_retention, avg_like_rate, sample_size in posts_with_hooks:
            # Find best performing hook type
            if avg_retention and avg_retention > 0.65:  # 65% retention threshold
                insights.append({
                    "insight_type": "hook",
                    "title": f"{hook_type.title()} hooks drive {int(avg_retention * 100)}% retention",
                    "description": f"Videos using {hook_type} hooks averaged {int(avg_retention * 100)}% retention, {int((avg_like_rate or 0) * 100)}% like rate",
                    "metric_impact": f"+{int((avg_retention - 0.5) / 0.5 * 100)}% vs baseline",
                    "sample_size": int(sample_size),
                    "confidence_score": min(sample_size / 30, 1.0),  # Higher sample = higher confidence
                    "pattern_data": {
                        "hook_type": hook_type,
                        "avg_retention": float(avg_retention),
                        "avg_like_rate": float(avg_like_rate or 0)
                    }
                })
        
        return insights
    
    def detect_optimal_posting_times(
        self,
        platform: str,
        lookback_days: int = 30
    ) -> Dict[str, Any]:
        """
        Detect best times to post on a platform
        
        Args:
            platform: Platform name
            lookback_days: Days to analyze
            
        Returns:
            Posting time insights
        """
        cutoff_date = datetime.now() - timedelta(days=lookback_days)
        
        # Get posts with performance data
        posts = self.db.query(
            PlatformPost,
            func.max(PlatformCheckback.views).label('max_views')
        ).join(
            PlatformCheckback
        ).filter(
            PlatformPost.platform == platform,
            PlatformPost.published_at >= cutoff_date
        ).group_by(
            PlatformPost.id
        ).all()
        
        # Group by hour of day
        hourly_performance = {}
        
        for post, max_views in posts:
            hour = post.published_at.hour
            
            if hour not in hourly_performance:
                hourly_performance[hour] = {
                    "total_views": 0,
                    "post_count": 0
                }
            
            hourly_performance[hour]["total_views"] += max_views or 0
            hourly_performance[hour]["post_count"] += 1
        
        # Calculate average views per hour
        hourly_avg = {
            hour: data["total_views"] / data["post_count"]
            for hour, data in hourly_performance.items()
            if data["post_count"] > 0
        }
        
        # Find peak hours
        if hourly_avg:
            best_hour = max(hourly_avg, key=hourly_avg.get)
            best_views = hourly_avg[best_hour]
            
            return {
                "platform": platform,
                "best_posting_hour": best_hour,
                "avg_views_at_best_time": int(best_views),
                "hourly_breakdown": {
                    str(h): int(v) for h, v in sorted(hourly_avg.items())
                },
                "recommendation": f"Post between {best_hour}:00-{(best_hour+1)%24}:00 for best performance"
            }
        
        return {"message": "Insufficient data"}
    
    def detect_high_performing_topics(
        self,
        lookback_days: int = 30,
        min_sample_size: int = 5
    ) -> List[Dict[str, Any]]:
        """
        Detect topics/themes that perform well
        
        Args:
            lookback_days: Days to analyze
            min_sample_size: Minimum posts per topic
            
        Returns:
            Topic insights
        """
        cutoff_date = datetime.now() - timedelta(days=lookback_days)
        
        # Get segments with their performance
        segments = self.db.query(
            VideoSegment.focus,
            func.avg(PlatformCheckback.engagement_rate).label('avg_engagement'),
            func.count(PlatformPost.id).label('sample_size')
        ).join(
            AnalyzedVideo, VideoSegment.video_id == AnalyzedVideo.id
        ).join(
            PlatformPost, AnalyzedVideo.id == PlatformPost.id  # Simplified
        ).join(
            PlatformCheckback, PlatformPost.id == PlatformCheckback.platform_post_id
        ).filter(
            VideoSegment.focus.isnot(None),
            PlatformPost.published_at >= cutoff_date
        ).group_by(
            VideoSegment.focus
        ).having(
            func.count(PlatformPost.id) >= min_sample_size
        ).order_by(
            func.avg(PlatformCheckback.engagement_rate).desc()
        ).limit(10).all()
        
        topics = []
        
        for focus, avg_engagement, sample_size in segments:
            if avg_engagement and avg_engagement > 0.05:  # 5% engagement threshold
                topics.append({
                    "topic": focus,
                    "avg_engagement_rate": round(float(avg_engagement) * 100, 2),
                    "posts_analyzed": int(sample_size),
                    "recommendation": f"Create more content about: {focus}"
                })
        
        return topics
    
    def generate_content_recommendations(
        self,
        user_id: Optional[uuid.UUID] = None,
        limit: int = 10
    ) -> List[Dict[str, Any]]:
        """
        Generate personalized content recommendations
        
        Args:
            user_id: Optional user ID filter
            limit: Max recommendations
            
        Returns:
            List of recommendations
        """
        recommendations = []
        
        # 1. Hook recommendations
        hook_insights = self.detect_hook_patterns(min_sample_size=5)
        
        if hook_insights:
            best_hook = max(hook_insights, key=lambda x: x['confidence_score'])
            recommendations.append({
                "type": "hook_strategy",
                "priority": "high",
                "title": f"Use {best_hook['pattern_data']['hook_type']} hooks",
                "description": best_hook['description'],
                "expected_impact": best_hook['metric_impact'],
                "action": f"Try starting your next video with a {best_hook['pattern_data']['hook_type']} hook"
            })
        
        # 2. Posting time recommendations
        platforms = ["tiktok", "instagram", "youtube"]
        for platform in platforms:
            timing = self.detect_optimal_posting_times(platform, lookback_days=30)
            
            if timing and "best_posting_hour" in timing:
                recommendations.append({
                    "type": "posting_schedule",
                    "priority": "medium",
                    "title": f"Optimal {platform.title()} posting time",
                    "description": timing["recommendation"],
                    "expected_impact": f"+{int((timing['avg_views_at_best_time'] / 1000) * 10)}% views",
                    "action": f"Schedule next {platform} post for {timing['best_posting_hour']}:00"
                })
        
        # 3. Topic recommendations
        topics = self.detect_high_performing_topics(lookback_days=30)
        
        if topics:
            top_topic = topics[0]
            recommendations.append({
                "type": "content_topic",
                "priority": "high",
                "title": f"Focus on high-performing topic",
                "description": f"'{top_topic['topic']}' generates {top_topic['avg_engagement_rate']}% engagement",
                "expected_impact": f"+{int(top_topic['avg_engagement_rate'] * 5)}% engagement",
                "action": f"Create content about: {top_topic['topic']}"
            })
        
        # 4. CTA recommendations (analyze comment sentiment)
        cta_posts = self.db.query(PostComment.is_cta_response, func.count())\
            .filter(PostComment.is_cta_response.isnot(None))\
            .group_by(PostComment.is_cta_response)\
            .all()
        
        if cta_posts:
            recommendations.append({
                "type": "cta_strategy",
                "priority": "medium",
                "title": "Add clear CTAs",
                "description": "Posts with clear CTAs get more engagement",
                "expected_impact": "+15% comment rate",
                "action": "End videos with a specific CTA (comment keyword, link click, etc.)"
            })
        
        return recommendations[:limit]
    
    def store_insight(
        self,
        insight: Dict[str, Any],
        expires_days: int = 30
    ) -> uuid.UUID:
        """
        Store an insight in the database
        
        Args:
            insight: Insight data
            expires_days: Days until insight expires
            
        Returns:
            Insight ID
        """
        expires_at = datetime.now() + timedelta(days=expires_days)
        
        content_insight = ContentInsight(
            insight_type=insight.get("insight_type", "general"),
            title=insight["title"],
            description=insight["description"],
            metric_impact=insight.get("metric_impact"),
            sample_size=insight.get("sample_size"),
            confidence_score=insight.get("confidence_score", 0.5),
            pattern_data=insight.get("pattern_data"),
            recommended_action=insight.get("recommended_action"),
            expires_at=expires_at,
            status="active"
        )
        
        self.db.add(content_insight)
        self.db.commit()
        self.db.refresh(content_insight)
        
        return content_insight.id
