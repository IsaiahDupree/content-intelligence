"""
Base AI Provider
================
Abstract base class for AI providers.
"""

import os
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional


@dataclass
class AIProviderConfig:
    """Configuration for AI provider."""
    provider: str = "openai"
    model: str = "gpt-4o-mini"
    api_key: Optional[str] = None
    temperature: float = 0.7
    max_tokens: int = 4096
    timeout: int = 60
    
    @classmethod
    def from_env(cls) -> "AIProviderConfig":
        """Load configuration from environment variables."""
        return cls(
            provider=os.getenv("AI_PROVIDER", "openai"),
            model=os.getenv("AI_MODEL", "gpt-4o-mini"),
            api_key=os.getenv("OPENAI_API_KEY") or os.getenv("ANTHROPIC_API_KEY"),
            temperature=float(os.getenv("AI_TEMPERATURE", "0.7")),
            max_tokens=int(os.getenv("AI_MAX_TOKENS", "4096")),
            timeout=int(os.getenv("AI_TIMEOUT", "60")),
        )


@dataclass
class TranscriptSegment:
    """Represents an identified segment from transcript analysis."""
    start_time: str  # MM:SS format
    end_time: str    # MM:SS format
    text: str
    relevance_score: float = 0.0
    reasoning: str = ""
    
    def to_dict(self) -> Dict[str, Any]:
        return {
            "start_time": self.start_time,
            "end_time": self.end_time,
            "text": self.text,
            "relevance_score": self.relevance_score,
            "reasoning": self.reasoning
        }
    
    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "TranscriptSegment":
        return cls(
            start_time=data.get("start_time", "00:00"),
            end_time=data.get("end_time", "00:00"),
            text=data.get("text", ""),
            relevance_score=float(data.get("relevance_score", 0.0)),
            reasoning=data.get("reasoning", "")
        )


@dataclass
class TranscriptAnalysis:
    """Result of transcript analysis."""
    segments: List[TranscriptSegment] = field(default_factory=list)
    summary: str = ""
    key_topics: List[str] = field(default_factory=list)
    total_duration: float = 0.0
    
    def to_dict(self) -> Dict[str, Any]:
        return {
            "segments": [s.to_dict() for s in self.segments],
            "summary": self.summary,
            "key_topics": self.key_topics,
            "total_duration": self.total_duration
        }


class AIProvider(ABC):
    """
    Abstract base class for AI providers.
    
    Implement this to add support for new AI services.
    """
    
    def __init__(self, config: Optional[AIProviderConfig] = None):
        self.config = config or AIProviderConfig.from_env()
    
    @property
    @abstractmethod
    def name(self) -> str:
        """Provider name (openai, anthropic, mock)."""
        pass
    
    @abstractmethod
    async def analyze_transcript(
        self,
        transcript: str,
        min_duration: int = 10,
        max_duration: int = 60,
        max_segments: int = 7
    ) -> TranscriptAnalysis:
        """
        Analyze transcript to identify engaging segments.
        
        Args:
            transcript: Formatted transcript with timestamps
            min_duration: Minimum segment duration in seconds
            max_duration: Maximum segment duration in seconds
            max_segments: Maximum number of segments to return
        
        Returns:
            TranscriptAnalysis with identified segments
        """
        pass
    
    @abstractmethod
    async def generate_clip_metadata(
        self,
        segment: TranscriptSegment,
        context: Optional[str] = None
    ) -> Dict[str, Any]:
        """
        Generate metadata for a clip (title, description, hashtags).
        
        Args:
            segment: The transcript segment
            context: Optional context about the video
        
        Returns:
            Dict with title, description, hashtags
        """
        pass
    
    @abstractmethod
    async def health_check(self) -> Dict[str, Any]:
        """
        Check if the provider is available and working.
        
        Returns:
            Dict with status, latency, model info
        """
        pass
