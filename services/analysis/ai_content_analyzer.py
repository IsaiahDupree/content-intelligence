"""
AI Content Analyzer (PIPE-002)
===============================
Analyze visual content using GPT-4 Vision to extract:
- Scene description
- Object detection
- Mood/emotion
- Niche classification
- Quality score (1-10)

This service extracts frames from videos and uses AI vision analysis
to understand content, which feeds into platform matching and approval.
"""

import os
import base64
import subprocess
import tempfile
from pathlib import Path
from typing import Dict, List, Optional, Any
from datetime import datetime, timezone
from dataclasses import dataclass, field
from enum import Enum
import asyncio
from loguru import logger
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from uuid import uuid4
import openai

from services.event_bus import EventBus, Topics
from database.models import Video
from config import settings


class ContentNiche(str, Enum):
    """Content niche categories"""
    FITNESS = "fitness"
    TECH = "tech"
    TRAVEL = "travel"
    FOOD = "food"
    FASHION = "fashion"
    BEAUTY = "beauty"
    GAMING = "gaming"
    EDUCATION = "education"
    BUSINESS = "business"
    LIFESTYLE = "lifestyle"
    ENTERTAINMENT = "entertainment"
    MUSIC = "music"
    SPORTS = "sports"
    PETS = "pets"
    FAMILY = "family"
    DIY = "diy"
    ART = "art"
    COMEDY = "comedy"
    NEWS = "news"
    OTHER = "other"


@dataclass
class ContentAnalysis:
    """AI-generated content analysis results"""
    scene_description: str
    objects: List[str] = field(default_factory=list)
    mood: str = ""
    emotions: List[str] = field(default_factory=list)
    niche: ContentNiche = ContentNiche.OTHER
    quality_score: float = 5.0  # 1-10 scale
    is_talking_head: bool = False
    has_text_overlay: bool = False
    is_broll: bool = False
    color_palette: List[str] = field(default_factory=list)
    lighting: str = ""  # "bright", "dark", "natural", "studio"
    composition: str = ""  # "centered", "rule-of-thirds", "dynamic"
    confidence_score: float = 0.0  # 0-1 confidence in analysis
    analysis_metadata: Dict[str, Any] = field(default_factory=dict)
    analyzed_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary"""
        return {
            "scene_description": self.scene_description,
            "objects": self.objects,
            "mood": self.mood,
            "emotions": self.emotions,
            "niche": self.niche.value if isinstance(self.niche, ContentNiche) else self.niche,
            "quality_score": self.quality_score,
            "is_talking_head": self.is_talking_head,
            "has_text_overlay": self.has_text_overlay,
            "is_broll": self.is_broll,
            "color_palette": self.color_palette,
            "lighting": self.lighting,
            "composition": self.composition,
            "confidence_score": self.confidence_score,
            "analysis_metadata": self.analysis_metadata,
            "analyzed_at": self.analyzed_at.isoformat()
        }


class AIContentAnalyzer:
    """
    AI Content Analyzer - Vision analysis for videos and images

    Uses GPT-4 Vision to analyze visual content and extract structured
    information about scenes, objects, mood, niche, and quality.

    Usage:
        analyzer = AIContentAnalyzer(db_session)

        # Analyze a video
        analysis = await analyzer.analyze_video(video_id)

        # Batch analyze videos
        results = await analyzer.batch_analyze(video_ids)
    """

    def __init__(self, db: AsyncSession):
        """
        Initialize AI Content Analyzer

        Args:
            db: Database session
        """
        self.db = db
        self.event_bus = EventBus.get_instance()
        self.event_bus.set_source("ai-content-analyzer")

        # OpenAI client
        self.openai_client = openai.AsyncOpenAI(api_key=settings.openai_api_key)

        # Analysis prompt template
        self.analysis_prompt = """Analyze this video frame and provide a detailed, structured analysis.

Return a JSON object with these fields:
{
    "scene_description": "Brief description of what's happening in the scene (1-2 sentences)",
    "objects": ["list", "of", "visible", "objects"],
    "mood": "overall mood (e.g., energetic, calm, intense, playful)",
    "emotions": ["list", "of", "emotions", "detected"],
    "niche": "primary content category (fitness, tech, travel, food, fashion, beauty, gaming, education, business, lifestyle, entertainment, music, sports, pets, family, diy, art, comedy, news, other)",
    "quality_score": 7.5,  // 1-10 scale (production quality, composition, lighting)
    "is_talking_head": false,  // true if person speaking directly to camera
    "has_text_overlay": false,  // true if text overlays visible
    "is_broll": false,  // true if B-roll footage (no people, scenic/atmospheric)
    "color_palette": ["dominant", "colors"],  // 2-3 main colors
    "lighting": "bright|dark|natural|studio",
    "composition": "centered|rule-of-thirds|dynamic",
    "confidence_score": 0.85  // 0-1 confidence in this analysis
}

