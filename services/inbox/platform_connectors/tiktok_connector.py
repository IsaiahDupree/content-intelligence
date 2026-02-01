"""
TikTok Connector for Community Inbox
Fetches comments and mentions from TikTok via RapidAPI.
"""

import os
import aiohttp
from datetime import datetime, timezone
from typing import Dict, List, Optional
from dataclasses import dataclass
from loguru import logger


RAPIDAPI_KEY = os.getenv("RAPIDAPI_KEY", "")
RAPIDAPI_HOST = "tiktok-scraper7.p.rapidapi.com"

TIKTOK_ACCOUNTS = {
    710: "@isaiah_dupree",
    243: "@the_isaiah_dupree",
    4508: "@dupree_isaiah",
    571: "@soursides_is_sour"
}


@dataclass
class TikTokMessage:
    """A TikTok comment."""
    id: str
    message_type: str  # comment
    sender_username: str
    sender_name: str
    sender_id: str
    content: str
    video_id: Optional[str] = None
    video_url: Optional[str] = None
    timestamp: datetime = None
    likes: int = 0
    replies: int = 0
    
    def to_dict(self) -> Dict:
        return {
            "id": self.id,
            "platform": "tiktok",
            "message_type": self.message_type,
            "sender_username": self.sender_username,
            "sender_name": self.sender_name,
            "sender_id": self.sender_id,
            "content": self.content,
            "video_id": self.video_id,
            "video_url": self.video_url,
            "timestamp": self.timestamp.isoformat() if self.timestamp else None,
            "likes": self.likes,
            "replies": self.replies
        }


class TikTokConnector:
    """
    Connects to TikTok via RapidAPI to fetch:
    - Video comments
    - Mentions (limited)
    """
    
    def __init__(self):
        self.api_key = RAPIDAPI_KEY
        self.host = RAPIDAPI_HOST
        logger.info("✅ TikTokConnector initialized")
    
    async def fetch_video_comments(
        self,
        video_id: str,
        limit: int = 50
    ) -> List[TikTokMessage]:
        """
        Fetch comments from a TikTok video.
        
        Args:
            video_id: TikTok video ID
            limit: Max comments to fetch
        
        Returns:
            List of TikTokMessage objects
        """
        messages = []
        
        try:
            async with aiohttp.ClientSession() as session:
                headers = {
                    "X-RapidAPI-Key": self.api_key,
                    "X-RapidAPI-Host": self.host
                }
                
                async with session.get(
                    f"https://{self.host}/comment/list",
                    headers=headers,
                    params={"video_id": video_id, "count": str(limit)}
                ) as response:
                    if response.status != 200:
                        logger.warning(f"TikTok API error: {response.status}")
                        return messages
                    
                    data = await response.json()
                    
                    comments = data.get("data", {}).get("comments", [])[:limit]
                    
                    for comment in comments:
                        user = comment.get("user", {})
                        
                        msg = TikTokMessage(
                            id=str(comment.get("cid", "")),
                            message_type="comment",
                            sender_username=user.get("unique_id", ""),
                            sender_name=user.get("nickname", ""),
                            sender_id=str(user.get("uid", "")),
                            content=comment.get("text", ""),
                            video_id=video_id,
                            video_url=f"https://www.tiktok.com/@user/video/{video_id}",
                            timestamp=datetime.fromtimestamp(
                                comment.get("create_time", 0),
                                tz=timezone.utc
                            ) if comment.get("create_time") else datetime.now(timezone.utc),
                            likes=comment.get("digg_count", 0),
                            replies=comment.get("reply_comment_total", 0)
                        )
                        messages.append(msg)
            
            logger.info(f"📥 Fetched {len(messages)} comments from TikTok video")
            
        except Exception as e:
            logger.error(f"TikTok fetch failed: {e}")
        
        return messages
    
    async def fetch_user_videos(
        self,
        username: str,
        limit: int = 12
    ) -> List[Dict]:
        """
        Fetch recent videos from a user.
        
        Args:
            username: TikTok username (without @)
            limit: Max videos to fetch
        
        Returns:
            List of video data dicts
        """
        videos = []
        
        try:
            async with aiohttp.ClientSession() as session:
                headers = {
                    "X-RapidAPI-Key": self.api_key,
                    "X-RapidAPI-Host": self.host
                }
                
                async with session.get(
                    f"https://{self.host}/user/posts",
                    headers=headers,
                    params={"unique_id": username.replace("@", ""), "count": str(limit)}
                ) as response:
                    if response.status != 200:
                        logger.warning(f"TikTok videos API error: {response.status}")
                        return videos
                    
                    data = await response.json()
                    
                    items = data.get("data", {}).get("videos", [])[:limit]
                    
                    for item in items:
                        videos.append({
                            "id": item.get("video_id") or item.get("aweme_id"),
                            "url": f"https://www.tiktok.com/@{username}/video/{item.get('video_id') or item.get('aweme_id')}",
                            "description": item.get("desc", ""),
                            "comment_count": item.get("comment_count", 0),
                            "like_count": item.get("digg_count", 0),
                            "play_count": item.get("play_count", 0),
                            "timestamp": item.get("create_time")
                        })
            
            logger.info(f"📥 Fetched {len(videos)} videos from @{username}")
            
        except Exception as e:
            logger.error(f"TikTok videos fetch failed: {e}")
        
        return videos
    
    async def fetch_all_comments(
        self,
        account_id: int = None,
        limit_per_video: int = 20
    ) -> List[TikTokMessage]:
        """
        Fetch comments from all recent videos for an account.
        """
        all_messages = []
        
        # Get username from account ID
        if account_id:
            username = TIKTOK_ACCOUNTS.get(account_id, "").replace("@", "")
        else:
            username = "isaiah_dupree"
        
        if not username:
            return all_messages
        
        # Get recent videos
        videos = await self.fetch_user_videos(username, limit=6)
        
        # Get comments from each video
        for video in videos:
            if video.get("comment_count", 0) > 0:
                video_id = video.get("id")
                if video_id:
                    comments = await self.fetch_video_comments(
                        str(video_id),
                        limit=limit_per_video
                    )
                    all_messages.extend(comments)
        
        return all_messages
    
    async def sync_to_inbox(self, account_id: int = None) -> Dict:
        """Sync TikTok messages to inbox."""
        try:
            from services.inbox import get_inbox_service
            
            inbox = get_inbox_service()
            messages = await self.fetch_all_comments(account_id)
            
            saved = 0
            for msg in messages:
                inbox_msg = inbox.create_message(
                    platform="tiktok",
                    message_type=msg.message_type,
                    sender_username=msg.sender_username,
                    sender_name=msg.sender_name,
                    content=msg.content,
                    post_id=msg.video_id,
                    external_id=msg.id
                )
                if inbox_msg:
                    saved += 1
            
            return {
                "success": True,
                "platform": "tiktok",
                "fetched": len(messages),
                "saved": saved
            }
            
        except Exception as e:
            logger.error(f"TikTok sync failed: {e}")
            return {"success": False, "error": str(e)}


# =============================================================================
# SINGLETON
# =============================================================================

_connector_instance: Optional[TikTokConnector] = None

def get_tiktok_connector() -> TikTokConnector:
    """Get singleton instance."""
    global _connector_instance
    if _connector_instance is None:
        _connector_instance = TikTokConnector()
    return _connector_instance
