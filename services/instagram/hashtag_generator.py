"""
Hashtag Generator Service
AI-powered hashtag recommendations based on content and trending data
Uses ModelRegistry for configurable model selection (Groq by default, 100% cost savings)
"""
import os
from typing import List, Dict, Optional
from loguru import logger
from sqlalchemy import create_engine, text

from config.model_registry import TaskType, ModelRegistry
from services.ai_client import AIClient

DATABASE_URL = os.getenv("DATABASE_URL", "postgresql://postgres:postgres@localhost:54322/postgres")


class HashtagGenerator:
    """
    Generates optimized hashtag sets for Instagram content.
    Uses ModelRegistry for AI model selection (Groq Llama 3.1 8B by default - FREE)
    
    Strategy:
    - 10 trending hashtags (high velocity, high competition)
    - 10 niche hashtags (medium competition, targeted)
    - 10 long-tail hashtags (low competition, specific)
    
    Total: 30 hashtags per content piece
    """
    
    def __init__(self):
        self.engine = create_engine(DATABASE_URL)
        
        # Get model configuration from registry
        self.config = ModelRegistry.get_model_config(TaskType.HASHTAG_GENERATION)
        try:
            self.client = AIClient(self.config)
            logger.info(f"HashtagGenerator using {self.config.provider}/{self.config.model}")
        except Exception as e:
            logger.warning(f"HashtagGenerator AI disabled: {e}")
            self.client = None
    
    async def generate_hashtags(
        self,
        content: str,
        niche: Optional[str] = None,
        existing_hashtags: Optional[List[str]] = None
    ) -> Dict[str, List[Dict]]:
        """
        Generate optimized hashtag set for content.
        
        Args:
            content: Video transcript or caption
            niche: Content niche (auto-detected if None)
            existing_hashtags: Hashtags already used
            
        Returns:
            Dict with trending, niche, and long-tail hashtag sets
        """
        logger.info("Generating hashtag recommendations")
        
        # Detect niche if not provided
        if not niche:
            niche = await self._detect_niche(content)
        
        # Get trending hashtags
        trending = self._get_trending_hashtags(niche, limit=10)
        
        # Get niche-specific hashtags
        niche_tags = self._get_niche_hashtags(niche, limit=10)
        
        # Generate long-tail hashtags with AI
        long_tail = await self._generate_long_tail_hashtags(content, niche, limit=10)
        
        # Filter out existing hashtags
        if existing_hashtags:
            existing_set = set(h.lower().lstrip('#') for h in existing_hashtags)
            trending = [h for h in trending if h['tag'].lower() not in existing_set]
            niche_tags = [h for h in niche_tags if h['tag'].lower() not in existing_set]
            long_tail = [h for h in long_tail if h['tag'].lower() not in existing_set]
        
        return {
            "trending": trending[:10],
            "niche": niche_tags[:10],
            "long_tail": long_tail[:10],
            "detected_niche": niche,
            "total_count": len(trending[:10]) + len(niche_tags[:10]) + len(long_tail[:10])
        }
    
    async def _detect_niche(self, content: str) -> str:
        """
        Detect content niche using AI.
        
        Returns niche category
        """
        prompt = f"""Analyze this Instagram content and identify the primary niche/category.

Content: {content[:500]}

Common niches:
- fitness, fashion, beauty, food, travel, lifestyle, tech, gaming, 
- business, motivation, education, art, photography, music, comedy,
- sports, health, wellness, parenting, pets, home, diy

Return ONLY the niche name, nothing else."""

        try:
            if not self.client:
                return "lifestyle"
            
            # Use AIClient unified interface (sync call)
            response = self.client.chat_completion(
                messages=[
                    {"role": "system", "content": "You are a social media niche detection expert."},
                    {"role": "user", "content": prompt}
                ],
                temperature=0.3,
                max_tokens=20
            )
            
            niche = response.strip().lower()
            return niche
            
        except Exception as e:
            logger.error(f"Niche detection failed: {e}")
            return "lifestyle"
    
    def _get_trending_hashtags(self, niche: str, limit: int = 10) -> List[Dict]:
        """
        Get trending hashtags from database.
        
        Returns hashtags with high velocity and trending scores
        """
        with self.engine.connect() as conn:
            result = conn.execute(text("""
                SELECT 
                    tag, media_count, velocity_7d, trending_score
                FROM ig_hashtags
                WHERE trending_score > 0
                  AND (category = :niche OR category IS NULL)
                ORDER BY trending_score DESC
                LIMIT :limit
            """), {"niche": niche, "limit": limit})
            
            hashtags = []
            for row in result.fetchall():
                hashtags.append({
                    "tag": row[0],
                    "media_count": row[1],
                    "velocity": round(float(row[2]), 2) if row[2] else 0,
                    "trending_score": round(float(row[3]), 1) if row[3] else 0,
                    "competition": "high",
                    "type": "trending"
                })
            
            return hashtags
    
    def _get_niche_hashtags(self, niche: str, limit: int = 10) -> List[Dict]:
        """
        Get niche-specific hashtags with medium competition.
        """
        with self.engine.connect() as conn:
            result = conn.execute(text("""
                SELECT 
                    tag, media_count, velocity_7d, trending_score
                FROM ig_hashtags
                WHERE category = :niche
                  AND media_count BETWEEN 10000 AND 500000
                ORDER BY trending_score DESC NULLS LAST
                LIMIT :limit
            """), {"niche": niche, "limit": limit})
            
            hashtags = []
            for row in result.fetchall():
                hashtags.append({
                    "tag": row[0],
                    "media_count": row[1],
                    "velocity": round(float(row[2]), 2) if row[2] else 0,
                    "trending_score": round(float(row[3]), 1) if row[3] else 0,
                    "competition": "medium",
                    "type": "niche"
                })
            
            return hashtags
    
    async def _generate_long_tail_hashtags(
        self,
        content: str,
        niche: str,
        limit: int = 10
    ) -> List[Dict]:
        """
        Generate long-tail hashtags using AI.
        
        These are specific, low-competition hashtags.
        """
        prompt = f"""Generate {limit} specific, long-tail hashtags for this Instagram content.

Content: {content[:300]}
Niche: {niche}

Requirements:
- Specific and descriptive (3-5 words combined)
- Low competition (niche-specific)
- Relevant to the content
- No generic hashtags

Return ONLY a comma-separated list of hashtags without # symbols.
Example: morningworkoutroutine, homefitnesstips, quickhealthybreakfast"""

        try:
            if not self.client:
                return []
            
            # Use AIClient unified interface (sync call)
            hashtags_text = self.client.chat_completion(
                messages=[
                    {"role": "system", "content": "You are a hashtag strategy expert."},
                    {"role": "user", "content": prompt}
                ],
                temperature=0.7,
                max_tokens=200
            ).strip()
            tags = [tag.strip().lstrip('#') for tag in hashtags_text.split(',')]
            
            # Format as list of dicts
            hashtags = []
            for tag in tags[:limit]:
                if tag:
                    hashtags.append({
                        "tag": tag,
                        "media_count": 0,  # Unknown for AI-generated
                        "velocity": 0,
                        "trending_score": 0,
                        "competition": "low",
                        "type": "long-tail"
                    })
            
            return hashtags
            
        except Exception as e:
            logger.error(f"Long-tail generation failed: {e}")
            return []
    
    def analyze_hashtag_performance(self, hashtag: str) -> Dict:
        """
        Analyze performance metrics for a specific hashtag.
        
        Returns competition score and recommendations
        """
        with self.engine.connect() as conn:
            result = conn.execute(text("""
                SELECT 
                    tag, media_count, velocity_7d, trending_score, category
                FROM ig_hashtags
                WHERE tag = :hashtag
            """), {"hashtag": hashtag.lstrip('#')}).fetchone()
            
            if not result:
                return {
                    "tag": hashtag,
                    "found": False,
                    "competition": "unknown",
                    "recommendation": "No data available"
                }
            
            tag, media_count, velocity, trending_score, category = result
            
            # Determine competition level
            if media_count > 1000000:
                competition = "very_high"
                recommendation = "High competition - hard to rank"
            elif media_count > 500000:
                competition = "high"
                recommendation = "High competition - use with niche tags"
            elif media_count > 100000:
                competition = "medium"
                recommendation = "Good balance - recommended"
            elif media_count > 10000:
                competition = "low"
                recommendation = "Low competition - easier to rank"
            else:
                competition = "very_low"
                recommendation = "Very low competition - niche audience"
            
            return {
                "tag": tag,
                "found": True,
                "media_count": media_count,
                "velocity": round(float(velocity), 2) if velocity else 0,
                "trending_score": round(float(trending_score), 1) if trending_score else 0,
                "competition": competition,
                "category": category,
                "recommendation": recommendation
            }
    
    def get_hashtag_suggestions_by_category(
        self,
        category: str,
        limit: int = 30
    ) -> List[Dict]:
        """
        Get hashtag suggestions for a specific category.
        
        Returns mix of trending, medium, and low competition tags
        """
        with self.engine.connect() as conn:
            # Get trending (high competition)
            trending = conn.execute(text("""
                SELECT tag, media_count, velocity_7d, trending_score
                FROM ig_hashtags
                WHERE category = :category
                  AND media_count > 500000
                ORDER BY trending_score DESC NULLS LAST
                LIMIT 10
            """), {"category": category}).fetchall()
            
            # Get medium competition
            medium = conn.execute(text("""
                SELECT tag, media_count, velocity_7d, trending_score
                FROM ig_hashtags
                WHERE category = :category
                  AND media_count BETWEEN 100000 AND 500000
                ORDER BY trending_score DESC NULLS LAST
                LIMIT 10
            """), {"category": category}).fetchall()
            
            # Get low competition
            low = conn.execute(text("""
                SELECT tag, media_count, velocity_7d, trending_score
                FROM ig_hashtags
                WHERE category = :category
                  AND media_count < 100000
                ORDER BY trending_score DESC NULLS LAST
                LIMIT 10
            """), {"category": category}).fetchall()
            
            # Combine and format
            all_tags = []
            
            for row in trending:
                all_tags.append({
                    "tag": row[0],
                    "media_count": row[1],
                    "velocity": round(float(row[2]), 2) if row[2] else 0,
                    "trending_score": round(float(row[3]), 1) if row[3] else 0,
                    "competition": "high"
                })
            
            for row in medium:
                all_tags.append({
                    "tag": row[0],
                    "media_count": row[1],
                    "velocity": round(float(row[2]), 2) if row[2] else 0,
                    "trending_score": round(float(row[3]), 1) if row[3] else 0,
                    "competition": "medium"
                })
            
            for row in low:
                all_tags.append({
                    "tag": row[0],
                    "media_count": row[1],
                    "velocity": round(float(row[2]), 2) if row[2] else 0,
                    "trending_score": round(float(row[3]), 1) if row[3] else 0,
                    "competition": "low"
                })
            
            return all_tags[:limit]


# Singleton instance
_generator_instance = None

def get_hashtag_generator() -> HashtagGenerator:
    """Get or create hashtag generator singleton"""
    global _generator_instance
    if _generator_instance is None:
        _generator_instance = HashtagGenerator()
    return _generator_instance
