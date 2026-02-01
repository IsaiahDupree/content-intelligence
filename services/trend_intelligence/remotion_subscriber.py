"""
Remotion Event Subscriber
=========================
Listens for Remotion completion events and updates render job status.

Subscribes to:
- remotion.completed
- remotion.failed
"""
import os
import json
import asyncio
from datetime import datetime
from typing import Dict, Any, Optional

from loguru import logger
from sqlalchemy import create_engine, text


DATABASE_URL = os.getenv("DATABASE_URL", "postgresql://postgres:postgres@localhost:54322/postgres")


class RemotionSubscriber:
    """
    Subscribes to Remotion events and updates trend_intelligence render jobs.
    
    This bridges the Remotion worker's event-driven system with our render_jobs table.
    """
    
    def __init__(self):
        self.engine = create_engine(DATABASE_URL)
        self._running = False
    
    async def start(self):
        """Start subscribing to Remotion events"""
        try:
            from services.event_bus import EventBus, Topics
            
            event_bus = EventBus.get_instance()
            
            # Subscribe to completion events
            await event_bus.subscribe(
                Topics.REMOTION_COMPLETED,
                self._handle_completed
            )
            await event_bus.subscribe(
                Topics.REMOTION_FAILED,
                self._handle_failed
            )
            
            self._running = True
            logger.success("✅ RemotionSubscriber started - listening for completion events")
            
        except ImportError:
            logger.warning("Event bus not available - RemotionSubscriber not started")
        except Exception as e:
            logger.error(f"Failed to start RemotionSubscriber: {e}")
    
    async def stop(self):
        """Stop subscribing"""
        self._running = False
        logger.info("RemotionSubscriber stopped")
    
    async def _handle_completed(self, event) -> None:
        """Handle remotion.completed event"""
        payload = event.payload if hasattr(event, 'payload') else event
        
        job_id = payload.get("job_id")
        video_path = payload.get("video_path")
        video_url = payload.get("video_url")
        duration = payload.get("duration_seconds")
        file_size_mb = payload.get("file_size_mb")
        correlation_id = payload.get("correlation_id")
        
        logger.info(f"📥 Received remotion.completed for job {job_id}")
        
        # Update render job in database
        await self._update_render_job(
            job_id=job_id,
            status="succeeded",
            output={
                "video_path": video_path,
                "video_url": video_url or f"/api/v1/renders/output/{job_id}",
                "duration_sec": duration,
                "size_bytes": int(file_size_mb * 1024 * 1024) if file_size_mb else None,
            }
        )
    
    async def _handle_failed(self, event) -> None:
        """Handle remotion.failed event"""
        payload = event.payload if hasattr(event, 'payload') else event
        
        job_id = payload.get("job_id")
        error = payload.get("error", "Unknown error")
        
        logger.warning(f"📥 Received remotion.failed for job {job_id}: {error}")
        
        # Update render job in database
        await self._update_render_job(
            job_id=job_id,
            status="failed",
            error=error
        )
    
    async def _update_render_job(
        self,
        job_id: str,
        status: str,
        output: Dict = None,
        error: str = None
    ):
        """Update render job status in database"""
        if not job_id:
            return
        
        with self.engine.connect() as conn:
            if status == "succeeded" and output:
                conn.execute(text("""
                    UPDATE render_jobs
                    SET status = :status, 
                        output = :output,
                        finished_at = NOW()
                    WHERE id = :job_id OR input_payload->>'job_id' = :job_id
                """), {
                    "job_id": job_id,
                    "status": status,
                    "output": json.dumps(output),
                })
            elif status == "failed":
                conn.execute(text("""
                    UPDATE render_jobs
                    SET status = :status,
                        error = :error,
                        finished_at = NOW()
                    WHERE id = :job_id OR input_payload->>'job_id' = :job_id
                """), {
                    "job_id": job_id,
                    "status": status,
                    "error": error,
                })
            
            conn.commit()
            logger.info(f"✅ Updated render job {job_id} to {status}")


# Singleton
_subscriber = None

def get_remotion_subscriber() -> RemotionSubscriber:
    global _subscriber
    if _subscriber is None:
        _subscriber = RemotionSubscriber()
    return _subscriber


async def start_remotion_subscriber():
    """Start the Remotion event subscriber"""
    subscriber = get_remotion_subscriber()
    await subscriber.start()
