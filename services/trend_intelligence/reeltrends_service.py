"""
ReelTrends Service - Instagram Creator Tools
=============================================
AI-powered content generation suite:
- Script Generator (3-beat scripts with time budgets)
- Captions Generator (3 styles + bucketed hashtags)
- Carousel Generator (slide copy + image inspiration)
- Hashtag Recommender (niche/format/discovery buckets)

Uses real OpenAI API calls for all AI features.
"""
import os
import json
import asyncio
from datetime import datetime, timezone
from typing import List, Dict, Any, Optional, Literal
from dataclasses import dataclass, field, asdict
from enum import Enum

import httpx
from openai import AsyncOpenAI
from loguru import logger

OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "")


class ScriptLength(str, Enum):
    SHORT = "short"      # 22s total
    MEDIUM = "medium"    # 45s total
    LONG = "long"        # 65s total


class ScriptTone(str, Enum):
    CASUAL = "casual"
    PROFESSIONAL = "professional"
    FUNNY = "funny"
    URGENT = "urgent"


class ScriptFormat(str, Enum):
    REEL = "reel"
    SHORT = "short"
    TALKING_HEAD = "talking_head"
    VOICEOVER = "voiceover"


class HookStyle(str, Enum):
    QUESTION = "question"
    BOLD_CLAIM = "bold_claim"
    CONTROVERSY = "controversy"
    STORY = "story"


class CaptionStyle(str, Enum):
    CLEAN = "clean"           # Professional, no hype
    PUNCHY = "punchy"         # Bold, viral energy
    TEACH_MODE = "teach_mode" # Educational micro-thread


class CarouselStyle(str, Enum):
    MINIMAL = "minimal"
    BOLD = "bold"
    GRADIENT = "gradient"
    PHOTO_OVERLAY = "photo_overlay"


# =========================================================================
# Data Classes
# =========================================================================

@dataclass
class ScriptBeat:
    """A single beat/section of a script"""
    name: str                    # "build_up", "punchline", "cta"
    duration_seconds: int
    script: str
    visual_notes: str
    word_count: int


@dataclass
class ScriptResult:
    """Complete script generation result"""
    beats: List[ScriptBeat]
    total_duration: int
    estimated_word_count: int
    hooks: List[str]             # Alternative hook options
    hashtag_suggestions: List[str]
    topic: str
    tone: str
    format: str
    generated_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    
    def to_dict(self) -> Dict:
        return {
            **asdict(self),
            "beats": [asdict(b) for b in self.beats]
        }


@dataclass
class Caption:
    """A single caption variant"""
    style: str
    caption: str
    character_count: int
    emoji_usage: str


@dataclass
class CaptionsResult:
    """Complete captions generation result"""
    captions: List[Caption]
    hashtags: Dict[str, List[str]]  # {niche: [], format: [], discovery: []}
    total_hashtag_count: int
    cta_suggestions: List[str]
    topic: str
    generated_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    
    def to_dict(self) -> Dict:
        return {
            **asdict(self),
            "captions": [asdict(c) for c in self.captions]
        }


@dataclass
class CarouselSlide:
    """A single carousel slide"""
    slide_number: int
    purpose: str                 # "hook", "value", "cta"
    headline: str
    body_text: str
    image_inspo: str
    color_suggestion: str
    layout: str                  # "text_only", "text_image", "quote"


@dataclass
class CarouselResult:
    """Complete carousel generation result"""
    title: str
    slides: List[CarouselSlide]
    cover_text: str
    design_style: str
    topic: str
    generated_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    
    def to_dict(self) -> Dict:
        return {
            **asdict(self),
            "slides": [asdict(s) for s in self.slides]
        }


@dataclass
class HashtagRecommendation:
    """Hashtag recommendations by bucket"""
    niche: List[str]
    format: List[str]
    discovery: List[str]
    total_count: int
    topic: str
    niche_category: str
    generated_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    
    def to_dict(self) -> Dict:
        return asdict(self)


