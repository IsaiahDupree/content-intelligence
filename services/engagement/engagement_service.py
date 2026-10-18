"""
Engagement Service

High-level API for managing auto-engagement across platforms.
Integrates with EventBus for pub/sub processing.

Usage:
    service = get_engagement_service()
    
    # Request engagement
    correlation_id = await service.request_engagement('threads', count=5)
    
    # Check status
    status = await service.get_status('threads')
    
    # Adjust limits
    await service.set_daily_limit('threads', 150)
    
    # Pause/resume
    await service.pause_platform('threads')
    await service.resume_platform('threads')
"""

import os
import sys
import logging
import random
from datetime import datetime, timezone
from typing import Optional, Dict, List, Any
from dataclasses import dataclass, field
from uuid import uuid4

# Add parent to path for imports
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(__file__))))

from services.event_bus import EventBus, Topics
from .comment_tracker import CommentTracker, PlatformStatus, get_comment_tracker

logger = logging.getLogger(__name__)

# Supported platforms
PLATFORMS = ['threads', 'instagram', 'tiktok', 'twitter']

# Delay configuration (seconds between comments)
DELAY_CONFIG = {
    'threads': {'min': 30, 'max': 120},
    'instagram': {'min': 45, 'max': 180},
    'tiktok': {'min': 30, 'max': 120},
    'twitter': {'min': 30, 'max': 90}
}


@dataclass
class EngagementRequest:
    """Request to engage with a platform."""
    platform: str
    count: int = 1
    correlation_id: str = field(default_factory=lambda: f"eng-{uuid4().hex[:8]}")


@dataclass
class EngagementResult:
    """Result of an engagement session."""
    correlation_id: str
    platform: str
    requested_count: int
    comments_posted: int = 0
    comments_skipped: int = 0
    errors: List[str] = field(default_factory=list)
    started_at: Optional[datetime] = None
    completed_at: Optional[datetime] = None
    success: bool = False


