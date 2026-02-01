"""
AI Title & Description Generator (PIPE-003)
===========================================
Generate engaging titles, descriptions, hashtags, and captions for videos
using AI (GPT-4) based on content analysis, platform, and target audience.

Features:
- Platform-specific optimization (TikTok, Instagram, YouTube, etc.)
- Multiple title variations with A/B testing
- SEO-optimized descriptions
- Trending hashtag suggestions
- Call-to-action generation
- FATE framework integration (Focus, Authority, Tribe, Emotion)
"""

import os
from typing import Dict, List, Optional, Any
from datetime import datetime, timezone
from dataclasses import dataclass, field
from enum import Enum
import json
import asyncio
from loguru import logger
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select

try:
    import openai
except ImportError:
    logger.warning("openai module not installed - AI Title Generator will use fallback mode")
    openai = None

from services.event_bus import EventBus, Topics
from database.models import Video
from config import settings


class Platform(str, Enum):
    """Social media platforms"""
    TIKTOK = "tiktok"
    INSTAGRAM = "instagram"
    YOUTUBE = "youtube"
    TWITTER = "twitter"
    LINKEDIN = "linkedin"
    FACEBOOK = "facebook"


class TitleStyle(str, Enum):
    """Title generation styles"""
    CURIOSITY = "curiosity"  # "You won't believe..."
    QUESTION = "question"  # "How does this work?"
    LISTICLE = "listicle"  # "5 ways to..."
    STORY = "story"  # "I tried... and this happened"
    DIRECT = "direct"  # "How to..."
    URGENCY = "urgency"  # "Stop doing this now"
    CONTROVERSIAL = "controversial"  # "Nobody talks about..."


@dataclass
class TitleVariation:
    """A single title variation"""
    text: str
    style: TitleStyle
    platform: Platform
    character_count: int
    hook_score: float = 0.0  # 0-1 score for hook strength
    seo_score: float = 0.0  # 0-1 score for SEO optimization
    engagement_prediction: float = 0.0  # 0-1 predicted engagement
    metadata: Dict[str, Any] = field(default_factory=dict)


