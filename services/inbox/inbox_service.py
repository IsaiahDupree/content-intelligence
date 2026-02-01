"""
Community Inbox Service
Implements unified message aggregation across all platforms.
"""

import os
import json
from datetime import datetime, timezone, timedelta
from typing import Dict, List, Optional, Any
from dataclasses import dataclass, field, asdict
from enum import Enum
from uuid import uuid4
from loguru import logger
from sqlalchemy import create_engine, text


DATABASE_URL = os.getenv("DATABASE_URL", "postgresql://postgres:postgres@127.0.0.1:54322/postgres")


# =============================================================================
# ENUMS
# =============================================================================

class MessagePlatform(str, Enum):
    INSTAGRAM = "instagram"
    TIKTOK = "tiktok"
    TWITTER = "twitter"
    YOUTUBE = "youtube"
    THREADS = "threads"
    FACEBOOK = "facebook"


class MessageType(str, Enum):
    COMMENT = "comment"
    DM = "dm"
    MENTION = "mention"
    REPLY = "reply"
    STORY_REPLY = "story_reply"


class MessageStatus(str, Enum):
    UNREAD = "unread"
    READ = "read"
    REPLIED = "replied"
    ARCHIVED = "archived"
    SPAM = "spam"


class MessagePriority(str, Enum):
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"


class Sentiment(str, Enum):
    POSITIVE = "positive"
    NEUTRAL = "neutral"
    NEGATIVE = "negative"
    QUESTION = "question"


# =============================================================================
# DATA MODELS
# =============================================================================

@dataclass
class InboxMessage:
    """Unified message from any platform."""
    id: str = field(default_factory=lambda: str(uuid4()))
    platform: str = "instagram"
    message_type: str = "comment"
    
    # Sender info
    sender_username: str = ""
    sender_name: str = ""
    sender_avatar_url: str = ""
    sender_follower_count: int = 0
    
    # Content
    content: str = ""
    media_url: Optional[str] = None
    
    # Context
    post_id: Optional[str] = None
    post_url: Optional[str] = None
    post_caption: Optional[str] = None
    parent_message_id: Optional[str] = None
    thread_id: Optional[str] = None
    
    # Status
    status: str = MessageStatus.UNREAD.value
    priority: str = MessagePriority.MEDIUM.value
    sentiment: str = Sentiment.NEUTRAL.value
    
    # Engagement scoring
    engagement_score: int = 0
    is_verified: bool = False
    is_influencer: bool = False
    
    # Metadata
    platform_message_id: str = ""
    received_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    read_at: Optional[datetime] = None
    replied_at: Optional[datetime] = None
    
    # Tags and notes
    tags: List[str] = field(default_factory=list)
    internal_notes: str = ""
    assigned_to: Optional[str] = None
    
    def to_dict(self) -> Dict:
        """Serialize to dictionary."""
        return {
            "id": self.id,
            "platform": self.platform,
            "message_type": self.message_type,
            "sender_username": self.sender_username,
            "sender_name": self.sender_name,
            "sender_avatar_url": self.sender_avatar_url,
            "sender_follower_count": self.sender_follower_count,
            "content": self.content,
            "media_url": self.media_url,
            "post_id": self.post_id,
            "post_url": self.post_url,
            "post_caption": self.post_caption[:100] + "..." if self.post_caption and len(self.post_caption) > 100 else self.post_caption,
            "parent_message_id": self.parent_message_id,
            "thread_id": self.thread_id,
            "status": self.status,
            "priority": self.priority,
            "sentiment": self.sentiment,
            "engagement_score": self.engagement_score,
            "is_verified": self.is_verified,
            "is_influencer": self.is_influencer,
            "platform_message_id": self.platform_message_id,
            "received_at": self.received_at.isoformat() if self.received_at else None,
            "read_at": self.read_at.isoformat() if self.read_at else None,
            "replied_at": self.replied_at.isoformat() if self.replied_at else None,
            "tags": self.tags,
            "internal_notes": self.internal_notes,
            "assigned_to": self.assigned_to
        }


