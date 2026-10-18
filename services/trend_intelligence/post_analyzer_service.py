"""
Post Analyzer Service
=====================
Analyzes video content and provides detailed scoring:
- Hook Score (1-10)
- Body Score (1-10)
- Visual Score (1-10)
- Audio Score (1-10)
- Pacing Score (1-10)
- CTA Score (1-10)

Uses AI to evaluate content quality and provide actionable feedback.
"""
import os
import json
import asyncio
from datetime import datetime, timezone
from typing import List, Dict, Any, Optional
from dataclasses import dataclass, field, asdict

from openai import AsyncOpenAI
from loguru import logger

OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "")


@dataclass
class ScoreBreakdown:
    """Detailed breakdown for a single score category"""
    score: float              # 1-10
    reason: str               # Why this score
    strengths: List[str]      # What's working
    improvements: List[str]   # What to improve


@dataclass
class PostAnalysisResult:
    """Complete post analysis result"""
    # Overall
    overall_score: float
    grade: str                # A, B, C, D, F
    
    # Individual scores
    hook_score: float
    body_score: float
    visual_score: float
    audio_score: float
    pacing_score: float
    cta_score: float
    
    # Detailed breakdowns
    hook_breakdown: ScoreBreakdown
    body_breakdown: ScoreBreakdown
    visual_breakdown: ScoreBreakdown
    audio_breakdown: ScoreBreakdown
    pacing_breakdown: ScoreBreakdown
    cta_breakdown: ScoreBreakdown
    
    # Content analysis
    detected_hook: str
    detected_cta: str
    key_points: List[str]
    
    # Recommendations
    top_strengths: List[str]
    top_improvements: List[str]
    quick_wins: List[str]
    
    # Viral potential
    viral_score: float        # 1-10
    viral_factors: List[str]
    
    # Metadata
    content_type: str         # "talking_head", "voiceover", "b_roll", "screen_record"
    estimated_duration: str
    analyzed_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    
    def to_dict(self) -> Dict:
        return {
            **asdict(self),
            "hook_breakdown": asdict(self.hook_breakdown),
            "body_breakdown": asdict(self.body_breakdown),
            "visual_breakdown": asdict(self.visual_breakdown),
            "audio_breakdown": asdict(self.audio_breakdown),
            "pacing_breakdown": asdict(self.pacing_breakdown),
            "cta_breakdown": asdict(self.cta_breakdown),
        }


@dataclass 
class ViralForecastResult:
    """Viral potential forecast"""
    viral_potential: str      # "low", "medium", "high"
    confidence: float         # 0-1
    score: float              # 1-10
    
    # Feature scores
    hook_strength: float
    topic_relevance: float
    format_fit: float
    timing_score: float
    trend_alignment: float
    
    # Audience estimates
    estimated_reach: Dict[str, int]    # {min, max}
    estimated_engagement: Dict[str, Any]
    audience_match: float
    
    # Factors
    positive_factors: List[str]
    negative_factors: List[str]
    suggestions: List[str]
    
    # Comparison
    percentile: int           # vs similar content
    
    analyzed_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    
    def to_dict(self) -> Dict:
        return asdict(self)


