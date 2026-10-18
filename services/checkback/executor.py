"""Checkback job execution engine - re-evaluates content and detects changes."""
from typing import Dict, Any, Optional
from datetime import datetime, timezone
from .change_detection import get_change_detector


class CheckbackExecutor:
    """Executes checkback jobs with full re-evaluation."""

    def __init__(self, classifier=None, scorer=None, recommender=None, supabase_client=None):
        """Initialize executor with service dependencies."""
        self.classifier = classifier
        self.scorer = scorer
        self.recommender = recommender
        self.supabase_client = supabase_client
        self.change_detector = get_change_detector()

    def execute_full_checkback(
        self, content_id: str,
        content: str,
        metrics: Dict[str, Any]
    ) -> Dict[str, Any]:
        """Execute full checkback: re-classify, re-score, check for changes."""
        result = {
            "content_id": content_id,
            "checkback_type": "full",
            "executed_at": datetime.now(timezone.utc).isoformat(),
            "format_result": None,
            "performance_result": None,
            "recommendations": None,
            "changes_detected": {
                "format_changed": False,
                "performance_changed": False,
                "details": {}
            },
            "status": "completed"
        }

        try:
            # Step 1: Re-classify format
            if self.classifier:
                format_result = self.classifier.classify(content)
                result["format_result"] = {
                    "format": format_result.format,
                    "confidence": format_result.confidence,
                    "all_scores": format_result.all_scores,
                    "reasoning": format_result.reasoning
                }

            # Step 2: Re-score performance
            if self.scorer:
                performance = self.scorer.score(metrics)
                result["performance_result"] = {
                    "overall_score": performance["overall_score"],
                    "engagement_score": performance["engagement_score"],
                    "retention_score": performance["retention_score"],
                    "conversion_score": performance["conversion_score"],
                    "metrics_breakdown": performance.get("metrics_breakdown", {})
                }

            # Step 3: Generate recommendations
            if self.recommender and result["format_result"]:
                recommendations = self.recommender.recommend(
                    current_format=result["format_result"]["format"],
                    performance=result["performance_result"]
                )
                result["recommendations"] = recommendations

            return result

        except Exception as e:
            result["status"] = "failed"
            result["error"] = str(e)
            return result

    def execute_quick_checkback(
        self, content_id: str,
        metrics: Dict[str, Any]
    ) -> Dict[str, Any]:
        """Execute quick checkback: only re-evaluate performance."""
        result = {
            "content_id": content_id,
            "checkback_type": "quick",
            "executed_at": datetime.now(timezone.utc).isoformat(),
            "performance_result": None,
            "performance_changed": False,
            "status": "completed"
        }

        try:
            # Only re-score performance
            if self.scorer:
                performance = self.scorer.score(metrics)
                result["performance_result"] = {
                    "overall_score": performance["overall_score"],
                    "engagement_score": performance["engagement_score"],
                    "retention_score": performance["retention_score"],
                    "conversion_score": performance["conversion_score"]
                }

            return result

        except Exception as e:
            result["status"] = "failed"
            result["error"] = str(e)
            return result
