"""Format caching layer - caches classification results for performance."""
from typing import Dict, Any, Optional
import hashlib
from datetime import datetime, timedelta, timezone


class ClassificationCache:
    """In-memory cache for format classification results with TTL."""

    DEFAULT_TTL_HOURS = 24

    def __init__(self, ttl_hours: int = DEFAULT_TTL_HOURS):
        self.cache: Dict[str, Dict[str, Any]] = {}
        self.ttl = timedelta(hours=ttl_hours)

    def _make_key(self, content: str) -> str:
        """Generate cache key from content hash."""
        content_hash = hashlib.md5(content.encode()).hexdigest()
        return f"format_cache:{content_hash}"

    def get(self, content: str) -> Optional[Dict[str, Any]]:
        """Get cached classification result if valid."""
        key = self._make_key(content)

        if key not in self.cache:
            return None

        cached = self.cache[key]

        # Check if expired
        cached_at = datetime.fromisoformat(cached.get("cached_at", ""))
        if datetime.now(timezone.utc) - cached_at.replace(tzinfo=timezone.utc) > self.ttl:
            del self.cache[key]
            return None

        return cached.get("data")

    def set(self, content: str, data: Dict[str, Any]) -> None:
        """Store classification result in cache."""
        key = self._make_key(content)
        self.cache[key] = {
            "data": data,
            "cached_at": datetime.now(timezone.utc).isoformat()
        }

    def invalidate(self, content: str) -> None:
        """Remove cache entry for content."""
        key = self._make_key(content)
        if key in self.cache:
            del self.cache[key]

    def clear(self) -> None:
        """Clear all cache entries."""
        self.cache.clear()

    def cleanup_expired(self) -> int:
        """Remove all expired entries. Returns count removed."""
        expired_keys = []

        for key, cached in self.cache.items():
            cached_at = datetime.fromisoformat(cached.get("cached_at", ""))
            if datetime.now(timezone.utc) - cached_at.replace(tzinfo=timezone.utc) > self.ttl:
                expired_keys.append(key)

        for key in expired_keys:
            del self.cache[key]

        return len(expired_keys)

    def stats(self) -> Dict[str, Any]:
        """Get cache statistics."""
        self.cleanup_expired()

        return {
            "entries": len(self.cache),
            "ttl_hours": self.ttl.total_seconds() / 3600,
            "size_bytes": sum(
                len(str(v).encode()) for v in self.cache.values()
            )
        }
