"""
Remotion Shipper - Fast video production from trend flash scripts
Renders 30-45s vertical videos within 10-30 minutes.
"""

import os
import subprocess
import asyncio
from datetime import datetime, timezone
from typing import Dict, List, Optional
from dataclasses import dataclass
from pathlib import Path
from uuid import uuid4
from loguru import logger

from .flash_generator import TrendFlashContent


REMOTION_PROJECT_PATH = os.getenv(
    "REMOTION_PROJECT_PATH",
    "/Users/isaiahdupree/Documents/Software/MediaPoster/remotion"
)

OUTPUT_DIR = os.getenv(
    "TREND_FLASH_OUTPUT",
    "/Users/isaiahdupree/Documents/Software/MediaPoster/output/trend_flash"
)


@dataclass
class RenderJob:
    """A Remotion render job."""
    id: str
    content_id: str
    status: str  # pending, rendering, complete, failed
    output_path: str = ""
    duration_seconds: int = 35
    error: str = ""
    started_at: Optional[datetime] = None
    completed_at: Optional[datetime] = None
    
    def to_dict(self) -> Dict:
        return {
            "id": self.id,
            "content_id": self.content_id,
            "status": self.status,
            "output_path": self.output_path,
            "duration_seconds": self.duration_seconds,
            "error": self.error,
            "started_at": self.started_at.isoformat() if self.started_at else None,
            "completed_at": self.completed_at.isoformat() if self.completed_at else None
        }


