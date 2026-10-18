"""
Sentiment Analysis Service (Standalone)
========================================
Analyze text sentiment using rule-based patterns and optional AI.

Scores text on a scale of -1 (negative) to +1 (positive) with labels:
- Negative: -1.0 to -0.3
- Neutral: -0.3 to +0.3
- Positive: +0.3 to +1.0

This is a standalone version without database/event_bus dependencies.
"""

import re
from typing import Dict, List, Optional, Literal
from dataclasses import dataclass


SentimentLabel = Literal["negative", "neutral", "positive"]


@dataclass
class SentimentResult:
    """Sentiment analysis result"""
    score: float  # -1.0 to +1.0
    label: SentimentLabel
    confidence: float  # 0.0 to 1.0
    emotions: Dict[str, float]  # Emotion scores
    themes: List[str]  # Detected themes/topics
    reasoning: str  # Explanation


class SentimentAnalyzerStandalone:
    """
    Rule-based Sentiment Analyzer
    
    Uses pattern matching for fast sentiment analysis without AI dependencies.
    """
    
    # Positive patterns and words
    POSITIVE_PATTERNS = [
        r'\b(love|amazing|awesome|great|excellent|fantastic|wonderful|brilliant)\b',
        r'\b(happy|excited|thrilled|delighted|pleased|grateful|thankful)\b',
        r'\b(best|perfect|incredible|outstanding|exceptional|superb)\b',
        r'\b(success|win|winning|winner|achieve|achieved|accomplish)\b',
        r'\b(beautiful|inspiring|motivating|empowering|uplifting)\b',
        r'\b(thank you|thanks|appreciate|blessed|fortunate)\b',
        r'\b(recommend|highly recommend|must try|game changer)\b',
        r'[!]{1,3}',  # Exclamation marks (mild positive)
        r'❤️|🔥|💪|🙌|👏|✨|🎉|💯',  # Positive emojis
    ]
    
    # Negative patterns and words
    NEGATIVE_PATTERNS = [
        r'\b(hate|terrible|awful|horrible|disgusting|worst)\b',
        r'\b(sad|angry|frustrated|disappointed|annoyed|upset)\b',
        r'\b(fail|failed|failure|losing|lost|waste|wasted)\b',
        r'\b(never|can\'t|won\'t|don\'t|shouldn\'t|wouldn\'t)\b',
        r'\b(problem|issue|bug|broken|doesn\'t work|not working)\b',
        r'\b(scam|fake|fraud|ripoff|rip off|overpriced)\b',
        r'\b(boring|dull|useless|pointless|meaningless)\b',
        r'\b(struggle|struggling|difficult|hard|tough|pain)\b',
        r'😢|😭|😡|😤|💔|👎',  # Negative emojis
    ]
    
    # Emotion keywords
    EMOTION_KEYWORDS = {
        'joy': ['happy', 'excited', 'love', 'amazing', 'wonderful', 'great', 'best', 'thrilled'],
        'anger': ['angry', 'frustrated', 'annoyed', 'hate', 'furious', 'mad', 'outraged'],
        'sadness': ['sad', 'disappointed', 'depressed', 'unhappy', 'heartbroken', 'crying'],
        'fear': ['scared', 'afraid', 'worried', 'anxious', 'nervous', 'terrified', 'panic'],
        'surprise': ['surprised', 'shocked', 'amazed', 'astonished', 'unexpected', 'wow'],
        'disgust': ['disgusted', 'gross', 'nasty', 'revolting', 'sick', 'terrible'],
    }
    
    def __init__(self):
        """Initialize the sentiment analyzer"""
        self.positive_regexes = [re.compile(p, re.IGNORECASE) for p in self.POSITIVE_PATTERNS]
        self.negative_regexes = [re.compile(p, re.IGNORECASE) for p in self.NEGATIVE_PATTERNS]
    
    def analyze(self, text: str, context: Optional[str] = None) -> SentimentResult:
        """
        Analyze sentiment of text
        
        Args:
            text: Text to analyze
            context: Additional context (optional)
            
        Returns:
            SentimentResult with score, label, emotions, themes
        """
        if not text or len(text.strip()) == 0:
            return SentimentResult(
                score=0.0,
                label="neutral",
                confidence=1.0,
                emotions={},
                themes=[],
                reasoning="Empty text"
            )
        
        text_lower = text.lower()
        
        # Count positive and negative matches
        positive_count = sum(1 for regex in self.positive_regexes if regex.search(text))
        negative_count = sum(1 for regex in self.negative_regexes if regex.search(text))
        
        # Calculate raw score
        total_matches = positive_count + negative_count
        if total_matches == 0:
            raw_score = 0.0
            confidence = 0.5  # Low confidence for no matches
        else:
            raw_score = (positive_count - negative_count) / total_matches
            confidence = min(0.3 + (total_matches * 0.1), 0.95)
        
        # Normalize to -1 to +1
        score = max(-1.0, min(1.0, raw_score))
        
        # Determine label
        if score < -0.3:
            label = "negative"
        elif score > 0.3:
            label = "positive"
        else:
            label = "neutral"
        
        # Detect emotions
        emotions = {}
        for emotion, keywords in self.EMOTION_KEYWORDS.items():
            emotion_matches = sum(1 for kw in keywords if kw in text_lower)
            if emotion_matches > 0:
                emotions[emotion] = min(emotion_matches * 0.3, 1.0)
        
        # Extract themes (simple keyword extraction)
        themes = []
        theme_patterns = [
            (r'\b(business|startup|entrepreneur|company)\b', 'business'),
            (r'\b(content|video|social media|tiktok|instagram|youtube)\b', 'content'),
            (r'\b(growth|scale|success|win)\b', 'growth'),
            (r'\b(marketing|sales|revenue|money)\b', 'marketing'),
            (r'\b(learn|education|tutorial|how to)\b', 'education'),
        ]
        for pattern, theme in theme_patterns:
            if re.search(pattern, text_lower):
                themes.append(theme)
        
        # Build reasoning
        reasons = []
        if positive_count > 0:
            reasons.append(f"{positive_count} positive signals")
        if negative_count > 0:
            reasons.append(f"{negative_count} negative signals")
        if not reasons:
            reasons.append("No strong sentiment signals detected")
        
        return SentimentResult(
            score=round(score, 2),
            label=label,
            confidence=round(confidence, 2),
            emotions=emotions,
            themes=themes,
            reasoning=", ".join(reasons)
        )


# Singleton instance
_analyzer: Optional[SentimentAnalyzerStandalone] = None


def get_sentiment_analyzer() -> SentimentAnalyzerStandalone:
    """Get singleton sentiment analyzer instance"""
    global _analyzer
    if _analyzer is None:
        _analyzer = SentimentAnalyzerStandalone()
    return _analyzer
