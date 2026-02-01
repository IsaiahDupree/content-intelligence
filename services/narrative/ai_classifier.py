"""
AI Content Classifier using OpenAI

Classifies video content into narrative pillars using GPT-4
with detailed reasoning and confidence scores.
"""

import os
import json
import logging
from typing import List, Dict, Optional, Tuple, Any
from dataclasses import dataclass

logger = logging.getLogger(__name__)


@dataclass
class ClassificationResult:
    """Result of AI content classification"""
    primary_pillar: str
    secondary_pillar: Optional[str]
    confidence: float
    reasoning: str
    topics_detected: List[str]
    suggested_hooks: List[str]
    content_type: str  # educational, entertainment, promotional, etc.


class AIContentClassifier:
    """
    Classifies video content into narrative pillars using OpenAI GPT.
    """
    
    def __init__(self, api_key: Optional[str] = None):
        self.api_key = api_key or os.getenv("OPENAI_API_KEY")
        self.model = "gpt-4o-mini"  # Cost-effective for classification
    
    async def classify_video(
        self,
        video_title: str,
        transcript: Optional[str] = None,
        topics: Optional[List[str]] = None,
        hooks: Optional[List[str]] = None,
        pillars: Optional[List[Dict[str, Any]]] = None
    ) -> ClassificationResult:
        """
        Classify a video into narrative pillars.
        
        Args:
            video_title: Title of the video
            transcript: Full or partial transcript
            topics: Detected topics from analysis
            hooks: Detected hooks from analysis
            pillars: Available pillars to classify into
            
        Returns:
            ClassificationResult with pillar assignment and reasoning
        """
        if not self.api_key:
            # Fallback to keyword-based classification
            return self._keyword_classify(video_title, transcript, topics, pillars)
        
        try:
            import openai
            client = openai.OpenAI(api_key=self.api_key)
            
            # Build pillar descriptions
            pillar_desc = self._format_pillars(pillars)
            
            # Build content context
            content_context = self._build_content_context(video_title, transcript, topics, hooks)
            
            prompt = f"""Analyze this video content and classify it into the most appropriate narrative pillar.

## Available Pillars:
{pillar_desc}

## Video Content:
{content_context}

## Task:
1. Determine which pillar this content best fits into
2. Identify a secondary pillar if applicable
3. Provide confidence score (0-100)
4. Explain your reasoning in 1-2 sentences
5. List 2-3 key topics detected
6. Suggest 2 potential hooks for this content
7. Classify content type (educational, entertainment, promotional, personal, inspirational)

Respond in JSON format:
{{
    "primary_pillar": "pillar name",
    "secondary_pillar": "pillar name or null",
    "confidence": 85,
    "reasoning": "Brief explanation",
    "topics_detected": ["topic1", "topic2"],
    "suggested_hooks": ["hook1", "hook2"],
    "content_type": "educational"
}}"""

            response = client.chat.completions.create(
                model=self.model,
                messages=[
                    {"role": "system", "content": "You are a content strategist expert at classifying social media content into narrative pillars."},
                    {"role": "user", "content": prompt}
                ],
                temperature=0.3,
                max_tokens=500
            )
            
            result_text = response.choices[0].message.content
            
            # Parse JSON response
            try:
                # Clean up response if needed
                if "```json" in result_text:
                    result_text = result_text.split("```json")[1].split("```")[0]
                elif "```" in result_text:
                    result_text = result_text.split("```")[1].split("```")[0]
                
                result = json.loads(result_text.strip())
                
                return ClassificationResult(
                    primary_pillar=result.get("primary_pillar", "Uncategorized"),
                    secondary_pillar=result.get("secondary_pillar"),
                    confidence=result.get("confidence", 70) / 100,
                    reasoning=result.get("reasoning", ""),
                    topics_detected=result.get("topics_detected", []),
                    suggested_hooks=result.get("suggested_hooks", []),
                    content_type=result.get("content_type", "general")
                )
            except json.JSONDecodeError:
                logger.warning(f"Failed to parse AI response: {result_text[:100]}")
                return self._keyword_classify(video_title, transcript, topics, pillars)
                
        except Exception as e:
            logger.error(f"OpenAI classification failed: {e}")
            return self._keyword_classify(video_title, transcript, topics, pillars)
    
    async def classify_batch(
        self,
        videos: List[Dict[str, Any]],
        pillars: List[Dict[str, Any]]
    ) -> List[ClassificationResult]:
        """Classify multiple videos efficiently"""
        results = []
        
        for video in videos:
            result = await self.classify_video(
                video_title=video.get("title", ""),
                transcript=video.get("transcript"),
                topics=video.get("topics"),
                hooks=video.get("hooks"),
                pillars=pillars
            )
            results.append(result)
        
        return results
    
    def _format_pillars(self, pillars: Optional[List[Dict[str, Any]]]) -> str:
        """Format pillars for prompt"""
        if not pillars:
            return """
- Pain Points (value): Content addressing audience struggles and challenges
- Social Proof (proof): Testimonials, results, transformations
- Process/How-To (value): Educational content, tutorials, step-by-step guides
- Personality (value): Behind-the-scenes, authentic moments, personal stories
- Product/Service (cta): Direct product showcases and demonstrations
- Promotion/CTA (cta): Clear calls-to-action
- Education (value): Industry knowledge, thought leadership"""
        
        lines = []
        for p in pillars:
            name = p.get("name", "Unknown")
            ptype = p.get("pillar_type", "value")
            desc = p.get("description", "")
            keywords = p.get("keywords", [])
            
            keyword_str = ", ".join(keywords[:5]) if keywords else ""
            lines.append(f"- {name} ({ptype}): {desc}. Keywords: {keyword_str}")
        
        return "\n".join(lines)
    
    def _build_content_context(
        self,
        title: str,
        transcript: Optional[str],
        topics: Optional[List[str]],
        hooks: Optional[List[str]]
    ) -> str:
        """Build content context for classification"""
        parts = [f"Title: {title}"]
        
        if transcript:
            # Truncate transcript to first 500 chars
            truncated = transcript[:500] + "..." if len(transcript) > 500 else transcript
            parts.append(f"Transcript excerpt: {truncated}")
        
        if topics:
            parts.append(f"Detected topics: {', '.join(topics[:5])}")
        
        if hooks:
            parts.append(f"Detected hooks: {', '.join(hooks[:3])}")
        
        return "\n".join(parts)
    
    def _keyword_classify(
        self,
        title: str,
        transcript: Optional[str],
        topics: Optional[List[str]],
        pillars: Optional[List[Dict[str, Any]]]
    ) -> ClassificationResult:
        """Fallback keyword-based classification"""
        text = " ".join([
            title or "",
            transcript or "",
            " ".join(topics or [])
        ]).lower()
        
        # Default pillar keywords
        pillar_keywords = {
            "Process/How-To": ["how to", "tutorial", "step by step", "guide", "tips", "learn", "easy"],
            "Pain Points": ["problem", "struggle", "frustrated", "challenge", "difficult", "stuck", "fail"],
            "Social Proof": ["results", "success", "testimonial", "transformation", "before after", "review"],
            "Personality": ["behind the scenes", "day in life", "personal", "story", "journey", "real", "me"],
            "Product/Service": ["product", "service", "offering", "available", "new", "launch"],
            "Promotion/CTA": ["sign up", "subscribe", "join", "download", "buy", "get", "link"],
            "Education": ["industry", "trends", "insight", "analysis", "future", "prediction"]
        }
        
        # Use custom pillars if provided
        if pillars:
            pillar_keywords = {}
            for p in pillars:
                name = p.get("name", "Unknown")
                keywords = p.get("keywords", [])
                pillar_keywords[name] = keywords
        
        # Score each pillar
        scores = {}
        for pillar, keywords in pillar_keywords.items():
            score = sum(1 for kw in keywords if kw.lower() in text)
            scores[pillar] = score
        
        # Get top 2
        sorted_pillars = sorted(scores.items(), key=lambda x: x[1], reverse=True)
        
        primary = sorted_pillars[0] if sorted_pillars else ("Uncategorized", 0)
        secondary = sorted_pillars[1] if len(sorted_pillars) > 1 and sorted_pillars[1][1] > 0 else (None, 0)
        
        # Calculate confidence
        total_matches = sum(s for _, s in sorted_pillars)
        confidence = (primary[1] / max(total_matches, 1)) if total_matches > 0 else 0.5
        
        return ClassificationResult(
            primary_pillar=primary[0],
            secondary_pillar=secondary[0] if secondary[0] else None,
            confidence=min(confidence, 1.0),
            reasoning=f"Matched {primary[1]} keywords for {primary[0]}",
            topics_detected=topics[:3] if topics else [],
            suggested_hooks=[],
            content_type="general"
        )


