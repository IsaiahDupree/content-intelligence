"""
Account-Seeded Trend Crawler
============================
Discovers trends by crawling seed accounts (competitors) and extracting patterns.

The key insight: Don't search for keywords. Feed accounts, extract signals.

Flow:
1. Seed Set - Competitor accounts in your niche
2. Ingest - Pull recent posts/reels from each account
3. Extract - Hooks (caption starts), hashtags, topics, formats
4. Score - Velocity, breadth, performance uplift
5. Output - Trending Now cards for brief generation

Supports: TikTok (via tiktok-scraper7) and Instagram (via instagram-looter2)
"""
import os
import re
import json
import asyncio
from datetime import datetime, timedelta, timezone
from typing import List, Dict, Any, Optional, Set
from dataclasses import dataclass, field
from collections import Counter, defaultdict
from enum import Enum

import httpx
from loguru import logger
from sqlalchemy import create_engine, text

DATABASE_URL = os.getenv("DATABASE_URL", "postgresql://postgres:postgres@localhost:54322/postgres")
RAPIDAPI_KEY = os.getenv("RAPIDAPI_KEY", "")


class TrendType(str, Enum):
    HOOK = "hook"           # Opening phrase pattern
    HASHTAG = "hashtag"     # Tag cluster
    FORMAT = "format"       # Video structure/template
    TOPIC = "topic"         # Semantic cluster
    SOUND = "sound"         # Audio trend


class TrendStatus(str, Enum):
    EMERGING = "emerging"   # High velocity, low mentions
    RISING = "rising"       # High velocity, growing mentions
    PEAK = "peak"           # Slowing velocity, high mentions
    STABLE = "stable"       # Low velocity, medium mentions
    DECLINING = "declining" # Negative velocity


@dataclass
class TrendCandidate:
    """A discovered trend from account-seeded crawling"""
    trend_type: TrendType
    identifier: str           # The hook phrase, hashtag, etc.
    display_title: str        # Human-readable title
    mentions: int = 0         # Times seen in sample
    unique_creators: int = 0  # Breadth (different accounts using it)
    total_views: int = 0      # Sum of views on posts with this trend
    avg_engagement: float = 0.0  # Avg engagement rate
    velocity: float = 0.0     # Growth rate
    score: float = 0.0        # Composite trend score
    status: TrendStatus = TrendStatus.EMERGING
    example_posts: List[Dict] = field(default_factory=list)
    creators: List[str] = field(default_factory=list)


@dataclass
class SeedAccount:
    """An account to crawl for trends"""
    platform: str        # 'tiktok' or 'instagram'
    username: str        # @handle
    niche: str = ""      # Optional niche label
    priority: int = 1    # 1-10, higher = crawl more often


# Phrases that are too generic to be hooks
GENERIC_PHRASES = {
    "this is", "that is", "here is", "there is", "it is",
    "i am", "you are", "we are", "they are", "he is", "she is",
    "what if", "did you", "do you", "can you", "will you",
    "the way", "the thing", "the problem", "the best", "the worst",
    "i think", "i know", "i want", "i need", "i love", "i hate",
}

# Hashtags that are noise (platform-specific, not niche trends)
NOISE_HASHTAGS = {
    "fyp", "foryou", "foryoupage", "viral", "trending", "tiktok",
    "fy", "fypシ", "fypage", "explore", "explorepage", "reels",
    "reel", "instagram", "instagood", "love", "like", "follow",
    "photooftheday", "beautiful", "happy", "cute", "fashion",
    "1", "2", "3", "4", "5", "video", "share", "comment",
}


