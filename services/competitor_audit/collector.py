"""
Competitor Collector Service
============================
Fetches profile and posts from various platforms via RapidAPI.
Handles rate limiting and stores raw data for processing.
"""
import os
import asyncio
from datetime import datetime, timedelta
from typing import Dict, Any, List, Optional
from dataclasses import dataclass, field, asdict
from loguru import logger
import aiohttp

from sqlalchemy import create_engine, text


@dataclass
class CompetitorProfile:
    """Collected competitor profile data"""
    platform: str
    handle: str
    profile_url: Optional[str] = None
    platform_user_id: Optional[str] = None
    display_name: Optional[str] = None
    bio_text: Optional[str] = None
    category: Optional[str] = None
    linkout_urls: List[str] = field(default_factory=list)
    avatar_url: Optional[str] = None
    banner_url: Optional[str] = None
    pinned_post_ids: List[str] = field(default_factory=list)
    follower_count: int = 0
    following_count: int = 0
    post_count: int = 0
    platform_raw_profile: Optional[Dict] = None
    fetched_at: str = field(default_factory=lambda: datetime.utcnow().isoformat())


@dataclass
class CompetitorPost:
    """Collected competitor post data"""
    platform: str
    platform_post_id: str
    permalink: Optional[str] = None
    posted_at: Optional[str] = None
    caption_text: Optional[str] = None
    hashtags: List[str] = field(default_factory=list)
    mentions: List[str] = field(default_factory=list)
    media_type: Optional[str] = None
    media_urls: List[str] = field(default_factory=list)
    thumbnail_url: Optional[str] = None
    duration_sec: Optional[float] = None
    views: int = 0
    likes: int = 0
    comments: int = 0
    shares: int = 0
    saves: int = 0
    audio_id: Optional[str] = None
    audio_title: Optional[str] = None
    audio_artist: Optional[str] = None
    is_original_audio: bool = False
    is_pinned: bool = False
    platform_raw_post: Optional[Dict] = None
    fetched_at: str = field(default_factory=lambda: datetime.utcnow().isoformat())


