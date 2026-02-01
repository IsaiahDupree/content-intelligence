"""
Mock AI Provider
================
Mock AI provider for testing without API calls.

Returns deterministic results based on input for verifiable testing.
"""

import hashlib
import logging
import re
from typing import Any, Dict, List, Optional

from .base import AIProvider, AIProviderConfig, TranscriptAnalysis, TranscriptSegment

logger = logging.getLogger(__name__)


class MockAIProvider(AIProvider):
    """
    Mock AI provider for testing.
    
    Returns deterministic results based on transcript content,
    allowing for verifiable, repeatable tests.
    """
    
    def __init__(self, config: Optional[AIProviderConfig] = None):
        super().__init__(config)
        self._call_count = 0
        self._last_transcript = None
        self._last_analysis = None
    
    @property
    def name(self) -> str:
        return "mock"
    
    @property
    def call_count(self) -> int:
        """Number of times analyze_transcript was called."""
        return self._call_count
    
    def reset(self):
        """Reset mock state for testing."""
        self._call_count = 0
        self._last_transcript = None
        self._last_analysis = None
    
    async def analyze_transcript(
        self,
        transcript: str,
        min_duration: int = 10,
        max_duration: int = 60,
        max_segments: int = 7
    ) -> TranscriptAnalysis:
        """
        Analyze transcript with deterministic mock results.
        
        Extracts segments based on timestamp patterns in the transcript.
        Results are reproducible for the same input.
        """
        self._call_count += 1
        self._last_transcript = transcript
        
        # Parse transcript lines to find segments
        segments = self._extract_segments_from_transcript(
            transcript, min_duration, max_duration, max_segments
        )
        
        # Generate deterministic summary based on content hash
        content_hash = hashlib.md5(transcript.encode()).hexdigest()[:8]
        
        # Extract topics from transcript
        topics = self._extract_topics(transcript)
        
        analysis = TranscriptAnalysis(
            segments=segments,
            summary=f"Mock analysis of transcript ({len(transcript)} chars, hash: {content_hash})",
            key_topics=topics[:5],
            total_duration=sum(
                self._parse_timestamp(s.end_time) - self._parse_timestamp(s.start_time)
                for s in segments
            )
        )
        
        self._last_analysis = analysis
        logger.info(f"MockAIProvider: Analyzed transcript, found {len(segments)} segments")
        
        return analysis
    
    def _extract_segments_from_transcript(
        self,
        transcript: str,
        min_duration: int,
        max_duration: int,
        max_segments: int
    ) -> List[TranscriptSegment]:
        """Extract segments from transcript timestamps."""
        segments = []
        
        # Pattern: [MM:SS - MM:SS] text
        pattern = r'\[(\d+:\d+)\s*-\s*(\d+:\d+)\]\s*(.+)'
        matches = re.findall(pattern, transcript)
        
        if not matches:
            # Create mock segments if no timestamps found
            return self._create_mock_segments(transcript, min_duration, max_segments)
        
        # Group consecutive matches into segments
        current_start = None
        current_end = None
        current_text = []
        
        for i, (start, end, text) in enumerate(matches):
            if current_start is None:
                current_start = start
            current_end = end
            current_text.append(text.strip())
            
            # Check if we should close this segment
            duration = self._parse_timestamp(current_end) - self._parse_timestamp(current_start)
            
            if duration >= min_duration:
                # Create segment
                relevance = self._calculate_relevance(current_text)
                segments.append(TranscriptSegment(
                    start_time=current_start,
                    end_time=current_end,
                    text=' '.join(current_text),
                    relevance_score=relevance,
                    reasoning=f"Mock segment {len(segments) + 1}: Contains {len(current_text)} lines"
                ))
                
                # Reset for next segment
                current_start = None
                current_end = None
                current_text = []
                
                if len(segments) >= max_segments:
                    break
        
        # Sort by relevance
        segments.sort(key=lambda x: x.relevance_score, reverse=True)
        
        return segments[:max_segments]
    
    def _create_mock_segments(
        self,
        transcript: str,
        min_duration: int,
        max_segments: int
    ) -> List[TranscriptSegment]:
        """Create mock segments when no timestamps are found."""
        segments = []
        words = transcript.split()
        
        # Create segments based on word count
        words_per_segment = max(20, len(words) // max_segments)
        
        for i in range(min(max_segments, 3)):
            start_sec = i * min_duration
            end_sec = start_sec + min_duration + 5
            
            start_word = i * words_per_segment
            end_word = min(start_word + words_per_segment, len(words))
            
            text = ' '.join(words[start_word:end_word])
            
            segments.append(TranscriptSegment(
                start_time=self._format_timestamp(start_sec),
                end_time=self._format_timestamp(end_sec),
                text=text or f"Mock segment {i + 1}",
                relevance_score=0.8 - (i * 0.1),
                reasoning=f"Mock segment {i + 1} (no timestamps in input)"
            ))
        
        return segments
    
    def _calculate_relevance(self, text_lines: List[str]) -> float:
        """Calculate mock relevance score based on text features."""
        text = ' '.join(text_lines).lower()
        
        # Higher score for engaging words
        engaging_words = ['amazing', 'incredible', 'secret', 'tip', 'hack', 'learn', 
                         'discover', 'why', 'how', 'best', 'top', 'must', 'never',
                         'always', 'actually', 'honestly', 'literally']
        
        score = 0.5
        for word in engaging_words:
            if word in text:
                score += 0.05
        
        # Cap at 0.95
        return min(0.95, score)
    
    def _extract_topics(self, transcript: str) -> List[str]:
        """Extract mock topics from transcript."""
        text = transcript.lower()
        
        # Common topic keywords
        topic_keywords = {
            'productivity': ['productivity', 'efficient', 'workflow', 'organize'],
            'lifestyle': ['morning', 'routine', 'life', 'day'],
            'technology': ['tech', 'app', 'software', 'device', 'phone'],
            'business': ['business', 'money', 'income', 'revenue', 'startup'],
            'health': ['health', 'fitness', 'workout', 'exercise', 'diet'],
            'education': ['learn', 'study', 'course', 'tutorial', 'how to'],
            'entertainment': ['fun', 'funny', 'game', 'music', 'movie'],
        }
        
        topics = []
        for topic, keywords in topic_keywords.items():
            if any(kw in text for kw in keywords):
                topics.append(topic)
        
        return topics if topics else ['general']
    
    async def generate_clip_metadata(
        self,
        segment: TranscriptSegment,
        context: Optional[str] = None
    ) -> Dict[str, Any]:
        """Generate mock metadata for a clip."""
        # Create deterministic title from text
        words = segment.text.split()[:5]
        title = ' '.join(words).title() + "..." if len(segment.text) > 30 else segment.text
        
        return {
            "title": title[:60],
            "description": f"Mock description for segment at {segment.start_time}",
            "hashtags": ["#mock", "#test", "#viral", "#fyp", "#foryou"]
        }
    
    async def health_check(self) -> Dict[str, Any]:
        """Mock health check always returns healthy."""
        return {
            "status": "healthy",
            "provider": self.name,
            "model": "mock-model",
            "latency_ms": 1,
            "response": "OK (mock)"
        }
    
    def _parse_timestamp(self, timestamp: str) -> float:
        """Parse MM:SS to seconds."""
        try:
            parts = timestamp.strip().split(':')
            if len(parts) == 2:
                return int(parts[0]) * 60 + int(parts[1])
            return float(timestamp)
        except Exception:
            return 0.0
    
    def _format_timestamp(self, seconds: int) -> str:
        """Format seconds to MM:SS."""
        minutes = seconds // 60
        secs = seconds % 60
        return f"{minutes:02d}:{secs:02d}"
