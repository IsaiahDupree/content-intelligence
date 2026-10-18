"""
Best Time To Post Service
=========================
Analyzes posting history to recommend optimal posting times.

Features:
- Traffic curve by hour/day
- Peak time detection
- Countdown to next peak
- "Don't post yet" warnings
- Push notification scheduling

Algorithm:
score(hour) = 0.6 * normalized_follower_activity + 0.4 * normalized_historical_performance
"""
import os
import json
import statistics
from datetime import datetime, timedelta, timezone
from typing import List, Dict, Any, Optional, Tuple
from dataclasses import dataclass, field, asdict
from collections import defaultdict
from enum import Enum

from sqlalchemy import create_engine, text
from loguru import logger

DATABASE_URL = os.getenv("DATABASE_URL", "postgresql://postgres:postgres@localhost:54322/postgres")


class DayOfWeek(int, Enum):
    SUNDAY = 0
    MONDAY = 1
    TUESDAY = 2
    WEDNESDAY = 3
    THURSDAY = 4
    FRIDAY = 5
    SATURDAY = 6


@dataclass
class HourlyScore:
    """Score for a specific hour"""
    hour: int                    # 0-23
    score: float                 # 0-1 normalized
    post_count: int              # Historical posts at this hour
    avg_engagement_rate: float   # Average ER at this hour
    avg_views: int               # Average views at this hour
    label: str                   # "12 AM", "1 PM", etc.


@dataclass
class DailyPattern:
    """Pattern for a specific day of week"""
    day: int                     # 0-6 (Sunday-Saturday)
    day_name: str
    hourly_scores: List[HourlyScore]
    peak_hour: int
    peak_score: float
    total_posts: int


@dataclass
class BestTimeResult:
    """Complete best time analysis result"""
    # Current state
    current_hour: int
    current_day: int
    current_score: float
    
    # Recommendations
    peak_hour_today: int
    peak_score_today: float
    should_wait: bool
    wait_reason: str
    countdown_minutes: int
    next_peak_time: str          # ISO timestamp
    
    # Traffic curve
    hourly_scores: List[HourlyScore]
    daily_patterns: List[DailyPattern]
    
    # Weekly heatmap data
    heatmap: List[List[float]]   # 7 days x 24 hours
    
    # Insights
    best_days: List[str]
    worst_days: List[str]
    optimal_windows: List[Dict]  # [{start: "9AM", end: "11AM", day: "Monday"}]
    
    # Metadata
    posts_analyzed: int
    date_range_days: int
    computed_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    
    def to_dict(self) -> Dict:
        data = asdict(self)
        data['hourly_scores'] = [asdict(h) for h in self.hourly_scores]
        data['daily_patterns'] = [
            {**asdict(d), 'hourly_scores': [asdict(h) for h in d.hourly_scores]}
            for d in self.daily_patterns
        ]
        return data


