"""
Trend Scoring Service - Velocity + Saturation Calculations
"""
from datetime import datetime, timedelta
from typing import List, Dict, Any, Optional
from dataclasses import dataclass
from statistics import median
from loguru import logger


@dataclass
class TrendScore:
    entity_id: str
    entity_type: str
    name: str
    velocity_score: float  # 0-100, how fast it's growing
    saturation_score: float  # 0-100, how crowded it is
    efficiency_score: float  # 0-100, engagement quality
    trend_score: float  # Composite score
    adoption_24h: int
    adoption_delta: str  # e.g., "+42%"
    status: str  # 'rising' | 'hot' | 'saturated' | 'declining'


class TrendScoringService:
    """
    Calculates trend scores based on:
    - Velocity: How fast adoption is growing
    - Saturation: How crowded/competitive it is
    - Efficiency: Engagement quality (likes/plays ratio)
    
    Usage:
        scorer = TrendScoringService()
        scores = scorer.score_hashtags(media_list)
    """
    
    # Thresholds
    SATURATION_THRESHOLD = 10000  # Posts before considered saturated
    HIGH_VELOCITY_THRESHOLD = 50  # Score above this = "hot"
    
    def score_hashtags(self, media_list: List[Dict]) -> List[TrendScore]:
        """Score hashtags based on media using them."""
        hashtag_data: Dict[str, Dict] = {}
        
        for media in media_list:
            hashtags = media.get("hashtags", [])
            posted_at = media.get("posted_at", datetime.utcnow())
            plays = media.get("play_count", 0)
            likes = media.get("like_count", 0)
            
            for tag in hashtags:
                if tag not in hashtag_data:
                    hashtag_data[tag] = {
                        "posts": [],
                        "creators": set(),
                        "plays": [],
                        "likes": []
                    }
                
                hashtag_data[tag]["posts"].append(posted_at)
                hashtag_data[tag]["creators"].add(media.get("author_id", ""))
                hashtag_data[tag]["plays"].append(plays)
                hashtag_data[tag]["likes"].append(likes)
        
        scores = []
        for tag, data in hashtag_data.items():
            score = self._calculate_score(
                entity_id=tag,
                entity_type="hashtag",
                name=f"#{tag}",
                posts=data["posts"],
                unique_creators=len(data["creators"]),
                plays=data["plays"],
                likes=data["likes"]
            )
            scores.append(score)
        
        return sorted(scores, key=lambda x: x.trend_score, reverse=True)
    
    def score_sounds(self, media_list: List[Dict]) -> List[TrendScore]:
        """Score sounds/audio based on media using them."""
        sound_data: Dict[str, Dict] = {}
        
        for media in media_list:
            music_id = media.get("music_id")
            if not music_id:
                continue
            
            if music_id not in sound_data:
                sound_data[music_id] = {
                    "title": media.get("music_title", "Unknown"),
                    "posts": [],
                    "creators": set(),
                    "plays": [],
                    "likes": []
                }
            
            sound_data[music_id]["posts"].append(media.get("posted_at", datetime.utcnow()))
            sound_data[music_id]["creators"].add(media.get("author_id", ""))
            sound_data[music_id]["plays"].append(media.get("play_count", 0))
            sound_data[music_id]["likes"].append(media.get("like_count", 0))
        
        scores = []
        for sound_id, data in sound_data.items():
            score = self._calculate_score(
                entity_id=sound_id,
                entity_type="sound",
                name=data["title"],
                posts=data["posts"],
                unique_creators=len(data["creators"]),
                plays=data["plays"],
                likes=data["likes"]
            )
            scores.append(score)
        
        return sorted(scores, key=lambda x: x.trend_score, reverse=True)
    
    def _calculate_score(
        self,
        entity_id: str,
        entity_type: str,
        name: str,
        posts: List[datetime],
        unique_creators: int,
        plays: List[int],
        likes: List[int]
    ) -> TrendScore:
        """Calculate velocity, saturation, and composite scores."""
        now = datetime.utcnow()
        
        # Count posts in different time windows
        posts_24h = sum(1 for p in posts if (now - p).total_seconds() < 86400)
        posts_7d = sum(1 for p in posts if (now - p).total_seconds() < 604800)
        total_posts = len(posts)
        
        # VELOCITY: How fast is adoption growing?
        # Higher when recent posts are increasing
        if posts_7d > 0:
            velocity_ratio = (posts_24h * 7) / max(posts_7d, 1)  # Normalized daily rate
            velocity_score = min(100, velocity_ratio * 30)  # Scale to 0-100
        else:
            velocity_score = 0
        
        # Boost for unique creators (diversity signal)
        creator_boost = min(20, unique_creators * 2)
        velocity_score = min(100, velocity_score + creator_boost)
        
        # SATURATION: How crowded is it?
        saturation_score = min(100, (total_posts / self.SATURATION_THRESHOLD) * 100)
        
        # EFFICIENCY: Engagement quality
        if plays and sum(plays) > 0:
            median_plays = median(plays) if plays else 0
            median_likes = median(likes) if likes else 0
            engagement_rate = median_likes / max(median_plays, 1)
            efficiency_score = min(100, engagement_rate * 1000)  # Scale up
        else:
            efficiency_score = 50  # Neutral
        
        # COMPOSITE TREND SCORE
        # Higher velocity + lower saturation + higher efficiency = better
        trend_score = (
            velocity_score * 0.5 +
            (100 - saturation_score) * 0.3 +
            efficiency_score * 0.2
        )
        
        # Calculate adoption delta
        if posts_7d > posts_24h * 7:
            delta_pct = -((posts_7d - posts_24h * 7) / max(posts_7d, 1)) * 100
        else:
            delta_pct = ((posts_24h * 7 - posts_7d) / max(posts_7d, 1)) * 100
        adoption_delta = f"+{int(delta_pct)}%" if delta_pct >= 0 else f"{int(delta_pct)}%"
        
        # Determine status
        if velocity_score > 70 and saturation_score < 30:
            status = "rising"
        elif velocity_score > 50:
            status = "hot"
        elif saturation_score > 70:
            status = "saturated"
        else:
            status = "declining"
        
        return TrendScore(
            entity_id=entity_id,
            entity_type=entity_type,
            name=name,
            velocity_score=round(velocity_score, 1),
            saturation_score=round(saturation_score, 1),
            efficiency_score=round(efficiency_score, 1),
            trend_score=round(trend_score, 1),
            adoption_24h=posts_24h,
            adoption_delta=adoption_delta,
            status=status
        )


def calculate_velocity(adoption_24h: int, adoption_7d_avg: float) -> float:
    """Simple velocity calculation."""
    if adoption_7d_avg <= 0:
        return 100 if adoption_24h > 0 else 0
    return min(100, (adoption_24h / adoption_7d_avg) * 50)


def calculate_saturation(total_posts: int, threshold: int = 10000) -> float:
    """Simple saturation calculation."""
    return min(100, (total_posts / threshold) * 100)
