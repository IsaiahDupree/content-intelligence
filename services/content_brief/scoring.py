"""
Brief Scoring System
====================
"Worth Covering" scoring (0-100) for content briefs.
"""

import logging
from typing import Dict, Any, Optional, List

from .models import BriefScore, TrendCard, TrendCluster, BriefAngle

logger = logging.getLogger(__name__)


class BriefScorer:
    """
    Scores content briefs using the "Worth Covering" rubric.
    
    Scoring Formula (0-100):
    - Velocity (0-25): Views/hour growth, shares/saves rate, comment velocity
    - Intent (0-20): "How do I...", "What tool...", "Template?", "Link?", "Price?"
    - Product Fit (0-25): Can you point to service/product/lead magnet?
    - Differentiation (0-15): Can you add unique lens?
    - Production Feasibility (0-15): Can you produce it fast at quality bar?
    
    Threshold: Only publish if Score ≥ 70, OR Score ≥ 60 + strategic tie-in
    """
    
    def __init__(self, product_keywords: Optional[List[str]] = None):
        """
        Initialize scorer.
        
        Args:
            product_keywords: Keywords related to your products/services for product fit scoring
        """
        self.product_keywords = product_keywords or [
            "automation", "dashboard", "content system", "template", "course",
            "app", "tool", "service", "workflow"
        ]
    
    def score_trend_card(self, card: TrendCard) -> BriefScore:
        """
        Score a trend card.
        
        Args:
            card: Trend card to score
        
        Returns:
            BriefScore with breakdown
        """
        velocity = self._score_velocity(card)
        intent = self._score_intent(card)
        product_fit = self._score_product_fit(card)
        differentiation = self._score_differentiation(card)
        production_feasibility = self._score_production_feasibility(card)
        
        total = velocity + intent + product_fit + differentiation + production_feasibility
        
        return BriefScore(
            total=total,
            velocity=velocity,
            intent=intent,
            product_fit=product_fit,
            differentiation=differentiation,
            production_feasibility=production_feasibility
        )
    
    def score_cluster(self, cluster: TrendCluster) -> BriefScore:
        """
        Score a trend cluster (aggregated from multiple trends).
        
        Args:
            cluster: Trend cluster to score
        
        Returns:
            BriefScore with breakdown
        """
        # Aggregate metrics from all trends in cluster
        if not cluster.trends:
            return BriefScore(total=0.0)
        
        # Average velocity across trends
        avg_views_growth = sum(t.views_growth for t in cluster.trends) / len(cluster.trends)
        avg_shares_rate = sum(t.shares_save_rate for t in cluster.trends) / len(cluster.trends)
        avg_comment_rate = sum(t.comment_rate for t in cluster.trends) / len(cluster.trends)
        
        # Aggregate comments and questions
        all_comments = []
        all_questions = []
        for trend in cluster.trends:
            all_comments.extend(trend.top_comments)
            all_questions.extend(trend.repeated_questions)
        
        # Create aggregated card for scoring
        aggregated_card = TrendCard(
            trend_id=cluster.cluster_id,
            trend_type="cluster",
            trend_name=cluster.name,
            platform="multi",
            views_growth=avg_views_growth,
            shares_save_rate=avg_shares_rate,
            comment_rate=avg_comment_rate,
            top_comments=all_comments,
            repeated_questions=all_questions
        )
        
        return self.score_trend_card(aggregated_card)
    
    def score_angle(self, angle: BriefAngle, cluster: TrendCluster) -> BriefScore:
        """
        Score a content angle.
        
        Args:
            angle: Content angle to score
            cluster: Associated trend cluster
        
        Returns:
            BriefScore with breakdown
        """
        # Start with cluster score
        base_score = self.score_cluster(cluster)
        
        # Adjust based on angle characteristics
        # High-intent angles get bonus
        if angle.intent in ["buy", "fix", "compare"]:
            base_score.intent = min(20.0, base_score.intent * 1.2)
        
        # Product-fit angles get bonus
        if angle.convergence_pattern in ["Problem × Tool", "Niche × Constraint"]:
            base_score.product_fit = min(25.0, base_score.product_fit * 1.3)
        
        # Recalculate total
        base_score.total = (
            base_score.velocity +
            base_score.intent +
            base_score.product_fit +
            base_score.differentiation +
            base_score.production_feasibility
        )
        
        return base_score
    
    def _score_velocity(self, card: TrendCard) -> float:
        """Score velocity (0-25)."""
        score = 0.0
        
        # Views/hour growth (0-10)
        if card.views_growth > 100:
            score += 10.0
        elif card.views_growth > 50:
            score += 7.0
        elif card.views_growth > 20:
            score += 4.0
        elif card.views_growth > 5:
            score += 2.0
        
        # Shares/saves rate (0-8)
        if card.shares_save_rate > 0.15:  # 15%+
            score += 8.0
        elif card.shares_save_rate > 0.10:  # 10%+
            score += 5.0
        elif card.shares_save_rate > 0.05:  # 5%+
            score += 3.0
        
        # Comment velocity (0-7)
        if card.comment_rate > 0.10:  # 10%+
            score += 7.0
        elif card.comment_rate > 0.05:  # 5%+
            score += 4.0
        elif card.comment_rate > 0.02:  # 2%+
            score += 2.0
        
        return min(25.0, score)
    
    def _score_intent(self, card: TrendCard) -> float:
        """Score intent signals (0-20)."""
        score = 0.0
        
        # Check comments and questions for intent signals
        all_text = " ".join(card.top_comments + card.repeated_questions).lower()
        
        # High-intent phrases
        high_intent_phrases = [
            "how do i", "how to", "what tool", "what app", "template",
            "link", "price", "cost", "worth it", "best for", "alternative",
            "can you show", "workflow", "tutorial", "guide"
        ]
        
        intent_count = sum(1 for phrase in high_intent_phrases if phrase in all_text)
        
        if intent_count >= 5:
            score = 20.0
        elif intent_count >= 3:
            score = 15.0
        elif intent_count >= 2:
            score = 10.0
        elif intent_count >= 1:
            score = 5.0
        
        return min(20.0, score)
    
    def _score_product_fit(self, card: TrendCard) -> float:
        """Score product fit (0-25)."""
        score = 0.0
        
        # Check if trend relates to your products/services
        all_text = " ".join(card.top_comments + card.repeated_questions).lower()
        
        # Count product keyword matches
        matches = sum(1 for keyword in self.product_keywords if keyword in all_text)
        
        if matches >= 3:
            score = 25.0
        elif matches >= 2:
            score = 18.0
        elif matches >= 1:
            score = 12.0
        
        # Bonus for specific product mentions
        if any(keyword in all_text for keyword in ["automation", "dashboard", "content system"]):
            score = min(25.0, score + 5.0)
        
        return min(25.0, score)
    
    def _score_differentiation(self, card: TrendCard) -> float:
        """Score differentiation potential (0-15)."""
        score = 0.0
        
        # Can you add unique lens?
        # Engineering framing, frameworks, teardown, measurable steps
        
        # Check if trend allows for unique angles
        if card.what_people_stuck_on:
            score += 5.0  # Can provide solutions
        
        if card.what_people_achieve:
            score += 5.0  # Can provide frameworks
        
        # Format allows for unique approach
        if card.format in ["explainer", "tutorial", "teardown"]:
            score += 5.0
        
        return min(15.0, score)
    
    def _score_production_feasibility(self, card: TrendCard) -> float:
        """Score production feasibility (0-15)."""
        score = 0.0
        
        # Can you produce it fast at quality bar?
        # Check format complexity
        if card.format in ["talking_head", "screen_record"]:
            score += 10.0  # Easy to produce
        elif card.format in ["meme_edit", "explainer"]:
            score += 7.0  # Moderate complexity
        elif card.format in ["listicle"]:
            score += 5.0  # More complex
        
        # Check if you have relevant assets/examples
        # For now, assume moderate feasibility
        score += 3.0
        
        return min(15.0, score)
    
    def is_worth_covering(
        self,
        score: BriefScore,
        threshold: float = 70.0,
        strategic_threshold: float = 60.0,
        is_strategic: bool = False
    ) -> bool:
        """
        Determine if brief is worth covering.
        
        Args:
            score: Brief score
            threshold: Standard threshold (default 70)
            strategic_threshold: Strategic threshold (default 60)
            is_strategic: Whether this is strategically important
        
        Returns:
            True if worth covering
        """
        if score.total >= threshold:
            return True
        
        if is_strategic and score.total >= strategic_threshold:
            return True
        
        return False