@dataclass
class SavedReply:
    """Saved reply template."""
    id: str = field(default_factory=lambda: str(uuid4()))
    name: str = ""
    content: str = ""
    category: str = "general"
    variables: List[str] = field(default_factory=list)  # e.g., ["name", "product"]
    use_count: int = 0
    platforms: List[str] = field(default_factory=list)  # empty = all platforms
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    
    def to_dict(self) -> Dict:
        return {
            "id": self.id,
            "name": self.name,
            "content": self.content,
            "category": self.category,
            "variables": self.variables,
            "use_count": self.use_count,
            "platforms": self.platforms,
            "created_at": self.created_at.isoformat() if self.created_at else None
        }


@dataclass
class ContentIdea:
    """Content idea generated from inbox message."""
    id: str = field(default_factory=lambda: str(uuid4()))
    source_message_id: str = ""
    title: str = ""
    description: str = ""
    content_type: str = "video"  # video, post, story, thread
    platforms: List[str] = field(default_factory=list)
    status: str = "idea"  # idea, planned, in_progress, published
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    
    def to_dict(self) -> Dict:
        return {
            "id": self.id,
            "source_message_id": self.source_message_id,
            "title": self.title,
            "description": self.description,
            "content_type": self.content_type,
            "platforms": self.platforms,
            "status": self.status,
            "created_at": self.created_at.isoformat() if self.created_at else None
        }


# =============================================================================
# INBOX SERVICE
# =============================================================================

