"""Core UGC Format Detector - identifies format type with confidence scoring."""
import json
from typing import Dict, Any
from dataclasses import dataclass


@dataclass
class FormatDetectionResult:
    format: str
    confidence: float
    all_scores: Dict[str, float]
    reasoning: str


class UGCFormatClassifier:
    """Detects UGC format types from content."""

    FORMATS = ["testimonial", "unboxing", "demo", "lifestyle", "story", "trend"]

    TESTIMONIAL_KEYWORDS = [
        "love", "amazing", "best", "recommend", "worth", "review",
        "changed my life", "game changer", "seriously", "honestly",
        "can't live without", "highly recommend", "absolute", "perfect"
    ]

    UNBOXING_KEYWORDS = [
        "unboxing", "opening", "first look", "first impression", "out of box",
        "packaging", "excited", "can't wait", "let's open", "brand new"
    ]

    DEMO_KEYWORDS = [
        "how to", "tutorial", "feature", "show you", "here's how",
        "works like this", "let me show", "demonstration", "step by step"
    ]

    LIFESTYLE_KEYWORDS = [
        "lifestyle", "day in my life", "morning routine", "aesthetic",
        "sunset", "vibe", "goals", "dream", "aspire", "inspiring"
    ]

    STORY_KEYWORDS = [
        "journey", "story", "experience", "happened", "once", "told",
        "struggle", "overcome", "transformation", "before and after"
    ]

    TREND_KEYWORDS = [
        "challenge", "trend", "viral", "everyone's doing", "you should try",
        "hashtag", "#", "sound", "dance", "following trend"
    ]

    def __init__(self):
        self.format_keywords = {
            "testimonial": self.TESTIMONIAL_KEYWORDS,
            "unboxing": self.UNBOXING_KEYWORDS,
            "demo": self.DEMO_KEYWORDS,
            "lifestyle": self.LIFESTYLE_KEYWORDS,
            "story": self.STORY_KEYWORDS,
            "trend": self.TREND_KEYWORDS
        }

    def classify(self, content: str, content_metadata: Dict[str, Any] = None) -> FormatDetectionResult:
        """Detect UGC format from content."""
        content_lower = content.lower()

        scores = {}

        # Score each format
        for fmt in self.FORMATS:
            keywords = self.format_keywords[fmt]
            # Count keyword matches
            matches = sum(1 for kw in keywords if kw.lower() in content_lower)
            # Normalize to 0-1 range
            score = min(matches / len(keywords), 1.0)
            scores[fmt] = score

        # Get best match
        best_format = max(scores, key=scores.get)
        confidence = scores[best_format]

        # Boost confidence based on content length (more content = more confidence)
        content_len = len(content)
        if content_len > 500:
            confidence = min(confidence + 0.2, 1.0)
        elif content_len < 50:
            confidence = max(confidence - 0.3, 0.0)

        # Build reasoning
        top_formats = sorted(scores.items(), key=lambda x: x[1], reverse=True)[:3]
        top_matches = ", ".join([f"{fmt} ({score:.0%})" for fmt, score in top_formats])
        reasoning = f"Detected based on keyword analysis. Top matches: {top_matches}"

        return FormatDetectionResult(
            format=best_format,
            confidence=min(confidence, 1.0),
            all_scores=scores,
            reasoning=reasoning
        )
