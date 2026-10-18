"""
Instagram Scraper Stable API Adapter
Implementation using instagram-scraper-stable-api.p.rapidapi.com (RockSolid APIs)

PRO Plan Limits:
- Rate limit varies by endpoint
- Real-time data (no caching)

Key Endpoints (uses form-urlencoded, .php suffix):
- POST /get_ig_user_reels.php - Get user reels with audio (username_or_url, amount)
- POST /get_ig_user_posts.php - Get user posts (username_or_url, amount)
- POST /get_ig_account_data.php - Get account/profile data (username_or_url)
- GET /get_ig_reel_data.php - Detailed reel data with play_count (code_or_id_or_url)
- POST /search_ig.php - Search users and hashtags (query)
"""
import os
import httpx
import asyncio
from typing import Optional, List, Dict, Any
from datetime import datetime
from dataclasses import dataclass
from loguru import logger

from .base import (
    InstagramAdapter,
    Profile,
    MediaItem,
    MediaPage,
    MediaType,
    HashtagData,
    SearchResults,
    AudioInfo
)


@dataclass
class ReelData:
    """Detailed reel data with audio info"""
    id: str
    shortcode: str
    caption: str
    video_url: str
    thumbnail_url: str
    audio_url: Optional[str]
    audio_title: Optional[str]
    audio_artist: Optional[str]
    play_count: int
    like_count: int
    comment_count: int
    duration_seconds: float
    timestamp: datetime


