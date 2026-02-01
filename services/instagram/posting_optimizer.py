"""
Posting Optimizer Service
Calculates best times to post based on historical engagement and audience activity
"""
import os
from typing import Dict, List, Optional, Tuple
from datetime import datetime, time, timedelta
from collections import defaultdict
from loguru import logger
from sqlalchemy import create_engine, text

DATABASE_URL = os.getenv("DATABASE_URL", "postgresql://postgres:postgres@localhost:54322/postgres")


class PostingOptimizer:
    """
    Analyzes historical post performance to determine optimal posting times.
    
    Features:
    - Hour-of-day engagement analysis
    - Day-of-week performance patterns
    - Content-type specific recommendations
    - Timezone-aware scheduling
    """
    
    def __init__(self):
        self.engine = create_engine(DATABASE_URL)
        logger.info("Posting Optimizer initialized")
    
    def get_best_times(
        self,
        profile_id: Optional[str] = None,
        content_type: str = "REEL",
        timezone: str = "UTC",
        top_n: int = 5
    ) -> List[Dict]:
        """
        Get best times to post based on historical data.
        
        Args:
            profile_id: Instagram profile ID (optional, uses all data if None)
            content_type: REEL, IMAGE, or CAROUSEL
            timezone: Timezone for results
            top_n: Number of time slots to return
            
        Returns:
            List of optimal posting times with engagement scores
        """
        logger.info(f"Calculating best times for {content_type}")
        
        # Get historical engagement by hour
        hourly_engagement = self._get_hourly_engagement(profile_id, content_type)
        
        # Get day-of-week patterns
        daily_patterns = self._get_daily_patterns(profile_id, content_type)
        
        # Combine and rank time slots
        optimal_times = self._rank_time_slots(hourly_engagement, daily_patterns, top_n)
        
        logger.info(f"Found {len(optimal_times)} optimal posting times")
        return optimal_times
    
    def _get_hourly_engagement(
        self,
        profile_id: Optional[str],
        content_type: str
    ) -> Dict[int, float]:
        """
        Calculate average engagement rate by hour of day.
        
        Returns dict of {hour: avg_engagement_rate}
        """
        with self.engine.connect() as conn:
            query = """
                SELECT 
                    EXTRACT(HOUR FROM timestamp) as hour,
                    AVG((like_count + comment_count * 2.0) / NULLIF(play_count, 0)) as avg_engagement
                FROM ig_media
                WHERE media_type = :content_type
                  AND timestamp IS NOT NULL
                  AND play_count > 0
            """
            
            params = {"content_type": content_type}
            
            if profile_id:
                query += " AND profile_id = :profile_id"
                params["profile_id"] = profile_id
            
            query += """
                GROUP BY EXTRACT(HOUR FROM timestamp)
                ORDER BY hour
            """
            
            result = conn.execute(text(query), params)
            
            hourly_data = {}
            for row in result.fetchall():
                hour = int(row[0])
                engagement = float(row[1]) if row[1] else 0
                hourly_data[hour] = engagement
            
            return hourly_data
    
    def _get_daily_patterns(
        self,
        profile_id: Optional[str],
        content_type: str
    ) -> Dict[int, float]:
        """
        Calculate average engagement by day of week.
        
        Returns dict of {day: avg_engagement_rate}
        0 = Monday, 6 = Sunday
        """
        with self.engine.connect() as conn:
            query = """
                SELECT 
                    EXTRACT(DOW FROM timestamp) as day_of_week,
                    AVG((like_count + comment_count * 2.0) / NULLIF(play_count, 0)) as avg_engagement
                FROM ig_media
                WHERE media_type = :content_type
                  AND timestamp IS NOT NULL
                  AND play_count > 0
            """
            
            params = {"content_type": content_type}
            
            if profile_id:
                query += " AND profile_id = :profile_id"
                params["profile_id"] = profile_id
            
            query += """
                GROUP BY EXTRACT(DOW FROM timestamp)
                ORDER BY day_of_week
            """
            
            result = conn.execute(text(query), params)
            
            daily_data = {}
            for row in result.fetchall():
                day = int(row[0])
                engagement = float(row[1]) if row[1] else 0
                daily_data[day] = engagement
            
            return daily_data
    
    def _rank_time_slots(
        self,
        hourly_engagement: Dict[int, float],
        daily_patterns: Dict[int, float],
        top_n: int
    ) -> List[Dict]:
        """
        Combine hourly and daily patterns to rank optimal time slots.
        """
        if not hourly_engagement:
            # Return default times if no data
            return self._get_default_times(top_n)
        
        # Calculate scores for each hour
        hour_scores = []
        for hour, engagement in hourly_engagement.items():
            # Normalize engagement (0-1 scale)
            max_engagement = max(hourly_engagement.values()) if hourly_engagement.values() else 1
            normalized_score = engagement / max_engagement if max_engagement > 0 else 0
            
            hour_scores.append({
                "hour": hour,
                "score": normalized_score,
                "engagement_rate": engagement
            })
        
        # Sort by score
        hour_scores.sort(key=lambda x: x["score"], reverse=True)
        
        # Take top N
        optimal_times = []
        for slot in hour_scores[:top_n]:
            # Find best days for this hour
            best_days = self._get_best_days_for_hour(slot["hour"], daily_patterns)
            
            optimal_times.append({
                "hour": slot["hour"],
                "time_display": f"{slot['hour']:02d}:00",
                "score": round(slot["score"] * 100, 1),
                "engagement_rate": round(slot["engagement_rate"], 4),
                "best_days": best_days,
                "recommendation": self._get_time_recommendation(slot["hour"])
            })
        
        return optimal_times
    
    def _get_best_days_for_hour(
        self,
        hour: int,
        daily_patterns: Dict[int, float]
    ) -> List[str]:
        """Get best days of week for a specific hour"""
        if not daily_patterns:
            return ["Monday", "Wednesday", "Friday"]
        
        # Sort days by engagement
        sorted_days = sorted(
            daily_patterns.items(),
            key=lambda x: x[1],
            reverse=True
        )
        
        # Map day numbers to names
        day_names = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]
        
        # Return top 3 days
        best_days = [day_names[day] for day, _ in sorted_days[:3]]
        return best_days
    
    def _get_time_recommendation(self, hour: int) -> str:
        """Get human-readable recommendation for a time slot"""
        if 6 <= hour < 9:
            return "Morning commute - high engagement"
        elif 12 <= hour < 14:
            return "Lunch break - peak browsing time"
        elif 17 <= hour < 20:
            return "Evening commute - prime time"
        elif 20 <= hour < 23:
            return "Night browsing - relaxation time"
        else:
            return "Off-peak hours - lower competition"
    
    def _get_default_times(self, top_n: int) -> List[Dict]:
        """Return default optimal times when no data available"""
        default_times = [
            {"hour": 18, "time_display": "18:00", "score": 95.0, "engagement_rate": 0.08, 
             "best_days": ["Monday", "Wednesday", "Friday"], "recommendation": "Evening commute - prime time"},
            {"hour": 12, "time_display": "12:00", "score": 90.0, "engagement_rate": 0.075,
             "best_days": ["Tuesday", "Thursday", "Friday"], "recommendation": "Lunch break - peak browsing time"},
            {"hour": 21, "time_display": "21:00", "score": 85.0, "engagement_rate": 0.07,
             "best_days": ["Monday", "Tuesday", "Wednesday"], "recommendation": "Night browsing - relaxation time"},
            {"hour": 8, "time_display": "08:00", "score": 80.0, "engagement_rate": 0.065,
             "best_days": ["Monday", "Wednesday", "Friday"], "recommendation": "Morning commute - high engagement"},
            {"hour": 15, "time_display": "15:00", "score": 75.0, "engagement_rate": 0.06,
             "best_days": ["Tuesday", "Thursday", "Saturday"], "recommendation": "Afternoon break - steady engagement"}
        ]
        
        return default_times[:top_n]
    
    def get_performance_by_hour(
        self,
        profile_id: Optional[str] = None,
        content_type: str = "REEL"
    ) -> List[Dict]:
        """
        Get detailed performance metrics for each hour of the day.
        
        Returns 24-hour breakdown with engagement metrics.
        """
        hourly_engagement = self._get_hourly_engagement(profile_id, content_type)
        
        # Fill in missing hours with 0
        performance = []
        for hour in range(24):
            engagement = hourly_engagement.get(hour, 0)
            
            performance.append({
                "hour": hour,
                "time_display": f"{hour:02d}:00",
                "engagement_rate": round(engagement, 4),
                "relative_score": 0  # Will calculate after
            })
        
        # Calculate relative scores (0-100)
        max_engagement = max([p["engagement_rate"] for p in performance]) if performance else 1
        if max_engagement > 0:
            for p in performance:
                p["relative_score"] = round((p["engagement_rate"] / max_engagement) * 100, 1)
        
        return performance
    
    def get_performance_by_day(
        self,
        profile_id: Optional[str] = None,
        content_type: str = "REEL"
    ) -> List[Dict]:
        """
        Get detailed performance metrics for each day of the week.
        """
        daily_patterns = self._get_daily_patterns(profile_id, content_type)
        
        day_names = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]
        
        performance = []
        for day_num, day_name in enumerate(day_names):
            engagement = daily_patterns.get(day_num, 0)
            
            performance.append({
                "day": day_name,
                "day_number": day_num,
                "engagement_rate": round(engagement, 4),
                "relative_score": 0
            })
        
        # Calculate relative scores
        max_engagement = max([p["engagement_rate"] for p in performance]) if performance else 1
        if max_engagement > 0:
            for p in performance:
                p["relative_score"] = round((p["engagement_rate"] / max_engagement) * 100, 1)
        
        return performance
    
    def suggest_posting_schedule(
        self,
        posts_per_week: int = 7,
        profile_id: Optional[str] = None,
        content_type: str = "REEL"
    ) -> List[Dict]:
        """
        Generate a complete posting schedule for the week.
        
        Args:
            posts_per_week: Number of posts to schedule
            profile_id: Instagram profile ID
            content_type: Type of content
            
        Returns:
            List of scheduled time slots
        """
        # Get best times
        best_times = self.get_best_times(profile_id, content_type, top_n=posts_per_week)
        
        # Get day patterns
        daily_patterns = self._get_daily_patterns(profile_id, content_type)
        
        # Create schedule
        schedule = []
        for i, time_slot in enumerate(best_times):
            # Distribute across best days
            best_days = time_slot["best_days"]
            day = best_days[i % len(best_days)]
            
            schedule.append({
                "day": day,
                "hour": time_slot["hour"],
                "time_display": time_slot["time_display"],
                "score": time_slot["score"],
                "recommendation": time_slot["recommendation"]
            })
        
        # Sort by day of week
        day_order = {"Monday": 0, "Tuesday": 1, "Wednesday": 2, "Thursday": 3, 
                     "Friday": 4, "Saturday": 5, "Sunday": 6}
        schedule.sort(key=lambda x: (day_order[x["day"]], x["hour"]))
        
        return schedule


# Singleton instance
_optimizer_instance = None

def get_posting_optimizer() -> PostingOptimizer:
    """Get or create posting optimizer singleton"""
    global _optimizer_instance
    if _optimizer_instance is None:
        _optimizer_instance = PostingOptimizer()
    return _optimizer_instance
