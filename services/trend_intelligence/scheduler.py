"""
Autonomous Trend Discovery Scheduler
=====================================
Runs background tasks to continuously discover trends:

1. Scheduled Ingestion - Pull posts from tracked sources every N hours
2. Auto-Clustering - Group new posts into trends after ingestion
3. Score Updates - Recompute trend scores periodically
4. Emerging Alerts - Detect and flag fast-rising trends
5. Source Discovery - Find new accounts/hashtags to track
"""
import os
import asyncio
from datetime import datetime, timedelta
from typing import List, Dict, Any, Optional
from dataclasses import dataclass, field

from loguru import logger
from sqlalchemy import create_engine, text

from .job_queue import get_job_queue, QueueType
from .workers import register_all_handlers


DATABASE_URL = os.getenv("DATABASE_URL", "postgresql://postgres:postgres@localhost:54322/postgres")


@dataclass
class ScheduleConfig:
    """Configuration for scheduled tasks"""
    ingest_interval_hours: float = 6.0  # How often to ingest from sources
    cluster_interval_hours: float = 2.0  # How often to run clustering
    score_interval_hours: float = 1.0   # How often to update scores
    alert_interval_minutes: float = 30.0  # How often to check for emerging trends
    min_posts_for_cluster: int = 2
    emerging_velocity_threshold: float = 0.5  # Velocity threshold for "emerging" status


