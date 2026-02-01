"""
Instagram Service
High-level service for Instagram data operations using provider adapters
"""
import os
from typing import Optional, List
from loguru import logger
from sqlalchemy import create_engine, text
from datetime import datetime

from .adapters import (
    InstagramAdapter,
    RapidApiInstagramAdapter,
    Profile,
    MediaItem,
    MediaPage,
    HashtagData,
    SearchResults,
    SearchType
)

DATABASE_URL = os.getenv("DATABASE_URL", "postgresql://postgres:postgres@localhost:54322/postgres")


class InstagramService:
    """
    Main service for Instagram operations.
    Manages provider adapters and database persistence.
    """
    
    def __init__(self, adapter: Optional[InstagramAdapter] = None):
        """
        Initialize service with a provider adapter.
        Defaults to RapidAPI adapter if none provided.
        """
        self.adapter = adapter or RapidApiInstagramAdapter()
        self.engine = create_engine(DATABASE_URL)
        logger.info(f"Instagram service initialized with {self.adapter.name}")
    
    async def fetch_and_save_profile(self, identifier: str) -> Profile:
        """
        Fetch profile from provider and save to database.
        
        Args:
            identifier: Username, user ID, or profile URL
            
        Returns:
            Profile object
        """
        logger.info(f"Fetching profile: {identifier}")
        profile = await self.adapter.get_profile(identifier)
        
        # Save to database
        self._save_profile(profile)
        
        logger.info(f"Saved profile: {profile.username} ({profile.followers_count} followers)")
        return profile
    
    async def fetch_and_save_media(
        self,
        identifier: str,
        cursor: Optional[str] = None,
        limit: int = 50
    ) -> MediaPage:
        """
        Fetch media from provider and save to database.
        
        Args:
            identifier: Username, user ID, or profile URL
            cursor: Pagination cursor
            limit: Max items to fetch
            
        Returns:
            MediaPage with items
        """
        logger.info(f"Fetching media for: {identifier}")
        media_page = await self.adapter.get_media(identifier, cursor, limit)
        
        # Get or create profile first
        profile = await self.fetch_and_save_profile(identifier)
        
        # Save media items
        for item in media_page.items:
            self._save_media_item(item, profile.id)
        
        logger.info(f"Saved {len(media_page.items)} media items for {identifier}")
        return media_page
    
    async def fetch_and_save_reels(
        self,
        identifier: str,
        cursor: Optional[str] = None,
        limit: int = 50
    ) -> MediaPage:
        """
        Fetch reels from provider and save to database.
        
        Args:
            identifier: Username, user ID, or profile URL
            cursor: Pagination cursor
            limit: Max items to fetch
            
        Returns:
            MediaPage with reel items
        """
        logger.info(f"Fetching reels for: {identifier}")
        reels_page = await self.adapter.get_reels(identifier, cursor, limit)
        
        # Get or create profile first
        profile = await self.fetch_and_save_profile(identifier)
        
        # Save reel items
        for item in reels_page.items:
            self._save_media_item(item, profile.id)
            
            # Save audio info if present
            if item.audio:
                self._save_audio(item.audio)
        
        logger.info(f"Saved {len(reels_page.items)} reels for {identifier}")
        return reels_page
    
    async def fetch_and_save_hashtag(self, tag: str) -> HashtagData:
        """
        Fetch hashtag data and save to database.
        
        Args:
            tag: Hashtag (with or without #)
            
        Returns:
            HashtagData object
        """
        logger.info(f"Fetching hashtag: {tag}")
        hashtag_data = await self.adapter.get_hashtag(tag)
        
        # Save hashtag info
        self._save_hashtag(hashtag_data)
        
        # Save top and recent posts
        for item in hashtag_data.top_posts + hashtag_data.recent_posts:
            self._save_media_item(item, None)
        
        logger.info(f"Saved hashtag {tag} with {len(hashtag_data.top_posts)} top posts")
        return hashtag_data
    
    def _save_profile(self, profile: Profile):
        """Save profile to database"""
        with self.engine.connect() as conn:
            conn.execute(text("""
                INSERT INTO ig_profiles (
                    id, username, full_name, bio, followers_count, following_count,
                    media_count, is_verified, profile_pic_url, provider,
                    external_url, is_business, category, last_fetched_at
                )
                VALUES (
                    :id, :username, :full_name, :bio, :followers_count, :following_count,
                    :media_count, :is_verified, :profile_pic_url, :provider,
                    :external_url, :is_business, :category, NOW()
                )
                ON CONFLICT (username) DO UPDATE SET
                    full_name = EXCLUDED.full_name,
                    bio = EXCLUDED.bio,
                    followers_count = EXCLUDED.followers_count,
                    following_count = EXCLUDED.following_count,
                    media_count = EXCLUDED.media_count,
                    is_verified = EXCLUDED.is_verified,
                    profile_pic_url = EXCLUDED.profile_pic_url,
                    external_url = EXCLUDED.external_url,
                    is_business = EXCLUDED.is_business,
                    category = EXCLUDED.category,
                    last_fetched_at = NOW()
            """), {
                "id": profile.id,
                "username": profile.username,
                "full_name": profile.full_name,
                "bio": profile.bio,
                "followers_count": profile.followers_count,
                "following_count": profile.following_count,
                "media_count": profile.media_count,
                "is_verified": profile.is_verified,
                "profile_pic_url": profile.profile_pic_url,
                "provider": profile.provider,
                "external_url": profile.external_url,
                "is_business": profile.is_business,
                "category": profile.category
            })
            conn.commit()
    
    def _save_media_item(self, item: MediaItem, profile_id: Optional[str]):
        """Save media item to database"""
        with self.engine.connect() as conn:
            conn.execute(text("""
                INSERT INTO ig_media (
                    id, ig_media_id, profile_id, media_type, caption, permalink,
                    thumbnail_url, like_count, comment_count, play_count,
                    timestamp, video_url, audio_id, hashtags, mentions
                )
                VALUES (
                    gen_random_uuid(), :ig_media_id, :profile_id, :media_type, :caption, :permalink,
                    :thumbnail_url, :like_count, :comment_count, :play_count,
                    :timestamp, :video_url, :audio_id, :hashtags, :mentions
                )
                ON CONFLICT (ig_media_id) DO UPDATE SET
                    like_count = EXCLUDED.like_count,
                    comment_count = EXCLUDED.comment_count,
                    play_count = EXCLUDED.play_count
            """), {
                "ig_media_id": item.id,
                "profile_id": profile_id,
                "media_type": item.media_type.value,
                "caption": item.caption,
                "permalink": item.permalink,
                "thumbnail_url": item.thumbnail_url,
                "like_count": item.like_count,
                "comment_count": item.comment_count,
                "play_count": item.play_count,
                "timestamp": item.timestamp,
                "video_url": item.video_url,
                "audio_id": item.audio.id if item.audio else None,
                "hashtags": item.hashtags,
                "mentions": item.mentions
            })
            conn.commit()
    
    def _save_audio(self, audio):
        """Save audio info to database"""
        with self.engine.connect() as conn:
            conn.execute(text("""
                INSERT INTO ig_audio (
                    id, audio_id, title, artist, duration_ms, first_seen_at
                )
                VALUES (
                    gen_random_uuid(), :audio_id, :title, :artist, :duration_ms, NOW()
                )
                ON CONFLICT (audio_id) DO UPDATE SET
                    usage_count = ig_audio.usage_count + 1,
                    last_updated_at = NOW()
            """), {
                "audio_id": audio.id,
                "title": audio.title,
                "artist": audio.artist,
                "duration_ms": audio.duration_ms
            })
            conn.commit()
    
    def _save_hashtag(self, hashtag_data: HashtagData):
        """Save hashtag info to database"""
        with self.engine.connect() as conn:
            conn.execute(text("""
                INSERT INTO ig_hashtags (
                    id, tag, media_count, last_updated_at
                )
                VALUES (
                    gen_random_uuid(), :tag, :media_count, NOW()
                )
                ON CONFLICT (tag) DO UPDATE SET
                    media_count = EXCLUDED.media_count,
                    last_updated_at = NOW()
            """), {
                "tag": hashtag_data.tag,
                "media_count": hashtag_data.media_count
            })
            conn.commit()


# Singleton instance
_service_instance = None

def get_instagram_service() -> InstagramService:
    """Get or create Instagram service singleton"""
    global _service_instance
    if _service_instance is None:
        _service_instance = InstagramService()
    return _service_instance