class TrendCrawler:
    """
    Account-seeded trend discovery.
    
    Instead of searching for keywords, we:
    1. Crawl a set of seed accounts (competitors/creators in niche)
    2. Extract trend signals from their content
    3. Score by velocity, breadth, and performance
    """
    
    def __init__(self):
        self.engine = create_engine(DATABASE_URL)
        self.http_client = None
        self.seed_accounts: List[SeedAccount] = []
    
    async def _get_client(self) -> httpx.AsyncClient:
        if self.http_client is None:
            self.http_client = httpx.AsyncClient(timeout=60.0)
        return self.http_client
    
    async def close(self):
        if self.http_client:
            await self.http_client.aclose()
            self.http_client = None
    
    # =========================================
    # Seed Account Management
    # =========================================
    
    def add_seed_accounts(self, accounts: List[Dict]):
        """Add accounts to the seed set"""
        for acc in accounts:
            self.seed_accounts.append(SeedAccount(
                platform=acc.get("platform", "tiktok"),
                username=acc.get("username", ""),
                niche=acc.get("niche", ""),
                priority=acc.get("priority", 1),
            ))
        logger.info(f"📌 Added {len(accounts)} seed accounts")
    
    def set_seed_accounts(self, accounts: List[Dict]):
        """Replace seed set with new accounts"""
        self.seed_accounts = []
        self.add_seed_accounts(accounts)
    
    async def load_seed_accounts_from_db(
        self,
        workspace_id: str = "00000000-0000-0000-0000-000000000001"
    ):
        """Load seed accounts from workspace_sources table"""
        with self.engine.connect() as conn:
            result = conn.execute(text("""
                SELECT platform, identifier, niche
                FROM workspace_sources
                WHERE workspace_id = :workspace_id
                  AND is_active = true
                  AND source_type = 'account'
            """), {"workspace_id": workspace_id})
            
            for row in result.fetchall():
                self.seed_accounts.append(SeedAccount(
                    platform=row[0],
                    username=row[1],
                    niche=row[2] or "",
                ))
        
        logger.info(f"📂 Loaded {len(self.seed_accounts)} seed accounts from DB")
    
    # =========================================
    # Ingest from Seed Accounts
    # =========================================
    
    async def crawl_seed_accounts(self, posts_per_account: int = 30) -> List[Dict]:
        """Crawl recent posts from all seed accounts"""
        all_posts = []
        
        logger.info(f"🔍 Crawling {len(self.seed_accounts)} seed accounts...")
        
        for account in self.seed_accounts:
            try:
                if account.platform == "tiktok":
                    posts = await self._fetch_tiktok_posts(account.username, posts_per_account)
                elif account.platform == "instagram":
                    posts = await self._fetch_instagram_posts(account.username, posts_per_account)
                else:
                    continue
                
                # Tag posts with source account
                for post in posts:
                    post["_source_account"] = account.username
                    post["_platform"] = account.platform
                    post["_niche"] = account.niche
                
                all_posts.extend(posts)
                logger.info(f"  ✓ @{account.username}: {len(posts)} posts")
                
                await asyncio.sleep(0.5)  # Rate limit
                
            except Exception as e:
                logger.warning(f"  ✗ @{account.username} failed: {e}")
        
        logger.success(f"✅ Crawled {len(all_posts)} total posts")
        return all_posts
    
    async def _fetch_tiktok_posts(self, username: str, count: int) -> List[Dict]:
        """Fetch recent posts from a TikTok account"""
        client = await self._get_client()
        
        response = await client.get(
            "https://tiktok-scraper7.p.rapidapi.com/user/posts",
            headers={
                "x-rapidapi-key": RAPIDAPI_KEY,
                "x-rapidapi-host": "tiktok-scraper7.p.rapidapi.com"
            },
            params={"unique_id": username, "count": str(count)}
        )
        
        if response.status_code == 200:
            data = response.json()
            return data.get("data", {}).get("videos", []) or []
        return []
    
    async def _fetch_instagram_posts(self, username: str, count: int) -> List[Dict]:
        """Fetch recent posts from an Instagram account"""
        client = await self._get_client()
        
        # First get user ID
        response = await client.get(
            "https://instagram-looter2.p.rapidapi.com/profile",
            headers={
                "x-rapidapi-key": RAPIDAPI_KEY,
                "x-rapidapi-host": "instagram-looter2.p.rapidapi.com"
            },
            params={"username": username}
        )
        
        if response.status_code != 200:
            return []
        
        user_data = response.json()
        user_id = user_data.get("id") or user_data.get("pk")
        
        if not user_id:
            return []
        
        # Now get posts
        response = await client.get(
            "https://instagram-looter2.p.rapidapi.com/reels",
            headers={
                "x-rapidapi-key": RAPIDAPI_KEY,
                "x-rapidapi-host": "instagram-looter2.p.rapidapi.com"
            },
            params={"user_id": str(user_id), "count": str(count)}
        )
        
        if response.status_code == 200:
            data = response.json()
            return data.get("items", []) or data.get("data", []) or []
        return []
    
    # =========================================
    # Extract Trend Signals
    # =========================================
    
    def extract_trends(self, posts: List[Dict]) -> Dict[str, List[TrendCandidate]]:
        """Extract trend candidates from crawled posts"""
        logger.info(f"🔍 Extracting trends from {len(posts)} posts...")
        
        trends = {
            "hooks": self._extract_hooks(posts),
            "hashtags": self._extract_hashtags(posts),
            "sounds": self._extract_sounds(posts),
        }
        
        total = sum(len(v) for v in trends.values())
        logger.success(f"✅ Found {total} trend candidates")
        
        return trends
    
    def _extract_hooks(self, posts: List[Dict]) -> List[TrendCandidate]:
        """
        Extract hook phrases from the START of captions.
        
        Hooks are the first 3-8 words that grab attention.
        We look for phrases that appear across multiple creators.
        """
        hook_data = defaultdict(lambda: {
            "count": 0,
            "creators": set(),
            "total_views": 0,
            "examples": [],
        })
        
        for post in posts:
            # Get caption
            caption = post.get("title", "") or post.get("desc", "") or post.get("caption", "")
            if isinstance(caption, dict):
                caption = caption.get("text", "")
            
            if not caption or len(caption) < 10:
                continue
            
            # Clean caption - remove hashtags and mentions at end
            clean_caption = re.sub(r'#\w+', '', caption)
            clean_caption = re.sub(r'@\w+', '', clean_caption)
            clean_caption = clean_caption.strip()
            
            # Extract the FIRST 3-8 words as potential hook
            words = clean_caption.split()[:8]
            if len(words) < 3:
                continue
            
            # Try different lengths for hook
            for length in [5, 4, 3, 6, 7]:
                if len(words) >= length:
                    hook = " ".join(words[:length]).lower().strip()
                    
                    # Skip generic phrases
                    if any(hook.startswith(g) for g in GENERIC_PHRASES):
                        continue
                    
                    # Skip if too short or just punctuation
                    if len(hook) < 15 or not re.search(r'[a-z]', hook):
                        continue
                    
                    creator = post.get("_source_account", "") or post.get("author", {}).get("unique_id", "")
                    views = post.get("play_count", 0) or post.get("view_count", 0) or 0
                    
                    hook_data[hook]["count"] += 1
                    hook_data[hook]["creators"].add(creator)
                    hook_data[hook]["total_views"] += views
                    
                    if len(hook_data[hook]["examples"]) < 5:
                        hook_data[hook]["examples"].append({
                            "caption": caption[:100],
                            "creator": creator,
                            "views": views,
                        })
                    
                    break  # Only extract one hook per post
        
        # Convert to candidates
        candidates = []
        for hook, data in hook_data.items():
            # Include if seen multiple times (lower threshold for small samples)
            if data["count"] >= 2:
                candidate = TrendCandidate(
                    trend_type=TrendType.HOOK,
                    identifier=hook,
                    display_title=f'"{hook}..."',
                    mentions=data["count"],
                    unique_creators=len(data["creators"]),
                    total_views=data["total_views"],
                    creators=list(data["creators"])[:5],
                    example_posts=data["examples"],
                )
                candidate.score = self._calculate_score(candidate)
                candidate.status = self._determine_status(candidate)
                candidates.append(candidate)
        
        # Sort by score
        candidates.sort(key=lambda x: x.score, reverse=True)
        return candidates[:20]
    
    def _extract_hashtags(self, posts: List[Dict]) -> List[TrendCandidate]:
        """Extract hashtag trends from posts"""
        hashtag_data = defaultdict(lambda: {
            "count": 0,
            "creators": set(),
            "total_views": 0,
            "examples": [],
        })
        
        for post in posts:
            caption = post.get("title", "") or post.get("desc", "") or post.get("caption", "")
            if isinstance(caption, dict):
                caption = caption.get("text", "")
            
            # Extract hashtags
            hashtags = re.findall(r'#(\w+)', caption.lower())
            
            creator = post.get("_source_account", "") or post.get("author", {}).get("unique_id", "")
            views = post.get("play_count", 0) or post.get("view_count", 0) or 0
            
            for tag in hashtags:
                # Skip noise hashtags
                if tag in NOISE_HASHTAGS or len(tag) < 3:
                    continue
                
                hashtag_data[tag]["count"] += 1
                hashtag_data[tag]["creators"].add(creator)
                hashtag_data[tag]["total_views"] += views
                
                if len(hashtag_data[tag]["examples"]) < 3:
                    hashtag_data[tag]["examples"].append({
                        "creator": creator,
                        "views": views,
                    })
        
        # Convert to candidates
        candidates = []
        for tag, data in hashtag_data.items():
            # Include if seen multiple times
            if data["count"] >= 2:
                candidate = TrendCandidate(
                    trend_type=TrendType.HASHTAG,
                    identifier=tag,
                    display_title=f"#{tag}",
                    mentions=data["count"],
                    unique_creators=len(data["creators"]),
                    total_views=data["total_views"],
                    creators=list(data["creators"])[:5],
                    example_posts=data["examples"],
                )
                candidate.score = self._calculate_score(candidate)
                candidate.status = self._determine_status(candidate)
                candidates.append(candidate)
        
        candidates.sort(key=lambda x: x.score, reverse=True)
        return candidates[:20]
    
    def _extract_sounds(self, posts: List[Dict]) -> List[TrendCandidate]:
        """Extract sound/music trends from posts"""
        sound_data = defaultdict(lambda: {
            "count": 0,
            "creators": set(),
            "total_views": 0,
            "title": "",
        })
        
        for post in posts:
            music = post.get("music_info", {}) or post.get("music", {}) or {}
            if not isinstance(music, dict):
                continue
            
            sound_id = str(music.get("id", ""))
            sound_title = music.get("title", "") or music.get("name", "")
            
            if not sound_id or sound_id == "0":
                continue
            
            creator = post.get("_source_account", "") or post.get("author", {}).get("unique_id", "")
            views = post.get("play_count", 0) or 0
            
            sound_data[sound_id]["count"] += 1
            sound_data[sound_id]["creators"].add(creator)
            sound_data[sound_id]["total_views"] += views
            sound_data[sound_id]["title"] = sound_title
        
        candidates = []
        for sound_id, data in sound_data.items():
            if len(data["creators"]) >= 2 and data["count"] >= 3:
                candidate = TrendCandidate(
                    trend_type=TrendType.SOUND,
                    identifier=sound_id,
                    display_title=f"🎵 {data['title'][:40]}" if data['title'] else f"🎵 Sound {sound_id[:8]}",
                    mentions=data["count"],
                    unique_creators=len(data["creators"]),
                    total_views=data["total_views"],
                    creators=list(data["creators"])[:5],
                )
                candidate.score = self._calculate_score(candidate)
                candidate.status = self._determine_status(candidate)
                candidates.append(candidate)
        
        candidates.sort(key=lambda x: x.score, reverse=True)
        return candidates[:10]
    
    # =========================================
    # Scoring
    # =========================================
    
    def _calculate_score(self, candidate: TrendCandidate) -> float:
        """
        Calculate trend score based on:
        - Breadth (unique creators) - most important
        - Mentions (frequency)
        - Performance (total views)
        """
        # Breadth score (0-0.4) - unique creators is key signal
        breadth = min(candidate.unique_creators / 10, 1.0) * 0.4
        
        # Frequency score (0-0.3)
        frequency = min(candidate.mentions / 20, 1.0) * 0.3
        
        # Performance score (0-0.3)
        if candidate.total_views > 0:
            # Log scale for views
            import math
            perf = min(math.log10(candidate.total_views + 1) / 7, 1.0) * 0.3
        else:
            perf = 0.0
        
        return breadth + frequency + perf
    
    def _determine_status(self, candidate: TrendCandidate) -> TrendStatus:
        """Determine trend status based on score and breadth"""
        if candidate.score > 0.6:
            return TrendStatus.RISING
        elif candidate.score > 0.4:
            return TrendStatus.EMERGING
        elif candidate.unique_creators >= 5:
            return TrendStatus.STABLE
        else:
            return TrendStatus.EMERGING
    
    # =========================================
    # Save to Database
    # =========================================
    
    async def save_trends(
        self,
        trends: Dict[str, List[TrendCandidate]],
        workspace_id: str = "00000000-0000-0000-0000-000000000001"
    ) -> int:
        """Save discovered trends to database"""
        saved = 0
        
        with self.engine.connect() as conn:
            # Clear old discovered trends (keep manually created ones)
            conn.execute(text("""
                DELETE FROM trend_clusters 
                WHERE workspace_id = :workspace_id 
                  AND cluster_type IN ('hook', 'hashtag', 'sound', 'format', 'topic')
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
        
        logger.info(f"💾 Saved {saved} trends to database")
        return saved
    
    # =========================================
    # Full Pipeline
    # =========================================
    
    async def discover(
        self,
        workspace_id: str = "00000000-0000-0000-0000-000000000001",
        posts_per_account: int = 30
    ) -> Dict[str, Any]:
        """
        Run full trend discovery pipeline.
        
        1. Load seed accounts from DB (or use pre-set ones)
        2. Crawl recent posts from each
        3. Extract trend signals
        4. Score and rank
        5. Save to database
        """
        logger.info("🚀 Starting account-seeded trend discovery...")
        
        results = {
            "seed_accounts": 0,
            "posts_crawled": 0,
            "trends_found": {},
            "saved": 0,
        }
        
        # Load seed accounts if not already set
        if not self.seed_accounts:
            await self.load_seed_accounts_from_db(workspace_id)
        
        results["seed_accounts"] = len(self.seed_accounts)
        
        if not self.seed_accounts:
            logger.warning("No seed accounts configured. Add accounts first.")
            return results
        
        # Crawl posts
        posts = await self.crawl_seed_accounts(posts_per_account)
        results["posts_crawled"] = len(posts)
        
        if not posts:
            logger.warning("No posts crawled from seed accounts")
            return results
        
        # Extract trends
        trends = self.extract_trends(posts)
        results["trends_found"] = {k: len(v) for k, v in trends.items()}
        
        # Save to DB
        results["saved"] = await self.save_trends(trends, workspace_id)
        
        # Log summary
        logger.info("\n📊 Discovered Trends:")
        for trend_type, candidates in trends.items():
            if candidates:
                logger.info(f"  {trend_type.upper()}:")
                for c in candidates[:3]:
                    logger.info(f"    • {c.display_title} (score={c.score:.2f}, creators={c.unique_creators})")
        
        await self.close()
        
        logger.success("✅ Trend discovery complete")
        return results


# Singleton
_crawler = None

def get_trend_crawler() -> TrendCrawler:
    global _crawler
    if _crawler is None:
        _crawler = TrendCrawler()
    return _crawler
