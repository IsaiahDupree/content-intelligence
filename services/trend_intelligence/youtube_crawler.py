"""
YouTube Trend Crawler
=====================
Multi-collector trend discovery for YouTube using:
1. YouTube Data API v3 (uploads playlist - most reliable)
2. RSS feeds (delta detection for new uploads)
3. Search API (discovery sampling - optional)

Trend Detection:
- Title templates ("I tried X", "X explained", "Do THIS not THAT")
- Topic clusters (embedding-based)
- Entity trends (products, tools, people mentioned)
- Format trends (duration, chapters, shorts vs longform)

Scoring:
- Channel-normalized uplift (video views / channel median)
- Breadth (unique channels using pattern)
- Velocity (mentions/day growth)
"""
import os
import re
import json
import asyncio
import feedparser
from datetime import datetime, timedelta, timezone
from typing import List, Dict, Any, Optional, Set, Tuple
from dataclasses import dataclass, field
from collections import Counter, defaultdict
from enum import Enum

import httpx
from loguru import logger
from sqlalchemy import create_engine, text
from openai import OpenAI

DATABASE_URL = os.getenv("DATABASE_URL", "postgresql://postgres:postgres@localhost:54322/postgres")
YOUTUBE_API_KEY = os.getenv("YOUTUBE_API_KEY", "")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "")


class YTTrendType(str, Enum):
    TITLE_TEMPLATE = "title_template"  # Repeating title structure
    TOPIC = "topic"                     # Subject cluster (semantic)
    ENTITY = "entity"                   # Product/tool/person
    FORMAT = "format"                   # Video structure/length
    DESCRIPTION_PATTERN = "description_pattern"  # Common description elements


class TrendStatus(str, Enum):
    EMERGING = "emerging"
    RISING = "rising"
    PEAK = "peak"
    STABLE = "stable"
    DECLINING = "declining"


@dataclass
class YTTrendCandidate:
    """A discovered YouTube trend"""
    trend_type: YTTrendType
    identifier: str
    display_title: str
    mentions: int = 0
    unique_channels: int = 0
    total_views: int = 0
    median_uplift: float = 0.0  # Median performance vs channel baseline
    velocity: float = 0.0
    score: float = 0.0
    status: TrendStatus = TrendStatus.EMERGING
    example_videos: List[Dict] = field(default_factory=list)
    channels: List[str] = field(default_factory=list)
    
    # Phase 1 enrichment
    avg_engagement_rate: float = 0.0
    common_products: List[str] = field(default_factory=list)
    common_ctas: List[str] = field(default_factory=list)
    thumbnail_urls: List[str] = field(default_factory=list)


@dataclass
class YouTubeChannel:
    """A channel to crawl for trends"""
    channel_id: str
    title: str = ""
    niche: str = ""
    uploads_playlist_id: str = ""  # UCxxx -> UUxxx
    median_views_7d: int = 0       # Baseline for uplift calculation


# Common title templates to detect
TITLE_TEMPLATES = [
    (r"^i tried (.+)", "I tried X"),
    (r"^(.+) explained$", "X explained"),
    (r"^how to (.+)", "How to X"),
    (r"^(.+) tutorial$", "X tutorial"),
    (r"^do this not that", "Do THIS not THAT"),
    (r"^the truth about (.+)", "The truth about X"),
    (r"^(.+) vs (.+)", "X vs Y"),
    (r"^why (.+) is (.+)", "Why X is Y"),
    (r"^(.+) in \d+ (minutes?|days?|hours?)", "X in N time"),
    (r"^stop (.+)", "Stop X"),
    (r"^never (.+)", "Never X"),
    (r"^(.+) you need to know", "X you need to know"),
]