class PostAnalyzerService:
    """
    Analyzes video/post content quality using AI.
    Provides detailed scores and actionable feedback.
    """
    
    def __init__(self, api_key: str = None):
        self.api_key = api_key or OPENAI_API_KEY
        self.client = AsyncOpenAI(api_key=self.api_key) if self.api_key else None
        self.model = "gpt-4o"
    
    async def analyze_post(
        self,
        transcript: str = None,
        caption: str = None,
        title: str = None,
        description: str = None,
        video_url: str = None,
        content_type: str = "reel"
    ) -> PostAnalysisResult:
        """
        Analyze a post and provide detailed scores.
        
        Args:
            transcript: Video transcript/script
            caption: Post caption
            title: Video title
            description: Video description
            video_url: URL to video (for future vision analysis)
            content_type: Type of content (reel, short, talking_head, etc.)
        """
        if not self.client:
            raise ValueError("OpenAI API key not configured")
        
        # Build content for analysis
        content_text = ""
        if title:
            content_text += f"TITLE: {title}\n\n"
        if transcript:
            content_text += f"TRANSCRIPT/SCRIPT:\n{transcript}\n\n"
        if caption:
            content_text += f"CAPTION:\n{caption}\n\n"
        if description:
            content_text += f"DESCRIPTION:\n{description}\n\n"
        
        if not content_text.strip():
            raise ValueError("At least one of transcript, caption, title, or description is required")
        
        prompt = f"""Analyze this {content_type} content and provide detailed scoring.

{content_text}

Output ONLY valid JSON (no markdown):
{{
  "overall_score": <1-10>,
  "grade": "<A/B/C/D/F>",
  
  "scores": {{
    "hook": {{
      "score": <1-10>,
      "reason": "Why this score",
      "strengths": ["strength 1", "strength 2"],
      "improvements": ["improvement 1", "improvement 2"]
    }},
    "body": {{
      "score": <1-10>,
      "reason": "Why this score",
      "strengths": ["..."],
      "improvements": ["..."]
    }},
    "visual": {{
      "score": <1-10>,
      "reason": "Assessment based on described content",
      "strengths": ["..."],
      "improvements": ["..."]
    }},
    "audio": {{
      "score": <1-10>,
      "reason": "Assessment based on script pacing/flow",
      "strengths": ["..."],
      "improvements": ["..."]
    }},
    "pacing": {{
      "score": <1-10>,
      "reason": "How well the content flows",
      "strengths": ["..."],
      "improvements": ["..."]
    }},
    "cta": {{
      "score": <1-10>,
      "reason": "Effectiveness of call to action",
      "strengths": ["..."],
      "improvements": ["..."]
    }}
  }},
  
  "content_analysis": {{
    "detected_hook": "The hook phrase/moment identified",
    "detected_cta": "The CTA identified (or 'None found')",
    "key_points": ["Main point 1", "Main point 2", "Main point 3"],
    "content_type": "talking_head|voiceover|b_roll|screen_record|mixed"
  }},
  
  "recommendations": {{
    "top_strengths": ["Best thing 1", "Best thing 2", "Best thing 3"],
    "top_improvements": ["Fix this 1", "Fix this 2", "Fix this 3"],
    "quick_wins": ["Easy fix 1", "Easy fix 2"]
  }},
  
  "viral_assessment": {{
    "score": <1-10>,
    "factors": ["Factor driving virality 1", "Factor 2"]
  }}
}}

Scoring criteria:
- Hook (1-10): Does it grab attention in first 3 seconds? Pattern interrupt? Curiosity gap?
- Body (1-10): Is the content valuable, clear, and engaging? Good structure?
- Visual (1-10): Are visuals described/implied compelling? (Assess based on content type)
- Audio (1-10): Is the script natural, conversational, easy to listen to?
- Pacing (1-10): Does it maintain attention? Good rhythm? Not too fast/slow?
- CTA (1-10): Is there a clear, compelling call to action?

Grade scale: A (9-10), B (7-8), C (5-6), D (3-4), F (1-2)"""

        try:
            response = await self.client.chat.completions.create(
                model=self.model,
                messages=[
                    {
                        "role": "system",
                        "content": "You are an expert content analyst specializing in short-form video. Provide detailed, actionable feedback. Output ONLY valid JSON."
                    },
                    {"role": "user", "content": prompt}
                ],
                temperature=0.7,
                max_tokens=3000
            )
            
            content = response.choices[0].message.content.strip()
            if content.startswith("```"):
                content = content.split("```")[1]
                if content.startswith("json"):
                    content = content[4:]
            content = content.strip()
            
            data = json.loads(content)
            
            scores = data.get("scores", {})
            content_analysis = data.get("content_analysis", {})
            recommendations = data.get("recommendations", {})
            viral = data.get("viral_assessment", {})
            
            def make_breakdown(score_data: Dict) -> ScoreBreakdown:
                return ScoreBreakdown(
                    score=score_data.get("score", 5),
                    reason=score_data.get("reason", ""),
                    strengths=score_data.get("strengths", []),
                    improvements=score_data.get("improvements", [])
                )
            
            return PostAnalysisResult(
                overall_score=data.get("overall_score", 5),
                grade=data.get("grade", "C"),
                hook_score=scores.get("hook", {}).get("score", 5),
                body_score=scores.get("body", {}).get("score", 5),
                visual_score=scores.get("visual", {}).get("score", 5),
                audio_score=scores.get("audio", {}).get("score", 5),
                pacing_score=scores.get("pacing", {}).get("score", 5),
                cta_score=scores.get("cta", {}).get("score", 5),
                hook_breakdown=make_breakdown(scores.get("hook", {})),
                body_breakdown=make_breakdown(scores.get("body", {})),
                visual_breakdown=make_breakdown(scores.get("visual", {})),
                audio_breakdown=make_breakdown(scores.get("audio", {})),
                pacing_breakdown=make_breakdown(scores.get("pacing", {})),
                cta_breakdown=make_breakdown(scores.get("cta", {})),
                detected_hook=content_analysis.get("detected_hook", ""),
                detected_cta=content_analysis.get("detected_cta", ""),
                key_points=content_analysis.get("key_points", []),
                top_strengths=recommendations.get("top_strengths", []),
                top_improvements=recommendations.get("top_improvements", []),
                quick_wins=recommendations.get("quick_wins", []),
                viral_score=viral.get("score", 5),
                viral_factors=viral.get("factors", []),
                content_type=content_analysis.get("content_type", content_type),
                estimated_duration=""
            )
            
        except json.JSONDecodeError as e:
            logger.error(f"JSON parse error in post analysis: {e}")
            raise ValueError(f"Failed to parse AI response: {e}")
        except Exception as e:
            logger.error(f"Post analysis error: {e}")
            raise
    
    async def forecast_viral_potential(
        self,
        topic: str,
        hook: str = None,
        format: str = "reel",
        niche: str = None,
        target_audience: str = None
    ) -> ViralForecastResult:
        """
        Forecast viral potential for content idea.
        
        Args:
            topic: Content topic/idea
            hook: Planned hook
            format: Content format (reel, short, carousel, etc.)
            niche: Content niche
            target_audience: Target audience description
        """
        if not self.client:
            raise ValueError("OpenAI API key not configured")
        
        prompt = f"""Forecast the viral potential for this content idea:

TOPIC: {topic}
{f'HOOK: {hook}' if hook else ''}
FORMAT: {format}
{f'NICHE: {niche}' if niche else ''}
{f'TARGET AUDIENCE: {target_audience}' if target_audience else ''}

Output ONLY valid JSON (no markdown):
{{
  "viral_potential": "low|medium|high",
  "confidence": <0-1>,
  "score": <1-10>,
  
  "feature_scores": {{
    "hook_strength": <1-10>,
    "topic_relevance": <1-10>,
    "format_fit": <1-10>,
    "timing_score": <1-10>,
    "trend_alignment": <1-10>
  }},
  
  "estimates": {{
    "reach": {{"min": <number>, "max": <number>}},
    "engagement": {{
      "likes_range": [<min>, <max>],
      "comments_range": [<min>, <max>],
      "shares_range": [<min>, <max>],
      "saves_range": [<min>, <max>],
      "er_range": [<min_percent>, <max_percent>]
    }},
    "audience_match": <0-1>
  }},
  
  "factors": {{
    "positive": ["Factor 1", "Factor 2", "Factor 3"],
    "negative": ["Risk 1", "Risk 2"]
  }},
  
  "suggestions": [
    "Specific improvement 1",
    "Specific improvement 2",
    "Specific improvement 3"
  ],
  
  "percentile": <1-100>
}}

Consider:
- Current trends and algorithm preferences
- Topic saturation vs novelty
- Hook effectiveness
- Shareability factors
- Target audience alignment
- Format optimization"""

        try:
            response = await self.client.chat.completions.create(
                model=self.model,
                messages=[
                    {
                        "role": "system",
                        "content": "You are an expert social media strategist who forecasts content performance. Be realistic but optimistic. Output ONLY valid JSON."
                    },
                    {"role": "user", "content": prompt}
                ],
                temperature=0.7,
                max_tokens=2000
            )
            
            content = response.choices[0].message.content.strip()
            if content.startswith("```"):
                content = content.split("```")[1]
                if content.startswith("json"):
                    content = content[4:]
            content = content.strip()
            
            data = json.loads(content)
            
            features = data.get("feature_scores", {})
            estimates = data.get("estimates", {})
            factors = data.get("factors", {})
            
            return ViralForecastResult(
                viral_potential=data.get("viral_potential", "medium"),
                confidence=data.get("confidence", 0.5),
                score=data.get("score", 5),
                hook_strength=features.get("hook_strength", 5),
                topic_relevance=features.get("topic_relevance", 5),
                format_fit=features.get("format_fit", 5),
                timing_score=features.get("timing_score", 5),
                trend_alignment=features.get("trend_alignment", 5),
                estimated_reach=estimates.get("reach", {"min": 1000, "max": 10000}),
                estimated_engagement=estimates.get("engagement", {}),
                audience_match=estimates.get("audience_match", 0.5),
                positive_factors=factors.get("positive", []),
                negative_factors=factors.get("negative", []),
                suggestions=data.get("suggestions", []),
                percentile=data.get("percentile", 50)
            )
            
        except json.JSONDecodeError as e:
            logger.error(f"JSON parse error in viral forecast: {e}")
            raise ValueError(f"Failed to parse AI response: {e}")
        except Exception as e:
            logger.error(f"Viral forecast error: {e}")
            raise


