"""
Hypothesis Framework (EXP-002)
===============================
Define and track testable hypotheses for content experiments.

A hypothesis states:
- What we believe will happen
- Success criteria (metrics and thresholds)
- Required sample size for statistical significance
- Pass/fail determination based on results
"""

from typing import Optional, Dict, Any, List
from datetime import datetime, timezone
from enum import Enum
from dataclasses import dataclass, field
from uuid import uuid4
from loguru import logger


class HypothesisStatus(Enum):
    """Status of a hypothesis"""
    DRAFT = "draft"  # Not yet tested
    TESTING = "testing"  # Currently being tested
    PASSED = "passed"  # Hypothesis confirmed
    FAILED = "failed"  # Hypothesis rejected
    INCONCLUSIVE = "inconclusive"  # Not enough data or mixed results


class MetricComparison(Enum):
    """How to compare metric against threshold"""
    GREATER_THAN = "greater_than"
    LESS_THAN = "less_than"
    EQUAL_TO = "equal_to"
    INCREASE_BY = "increase_by"  # % increase
    DECREASE_BY = "decrease_by"  # % decrease


@dataclass
class SuccessCriterion:
    """A single success criterion for a hypothesis"""
    metric_name: str  # e.g., "engagement_rate", "click_rate", "conversion_rate"
    comparison: MetricComparison
    threshold: float
    weight: float = 1.0  # Importance weight (0-1)
    is_required: bool = True  # Must pass for hypothesis to pass

    def evaluate(self, actual_value: float) -> bool:
        """
        Evaluate if criterion is met

        Args:
            actual_value: Actual metric value observed

        Returns:
            True if criterion met, False otherwise
        """
        if self.comparison == MetricComparison.GREATER_THAN:
            return actual_value > self.threshold
        elif self.comparison == MetricComparison.LESS_THAN:
            return actual_value < self.threshold
        elif self.comparison == MetricComparison.EQUAL_TO:
            return abs(actual_value - self.threshold) < 0.01  # Within 1%
        elif self.comparison == MetricComparison.INCREASE_BY:
            # Threshold is baseline, actual_value is new value
            increase_pct = ((actual_value - self.threshold) / self.threshold) * 100
            return increase_pct >= self.threshold
        elif self.comparison == MetricComparison.DECREASE_BY:
            decrease_pct = ((self.threshold - actual_value) / self.threshold) * 100
            return decrease_pct >= self.threshold

        return False


