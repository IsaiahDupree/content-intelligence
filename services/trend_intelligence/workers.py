"""
Trend Intelligence Workers
===========================
Background job handlers for the trend pipeline.

Each worker handles a specific queue:
- ingest_worker: Pull posts from platforms
- enrich_worker: Add transcripts, comments, OCR
- cluster_worker: Group posts into trends
- score_worker: Compute velocity and engagement
- lingo_worker: Extract phrases and meaning
- brief_worker: Generate content briefs
- render_worker: Render videos
- notify_worker: Fire webhooks
"""
import os
import json
import asyncio
from datetime import datetime, timedelta
from typing import Dict, Any, Optional, List

from loguru import logger

from .job_queue import JobQueue, QueueType, get_job_queue
from .ingest_service import get_ingest_service
from .cluster_service import get_cluster_service
from .brief_service import get_brief_service
from .render_service import get_trend_render_service
from .models import Platform, WorkspaceSource


# =========================================
# INGEST WORKER
# =========================================

async def ingest_worker(payload: Dict[str, Any], workspace_id: str) -> Dict:
    """
    Pull posts from a platform source.
    
    Payload:
        - platform: str (instagram, tiktok)
        - username: str (optional)
        - hashtag: str (optional)
        - count: int (default 12)
    """
    logger.info(f"🔄 [IngestWorker] Starting ingestion")
    
    service = get_ingest_service()
    platform = payload.get("platform", "instagram")
    username = payload.get("username")
    hashtag = payload.get("hashtag")
    count = payload.get("count", 12)
    
    posts = []
    
    if platform == "instagram":
        if username:
            posts = await service.ingest_instagram_user(
                username=username,
                workspace_id=workspace_id,
                count=count
            )
        elif hashtag:
            posts = await service.ingest_instagram_hashtag(
                hashtag=hashtag,
                workspace_id=workspace_id,
                count=count
            )
    
    # Queue next step: clustering
    if posts:
        queue = get_job_queue()
        await queue.enqueue(
            QueueType.CLUSTER,
            {"trigger": "ingest_complete", "posts_count": len(posts)},
            workspace_id=workspace_id
        )
    
    return {
        "platform": platform,
        "posts_ingested": len(posts),
        "source": username or hashtag
    }


# =========================================
# ENRICH WORKER
# =========================================

async def enrich_worker(payload: Dict[str, Any], workspace_id: str) -> Dict:
    """
    Enrich posts with additional data.
    
    Payload:
        - post_ids: List[str] (optional, enriches all recent if not provided)
        - fetch_comments: bool
        - fetch_transcript: bool
        - fetch_ocr: bool
    """
    logger.info(f"🔄 [EnrichWorker] Starting enrichment")
    
    # TODO: Implement enrichment (comments, transcripts, OCR)
    # This would call RapidAPI for comments, Whisper for transcripts, etc.
    
    return {
        "enriched": 0,
        "message": "Enrichment worker placeholder"
    }


# =========================================
# EMBED WORKER
# =========================================

async def embed_worker(payload: Dict[str, Any], workspace_id: str) -> Dict:
    """
    Create embeddings for text content.
    
    Payload:
        - source_type: str (caption, comment, transcript)
        - source_ids: List[str] (optional)
    """
    logger.info(f"🔄 [EmbedWorker] Creating embeddings")
    
    # TODO: Implement embedding generation with OpenAI
    # This would create vector embeddings for semantic search
    
    return {
        "embedded": 0,
        "message": "Embed worker placeholder"
    }


# =========================================
# CLUSTER WORKER
# =========================================

async def cluster_worker(payload: Dict[str, Any], workspace_id: str) -> Dict:
    """
    Group posts into trend clusters.
    
    Payload:
        - min_posts: int (minimum posts per cluster)
        - cluster_types: List[str] (hashtag, audio, topic)
    """
    logger.info(f"🔄 [ClusterWorker] Clustering posts")
    
    service = get_cluster_service()
    min_posts = payload.get("min_posts", 2)
    cluster_types = payload.get("cluster_types", ["hashtag", "audio"])
    
    results = {
        "hashtag_clusters": 0,
        "audio_clusters": 0,
    }
    
    if "hashtag" in cluster_types:
        clusters = await service.cluster_by_hashtag(
            workspace_id=workspace_id,
            min_posts=min_posts
        )
        results["hashtag_clusters"] = len(clusters)
    
    if "audio" in cluster_types:
        clusters = await service.cluster_by_audio(
            workspace_id=workspace_id,
            min_posts=min_posts
        )
        results["audio_clusters"] = len(clusters)
    
    # Queue next step: scoring
    total_clusters = results["hashtag_clusters"] + results["audio_clusters"]
    if total_clusters > 0:
        queue = get_job_queue()
        await queue.enqueue(
            QueueType.SCORE,
            {"trigger": "cluster_complete", "clusters_count": total_clusters},
            workspace_id=workspace_id
        )
    
    return results


