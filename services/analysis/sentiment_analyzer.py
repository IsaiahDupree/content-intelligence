"""
Sentiment Analysis Service (CUR-002)
=====================================
Analyze transcript sentiment using AI to score content emotional tone.

Scores transcripts on a scale of -1 (negative) to +1 (positive) with labels:
- Negative: -1.0 to -0.3
- Neutral: -0.3 to +0.3
- Positive: +0.3 to +1.0

Features:
- AI-powered sentiment analysis (GPT-4)
- Batch processing support
- Emotion detection (joy, anger, sadness, fear, surprise, disgust)
- Topic/theme extraction
- Confidence scoring
"""

import asyncio
from typing import Dict, Any, List, Optional, Literal
from dataclasses import dataclass
from loguru import logger
from openai import AsyncOpenAI
import os

from database.connection import get_db_context
from database.models import ContentAnalysis
from services.event_bus import EventBus, Topics


SentimentLabel = Literal["negative", "neutral", "positive"]


@dataclass
class SentimentResult:
    """Sentiment analysis result"""
    score: float  # -1.0 to +1.0
    label: SentimentLabel
    confidence: float  # 0.0 to 1.0
    emotions: Dict[str, float]  # Emotion scores
    themes: List[str]  # Detected themes/topics
    reasoning: str  # AI explanation


