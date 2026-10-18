"""
Ingest Service - Pull and normalize posts from social platforms
"""
import os
import json
import asyncio
from datetime import datetime, timedelta
from typing import List, Dict, Any, Optional
from dataclasses import asdict

import httpx
from loguru import logger
from sqlalchemy import create_engine, text

from .models import PostRaw, PostMetrics, AudioRef, Platform, WorkspaceSource


DATABASE_URL = os.getenv("DATABASE_URL", "postgresql://postgres:postgres@localhost:54322/postgres")
RAPIDAPI_KEY = os.getenv("RAPIDAPI_KEY", "")

# Platform-specific RapidAPI hosts
INSTAGRAM_HOST = "instagram-looter2.p.rapidapi.com"
TIKTOK_HOST = "tiktok-scraper7.p.rapidapi.com"


class IngestService:
    """
    Service for ingesting and normalizing posts from social platforms.
    
    Supports:
    - Instagram (via RapidAPI Instagram Looter)
    - TikTok (via RapidAPI)
    - YouTube (planned)
    """
    
    def __init__(self):
        self.engine = create_engine(DATABASE_URL)
        self.http_client = None
    
    async def _get_client(self) -> httpx.AsyncClient:
        if self.http_client is None:
            self.http_client = httpx.AsyncClient(timeout=30.0)
        return self.http_client
    
    async def close(self):
        if self.http_client:
            await self.http_client.aclose()
            self.http_client = None
    
    # =========================================
    # RapidAPI Helpers
    # =========================================
    
    async def _rapidapi_get(self, path: str, host: str = INSTAGRAM_HOST) -> Dict:
        """Make a GET request to RapidAPI"""
        client = await self._get_client()
        url = f"https://{host}{path}"
        
        headers = {
            "x-rapidapi-key": RAPIDAPI_KEY,
            "x-rapidapi-host": host,
        }
        
        try:
            response = await client.get(url, headers=headers)
            response.raise_for_status()
            return response.json()
        except Exception as e:
            logger.error(f"RapidAPI error: {e}")
            return {}
    
    # =========================================
    # Instagram Ingestion
    # =========================================
    
    async def ingest_instagram_user(
        self,
        username: str,
        workspace_id: str = "00000000-0000-0000-0000-000000000001",
        count: int = 12
    ) -> List[PostRaw]:
        """Ingest posts from an Instagram user"""
        logger.info(f"📥 Ingesting Instagram posts for @{username}")
        
        # Get user info first
        user_info = await self._rapidapi_get(f"/user-info-v2?username={username}")
        if not user_info:
            logger.warning(f"Could not fetch user info for @{username}")
            return []
        
        user_id = user_info.get("id") or user_info.get("pk")
        if not user_id:
            logger.warning(f"No user ID found for @{username}")
            return []
        
        # Get media
        media_data = await self._rapidapi_get(f"/user-feeds?id={user_id}&count={count}")
        items = media_data.get("items", []) if isinstance(media_data, dict) else []
        
        posts = []
        for item in items:
            post = self._normalize_instagram_post(item, workspace_id)
            if post:
                posts.append(post)
        
        # Save to database
        saved = await self._save_posts(posts)
        logger.success(f"✅ Ingested {saved} posts from @{username}")
        
        return posts
    
    async def ingest_instagram_hashtag(
        self,
        hashtag: str,
        workspace_id: str = "00000000-0000-0000-0000-000000000001",
        count: int = 20
    ) -> List[PostRaw]:
        """Ingest posts from an Instagram hashtag"""
        logger.info(f"📥 Ingesting Instagram posts for #{hashtag}")
        
        # Search hashtag
        data = await self._rapidapi_get(f"/hashtag-feeds?hashtag={hashtag}&count={count}")
        items = data.get("items", []) if isinstance(data, dict) else []
        
        posts = []
        for item in items:
            post = self._normalize_instagram_post(item, workspace_id)
            if post:
                posts.append(post)
        
        saved = await self._save_posts(posts)
        logger.success(f"✅ Ingested {saved} posts from #{hashtag}")
        
        return posts
    
    def _normalize_instagram_post(self, item: Dict, workspace_id: str) -> Optional[PostRaw]:
        """Normalize an Instagram post to our standard format"""
        try:
            # Extract basic info
            post_id = str(item.get("pk") or item.get("id", ""))
            if not post_id:
                return None
            
            # Author info
            user = item.get("user", {})
            author_handle = user.get("username", "")
            author_followers = user.get("follower_count", 0)
            
            # Caption
            caption_data = item.get("caption", {}) or {}
            caption_text = caption_data.get("text", "") if isinstance(caption_data, dict) else str(caption_data)
            
            # Extract hashtags from caption
            hashtags = []
            if caption_text:
                import re
                hashtags = re.findall(r'#(\w+)', caption_text)
            
            # Metrics
            metrics = PostMetrics(
                views=item.get("play_count", 0) or item.get("view_count", 0) or 0,
                likes=item.get("like_count", 0) or 0,
                comments=item.get("comment_count", 0) or 0,
                shares=item.get("reshare_count", 0) or 0,
                saves=item.get("save_count", 0) or 0,
            )
            
            # Audio
            audio_ref = None
            music = item.get("music_info") or item.get("clips_metadata", {}).get("music_info")
            if music:
                audio_ref = AudioRef(
                    sound_id=str(music.get("audio_id", "")),
                    title=music.get("title", ""),
                    creator=music.get("artist_name", ""),
                    is_original=music.get("is_original_audio_on_instagram", False),
                )
            
            # Media type
            media_type = "video" if item.get("media_type") == 2 else "image"
            if item.get("carousel_media"):
                media_type = "carousel"
            
            # Timestamp
            posted_at = None
            taken_at = item.get("taken_at")
            if taken_at:
                posted_at = datetime.fromtimestamp(taken_at)
            
            # Permalink
            code = item.get("code", "")
            permalink = f"https://www.instagram.com/p/{code}/" if code else ""
            
            return PostRaw(
                workspace_id=workspace_id,
                platform=Platform.INSTAGRAM,
                platform_post_id=post_id,
                author_handle=author_handle,
                author_id=str(user.get("pk", "")),
                author_followers=author_followers,
                posted_at=posted_at,
                fetched_at=datetime.now(),
                caption_text=caption_text,
                hashtags=hashtags,
                metrics=metrics,
                audio_ref=audio_ref,
                media_type=media_type,
                permalink=permalink,
                language="en",
                extra={"shortcode": code},
            )
            
        except Exception as e:
            logger.error(f"Failed to normalize Instagram post: {e}")
            return None
    
    # =========================================
    # TikTok Ingestion
    # =========================================
    
    async def ingest_tiktok_user(
        self,
        username: str,
        workspace_id: str = "00000000-0000-0000-0000-000000000001",
        count: int = 12
    ) -> List[PostRaw]:
        """Ingest posts from a TikTok user"""
        logger.info(f"📥 Ingesting TikTok posts for @{username}")
        
        # Get user posts
        data = await self._rapidapi_get(
            f"/user/posts?unique_id={username}&count={count}",
            host=TIKTOK_HOST
        )
        
        videos = data.get("data", {}).get("videos", [])
        if not videos:
            logger.warning(f"No TikTok videos found for @{username}")
            return []
        
        posts = []
        for item in videos:
            post = self._normalize_tiktok_post(item, workspace_id)
            if post:
                posts.append(post)
        
        saved = await self._save_posts(posts)
        logger.success(f"✅ Ingested {saved} TikTok posts from @{username}")
        
        return posts
    
    def _normalize_tiktok_post(self, item: Dict, workspace_id: str) -> Optional[PostRaw]:
        """Normalize a TikTok post to our standard format"""
        try:
            # Handle case where item might be a string (video ID only)
            if isinstance(item, str):
                return None
            
            post_id = str(item.get("video_id") or item.get("aweme_id") or item.get("id", ""))
            if not post_id:
                return None
            
            # Author info - can be dict or nested
            author = item.get("author", {}) or {}
            if isinstance(author, str):
                author = {}
            author_handle = author.get("unique_id", "") or author.get("uniqueId", "") or ""
            author_followers = author.get("follower_count", 0) or author.get("followerCount", 0) or 0
            
            # Caption - tiktok-scraper7 uses "title"
            caption_text = item.get("title", "") or item.get("desc", "") or ""
            
            # Extract hashtags
            hashtags = []
            if caption_text:
                import re
                hashtags = re.findall(r'#(\w+)', caption_text)
            
            # Metrics - tiktok-scraper7 returns them at top level
            metrics = PostMetrics(
                views=item.get("play_count", 0) or item.get("playCount", 0) or 0,
                likes=item.get("digg_count", 0) or item.get("diggCount", 0) or 0,
                comments=item.get("comment_count", 0) or item.get("commentCount", 0) or 0,
                shares=item.get("share_count", 0) or item.get("shareCount", 0) or 0,
                saves=item.get("collect_count", 0) or item.get("collectCount", 0) or 0,
            )
            
            # Audio
            audio_ref = None
            music_info = item.get("music_info", {}) or {}
            if isinstance(music_info, dict) and music_info:
                audio_ref = AudioRef(
                    sound_id=str(music_info.get("id", "")),
                    title=music_info.get("title", ""),
                    creator=music_info.get("author", ""),
                    is_original=music_info.get("original", False),
                )
            
            # Timestamp
            posted_at = None
            create_time = item.get("create_time") or item.get("createTime")
            if create_time:
                try:
                    posted_at = datetime.fromtimestamp(int(create_time))
                except (ValueError, OSError):
                    pass
            
            # Permalink
            permalink = f"https://www.tiktok.com/@{author_handle}/video/{post_id}" if author_handle else ""
            
            return PostRaw(
                workspace_id=workspace_id,
                platform=Platform.TIKTOK,
                platform_post_id=post_id,
                author_handle=author_handle,
                author_id=str(author.get("id", "") if isinstance(author, dict) else ""),
                author_followers=author_followers,
                posted_at=posted_at,
                fetched_at=datetime.now(),
                caption_text=caption_text,
                hashtags=hashtags,
                metrics=metrics,
                audio_ref=audio_ref,
                media_type="video",
                permalink=permalink,
                language="en",
                extra={"source": "tiktok_scraper7", "duration": item.get("duration", 0)},
            )
            
        except Exception as e:
            logger.error(f"Failed to normalize TikTok post: {e}")
            return None
    
    # =========================================
    # Database Operations
    # =========================================
    
    async def _save_posts(self, posts: List[PostRaw]) -> int:
        """Save posts to database with upsert"""
        if not posts:
            return 0
        
        saved = 0
        with self.engine.connect() as conn:
            for post in posts:
                try:
                    conn.execute(text("""
                        INSERT INTO posts_raw (
                            workspace_id, platform, platform_post_id,
                            author_handle, author_id, author_followers,
                            posted_at, fetched_at, caption_text,
                            hashtags, metrics, audio_ref, media_type,
                            permalink, language, extra
                        ) VALUES (
                            :workspace_id, :platform, :platform_post_id,
                            :author_handle, :author_id, :author_followers,
                            :posted_at, :fetched_at, :caption_text,
                            :hashtags, :metrics, :audio_ref, :media_type,
                            :permalink, :language, :extra
                        )
                        ON CONFLICT (platform, platform_post_id)
                        DO UPDATE SET
                            metrics = :metrics,
                            fetched_at = :fetched_at
                    """), {
                        "workspace_id": post.workspace_id,
                        "platform": post.platform.value,
                        "platform_post_id": post.platform_post_id,
                        "author_handle": post.author_handle,
                        "author_id": post.author_id,
                        "author_followers": post.author_followers,
                        "posted_at": post.posted_at,
                        "fetched_at": post.fetched_at,
                        "caption_text": post.caption_text,
                        "hashtags": json.dumps(post.hashtags),
                        "metrics": json.dumps(post.metrics.to_dict()),
                        "audio_ref": json.dumps(post.audio_ref.to_dict()) if post.audio_ref else None,
                        "media_type": post.media_type,
                        "permalink": post.permalink,
                        "language": post.language,
                        "extra": json.dumps(post.extra),
                    })
                    saved += 1
                except Exception as e:
                    logger.error(f"Failed to save post {post.platform_post_id}: {e}")
            
            conn.commit()
        
        return saved
    
    async def get_posts(
        self,
        workspace_id: str = "00000000-0000-0000-0000-000000000001",
        platform: Optional[str] = None,
        since: Optional[datetime] = None,
        limit: int = 100
    ) -> List[Dict]:
        """Get posts from database"""
        with self.engine.connect() as conn:
            query = """
                SELECT * FROM posts_raw
                WHERE workspace_id = :workspace_id
            """
            params = {"workspace_id": workspace_id, "limit": limit}
            
            if platform:
                query += " AND platform = :platform"
                params["platform"] = platform
            
            if since:
                query += " AND posted_at >= :since"
                params["since"] = since
            
            query += " ORDER BY posted_at DESC LIMIT :limit"
            
            result = conn.execute(text(query), params)
            rows = result.fetchall()
            
            return [dict(row._mapping) for row in rows]
    
    # =========================================
    # Batch Ingestion
    # =========================================
    
    async def run_ingest_for_source(self, source: WorkspaceSource) -> int:
        """Run ingestion for a workspace source"""
        total = 0
        
        if source.platform == Platform.INSTAGRAM:
            # Ingest from seed accounts
            for account in source.seed_accounts:
                posts = await self.ingest_instagram_user(
                    account, 
                    source.workspace_id,
                    count=12
                )
                total += len(posts)
                await asyncio.sleep(1)  # Rate limit
            
            # Ingest from seed hashtags
            for hashtag in source.seed_hashtags:
                posts = await self.ingest_instagram_hashtag(
                    hashtag,
                    source.workspace_id,
                    count=20
                )
                total += len(posts)
                await asyncio.sleep(1)
        
        return total


# Singleton
_ingest_service = None

def get_ingest_service() -> IngestService:
    global _ingest_service
    if _ingest_service is None:
        _ingest_service = IngestService()
    return _ingest_service