@dataclass
class ContentSuggestions:
    """AI-generated content suggestions for a video"""
    titles: List[TitleVariation] = field(default_factory=list)
    descriptions: Dict[Platform, str] = field(default_factory=dict)
    hashtags: List[str] = field(default_factory=list)
    call_to_action: str = ""
    key_moments: List[Dict[str, Any]] = field(default_factory=list)
    target_keywords: List[str] = field(default_factory=list)
    generated_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary"""
        return {
            "titles": [
                {
                    "text": t.text,
                    "style": t.style.value,
                    "platform": t.platform.value,
                    "character_count": t.character_count,
                    "hook_score": t.hook_score,
                    "seo_score": t.seo_score,
                    "engagement_prediction": t.engagement_prediction,
                    "metadata": t.metadata
                }
                for t in self.titles
            ],
            "descriptions": {p.value: desc for p, desc in self.descriptions.items()},
            "hashtags": self.hashtags,
            "call_to_action": self.call_to_action,
            "key_moments": self.key_moments,
            "target_keywords": self.target_keywords,
            "generated_at": self.generated_at.isoformat()
        }


class AITitleGenerator:
    """
    AI Title & Description Generator (PIPE-003)

    Generates platform-optimized titles, descriptions, and metadata
    using GPT-4 based on content analysis and best practices.

    Usage:
        generator = AITitleGenerator(db_session)

        # Generate suggestions for a video
        suggestions = await generator.generate_suggestions(
            video_id="abc123",
            platforms=[Platform.TIKTOK, Platform.INSTAGRAM],
            content_analysis={"scene_description": "..."}
        )

        # Get title variations
        titles = suggestions.titles
        for title in titles:
            print(f"{title.platform.value}: {title.text}")
    """

    def __init__(self, db: AsyncSession):
        """
        Initialize AI Title Generator

        Args:
            db: Database session
        """
        self.db = db
        self.event_bus = EventBus.get_instance()
        self.event_bus.set_source("ai-title-generator")

        # OpenAI client (if available)
        if openai and settings.openai_api_key:
            self.openai_client = openai.AsyncOpenAI(api_key=settings.openai_api_key)
            self.use_ai = True
            logger.info("🎯 AI Title Generator initialized (OpenAI enabled)")
        else:
            self.openai_client = None
            self.use_ai = False
            logger.warning("⚠️  AI Title Generator initialized (fallback mode - no OpenAI)")

    async def generate_suggestions(
        self,
        video_id: str,
        platforms: List[Platform],
        content_analysis: Optional[Dict[str, Any]] = None,
        transcript: Optional[str] = None,
        target_audience: Optional[str] = None,
        brand_voice: Optional[str] = None,
        include_hashtags: bool = True,
        title_variations: int = 3
    ) -> ContentSuggestions:
        """
        Generate comprehensive content suggestions for a video

        Args:
            video_id: Video ID
            platforms: List of target platforms
            content_analysis: Optional pre-computed content analysis
            transcript: Optional video transcript
            target_audience: Optional target audience description
            brand_voice: Optional brand voice guidelines
            include_hashtags: Generate hashtags (default: True)
            title_variations: Number of title variations per style (default: 3)

        Returns:
            ContentSuggestions object with titles, descriptions, hashtags, etc.
        """
        try:
            # Get video from database
            stmt = select(Video).where(Video.id == video_id)
            result = await self.db.execute(stmt)
            video = result.scalar_one_or_none()

            if not video:
                logger.error(f"Video not found: {video_id}")
                return ContentSuggestions()

            # Build context for AI
            context = self._build_context(
                video=video,
                content_analysis=content_analysis,
                transcript=transcript,
                target_audience=target_audience,
                brand_voice=brand_voice
            )

            # Generate suggestions
            if self.use_ai:
                suggestions = await self._generate_with_ai(
                    context=context,
                    platforms=platforms,
                    title_variations=title_variations,
                    include_hashtags=include_hashtags
                )
            else:
                suggestions = await self._generate_fallback(
                    context=context,
                    platforms=platforms,
                    title_variations=title_variations,
                    include_hashtags=include_hashtags
                )

            # Emit event
            await self.event_bus.publish(
                Topics.AI_GENERATION_COMPLETED,
                {
                    "video_id": video_id,
                    "type": "title_description_generation",
                    "platforms": [p.value for p in platforms],
                    "title_count": len(suggestions.titles),
                    "hashtag_count": len(suggestions.hashtags)
                }
            )

            logger.success(f"✓ Generated suggestions for video {video_id}: {len(suggestions.titles)} titles, {len(suggestions.hashtags)} hashtags")

            return suggestions

        except Exception as e:
            logger.error(f"Error generating suggestions for video {video_id}: {e}")
            return ContentSuggestions()

    def _build_context(
        self,
        video: Any,
        content_analysis: Optional[Dict[str, Any]],
        transcript: Optional[str],
        target_audience: Optional[str],
        brand_voice: Optional[str]
    ) -> Dict[str, Any]:
        """Build context dictionary for AI generation"""
        context = {
            "video_title": video.title or "Untitled",
            "video_description": video.description or "",
            "duration": getattr(video, 'duration', 0),
            "content_analysis": content_analysis or {},
            "transcript": transcript or "",
            "target_audience": target_audience or "general",
            "brand_voice": brand_voice or "engaging and authentic"
        }
        return context

    async def _generate_with_ai(
        self,
        context: Dict[str, Any],
        platforms: List[Platform],
        title_variations: int,
        include_hashtags: bool
    ) -> ContentSuggestions:
        """Generate suggestions using GPT-4"""
        try:
            # Build prompt
            prompt = self._build_generation_prompt(
                context=context,
                platforms=platforms,
                title_variations=title_variations,
                include_hashtags=include_hashtags
            )

            # Call GPT-4
            response = await self.openai_client.chat.completions.create(
                model="gpt-4o",
                messages=[
                    {
                        "role": "system",
                        "content": "You are an expert social media content strategist specializing in viral content creation across TikTok, Instagram, YouTube, and other platforms. You understand the FATE framework (Focus, Authority, Tribe, Emotion) and create engaging, platform-optimized content."
                    },
                    {
                        "role": "user",
                        "content": prompt
                    }
                ],
                response_format={"type": "json_object"},
                temperature=0.7,
                max_tokens=2000
            )

            # Parse response
            content = response.choices[0].message.content
            result = json.loads(content)

            # Convert to ContentSuggestions
            suggestions = self._parse_ai_response(result, platforms)

            return suggestions

        except Exception as e:
            logger.error(f"Error generating with AI: {e}")
            # Fallback to rule-based generation
            return await self._generate_fallback(context, platforms, title_variations, include_hashtags)

    def _build_generation_prompt(
        self,
        context: Dict[str, Any],
        platforms: List[Platform],
        title_variations: int,
        include_hashtags: bool
    ) -> str:
        """Build the prompt for GPT-4"""
        platform_names = [p.value for p in platforms]

        prompt = f"""Generate engaging content suggestions for a video with the following context:

