"""
Trend Crawler Service
Monitors seed accounts to detect trending audio, hashtags, and content formats
"""
import os
import asyncio
from typing import List, Dict, Set, Optional
from datetime import datetime, date, timedelta
from collections import defaultdict
from loguru import logger
from sqlalchemy import create_engine, text

from .instagram_service import get_instagram_service
from .adapters import MediaItem, MediaType

DATABASE_URL = os.getenv("DATABASE_URL", "postgresql://postgres:postgres@localhost:54322/postgres")


class TrendCrawler:
    """
    Crawls seed accounts to identify trending content patterns.
    
    Tracks:
    - Audio usage frequency and velocity
    - Hashtag usage and growth
    - Content format patterns (hooks, POVs, etc.)
    """
    
    def __init__(self, seed_accounts: Optional[List[str]] = None):
        """
        Initialize trend crawler with seed accounts.
        
        Args:
            seed_accounts: List of Instagram usernames to monitor
        """
        self.engine = create_engine(DATABASE_URL)
        self.instagram_service = get_instagram_service()
        
        # Default seed accounts (high-engagement creators)
        self.seed_accounts = seed_accounts or [
            "instagram",
            "natgeo",
            "nike",
            "redbull",
            "gopro",
            "netflix",
            "spotify",
            "airbnb",
            "starbucks",
            "cocacola"
        ]
        
        logger.info(f"Trend crawler initialized with {len(self.seed_accounts)} seed accounts")
    
    async def crawl_all_seeds(self, reels_per_account: int = 50):
        """
        Crawl all seed accounts and extract trend data.
        
        Args:
            reels_per_account: Number of recent reels to fetch per account
        """
        logger.info(f"Starting trend crawl of {len(self.seed_accounts)} accounts")
        
        audio_counts = defaultdict(int)
        hashtag_counts = defaultdict(int)
        format_patterns = defaultdict(int)
        
        for username in self.seed_accounts:
            try:
                logger.info(f"Crawling {username}...")
                
                # Fetch recent reels
                reels_page = await self.instagram_service.fetch_and_save_reels(
                    username,
                    limit=reels_per_account
                )
                
                # Extract trends from reels
                for reel in reels_page.items:
                    # Track audio usage
                    if reel.audio:
                        audio_counts[reel.audio.id] += 1
                    
                    # Track hashtags
                    for hashtag in reel.hashtags:
                        hashtag_counts[hashtag] += 1
                    
                    # Detect format patterns
                    format_type = self._detect_format(reel)
                    if format_type:
                        format_patterns[format_type] += 1
                
                logger.info(f"✓ Crawled {username}: {len(reels_page.items)} reels")
                
                # Rate limiting
                await asyncio.sleep(2)
                
            except Exception as e:
                logger.error(f"Failed to crawl {username}: {e}")
        
        # Save observations to database
        today = date.today()
        self._save_audio_observations(audio_counts, today)
        self._save_hashtag_observations(hashtag_counts, today)
        self._save_format_observations(format_patterns, today)
        
        logger.info(f"Crawl complete: {len(audio_counts)} audio tracks, {len(hashtag_counts)} hashtags, {len(format_patterns)} formats")
        
        return {
            "audio_count": len(audio_counts),
            "hashtag_count": len(hashtag_counts),
            "format_count": len(format_patterns),
            "accounts_crawled": len(self.seed_accounts)
        }
    
    def _detect_format(self, reel: MediaItem) -> Optional[str]:
        """
        Detect content format type from reel.
        
        Returns format type string or None
        """
        caption = reel.caption.lower() if reel.caption else ""
        
        # Text-Hook Short-Form
        if any(word in caption for word in ["wait for it", "watch till end", "wait until"]):
            return "text-hook-short-form"
        
        # POV (Point of View)
        if caption.startswith("pov:") or "pov:" in caption:
            return "pov"
        
        # Tutorial/How-To
        if any(word in caption for word in ["how to", "tutorial", "step by step", "guide"]):
            return "tutorial"
        
        # Storytelling
        if any(word in caption for word in ["story time", "storytime", "let me tell you"]):
            return "storytelling"
        
        # Behind the Scenes
        if any(word in caption for word in ["bts", "behind the scenes", "backstage"]):
            return "behind-the-scenes"
        
        # Transformation
        if any(word in caption for word in ["before and after", "transformation", "glow up"]):
            return "transformation"
        
        # Day in the Life
        if any(word in caption for word in ["day in", "vlog", "daily routine"]):
            return "day-in-life"
        
        # Overhead/Flat Lay
        if any(word in caption for word in ["overhead", "flat lay", "top view"]):
            return "overhead-flat-lay"
        
        return None
    
    def _save_audio_observations(self, audio_counts: Dict[str, int], observation_date: date):
        """Save audio usage observations to database"""
        with self.engine.connect() as conn:
            for audio_id, count in audio_counts.items():
                try:
                    conn.execute(text("""
                        INSERT INTO trend_observations (
                            entity_type, entity_id, observation_date, usage_count
                        )
                        VALUES ('audio', :audio_id, :date, :count)
                        ON CONFLICT (entity_type, entity_id, observation_date, region)
                        DO UPDATE SET usage_count = EXCLUDED.usage_count
                    """), {
                        "audio_id": audio_id,
                        "date": observation_date,
                        "count": count
                    })
                except Exception as e:
                    logger.warning(f"Failed to save audio observation: {e}")
            
            conn.commit()
            logger.info(f"Saved {len(audio_counts)} audio observations")
    
    def _save_hashtag_observations(self, hashtag_counts: Dict[str, int], observation_date: date):
        """Save hashtag usage observations to database"""
        with self.engine.connect() as conn:
            for hashtag, count in hashtag_counts.items():
                try:
                    conn.execute(text("""
                        INSERT INTO trend_observations (
                            entity_type, entity_id, observation_date, usage_count
                        )
                        VALUES ('hashtag', :hashtag, :date, :count)
                        ON CONFLICT (entity_type, entity_id, observation_date, region)
                        DO UPDATE SET usage_count = EXCLUDED.usage_count
                    """), {
                        "hashtag": hashtag,
                        "date": observation_date,
                        "count": count
                    })
                except Exception as e:
                    logger.warning(f"Failed to save hashtag observation: {e}")
            
            conn.commit()
            logger.info(f"Saved {len(hashtag_counts)} hashtag observations")
    
    def _save_format_observations(self, format_counts: Dict[str, int], observation_date: date):
        """Save format usage observations to database"""
        with self.engine.connect() as conn:
            for format_type, count in format_counts.items():
                try:
                    conn.execute(text("""
                        INSERT INTO trend_observations (
                            entity_type, entity_id, observation_date, usage_count
                        )
                        VALUES ('format', :format_type, :date, :count)
                        ON CONFLICT (entity_type, entity_id, observation_date, region)
                        DO UPDATE SET usage_count = EXCLUDED.usage_count
                    """), {
                        "format_type": format_type,
                        "date": observation_date,
                        "count": count
                    })
                except Exception as e:
                    logger.warning(f"Failed to save format observation: {e}")
            
            conn.commit()
            logger.info(f"Saved {len(format_counts)} format observations")
    
    async def crawl_trending_hashtags(self, hashtags: List[str]):
        """
        Crawl specific hashtags to gather trend data.
        
        Args:
            hashtags: List of hashtags to crawl
        """
        logger.info(f"Crawling {len(hashtags)} trending hashtags")
        
        for hashtag in hashtags:
            try:
                hashtag_data = await self.instagram_service.fetch_and_save_hashtag(hashtag)
                logger.info(f"✓ Crawled #{hashtag}: {hashtag_data.media_count} posts")
                
                # Rate limiting
                await asyncio.sleep(2)
                
            except Exception as e:
                logger.error(f"Failed to crawl #{hashtag}: {e}")


# Singleton instance
_crawler_instance = None

def get_trend_crawler() -> TrendCrawler:
    """Get or create trend crawler singleton"""
    global _crawler_instance
    if _crawler_instance is None:
        _crawler_instance = TrendCrawler()
    return _crawler_instance
