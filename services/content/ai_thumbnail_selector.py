"""
AI-Powered Best Frame Selection for Thumbnails
Uses GPT-4 Vision to analyze frames and select the best thumbnail
"""
import os
import subprocess
import hashlib
import base64
from pathlib import Path
from typing import Dict, List, Optional, Tuple, Any
from dataclasses import dataclass
import json
from loguru import logger

# Load environment variables
from dotenv import load_dotenv
load_dotenv()

# Optional imports for computer vision fallback
try:
    import cv2
    import numpy as np
    CV2_AVAILABLE = True
except ImportError:
    CV2_AVAILABLE = False
    logger.warning("cv2 not available - using basic frame selection")

try:
    from openai import OpenAI
    OPENAI_AVAILABLE = True
except ImportError:
    OPENAI_AVAILABLE = False
    logger.warning("openai not available - using CV2 fallback")


@dataclass
class FrameAnalysis:
    """Analysis results for a video frame"""
    frame_path: str
    timestamp: float
    ai_score: float
    cv_score: float
    combined_score: float
    analysis: Dict[str, Any]


class AIThumbnailSelector:
    """
    AI-powered thumbnail selection service.
    Extracts frames, analyzes with GPT-4 Vision, and selects the best one.
    """
    
    def __init__(self, openai_api_key: Optional[str] = None):
        self.openai_api_key = openai_api_key or os.getenv("OPENAI_API_KEY")
        self.client = None
        if OPENAI_AVAILABLE and self.openai_api_key:
            self.client = OpenAI(api_key=self.openai_api_key)
        
        self.thumb_dir = "/tmp/mediaposter/thumbnails"
        self.temp_dir = "/tmp/mediaposter/frame_candidates"
        os.makedirs(self.thumb_dir, exist_ok=True)
        os.makedirs(self.temp_dir, exist_ok=True)
    
    def extract_frames(
        self,
        video_path: str,
        num_frames: int = 8,
        skip_start_percent: float = 0.05,
        skip_end_percent: float = 0.05
    ) -> List[Tuple[str, float]]:
        """
        Extract candidate frames from video at strategic intervals.
        
        Args:
            video_path: Path to video file
            num_frames: Number of frames to extract
            skip_start_percent: Skip this % of video at start
            skip_end_percent: Skip this % of video at end
            
        Returns:
            List of (frame_path, timestamp) tuples
        """
        video_name = Path(video_path).stem
        output_prefix = f"{self.temp_dir}/{video_name}"
        
        # Get video duration
        try:
            probe_cmd = [
                "ffprobe", "-v", "error",
                "-show_entries", "format=duration",
                "-of", "json",
                video_path
            ]
            result = subprocess.run(probe_cmd, capture_output=True, text=True, check=True)
            duration = float(json.loads(result.stdout)["format"]["duration"])
        except Exception as e:
            logger.error(f"Could not get duration: {e}")
            duration = 60.0  # Default assumption
        
        # Calculate frame extraction points (skip intro/outro)
        start_time = duration * skip_start_percent
        end_time = duration * (1 - skip_end_percent)
        usable_duration = end_time - start_time
        interval = usable_duration / (num_frames + 1)
        
        frames = []
        for i in range(1, num_frames + 1):
            timestamp = start_time + (interval * i)
            output_path = f"{output_prefix}_frame_{i:02d}_{timestamp:.2f}s.jpg"
            
            try:
                cmd = [
                    "ffmpeg", "-y",
                    "-ss", str(timestamp),
                    "-i", video_path,
                    "-vframes", "1",
                    "-q:v", "2",
                    output_path
                ]
                subprocess.run(cmd, capture_output=True, check=True)
                
                if os.path.exists(output_path):
                    frames.append((output_path, timestamp))
                    logger.debug(f"Extracted frame at {timestamp:.2f}s")
            except Exception as e:
                logger.warning(f"Failed to extract frame at {timestamp:.2f}s: {e}")
        
        return frames
    
    def analyze_frame_cv(self, frame_path: str) -> Dict[str, float]:
        """
        Analyze frame quality using computer vision (fallback/supplement).
        
        Returns scores for sharpness, brightness, contrast, faces, vibrancy.
        """
        if not CV2_AVAILABLE:
            return {"cv_score": 0.5}
        
        try:
            img = cv2.imread(frame_path)
            if img is None:
                return {"cv_score": 0.0}
            
            gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
            
            # Sharpness (Laplacian variance)
            laplacian_var = cv2.Laplacian(gray, cv2.CV_64F).var()
            sharpness = min(laplacian_var / 500, 1.0)
            
            # Brightness (prefer mid-range)
            brightness_raw = np.mean(gray) / 255.0
            brightness = 1.0 - abs(brightness_raw - 0.5) * 2
            
            # Contrast
            contrast = min(gray.std() / 128.0, 1.0)
            
            # Face detection
            face_cascade = cv2.CascadeClassifier(
                cv2.data.haarcascades + 'haarcascade_frontalface_default.xml'
            )
            faces = face_cascade.detectMultiScale(gray, 1.1, 4)
            face_score = min(len(faces) * 0.3, 1.0)
            
            # Color vibrancy
            hsv = cv2.cvtColor(img, cv2.COLOR_BGR2HSV)
            vibrancy = np.mean(hsv[:, :, 1]) / 255.0
            
            # Combined CV score
            cv_score = (
                sharpness * 0.25 +
                brightness * 0.20 +
                contrast * 0.20 +
                face_score * 0.20 +
                vibrancy * 0.15
            )
            
            return {
                "sharpness": round(sharpness, 3),
                "brightness": round(brightness, 3),
                "contrast": round(contrast, 3),
                "faces_detected": len(faces),
                "face_score": round(face_score, 3),
                "vibrancy": round(vibrancy, 3),
                "cv_score": round(cv_score, 3)
            }
        except Exception as e:
            logger.error(f"CV analysis failed: {e}")
            return {"cv_score": 0.5}
    
    def analyze_frame_ai(self, frame_path: str) -> Dict[str, Any]:
        """
        Analyze frame using GPT-4 Vision for thumbnail suitability.
        
        Returns AI analysis with score and reasoning.
        """
        if not self.client:
            return {"ai_score": 0.5, "reasoning": "AI not available"}
        
        try:
            # Read and encode image
            with open(frame_path, "rb") as f:
                image_data = base64.b64encode(f.read()).decode("utf-8")
            
            response = self.client.chat.completions.create(
                model="gpt-4o-mini",
                messages=[
                    {
                        "role": "system",
                        "content": """You are a social media thumbnail expert. Analyze this video frame for thumbnail potential.

Score 0-100 based on:
- Visual appeal and composition (25%)
- Subject clarity and focus (25%)
- Emotional engagement potential (20%)
- Color and lighting quality (15%)
- Click-worthiness for social media (15%)

Respond in JSON format:
{
    "score": 85,
    "composition": "good/average/poor",
    "subject_clarity": "excellent/good/average/poor", 
    "emotional_appeal": "high/medium/low",
    "lighting": "excellent/good/average/poor",
    "click_potential": "high/medium/low",
    "issues": ["any problems"],
    "strengths": ["positive aspects"],
    "reasoning": "brief explanation"
}"""
                    },
                    {
                        "role": "user",
                        "content": [
                            {"type": "text", "text": "Rate this frame for social media thumbnail use:"},
                            {
                                "type": "image_url",
                                "image_url": {"url": f"data:image/jpeg;base64,{image_data}"}
                            }
                        ]
                    }
                ],
                max_tokens=500
            )
            
            # Parse response
            content = response.choices[0].message.content
            
            # Extract JSON from response
            try:
                # Handle markdown code blocks
                if "```json" in content:
                    content = content.split("```json")[1].split("```")[0]
                elif "```" in content:
                    content = content.split("```")[1].split("```")[0]
                
                analysis = json.loads(content.strip())
                ai_score = analysis.get("score", 50) / 100.0
                
                return {
                    "ai_score": round(ai_score, 3),
                    "composition": analysis.get("composition", "unknown"),
                    "subject_clarity": analysis.get("subject_clarity", "unknown"),
                    "emotional_appeal": analysis.get("emotional_appeal", "unknown"),
                    "lighting": analysis.get("lighting", "unknown"),
                    "click_potential": analysis.get("click_potential", "unknown"),
                    "issues": analysis.get("issues", []),
                    "strengths": analysis.get("strengths", []),
                    "reasoning": analysis.get("reasoning", "")
                }
            except json.JSONDecodeError:
                # Fallback: extract score from text
                import re
                score_match = re.search(r'(\d+)/100|score[:\s]+(\d+)', content.lower())
                if score_match:
                    score = int(score_match.group(1) or score_match.group(2))
                    return {"ai_score": score / 100.0, "reasoning": content[:200]}
                return {"ai_score": 0.5, "reasoning": content[:200]}
                
        except Exception as e:
            logger.error(f"AI analysis failed: {e}")
            return {"ai_score": 0.5, "reasoning": str(e)}
    
    def select_best_frame(
        self,
        video_path: str,
        num_candidates: int = 8,
        use_ai: bool = True,
        ai_weight: float = 0.6
    ) -> FrameAnalysis:
        """
        Select the best frame from a video for thumbnail use.
        
        Args:
            video_path: Path to video file
            num_candidates: Number of frames to analyze
            use_ai: Whether to use AI analysis
            ai_weight: Weight for AI score (vs CV score)
            
        Returns:
            FrameAnalysis with best frame info
        """
        logger.info(f"Selecting best frame from {video_path}")
        
        # Extract candidate frames
        frames = self.extract_frames(video_path, num_frames=num_candidates)
        
        if not frames:
            raise ValueError(f"Could not extract frames from {video_path}")
        
        # Analyze each frame
        analyses = []
        for frame_path, timestamp in frames:
            # CV analysis (fast)
            cv_result = self.analyze_frame_cv(frame_path)
            cv_score = cv_result.get("cv_score", 0.5)
            
            # AI analysis (slower but more accurate)
            ai_result = {}
            ai_score = 0.5
            if use_ai and self.client:
                ai_result = self.analyze_frame_ai(frame_path)
                ai_score = ai_result.get("ai_score", 0.5)
            
            # Combined score
            if use_ai and self.client:
                combined = (ai_score * ai_weight) + (cv_score * (1 - ai_weight))
            else:
                combined = cv_score
            
            analysis = FrameAnalysis(
                frame_path=frame_path,
                timestamp=timestamp,
                ai_score=ai_score,
                cv_score=cv_score,
                combined_score=round(combined, 3),
                analysis={**cv_result, **ai_result}
            )
            analyses.append(analysis)
            
            logger.debug(f"Frame {timestamp:.2f}s: AI={ai_score:.2f}, CV={cv_score:.2f}, Combined={combined:.2f}")
        
        # Select best frame
        best = max(analyses, key=lambda x: x.combined_score)
        logger.info(f"Best frame: {best.timestamp:.2f}s with score {best.combined_score:.2f}")
        
        return best
    
    def generate_thumbnail(
        self,
        video_path: str,
        video_id: str,
        use_ai: bool = True
    ) -> Tuple[str, Dict[str, Any]]:
        """
        Generate the best thumbnail for a video.
        
        Args:
            video_path: Path to video file
            video_id: Video ID for naming
            use_ai: Whether to use AI analysis
            
        Returns:
            Tuple of (thumbnail_path, analysis_data)
        """
        # Select best frame
        best = self.select_best_frame(video_path, use_ai=use_ai)
        
        # Generate final thumbnail filename
        video_name = Path(video_path).stem
        file_hash = hashlib.md5(f"{video_id}{best.timestamp}".encode()).hexdigest()[:16]
        thumb_filename = f"{video_name}_{file_hash}_medium.jpg"
        thumb_path = os.path.join(self.thumb_dir, thumb_filename)
        
        # Copy best frame to thumbnail location
        import shutil
        shutil.copy(best.frame_path, thumb_path)
        
        logger.info(f"Generated thumbnail: {thumb_path}")
        
        # Clean up temporary frames
        for f in Path(self.temp_dir).glob(f"{video_name}_frame_*"):
            try:
                f.unlink()
            except Exception:
                pass
        
        return thumb_path, {
            "timestamp": best.timestamp,
            "ai_score": best.ai_score,
            "cv_score": best.cv_score,
            "combined_score": best.combined_score,
            "analysis": best.analysis
        }


# Convenience function for scripts
def generate_best_thumbnail(video_path: str, video_id: str, use_ai: bool = True) -> Tuple[str, Dict]:
    """Generate the best thumbnail for a video."""
    selector = AIThumbnailSelector()
    return selector.generate_thumbnail(video_path, video_id, use_ai=use_ai)
