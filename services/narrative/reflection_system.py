"""
Reflection System for Narrative Scheduling

Analyzes weekly performance and generates learnings
for continuous improvement of content strategy.
"""

import os
import json
import logging
from typing import List, Dict, Optional, Any
from datetime import datetime, date, timedelta
from dataclasses import dataclass, field
from sqlalchemy import create_engine, text

from .models import Learning, PerformanceMetrics, WeeklyPlan

logger = logging.getLogger(__name__)

DATABASE_URL = os.getenv("DATABASE_URL", "postgresql://postgres:postgres@127.0.0.1:54322/postgres")


@dataclass
class PillarInsight:
    """Insight about a specific pillar's performance"""
    pillar_name: str
    posts_count: int
    avg_views: float
    avg_engagement: float
    performance_vs_average: float  # percentage above/below average
    verdict: str  # "exceeded", "met", "underperformed"
    insight: str
    recommendation: str


@dataclass 
class WeeklyReflection:
    """Complete weekly reflection report"""
    schedule_id: str
    week_start: date
    week_end: date
    
    # Goal Progress
    goal_statement: str
    goal_progress_pct: float
    goal_on_track: bool
    
    # Overall Metrics
    total_posts: int
    total_views: int
    total_engagement: int
    avg_engagement_rate: float
    
    # Pillar Analysis
    pillar_insights: List[PillarInsight] = field(default_factory=list)
    top_performing_pillar: Optional[str] = None
    underperforming_pillar: Optional[str] = None
    
    # Learnings
    learnings: List[Learning] = field(default_factory=list)
    
    # Recommendations for next week
    recommendations: List[str] = field(default_factory=list)
    pillar_adjustments: Dict[str, float] = field(default_factory=dict)
    
    def to_dict(self) -> Dict[str, Any]:
        return {
            "schedule_id": self.schedule_id,
            "week_start": self.week_start.isoformat(),
            "week_end": self.week_end.isoformat(),
            "goal_statement": self.goal_statement,
            "goal_progress_pct": self.goal_progress_pct,
            "goal_on_track": self.goal_on_track,
            "total_posts": self.total_posts,
            "total_views": self.total_views,
            "total_engagement": self.total_engagement,
            "avg_engagement_rate": self.avg_engagement_rate,
            "pillar_insights": [
                {
                    "pillar": p.pillar_name,
                    "posts": p.posts_count,
                    "avg_views": p.avg_views,
                    "avg_engagement": p.avg_engagement,
                    "vs_average": p.performance_vs_average,
                    "verdict": p.verdict,
                    "insight": p.insight,
                    "recommendation": p.recommendation
                } for p in self.pillar_insights
            ],
            "top_performing_pillar": self.top_performing_pillar,
            "underperforming_pillar": self.underperforming_pillar,
            "learnings": [l.to_dict() for l in self.learnings],
            "recommendations": self.recommendations,
            "pillar_adjustments": self.pillar_adjustments
        }