# Test functions
async def test_post_analyzer():
    service = PostAnalyzerService()
    
    print("\n" + "="*60)
    print("📊 POST ANALYZER TEST")
    print("="*60)
    
    result = await service.analyze_post(
        transcript="""
        Stop scrolling - this changed everything for me.
        
        I used to waste 3 hours every morning checking emails and social media.
        Then I discovered the 90-minute focus block technique.
        
        Here's how it works:
        First 90 minutes of your day - no phone, no email, no meetings.
        Just deep work on your most important task.
        
        The results? I doubled my output in half the time.
        
        Try it tomorrow and let me know what happens.
        Follow for more productivity tips.
        """,
        caption="This simple morning routine hack changed my life 🚀 #productivity #morningroutine #success",
        content_type="talking_head"
    )
    
    print(f"\nOverall Score: {result.overall_score}/10 (Grade: {result.grade})")
    print(f"\nIndividual Scores:")
    print(f"  Hook: {result.hook_score}/10")
    print(f"  Body: {result.body_score}/10")
    print(f"  Visual: {result.visual_score}/10")
    print(f"  Audio: {result.audio_score}/10")
    print(f"  Pacing: {result.pacing_score}/10")
    print(f"  CTA: {result.cta_score}/10")
    print(f"\nDetected Hook: {result.detected_hook}")
    print(f"Detected CTA: {result.detected_cta}")
    print(f"\nTop Strengths:")
    for s in result.top_strengths[:3]:
        print(f"  ✓ {s}")
    print(f"\nTop Improvements:")
    for i in result.top_improvements[:3]:
        print(f"  → {i}")
    print(f"\nViral Score: {result.viral_score}/10")