# =========================================
# SCORE WORKER
# =========================================

async def score_worker(payload: Dict[str, Any], workspace_id: str) -> Dict:
    """
    Compute velocity and engagement scores for clusters.
    
    Payload:
        - time_windows: List[str] (1h, 6h, 24h, 7d)
        - cluster_ids: List[str] (optional, scores all if not provided)
    """
    logger.info(f"🔄 [ScoreWorker] Computing scores")
    
    service = get_cluster_service()
    time_windows = payload.get("time_windows", ["24h"])
    
    total_scores = 0
    for window in time_windows:
        scores = await service.compute_scores(
            workspace_id=workspace_id,
            time_window=window
        )
        total_scores += len(scores)
    
    # Queue next step: lingo extraction for top clusters
    if total_scores > 0:
        queue = get_job_queue()
        await queue.enqueue(
            QueueType.LINGO,
            {"trigger": "score_complete", "top_n": 10},
            workspace_id=workspace_id
        )
    
    return {
        "scores_computed": total_scores,
        "windows": time_windows
    }


# =========================================
# LINGO WORKER
# =========================================

async def lingo_worker(payload: Dict[str, Any], workspace_id: str) -> Dict:
    """
    Extract language patterns from top clusters.
    
    Payload:
        - cluster_ids: List[str] (optional)
        - top_n: int (extract for top N clusters by score)
    """
    logger.info(f"🔄 [LingoWorker] Extracting lingo")
    
    service = get_cluster_service()
    cluster_ids = payload.get("cluster_ids", [])
    top_n = payload.get("top_n", 10)
    
    # If no specific clusters, get top by score
    if not cluster_ids:
        trends = await service.get_trending(
            workspace_id=workspace_id,
            limit=top_n
        )
        cluster_ids = [str(t.get("id")) for t in trends if t.get("id")]
    
    extracted = 0
    for cluster_id in cluster_ids:
        lingo = await service.extract_lingo(cluster_id)
        if lingo:
            extracted += 1
        
        # Small delay to avoid rate limits
        await asyncio.sleep(0.5)
    
    return {
        "lingo_extracted": extracted,
        "clusters_processed": len(cluster_ids)
    }


# =========================================
# BRIEF WORKER
# =========================================

async def brief_worker(payload: Dict[str, Any], workspace_id: str) -> Dict:
    """
    Generate content briefs from clusters.
    
    Payload:
        - cluster_id: str
        - platform_target: str
        - tone: Dict
        - brand_safe_mode: bool
    """
    logger.info(f"🔄 [BriefWorker] Generating brief")
    
    service = get_brief_service()
    
    cluster_id = payload.get("cluster_id")
    if not cluster_id:
        return {"error": "cluster_id required"}
    
    brief = await service.generate_brief(
        cluster_id=cluster_id,
        platform_target=payload.get("platform_target", "tiktok"),
        tone=payload.get("tone", {"style": "engaging"}),
        brand_safe_mode=payload.get("brand_safe_mode", True),
        workspace_id=workspace_id
    )
    
    if brief:
        # Optionally queue render job
        if payload.get("auto_render"):
            queue = get_job_queue()
            await queue.enqueue(
                QueueType.RENDER,
                {
                    "brief_id": brief.id,
                    "format_template_id": payload.get("format_template_id", "00000000-0000-0000-0000-000000000101")
                },
                workspace_id=workspace_id
            )
        
        return {
            "brief_id": brief.id,
            "title": brief.title,
            "hooks_count": len(brief.hooks)
        }
    
    return {"error": "Failed to generate brief"}


# =========================================
# RENDER WORKER
# =========================================

