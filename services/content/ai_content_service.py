"""
AI Content Service
Generates titles, descriptions, hashtags using AI (OpenAI/Claude/Local).
Supports multiple providers with fallback.
"""

import os
import json
import asyncio
import httpx
from typing import Dict, Any, List, Optional
from datetime import datetime
from enum import Enum
from pydantic import BaseModel, Field
import logging
import re

logger = logging.getLogger(__name__)


class AIProvider(str, Enum):
    OPENAI = "openai"
    ANTHROPIC = "anthropic"
    LOCAL = "local"  # Fallback templates


class ContentTone(str, Enum):
    PROFESSIONAL = "professional"
    CASUAL = "casual"
    ENTHUSIASTIC = "enthusiastic"
    EDUCATIONAL = "educational"
    HUMOROUS = "humorous"
    INSPIRATIONAL = "inspirational"


class Platform(str, Enum):
    TIKTOK = "tiktok"
    YOUTUBE = "youtube"
    INSTAGRAM = "instagram"
    TWITTER = "twitter"
    LINKEDIN = "linkedin"
    THREADS = "threads"


class ContentAnalysis(BaseModel):
    """Analysis of content for AI generation"""
    content_type: str  # image, video, clip
    duration_seconds: Optional[float] = None
    detected_objects: List[str] = Field(default_factory=list)
    detected_text: List[str] = Field(default_factory=list)
    dominant_colors: List[str] = Field(default_factory=list)
    scene_description: Optional[str] = None
    mood: Optional[str] = None
    niche: str = "general"
    quality_score: float = 0.0


class GeneratedContent(BaseModel):
    """AI-generated content for a platform"""
    title: str
    description: str
    hashtags: List[str]
    call_to_action: Optional[str] = None
    platform: Platform
    tone: ContentTone
    character_count: int = 0
    variations: List[Dict] = Field(default_factory=list)
    generated_at: str = Field(default_factory=lambda: datetime.now().isoformat())
    provider: AIProvider = AIProvider.LOCAL


