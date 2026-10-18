"""
Engagement Service Package

Provides auto-engagement functionality for social media platforms:
- Comment tracking to prevent duplicates
- Daily limits enforcement
- Pub/sub integration for scalable processing
"""

from .comment_tracker import (
    CommentTracker,
    EngagementComment,
    PlatformStatus,
    get_comment_tracker
)
from .engagement_service import (
    EngagementService,
    EngagementRequest,
    EngagementResult,
    get_engagement_service
)

__all__ = [
    'CommentTracker',
    'EngagementComment',
    'PlatformStatus',
    'get_comment_tracker',
    'EngagementService',
    'EngagementRequest',
    'EngagementResult',
    'get_engagement_service',
]
