"""
Post Ranker Service
===================
Scores and ranks competitor posts by velocity, engagement, and viral potential.
Uses time-series data when available for accurate velocity calculations.
"""
import os
from datetime import datetime, timedelta
from typing import Dict, Any, List, Optional, Tuple
from dataclasses import dataclass, field
from loguru import logger

from sqlalchemy import create_engine, text


@dataclass
class PostScore:
    """Detailed scoring for a post"""
    post_id: str
    
    # Raw metrics
    views: int = 0
    likes: int = 0
    comments: int = 0
    shares: int = 0
    
    # Calculated scores (0-100)
    velocity_score: float = 0.0  # Growth rate
    engagement_score: float = 0.0  # Interaction rate
    viral_potential_score: float = 0.0  # Virality indicators
    comment_quality_score: float = 0.0  # Question density, engagement depth
    template_worthiness_score: float = 0.0  # How replicable is this format
    
    # Composite
    overall_score: float = 0.0
    rank: int = 0
    
    # Context
    hours_since_post: Optional[float] = None
    views_per_hour: Optional[float] = None
    engagement_rate: Optional[float] = None
    
    # Reasoning
    score_reasoning: str = ""


@dataclass
class RankingResult:
    """Result of a ranking operation"""
    account_id: str
    ranking_type: str
    time_window: str
    posts_ranked: int
    rankings: List[PostScore] = field(default_factory=list)
    scoring_config: Dict[str, float] = field(default_factory=dict)
    created_at: str = field(default_factory=lambda: datetime.utcnow().isoformat())