**Video Information:**
- Title: {context['video_title']}
- Description: {context['video_description'][:200]}
- Duration: {context['duration']}s
- Scene: {context['content_analysis'].get('scene_description', 'N/A')[:200]}
- Niche: {context['content_analysis'].get('niche', 'N/A')}

**Target:**
- Platforms: {', '.join(platform_names)}
- Audience: {context['target_audience']}
- Brand Voice: {context['brand_voice']}

**Required Output (JSON):**
{{
    "titles": [
        {{
            "text": "Engaging title text (max 100 chars for TikTok/IG, 100 for YouTube)",
            "style": "curiosity|question|listicle|story|direct|urgency|controversial",
            "platform": "tiktok|instagram|youtube|twitter",
            "hook_score": 0.85,
            "seo_score": 0.75,
            "engagement_prediction": 0.80
        }}
    ],
    "descriptions": {{
        "tiktok": "150 char description with hook and CTA",
        "instagram": "2200 char description with story and hashtags",
        "youtube": "Full SEO description with timestamps and links"
    }},
    "hashtags": ["#trending", "#viral", ...],
    "call_to_action": "Specific CTA",
    "key_moments": [
        {{"timestamp": 5, "description": "Hook moment", "emotion": "surprise"}}
    ],
    "target_keywords": ["keyword1", "keyword2", ...]
}}

**Requirements:**
1. Generate {title_variations} titles per style (curiosity, question, direct)
2. Use FATE framework: Focus (hook), Authority (proof), Tribe (identity), Emotion (story)
3. Platform-specific character limits:
   - TikTok: 100 chars
   - Instagram: 125 chars (caption: 2200)
   - YouTube: 100 chars (description: 5000)
   - Twitter: 280 chars
4. Include power words: "secret", "never", "finally", "proven"
5. {f"Generate 15-25 relevant hashtags" if include_hashtags else "No hashtags needed"}
6. Predict engagement scores (0-1) based on hook strength, novelty, and emotional appeal