class AIScheduleJustifier:
    """
    Generates detailed justifications for scheduling decisions using AI.
    """
    
    def __init__(self, api_key: Optional[str] = None):
        self.api_key = api_key or os.getenv("OPENAI_API_KEY")
        self.model = "gpt-4o-mini"
    
    async def generate_schedule_justification(
        self,
        goal: Dict[str, Any],
        selected_videos: List[Dict[str, Any]],
        pillar_distribution: Dict[str, int],
        platform_distribution: Dict[str, int],
        previous_performance: Optional[Dict[str, Any]] = None
    ) -> str:
        """Generate a detailed justification for the schedule"""
        
        if not self.api_key:
            return self._basic_justification(goal, pillar_distribution, platform_distribution)
        
        try:
            import openai
            client = openai.OpenAI(api_key=self.api_key)
            
            prompt = f"""Generate a strategic justification for this 7-day content schedule.

## Narrative Goal:
{goal.get('goal_statement', 'Build engagement')}

Primary CTA: {goal.get('primary_cta', 'follow')}
Target Audience: {goal.get('target_audience', 'General')}

## Schedule Summary:
- Total posts: {sum(pillar_distribution.values())}
- Pillar distribution: {json.dumps(pillar_distribution)}
- Platform distribution: {json.dumps(platform_distribution)}

## Selected Videos:
{self._format_videos(selected_videos[:5])}

{self._format_previous_performance(previous_performance)}

Write a 3-4 paragraph strategic justification explaining:
1. How this schedule aligns with the narrative goal
2. Why the pillar mix was chosen
3. Platform strategy rationale
4. Expected outcomes and what to monitor

Be specific and strategic. Write in second person ("Your schedule...").
"""

            response = client.chat.completions.create(
                model=self.model,
                messages=[
                    {"role": "system", "content": "You are a social media strategist writing schedule justifications for content creators."},
                    {"role": "user", "content": prompt}
                ],
                temperature=0.7,
                max_tokens=600
            )
            
            return response.choices[0].message.content
            
        except Exception as e:
            logger.error(f"AI justification failed: {e}")
            return self._basic_justification(goal, pillar_distribution, platform_distribution)
    
    def _format_videos(self, videos: List[Dict[str, Any]]) -> str:
        """Format videos for prompt"""
        lines = []
        for v in videos:
            title = v.get("title", "Untitled")[:40]
            pillar = v.get("pillar", "Unknown")
            score = v.get("score", "N/A")
            lines.append(f"- {title} | {pillar} | Score: {score}")
        return "\n".join(lines) if lines else "No videos selected"
    
    def _format_previous_performance(self, performance: Optional[Dict[str, Any]]) -> str:
        """Format previous performance for prompt"""
        if not performance:
            return ""
        
        return f"""
## Previous Week Performance:
- Avg engagement: {performance.get('avg_engagement_rate', 'N/A')}%
- Total views: {performance.get('total_views', 'N/A')}
- Top pillar: {performance.get('top_pillar', 'N/A')}
- Bottom pillar: {performance.get('bottom_pillar', 'N/A')}
"""
    
    def _basic_justification(
        self,
        goal: Dict[str, Any],
        pillar_dist: Dict[str, int],
        platform_dist: Dict[str, int]
    ) -> str:
        """Generate basic justification without AI"""
        total = sum(pillar_dist.values())
        
        lines = [
            "## Schedule Justification\n",
            f"This schedule is designed to achieve: **{goal.get('goal_statement', 'Build engagement')}**\n",
            f"Primary call-to-action: **{goal.get('primary_cta', 'follow')}**\n",
            "\n### Content Distribution\n"
        ]
        
        for pillar, count in sorted(pillar_dist.items(), key=lambda x: x[1], reverse=True):
            pct = (count / total * 100) if total > 0 else 0
            lines.append(f"- **{pillar}**: {count} posts ({pct:.0f}%)")
        
        lines.append("\n\n### Platform Strategy\n")
        for platform, count in platform_dist.items():
            lines.append(f"- **{platform.title()}**: {count} posts")
        
        return "\n".join(lines)
