"""
RapidAPI Instagram Adapter
Implementation using instagram-looter2.p.rapidapi.com
"""
import os
import httpx
from typing import Optional, List, Dict, Any
from datetime import datetime
from loguru import logger

from .base import (
    InstagramAdapter,
    Profile,
    MediaItem,
    MediaPage,
    MediaType,
    HashtagData,
    SearchResults,
    SearchType,
    AudioInfo
)


class RapidApiInstagramAdapter(InstagramAdapter):
    """
    Instagram data adapter using RapidAPI's instagram-looter2 provider.
    
    API Documentation:
    - Base URL: https://instagram-looter2.p.rapidapi.com
    - Auth: X-RapidAPI-Key and X-RapidAPI-Host headers
    - Flexible identifier: username_or_id_or_url parameter
    """
    
    def __init__(
        self,
        api_key: Optional[str] = None,
        host: str = "instagram-looter2.p.rapidapi.com"
    ):
        super().__init__(api_key or os.getenv("RAPIDAPI_KEY"))
        self.name = "rapidapi-instagram-looter2"
        self.type = "scraper"
        self.host = host
        self.base_url = f"https://{host}"
        self.timeout = 30.0
        
        if not self.api_key:
            logger.warning("RapidAPI key not configured - adapter will fail")
    
    def _get_headers(self) -> Dict[str, str]:
        """Get required RapidAPI headers"""
        return {
            "X-RapidAPI-Key": self.api_key,
            "X-RapidAPI-Host": self.host
        }
    
    async def get_profile(self, identifier: str) -> Profile:
        """
        Fetch Instagram profile information.
        
        Endpoint: GET /v1/info
        Params: username_or_id_or_url
        """
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            try:
                response = await client.get(
                    f"{self.base_url}/v1/info",
                    params={"username_or_id_or_url": identifier},
                    headers=self._get_headers()
                )
                response.raise_for_status()
                data = response.json()
                
                # Parse response
                profile_data = data.get("data", {})
                
                return Profile(
                    id=str(profile_data.get("id", profile_data.get("pk", ""))),
                    username=profile_data.get("username", ""),
                    full_name=profile_data.get("full_name", ""),
                    bio=profile_data.get("biography", ""),
                    followers_count=profile_data.get("follower_count", 0),
                    following_count=profile_data.get("following_count", 0),
                    media_count=profile_data.get("media_count", 0),
                    is_verified=profile_data.get("is_verified", False),
                    profile_pic_url=profile_data.get("profile_pic_url", ""),
                    provider=self.name,
                    external_url=profile_data.get("external_url"),
                    is_business=profile_data.get("is_business"),
                    category=profile_data.get("category")
                )
                
            except httpx.HTTPStatusError as e:
                logger.error(f"RapidAPI HTTP error fetching profile {identifier}: {e}")
                raise
            except Exception as e:
                logger.error(f"Error fetching profile {identifier}: {e}")
                raise
    
    async def get_media(
        self,
        identifier: str,
        cursor: Optional[str] = None,
        limit: int = 50
    ) -> MediaPage:
        """
        Fetch media posts from a profile.
        
        Endpoint: GET /v1/posts
        Params: username_or_id_or_url, cursor (optional)
        """
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            try:
                params = {
                    "username_or_id_or_url": identifier,
                    "limit": limit
                }
                if cursor:
                    params["cursor"] = cursor
                
                response = await client.get(
                    f"{self.base_url}/v1/posts",
                    params=params,
                    headers=self._get_headers()
                )
                response.raise_for_status()
                data = response.json()
                
                return self._parse_media_page(data)
                
            except httpx.HTTPStatusError as e:
                logger.error(f"RapidAPI HTTP error fetching media {identifier}: {e}")
                raise
            except Exception as e:
                logger.error(f"Error fetching media {identifier}: {e}")
                raise
    
    async def get_reels(
        self,
        identifier: str,
        cursor: Optional[str] = None,
        limit: int = 50
    ) -> MediaPage:
        """
        Fetch reels from a profile by getting posts and filtering for videos.
        
        NOTE: instagram-looter2 API does NOT have a /v1/reels endpoint.
        We use /v1/posts and filter for video content instead.
        
        Endpoint: GET /v1/posts
        Params: username_or_id_or_url, cursor (optional)
        """
        logger.info(f"[RapidAPI] Fetching reels via /v1/posts for {identifier}")
        
        # Use get_media (which uses /v1/posts) and filter for video content
        try:
            media_page = await self.get_media(identifier, cursor, limit)
            
            # Filter for video/reel content only
            reels = [
                item for item in media_page.items 
                if item.media_type in (MediaType.VIDEO, MediaType.REEL)
            ]
            
            logger.info(f"[RapidAPI] Found {len(reels)} videos/reels out of {len(media_page.items)} posts for {identifier}")
            
            return MediaPage(
                items=reels,
                next_cursor=media_page.next_cursor,
                has_more=media_page.has_more
            )
            
        except httpx.HTTPStatusError as e:
            logger.error(f"RapidAPI HTTP error fetching posts for reels {identifier}: {e}")
            raise
        except Exception as e:
            logger.error(f"Error fetching reels {identifier}: {e}")
            raise
    
    async def get_hashtag(self, tag: str) -> HashtagData:
        """
        Fetch hashtag information and posts.
        
        Endpoint: GET /v1/hashtag
        Params: tag
        """
        clean_tag = self._clean_tag(tag)
        
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            try:
                response = await client.get(
                    f"{self.base_url}/v1/hashtag",
                    params={"tag": clean_tag},
                    headers=self._get_headers()
                )
                response.raise_for_status()
                data = response.json()
                
                hashtag_data = data.get("data", {})
                
                # Parse top and recent posts
                top_posts = []
                recent_posts = []
                
                for item in hashtag_data.get("top_posts", []):
                    try:
                        media_item = self._parse_media_item(item)
                        top_posts.append(media_item)
                    except Exception as e:
                        logger.warning(f"Failed to parse top post: {e}")
                
                for item in hashtag_data.get("recent_posts", []):
                    try:
                        media_item = self._parse_media_item(item)
                        recent_posts.append(media_item)
                    except Exception as e:
                        logger.warning(f"Failed to parse recent post: {e}")
                
                return HashtagData(
                    tag=clean_tag,
                    media_count=hashtag_data.get("media_count", 0),
                    top_posts=top_posts,
                    recent_posts=recent_posts
                )
                
            except httpx.HTTPStatusError as e:
                logger.error(f"RapidAPI HTTP error fetching hashtag {tag}: {e}")
                raise
            except Exception as e:
                logger.error(f"Error fetching hashtag {tag}: {e}")
                raise
    
    async def search(
        self,
        query: str,
        search_type: SearchType = SearchType.ACCOUNTS
    ) -> SearchResults:
        """
        Search for accounts, hashtags, or places.
        
        Endpoint: GET /v1/search
        Params: query, type
        """
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            try:
                response = await client.get(
                    f"{self.base_url}/v1/search",
                    params={
                        "query": query,
                        "type": search_type.value
                    },
                    headers=self._get_headers()
                )
                response.raise_for_status()
                data = response.json()
                
                results_data = data.get("data", {})
                
                accounts = []
                hashtags = []
                
                # Parse accounts
                for account_data in results_data.get("users", []):
                    try:
                        profile = Profile(
                            id=str(account_data.get("pk", "")),
                            username=account_data.get("username", ""),
                            full_name=account_data.get("full_name", ""),
                            bio="",
                            followers_count=account_data.get("follower_count", 0),
                            following_count=0,
                            media_count=0,
                            is_verified=account_data.get("is_verified", False),
                            profile_pic_url=account_data.get("profile_pic_url", ""),
                            provider=self.name
                        )
                        accounts.append(profile)
                    except Exception as e:
                        logger.warning(f"Failed to parse account: {e}")
                
                # Parse hashtags
                for hashtag_data in results_data.get("hashtags", []):
                    try:
                        tag = hashtag_data.get("name", "")
                        if tag:
                            hashtags.append(tag)
                    except Exception as e:
                        logger.warning(f"Failed to parse hashtag: {e}")
                
                return SearchResults(
                    accounts=accounts,
                    hashtags=hashtags,
                    cursor=results_data.get("cursor"),
                    has_more=results_data.get("has_more", False)
                )
                
            except httpx.HTTPStatusError as e:
                logger.error(f"RapidAPI HTTP error searching {query}: {e}")
                raise
            except Exception as e:
                logger.error(f"Error searching {query}: {e}")
                raise
    
    async def is_healthy(self) -> bool:
        """Check if the RapidAPI service is responding"""
        try:
            # Try a simple profile fetch with a known account
            await self.get_profile("instagram")
            return True
        except Exception as e:
            logger.error(f"Health check failed: {e}")
            return False
    
    def _parse_media_page(self, data: Dict[str, Any]) -> MediaPage:
        """Parse API response into MediaPage"""
        page_data = data.get("data", {})
        items = []
        
        for item_data in page_data.get("items", []):
            try:
                media_item = self._parse_media_item(item_data)
                items.append(media_item)
            except Exception as e:
                logger.warning(f"Failed to parse media item: {e}")
        
        return MediaPage(
            items=items,
            cursor=page_data.get("pagination_token") or page_data.get("next_cursor"),
            has_more=page_data.get("has_more", False)
        )
    
    def _parse_media_item(self, item_data: Dict[str, Any]) -> MediaItem:
        """Parse a single media item from API response"""
        # Determine media type
        media_type_raw = item_data.get("media_type", "IMAGE")
        if "reel" in str(media_type_raw).lower() or item_data.get("product_type") == "clips":
            media_type = MediaType.REEL
        elif "video" in str(media_type_raw).lower():
            media_type = MediaType.VIDEO
        elif "carousel" in str(media_type_raw).lower():
            media_type = MediaType.CAROUSEL
        else:
            media_type = MediaType.IMAGE
        
        # Extract caption
        caption = ""
        if "caption" in item_data:
            if isinstance(item_data["caption"], dict):
                caption = item_data["caption"].get("text", "")
            else:
                caption = str(item_data["caption"])
        
        # Extract audio info for reels
        audio = None
        if media_type == MediaType.REEL and "audio" in item_data:
            audio_data = item_data["audio"]
            audio = AudioInfo(
                id=str(audio_data.get("id", "")),
                title=audio_data.get("title", "Original Audio"),
                artist=audio_data.get("artist", ""),
                duration_ms=audio_data.get("duration_ms")
            )
        
        # Parse timestamp
        timestamp = datetime.now()
        if "taken_at" in item_data:
            try:
                timestamp = datetime.fromtimestamp(item_data["taken_at"])
            except Exception:
                pass
        elif "timestamp" in item_data:
            try:
                timestamp = datetime.fromisoformat(item_data["timestamp"].replace("Z", "+00:00"))
            except Exception:
                pass
        
        # Get video URL if available
        video_url = None
        if media_type in [MediaType.REEL, MediaType.VIDEO]:
            video_url = item_data.get("video_url") or item_data.get("video_versions", [{}])[0].get("url")
        
        # Get thumbnail
        thumbnail_url = ""
        if "thumbnail_url" in item_data:
            thumbnail_url = item_data["thumbnail_url"]
        elif "image_versions2" in item_data:
            candidates = item_data["image_versions2"].get("candidates", [])
            if candidates:
                thumbnail_url = candidates[0].get("url", "")
        
        # Build permalink
        code = item_data.get("code", item_data.get("shortcode", ""))
        permalink = f"https://www.instagram.com/p/{code}/" if code else ""
        
        return MediaItem(
            id=str(item_data.get("id", item_data.get("pk", ""))),
            media_type=media_type,
            caption=caption,
            permalink=permalink,
            thumbnail_url=thumbnail_url,
            like_count=item_data.get("like_count", 0),
            comment_count=item_data.get("comment_count", 0),
            play_count=item_data.get("play_count") or item_data.get("view_count"),
            timestamp=timestamp,
            video_url=video_url,
            audio=audio,
            hashtags=self._extract_hashtags(caption),
            mentions=self._extract_mentions(caption)
        )
