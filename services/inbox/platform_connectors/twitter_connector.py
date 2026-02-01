"""
Twitter/X Connector for Community Inbox
Fetches mentions, replies, and DMs from Twitter.
"""

import os
import aiohttp
from datetime import datetime, timezone
from typing import Dict, List, Optional
from dataclasses import dataclass
from loguru import logger


RAPIDAPI_KEY = os.getenv("RAPIDAPI_KEY", "")
RAPIDAPI_HOST = "twitter241.p.rapidapi.com"

TWITTER_ACCOUNTS = {
    4151: "@IsaiahDupree7"
}


@dataclass
class TwitterMessage:
    """A Twitter mention, reply, or DM."""
    id: str
    message_type: str  # mention, reply, dm
    sender_username: str
    sender_name: str
    sender_id: str
    content: str
    tweet_id: Optional[str] = None
    tweet_url: Optional[str] = None
    timestamp: datetime = None
    likes: int = 0
    retweets: int = 0
    
    def to_dict(self) -> Dict:
        return {
            "id": self.id,
            "platform": "twitter",
            "message_type": self.message_type,
            "sender_username": self.sender_username,
            "sender_name": self.sender_name,
            "sender_id": self.sender_id,
            "content": self.content,
            "tweet_id": self.tweet_id,
            "tweet_url": self.tweet_url,
            "timestamp": self.timestamp.isoformat() if self.timestamp else None,
            "likes": self.likes,
            "retweets": self.retweets
        }