Generate the JSON now:"""

        return prompt

    def _parse_ai_response(
        self,
        result: Dict[str, Any],
        platforms: List[Platform]
    ) -> ContentSuggestions:
        """Parse AI response into ContentSuggestions"""
        suggestions = ContentSuggestions()

        # Parse titles
        for title_data in result.get("titles", []):
            try:
                platform_str = title_data.get("platform", "tiktok")
                platform = Platform(platform_str) if platform_str in [p.value for p in Platform] else Platform.TIKTOK

                style_str = title_data.get("style", "direct")
                style = TitleStyle(style_str) if style_str in [s.value for s in TitleStyle] else TitleStyle.DIRECT

                title = TitleVariation(
                    text=title_data.get("text", ""),
                    style=style,
                    platform=platform,
                    character_count=len(title_data.get("text", "")),
                    hook_score=float(title_data.get("hook_score", 0.5)),
                    seo_score=float(title_data.get("seo_score", 0.5)),
                    engagement_prediction=float(title_data.get("engagement_prediction", 0.5)),
                    metadata={}
                )
                suggestions.titles.append(title)
            except Exception as e:
                logger.debug(f"Error parsing title: {e}")

        # Parse descriptions
        for platform_str, desc in result.get("descriptions", {}).items():
            try:
                platform = Platform(platform_str)
                suggestions.descriptions[platform] = desc
            except Exception as e:
                logger.debug(f"Error parsing description for {platform_str}: {e}")

        # Parse other fields
        suggestions.hashtags = result.get("hashtags", [])
        suggestions.call_to_action = result.get("call_to_action", "")
        suggestions.key_moments = result.get("key_moments", [])
        suggestions.target_keywords = result.get("target_keywords", [])

        return suggestions

    async def _generate_fallback(
        self,
        context: Dict[str, Any],
        platforms: List[Platform],
        title_variations: int,
        include_hashtags: bool
    ) -> ContentSuggestions:
        """Generate suggestions using rule-based fallback"""
        suggestions = ContentSuggestions()

        # Get content info
        title = context['video_title']
        niche = context['content_analysis'].get('niche', 'lifestyle')

        # Generate title variations
        title_templates = {
            TitleStyle.CURIOSITY: [
                f"You won't believe what happened: {title}",
                f"This will change everything you know about {niche}",
                f"The secret to {title} nobody talks about"
            ],
            TitleStyle.QUESTION: [
                f"How does {title} actually work?",
                f"What if I told you about {title}?",
                f"Why is everyone talking about {title}?"
            ],
            TitleStyle.DIRECT: [
                f"How to: {title}",
                f"{title} - Complete Guide",
                f"Everything you need to know about {title}"
            ]
        }

        for platform in platforms:
            for style, templates in title_templates.items():
                for template in templates[:title_variations]:
                    title_obj = TitleVariation(
                        text=template[:100],  # Platform character limit
                        style=style,
                        platform=platform,
                        character_count=len(template[:100]),
                        hook_score=0.6,
                        seo_score=0.5,
                        engagement_prediction=0.55,
                        metadata={"generated_by": "fallback"}
                    )
                    suggestions.titles.append(title_obj)

        # Generate platform descriptions
        for platform in platforms:
            if platform == Platform.TIKTOK:
                suggestions.descriptions[platform] = f"{title} - Watch till the end! 🔥 #fyp"
            elif platform == Platform.INSTAGRAM:
                suggestions.descriptions[platform] = f"{title}\n\nSave this for later! 📌\n\n#explore #viral"
            elif platform == Platform.YOUTUBE:
                suggestions.descriptions[platform] = f"{title}\n\nIn this video, we explore everything you need to know.\n\n⏰ Timestamps:\n0:00 - Intro\n\n🔔 Subscribe for more!"

        # Generate hashtags
        if include_hashtags:
            base_hashtags = ["#fyp", "#viral", "#trending", "#explore", "#foryou", f"#{niche}"]
            suggestions.hashtags = base_hashtags

        suggestions.call_to_action = "Follow for more!"
        suggestions.target_keywords = [title, niche]

        return suggestions

    async def batch_generate(
        self,
        video_ids: List[str],
        platforms: List[Platform],
        max_concurrent: int = 3
    ) -> Dict[str, ContentSuggestions]:
        """
        Batch generate suggestions for multiple videos

        Args:
            video_ids: List of video IDs
            platforms: Platforms to generate for
            max_concurrent: Max concurrent generations

        Returns:
            Dictionary mapping video_id to ContentSuggestions
        """
        logger.info(f"🔄 Batch generating suggestions for {len(video_ids)} videos...")

        results = {}

        # Process in batches
        for i in range(0, len(video_ids), max_concurrent):
            batch = video_ids[i:i+max_concurrent]

            tasks = [
                self.generate_suggestions(video_id=vid, platforms=platforms)
                for vid in batch
            ]
            batch_results = await asyncio.gather(*tasks, return_exceptions=True)

            for video_id, result in zip(batch, batch_results):
                if isinstance(result, Exception):
                    logger.error(f"Error generating for {video_id}: {result}")
                    results[video_id] = ContentSuggestions()
                else:
                    results[video_id] = result

            # Brief delay between batches
            if i + max_concurrent < len(video_ids):
                await asyncio.sleep(0.5)

        success_count = sum(1 for r in results.values() if len(r.titles) > 0)
        logger.success(f"✓ Batch generation complete: {success_count}/{len(video_ids)} successful")

        return results
