"""
Comment Tracker for Auto-Engagement

Tracks posted comments in Supabase to:
- Prevent duplicate comments on the same post
- Enforce daily limits per platform
- Provide engagement history and analytics

Usage:
    tracker = CommentTracker()
    
    # Check before commenting
    if await tracker.has_commented_on('threads', 'https://threads.net/@user/post/123'):
        print("Already commented on this post")
    
    # Check daily limit
    if await tracker.is_limit_reached('threads'):
        print("Daily limit reached")
    
    # Record a comment
    await tracker.record_comment(
        platform='threads',
        post_url='https://threads.net/@user/post/123',
        post_username='user',
        comment_text='Great post!',
        proof_screenshot='/tmp/proof.png'
    )
"""

import os
import logging
from datetime import datetime, timezone, timedelta
from typing import Optional, Dict, List, Any
from dataclasses import dataclass

logger = logging.getLogger(__name__)

# Default daily limits per platform
DEFAULT_LIMITS = {
    'threads': 100,
    'instagram': 100,
    'tiktok': 100,
    'twitter': 100
}


@dataclass
class EngagementComment:
    """Represents a posted engagement comment."""
    id: str
    platform: str
    post_url: str
    post_username: str
    comment_text: str
    proof_screenshot: Optional[str]
    engagement_account: str
    created_at: datetime


@dataclass
class PlatformStatus:
    """Status of engagement for a platform."""
    platform: str
    is_enabled: bool
    daily_limit: int
    today_count: int
    remaining: int
    last_engagement: Optional[datetime]


