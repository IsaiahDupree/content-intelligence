"""
Posting Time Analyzer
=====================
Analyzes competitor post timing to identify optimal posting times.
Groups posts by hour/day and calculates performance metrics.
"""

import os
from datetime import datetime, timedelta
from typing import Dict, Any, List, Optional, Tuple
from dataclasses import dataclass, field
from loguru import logger
from collections import defaultdict

from sqlalchemy import create_engine, text


@dataclass
class TimeSlotPerformance:
    """Performance metrics for a specific time slot"""
    hour: int  # 0-23
    day_of_week: Optional[int] = None  # 0=Monday, 6=Sunday
    post_count: int = 0
    avg_views: float = 0.0
    avg_likes: float = 0.0
    avg_comments: float = 0.0
    avg_engagement_rate: float = 0.0
    total_views: int = 0
    total_likes: int = 0
    total_comments: int = 0


@dataclass
class PostingTimeRecommendation:
    """Recommendation for optimal posting times"""
    best_hours: List[int]  # Top 3 hours
    best_days: List[int]  # Top 3 days (0=Monday)
    best_combinations: List[Dict[str, Any]]  # Top hour+day combinations
    worst_hours: List[int]  # Bottom 3 hours
    insights: List[str]  # Key insights
    timezone: str = "UTC"


