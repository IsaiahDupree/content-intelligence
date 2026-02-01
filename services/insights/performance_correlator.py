"""
Performance Correlation Engine
Links video segments to post performance metrics and identifies successful patterns
"""
import logging
from typing import List, Dict, Optional, Any, Tuple
from datetime import datetime, timedelta
from sqlalchemy.orm import Session
from sqlalchemy import func, desc, asc
import uuid
import numpy as np

from database.models import (
    VideoSegment,
    SegmentPerformance,
    PlatformPost,
    AnalyzedVideo,
    ClipPost
)


logger = logging.getLogger(__name__)


class PerformanceCorrelator:
    """
    Service for correlating video segments with performance metrics
    
    Features:
    - Link segments to post metrics
    - Analyze top performing patterns (hooks, emotions, etc.)
    - Predict performance for new segments
    - Generate insights based on correlation data
    """
    
    def __init__(self, db: Session):
        self.db = db
    
    def correlate_segment_to_metrics(
        self, 
        segment_id: str, 
        post_ids: List[str]
    ) -> List[SegmentPerformance]:
        """
        Calculate and store performance metrics for a segment across specific posts
        
        Args:
            segment_id: UUID of the segment
            post_ids: List of post UUIDs to analyze
            
        Returns:
            List of created/updated SegmentPerformance records
        """
        segment = self.db.query(VideoSegment).filter(VideoSegment.id == uuid.UUID(str(segment_id))).first()
        if not segment:
            raise ValueError(f"Segment {segment_id} not found")
            
        results = []
        
        for post_id in post_ids:
            post = self.db.query(PlatformPost).filter(PlatformPost.id == uuid.UUID(str(post_id))).first()
            if not post:
                continue
                
            # In a real implementation, we would fetch granular retention data from the platform API
            # For now, we'll estimate segment performance based on overall post performance
            # and the segment's position in the video
            
            # Placeholder logic for metric attribution
            # 1. Get total post metrics
            total_views = getattr(post, 'views', 0) or 0
            total_likes = getattr(post, 'likes', 0) or 0
            total_comments = getattr(post, 'comments', 0) or 0
            total_shares = getattr(post, 'shares', 0) or 0
            
            # 2. Estimate retention based on segment position (simple decay model)
            # Start retention is higher, end retention is lower
            video_duration = segment.video.duration_seconds if segment.video else 60.0
            start_pct = segment.start_s / max(video_duration, 1.0)
            end_pct = segment.end_s / max(video_duration, 1.0)
            
            # Simple linear decay assumption: 100% at start -> 40% at end
            retention_start = max(1.0 - (start_pct * 0.6), 0.0)
            retention_end = max(1.0 - (end_pct * 0.6), 0.0)
            
            views_at_start = int(total_views * retention_start)
            views_at_end = int(total_views * retention_end)
            
            # Retention rate for this specific segment
            segment_retention = views_at_end / max(views_at_start, 1)
            
            # 3. Attribute engagement (proportional to duration)
            segment_duration = segment.end_s - segment.start_s
            duration_pct = segment_duration / max(video_duration, 1.0)
            
            # Check if existing record exists
            perf_record = self.db.query(SegmentPerformance).filter(
                SegmentPerformance.segment_id == uuid.UUID(str(segment_id)),
                SegmentPerformance.post_id == uuid.UUID(str(post_id))
            ).first()
            
            if not perf_record:
                perf_record = SegmentPerformance(
                    id=uuid.uuid4(),
                    segment_id=segment.id,
                    post_id=post.id
                )
                self.db.add(perf_record)
            
            # Update metrics
            perf_record.views_at_start = views_at_start
            perf_record.views_at_end = views_at_end
            perf_record.retention_rate = segment_retention
            
            # Estimate engagement during segment (weighted by segment importance/duration)
            # This is a heuristic; real data would come from timestamped comments/likes
            perf_record.likes_during = int(total_likes * duration_pct)
            perf_record.comments_during = int(total_comments * duration_pct)
            perf_record.shares_during = int(total_shares * duration_pct)
            
            perf_record.measured_at = datetime.now()
            
            results.append(perf_record)
            
        try:
            self.db.commit()
            return results
        except Exception as e:
            self.db.rollback()
            logger.error(f"Error correlating metrics: {e}")
            raise

    def find_top_performing_patterns(
        self, 
        pattern_type: str = "hook", 
        limit: int = 5
    ) -> List[Dict[str, Any]]:
        """
        Identify top performing patterns based on aggregated metrics
        
        Args:
            pattern_type: 'hook', 'emotion', 'topic', 'duration'
            limit: Number of patterns to return
            
        Returns:
            List of patterns with performance scores
        """
        if pattern_type == "hook":
            return self._analyze_hook_patterns(limit)
        elif pattern_type == "emotion":
            return self._analyze_emotion_patterns(limit)
        elif pattern_type == "duration":
            return self._analyze_duration_patterns(limit)
        else:
            return []

    def _analyze_hook_patterns(self, limit: int) -> List[Dict[str, Any]]:
        """Analyze which hook psychology patterns perform best"""
        # Get all hook segments with performance data
        segments = self.db.query(VideoSegment, SegmentPerformance).join(
            SegmentPerformance, VideoSegment.id == SegmentPerformance.segment_id
        ).filter(
            VideoSegment.segment_type == "hook"
        ).all()
        
        pattern_stats = {}
        
        for segment, perf in segments:
            # Extract FATE patterns
            fate_tags = [segment.hook_type] if segment.hook_type else []
            if not fate_tags:
                continue
                
            score = perf.engagement_score or 0
            
            for tag in fate_tags:
                if tag not in pattern_stats:
                    pattern_stats[tag] = {"total_score": 0, "count": 0}
                pattern_stats[tag]["total_score"] += score
                pattern_stats[tag]["count"] += 1
        
        # Calculate averages
        results = []
        for tag, stats in pattern_stats.items():
            if stats["count"] > 0:
                avg_score = stats["total_score"] / stats["count"]
                results.append({
                    "pattern": tag,
                    "avg_score": avg_score,
                    "sample_size": stats["count"],
                    "type": "hook_psychology"
                })
        
        # Sort by score
        return sorted(results, key=lambda x: x["avg_score"], reverse=True)[:limit]

    def _analyze_emotion_patterns(self, limit: int) -> List[Dict[str, Any]]:
        """Analyze which emotions drive the most engagement"""
        segments = self.db.query(VideoSegment, SegmentPerformance).join(
            SegmentPerformance, VideoSegment.id == SegmentPerformance.segment_id
        ).all()
        
        emotion_stats = {}
        
        for segment, perf in segments:
            emotions = [segment.emotion] if segment.emotion else []
            score = perf.engagement_score or 0
            
            for emotion in emotions:
                if emotion not in emotion_stats:
                    emotion_stats[emotion] = {"total_score": 0, "count": 0}
                emotion_stats[emotion]["total_score"] += score
                emotion_stats[emotion]["count"] += 1
                
        results = []
        for emotion, stats in emotion_stats.items():
            if stats["count"] > 0:
                results.append({
                    "pattern": emotion,
                    "avg_score": stats["total_score"] / stats["count"],
                    "sample_size": stats["count"],
                    "type": "emotion"
                })
                
        return sorted(results, key=lambda x: x["avg_score"], reverse=True)[:limit]

    def _analyze_duration_patterns(self, limit: int) -> List[Dict[str, Any]]:
        """Analyze optimal segment durations"""
        segments = self.db.query(VideoSegment, SegmentPerformance).join(
            SegmentPerformance, VideoSegment.id == SegmentPerformance.segment_id
        ).all()
        
        # Bucket durations: 0-5s, 5-10s, 10-15s, etc.
        buckets = {}
        bucket_size = 5.0
        
        for segment, perf in segments:
            duration = segment.end_s - segment.start_s
            bucket_idx = int(duration / bucket_size)
            bucket_key = f"{bucket_idx * bucket_size}-{(bucket_idx + 1) * bucket_size}s"
            
            if bucket_key not in buckets:
                buckets[bucket_key] = {"total_score": 0, "count": 0}
            
            buckets[bucket_key]["total_score"] += (perf.engagement_score or 0)
            buckets[bucket_key]["count"] += 1
            
        results = []
        for bucket, stats in buckets.items():
            if stats["count"] > 0:
                results.append({
                    "pattern": bucket,
                    "avg_score": stats["total_score"] / stats["count"],
                    "sample_size": stats["count"],
                    "type": "duration"
                })
                
        return sorted(results, key=lambda x: x["avg_score"], reverse=True)[:limit]

    def predict_segment_performance(self, segment_data: Dict[str, Any]) -> Dict[str, Any]:
        """
        Predict performance score for a potential segment based on historical data
        
        Args:
            segment_data: Dict containing 'segment_type', 'psychology_tags', 'duration'
            
        Returns:
            Dict with predicted score and confidence
        """
        # 1. Get baseline score for segment type
        seg_type = segment_data.get("segment_type", "body")
        type_baseline = 0.5  # Default
        
        # 2. Adjust based on psychology tags (hooks, emotions)
        tags = segment_data.get("psychology_tags", {})
        fate_patterns = tags.get("fate_patterns", [])
        emotions = tags.get("emotions", [])
        
        # Get historical performance for these patterns
        top_hooks = self.find_top_performing_patterns("hook", limit=100)
        top_emotions = self.find_top_performing_patterns("emotion", limit=100)
        
        hook_modifier = 0
        if fate_patterns and top_hooks:
            matches = [p for p in top_hooks if p["pattern"] in fate_patterns]
            if matches:
                avg_match_score = sum(m["avg_score"] for m in matches) / len(matches)
                hook_modifier = (avg_match_score - 0.5) * 0.5  # Weight factor
        
        emotion_modifier = 0
        if emotions and top_emotions:
            matches = [p for p in top_emotions if p["pattern"] in emotions]
            if matches:
                avg_match_score = sum(m["avg_score"] for m in matches) / len(matches)
                emotion_modifier = (avg_match_score - 0.5) * 0.3  # Weight factor
        
        # 3. Calculate final prediction
        predicted_score = type_baseline + hook_modifier + emotion_modifier
        predicted_score = max(0.1, min(0.95, predicted_score))  # Clamp
        
        return {
            "predicted_score": round(predicted_score, 2),
            "confidence": "medium" if (hook_modifier or emotion_modifier) else "low",
            "factors": {
                "baseline": type_baseline,
                "hook_impact": round(hook_modifier, 2),
                "emotion_impact": round(emotion_modifier, 2)
            }
        }