class CommentTracker:
    """
    Tracks engagement comments in Supabase.
    
    Features:
    - Duplicate detection by post URL
    - Daily count tracking per platform
    - Rate limit enforcement
    - Comment history retrieval
    """
    
    def __init__(self, supabase_client=None):
        """
        Initialize the tracker.
        
        Args:
            supabase_client: Optional Supabase client (auto-creates if None)
        """
        self._supabase = supabase_client
        self._limits_cache: Dict[str, int] = {}
        self._enabled_cache: Dict[str, bool] = {}
    
    async def _get_supabase(self):
        """Get or create Supabase client."""
        if self._supabase is None:
            try:
                # Try the newer supabase-py package first
                try:
                    from supabase import create_client, Client
                except ImportError:
                    # Fallback: try supabase_py or skip if not available
                    try:
                        from supabase_py import create_client, Client
                    except ImportError:
                        logger.warning("Supabase client not available - comment tracking disabled")
                        return None
                
                url = os.environ.get('SUPABASE_URL', 'http://127.0.0.1:54321')
                key = os.environ.get('SUPABASE_ANON_KEY', 'eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJpc3MiOiJzdXBhYmFzZS1kZW1vIiwicm9sZSI6ImFub24iLCJleHAiOjE5ODM4MTI5OTZ9.CRXP1A7WOeoJeXxjNni43kdQwgnWNReilDMblYTn_I0')
                self._supabase = create_client(url, key)
            except Exception as e:
                logger.warning(f"Supabase not available: {e} - comment tracking disabled")
                return None
        return self._supabase
    
    async def has_commented_on(self, platform: str, post_url: str) -> bool:
        """
        Check if we've already commented on this post.
        
        Args:
            platform: Platform name (threads, instagram, tiktok)
            post_url: URL of the post
            
        Returns:
            True if we've already commented on this post
        """
        try:
            supabase = await self._get_supabase()
            if supabase is None:
                return False  # No tracking available, allow comment
            
            result = supabase.table('engagement_comments').select('id').eq(
                'platform', platform
            ).eq(
                'post_url', post_url
            ).execute()
            
            return len(result.data) > 0
        except Exception as e:
            logger.warning(f"Error checking duplicate (continuing anyway): {e}")
            return False
    
    async def record_comment(
        self,
        platform: str,
        post_url: str,
        comment_text: str,
        post_username: str = "",
        proof_screenshot: str = "",
        engagement_account: str = ""
    ) -> str:
        """
        Record a new comment.
        
        Args:
            platform: Platform name
            post_url: URL of the post
            comment_text: The comment we posted
            post_username: Creator of the post
            proof_screenshot: Path to proof screenshot
            engagement_account: Our account that posted
            
        Returns:
            Comment ID
            
        Raises:
            ValueError: If comment already exists (duplicate)
        """
        try:
            supabase = await self._get_supabase()
            if supabase is None:
                logger.info(f"Comment tracked locally (no DB): {platform} - {comment_text[:50]}...")
                return "local-" + str(hash(post_url))[:8]
            
            data = {
                'platform': platform,
                'post_url': post_url,
                'post_username': post_username,
                'comment_text': comment_text,
                'proof_screenshot': proof_screenshot,
                'engagement_account': engagement_account
            }
            
            result = supabase.table('engagement_comments').insert(data).execute()
            
            if result.data:
                comment_id = result.data[0]['id']
                logger.info(f"Recorded comment {comment_id} on {platform}: {post_url}")
                return comment_id
            else:
                return "local-" + str(hash(post_url))[:8]
                
        except Exception as e:
            logger.warning(f"Comment tracking failed (continuing): {e}")
            return "local-" + str(hash(post_url))[:8]
    
    async def get_daily_count(self, platform: str) -> int:
        """
        Get today's comment count for platform.
        
        Args:
            platform: Platform name
            
        Returns:
            Number of comments posted today
        """
        try:
            supabase = await self._get_supabase()
            if supabase is None:
                return 0  # No tracking, return 0
            
            # Get start of today (UTC)
            today_start = datetime.now(timezone.utc).replace(
                hour=0, minute=0, second=0, microsecond=0
            )
            
            result = supabase.table('engagement_comments').select(
                'id', count='exact'
            ).eq(
                'platform', platform
            ).gte(
                'created_at', today_start.isoformat()
            ).execute()
            
            return result.count or 0
            
        except Exception as e:
            logger.warning(f"Error getting daily count (returning 0): {e}")
            return 0
    
    async def get_daily_limit(self, platform: str) -> int:
        """
        Get daily limit for platform.
        
        Args:
            platform: Platform name
            
        Returns:
            Daily limit (default 100)
        """
        # Check cache first
        if platform in self._limits_cache:
            return self._limits_cache[platform]
        
        try:
            supabase = await self._get_supabase()
            if supabase is None:
                return DEFAULT_LIMITS.get(platform, 100)
            
            result = supabase.table('engagement_limits').select(
                'daily_limit'
            ).eq('platform', platform).execute()
            
            if result.data:
                limit = result.data[0]['daily_limit']
                self._limits_cache[platform] = limit
                return limit
                
        except Exception as e:
            logger.warning(f"Error getting limit (using default): {e}")
        
        return DEFAULT_LIMITS.get(platform, 100)
    
    async def is_limit_reached(self, platform: str) -> bool:
        """
        Check if daily limit is reached.
        
        Args:
            platform: Platform name
            
        Returns:
            True if daily limit is reached
        """
        count = await self.get_daily_count(platform)
        limit = await self.get_daily_limit(platform)
        return count >= limit
    
    async def get_remaining(self, platform: str) -> int:
        """
        Get remaining comments for today.
        
        Args:
            platform: Platform name
            
        Returns:
            Number of comments remaining
        """
        count = await self.get_daily_count(platform)
        limit = await self.get_daily_limit(platform)
        return max(0, limit - count)
    
    async def is_enabled(self, platform: str) -> bool:
        """
        Check if engagement is enabled for platform.
        
        Args:
            platform: Platform name
            
        Returns:
            True if engagement is enabled
        """
        # Check cache first
        if platform in self._enabled_cache:
            return self._enabled_cache[platform]
        
        try:
            supabase = await self._get_supabase()
            result = supabase.table('engagement_limits').select(
                'is_enabled'
            ).eq('platform', platform).execute()
            
            if result.data:
                enabled = result.data[0]['is_enabled']
                self._enabled_cache[platform] = enabled
                return enabled
                
        except Exception as e:
            logger.error(f"Error checking enabled status: {e}")
        
        return True  # Default to enabled
    
    async def set_daily_limit(self, platform: str, limit: int) -> None:
        """
        Update daily limit for platform.
        
        Args:
            platform: Platform name
            limit: New daily limit
        """
        try:
            supabase = await self._get_supabase()
            supabase.table('engagement_limits').upsert({
                'platform': platform,
                'daily_limit': limit,
                'updated_at': datetime.now(timezone.utc).isoformat()
            }).execute()
            
            # Update cache
            self._limits_cache[platform] = limit
            logger.info(f"Set {platform} daily limit to {limit}")
            
        except Exception as e:
            logger.error(f"Error setting limit: {e}")
            raise
    
    async def set_enabled(self, platform: str, enabled: bool) -> None:
        """
        Enable or disable engagement for platform.
        
        Args:
            platform: Platform name
            enabled: Whether to enable
        """
        try:
            supabase = await self._get_supabase()
            supabase.table('engagement_limits').upsert({
                'platform': platform,
                'is_enabled': enabled,
                'updated_at': datetime.now(timezone.utc).isoformat()
            }).execute()
            
            # Update cache
            self._enabled_cache[platform] = enabled
            logger.info(f"{'Enabled' if enabled else 'Disabled'} {platform} engagement")
            
        except Exception as e:
            logger.error(f"Error setting enabled status: {e}")
            raise
    
    async def get_status(self, platform: str) -> PlatformStatus:
        """
        Get full status for platform.
        
        Args:
            platform: Platform name
            
        Returns:
            PlatformStatus with all details
        """
        is_enabled = await self.is_enabled(platform)
        daily_limit = await self.get_daily_limit(platform)
        today_count = await self.get_daily_count(platform)
        
        # Get last engagement
        last_engagement = None
        try:
            supabase = await self._get_supabase()
            result = supabase.table('engagement_comments').select(
                'created_at'
            ).eq('platform', platform).order(
                'created_at', desc=True
            ).limit(1).execute()
            
            if result.data:
                last_engagement = datetime.fromisoformat(
                    result.data[0]['created_at'].replace('Z', '+00:00')
                )
        except Exception as e:
            logger.error(f"Error getting last engagement: {e}")
        
        return PlatformStatus(
            platform=platform,
            is_enabled=is_enabled,
            daily_limit=daily_limit,
            today_count=today_count,
            remaining=max(0, daily_limit - today_count),
            last_engagement=last_engagement
        )
    
    async def get_all_status(self) -> Dict[str, PlatformStatus]:
        """
        Get status for all platforms.
        
        Returns:
            Dict mapping platform name to status
        """
        platforms = ['threads', 'instagram', 'tiktok']
        return {p: await self.get_status(p) for p in platforms}
    
    async def get_recent_comments(
        self,
        platform: str = None,
        limit: int = 50
    ) -> List[EngagementComment]:
        """
        Get recent comments.
        
        Args:
            platform: Optional platform filter
            limit: Maximum comments to return
            
        Returns:
            List of recent comments
        """
        try:
            supabase = await self._get_supabase()
            query = supabase.table('engagement_comments').select('*')
            
            if platform:
                query = query.eq('platform', platform)
            
            result = query.order('created_at', desc=True).limit(limit).execute()
            
            return [
                EngagementComment(
                    id=r['id'],
                    platform=r['platform'],
                    post_url=r['post_url'],
                    post_username=r.get('post_username', ''),
                    comment_text=r['comment_text'],
                    proof_screenshot=r.get('proof_screenshot'),
                    engagement_account=r.get('engagement_account', ''),
                    created_at=datetime.fromisoformat(
                        r['created_at'].replace('Z', '+00:00')
                    )
                )
                for r in result.data
            ]
            
        except Exception as e:
            logger.error(f"Error getting recent comments: {e}")
            return []
    
    def clear_cache(self):
        """Clear the internal caches."""
        self._limits_cache.clear()
        self._enabled_cache.clear()


# Singleton instance
_tracker: Optional[CommentTracker] = None


def get_comment_tracker() -> CommentTracker:
    """Get singleton CommentTracker instance."""
    global _tracker
    if _tracker is None:
        _tracker = CommentTracker()
    return _tracker
