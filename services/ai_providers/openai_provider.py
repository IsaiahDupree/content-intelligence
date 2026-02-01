"""
OpenAI Provider
===============
AI provider implementation using OpenAI's GPT models.
"""

import json
import logging
import time
from typing import Any, Dict, List, Optional

from .base import AIProvider, AIProviderConfig, TranscriptAnalysis, TranscriptSegment

logger = logging.getLogger(__name__)


# System prompt for segment identification
SEGMENT_ANALYSIS_PROMPT = """You are an expert at analyzing video transcripts to find the most engaging segments for short-form content creation.

SELECTION CRITERIA:
1. STRONG HOOKS - Attention-grabbing opening lines that make people stop scrolling
2. VALUABLE CONTENT - Tips, insights, interesting facts, actionable advice
3. EMOTIONAL MOMENTS - Excitement, surprise, humor, inspiration, controversy
4. COMPLETE THOUGHTS - Self-contained ideas that make sense without context
5. SHAREABLE CONTENT - Content people would want to share with others

TIMING GUIDELINES:
- Segments MUST be between {min_duration}-{max_duration} seconds
- Use EXACT timestamps from the transcript (MM:SS format)
- start_time MUST be less than end_time
- Include enough context for the segment to stand alone

OUTPUT FORMAT (JSON):
{{
    "segments": [
        {{
            "start_time": "MM:SS",
            "end_time": "MM:SS",
            "text": "exact transcript text for this segment",
            "relevance_score": 0.0-1.0,
            "reasoning": "why this segment is engaging"
        }}
    ],
    "summary": "2-3 sentence summary of the full video",
    "key_topics": ["topic1", "topic2", "topic3"]
}}

Find up to {max_segments} compelling segments. Quality over quantity."""


class OpenAIProvider(AIProvider):
    """OpenAI GPT-based AI provider."""
    
    def __init__(self, config: Optional[AIProviderConfig] = None):
        super().__init__(config)
        self._client = None
    
    @property
    def name(self) -> str:
        return "openai"
    
    def _get_client(self):
        """Lazy load OpenAI client."""
        if self._client is None:
            try:
                from openai import OpenAI
                import os
                
                api_key = self.config.api_key or os.getenv("OPENAI_API_KEY")
                if not api_key:
                    raise ValueError("OPENAI_API_KEY not set")
                
                self._client = OpenAI(api_key=api_key)
            except ImportError:
                raise ImportError("openai package not installed. Run: pip install openai")
        return self._client
    
    async def analyze_transcript(
        self,
        transcript: str,
        min_duration: int = 10,
        max_duration: int = 60,
        max_segments: int = 7
    ) -> TranscriptAnalysis:
        """Analyze transcript using GPT to find engaging segments."""
        import asyncio
        
        client = self._get_client()
        
        system_prompt = SEGMENT_ANALYSIS_PROMPT.format(
            min_duration=min_duration,
            max_duration=max_duration,
            max_segments=max_segments
        )
        
        try:
            # Run in thread pool since openai client is sync
            response = await asyncio.to_thread(
                client.chat.completions.create,
                model=self.config.model,
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": f"Analyze this transcript and find engaging segments:\n\n{transcript}"}
                ],
                response_format={"type": "json_object"},
                temperature=self.config.temperature,
                max_tokens=self.config.max_tokens
            )
            
            result = json.loads(response.choices[0].message.content)
            
            # Parse and validate segments
            segments = []
            for seg_data in result.get("segments", []):
                segment = TranscriptSegment.from_dict(seg_data)
                
                # Validate segment timing
                start_secs = self._parse_timestamp(segment.start_time)
                end_secs = self._parse_timestamp(segment.end_time)
                duration = end_secs - start_secs
                
                if duration >= min_duration and duration <= max_duration and start_secs < end_secs:
                    segments.append(segment)
                else:
                    logger.warning(f"Skipping invalid segment: {segment.start_time}-{segment.end_time} ({duration}s)")
            
            # Sort by relevance score
            segments.sort(key=lambda x: x.relevance_score, reverse=True)
            
            return TranscriptAnalysis(
                segments=segments[:max_segments],
                summary=result.get("summary", ""),
                key_topics=result.get("key_topics", []),
                total_duration=sum(
                    self._parse_timestamp(s.end_time) - self._parse_timestamp(s.start_time)
                    for s in segments[:max_segments]
                )
            )
            
        except Exception as e:
            logger.error(f"OpenAI transcript analysis failed: {e}")
            raise
    
    async def generate_clip_metadata(
        self,
        segment: TranscriptSegment,
        context: Optional[str] = None
    ) -> Dict[str, Any]:
        """Generate metadata for a clip using GPT."""
        import asyncio
        
        client = self._get_client()
        
        prompt = f"""Generate viral-worthy metadata for this video clip:

Clip text: "{segment.text}"
Duration: {segment.start_time} - {segment.end_time}
{f'Context: {context}' if context else ''}

Generate:
1. A catchy, scroll-stopping title (max 60 chars)
2. An engaging description (max 150 chars)
3. 5-7 relevant hashtags for TikTok/Instagram

OUTPUT FORMAT (JSON):
{{
    "title": "catchy title here",
    "description": "engaging description",
    "hashtags": ["#hashtag1", "#hashtag2", ...]
}}"""

        try:
            response = await asyncio.to_thread(
                client.chat.completions.create,
                model=self.config.model,
                messages=[
                    {"role": "user", "content": prompt}
                ],
                response_format={"type": "json_object"},
                temperature=0.8,
                max_tokens=500
            )
            
            return json.loads(response.choices[0].message.content)
            
        except Exception as e:
            logger.error(f"OpenAI metadata generation failed: {e}")
            return {
                "title": segment.text[:60] if segment.text else "Untitled Clip",
                "description": segment.reasoning or "",
                "hashtags": ["#viral", "#fyp", "#foryou"]
            }
    
    async def health_check(self) -> Dict[str, Any]:
        """Check OpenAI API availability."""
        import asyncio
        
        start_time = time.time()
        
        try:
            client = self._get_client()
            
            response = await asyncio.to_thread(
                client.chat.completions.create,
                model=self.config.model,
                messages=[{"role": "user", "content": "Say 'OK' if you're working."}],
                max_tokens=10
            )
            
            latency = time.time() - start_time
            
            return {
                "status": "healthy",
                "provider": self.name,
                "model": self.config.model,
                "latency_ms": int(latency * 1000),
                "response": response.choices[0].message.content
            }
            
        except Exception as e:
            return {
                "status": "unhealthy",
                "provider": self.name,
                "model": self.config.model,
                "error": str(e),
                "latency_ms": int((time.time() - start_time) * 1000)
            }
    
    def _parse_timestamp(self, timestamp: str) -> float:
        """Parse MM:SS to seconds."""
        try:
            parts = timestamp.strip().split(':')
            if len(parts) == 2:
                return int(parts[0]) * 60 + int(parts[1])
            elif len(parts) == 3:
                return int(parts[0]) * 3600 + int(parts[1]) * 60 + int(parts[2])
            return float(timestamp)
        except Exception:
            return 0.0
