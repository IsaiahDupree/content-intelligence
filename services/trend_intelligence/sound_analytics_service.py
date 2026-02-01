"""
Sound Analytics Service
=======================
Track and forecast trending sounds for Instagram/TikTok.

Features:
- Track sound usage over time
- Forecast sound trends (rising/falling/stable)
- Sound of the day recommendations
- Save sounds for later use
- Top performing sounds by niche

Data sources:
- User-submitted sounds
- RapidAPI trend data
- Manual curation
"""
import os
import json
import asyncio
from datetime import datetime, timedelta, timezone
from typing import List, Dict, Any, Optional
from dataclasses import dataclass, field, asdict
from collections import defaultdict
from enum import Enum

from openai import AsyncOpenAI
from sqlalchemy import create_engine, text
from loguru import logger

DATABASE_URL = os.getenv("DATABASE_URL", "postgresql://postgres:postgres@localhost:54322/postgres")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "")


class SoundTrend(str, Enum):
    RISING = "rising"
    STABLE = "stable"
    FALLING = "falling"
    VIRAL = "viral"
    NEW = "new"


@dataclass
class SoundMetrics:
    """Metrics for a single sound"""
    sound_id: str
    platform: str               # instagram, tiktok
    title: str
    artist: str
    duration_seconds: int
    
    # Current metrics
    uses_today: int
    uses_7d: int
    uses_30d: int
    total_uses: int
    
    # Engagement
    avg_views: int
    avg_likes: int
    avg_engagement_rate: float
    
    # Trend analysis
    trend: SoundTrend
    velocity: float             # % change per day
    forecast_7d: str            # "up 25%", "down 10%", "stable"
    peak_date: Optional[str]    # When it peaked/will peak
    
    # Categories
    niches: List[str]
    moods: List[str]
    
    # Metadata
    cover_url: Optional[str]
    preview_url: Optional[str]
    discovered_at: str
    updated_at: str


@dataclass
class SoundTimeseries:
    """Time series data for a sound"""
    sound_id: str
    dates: List[str]
    uses: List[int]
    views: List[int]
    likes: List[int]
    forecast_dates: List[str]
    forecast_uses: List[int]


@dataclass
class SoundOfTheDay:
    """Featured sound recommendation"""
    sound: SoundMetrics
    reason: str
    best_for: List[str]
    example_hooks: List[str]
    suggested_niches: List[str]


@dataclass
class SoundSearchResult:
    """Search results for sounds"""
    sounds: List[SoundMetrics]
    total_count: int
    page: int
    per_page: int
    filters_applied: Dict[str, Any]