class InboxService:
    """
    Community Inbox Service.
    
    Aggregates messages from all platforms into a unified inbox.
    """
    
    def __init__(self):
        self.engine = create_engine(DATABASE_URL)
        self._ensure_tables()
        logger.info("✅ InboxService initialized")
    
    def _ensure_tables(self):
        """Create database tables if they don't exist."""
        with self.engine.connect() as conn:
            conn.execute(text("""
                CREATE TABLE IF NOT EXISTS inbox_messages (
                    id TEXT PRIMARY KEY,
                    platform TEXT NOT NULL,
                    message_type TEXT NOT NULL,
                    sender_username TEXT NOT NULL,
                    sender_name TEXT,
                    sender_avatar_url TEXT,
                    sender_follower_count INTEGER DEFAULT 0,
                    content TEXT,
                    media_url TEXT,
                    post_id TEXT,
                    post_url TEXT,
                    post_caption TEXT,
                    parent_message_id TEXT,
                    thread_id TEXT,
                    status TEXT DEFAULT 'unread',
                    priority TEXT DEFAULT 'medium',
                    sentiment TEXT DEFAULT 'neutral',
                    engagement_score INTEGER DEFAULT 0,
                    is_verified BOOLEAN DEFAULT FALSE,
                    is_influencer BOOLEAN DEFAULT FALSE,
                    platform_message_id TEXT,
                    received_at TIMESTAMP DEFAULT NOW(),
                    read_at TIMESTAMP,
                    replied_at TIMESTAMP,
                    tags TEXT[] DEFAULT '{}',
                    internal_notes TEXT,
                    assigned_to TEXT,
                    created_at TIMESTAMP DEFAULT NOW(),
                    UNIQUE(platform, platform_message_id)
                )
            """))
            
            conn.execute(text("""
                CREATE TABLE IF NOT EXISTS inbox_replies (
                    id TEXT PRIMARY KEY,
                    message_id TEXT NOT NULL REFERENCES inbox_messages(id),
                    content TEXT NOT NULL,
                    sent_at TIMESTAMP DEFAULT NOW(),
                    sent_by TEXT,
                    is_ai_generated BOOLEAN DEFAULT FALSE,
                    platform_reply_id TEXT
                )
            """))
            
            conn.execute(text("""
                CREATE TABLE IF NOT EXISTS saved_replies (
                    id TEXT PRIMARY KEY,
                    name TEXT NOT NULL,
                    content TEXT NOT NULL,
                    category TEXT DEFAULT 'general',
                    variables TEXT[] DEFAULT '{}',
                    use_count INTEGER DEFAULT 0,
                    platforms TEXT[] DEFAULT '{}',
                    created_at TIMESTAMP DEFAULT NOW()
                )
            """))
            
            conn.execute(text("""
                CREATE TABLE IF NOT EXISTS inbox_content_ideas (
                    id TEXT PRIMARY KEY,
                    source_message_id TEXT REFERENCES inbox_messages(id),
                    title TEXT NOT NULL,
                    description TEXT,
                    content_type TEXT DEFAULT 'video',
                    platforms TEXT[] DEFAULT '{}',
                    status TEXT DEFAULT 'idea',
                    created_at TIMESTAMP DEFAULT NOW()
                )
            """))
            
            conn.execute(text("""
                CREATE INDEX IF NOT EXISTS idx_inbox_status 
                ON inbox_messages(status)
            """))
            
            conn.execute(text("""
                CREATE INDEX IF NOT EXISTS idx_inbox_platform 
                ON inbox_messages(platform)
            """))
            
            conn.execute(text("""
                CREATE INDEX IF NOT EXISTS idx_inbox_received 
                ON inbox_messages(received_at DESC)
            """))
            
            conn.commit()
        
        logger.info("✅ Inbox tables created")
    
    # -------------------------------------------------------------------------
    # MESSAGE MANAGEMENT
    # -------------------------------------------------------------------------
    
    def add_message(self, message: InboxMessage) -> InboxMessage:
        """Add a new message to the inbox."""
        with self.engine.connect() as conn:
            conn.execute(text("""
                INSERT INTO inbox_messages (
                    id, platform, message_type, sender_username, sender_name,
                    sender_avatar_url, sender_follower_count, content, media_url,
                    post_id, post_url, post_caption, parent_message_id, thread_id,
                    status, priority, sentiment, engagement_score, is_verified,
                    is_influencer, platform_message_id, received_at, tags,
                    internal_notes, assigned_to
                ) VALUES (
                    :id, :platform, :message_type, :sender_username, :sender_name,
                    :sender_avatar_url, :sender_follower_count, :content, :media_url,
                    :post_id, :post_url, :post_caption, :parent_message_id, :thread_id,
                    :status, :priority, :sentiment, :engagement_score, :is_verified,
                    :is_influencer, :platform_message_id, :received_at, :tags,
                    :internal_notes, :assigned_to
                )
                ON CONFLICT (platform, platform_message_id) DO NOTHING
            """), {
                "id": message.id,
                "platform": message.platform,
                "message_type": message.message_type,
                "sender_username": message.sender_username,
                "sender_name": message.sender_name,
                "sender_avatar_url": message.sender_avatar_url,
                "sender_follower_count": message.sender_follower_count,
                "content": message.content,
                "media_url": message.media_url,
                "post_id": message.post_id,
                "post_url": message.post_url,
                "post_caption": message.post_caption,
                "parent_message_id": message.parent_message_id,
                "thread_id": message.thread_id,
                "status": message.status,
                "priority": message.priority,
                "sentiment": message.sentiment,
                "engagement_score": message.engagement_score,
                "is_verified": message.is_verified,
                "is_influencer": message.is_influencer,
                "platform_message_id": message.platform_message_id,
                "received_at": message.received_at,
                "tags": message.tags,
                "internal_notes": message.internal_notes,
                "assigned_to": message.assigned_to
            })
            conn.commit()
        
        logger.info(f"📨 Message added: {message.platform} from @{message.sender_username}")
        return message
    
    def get_message(self, message_id: str) -> Optional[InboxMessage]:
        """Get a message by ID."""
        with self.engine.connect() as conn:
            result = conn.execute(text("""
                SELECT * FROM inbox_messages WHERE id = :id
            """), {"id": message_id}).fetchone()
            
            if result:
                return self._row_to_message(result._mapping)
        
        return None
    
    def get_messages(
        self,
        status: Optional[str] = None,
        platform: Optional[str] = None,
        message_type: Optional[str] = None,
        priority: Optional[str] = None,
        limit: int = 50,
        offset: int = 0
    ) -> List[InboxMessage]:
        """Get messages with filters."""
        query = "SELECT * FROM inbox_messages WHERE 1=1"
        params: Dict[str, Any] = {"limit": limit, "offset": offset}
        
        if status:
            query += " AND status = :status"
            params["status"] = status
        
        if platform:
            query += " AND platform = :platform"
            params["platform"] = platform
        
        if message_type:
            query += " AND message_type = :message_type"
            params["message_type"] = message_type
        
        if priority:
            query += " AND priority = :priority"
            params["priority"] = priority
        
        query += " ORDER BY received_at DESC LIMIT :limit OFFSET :offset"
        
        with self.engine.connect() as conn:
            results = conn.execute(text(query), params).fetchall()
            return [self._row_to_message(r._mapping) for r in results]
    
    def update_status(self, message_id: str, status: str) -> bool:
        """Update message status."""
        with self.engine.connect() as conn:
            result = conn.execute(text("""
                UPDATE inbox_messages 
                SET status = :status,
                    read_at = CASE WHEN :status = 'read' THEN NOW() ELSE read_at END,
                    replied_at = CASE WHEN :status = 'replied' THEN NOW() ELSE replied_at END
                WHERE id = :id
            """), {"id": message_id, "status": status})
            conn.commit()
            return result.rowcount > 0
    
    def add_tags(self, message_id: str, tags: List[str]) -> bool:
        """Add tags to a message."""
        with self.engine.connect() as conn:
            result = conn.execute(text("""
                UPDATE inbox_messages 
                SET tags = array_cat(tags, :tags)
                WHERE id = :id
            """), {"id": message_id, "tags": tags})
            conn.commit()
            return result.rowcount > 0
    
    def assign_message(self, message_id: str, assigned_to: str) -> bool:
        """Assign message to a user."""
        with self.engine.connect() as conn:
            result = conn.execute(text("""
                UPDATE inbox_messages 
                SET assigned_to = :assigned_to
                WHERE id = :id
            """), {"id": message_id, "assigned_to": assigned_to})
            conn.commit()
            return result.rowcount > 0
    
    def get_unread_count(self, platform: Optional[str] = None) -> int:
        """Get count of unread messages."""
        query = "SELECT COUNT(*) FROM inbox_messages WHERE status = 'unread'"
        params: Dict[str, Any] = {}
        
        if platform:
            query += " AND platform = :platform"
            params["platform"] = platform
        
        with self.engine.connect() as conn:
            result = conn.execute(text(query), params).fetchone()
            return result[0] if result else 0
    
    def get_stats(self) -> Dict:
        """Get inbox statistics."""
        with self.engine.connect() as conn:
            # Total counts by status
            status_counts = conn.execute(text("""
                SELECT status, COUNT(*) as count
                FROM inbox_messages
                GROUP BY status
            """)).fetchall()
            
            # Counts by platform
            platform_counts = conn.execute(text("""
                SELECT platform, COUNT(*) as count
                FROM inbox_messages
                GROUP BY platform
            """)).fetchall()
            
            # Today's messages
            today_count = conn.execute(text("""
                SELECT COUNT(*) FROM inbox_messages
                WHERE received_at >= CURRENT_DATE
            """)).fetchone()
            
            return {
                "by_status": {r[0]: r[1] for r in status_counts},
                "by_platform": {r[0]: r[1] for r in platform_counts},
                "today": today_count[0] if today_count else 0,
                "unread": self.get_unread_count()
            }
    
    def _row_to_message(self, row: Dict) -> InboxMessage:
        """Convert database row to InboxMessage."""
        return InboxMessage(
            id=row.get("id"),
            platform=row.get("platform"),
            message_type=row.get("message_type"),
            sender_username=row.get("sender_username"),
            sender_name=row.get("sender_name"),
            sender_avatar_url=row.get("sender_avatar_url"),
            sender_follower_count=row.get("sender_follower_count", 0),
            content=row.get("content"),
            media_url=row.get("media_url"),
            post_id=row.get("post_id"),
            post_url=row.get("post_url"),
            post_caption=row.get("post_caption"),
            parent_message_id=row.get("parent_message_id"),
            thread_id=row.get("thread_id"),
            status=row.get("status"),
            priority=row.get("priority"),
            sentiment=row.get("sentiment"),
            engagement_score=row.get("engagement_score", 0),
            is_verified=row.get("is_verified", False),
            is_influencer=row.get("is_influencer", False),
            platform_message_id=row.get("platform_message_id"),
            received_at=row.get("received_at"),
            read_at=row.get("read_at"),
            replied_at=row.get("replied_at"),
            tags=row.get("tags", []),
            internal_notes=row.get("internal_notes"),
            assigned_to=row.get("assigned_to")
        )
    
    # -------------------------------------------------------------------------
    # SAVED REPLIES
    # -------------------------------------------------------------------------
    
    def create_saved_reply(self, reply: SavedReply) -> SavedReply:
        """Create a saved reply template."""
        with self.engine.connect() as conn:
            conn.execute(text("""
                INSERT INTO saved_replies (id, name, content, category, variables, platforms)
                VALUES (:id, :name, :content, :category, :variables, :platforms)
            """), {
                "id": reply.id,
                "name": reply.name,
                "content": reply.content,
                "category": reply.category,
                "variables": reply.variables,
                "platforms": reply.platforms
            })
            conn.commit()
        
        logger.info(f"💾 Saved reply created: {reply.name}")
        return reply
    
    def get_saved_replies(self, category: Optional[str] = None) -> List[SavedReply]:
        """Get all saved replies."""
        query = "SELECT * FROM saved_replies"
        params: Dict[str, Any] = {}
        
        if category:
            query += " WHERE category = :category"
            params["category"] = category
        
        query += " ORDER BY use_count DESC"
        
        with self.engine.connect() as conn:
            results = conn.execute(text(query), params).fetchall()
            return [
                SavedReply(
                    id=r[0],
                    name=r[1],
                    content=r[2],
                    category=r[3],
                    variables=r[4] or [],
                    use_count=r[5],
                    platforms=r[6] or [],
                    created_at=r[7]
                )
                for r in results
            ]
    
    def use_saved_reply(self, reply_id: str, variables: Dict[str, str] = None) -> Optional[str]:
        """Use a saved reply, replacing variables."""
        with self.engine.connect() as conn:
            result = conn.execute(text("""
                SELECT content FROM saved_replies WHERE id = :id
            """), {"id": reply_id}).fetchone()
            
            if not result:
                return None
            
            content = result[0]
            
            # Replace variables
            if variables:
                for var_name, var_value in variables.items():
                    content = content.replace(f"{{{var_name}}}", var_value)
            
            # Increment use count
            conn.execute(text("""
                UPDATE saved_replies SET use_count = use_count + 1 WHERE id = :id
            """), {"id": reply_id})
            conn.commit()
            
            return content
    
    # -------------------------------------------------------------------------
    # CONTENT IDEAS
    # -------------------------------------------------------------------------
    
    def create_content_idea(
        self,
        message_id: str,
        title: str,
        description: str = "",
        content_type: str = "video",
        platforms: List[str] = None
    ) -> ContentIdea:
        """Create a content idea from a message."""
        idea = ContentIdea(
            source_message_id=message_id,
            title=title,
            description=description,
            content_type=content_type,
            platforms=platforms or []
        )
        
        with self.engine.connect() as conn:
            conn.execute(text("""
                INSERT INTO inbox_content_ideas 
                (id, source_message_id, title, description, content_type, platforms, status)
                VALUES (:id, :source_message_id, :title, :description, :content_type, :platforms, :status)
            """), {
                "id": idea.id,
                "source_message_id": idea.source_message_id,
                "title": idea.title,
                "description": idea.description,
                "content_type": idea.content_type,
                "platforms": idea.platforms,
                "status": idea.status
            })
            conn.commit()
        
        logger.info(f"💡 Content idea created: {title}")
        return idea
    
    def get_content_ideas(self, status: Optional[str] = None) -> List[ContentIdea]:
        """Get content ideas."""
        query = "SELECT * FROM inbox_content_ideas"
        params: Dict[str, Any] = {}
        
        if status:
            query += " WHERE status = :status"
            params["status"] = status
        
        query += " ORDER BY created_at DESC"
        
        with self.engine.connect() as conn:
            results = conn.execute(text(query), params).fetchall()
            return [
                ContentIdea(
                    id=r[0],
                    source_message_id=r[1],
                    title=r[2],
                    description=r[3],
                    content_type=r[4],
                    platforms=r[5] or [],
                    status=r[6],
                    created_at=r[7]
                )
                for r in results
            ]


# =============================================================================
# SINGLETON
# =============================================================================

_inbox_instance: Optional[InboxService] = None

def get_inbox_service() -> InboxService:
    """Get singleton instance of InboxService."""
    global _inbox_instance
    if _inbox_instance is None:
        _inbox_instance = InboxService()
    return _inbox_instance
