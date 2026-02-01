"""
Vision Analysis Service using OpenAI Vision API
Analyzes video frames for shot types, objects, text, and visual patterns
"""
import base64
import logging
from typing import Dict, List, Optional, Any
from pathlib import Path
import json
from openai import OpenAI
from config import get_settings

logger = logging.getLogger(__name__)
settings = get_settings()


class VisionAnalyzer:
    """Analyzes video frames using OpenAI Vision API"""
    
    def __init__(self, api_key: Optional[str] = None):
        """
        Initialize vision analyzer
        
        Args:
            api_key: OpenAI API key (defaults to settings.OPENAI_API_KEY)
        """
        self.api_key = api_key or settings.OPENAI_API_KEY
        self.client = OpenAI(api_key=self.api_key) if self.api_key else None
        
        if not self.client:
            logger.warning("OpenAI API key not configured - vision analysis disabled")
    
    def is_enabled(self) -> bool:
        """Check if vision analysis is enabled"""
        return self.client is not None
    
    def encode_image(self, image_path: str) -> str:
        """
        Encode image to base64
        
        Args:
            image_path: Path to image file
            
        Returns:
            Base64 encoded image string
        """
        with open(image_path, "rb") as image_file:
            return base64.b64encode(image_file.read()).decode('utf-8')
    
    def analyze_frame(
        self,
        image_path: str,
        analysis_type: str = "comprehensive"
    ) -> Dict[str, Any]:
        """
        Analyze a single frame using OpenAI Vision
        
        Args:
            image_path: Path to frame image
            analysis_type: Type of analysis ('comprehensive', 'quick', 'hook', 'text')
            
        Returns:
            Analysis results dict
        """
        if not self.is_enabled():
            logger.error("Vision analysis not enabled - missing API key")
            return {}
        
        try:
            # Encode image
            base64_image = self.encode_image(image_path)
            
            # Select prompt based on analysis type
            prompt = self._get_analysis_prompt(analysis_type)
            
            # Call OpenAI Vision API
            response = self.client.chat.completions.create(
                model="gpt-4o",  # GPT-4 Vision model
                messages=[
                    {
                        "role": "user",
                        "content": [
                            {
                                "type": "text",
                                "text": prompt
                            },
                            {
                                "type": "image_url",
                                "image_url": {
                                    "url": f"data:image/jpeg;base64,{base64_image}",
                                    "detail": "high"  # High detail for better analysis
                                }
                            }
                        ]
                    }
                ],
                max_tokens=500,
                temperature=0.3  # Lower temperature for more consistent analysis
            )
            
            # Parse response
            content = response.choices[0].message.content
            
            # Try to parse as JSON if it looks like JSON
            if content.strip().startswith('{'):
                try:
                    return json.loads(content)
                except json.JSONDecodeError:
                    pass
            
            # Return structured format
            return {
                "raw_analysis": content,
                "model": response.model,
                "usage": {
                    "prompt_tokens": response.usage.prompt_tokens,
                    "completion_tokens": response.usage.completion_tokens,
                    "total_tokens": response.usage.total_tokens
                }
            }
            
        except Exception as e:
            logger.error(f"Error analyzing frame {image_path}: {e}")
            return {"error": str(e)}
    
    def _get_analysis_prompt(self, analysis_type: str) -> str:
        """Get the appropriate prompt for analysis type"""
        
        if analysis_type == "comprehensive":
            return """Analyze this video frame and return a JSON object with the following:
{
  "shot_type": "close_up" | "medium" | "wide" | "screen_record" | "broll",
  "presence": ["face", "full_body", "hands", "laptop", "whiteboard", "phone"],
  "text_on_screen": "any visible text (empty string if none)",
  "objects": ["list", "of", "visible", "objects"],
  "brightness_level": "dark" | "normal" | "bright",
  "color_temperature": "warm" | "neutral" | "cool",
  "visual_clutter_score": 0.0 to 1.0 (how busy/messy the frame is),
  "is_pattern_interrupt": true/false (sudden visual change, zoom, cut),
  "is_hook_frame": true/false (direct eye contact, bold text, attention-grabbing),
  "has_meme_element": true/false (reaction face, overlay, humor)
}"""
        
        elif analysis_type == "quick":
            return """Analyze this video frame briefly. Return JSON with:
{
  "shot_type": "close_up" | "medium" | "wide" | "screen_record",
  "has_face": true/false,
  "has_text": true/false,
  "is_hook_frame": true/false
}"""
        
        elif analysis_type == "hook":
            return """Is this frame a good "hook" frame for social media? Return JSON:
{
  "is_hook_frame": true/false,
  "hook_score": 0.0 to 1.0,
  "reasons": ["direct eye contact", "bold text", "pattern interrupt", etc],
  "suggestions": ["how to improve it"]
}"""
        
        elif analysis_type == "text":
            return """Extract all visible text from this image. Return JSON:
{
  "text_on_screen": "exact text visible",
  "text_locations": ["top", "center", "bottom"],
  "text_style": "caption" | "headline" | "overlay" | "ui_element",
  "is_readable": true/false
}"""
        
        else:
            return "Describe what you see in this video frame in detail."
    
    def batch_analyze_frames(
        self,
        frame_paths: List[str],
        analysis_type: str = "quick",
        max_concurrent: int = 5
    ) -> List[Dict[str, Any]]:
        """
        Analyze multiple frames (sequentially for now)
        
        Args:
            frame_paths: List of frame image paths
            analysis_type: Type of analysis
            max_concurrent: Max concurrent requests (not used yet)
            
        Returns:
            List of analysis results
        """
        results = []
        
        for i, frame_path in enumerate(frame_paths):
            logger.info(f"Analyzing frame {i+1}/{len(frame_paths)}: {frame_path}")
            result = self.analyze_frame(frame_path, analysis_type)
            results.append({
                "frame_path": frame_path,
                "analysis": result
            })
        
        return results
    
    def detect_pattern_interrupt(
        self,
        frame1_path: str,
        frame2_path: str
    ) -> Dict[str, Any]:
        """
        Compare two consecutive frames to detect pattern interrupts
        
        Args:
            frame1_path: Path to first frame
            frame2_path: Path to second frame
            
        Returns:
            Pattern interrupt analysis
        """
        if not self.is_enabled():
            return {}
        
        try:
            # Encode both images
            base64_frame1 = self.encode_image(frame1_path)
            base64_frame2 = self.encode_image(frame2_path)
            
            prompt = """Compare these two consecutive video frames. Return JSON:
{
  "has_pattern_interrupt": true/false,
  "interrupt_type": "zoom" | "cut" | "color_change" | "motion" | "none",
  "interrupt_strength": 0.0 to 1.0,
  "description": "what changed"
}"""
            
            response = self.client.chat.completions.create(
                model="gpt-4o",
                messages=[
                    {
                        "role": "user",
                        "content": [
                            {"type": "text", "text": prompt},
                            {
                                "type": "image_url",
                                "image_url": {
                                    "url": f"data:image/jpeg;base64,{base64_frame1}",
                                    "detail": "low"
                                }
                            },
                            {
                                "type": "image_url",
                                "image_url": {
                                    "url": f"data:image/jpeg;base64,{base64_frame2}",
                                    "detail": "low"
                                }
                            }
                        ]
                    }
                ],
                max_tokens=200,
                temperature=0.3
            )
            
            content = response.choices[0].message.content
            
            # Try to parse JSON
            if content.strip().startswith('{'):
                try:
                    return json.loads(content)
                except json.JSONDecodeError:
                    pass
            
            return {"raw_analysis": content}
            
        except Exception as e:
            logger.error(f"Error detecting pattern interrupt: {e}")
            return {"error": str(e)}
