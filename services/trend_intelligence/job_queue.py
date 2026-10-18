"""
Job Queue System for Trend Intelligence Pipeline
================================================
Simple in-memory queue with database persistence for reliability.
Uses asyncio for concurrent processing.

Queues:
- q_ingest: Pull new posts for workspace sources
- q_enrich: Fetch comments, transcripts, OCR
- q_embed: Create text embeddings
- q_cluster: Group posts into trends
- q_score: Compute velocity/engagement scores
- q_lingo: Extract phrases and meaning
- q_brief: Generate content briefs
- q_render: Render videos
- q_notify: Fire webhooks
"""
import os
import json
import asyncio
from datetime import datetime, timedelta
from typing import Dict, Any, Optional, List, Callable
from dataclasses import dataclass, field
from enum import Enum
import uuid

from loguru import logger
from sqlalchemy import create_engine, text


DATABASE_URL = os.getenv("DATABASE_URL", "postgresql://postgres:postgres@localhost:54322/postgres")


class JobStatus(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    RETRYING = "retrying"


class QueueType(str, Enum):
    INGEST = "q_ingest"
    ENRICH = "q_enrich"
    EMBED = "q_embed"
    CLUSTER = "q_cluster"
    SCORE = "q_score"
    LINGO = "q_lingo"
    BRIEF = "q_brief"
    RENDER = "q_render"
    NOTIFY = "q_notify"


@dataclass
class Job:
    """A job in the queue"""
    id: str = field(default_factory=lambda: str(uuid.uuid4()))
    queue: QueueType = QueueType.INGEST
    workspace_id: str = "00000000-0000-0000-0000-000000000001"
    payload: Dict[str, Any] = field(default_factory=dict)
    status: JobStatus = JobStatus.PENDING
    priority: int = 0
    attempts: int = 0
    max_attempts: int = 3
    error: Optional[str] = None
    result: Optional[Dict] = None
    created_at: datetime = field(default_factory=datetime.now)
    started_at: Optional[datetime] = None
    finished_at: Optional[datetime] = None
    
    def to_dict(self) -> Dict:
        return {
            "id": self.id,
            "queue": self.queue.value,
            "workspace_id": self.workspace_id,
            "payload": self.payload,
            "status": self.status.value,
            "priority": self.priority,
            "attempts": self.attempts,
            "max_attempts": self.max_attempts,
            "error": self.error,
            "result": self.result,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "started_at": self.started_at.isoformat() if self.started_at else None,
            "finished_at": self.finished_at.isoformat() if self.finished_at else None,
        }


class JobQueue:
    """
    Simple job queue with database persistence.
    
    Usage:
        queue = JobQueue()
        
        # Add a job
        job_id = await queue.enqueue(QueueType.INGEST, {"username": "example"})
        
        # Process jobs
        await queue.process_queue(QueueType.INGEST, handler_func)
    """
    
    def __init__(self):
        self.engine = create_engine(DATABASE_URL)
        self._handlers: Dict[QueueType, Callable] = {}
        self._running = False
        self._tasks: List[asyncio.Task] = []
        
        # Ensure jobs table exists
        self._ensure_table()
    
    def _ensure_table(self):
        """Create jobs table if it doesn't exist"""
        with self.engine.connect() as conn:
            conn.execute(text("""
                CREATE TABLE IF NOT EXISTS trend_jobs (
                    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
                    queue TEXT NOT NULL,
                    workspace_id UUID DEFAULT '00000000-0000-0000-0000-000000000001',
                    payload JSONB DEFAULT '{}',
                    status TEXT DEFAULT 'pending',
                    priority INTEGER DEFAULT 0,
                    attempts INTEGER DEFAULT 0,
                    max_attempts INTEGER DEFAULT 3,
                    error TEXT,
                    result JSONB,
                    created_at TIMESTAMPTZ DEFAULT NOW(),
                    started_at TIMESTAMPTZ,
                    finished_at TIMESTAMPTZ
                )
            """))
            conn.execute(text("""
                CREATE INDEX IF NOT EXISTS idx_trend_jobs_queue_status 
                ON trend_jobs(queue, status, priority DESC)
            """))
            conn.commit()
    
    async def enqueue(
        self,
        queue: QueueType,
        payload: Dict[str, Any],
        workspace_id: str = "00000000-0000-0000-0000-000000000001",
        priority: int = 0
    ) -> str:
        """Add a job to the queue"""
        job_id = str(uuid.uuid4())
        
        with self.engine.connect() as conn:
            conn.execute(text("""
                INSERT INTO trend_jobs (id, queue, workspace_id, payload, priority)
                VALUES (:id, :queue, :workspace_id, :payload, :priority)
            """), {
                "id": job_id,
                "queue": queue.value,
                "workspace_id": workspace_id,
                "payload": json.dumps(payload),
                "priority": priority,
            })
            conn.commit()
        
        logger.info(f"📥 Enqueued job {job_id[:8]} to {queue.value}")
        return job_id
    
    async def get_next_job(self, queue: QueueType) -> Optional[Job]:
        """Get the next pending job from a queue"""
        with self.engine.connect() as conn:
            # Atomically claim a job
            result = conn.execute(text("""
                UPDATE trend_jobs
                SET status = 'running', started_at = NOW(), attempts = attempts + 1
                WHERE id = (
                    SELECT id FROM trend_jobs
                    WHERE queue = :queue AND status = 'pending'
                    ORDER BY priority DESC, created_at ASC
                    LIMIT 1
                    FOR UPDATE SKIP LOCKED
                )
                RETURNING *
            """), {"queue": queue.value})
            
            row = result.fetchone()
            conn.commit()
            
            if row:
                data = dict(row._mapping)
                return Job(
                    id=str(data["id"]),
                    queue=QueueType(data["queue"]),
                    workspace_id=str(data["workspace_id"]),
                    payload=data["payload"] or {},
                    status=JobStatus(data["status"]),
                    priority=data["priority"],
                    attempts=data["attempts"],
                    max_attempts=data["max_attempts"],
                    error=data["error"],
                    result=data["result"],
                    created_at=data["created_at"],
                    started_at=data["started_at"],
                    finished_at=data["finished_at"],
                )
        
        return None
    
    async def complete_job(self, job_id: str, result: Dict = None):
        """Mark a job as completed"""
        with self.engine.connect() as conn:
            conn.execute(text("""
                UPDATE trend_jobs
                SET status = 'completed', finished_at = NOW(), result = :result
                WHERE id = :id
            """), {"id": job_id, "result": json.dumps(result or {})})
            conn.commit()
        
        logger.success(f"✅ Job {job_id[:8]} completed")
    
    async def fail_job(self, job_id: str, error: str):
        """Mark a job as failed or schedule for retry"""
        with self.engine.connect() as conn:
            # Check if should retry
            result = conn.execute(text("""
                SELECT attempts, max_attempts FROM trend_jobs WHERE id = :id
            """), {"id": job_id})
            row = result.fetchone()
            
            if row and row[0] < row[1]:
                # Retry
                conn.execute(text("""
                    UPDATE trend_jobs
                    SET status = 'pending', error = :error
                    WHERE id = :id
                """), {"id": job_id, "error": error})
                logger.warning(f"⚠️ Job {job_id[:8]} will retry (attempt {row[0]}/{row[1]})")
            else:
                # Final failure
                conn.execute(text("""
                    UPDATE trend_jobs
                    SET status = 'failed', finished_at = NOW(), error = :error
                    WHERE id = :id
                """), {"id": job_id, "error": error})
                logger.error(f"❌ Job {job_id[:8]} failed: {error[:50]}")
            
            conn.commit()
    
    def register_handler(self, queue: QueueType, handler: Callable):
        """Register a handler function for a queue"""
        self._handlers[queue] = handler
        logger.info(f"📌 Registered handler for {queue.value}")
    
    async def process_queue(self, queue: QueueType, handler: Callable = None):
        """Process jobs from a queue"""
        handler = handler or self._handlers.get(queue)
        if not handler:
            raise ValueError(f"No handler registered for {queue.value}")
        
        job = await self.get_next_job(queue)
        if not job:
            return None
        
        logger.info(f"🔄 Processing job {job.id[:8]} from {queue.value}")
        
        try:
            result = await handler(job.payload, job.workspace_id)
            await self.complete_job(job.id, result)
            return result
        except Exception as e:
            await self.fail_job(job.id, str(e))
            raise
    
    async def start_workers(self, queues: List[QueueType] = None, poll_interval: float = 5.0):
        """Start background workers for specified queues"""
        queues = queues or list(QueueType)
        self._running = True
        
        async def worker(queue: QueueType):
            while self._running:
                try:
                    result = await self.process_queue(queue)
                    if result is None:
                        # No jobs, wait before polling again
                        await asyncio.sleep(poll_interval)
                except Exception as e:
                    logger.error(f"Worker error ({queue.value}): {e}")
                    await asyncio.sleep(poll_interval)
        
        for queue in queues:
            if queue in self._handlers:
                task = asyncio.create_task(worker(queue))
                self._tasks.append(task)
                logger.info(f"🚀 Started worker for {queue.value}")
    
    async def stop_workers(self):
        """Stop all workers"""
        self._running = False
        for task in self._tasks:
            task.cancel()
        self._tasks.clear()
        logger.info("🛑 Workers stopped")
    
    async def get_queue_stats(self) -> Dict[str, Dict]:
        """Get statistics for all queues"""
        with self.engine.connect() as conn:
            result = conn.execute(text("""
                SELECT 
                    queue,
                    status,
                    COUNT(*) as count
                FROM trend_jobs
                GROUP BY queue, status
            """))
            
            stats = {}
            for row in result.fetchall():
                queue = row[0]
                status = row[1]
                count = row[2]
                
                if queue not in stats:
                    stats[queue] = {"pending": 0, "running": 0, "completed": 0, "failed": 0}
                stats[queue][status] = count
            
            return stats
    
    async def get_recent_jobs(
        self,
        queue: QueueType = None,
        status: JobStatus = None,
        limit: int = 20
    ) -> List[Dict]:
        """Get recent jobs"""
        with self.engine.connect() as conn:
            query = "SELECT * FROM trend_jobs WHERE 1=1"
            params = {"limit": limit}
            
            if queue:
                query += " AND queue = :queue"
                params["queue"] = queue.value
            
            if status:
                query += " AND status = :status"
                params["status"] = status.value
            
            query += " ORDER BY created_at DESC LIMIT :limit"
            
            result = conn.execute(text(query), params)
            return [dict(row._mapping) for row in result.fetchall()]


# Singleton
_job_queue = None

def get_job_queue() -> JobQueue:
    global _job_queue
    if _job_queue is None:
        _job_queue = JobQueue()
    return _job_queue
