"""
Seedless Trend Crawler
======================
Discovers trends WITHOUT keywords by:
1. Random sampling from platform discovery surfaces
2. Automatic feature extraction (n-grams, hashtags, sounds)
3. Clustering and velocity scoring
4. Trend candidate ranking

The key idea: Sample reality, let math + clustering tell you what's repeating.

Pipeline:
1. Ingest (Seedless) - Random sample by date/region
2. Normalize - Store in unified schema
3. Feature Extraction - Hooks, topics, co-occurrence graphs
4. Trend Detection - Rank by velocity, breadth, freshness
5. Expand - Targeted pulls once trends discovered
"""
import os
import re
import json
import asyncio
from datetime import datetime, timedelta, timezone
from typing import List, Dict, Any, Optional, Tuple
from dataclasses import dataclass, field
from collections import Counter
from enum import Enum

import httpx
from loguru import logger
from sqlalchemy import create_engine, text

from .models import PostRaw, PostMetrics, Platform


DATABASE_URL = os.getenv("DATABASE_URL", "postgresql://postgres:postgres@localhost:54322/postgres")
RAPIDAPI_KEY = os.getenv("RAPIDAPI_KEY", "")


class TrendType(str, Enum):
    HASHTAG = "hashtag"
    SOUND = "sound"
    HOOK = "hook"
    TOPIC = "topic"


class TrendStatus(str, Enum):
    EMERGING = "emerging"      # Velocity high, mentions low
    RISING = "rising"          # Velocity high, mentions medium
    PEAK = "peak"              # Velocity slowing, mentions high
    STABLE = "stable"          # Velocity low, mentions medium
    DECLINING = "declining"    # Velocity negative


@dataclass
class TrendCandidate:
    """A potential trend discovered from sampling"""
    trend_type: TrendType
    identifier: str           # hashtag name, sound_id, or hook phrase
    title: str                # Display title
    mentions_24h: int = 0     # Count in last 24h
    mentions_prev_24h: int = 0  # Count in previous 24h
    velocity: float = 0.0     # Δmentions/hour
    acceleration: float = 0.0  # Δvelocity
    unique_creators: int = 0  # Breadth
    avg_engagement: float = 0.0
    freshness_score: float = 0.0  # How recent
    reproducibility: float = 0.0  # Is it a format vs one-off
    saturation: float = 0.0   # Penalty for peaking
    trend_score: float = 0.0  # Final composite score
    status: TrendStatus = TrendStatus.EMERGING
    example_posts: List[str] = field(default_factory=list)
    top_creators: List[str] = field(default_factory=list)
    co_occurring: List[str] = field(default_factory=list)  # Related hashtags/sounds


@dataclass 
class CrawlConfig:
    """Configuration for seedless crawling"""
    sample_size: int = 500           # Posts per crawl
    lookback_hours: int = 168        # How far back to sample (7 days)
    region: str = "US"               # Target region
    min_velocity: float = 0.1        # Minimum velocity to be a candidate
    min_mentions: int = 5            # Minimum mentions to be a candidate
    min_creators: int = 3            # Minimum unique creators (breadth)
    hook_word_range: Tuple[int, int] = (3, 8)  # Word count for hook phrases
    top_hooks_count: int = 30        # Top hooks to extract


# Common words/phrases to filter out of hooks
STOPWORD_PHRASES = {
    "the", "a", "an", "is", "are", "was", "were", "be", "been", "being",
    "have", "has", "had", "do", "does", "did", "will", "would", "could",
    "should", "may", "might", "must", "shall", "can", "need", "dare",
    "this is", "that is", "it is", "there is", "here is", "and the",
    "to the", "in the", "on the", "for the", "with the", "at the",
    "first piece", "and realize", "this is the", "that was", "i was",
    "you are", "we are", "they are", "he is", "she is", "it was",
}

# Hashtags to ignore (too generic or just noise)
IGNORED_HASHTAGS = {
    "fyp", "foryou", "foryoupage", "viral", "trending", "tiktok",
    "fy", "fypシ", "fypage", "explore", "explorepage", "1", "2", "3",
    "video", "reels", "reel", "like", "follow", "share", "comment",
}


