"""
Enhanced Vision Analyzer
========================
Advanced visual analysis with structured extraction for:
- Color palette and mood
- Lighting analysis
- Camera angles and shot types
- Camera motion detection
- Scene boundary detection
- Pattern interrupt detection

Addresses gaps identified in ANALYSIS_TO_GENERATION_DATA_AUDIT.md
"""
import os
import json
import base64
from pathlib import Path
from datetime import datetime
from typing import Dict, Any, List, Optional, Tuple
from dataclasses import dataclass, field, asdict
from loguru import logger

from openai import OpenAI
import numpy as np

try:
    from PIL import Image
    import cv2
    HAS_CV2 = True
except ImportError:
    HAS_CV2 = False
    logger.warning("OpenCV not installed - some visual analysis features disabled")


@dataclass
class ColorPalette:
    """Extracted color palette"""
    primary: str  # Hex color
    secondary: str
    accent: str
    colors: List[str]  # All detected colors
    mood: str  # warm, cool, neutral, vibrant, muted
    contrast_level: str  # low, medium, high


@dataclass
class LightingAnalysis:
    """Lighting characteristics"""
    type: str  # natural, studio, dramatic, soft, harsh, mixed
    direction: str  # front, side, back, top, ambient
    quality: str  # soft, hard, diffused
    exposure: str  # underexposed, proper, overexposed
    shadows: str  # minimal, moderate, dramatic


@dataclass
class CameraInfo:
    """Camera and shot information"""
    shot_type: str  # close-up, medium, wide, extreme-close, extreme-wide
    angle: str  # eye-level, low, high, dutch, overhead, worm
    movement: str  # static, pan, tilt, zoom, tracking, handheld, dolly
    movement_confidence: float  # 0-1
    depth_of_field: str  # shallow, medium, deep


@dataclass
class SceneElements:
    """Elements detected in scene"""
    setting: str  # indoor, outdoor, studio, etc.
    setting_specific: str  # office, bedroom, street, etc.
    main_subjects: List[str]
    objects: List[str]
    text_on_screen: List[str]
    text_style: str  # caption, title, subtitle, graphics, none
    people_count: int
    facial_expressions: List[str]
    body_language: str


@dataclass
class ViralIndicators:
    """Viral potential indicators"""
    hook_potential: int  # 0-100
    pattern_interrupt: bool
    scroll_stopper_elements: List[str]
    meme_potential: bool
    emotional_trigger: str
    curiosity_gap: bool


@dataclass
class StructuredFrameAnalysis:
    """Complete structured analysis of a frame"""
    frame_index: int
    timestamp: float
    
    # Visual elements
    color_palette: ColorPalette
    lighting: LightingAnalysis
    camera: CameraInfo
    scene: SceneElements
    viral: ViralIndicators
    
    # Raw description
    description: str
    
    # Metadata
    analysis_version: str = "1.0"
    model_used: str = ""
    analyzed_at: str = field(default_factory=lambda: datetime.utcnow().isoformat())


@dataclass
class SceneBoundary:
    """Detected scene boundary"""
    frame_index: int
    timestamp: float
    boundary_type: str  # cut, dissolve, fade, wipe
    confidence: float
    visual_change_score: float
    previous_scene_summary: str
    next_scene_preview: str


@dataclass
class CameraMotionSequence:
    """Camera motion detected across frames"""
    start_frame: int
    end_frame: int
    start_time: float
    end_time: float
    motion_type: str
    confidence: float
    direction: str  # left, right, up, down, in, out
    speed: str  # slow, medium, fast


