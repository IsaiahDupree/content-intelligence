"""
YouTube Channel Analyzer (TubeLab-style)
=========================================
Provides channel-level analytics with computed metrics:
- Revenue estimates (30d)
- RPM estimates
- Typical views
- Views/sub multiplier
- Velocity metrics
- Insight pills (high demand, loyal viewers, etc.)

Based on TubeLab's Niche Finder / Discover experience.
"""
import os
import re
import json
import asyncio
import statistics
from datetime import datetime, timedelta, timezone
from typing import List, Dict, Any, Optional, Tuple
from dataclasses import dataclass, field, asdict
from collections import defaultdict
from enum import Enum

import httpx
from loguru import logger
from sqlalchemy import create_engine, text

DATABASE_URL = os.getenv("DATABASE_URL", "postgresql://postgres:postgres@localhost:54322/postgres")
YOUTUBE_API_KEY = os.getenv("YOUTUBE_API_KEY", "")


class InsightType(str, Enum):
    """Insight pill types like TubeLab"""
    HIGH_DEMAND = "high_demand"
    LOYAL_VIEWERS = "loyal_viewers"
    HIGH_COMMITMENT = "high_commitment"
    HIGH_QUALITY = "high_quality"
    FACELESS = "faceless"
    CASH_COW = "cash_cow"
    BREAKOUT = "breakout"
    CONSISTENT = "consistent"
    VIRAL_POTENTIAL = "viral_potential"


@dataclass
class ChannelInsight:
    """An insight pill for a channel"""
    type: InsightType
    label: str
    tooltip: str
    score: float = 0.0
    confidence: float = 0.0


@dataclass
class VideoSnapshot:
    """A video with its metrics"""
    video_id: str
    title: str
    published_at: datetime
    views: int
    likes: int
    comments: int
    duration_seconds: int
    thumbnail_url: str
    description: str = ""
    tags: List[str] = field(default_factory=list)
    
    @property
    def like_rate(self) -> float:
        return (self.likes / self.views * 100) if self.views > 0 else 0.0
    
    @property
    def comment_rate(self) -> float:
        return (self.comments / self.views * 1000) if self.views > 0 else 0.0
    
    @property
    def engagement_rate(self) -> float:
        return ((self.likes + self.comments) / self.views * 100) if self.views > 0 else 0.0


@dataclass
class ChannelMetrics:
    """Full channel analytics like TubeLab"""
    channel_id: str
    title: str
    handle: str = ""
    description: str = ""
    country: str = ""
    language: str = ""
    created_at: Optional[datetime] = None
    
    # Core stats
    subscribers: int = 0
    total_views: int = 0
    total_videos: int = 0
    last_upload_at: Optional[datetime] = None
    
    # Computed metrics (TubeLab style)
    typical_views: int = 0              # Median views of last N uploads
    views_sub_multiplier: float = 0.0   # typical_views / subs
    velocity_7d: float = 0.0            # Avg views in first 7 days
    velocity_30d: float = 0.0           # Avg views growth rate
    active_days: int = 0                # Days since first upload
    uploads_30d: int = 0                # Videos uploaded in last 30d
    views_30d: int = 0                  # Est. views in last 30d
    
    # Revenue estimates
    rpm_estimate: float = 0.0           # Estimated RPM ($)
    revenue_30d_estimate: float = 0.0   # (views_30d / 1000) * rpm
    monetization_likelihood: float = 0.0 # 0-1 probability monetized
    
    # Engagement metrics
    avg_like_rate: float = 0.0          # Avg likes/views %
    avg_comment_rate: float = 0.0       # Avg comments/1k views
    avg_engagement_rate: float = 0.0    # Combined
    
    # Content analysis
    avg_duration_seconds: int = 0
    content_type: str = ""              # "long_form", "shorts", "mixed"
    is_faceless: bool = False
    upload_consistency: float = 0.0     # 0-1 score
    
    # Niche info
    niche_tags: List[str] = field(default_factory=list)
    niche_confidence: float = 0.0
    
    # Insight pills
    insights: List[ChannelInsight] = field(default_factory=list)
    
    # Recent videos
    recent_videos: List[VideoSnapshot] = field(default_factory=list)
    top_videos: List[VideoSnapshot] = field(default_factory=list)
    
    # Thumbnail URL
    thumbnail_url: str = ""
    banner_url: str = ""
    
    def to_dict(self) -> Dict:
        """Convert to JSON-serializable dict"""
        data = asdict(self)
        # Convert datetime fields
        if data.get('created_at'):
            data['created_at'] = data['created_at'].isoformat()
        if data.get('last_upload_at'):
            data['last_upload_at'] = data['last_upload_at'].isoformat()
        # Convert insights
        data['insights'] = [
            {
                'type': i['type'].value if hasattr(i['type'], 'value') else i['type'],
                'label': i['label'],
                'tooltip': i['tooltip'],
                'score': i['score'],
                'confidence': i['confidence']
            }
            for i in data['insights']
        ]
        # Convert videos
        for v in data.get('recent_videos', []):
            if v.get('published_at'):
                v['published_at'] = v['published_at'].isoformat() if hasattr(v['published_at'], 'isoformat') else v['published_at']
        for v in data.get('top_videos', []):
            if v.get('published_at'):
                v['published_at'] = v['published_at'].isoformat() if hasattr(v['published_at'], 'isoformat') else v['published_at']
        return data


