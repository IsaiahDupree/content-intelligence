"""
Render Service - Generate videos from briefs using Remotion/FFmpeg
===================================================================
Integrates with the existing Remotion event bus system.

Rendering Flow:
1. Create render job from brief + format template
2. Build Remotion-compatible payload
3. Publish to remotion.requested event
4. Listen for remotion.completed/failed events
5. Update render job status
"""
import os
import json
import uuid
from datetime import datetime
from typing import Dict, Any, Optional, List
from pathlib import Path

import httpx
from loguru import logger
from sqlalchemy import create_engine, text

from .models import RenderJob, RenderStatus


DATABASE_URL = os.getenv("DATABASE_URL", "postgresql://postgres:postgres@localhost:54322/postgres")
REMOTION_URL = os.getenv("REMOTION_URL", "http://localhost:8686")


class TrendRenderService:
    """
    Service for rendering videos from content briefs.
    
    Supports:
    - Remotion (primary)
    - FFmpeg (fallback for simple compositions)
    """
    
    def __init__(self):
        self.engine = create_engine(DATABASE_URL)
        self.output_dir = Path("data/trend_renders")
        self.output_dir.mkdir(parents=True, exist_ok=True)
    
    async def create_render_job(
        self,
        brief_id: str,
        format_template_id: str,
        overrides: Dict[str, Any] = None,
        workspace_id: str = "00000000-0000-0000-0000-000000000001"
    ) -> Optional[RenderJob]:
        """Create a render job from a brief"""
        logger.info(f"🎬 Creating render job for brief {brief_id}")
        
        # Get brief
        brief = await self._get_brief(brief_id)
        if not brief:
            logger.error(f"Brief {brief_id} not found")
            return None
        
        # Get format template
        template = await self._get_template(format_template_id)
        if not template:
            logger.error(f"Template {format_template_id} not found")
            return None
        
        # Build input payload
        input_payload = self._build_input_payload(brief, template, overrides or {})
        
        # Create job
        job = RenderJob(
            workspace_id=workspace_id,
            brief_id=brief_id,
            format_template_id=format_template_id,
            engine=template.get("engine", "remotion"),
            status=RenderStatus.QUEUED,
            input_payload=input_payload,
            created_at=datetime.now(),
        )
        
        # Save to database
        job.id = await self._save_job(job)
        
        logger.success(f"✅ Created render job: {job.id}")
        return job
    
    async def _get_brief(self, brief_id: str) -> Optional[Dict]:
        """Get brief from database"""
        with self.engine.connect() as conn:
            result = conn.execute(text("""
                SELECT * FROM briefs WHERE id = :brief_id
            """), {"brief_id": brief_id})
            row = result.fetchone()
            if row:
                return dict(row._mapping)
        return None
    
    async def _get_template(self, template_id: str) -> Optional[Dict]:
        """Get format template from database"""
        with self.engine.connect() as conn:
            result = conn.execute(text("""
                SELECT * FROM format_templates WHERE id = :template_id
            """), {"template_id": template_id})
            row = result.fetchone()
            if row:
                return dict(row._mapping)
        return None
    
    def _build_input_payload(
        self,
        brief: Dict,
        template: Dict,
        overrides: Dict
    ) -> Dict[str, Any]:
        """Build the input payload for the render job"""
        
        # Parse brief fields
        hooks = brief.get("hooks", [])
        script = brief.get("script_outline", {})
        shotlist = brief.get("shotlist", [{}])
        cta = brief.get("cta", {})
        
        # Default settings from template
        default_settings = template.get("default_settings", {})
        
        payload = {
            "title": hooks[0] if hooks else brief.get("title", ""),
            "script": script,
            "on_screen_text": shotlist[0].get("on_screen_text", []) if shotlist else [],
            "broll": shotlist[0].get("broll", []) if shotlist else [],
            "cta_text": cta.get("primary", ""),
            "settings": {
                "duration_sec": overrides.get("duration_sec", default_settings.get("duration_sec", 22)),
                "fps": overrides.get("fps", default_settings.get("fps", 30)),
                "resolution": overrides.get("resolution", default_settings.get("resolution", "1080x1920")),
                "aspect": overrides.get("aspect", "9:16"),
            }
        }
        
        # Apply any additional overrides
        for key, value in overrides.items():
            if key not in ["duration_sec", "fps", "resolution", "aspect"]:
                payload[key] = value
        
        return payload
    
    async def _save_job(self, job: RenderJob) -> str:
        """Save render job to database"""
        with self.engine.connect() as conn:
            result = conn.execute(text("""
                INSERT INTO render_jobs (
                    workspace_id, brief_id, format_template_id,
                    engine, status, input_payload
                ) VALUES (
                    :workspace_id, :brief_id, :format_template_id,
                    :engine, :status, :input_payload
                )
                RETURNING id
            """), {
                "workspace_id": job.workspace_id,
                "brief_id": job.brief_id,
                "format_template_id": job.format_template_id,
                "engine": job.engine,
                "status": job.status.value,
                "input_payload": json.dumps(job.input_payload),
            })
            
            job_id = str(result.fetchone()[0])
            conn.commit()
            return job_id
    
    async def execute_render(self, job_id: str) -> RenderJob:
        """Execute a render job"""
        logger.info(f"🎬 Executing render job {job_id}")
        
        # Get job
        job = await self.get_job(job_id)
        if not job:
            raise ValueError(f"Job {job_id} not found")
        
        # Update status
        await self._update_job_status(job_id, RenderStatus.RUNNING)
        
        try:
            engine = job.get("engine", "remotion")
            
            if engine == "remotion":
                result = await self._render_with_remotion(job)
            elif engine == "ffmpeg":
                result = await self._render_with_ffmpeg(job)
            else:
                raise ValueError(f"Unknown engine: {engine}")
            
            # Update job with result
            await self._update_job_result(job_id, result)
            
            logger.success(f"✅ Render complete: {result.get('video_url')}")
            return await self.get_job(job_id)
            
        except Exception as e:
            logger.error(f"Render failed: {e}")
            await self._update_job_error(job_id, str(e))
            raise
    
    async def _render_with_remotion(self, job: Dict) -> Dict:
        """
        Render using Remotion service via event bus.
        
        Uses the existing Remotion worker which subscribes to remotion.requested events.
        """
        job_id = str(job.get("id"))
        input_payload = job.get("input_payload", {})
        
        # Try event bus first (preferred)
        try:
            from services.event_bus import EventBus, Topics
            event_bus = EventBus.get_instance()
            
            # Build Remotion-compatible payload
            remotion_payload = self._build_remotion_payload(job_id, input_payload)
            
            # Publish to event bus
            correlation_id = str(uuid.uuid4())
            await event_bus.publish(
                topic=Topics.REMOTION_REQUESTED,
                payload=remotion_payload,
                correlation_id=correlation_id,
                source="trend_intelligence"
            )
            
            logger.info(f"📤 Published remotion.requested event for job {job_id}")
            
            # Return queued status - actual result comes via callback
            return {
                "status": "queued",
                "video_url": f"/api/trend-render/output/{job_id}",
                "correlation_id": correlation_id,
            }
            
        except ImportError:
            logger.warning("Event bus not available, falling back to HTTP")
        except Exception as e:
            logger.warning(f"Event bus failed: {e}, falling back to HTTP")
        
        # Fallback: Direct HTTP call to Remotion API
        async with httpx.AsyncClient(timeout=300.0) as client:
            try:
                response = await client.post(
                    f"http://localhost:5555/api/remotion/render",
                    json=self._build_remotion_payload(job_id, input_payload)
                )
                
                if response.status_code == 200:
                    data = response.json()
                    return {
                        "video_url": f"/api/trend-render/output/{job_id}",
                        "job_id": data.get("job_id"),
                        "status": data.get("status", "queued"),
                    }
                else:
                    raise Exception(f"Remotion API error: {response.text}")
            except httpx.ConnectError:
                logger.warning("Remotion service not available, using FFmpeg fallback")
                return await self._render_with_ffmpeg(job)
    
    def _build_remotion_payload(self, job_id: str, input_payload: Dict) -> Dict:
        """Build Remotion-compatible payload from brief input"""
        title = input_payload.get("title", "")
        script = input_payload.get("script", {})
        on_screen_text = input_payload.get("on_screen_text", [])
        broll = input_payload.get("broll", [])
        settings = input_payload.get("settings", {})
        
        # Build layers for Remotion
        layers = []
        
        # Text layer for title/hook
        if title:
            layers.append({
                "id": "title_layer",
                "type": "text",
                "content": title,
                "start": 0.0,
                "end": 3.0,
                "style": {
                    "fontSize": 48,
                    "fontWeight": "bold",
                    "color": "#FFFFFF",
                    "textAlign": "center"
                },
                "position": {"x": "50%", "y": "50%"},
                "animation": "fadeIn"
            })
        
        # On-screen text layers
        for i, text in enumerate(on_screen_text[:5]):
            start_time = 3.0 + (i * 4.0)
            layers.append({
                "id": f"text_layer_{i}",
                "type": "text",
                "content": text,
                "start": start_time,
                "end": start_time + 4.0,
                "style": {
                    "fontSize": 36,
                    "color": "#FFFFFF",
                    "textAlign": "center"
                },
                "position": {"x": "50%", "y": "80%"},
                "animation": "slideUp"
            })
        
        return {
            "job_id": job_id,
            "composition": "MainComposition",
            "layers": layers,
            "output": {
                "format": "mp4",
                "resolution": settings.get("resolution", "1080x1920"),
                "fps": settings.get("fps", 30)
            },
            "props": {
                "title": title,
                "script": script,
                "duration": settings.get("duration_sec", 22),
            }
        }
    
    async def _render_with_ffmpeg(self, job: Dict) -> Dict:
        """Render using FFmpeg (simple compositions)"""
        import subprocess
        
        job_id = job.get("id")
        input_payload = job.get("input_payload", {})
        
        output_path = self.output_dir / f"{job_id}.mp4"
        
        # Build FFmpeg command for text overlay on solid background
        title = input_payload.get("title", "")
        settings = input_payload.get("settings", {})
        duration = settings.get("duration_sec", 10)
        resolution = settings.get("resolution", "1080x1920").split("x")
        
        width = int(resolution[0])
        height = int(resolution[1])
        
        # Create video with text overlay
        cmd = [
            "ffmpeg", "-y",
            "-f", "lavfi",
            "-i", f"color=c=black:s={width}x{height}:d={duration}",
            "-vf", f"drawtext=text='{title[:50]}':fontsize=48:fontcolor=white:x=(w-text_w)/2:y=(h-text_h)/2:fontfile=/System/Library/Fonts/Helvetica.ttc",
            "-c:v", "libx264",
            "-pix_fmt", "yuv420p",
            str(output_path)
        ]
        
        process = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
        
        if process.returncode != 0:
            raise Exception(f"FFmpeg error: {process.stderr[:200]}")
        
        file_size = output_path.stat().st_size if output_path.exists() else 0
        
        return {
            "video_url": f"/api/trend-render/output/{job_id}",
            "video_path": str(output_path),
            "duration_sec": duration,
            "size_bytes": file_size,
        }
    
    async def get_job(self, job_id: str) -> Optional[Dict]:
        """Get render job by ID"""
        with self.engine.connect() as conn:
            result = conn.execute(text("""
                SELECT * FROM render_jobs WHERE id = :job_id
            """), {"job_id": job_id})
            row = result.fetchone()
            if row:
                return dict(row._mapping)
        return None
    
    async def _update_job_status(self, job_id: str, status: RenderStatus):
        """Update job status"""
        with self.engine.connect() as conn:
            conn.execute(text("""
                UPDATE render_jobs 
                SET status = :status, started_at = NOW()
                WHERE id = :job_id
            """), {"job_id": job_id, "status": status.value})
            conn.commit()
    
    async def _update_job_result(self, job_id: str, result: Dict):
        """Update job with successful result"""
        with self.engine.connect() as conn:
            conn.execute(text("""
                UPDATE render_jobs 
                SET status = 'succeeded', output = :output, finished_at = NOW()
                WHERE id = :job_id
            """), {"job_id": job_id, "output": json.dumps(result)})
            conn.commit()
    
    async def _update_job_error(self, job_id: str, error: str):
        """Update job with error"""
        with self.engine.connect() as conn:
            conn.execute(text("""
                UPDATE render_jobs 
                SET status = 'failed', error = :error, finished_at = NOW()
                WHERE id = :job_id
            """), {"job_id": job_id, "error": error})
            conn.commit()
    
    async def list_jobs(
        self,
        workspace_id: str = "00000000-0000-0000-0000-000000000001",
        status: Optional[str] = None,
        limit: int = 20
    ) -> list:
        """List render jobs"""
        with self.engine.connect() as conn:
            query = "SELECT * FROM render_jobs WHERE workspace_id = :workspace_id"
            params = {"workspace_id": workspace_id, "limit": limit}
            
            if status:
                query += " AND status = :status"
                params["status"] = status
            
            query += " ORDER BY created_at DESC LIMIT :limit"
            
            result = conn.execute(text(query), params)
            return [dict(row._mapping) for row in result.fetchall()]


# Singleton
_render_service = None

def get_trend_render_service() -> TrendRenderService:
    global _render_service
    if _render_service is None:
        _render_service = TrendRenderService()
    return _render_service