class BestTimeService:
    """
    Analyzes posting history to recommend optimal posting times.
    
    Uses a weighted combination of:
    - Historical post performance by hour
    - Engagement rate patterns
    - View velocity in first hours
    """
    
    def __init__(self):
        self.engine = create_engine(DATABASE_URL)
    
    def _hour_label(self, hour: int) -> str:
        """Convert 24h to 12h format"""
        if hour == 0:
            return "12 AM"
        elif hour < 12:
            return f"{hour} AM"
        elif hour == 12:
            return "12 PM"
        else:
            return f"{hour - 12} PM"
    
    def _day_name(self, day: int) -> str:
        """Get day name from index"""
        days = ["Sunday", "Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday"]
        return days[day]
    
    async def analyze_best_time(
        self,
        user_id: str = None,
        platform: str = "instagram",
        days_to_analyze: int = 90
    ) -> BestTimeResult:
        """
        Analyze posting history to find best times.
        
        Args:
            user_id: Optional user ID to filter posts
            platform: Platform to analyze (instagram, tiktok, youtube)
            days_to_analyze: How many days of history to consider
        """
        # Get historical posts
        posts = self._fetch_posts(user_id, platform, days_to_analyze)
        
        if not posts:
            # Return default pattern if no posts
            return self._generate_default_pattern()
        
        # Analyze by hour
        hourly_data = self._analyze_hourly(posts)
        
        # Analyze by day
        daily_data = self._analyze_daily(posts)
        
        # Build heatmap
        heatmap = self._build_heatmap(posts)
        
        # Get current time info
        now = datetime.now(timezone.utc)
        current_hour = now.hour
        current_day = now.weekday()  # Monday=0 in Python
        # Convert to Sunday=0 format
        current_day = (current_day + 1) % 7
        
        # Find today's peak
        today_scores = heatmap[current_day]
        peak_hour_today = today_scores.index(max(today_scores))
        peak_score_today = today_scores[peak_hour_today]
        current_score = today_scores[current_hour]
        
        # Determine if should wait
        should_wait = current_score < peak_score_today * 0.8
        wait_reason = ""
        countdown_minutes = 0
        
        if should_wait:
            if current_hour < peak_hour_today:
                countdown_minutes = (peak_hour_today - current_hour) * 60
                wait_reason = f"Peak time is at {self._hour_label(peak_hour_today)}. Current hour is only {int(current_score/peak_score_today*100)}% of peak."
            else:
                # Peak already passed, find next day's peak
                next_day = (current_day + 1) % 7
                next_peak = heatmap[next_day].index(max(heatmap[next_day]))
                wait_reason = f"Today's peak has passed. Tomorrow's peak is at {self._hour_label(next_peak)}."
                countdown_minutes = (24 - current_hour + next_peak) * 60
        
        # Calculate next peak time
        if current_hour < peak_hour_today:
            next_peak_time = now.replace(hour=peak_hour_today, minute=0, second=0)
        else:
            next_peak_time = (now + timedelta(days=1)).replace(hour=peak_hour_today, minute=0, second=0)
        
        # Find best/worst days
        day_totals = [(i, sum(heatmap[i])) for i in range(7)]
        day_totals.sort(key=lambda x: x[1], reverse=True)
        best_days = [self._day_name(d[0]) for d in day_totals[:3]]
        worst_days = [self._day_name(d[0]) for d in day_totals[-2:]]
        
        # Find optimal windows (consecutive hours above 80% of peak)
        optimal_windows = self._find_optimal_windows(heatmap)
        
        return BestTimeResult(
            current_hour=current_hour,
            current_day=current_day,
            current_score=round(current_score, 3),
            peak_hour_today=peak_hour_today,
            peak_score_today=round(peak_score_today, 3),
            should_wait=should_wait,
            wait_reason=wait_reason,
            countdown_minutes=countdown_minutes,
            next_peak_time=next_peak_time.isoformat(),
            hourly_scores=hourly_data,
            daily_patterns=daily_data,
            heatmap=[[round(h, 3) for h in day] for day in heatmap],
            best_days=best_days,
            worst_days=worst_days,
            optimal_windows=optimal_windows,
            posts_analyzed=len(posts),
            date_range_days=days_to_analyze
        )
    
    def _fetch_posts(
        self,
        user_id: str,
        platform: str,
        days: int
    ) -> List[Dict]:
        """Fetch posts from database"""
        try:
            with self.engine.connect() as conn:
                # Try to fetch from posted_content table
                result = conn.execute(text("""
                    SELECT 
                        id,
                        platform,
                        posted_at,
                        EXTRACT(HOUR FROM posted_at) as hour,
                        EXTRACT(DOW FROM posted_at) as day_of_week,
                        views,
                        likes,
                        comments,
                        shares,
                        CASE 
                            WHEN views > 0 THEN (likes + comments)::float / views * 100
                            ELSE 0
                        END as engagement_rate
                    FROM posted_content
                    WHERE posted_at >= NOW() - INTERVAL :days DAY
                    AND (:platform IS NULL OR platform = :platform)
                    ORDER BY posted_at DESC
                """), {"days": days, "platform": platform})
                
                posts = []
                for row in result:
                    posts.append({
                        "id": row[0],
                        "platform": row[1],
                        "posted_at": row[2],
                        "hour": int(row[3]) if row[3] else 0,
                        "day_of_week": int(row[4]) if row[4] else 0,
                        "views": row[5] or 0,
                        "likes": row[6] or 0,
                        "comments": row[7] or 0,
                        "shares": row[8] or 0,
                        "engagement_rate": row[9] or 0
                    })
                
                return posts
        except Exception as e:
            logger.warning(f"Could not fetch posts: {e}")
            return []
    
    def _analyze_hourly(self, posts: List[Dict]) -> List[HourlyScore]:
        """Analyze posts by hour"""
        hourly = defaultdict(lambda: {
            "posts": [],
            "views": [],
            "engagement_rates": []
        })
        
        for post in posts:
            hour = post["hour"]
            hourly[hour]["posts"].append(post)
            hourly[hour]["views"].append(post["views"])
            hourly[hour]["engagement_rates"].append(post["engagement_rate"])
        
        # Calculate scores
        scores = []
        max_posts = max(len(h["posts"]) for h in hourly.values()) if hourly else 1
        max_er = max(
            statistics.mean(h["engagement_rates"]) if h["engagement_rates"] else 0
            for h in hourly.values()
        ) if hourly else 1
        
        for hour in range(24):
            data = hourly[hour]
            post_count = len(data["posts"])
            avg_views = statistics.mean(data["views"]) if data["views"] else 0
            avg_er = statistics.mean(data["engagement_rates"]) if data["engagement_rates"] else 0
            
            # Score: weighted combination of post frequency and engagement
            frequency_score = post_count / max_posts if max_posts > 0 else 0
            er_score = avg_er / max_er if max_er > 0 else 0
            combined_score = 0.4 * frequency_score + 0.6 * er_score
            
            scores.append(HourlyScore(
                hour=hour,
                score=round(combined_score, 3),
                post_count=post_count,
                avg_engagement_rate=round(avg_er, 2),
                avg_views=int(avg_views),
                label=self._hour_label(hour)
            ))
        
        return scores
    
    def _analyze_daily(self, posts: List[Dict]) -> List[DailyPattern]:
        """Analyze posts by day of week"""
        daily = defaultdict(lambda: defaultdict(lambda: {
            "posts": [],
            "views": [],
            "engagement_rates": []
        }))
        
        for post in posts:
            day = int(post["day_of_week"])
            hour = post["hour"]
            daily[day][hour]["posts"].append(post)
            daily[day][hour]["views"].append(post["views"])
            daily[day][hour]["engagement_rates"].append(post["engagement_rate"])
        
        patterns = []
        for day in range(7):
            hourly_scores = []
            day_posts = 0
            
            # Get max values for normalization
            max_posts = max(
                len(daily[day][h]["posts"]) for h in range(24)
            ) if daily[day] else 1
            max_er = max(
                statistics.mean(daily[day][h]["engagement_rates"]) 
                if daily[day][h]["engagement_rates"] else 0
                for h in range(24)
            ) if daily[day] else 1
            
            for hour in range(24):
                data = daily[day][hour]
                post_count = len(data["posts"])
                day_posts += post_count
                avg_views = statistics.mean(data["views"]) if data["views"] else 0
                avg_er = statistics.mean(data["engagement_rates"]) if data["engagement_rates"] else 0
                
                frequency_score = post_count / max_posts if max_posts > 0 else 0
                er_score = avg_er / max_er if max_er > 0 else 0
                combined_score = 0.4 * frequency_score + 0.6 * er_score
                
                hourly_scores.append(HourlyScore(
                    hour=hour,
                    score=round(combined_score, 3),
                    post_count=post_count,
                    avg_engagement_rate=round(avg_er, 2),
                    avg_views=int(avg_views),
                    label=self._hour_label(hour)
                ))
            
            # Find peak
            peak_hour = max(range(24), key=lambda h: hourly_scores[h].score)
            peak_score = hourly_scores[peak_hour].score
            
            patterns.append(DailyPattern(
                day=day,
                day_name=self._day_name(day),
                hourly_scores=hourly_scores,
                peak_hour=peak_hour,
                peak_score=peak_score,
                total_posts=day_posts
            ))
        
        return patterns
    
    def _build_heatmap(self, posts: List[Dict]) -> List[List[float]]:
        """Build 7x24 heatmap of engagement scores"""
        # Initialize with small base values
        heatmap = [[0.1 for _ in range(24)] for _ in range(7)]
        counts = [[0 for _ in range(24)] for _ in range(7)]
        er_sums = [[0.0 for _ in range(24)] for _ in range(7)]
        
        for post in posts:
            day = int(post["day_of_week"])
            hour = post["hour"]
            counts[day][hour] += 1
            er_sums[day][hour] += post["engagement_rate"]
        
        # Calculate average ER for each cell
        for day in range(7):
            for hour in range(24):
                if counts[day][hour] > 0:
                    heatmap[day][hour] = er_sums[day][hour] / counts[day][hour]
        
        # Normalize to 0-1
        max_val = max(max(row) for row in heatmap)
        if max_val > 0:
            heatmap = [[v / max_val for v in row] for row in heatmap]
        
        return heatmap
    
    def _find_optimal_windows(self, heatmap: List[List[float]]) -> List[Dict]:
        """Find consecutive hours above 80% of daily peak"""
        windows = []
        
        for day in range(7):
            day_scores = heatmap[day]
            peak = max(day_scores)
            threshold = peak * 0.8
            
            # Find consecutive hours above threshold
            in_window = False
            window_start = 0
            
            for hour in range(24):
                if day_scores[hour] >= threshold:
                    if not in_window:
                        in_window = True
                        window_start = hour
                else:
                    if in_window:
                        # Window ended
                        if hour - window_start >= 2:  # At least 2 hours
                            windows.append({
                                "day": self._day_name(day),
                                "start": self._hour_label(window_start),
                                "end": self._hour_label(hour - 1),
                                "peak_score": round(peak, 2)
                            })
                        in_window = False
            
            # Check if window extends to end of day
            if in_window and 24 - window_start >= 2:
                windows.append({
                    "day": self._day_name(day),
                    "start": self._hour_label(window_start),
                    "end": self._hour_label(23),
                    "peak_score": round(peak, 2)
                })
        
        return windows
    
    def _generate_default_pattern(self) -> BestTimeResult:
        """Generate default pattern when no post history exists"""
        # Use industry-standard best times
        default_peaks = {
            0: 10,  # Sunday 10 AM
            1: 11,  # Monday 11 AM
            2: 10,  # Tuesday 10 AM
            3: 11,  # Wednesday 11 AM
            4: 12,  # Thursday 12 PM
            5: 11,  # Friday 11 AM
            6: 10,  # Saturday 10 AM
        }
        
        # Generate synthetic heatmap
        heatmap = []
        for day in range(7):
            peak = default_peaks[day]
            day_scores = []
            for hour in range(24):
                # Gaussian-like distribution around peak
                distance = abs(hour - peak)
                score = max(0.1, 1.0 - (distance * 0.08))
                # Reduce late night/early morning
                if hour < 6 or hour > 22:
                    score *= 0.3
                day_scores.append(round(score, 3))
            heatmap.append(day_scores)
        
        now = datetime.now(timezone.utc)
        current_hour = now.hour
        current_day = (now.weekday() + 1) % 7
        
        peak_hour_today = default_peaks[current_day]
        
        return BestTimeResult(
            current_hour=current_hour,
            current_day=current_day,
            current_score=heatmap[current_day][current_hour],
            peak_hour_today=peak_hour_today,
            peak_score_today=1.0,
            should_wait=current_hour < peak_hour_today,
            wait_reason="Based on general best practices (no post history yet)",
            countdown_minutes=(peak_hour_today - current_hour) * 60 if current_hour < peak_hour_today else 0,
            next_peak_time=(now.replace(hour=peak_hour_today, minute=0)).isoformat(),
            hourly_scores=[
                HourlyScore(
                    hour=h,
                    score=heatmap[current_day][h],
                    post_count=0,
                    avg_engagement_rate=0,
                    avg_views=0,
                    label=self._hour_label(h)
                )
                for h in range(24)
            ],
            daily_patterns=[],
            heatmap=heatmap,
            best_days=["Tuesday", "Wednesday", "Thursday"],
            worst_days=["Sunday", "Saturday"],
            optimal_windows=[
                {"day": "Weekdays", "start": "9 AM", "end": "12 PM", "peak_score": 0.95}
            ],
            posts_analyzed=0,
            date_range_days=0
        )


# Test function
async def test_best_time():
    service = BestTimeService()
    result = await service.analyze_best_time(platform="instagram")
    
    print("\n" + "="*60)
    print("⏰ BEST TIME TO POST ANALYSIS")
    print("="*60)
    print(f"Posts analyzed: {result.posts_analyzed}")
    print(f"Current time: {result.current_hour}:00 ({result.hourly_scores[result.current_hour].label})")
    print(f"Current score: {result.current_score}")
    print(f"\nPeak today: {result.peak_hour_today}:00 ({result.hourly_scores[result.peak_hour_today].label})")
    print(f"Peak score: {result.peak_score_today}")
    print(f"\nShould wait: {result.should_wait}")
    if result.should_wait:
        print(f"Reason: {result.wait_reason}")
        print(f"Countdown: {result.countdown_minutes} minutes")
    print(f"\nBest days: {', '.join(result.best_days)}")
    print(f"Worst days: {', '.join(result.worst_days)}")
    print(f"\nOptimal windows:")
    for w in result.optimal_windows[:3]:
        print(f"  {w['day']}: {w['start']} - {w['end']}")


if __name__ == "__main__":
    import asyncio
    asyncio.run(test_best_time())