# =========================================================================
# Timing Budgets
# =========================================================================

TIMING_BUDGETS = {
    ScriptLength.SHORT: {"build_up": 8, "punchline": 8, "cta": 6, "total": 22},
    ScriptLength.MEDIUM: {"build_up": 15, "punchline": 15, "cta": 15, "total": 45},
    ScriptLength.LONG: {"build_up": 25, "punchline": 25, "cta": 15, "total": 65},
}


# =========================================================================
# ReelTrends Service
# =========================================================================

class ReelTrendsService:
    """
    AI-powered content generation for Instagram/Reels creators.
    """
    
    def __init__(self, api_key: str = None):
        self.api_key = api_key or OPENAI_API_KEY
        self.client = AsyncOpenAI(api_key=self.api_key) if self.api_key else None
        self.model = "gpt-4o"  # Use latest model
    
    # =========================================================================
    # Script Generator
    # =========================================================================
    
    async def generate_script(
        self,
        topic: str,
        tone: ScriptTone = ScriptTone.CASUAL,
        length: ScriptLength = ScriptLength.MEDIUM,
        format: ScriptFormat = ScriptFormat.REEL,
        niche: str = None,
        hook_style: HookStyle = HookStyle.QUESTION
    ) -> ScriptResult:
        """
        Generate a 3-beat video script with time budgets.
        
        Args:
            topic: What the video is about
            tone: casual, professional, funny, urgent
            length: short (22s), medium (45s), long (65s)
            format: reel, short, talking_head, voiceover
            niche: Optional niche category
            hook_style: question, bold_claim, controversy, story
        """
        if not self.client:
            raise ValueError("OpenAI API key not configured")
        
        timing = TIMING_BUDGETS[length]
        
        prompt = f"""Create a {length.value} video script about: {topic}

Tone: {tone.value}
Format: {format.value}
Hook style: {hook_style.value}
{f'Niche: {niche}' if niche else ''}

Timing budget:
- Build-up: {timing['build_up']} seconds
- Punchline: {timing['punchline']} seconds  
- CTA: {timing['cta']} seconds
- Total: {timing['total']} seconds

Output ONLY valid JSON (no markdown, no code blocks):
{{
  "beats": [
    {{
      "name": "build_up",
      "duration_seconds": {timing['build_up']},
      "script": "The actual script text to say...",
      "visual_notes": "What to show on screen",
      "word_count": <number>
    }},
    {{
      "name": "punchline",
      "duration_seconds": {timing['punchline']},
      "script": "...",
      "visual_notes": "...",
      "word_count": <number>
    }},
    {{
      "name": "cta",
      "duration_seconds": {timing['cta']},
      "script": "...",
      "visual_notes": "...",
      "word_count": <number>
    }}
  ],
  "hooks": ["Alternative hook 1", "Alternative hook 2", "Alternative hook 3"],
  "hashtag_suggestions": ["#tag1", "#tag2", "#tag3", "#tag4", "#tag5"]
}}

Rules:
- ~2.5 words per second speaking pace
- Hook must grab attention in first 2 seconds
- End with clear CTA
- Be conversational, not salesy
- Make it authentic and engaging"""

        try:
            response = await self.client.chat.completions.create(
                model=self.model,
                messages=[
                    {
                        "role": "system",
                        "content": "You are an expert short-form video scriptwriter. Generate scripts that are engaging, authentic, and optimized for Reels/TikTok. Output ONLY valid JSON, no markdown."
                    },
                    {"role": "user", "content": prompt}
                ],
                temperature=0.8,
                max_tokens=2000
            )
            
            content = response.choices[0].message.content.strip()
            # Clean up any markdown formatting
            if content.startswith("```"):
                content = content.split("```")[1]
                if content.startswith("json"):
                    content = content[4:]
            content = content.strip()
            
            data = json.loads(content)
            
            beats = [
                ScriptBeat(
                    name=b["name"],
                    duration_seconds=b["duration_seconds"],
                    script=b["script"],
                    visual_notes=b["visual_notes"],
                    word_count=b["word_count"]
                )
                for b in data["beats"]
            ]
            
            return ScriptResult(
                beats=beats,
                total_duration=timing["total"],
                estimated_word_count=sum(b.word_count for b in beats),
                hooks=data.get("hooks", []),
                hashtag_suggestions=data.get("hashtag_suggestions", []),
                topic=topic,
                tone=tone.value,
                format=format.value
            )
            
        except json.JSONDecodeError as e:
            logger.error(f"JSON parse error in script generation: {e}")
            logger.error(f"Raw content: {content}")
            raise ValueError(f"Failed to parse AI response: {e}")
        except Exception as e:
            logger.error(f"Script generation error: {e}")
            raise
    
    # =========================================================================
    # Captions Generator
    # =========================================================================
    
    async def generate_captions(
        self,
        topic: str,
        tone: str = None,
        niche: str = None,
        include_hashtags: bool = True,
        emoji_level: Literal["minimal", "moderate", "heavy"] = "moderate"
    ) -> CaptionsResult:
        """
        Generate 3 caption variants + bucketed hashtags.
        
        Styles:
        - Clean: Professional, no hype, credible
        - Punchy: Bold, viral energy, meme-y
        - Teach-Mode: Educational micro-thread
        
        Hashtag buckets:
        - Niche: 5 tags targeting specific audience
        - Format: 3 tags for content type
        - Discovery: 2 broad reach tags
        """
        if not self.client:
            raise ValueError("OpenAI API key not configured")
        
        prompt = f"""Create 3 Instagram captions for content about: {topic}

{f'Tone: {tone}' if tone else ''}
{f'Niche: {niche}' if niche else 'Niche: general'}
Emoji level: {emoji_level}

Output ONLY valid JSON (no markdown, no code blocks):
{{
  "captions": [
    {{
      "style": "clean",
      "caption": "Professional, no hype, credible caption text here...",
      "character_count": <number>,
      "emoji_usage": "minimal"
    }},
    {{
      "style": "punchy",
      "caption": "Bold, viral energy, meme-y caption with emojis 🔥...",
      "character_count": <number>,
      "emoji_usage": "heavy"
    }},
    {{
      "style": "teach_mode",
      "caption": "Educational micro-thread style:\\n\\n1. First point...\\n2. Second point...",
      "character_count": <number>,
      "emoji_usage": "moderate"
    }}
  ],
  "hashtags": {{
    "niche": ["#tag1", "#tag2", "#tag3", "#tag4", "#tag5"],
    "format": ["#reels", "#tutorial", "#tips"],
    "discovery": ["#viral", "#trending"]
  }},
  "cta_suggestions": [
    "Save this for later 📌",
    "Drop a 🔥 if this helped",
    "Tag someone who needs this"
  ]
}}

Rules:
- Clean: No excessive emojis, professional tone, value-focused
- Punchy: Bold claims, trending phrases, heavy emojis, viral energy
- Teach-Mode: Numbered points or mini-thread format, educational
- Each caption should work standalone
- Max 2200 characters per caption
- Include line breaks for readability"""

        try:
            response = await self.client.chat.completions.create(
                model=self.model,
                messages=[
                    {
                        "role": "system",
                        "content": "You are a social media caption expert. Generate 3 caption variants that are engaging and platform-optimized. Output ONLY valid JSON, no markdown."
                    },
                    {"role": "user", "content": prompt}
                ],
                temperature=0.8,
                max_tokens=2000
            )
            
            content = response.choices[0].message.content.strip()
            if content.startswith("```"):
                content = content.split("```")[1]
                if content.startswith("json"):
                    content = content[4:]
            content = content.strip()
            
            data = json.loads(content)
            
            captions = [
                Caption(
                    style=c["style"],
                    caption=c["caption"],
                    character_count=c.get("character_count", len(c["caption"])),
                    emoji_usage=c.get("emoji_usage", "moderate")
                )
                for c in data["captions"]
            ]
            
            hashtags = data.get("hashtags", {"niche": [], "format": [], "discovery": []})
            
            return CaptionsResult(
                captions=captions,
                hashtags=hashtags,
                total_hashtag_count=sum(len(v) for v in hashtags.values()),
                cta_suggestions=data.get("cta_suggestions", []),
                topic=topic
            )
            
        except json.JSONDecodeError as e:
            logger.error(f"JSON parse error in captions generation: {e}")
            raise ValueError(f"Failed to parse AI response: {e}")
        except Exception as e:
            logger.error(f"Captions generation error: {e}")
            raise
    
    # =========================================================================
    # Carousel Generator
    # =========================================================================
    
    async def generate_carousel(
        self,
        topic: str,
        slide_count: int = 5,
        style: CarouselStyle = CarouselStyle.MINIMAL,
        niche: str = None
    ) -> CarouselResult:
        """
        Generate carousel slide content with copy + image inspiration.
        
        Structure:
        - Slide 1: Hook (question or bold claim)
        - Slides 2-N-1: Value (steps, framework, examples)
        - Slide N: CTA (takeaway + action)
        """
        if not self.client:
            raise ValueError("OpenAI API key not configured")
        
        slide_count = max(3, min(slide_count, 10))  # Clamp to 3-10
        
        prompt = f"""Create a {slide_count}-slide Instagram carousel about: {topic}

Style: {style.value}
{f'Niche: {niche}' if niche else ''}

Output ONLY valid JSON (no markdown, no code blocks):
{{
  "title": "Carousel title for internal reference",
  "slides": [
    {{
      "slide_number": 1,
      "purpose": "hook",
      "headline": "Big bold attention-grabbing text",
      "body_text": "Supporting copy (1-2 sentences)",
      "image_inspo": "Visual suggestion for this slide",
      "color_suggestion": "#HEXCODE",
      "layout": "text_only"
    }},
    {{
      "slide_number": 2,
      "purpose": "value",
      "headline": "First key point",
      "body_text": "Explanation...",
      "image_inspo": "...",
      "color_suggestion": "#HEXCODE",
      "layout": "text_image"
    }},
    // ... more value slides ...
    {{
      "slide_number": {slide_count},
      "purpose": "cta",
      "headline": "Action text",
      "body_text": "Final takeaway + what to do next",
      "image_inspo": "...",
      "color_suggestion": "#HEXCODE",
      "layout": "text_only"
    }}
  ],
  "cover_text": "Text for the cover/thumbnail that shows in feed",
  "design_style": "{style.value}"
}}

Structure:
- Slide 1: Hook (question or bold claim to stop the scroll)
- Slides 2-{slide_count - 1}: Value (steps, framework, tips, examples)
- Slide {slide_count}: CTA (takeaway + action)

Rules:
- Headlines: 3-6 words max, scannable
- Body text: 1-2 sentences max
- Each slide should be readable in 2 seconds
- Use contrast colors for readability
- Layout options: text_only, text_image, quote"""

        try:
            response = await self.client.chat.completions.create(
                model=self.model,
                messages=[
                    {
                        "role": "system",
                        "content": "You are a carousel content strategist. Create slide-by-slide content with headlines, body text, and visual suggestions. Output ONLY valid JSON, no markdown."
                    },
                    {"role": "user", "content": prompt}
                ],
                temperature=0.8,
                max_tokens=3000
            )
            
            content = response.choices[0].message.content.strip()
            if content.startswith("```"):
                content = content.split("```")[1]
                if content.startswith("json"):
                    content = content[4:]
            content = content.strip()
            
            data = json.loads(content)
            
            slides = [
                CarouselSlide(
                    slide_number=s["slide_number"],
                    purpose=s["purpose"],
                    headline=s["headline"],
                    body_text=s["body_text"],
                    image_inspo=s["image_inspo"],
                    color_suggestion=s.get("color_suggestion", "#000000"),
                    layout=s.get("layout", "text_only")
                )
                for s in data["slides"]
            ]
            
            return CarouselResult(
                title=data.get("title", topic),
                slides=slides,
                cover_text=data.get("cover_text", ""),
                design_style=data.get("design_style", style.value),
                topic=topic
            )
            
        except json.JSONDecodeError as e:
            logger.error(f"JSON parse error in carousel generation: {e}")
            raise ValueError(f"Failed to parse AI response: {e}")
        except Exception as e:
            logger.error(f"Carousel generation error: {e}")
            raise
    
    # =========================================================================
    # Hashtag Recommender
    # =========================================================================
    
    async def recommend_hashtags(
        self,
        topic: str,
        niche: str = None,
        count: int = 10
    ) -> HashtagRecommendation:
        """
        Recommend hashtags in three buckets:
        - Niche: 5 tags targeting specific audience
        - Format: 3 tags for content type
        - Discovery: 2 broad reach tags
        """
        if not self.client:
            raise ValueError("OpenAI API key not configured")
        
        prompt = f"""Recommend Instagram hashtags for content about: {topic}

{f'Niche/Category: {niche}' if niche else 'Determine the most relevant niche category'}

Output ONLY valid JSON (no markdown, no code blocks):
{{
  "niche_category": "the detected or specified niche",
  "hashtags": {{
    "niche": ["#specific1", "#specific2", "#specific3", "#specific4", "#specific5"],
    "format": ["#reels", "#tutorial", "#tips"],
    "discovery": ["#explore", "#viral"]
  }},
  "reasoning": {{
    "niche": "Why these niche tags were chosen",
    "format": "Why these format tags",
    "discovery": "Why these discovery tags"
  }}
}}

Bucket purposes:
- Niche (5 tags): Target your specific audience, moderate competition
- Format (3 tags): Content type (#reels, #carousel, #tutorial, #howto)
- Discovery (2 tags): Broad reach, high volume (#viral, #trending, #fyp, #explore)

Rules:
- Include the # symbol
- Mix popular and mid-tier tags for reach + discoverability
- Avoid banned or spammy hashtags
- Make them relevant to the actual topic"""

        try:
            response = await self.client.chat.completions.create(
                model=self.model,
                messages=[
                    {
                        "role": "system",
                        "content": "You are a social media hashtag strategist. Recommend hashtags that balance reach and relevance. Output ONLY valid JSON, no markdown."
                    },
                    {"role": "user", "content": prompt}
                ],
                temperature=0.7,
                max_tokens=1000
            )
            
            content = response.choices[0].message.content.strip()
            if content.startswith("```"):
                content = content.split("```")[1]
                if content.startswith("json"):
                    content = content[4:]
            content = content.strip()
            
            data = json.loads(content)
            hashtags = data.get("hashtags", {})
            
            return HashtagRecommendation(
                niche=hashtags.get("niche", []),
                format=hashtags.get("format", []),
                discovery=hashtags.get("discovery", []),
                total_count=sum(len(v) for v in hashtags.values()),
                topic=topic,
                niche_category=data.get("niche_category", niche or "general")
            )
            
        except json.JSONDecodeError as e:
            logger.error(f"JSON parse error in hashtag recommendation: {e}")
            raise ValueError(f"Failed to parse AI response: {e}")
        except Exception as e:
            logger.error(f"Hashtag recommendation error: {e}")
            raise
    
    # =========================================================================
    # Batch Generation
    # =========================================================================
    
    async def generate_content_pack(
        self,
        topic: str,
        niche: str = None,
        tone: str = "casual"
    ) -> Dict[str, Any]:
        """
        Generate a complete content pack with script, captions, and carousel.
        """
        results = await asyncio.gather(
            self.generate_script(
                topic=topic,
                tone=ScriptTone(tone),
                niche=niche
            ),
            self.generate_captions(
                topic=topic,
                niche=niche
            ),
            self.generate_carousel(
                topic=topic,
                niche=niche
            ),
            self.recommend_hashtags(
                topic=topic,
                niche=niche
            ),
            return_exceptions=True
        )
        
        return {
            "topic": topic,
            "niche": niche,
            "script": results[0].to_dict() if not isinstance(results[0], Exception) else {"error": str(results[0])},
            "captions": results[1].to_dict() if not isinstance(results[1], Exception) else {"error": str(results[1])},
            "carousel": results[2].to_dict() if not isinstance(results[2], Exception) else {"error": str(results[2])},
            "hashtags": results[3].to_dict() if not isinstance(results[3], Exception) else {"error": str(results[3])},
            "generated_at": datetime.now(timezone.utc).isoformat()
        }