class SoundAnalyticsService:
    """
    Analyzes and forecasts sound trends.
    
    Note: Real sound data requires platform API access or licensed providers.
    This implementation uses:
    - Database-stored sounds (user submissions + API imports)
    - AI for trend analysis and recommendations
    """
    
    def __init__(self):
        self.engine = create_engine(DATABASE_URL)
        self.openai = AsyncOpenAI(api_key=OPENAI_API_KEY) if OPENAI_API_KEY else None
        self.model = "gpt-4o"
    
    async def get_trending_sounds(
        self,
        platform: str = "instagram",
        niche: Optional[str] = None,
        limit: int = 20,
        time_range: str = "7d"
    ) -> List[SoundMetrics]:
        """
        Get currently trending sounds.
        
        Args:
            platform: instagram or tiktok
            niche: Filter by niche/category
            limit: Number of sounds to return
            time_range: 24h, 7d, 30d
        """
        try:
            sounds = self._fetch_sounds_from_db(platform, niche, limit, time_range)
            if sounds:
                return sounds
        except Exception as e:
            logger.warning(f"Could not fetch sounds from DB: {e}")
        
        # Return curated trending sounds if no DB data
        return await self._generate_trending_sounds(platform, niche, limit)
    
    async def get_sound_of_the_day(
        self,
        niche: Optional[str] = None,
        content_type: str = "reel"
    ) -> SoundOfTheDay:
        """Get AI-recommended sound of the day"""
        if not self.openai:
            raise ValueError("OpenAI API key not configured")
        
        prompt = f"""Recommend a trending sound for Instagram/TikTok content creation.

{f'Niche: {niche}' if niche else 'General content'}
Content type: {content_type}

Output ONLY valid JSON:
{{
  "sound": {{
    "title": "Song or sound title",
    "artist": "Artist name",
    "duration_seconds": 30,
    "trend": "rising|viral|stable",
    "niches": ["niche1", "niche2"],
    "moods": ["energetic", "calm", etc]
  }},
  "reason": "Why this sound is perfect right now",
  "best_for": ["content type 1", "content type 2"],
  "example_hooks": ["Hook idea using this sound 1", "Hook idea 2"],
  "suggested_niches": ["niche1", "niche2", "niche3"]
}}

Consider:
- Current trending sounds on Reels/TikTok
- Sound-to-content fit
- Engagement potential
- Avoid overused/saturated sounds"""

        try:
            response = await self.openai.chat.completions.create(
                model=self.model,
                messages=[
                    {"role": "system", "content": "You are an expert on social media audio trends. Recommend sounds based on current trends."},
                    {"role": "user", "content": prompt}
                ],
                temperature=0.8,
                max_tokens=1000
            )
            
            content = response.choices[0].message.content.strip()
            if content.startswith("```"):
                content = content.split("```")[1]
                if content.startswith("json"):
                    content = content[4:]
            content = content.strip()
            
            data = json.loads(content)
            sound_data = data.get("sound", {})
            
            now = datetime.now(timezone.utc).isoformat()
            sound = SoundMetrics(
                sound_id=f"rec_{datetime.now().strftime('%Y%m%d')}",
                platform="instagram",
                title=sound_data.get("title", ""),
                artist=sound_data.get("artist", ""),
                duration_seconds=sound_data.get("duration_seconds", 30),
                uses_today=0,
                uses_7d=0,
                uses_30d=0,
                total_uses=0,
                avg_views=0,
                avg_likes=0,
                avg_engagement_rate=0,
                trend=SoundTrend(sound_data.get("trend", "rising")),
                velocity=0,
                forecast_7d="",
                peak_date=None,
                niches=sound_data.get("niches", []),
                moods=sound_data.get("moods", []),
                cover_url=None,
                preview_url=None,
                discovered_at=now,
                updated_at=now
            )
            
            return SoundOfTheDay(
                sound=sound,
                reason=data.get("reason", ""),
                best_for=data.get("best_for", []),
                example_hooks=data.get("example_hooks", []),
                suggested_niches=data.get("suggested_niches", [])
            )
            
        except json.JSONDecodeError as e:
            logger.error(f"JSON parse error: {e}")
            raise ValueError(f"Failed to parse AI response: {e}")
        except Exception as e:
            logger.error(f"Sound of the day error: {e}")
            raise
    
    async def analyze_sound(
        self,
        sound_name: str,
        artist: Optional[str] = None
    ) -> Dict[str, Any]:
        """
        Analyze a specific sound's trend potential.
        
        Args:
            sound_name: Name/title of the sound
            artist: Optional artist name
        """
        if not self.openai:
            raise ValueError("OpenAI API key not configured")
        
        prompt = f"""Analyze this sound for social media content potential:

Sound: {sound_name}
{f'Artist: {artist}' if artist else ''}

Output ONLY valid JSON:
{{
  "sound_name": "{sound_name}",
  "artist": "{artist or 'Unknown'}",
  
  "trend_analysis": {{
    "current_status": "viral|rising|stable|falling|unknown",
    "saturation_level": "low|medium|high",
    "estimated_uses_daily": <number>,
    "peak_prediction": "already peaked|peaking now|will peak in X days|unknown"
  }},
  
  "content_fit": {{
    "best_niches": ["niche1", "niche2", "niche3"],
    "best_formats": ["talking head", "transitions", "montage", etc],
    "mood": "energetic|calm|dramatic|funny|etc",
    "pacing": "fast|medium|slow"
  }},
  
  "usage_tips": [
    "Tip 1 for using this sound",
    "Tip 2",
    "Tip 3"
  ],
  
  "similar_sounds": [
    {{"name": "Similar sound 1", "artist": "Artist"}},
    {{"name": "Similar sound 2", "artist": "Artist"}}
  ],
  
  "recommendation": {{
    "should_use": true|false,
    "reason": "Why or why not",
    "timing": "Use now|Wait|Too late"
  }}
}}"""

        try:
            response = await self.openai.chat.completions.create(
                model=self.model,
                messages=[
                    {"role": "system", "content": "You are an expert on TikTok/Instagram audio trends. Analyze sounds for content creation."},
                    {"role": "user", "content": prompt}
                ],
                temperature=0.7,
                max_tokens=1500
            )
            
            content = response.choices[0].message.content.strip()
            if content.startswith("```"):
                content = content.split("```")[1]
                if content.startswith("json"):
                    content = content[4:]
            content = content.strip()
            
            data = json.loads(content)
            data["analyzed_at"] = datetime.now(timezone.utc).isoformat()
            return data
            
        except json.JSONDecodeError as e:
            logger.error(f"JSON parse error: {e}")
            raise ValueError(f"Failed to parse AI response: {e}")
        except Exception as e:
            logger.error(f"Sound analysis error: {e}")
            raise
    
    async def get_sounds_for_niche(
        self,
        niche: str,
        mood: Optional[str] = None,
        count: int = 5
    ) -> List[Dict[str, Any]]:
        """Get sound recommendations for a specific niche"""
        if not self.openai:
            raise ValueError("OpenAI API key not configured")
        
        prompt = f"""Recommend {count} trending sounds for this niche:

Niche: {niche}
{f'Mood: {mood}' if mood else ''}

Output ONLY valid JSON array:
[
  {{
    "title": "Sound title",
    "artist": "Artist name",
    "why_it_works": "Why this sound fits the niche",
    "content_ideas": ["Content idea 1", "Content idea 2"],
    "trend_status": "viral|rising|stable",
    "difficulty": "easy|medium|hard"
  }}
]

Focus on:
- Currently trending sounds (not overused)
- Good fit for the niche
- Versatile for different content types"""

        try:
            response = await self.openai.chat.completions.create(
                model=self.model,
                messages=[
                    {"role": "system", "content": "You are an expert on social media audio trends."},
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
            
            sounds = json.loads(content)
            return sounds
            
        except json.JSONDecodeError as e:
            logger.error(f"JSON parse error: {e}")
            raise ValueError(f"Failed to parse AI response: {e}")
        except Exception as e:
            logger.error(f"Niche sounds error: {e}")
            raise
    
    def _fetch_sounds_from_db(
        self,
        platform: str,
        niche: Optional[str],
        limit: int,
        time_range: str
    ) -> List[SoundMetrics]:
        """Fetch sounds from database"""
        # This would query a sounds table if it exists
        # For now, return empty list to trigger AI generation
        return []
    
    async def _generate_trending_sounds(
        self,
        platform: str,
        niche: Optional[str],
        limit: int
    ) -> List[SoundMetrics]:
        """Generate trending sound recommendations using AI"""
        if not self.openai:
            return []
        
        prompt = f"""List {limit} currently trending sounds on {platform}:

{f'Focus on niche: {niche}' if niche else 'General/various niches'}

Output ONLY valid JSON array:
[
  {{
    "title": "Sound title",
    "artist": "Artist name",
    "duration_seconds": 30,
    "trend": "viral|rising|stable|falling",
    "velocity_percent": <daily change %>,
    "niches": ["niche1", "niche2"],
    "moods": ["mood1", "mood2"],
    "uses_estimate": <estimated daily uses>
  }}
]

Include a mix of:
- Viral sounds (3-4)
- Rising sounds (3-4)
- Stable/reliable sounds (2-3)"""

        try:
            response = await self.openai.chat.completions.create(
                model=self.model,
                messages=[
                    {"role": "system", "content": "You are an expert on TikTok/Instagram audio trends. Provide realistic trending sound data."},
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
            now = datetime.now(timezone.utc).isoformat()
            
            sounds = []
            for i, s in enumerate(data):
                sounds.append(SoundMetrics(
                    sound_id=f"gen_{i}_{datetime.now().strftime('%Y%m%d')}",
                    platform=platform,
                    title=s.get("title", ""),
                    artist=s.get("artist", ""),
                    duration_seconds=s.get("duration_seconds", 30),
                    uses_today=s.get("uses_estimate", 0),
                    uses_7d=s.get("uses_estimate", 0) * 7,
                    uses_30d=s.get("uses_estimate", 0) * 30,
                    total_uses=s.get("uses_estimate", 0) * 30,
                    avg_views=0,
                    avg_likes=0,
                    avg_engagement_rate=0,
                    trend=SoundTrend(s.get("trend", "stable")),
                    velocity=s.get("velocity_percent", 0),
                    forecast_7d="",
                    peak_date=None,
                    niches=s.get("niches", []),
                    moods=s.get("moods", []),
                    cover_url=None,
                    preview_url=None,
                    discovered_at=now,
                    updated_at=now
                ))
            
            return sounds
            
        except Exception as e:
            logger.error(f"Trending sounds generation error: {e}")
            return []
    
    async def save_sound(
        self,
        user_id: str,
        sound_id: str,
        notes: Optional[str] = None
    ) -> bool:
        """Save a sound to user's collection"""
        try:
            with self.engine.connect() as conn:
                conn.execute(text("""
                    INSERT INTO saved_sounds (user_id, sound_id, notes, saved_at)
                    VALUES (:user_id, :sound_id, :notes, NOW())
                    ON CONFLICT (user_id, sound_id) DO UPDATE SET notes = :notes
                """), {"user_id": user_id, "sound_id": sound_id, "notes": notes})
                conn.commit()
            return True
        except Exception as e:
            logger.warning(f"Could not save sound: {e}")
            return False
    
    async def get_saved_sounds(self, user_id: str) -> List[Dict]:
        """Get user's saved sounds"""
        try:
            with self.engine.connect() as conn:
                result = conn.execute(text("""
                    SELECT sound_id, notes, saved_at
                    FROM saved_sounds
                    WHERE user_id = :user_id
                    ORDER BY saved_at DESC
                """), {"user_id": user_id})
                return [dict(row._mapping) for row in result]
        except Exception as e:
            logger.warning(f"Could not fetch saved sounds: {e}")
            return []


# Test function
async def test_sound_analytics():
    service = SoundAnalyticsService()
    
    print("\n" + "="*60)
    print("🎵 SOUND ANALYTICS TEST")
    print("="*60)
    
    # Test sound of the day
    print("\n1. Sound of the Day:")
    sotd = await service.get_sound_of_the_day(niche="fitness")
    print(f"   Title: {sotd.sound.title}")
    print(f"   Artist: {sotd.sound.artist}")
    print(f"   Reason: {sotd.reason}")
    print(f"   Best for: {', '.join(sotd.best_for[:3])}")
    
    # Test sound analysis
    print("\n2. Analyze Sound:")
    analysis = await service.analyze_sound("Makeba", "Jain")
    print(f"   Status: {analysis['trend_analysis']['current_status']}")
    print(f"   Saturation: {analysis['trend_analysis']['saturation_level']}")
    print(f"   Should use: {analysis['recommendation']['should_use']}")
    print(f"   Reason: {analysis['recommendation']['reason']}")
    
    # Test niche sounds
    print("\n3. Sounds for Niche:")
    niche_sounds = await service.get_sounds_for_niche("technology", count=3)
    for s in niche_sounds:
        print(f"   - {s['title']} by {s['artist']} ({s['trend_status']})")


if __name__ == "__main__":
    asyncio.run(test_sound_analytics())
