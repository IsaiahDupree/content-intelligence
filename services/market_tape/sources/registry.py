"""Construct all provider lanes; blocked lanes remain observable and re-testable."""

from __future__ import annotations

from typing import List

from .base import MarketSource
from .local_research import LocalResearchSource
from .social import (
    InstagramRapidSource,
    MetaGraphSource,
    ThreadsKeywordSearchSource,
    TikTokRapidSource,
    TikTokResearchSource,
    XRecentSearchSource,
)
from .youtube import YouTubeSource
from ..config import MarketTapeConfig


def build_sources(config: MarketTapeConfig, run_id: str, budget_for) -> List[MarketSource]:
    def budget(platform: str, source_id: str) -> int:
        return budget_for(source_id, config.request_limit_for(platform))

    local_sources: List[MarketSource] = [
        LocalResearchSource(
            config, run_id, budget(platform, f"safari-local-research-{platform}"),
            platform=platform, api_platform="twitter" if platform == "x" else platform,
        )
        for platform in ("tiktok", "instagram", "x", "facebook", "threads")
    ]
    sources: List[MarketSource] = [
        YouTubeSource(config, run_id, budget("youtube", YouTubeSource.source_id)),
        *local_sources,
        TikTokResearchSource(config, run_id, budget("tiktok", TikTokResearchSource.source_id)),
        TikTokRapidSource(config, run_id, budget("tiktok", TikTokRapidSource.source_id)),
        InstagramRapidSource(config, run_id, budget("instagram", InstagramRapidSource.source_id)),
        XRecentSearchSource(config, run_id, budget("x", XRecentSearchSource.source_id)),
        MetaGraphSource(
            config, run_id, budget("instagram", "instagram-graph-authorized"),
            platform="instagram", account_env="INSTAGRAM_BUSINESS_ACCOUNT_ID",
            token_envs=("INSTAGRAM_ACCESS_TOKEN", "META_ACCESS_TOKEN"),
            source_id="instagram-graph-authorized", edge="media",
            fields="id,caption,media_type,permalink,timestamp,username,thumbnail_url,like_count,comments_count",
        ),
        MetaGraphSource(
            config, run_id, budget("facebook", "facebook-graph-authorized"),
            platform="facebook", account_env="FACEBOOK_PAGE_ID",
            token_envs=("FACEBOOK_ACCESS_TOKEN", "META_ACCESS_TOKEN"),
            source_id="facebook-graph-authorized", edge="videos",
            fields="id,title,description,created_time,permalink_url,views,likes.summary(true),comments.summary(true)",
        ),
        ThreadsKeywordSearchSource(
            config,
            run_id,
            budget("threads", ThreadsKeywordSearchSource.source_id),
        ),
    ]
    return sources