class PostRanker:
    """
    Ranks competitor posts using multiple scoring dimensions.
    Supports velocity-based, engagement-based, and composite rankings.
    """
    
    # Default scoring weights
    DEFAULT_WEIGHTS = {
        "velocity": 0.30,
        "engagement": 0.25,
        "viral_potential": 0.20,
        "comment_quality": 0.10,
        "template_worthiness": 0.15
    }
    
    # Engagement rate benchmarks by platform
    ENGAGEMENT_BENCHMARKS = {
        "instagram": {"low": 0.01, "avg": 0.03, "high": 0.06},
        "tiktok": {"low": 0.02, "avg": 0.05, "high": 0.10},
        "youtube": {"low": 0.01, "avg": 0.02, "high": 0.05},
        "x": {"low": 0.005, "avg": 0.015, "high": 0.03}
    }
    
    def __init__(self, db_url: Optional[str] = None):
        self.db_url = db_url or os.getenv("DATABASE_URL", "postgresql://postgres:postgres@127.0.0.1:54322/postgres")
        self.engine = create_engine(self.db_url)
    
    def calculate_velocity_score(
        self,
        views: int,
        hours_since_post: float,
        platform: str = "instagram"
    ) -> Tuple[float, float]:
        """
        Calculate velocity score based on views per hour.
        Returns (score, views_per_hour)
        """
        if hours_since_post <= 0:
            hours_since_post = 1
        
        views_per_hour = views / hours_since_post
        
        # Platform-specific velocity benchmarks (views per hour)
        benchmarks = {
            "instagram": {"low": 100, "avg": 500, "high": 2000, "viral": 10000},
            "tiktok": {"low": 500, "avg": 2000, "high": 10000, "viral": 50000},
            "youtube": {"low": 50, "avg": 200, "high": 1000, "viral": 5000}
        }
        
        bench = benchmarks.get(platform, benchmarks["instagram"])
        
        if views_per_hour >= bench["viral"]:
            score = 95 + min(5, (views_per_hour - bench["viral"]) / bench["viral"] * 5)
        elif views_per_hour >= bench["high"]:
            score = 80 + (views_per_hour - bench["high"]) / (bench["viral"] - bench["high"]) * 15
        elif views_per_hour >= bench["avg"]:
            score = 50 + (views_per_hour - bench["avg"]) / (bench["high"] - bench["avg"]) * 30
        elif views_per_hour >= bench["low"]:
            score = 20 + (views_per_hour - bench["low"]) / (bench["avg"] - bench["low"]) * 30
        else:
            score = max(0, views_per_hour / bench["low"] * 20)
        
        return min(100, score), views_per_hour
    
    def calculate_engagement_score(
        self,
        views: int,
        likes: int,
        comments: int,
        shares: int,
        platform: str = "instagram"
    ) -> Tuple[float, float]:
        """
        Calculate engagement score based on interaction rate.
        Returns (score, engagement_rate)
        """
        if views <= 0:
            return 0, 0
        
        # Weighted engagement calculation
        # Comments and shares are worth more than likes
        engagement = likes + (comments * 3) + (shares * 5)
        engagement_rate = engagement / views
        
        bench = self.ENGAGEMENT_BENCHMARKS.get(platform, self.ENGAGEMENT_BENCHMARKS["instagram"])
        
        if engagement_rate >= bench["high"]:
            score = 80 + min(20, (engagement_rate - bench["high"]) / bench["high"] * 20)
        elif engagement_rate >= bench["avg"]:
            score = 50 + (engagement_rate - bench["avg"]) / (bench["high"] - bench["avg"]) * 30
        elif engagement_rate >= bench["low"]:
            score = 20 + (engagement_rate - bench["low"]) / (bench["avg"] - bench["low"]) * 30
        else:
            score = max(0, engagement_rate / bench["low"] * 20)
        
        return min(100, score), engagement_rate
    
    def calculate_viral_potential(
        self,
        views: int,
        shares: int,
        comments: int,
        hours_since_post: float
    ) -> float:
        """
        Estimate viral potential based on share ratio and comment engagement.
        """
        score = 0
        
        # Share ratio is key indicator of virality
        if views > 0:
            share_ratio = shares / views
            if share_ratio >= 0.05:  # 5%+ share rate is exceptional
                score += 40
            elif share_ratio >= 0.02:
                score += 30
            elif share_ratio >= 0.01:
                score += 20
            elif share_ratio >= 0.005:
                score += 10
        
        # Comment-to-like ratio indicates engagement depth
        # High comments relative to views suggests conversation
        if views > 0:
            comment_ratio = comments / views
            if comment_ratio >= 0.01:  # 1%+ comment rate
                score += 30
            elif comment_ratio >= 0.005:
                score += 20
            elif comment_ratio >= 0.002:
                score += 10
        
        # Fresh + high engagement = viral potential
        if hours_since_post < 24 and views > 10000:
            score += 20
        elif hours_since_post < 48 and views > 50000:
            score += 15
        
        return min(100, score)
    
    def calculate_template_worthiness(
        self,
        engagement_score: float,
        has_hook_analysis: bool = False,
        hook_score: float = 0,
        has_beat_sheet: bool = False
    ) -> float:
        """
        Score how suitable this post is as a template.
        High engagement + clear structure = good template.
        """
        score = engagement_score * 0.4  # Base from engagement
        
        if has_hook_analysis and hook_score > 70:
            score += 25
        elif has_hook_analysis and hook_score > 50:
            score += 15
        
        if has_beat_sheet:
            score += 20
        
        # Bonus for very high engagement (proven format)
        if engagement_score > 80:
            score += 15
        
        return min(100, score)
    
    def rank_posts(
        self,
        account_id: str,
        posts: List[Dict[str, Any]],
        platform: str = "instagram",
        weights: Optional[Dict[str, float]] = None,
        ranking_type: str = "composite",
        time_window: str = "all"
    ) -> RankingResult:
        """
        Rank posts using specified scoring method.
        
        Args:
            account_id: Account being analyzed
            posts: List of post dicts with metrics
            platform: Platform for benchmark comparison
            weights: Custom scoring weights
            ranking_type: 'velocity', 'engagement', 'viral_potential', 'composite'
            time_window: Time filter applied
        """
        weights = weights or self.DEFAULT_WEIGHTS
        scored_posts: List[PostScore] = []
        
        for post in posts:
            post_id = post.get("post_id", post.get("id", ""))
            views = post.get("views", 0) or 0
            likes = post.get("likes", 0) or 0
            comments = post.get("comments", 0) or 0
            shares = post.get("shares", 0) or 0
            
            # Calculate hours since post
            posted_at = post.get("posted_at")
            hours_since = 720  # Default to 30 days
            if posted_at:
                try:
                    if isinstance(posted_at, str):
                        post_dt = datetime.fromisoformat(posted_at.replace("Z", "+00:00"))
                    else:
                        post_dt = posted_at
                    hours_since = (datetime.utcnow() - post_dt.replace(tzinfo=None)).total_seconds() / 3600
                except:
                    pass
            
            # Calculate individual scores
            velocity_score, views_per_hour = self.calculate_velocity_score(
                views, hours_since, platform
            )
            engagement_score, engagement_rate = self.calculate_engagement_score(
                views, likes, comments, shares, platform
            )
            viral_score = self.calculate_viral_potential(
                views, shares, comments, hours_since
            )
            
            # Get deep audit scores if available
            hook_score = post.get("hook_score", 0)
            has_beat_sheet = bool(post.get("beat_sheet"))
            
            template_score = self.calculate_template_worthiness(
                engagement_score,
                has_hook_analysis=hook_score > 0,
                hook_score=hook_score,
                has_beat_sheet=has_beat_sheet
            )
            
            # Calculate composite score
            if ranking_type == "velocity":
                overall = velocity_score
            elif ranking_type == "engagement":
                overall = engagement_score
            elif ranking_type == "viral_potential":
                overall = viral_score
            elif ranking_type == "template_worthy":
                overall = template_score
            else:  # composite
                overall = (
                    velocity_score * weights.get("velocity", 0.3) +
                    engagement_score * weights.get("engagement", 0.25) +
                    viral_score * weights.get("viral_potential", 0.2) +
                    template_score * weights.get("template_worthiness", 0.15)
                )
            
            # Build reasoning
            reasoning_parts = []
            if velocity_score > 70:
                reasoning_parts.append(f"High velocity ({views_per_hour:.0f} views/hr)")
            if engagement_rate and engagement_rate > 0.03:
                reasoning_parts.append(f"Strong engagement ({engagement_rate:.1%})")
            if viral_score > 60:
                reasoning_parts.append("Viral indicators present")
            
            scored_posts.append(PostScore(
                post_id=post_id,
                views=views,
                likes=likes,
                comments=comments,
                shares=shares,
                velocity_score=velocity_score,
                engagement_score=engagement_score,
                viral_potential_score=viral_score,
                template_worthiness_score=template_score,
                overall_score=overall,
                hours_since_post=hours_since,
                views_per_hour=views_per_hour,
                engagement_rate=engagement_rate,
                score_reasoning=" | ".join(reasoning_parts) if reasoning_parts else "Average performance"
            ))
        
        # Sort by overall score
        scored_posts.sort(key=lambda p: p.overall_score, reverse=True)
        
        # Assign ranks
        for i, post in enumerate(scored_posts):
            post.rank = i + 1
        
        return RankingResult(
            account_id=account_id,
            ranking_type=ranking_type,
            time_window=time_window,
            posts_ranked=len(scored_posts),
            rankings=scored_posts,
            scoring_config=weights
        )
    
    async def save_ranking(self, ranking: RankingResult) -> str:
        """Save ranking result to database"""
        try:
            with self.engine.connect() as conn:
                # Convert dataclass list to dict list
                rankings_json = []
                for r in ranking.rankings:
                    rankings_json.append({
                        "post_id": r.post_id,
                        "rank": r.rank,
                        "overall_score": r.overall_score,
                        "velocity_score": r.velocity_score,
                        "engagement_score": r.engagement_score,
                        "viral_potential_score": r.viral_potential_score,
                        "reasoning": r.score_reasoning
                    })
                
                result = conn.execute(text("""
                    INSERT INTO competitor_post_ranking (
                        account_id, ranking_type, time_window,
                        ranked_posts, scoring_config
                    ) VALUES (
                        :account_id, :ranking_type, :time_window,
                        :ranked_posts, :scoring_config
                    )
                    RETURNING ranking_id
                """), {
                    "account_id": ranking.account_id,
                    "ranking_type": ranking.ranking_type,
                    "time_window": ranking.time_window,
                    "ranked_posts": rankings_json,
                    "scoring_config": ranking.scoring_config
                })
                conn.commit()
                row = result.fetchone()
                return str(row[0])
        except Exception as e:
            logger.error(f"Failed to save ranking: {e}")
            raise
