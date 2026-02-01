"""
Content Analyzer Service
AI-powered video analysis with trend matching and recommendations
"""
import os
import asyncio
from typing import Dict, List, Optional, Tuple
from datetime import datetime
from loguru import logger
from sqlalchemy import create_engine, text
import openai

from .trend_cards_library import get_trend_cards_library

DATABASE_URL = os.getenv("DATABASE_URL", "postgresql://postgres:postgres@localhost:54322/postgres")
openai.api_key = os.getenv("OPENAI_API_KEY")


class ContentAnalyzer:
    """
    Analyzes video content and provides AI-powered recommendations.
    
    Features:
    - Hook type detection (text-based, visual, audio)
    - Pacing analysis (cuts per minute, scene duration)
    - On-screen text density (OCR + positioning)
    - Sentiment analysis
    - Trend matching against trend cards
    - "Do this next" recommendations
    """
    
    def __init__(self):
        self.engine = create_engine(DATABASE_URL)
        self.trend_library = get_trend_cards_library()
        logger.info("Content Analyzer initialized")
    
    async def analyze_video(
        self,
        video_id: str,
        transcript: str,
        caption: Optional[str] = None,
        hashtags: Optional[List[str]] = None,
        duration_sec: Optional[float] = None
    ) -> Dict:
        """
        Perform complete video analysis.
        
        Args:
            video_id: Video identifier
            transcript: Video transcript from Whisper
            caption: Video caption/description
            hashtags: List of hashtags
            duration_sec: Video duration in seconds
            
        Returns:
            Complete analysis results with recommendations
        """
        logger.info(f"Analyzing video {video_id}")
        
        # Create analysis job
        job_id = self._create_analysis_job(video_id)
        
        try:
            # Update status to processing
            self._update_job_status(job_id, "processing")
            
            # Run analysis steps
            hook_type = await self._detect_hook_type(transcript, caption)
            pacing = await self._analyze_pacing(transcript, duration_sec)
            text_density = await self._analyze_text_density(transcript, duration_sec)
            sentiment = await self._analyze_sentiment(transcript, caption)
            
            # Match to trend cards
            matched_cards = self._match_to_trends(caption or "", hashtags or [], transcript)
            
            # Generate AI recommendations
            recommendations = await self._generate_recommendations(
                transcript,
                caption,
                hashtags,
                hook_type,
                pacing,
                matched_cards
            )
            
            # Save results
            results = {
                "job_id": job_id,
                "video_id": video_id,
                "hook_type": hook_type,
                "pacing": pacing,
                "text_density": text_density,
                "sentiment": sentiment,
                "matched_trend_cards": [card["id"] for card in matched_cards],
                "recommendations": recommendations,
                "status": "completed"
            }
            
            self._save_analysis_results(job_id, results)
            self._update_job_status(job_id, "completed")
            
            logger.info(f"Analysis complete for video {video_id}")
            return results
            
        except Exception as e:
            logger.error(f"Analysis failed for video {video_id}: {e}")
            self._update_job_status(job_id, "failed", str(e))
            raise
    
    async def _detect_hook_type(self, transcript: str, caption: Optional[str]) -> str:
        """
        Detect the type of hook used in the video.
        
        Returns: 'text-based', 'visual', 'audio', 'question', 'shock', 'curiosity'
        """
        prompt = f"""Analyze this video content and identify the hook type.

Transcript: {transcript[:500]}
Caption: {caption or 'N/A'}

Hook types:
- text-based: Uses on-screen text to grab attention
- visual: Uses striking visuals or imagery
- audio: Uses music, sound effects, or voice tone
- question: Starts with a question to engage viewer
- shock: Uses surprising or shocking statement
- curiosity: Creates curiosity gap ("wait for it", "you won't believe")

Return ONLY the hook type, nothing else."""

        try:
            response = await openai.ChatCompletion.acreate(
                model="gpt-4",
                messages=[
                    {"role": "system", "content": "You are a video content analyst."},
                    {"role": "user", "content": prompt}
                ],
                temperature=0.3,
                max_tokens=50
            )
            
            hook_type = response.choices[0].message.content.strip().lower()
            return hook_type
            
        except Exception as e:
            logger.error(f"Hook detection failed: {e}")
            return "unknown"
    
    async def _analyze_pacing(self, transcript: str, duration_sec: Optional[float]) -> Dict:
        """
        Analyze video pacing based on transcript and duration.
        
        Returns pacing metrics
        """
        if not transcript or not duration_sec:
            return {"speed": "unknown", "cuts_per_minute": 0}
        
        # Estimate cuts from transcript breaks
        sentences = transcript.split('.')
        estimated_cuts = len([s for s in sentences if len(s.strip()) > 10])
        cuts_per_minute = (estimated_cuts / duration_sec) * 60 if duration_sec > 0 else 0
        
        # Determine pacing speed
        if cuts_per_minute > 20:
            speed = "fast"
        elif cuts_per_minute > 10:
            speed = "medium"
        else:
            speed = "slow"
        
        return {
            "speed": speed,
            "cuts_per_minute": round(cuts_per_minute, 1),
            "estimated_scenes": estimated_cuts
        }
    
    async def _analyze_text_density(self, transcript: str, duration_sec: Optional[float]) -> float:
        """
        Calculate text density (words per second).
        
        Returns words per second
        """
        if not transcript or not duration_sec or duration_sec == 0:
            return 0.0
        
        word_count = len(transcript.split())
        words_per_second = word_count / duration_sec
        
        return round(words_per_second, 2)
    
    async def _analyze_sentiment(self, transcript: str, caption: Optional[str]) -> str:
        """
        Analyze sentiment of the content.
        
        Returns: 'positive', 'neutral', 'negative'
        """
        text = f"{transcript} {caption or ''}"
        
        prompt = f"""Analyze the sentiment of this content.

Content: {text[:500]}

Return ONLY one word: positive, neutral, or negative"""

        try:
            response = await openai.ChatCompletion.acreate(
                model="gpt-4",
                messages=[
                    {"role": "system", "content": "You are a sentiment analysis expert."},
                    {"role": "user", "content": prompt}
                ],
                temperature=0.3,
                max_tokens=10
            )
            
            sentiment = response.choices[0].message.content.strip().lower()
            return sentiment if sentiment in ['positive', 'neutral', 'negative'] else 'neutral'
            
        except Exception as e:
            logger.error(f"Sentiment analysis failed: {e}")
            return "neutral"
    
    def _match_to_trends(self, caption: str, hashtags: List[str], transcript: str) -> List[Dict]:
        """
        Match content to trending format cards.
        
        Returns list of matched cards with confidence scores
        """
        # Use trend library's matching
        matches = self.trend_library.match_content_to_cards(caption, hashtags)
        
        # Also check transcript for additional patterns
        transcript_lower = transcript.lower()
        
        # Boost confidence if transcript supports the match
        for match in matches:
            format_type = match.get("format_type", "")
            
            # Add transcript-based confidence boost
            if format_type == "tutorial" and any(word in transcript_lower for word in ["step", "first", "next", "then"]):
                match["confidence"] = min(match["confidence"] + 0.2, 1.0)
            elif format_type == "storytelling" and any(word in transcript_lower for word in ["so", "and then", "but", "because"]):
                match["confidence"] = min(match["confidence"] + 0.2, 1.0)
            elif format_type == "pov" and "pov" in transcript_lower:
                match["confidence"] = min(match["confidence"] + 0.3, 1.0)
        
        # Sort by confidence
        matches.sort(key=lambda x: x["confidence"], reverse=True)
        
        return matches[:5]  # Return top 5 matches
    
    async def _generate_recommendations(
        self,
        transcript: str,
        caption: Optional[str],
        hashtags: Optional[List[str]],
        hook_type: str,
        pacing: Dict,
        matched_cards: List[Dict]
    ) -> List[Dict]:
        """
        Generate AI-powered "do this next" recommendations.
        
        Returns list of actionable recommendations
        """
        # Build context for GPT
        matched_formats = [card["name"] for card in matched_cards[:3]]
        
        prompt = f"""You are a social media content strategist. Analyze this Instagram Reel and provide 5 specific, actionable recommendations to improve performance.

Content Analysis:
- Transcript: {transcript[:300]}
- Caption: {caption or 'N/A'}
- Hashtags: {', '.join(hashtags[:5]) if hashtags else 'None'}
- Hook Type: {hook_type}
- Pacing: {pacing['speed']} ({pacing['cuts_per_minute']} cuts/min)
- Matched Formats: {', '.join(matched_formats)}

Provide 5 recommendations in this exact JSON format:
[
  {{
    "title": "Short title",
    "description": "Detailed actionable advice",
    "priority": "high|medium|low",
    "category": "hook|pacing|format|hashtags|caption"
  }}
]

Focus on:
1. Improving the hook to grab attention faster
2. Optimizing pacing and cuts
3. Leveraging trending formats
4. Better hashtag strategy
5. Caption improvements

Return ONLY valid JSON, no other text."""

        try:
            response = await openai.ChatCompletion.acreate(
                model="gpt-4",
                messages=[
                    {"role": "system", "content": "You are a social media content strategist specializing in Instagram Reels."},
                    {"role": "user", "content": prompt}
                ],
                temperature=0.7,
                max_tokens=1000
            )
            
            import json
            recommendations_text = response.choices[0].message.content.strip()
            
            # Extract JSON if wrapped in markdown
            if "```json" in recommendations_text:
                recommendations_text = recommendations_text.split("```json")[1].split("```")[0].strip()
            elif "```" in recommendations_text:
                recommendations_text = recommendations_text.split("```")[1].split("```")[0].strip()
            
            recommendations = json.loads(recommendations_text)
            
            return recommendations
            
        except Exception as e:
            logger.error(f"Recommendation generation failed: {e}")
            
            # Fallback recommendations
            return [
                {
                    "title": "Improve Hook",
                    "description": f"Your current hook type is '{hook_type}'. Consider starting with a question or curiosity gap to grab attention in the first 3 seconds.",
                    "priority": "high",
                    "category": "hook"
                },
                {
                    "title": "Optimize Pacing",
                    "description": f"Your pacing is {pacing['speed']} with {pacing['cuts_per_minute']} cuts per minute. Fast-paced content (15-20 cuts/min) tends to perform better.",
                    "priority": "medium",
                    "category": "pacing"
                },
                {
                    "title": "Use Trending Format",
                    "description": f"Try incorporating these trending formats: {', '.join(matched_formats[:2])}",
                    "priority": "high",
                    "category": "format"
                }
            ]
    
    def _create_analysis_job(self, video_id: str) -> str:
        """Create a new analysis job in the database"""
        with self.engine.connect() as conn:
            result = conn.execute(text("""
                INSERT INTO ig_analysis_jobs (video_id, status)
                VALUES (:video_id, 'pending')
                RETURNING id
            """), {"video_id": video_id}).fetchone()
            
            conn.commit()
            return str(result[0])
    
    def _update_job_status(self, job_id: str, status: str, error_message: Optional[str] = None):
        """Update analysis job status"""
        with self.engine.connect() as conn:
            if status == "completed":
                conn.execute(text("""
                    UPDATE ig_analysis_jobs
                    SET status = :status, completed_at = NOW()
                    WHERE id = :job_id
                """), {"status": status, "job_id": job_id})
            else:
                conn.execute(text("""
                    UPDATE ig_analysis_jobs
                    SET status = :status, error_message = :error
                    WHERE id = :job_id
                """), {"status": status, "job_id": job_id, "error": error_message})
            
            conn.commit()
    
    def _save_analysis_results(self, job_id: str, results: Dict):
        """Save analysis results to database"""
        with self.engine.connect() as conn:
            import json
            
            conn.execute(text("""
                UPDATE ig_analysis_jobs
                SET 
                    hook_type = :hook_type,
                    pacing = :pacing,
                    text_density = :text_density,
                    matched_trend_cards = :matched_cards,
                    recommendations = :recommendations
                WHERE id = :job_id
            """), {
                "job_id": job_id,
                "hook_type": results.get("hook_type"),
                "pacing": results.get("pacing", {}).get("speed"),
                "text_density": results.get("text_density"),
                "matched_cards": results.get("matched_trend_cards", []),
                "recommendations": json.dumps(results.get("recommendations", []))
            })
            
            conn.commit()
    
    def get_analysis_results(self, job_id: str) -> Optional[Dict]:
        """Get analysis results by job ID"""
        with self.engine.connect() as conn:
            result = conn.execute(text("""
                SELECT 
                    id, video_id, status, hook_type, pacing,
                    text_density, matched_trend_cards, recommendations,
                    error_message, created_at, completed_at
                FROM ig_analysis_jobs
                WHERE id = :job_id
            """), {"job_id": job_id}).fetchone()
            
            if not result:
                return None
            
            import json
            
            return {
                "job_id": str(result[0]),
                "video_id": result[1],
                "status": result[2],
                "hook_type": result[3],
                "pacing": result[4],
                "text_density": float(result[5]) if result[5] else 0,
                "matched_trend_cards": result[6] or [],
                "recommendations": json.loads(result[7]) if result[7] else [],
                "error_message": result[8],
                "created_at": result[9].isoformat() if result[9] else None,
                "completed_at": result[10].isoformat() if result[10] else None
            }


# Singleton instance
_analyzer_instance = None

def get_content_analyzer() -> ContentAnalyzer:
    """Get or create content analyzer singleton"""
    global _analyzer_instance
    if _analyzer_instance is None:
        _analyzer_instance = ContentAnalyzer()
    return _analyzer_instance