class EngagementService:
    """
    Service for managing auto-engagement.
    
    Provides API for:
    - Triggering engagement sessions via pub/sub
    - Checking status and limits
    - Pausing/resuming engagement
    - Managing daily limits
    """
    
    def __init__(
        self,
        event_bus: Optional[EventBus] = None,
        tracker: Optional[CommentTracker] = None
    ):
        """
        Initialize the service.
        
        Args:
            event_bus: EventBus instance (uses singleton if None)
            tracker: CommentTracker instance (uses singleton if None)
        """
        self._event_bus = event_bus
        self._tracker = tracker
        self._pending_requests: Dict[str, EngagementRequest] = {}
    
    @property
    def event_bus(self) -> EventBus:
        """Get EventBus instance."""
        if self._event_bus is None:
            self._event_bus = EventBus.get_instance()
        return self._event_bus
    
    @property
    def tracker(self) -> CommentTracker:
        """Get CommentTracker instance."""
        if self._tracker is None:
            self._tracker = get_comment_tracker()
        return self._tracker
    
    async def request_engagement(
        self,
        platform: str,
        count: int = 1
    ) -> str:
        """
        Request an engagement session.
        
        Publishes ENGAGEMENT_REQUESTED event for worker to process.
        
        Args:
            platform: Platform to engage on (threads, instagram, tiktok)
            count: Number of comments to post
            
        Returns:
            Correlation ID for tracking
            
        Raises:
            ValueError: If platform is invalid or limit reached
        """
        if platform not in PLATFORMS:
            raise ValueError(f"Invalid platform: {platform}. Must be one of {PLATFORMS}")
        
        # Check if enabled
        if not await self.tracker.is_enabled(platform):
            raise ValueError(f"Engagement is paused for {platform}")
        
        # Check limit
        remaining = await self.tracker.get_remaining(platform)
        if remaining <= 0:
            raise ValueError(f"Daily limit reached for {platform}")
        
        # Adjust count if needed
        actual_count = min(count, remaining)
        
        # Create request
        request = EngagementRequest(
            platform=platform,
            count=actual_count
        )
        
        # Track pending request
        self._pending_requests[request.correlation_id] = request
        
        # Publish event
        await self.event_bus.publish(
            Topics.ENGAGEMENT_REQUESTED,
            {
                'platform': platform,
                'count': actual_count,
                'requested_count': count,
                'remaining_after': remaining - actual_count
            },
            correlation_id=request.correlation_id
        )
        
        logger.info(
            f"Requested {actual_count} engagements on {platform} "
            f"(correlation_id={request.correlation_id})"
        )
        
        return request.correlation_id
    
    async def request_all_platforms(
        self,
        count_per_platform: int = 1
    ) -> Dict[str, str]:
        """
        Request engagement on all enabled platforms.
        
        Args:
            count_per_platform: Number of comments per platform
            
        Returns:
            Dict mapping platform to correlation_id
        """
        results = {}
        
        for platform in PLATFORMS:
            try:
                if await self.tracker.is_enabled(platform):
                    correlation_id = await self.request_engagement(
                        platform, count_per_platform
                    )
                    results[platform] = correlation_id
                else:
                    logger.info(f"Skipping {platform} (disabled)")
            except ValueError as e:
                logger.warning(f"Could not request {platform}: {e}")
                results[platform] = f"error: {e}"
        
        return results
    
    async def get_status(self, platform: str = None) -> Dict[str, Any]:
        """
        Get engagement status.
        
        Args:
            platform: Optional specific platform (returns all if None)
            
        Returns:
            Status dict with platform details
        """
        if platform:
            status = await self.tracker.get_status(platform)
            return {
                'platform': status.platform,
                'is_enabled': status.is_enabled,
                'daily_limit': status.daily_limit,
                'today_count': status.today_count,
                'remaining': status.remaining,
                'last_engagement': status.last_engagement.isoformat() if status.last_engagement else None
            }
        
        # Get all platforms
        all_status = await self.tracker.get_all_status()
        total_today = sum(s.today_count for s in all_status.values())
        
        return {
            'platforms': {
                p: {
                    'is_enabled': s.is_enabled,
                    'daily_limit': s.daily_limit,
                    'today_count': s.today_count,
                    'remaining': s.remaining,
                    'last_engagement': s.last_engagement.isoformat() if s.last_engagement else None
                }
                for p, s in all_status.items()
            },
            'total_today': total_today
        }
    
    async def set_daily_limit(self, platform: str, limit: int) -> None:
        """
        Update daily limit for platform.
        
        Args:
            platform: Platform name
            limit: New daily limit
        """
        if platform not in PLATFORMS:
            raise ValueError(f"Invalid platform: {platform}")
        if limit < 0:
            raise ValueError("Limit must be non-negative")
        
        await self.tracker.set_daily_limit(platform, limit)
        
        # Publish event
        await self.event_bus.publish(
            Topics.ENGAGEMENT_RESUMED,  # Reuse for config change
            {
                'platform': platform,
                'action': 'limit_changed',
                'new_limit': limit
            }
        )
    
    async def pause_platform(self, platform: str) -> None:
        """
        Pause engagement for a platform.
        
        Args:
            platform: Platform name or 'all'
        """
        platforms = PLATFORMS if platform == 'all' else [platform]
        
        for p in platforms:
            if p not in PLATFORMS:
                raise ValueError(f"Invalid platform: {p}")
            
            await self.tracker.set_enabled(p, False)
            
            await self.event_bus.publish(
                Topics.ENGAGEMENT_PAUSED,
                {'platform': p}
            )
            
            logger.info(f"Paused engagement for {p}")
    
    async def resume_platform(self, platform: str) -> None:
        """
        Resume engagement for a platform.
        
        Args:
            platform: Platform name or 'all'
        """
        platforms = PLATFORMS if platform == 'all' else [platform]
        
        for p in platforms:
            if p not in PLATFORMS:
                raise ValueError(f"Invalid platform: {p}")
            
            await self.tracker.set_enabled(p, True)
            
            await self.event_bus.publish(
                Topics.ENGAGEMENT_RESUMED,
                {'platform': p}
            )
            
            logger.info(f"Resumed engagement for {p}")
    
    async def get_recent_comments(
        self,
        platform: str = None,
        limit: int = 50
    ) -> List[Dict[str, Any]]:
        """
        Get recent engagement comments.
        
        Args:
            platform: Optional platform filter
            limit: Maximum comments to return
            
        Returns:
            List of comment dicts
        """
        comments = await self.tracker.get_recent_comments(platform, limit)
        return [
            {
                'id': c.id,
                'platform': c.platform,
                'post_url': c.post_url,
                'post_username': c.post_username,
                'comment_text': c.comment_text,
                'proof_screenshot': c.proof_screenshot,
                'engagement_account': c.engagement_account,
                'created_at': c.created_at.isoformat()
            }
            for c in comments
        ]
    
    def get_delay_for_platform(self, platform: str) -> int:
        """
        Get random delay for platform (between comments).
        
        Args:
            platform: Platform name
            
        Returns:
            Delay in seconds
        """
        config = DELAY_CONFIG.get(platform, {'min': 30, 'max': 120})
        return random.randint(config['min'], config['max'])


# Singleton instance
_service: Optional[EngagementService] = None


def get_engagement_service() -> EngagementService:
    """Get singleton EngagementService instance."""
    global _service
    if _service is None:
        _service = EngagementService()
    return _service
