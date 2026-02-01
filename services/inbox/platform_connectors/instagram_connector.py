"""
Instagram Connector for Community Inbox
Fetches comments, DMs, and mentions from Instagram via RapidAPI.
"""

import os
import aiohttp
from datetime import datetime, timezone
from typing import Dict, List, Optional
from dataclasses import dataclass
from loguru import logger


RAPIDAPI_KEY = os.getenv("RAPIDAPI_KEY", "")
RAPIDAPI_HOST = "instagram-looter2.p.rapidapi.com"

INSTAGRAM_ACCOUNTS = {
    807: "@the_isaiah_dupree",
    670: "@the_isaiah_dupree_",
    1369: "@dupree_isaiah_",
    4508: "@dupree_isaiah"
}


@dataclass
class InstagramMessage:
    """An Instagram comment or DM."""
    id: str
    message_type: str  # comment, dm, mention
    sender_username: str
    sender_name: str
    sender_id: str
    content: str
    post_id: Optional[str] = None
    post_url: Optional[str] = None
    timestamp: datetime = None
    likes: int = 0
    replies: int = 0
    
    def to_dict(self) -> Dict:
        return {
            "id": self.id,
            "platform": "instagram",
            "message_type": self.message_type,
            "sender_username": self.sender_username,
            "sender_name": self.sender_name,
            "sender_id": self.sender_id,
            "content": self.content,
            "post_id": self.post_id,
            "post_url": self.post_url,
            "timestamp": self.timestamp.isoformat() if self.timestamp else None,
            "likes": self.likes,
            "replies": self.replies
        }


class InstagramConnector:
    """
    Connects to Instagram via RapidAPI to fetch:
    - Post comments
    - DM messages (limited)
    - Mentions
    """
    
    def __init__(self):
        self.api_key = RAPIDAPI_KEY
        self.host = RAPIDAPI_HOST
        logger.info("✅ InstagramConnector initialized")
    
    async def fetch_post_comments(
        self,
        post_url: str,
        limit: int = 50
    ) -> List[InstagramMessage]:
        """
        Fetch comments from an Instagram post.
        
        Args:
            post_url: Full Instagram post URL
            limit: Max comments to fetch
        
        Returns:
            List of InstagramMessage objects
        """
        messages = []
        
        try:
            async with aiohttp.ClientSession() as session:
                headers = {
                    "X-RapidAPI-Key": self.api_key,
                    "X-RapidAPI-Host": self.host
                }
                
                async with session.get(
                    f"https://{self.host}/post",
                    headers=headers,
                    params={"url": post_url}
                ) as response:
                    if response.status != 200:
                        logger.warning(f"Instagram API error: {response.status}")
                        return messages
                    
                    data = await response.json()
                    
                    # Extract post ID
                    post_id = data.get("id") or data.get("pk")
                    
                    # Get comments
                    comments = data.get("comments", [])[:limit]
                    
                    for comment in comments:
                        user = comment.get("user", {})
                        
                        msg = InstagramMessage(
                            id=str(comment.get("pk", "")),
                            message_type="comment",
                            sender_username=user.get("username", ""),
                            sender_name=user.get("full_name", ""),
                            sender_id=str(user.get("pk", "")),
                            content=comment.get("text", ""),
                            post_id=str(post_id),
                            post_url=post_url,
                            timestamp=datetime.fromtimestamp(
                                comment.get("created_at_utc", 0),
                                tz=timezone.utc
                            ) if comment.get("created_at_utc") else datetime.now(timezone.utc),
                            likes=comment.get("comment_like_count", 0),
                            replies=len(comment.get("child_comments", []))
                        )
                        messages.append(msg)
            
            logger.info(f"📥 Fetched {len(messages)} comments from Instagram")
            
        except Exception as e:
            logger.error(f"Instagram fetch failed: {e}")
        
        return messages
    
    async def fetch_user_posts(
        self,
        username: str,
        limit: int = 12
    ) -> List[Dict]:
        """
        Fetch recent posts from a user to then get comments.
        
        Args:
            username: Instagram username (without @)
            limit: Max posts to fetch
        
        Returns:
            List of post data dicts
        """
        posts = []
        
        try:
            async with aiohttp.ClientSession() as session:
                headers = {
                    "X-RapidAPI-Key": self.api_key,
                    "X-RapidAPI-Host": self.host
                }
                
                async with session.get(
                    f"https://{self.host}/v1/posts",
                    headers=headers,
                    params={"username": username.replace("@", "")}
                ) as response:
                    if response.status != 200:
                        logger.warning(f"Instagram posts API error: {response.status}")
                        return posts
                    
                    data = await response.json()
                    
                    items = data.get("data", {}).get("items", [])[:limit]
                    
                    for item in items:
                        posts.append({
                            "id": item.get("pk"),
                            "shortcode": item.get("code"),
                            "url": f"https://www.instagram.com/p/{item.get('code')}/",
                            "caption": item.get("caption", {}).get("text", ""),
                            "comment_count": item.get("comment_count", 0),
                            "like_count": item.get("like_count", 0),
                            "timestamp": item.get("taken_at")
                        })
            
            logger.info(f"📥 Fetched {len(posts)} posts from @{username}")
            
        except Exception as e:
            logger.error(f"Instagram posts fetch failed: {e}")
        
        return posts
    
    async def fetch_all_comments(
        self,
        account_id: int = None,
        limit_per_post: int = 20
    ) -> List[InstagramMessage]:
        """
        Fetch comments from all recent posts for an account.
        
        Args:
            account_id: Blotato account ID (807, 670, etc.)
            limit_per_post: Max comments per post
        
        Returns:
            All comments across posts
        """
        all_messages = []
        
        # Get username from account ID
        if account_id:
            username = INSTAGRAM_ACCOUNTS.get(account_id, "").replace("@", "")
        else:
            username = "the_isaiah_dupree"
        
        if not username:
            return all_messages
        
        # Get recent posts
        posts = await self.fetch_user_posts(username, limit=6)
        
        # Get comments from each post
        for post in posts:
            if post.get("comment_count", 0) > 0:
                comments = await self.fetch_post_comments(
                    post.get("url"),
                    limit=limit_per_post
                )
                all_messages.extend(comments)
        
        return all_messages
    
    async def sync_to_inbox(self, account_id: int = None) -> Dict:
        """
        Sync Instagram messages to the inbox database.
        
        Returns:
            Sync result summary
        """
        try:
            from services.inbox import get_inbox_service
            
            inbox = get_inbox_service()
            messages = await self.fetch_all_comments(account_id)
            
            saved = 0
            for msg in messages:
                # Convert to inbox format and save
                inbox_msg = inbox.create_message(
                    platform="instagram",
                    message_type=msg.message_type,
                    sender_username=msg.sender_username,
                    sender_name=msg.sender_name,
                    content=msg.content,
                    post_id=msg.post_id,
                    external_id=msg.id
                )
                if inbox_msg:
                    saved += 1
            
            return {
                "success": True,
                "platform": "instagram",
                "fetched": len(messages),
                "saved": saved
            }
            
        except Exception as e:
            logger.error(f"Instagram sync failed: {e}")
            return {"success": False, "error": str(e)}


# =============================================================================
# SINGLETON
# =============================================================================

_connector_instance: Optional[InstagramConnector] = None

def get_instagram_connector() -> InstagramConnector:
    """Get singleton instance of InstagramConnector."""
    global _connector_instance
    if _connector_instance is None:
        _connector_instance = InstagramConnector()
    return _connector_instance