class InstagramStableAdapter(InstagramAdapter):
    """
    Instagram data adapter using Instagram Scraper Stable API.
    
    API Provider: RockSolid APIs
    Host: instagram-scraper-api2.p.rapidapi.com
    
    Features:
    - User reels with audio URLs
    - Detailed media data with play_count
    - User profile, posts, followers
    - Real-time data
    """
    
    def __init__(
        self,
        api_key: Optional[str] = None,
        host: str = "instagram-scraper-stable-api.p.rapidapi.com"
    ):
        super().__init__(api_key or os.getenv("RAPIDAPI_KEY"))
        self.name = "instagram-scraper-stable"
        self.type = "scraper"
        self.host = host
        self.base_url = f"https://{host}"
        self.timeout = 30.0
        
        # Rate limit tracking
        self._request_count = 0
        self._last_request_time = None
        self._rate_limit_remaining = None
        
        if not self.api_key:
            logger.warning("RAPIDAPI_KEY not configured - adapter will fail")
    
    def _get_headers(self, use_form: bool = False) -> Dict[str, str]:
        """Get required RapidAPI headers"""
        content_type = "application/x-www-form-urlencoded" if use_form else "application/json"
        return {
            "X-RapidAPI-Key": self.api_key,
            "X-RapidAPI-Host": self.host,
            "Content-Type": content_type
        }
    
    def _update_rate_limits(self, response: httpx.Response):
        """Track rate limits from response headers"""
        self._rate_limit_remaining = response.headers.get("X-RateLimit-Remaining")
        if self._rate_limit_remaining:
            logger.debug(f"Rate limit remaining: {self._rate_limit_remaining}")
    
    async def get_profile(self, username: str) -> Optional[Profile]:
        """
        Fetch Instagram profile information.
        
        Endpoint: POST /get_ig_account_data.php
        """
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            try:
                response = await client.post(
                    f"{self.base_url}/get_ig_account_data.php",
                    headers=self._get_headers(use_form=True),
                    data={"username_or_url": username}
                )
                response.raise_for_status()
                self._update_rate_limits(response)
                
                data = response.json()
                user_data = data.get("user", data)
                
                return Profile(
                    id=str(user_data.get("id", user_data.get("pk", ""))),
                    username=user_data.get("username", username),
                    full_name=user_data.get("full_name", ""),
                    bio=user_data.get("biography", ""),
                    followers_count=user_data.get("follower_count", 0),
                    following_count=user_data.get("following_count", 0),
                    media_count=user_data.get("media_count", 0),
                    is_verified=user_data.get("is_verified", False),
                    profile_pic_url=user_data.get("profile_pic_url_hd", user_data.get("profile_pic_url", "")),
                    provider=self.name,
                    external_url=user_data.get("external_url"),
                    is_business=user_data.get("is_business_account", False),
                    category=user_data.get("category_name")
                )
                
            except httpx.HTTPStatusError as e:
                logger.error(f"HTTP error getting profile {username}: {e}")
                return None
            except Exception as e:
                logger.error(f"Error getting profile {username}: {e}")
                return None
    
    async def get_user_reels(
        self, 
        username: str, 
        count: int = 12,
        pagination_token: Optional[str] = None
    ) -> List[ReelData]:
        """
        Fetch user reels with audio information.
        
        Endpoint: POST /get_ig_user_reels.php
        Params: username_or_url, amount
        
        Returns reels with video URLs and audio metadata.
        """
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            try:
                form_data = {
                    "username_or_url": username,
                    "amount": str(count)
                }
                
                response = await client.post(
                    f"{self.base_url}/get_ig_user_reels.php",
                    headers=self._get_headers(use_form=True),
                    data=form_data
                )
                response.raise_for_status()
                self._update_rate_limits(response)
                
                data = response.json()
                # API returns {"reels": [{"node": {"media": {...}}}]}
                items = data.get("reels", [])
                
                reels = []
                for item in items:
                    # Extract media from nested structure
                    media = item.get("node", {}).get("media", item.get("media", item))
                    reel = self._parse_reel_item(media)
                    if reel:
                        reels.append(reel)
                
                logger.info(f"Fetched {len(reels)} reels for @{username}")
                return reels
                
            except httpx.HTTPStatusError as e:
                logger.error(f"HTTP error getting reels for {username}: {e}")
                return []
            except Exception as e:
                logger.error(f"Error getting reels for {username}: {e}")
                return []
    
    async def get_reel_by_shortcode(self, shortcode: str) -> Optional[ReelData]:
        """
        Get detailed reel data by shortcode/code.
        
        Endpoint: GET /v1/reel_by_shortcode
        
        Returns full reel data including video URL and audio.
        """
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            try:
                response = await client.get(
                    f"{self.base_url}/v1/reel_by_shortcode",
                    headers=self._get_headers(),
                    params={"shortcode": shortcode}
                )
                response.raise_for_status()
                self._update_rate_limits(response)
                
                data = response.json().get("data", {})
                return self._parse_reel_item(data)
                
            except httpx.HTTPStatusError as e:
                logger.error(f"HTTP error getting reel {shortcode}: {e}")
                return None
            except Exception as e:
                logger.error(f"Error getting reel {shortcode}: {e}")
                return None
    
    async def get_media_by_shortcode(self, shortcode: str) -> Optional[Dict[str, Any]]:
        """
        Get detailed media data (post or reel) by shortcode.
        
        Endpoint: GET /v1/media_by_shortcode
        
        Returns full media data with play_count for videos.
        """
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            try:
                response = await client.get(
                    f"{self.base_url}/v1/media_by_shortcode",
                    headers=self._get_headers(),
                    params={"shortcode": shortcode}
                )
                response.raise_for_status()
                self._update_rate_limits(response)
                
                return response.json().get("data", {})
                
            except httpx.HTTPStatusError as e:
                logger.error(f"HTTP error getting media {shortcode}: {e}")
                return None
            except Exception as e:
                logger.error(f"Error getting media {shortcode}: {e}")
                return None
    
    async def get_user_posts(
        self, 
        username: str, 
        count: int = 12,
        pagination_token: Optional[str] = None
    ) -> MediaPage:
        """
        Fetch user posts.
        
        Endpoint: POST /v1/posts
        """
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            try:
                payload = {
                    "username_or_id_or_url": username,
                    "count": count
                }
                if pagination_token:
                    payload["pagination_token"] = pagination_token
                
                response = await client.post(
                    f"{self.base_url}/v1/posts",
                    headers=self._get_headers(),
                    json=payload
                )
                response.raise_for_status()
                self._update_rate_limits(response)
                
                data = response.json()
                items_data = data.get("data", {}).get("items", [])
                next_cursor = data.get("pagination_token")
                
                items = []
                for item in items_data:
                    media_item = self._parse_media_item(item)
                    if media_item:
                        items.append(media_item)
                
                return MediaPage(
                    items=items,
                    cursor=next_cursor,
                    has_more=bool(next_cursor)
                )
                
            except Exception as e:
                logger.error(f"Error getting posts for {username}: {e}")
                return MediaPage(items=[], cursor=None, has_more=False)
    
    async def search(self, query: str) -> SearchResults:
        """
        Search Instagram for users and hashtags.
        
        Endpoint: POST /v1/search
        """
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            try:
                response = await client.post(
                    f"{self.base_url}/v1/search",
                    headers=self._get_headers(),
                    json={"query": query}
                )
                response.raise_for_status()
                self._update_rate_limits(response)
                
                data = response.json().get("data", {})
                
                accounts = []
                for user in data.get("users", []):
                    accounts.append(Profile(
                        id=str(user.get("pk", "")),
                        username=user.get("username", ""),
                        full_name=user.get("full_name", ""),
                        bio="",
                        followers_count=user.get("follower_count", 0),
                        following_count=0,
                        media_count=0,
                        is_verified=user.get("is_verified", False),
                        profile_pic_url=user.get("profile_pic_url", ""),
                        provider=self.name
                    ))
                
                hashtags = [h.get("name", "") for h in data.get("hashtags", [])]
                
                return SearchResults(
                    accounts=accounts,
                    hashtags=hashtags
                )
                
            except Exception as e:
                logger.error(f"Error searching for {query}: {e}")
                return SearchResults(accounts=[], hashtags=[])
    
    def _parse_reel_item(self, item: Dict[str, Any]) -> Optional[ReelData]:
        """Parse a reel item from API response"""
        try:
            # Get video URL from video_versions
            video_url = ""
            video_versions = item.get("video_versions", [])
            if video_versions:
                video_url = video_versions[0].get("url", "")
            
            # Get audio info from clips_metadata
            audio_url = None
            audio_title = None
            audio_artist = None
            
            clips_metadata = item.get("clips_metadata", {})
            music_info = clips_metadata.get("music_info", {})
            if music_info:
                music_asset = music_info.get("music_asset_info", {})
                audio_url = music_asset.get("progressive_download_url")
                audio_title = music_asset.get("title")
                audio_artist = music_asset.get("display_artist")
            
            # Fallback to original sound
            if not audio_url:
                original_sound = clips_metadata.get("original_sound_info", {})
                if original_sound:
                    audio_asset = original_sound.get("audio_asset_info", {})
                    audio_url = audio_asset.get("progressive_download_url")
                    audio_title = original_sound.get("original_audio_title", "Original Sound")
                    ig_artist = original_sound.get("ig_artist", {})
                    audio_artist = ig_artist.get("username", "Unknown")
            
            # Get caption
            caption = ""
            caption_data = item.get("caption")
            if caption_data:
                caption = caption_data.get("text", "") if isinstance(caption_data, dict) else str(caption_data)
            
            # Get timestamp
            timestamp = datetime.now()
            taken_at = item.get("taken_at")
            if taken_at:
                try:
                    timestamp = datetime.fromtimestamp(taken_at)
                except:
                    pass
            
            return ReelData(
                id=str(item.get("id", item.get("pk", ""))),
                shortcode=item.get("code", ""),
                caption=caption[:200] if caption else "",
                video_url=video_url,
                thumbnail_url=item.get("image_versions2", {}).get("candidates", [{}])[0].get("url", ""),
                audio_url=audio_url,
                audio_title=audio_title,
                audio_artist=audio_artist,
                play_count=item.get("play_count", 0),
                like_count=item.get("like_count", 0),
                comment_count=item.get("comment_count", 0),
                duration_seconds=item.get("video_duration", 0),
                timestamp=timestamp
            )
            
        except Exception as e:
            logger.error(f"Error parsing reel item: {e}")
            return None
    
    def _parse_media_item(self, item: Dict[str, Any]) -> Optional[MediaItem]:
        """Parse a media item from API response"""
        try:
            media_type = MediaType.IMAGE
            if item.get("media_type") == 2 or item.get("is_video"):
                media_type = MediaType.VIDEO
            elif item.get("media_type") == 8:
                media_type = MediaType.CAROUSEL
            
            # Get caption
            caption = ""
            caption_data = item.get("caption")
            if caption_data:
                caption = caption_data.get("text", "") if isinstance(caption_data, dict) else str(caption_data)
            
            # Get thumbnail
            thumbnail_url = ""
            image_versions = item.get("image_versions2", {})
            candidates = image_versions.get("candidates", [])
            if candidates:
                thumbnail_url = candidates[0].get("url", "")
            
            # Get video URL if video
            video_url = None
            if media_type == MediaType.VIDEO:
                video_versions = item.get("video_versions", [])
                if video_versions:
                    video_url = video_versions[0].get("url")
            
            # Get audio info
            audio = None
            clips_metadata = item.get("clips_metadata", {})
            music_info = clips_metadata.get("music_info", {})
            if music_info:
                music_asset = music_info.get("music_asset_info", {})
                audio = AudioInfo(
                    id=str(music_asset.get("audio_id", "")),
                    title=music_asset.get("title", "Unknown"),
                    artist=music_asset.get("display_artist", "Unknown"),
                    duration_ms=music_asset.get("duration_in_ms")
                )
            
            # Get timestamp
            timestamp = datetime.now()
            taken_at = item.get("taken_at")
            if taken_at:
                try:
                    timestamp = datetime.fromtimestamp(taken_at)
                except:
                    pass
            
            return MediaItem(
                id=str(item.get("id", item.get("pk", ""))),
                media_type=media_type,
                caption=caption,
                permalink=f"https://www.instagram.com/p/{item.get('code', '')}/",
                thumbnail_url=thumbnail_url,
                like_count=item.get("like_count", 0),
                comment_count=item.get("comment_count", 0),
                timestamp=timestamp,
                play_count=item.get("play_count"),
                video_url=video_url,
                audio=audio
            )
            
        except Exception as e:
            logger.error(f"Error parsing media item: {e}")
            return None
    
    def get_rate_limit_status(self) -> Dict[str, Any]:
        """Get current rate limit status"""
        return {
            "requests_made": self._request_count,
            "rate_limit_remaining": self._rate_limit_remaining,
            "last_request": self._last_request_time.isoformat() if self._last_request_time else None
        }


# Singleton instance
_stable_adapter: Optional[InstagramStableAdapter] = None


def get_instagram_stable_adapter() -> InstagramStableAdapter:
    """Get singleton adapter instance"""
    global _stable_adapter
    if _stable_adapter is None:
        _stable_adapter = InstagramStableAdapter()
    return _stable_adapter