class AIContentService:
    """Service for AI-powered content generation"""
    
    # Platform-specific limits
    PLATFORM_LIMITS = {
        Platform.TIKTOK: {"title": 150, "description": 2200, "hashtags": 5},
        Platform.YOUTUBE: {"title": 100, "description": 5000, "hashtags": 15},
        Platform.INSTAGRAM: {"title": 0, "description": 2200, "hashtags": 30},
        Platform.TWITTER: {"title": 0, "description": 280, "hashtags": 3},
        Platform.LINKEDIN: {"title": 150, "description": 3000, "hashtags": 5},
        Platform.THREADS: {"title": 0, "description": 500, "hashtags": 5},
    }
    
    # Niche-specific hashtag banks
    NICHE_HASHTAGS = {
        "fitness": ["fitness", "workout", "gym", "health", "fitfam", "training", "motivation", "exercise", "gains", "fit"],
        "tech": ["tech", "technology", "coding", "programming", "developer", "software", "ai", "innovation", "digital", "startup"],
        "lifestyle": ["lifestyle", "life", "daily", "vibes", "mood", "aesthetic", "living", "mindset", "growth", "self"],
        "comedy": ["funny", "comedy", "humor", "lol", "meme", "jokes", "laugh", "viral", "relatable", "hilarious"],
        "education": ["education", "learn", "tips", "howto", "tutorial", "knowledge", "facts", "didyouknow", "learning", "study"],
        "gaming": ["gaming", "gamer", "game", "videogames", "twitch", "esports", "gameplay", "streamer", "ps5", "xbox"],
        "food": ["food", "foodie", "recipe", "cooking", "yummy", "delicious", "homemade", "foodporn", "chef", "tasty"],
        "travel": ["travel", "wanderlust", "adventure", "explore", "vacation", "travelphotography", "destination", "trip", "tourism", "travelgram"],
        "fashion": ["fashion", "style", "ootd", "outfit", "trendy", "fashionista", "clothing", "streetstyle", "lookbook", "styling"],
        "music": ["music", "musician", "song", "artist", "newmusic", "producer", "beats", "singer", "hiphop", "pop"],
    }
    
    def __init__(self):
        self.openai_key = os.getenv("OPENAI_API_KEY")
        self.anthropic_key = os.getenv("ANTHROPIC_API_KEY")
        self._provider = self._select_provider()
    
    def _select_provider(self) -> AIProvider:
        """Select best available AI provider"""
        if self.anthropic_key:
            return AIProvider.ANTHROPIC
        elif self.openai_key:
            return AIProvider.OPENAI
        return AIProvider.LOCAL
    
    async def generate_content(
        self,
        analysis: ContentAnalysis,
        platform: Platform,
        tone: ContentTone = ContentTone.CASUAL,
        num_variations: int = 3,
    ) -> GeneratedContent:
        """
        Generate title, description, and hashtags for content.
        Uses AI when available, falls back to templates.
        """
        if self._provider == AIProvider.ANTHROPIC:
            return await self._generate_with_anthropic(analysis, platform, tone, num_variations)
        elif self._provider == AIProvider.OPENAI:
            return await self._generate_with_openai(analysis, platform, tone, num_variations)
        else:
            return self._generate_with_templates(analysis, platform, tone, num_variations)
    
    async def _generate_with_anthropic(
        self,
        analysis: ContentAnalysis,
        platform: Platform,
        tone: ContentTone,
        num_variations: int,
    ) -> GeneratedContent:
        """Generate content using Claude API"""
        prompt = self._build_prompt(analysis, platform, tone, num_variations)
        
        try:
            async with httpx.AsyncClient(timeout=30.0) as client:
                response = await client.post(
                    "https://api.anthropic.com/v1/messages",
                    headers={
                        "x-api-key": self.anthropic_key,
                        "anthropic-version": "2023-06-01",
                        "content-type": "application/json",
                    },
                    json={
                        "model": "claude-3-haiku-20240307",
                        "max_tokens": 1024,
                        "messages": [{"role": "user", "content": prompt}],
                    },
                )
                
                if response.status_code == 200:
                    data = response.json()
                    content_text = data["content"][0]["text"]
                    return self._parse_ai_response(content_text, platform, tone, AIProvider.ANTHROPIC)
                else:
                    logger.warning(f"Anthropic API error: {response.status_code}")
                    return self._generate_with_templates(analysis, platform, tone, num_variations)
                    
        except Exception as e:
            logger.error(f"Anthropic generation failed: {e}")
            return self._generate_with_templates(analysis, platform, tone, num_variations)
    
    async def _generate_with_openai(
        self,
        analysis: ContentAnalysis,
        platform: Platform,
        tone: ContentTone,
        num_variations: int,
    ) -> GeneratedContent:
        """Generate content using OpenAI API"""
        prompt = self._build_prompt(analysis, platform, tone, num_variations)
        
        try:
            async with httpx.AsyncClient(timeout=30.0) as client:
                response = await client.post(
                    "https://api.openai.com/v1/chat/completions",
                    headers={
                        "Authorization": f"Bearer {self.openai_key}",
                        "Content-Type": "application/json",
                    },
                    json={
                        "model": "gpt-3.5-turbo",
                        "messages": [{"role": "user", "content": prompt}],
                        "max_tokens": 1024,
                        "temperature": 0.8,
                    },
                )
                
                if response.status_code == 200:
                    data = response.json()
                    content_text = data["choices"][0]["message"]["content"]
                    return self._parse_ai_response(content_text, platform, tone, AIProvider.OPENAI)
                else:
                    logger.warning(f"OpenAI API error: {response.status_code}")
                    return self._generate_with_templates(analysis, platform, tone, num_variations)
                    
        except Exception as e:
            logger.error(f"OpenAI generation failed: {e}")
            return self._generate_with_templates(analysis, platform, tone, num_variations)
    
    def _generate_with_templates(
        self,
        analysis: ContentAnalysis,
        platform: Platform,
        tone: ContentTone,
        num_variations: int,
    ) -> GeneratedContent:
        """Generate content using templates (fallback)"""
        limits = self.PLATFORM_LIMITS[platform]
        niche = analysis.niche.lower()
        
        # Get niche-specific hashtags
        hashtags = self.NICHE_HASHTAGS.get(niche, self.NICHE_HASHTAGS["lifestyle"])[:limits["hashtags"]]
        
        # Generate title based on tone and content
        title = self._generate_template_title(analysis, tone, platform)
        
        # Generate description
        description = self._generate_template_description(analysis, tone, platform)
        
        # Generate variations
        variations = []
        for i in range(num_variations):
            variations.append({
                "title": self._generate_template_title(analysis, tone, platform, variation=i+1),
                "description": self._generate_template_description(analysis, tone, platform, variation=i+1),
                "hashtags": self._shuffle_hashtags(hashtags),
            })
        
        # Call to action
        cta = self._generate_cta(platform, tone)
        
        return GeneratedContent(
            title=title[:limits["title"]] if limits["title"] > 0 else "",
            description=description[:limits["description"]],
            hashtags=[f"#{tag}" for tag in hashtags],
            call_to_action=cta,
            platform=platform,
            tone=tone,
            character_count=len(title) + len(description),
            variations=variations,
            provider=AIProvider.LOCAL,
        )
    
    def _build_prompt(
        self,
        analysis: ContentAnalysis,
        platform: Platform,
        tone: ContentTone,
        num_variations: int,
    ) -> str:
        """Build prompt for AI generation"""
        limits = self.PLATFORM_LIMITS[platform]
        
        prompt = f"""Generate engaging social media content for {platform.value}.

Content Analysis:
- Type: {analysis.content_type}
- Niche: {analysis.niche}
- Scene: {analysis.scene_description or 'Not specified'}
- Mood: {analysis.mood or 'Not specified'}
- Detected objects: {', '.join(analysis.detected_objects) or 'None'}
- Quality score: {analysis.quality_score}/100

Requirements:
- Tone: {tone.value}
- Title max: {limits['title']} characters (leave empty if 0)
- Description max: {limits['description']} characters
- Hashtags: exactly {limits['hashtags']} relevant hashtags

Generate {num_variations} variations in this JSON format:
{{
    "primary": {{
        "title": "...",
        "description": "...",
        "hashtags": ["tag1", "tag2", ...],
        "call_to_action": "..."
    }},
    "variations": [
        {{"title": "...", "description": "...", "hashtags": [...]}},
        ...
    ]
}}

Make content viral-worthy, engaging, and platform-optimized. Include emojis where appropriate."""
        
        return prompt
    
    def _parse_ai_response(
        self,
        response_text: str,
        platform: Platform,
        tone: ContentTone,
        provider: AIProvider,
    ) -> GeneratedContent:
        """Parse AI response into GeneratedContent"""
        try:
            # Try to extract JSON from response
            json_match = re.search(r'\{[\s\S]*\}', response_text)
            if json_match:
                data = json.loads(json_match.group())
                primary = data.get("primary", data)
                
                return GeneratedContent(
                    title=primary.get("title", ""),
                    description=primary.get("description", ""),
                    hashtags=primary.get("hashtags", []),
                    call_to_action=primary.get("call_to_action"),
                    platform=platform,
                    tone=tone,
                    character_count=len(primary.get("title", "")) + len(primary.get("description", "")),
                    variations=data.get("variations", []),
                    provider=provider,
                )
        except (json.JSONDecodeError, KeyError) as e:
            logger.warning(f"Failed to parse AI response: {e}")
        
        # Fallback: use response as description
        return GeneratedContent(
            title="",
            description=response_text[:2000],
            hashtags=[],
            platform=platform,
            tone=tone,
            character_count=len(response_text[:2000]),
            provider=provider,
        )
    
    def _generate_template_title(
        self,
        analysis: ContentAnalysis,
        tone: ContentTone,
        platform: Platform,
        variation: int = 0,
    ) -> str:
        """Generate title from templates"""
        niche = analysis.niche.lower()
        
        templates = {
            ContentTone.ENTHUSIASTIC: [
                f"🔥 You WON'T Believe This {niche.title()} Content!",
                f"THIS is Why I Love {niche.title()}! 💯",
                f"The BEST {niche.title()} Moment Ever! 🎯",
                f"Wait For It... 😱 #{niche.title()}",
            ],
            ContentTone.CASUAL: [
                f"Just vibing with some {niche} content ✨",
                f"POV: You discovered great {niche} content",
                f"This is your sign to check out {niche}",
                f"Me + {niche.title()} = 💕",
            ],
            ContentTone.EDUCATIONAL: [
                f"Learn This {niche.title()} Tip Now 📚",
                f"The Secret to {niche.title()} Success",
                f"5 Things About {niche.title()} You Should Know",
                f"How to Master {niche.title()} in 2024",
            ],
            ContentTone.PROFESSIONAL: [
                f"Professional {niche.title()} Insights",
                f"Industry Update: {niche.title()} Trends",
                f"Expert Analysis: {niche.title()}",
                f"Strategic {niche.title()} Approach",
            ],
            ContentTone.HUMOROUS: [
                f"When {niche} hits different 😂",
                f"Nobody: ... Me: *does {niche}*",
                f"The {niche} struggle is real 💀",
                f"POV: {niche.title()} gone wrong (or right?)",
            ],
            ContentTone.INSPIRATIONAL: [
                f"Your {niche.title()} Journey Starts Here 🌟",
                f"Believe in Your {niche.title()} Dreams ✨",
                f"Transform Your Life with {niche.title()}",
                f"The Power of {niche.title()} 💪",
            ],
        }
        
        titles = templates.get(tone, templates[ContentTone.CASUAL])
        return titles[variation % len(titles)]
    
    def _generate_template_description(
        self,
        analysis: ContentAnalysis,
        tone: ContentTone,
        platform: Platform,
        variation: int = 0,
    ) -> str:
        """Generate description from templates"""
        niche = analysis.niche.lower()
        
        descriptions = {
            ContentTone.ENTHUSIASTIC: [
                f"🔥 This {niche} content is absolutely INCREDIBLE! Drop a 🙌 if you agree!\n\n"
                f"I've been working on this for a while and I'm so excited to share it with you all!\n\n"
                f"Make sure to follow for more amazing {niche} content! ⬇️",
                
                f"WOW! 😱 I can't believe how well this turned out!\n\n"
                f"This is exactly why I love creating {niche} content for you guys!\n\n"
                f"Hit that follow button for daily {niche} inspiration! 💯",
            ],
            ContentTone.CASUAL: [
                f"Hey everyone! ✨ Just wanted to share this {niche} moment with you.\n\n"
                f"Sometimes it's the simple things that make the best content, right?\n\n"
                f"Let me know what you think in the comments! 💭",
                
                f"So this happened... 😊\n\n"
                f"I love sharing my {niche} journey with you all.\n\n"
                f"Follow along for more! 🙏",
            ],
            ContentTone.EDUCATIONAL: [
                f"📚 Here's what you need to know about {niche}:\n\n"
                f"1️⃣ Start with the basics\n"
                f"2️⃣ Practice consistently\n"
                f"3️⃣ Learn from mistakes\n\n"
                f"Save this for later! 🔖",
                
                f"Let me break this down for you 👇\n\n"
                f"Understanding {niche} doesn't have to be complicated.\n\n"
                f"Follow for more tips and tutorials! 📖",
            ],
            ContentTone.PROFESSIONAL: [
                f"As a {niche} professional, I've learned that success comes from consistency.\n\n"
                f"Here's my latest project showcasing industry best practices.\n\n"
                f"Connect with me for more insights.",
                
                f"Today's {niche} insight:\n\n"
                f"Quality over quantity always wins in this space.\n\n"
                f"What are your thoughts? Let's discuss below.",
            ],
        }
        
        descs = descriptions.get(tone, descriptions[ContentTone.CASUAL])
        return descs[variation % len(descs)]
    
    def _generate_cta(self, platform: Platform, tone: ContentTone) -> str:
        """Generate call-to-action"""
        ctas = {
            Platform.TIKTOK: "Follow for more! 🔔",
            Platform.YOUTUBE: "Subscribe & hit the bell! 🔔",
            Platform.INSTAGRAM: "Save this post! 🔖",
            Platform.TWITTER: "Retweet if you agree! 🔄",
            Platform.LINKEDIN: "Let's connect! 🤝",
            Platform.THREADS: "Drop your thoughts below! 💭",
        }
        return ctas.get(platform, "Follow for more!")
    
    def _shuffle_hashtags(self, hashtags: List[str]) -> List[str]:
        """Shuffle hashtags for variation"""
        import random
        shuffled = hashtags.copy()
        random.shuffle(shuffled)
        return [f"#{tag}" for tag in shuffled]
    
    async def analyze_content_for_generation(
        self,
        content_type: str,
        file_path: Optional[str] = None,
        existing_analysis: Optional[Dict] = None,
    ) -> ContentAnalysis:
        """
        Analyze content to prepare for AI generation.
        Uses existing analysis if available, or creates mock analysis.
        """
        if existing_analysis:
            return ContentAnalysis(
                content_type=content_type,
                duration_seconds=existing_analysis.get("duration"),
                detected_objects=existing_analysis.get("objects", []),
                detected_text=existing_analysis.get("text", []),
                dominant_colors=existing_analysis.get("colors", []),
                scene_description=existing_analysis.get("scene"),
                mood=existing_analysis.get("mood"),
                niche=existing_analysis.get("niche", "general"),
                quality_score=existing_analysis.get("quality_score", 75.0),
            )
        
        # Mock analysis for testing
        return ContentAnalysis(
            content_type=content_type,
            niche="lifestyle",
            scene_description="General content scene",
            mood="positive",
            quality_score=75.0,
        )
    
    async def generate_variations(
        self,
        original: GeneratedContent,
        num_variations: int = 5,
    ) -> List[Dict]:
        """Generate additional variations of existing content"""
        variations = original.variations.copy()
        
        # Add more variations using templates
        for i in range(len(variations), num_variations):
            analysis = ContentAnalysis(
                content_type="video",
                niche="lifestyle",
                quality_score=75.0,
            )
            variations.append({
                "title": self._generate_template_title(analysis, original.tone, original.platform, variation=i),
                "description": self._generate_template_description(analysis, original.tone, original.platform, variation=i),
                "hashtags": self._shuffle_hashtags([h.replace("#", "") for h in original.hashtags]),
            })
        
        return variations[:num_variations]


# Singleton instance
ai_content_service = AIContentService()
