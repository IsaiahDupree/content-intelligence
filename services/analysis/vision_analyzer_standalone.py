"""
Vision Analysis Service (Standalone)
=====================================
Analyzes images using OpenAI Vision API for:
- Shot types and framing
- Object detection
- Text extraction
- Hook potential scoring
- Pattern interrupt detection

Standalone version without MediaPoster dependencies.
"""
import os
import base64
import json
from typing import Dict, Any, List, Optional
from dataclasses import dataclass, asdict


@dataclass
class VisionAnalysisResult:
    """Complete vision analysis result"""
    shot_type: str  # close_up, medium, wide, screen_record, broll
    has_face: bool
    has_text: bool
    text_on_screen: str
    objects: List[str]
    is_hook_frame: bool
    hook_score: float
    is_pattern_interrupt: bool
    brightness: str  # dark, normal, bright
    color_temperature: str  # warm, neutral, cool
    suggestions: List[str]
    raw_response: Optional[str] = None
    
    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


class VisionAnalyzerStandalone:
    """
    Vision analyzer using OpenAI Vision API.
    
    Analyzes images for content creation insights including
    shot types, hook potential, and visual elements.
    """
    
    ANALYSIS_PROMPTS = {
        "comprehensive": """Analyze this video frame and return a JSON object with:
{
  "shot_type": "close_up" | "medium" | "wide" | "screen_record" | "broll",
  "has_face": true/false,
  "has_text": true/false,
  "text_on_screen": "any visible text (empty if none)",
  "objects": ["list", "of", "visible", "objects"],
  "is_hook_frame": true/false (attention-grabbing frame),
  "hook_score": 0.0 to 1.0,
  "is_pattern_interrupt": true/false,
  "brightness": "dark" | "normal" | "bright",
  "color_temperature": "warm" | "neutral" | "cool",
  "suggestions": ["how to improve for social media"]
}
Return ONLY valid JSON, no other text.""",

        "quick": """Analyze this frame briefly. Return JSON only:
{
  "shot_type": "close_up" | "medium" | "wide" | "screen_record",
  "has_face": true/false,
  "has_text": true/false,
  "is_hook_frame": true/false,
  "hook_score": 0.0 to 1.0
}""",

        "hook": """Rate this frame as a social media hook. Return JSON only:
{
  "is_hook_frame": true/false,
  "hook_score": 0.0 to 1.0,
  "reasons": ["why it works or doesn't"],
  "improvements": ["suggestions to make it better"]
}""",

        "text": """Extract all visible text. Return JSON only:
{
  "text_on_screen": "exact text visible",
  "text_locations": ["top", "center", "bottom"],
  "text_style": "caption" | "headline" | "overlay" | "none",
  "is_readable": true/false
}"""
    }
    
    def __init__(self, api_key: Optional[str] = None):
        """Initialize vision analyzer with OpenAI API key."""
        self.api_key = api_key or os.getenv("OPENAI_API_KEY", "")
        self.client = None
        
        if self.api_key:
            try:
                from openai import OpenAI
                self.client = OpenAI(api_key=self.api_key)
            except ImportError:
                pass
    
    def is_enabled(self) -> bool:
        """Check if vision analysis is available."""
        return self.client is not None and bool(self.api_key)
    
    def encode_image(self, image_path: str) -> str:
        """Encode image file to base64."""
        with open(image_path, "rb") as f:
            return base64.b64encode(f.read()).decode("utf-8")
    
    def encode_image_bytes(self, image_bytes: bytes) -> str:
        """Encode image bytes to base64."""
        return base64.b64encode(image_bytes).decode("utf-8")
    
    def analyze(
        self,
        image_path: Optional[str] = None,
        image_bytes: Optional[bytes] = None,
        image_base64: Optional[str] = None,
        analysis_type: str = "comprehensive"
    ) -> VisionAnalysisResult:
        """
        Analyze an image using OpenAI Vision API.
        
        Args:
            image_path: Path to image file
            image_bytes: Raw image bytes
            image_base64: Base64 encoded image
            analysis_type: Type of analysis (comprehensive, quick, hook, text)
            
        Returns:
            VisionAnalysisResult with analysis data
        """
        # Default result for errors/fallback
        default_result = VisionAnalysisResult(
            shot_type="unknown",
            has_face=False,
            has_text=False,
            text_on_screen="",
            objects=[],
            is_hook_frame=False,
            hook_score=0.0,
            is_pattern_interrupt=False,
            brightness="normal",
            color_temperature="neutral",
            suggestions=[]
        )
        
        if not self.is_enabled():
            default_result.suggestions = ["Vision API not available - set OPENAI_API_KEY"]
            return default_result
        
        # Get base64 image
        try:
            if image_path:
                base64_image = self.encode_image(image_path)
            elif image_bytes:
                base64_image = self.encode_image_bytes(image_bytes)
            elif image_base64:
                base64_image = image_base64
            else:
                default_result.suggestions = ["No image provided"]
                return default_result
        except Exception as e:
            default_result.suggestions = [f"Error encoding image: {e}"]
            return default_result
        
        # Get prompt
        prompt = self.ANALYSIS_PROMPTS.get(analysis_type, self.ANALYSIS_PROMPTS["comprehensive"])
        
        try:
            response = self.client.chat.completions.create(
                model="gpt-4o",
                messages=[{
                    "role": "user",
                    "content": [
                        {"type": "text", "text": prompt},
                        {
                            "type": "image_url",
                            "image_url": {
                                "url": f"data:image/jpeg;base64,{base64_image}",
                                "detail": "high"
                            }
                        }
                    ]
                }],
                max_tokens=500,
                temperature=0.3
            )
            
            content = response.choices[0].message.content
            
            # Parse JSON response
            try:
                # Try to extract JSON from response
                if "{" in content:
                    json_start = content.index("{")
                    json_end = content.rindex("}") + 1
                    json_str = content[json_start:json_end]
                    data = json.loads(json_str)
                    
                    return VisionAnalysisResult(
                        shot_type=data.get("shot_type", "unknown"),
                        has_face=data.get("has_face", False),
                        has_text=data.get("has_text", False),
                        text_on_screen=data.get("text_on_screen", ""),
                        objects=data.get("objects", []),
                        is_hook_frame=data.get("is_hook_frame", False),
                        hook_score=float(data.get("hook_score", 0.0)),
                        is_pattern_interrupt=data.get("is_pattern_interrupt", False),
                        brightness=data.get("brightness", "normal"),
                        color_temperature=data.get("color_temperature", "neutral"),
                        suggestions=data.get("suggestions", data.get("improvements", [])),
                        raw_response=content
                    )
            except (json.JSONDecodeError, ValueError):
                pass
            
            # Return with raw response if parsing failed
            default_result.raw_response = content
            default_result.suggestions = ["Could not parse structured response"]
            return default_result
            
        except Exception as e:
            default_result.suggestions = [f"API error: {str(e)}"]
            return default_result


# Singleton instance
_analyzer: Optional[VisionAnalyzerStandalone] = None


def get_vision_analyzer() -> VisionAnalyzerStandalone:
    """Get singleton vision analyzer instance."""
    global _analyzer
    if _analyzer is None:
        _analyzer = VisionAnalyzerStandalone()
    return _analyzer
