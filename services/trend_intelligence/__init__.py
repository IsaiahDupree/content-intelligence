"""
Trend Intelligence System
=========================
Turn social media trends into actionable content briefs and rendered videos.

Pipeline:
1. Ingest → Normalize → Store (posts_raw)
2. Enrich → Embed → Cluster (trend_clusters)
3. Score → Lingo extraction (trend_scores, cluster_lingo)
4. Brief generation → Render (briefs, render_jobs)
"""

from .models import (
    PostRaw,
    TrendCluster,
    TrendScore,
    ClusterLingo,
    Brief,
    RenderJob,
    WorkspaceSource,
)
from .ingest_service import IngestService, get_ingest_service
from .cluster_service import ClusterService, get_cluster_service
from .brief_service import BriefService, get_brief_service
from .render_service import TrendRenderService, get_trend_render_service
from .job_queue import JobQueue, QueueType, JobStatus, get_job_queue
from .workers import (
    register_all_handlers,
    start_trend_workers,
    stop_trend_workers,
)
from .instagram_trends import (
    InstagramTrendsService,
    get_instagram_trends_service,
    Region,
    TrendingHashtag,
    TrendingSound,
    TrendingFormat,
    InstagramTrendsFeed
)

__all__ = [
    # Models
    "PostRaw",
    "TrendCluster",
    "TrendScore",
    "ClusterLingo",
    "Brief",
    "RenderJob",
    "WorkspaceSource",
    # Services
    "IngestService",
    "ClusterService",
    "BriefService",
    "TrendRenderService",
    "get_ingest_service",
    "get_cluster_service",
    "get_brief_service",
    "get_trend_render_service",
    # Instagram Trends
    "InstagramTrendsService",
    "get_instagram_trends_service",
    "Region",
    "TrendingHashtag",
    "TrendingSound",
    "TrendingFormat",
    "InstagramTrendsFeed",
    # Job Queue
    "JobQueue",
    "QueueType",
    "JobStatus",
    "get_job_queue",
    # Workers
    "register_all_handlers",
    "start_trend_workers",
    "stop_trend_workers",
]
