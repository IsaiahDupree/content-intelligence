"""
AI Coach Service for PRD2
Generates strategic insights and derivative plans
"""
from uuid import uuid4
from typing import List, Optional
from datetime import datetime

from models.supabase_models import (
    AICoachInsight, MediaAnalysis, PostingMetrics, Comment,
    DerivativeMediaPlan, DerivativeFormat, Platform, PlanStatus
)

class CoachService:
    """Service to analyze performance and suggest improvements"""

    def generate_insight(
        self, 
        analysis: MediaAnalysis, 
        metrics: PostingMetrics, 
        comments: List[Comment],
        checkpoint: str
    ) -> AICoachInsight:
        """
        Compare Pre vs Post social scores and generate feedback
        """
        # Determine performance gap
        pre_score = analysis.pre_social_score or 0
        post_score = metrics.post_social_score or 0
        gap = post_score - pre_score
        
        what_worked = []
        what_to_change = []
        summary = ""
        
        if post_score > 80:
            summary = "Viral hit! Performance exceeded expectations."
            what_worked.append("High engagement rate")
            if comments and any(c.sentiment_score > 0.5 for c in comments):
                what_worked.append("Positive audience sentiment")
        elif gap < -20:
            summary = "Underperformed relative to AI prediction."
            what_to_change.append("Hook might be misleading")
            what_to_change.append("Check pacing in first 3 seconds")
        else:
            summary = "Performed within expected range."

        # Suggest actions
        next_actions = []
        if post_score > 50:
            next_actions.append({"action": "remix", "priority": "high"})
            
        return AICoachInsight(
            id=uuid4(),
            media_id=analysis.media_id,
            schedule_id=metrics.schedule_id,
            checkpoint=checkpoint,
            summary=summary,
            what_worked=what_worked,
            what_to_change=what_to_change,
            next_actions=next_actions,
            input_snapshot={
                "pre_score": pre_score,
                "post_score": post_score,
                "views": metrics.views
            }
        )

    def generate_derivative_plans(self, insight: AICoachInsight) -> List[DerivativeMediaPlan]:
        """
        Create actionable plans if content is good
        """
        plans = []
        # Logic: If viral, making a 'face_cam' explainer and a 'broll_text' remix
        post_score = insight.input_snapshot.get("post_score", 0)
        
        if post_score > 80:
            # Plan 1: Face Cam
            plans.append(DerivativeMediaPlan(
                formatted_type=DerivativeFormat.FACE_CAM,
                instructions="Discuss why this video went viral.",
                target_platform=Platform.TIKTOK,
                estimated_length_sec=45,
                status=PlanStatus.PLANNED,
                format_type=DerivativeFormat.FACE_CAM
            ))
            
            # Plan 2: B-Roll Remix
            plans.append(DerivativeMediaPlan(
                format_type=DerivativeFormat.BROLL_TEXT,
                instructions="Reuse hook text with new background footage.",
                target_platform=Platform.INSTAGRAM_REELS,
                estimated_length_sec=15,
                status=PlanStatus.PLANNED
            ))
            
        return plans