@dataclass
class Hypothesis:
    """
    A testable hypothesis about content performance

    Example:
        "Posts with questions in the first line increase reply rate by 20%"
    """
    hypothesis_id: str
    statement: str  # Human-readable hypothesis
    success_criteria: List[SuccessCriterion]
    min_sample_size: int  # Minimum posts needed for significance
    max_sample_size: int  # Maximum posts to test
    confidence_level: float = 0.95  # Statistical confidence (e.g., 95%)

    # Tracking
    status: HypothesisStatus = HypothesisStatus.DRAFT
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    started_at: Optional[datetime] = None
    completed_at: Optional[datetime] = None

    # Results
    samples_collected: int = 0
    results: Dict[str, float] = field(default_factory=dict)  # metric_name -> value
    criteria_passed: List[str] = field(default_factory=list)
    criteria_failed: List[str] = field(default_factory=list)

    # Metadata
    tags: List[str] = field(default_factory=list)
    notes: str = ""

    def start_testing(self) -> None:
        """Begin testing this hypothesis"""
        self.status = HypothesisStatus.TESTING
        self.started_at = datetime.now(timezone.utc)
        logger.info(f"🔬 Started testing hypothesis: {self.statement}")

    def add_result(self, metric_name: str, value: float) -> None:
        """
        Add a result for a metric

        Args:
            metric_name: Name of the metric
            value: Observed value
        """
        self.results[metric_name] = value
        self.samples_collected += 1

        logger.debug(f"📊 Added result: {metric_name} = {value:.2f}")

    def evaluate(self) -> HypothesisStatus:
        """
        Evaluate if hypothesis passed, failed, or is inconclusive

        Returns:
            New status based on evaluation
        """
        if self.samples_collected < self.min_sample_size:
            logger.warning(
                f"⚠️ Not enough samples ({self.samples_collected}/{self.min_sample_size})"
            )
            return HypothesisStatus.INCONCLUSIVE

        # Evaluate each criterion
        required_passed = 0
        required_total = 0
        optional_passed = 0
        optional_total = 0

        self.criteria_passed = []
        self.criteria_failed = []

        for criterion in self.success_criteria:
            if criterion.metric_name not in self.results:
                logger.warning(f"⚠️ Missing result for {criterion.metric_name}")
                continue

            actual_value = self.results[criterion.metric_name]
            passed = criterion.evaluate(actual_value)

            if passed:
                self.criteria_passed.append(criterion.metric_name)
                if criterion.is_required:
                    required_passed += 1
                else:
                    optional_passed += 1
            else:
                self.criteria_failed.append(criterion.metric_name)

            if criterion.is_required:
                required_total += 1
            else:
                optional_total += 1

        # Determine overall pass/fail
        if required_total > 0 and required_passed < required_total:
            # At least one required criterion failed
            self.status = HypothesisStatus.FAILED
        elif required_passed == required_total and required_total > 0:
            # All required criteria passed
            self.status = HypothesisStatus.PASSED
        else:
            # Not enough data or no required criteria
            self.status = HypothesisStatus.INCONCLUSIVE

        self.completed_at = datetime.now(timezone.utc)

        logger.info(
            f"📊 Hypothesis evaluation complete: {self.status.value} | "
            f"Required: {required_passed}/{required_total} | "
            f"Optional: {optional_passed}/{optional_total}"
        )

        return self.status

    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary"""
        return {
            "hypothesis_id": self.hypothesis_id,
            "statement": self.statement,
            "status": self.status.value,
            "samples_collected": self.samples_collected,
            "min_sample_size": self.min_sample_size,
            "results": self.results,
            "criteria_passed": self.criteria_passed,
            "criteria_failed": self.criteria_failed,
            "created_at": self.created_at.isoformat(),
            "started_at": self.started_at.isoformat() if self.started_at else None,
            "completed_at": self.completed_at.isoformat() if self.completed_at else None,
        }


class HypothesisFramework:
    """
    Framework for managing content experiment hypotheses

    Provides methods to create, track, and evaluate hypotheses
    across multiple content experiments.
    """

    def __init__(self):
        self.hypotheses: Dict[str, Hypothesis] = {}
        logger.info("🔬 Hypothesis Framework initialized")

    def create_hypothesis(
        self,
        statement: str,
        success_criteria: List[SuccessCriterion],
        min_sample_size: int = 30,
        max_sample_size: int = 100,
        tags: Optional[List[str]] = None
    ) -> Hypothesis:
        """
        Create a new hypothesis

        Args:
            statement: Human-readable hypothesis statement
            success_criteria: List of criteria that define success
            min_sample_size: Minimum samples for statistical significance
            max_sample_size: Maximum samples to collect
            tags: Optional tags for categorization

        Returns:
            Created Hypothesis
        """
        hypothesis_id = str(uuid4())

        hypothesis = Hypothesis(
            hypothesis_id=hypothesis_id,
            statement=statement,
            success_criteria=success_criteria,
            min_sample_size=min_sample_size,
            max_sample_size=max_sample_size,
            tags=tags or []
        )

        self.hypotheses[hypothesis_id] = hypothesis

        logger.success(f"✅ Created hypothesis: {statement}")

        return hypothesis

    def get_hypothesis(self, hypothesis_id: str) -> Optional[Hypothesis]:
        """Get a hypothesis by ID"""
        return self.hypotheses.get(hypothesis_id)

    def list_hypotheses(
        self,
        status: Optional[HypothesisStatus] = None,
        tags: Optional[List[str]] = None
    ) -> List[Hypothesis]:
        """
        List hypotheses, optionally filtered

        Args:
            status: Filter by status
            tags: Filter by tags (any match)

        Returns:
            List of matching hypotheses
        """
        results = list(self.hypotheses.values())

        if status:
            results = [h for h in results if h.status == status]

        if tags:
            results = [
                h for h in results
                if any(tag in h.tags for tag in tags)
            ]

        return results

    def get_active_hypotheses(self) -> List[Hypothesis]:
        """Get hypotheses currently being tested"""
        return self.list_hypotheses(status=HypothesisStatus.TESTING)

    def get_summary_stats(self) -> Dict[str, Any]:
        """Get summary statistics across all hypotheses"""
        total = len(self.hypotheses)
        by_status = {status: 0 for status in HypothesisStatus}

        for hyp in self.hypotheses.values():
            by_status[hyp.status] += 1

        passed = by_status[HypothesisStatus.PASSED]
        failed = by_status[HypothesisStatus.FAILED]
        success_rate = (passed / (passed + failed) * 100) if (passed + failed) > 0 else 0

        return {
            "total_hypotheses": total,
            "by_status": {status.value: count for status, count in by_status.items()},
            "success_rate": success_rate,
            "active_tests": by_status[HypothesisStatus.TESTING]
        }


# Singleton instance
_framework_instance: Optional[HypothesisFramework] = None


def get_hypothesis_framework() -> HypothesisFramework:
    """Get or create the hypothesis framework singleton"""
    global _framework_instance
    if _framework_instance is None:
        _framework_instance = HypothesisFramework()
    return _framework_instance