class TrendScheduler:
    """
    DEPRECATED: Keyword-based scheduler.
    Use SeedlessCrawler instead for automatic trend discovery.
    
    This scheduler is disabled by default. The new approach uses
    random sampling from platform discovery surfaces to find trends
    without requiring seed keywords/accounts.
    """
    
    # Set to True to enable (disabled by default)
    ENABLED = False
    
    def __init__(self, config: ScheduleConfig = None):
        self.config = config or ScheduleConfig()
        self.engine = create_engine(DATABASE_URL)
        self._running = False
        self._tasks: List[asyncio.Task] = []
    
    async def start(self):
        """Start all scheduled tasks"""
        logger.info("🚀 Starting Trend Discovery Scheduler...")
        
        # Ensure job handlers are registered
        queue = get_job_queue()
        register_all_handlers(queue)
        
        self._running = True
        
        # Start background tasks
        self._tasks = [
            asyncio.create_task(self._ingest_loop()),
            asyncio.create_task(self._cluster_loop()),
            asyncio.create_task(self._score_loop()),
            asyncio.create_task(self._alert_loop()),
        ]
        
        logger.success("✅ Trend Scheduler started with 4 background tasks")
    
    async def stop(self):
        """Stop all scheduled tasks"""
        logger.info("🛑 Stopping Trend Scheduler...")
        self._running = False
        
        for task in self._tasks:
            task.cancel()
        
        self._tasks.clear()
        logger.info("Trend Scheduler stopped")
    
    # =========================================
    # Source Management
    # =========================================
    
    async def add_source(
        self,
        platform: str,
        source_type: str,  # 'account' or 'hashtag'
        identifier: str,   # username or hashtag
        workspace_id: str = "00000000-0000-0000-0000-000000000001"
    ) -> str:
        """Add a source to track"""
        with self.engine.connect() as conn:
            # Check if source already exists
            result = conn.execute(text("""
                SELECT id FROM workspace_sources 
                WHERE workspace_id = :workspace_id 
                  AND platform = :platform 
                  AND identifier = :identifier
            """), {
                "workspace_id": workspace_id,
                "platform": platform,
                "identifier": identifier,
            })
            existing = result.fetchone()
            
            if existing:
                # Update existing
                source_id = str(existing[0])
                conn.execute(text("""
                    UPDATE workspace_sources 
                    SET is_active = true, source_type = :source_type, updated_at = NOW()
                    WHERE id = :id
                """), {"id": source_id, "source_type": source_type})
            else:
                # Insert new
                result = conn.execute(text("""
                    INSERT INTO workspace_sources (workspace_id, platform, source_type, identifier, is_active)
                    VALUES (:workspace_id, :platform, :source_type, :identifier, true)
                    RETURNING id
                """), {
                    "workspace_id": workspace_id,
                    "platform": platform,
                    "source_type": source_type,
                    "identifier": identifier,
                })
                source_id = str(result.fetchone()[0])
            
            conn.commit()
        
        logger.info(f"📌 Added source: {platform}/{identifier}")
        return source_id
    
    async def remove_source(self, source_id: str):
        """Deactivate a source"""
        with self.engine.connect() as conn:
            conn.execute(text("""
                UPDATE workspace_sources SET is_active = false WHERE id = :id
            """), {"id": source_id})
            conn.commit()
    
    async def get_active_sources(
        self,
        workspace_id: str = "00000000-0000-0000-0000-000000000001"
    ) -> List[Dict]:
        """Get all active sources"""
        with self.engine.connect() as conn:
            result = conn.execute(text("""
                SELECT id, platform, source_type, identifier, last_fetched_at
                FROM workspace_sources
                WHERE workspace_id = :workspace_id AND is_active = true
                ORDER BY platform, identifier
            """), {"workspace_id": workspace_id})
            
            return [dict(row._mapping) for row in result.fetchall()]
    
    # =========================================
    # Scheduled Loops
    # =========================================
    
    async def _ingest_loop(self):
        """Periodically ingest from all active sources"""
        interval = self.config.ingest_interval_hours * 3600
        
        while self._running:
            try:
                await self._run_ingestion()
            except Exception as e:
                logger.error(f"Ingest loop error: {e}")
            
            await asyncio.sleep(interval)
    
    async def _cluster_loop(self):
        """Periodically run clustering"""
        interval = self.config.cluster_interval_hours * 3600
        
        # Wait a bit before first run to let ingestion happen first
        await asyncio.sleep(60)
        
        while self._running:
            try:
                await self._run_clustering()
            except Exception as e:
                logger.error(f"Cluster loop error: {e}")
            
            await asyncio.sleep(interval)
    
    async def _score_loop(self):
        """Periodically update trend scores"""
        interval = self.config.score_interval_hours * 3600
        
        # Wait for clustering to run first
        await asyncio.sleep(120)
        
        while self._running:
            try:
                await self._run_scoring()
            except Exception as e:
                logger.error(f"Score loop error: {e}")
            
            await asyncio.sleep(interval)
    
    async def _alert_loop(self):
        """Check for emerging trends and send alerts"""
        interval = self.config.alert_interval_minutes * 60
        
        # Wait for scoring to run first
        await asyncio.sleep(180)
        
        while self._running:
            try:
                await self._check_emerging_trends()
            except Exception as e:
                logger.error(f"Alert loop error: {e}")
            
            await asyncio.sleep(interval)
    
    # =========================================
    # Pipeline Execution
    # =========================================
    
    async def _run_ingestion(self):
        """Ingest from all active sources"""
        logger.info("📥 Running scheduled ingestion...")
        
        sources = await self.get_active_sources()
        if not sources:
            logger.info("No active sources to ingest from")
            return
        
        queue = get_job_queue()
        jobs_queued = 0
        
        for source in sources:
            platform = source["platform"]
            identifier = source["identifier"]
            source_type = source["source_type"]
            
            # Queue ingest job
            payload = {
                "platform": platform,
                "count": 20,
            }
            
            if source_type == "account":
                payload["username"] = identifier
            else:
                payload["hashtag"] = identifier
            
            await queue.enqueue(QueueType.INGEST, payload, priority=5)
            jobs_queued += 1
            
            # Update last_fetched_at
            with self.engine.connect() as conn:
                conn.execute(text("""
                    UPDATE workspace_sources 
                    SET last_fetched_at = NOW() 
                    WHERE id = :id
                """), {"id": source["id"]})
                conn.commit()
        
        logger.success(f"✅ Queued {jobs_queued} ingestion jobs")
        
        # Process jobs immediately
        for _ in range(jobs_queued):
            try:
                await queue.process_queue(QueueType.INGEST)
            except Exception as e:
                logger.error(f"Ingest job failed: {e}")
    
    async def _run_clustering(self):
        """Run clustering on recent posts"""
        logger.info("🔍 Running scheduled clustering...")
        
        queue = get_job_queue()
        
        await queue.enqueue(
            QueueType.CLUSTER,
            {"min_posts": self.config.min_posts_for_cluster},
            priority=5
        )
        
        try:
            result = await queue.process_queue(QueueType.CLUSTER)
            logger.success(f"✅ Clustering complete: {result}")
        except Exception as e:
            logger.error(f"Clustering failed: {e}")
    
    async def _run_scoring(self):
        """Update trend scores"""
        logger.info("📊 Running scheduled scoring...")
        
        queue = get_job_queue()
        
        await queue.enqueue(
            QueueType.SCORE,
            {"time_windows": ["1h", "6h", "24h"]},
            priority=5
        )
        
        try:
            result = await queue.process_queue(QueueType.SCORE)
            logger.success(f"✅ Scoring complete: {result}")
        except Exception as e:
            logger.error(f"Scoring failed: {e}")
    
    async def _check_emerging_trends(self):
        """Detect emerging trends and create alerts"""
        logger.info("🔔 Checking for emerging trends...")
        
        with self.engine.connect() as conn:
            # Find high-velocity trends
            result = conn.execute(text("""
                SELECT 
                    tc.id, tc.title, ts.velocity, ts.score,
                    tc.status, tc.created_at
                FROM trend_clusters tc
                JOIN trend_scores ts ON tc.id = ts.cluster_id
                WHERE ts.time_window = '24h'
                  AND ts.velocity >= :threshold
                  AND tc.created_at >= NOW() - INTERVAL '48 hours'
                ORDER BY ts.velocity DESC
                LIMIT 10
            """), {"threshold": self.config.emerging_velocity_threshold})
            
            emerging = result.fetchall()
            
            if emerging:
                logger.info(f"🔥 Found {len(emerging)} emerging trends!")
                
                for trend in emerging:
                    trend_id = str(trend[0])
                    title = trend[1]
                    velocity = trend[2]
                    current_status = trend[4]
                    
                    # Update status to "emerging" if not already
                    if current_status != "emerging":
                        conn.execute(text("""
                            UPDATE trend_clusters 
                            SET status = 'emerging', updated_at = NOW()
                            WHERE id = :id
                        """), {"id": trend_id})
                    
                    # Queue lingo extraction for top emerging trends
                    queue = get_job_queue()
                    await queue.enqueue(
                        QueueType.LINGO,
                        {"cluster_id": trend_id},
                        priority=8
                    )
                    
                    logger.info(f"  🔥 {title} (velocity: {velocity:.2f})")
                
                conn.commit()
            else:
                logger.info("No emerging trends detected")
    
    # =========================================
    # Manual Triggers
    # =========================================
    
    async def run_full_pipeline(self):
        """Manually trigger full pipeline"""
        logger.info("🚀 Running full pipeline manually...")
        
        await self._run_ingestion()
        await asyncio.sleep(2)
        
        await self._run_clustering()
        await asyncio.sleep(1)
        
        await self._run_scoring()
        await asyncio.sleep(1)
        
        await self._check_emerging_trends()
        
        logger.success("✅ Full pipeline complete")
    
    async def discover_related_sources(
        self,
        seed_username: str,
        platform: str = "tiktok"
    ) -> List[str]:
        """
        Discover related accounts to track based on a seed account.
        Uses the accounts that appear in comments/duets/mentions.
        """
        logger.info(f"🔍 Discovering sources related to @{seed_username}...")
        
        # For now, return some popular creator accounts
        # In production, this would analyze mentions, duets, and collaborations
        suggested = []
        
        with self.engine.connect() as conn:
            # Find accounts that share hashtags with the seed
            result = conn.execute(text("""
                SELECT DISTINCT author_handle
                FROM posts_raw
                WHERE platform = :platform
                  AND author_handle != :seed
                  AND hashtags && (
                      SELECT hashtags FROM posts_raw 
                      WHERE author_handle = :seed 
                      LIMIT 1
                  )
                LIMIT 10
            """), {"platform": platform, "seed": seed_username})
            
            suggested = [row[0] for row in result.fetchall() if row[0]]
        
        return suggested


# Singleton
_scheduler = None

def get_scheduler() -> TrendScheduler:
    global _scheduler
    if _scheduler is None:
        _scheduler = TrendScheduler()
    return _scheduler


async def start_autonomous_discovery():
    """Start the autonomous trend discovery system"""
    scheduler = get_scheduler()
    await scheduler.start()


async def stop_autonomous_discovery():
    """Stop the autonomous trend discovery system"""
    scheduler = get_scheduler()
    await scheduler.stop()