class CompetitorCollector:
    """
    Collects competitor profile and post data from various platforms.
    Uses RapidAPI for data fetching with rate limiting.
    """
    
    # Platform API configurations
    PLATFORM_CONFIGS = {
        "instagram": {
            "host": "instagram-looter2.p.rapidapi.com",
            "profile_endpoint": "/v1/info",
            "posts_endpoint": "/v1/posts",
            "profile_param": "username",
            "posts_param": "username"
        },
        "tiktok": {
            "host": "tiktok-scraper7.p.rapidapi.com",
            "profile_endpoint": "/user/info",
            "posts_endpoint": "/user/posts",
            "profile_param": "unique_id",
            "posts_param": "unique_id"
        },
        "youtube": {
            "host": "youtube-v31.p.rapidapi.com",
            "profile_endpoint": "/channels",
            "posts_endpoint": "/search",
            "profile_param": "forUsername",
            "posts_param": "channelId"
        }
    }
    
    def __init__(
        self,
        db_url: Optional[str] = None,
        rapidapi_key: Optional[str] = None
    ):
        self.db_url = db_url or os.getenv("DATABASE_URL", "postgresql://postgres:postgres@127.0.0.1:54322/postgres")
        self.engine = create_engine(self.db_url)
        self.rapidapi_key = rapidapi_key or os.getenv("RAPIDAPI_KEY")
        
        if not self.rapidapi_key:
            logger.warning("RAPIDAPI_KEY not set - collector will not work")
    
    async def collect_profile(
        self,
        platform: str,
        handle: str
    ) -> Optional[CompetitorProfile]:
        """
        Collect competitor profile data.
        
        Args:
            platform: Platform name (instagram, tiktok, youtube)
            handle: Username/handle without @
        """
        if not self.rapidapi_key:
            logger.error("RAPIDAPI_KEY not configured")
            return None
        
        config = self.PLATFORM_CONFIGS.get(platform)
        if not config:
            logger.error(f"Unsupported platform: {platform}")
            return None
        
        handle = handle.lstrip("@").strip()
        
        try:
            headers = {
                "X-RapidAPI-Key": self.rapidapi_key,
                "X-RapidAPI-Host": config["host"]
            }
            
            url = f"https://{config['host']}{config['profile_endpoint']}"
            params = {config["profile_param"]: handle}
            
            # YouTube needs additional params
            if platform == "youtube":
                params["part"] = "snippet,statistics,brandingSettings"
            
            async with aiohttp.ClientSession() as session:
                async with session.get(url, headers=headers, params=params, timeout=30) as response:
                    if response.status != 200:
                        logger.error(f"Profile fetch failed: {response.status}")
                        return None
                    
                    data = await response.json()
                    return self._parse_profile(platform, handle, data)
                    
        except Exception as e:
            logger.error(f"Profile collection failed: {e}")
            return None
    
    def _parse_profile(
        self,
        platform: str,
        handle: str,
        data: Dict[str, Any]
    ) -> CompetitorProfile:
        """Parse platform-specific profile response"""
        
        if platform == "instagram":
            user = data.get("user", data)
            bio = user.get("biography", "")
            
            # Extract links from bio
            linkout_urls = []
            external_url = user.get("external_url")
            if external_url:
                linkout_urls.append(external_url)
            
            return CompetitorProfile(
                platform=platform,
                handle=handle,
                profile_url=f"https://instagram.com/{handle}",
                platform_user_id=str(user.get("pk") or user.get("id")),
                display_name=user.get("full_name"),
                bio_text=bio,
                category=user.get("category"),
                linkout_urls=linkout_urls,
                avatar_url=user.get("profile_pic_url_hd") or user.get("profile_pic_url"),
                follower_count=user.get("follower_count", 0),
                following_count=user.get("following_count", 0),
                post_count=user.get("media_count", 0),
                platform_raw_profile=data
            )
        
        elif platform == "tiktok":
            user_info = data.get("userInfo", data)
            user = user_info.get("user", {})
            stats = user_info.get("stats", {})
            
            return CompetitorProfile(
                platform=platform,
                handle=handle,
                profile_url=f"https://tiktok.com/@{handle}",
                platform_user_id=user.get("id"),
                display_name=user.get("nickname"),
                bio_text=user.get("signature"),
                linkout_urls=[user.get("bioLink", {}).get("link")] if user.get("bioLink") else [],
                avatar_url=user.get("avatarLarger") or user.get("avatarMedium"),
                follower_count=stats.get("followerCount", 0),
                following_count=stats.get("followingCount", 0),
                post_count=stats.get("videoCount", 0),
                platform_raw_profile=data
            )
        
        elif platform == "youtube":
            items = data.get("items", [])
            if not items:
                return CompetitorProfile(platform=platform, handle=handle)
            
            channel = items[0]
            snippet = channel.get("snippet", {})
            stats = channel.get("statistics", {})
            branding = channel.get("brandingSettings", {}).get("channel", {})
            
            return CompetitorProfile(
                platform=platform,
                handle=handle,
                profile_url=f"https://youtube.com/@{handle}",
                platform_user_id=channel.get("id"),
                display_name=snippet.get("title"),
                bio_text=snippet.get("description"),
                category=branding.get("keywords"),
                linkout_urls=[branding.get("unsubscribedTrailer")] if branding.get("unsubscribedTrailer") else [],
                avatar_url=snippet.get("thumbnails", {}).get("high", {}).get("url"),
                banner_url=channel.get("brandingSettings", {}).get("image", {}).get("bannerExternalUrl"),
                follower_count=int(stats.get("subscriberCount", 0)),
                post_count=int(stats.get("videoCount", 0)),
                platform_raw_profile=data
            )
        
        return CompetitorProfile(platform=platform, handle=handle, platform_raw_profile=data)
    
    async def collect_posts(
        self,
        platform: str,
        handle: str,
        limit: int = 20,
        platform_user_id: Optional[str] = None
    ) -> List[CompetitorPost]:
        """
        Collect recent posts from competitor.
        
        Args:
            platform: Platform name
            handle: Username/handle
            limit: Max posts to fetch
            platform_user_id: Platform's internal user ID (for some APIs)
        """
        if not self.rapidapi_key:
            logger.error("RAPIDAPI_KEY not configured")
            return []
        
        config = self.PLATFORM_CONFIGS.get(platform)
        if not config:
            logger.error(f"Unsupported platform: {platform}")
            return []
        
        handle = handle.lstrip("@").strip()
        
        try:
            headers = {
                "X-RapidAPI-Key": self.rapidapi_key,
                "X-RapidAPI-Host": config["host"]
            }
            
            url = f"https://{config['host']}{config['posts_endpoint']}"
            
            # Build params based on platform
            if platform == "youtube":
                params = {
                    "channelId": platform_user_id or handle,
                    "part": "snippet",
                    "order": "date",
                    "maxResults": str(min(limit, 50))
                }
            else:
                params = {
                    config["posts_param"]: handle,
                    "count": str(limit) if platform == "tiktok" else None
                }
                params = {k: v for k, v in params.items() if v}
            
            async with aiohttp.ClientSession() as session:
                async with session.get(url, headers=headers, params=params, timeout=30) as response:
                    if response.status != 200:
                        logger.error(f"Posts fetch failed: {response.status}")
                        return []
                    
                    data = await response.json()
                    return self._parse_posts(platform, handle, data, limit)
                    
        except Exception as e:
            logger.error(f"Posts collection failed: {e}")
            return []
    
    def _parse_posts(
        self,
        platform: str,
        handle: str,
        data: Any,
        limit: int
    ) -> List[CompetitorPost]:
        """Parse platform-specific posts response"""
        posts = []
        
        if platform == "instagram":
            items = data if isinstance(data, list) else data.get("posts", data.get("items", []))
            
            for item in items[:limit]:
                # Extract hashtags from caption
                caption = ""
                if isinstance(item.get("caption"), dict):
                    caption = item["caption"].get("text", "")
                elif isinstance(item.get("caption"), str):
                    caption = item["caption"]
                
                hashtags = [tag for tag in caption.split() if tag.startswith("#")]
                mentions = [m for m in caption.split() if m.startswith("@")]
                
                post = CompetitorPost(
                    platform=platform,
                    platform_post_id=str(item.get("pk") or item.get("id")),
                    permalink=f"https://instagram.com/p/{item.get('shortcode', '')}",
                    posted_at=datetime.fromtimestamp(item.get("taken_at", 0)).isoformat() if item.get("taken_at") else None,
                    caption_text=caption,
                    hashtags=hashtags,
                    mentions=mentions,
                    media_type="video" if item.get("is_video") else "image",
                    thumbnail_url=item.get("thumbnail_url") or item.get("display_url"),
                    duration_sec=item.get("video_duration"),
                    views=item.get("play_count", item.get("view_count", 0)),
                    likes=item.get("like_count", 0),
                    comments=item.get("comment_count", 0),
                    is_pinned=item.get("is_pinned", False),
                    platform_raw_post=item
                )
                
                # Extract audio info
                music = item.get("music_info") or item.get("clips_music_attribution_info") or {}
                if music:
                    post.audio_id = str(music.get("audio_id", ""))
                    post.audio_title = music.get("title") or music.get("song_name")
                    post.audio_artist = music.get("artist_name")
                    post.is_original_audio = music.get("is_original_sound", False)
                
                posts.append(post)
        
        elif platform == "tiktok":
            items = data.get("videos", data.get("itemList", data.get("data", [])))
            
            for item in items[:limit]:
                desc = item.get("desc", "")
                hashtags = [tag.get("hashtagName", f"#{tag.get('name', '')}") 
                           for tag in item.get("textExtra", []) if tag.get("hashtagName") or tag.get("name")]
                
                stats = item.get("stats", {})
                
                post = CompetitorPost(
                    platform=platform,
                    platform_post_id=str(item.get("id")),
                    permalink=f"https://tiktok.com/@{handle}/video/{item.get('id')}",
                    posted_at=datetime.fromtimestamp(item.get("createTime", 0)).isoformat() if item.get("createTime") else None,
                    caption_text=desc,
                    hashtags=hashtags,
                    media_type="video",
                    thumbnail_url=item.get("cover") or item.get("originCover"),
                    duration_sec=item.get("duration"),
                    views=stats.get("playCount", item.get("play_count", 0)),
                    likes=stats.get("diggCount", item.get("digg_count", 0)),
                    comments=stats.get("commentCount", item.get("comment_count", 0)),
                    shares=stats.get("shareCount", item.get("share_count", 0)),
                    platform_raw_post=item
                )
                
                # Audio info
                music = item.get("music", {})
                if music:
                    post.audio_id = str(music.get("id", ""))
                    post.audio_title = music.get("title")
                    post.audio_artist = music.get("authorName")
                    post.is_original_audio = music.get("original", False)
                
                posts.append(post)
        
        elif platform == "youtube":
            items = data.get("items", [])
            
            for item in items[:limit]:
                snippet = item.get("snippet", {})
                video_id = item.get("id", {}).get("videoId") or item.get("id")
                
                post = CompetitorPost(
                    platform=platform,
                    platform_post_id=str(video_id),
                    permalink=f"https://youtube.com/watch?v={video_id}",
                    posted_at=snippet.get("publishedAt"),
                    caption_text=snippet.get("title"),
                    media_type="video",
                    thumbnail_url=snippet.get("thumbnails", {}).get("high", {}).get("url"),
                    platform_raw_post=item
                )
                posts.append(post)
        
        return posts
    
    async def save_profile(self, profile: CompetitorProfile) -> str:
        """Save profile to database, return account_id"""
        try:
            with self.engine.connect() as conn:
                result = conn.execute(text("""
                    INSERT INTO competitor_account (
                        platform, handle, profile_url, platform_user_id,
                        display_name, bio_text, category, linkout_urls,
                        avatar_url, banner_url, pinned_post_ids,
                        follower_count, following_count, post_count,
                        platform_raw_profile, fetched_at
                    ) VALUES (
                        :platform, :handle, :profile_url, :platform_user_id,
                        :display_name, :bio_text, :category, :linkout_urls,
                        :avatar_url, :banner_url, :pinned_post_ids,
                        :follower_count, :following_count, :post_count,
                        :platform_raw_profile, :fetched_at
                    )
                    ON CONFLICT (platform, handle) DO UPDATE SET
                        profile_url = :profile_url,
                        platform_user_id = :platform_user_id,
                        display_name = :display_name,
                        bio_text = :bio_text,
                        category = :category,
                        linkout_urls = :linkout_urls,
                        avatar_url = :avatar_url,
                        follower_count = :follower_count,
                        following_count = :following_count,
                        post_count = :post_count,
                        platform_raw_profile = :platform_raw_profile,
                        fetched_at = :fetched_at,
                        updated_at = NOW()
                    RETURNING account_id
                """), {
                    "platform": profile.platform,
                    "handle": profile.handle,
                    "profile_url": profile.profile_url,
                    "platform_user_id": profile.platform_user_id,
                    "display_name": profile.display_name,
                    "bio_text": profile.bio_text,
                    "category": profile.category,
                    "linkout_urls": profile.linkout_urls,
                    "avatar_url": profile.avatar_url,
                    "banner_url": profile.banner_url,
                    "pinned_post_ids": profile.pinned_post_ids,
                    "follower_count": profile.follower_count,
                    "following_count": profile.following_count,
                    "post_count": profile.post_count,
                    "platform_raw_profile": profile.platform_raw_profile,
                    "fetched_at": profile.fetched_at
                })
                conn.commit()
                row = result.fetchone()
                return str(row[0])
        except Exception as e:
            logger.error(f"Failed to save profile: {e}")
            raise
    
    async def save_posts(self, account_id: str, posts: List[CompetitorPost]) -> int:
        """Save posts to database, return count saved"""
        saved = 0
        try:
            with self.engine.connect() as conn:
                for post in posts:
                    conn.execute(text("""
                        INSERT INTO competitor_post (
                            account_id, platform, platform_post_id, permalink,
                            posted_at, caption_text, hashtags, mentions,
                            media_type, media_urls, thumbnail_url, duration_sec,
                            views, likes, comments, shares, saves,
                            audio_id, audio_title, audio_artist, is_original_audio,
                            is_pinned, platform_raw_post, fetched_at
                        ) VALUES (
                            :account_id, :platform, :platform_post_id, :permalink,
                            :posted_at, :caption_text, :hashtags, :mentions,
                            :media_type, :media_urls, :thumbnail_url, :duration_sec,
                            :views, :likes, :comments, :shares, :saves,
                            :audio_id, :audio_title, :audio_artist, :is_original_audio,
                            :is_pinned, :platform_raw_post, :fetched_at
                        )
                        ON CONFLICT (platform, platform_post_id) DO UPDATE SET
                            views = :views,
                            likes = :likes,
                            comments = :comments,
                            shares = :shares,
                            saves = :saves,
                            fetched_at = :fetched_at
                    """), {
                        "account_id": account_id,
                        "platform": post.platform,
                        "platform_post_id": post.platform_post_id,
                        "permalink": post.permalink,
                        "posted_at": post.posted_at,
                        "caption_text": post.caption_text,
                        "hashtags": post.hashtags,
                        "mentions": post.mentions,
                        "media_type": post.media_type,
                        "media_urls": post.media_urls,
                        "thumbnail_url": post.thumbnail_url,
                        "duration_sec": post.duration_sec,
                        "views": post.views,
                        "likes": post.likes,
                        "comments": post.comments,
                        "shares": post.shares,
                        "saves": post.saves,
                        "audio_id": post.audio_id,
                        "audio_title": post.audio_title,
                        "audio_artist": post.audio_artist,
                        "is_original_audio": post.is_original_audio,
                        "is_pinned": post.is_pinned,
                        "platform_raw_post": post.platform_raw_post,
                        "fetched_at": post.fetched_at
                    })
                    saved += 1
                conn.commit()
        except Exception as e:
            logger.error(f"Failed to save posts: {e}")
        
        return saved
    
    async def full_collect(
        self,
        platform: str,
        handle: str,
        post_limit: int = 20
    ) -> Dict[str, Any]:
        """
        Full collection: profile + posts.
        
        Returns:
            {account_id, profile, posts_count, posts}
        """
        logger.info(f"Starting full collection for @{handle} on {platform}")
        
        # Collect profile
        profile = await self.collect_profile(platform, handle)
        if not profile:
            return {"success": False, "error": "Failed to fetch profile"}
        
        # Save profile
        account_id = await self.save_profile(profile)
        logger.info(f"Saved profile: {account_id}")
        
        # Collect posts
        posts = await self.collect_posts(
            platform, handle, 
            limit=post_limit,
            platform_user_id=profile.platform_user_id
        )
        
        # Save posts
        saved_count = await self.save_posts(account_id, posts)
        logger.info(f"Saved {saved_count} posts")
        
        return {
            "success": True,
            "account_id": account_id,
            "profile": asdict(profile),
            "posts_count": saved_count,
            "posts": [asdict(p) for p in posts]
        }
