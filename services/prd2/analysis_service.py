"""
Analysis Service for PRD2
Handles AI analysis, transcription, and pre-social scoring
"""
from uuid import UUID, uuid4
from typing import Dict, Any, List

from models.supabase_models import MediaAnalysis, MediaAsset

class AnalysisService:
    """Service to analyze media assets and generate pre-social scores"""

    def analyze_media(self, asset: MediaAsset) -> MediaAnalysis:
        """Perform full analysis pipeline on a media asset"""
        # 1. Transcribe (Mock)
        transcript = self._transcribe_audio(asset.storage_path)
        
        # 2. Vision Analysis (Mock)
        vision_data = self._analyze_frames(asset.storage_path)
        
        # 3. Calculate Score
        score, explanation = self._calculate_pre_social_score(vision_data, transcript)
        
        return MediaAnalysis(
            id=uuid4(),
            media_id=asset.id,
            transcript=transcript,
            transcript_language="en",
            topics=vision_data.get("topics", []),
            frames_sampled=vision_data.get("frames_sampled", 0),
            virality_features=vision_data.get("features", {}),
            pre_social_score=score,
            pre_social_explanation=explanation,
            ai_caption_suggestions=["Check this out #viral"],
            ai_hashtag_suggestions=["#fyp", "#trending"]
        )

    def _transcribe_audio(self, path: str) -> str:
        """Mock Whisper transcription"""
        return "This is a sample viral video transcript with hook."

    def _analyze_frames(self, path: str) -> Dict[str, Any]:
        """Mock Computer Vision analysis"""
        # In real impl, we'd use OpenAI Vision or similar
        return {
            "frames_sampled": 10,
            "topics": ["entertainment", "comedy"],
            "features": {
                "hook_strength": "high",
                "pacing": "fast",
                "face_detected": True,
                "text_overlay": True
            }
        }

    def _calculate_pre_social_score(self, vision_data: Dict[str, Any], transcript: str) -> tuple[float, str]:
        """
        Derive pre-social score (0-100) based on viral features
        Logic:
        - Hook presence: +30
        - Pacing fast: +20
        - Face detected: +20
        - Text overlay: +10
        - Transcript keywords: +20
        """
        score = 0.0
        reasons = []
        features = vision_data.get("features", {})
        
        if features.get("hook_strength") == "high":
            score += 30
            reasons.append("Strong hook detected")
            
        if features.get("pacing") == "fast":
            score += 20
            reasons.append("Fast pacing")
            
        if features.get("face_detected"):
            score += 20
            reasons.append("Face detected")
            
        if features.get("text_overlay"):
            score += 10
            reasons.append("Text overlay present")
            
        if "viral" in transcript.lower():
            score += 20
            reasons.append("Viral keywords found")
            
        return min(score, 100.0), "; ".join(reasons)