class YouTubeCrawler:
    """
    Multi-collector YouTube trend discovery.
    
    Crawls channel panels using:
    1. Uploads playlist (reliable, complete)
    2. RSS feeds (fast delta detection)
    3. Search API (discovery sampling)
    """
    
    def __init__(self):
        self.engine = create_engine(DATABASE_URL)
        self.http_client = None
        self.channels: List[YouTubeChannel] = []
        self.api_key = YOUTUBE_API_KEY
        self.openai_client = OpenAI(api_key=OPENAI_API_KEY) if OPENAI_API_KEY else None
    
    async def _get_client(self) -> httpx.AsyncClient:
        if self.http_client is None:
            self.http_client = httpx.AsyncClient(timeout=60.0)
        return self.http_client
    
    async def close(self):
        if self.http_client:
            await self.http_client.aclose()
            self.http_client = None
    
    # =========================================
    # Channel Management
    # =========================================
    
    def add_channels(self, channels: List[Dict]):
        """Add channels to crawl"""
        for ch in channels:
            channel_id = ch.get("channel_id", "")
            # Convert UC to UU for uploads playlist
            uploads_id = "UU" + channel_id[2:] if channel_id.startswith("UC") else ""
            
            self.channels.append(YouTubeChannel(
                channel_id=channel_id,
                title=ch.get("title", ""),
                niche=ch.get("niche", ""),
                uploads_playlist_id=uploads_id,
            ))
        
        logger.info(f"📌 Added {len(channels)} YouTube channels")
    
    def set_channels(self, channels: List[Dict]):
        """Replace channel list"""
        self.channels = []
        self.add_channels(channels)
    
    async def load_channels_from_db(
        self,
        workspace_id: str = "00000000-0000-0000-0000-000000000001"
    ):
        """Load channels from workspace_sources"""
        with self.engine.connect() as conn:
            result = conn.execute(text("""
                SELECT identifier, niche
                FROM workspace_sources
                WHERE workspace_id = :workspace_id
                  AND platform = 'youtube'
                  AND is_active = true
                  AND source_type = 'channel'
            """), {"workspace_id": workspace_id})
            
            for row in result.fetchall():
                channel_id = row[0]
                uploads_id = "UU" + channel_id[2:] if channel_id.startswith("UC") else ""
                
                self.channels.append(YouTubeChannel(
                    channel_id=channel_id,
                    niche=row[1] or "",
                    uploads_playlist_id=uploads_id,
                ))
        
        logger.info(f"📂 Loaded {len(self.channels)} YouTube channels from DB")
    
    # =========================================
    # Collector 1: YouTube Data API v3
    # =========================================
    
    async def fetch_channel_uploads(
        self,
        channel: YouTubeChannel,
        max_results: int = 50
    ) -> List[Dict]:
        """
        Fetch recent uploads from a channel using uploads playlist.
        This is the most reliable method.
        """
        if not channel.uploads_playlist_id:
            logger.warning(f"No uploads playlist ID for {channel.channel_id}")
            return []
        
        client = await self._get_client()
        videos = []
        
        try:
            # Get video IDs from uploads playlist
            response = await client.get(
                "https://www.googleapis.com/youtube/v3/playlistItems",
                params={
                    "part": "snippet,contentDetails",
                    "playlistId": channel.uploads_playlist_id,
                    "maxResults": min(max_results, 50),
                    "key": self.api_key,
                }
            )
            
            if response.status_code != 200:
                logger.warning(f"Playlist fetch failed: {response.status_code}")
                return []
            
            data = response.json()
            items = data.get("items", [])
            
            # Extract video IDs
            video_ids = [
                item["contentDetails"]["videoId"]
                for item in items
                if "contentDetails" in item
            ]
            
            if not video_ids:
                return []
            
            # Fetch full video details in batch (with descriptions and topicDetails)
            videos = await self._fetch_video_details(video_ids)
            
            # Tag with channel info
            for video in videos:
                video["_channel_id"] = channel.channel_id
                video["_channel_title"] = channel.title
                video["_niche"] = channel.niche
            
            logger.info(f"  ✓ {channel.title or channel.channel_id[:12]}: {len(videos)} videos")
            
        except Exception as e:
            logger.warning(f"Channel fetch failed: {e}")
        
        return videos
    
    async def _fetch_video_details(self, video_ids: List[str]) -> List[Dict]:
        """Fetch full video details for a batch of video IDs"""
        client = await self._get_client()
        
        # API allows up to 50 IDs per request
        all_videos = []
        
        for i in range(0, len(video_ids), 50):
            batch = video_ids[i:i+50]
            
            try:
                response = await client.get(
                    "https://www.googleapis.com/youtube/v3/videos",
                    params={
                        "part": "snippet,statistics,contentDetails,topicDetails",
                        "id": ",".join(batch),
                        "key": self.api_key,
                    }
                )
                
                if response.status_code == 200:
                    data = response.json()
                    all_videos.extend(data.get("items", []))
            
            except Exception as e:
                logger.debug(f"Video details batch failed: {e}")
        
        return all_videos
    
    # =========================================
    # Collector 2: RSS Feed Delta
    # =========================================
    
    async def fetch_channel_rss(self, channel_id: str) -> List[Dict]:
        """
        Fetch recent uploads via RSS feed.
        Fast and cheap for delta detection.
        """
        url = f"https://www.youtube.com/feeds/videos.xml?channel_id={channel_id}"
        
        try:
            client = await self._get_client()
            response = await client.get(url)
            
            if response.status_code != 200:
                return []
            
            feed = feedparser.parse(response.text)
            
            # Extract video IDs from RSS
            video_ids = []
            for entry in feed.entries[:15]:  # RSS typically returns ~15 items
                video_id = entry.get("yt_videoid", "")
                if video_id:
                    video_ids.append(video_id)
            
            # Hydrate with full details
            if video_ids:
                return await self._fetch_video_details(video_ids)
            
        except Exception as e:
            logger.debug(f"RSS fetch failed for {channel_id}: {e}")
        
        return []
    
    # =========================================
    # Collector 3: Search API Discovery
    # =========================================
    
    async def discover_videos_by_search(
        self,
        category_id: str = None,
        region_code: str = "US",
        max_results: int = 50,
        published_after_days: int = 7
    ) -> List[Dict]:
        """
        Discover new videos via YouTube Search API.
        
        Use for:
        - Finding new channels to add to panels
        - Sampling trending content in a category
        - Discovery without knowing specific channels
        
        Note: Search API quota cost is higher (100 units per call).
        Use sparingly for discovery, not as primary crawl method.
        """
        client = await self._get_client()
        
        published_after = (datetime.now(timezone.utc) - timedelta(days=published_after_days)).isoformat()
        
        params = {
            "part": "snippet",
            "type": "video",
            "order": "date",  # Most recent
            "maxResults": min(max_results, 50),
            "regionCode": region_code,
            "publishedAfter": published_after,
            "key": self.api_key,
        }
        
        if category_id:
            params["videoCategoryId"] = category_id
        
        try:
            response = await client.get(
                "https://www.googleapis.com/youtube/v3/search",
                params=params
            )
            
            if response.status_code != 200:
                logger.warning(f"Search API failed: {response.status_code}")
                return []
            
            data = response.json()
            items = data.get("items", [])
            
            # Extract video IDs
            video_ids = [
                item["id"]["videoId"]
                for item in items
                if item.get("id", {}).get("videoId")
            ]
            
            if not video_ids:
                return []
            
            # Hydrate with full details
            videos = await self._fetch_video_details(video_ids)
            
            logger.info(f"🔍 Search discovery: {len(videos)} videos found")
            return videos
            
        except Exception as e:
            logger.warning(f"Search discovery failed: {e}")
            return []
    
    async def discover_channels_from_videos(self, videos: List[Dict]) -> List[str]:
        """
        Extract unique channel IDs from discovered videos.
        Use to expand niche panels.
        """
        channel_ids = set()
        
        for video in videos:
            snippet = video.get("snippet", {})
            channel_id = snippet.get("channelId", "")
            if channel_id:
                channel_ids.add(channel_id)
        
        return list(channel_ids)
    
    # =========================================
    # Collector 4: Comment Analysis (Phase 3)
    # =========================================
    
    async def fetch_video_comments(self, video_id: str, max_results: int = 50) -> List[Dict]:
        """
        Fetch top comments for a video.
        
        Returns list of comments with:
        - text: Comment text
        - likes: Like count
        - author: Author display name
        - published_at: Publish date
        """
        client = await self._get_client()
        
        try:
            response = await client.get(
                "https://www.googleapis.com/youtube/v3/commentThreads",
                params={
                    "part": "snippet",
                    "videoId": video_id,
                    "maxResults": min(max_results, 100),
                    "order": "relevance",
                    "key": self.api_key,
                }
            )
            
            if response.status_code != 200:
                return []
            
            data = response.json()
            comments = []
            
            for item in data.get("items", []):
                snippet = item.get("snippet", {}).get("topLevelComment", {}).get("snippet", {})
                comments.append({
                    "text": snippet.get("textDisplay", ""),
                    "text_original": snippet.get("textOriginal", ""),
                    "likes": snippet.get("likeCount", 0),
                    "author": snippet.get("authorDisplayName", ""),
                    "published_at": snippet.get("publishedAt", ""),
                })
            
            return comments
            
        except Exception as e:
            logger.debug(f"Comment fetch failed for {video_id}: {e}")
            return []
    
    async def fetch_comments_batch(self, video_ids: List[str], comments_per_video: int = 20) -> Dict[str, List[Dict]]:
        """
        Fetch comments for multiple videos.
        
        Returns dict mapping video_id -> comments
        """
        all_comments = {}
        
        # Limit to avoid quota exhaustion (each call = 1 unit)
        video_ids = video_ids[:30]  # Max 30 videos
        
        for video_id in video_ids:
            comments = await self.fetch_video_comments(video_id, comments_per_video)
            if comments:
                all_comments[video_id] = comments
            
            await asyncio.sleep(0.2)  # Rate limit
        
        logger.info(f"💬 Fetched comments for {len(all_comments)} videos")
        return all_comments
    
    # =========================================
    # Crawl All Channels
    # =========================================
    
    async def crawl_channels(self, videos_per_channel: int = 50) -> List[Dict]:
        """Crawl all channels and return video data"""
        logger.info(f"🔍 Crawling {len(self.channels)} YouTube channels...")
        
        all_videos = []
        
        for channel in self.channels:
            try:
                videos = await self.fetch_channel_uploads(channel, videos_per_channel)
                all_videos.extend(videos)
                
                await asyncio.sleep(0.3)  # Rate limit
                
            except Exception as e:
                logger.warning(f"Channel {channel.channel_id} failed: {e}")
        
        logger.success(f"✅ Crawled {len(all_videos)} total videos")
        return all_videos
    
    # =========================================
    # Calculate Channel Baselines
    # =========================================
    
    def calculate_channel_baselines(self, videos: List[Dict]):
        """Calculate median views per channel for uplift scoring"""
        channel_views = defaultdict(list)
        
        for video in videos:
            channel_id = video.get("_channel_id", "")
            stats = video.get("statistics", {})
            views = int(stats.get("viewCount", 0))
            
            if channel_id and views > 0:
                channel_views[channel_id].append(views)
        
        # Calculate medians
        for channel in self.channels:
            if channel.channel_id in channel_views:
                views_list = sorted(channel_views[channel.channel_id])
                median_idx = len(views_list) // 2
                channel.median_views_7d = views_list[median_idx] if views_list else 0
        
        logger.info(f"📊 Calculated baselines for {len(channel_views)} channels")
    
    # =========================================
    # Extract Trend Signals
    # =========================================
    
    def extract_trends(self, videos: List[Dict]) -> Dict[str, List[YTTrendCandidate]]:
        """Extract trend candidates from videos"""
        logger.info(f"🔍 Extracting YouTube trends from {len(videos)} videos...")
        
        # Calculate baselines first
        self.calculate_channel_baselines(videos)
        
        trends = {
            "title_templates": self._extract_title_templates(videos),
            "entities": self._extract_entities(videos),
            "formats": self._extract_format_trends(videos),
            "description_patterns": self._extract_description_patterns(videos),
        }
        
        # Phase 2: Topic clustering with OpenAI
        if self.openai_client:
            try:
                topic_clusters = self._extract_topic_clusters(videos)
                trends["topics"] = topic_clusters
            except Exception as e:
                logger.warning(f"Topic clustering failed: {e}")
                trends["topics"] = []
        else:
            trends["topics"] = []
        
        total = sum(len(v) for v in trends.values())
        logger.success(f"✅ Found {total} YouTube trend candidates")
        
        return trends
    
    def _extract_title_templates(self, videos: List[Dict]) -> List[YTTrendCandidate]:
        """Extract repeating title templates"""
        template_data = defaultdict(lambda: {
            "count": 0,
            "channels": set(),
            "total_views": 0,
            "uplifts": [],
            "examples": [],
        })
        
        for video in videos:
            snippet = video.get("snippet", {})
            title = snippet.get("title", "").lower()
            
            if not title:
                continue
            
            # Match against known templates
            matched_template = None
            for pattern, template_name in TITLE_TEMPLATES:
                if re.search(pattern, title, re.IGNORECASE):
                    matched_template = template_name
                    break
            
            if not matched_template:
                continue
            
            # Get video stats
            channel_id = video.get("_channel_id", "")
            stats = video.get("statistics", {})
            views = int(stats.get("viewCount", 0))
            
            # Calculate uplift
            channel = next((c for c in self.channels if c.channel_id == channel_id), None)
            uplift = 0.0
            if channel and channel.median_views_7d > 0:
                uplift = views / channel.median_views_7d
            
            template_data[matched_template]["count"] += 1
            template_data[matched_template]["channels"].add(channel_id)
            template_data[matched_template]["total_views"] += views
            if uplift > 0:
                template_data[matched_template]["uplifts"].append(uplift)
            
            if len(template_data[matched_template]["examples"]) < 5:
                template_data[matched_template]["examples"].append({
                    "title": snippet.get("title", "")[:80],
                    "video_id": video.get("id", ""),
                    "views": views,
                    "channel": video.get("_channel_title", ""),
                })
        
        # Convert to candidates
        candidates = []
        for template, data in template_data.items():
            if data["count"] >= 2:  # At least 2 uses
                median_uplift = 0.0
                if data["uplifts"]:
                    sorted_uplifts = sorted(data["uplifts"])
                    median_uplift = sorted_uplifts[len(sorted_uplifts) // 2]
                
                candidate = YTTrendCandidate(
                    trend_type=YTTrendType.TITLE_TEMPLATE,
                    identifier=template,
                    display_title=f'📝 "{template}"',
                    mentions=data["count"],
                    unique_channels=len(data["channels"]),
                    total_views=data["total_views"],
                    median_uplift=median_uplift,
                    channels=list(data["channels"])[:5],
                    example_videos=data["examples"],
                )
                candidate.score = self._calculate_score(candidate)
                candidate.status = self._determine_status(candidate)
                candidates.append(candidate)
        
        candidates.sort(key=lambda x: x.score, reverse=True)
        return candidates[:20]
    
    def _extract_entities(self, videos: List[Dict]) -> List[YTTrendCandidate]:
        """Extract trending entities (products, tools, people) from titles"""
        # Simple entity extraction - look for capitalized words/phrases
        entity_data = defaultdict(lambda: {
            "count": 0,
            "channels": set(),
            "total_views": 0,
            "uplifts": [],
        })
        
        for video in videos:
            snippet = video.get("snippet", {})
            title = snippet.get("title", "")
            
            # Extract capitalized words (potential entities)
            entities = re.findall(r'\b[A-Z][a-z]+(?:\s+[A-Z][a-z]+)*\b', title)
            
            channel_id = video.get("_channel_id", "")
            stats = video.get("statistics", {})
            views = int(stats.get("viewCount", 0))
            
            channel = next((c for c in self.channels if c.channel_id == channel_id), None)
            uplift = 0.0
            if channel and channel.median_views_7d > 0:
                uplift = views / channel.median_views_7d
            
            for entity in entities:
                if len(entity) < 3 or entity.lower() in ["the", "this", "that"]:
                    continue
                
                entity_data[entity]["count"] += 1
                entity_data[entity]["channels"].add(channel_id)
                entity_data[entity]["total_views"] += views
                if uplift > 0:
                    entity_data[entity]["uplifts"].append(uplift)
        
        candidates = []
        for entity, data in entity_data.items():
            if len(data["channels"]) >= 2 and data["count"] >= 3:
                median_uplift = 0.0
                if data["uplifts"]:
                    sorted_uplifts = sorted(data["uplifts"])
                    median_uplift = sorted_uplifts[len(sorted_uplifts) // 2]
                
                candidate = YTTrendCandidate(
                    trend_type=YTTrendType.ENTITY,
                    identifier=entity,
                    display_title=f"🏷️ {entity}",
                    mentions=data["count"],
                    unique_channels=len(data["channels"]),
                    total_views=data["total_views"],
                    median_uplift=median_uplift,
                    channels=list(data["channels"])[:5],
                )
                candidate.score = self._calculate_score(candidate)
                candidate.status = self._determine_status(candidate)
                candidates.append(candidate)
        
        candidates.sort(key=lambda x: x.score, reverse=True)
        return candidates[:20]
    
    def _extract_format_trends(self, videos: List[Dict]) -> List[YTTrendCandidate]:
        """Extract format trends (Shorts, duration buckets, etc.)"""
        format_data = defaultdict(lambda: {
            "count": 0,
            "channels": set(),
            "total_views": 0,
            "uplifts": [],
        })
        
        for video in videos:
            content_details = video.get("contentDetails", {})
            duration_str = content_details.get("duration", "")
            
            # Parse ISO 8601 duration (PT1M30S)
            duration_seconds = self._parse_duration(duration_str)
            
            # Categorize
            if duration_seconds < 60:
                format_type = "Shorts (<60s)"
            elif duration_seconds < 300:
                format_type = "Short-form (1-5min)"
            elif duration_seconds < 900:
                format_type = "Mid-form (5-15min)"
            else:
                format_type = "Long-form (15min+)"
            
            channel_id = video.get("_channel_id", "")
            stats = video.get("statistics", {})
            views = int(stats.get("viewCount", 0))
            
            channel = next((c for c in self.channels if c.channel_id == channel_id), None)
            uplift = 0.0
            if channel and channel.median_views_7d > 0:
                uplift = views / channel.median_views_7d
            
            format_data[format_type]["count"] += 1
            format_data[format_type]["channels"].add(channel_id)
            format_data[format_type]["total_views"] += views
            if uplift > 0:
                format_data[format_type]["uplifts"].append(uplift)
        
        candidates = []
        for fmt, data in format_data.items():
            if data["count"] >= 5:
                median_uplift = 0.0
                if data["uplifts"]:
                    sorted_uplifts = sorted(data["uplifts"])
                    median_uplift = sorted_uplifts[len(sorted_uplifts) // 2]
                
                candidate = YTTrendCandidate(
                    trend_type=YTTrendType.FORMAT,
                    identifier=fmt,
                    display_title=f"📹 {fmt}",
                    mentions=data["count"],
                    unique_channels=len(data["channels"]),
                    total_views=data["total_views"],
                    median_uplift=median_uplift,
                    channels=list(data["channels"])[:5],
                )
                candidate.score = self._calculate_score(candidate)
                candidate.status = self._determine_status(candidate)
                candidates.append(candidate)
        
        candidates.sort(key=lambda x: x.score, reverse=True)
        return candidates[:10]
    
    def _extract_description_patterns(self, videos: List[Dict]) -> List[YTTrendCandidate]:
        """Extract common description patterns (products, CTAs, affiliate links)"""
        from .youtube_enrichment_helpers import extract_description_metadata, extract_common_elements
        
        all_products = []
        all_ctas = []
        all_affiliate_counts = []
        channel_products = defaultdict(set)
        
        for video in videos:
            snippet = video.get("snippet", {})
            description = snippet.get("description", "")
            channel_id = video.get("_channel_id", "")
            
            metadata = extract_description_metadata(description)
            
            all_products.extend(metadata["products"])
            all_ctas.extend(metadata["ctas"])
            
            if metadata["affiliate_links"]:
                all_affiliate_counts.append(len(metadata["affiliate_links"]))
            
            for product in metadata["products"]:
                channel_products[product].add(channel_id)
        
        # Find common products across channels
        candidates = []
        common_products = extract_common_elements(all_products, min_frequency=3)
        
        for product in common_products[:5]:
            channels_using = len(channel_products[product])
            if channels_using >= 2:
                candidate = YTTrendCandidate(
                    trend_type=YTTrendType.DESCRIPTION_PATTERN,
                    identifier=f"product_{product}",
                    display_title=f"🛠️ {product.title()}",
                    mentions=all_products.count(product),
                    unique_channels=channels_using,
                    common_products=[product],
                )
                candidate.score = self._calculate_score(candidate)
                candidate.status = self._determine_status(candidate)
                candidates.append(candidate)
        
        candidates.sort(key=lambda x: x.score, reverse=True)
        return candidates[:10]
    
    def _extract_topic_clusters(self, videos: List[Dict]) -> List[YTTrendCandidate]:
        """
        Phase 2: Semantic topic clustering using OpenAI embeddings.
        Groups videos by meaning rather than exact title matches.
        """
        if not self.openai_client:
            return []
        
        from .youtube_enrichment_helpers import cluster_by_embeddings, calculate_engagement_metrics, calculate_channel_engagement_baseline
        
        logger.info("🤖 Clustering topics with OpenAI embeddings...")
        
        # Prepare texts for embedding (title + description snippet)
        texts = []
        video_map = []
        
        for video in videos:
            snippet = video.get("snippet", {})
            title = snippet.get("title", "")
            description = snippet.get("description", "")[:200]  # First 200 chars
            
            if title:
                text = f"{title}\n{description}"
                texts.append(text)
                video_map.append(video)
        
        if len(texts) < 5:
            logger.warning("Not enough videos for topic clustering")
            return []
        
        # Get embeddings from OpenAI
        try:
            response = self.openai_client.embeddings.create(
                model="text-embedding-3-small",
                input=texts[:100]  # Limit to 100 for cost control
            )
            
            embeddings = [item.embedding for item in response.data]
            
            # Cluster embeddings
            cluster_ids = cluster_by_embeddings(embeddings, min_cluster_size=3)
            
            # Group videos by cluster
            clusters = defaultdict(list)
            for i, cluster_id in enumerate(cluster_ids):
                if cluster_id >= 0:  # -1 means noise
                    clusters[cluster_id].append(video_map[i])
            
            # Calculate engagement baselines
            engagement_baselines = calculate_channel_engagement_baseline(videos)
            
            # Create trend candidates for each cluster
            candidates = []
            for cluster_id, cluster_videos in clusters.items():
                if len(cluster_videos) < 3:
                    continue
                
                # Get unique channels
                channels = set(v.get("_channel_id", "") for v in cluster_videos)
                channels = [c for c in channels if c]
                
                if len(channels) < 2:
                    continue
                
                # Calculate metrics
                total_views = sum(int(v.get("statistics", {}).get("viewCount", 0)) for v in cluster_videos)
                
                # Calculate average engagement
                engagement_scores = []
                for video in cluster_videos:
                    channel_id = video.get("_channel_id", "")
                    baseline = engagement_baselines.get(channel_id, {})
                    metrics = calculate_engagement_metrics(video, baseline)
                    engagement_scores.append(metrics["engagement_score"])
                
                avg_engagement = sum(engagement_scores) / len(engagement_scores) if engagement_scores else 0
                
                # Get example titles
                example_titles = [v.get("snippet", {}).get("title", "") for v in cluster_videos[:5]]
                
                # Use GPT to generate cluster label
                try:
                    label_response = self.openai_client.chat.completions.create(
                        model="gpt-4o-mini",
                        messages=[{
                            "role": "user",
                            "content": f"What single topic do these YouTube video titles share? Give a 2-4 word label.\n\nTitles:\n" + "\n".join(f"- {t}" for t in example_titles[:5])
                        }],
                        max_tokens=20,
                        temperature=0.3,
                    )
                    
                    topic_label = label_response.choices[0].message.content.strip()
                
                except Exception as e:
                    logger.debug(f"Topic labeling failed: {e}")
                    topic_label = f"Topic Cluster {cluster_id}"
                
                # Create candidate
                candidate = YTTrendCandidate(
                    trend_type=YTTrendType.TOPIC,
                    identifier=f"topic_{cluster_id}",
                    display_title=f"💡 {topic_label}",
                    mentions=len(cluster_videos),
                    unique_channels=len(channels),
                    total_views=total_views,
                    avg_engagement_rate=avg_engagement,
                    example_videos=[{
                        "title": v.get("snippet", {}).get("title", "")[:80],
                        "video_id": v.get("id", ""),
                        "views": int(v.get("statistics", {}).get("viewCount", 0)),
                    } for v in cluster_videos[:5]],
                    channels=list(channels)[:5],
                )
                
                candidate.score = self._calculate_score(candidate)
                candidate.status = self._determine_status(candidate)
                candidates.append(candidate)
            
            candidates.sort(key=lambda x: x.score, reverse=True)
            logger.success(f"✅ Found {len(candidates)} topic clusters")
            
            return candidates[:10]
        
        except Exception as e:
            logger.error(f"Topic clustering failed: {e}")
            return []
    
    def _parse_duration(self, duration_str: str) -> int:
        """Parse ISO 8601 duration to seconds"""
        if not duration_str:
            return 0
        
        # PT1H2M30S -> 3750 seconds
        match = re.match(r'PT(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?', duration_str)
        if not match:
            return 0
        
        hours = int(match.group(1) or 0)
        minutes = int(match.group(2) or 0)
        seconds = int(match.group(3) or 0)
        
        return hours * 3600 + minutes * 60 + seconds
    
    # =========================================
    # Scoring
    # =========================================
    
    def _calculate_score(self, candidate: YTTrendCandidate) -> float:
        """
        Score with channel-normalized uplift:
        - Breadth (45%) - unique channels
        - Velocity (30%) - frequency
        - Uplift (25%) - performance vs baseline
        """
        # Breadth
        breadth = min(candidate.unique_channels / 10, 1.0) * 0.45
        
        # Frequency
        frequency = min(candidate.mentions / 20, 1.0) * 0.30
        
        # Uplift (median performance vs channel baseline)
        uplift_score = min(candidate.median_uplift / 2.0, 1.0) * 0.25
        
        return breadth + frequency + uplift_score
    
    def _determine_status(self, candidate: YTTrendCandidate) -> TrendStatus:
        """Determine trend status"""
        if candidate.score > 0.6:
            return TrendStatus.RISING
        elif candidate.score > 0.4:
            return TrendStatus.EMERGING
        elif candidate.unique_channels >= 5:
            return TrendStatus.STABLE
        else:
            return TrendStatus.EMERGING
    
    # =========================================
    # Save to Database
    # =========================================
    
    async def save_trends(
        self,
        trends: Dict[str, List[YTTrendCandidate]],
        workspace_id: str = "00000000-0000-0000-0000-000000000001"
    ) -> int:
        """Save discovered trends to database"""
        saved = 0
        
        with self.engine.connect() as conn:
            # Clear old YouTube trends
            conn.execute(text("""
                DELETE FROM trend_clusters 
                WHERE workspace_id = :workspace_id 
                  AND cluster_type IN ('title_template', 'entity', 'format', 'topic')
            """), {"workspace_id": workspace_id})
            
            for trend_type, candidates in trends.items():
                for candidate in candidates:
                    try:
                        conn.execute(text("""
                            INSERT INTO trend_clusters (
                                workspace_id, cluster_type, title, status, confidence
                            ) VALUES (
                                :workspace_id, :cluster_type, :title, :status, :confidence
                            )
                        """), {
                            "workspace_id": workspace_id,
                            "cluster_type": candidate.trend_type.value,
                            "title": candidate.display_title,
                            "status": candidate.status.value,
                            "confidence": candidate.score,
                        })
                        saved += 1
                    except Exception as e:
                        logger.debug(f"Failed to save trend: {e}")
            
            conn.commit()
        
        logger.info(f"💾 Saved {saved} YouTube trends to database")
        return saved
    
    # =========================================
    # Full Pipeline
    # =========================================
    
    async def discover(
        self,
        workspace_id: str = "00000000-0000-0000-0000-000000000001",
        videos_per_channel: int = 50,
        analyze_comments: bool = False,
        analyze_thumbnails: bool = False,
        analyze_keywords: bool = False
    ) -> Dict[str, Any]:
        """
        Run full YouTube trend discovery pipeline.
        
        1. Load channels from DB
        2. Crawl uploads via API
        3. Extract trend signals
        4. Score with channel-normalized uplift
        5. Optionally analyze comments (Phase 3)
        6. Optionally analyze thumbnails (Phase 4)
        7. Optionally analyze keywords (Phase 5)
        8. Save to database
        """
        logger.info("🚀 Starting YouTube trend discovery...")
        
        results = {
            "channels": 0,
            "videos_crawled": 0,
            "trends_found": {},
            "saved": 0,
            "comment_insights": None,
            "thumbnail_insights": None,
            "keyword_insights": None,
        }
        
        # Load channels if not set
        if not self.channels:
            await self.load_channels_from_db(workspace_id)
        
        results["channels"] = len(self.channels)
        
        if not self.channels:
            logger.warning("No YouTube channels configured")
            return results
        
        # Crawl videos
        videos = await self.crawl_channels(videos_per_channel)
        results["videos_crawled"] = len(videos)
        
        if not videos:
            logger.warning("No videos crawled")
            return results
        
        # Extract trends
        trends = self.extract_trends(videos)
        
        # Phase 3: Comment analysis
        if analyze_comments:
            comment_insights = await self._analyze_comments(videos)
            results["comment_insights"] = comment_insights
            
            # Add audience interest trends from comments
            if comment_insights:
                audience_trends = self._extract_audience_trends(comment_insights)
                if audience_trends:
                    trends["audience_interests"] = audience_trends
        
        # Phase 4: Thumbnail analysis
        if analyze_thumbnails:
            thumbnail_insights = await self._analyze_thumbnails(videos)
            results["thumbnail_insights"] = thumbnail_insights
            
            # Add thumbnail style trends
            if thumbnail_insights:
                thumbnail_trends = self._extract_thumbnail_trends(thumbnail_insights)
                if thumbnail_trends:
                    trends["thumbnail_styles"] = thumbnail_trends
        
        # Phase 5: Keyword analysis
        if analyze_keywords:
            keyword_insights = await self._analyze_keywords(videos)
            results["keyword_insights"] = keyword_insights
            
            # Add keyword opportunity trends
            if keyword_insights:
                keyword_trends = self._extract_keyword_trends(keyword_insights)
                if keyword_trends:
                    trends["keyword_opportunities"] = keyword_trends
        
        results["trends_found"] = {k: len(v) for k, v in trends.items()}
        
        # Save to DB
        results["saved"] = await self.save_trends(trends, workspace_id)
        
        # Log summary
        logger.info("\n📊 Discovered YouTube Trends:")
        for trend_type, candidates in trends.items():
            if candidates:
                logger.info(f"  {trend_type.upper()}:")
                for c in candidates[:3]:
                    logger.info(f"    • {c.display_title} (score={c.score:.2f}, channels={c.unique_channels}, uplift={c.median_uplift:.1f}x)")
        
        await self.close()
        
        logger.success("✅ YouTube trend discovery complete")
        return results
    
    async def _analyze_comments(self, videos: List[Dict]) -> Dict[str, Any]:
        """
        Phase 3: Analyze comments to extract audience insights.
        
        Fetches top comments from high-performing videos and extracts:
        - Questions viewers are asking
        - Content requests
        - Trending topics in discussions
        - Sentiment signals
        """
        from .youtube_enrichment_helpers import aggregate_comment_themes, extract_audience_interests
        
        logger.info("💬 Phase 3: Analyzing comments for audience insights...")
        
        # Select top videos by engagement for comment analysis
        videos_with_comments = []
        for v in videos:
            stats = v.get("statistics", {})
            comment_count = int(stats.get("commentCount", 0))
            if comment_count > 10:  # Only analyze videos with meaningful comment activity
                videos_with_comments.append({
                    "video": v,
                    "comment_count": comment_count,
                })
        
        # Sort by comment count and take top 20
        videos_with_comments.sort(key=lambda x: x["comment_count"], reverse=True)
        top_videos = [v["video"] for v in videos_with_comments[:20]]
        
        if not top_videos:
            logger.warning("No videos with sufficient comments found")
            return {}
        
        # Fetch comments
        video_ids = [v.get("id", "") for v in top_videos if v.get("id")]
        all_comments = await self.fetch_comments_batch(video_ids, comments_per_video=30)
        
        if not all_comments:
            logger.warning("No comments fetched")
            return {}
        
        # Aggregate themes
        aggregated = aggregate_comment_themes(all_comments)
        
        # Extract audience interests
        interests = extract_audience_interests(aggregated)
        aggregated["audience_interests"] = interests
        
        logger.success(f"✅ Comment analysis complete: {aggregated['videos_analyzed']} videos, {len(aggregated['top_questions'])} questions, {len(interests)} interests")
        
        return aggregated
    
    def _extract_audience_trends(self, comment_insights: Dict[str, Any]) -> List[YTTrendCandidate]:
        """
        Convert comment insights into trend candidates.
        
        Creates trends from:
        - Frequently asked questions (content gaps)
        - Content requests (demand signals)
        - Trending discussion topics
        """
        candidates = []
        
        # Create trends from audience interests
        interests = comment_insights.get("audience_interests", [])
        for i, interest in enumerate(interests[:10]):
            candidate = YTTrendCandidate(
                trend_type=YTTrendType.TOPIC,  # Audience interest = topic opportunity
                identifier=f"audience_interest_{i}",
                display_title=f"❓ {interest.title()}",
                mentions=1,
                unique_channels=comment_insights.get("videos_analyzed", 1),
            )
            candidate.score = 0.3 + (0.05 * (10 - i))  # Higher score for top interests
            candidate.status = TrendStatus.EMERGING
            candidates.append(candidate)
        
        # Create trends from top questions
        questions = comment_insights.get("top_questions", [])
        question_themes = {}
        
        for q in questions[:20]:
            text = q["text"].lower()
            
            # Group similar questions
            for keyword in ["how", "what", "why", "best", "recommend", "tutorial"]:
                if keyword in text:
                    if keyword not in question_themes:
                        question_themes[keyword] = {
                            "count": 0,
                            "total_likes": 0,
                            "examples": [],
                        }
                    question_themes[keyword]["count"] += 1
                    question_themes[keyword]["total_likes"] += q["likes"]
                    if len(question_themes[keyword]["examples"]) < 3:
                        question_themes[keyword]["examples"].append(q["text"][:100])
                    break
        
        # Create trend for "How-to" questions if significant
        if question_themes.get("how", {}).get("count", 0) >= 3:
            how_data = question_themes["how"]
            candidate = YTTrendCandidate(
                trend_type=YTTrendType.TOPIC,
                identifier="audience_howto",
                display_title="❓ How-to Questions",
                mentions=how_data["count"],
                unique_channels=comment_insights.get("videos_analyzed", 1),
                example_videos=[{"title": ex} for ex in how_data["examples"]],
            )
            candidate.score = min(0.2 + (how_data["count"] * 0.05), 0.6)
            candidate.status = TrendStatus.EMERGING
            candidates.append(candidate)
        
        # Create trend for "What/Best" recommendations if significant
        if question_themes.get("best", {}).get("count", 0) >= 2 or question_themes.get("recommend", {}).get("count", 0) >= 2:
            best_data = question_themes.get("best", {"count": 0, "examples": []})
            rec_data = question_themes.get("recommend", {"count": 0, "examples": []})
            
            candidate = YTTrendCandidate(
                trend_type=YTTrendType.TOPIC,
                identifier="audience_recommendations",
                display_title="❓ Recommendation Requests",
                mentions=best_data["count"] + rec_data["count"],
                unique_channels=comment_insights.get("videos_analyzed", 1),
                example_videos=[{"title": ex} for ex in (best_data["examples"] + rec_data["examples"])[:3]],
            )
            candidate.score = 0.35
            candidate.status = TrendStatus.EMERGING
            candidates.append(candidate)
        
        candidates.sort(key=lambda x: x.score, reverse=True)
        return candidates[:10]
    
    async def _analyze_thumbnails(self, videos: List[Dict]) -> Dict[str, Any]:
        """
        Phase 4: Analyze thumbnails to extract visual patterns.
        
        Downloads and analyzes thumbnails from top videos to identify:
        - Color schemes that perform well
        - Face vs no-face patterns
        - Text overlay styles
        - Visual composition trends
        """
        from .youtube_enrichment_helpers import analyze_thumbnail_full
        
        logger.info("🖼️ Phase 4: Analyzing thumbnails for visual patterns...")
        
        # Select top videos by views for thumbnail analysis
        sorted_videos = sorted(
            videos,
            key=lambda v: int(v.get("statistics", {}).get("viewCount", 0)),
            reverse=True
        )[:20]  # Top 20 videos
        
        analyses = []
        client = await self._get_client()
        
        for video in sorted_videos:
            snippet = video.get("snippet", {})
            thumbnails = snippet.get("thumbnails", {})
            
            # Get highest quality thumbnail
            thumbnail_url = None
            for quality in ["maxres", "high", "medium", "default"]:
                if quality in thumbnails:
                    thumbnail_url = thumbnails[quality].get("url")
                    break
            
            if not thumbnail_url:
                continue
            
            try:
                analysis = await analyze_thumbnail_full(thumbnail_url, client)
                analysis["video_id"] = video.get("id", "")
                analysis["video_title"] = snippet.get("title", "")[:60]
                analysis["views"] = int(video.get("statistics", {}).get("viewCount", 0))
                analyses.append(analysis)
                
                await asyncio.sleep(0.1)  # Rate limit
                
            except Exception as e:
                logger.debug(f"Thumbnail analysis failed: {e}")
        
        if not analyses:
            logger.warning("No thumbnails analyzed")
            return {}
        
        # Aggregate results
        style_counts = Counter(a.get("summary", {}).get("style", "unknown") for a in analyses)
        color_schemes = Counter(a.get("colors", {}).get("color_scheme", "unknown") for a in analyses)
        face_count = sum(1 for a in analyses if a.get("faces", {}).get("has_face"))
        text_count = sum(1 for a in analyses if a.get("text", {}).get("has_text"))
        
        # Calculate performance by style
        style_performance = defaultdict(lambda: {"views": 0, "count": 0})
        for a in analyses:
            style = a.get("summary", {}).get("style", "unknown")
            style_performance[style]["views"] += a.get("views", 0)
            style_performance[style]["count"] += 1
        
        for style in style_performance:
            if style_performance[style]["count"] > 0:
                style_performance[style]["avg_views"] = (
                    style_performance[style]["views"] / style_performance[style]["count"]
                )
        
        logger.success(f"✅ Thumbnail analysis complete: {len(analyses)} thumbnails analyzed")
        
        return {
            "thumbnails_analyzed": len(analyses),
            "style_distribution": dict(style_counts),
            "color_schemes": dict(color_schemes),
            "face_percentage": round(face_count / len(analyses) * 100, 1) if analyses else 0,
            "text_percentage": round(text_count / len(analyses) * 100, 1) if analyses else 0,
            "style_performance": dict(style_performance),
            "sample_analyses": analyses[:5],  # Include 5 samples
        }
    
    def _extract_thumbnail_trends(self, thumbnail_insights: Dict[str, Any]) -> List[YTTrendCandidate]:
        """
        Convert thumbnail insights into trend candidates.
        
        Creates trends for:
        - Best performing thumbnail styles
        - Color scheme trends
        - Face/text usage patterns
        """
        candidates = []
        
        style_performance = thumbnail_insights.get("style_performance", {})
        
        # Find best performing styles
        styles_sorted = sorted(
            [(style, data) for style, data in style_performance.items()],
            key=lambda x: x[1].get("avg_views", 0),
            reverse=True
        )
        
        for i, (style, data) in enumerate(styles_sorted[:3]):
            if data.get("count", 0) >= 2:  # At least 2 examples
                candidate = YTTrendCandidate(
                    trend_type=YTTrendType.FORMAT,
                    identifier=f"thumbnail_{style}",
                    display_title=f"🖼️ {style.replace('_', ' ').title()} Thumbnails",
                    mentions=data.get("count", 0),
                    unique_channels=data.get("count", 0),
                    total_views=data.get("views", 0),
                )
                candidate.score = 0.4 + (0.1 * (3 - i))
                candidate.status = TrendStatus.EMERGING if i > 0 else TrendStatus.RISING
                candidates.append(candidate)
        
        # Color scheme trend
        color_schemes = thumbnail_insights.get("color_schemes", {})
        dominant_scheme = max(color_schemes.items(), key=lambda x: x[1])[0] if color_schemes else None
        
        if dominant_scheme and color_schemes.get(dominant_scheme, 0) >= 3:
            candidate = YTTrendCandidate(
                trend_type=YTTrendType.FORMAT,
                identifier=f"color_{dominant_scheme}",
                display_title=f"🎨 {dominant_scheme.title()} Color Palette",
                mentions=color_schemes[dominant_scheme],
                unique_channels=thumbnail_insights.get("thumbnails_analyzed", 1),
            )
            candidate.score = 0.35
            candidate.status = TrendStatus.EMERGING
            candidates.append(candidate)
        
        # Face usage trend
        face_pct = thumbnail_insights.get("face_percentage", 0)
        if face_pct >= 60:
            candidate = YTTrendCandidate(
                trend_type=YTTrendType.FORMAT,
                identifier="thumbnail_faces",
                display_title=f"😀 Face-Forward Thumbnails ({face_pct:.0f}%)",
                mentions=int(face_pct / 10),
                unique_channels=thumbnail_insights.get("thumbnails_analyzed", 1),
            )
            candidate.score = 0.40
            candidate.status = TrendStatus.RISING
            candidates.append(candidate)
        elif face_pct <= 30:
            candidate = YTTrendCandidate(
                trend_type=YTTrendType.FORMAT,
                identifier="thumbnail_noface",
                display_title=f"🎬 Cinematic/No-Face Thumbnails ({100-face_pct:.0f}%)",
                mentions=int((100 - face_pct) / 10),
                unique_channels=thumbnail_insights.get("thumbnails_analyzed", 1),
            )
            candidate.score = 0.35
            candidate.status = TrendStatus.EMERGING
            candidates.append(candidate)
        
        candidates.sort(key=lambda x: x.score, reverse=True)
        return candidates[:5]
    
    async def _analyze_keywords(self, videos: List[Dict]) -> Dict[str, Any]:
        """
        Phase 5: Analyze keywords to find content opportunities.
        
        Extracts keywords from titles/descriptions and calculates:
        - Performance metrics (views per keyword)
        - Cross-channel usage (breadth)
        - Opportunity scores
        """
        from .youtube_enrichment_helpers import extract_keywords_from_videos, rank_keywords_by_opportunity
        
        logger.info("🔑 Phase 5: Analyzing keywords for content opportunities...")
        
        # Extract keywords from all videos
        keyword_data = extract_keywords_from_videos(videos)
        
        if not keyword_data:
            logger.warning("No keywords extracted")
            return {}
        
        # Rank by opportunity
        ranked_keywords = rank_keywords_by_opportunity(keyword_data, min_count=2)
        
        # Calculate summary stats
        total_keywords = len(keyword_data)
        keywords_with_breadth = sum(1 for kw, data in keyword_data.items() if data.get("unique_channels", 0) >= 2)
        
        logger.success(f"✅ Keyword analysis complete: {total_keywords} keywords, {len(ranked_keywords)} opportunities")
        
        return {
            "total_keywords": total_keywords,
            "keywords_with_cross_channel_usage": keywords_with_breadth,
            "top_opportunities": ranked_keywords[:20],
            "all_keyword_data": {k: v for k, v in list(keyword_data.items())[:50]},  # Limit for response size
        }
    
    def _extract_keyword_trends(self, keyword_insights: Dict[str, Any]) -> List[YTTrendCandidate]:
        """
        Convert keyword insights into trend candidates.
        
        Creates trends for top keyword opportunities.
        """
        candidates = []
        
        top_opportunities = keyword_insights.get("top_opportunities", [])
        
        for i, kw_data in enumerate(top_opportunities[:10]):
            keyword = kw_data.get("keyword", "")
            if not keyword:
                continue
            
            candidate = YTTrendCandidate(
                trend_type=YTTrendType.TOPIC,
                identifier=f"keyword_{keyword.replace(' ', '_')}",
                display_title=f"🔑 \"{keyword}\"",
                mentions=kw_data.get("video_count", 0),
                unique_channels=kw_data.get("unique_channels", 0),
                total_views=kw_data.get("total_views", 0),
            )
            
            # Use opportunity score
            candidate.score = kw_data.get("opportunity_score", 0.3)
            candidate.status = TrendStatus.RISING if candidate.score > 0.5 else TrendStatus.EMERGING
            candidates.append(candidate)
        
        candidates.sort(key=lambda x: x.score, reverse=True)
        return candidates[:10]


# Singleton
_yt_crawler = None

def get_youtube_crawler() -> YouTubeCrawler:
    global _yt_crawler
    if _yt_crawler is None:
        _yt_crawler = YouTubeCrawler()
    return _yt_crawler