class SentimentAnalyzer:
    """
    Sentiment Analysis Service

    Uses GPT-4 to analyze transcript sentiment with:
    - Sentiment score (-1 to +1)
    - Sentiment label (negative/neutral/positive)
    - Emotion breakdown
    - Theme extraction
    - Confidence scoring
    """

    def __init__(self):
        """Initialize sentiment analyzer"""
        self.client = AsyncOpenAI(api_key=os.getenv("OPENAI_API_KEY"))
        self.model = "gpt-4o"  # Latest model with best reasoning

        # Event bus
        self.event_bus = EventBus.get_instance()
        self.event_bus.set_source("sentiment-analyzer")

        logger.info("🎭 Sentiment Analyzer initialized")

    async def analyze_text(
        self,
        text: str,
        context: Optional[str] = None
    ) -> SentimentResult:
        """
        Analyze sentiment of text

        Args:
            text: Text to analyze (transcript, caption, etc.)
            context: Additional context (optional)

        Returns:
            SentimentResult with score, label, emotions, themes
        """
        if not text or len(text.strip()) == 0:
            # Empty text is neutral
            return SentimentResult(
                score=0.0,
                label="neutral",
                confidence=1.0,
                emotions={},
                themes=[],
                reasoning="Empty text"
            )

        # Construct prompt
        system_prompt = """You are a sentiment analysis expert. Analyze the emotional tone of the given text and provide:

1. **Sentiment Score**: A number from -1.0 (very negative) to +1.0 (very positive)
2. **Sentiment Label**: "negative" (-1.0 to -0.3), "neutral" (-0.3 to +0.3), or "positive" (+0.3 to +1.0)
3. **Confidence**: How confident you are in this analysis (0.0 to 1.0)
4. **Emotions**: Scores for: joy, anger, sadness, fear, surprise, disgust (each 0.0 to 1.0)
5. **Themes**: List of main themes/topics in the text
6. **Reasoning**: Brief explanation of your analysis

Respond ONLY with valid JSON in this exact format:
{
  "score": -0.5,
  "label": "negative",
  "confidence": 0.9,
  "emotions": {
    "joy": 0.1,
    "anger": 0.7,
    "sadness": 0.4,
    "fear": 0.2,
    "surprise": 0.0,
    "disgust": 0.3
  },
  "themes": ["theme1", "theme2"],
  "reasoning": "The text expresses frustration and disappointment..."
}"""

        user_prompt = f"""Analyze this text:

{text}"""

        if context:
            user_prompt += f"\n\nContext: {context}"

        try:
            # Call GPT-4
            response = await self.client.chat.completions.create(
                model=self.model,
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_prompt}
                ],
                response_format={"type": "json_object"},
                temperature=0.3,  # Lower temperature for more consistent analysis
            )

            # Parse response
            import json
            result_json = json.loads(response.choices[0].message.content)

            # Validate score range
            score = max(-1.0, min(1.0, result_json["score"]))

            # Auto-correct label if it doesn't match score
            if score <= -0.3:
                label = "negative"
            elif score >= 0.3:
                label = "positive"
            else:
                label = "neutral"

            return SentimentResult(
                score=score,
                label=label,
                confidence=result_json.get("confidence", 0.8),
                emotions=result_json.get("emotions", {}),
                themes=result_json.get("themes", []),
                reasoning=result_json.get("reasoning", "")
            )

        except Exception as e:
            logger.error(f"Sentiment analysis failed: {e}")
            # Return neutral on error
            return SentimentResult(
                score=0.0,
                label="neutral",
                confidence=0.0,
                emotions={},
                themes=[],
                reasoning=f"Analysis error: {str(e)}"
            )

    async def analyze_transcript(
        self,
        analysis_id: str
    ) -> Optional[SentimentResult]:
        """
        Analyze sentiment of a content analysis transcript

        Args:
            analysis_id: ID of ContentAnalysis record

        Returns:
            SentimentResult or None if no transcript
        """
        async with get_db_context() as db:
            # Get content analysis
            analysis = await db.get(ContentAnalysis, analysis_id)

            if not analysis or not analysis.transcript:
                logger.warning(f"No transcript found for analysis {analysis_id}")
                return None

            # Analyze sentiment
            result = await self.analyze_text(
                text=analysis.transcript,
                context=f"Video content: {analysis.scene_description or 'unknown'}"
            )

            # Update analysis record with sentiment data
            analysis.sentiment_score = result.score
            analysis.sentiment_label = result.label
            analysis.detected_emotions = result.emotions
            analysis.detected_themes = result.themes

            await db.commit()

            # Emit event
            await self.event_bus.publish(
                Topics.SENTIMENT_ANALYSIS_COMPLETED,
                {
                    'analysis_id': str(analysis.id),
                    'media_id': str(analysis.media_id),
                    'sentiment_score': result.score,
                    'sentiment_label': result.label,
                    'confidence': result.confidence
                }
            )

            logger.info(
                f"✓ Sentiment analyzed | "
                f"Score: {result.score:.2f} | "
                f"Label: {result.label} | "
                f"Analysis: {analysis_id[:8]}"
            )

            return result

    async def batch_analyze_unscored(
        self,
        max_items: Optional[int] = None
    ) -> Dict[str, Any]:
        """
        Batch analyze all content analyses without sentiment scores

        Args:
            max_items: Maximum number of items to process

        Returns:
            Summary of batch analysis
        """
        async with get_db_context() as db:
            from sqlalchemy import select

            # Find analyses with transcripts but no sentiment score
            query = (
                select(ContentAnalysis)
                .where(
                    ContentAnalysis.transcript.isnot(None),
                    ContentAnalysis.sentiment_score.is_(None)
                )
                .order_by(ContentAnalysis.analyzed_at.desc())
            )

            if max_items:
                query = query.limit(max_items)

            result = await db.execute(query)
            analyses = result.scalars().all()

            if not analyses:
                return {
                    'success': True,
                    'message': 'No unscored analyses found',
                    'processed': 0
                }

            logger.info(f"Found {len(analyses)} analyses to score")

            # Process each analysis
            processed = 0
            failed = 0

            for analysis in analyses:
                try:
                    result = await self.analyze_transcript(str(analysis.id))
                    if result:
                        processed += 1
                except Exception as e:
                    logger.error(f"Failed to analyze {analysis.id}: {e}")
                    failed += 1

            return {
                'success': True,
                'total': len(analyses),
                'processed': processed,
                'failed': failed,
                'percent_complete': (processed / len(analyses) * 100) if analyses else 0
            }

    async def get_sentiment_stats(self) -> Dict[str, Any]:
        """
        Get statistics on sentiment distribution

        Returns:
            Statistics dictionary
        """
        async with get_db_context() as db:
            from sqlalchemy import select, func

            # Total with transcripts
            total_result = await db.execute(
                select(func.count(ContentAnalysis.id))
                .where(ContentAnalysis.transcript.isnot(None))
            )
            total = total_result.scalar() or 0

            # Scored
            scored_result = await db.execute(
                select(func.count(ContentAnalysis.id))
                .where(ContentAnalysis.sentiment_score.isnot(None))
            )
            scored = scored_result.scalar() or 0

            # Distribution
            positive_result = await db.execute(
                select(func.count(ContentAnalysis.id))
                .where(ContentAnalysis.sentiment_label == 'positive')
            )
            positive = positive_result.scalar() or 0

            neutral_result = await db.execute(
                select(func.count(ContentAnalysis.id))
                .where(ContentAnalysis.sentiment_label == 'neutral')
            )
            neutral = neutral_result.scalar() or 0

            negative_result = await db.execute(
                select(func.count(ContentAnalysis.id))
                .where(ContentAnalysis.sentiment_label == 'negative')
            )
            negative = negative_result.scalar() or 0

            # Average score
            avg_result = await db.execute(
                select(func.avg(ContentAnalysis.sentiment_score))
                .where(ContentAnalysis.sentiment_score.isnot(None))
            )
            avg_score = avg_result.scalar() or 0.0

            return {
                'total_transcripts': total,
                'scored': scored,
                'unscored': total - scored,
                'percent_scored': (scored / total * 100) if total > 0 else 0,
                'distribution': {
                    'positive': positive,
                    'neutral': neutral,
                    'negative': negative
                },
                'average_score': round(float(avg_score), 3)
            }


# Singleton instance
_sentiment_analyzer: Optional[SentimentAnalyzer] = None


def get_sentiment_analyzer() -> SentimentAnalyzer:
    """Get singleton instance of SentimentAnalyzer"""
    global _sentiment_analyzer
    if _sentiment_analyzer is None:
        _sentiment_analyzer = SentimentAnalyzer()
    return _sentiment_analyzer
