"""
Trend Clustering
================
Groups similar trends across platforms into clusters.
"""

import logging
from typing import List, Dict, Any, Optional
from collections import defaultdict

from .models import TrendCard, TrendCluster

logger = logging.getLogger(__name__)


class TrendClusterer:
    """
    Clusters trends by meaning, not just keywords.
    
    Groups:
    - "IG Reels trend" + "TikTok sound trend" + "YouTube Shorts trend" → one cluster
    - Summarizes cluster into: what changed, why people care, what's the debate
    """
    
    def __init__(self):
        """Initialize clusterer."""
        pass
    
    def cluster_trends(self, trends: List[TrendCard]) -> List[TrendCluster]:
        """
        Cluster trends by semantic similarity.
        
        Args:
            trends: List of trend cards
        
        Returns:
            List of trend clusters
        """
        if not trends:
            return []
        
        # Simple clustering by trend name similarity
        # In production, would use semantic similarity (embeddings)
        clusters: Dict[str, List[TrendCard]] = defaultdict(list)
        
        for trend in trends:
            # Normalize trend name for clustering
            normalized = self._normalize_trend_name(trend.trend_name)
            cluster_key = normalized
            
            # Check for similar existing clusters
            matched_cluster = None
            for existing_key in clusters.keys():
                if self._are_similar(normalized, existing_key):
                    matched_cluster = existing_key
                    break
            
            if matched_cluster:
                clusters[matched_cluster].append(trend)
            else:
                clusters[cluster_key].append(trend)
        
        # Create cluster objects
        result = []
        for cluster_id, cluster_trends in clusters.items():
            cluster = self._create_cluster(cluster_id, cluster_trends)
            result.append(cluster)
        
        return result
    
    def _normalize_trend_name(self, name: str) -> str:
        """Normalize trend name for clustering."""
        # Remove common prefixes/suffixes
        name = name.lower().strip()
        name = name.replace("#", "")
        name = name.replace("trend", "").strip()
        return name
    
    def _are_similar(self, name1: str, name2: str, threshold: float = 0.7) -> bool:
        """
        Check if two trend names are similar.
        
        Simple implementation - in production would use semantic similarity.
        """
        # Simple word overlap check
        words1 = set(name1.split())
        words2 = set(name2.split())
        
        if not words1 or not words2:
            return False
        
        overlap = len(words1 & words2)
        union = len(words1 | words2)
        
        similarity = overlap / union if union > 0 else 0.0
        
        return similarity >= threshold
    
    def _create_cluster(self, cluster_id: str, trends: List[TrendCard]) -> TrendCluster:
        """Create a cluster from trends."""
        # Aggregate metrics
        total_views = sum(t.views_growth * 1000 for t in trends)  # Rough estimate
        avg_velocity = sum(t.views_growth for t in trends) / len(trends) if trends else 0.0
        
        # Generate summary
        what_changed = self._summarize_what_changed(trends)
        why_people_care = self._summarize_why_care(trends)
        what_debate = self._summarize_debate(trends)
        
        # Cluster name from most common trend name
        cluster_name = trends[0].trend_name if trends else cluster_id
        
        return TrendCluster(
            cluster_id=cluster_id,
            name=cluster_name,
            trends=trends,
            what_changed=what_changed,
            why_people_care=why_people_care,
            what_debate=what_debate,
            total_views=total_views,
            avg_velocity=avg_velocity
        )
    
    def _summarize_what_changed(self, trends: List[TrendCard]) -> str:
        """Summarize what changed in this cluster."""
        # Aggregate from trends
        formats = [t.format for t in trends if t.format]
        if formats:
            return f"Trending {formats[0]} format across multiple platforms"
        return "Trending content format"
    
    def _summarize_why_care(self, trends: List[TrendCard]) -> str:
        """Summarize why people care."""
        # Aggregate from trends
        stuck_on = [t.what_people_stuck_on for t in trends if t.what_people_stuck_on]
        if stuck_on:
            return stuck_on[0]
        return "People are engaging with this content"
    
    def _summarize_debate(self, trends: List[TrendCard]) -> Optional[str]:
        """Summarize the debate around this trend."""
        # Could analyze comments for debate signals
        return None

