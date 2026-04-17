"""Change detection logic for identifying significant format and performance changes."""
from typing import Dict, Any, Optional, Tuple
from datetime import datetime, timezone
import json


class ChangeDetector:
    """Detects significant changes in format classification and performance."""

    # Confidence threshold for format change detection (0-1)
    FORMAT_CONFIDENCE_THRESHOLD = 0.15
    # Performance score threshold for significant change (0-100)
    PERFORMANCE_CHANGE_THRESHOLD = 10.0
    # Minimum engagement change percentage to flag
    ENGAGEMENT_CHANGE_THRESHOLD = 0.20

    def __init__(self):
        self.change_log: Dict[str, list] = {}

    def detect_format_change(
        self, content_id: str,
        previous_format: Dict[str, Any],
        current_format: Dict[str, Any]
    ) -> Tuple[bool, Dict[str, Any]]:
        """Detect if format classification has significantly changed.

        Args:
            content_id: ID of the content
            previous_format: Previous classification result
            current_format: Current classification result

        Returns:
            (has_changed, change_details)
        """
        if not previous_format or not current_format:
            return False, {}

        prev_format = previous_format.get("format")
        curr_format = current_format.get("format")
        prev_confidence = previous_format.get("confidence", 0)
        curr_confidence = current_format.get("confidence", 0)

        # Check if format type changed
        format_changed = prev_format != curr_format

        # Check if confidence significantly decreased
        confidence_drop = prev_confidence - curr_confidence
        confidence_significant = confidence_drop > self.FORMAT_CONFIDENCE_THRESHOLD

        has_changed = format_changed or confidence_significant

        change_details = {
            "content_id": content_id,
            "format_changed": format_changed,
            "previous_format": prev_format,
            "current_format": curr_format,
            "confidence_drop": float(confidence_drop),
            "confidence_significant": confidence_significant,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "change_summary": self._build_change_summary(
                format_changed, confidence_drop
            )
        }

        return has_changed, change_details

    def detect_performance_change(
        self, content_id: str,
        previous_score: Dict[str, Any],
        current_score: Dict[str, Any]
    ) -> Tuple[bool, Dict[str, Any]]:
        """Detect significant changes in performance scores.

        Args:
            content_id: ID of the content
            previous_score: Previous performance scores
            current_score: Current performance scores

        Returns:
            (has_changed, change_details)
        """
        if not previous_score or not current_score:
            return False, {}

        prev_overall = float(previous_score.get("overall_score", 0))
        curr_overall = float(current_score.get("overall_score", 0))

        # Calculate change
        score_change = curr_overall - prev_overall
        score_significant = abs(score_change) > self.PERFORMANCE_CHANGE_THRESHOLD

        # Check individual metric changes
        metric_changes = self._analyze_metric_changes(previous_score, current_score)

        has_changed = score_significant or any(m["significant"] for m in metric_changes)

        change_details = {
            "content_id": content_id,
            "previous_overall_score": prev_overall,
            "current_overall_score": curr_overall,
            "score_change": float(score_change),
            "score_significant": score_significant,
            "metric_changes": metric_changes,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "change_summary": self._build_performance_summary(score_change, metric_changes)
        }

        return has_changed, change_details

    def _analyze_metric_changes(self, previous: Dict, current: Dict) -> list:
        """Analyze changes in individual performance metrics."""
        metrics = ["engagement_score", "retention_score", "conversion_score"]
        changes = []

        for metric in metrics:
            prev_val = float(previous.get(metric, 0))
            curr_val = float(current.get(metric, 0))
            change = curr_val - prev_val

            changes.append({
                "metric": metric,
                "previous": prev_val,
                "current": curr_val,
                "change": float(change),
                "significant": abs(change) > self.PERFORMANCE_CHANGE_THRESHOLD
            })

        return changes

    def _build_change_summary(self, format_changed: bool, confidence_drop: float) -> str:
        """Build human-readable summary of format changes."""
        if format_changed:
            return f"Format classification changed with confidence drop of {confidence_drop:.1%}"
        elif confidence_drop > 0:
            return f"Confidence decreased by {confidence_drop:.1%}"
        else:
            return "No significant format change detected"

    def _build_performance_summary(self, score_change: float, metric_changes: list) -> str:
        """Build human-readable summary of performance changes."""
        if score_change > 0:
            direction = "improved"
        elif score_change < 0:
            direction = "declined"
        else:
            direction = "remained stable"

        return f"Overall score {direction} by {abs(score_change):.1f} points"

    def log_change(self, content_id: str, change_type: str, details: Dict[str, Any]) -> None:
        """Log a detected change."""
        if content_id not in self.change_log:
            self.change_log[content_id] = []

        self.change_log[content_id].append({
            "type": change_type,
            "details": details,
            "logged_at": datetime.now(timezone.utc).isoformat()
        })

    def get_change_history(self, content_id: str) -> list:
        """Get change history for content."""
        return self.change_log.get(content_id, [])

    def clear_change_history(self, content_id: str) -> None:
        """Clear change history for content."""
        if content_id in self.change_log:
            del self.change_log[content_id]


# Singleton instance
_change_detector = None


def get_change_detector() -> ChangeDetector:
    """Get or create change detector singleton."""
    global _change_detector
    if _change_detector is None:
        _change_detector = ChangeDetector()
    return _change_detector
