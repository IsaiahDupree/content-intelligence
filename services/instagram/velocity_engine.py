"""
Velocity Calculation Engine
Calculates growth rates and trending scores for audio, hashtags, and formats
"""
import os
from typing import Dict, List, Optional, Tuple
from datetime import date, timedelta
from loguru import logger
from sqlalchemy import create_engine, text

DATABASE_URL = os.getenv("DATABASE_URL", "postgresql://postgres:postgres@localhost:54322/postgres")


class VelocityEngine:
    """
    Calculates velocity (growth rate) and trending scores for content entities.
    
    Velocity = (current_usage - previous_usage) / previous_usage
    Trending Score = velocity × engagement_weight × recency_weight
    """
    
    def __init__(self):
        self.engine = create_engine(DATABASE_URL)
        logger.info("Velocity engine initialized")
    
    def calculate_all_velocities(self, lookback_days: int = 7):
        """
        Calculate velocity for all entities (audio, hashtags, formats).
        
        Args:
            lookback_days: Number of days to look back for comparison
        """
        logger.info(f"Calculating velocities with {lookback_days}-day lookback")
        
        # Calculate for each entity type
        audio_updated = self._calculate_entity_velocity("audio", lookback_days)
        hashtag_updated = self._calculate_entity_velocity("hashtag", lookback_days)
        format_updated = self._calculate_entity_velocity("format", lookback_days)
        
        logger.info(f"Velocity calculation complete: {audio_updated} audio, {hashtag_updated} hashtags, {format_updated} formats")
        
        return {
            "audio_updated": audio_updated,
            "hashtag_updated": hashtag_updated,
            "format_updated": format_updated
        }
    
    def _calculate_entity_velocity(self, entity_type: str, lookback_days: int) -> int:
        """
        Calculate velocity for a specific entity type.
        
        Returns number of entities updated
        """
        today = date.today()
        comparison_date = today - timedelta(days=lookback_days)
        
        with self.engine.connect() as conn:
            # Get all entities with observations
            result = conn.execute(text("""
                SELECT DISTINCT entity_id
                FROM trend_observations
                WHERE entity_type = :entity_type
            """), {"entity_type": entity_type})
            
            entity_ids = [row[0] for row in result.fetchall()]
            
            updated_count = 0
            for entity_id in entity_ids:
                velocity = self._calculate_single_velocity(
                    entity_type,
                    entity_id,
                    today,
                    comparison_date
                )
                
                if velocity is not None:
                    self._save_velocity(entity_type, entity_id, velocity, lookback_days)
                    updated_count += 1
            
            return updated_count
    
    def _calculate_single_velocity(
        self,
        entity_type: str,
        entity_id: str,
        current_date: date,
        comparison_date: date
    ) -> Optional[float]:
        """
        Calculate velocity for a single entity.
        
        Returns velocity as a float, or None if insufficient data
        """
        with self.engine.connect() as conn:
            # Get current usage (sum of recent days)
            current_result = conn.execute(text("""
                SELECT COALESCE(SUM(usage_count), 0) as total
                FROM trend_observations
                WHERE entity_type = :entity_type
                  AND entity_id = :entity_id
                  AND observation_date >= :current_date - INTERVAL '3 days'
                  AND observation_date <= :current_date
            """), {
                "entity_type": entity_type,
                "entity_id": entity_id,
                "current_date": current_date
            }).fetchone()
            
            current_usage = current_result[0] if current_result else 0
            
            # Get previous usage (same window, shifted back)
            previous_result = conn.execute(text("""
                SELECT COALESCE(SUM(usage_count), 0) as total
                FROM trend_observations
                WHERE entity_type = :entity_type
                  AND entity_id = :entity_id
                  AND observation_date >= :comparison_date - INTERVAL '3 days'
                  AND observation_date <= :comparison_date
            """), {
                "entity_type": entity_type,
                "entity_id": entity_id,
                "comparison_date": comparison_date
            }).fetchone()
            
            previous_usage = previous_result[0] if previous_result else 0
            
            # Calculate velocity
            if previous_usage == 0:
                if current_usage > 0:
                    return 1.0  # 100% growth from zero
                else:
                    return None  # No data
            
            velocity = (current_usage - previous_usage) / previous_usage
            return velocity
    
    def _save_velocity(self, entity_type: str, entity_id: str, velocity: float, days: int):
        """Save calculated velocity to appropriate table"""
        with self.engine.connect() as conn:
            if entity_type == "audio":
                conn.execute(text("""
                    UPDATE ig_audio
                    SET velocity_7d = :velocity,
                        last_updated_at = NOW()
                    WHERE audio_id = :entity_id
                """), {"velocity": velocity, "entity_id": entity_id})
            
            elif entity_type == "hashtag":
                conn.execute(text("""
                    UPDATE ig_hashtags
                    SET velocity_7d = :velocity,
                        last_updated_at = NOW()
                    WHERE tag = :entity_id
                """), {"velocity": velocity, "entity_id": entity_id})
            
            elif entity_type == "format":
                conn.execute(text("""
                    UPDATE trend_cards
                    SET velocity_7d = :velocity
                    WHERE name = :entity_id
                """), {"velocity": velocity, "entity_id": entity_id})
            
            conn.commit()
    
    def calculate_trending_scores(self):
        """
        Calculate trending scores for all entities.
        
        Trending Score = velocity × usage_weight × recency_weight
        """
        logger.info("Calculating trending scores")
        
        audio_updated = self._calculate_audio_trending_scores()
        hashtag_updated = self._calculate_hashtag_trending_scores()
        format_updated = self._calculate_format_trending_scores()
        
        logger.info(f"Trending scores updated: {audio_updated} audio, {hashtag_updated} hashtags, {format_updated} formats")
        
        return {
            "audio_updated": audio_updated,
            "hashtag_updated": hashtag_updated,
            "format_updated": format_updated
        }
    
    def _calculate_audio_trending_scores(self) -> int:
        """Calculate trending scores for audio tracks"""
        with self.engine.connect() as conn:
            # Get all audio with velocity data
            result = conn.execute(text("""
                SELECT audio_id, velocity_7d, usage_count, 
                       EXTRACT(EPOCH FROM (NOW() - last_updated_at)) / 3600 as hours_since_update
                FROM ig_audio
                WHERE velocity_7d IS NOT NULL
            """))
            
            updated_count = 0
            for row in result.fetchall():
                audio_id, velocity, usage_count, hours_old = row
                
                # Calculate trending score
                # Higher velocity = more trending
                # Higher usage = more popular
                # More recent = more relevant
                velocity_weight = max(0, velocity) * 10  # Amplify positive velocity
                usage_weight = min(usage_count / 100, 10)  # Cap at 10x
                recency_weight = max(0, 1 - (hours_old / 168))  # Decay over 1 week
                
                trending_score = velocity_weight * usage_weight * recency_weight
                
                # Save score
                conn.execute(text("""
                    UPDATE ig_audio
                    SET trending_score = :score
                    WHERE audio_id = :audio_id
                """), {"score": trending_score, "audio_id": audio_id})
                
                updated_count += 1
            
            conn.commit()
            return updated_count
    
    def _calculate_hashtag_trending_scores(self) -> int:
        """Calculate trending scores for hashtags"""
        with self.engine.connect() as conn:
            # Get all hashtags with velocity data
            result = conn.execute(text("""
                SELECT tag, velocity_7d, media_count,
                       EXTRACT(EPOCH FROM (NOW() - last_updated_at)) / 3600 as hours_since_update
                FROM ig_hashtags
                WHERE velocity_7d IS NOT NULL
            """))
            
            updated_count = 0
            for row in result.fetchall():
                tag, velocity, media_count, hours_old = row
                
                # Calculate trending score
                velocity_weight = max(0, velocity) * 10
                usage_weight = min(media_count / 1000, 10)
                recency_weight = max(0, 1 - (hours_old / 168))
                
                trending_score = velocity_weight * usage_weight * recency_weight
                
                # Save score
                conn.execute(text("""
                    UPDATE ig_hashtags
                    SET trending_score = :score
                    WHERE tag = :tag
                """), {"score": trending_score, "tag": tag})
                
                updated_count += 1
            
            conn.commit()
            return updated_count
    
    def _calculate_format_trending_scores(self) -> int:
        """Calculate trending scores for content formats"""
        with self.engine.connect() as conn:
            # Get all formats with velocity data
            result = conn.execute(text("""
                SELECT name, velocity_7d,
                       EXTRACT(EPOCH FROM (NOW() - created_at)) / 3600 as hours_since_created
                FROM trend_cards
                WHERE velocity_7d IS NOT NULL
            """))
            
            updated_count = 0
            for row in result.fetchall():
                name, velocity, hours_old = row
                
                # Calculate trending score
                velocity_weight = max(0, velocity) * 10
                recency_weight = max(0, 1 - (hours_old / 336))  # Decay over 2 weeks
                
                trending_score = velocity_weight * recency_weight
                
                # Save score
                conn.execute(text("""
                    UPDATE trend_cards
                    SET trending_score = :score
                    WHERE name = :name
                """), {"score": trending_score, "name": name})
                
                updated_count += 1
            
            conn.commit()
            return updated_count
    
    def get_trending_audio(self, limit: int = 50, region: Optional[str] = None) -> List[Dict]:
        """
        Get top trending audio tracks.
        
        Args:
            limit: Max number of results
            region: Optional region filter
            
        Returns:
            List of audio tracks with trending data
        """
        with self.engine.connect() as conn:
            result = conn.execute(text("""
                SELECT 
                    audio_id, title, artist, usage_count,
                    velocity_7d, trending_score
                FROM ig_audio
                WHERE trending_score IS NOT NULL
                ORDER BY trending_score DESC
                LIMIT :limit
            """), {"limit": limit})
            
            return [
                {
                    "audio_id": row[0],
                    "title": row[1],
                    "artist": row[2],
                    "usage_count": row[3],
                    "velocity_7d": float(row[4]) if row[4] else 0,
                    "trending_score": float(row[5]) if row[5] else 0
                }
                for row in result.fetchall()
            ]
    
    def get_trending_hashtags(self, limit: int = 50, region: Optional[str] = None) -> List[Dict]:
        """
        Get top trending hashtags.
        
        Args:
            limit: Max number of results
            region: Optional region filter
            
        Returns:
            List of hashtags with trending data
        """
        with self.engine.connect() as conn:
            result = conn.execute(text("""
                SELECT 
                    tag, media_count, velocity_7d, trending_score, category
                FROM ig_hashtags
                WHERE trending_score IS NOT NULL
                ORDER BY trending_score DESC
                LIMIT :limit
            """), {"limit": limit})
            
            return [
                {
                    "tag": row[0],
                    "media_count": row[1],
                    "velocity_7d": float(row[2]) if row[2] else 0,
                    "trending_score": float(row[3]) if row[3] else 0,
                    "category": row[4]
                }
                for row in result.fetchall()
            ]
    
    def get_trending_formats(self, limit: int = 50, region: Optional[str] = None) -> List[Dict]:
        """
        Get top trending content formats.
        
        Args:
            limit: Max number of results
            region: Optional region filter
            
        Returns:
            List of formats with trending data
        """
        with self.engine.connect() as conn:
            result = conn.execute(text("""
                SELECT 
                    name, description, format_type, velocity_7d, trending_score
                FROM trend_cards
                WHERE trending_score IS NOT NULL
                ORDER BY trending_score DESC
                LIMIT :limit
            """), {"limit": limit})
            
            return [
                {
                    "name": row[0],
                    "description": row[1],
                    "format_type": row[2],
                    "velocity_7d": float(row[3]) if row[3] else 0,
                    "trending_score": float(row[4]) if row[4] else 0
                }
                for row in result.fetchall()
            ]


# Singleton instance
_engine_instance = None

def get_velocity_engine() -> VelocityEngine:
    """Get or create velocity engine singleton"""
    global _engine_instance
    if _engine_instance is None:
        _engine_instance = VelocityEngine()
    return _engine_instance