Be specific and accurate. Focus on visual elements that would help categorize and match this content to social platforms."""

        logger.info("🎨 AI Content Analyzer initialized")

    async def extract_frame(
        self,
        video_path: str,
        timestamp: float = 2.0,
        output_dir: Optional[str] = None
    ) -> Optional[str]:
        """
        Extract a single frame from video using ffmpeg

        Args:
            video_path: Path to video file
            timestamp: Timestamp in seconds to extract (default: 2.0s)
            output_dir: Directory to save frame (default: temp)

        Returns:
            Path to extracted frame image, or None on error
        """
        try:
            if not os.path.exists(video_path):
                logger.error(f"Video not found: {video_path}")
                return None

            # Create output directory if needed
            if output_dir:
                os.makedirs(output_dir, exist_ok=True)
            else:
                output_dir = tempfile.gettempdir()

            # Generate output filename
            video_name = Path(video_path).stem
            frame_path = os.path.join(output_dir, f"{video_name}_frame_{timestamp}s.jpg")

            # Extract frame with ffmpeg
            cmd = [
                "ffmpeg",
                "-y",  # Overwrite
                "-ss", str(timestamp),  # Seek to timestamp
                "-i", video_path,  # Input file
                "-vframes", "1",  # Extract 1 frame
                "-q:v", "2",  # High quality
                frame_path
            ]

            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=30
            )

            if result.returncode != 0:
                logger.error(f"ffmpeg error: {result.stderr}")
                return None

            if os.path.exists(frame_path):
                logger.debug(f"Extracted frame: {frame_path}")
                return frame_path
            else:
                logger.error("Frame extraction failed - file not created")
                return None

        except subprocess.TimeoutExpired:
            logger.error("ffmpeg timeout")
            return None
        except Exception as e:
            logger.error(f"Error extracting frame: {e}")
            return None

    async def extract_multiple_frames(
        self,
        video_path: str,
        timestamps: List[float] = None,
        num_frames: int = 3
    ) -> List[str]:
        """
        Extract multiple frames from video

        Args:
            video_path: Path to video file
            timestamps: Specific timestamps to extract (optional)
            num_frames: Number of frames to extract if timestamps not provided

        Returns:
            List of frame image paths
        """
        if timestamps is None:
            # Extract frames at regular intervals (e.g., 2s, 5s, 10s)
            timestamps = [2.0, 5.0, 10.0][:num_frames]

        frames = []
        for timestamp in timestamps:
            frame_path = await self.extract_frame(video_path, timestamp)
            if frame_path:
                frames.append(frame_path)

        return frames

    def encode_image_to_base64(self, image_path: str) -> str:
        """
        Encode image to base64 for GPT-4 Vision

        Args:
            image_path: Path to image file

        Returns:
            Base64-encoded image string
        """
        with open(image_path, 'rb') as f:
            return base64.b64encode(f.read()).decode('utf-8')

    async def analyze_image_with_gpt4_vision(
        self,
        image_path: str
    ) -> Optional[Dict[str, Any]]:
        """
        Analyze image using GPT-4 Vision

        Args:
            image_path: Path to image file

        Returns:
            Analysis result dictionary, or None on error
        """
        try:
            # Encode image
            image_base64 = self.encode_image_to_base64(image_path)

            # Call GPT-4 Vision
            response = await self.openai_client.chat.completions.create(
                model="gpt-4o",  # GPT-4 Turbo with vision
                messages=[
                    {
                        "role": "user",
                        "content": [
                            {"type": "text", "text": self.analysis_prompt},
                            {
                                "type": "image_url",
                                "image_url": {
                                    "url": f"data:image/jpeg;base64,{image_base64}"
                                }
                            }
                        ]
                    }
                ],
                response_format={"type": "json_object"},
                max_tokens=1000,
                temperature=0.3
            )

            # Parse response
            content = response.choices[0].message.content
            import json
            analysis = json.loads(content)

            logger.debug(f"GPT-4 Vision analysis: {analysis.get('scene_description', '')[:100]}")

            return analysis

        except Exception as e:
            logger.error(f"Error analyzing image with GPT-4 Vision: {e}")
            return None

    async def analyze_video(
        self,
        video_id: str,
        use_multiple_frames: bool = True
    ) -> Optional[ContentAnalysis]:
        """
        Analyze a video using AI vision

        Args:
            video_id: Video ID (UUID)
            use_multiple_frames: Extract and analyze multiple frames (default: True)

        Returns:
            ContentAnalysis object, or None on error
        """
        try:
            # Get video from database
            stmt = select(Video).where(Video.id == video_id)
            result = await self.db.execute(stmt)
            video = result.scalar_one_or_none()

            if not video:
                logger.error(f"Video not found: {video_id}")
                return None

            if not video.source_uri or not os.path.exists(video.source_uri):
                logger.error(f"Video file not found: {video.source_uri}")
                return None

            # Extract frame(s)
            if use_multiple_frames:
                frame_paths = await self.extract_multiple_frames(video.source_uri, num_frames=3)
            else:
                frame_path = await self.extract_frame(video.source_uri)
                frame_paths = [frame_path] if frame_path else []

            if not frame_paths:
                logger.error(f"No frames extracted from video: {video_id}")
                return None

            # Analyze first frame (or combine analyses if multiple)
            # For now, just use the first frame
            analysis_result = await self.analyze_image_with_gpt4_vision(frame_paths[0])

            if not analysis_result:
                return None

            # Convert to ContentAnalysis object
            niche_str = analysis_result.get("niche", "other").lower()
            try:
                niche = ContentNiche(niche_str)
            except ValueError:
                niche = ContentNiche.OTHER

            analysis = ContentAnalysis(
                scene_description=analysis_result.get("scene_description", ""),
                objects=analysis_result.get("objects", []),
                mood=analysis_result.get("mood", ""),
                emotions=analysis_result.get("emotions", []),
                niche=niche,
                quality_score=float(analysis_result.get("quality_score", 5.0)),
                is_talking_head=analysis_result.get("is_talking_head", False),
                has_text_overlay=analysis_result.get("has_text_overlay", False),
                is_broll=analysis_result.get("is_broll", False),
                color_palette=analysis_result.get("color_palette", []),
                lighting=analysis_result.get("lighting", ""),
                composition=analysis_result.get("composition", ""),
                confidence_score=float(analysis_result.get("confidence_score", 0.0)),
                analysis_metadata={
                    "frames_analyzed": len(frame_paths),
                    "raw_gpt4_response": analysis_result
                }
            )

            logger.success(f"✓ Analyzed video {video_id}: {analysis.niche.value}, quality={analysis.quality_score}")

            # Emit event
            await self.event_bus.publish(
                Topics.AI_ANALYSIS_COMPLETED,
                {
                    "video_id": video_id,
                    "niche": analysis.niche.value,
                    "quality_score": analysis.quality_score,
                    "scene_description": analysis.scene_description[:100]
                }
            )

            # Clean up temp frames
            for frame_path in frame_paths:
                try:
                    if frame_path and os.path.exists(frame_path):
                        os.remove(frame_path)
                except Exception as e:
                    logger.debug(f"Error cleaning up frame: {e}")

            return analysis

        except Exception as e:
            logger.error(f"Error analyzing video {video_id}: {e}")
            return None

    async def batch_analyze(
        self,
        video_ids: List[str],
        max_concurrent: int = 3,
        delay_between: float = 1.0
    ) -> Dict[str, Optional[ContentAnalysis]]:
        """
        Batch analyze multiple videos

        Args:
            video_ids: List of video IDs
            max_concurrent: Maximum concurrent analyses (default: 3)
            delay_between: Delay between analyses in seconds (default: 1.0)

        Returns:
            Dictionary mapping video_id to ContentAnalysis (or None on error)
        """
        logger.info(f"🔄 Batch analyzing {len(video_ids)} videos...")

        results = {}

        # Process in batches
        for i in range(0, len(video_ids), max_concurrent):
            batch = video_ids[i:i+max_concurrent]

            # Analyze batch
            tasks = [self.analyze_video(video_id) for video_id in batch]
            batch_results = await asyncio.gather(*tasks, return_exceptions=True)

            # Store results
            for video_id, result in zip(batch, batch_results):
                if isinstance(result, Exception):
                    logger.error(f"Error analyzing {video_id}: {result}")
                    results[video_id] = None
                else:
                    results[video_id] = result

            # Delay between batches
            if i + max_concurrent < len(video_ids):
                await asyncio.sleep(delay_between)

        success_count = sum(1 for r in results.values() if r is not None)
        logger.success(f"✓ Batch analysis complete: {success_count}/{len(video_ids)} successful")

        return results

    async def get_unanalyzed_videos(
        self,
        limit: int = 100
    ) -> List[str]:
        """
        Get list of video IDs that haven't been analyzed

        Args:
            limit: Maximum number to return

        Returns:
            List of video IDs
        """
        try:
            # Query videos without analysis
            # For now, just get all pending videos
            stmt = select(Video.id).where(
                Video.status == "pending_analysis"
            ).limit(limit)

            result = await self.db.execute(stmt)
            video_ids = [row[0] for row in result.all()]

            logger.info(f"Found {len(video_ids)} unanalyzed videos")

            return video_ids

        except Exception as e:
            logger.error(f"Error getting unanalyzed videos: {e}")
            return []
