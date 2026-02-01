"""
AI Recommendation Service
Orchestrates the generation of content and strategy recommendations using
InsightsEngine, PerformanceCorrelator, and LLM integration.
"""
import logging
import uuid
from typing import List, Dict, Any, Optional
from datetime import datetime, timedelta
from sqlalchemy.orm import Session

from services.insights_engine import InsightsEngine
from services.performance_correlator import PerformanceCorrelator
from database.models import ContentInsight

logger = logging.getLogger(__name__)

class AIRecommendationService:
    def __init__(self, db: Session):
        self.db = db
        self.insights_engine = InsightsEngine(db)
        self.performance_correlator = PerformanceCorrelator(db)
        # self.llm_client = ... # Initialize LLM client here (e.g., OpenAI)

    async def generate_daily_recommendations(self, user_id: uuid.UUID) -> List[Dict[str, Any]]:
        """
        Generate a fresh set of recommendations for the user.
        This aggregates insights from various engines and formats them.
        """
        logger.info(f"Generating daily recommendations for user {user_id}")
        
        recommendations = []
        
        # 1. Get content recommendations from InsightsEngine
        content_recs = self.insights_engine.generate_content_recommendations(user_id=user_id, limit=5)
        for rec in content_recs:
            # Enhance with narrative if needed
            rec['narrative'] = await self._generate_narrative(rec)
            recommendations.append(rec)
            
            # Store in DB
            self.insights_engine.store_insight(rec)

        # 2. Get pattern-based recommendations from PerformanceCorrelator
        # (e.g., "Your videos with 'Curiosity' hooks perform 20% better")
        hook_patterns = self.performance_correlator.find_top_performing_patterns("hook", limit=3)
        for pattern in hook_patterns:
            rec = {
                "type": "pattern_insight",
                "priority": "medium",
                "title": f"High performing hook: {pattern['pattern']}",
                "description": f"Hooks of type '{pattern['pattern']}' are driving higher engagement.",
                "metric_impact": f"Avg Score: {pattern['avg_score']:.2f}",
                "confidence_score": 0.8, # Placeholder
                "pattern_data": pattern
            }
            rec['narrative'] = await self._generate_narrative(rec)
            recommendations.append(rec)
            self.insights_engine.store_insight(rec)

        return recommendations

    async def _generate_narrative(self, recommendation_data: Dict[str, Any]) -> str:
        """
        Generate a natural language explanation for the recommendation using LLM.
        """
        # Placeholder for LLM call
        # In a real implementation, this would call OpenAI/Claude with a prompt
        # constructed from recommendation_data.
        
        title = recommendation_data.get('title', 'Recommendation')
        description = recommendation_data.get('description', '')
        action = recommendation_data.get('action', '')
        
        return f"Based on your recent performance, we suggest: {title}. {description}. To improve, try to {action}."

    def get_active_recommendations(self, user_id: uuid.UUID, limit: int = 10) -> List[ContentInsight]:
        """
        Retrieve active, unexpired recommendations from the database.
        """
        return self.db.query(ContentInsight).filter(
            ContentInsight.status == 'active',
            ContentInsight.expires_at > datetime.now()
            # Add user_id filter if ContentInsight has user_id (it should)
        ).order_by(ContentInsight.created_at.desc()).limit(limit).all()

    def action_recommendation(self, insight_id: uuid.UUID, action: str) -> bool:
        """
        Handle user action on a recommendation (accept, reject, dismiss).
        """
        insight = self.db.query(ContentInsight).filter(ContentInsight.id == insight_id).first()
        if not insight:
            return False
            
        if action == 'accept':
            insight.status = 'accepted'
            # Trigger automated action if applicable (e.g., schedule post)
        elif action == 'reject':
            insight.status = 'rejected'
            # Learn from rejection
        elif action == 'dismiss':
            insight.status = 'dismissed'
            
        self.db.commit()
        return True