async def render_worker(payload: Dict[str, Any], workspace_id: str) -> Dict:
    """
    Render a video from a brief.
    
    Payload:
        - brief_id: str
        - format_template_id: str
        - overrides: Dict
    """
    logger.info(f"🔄 [RenderWorker] Rendering video")
    
    service = get_trend_render_service()
    
    brief_id = payload.get("brief_id")
    format_template_id = payload.get("format_template_id")
    
    if not brief_id or not format_template_id:
        return {"error": "brief_id and format_template_id required"}
    
    # Create render job
    job = await service.create_render_job(
        brief_id=brief_id,
        format_template_id=format_template_id,
        overrides=payload.get("overrides", {}),
        workspace_id=workspace_id
    )
    
    if not job:
        return {"error": "Failed to create render job"}
    
    # Execute render
    try:
        await service.execute_render(job.id)
        
        # Queue notification
        queue = get_job_queue()
        await queue.enqueue(
            QueueType.NOTIFY,
            {
                "event": "render.done",
                "render_job_id": job.id,
                "brief_id": brief_id
            },
            workspace_id=workspace_id
        )
        
        return {
            "render_job_id": job.id,
            "status": "completed"
        }
    except Exception as e:
        return {
            "render_job_id": job.id,
            "status": "failed",
            "error": str(e)
        }


# =========================================
# NOTIFY WORKER
# =========================================

async def notify_worker(payload: Dict[str, Any], workspace_id: str) -> Dict:
    """
    Fire webhooks and notifications.
    
    Payload:
        - event: str (trend.emerging, brief.ready, render.done)
        - data: Dict (event-specific data)
    """
    logger.info(f"🔄 [NotifyWorker] Sending notifications")
    
    event = payload.get("event")
    
    # TODO: Implement webhook firing
    # This would query webhook_subscriptions and POST to target_urls
    
    return {
        "event": event,
        "notifications_sent": 0,
        "message": "Notify worker placeholder"
    }


# =========================================
# FULL PIPELINE WORKER
# =========================================

async def full_pipeline_worker(payload: Dict[str, Any], workspace_id: str) -> Dict:
    """
    Run the full trend intelligence pipeline.
    
    Payload:
        - sources: List of {platform, username/hashtag}
        - generate_briefs: bool
        - auto_render: bool
    """
    logger.info(f"🚀 [FullPipeline] Starting full pipeline")
    
    queue = get_job_queue()
    sources = payload.get("sources", [])
    
    # Queue ingest jobs for each source
    job_ids = []
    for source in sources:
        job_id = await queue.enqueue(
            QueueType.INGEST,
            source,
            workspace_id=workspace_id,
            priority=10  # Higher priority
        )
        job_ids.append(job_id)
    
    return {
        "pipeline": "started",
        "ingest_jobs": len(job_ids),
        "job_ids": job_ids
    }


# =========================================
# REGISTER ALL HANDLERS
# =========================================

def register_all_handlers(queue: JobQueue = None):
    """Register all worker handlers with the job queue"""
    queue = queue or get_job_queue()
    
    queue.register_handler(QueueType.INGEST, ingest_worker)
    queue.register_handler(QueueType.ENRICH, enrich_worker)
    queue.register_handler(QueueType.EMBED, embed_worker)
    queue.register_handler(QueueType.CLUSTER, cluster_worker)
    queue.register_handler(QueueType.SCORE, score_worker)
    queue.register_handler(QueueType.LINGO, lingo_worker)
    queue.register_handler(QueueType.BRIEF, brief_worker)
    queue.register_handler(QueueType.RENDER, render_worker)
    queue.register_handler(QueueType.NOTIFY, notify_worker)
    
    logger.success("✅ All trend intelligence workers registered")


# =========================================
# STARTUP
# =========================================

async def start_trend_workers():
    """Start all trend intelligence workers"""
    queue = get_job_queue()
    register_all_handlers(queue)
    
    # Start workers for main queues
    await queue.start_workers([
        QueueType.INGEST,
        QueueType.CLUSTER,
        QueueType.SCORE,
        QueueType.LINGO,
        QueueType.BRIEF,
        QueueType.RENDER,
        QueueType.NOTIFY,
    ])
    
    logger.success("🚀 Trend intelligence workers started")


async def stop_trend_workers():
    """Stop all trend intelligence workers"""
    queue = get_job_queue()
    await queue.stop_workers()
