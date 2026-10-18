"""Repurposing Recommendations Engine - suggests formats for content repurposing."""
from typing import Dict, List, Any
from dataclasses import dataclass


@dataclass
class RepurposingRecommendation:
    format: str
    viability_score: float  # 0-100
    rationale: str


class RepurposingRecommender:
    """Generates repurposing recommendations based on format and performance."""

    # Format compatibility matrix
    COMPATIBILITY = {
        "testimonial": {
            "unboxing": 0.3,  # Low - different narrative
            "demo": 0.5,      # Medium - can demo the product
            "lifestyle": 0.7, # High - testimonial fits lifestyle
            "story": 0.8,     # High - testimonial is a story
            "trend": 0.2      # Low - testimonial doesn't fit trends
        },
        "unboxing": {
            "testimonial": 0.6,  # Medium - unboxing is a testimonial
            "demo": 0.9,         # High - demo follows unboxing
            "lifestyle": 0.5,    # Medium - unboxing is lifestyle
            "story": 0.4,        # Low - unboxing is brief
            "trend": 0.3         # Low - unboxing has shelf life
        },
        "demo": {
            "testimonial": 0.5,  # Medium - demo leads to testimonial
            "unboxing": 0.7,     # High - unboxing is first part of demo
            "lifestyle": 0.4,    # Low - demo is technical
            "story": 0.3,        # Low - demo is procedural
            "trend": 0.2         # Low - demos don't trend
        },
        "lifestyle": {
            "testimonial": 0.8,  # High - lifestyle implies testimonial
            "unboxing": 0.4,     # Low - lifestyle is aspirational
            "demo": 0.3,         # Low - lifestyle hides mechanics
            "story": 0.7,        # High - lifestyle is storytelling
            "trend": 0.5         # Medium - lifestyle can be trendy
        },
        "story": {
            "testimonial": 0.9,  # High - story is ultimate testimonial
            "unboxing": 0.3,     # Low - story isn't about product
            "demo": 0.2,         # Low - story doesn't show mechanics
            "lifestyle": 0.8,    # High - story is lifestyle
            "trend": 0.4         # Low - stories don't trend as easily
        },
        "trend": {
            "testimonial": 0.2,   # Low - trend participation isn't testimonial
            "unboxing": 0.2,      # Low - unboxing isn't trendy
            "demo": 0.1,          # Very low - demo isn't viral
            "lifestyle": 0.6,     # Medium - trends can be lifestyle
            "story": 0.3          # Low - trends fade quickly
        }
    }

    def recommend(self, current_format: str, performance_data: Dict[str, float]) -> List[RepurposingRecommendation]:
        """Generate repurposing recommendations.

        Args:
            current_format: Current format (e.g., 'testimonial')
            performance_data: Engagement/performance metrics (0-100 scale)

        Returns:
            List of repurposing recommendations, ranked by viability
        """
        recommendations = []

        # Get compatibility scores for this format
        compatibility = self.COMPATIBILITY.get(current_format, {})

        for target_format, base_score in compatibility.items():
            # Adjust based on performance
            performance_modifier = self._get_performance_modifier(
                current_format, target_format, performance_data
            )

            viability = (base_score + performance_modifier) * 100
            viability = min(100, max(0, viability))

            rationale = self._generate_rationale(
                current_format, target_format, viability
            )

            recommendations.append(RepurposingRecommendation(
                format=target_format,
                viability_score=viability,
                rationale=rationale
            ))

        # Sort by viability score (descending)
        recommendations.sort(key=lambda x: x.viability_score, reverse=True)

        return recommendations

    def _get_performance_modifier(self, current_format: str, target_format: str,
                                 performance_data: Dict[str, float]) -> float:
        """Adjust viability based on performance metrics."""
        performance_score = performance_data.get("overall_score", 50) / 100.0

        # High-performing content has broader repurposing potential
        if performance_score > 0.8:
            return 0.2  # +20% modifier
        elif performance_score > 0.6:
            return 0.1  # +10% modifier
        elif performance_score < 0.3:
            return -0.15  # -15% modifier (underperforming content)

        return 0  # No modifier for average performance

    def _generate_rationale(self, current_format: str, target_format: str,
                           viability: float) -> str:
        """Generate human-readable rationale for recommendation."""
        if viability > 80:
            return f"Excellent fit - {current_format} content transitions naturally to {target_format} format"
        elif viability > 60:
            return f"Good potential - {current_format} can be adapted for {target_format} with some modification"
        elif viability > 40:
            return f"Possible but requires effort - {current_format} would need significant rework for {target_format}"
        else:
            return f"Low potential - {current_format} doesn't align well with {target_format} format"