class ReflectionSystem:
    """
    Analyzes schedule performance and generates learnings.
    """
    
    def __init__(self):
        self.engine = create_engine(DATABASE_URL)
    
    async def generate_weekly_reflection(
        self,
        schedule_id: str
    ) -> WeeklyReflection:
        """
        Generate a comprehensive reflection for a completed schedule.
        
        Args:
            schedule_id: The weekly schedule to analyze
            
        Returns:
            WeeklyReflection with insights and learnings
        """
        logger.info(f"[Reflection] Generating reflection for schedule {schedule_id}")
        
        # Load schedule and performance data
        schedule_data = await self._load_schedule_data(schedule_id)
        performance_data = await self._load_performance_data(schedule_id)
        goal_data = await self._load_goal_data(schedule_data.get("goal_id"))
        
        # Analyze pillar performance
        pillar_insights = await self._analyze_pillar_performance(
            schedule_id, 
            performance_data
        )
        
        # Determine top/bottom performers
        sorted_pillars = sorted(
            pillar_insights, 
            key=lambda p: p.avg_engagement, 
            reverse=True
        )
        
        top_pillar = sorted_pillars[0].pillar_name if sorted_pillars else None
        bottom_pillar = sorted_pillars[-1].pillar_name if sorted_pillars else None
        
        # Generate learnings
        learnings = await self._generate_learnings(
            schedule_id,
            pillar_insights,
            performance_data
        )
        
        # Generate recommendations
        recommendations, pillar_adjustments = await self._generate_recommendations(
            pillar_insights,
            goal_data,
            performance_data
        )
        
        # Assess goal progress
        goal_progress = self._assess_goal_progress(goal_data, performance_data)
        
        reflection = WeeklyReflection(
            schedule_id=schedule_id,
            week_start=schedule_data.get("week_start", date.today()),
            week_end=schedule_data.get("week_end", date.today()),
            goal_statement=goal_data.get("goal_statement", "Build engagement"),
            goal_progress_pct=goal_progress.get("progress", 0),
            goal_on_track=goal_progress.get("on_track", False),
            total_posts=performance_data.get("total_posts", 0),
            total_views=performance_data.get("total_views", 0),
            total_engagement=performance_data.get("total_engagement", 0),
            avg_engagement_rate=performance_data.get("avg_engagement_rate", 0),
            pillar_insights=pillar_insights,
            top_performing_pillar=top_pillar,
            underperforming_pillar=bottom_pillar,
            learnings=learnings,
            recommendations=recommendations,
            pillar_adjustments=pillar_adjustments
        )
        
        # Save reflection to database
        await self._save_reflection(reflection)
        
        # Save learnings to database
        await self._save_learnings(learnings)
        
        logger.info(f"[Reflection] Generated {len(learnings)} learnings, {len(recommendations)} recommendations")
        
        return reflection
    
    async def _load_schedule_data(self, schedule_id: str) -> Dict[str, Any]:
        """Load schedule data from database"""
        with self.engine.connect() as conn:
            result = conn.execute(text("""
                SELECT id, goal_id, week_start, week_end, total_posts, 
                       pillar_distribution, platform_distribution, status
                FROM weekly_schedules WHERE id = :id
            """), {"id": schedule_id})
            
            row = result.fetchone()
            if row:
                return {
                    "id": str(row[0]),
                    "goal_id": str(row[1]) if row[1] else None,
                    "week_start": row[2],
                    "week_end": row[3],
                    "total_posts": row[4],
                    "pillar_distribution": json.loads(row[5]) if row[5] else {},
                    "platform_distribution": json.loads(row[6]) if row[6] else {},
                    "status": row[7]
                }
        
        return {}
    
    async def _load_performance_data(self, schedule_id: str) -> Dict[str, Any]:
        """Load performance metrics for scheduled posts"""
        with self.engine.connect() as conn:
            # Get posts from this schedule
            result = conn.execute(text("""
                SELECT ss.pillar, ss.video_id,
                       pc.views, pc.likes, pc.comments, pc.shares
                FROM schedule_slots ss
                LEFT JOIN posted_content pc ON pc.content_id::text = ss.video_id::text
                WHERE ss.schedule_id = :schedule_id
            """), {"schedule_id": schedule_id})
            
            posts = list(result)
            
            if not posts:
                return {
                    "total_posts": 0,
                    "total_views": 0,
                    "total_engagement": 0,
                    "avg_engagement_rate": 0,
                    "pillar_metrics": {}
                }
            
            # Aggregate metrics
            total_views = sum(p[2] or 0 for p in posts)
            total_likes = sum(p[3] or 0 for p in posts)
            total_comments = sum(p[4] or 0 for p in posts)
            total_shares = sum(p[5] or 0 for p in posts)
            total_engagement = total_likes + total_comments + total_shares
            
            avg_engagement = (total_engagement / total_views * 100) if total_views > 0 else 0
            
            # Per-pillar metrics
            pillar_metrics = {}
            for post in posts:
                pillar = post[0] or "Uncategorized"
                if pillar not in pillar_metrics:
                    pillar_metrics[pillar] = {
                        "posts": 0,
                        "views": 0,
                        "engagement": 0
                    }
                
                pillar_metrics[pillar]["posts"] += 1
                pillar_metrics[pillar]["views"] += post[2] or 0
                pillar_metrics[pillar]["engagement"] += (post[3] or 0) + (post[4] or 0) + (post[5] or 0)
            
            return {
                "total_posts": len(posts),
                "total_views": total_views,
                "total_engagement": total_engagement,
                "avg_engagement_rate": avg_engagement,
                "pillar_metrics": pillar_metrics
            }
    
    async def _load_goal_data(self, goal_id: Optional[str]) -> Dict[str, Any]:
        """Load goal data"""
        if not goal_id:
            return {"goal_statement": "Build engagement", "primary_cta": "follow"}
        
        with self.engine.connect() as conn:
            result = conn.execute(text("""
                SELECT goal_statement, primary_cta, target_audience
                FROM narrative_goals WHERE id = :id
            """), {"id": goal_id})
            
            row = result.fetchone()
            if row:
                return {
                    "goal_statement": row[0] or "Build engagement",
                    "primary_cta": row[1] or "follow",
                    "target_audience": row[2] or ""
                }
        
        return {"goal_statement": "Build engagement", "primary_cta": "follow"}
    
    async def _analyze_pillar_performance(
        self,
        schedule_id: str,
        performance_data: Dict[str, Any]
    ) -> List[PillarInsight]:
        """Analyze each pillar's performance using AI."""
        
        # Try AI-powered pillar insights first
        openai_api_key = os.getenv("OPENAI_API_KEY")
        if openai_api_key:
            try:
                return await self._analyze_pillar_performance_with_ai(
                    performance_data, openai_api_key
                )
            except Exception as e:
                logger.warning(f"[Reflection] AI pillar analysis failed: {e}")
        
        # Fallback to rule-based analysis
        return self._analyze_pillar_performance_fallback(performance_data)
    
    async def _analyze_pillar_performance_with_ai(
        self,
        performance_data: Dict[str, Any],
        api_key: str
    ) -> List[PillarInsight]:
        """Use real OpenAI to generate deeper pillar insights."""
        from openai import OpenAI
        client = OpenAI(api_key=api_key)
        
        pillar_metrics = performance_data.get("pillar_metrics", {})
        avg_engagement = performance_data.get("avg_engagement_rate", 0)
        
        # Build pillar data for AI
        pillar_data = []
        for pillar_name, metrics in pillar_metrics.items():
            posts = metrics.get("posts", 0)
            views = metrics.get("views", 0)
            engagement = metrics.get("engagement", 0)
            avg_views = views / posts if posts > 0 else 0
            pillar_engagement = (engagement / views * 100) if views > 0 else 0
            vs_average = ((pillar_engagement - avg_engagement) / avg_engagement * 100) if avg_engagement > 0 else 0
            
            pillar_data.append({
                "pillar": pillar_name,
                "posts": posts,
                "views": views,
                "avg_views": round(avg_views, 0),
                "engagement_rate": round(pillar_engagement, 2),
                "vs_average_percent": round(vs_average, 1)
            })
        
        prompt = f"""You are a content strategist analyzing pillar performance data.

OVERALL PERFORMANCE:
- Total Views: {performance_data.get('total_views', 0)}
- Avg Engagement Rate: {avg_engagement:.2f}%

PILLAR METRICS:
{json.dumps(pillar_data, indent=2)}

For EACH pillar, provide a deep analysis:
1. What does the data tell us about this pillar's performance?
2. What's the verdict: "exceeded", "met", or "underperformed"?
3. What specific, actionable recommendation would improve results?

Respond in JSON:
{{
    "pillar_insights": [
        {{
            "pillar": "pillar name",
            "verdict": "exceeded" | "met" | "underperformed",
            "insight": "Specific insight about what the data reveals (not generic)",
            "recommendation": "Actionable recommendation for next week"
        }},
        ...
    ]
}}

Be specific and data-driven. Reference actual numbers in your insights."""

        response = client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[
                {"role": "system", "content": "You are a data-driven content strategist. Provide specific, actionable insights."},
                {"role": "user", "content": prompt}
            ],
            temperature=0.4,
            response_format={"type": "json_object"}
        )
        
        result = json.loads(response.choices[0].message.content)
        ai_insights = result.get("pillar_insights", [])
        
        insights = []
        for item in ai_insights:
            pillar_name = item.get("pillar", "")
            metrics = pillar_metrics.get(pillar_name, {})
            posts = metrics.get("posts", 0)
            views = metrics.get("views", 0)
            engagement = metrics.get("engagement", 0)
            avg_views = views / posts if posts > 0 else 0
            pillar_engagement = (engagement / views * 100) if views > 0 else 0
            vs_average = ((pillar_engagement - avg_engagement) / avg_engagement * 100) if avg_engagement > 0 else 0
            
            insights.append(PillarInsight(
                pillar_name=pillar_name,
                posts_count=posts,
                avg_views=avg_views,
                avg_engagement=pillar_engagement,
                performance_vs_average=vs_average,
                verdict=item.get("verdict", "met"),
                insight=item.get("insight", f"{pillar_name} analysis"),
                recommendation=item.get("recommendation", "Continue monitoring")
            ))
        
        logger.info(f"[AI Pillar Insights] Generated {len(insights)} AI-powered pillar insights")
        return insights
    
    def _analyze_pillar_performance_fallback(
        self,
        performance_data: Dict[str, Any]
    ) -> List[PillarInsight]:
        """Fallback: Rule-based pillar analysis."""
        insights = []
        
        pillar_metrics = performance_data.get("pillar_metrics", {})
        avg_engagement = performance_data.get("avg_engagement_rate", 0)
        
        for pillar_name, metrics in pillar_metrics.items():
            posts = metrics.get("posts", 0)
            views = metrics.get("views", 0)
            engagement = metrics.get("engagement", 0)
            
            avg_views = views / posts if posts > 0 else 0
            pillar_engagement = (engagement / views * 100) if views > 0 else 0
            
            vs_average = ((pillar_engagement - avg_engagement) / avg_engagement * 100) if avg_engagement > 0 else 0
            
            if vs_average > 20:
                verdict = "exceeded"
                insight = f"{pillar_name} significantly outperformed average"
                recommendation = f"Consider increasing {pillar_name} allocation"
            elif vs_average < -20:
                verdict = "underperformed"
                insight = f"{pillar_name} underperformed compared to other pillars"
                recommendation = f"Review {pillar_name} content quality or reduce allocation"
            else:
                verdict = "met"
                insight = f"{pillar_name} performed as expected"
                recommendation = f"Maintain current {pillar_name} strategy"
            
            insights.append(PillarInsight(
                pillar_name=pillar_name,
                posts_count=posts,
                avg_views=avg_views,
                avg_engagement=pillar_engagement,
                performance_vs_average=vs_average,
                verdict=verdict,
                insight=insight,
                recommendation=recommendation
            ))
        
        return insights
    
    async def _generate_learnings(
        self,
        schedule_id: str,
        pillar_insights: List[PillarInsight],
        performance_data: Dict[str, Any]
    ) -> List[Learning]:
        """Generate learnings from performance analysis using AI."""
        
        # Try AI-powered learning synthesis
        openai_api_key = os.getenv("OPENAI_API_KEY")
        if openai_api_key:
            try:
                return await self._generate_learnings_with_ai(
                    schedule_id, pillar_insights, performance_data, openai_api_key
                )
            except Exception as e:
                logger.warning(f"[Reflection] AI learning synthesis failed: {e}")
        
        # Fallback to rule-based learnings
        return self._generate_learnings_fallback(schedule_id, pillar_insights, performance_data)
    
    async def _generate_learnings_with_ai(
        self,
        schedule_id: str,
        pillar_insights: List[PillarInsight],
        performance_data: Dict[str, Any],
        api_key: str
    ) -> List[Learning]:
        """Use real OpenAI to synthesize deeper learnings from performance data."""
        from openai import OpenAI
        import json
        client = OpenAI(api_key=api_key)
        
        # Build context for AI
        pillar_summary = []
        for insight in pillar_insights:
            pillar_summary.append({
                "pillar": insight.pillar_name,
                "posts": insight.posts_count,
                "avg_views": insight.avg_views,
                "avg_engagement": insight.avg_engagement,
                "vs_average": insight.performance_vs_average,
                "verdict": insight.verdict
            })
        
        prompt = f"""You are a content strategist analyzing a week's performance to generate actionable learnings.

PERFORMANCE DATA:
- Total Views: {performance_data.get('total_views', 0)}
- Avg Engagement Rate: {performance_data.get('avg_engagement_rate', 0):.1f}%
- Followers Gained: {performance_data.get('followers_gained', 0)}

PILLAR PERFORMANCE:
{json.dumps(pillar_summary, indent=2)}

Analyze this data and identify 3-5 non-obvious learnings that will improve next week's strategy.

For each learning, consider:
1. What pattern or insight does the data reveal?
2. How confident are we in this insight? (0.5-1.0)
3. What specific action should we take?

Respond in JSON:
{{
    "learnings": [
        {{
            "type": "content_timing" | "pillar_performance" | "audience_behavior" | "format_effectiveness" | "platform_specific",
            "insight": "The specific insight discovered",
            "confidence": 0.85,
            "action": "Specific, actionable recommendation"
        }},
        ...
    ]
}}

Focus on insights that are actionable and specific, not generic advice."""

        response = client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[
                {"role": "system", "content": "You are a data-driven content strategist. Identify actionable patterns."},
                {"role": "user", "content": prompt}
            ],
            temperature=0.4,
            response_format={"type": "json_object"}
        )
        
        result = json.loads(response.choices[0].message.content)
        ai_learnings = result.get("learnings", [])
        
        learnings = []
        for item in ai_learnings:
            learnings.append(Learning(
                learning_type=item.get("type", "general"),
                insight=item.get("insight", ""),
                confidence=item.get("confidence", 0.7),
                action=item.get("action", ""),
                source_schedule_id=schedule_id
            ))
        
        logger.info(f"[AI Learning] Generated {len(learnings)} AI-powered learnings")
        return learnings
    
    def _generate_learnings_fallback(
        self,
        schedule_id: str,
        pillar_insights: List[PillarInsight],
        performance_data: Dict[str, Any]
    ) -> List[Learning]:
        """Fallback: Rule-based learning generation"""
        learnings = []
        
        for insight in pillar_insights:
            if insight.verdict == "exceeded":
                learnings.append(Learning(
                    learning_type="pillar_performance",
                    insight=insight.insight,
                    confidence=0.85,
                    action=insight.recommendation,
                    source_schedule_id=schedule_id
                ))
            elif insight.verdict == "underperformed":
                learnings.append(Learning(
                    learning_type="pillar_performance",
                    insight=insight.insight,
                    confidence=0.75,
                    action=insight.recommendation,
                    source_schedule_id=schedule_id
                ))
        
        avg_engagement = performance_data.get("avg_engagement_rate", 0)
        if avg_engagement > 5:
            learnings.append(Learning(
                learning_type="overall_performance",
                insight=f"Week achieved {avg_engagement:.1f}% engagement rate - above industry average",
                confidence=0.9,
                action="Continue current content mix strategy",
                source_schedule_id=schedule_id
            ))
        elif avg_engagement < 2:
            learnings.append(Learning(
                learning_type="overall_performance",
                insight=f"Week achieved only {avg_engagement:.1f}% engagement - below target",
                confidence=0.8,
                action="Review content quality, timing, and hook effectiveness",
                source_schedule_id=schedule_id
            ))
        
        return learnings
    
    async def _generate_recommendations(
        self,
        pillar_insights: List[PillarInsight],
        goal_data: Dict[str, Any],
        performance_data: Dict[str, Any]
    ) -> tuple[List[str], Dict[str, float]]:
        """Generate recommendations for next week"""
        recommendations = []
        pillar_adjustments = {}
        
        for insight in pillar_insights:
            if insight.verdict == "exceeded":
                # Increase by 10%
                pillar_adjustments[insight.pillar_name] = 1.10
                recommendations.append(
                    f"Increase {insight.pillar_name} content by 10% - outperformed by {insight.performance_vs_average:.0f}%"
                )
            elif insight.verdict == "underperformed":
                # Decrease by 10%
                pillar_adjustments[insight.pillar_name] = 0.90
                recommendations.append(
                    f"Reduce {insight.pillar_name} by 10% or improve content quality"
                )
        
        # Goal-based recommendations
        primary_cta = goal_data.get("primary_cta", "follow")
        if primary_cta == "waitlist" or primary_cta == "purchase":
            recommendations.append(
                "Consider adding stronger CTAs to Process/How-To content"
            )
        
        if not recommendations:
            recommendations.append("Schedule performed as expected - maintain current strategy")
        
        return recommendations, pillar_adjustments
    
    def _assess_goal_progress(
        self,
        goal_data: Dict[str, Any],
        performance_data: Dict[str, Any]
    ) -> Dict[str, Any]:
        """Assess progress toward goal"""
        # Simplified assessment based on engagement
        avg_engagement = performance_data.get("avg_engagement_rate", 0)
        
        # Consider 4%+ as on track
        on_track = avg_engagement >= 4.0
        progress = min(avg_engagement / 4.0 * 100, 100)
        
        return {
            "on_track": on_track,
            "progress": progress
        }
    
    async def _save_reflection(self, reflection: WeeklyReflection):
        """Save reflection to database"""
        with self.engine.connect() as conn:
            conn.execute(text("""
                INSERT INTO schedule_performance (
                    schedule_id, week_start, week_end, total_posts, total_views,
                    total_likes, total_comments, avg_engagement_rate,
                    goal_progress_pct, pillar_performance
                ) VALUES (
                    :schedule_id, :week_start, :week_end, :total_posts, :total_views,
                    0, 0, :avg_engagement, :goal_progress, :pillar_perf
                )
                ON CONFLICT (schedule_id) DO UPDATE SET
                    total_views = EXCLUDED.total_views,
                    avg_engagement_rate = EXCLUDED.avg_engagement_rate,
                    goal_progress_pct = EXCLUDED.goal_progress_pct,
                    pillar_performance = EXCLUDED.pillar_performance
            """), {
                "schedule_id": reflection.schedule_id,
                "week_start": reflection.week_start,
                "week_end": reflection.week_end,
                "total_posts": reflection.total_posts,
                "total_views": reflection.total_views,
                "avg_engagement": reflection.avg_engagement_rate,
                "goal_progress": reflection.goal_progress_pct,
                "pillar_perf": json.dumps({
                    p.pillar_name: {
                        "posts": p.posts_count,
                        "avg_views": p.avg_views,
                        "avg_engagement": p.avg_engagement,
                        "verdict": p.verdict
                    } for p in reflection.pillar_insights
                })
            })
            conn.commit()
    
    async def _save_learnings(self, learnings: List[Learning]):
        """Save learnings to database"""
        with self.engine.connect() as conn:
            for learning in learnings:
                conn.execute(text("""
                    INSERT INTO learnings (
                        id, learning_type, insight, confidence, action,
                        source_schedule_id, applied
                    ) VALUES (
                        :id, :type, :insight, :confidence, :action,
                        :source_id, FALSE
                    )
                """), {
                    "id": learning.id,
                    "type": learning.learning_type,
                    "insight": learning.insight,
                    "confidence": learning.confidence,
                    "action": learning.action,
                    "source_id": learning.source_schedule_id
                })
            conn.commit()
    
    async def generate_recommendations(
        self,
        goal_data: Dict[str, Any],
        performance_data: Dict[str, Any],
        pillar_insights: List[PillarInsight],
        learnings: List[Learning]
    ) -> List[Dict[str, Any]]:
        """Generate AI-powered strategic recommendations for next week."""
        
        openai_api_key = os.getenv("OPENAI_API_KEY")
        if not openai_api_key:
            return self._generate_recommendations_fallback(pillar_insights, learnings)
        
        try:
            from openai import OpenAI
            client = OpenAI(api_key=openai_api_key)
            
            # Build context
            pillar_summary = [{
                "pillar": p.pillar_name,
                "verdict": p.verdict,
                "insight": p.insight
            } for p in pillar_insights]
            
            learning_summary = [{
                "type": l.learning_type,
                "insight": l.insight,
                "action": l.action
            } for l in learnings[:5]]
            
            prompt = f"""You are a strategic content advisor generating recommendations for next week's content plan.

GOAL: {goal_data.get('goal_statement', 'Build engagement')}
TARGET AUDIENCE: {goal_data.get('target_audience', 'general audience')}
PRIMARY CTA: {goal_data.get('primary_cta', 'follow')}

THIS WEEK'S PERFORMANCE:
- Total Views: {performance_data.get('total_views', 0)}
- Avg Engagement: {performance_data.get('avg_engagement_rate', 0):.1f}%
- Followers Gained: {performance_data.get('followers_gained', 0)}

PILLAR INSIGHTS:
{json.dumps(pillar_summary, indent=2)}

KEY LEARNINGS:
{json.dumps(learning_summary, indent=2)}

Generate 3-5 strategic recommendations for next week. Each should be:
1. Specific and actionable
2. Based on the data provided
3. Prioritized by expected impact

Respond in JSON:
{{
    "recommendations": [
        {{
            "priority": 1,
            "category": "content_mix" | "timing" | "platform" | "engagement" | "growth",
            "title": "Short recommendation title",
            "description": "Detailed explanation of what to do and why",
            "expected_impact": "What improvement this should drive",
            "implementation": "How to implement this recommendation"
        }},
        ...
    ]
}}"""

            response = client.chat.completions.create(
                model="gpt-4o-mini",
                messages=[
                    {"role": "system", "content": "You are a strategic content advisor. Provide actionable, data-driven recommendations."},
                    {"role": "user", "content": prompt}
                ],
                temperature=0.5,
                response_format={"type": "json_object"}
            )
            
            result = json.loads(response.choices[0].message.content)
            recommendations = result.get("recommendations", [])
            
            logger.info(f"[AI Recommendations] Generated {len(recommendations)} strategic recommendations")
            return recommendations
            
        except Exception as e:
            logger.warning(f"[Recommendations] AI generation failed: {e}")
            return self._generate_recommendations_fallback(pillar_insights, learnings)
    
    def _generate_recommendations_fallback(
        self,
        pillar_insights: List[PillarInsight],
        learnings: List[Learning]
    ) -> List[Dict[str, Any]]:
        """Fallback: Generate basic recommendations from insights."""
        recommendations = []
        priority = 1
        
        # Generate from pillar insights
        for insight in pillar_insights:
            if insight.verdict == "exceeded":
                recommendations.append({
                    "priority": priority,
                    "category": "content_mix",
                    "title": f"Increase {insight.pillar_name} content",
                    "description": insight.recommendation,
                    "expected_impact": "Higher engagement based on past performance",
                    "implementation": f"Add 1-2 more {insight.pillar_name} posts next week"
                })
                priority += 1
            elif insight.verdict == "underperformed":
                recommendations.append({
                    "priority": priority,
                    "category": "content_mix",
                    "title": f"Review {insight.pillar_name} strategy",
                    "description": insight.recommendation,
                    "expected_impact": "Improved engagement for this pillar",
                    "implementation": f"Analyze top performers in {insight.pillar_name} and replicate"
                })
                priority += 1
        
        # Add from learnings
        for learning in learnings[:2]:
            recommendations.append({
                "priority": priority,
                "category": learning.learning_type,
                "title": learning.insight[:50],
                "description": learning.action,
                "expected_impact": "Based on accumulated learnings",
                "implementation": learning.action
            })
            priority += 1
        
        return recommendations[:5]
    
    async def get_accumulated_learnings(
        self,
        goal_id: Optional[str] = None,
        min_confidence: float = 0.7,
        unapplied_only: bool = True
    ) -> List[Learning]:
        """Get accumulated learnings for planning"""
        with self.engine.connect() as conn:
            query = """
                SELECT id, learning_type, insight, confidence, action, 
                       source_schedule_id, applied
                FROM learnings
                WHERE confidence >= :min_confidence
            """
            params = {"min_confidence": min_confidence}
            
            if unapplied_only:
                query += " AND applied = FALSE"
            
            query += " ORDER BY confidence DESC, created_at DESC LIMIT 10"
            
            result = conn.execute(text(query), params)
            
            learnings = []
            for row in result:
                learnings.append(Learning(
                    id=str(row[0]),
                    learning_type=row[1],
                    insight=row[2],
                    confidence=row[3],
                    action=row[4],
                    source_schedule_id=str(row[5]) if row[5] else "",
                    applied=row[6]
                ))
            
            return learnings
    
    # =========================================================================
    # AI AUDIENCE SEGMENTATION
    # =========================================================================
    
    async def predict_audience_segments(
        self,
        content_data: List[Dict[str, Any]]
    ) -> List[Dict[str, Any]]:
        """
        AI-powered audience segmentation - predict which content resonates with specific segments.
        """
        try:
            from openai import OpenAI
            client = OpenAI()
            
            # Prepare content summary
            content_summary = []
            for c in content_data[:20]:
                content_summary.append({
                    "topics": c.get("topics", [])[:3],
                    "tone": c.get("tone"),
                    "engagement": c.get("engagement_rate", 0)
                })
            
            prompt = f"""Analyze this content performance data and identify audience segments.

Content Data:
{json.dumps(content_summary, indent=2)}

For each segment, predict:
1. Segment name (e.g., "Tech Enthusiasts", "Lifestyle Seekers")
2. Content preferences (what topics/tone they engage with)
3. Best posting times for this segment
4. Estimated segment size (% of audience)

Return JSON:
{{
  "segments": [
    {{
      "name": "Segment Name",
      "description": "Brief description",
      "preferred_topics": ["topic1", "topic2"],
      "preferred_tone": "casual/professional/inspirational",
      "best_posting_times": ["9:00 AM", "6:00 PM"],
      "estimated_percentage": 25
    }}
  ]
}}

Return ONLY valid JSON."""

            response = client.chat.completions.create(
                model="gpt-4o-mini",
                messages=[{"role": "user", "content": prompt}],
                temperature=0.7,
                max_tokens=600
            )
            
            result = json.loads(response.choices[0].message.content.strip())
            logger.info(f"[AI Audience] Identified {len(result.get('segments', []))} audience segments")
            return result.get("segments", [])
            
        except Exception as e:
            logger.warning(f"[AI Audience] Segmentation failed: {e}")
            return [
                {"name": "General Audience", "description": "Broad audience segment", "estimated_percentage": 100}
            ]
    
    # =========================================================================
    # AI TREND INTEGRATION
    # =========================================================================
    
    async def integrate_trends(
        self,
        current_plan: List[Dict[str, Any]],
        trending_topics: Optional[List[str]] = None
    ) -> List[Dict[str, Any]]:
        """
        AI-powered trend integration - incorporate trending topics into content planning.
        """
        try:
            from openai import OpenAI
            client = OpenAI()
            
            # If no trending topics provided, use placeholder
            if not trending_topics:
                trending_topics = ["AI technology", "sustainability", "remote work", "wellness"]
            
            prompt = f"""You are a social media strategist. Integrate trending topics into this content plan.

Current Plan:
{json.dumps(current_plan[:5], indent=2)}

Trending Topics:
{json.dumps(trending_topics)}

For each plan item, suggest how to incorporate relevant trends without losing the original message.
Also suggest 1-2 new content ideas based purely on trends.

Return JSON:
{{
  "enhanced_plan": [
    {{
      "original_id": "video_id",
      "trend_integration": "How to incorporate trend",
      "suggested_hashtags": ["#trend1", "#trend2"]
    }}
  ],
  "new_trend_content": [
    {{
      "trend": "trend name",
      "content_idea": "Content concept",
      "priority": "high/medium/low"
    }}
  ]
}}

Return ONLY valid JSON."""

            response = client.chat.completions.create(
                model="gpt-4o-mini",
                messages=[{"role": "user", "content": prompt}],
                temperature=0.7,
                max_tokens=600
            )
            
            result = json.loads(response.choices[0].message.content.strip())
            logger.info(f"[AI Trends] Integrated trends into {len(result.get('enhanced_plan', []))} items")
            return result
            
        except Exception as e:
            logger.warning(f"[AI Trends] Trend integration failed: {e}")
            return {"enhanced_plan": [], "new_trend_content": []}
    
    # =========================================================================
    # AI A/B TEST DESIGN
    # =========================================================================
    
    async def design_ab_tests(
        self,
        content_items: List[Dict[str, Any]],
        goals: Optional[List[str]] = None
    ) -> List[Dict[str, Any]]:
        """
        AI-powered A/B test design - automatically design experiments to test hypotheses.
        """
        try:
            from openai import OpenAI
            client = OpenAI()
            
            if not goals:
                goals = ["increase engagement", "grow followers", "improve watch time"]
            
            prompt = f"""Design A/B tests for this content to achieve these goals.

Content Items (sample):
{json.dumps(content_items[:5], indent=2)}

Goals:
{json.dumps(goals)}

Design 2-3 A/B tests that:
1. Have clear hypotheses
2. Define control vs variant
3. Specify success metrics
4. Estimate required sample size

Return JSON:
{{
  "experiments": [
    {{
      "name": "Test Name",
      "hypothesis": "If we do X, then Y will happen because Z",
      "control": "Description of control version",
      "variant": "Description of variant version",
      "variable_tested": "What's being changed",
      "success_metric": "engagement_rate/views/etc",
      "minimum_sample_size": 100,
      "expected_lift": "10-20%",
      "priority": "high/medium/low"
    }}
  ]
}}

Return ONLY valid JSON."""

            response = client.chat.completions.create(
                model="gpt-4o-mini",
                messages=[{"role": "user", "content": prompt}],
                temperature=0.7,
                max_tokens=700
            )
            
            result = json.loads(response.choices[0].message.content.strip())
            experiments = result.get("experiments", [])
            logger.info(f"[AI A/B] Designed {len(experiments)} experiments")
            return experiments
            
        except Exception as e:
            logger.warning(f"[AI A/B] Test design failed: {e}")
            return [
                {
                    "name": "Posting Time Test",
                    "hypothesis": "Morning posts get higher engagement",
                    "control": "Evening posting (6-8 PM)",
                    "variant": "Morning posting (8-10 AM)",
                    "variable_tested": "posting_time",
                    "success_metric": "engagement_rate",
                    "minimum_sample_size": 50,
                    "expected_lift": "10-15%",
                    "priority": "medium"
                }
            ]