class EnhancedVisionAnalyzer:
    """
    Enhanced vision analyzer with structured extraction.
    Uses GPT-4 Vision for AI analysis and OpenCV for motion detection.
    """
    
    STRUCTURED_ANALYSIS_PROMPT = """Analyze this video frame and provide a DETAILED STRUCTURED analysis.

Return a JSON object with EXACTLY this structure:
{
    "description": "detailed description of what's happening",
    
    "color_palette": {
        "primary": "#hexcolor",
        "secondary": "#hexcolor", 
        "accent": "#hexcolor",
        "colors": ["#hex1", "#hex2", "#hex3"],
        "mood": "warm|cool|neutral|vibrant|muted",
        "contrast_level": "low|medium|high"
    },
    
    "lighting": {
        "type": "natural|studio|dramatic|soft|harsh|mixed",
        "direction": "front|side|back|top|ambient",
        "quality": "soft|hard|diffused",
        "exposure": "underexposed|proper|overexposed",
        "shadows": "minimal|moderate|dramatic"
    },
    
    "camera": {
        "shot_type": "extreme-close|close-up|medium|wide|extreme-wide",
        "angle": "eye-level|low|high|dutch|overhead|worm",
        "movement_hint": "static|pan|tilt|zoom|tracking|handheld",
        "depth_of_field": "shallow|medium|deep"
    },
    
    "scene": {
        "setting": "indoor|outdoor|studio|mixed",
        "setting_specific": "specific location like office, bedroom, street",
        "main_subjects": ["list of main subjects"],
        "objects": ["notable objects visible"],
        "text_on_screen": ["any visible text"],
        "text_style": "caption|title|subtitle|graphics|none",
        "people_count": 0,
        "facial_expressions": ["expressions if faces visible"],
        "body_language": "description of body language if people visible"
    },
    
    "viral": {
        "hook_potential": 0-100,
        "pattern_interrupt": true|false,
        "scroll_stopper_elements": ["elements that stop scrolling"],
        "meme_potential": true|false,
        "emotional_trigger": "curiosity|fear|joy|anger|surprise|none",
        "curiosity_gap": true|false
    }
}

Be precise with colors (estimate hex codes), thorough with objects, and analytical about viral potential."""

    FRAME_COMPARISON_PROMPT = """Compare these two sequential video frames and analyze:

1. CAMERA MOTION: Did the camera move? How?
   - Motion type: static, pan (left/right), tilt (up/down), zoom (in/out), tracking, dolly, handheld shake
   - Direction and speed
   - Confidence (0-100%)

2. SCENE CHANGE: Is this the same scene?
   - If different: what type of transition (cut, dissolve, fade, wipe)
   - Visual change magnitude (0-100%)

3. SUBJECT MOTION: Did subjects move significantly?

Return JSON:
{
    "camera_motion": {
        "detected": true|false,
        "type": "static|pan|tilt|zoom|tracking|dolly|handheld",
        "direction": "left|right|up|down|in|out|none",
        "speed": "slow|medium|fast",
        "confidence": 0-100
    },
    "scene_change": {
        "is_same_scene": true|false,
        "transition_type": "cut|dissolve|fade|wipe|none",
        "visual_change_score": 0-100,
        "change_description": "what changed"
    },
    "subject_motion": {
        "detected": true|false,
        "description": "what moved"
    }
}"""

    def __init__(
        self,
        openai_api_key: Optional[str] = None,
        model: str = "gpt-4o"
    ):
        self.api_key = openai_api_key or os.getenv("OPENAI_API_KEY")
        self.model = model
        self.client = OpenAI(api_key=self.api_key) if self.api_key else None
        
        if not self.client:
            logger.warning("OpenAI not configured - vision analysis will not work")
    
    def encode_image(self, image_path: Path) -> str:
        """Encode image to base64"""
        with open(image_path, "rb") as f:
            return base64.b64encode(f.read()).decode("utf-8")
    
    async def analyze_frame_structured(
        self,
        image_path: Path,
        frame_index: int = 0,
        timestamp: float = 0.0
    ) -> StructuredFrameAnalysis:
        """
        Analyze a single frame with structured extraction.
        
        Args:
            image_path: Path to frame image
            frame_index: Index of frame in video
            timestamp: Timestamp in seconds
            
        Returns:
            StructuredFrameAnalysis with all extracted data
        """
        if not self.client:
            raise RuntimeError("OpenAI client not configured")
        
        if not Path(image_path).exists():
            raise FileNotFoundError(f"Frame not found: {image_path}")
        
        logger.info(f"Analyzing frame {frame_index} at {timestamp:.2f}s")
        
        base64_image = self.encode_image(Path(image_path))
        
        try:
            response = self.client.chat.completions.create(
                model=self.model,
                messages=[
                    {
                        "role": "user",
                        "content": [
                            {"type": "text", "text": self.STRUCTURED_ANALYSIS_PROMPT},
                            {
                                "type": "image_url",
                                "image_url": {
                                    "url": f"data:image/jpeg;base64,{base64_image}",
                                    "detail": "high"
                                }
                            }
                        ]
                    }
                ],
                response_format={"type": "json_object"},
                max_tokens=1500,
                temperature=0.2
            )
            
            analysis = json.loads(response.choices[0].message.content)
            
            # Parse into structured dataclasses
            color_data = analysis.get("color_palette", {})
            lighting_data = analysis.get("lighting", {})
            camera_data = analysis.get("camera", {})
            scene_data = analysis.get("scene", {})
            viral_data = analysis.get("viral", {})
            
            return StructuredFrameAnalysis(
                frame_index=frame_index,
                timestamp=timestamp,
                color_palette=ColorPalette(
                    primary=color_data.get("primary", "#000000"),
                    secondary=color_data.get("secondary", "#000000"),
                    accent=color_data.get("accent", "#000000"),
                    colors=color_data.get("colors", []),
                    mood=color_data.get("mood", "neutral"),
                    contrast_level=color_data.get("contrast_level", "medium")
                ),
                lighting=LightingAnalysis(
                    type=lighting_data.get("type", "mixed"),
                    direction=lighting_data.get("direction", "ambient"),
                    quality=lighting_data.get("quality", "soft"),
                    exposure=lighting_data.get("exposure", "proper"),
                    shadows=lighting_data.get("shadows", "moderate")
                ),
                camera=CameraInfo(
                    shot_type=camera_data.get("shot_type", "medium"),
                    angle=camera_data.get("angle", "eye-level"),
                    movement=camera_data.get("movement_hint", "static"),
                    movement_confidence=0.5,  # Will be updated by motion detection
                    depth_of_field=camera_data.get("depth_of_field", "medium")
                ),
                scene=SceneElements(
                    setting=scene_data.get("setting", "unknown"),
                    setting_specific=scene_data.get("setting_specific", ""),
                    main_subjects=scene_data.get("main_subjects", []),
                    objects=scene_data.get("objects", []),
                    text_on_screen=scene_data.get("text_on_screen", []),
                    text_style=scene_data.get("text_style", "none"),
                    people_count=scene_data.get("people_count", 0),
                    facial_expressions=scene_data.get("facial_expressions", []),
                    body_language=scene_data.get("body_language", "")
                ),
                viral=ViralIndicators(
                    hook_potential=viral_data.get("hook_potential", 50),
                    pattern_interrupt=viral_data.get("pattern_interrupt", False),
                    scroll_stopper_elements=viral_data.get("scroll_stopper_elements", []),
                    meme_potential=viral_data.get("meme_potential", False),
                    emotional_trigger=viral_data.get("emotional_trigger", "none"),
                    curiosity_gap=viral_data.get("curiosity_gap", False)
                ),
                description=analysis.get("description", ""),
                model_used=self.model
            )
            
        except Exception as e:
            logger.error(f"Structured analysis failed: {e}")
            raise
    
    async def compare_frames_for_motion(
        self,
        frame1_path: Path,
        frame2_path: Path,
        time_delta: float = 1.0
    ) -> Dict[str, Any]:
        """
        Compare two frames to detect camera motion and scene changes.
        
        Args:
            frame1_path: Path to first frame
            frame2_path: Path to second frame
            time_delta: Time between frames in seconds
            
        Returns:
            Motion and scene change analysis
        """
        if not self.client:
            raise RuntimeError("OpenAI client not configured")
        
        base64_1 = self.encode_image(Path(frame1_path))
        base64_2 = self.encode_image(Path(frame2_path))
        
        try:
            response = self.client.chat.completions.create(
                model=self.model,
                messages=[
                    {
                        "role": "user",
                        "content": [
                            {"type": "text", "text": self.FRAME_COMPARISON_PROMPT},
                            {
                                "type": "image_url",
                                "image_url": {
                                    "url": f"data:image/jpeg;base64,{base64_1}",
                                    "detail": "high"
                                }
                            },
                            {
                                "type": "image_url",
                                "image_url": {
                                    "url": f"data:image/jpeg;base64,{base64_2}",
                                    "detail": "high"
                                }
                            }
                        ]
                    }
                ],
                response_format={"type": "json_object"},
                max_tokens=500,
                temperature=0.2
            )
            
            return json.loads(response.choices[0].message.content)
            
        except Exception as e:
            logger.error(f"Frame comparison failed: {e}")
            raise
    
    def detect_motion_opencv(
        self,
        frame1_path: Path,
        frame2_path: Path
    ) -> Dict[str, Any]:
        """
        Detect camera/subject motion using OpenCV optical flow.
        Fast but less accurate than AI analysis.
        
        Args:
            frame1_path: Path to first frame
            frame2_path: Path to second frame
            
        Returns:
            Motion analysis from optical flow
        """
        if not HAS_CV2:
            return {"error": "OpenCV not installed", "motion_detected": False}
        
        try:
            # Read frames
            img1 = cv2.imread(str(frame1_path))
            img2 = cv2.imread(str(frame2_path))
            
            if img1 is None or img2 is None:
                return {"error": "Could not read frames", "motion_detected": False}
            
            # Convert to grayscale
            gray1 = cv2.cvtColor(img1, cv2.COLOR_BGR2GRAY)
            gray2 = cv2.cvtColor(img2, cv2.COLOR_BGR2GRAY)
            
            # Calculate optical flow using Farneback method
            flow = cv2.calcOpticalFlowFarneback(
                gray1, gray2, None,
                pyr_scale=0.5, levels=3, winsize=15,
                iterations=3, poly_n=5, poly_sigma=1.2, flags=0
            )
            
            # Analyze flow vectors
            mag, ang = cv2.cartToPolar(flow[..., 0], flow[..., 1])
            
            # Calculate statistics
            avg_magnitude = np.mean(mag)
            avg_angle = np.mean(ang)
            max_magnitude = np.max(mag)
            
            # Determine motion type based on flow patterns
            motion_type = "static"
            direction = "none"
            confidence = 0.0
            
            if avg_magnitude > 2.0:  # Significant motion threshold
                confidence = min(1.0, avg_magnitude / 10.0)
                
                # Analyze dominant direction
                horizontal_flow = np.mean(flow[..., 0])
                vertical_flow = np.mean(flow[..., 1])
                
                if abs(horizontal_flow) > abs(vertical_flow):
                    if horizontal_flow > 0:
                        motion_type = "pan"
                        direction = "right"
                    else:
                        motion_type = "pan"
                        direction = "left"
                else:
                    if vertical_flow > 0:
                        motion_type = "tilt"
                        direction = "down"
                    else:
                        motion_type = "tilt"
                        direction = "up"
                
                # Check for zoom (radial flow)
                center_y, center_x = flow.shape[:2]
                center_y //= 2
                center_x //= 2
                
                # Sample flow at edges vs center
                edge_mag = (mag[0, :].mean() + mag[-1, :].mean() + 
                           mag[:, 0].mean() + mag[:, -1].mean()) / 4
                center_mag = mag[center_y-50:center_y+50, center_x-50:center_x+50].mean()
                
                if edge_mag > center_mag * 1.5:
                    motion_type = "zoom"
                    direction = "out"
                elif center_mag > edge_mag * 1.5:
                    motion_type = "zoom"
                    direction = "in"
            
            # Detect scene change (large overall change)
            frame_diff = cv2.absdiff(gray1, gray2)
            change_score = np.mean(frame_diff) / 255.0 * 100
            
            return {
                "motion_detected": avg_magnitude > 2.0,
                "motion_type": motion_type,
                "direction": direction,
                "confidence": confidence,
                "avg_magnitude": float(avg_magnitude),
                "max_magnitude": float(max_magnitude),
                "scene_change_score": float(change_score),
                "is_scene_boundary": change_score > 30  # High change = likely scene cut
            }
            
        except Exception as e:
            logger.error(f"OpenCV motion detection failed: {e}")
            return {"error": str(e), "motion_detected": False}
    
    async def detect_scene_boundaries(
        self,
        frame_paths: List[Path],
        timestamps: List[float],
        threshold: float = 30.0
    ) -> List[SceneBoundary]:
        """
        Detect scene boundaries across a sequence of frames.
        
        Args:
            frame_paths: List of paths to frame images
            timestamps: Corresponding timestamps
            threshold: Visual change threshold for scene boundary
            
        Returns:
            List of detected scene boundaries
        """
        boundaries = []
        
        if len(frame_paths) < 2:
            return boundaries
        
        logger.info(f"Detecting scene boundaries across {len(frame_paths)} frames")
        
        for i in range(1, len(frame_paths)):
            # Use OpenCV for fast initial detection
            motion_result = self.detect_motion_opencv(frame_paths[i-1], frame_paths[i])
            
            if motion_result.get("is_scene_boundary", False):
                change_score = motion_result.get("scene_change_score", 0)
                
                if change_score >= threshold:
                    # Use AI for detailed analysis of the boundary
                    if self.client:
                        try:
                            comparison = await self.compare_frames_for_motion(
                                frame_paths[i-1], frame_paths[i]
                            )
                            scene_change = comparison.get("scene_change", {})
                            transition_type = scene_change.get("transition_type", "cut")
                        except:
                            transition_type = "cut"
                    else:
                        transition_type = "cut"
                    
                    boundaries.append(SceneBoundary(
                        frame_index=i,
                        timestamp=timestamps[i],
                        boundary_type=transition_type,
                        confidence=min(1.0, change_score / 50.0),
                        visual_change_score=change_score,
                        previous_scene_summary="",
                        next_scene_preview=""
                    ))
                    
                    logger.info(f"Scene boundary detected at {timestamps[i]:.2f}s (score: {change_score:.1f})")
        
        return boundaries
    
    async def detect_camera_motion_sequence(
        self,
        frame_paths: List[Path],
        timestamps: List[float]
    ) -> List[CameraMotionSequence]:
        """
        Detect camera motion sequences across frames.
        
        Args:
            frame_paths: List of frame paths
            timestamps: Corresponding timestamps
            
        Returns:
            List of detected motion sequences
        """
        sequences = []
        
        if len(frame_paths) < 2:
            return sequences
        
        logger.info(f"Detecting camera motion across {len(frame_paths)} frames")
        
        current_motion = None
        sequence_start = 0
        
        for i in range(1, len(frame_paths)):
            motion = self.detect_motion_opencv(frame_paths[i-1], frame_paths[i])
            
            if motion.get("motion_detected"):
                motion_type = motion.get("motion_type", "static")
                direction = motion.get("direction", "none")
                
                if current_motion is None:
                    # Start new sequence
                    current_motion = {
                        "type": motion_type,
                        "direction": direction,
                        "start": i - 1
                    }
                    sequence_start = i - 1
                elif (current_motion["type"] != motion_type or 
                      current_motion["direction"] != direction):
                    # End current sequence, start new one
                    sequences.append(CameraMotionSequence(
                        start_frame=sequence_start,
                        end_frame=i - 1,
                        start_time=timestamps[sequence_start],
                        end_time=timestamps[i - 1],
                        motion_type=current_motion["type"],
                        confidence=0.7,
                        direction=current_motion["direction"],
                        speed="medium"
                    ))
                    
                    current_motion = {
                        "type": motion_type,
                        "direction": direction,
                        "start": i - 1
                    }
                    sequence_start = i - 1
            else:
                # No motion - end any current sequence
                if current_motion is not None:
                    sequences.append(CameraMotionSequence(
                        start_frame=sequence_start,
                        end_frame=i - 1,
                        start_time=timestamps[sequence_start],
                        end_time=timestamps[i - 1],
                        motion_type=current_motion["type"],
                        confidence=0.7,
                        direction=current_motion["direction"],
                        speed="medium"
                    ))
                    current_motion = None
        
        # Close any open sequence
        if current_motion is not None:
            sequences.append(CameraMotionSequence(
                start_frame=sequence_start,
                end_frame=len(frame_paths) - 1,
                start_time=timestamps[sequence_start],
                end_time=timestamps[-1],
                motion_type=current_motion["type"],
                confidence=0.7,
                direction=current_motion["direction"],
                speed="medium"
            ))
        
        return sequences
    
    async def full_video_analysis(
        self,
        frame_paths: List[Path],
        timestamps: List[float],
        analyze_every_nth: int = 3
    ) -> Dict[str, Any]:
        """
        Perform full structured analysis of a video.
        
        Args:
            frame_paths: All extracted frame paths
            timestamps: Corresponding timestamps
            analyze_every_nth: Analyze every Nth frame with AI
            
        Returns:
            Complete video analysis with all structured data
        """
        logger.info(f"Starting full video analysis of {len(frame_paths)} frames")
        
        results = {
            "frame_analyses": [],
            "scene_boundaries": [],
            "camera_motions": [],
            "overall_style": {},
            "dominant_colors": [],
            "analysis_summary": ""
        }
        
        # 1. Detect scene boundaries (fast, uses OpenCV)
        results["scene_boundaries"] = await self.detect_scene_boundaries(
            frame_paths, timestamps
        )
        
        # 2. Detect camera motion sequences (fast, uses OpenCV)
        results["camera_motions"] = await self.detect_camera_motion_sequence(
            frame_paths, timestamps
        )
        
        # 3. Analyze key frames with AI (slower, selective)
        frames_to_analyze = list(range(0, len(frame_paths), analyze_every_nth))
        
        # Also include frames at scene boundaries
        for boundary in results["scene_boundaries"]:
            if boundary.frame_index not in frames_to_analyze:
                frames_to_analyze.append(boundary.frame_index)
        
        frames_to_analyze.sort()
        
        logger.info(f"Analyzing {len(frames_to_analyze)} key frames with AI")
        
        for idx in frames_to_analyze:
            try:
                analysis = await self.analyze_frame_structured(
                    frame_paths[idx],
                    frame_index=idx,
                    timestamp=timestamps[idx]
                )
                results["frame_analyses"].append(asdict(analysis))
            except Exception as e:
                logger.error(f"Failed to analyze frame {idx}: {e}")
        
        # 4. Aggregate style information
        if results["frame_analyses"]:
            results["overall_style"] = self._aggregate_style(results["frame_analyses"])
            results["dominant_colors"] = self._extract_dominant_colors(results["frame_analyses"])
        
        # 5. Generate summary
        results["analysis_summary"] = self._generate_summary(results)
        
        logger.success(f"Full video analysis complete: {len(results['frame_analyses'])} frames, "
                      f"{len(results['scene_boundaries'])} scene cuts, "
                      f"{len(results['camera_motions'])} motion sequences")
        
        return results
    
    def _aggregate_style(self, frame_analyses: List[Dict]) -> Dict[str, Any]:
        """Aggregate style information from multiple frames"""
        if not frame_analyses:
            return {}
        
        # Count occurrences
        shot_types = {}
        angles = {}
        lightings = {}
        moods = {}
        
        for fa in frame_analyses:
            camera = fa.get("camera", {})
            lighting = fa.get("lighting", {})
            color = fa.get("color_palette", {})
            
            shot = camera.get("shot_type", "unknown")
            shot_types[shot] = shot_types.get(shot, 0) + 1
            
            angle = camera.get("angle", "unknown")
            angles[angle] = angles.get(angle, 0) + 1
            
            light = lighting.get("type", "unknown")
            lightings[light] = lightings.get(light, 0) + 1
            
            mood = color.get("mood", "unknown")
            moods[mood] = moods.get(mood, 0) + 1
        
        return {
            "dominant_shot_type": max(shot_types, key=shot_types.get) if shot_types else "medium",
            "dominant_angle": max(angles, key=angles.get) if angles else "eye-level",
            "dominant_lighting": max(lightings, key=lightings.get) if lightings else "natural",
            "dominant_mood": max(moods, key=moods.get) if moods else "neutral",
            "shot_type_distribution": shot_types,
            "angle_distribution": angles,
            "lighting_distribution": lightings,
            "mood_distribution": moods
        }
    
    def _extract_dominant_colors(self, frame_analyses: List[Dict]) -> List[str]:
        """Extract most common colors across frames"""
        all_colors = []
        
        for fa in frame_analyses:
            palette = fa.get("color_palette", {})
            all_colors.extend(palette.get("colors", []))
            if palette.get("primary"):
                all_colors.append(palette["primary"])
        
        # Count and return top colors
        color_counts = {}
        for color in all_colors:
            color_counts[color] = color_counts.get(color, 0) + 1
        
        sorted_colors = sorted(color_counts.keys(), key=lambda c: color_counts[c], reverse=True)
        return sorted_colors[:10]
    
    def _generate_summary(self, results: Dict) -> str:
        """Generate human-readable summary"""
        summary_parts = []
        
        # Frame analysis summary
        if results["frame_analyses"]:
            style = results.get("overall_style", {})
            summary_parts.append(
                f"Visual Style: {style.get('dominant_shot_type', 'varied')} shots, "
                f"{style.get('dominant_lighting', 'mixed')} lighting, "
                f"{style.get('dominant_mood', 'neutral')} mood"
            )
        
        # Scene structure
        scene_count = len(results["scene_boundaries"]) + 1
        summary_parts.append(f"Structure: {scene_count} scenes detected")
        
        # Camera motion
        motions = results["camera_motions"]
        if motions:
            motion_types = set(m["motion_type"] for m in motions if isinstance(m, dict))
            summary_parts.append(f"Camera Work: {', '.join(motion_types)}")
        else:
            summary_parts.append("Camera Work: Mostly static")
        
        return " | ".join(summary_parts)