async def test_viral_forecast():
    service = PostAnalyzerService()
    
    print("\n" + "="*60)
    print("🔮 VIRAL FORECASTER TEST")
    print("="*60)
    
    result = await service.forecast_viral_potential(
        topic="5 AI tools that will replace your job in 2025",
        hook="Your boss doesn't want you to know about these AI tools",
        format="reel",
        niche="technology",
        target_audience="professionals worried about AI job displacement"
    )
    
    print(f"\nViral Potential: {result.viral_potential.upper()}")
    print(f"Confidence: {result.confidence*100:.0f}%")
    print(f"Score: {result.score}/10")
    print(f"Percentile: Top {100-result.percentile}% of similar content")
    print(f"\nFeature Scores:")
    print(f"  Hook Strength: {result.hook_strength}/10")
    print(f"  Topic Relevance: {result.topic_relevance}/10")
    print(f"  Format Fit: {result.format_fit}/10")
    print(f"  Trend Alignment: {result.trend_alignment}/10")
    print(f"\nEstimated Reach: {result.estimated_reach['min']:,} - {result.estimated_reach['max']:,}")
    print(f"\nPositive Factors:")
    for f in result.positive_factors[:3]:
        print(f"  ✓ {f}")
    print(f"\nRisk Factors:")
    for f in result.negative_factors[:2]:
        print(f"  ⚠ {f}")


if __name__ == "__main__":
    asyncio.run(test_post_analyzer())
    asyncio.run(test_viral_forecast())