class RemotionShipper:
    """
    Renders trend flash videos using Remotion.
    
    Pipeline:
    1. Take TrendFlashContent with script + captions
    2. Generate Remotion composition props
    3. Render to MP4
    4. Return path for posting
    """
    
    def __init__(self):
        self.project_path = Path(REMOTION_PROJECT_PATH)
        self.output_dir = Path(OUTPUT_DIR)
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self.jobs: Dict[str, RenderJob] = {}
        logger.info("✅ RemotionShipper initialized")
    
    async def render_video(
        self,
        content: TrendFlashContent,
        composition: str = "TrendFlash"
    ) -> RenderJob:
        """
        Render a video from TrendFlashContent.
        
        Args:
            content: Generated content with script and captions
            composition: Remotion composition to use
        
        Returns:
            RenderJob with status and output path
        """
        job_id = str(uuid4())[:8]
        output_filename = f"trend_flash_{content.id[:8]}_{job_id}.mp4"
        output_path = self.output_dir / output_filename
        
        job = RenderJob(
            id=job_id,
            content_id=content.id,
            status="pending",
            output_path=str(output_path),
            started_at=datetime.now(timezone.utc)
        )
        
        self.jobs[job_id] = job
        
        try:
            # Generate props for Remotion
            props = self._generate_props(content)
            
            # Render video
            job.status = "rendering"
            logger.info(f"🎬 Rendering video: {job_id}")
            
            success = await self._run_remotion_render(
                composition=composition,
                props=props,
                output_path=str(output_path)
            )
            
            if success:
                job.status = "complete"
                job.completed_at = datetime.now(timezone.utc)
                logger.info(f"✅ Video rendered: {output_path}")
            else:
                job.status = "failed"
                job.error = "Render failed"
            
        except Exception as e:
            job.status = "failed"
            job.error = str(e)
            logger.error(f"Render failed: {e}")
        
        return job
    
    def _generate_props(self, content: TrendFlashContent) -> Dict:
        """Generate Remotion composition props from content."""
        return {
            "script": {
                "hook": content.script_hook,
                "context": content.script_context,
                "take": content.script_take,
                "action": content.script_action,
                "cta": content.script_cta
            },
            "captions": content.captions,
            "titles": {
                "tiktok": content.title_tiktok,
                "instagram": content.title_instagram,
                "youtube": content.title_youtube
            },
            "style": {
                "variant": content.script_variant,
                "aspectRatio": "9:16",
                "duration": 35,
                "fps": 30
            },
            "branding": {
                "handle": "@isaiah_dupree",
                "watermark": False
            }
        }
    
    async def _run_remotion_render(
        self,
        composition: str,
        props: Dict,
        output_path: str
    ) -> bool:
        """Run Remotion render command."""
        import json
        
        # Check if Remotion project exists
        if not self.project_path.exists():
            logger.warning(f"Remotion project not found: {self.project_path}")
            # Create placeholder video instead
            return await self._create_placeholder_video(props, output_path)
        
        try:
            props_json = json.dumps(props)
            
            cmd = [
                "npx", "remotion", "render",
                composition,
                output_path,
                "--props", props_json,
                "--codec", "h264",
                "--crf", "18"
            ]
            
            result = await asyncio.to_thread(
                subprocess.run,
                cmd,
                cwd=str(self.project_path),
                capture_output=True,
                text=True,
                timeout=300  # 5 min timeout
            )
            
            if result.returncode == 0:
                return True
            else:
                logger.error(f"Remotion error: {result.stderr}")
                return await self._create_placeholder_video(props, output_path)
                
        except subprocess.TimeoutExpired:
            logger.error("Remotion render timeout")
            return False
        except Exception as e:
            logger.error(f"Remotion render failed: {e}")
            return await self._create_placeholder_video(props, output_path)
    
    async def _create_placeholder_video(self, props: Dict, output_path: str) -> bool:
        """Create a placeholder video with FFmpeg when Remotion isn't available."""
        try:
            script = props.get("script", {})
            hook = script.get("hook", "Trend Flash Video")[:50]
            
            # Create simple placeholder with text
            cmd = [
                "ffmpeg", "-y",
                "-f", "lavfi",
                "-i", "color=c=black:s=1080x1920:d=35",
                "-vf", f"drawtext=text='{hook}':fontcolor=white:fontsize=48:x=(w-text_w)/2:y=(h-text_h)/2",
                "-c:v", "libx264",
                "-t", "35",
                "-pix_fmt", "yuv420p",
                output_path
            ]
            
            result = await asyncio.to_thread(
                subprocess.run,
                cmd,
                capture_output=True,
                timeout=60
            )
            
            return result.returncode == 0
            
        except Exception as e:
            logger.error(f"Placeholder video failed: {e}")
            return False
    
    async def render_batch(
        self,
        contents: List[TrendFlashContent],
        max_concurrent: int = 2
    ) -> List[RenderJob]:
        """Render multiple videos with concurrency limit."""
        jobs = []
        
        for content in contents:
            job = await self.render_video(content)
            jobs.append(job)
            
            # Simple sequential for now, could add semaphore for parallel
            if job.status == "failed":
                logger.warning(f"Batch render: job {job.id} failed, continuing...")
        
        return jobs
    
    def get_job(self, job_id: str) -> Optional[RenderJob]:
        """Get a render job by ID."""
        return self.jobs.get(job_id)
    
    def get_pending_jobs(self) -> List[RenderJob]:
        """Get all pending/rendering jobs."""
        return [j for j in self.jobs.values() if j.status in ["pending", "rendering"]]
    
    def get_completed_jobs(self) -> List[RenderJob]:
        """Get all completed jobs."""
        return [j for j in self.jobs.values() if j.status == "complete"]
    
    def get_stats(self) -> Dict:
        """Get render statistics."""
        jobs = list(self.jobs.values())
        return {
            "total_jobs": len(jobs),
            "pending": len([j for j in jobs if j.status == "pending"]),
            "rendering": len([j for j in jobs if j.status == "rendering"]),
            "complete": len([j for j in jobs if j.status == "complete"]),
            "failed": len([j for j in jobs if j.status == "failed"])
        }


# =============================================================================
# SINGLETON
# =============================================================================

_shipper_instance: Optional[RemotionShipper] = None

def get_remotion_shipper() -> RemotionShipper:
    """Get singleton instance of RemotionShipper."""
    global _shipper_instance
    if _shipper_instance is None:
        _shipper_instance = RemotionShipper()
    return _shipper_instance