class SeedlessCrawler:
    """
    Discovers trends by sampling random content and finding patterns.
    
    No keywords needed - the system discovers what's trending by analyzing
    what's repeating faster than normal across the sample.
    """
    
    def __init__(self, config: CrawlConfig = None):
        self.config = config or CrawlConfig()
        self.engine = create_engine(DATABASE_URL)
        self.http_client = None
    
    async def _get_client(self) -> httpx.AsyncClient:
        if self.http_client is None:
            self.http_client = httpx.AsyncClient(timeout=60.0)
        return self.http_client
    
    async def close(self):
        if self.http_client:
            await self.http_client.aclose()
            self.http_client = None
    
    # =========================================
    # Step 1: Seedless Ingestion
    # =========================================
    
    async def sample_tiktok_trending(self, count: int = None) -> List[Dict]:
        """
        Sample from TikTok's trending/discovery surfaces.
        
        Uses multiple approaches:
        1. Trending hashtags endpoint
        2. For You feed simulation (popular creators)
        3. Explore/discover surfaces
        """
        count = count or self.config.sample_size
        posts = []
        
        logger.info(f"🔍 Sampling {count} posts from TikTok discovery surfaces...")
        
        # Approach 1: Get trending videos via scraper API
        client = await self._get_client()
        
        try:
            # TikTok trending feed
            response = await client.get(
                "https://tiktok-scraper7.p.rapidapi.com/feed/list",
                headers={
                    "x-rapidapi-key": RAPIDAPI_KEY,
                    "x-rapidapi-host": "tiktok-scraper7.p.rapidapi.com"
                },
                params={"region": self.config.region, "count": str(min(count, 30))}
            )
            
            if response.status_code == 200:
                data = response.json()
                # Feed returns data as a list directly
                feed_data = data.get("data", [])
                if isinstance(feed_data, list):
                    videos = feed_data
                elif isinstance(feed_data, dict):
                    videos = feed_data.get("videos", [])
                else:
                    videos = []
                posts.extend(videos)
                logger.info(f"  ✓ Got {len(videos)} from trending feed")
        except Exception as e:
            logger.warning(f"Trending feed failed: {e}")
        
        # Approach 2: Sample from popular hashtags
        trending_hashtags = ["fyp", "viral", "trending", "foryou", "explore"]
        for hashtag in trending_hashtags[:3]:
            try:
                response = await client.get(
                    "https://tiktok-scraper7.p.rapidapi.com/challenge/posts",
                    headers={
                        "x-rapidapi-key": RAPIDAPI_KEY,
                        "x-rapidapi-host": "tiktok-scraper7.p.rapidapi.com"
                    },
                    params={"challenge_name": hashtag, "count": "20"}
                )
                
                if response.status_code == 200:
                    data = response.json()
                    videos = data.get("data", {}).get("videos", []) or []
                    posts.extend(videos)
                    logger.info(f"  ✓ Got {len(videos)} from #{hashtag}")
            except Exception as e:
                logger.debug(f"Hashtag {hashtag} failed: {e}")
            
            await asyncio.sleep(0.5)  # Rate limit
        
        # Approach 3: Sample from popular creator accounts (known to work)
        popular_creators = ["garyvee", "khaby.lame", "charlidamelio", "mrbeast", "addisonre"]
        for creator in popular_creators[:3]:
            try:
                response = await client.get(
                    "https://tiktok-scraper7.p.rapidapi.com/user/posts",
                    headers={
                        "x-rapidapi-key": RAPIDAPI_KEY,
                        "x-rapidapi-host": "tiktok-scraper7.p.rapidapi.com"
                    },
                    params={"unique_id": creator, "count": "10"}
                )
                
                if response.status_code == 200:
                    data = response.json()
                    videos = data.get("data", {}).get("videos", []) or []
                    posts.extend(videos)
                    logger.info(f"  ✓ Got {len(videos)} from @{creator}")
            except Exception as e:
                logger.debug(f"Creator {creator} failed: {e}")
            
            await asyncio.sleep(0.3)
        
        logger.success(f"✅ Sampled {len(posts)} total posts")
        return posts
    
    async def ingest_sample(
        self,
        posts: List[Dict],
        workspace_id: str = "00000000-0000-0000-0000-000000000001"
    ) -> int:
        """Normalize and store sampled posts"""
        saved = 0
        
        with self.engine.connect() as conn:
            for post in posts:
                try:
                    # Skip if not a dict
                    if not isinstance(post, dict):
                        continue
                    
                    post_id = str(post.get("video_id") or post.get("aweme_id") or post.get("id", ""))
                    if not post_id:
                        continue
                    
                    # Extract fields
                    title = post.get("title", "") or post.get("desc", "") or ""
                    
                    # Hashtags
                    hashtags = []
                    if title:
                        hashtags = re.findall(r'#(\w+)', title)
                    
                    # Music/Sound
                    music_info = post.get("music_info", {}) or post.get("music", {}) or {}
                    sound_id = str(music_info.get("id", "")) if isinstance(music_info, dict) else ""
                    sound_title = music_info.get("title", "") if isinstance(music_info, dict) else ""
                    
                    # Author
                    author = post.get("author", {}) or {}
                    author_handle = author.get("unique_id", "") if isinstance(author, dict) else ""
                    
                    # Metrics
                    views = post.get("play_count", 0) or 0
                    likes = post.get("digg_count", 0) or 0
                    comments = post.get("comment_count", 0) or 0
                    shares = post.get("share_count", 0) or 0
                    
                    # Timestamp
                    create_time = post.get("create_time") or post.get("createTime")
                    posted_at = None
                    if create_time:
                        try:
                            posted_at = datetime.fromtimestamp(int(create_time))
                        except:
                            pass
                    
                    # Insert
                    conn.execute(text("""
                        INSERT INTO posts_raw (
                            workspace_id, platform, platform_post_id,
                            author_handle, posted_at, fetched_at, caption_text,
                            hashtags, metrics, media_type, extra
                        ) VALUES (
                            :workspace_id, 'tiktok', :post_id,
                            :author_handle, :posted_at, NOW(), :caption,
                            :hashtags, :metrics, 'video', :extra
                        )
                        ON CONFLICT (platform, platform_post_id)
                        DO UPDATE SET metrics = :metrics, fetched_at = NOW()
                    """), {
                        "workspace_id": workspace_id,
                        "post_id": post_id,
                        "author_handle": author_handle,
                        "posted_at": posted_at,
                        "caption": title,
                        "hashtags": json.dumps(hashtags),
                        "metrics": json.dumps({"views": views, "likes": likes, "comments": comments, "shares": shares}),
                        "extra": json.dumps({"sound_id": sound_id, "sound_title": sound_title, "source": "seedless_crawler"})
                    })
                    saved += 1
                    
                except Exception as e:
                    logger.debug(f"Failed to save post: {e}")
            
            conn.commit()
        
        logger.info(f"💾 Saved {saved} posts to database")
        return saved
    
    # =========================================
    # Step 2: Feature Extraction
    # =========================================
    
    async def extract_features(
        self,
        workspace_id: str = "00000000-0000-0000-0000-000000000001",
        lookback_hours: int = None
    ) -> Dict[str, List[TrendCandidate]]:
        """
        Extract trend candidates from sampled posts.
        Returns candidates grouped by type: hashtags, sounds, hooks
        """
        lookback = lookback_hours or self.config.lookback_hours
        since = datetime.now(timezone.utc) - timedelta(hours=lookback)
        since_prev = since - timedelta(hours=24)
        
        logger.info(f"🔍 Extracting features from posts since {since}...")
        
        candidates = {
            "hashtags": [],
            "sounds": [],
            "hooks": [],
        }
        
        with self.engine.connect() as conn:
            # Get recent posts
            result = conn.execute(text("""
                SELECT 
                    platform_post_id, author_handle, posted_at,
                    caption_text, hashtags, metrics, extra
                FROM posts_raw
                WHERE workspace_id = :workspace_id
                  AND fetched_at >= :since
                ORDER BY posted_at DESC
            """), {"workspace_id": workspace_id, "since": since_prev})
            
            posts = [dict(row._mapping) for row in result.fetchall()]
        
        if not posts:
            logger.warning("No posts found for feature extraction")
            return candidates
        
        logger.info(f"  Analyzing {len(posts)} posts...")
        
        # Separate by time window (handle timezone-naive datetimes)
        def make_aware(dt):
            if dt is None:
                return None
            if dt.tzinfo is None:
                return dt.replace(tzinfo=timezone.utc)
            return dt
        
        recent_posts = [p for p in posts if p.get("posted_at") and make_aware(p["posted_at"]) >= since]
        prev_posts = [p for p in posts if p.get("posted_at") and make_aware(p["posted_at"]) < since]
        
        # Extract hashtag candidates
        candidates["hashtags"] = self._extract_hashtag_candidates(recent_posts, prev_posts)
        
        # Extract sound candidates
        candidates["sounds"] = self._extract_sound_candidates(recent_posts, prev_posts)
        
        # Extract hook candidates (n-grams)
        candidates["hooks"] = self._extract_hook_candidates(recent_posts, prev_posts)
        
        total = sum(len(v) for v in candidates.values())
        logger.success(f"✅ Extracted {total} trend candidates")
        
        return candidates
    
    def _extract_hashtag_candidates(
        self,
        recent: List[Dict],
        prev: List[Dict]
    ) -> List[TrendCandidate]:
        """Extract trending hashtags"""
        # Count hashtags in each period
        recent_counts = Counter()
        recent_creators = {}
        recent_engagement = {}
        
        for post in recent:
            hashtags = post.get("hashtags", [])
            if isinstance(hashtags, str):
                hashtags = json.loads(hashtags) if hashtags else []
            
            author = post.get("author_handle", "")
            metrics = post.get("metrics", {})
            if isinstance(metrics, str):
                metrics = json.loads(metrics) if metrics else {}
            
            engagement = metrics.get("views", 0) + metrics.get("likes", 0) * 10
            
            for tag in hashtags:
                tag_lower = tag.lower()
                recent_counts[tag_lower] += 1
                
                if tag_lower not in recent_creators:
                    recent_creators[tag_lower] = set()
                recent_creators[tag_lower].add(author)
                
                if tag_lower not in recent_engagement:
                    recent_engagement[tag_lower] = []
                recent_engagement[tag_lower].append(engagement)
        
        prev_counts = Counter()
        for post in prev:
            hashtags = post.get("hashtags", [])
            if isinstance(hashtags, str):
                hashtags = json.loads(hashtags) if hashtags else []
            for tag in hashtags:
                prev_counts[tag.lower()] += 1
        
        # Build candidates
        candidates = []
        for tag, count in recent_counts.most_common(100):
            if count < self.config.min_mentions:
                continue
            
            prev_count = prev_counts.get(tag, 0)
            velocity = (count - prev_count) / 24.0  # per hour
            
            creators = recent_creators.get(tag, set())
            if len(creators) < self.config.min_creators:
                continue
            
            engagements = recent_engagement.get(tag, [])
            avg_eng = sum(engagements) / len(engagements) if engagements else 0
            
            candidate = TrendCandidate(
                trend_type=TrendType.HASHTAG,
                identifier=tag,
                title=f"#{tag}",
                mentions_24h=count,
                mentions_prev_24h=prev_count,
                velocity=velocity,
                unique_creators=len(creators),
                avg_engagement=avg_eng,
                top_creators=list(creators)[:5],
            )
            
            # Calculate status
            candidate.status = self._determine_status(candidate)
            candidate.trend_score = self._calculate_trend_score(candidate)
            
            candidates.append(candidate)
        
        # Sort by trend score
        candidates.sort(key=lambda x: x.trend_score, reverse=True)
        return candidates[:50]
    
    def _extract_sound_candidates(
        self,
        recent: List[Dict],
        prev: List[Dict]
    ) -> List[TrendCandidate]:
        """Extract trending sounds"""
        recent_counts = Counter()
        recent_creators = {}
        sound_titles = {}
        
        for post in recent:
            extra = post.get("extra", {})
            if isinstance(extra, str):
                extra = json.loads(extra) if extra else {}
            
            sound_id = extra.get("sound_id", "")
            sound_title = extra.get("sound_title", "")
            author = post.get("author_handle", "")
            
            if sound_id:
                recent_counts[sound_id] += 1
                sound_titles[sound_id] = sound_title
                
                if sound_id not in recent_creators:
                    recent_creators[sound_id] = set()
                recent_creators[sound_id].add(author)
        
        prev_counts = Counter()
        for post in prev:
            extra = post.get("extra", {})
            if isinstance(extra, str):
                extra = json.loads(extra) if extra else {}
            sound_id = extra.get("sound_id", "")
            if sound_id:
                prev_counts[sound_id] += 1
        
        candidates = []
        for sound_id, count in recent_counts.most_common(50):
            if count < self.config.min_mentions:
                continue
            
            creators = recent_creators.get(sound_id, set())
            if len(creators) < self.config.min_creators:
                continue
            
            prev_count = prev_counts.get(sound_id, 0)
            velocity = (count - prev_count) / 24.0
            
            title = sound_titles.get(sound_id, f"Sound {sound_id[:8]}")
            
            candidate = TrendCandidate(
                trend_type=TrendType.SOUND,
                identifier=sound_id,
                title=f"🎵 {title}",
                mentions_24h=count,
                mentions_prev_24h=prev_count,
                velocity=velocity,
                unique_creators=len(creators),
                top_creators=list(creators)[:5],
            )
            
            candidate.status = self._determine_status(candidate)
            candidate.trend_score = self._calculate_trend_score(candidate)
            candidates.append(candidate)
        
        candidates.sort(key=lambda x: x.trend_score, reverse=True)
        return candidates[:30]
    
    def _extract_hook_candidates(
        self,
        recent: List[Dict],
        prev: List[Dict]
    ) -> List[TrendCandidate]:
        """Extract trending hook phrases (n-grams)"""
        recent_ngrams = Counter()
        recent_creators = {}
        
        for post in recent:
            caption = post.get("caption_text", "") or ""
            author = post.get("author_handle", "")
            
            # Clean caption
            caption = re.sub(r'#\w+', '', caption)  # Remove hashtags
            caption = re.sub(r'@\w+', '', caption)  # Remove mentions
            caption = caption.strip()
            
            if len(caption) < 10:
                continue
            
            # Extract n-grams
            words = caption.lower().split()
            for n in range(self.config.hook_ngram_range[0], self.config.hook_ngram_range[1] + 1):
                for i in range(len(words) - n + 1):
                    ngram = " ".join(words[i:i+n])
                    if len(ngram) > 10:  # Min length
                        recent_ngrams[ngram] += 1
                        
                        if ngram not in recent_creators:
                            recent_creators[ngram] = set()
                        recent_creators[ngram].add(author)
        
        prev_ngrams = Counter()
        for post in prev:
            caption = post.get("caption_text", "") or ""
            caption = re.sub(r'#\w+', '', caption)
            caption = re.sub(r'@\w+', '', caption)
            words = caption.lower().split()
            for n in range(self.config.hook_ngram_range[0], self.config.hook_ngram_range[1] + 1):
                for i in range(len(words) - n + 1):
                    ngram = " ".join(words[i:i+n])
                    if len(ngram) > 10:
                        prev_ngrams[ngram] += 1
        
        candidates = []
        for ngram, count in recent_ngrams.most_common(200):
            if count < self.config.min_mentions:
                continue
            
            creators = recent_creators.get(ngram, set())
            if len(creators) < self.config.min_creators:
                continue
            
            prev_count = prev_ngrams.get(ngram, 0)
            velocity = (count - prev_count) / 24.0
            
            if velocity < self.config.min_velocity:
                continue
            
            candidate = TrendCandidate(
                trend_type=TrendType.HOOK,
                identifier=ngram,
                title=f'"{ngram}"',
                mentions_24h=count,
                mentions_prev_24h=prev_count,
                velocity=velocity,
                unique_creators=len(creators),
                top_creators=list(creators)[:5],
            )
            
            candidate.status = self._determine_status(candidate)
            candidate.trend_score = self._calculate_trend_score(candidate)
            candidates.append(candidate)
        
        candidates.sort(key=lambda x: x.trend_score, reverse=True)
        return candidates[:self.config.top_hooks_count]
    
    # =========================================
    # Step 3: Trend Scoring
    # =========================================
    
    def _calculate_trend_score(self, candidate: TrendCandidate) -> float:
        """
        Calculate composite trend score.
        
        TrendScore = velocity * breadth * freshness * reproducibility - saturation
        """
        # Velocity component (0-1)
        velocity_score = min(candidate.velocity / 2.0, 1.0) if candidate.velocity > 0 else 0
        
        # Breadth (unique creators) - normalized
        breadth_score = min(candidate.unique_creators / 10.0, 1.0)
        
        # Freshness - higher if mentions are recent
        freshness_score = 0.8  # Default, could be computed from timestamps
        
        # Reproducibility - is it a repeatable format?
        # Higher if many creators, lower if concentrated
        reproducibility = breadth_score * 0.8 + 0.2
        
        # Saturation penalty - penalize if already peaked
        saturation = 0.0
        if candidate.mentions_prev_24h > candidate.mentions_24h * 1.5:
            saturation = 0.3  # Declining
        
        # Composite
        score = (
            velocity_score * 0.35 +
            breadth_score * 0.25 +
            freshness_score * 0.2 +
            reproducibility * 0.2
        ) - saturation
        
        return max(0, min(1, score))
    
    def _determine_status(self, candidate: TrendCandidate) -> TrendStatus:
        """Determine trend status based on velocity and mentions"""
        velocity = candidate.velocity
        mentions = candidate.mentions_24h
        prev_mentions = candidate.mentions_prev_24h
        
        if velocity > 0.5 and mentions < 20:
            return TrendStatus.EMERGING
        elif velocity > 0.3 and mentions >= 20:
            return TrendStatus.RISING
        elif velocity < 0.1 and mentions > 50:
            return TrendStatus.PEAK
        elif velocity < 0 or mentions < prev_mentions:
            return TrendStatus.DECLINING
        else:
            return TrendStatus.STABLE
    
    # =========================================
    # Step 4: Save Trends
    # =========================================
    
    async def save_trends(
        self,
        candidates: Dict[str, List[TrendCandidate]],
        workspace_id: str = "00000000-0000-0000-0000-000000000001"
    ) -> int:
        """Save trend candidates to database"""
        saved = 0
        
        with self.engine.connect() as conn:
            for trend_type, items in candidates.items():
                for candidate in items:
                    try:
                        # Check if exists
                        result = conn.execute(text("""
                            SELECT id FROM trend_clusters
                            WHERE workspace_id = :workspace_id
                              AND cluster_type = :cluster_type
                              AND title = :title
                        """), {
                            "workspace_id": workspace_id,
                            "cluster_type": candidate.trend_type.value,
                            "title": candidate.title,
                        })
                        existing = result.fetchone()
                        
                        if existing:
                            # Update
                            conn.execute(text("""
                                UPDATE trend_clusters SET
                                    status = :status,
                                    confidence = :confidence,
                                    updated_at = NOW()
                                WHERE id = :id
                            """), {
                                "id": existing[0],
                                "status": candidate.status.value,
                                "confidence": candidate.trend_score,
                            })
                        else:
                            # Insert
                            conn.execute(text("""
                                INSERT INTO trend_clusters (
                                    workspace_id, cluster_type, title, status, confidence
                                ) VALUES (
                                    :workspace_id, :cluster_type, :title, :status, :confidence
                                )
                            """), {
                                "workspace_id": workspace_id,
                                "cluster_type": candidate.trend_type.value,
                                "title": candidate.title,
                                "status": candidate.status.value,
                                "confidence": candidate.trend_score,
                            })
                        
                        saved += 1
                    except Exception as e:
                        logger.debug(f"Failed to save trend: {e}")
            
            conn.commit()
        
        logger.info(f"💾 Saved {saved} trend candidates")
        return saved
    
    # =========================================
    # Full Pipeline
    # =========================================
    
    async def discover(
        self,
        workspace_id: str = "00000000-0000-0000-0000-000000000001"
    ) -> Dict[str, Any]:
        """
        Run full seedless discovery pipeline.
        
        1. Sample from discovery surfaces
        2. Ingest and normalize
        3. Extract features
        4. Score and rank
        5. Save trends
        """
        logger.info("🚀 Starting seedless trend discovery...")
        
        results = {
            "sampled": 0,
            "ingested": 0,
            "candidates": {},
            "saved": 0,
        }
        
        # Step 1: Sample
        posts = await self.sample_tiktok_trending()
        results["sampled"] = len(posts)
        
        # Step 2: Ingest
        results["ingested"] = await self.ingest_sample(posts, workspace_id)
        
        # Step 3: Extract features
        candidates = await self.extract_features(workspace_id)
        results["candidates"] = {
            k: len(v) for k, v in candidates.items()
        }
        
        # Step 4: Save
        results["saved"] = await self.save_trends(candidates, workspace_id)
        
        # Log top trends
        logger.info("\n📊 Top Discovered Trends:")
        for trend_type, items in candidates.items():
            if items:
                logger.info(f"  {trend_type.upper()}:")
                for item in items[:3]:
                    logger.info(f"    - {item.title} (score={item.trend_score:.2f}, velocity={item.velocity:.2f})")
        
        await self.close()
        
        logger.success("✅ Seedless discovery complete")
        return results


# Singleton
_crawler = None

def get_seedless_crawler() -> SeedlessCrawler:
    global _crawler
    if _crawler is None:
        _crawler = SeedlessCrawler()
    return _crawler