# =========================================================================
# Test Function
# =========================================================================

async def test_reeltrends():
    """Test all ReelTrends generators"""
    service = ReelTrendsService()
    
    topic = "5 productivity hacks for remote workers"
    
    print("\n" + "="*60)
    print("🎬 SCRIPT GENERATOR")
    print("="*60)
    
    try:
        script = await service.generate_script(
            topic=topic,
            tone=ScriptTone.CASUAL,
            length=ScriptLength.MEDIUM,
            format=ScriptFormat.TALKING_HEAD
        )
        print(f"Topic: {script.topic}")
        print(f"Duration: {script.total_duration}s")
        print(f"Words: {script.estimated_word_count}")
        for beat in script.beats:
            print(f"\n[{beat.name.upper()}] ({beat.duration_seconds}s)")
            print(f"  Script: {beat.script[:100]}...")
            print(f"  Visual: {beat.visual_notes[:50]}...")
        print(f"\nHooks: {script.hooks}")
    except Exception as e:
        print(f"Error: {e}")
    
    print("\n" + "="*60)
    print("📝 CAPTIONS GENERATOR")
    print("="*60)
    
    try:
        captions = await service.generate_captions(
            topic=topic,
            niche="productivity"
        )
        for cap in captions.captions:
            print(f"\n[{cap.style.upper()}] ({cap.character_count} chars)")
            print(f"  {cap.caption[:150]}...")
        print(f"\nHashtags: {captions.total_hashtag_count} total")
        print(f"  Niche: {captions.hashtags['niche']}")
        print(f"  Format: {captions.hashtags['format']}")
        print(f"  Discovery: {captions.hashtags['discovery']}")
    except Exception as e:
        print(f"Error: {e}")
    
    print("\n" + "="*60)
    print("🎠 CAROUSEL GENERATOR")
    print("="*60)
    
    try:
        carousel = await service.generate_carousel(
            topic=topic,
            slide_count=5,
            style=CarouselStyle.MINIMAL
        )
        print(f"Title: {carousel.title}")
        print(f"Cover: {carousel.cover_text}")
        for slide in carousel.slides:
            print(f"\n[Slide {slide.slide_number}] {slide.purpose.upper()}")
            print(f"  Headline: {slide.headline}")
            print(f"  Body: {slide.body_text[:80]}...")
            print(f"  Visual: {slide.image_inspo[:50]}...")
    except Exception as e:
        print(f"Error: {e}")
    
    print("\n" + "="*60)
    print("#️⃣ HASHTAG RECOMMENDER")
    print("="*60)
    
    try:
        hashtags = await service.recommend_hashtags(
            topic=topic,
            niche="productivity"
        )
        print(f"Category: {hashtags.niche_category}")
        print(f"Niche: {hashtags.niche}")
        print(f"Format: {hashtags.format}")
        print(f"Discovery: {hashtags.discovery}")
    except Exception as e:
        print(f"Error: {e}")


if __name__ == "__main__":
    asyncio.run(test_reeltrends())