# RPM estimates by niche (rough industry averages)
RPM_BY_NICHE = {
    "finance": 12.0,
    "investing": 15.0,
    "business": 10.0,
    "marketing": 8.0,
    "technology": 6.0,
    "programming": 5.0,
    "gaming": 2.5,
    "entertainment": 3.0,
    "music": 2.0,
    "lifestyle": 4.0,
    "health": 7.0,
    "fitness": 5.0,
    "education": 5.0,
    "cooking": 4.0,
    "travel": 5.0,
    "default": 4.0
}


class YouTubeChannelAnalyzer:
    """
    Analyzes YouTube channels with TubeLab-style metrics.
    
    Key features:
    - Revenue/RPM estimation
    - Insight pills (high demand, loyal viewers, etc.)
    - Niche detection
    - Breakout/velocity scoring
    """
    
    def __init__(self, api_key: str = None):
        self.api_key = api_key or YOUTUBE_API_KEY
        self._client: Optional[httpx.AsyncClient] = None
        self.engine = create_engine(DATABASE_URL)
    
    async def _get_client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=30.0)
        return self._client
    
    async def close(self):
        if self._client:
            await self._client.aclose()
            self._client = None
    
    # =========================================================================
    # Core API Methods
    # =========================================================================
    
    async def fetch_channel_details(self, channel_id: str) -> Optional[Dict]:
        """Fetch channel details from YouTube API"""
        if not self.api_key:
            logger.warning("No YouTube API key configured")
            return None
        
        client = await self._get_client()
        url = "https://www.googleapis.com/youtube/v3/channels"
        params = {
            "part": "snippet,statistics,contentDetails,brandingSettings,status",
            "id": channel_id,
            "key": self.api_key
        }
        
        try:
            response = await client.get(url, params=params)
            data = response.json()
            
            if "items" in data and len(data["items"]) > 0:
                return data["items"][0]
            return None
        except Exception as e:
            logger.error(f"Error fetching channel {channel_id}: {e}")
            return None
    
    async def fetch_channel_videos(
        self,
        channel_id: str,
        max_results: int = 50,
        published_after: datetime = None
    ) -> List[Dict]:
        """Fetch recent videos from a channel"""
        if not self.api_key:
            return []
        
        client = await self._get_client()
        
        # First get uploads playlist ID
        channel_data = await self.fetch_channel_details(channel_id)
        if not channel_data:
            return []
        
        uploads_playlist_id = channel_data.get("contentDetails", {}).get(
            "relatedPlaylists", {}
        ).get("uploads")
        
        if not uploads_playlist_id:
            return []
        
        # Fetch playlist items
        url = "https://www.googleapis.com/youtube/v3/playlistItems"
        params = {
            "part": "snippet,contentDetails",
            "playlistId": uploads_playlist_id,
            "maxResults": min(max_results, 50),
            "key": self.api_key
        }
        
        video_ids = []
        try:
            response = await client.get(url, params=params)
            data = response.json()
            
            for item in data.get("items", []):
                video_id = item.get("contentDetails", {}).get("videoId")
                if video_id:
                    video_ids.append(video_id)
        except Exception as e:
            logger.error(f"Error fetching playlist: {e}")
            return []
        
        if not video_ids:
            return []
        
        # Hydrate video details
        return await self._fetch_video_details(video_ids)
    
    async def _fetch_video_details(self, video_ids: List[str]) -> List[Dict]:
        """Fetch full video details in batches"""
        if not video_ids:
            return []
        
        client = await self._get_client()
        all_videos = []
        
        # Process in batches of 50
        for i in range(0, len(video_ids), 50):
            batch = video_ids[i:i+50]
            url = "https://www.googleapis.com/youtube/v3/videos"
            params = {
                "part": "snippet,statistics,contentDetails",
                "id": ",".join(batch),
                "key": self.api_key
            }
            
            try:
                response = await client.get(url, params=params)
                data = response.json()
                all_videos.extend(data.get("items", []))
            except Exception as e:
                logger.error(f"Error fetching video details: {e}")
        
        return all_videos
    
    # =========================================================================
    # Analysis Methods
    # =========================================================================
    
    async def analyze_channel(
        self,
        channel_id: str,
        videos_to_analyze: int = 30,
        detect_niche: bool = True
    ) -> Optional[ChannelMetrics]:
        """
        Full channel analysis with TubeLab-style metrics.
        
        Returns ChannelMetrics with:
        - Core stats (subs, views, uploads)
        - Computed metrics (typical_views, velocity, rpm_estimate)
        - Insight pills
        - Recent/top videos
        """
        # Fetch channel details
        channel_data = await self.fetch_channel_details(channel_id)
        if not channel_data:
            logger.warning(f"Could not fetch channel {channel_id}")
            return None
        
        # Fetch recent videos
        videos = await self.fetch_channel_videos(channel_id, max_results=videos_to_analyze)
        
        # Build channel metrics
        snippet = channel_data.get("snippet", {})
        stats = channel_data.get("statistics", {})
        branding = channel_data.get("brandingSettings", {})
        
        metrics = ChannelMetrics(
            channel_id=channel_id,
            title=snippet.get("title", ""),
            handle=snippet.get("customUrl", ""),
            description=snippet.get("description", ""),
            country=snippet.get("country", ""),
            language=snippet.get("defaultLanguage", ""),
            created_at=self._parse_datetime(snippet.get("publishedAt")),
            subscribers=int(stats.get("subscriberCount", 0)),
            total_views=int(stats.get("viewCount", 0)),
            total_videos=int(stats.get("videoCount", 0)),
            thumbnail_url=snippet.get("thumbnails", {}).get("high", {}).get("url", ""),
            banner_url=branding.get("image", {}).get("bannerExternalUrl", "")
        )
        
        # Process videos
        video_snapshots = []
        for v in videos:
            vs = self._video_to_snapshot(v)
            if vs:
                video_snapshots.append(vs)
        
        if video_snapshots:
            # Sort by date
            video_snapshots.sort(key=lambda x: x.published_at, reverse=True)
            metrics.recent_videos = video_snapshots[:10]
            
            # Get top videos by views
            metrics.top_videos = sorted(
                video_snapshots, key=lambda x: x.views, reverse=True
            )[:5]
            
            # Compute derived metrics
            metrics = self._compute_derived_metrics(metrics, video_snapshots)
        
        # Detect niche
        if detect_niche:
            metrics.niche_tags = self._detect_niche(metrics, video_snapshots)
        
        # Compute insight pills
        metrics.insights = self._compute_insights(metrics, video_snapshots)
        
        # Estimate revenue
        metrics = self._estimate_revenue(metrics)
        
        return metrics
    
    def _video_to_snapshot(self, video_data: Dict) -> Optional[VideoSnapshot]:
        """Convert YouTube API video to VideoSnapshot"""
        try:
            snippet = video_data.get("snippet", {})
            stats = video_data.get("statistics", {})
            content = video_data.get("contentDetails", {})
            
            return VideoSnapshot(
                video_id=video_data.get("id", ""),
                title=snippet.get("title", ""),
                published_at=self._parse_datetime(snippet.get("publishedAt")),
                views=int(stats.get("viewCount", 0)),
                likes=int(stats.get("likeCount", 0)),
                comments=int(stats.get("commentCount", 0)),
                duration_seconds=self._parse_duration(content.get("duration", "PT0S")),
                thumbnail_url=snippet.get("thumbnails", {}).get("high", {}).get("url", ""),
                description=snippet.get("description", ""),
                tags=snippet.get("tags", [])
            )
        except Exception as e:
            logger.error(f"Error parsing video: {e}")
            return None
    
    def _compute_derived_metrics(
        self,
        metrics: ChannelMetrics,
        videos: List[VideoSnapshot]
    ) -> ChannelMetrics:
        """Compute TubeLab-style derived metrics"""
        if not videos:
            return metrics
        
        now = datetime.now(timezone.utc)
        
        # Typical views (median of last N uploads)
        views_list = [v.views for v in videos]
        metrics.typical_views = int(statistics.median(views_list)) if views_list else 0
        
        # Views/sub multiplier
        if metrics.subscribers > 0:
            metrics.views_sub_multiplier = round(
                metrics.typical_views / metrics.subscribers, 2
            )
        
        # Last upload
        if videos:
            metrics.last_upload_at = videos[0].published_at
        
        # Active days
        if metrics.created_at:
            metrics.active_days = (now - metrics.created_at).days
        
        # Uploads in last 30 days
        cutoff_30d = now - timedelta(days=30)
        recent_videos = [v for v in videos if v.published_at >= cutoff_30d]
        metrics.uploads_30d = len(recent_videos)
        
        # Views in last 30 days (estimate from recent uploads)
        metrics.views_30d = sum(v.views for v in recent_videos)
        
        # Velocity (average views for videos 7-14 days old)
        cutoff_7d = now - timedelta(days=7)
        cutoff_14d = now - timedelta(days=14)
        velocity_videos = [
            v for v in videos 
            if cutoff_14d <= v.published_at < cutoff_7d
        ]
        if velocity_videos:
            metrics.velocity_7d = sum(v.views for v in velocity_videos) / len(velocity_videos)
        
        # 30d velocity (views per video)
        if recent_videos:
            metrics.velocity_30d = metrics.views_30d / len(recent_videos)
        
        # Engagement metrics
        like_rates = [v.like_rate for v in videos if v.views > 100]
        comment_rates = [v.comment_rate for v in videos if v.views > 100]
        engagement_rates = [v.engagement_rate for v in videos if v.views > 100]
        
        if like_rates:
            metrics.avg_like_rate = round(statistics.mean(like_rates), 2)
        if comment_rates:
            metrics.avg_comment_rate = round(statistics.mean(comment_rates), 2)
        if engagement_rates:
            metrics.avg_engagement_rate = round(statistics.mean(engagement_rates), 2)
        
        # Content type analysis
        durations = [v.duration_seconds for v in videos]
        if durations:
            metrics.avg_duration_seconds = int(statistics.mean(durations))
            
            shorts_count = sum(1 for d in durations if d <= 60)
            long_count = sum(1 for d in durations if d > 60)
            
            if shorts_count > len(durations) * 0.7:
                metrics.content_type = "shorts"
            elif long_count > len(durations) * 0.7:
                metrics.content_type = "long_form"
            else:
                metrics.content_type = "mixed"
        
        # Upload consistency (coefficient of variation of upload gaps)
        if len(videos) >= 3:
            upload_gaps = []
            sorted_videos = sorted(videos, key=lambda x: x.published_at)
            for i in range(1, len(sorted_videos)):
                gap = (sorted_videos[i].published_at - sorted_videos[i-1].published_at).days
                upload_gaps.append(gap)
            
            if upload_gaps and statistics.mean(upload_gaps) > 0:
                cv = statistics.stdev(upload_gaps) / statistics.mean(upload_gaps)
                # Lower CV = more consistent, convert to 0-1 score
                metrics.upload_consistency = round(max(0, 1 - cv / 2), 2)
        
        return metrics
    
    def _detect_niche(
        self,
        metrics: ChannelMetrics,
        videos: List[VideoSnapshot]
    ) -> List[str]:
        """Detect channel niche from content"""
        # Simple keyword-based detection for now
        # Can be upgraded to embedding-based clustering
        
        niche_keywords = {
            "finance": ["money", "investing", "stock", "crypto", "wealth", "financial", "budget"],
            "business": ["business", "entrepreneur", "startup", "company", "profit", "revenue"],
            "marketing": ["marketing", "ads", "brand", "growth", "traffic", "funnel", "conversion"],
            "technology": ["tech", "software", "app", "gadget", "review", "unboxing"],
            "programming": ["code", "programming", "developer", "javascript", "python", "tutorial"],
            "gaming": ["game", "gaming", "gameplay", "playthrough", "lets play", "stream"],
            "fitness": ["workout", "fitness", "gym", "exercise", "muscle", "weight"],
            "health": ["health", "diet", "nutrition", "wellness", "mental health"],
            "education": ["learn", "course", "tutorial", "explained", "how to"],
            "lifestyle": ["lifestyle", "vlog", "day in", "routine", "haul"],
            "entertainment": ["funny", "comedy", "prank", "challenge", "react"],
        }
        
        # Combine all text
        all_text = metrics.title.lower() + " " + metrics.description.lower()
        for v in videos[:20]:
            all_text += " " + v.title.lower() + " " + " ".join(v.tags).lower()
        
        # Score each niche
        niche_scores = {}
        for niche, keywords in niche_keywords.items():
            score = sum(1 for kw in keywords if kw in all_text)
            if score > 0:
                niche_scores[niche] = score
        
        # Return top niches
        sorted_niches = sorted(niche_scores.items(), key=lambda x: x[1], reverse=True)
        return [n[0] for n in sorted_niches[:3]]
    
    def _compute_insights(
        self,
        metrics: ChannelMetrics,
        videos: List[VideoSnapshot]
    ) -> List[ChannelInsight]:
        """Compute TubeLab-style insight pills"""
        insights = []
        
        # Scale thresholds based on channel size
        is_large_channel = metrics.subscribers >= 500000
        is_medium_channel = metrics.subscribers >= 50000
        
        # High Demand: views/sub threshold scales with size
        # Large channels (500k+): 0.1x is good
        # Medium channels (50k-500k): 0.3x is good
        # Small channels: 0.5x+ is good
        demand_threshold = 0.1 if is_large_channel else (0.3 if is_medium_channel else 0.5)
        if metrics.views_sub_multiplier >= demand_threshold and metrics.typical_views >= 5000:
            insights.append(ChannelInsight(
                type=InsightType.HIGH_DEMAND,
                label="High Demand",
                tooltip=f"Avg views are {metrics.views_sub_multiplier}× subscribers ({metrics.typical_views:,} typical)",
                score=metrics.views_sub_multiplier / demand_threshold,
                confidence=0.9 if metrics.views_sub_multiplier >= demand_threshold * 1.5 else 0.7
            ))
        
        # Loyal Viewers: threshold scales with channel size
        loyal_threshold = 0.08 if is_large_channel else (0.15 if is_medium_channel else 0.5)
        if metrics.subscribers > 0 and metrics.typical_views >= metrics.subscribers * loyal_threshold:
            insights.append(ChannelInsight(
                type=InsightType.LOYAL_VIEWERS,
                label="Loyal Viewers",
                tooltip=f"Typical views ≈ {int(metrics.typical_views/metrics.subscribers*100)}% of subscriber base",
                score=metrics.typical_views / metrics.subscribers,
                confidence=0.85
            ))
        
        # High Commitment: long-form + high comment rate
        if metrics.avg_duration_seconds >= 480 and metrics.avg_comment_rate >= 5:
            insights.append(ChannelInsight(
                type=InsightType.HIGH_COMMITMENT,
                label="High Commitment",
                tooltip=f"Long-form content ({metrics.avg_duration_seconds//60}min avg) with {metrics.avg_comment_rate:.1f} comments/1k views",
                score=metrics.avg_comment_rate / 5,
                confidence=0.8
            ))
        
        # High Quality: high engagement + consistent uploads
        if metrics.avg_like_rate >= 5 and metrics.upload_consistency >= 0.6:
            insights.append(ChannelInsight(
                type=InsightType.HIGH_QUALITY,
                label="High Quality",
                tooltip=f"{metrics.avg_like_rate:.1f}% like rate with consistent uploads",
                score=(metrics.avg_like_rate / 5 + metrics.upload_consistency) / 2,
                confidence=0.75
            ))
        
        # Faceless (placeholder - would need vision analysis)
        # For now, check description for faceless indicators
        faceless_keywords = ["no face", "faceless", "animation", "voiceover", "text-based"]
        desc_lower = metrics.description.lower()
        if any(kw in desc_lower for kw in faceless_keywords):
            insights.append(ChannelInsight(
                type=InsightType.FACELESS,
                label="Faceless",
                tooltip="Channel appears to use faceless content format",
                score=0.7,
                confidence=0.5
            ))
        
        # Cash Cow: high revenue potential
        if metrics.revenue_30d_estimate >= 5000 and metrics.views_sub_multiplier >= 1.0:
            insights.append(ChannelInsight(
                type=InsightType.CASH_COW,
                label="Cash Cow",
                tooltip=f"Est. ${metrics.revenue_30d_estimate:,.0f}/month revenue",
                score=metrics.revenue_30d_estimate / 10000,
                confidence=0.6
            ))
        
        # Breakout: rapid subscriber growth or viral videos
        if videos:
            max_views = max(v.views for v in videos)
            if max_views > metrics.typical_views * 5:
                insights.append(ChannelInsight(
                    type=InsightType.BREAKOUT,
                    label="Breakout",
                    tooltip=f"Has viral video with {max_views:,} views ({max_views//metrics.typical_views}× typical)",
                    score=max_views / metrics.typical_views / 10,
                    confidence=0.8
                ))
        
        # Consistent: regular uploads
        if metrics.upload_consistency >= 0.7 and metrics.uploads_30d >= 4:
            insights.append(ChannelInsight(
                type=InsightType.CONSISTENT,
                label="Consistent",
                tooltip=f"{metrics.uploads_30d} uploads in last 30 days with regular schedule",
                score=metrics.upload_consistency,
                confidence=0.85
            ))
        
        # Viral Potential: high engagement + views/sub
        if metrics.avg_engagement_rate >= 8 and metrics.views_sub_multiplier >= 1.2:
            insights.append(ChannelInsight(
                type=InsightType.VIRAL_POTENTIAL,
                label="Viral Potential",
                tooltip=f"{metrics.avg_engagement_rate:.1f}% engagement rate indicates shareability",
                score=metrics.avg_engagement_rate / 10,
                confidence=0.7
            ))
        
        return insights
    
    def _estimate_revenue(self, metrics: ChannelMetrics) -> ChannelMetrics:
        """Estimate channel revenue (TubeLab style)"""
        # Get RPM based on niche
        rpm = RPM_BY_NICHE.get("default", 4.0)
        for niche in metrics.niche_tags:
            if niche in RPM_BY_NICHE:
                rpm = RPM_BY_NICHE[niche]
                break
        
        # Adjust RPM based on engagement (higher engagement = better CPM)
        if metrics.avg_engagement_rate > 5:
            rpm *= 1.2
        elif metrics.avg_engagement_rate > 10:
            rpm *= 1.4
        
        # Adjust for content type (long-form gets better RPM)
        if metrics.content_type == "long_form":
            rpm *= 1.3
        elif metrics.content_type == "shorts":
            rpm *= 0.3  # Shorts have much lower RPM
        
        metrics.rpm_estimate = round(rpm, 2)
        
        # Monetization likelihood
        if metrics.subscribers >= 1000 and metrics.total_views >= 4000000:
            metrics.monetization_likelihood = 0.95
        elif metrics.subscribers >= 1000:
            # Estimate based on avg views
            watch_hours_est = (metrics.views_30d * metrics.avg_duration_seconds / 3600) * 12
            if watch_hours_est >= 4000:
                metrics.monetization_likelihood = 0.9
            else:
                metrics.monetization_likelihood = min(0.7, watch_hours_est / 4000)
        else:
            metrics.monetization_likelihood = metrics.subscribers / 1000 * 0.5
        
        # Revenue estimate
        metrics.revenue_30d_estimate = round(
            (metrics.views_30d / 1000) * metrics.rpm_estimate * metrics.monetization_likelihood,
            2
        )
        
        return metrics
    
    # =========================================================================
    # Utility Methods
    # =========================================================================
    
    def _parse_datetime(self, dt_str: str) -> Optional[datetime]:
        """Parse ISO datetime string"""
        if not dt_str:
            return None
        try:
            return datetime.fromisoformat(dt_str.replace("Z", "+00:00"))
        except:
            return None
    
    def _parse_duration(self, duration: str) -> int:
        """Parse ISO 8601 duration to seconds"""
        if not duration:
            return 0
        
        pattern = r'PT(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?'
        match = re.match(pattern, duration)
        if not match:
            return 0
        
        hours = int(match.group(1) or 0)
        minutes = int(match.group(2) or 0)
        seconds = int(match.group(3) or 0)
        
        return hours * 3600 + minutes * 60 + seconds
    
    # =========================================================================
    # Batch Analysis
    # =========================================================================
    
    async def analyze_channels_batch(
        self,
        channel_ids: List[str],
        videos_per_channel: int = 20
    ) -> List[ChannelMetrics]:
        """Analyze multiple channels"""
        results = []
        for channel_id in channel_ids:
            try:
                metrics = await self.analyze_channel(
                    channel_id,
                    videos_to_analyze=videos_per_channel
                )
                if metrics:
                    results.append(metrics)
            except Exception as e:
                logger.error(f"Error analyzing channel {channel_id}: {e}")
            
            # Rate limiting
            await asyncio.sleep(0.5)
        
        return results
    
    async def discover_channels_by_niche(
        self,
        query: str,
        max_results: int = 20
    ) -> List[ChannelMetrics]:
        """Discover channels by searching for a niche/keyword"""
        if not self.api_key:
            return []
        
        client = await self._get_client()
        url = "https://www.googleapis.com/youtube/v3/search"
        params = {
            "part": "snippet",
            "type": "channel",
            "q": query,
            "maxResults": min(max_results, 50),
            "key": self.api_key
        }
        
        try:
            response = await client.get(url, params=params)
            data = response.json()
            
            channel_ids = [
                item["id"]["channelId"]
                for item in data.get("items", [])
                if "channelId" in item.get("id", {})
            ]
            
            return await self.analyze_channels_batch(channel_ids)
        except Exception as e:
            logger.error(f"Error discovering channels: {e}")
            return []
    
    # =========================================================================
    # Database Methods
    # =========================================================================
    
    def save_channel_metrics(self, metrics: ChannelMetrics) -> bool:
        """Save channel metrics to database"""
        try:
            with self.engine.connect() as conn:
                conn.execute(text("""
                    INSERT INTO youtube_channel_metrics (
                        channel_id, title, handle, subscribers, total_views,
                        typical_views, views_sub_multiplier, velocity_7d,
                        rpm_estimate, revenue_30d_estimate, monetization_likelihood,
                        avg_like_rate, avg_comment_rate, content_type,
                        niche_tags, insights, updated_at
                    ) VALUES (
                        :channel_id, :title, :handle, :subscribers, :total_views,
                        :typical_views, :views_sub_multiplier, :velocity_7d,
                        :rpm_estimate, :revenue_30d_estimate, :monetization_likelihood,
                        :avg_like_rate, :avg_comment_rate, :content_type,
                        :niche_tags, :insights, NOW()
                    )
                    ON CONFLICT (channel_id) DO UPDATE SET
                        title = EXCLUDED.title,
                        subscribers = EXCLUDED.subscribers,
                        total_views = EXCLUDED.total_views,
                        typical_views = EXCLUDED.typical_views,
                        views_sub_multiplier = EXCLUDED.views_sub_multiplier,
                        velocity_7d = EXCLUDED.velocity_7d,
                        rpm_estimate = EXCLUDED.rpm_estimate,
                        revenue_30d_estimate = EXCLUDED.revenue_30d_estimate,
                        monetization_likelihood = EXCLUDED.monetization_likelihood,
                        avg_like_rate = EXCLUDED.avg_like_rate,
                        avg_comment_rate = EXCLUDED.avg_comment_rate,
                        content_type = EXCLUDED.content_type,
                        niche_tags = EXCLUDED.niche_tags,
                        insights = EXCLUDED.insights,
                        updated_at = NOW()
                """), {
                    "channel_id": metrics.channel_id,
                    "title": metrics.title,
                    "handle": metrics.handle,
                    "subscribers": metrics.subscribers,
                    "total_views": metrics.total_views,
                    "typical_views": metrics.typical_views,
                    "views_sub_multiplier": metrics.views_sub_multiplier,
                    "velocity_7d": metrics.velocity_7d,
                    "rpm_estimate": metrics.rpm_estimate,
                    "revenue_30d_estimate": metrics.revenue_30d_estimate,
                    "monetization_likelihood": metrics.monetization_likelihood,
                    "avg_like_rate": metrics.avg_like_rate,
                    "avg_comment_rate": metrics.avg_comment_rate,
                    "content_type": metrics.content_type,
                    "niche_tags": json.dumps(metrics.niche_tags),
                    "insights": json.dumps([asdict(i) for i in metrics.insights])
                })
                conn.commit()
            return True
        except Exception as e:
            logger.error(f"Error saving channel metrics: {e}")
            return False


