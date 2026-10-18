"""
Goal-Based Recommendations Service
Provides content recommendations based on user goals
Phase 3: Pre/Post Social Score + Coaching
"""
import logging
from typing import Dict, List, Any, Optional
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession
from datetime import datetime, timedelta

logger = logging.getLogger(__name__)


class GoalRecommendationsService:
    """
    Generates content recommendations based on user goals
    
    Provides:
    - "Post 3 more videos like these" suggestions
    - Format recommendations ("Try 9:16 + talking head")
    - Content strategy recommendations
    """
    
    async def get_recommendations_for_goal(
        self,
        db: AsyncSession,
        goal_id: str,
        goal_type: str,
        target_metrics: Dict[str, Any],
        limit: int = 5
    ) -> Dict[str, Any]:
        """
        Get recommendations for a specific goal
        
        Args:
            db: Database session
            goal_id: Goal ID
            goal_type: Goal type (grow_followers, increase_views, etc.)
            target_metrics: Target metrics dict
            limit: Number of recommendations to return
            
        Returns:
            Dict with recommendations
        """
        try:
            recommendations = {
                'goal_id': goal_id,
                'goal_type': goal_type,
                'similar_content': [],
                'format_recommendations': [],
                'content_strategy': [],
                'action_items': []
            }
            
            # 1. Find similar high-performing content
            if goal_type in ['increase_views', 'boost_engagement']:
                similar_content = await self._find_similar_performing_content(
                    db, goal_type, target_metrics, limit
                )
                recommendations['similar_content'] = similar_content
                recommendations['action_items'].append(
                    f"Post {len(similar_content)} more videos like your top performers"
                )
            
            # 2. Format recommendations
            format_recs = await self._get_format_recommendations(
                db, goal_type, target_metrics
            )
            recommendations['format_recommendations'] = format_recs
            
            # 3. Content strategy recommendations
            strategy_recs = await self._get_strategy_recommendations(
                goal_type, target_metrics
            )
            recommendations['content_strategy'] = strategy_recs
            
            return recommendations
            
        except Exception as e:
            logger.error(f"Error generating goal recommendations: {e}")
            raise
    
    async def _find_similar_performing_content(
        self,
        db: AsyncSession,
        goal_type: str,
        target_metrics: Dict[str, Any],
        limit: int
    ) -> List[Dict[str, Any]]:
        """
        Find content similar to top performers
        
        Returns videos/posts that performed well and match goal criteria
        """
        try:
            # Determine which metric to optimize for
            if goal_type == 'increase_views':
                metric = 'views'
                order_by = 'views_count DESC'
            elif goal_type == 'boost_engagement':
                metric = 'engagement_rate'
                order_by = 'engagement_rate DESC'
            else:
                metric = 'views'
                order_by = 'views_count DESC'
            
            # Query top performing content
            query = text(f"""
                SELECT 
                    smp.id,
                    smp.platform,
                    smp.media_type,
                    smp.caption,
                    smp.thumbnail_url,
                    sma.views_count,
                    sma.likes_count,
                    sma.comments_count,
                    sma.engagement_rate,
                    smp.posted_at
                FROM social_media_posts smp
                JOIN social_media_post_analytics sma ON smp.id = sma.post_id
                WHERE sma.snapshot_date = (
                    SELECT MAX(snapshot_date)
                    FROM social_media_post_analytics
                    WHERE post_id = smp.id
                )
                AND sma.{metric} > 0
                ORDER BY {order_by}
                LIMIT :limit
            """)
            
            result = await db.execute(query, {'limit': limit})
            rows = list(result.fetchall())  # Convert to list immediately
            
            return [
                {
                    'post_id': row.id,
                    'platform': row.platform,
                    'media_type': row.media_type,
                    'caption': row.caption[:100] if row.caption else '',
                    'thumbnail_url': row.thumbnail_url,
                    'metrics': {
                        'views': row.views_count or 0,
                        'likes': row.likes_count or 0,
                        'comments': row.comments_count or 0,
                        'engagement_rate': float(row.engagement_rate) if row.engagement_rate else 0
                    },
                    'posted_at': row.posted_at.isoformat() if row.posted_at else None
                }
                for row in rows
            ]
            
        except Exception as e:
            logger.error(f"Error finding similar content: {e}")
            return []
    
    async def _get_format_recommendations(
        self,
        db: AsyncSession,
        goal_type: str,
        target_metrics: Dict[str, Any]
    ) -> List[Dict[str, Any]]:
        """
        Get format recommendations based on goal and performance data
        
        Returns suggestions like "Try 9:16 + talking head"
        """
        recommendations = []
        
        # Analyze what formats perform best for this goal type
        try:
            query = text("""
                SELECT 
                    smp.media_type,
                    AVG(sma.views_count) as avg_views,
                    AVG(sma.engagement_rate) as avg_engagement,
                    COUNT(*) as post_count
                FROM social_media_posts smp
                JOIN social_media_post_analytics sma ON smp.id = sma.post_id
                WHERE sma.snapshot_date >= CURRENT_DATE - INTERVAL '30 days'
                GROUP BY smp.media_type
                HAVING COUNT(*) >= 3
                ORDER BY avg_engagement DESC
                LIMIT 3
            """)
            
            result = await db.execute(query)
            rows = list(result.fetchall())  # Convert to list immediately
            
            for row in rows:
                if row.media_type:
                    recommendations.append({
                        'format': row.media_type,
                        'reason': f"Your {row.media_type}s average {row.avg_engagement:.1f}% engagement",
                        'performance': {
                            'avg_views': int(row.avg_views) if row.avg_views else 0,
                            'avg_engagement': float(row.avg_engagement) if row.avg_engagement else 0,
                            'post_count': row.post_count
                        }
                    })
        except Exception as e:
            logger.error(f"Error getting format recommendations: {e}")
        
        # Add goal-specific format recommendations
        if goal_type == 'grow_followers':
            recommendations.append({
                'format': '9:16 vertical video + talking head',
                'reason': 'Talking head videos perform 2x better for follower growth',
                'performance': None
            })
        elif goal_type == 'increase_views':
            recommendations.append({
                'format': '9:16 vertical video + hook in first 3 seconds',
                'reason': 'Strong hooks increase view retention by 40%',
                'performance': None
            })
        elif goal_type == 'boost_engagement':
            recommendations.append({
                'format': '9:16 vertical video + captions + CTA',
                'reason': 'Captions and CTAs increase engagement by 60%',
                'performance': None
            })
        
        return recommendations
    
    def _get_strategy_recommendations(
        self,
        goal_type: str,
        target_metrics: Dict[str, Any]
    ) -> List[Dict[str, Any]]:
        """
        Get content strategy recommendations
        
        Returns high-level strategy suggestions
        """
        recommendations = []
        
        if goal_type == 'grow_followers':
            recommendations.extend([
                {
                    'title': 'Post consistently',
                    'description': 'Aim for 3-5 posts per week to maintain visibility',
                    'priority': 'high'
                },
                {
                    'title': 'Engage with comments',
                    'description': 'Respond to comments within 2 hours to boost engagement',
                    'priority': 'medium'
                },
                {
                    'title': 'Use trending hashtags',
                    'description': 'Include 3-5 trending hashtags relevant to your niche',
                    'priority': 'medium'
                }
            ])
        elif goal_type == 'increase_views':
            recommendations.extend([
                {
                    'title': 'Optimize first 3 seconds',
                    'description': 'Hook viewers immediately with a strong opening',
                    'priority': 'high'
                },
                {
                    'title': 'Post at peak times',
                    'description': 'Post when your audience is most active (check analytics)',
                    'priority': 'high'
                },
                {
                    'title': 'Use trending sounds/music',
                    'description': 'Leverage trending audio to increase discoverability',
                    'priority': 'medium'
                }
            ])
        elif goal_type == 'boost_engagement':
            recommendations.extend([
                {
                    'title': 'Ask questions in captions',
                    'description': 'Questions in captions increase comments by 3x',
                    'priority': 'high'
                },
                {
                    'title': 'Use polls and interactive features',
                    'description': 'Interactive content drives 2x more engagement',
                    'priority': 'high'
                },
                {
                    'title': 'Create shareable content',
                    'description': 'Content that provides value gets shared more',
                    'priority': 'medium'
                }
            ])
        
        return recommendations

