"""
Instagram Services
TrendTok-style Instagram analytics and trend discovery
"""

from .adapters import (
    InstagramAdapter,
    RapidApiInstagramAdapter,
    Profile,
    MediaItem,
    MediaPage,
    HashtagData,
    SearchResults
)

__all__ = [
    'InstagramAdapter',
    'RapidApiInstagramAdapter',
    'Profile',
    'MediaItem',
    'MediaPage',
    'HashtagData',
    'SearchResults',
]