# =========================================================================
# Test Function
# =========================================================================

async def test_channel_analyzer():
    """Test the channel analyzer with a sample channel"""
    analyzer = YouTubeChannelAnalyzer()
    
    try:
        # Test with Matt D'Avella
        channel_id = "UCJ24N4O0bP7LGLBDvye7oCA"
        
        logger.info(f"Analyzing channel: {channel_id}")
        metrics = await analyzer.analyze_channel(channel_id, videos_to_analyze=20)
        
        if metrics:
            print("\n" + "="*60)
            print(f"Channel: {metrics.title}")
            print(f"Handle: {metrics.handle}")
            print(f"Subscribers: {metrics.subscribers:,}")
            print(f"Total Views: {metrics.total_views:,}")
            print("="*60)
            print("\n📊 TubeLab-style Metrics:")
            print(f"  Typical Views: {metrics.typical_views:,}")
            print(f"  Views/Sub Multiplier: {metrics.views_sub_multiplier}×")
            print(f"  Velocity (7d): {metrics.velocity_7d:,.0f}")
            print(f"  Uploads (30d): {metrics.uploads_30d}")
            print(f"  Est. Views (30d): {metrics.views_30d:,}")
            print(f"\n💰 Revenue Estimates:")
            print(f"  RPM: ${metrics.rpm_estimate:.2f}")
            print(f"  Revenue (30d): ${metrics.revenue_30d_estimate:,.2f}")
            print(f"  Monetization: {metrics.monetization_likelihood*100:.0f}%")
            print(f"\n📈 Engagement:")
            print(f"  Like Rate: {metrics.avg_like_rate:.2f}%")
            print(f"  Comment Rate: {metrics.avg_comment_rate:.2f}/1k views")
            print(f"  Content Type: {metrics.content_type}")
            print(f"\n🏷️ Niches: {', '.join(metrics.niche_tags)}")
            print(f"\n💡 Insights:")
            for insight in metrics.insights:
                print(f"  • {insight.label}: {insight.tooltip}")
            print(f"\n🎬 Recent Videos:")
            for v in metrics.recent_videos[:3]:
                print(f"  • {v.title[:50]}... ({v.views:,} views)")
            
            return metrics
        else:
            print("Could not analyze channel")
            return None
    finally:
        await analyzer.close()


if __name__ == "__main__":
    asyncio.run(test_channel_analyzer())