class TwitterConnector:
    """
    Connects to Twitter/X to fetch:
    - Mentions
    - Replies to tweets
    - DMs (limited, requires auth)
    """
    
    def __init__(self):
        self.api_key = RAPIDAPI_KEY
        self.host = RAPIDAPI_HOST
        logger.info("✅ TwitterConnector initialized")
    
    async def fetch_mentions(
        self,
        username: str,
        limit: int = 50
    ) -> List[TwitterMessage]:
        """
        Fetch tweets mentioning a user.
        
        Args:
            username: Twitter username (without @)
            limit: Max mentions to fetch
        
        Returns:
            List of TwitterMessage objects
        """
        messages = []
        
        try:
            async with aiohttp.ClientSession() as session:
                headers = {
                    "X-RapidAPI-Key": self.api_key,
                    "X-RapidAPI-Host": self.host
                }
                
                # Search for mentions
                query = f"@{username.replace('@', '')}"
                
                async with session.get(
                    f"https://{self.host}/search",
                    headers=headers,
                    params={"query": query, "count": str(limit)}
                ) as response:
                    if response.status != 200:
                        logger.warning(f"Twitter API error: {response.status}")
                        return messages
                    
                    data = await response.json()
                    
                    tweets = data.get("result", {}).get("timeline", {}).get("instructions", [])
                    
                    for instruction in tweets:
                        entries = instruction.get("entries", [])
                        for entry in entries[:limit]:
                            content = entry.get("content", {})
                            tweet_result = content.get("itemContent", {}).get("tweet_results", {}).get("result", {})
                            
                            if not tweet_result:
                                continue
                            
                            legacy = tweet_result.get("legacy", {})
                            user = tweet_result.get("core", {}).get("user_results", {}).get("result", {}).get("legacy", {})
                            
                            msg = TwitterMessage(
                                id=legacy.get("id_str", ""),
                                message_type="mention",
                                sender_username=user.get("screen_name", ""),
                                sender_name=user.get("name", ""),
                                sender_id=legacy.get("user_id_str", ""),
                                content=legacy.get("full_text", ""),
                                tweet_id=legacy.get("id_str", ""),
                                tweet_url=f"https://twitter.com/{user.get('screen_name')}/status/{legacy.get('id_str')}",
                                timestamp=datetime.now(timezone.utc),
                                likes=legacy.get("favorite_count", 0),
                                retweets=legacy.get("retweet_count", 0)
                            )
                            messages.append(msg)
            
            logger.info(f"📥 Fetched {len(messages)} mentions from Twitter")
            
        except Exception as e:
            logger.error(f"Twitter fetch failed: {e}")
        
        return messages
    
    async def fetch_tweet_replies(
        self,
        tweet_id: str,
        limit: int = 50
    ) -> List[TwitterMessage]:
        """
        Fetch replies to a specific tweet.
        
        Args:
            tweet_id: Tweet ID
            limit: Max replies to fetch
        
        Returns:
            List of TwitterMessage objects
        """
        messages = []
        
        try:
            async with aiohttp.ClientSession() as session:
                headers = {
                    "X-RapidAPI-Key": self.api_key,
                    "X-RapidAPI-Host": self.host
                }
                
                async with session.get(
                    f"https://{self.host}/tweet-replies",
                    headers=headers,
                    params={"tweet_id": tweet_id, "count": str(limit)}
                ) as response:
                    if response.status != 200:
                        logger.warning(f"Twitter replies API error: {response.status}")
                        return messages
                    
                    data = await response.json()
                    
                    # Parse replies from response
                    replies = data.get("result", {}).get("replies", [])[:limit]
                    
                    for reply in replies:
                        legacy = reply.get("legacy", {})
                        user = reply.get("user", {})
                        
                        msg = TwitterMessage(
                            id=legacy.get("id_str", reply.get("id", "")),
                            message_type="reply",
                            sender_username=user.get("screen_name", ""),
                            sender_name=user.get("name", ""),
                            sender_id=str(user.get("id", "")),
                            content=legacy.get("full_text", reply.get("text", "")),
                            tweet_id=tweet_id,
                            timestamp=datetime.now(timezone.utc),
                            likes=legacy.get("favorite_count", 0)
                        )
                        messages.append(msg)
            
            logger.info(f"📥 Fetched {len(messages)} replies from Twitter")
            
        except Exception as e:
            logger.error(f"Twitter replies fetch failed: {e}")
        
        return messages
    
    async def fetch_user_tweets(
        self,
        username: str,
        limit: int = 10
    ) -> List[Dict]:
        """
        Fetch recent tweets from a user.
        """
        tweets = []
        
        try:
            async with aiohttp.ClientSession() as session:
                headers = {
                    "X-RapidAPI-Key": self.api_key,
                    "X-RapidAPI-Host": self.host
                }
                
                async with session.get(
                    f"https://{self.host}/user-tweets",
                    headers=headers,
                    params={"username": username.replace("@", ""), "count": str(limit)}
                ) as response:
                    if response.status != 200:
                        return tweets
                    
                    data = await response.json()
                    
                    items = data.get("result", {}).get("timeline", {}).get("instructions", [])
                    
                    for instruction in items:
                        entries = instruction.get("entries", [])
                        for entry in entries[:limit]:
                            content = entry.get("content", {})
                            tweet = content.get("itemContent", {}).get("tweet_results", {}).get("result", {})
                            
                            if tweet:
                                legacy = tweet.get("legacy", {})
                                tweets.append({
                                    "id": legacy.get("id_str"),
                                    "text": legacy.get("full_text"),
                                    "reply_count": legacy.get("reply_count", 0),
                                    "like_count": legacy.get("favorite_count", 0),
                                    "retweet_count": legacy.get("retweet_count", 0)
                                })
            
            logger.info(f"📥 Fetched {len(tweets)} tweets from @{username}")
            
        except Exception as e:
            logger.error(f"Twitter tweets fetch failed: {e}")
        
        return tweets
    
    async def fetch_all_messages(
        self,
        account_id: int = None,
        limit_per_source: int = 20
    ) -> List[TwitterMessage]:
        """
        Fetch all messages (mentions + replies) for an account.
        """
        all_messages = []
        
        # Get username from account ID
        if account_id:
            username = TWITTER_ACCOUNTS.get(account_id, "").replace("@", "")
        else:
            username = "IsaiahDupree7"
        
        if not username:
            return all_messages
        
        # Get mentions
        mentions = await self.fetch_mentions(username, limit=limit_per_source)
        all_messages.extend(mentions)
        
        # Get tweets and their replies
        tweets = await self.fetch_user_tweets(username, limit=5)
        for tweet in tweets:
            if tweet.get("reply_count", 0) > 0 and tweet.get("id"):
                replies = await self.fetch_tweet_replies(
                    tweet["id"],
                    limit=limit_per_source
                )
                all_messages.extend(replies)
        
        return all_messages
    
    async def sync_to_inbox(self, account_id: int = None) -> Dict:
        """Sync Twitter messages to inbox."""
        try:
            from services.inbox import get_inbox_service
            
            inbox = get_inbox_service()
            messages = await self.fetch_all_messages(account_id)
            
            saved = 0
            for msg in messages:
                inbox_msg = inbox.create_message(
                    platform="twitter",
                    message_type=msg.message_type,
                    sender_username=msg.sender_username,
                    sender_name=msg.sender_name,
                    content=msg.content,
                    post_id=msg.tweet_id,
                    external_id=msg.id
                )
                if inbox_msg:
                    saved += 1
            
            return {
                "success": True,
                "platform": "twitter",
                "fetched": len(messages),
                "saved": saved
            }
            
        except Exception as e:
            logger.error(f"Twitter sync failed: {e}")
            return {"success": False, "error": str(e)}


# =============================================================================
# SINGLETON
# =============================================================================

_connector_instance: Optional[TwitterConnector] = None

def get_twitter_connector() -> TwitterConnector:
    """Get singleton instance."""
    global _connector_instance
    if _connector_instance is None:
        _connector_instance = TwitterConnector()
    return _connector_instance