class PostingTimeAnalyzer:
    """
    Analyzes posting times to identify optimal posting windows.
    Groups posts by hour/day and calculates performance correlations.
    """
    
    def __init__(self, db_url: Optional[str] = None):
        self.db_url = db_url or os.getenv("DATABASE_URL", "postgresql://postgres:postgres@127.0.0.1:54322/postgres")
        self.engine = create_engine(self.db_url)
    
    async def analyze_account(
        self,
        account_id: str,
        days_back: int = 90
    ) -> PostingTimeRecommendation:
        """
        Analyze posting times for a specific competitor account.
        
        Args:
            account_id: Database account ID
            days_back: How many days of posts to analyze
        
        Returns:
            PostingTimeRecommendation with optimal posting times
        """
        # Fetch posts with metrics
        cutoff_date = datetime.utcnow() - timedelta(days=days_back)
        
        query = text("""
            SELECT 
                cp.posted_at,
                cp.views,
                cp.likes,
                cp.comments,
                cp.shares,
                cp.follower_count_at_post
            FROM competitor_posts cp
            WHERE cp.account_id = CAST(:account_id AS uuid)
                AND cp.posted_at >= :cutoff_date
                AND cp.posted_at IS NOT NULL
                AND cp.views > 0
            ORDER BY cp.posted_at DESC
        """)
        
        with self.engine.connect() as conn:
            result = conn.execute(query, {
                "account_id": account_id,
                "cutoff_date": cutoff_date.isoformat()
            })
            rows = result.fetchall()
        
        if not rows:
            logger.warning(f"No posts found for account {account_id}")
            return PostingTimeRecommendation(
                best_hours=[],
                best_days=[],
                best_combinations=[],
                worst_hours=[],
                insights=["No posts found for analysis"]
            )
        
        # Group by time slots
        hourly_stats: Dict[int, List[Dict]] = defaultdict(list)
        daily_stats: Dict[int, List[Dict]] = defaultdict(list)
        combination_stats: Dict[Tuple[int, int], List[Dict]] = defaultdict(list)
        
        for row in rows:
            posted_at_str = row[0]
            views = row[1] or 0
            likes = row[2] or 0
            comments = row[3] or 0
            shares = row[4] or 0
            follower_count = row[5] or 1
            
            if not posted_at_str:
                continue
            
            # Parse datetime
            if isinstance(posted_at_str, str):
                posted_at = datetime.fromisoformat(posted_at_str.replace("Z", "+00:00"))
            else:
                posted_at = posted_at_str
            
            hour = posted_at.hour
            day_of_week = posted_at.weekday()  # 0=Monday
            
            # Calculate engagement rate
            total_engagement = likes + comments + shares
            engagement_rate = (total_engagement / follower_count) * 100 if follower_count > 0 else 0
            
            post_data = {
                "views": views,
                "likes": likes,
                "comments": comments,
                "shares": shares,
                "engagement_rate": engagement_rate,
                "total_engagement": total_engagement
            }
            
            hourly_stats[hour].append(post_data)
            daily_stats[day_of_week].append(post_data)
            combination_stats[(hour, day_of_week)].append(post_data)
        
        # Calculate averages for each time slot
        time_slot_performances: List[TimeSlotPerformance] = []
        
        for hour, posts in hourly_stats.items():
            if posts:
                avg_views = sum(p["views"] for p in posts) / len(posts)
                avg_likes = sum(p["likes"] for p in posts) / len(posts)
                avg_comments = sum(p["comments"] for p in posts) / len(posts)
                avg_engagement_rate = sum(p["engagement_rate"] for p in posts) / len(posts)
                
                time_slot_performances.append(TimeSlotPerformance(
                    hour=hour,
                    post_count=len(posts),
                    avg_views=avg_views,
                    avg_likes=avg_likes,
                    avg_comments=avg_comments,
                    avg_engagement_rate=avg_engagement_rate,
                    total_views=sum(p["views"] for p in posts),
                    total_likes=sum(p["likes"] for p in posts),
                    total_comments=sum(p["comments"] for p in posts)
                ))
        
        # Sort by engagement rate
        time_slot_performances.sort(key=lambda x: x.avg_engagement_rate, reverse=True)
        
        # Get best hours (top 3 with at least 2 posts)
        best_hours = [
            ts.hour for ts in time_slot_performances 
            if ts.post_count >= 2
        ][:3]
        
        # Get worst hours (bottom 3 with at least 2 posts)
        worst_hours = [
            ts.hour for ts in reversed(time_slot_performances)
            if ts.post_count >= 2
        ][:3]
        
        # Analyze day performance
        daily_performances: List[Tuple[int, float]] = []
        for day, posts in daily_stats.items():
            if posts:
                avg_engagement_rate = sum(p["engagement_rate"] for p in posts) / len(posts)
                daily_performances.append((day, avg_engagement_rate))
        
        daily_performances.sort(key=lambda x: x[1], reverse=True)
        best_days = [day for day, _ in daily_performances[:3]]
        
        # Analyze hour+day combinations
        combination_performances: List[Tuple[Tuple[int, int], float]] = []
        for (hour, day), posts in combination_stats.items():
            if len(posts) >= 2:
                avg_engagement_rate = sum(p["engagement_rate"] for p in posts) / len(posts)
                combination_performances.append(((hour, day), avg_engagement_rate))
        
        combination_performances.sort(key=lambda x: x[1], reverse=True)
        best_combinations = [
            {
                "hour": hour,
                "day": day,
                "day_name": ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"][day],
                "avg_engagement_rate": rate,
                "post_count": len(combination_stats[(hour, day)])
            }
            for (hour, day), rate in combination_performances[:5]
        ]
        
        # Generate insights
        insights = self._generate_insights(
            time_slot_performances,
            daily_performances,
            best_combinations
        )
        
        return PostingTimeRecommendation(
            best_hours=best_hours,
            best_days=best_days,
            best_combinations=best_combinations,
            worst_hours=worst_hours,
            insights=insights
        )
    
    async def analyze_multiple_accounts(
        self,
        account_ids: List[str],
        days_back: int = 90
    ) -> PostingTimeRecommendation:
        """
        Analyze posting times across multiple competitor accounts.
        Aggregates data to find patterns across the niche.
        """
        all_hourly_stats: Dict[int, List[Dict]] = defaultdict(list)
        all_daily_stats: Dict[int, List[Dict]] = defaultdict(list)
        
        for account_id in account_ids:
            cutoff_date = datetime.utcnow() - timedelta(days=days_back)
            
            query = text("""
                SELECT 
                    cp.posted_at,
                    cp.views,
                    cp.likes,
                    cp.comments,
                    cp.shares,
                    cp.follower_count_at_post
                FROM competitor_posts cp
                WHERE cp.account_id = CAST(:account_id AS uuid)
                    AND cp.posted_at >= :cutoff_date
                    AND cp.posted_at IS NOT NULL
                    AND cp.views > 0
            """)
            
            with self.engine.connect() as conn:
                result = conn.execute(query, {
                    "account_id": account_id,
                    "cutoff_date": cutoff_date.isoformat()
                })
                rows = result.fetchall()
            
            for row in rows:
                posted_at_str = row[0]
                views = row[1] or 0
                likes = row[2] or 0
                comments = row[3] or 0
                shares = row[4] or 0
                follower_count = row[5] or 1
                
                if not posted_at_str:
                    continue
                
                if isinstance(posted_at_str, str):
                    posted_at = datetime.fromisoformat(posted_at_str.replace("Z", "+00:00"))
                else:
                    posted_at = posted_at_str
                
                hour = posted_at.hour
                day_of_week = posted_at.weekday()
                
                total_engagement = likes + comments + shares
                engagement_rate = (total_engagement / follower_count) * 100 if follower_count > 0 else 0
                
                post_data = {
                    "views": views,
                    "likes": likes,
                    "comments": comments,
                    "shares": shares,
                    "engagement_rate": engagement_rate,
                    "total_engagement": total_engagement
                }
                
                all_hourly_stats[hour].append(post_data)
                all_daily_stats[day_of_week].append(post_data)
        
        # Calculate aggregated performance
        time_slot_performances: List[TimeSlotPerformance] = []
        
        for hour, posts in all_hourly_stats.items():
            if posts:
                avg_views = sum(p["views"] for p in posts) / len(posts)
                avg_likes = sum(p["likes"] for p in posts) / len(posts)
                avg_comments = sum(p["comments"] for p in posts) / len(posts)
                avg_engagement_rate = sum(p["engagement_rate"] for p in posts) / len(posts)
                
                time_slot_performances.append(TimeSlotPerformance(
                    hour=hour,
                    post_count=len(posts),
                    avg_views=avg_views,
                    avg_likes=avg_likes,
                    avg_comments=avg_comments,
                    avg_engagement_rate=avg_engagement_rate
                ))
        
        time_slot_performances.sort(key=lambda x: x.avg_engagement_rate, reverse=True)
        
        best_hours = [ts.hour for ts in time_slot_performances if ts.post_count >= 5][:3]
        worst_hours = [ts.hour for ts in reversed(time_slot_performances) if ts.post_count >= 5][:3]
        
        daily_performances: List[Tuple[int, float]] = []
        for day, posts in all_daily_stats.items():
            if posts:
                avg_engagement_rate = sum(p["engagement_rate"] for p in posts) / len(posts)
                daily_performances.append((day, avg_engagement_rate))
        
        daily_performances.sort(key=lambda x: x[1], reverse=True)
        best_days = [day for day, _ in daily_performances[:3]]
        
        insights = self._generate_insights(
            time_slot_performances,
            daily_performances,
            []
        )
        
        return PostingTimeRecommendation(
            best_hours=best_hours,
            best_days=best_days,
            best_combinations=[],
            worst_hours=worst_hours,
            insights=insights
        )
    
    def _generate_insights(
        self,
        time_slot_performances: List[TimeSlotPerformance],
        daily_performances: List[Tuple[int, float]],
        best_combinations: List[Dict[str, Any]]
    ) -> List[str]:
        """Generate human-readable insights from the analysis."""
        insights = []
        
        if not time_slot_performances:
            return ["No data available for analysis"]
        
        # Best hour insight
        if time_slot_performances:
            best = time_slot_performances[0]
            insights.append(
                f"Best posting hour: {best.hour}:00 ({best.avg_engagement_rate:.2f}% avg engagement, "
                f"{best.post_count} posts analyzed)"
            )
        
        # Peak hours insight
        peak_hours = [ts.hour for ts in time_slot_performances[:3] if ts.post_count >= 2]
        if peak_hours:
            insights.append(f"Peak engagement hours: {', '.join(f'{h}:00' for h in peak_hours)}")
        
        # Day performance insight
        if daily_performances:
            best_day_idx, best_day_rate = daily_performances[0]
            day_names = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]
            insights.append(
                f"Best posting day: {day_names[best_day_idx]} ({best_day_rate:.2f}% avg engagement)"
            )
        
        # Combination insights
        if best_combinations:
            top_combo = best_combinations[0]
            insights.append(
                f"Optimal combination: {top_combo['day_name']} at {top_combo['hour']}:00 "
                f"({top_combo['avg_engagement_rate']:.2f}% engagement, {top_combo['post_count']} posts)"
            )
        
        # Volume insight
        total_posts = sum(ts.post_count for ts in time_slot_performances)
        if total_posts > 0:
            insights.append(f"Total posts analyzed: {total_posts}")
        
        return insights

