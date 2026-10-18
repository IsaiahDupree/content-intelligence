"""
Community Inbox Package
Unified message aggregation across all platforms.
"""

from .inbox_service import (
    InboxService,
    InboxMessage,
    SavedReply,
    ContentIdea,
    MessagePlatform,
    MessageType,
    MessageStatus,
    MessagePriority,
    Sentiment,
    get_inbox_service
)

__all__ = [
    "InboxService",
    "InboxMessage", 
    "SavedReply",
    "ContentIdea",
    "MessagePlatform",
    "MessageType",
    "MessageStatus",
    "MessagePriority",
    "Sentiment",
    "get_inbox_service"
]
