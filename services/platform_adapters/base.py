"""
Platform Publishing Service Base Classes
Defines interface for multi-platform content publishing
"""
from abc import ABC, abstractmethod
from typing import Dict, List, Optional, Any
from dataclasses import dataclass
from datetime import datetime
from enum import Enum


class PlatformType(str, Enum):
    """Supported social media platforms"""
    TIKTOK = "tiktok"
    INSTAGRAM = "instagram"
    YOUTUBE = "youtube"
    LINKEDIN = "linkedin"
    TWITTER = "twitter"
    FACEBOOK = "facebook"
    PINTEREST = "pinterest"
    SNAPCHAT = "snapchat"
    THREADS = "threads"


@dataclass
class PublishRequest:
    """Request to publish content to a platform"""
    video_path: str
    title: str
    caption: str
    hashtags: List[str]
    thumbnail_path: Optional[str] = None
    scheduled_time: Optional[datetime] = None
    platform_specific_options: Optional[Dict[str, Any]] = None


@dataclass
class PublishResult:
    """Result of publishing content"""
    success: bool
    platform: str
    platform_post_id: Optional[str] = None
    post_url: Optional[str] = None
    error_message: Optional[str] = None
    published_at: Optional[datetime] = None
    metadata: Optional[Dict[str, Any]] = None


@dataclass
class MetricsSnapshot:
    """Performance metrics at a specific time"""
    platform_post_id: str
    collected_at: datetime
    views: int = 0
    unique_viewers: int = 0
    impressions: int = 0
    avg_watch_time_s: Optional[float] = None
    avg_watch_pct: Optional[float] = None
    retention_curve: Optional[List[Dict[str, float]]] = None
    likes: int = 0
    comments: int = 0
    shares: int = 0
    saves: int = 0
    profile_taps: int = 0
    link_clicks: int = 0


@dataclass
class CommentData:
    """Single comment from a platform"""
    platform_comment_id: str
    author_handle: str
    author_name: Optional[str]
    text: str
    created_at: datetime
    likes: int = 0
    replies: int = 0
    is_reply: bool = False
    parent_comment_id: Optional[str] = None


class PlatformAdapter(ABC):
    """Base class for platform-specific adapters"""
    
    def __init__(self, credentials: Dict[str, str]):
        """
        Initialize adapter with credentials
        
        Args:
            credentials: Platform-specific credentials
        """
        self.credentials = credentials
        self.platform_type = self._get_platform_type()
    
    @abstractmethod
    def _get_platform_type(self) -> PlatformType:
        """Return the platform type this adapter handles"""
        pass
    
    @abstractmethod
    def is_authenticated(self) -> bool:
        """Check if credentials are valid and authenticated"""
        pass
    
    @abstractmethod
    def publish_video(self, request: PublishRequest) -> PublishResult:
        """
        Publish video to the platform
        
        Args:
            request: Publishing request details
            
        Returns:
            Result with post URL and ID
        """
        pass
    
    @abstractmethod
    def get_post_url(self, platform_post_id: str) -> Optional[str]:
        """
        Get public URL for a post
        
        Args:
            platform_post_id: Platform-specific post ID
            
        Returns:
            Public URL to the post
        """
        pass
    
    @abstractmethod
    def fetch_metrics(self, platform_post_id: str) -> Optional[MetricsSnapshot]:
        """
        Fetch current performance metrics for a post
        
        Args:
            platform_post_id: Platform-specific post ID
            
        Returns:
            Current metrics snapshot
        """
        pass
    
    @abstractmethod
    def fetch_comments(
        self,
        platform_post_id: str,
        limit: int = 100,
        since: Optional[datetime] = None
    ) -> List[CommentData]:
        """
        Fetch comments on a post
        
        Args:
            platform_post_id: Platform-specific post ID
            limit: Maximum number of comments to fetch
            since: Only fetch comments after this time
            
        Returns:
            List of comments
        """
        pass
    
    @abstractmethod
    def delete_post(self, platform_post_id: str) -> bool:
        """
        Delete a post from the platform
        
        Args:
            platform_post_id: Platform-specific post ID
            
        Returns:
            True if successful
        """
        pass
    
    def supports_scheduling(self) -> bool:
        """Whether this platform supports native scheduling"""
        return False
    
    def supports_retention_curve(self) -> bool:
        """Whether this platform provides retention curve data"""
        return False
    
    def get_rate_limits(self) -> Dict[str, int]:
        """
        Get API rate limits for this platform
        
        Returns:
            Dict with rate limit info
        """
        return {
            "posts_per_day": 100,
            "api_calls_per_hour": 1000
        }


class MockPlatformAdapter(PlatformAdapter):
    """Mock adapter for testing"""
    
    def __init__(self, platform_type: PlatformType, credentials: Optional[Dict[str, str]] = None):
        self._platform_type = platform_type
        super().__init__(credentials or {})
        self.published_posts = {}
    
    def _get_platform_type(self) -> PlatformType:
        return self._platform_type
    
    def is_authenticated(self) -> bool:
        return True
    
    def publish_video(self, request: PublishRequest) -> PublishResult:
        import uuid
        post_id = f"{self.platform_type.value}_{uuid.uuid4().hex[:8]}"
        post_url = f"https://{self.platform_type.value}.com/post/{post_id}"
        
        self.published_posts[post_id] = {
            "title": request.title,
            "caption": request.caption,
            "published_at": datetime.now()
        }
        
        return PublishResult(
            success=True,
            platform=self.platform_type.value,
            platform_post_id=post_id,
            post_url=post_url,
            published_at=datetime.now()
        )
    
    def get_post_url(self, platform_post_id: str) -> Optional[str]:
        return f"https://{self.platform_type.value}.com/post/{platform_post_id}"
    
    def fetch_metrics(self, platform_post_id: str) -> Optional[MetricsSnapshot]:
        import random
        return MetricsSnapshot(
            platform_post_id=platform_post_id,
            collected_at=datetime.now(),
            views=random.randint(1000, 10000),
            likes=random.randint(50, 500),
            comments=random.randint(10, 100),
            shares=random.randint(5, 50),
            saves=random.randint(10, 80)
        )
    
    def fetch_comments(
        self,
        platform_post_id: str,
        limit: int = 100,
        since: Optional[datetime] = None
    ) -> List[CommentData]:
        # Return mock comments
        return [
            CommentData(
                platform_comment_id=f"comment_{i}",
                author_handle=f"@user{i}",
                author_name=f"User {i}",
                text=f"Great content! #{i}",
                created_at=datetime.now()
            )
            for i in range(min(5, limit))
        ]
    
    def delete_post(self, platform_post_id: str) -> bool:
        if platform_post_id in self.published_posts:
            del self.published_posts[platform_post_id]
            return True
        return False
