"""
Threads Platform Adapter
(To be integrated - user mentioned comment abilities coming soon)
"""
from datetime import datetime
from typing import List, Optional
from services.platform_adapters.base import (
    PlatformAdapter, PlatformType, PublishRequest, PublishResult,
    MetricsSnapshot, CommentData
)


class ThreadsAdapter(PlatformAdapter):
    """Threads (Meta) publishing adapter"""
    
    def _get_platform_type(self) -> PlatformType:
        return PlatformType.THREADS
    
    def is_authenticated(self) -> bool:
        # Uses Meta authentication
        return "access_token" in self.credentials
    
    def publish_video(self, request: PublishRequest) -> PublishResult:
        # TODO: Implement Threads API publishing when available
        return PublishResult(
            success=False,
            platform=self.platform_type.value,
            error_message="Threads adapter not yet implemented"
        )
    
    def get_post_url(self, platform_post_id: str) -> Optional[str]:
        # Threads URLs format
        username = self.credentials.get("username", "user")
        return f"https://www.threads.net/@{username}/post/{platform_post_id}"
    
    def fetch_metrics(self, platform_post_id: str) -> Optional[MetricsSnapshot]:
        # TODO: Implement Threads Insights when available
        return None
    
    def fetch_comments(
        self,
        platform_post_id: str,
        limit: int = 100,
        since: Optional[datetime] = None
    ) -> List[CommentData]:
        # TODO: User mentioned comment abilities coming soon
        return []
    
    def delete_post(self, platform_post_id: str) -> bool:
        # TODO: Implement Threads delete
        return False
