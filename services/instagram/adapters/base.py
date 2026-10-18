"""
Base Instagram Adapter Interface
Defines the contract that all Instagram data providers must implement
"""
from abc import ABC, abstractmethod
from typing import Optional, List, Dict, Any
from datetime import datetime
from dataclasses import dataclass
from enum import Enum


class MediaType(str, Enum):
    """Instagram media types"""
    REEL = "REEL"
    IMAGE = "IMAGE"
    CAROUSEL = "CAROUSEL"
    VIDEO = "VIDEO"


class SearchType(str, Enum):
    """Search result types"""
    ACCOUNTS = "accounts"
    HASHTAGS = "hashtags"
    PLACES = "places"


@dataclass
class AudioInfo:
    """Audio/sound information from a reel"""
    id: str
    title: str
    artist: str
    duration_ms: Optional[int] = None


@dataclass
class Profile:
    """Normalized Instagram profile data"""
    id: str
    username: str
    full_name: str
    bio: str
    followers_count: int
    following_count: int
    media_count: int
    is_verified: bool
    profile_pic_url: str
    provider: str
    external_url: Optional[str] = None
    is_business: Optional[bool] = None
    category: Optional[str] = None


@dataclass
class MediaItem:
    """Normalized Instagram media item"""
    id: str
    media_type: MediaType
    caption: str
    permalink: str
    thumbnail_url: str
    like_count: int
    comment_count: int
    timestamp: datetime
    play_count: Optional[int] = None
    video_url: Optional[str] = None
    audio: Optional[AudioInfo] = None
    hashtags: List[str] = None
    mentions: List[str] = None
    
    def __post_init__(self):
        if self.hashtags is None:
            self.hashtags = []
        if self.mentions is None:
            self.mentions = []


@dataclass
class MediaPage:
    """Paginated media results"""
    items: List[MediaItem]
    cursor: Optional[str] = None
    has_more: bool = False


@dataclass
class HashtagData:
    """Hashtag information and metrics"""
    tag: str
    media_count: int
    top_posts: List[MediaItem]
    recent_posts: List[MediaItem]


@dataclass
class SearchResults:
    """Search results"""
    accounts: List[Profile]
    hashtags: List[str]
    cursor: Optional[str] = None
    has_more: bool = False


class InstagramAdapter(ABC):
    """
    Base adapter interface for Instagram data providers.
    All providers (RapidAPI, official API, etc.) must implement this interface.
    """
    
    def __init__(self, api_key: str, **kwargs):
        self.api_key = api_key
        self.name = "base"
        self.type = "unknown"
    
    @abstractmethod
    async def get_profile(self, identifier: str) -> Profile:
        """
        Fetch profile information.
        
        Args:
            identifier: Username, user ID, or profile URL
            
        Returns:
            Normalized Profile object
        """
        pass
    
    @abstractmethod
    async def get_media(
        self, 
        identifier: str, 
        cursor: Optional[str] = None,
        limit: int = 50
    ) -> MediaPage:
        """
        Fetch media/posts from a profile.
        
        Args:
            identifier: Username, user ID, or profile URL
            cursor: Pagination cursor from previous request
            limit: Maximum number of items to fetch
            
        Returns:
            MediaPage with items and pagination info
        """
        pass
    
    @abstractmethod
    async def get_reels(
        self,
        identifier: str,
        cursor: Optional[str] = None,
        limit: int = 50
    ) -> MediaPage:
        """
        Fetch reels specifically from a profile.
        
        Args:
            identifier: Username, user ID, or profile URL
            cursor: Pagination cursor
            limit: Maximum number of reels
            
        Returns:
            MediaPage with reel items
        """
        pass
    
    @abstractmethod
    async def get_hashtag(self, tag: str) -> HashtagData:
        """
        Fetch hashtag information and top posts.
        
        Args:
            tag: Hashtag (with or without #)
            
        Returns:
            HashtagData with metrics and posts
        """
        pass
    
    @abstractmethod
    async def search(
        self,
        query: str,
        search_type: SearchType = SearchType.ACCOUNTS
    ) -> SearchResults:
        """
        Search for accounts, hashtags, or places.
        
        Args:
            query: Search query
            search_type: Type of search to perform
            
        Returns:
            SearchResults with matching items
        """
        pass
    
    @abstractmethod
    async def is_healthy(self) -> bool:
        """
        Check if the provider is healthy and responding.
        
        Returns:
            True if healthy, False otherwise
        """
        pass
    
    def _extract_hashtags(self, caption: str) -> List[str]:
        """Extract hashtags from caption text"""
        if not caption:
            return []
        import re
        return re.findall(r'#(\w+)', caption)
    
    def _extract_mentions(self, caption: str) -> List[str]:
        """Extract @mentions from caption text"""
        if not caption:
            return []
        import re
        return re.findall(r'@(\w+)', caption)
    
    def _clean_tag(self, tag: str) -> str:
        """Remove # from hashtag if present"""
        return tag.lstrip('#')
