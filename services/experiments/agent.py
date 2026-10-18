"""
Experiment Agent (EXP-001)
===========================
AI agent for planning, executing, and analyzing content experiments.

The Experiment Agent:
1. Plans experiments based on goals and historical data
2. Executes experiments by coordinating content creation and publishing
3. Monitors experiment progress and collects results
4. Analyzes results and determines winners
5. Provides insights and recommendations for future experiments

Example experiments:
- Test different hooks in first 3 seconds
- Compare question-based vs statement-based captions
- Test posting times (morning vs evening)
- Compare content formats (talking head vs B-roll)
- Test CTA placements and wording
"""

from typing import Optional, Dict, Any, List
from datetime import datetime, timedelta, timezone
from enum import Enum
from dataclasses import dataclass, field
from uuid import uuid4
from loguru import logger

from .hypothesis import Hypothesis, SuccessCriterion, MetricComparison, HypothesisStatus


class ExperimentType(Enum):
    """Types of experiments"""
    AB_TEST = "ab_test"  # Two variants
    MULTIVARIATE = "multivariate"  # Multiple variants
    SEQUENTIAL = "sequential"  # Test variations over time
    FACTORIAL = "factorial"  # Test multiple factors simultaneously


class ExperimentStatus(Enum):
    """Status of an experiment"""
    PLANNING = "planning"
    READY = "ready"
    RUNNING = "running"
    PAUSED = "paused"
    COMPLETED = "completed"
    CANCELLED = "cancelled"


@dataclass
class ExperimentVariant:
    """A variant in an experiment"""
    variant_id: str
    name: str
    description: str
    config: Dict[str, Any]  # Configuration parameters
    sample_size: int = 0  # How many posts with this variant
    results: Dict[str, float] = field(default_factory=dict)  # Metric results


@dataclass
class Experiment:
    """
    A content experiment

    Tests a hypothesis by creating multiple variants and measuring
    their performance against defined success criteria.
    """
    experiment_id: str
    name: str
    description: str
    experiment_type: ExperimentType
    hypothesis: Hypothesis

    # Variants to test
    variants: List[ExperimentVariant]

    # Experiment parameters
    total_sample_size: int
    allocation_strategy: str = "equal"  # "equal", "weighted", "bandit"

    # Status tracking
    status: ExperimentStatus = ExperimentStatus.PLANNING
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    started_at: Optional[datetime] = None
    completed_at: Optional[datetime] = None

    # Results
    samples_collected: int = 0
    winner_variant_id: Optional[str] = None

    # Metadata
    tags: List[str] = field(default_factory=list)
    notes: str = ""

    def start(self) -> None:
        """Start the experiment"""
        self.status = ExperimentStatus.RUNNING
        self.started_at = datetime.now(timezone.utc)
        self.hypothesis.start_testing()

        logger.success(f"🚀 Started experiment: {self.name}")

    def add_result(self, variant_id: str, metrics: Dict[str, float]) -> None:
        """
        Add results for a variant

        Args:
            variant_id: ID of the variant
            metrics: Dictionary of metric_name -> value
        """
        for variant in self.variants:
            if variant.variant_id == variant_id:
                variant.sample_size += 1
                for metric_name, value in metrics.items():
                    if metric_name not in variant.results:
                        variant.results[metric_name] = value
                    else:
                        # Running average
                        n = variant.sample_size
                        variant.results[metric_name] = (
                            (variant.results[metric_name] * (n - 1) + value) / n
                        )

                self.samples_collected += 1
                logger.debug(f"📊 Added result for variant {variant.name}")
                break

    def analyze_results(self) -> Dict[str, Any]:
        """
        Analyze experiment results and determine winner

        Returns:
            Analysis dictionary with winner, significance, etc.
        """
        if self.samples_collected < self.hypothesis.min_sample_size:
            return {
                "status": "insufficient_data",
                "samples_collected": self.samples_collected,
                "samples_needed": self.hypothesis.min_sample_size
            }

        # Find best performing variant for each metric
        best_variants = {}

        for criterion in self.hypothesis.success_criteria:
            metric_name = criterion.metric_name
            best_variant = None
            best_value = float('-inf')

            for variant in self.variants:
                if metric_name in variant.results:
                    value = variant.results[metric_name]
                    if value > best_value:
                        best_value = value
                        best_variant = variant

            if best_variant:
                best_variants[metric_name] = {
                    "variant_id": best_variant.variant_id,
                    "variant_name": best_variant.name,
                    "value": best_value
                }

        # Determine overall winner (variant that passes most criteria)
        variant_scores = {v.variant_id: 0 for v in self.variants}

        for criterion in self.hypothesis.success_criteria:
            metric_name = criterion.metric_name
            if metric_name in best_variants:
                variant_id = best_variants[metric_name]["variant_id"]
                variant_scores[variant_id] += criterion.weight

        # Winner is variant with highest weighted score
        winner_id = max(variant_scores, key=variant_scores.get)
        self.winner_variant_id = winner_id

        winner_variant = next(v for v in self.variants if v.variant_id == winner_id)

        # Update hypothesis with winner's results
        for metric_name, value in winner_variant.results.items():
            self.hypothesis.add_result(metric_name, value)

        # Evaluate hypothesis
        hypothesis_status = self.hypothesis.evaluate()

        return {
            "status": "complete",
            "winner_variant_id": winner_id,
            "winner_name": winner_variant.name,
            "hypothesis_status": hypothesis_status.value,
            "best_per_metric": best_variants,
            "variant_scores": variant_scores
        }

    def complete(self) -> None:
        """Mark experiment as completed"""
        self.status = ExperimentStatus.COMPLETED
        self.completed_at = datetime.now(timezone.utc)

        analysis = self.analyze_results()

        logger.success(
            f"✅ Experiment completed: {self.name} | "
            f"Winner: {analysis.get('winner_name', 'N/A')}"
        )

    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary"""
        return {
            "experiment_id": self.experiment_id,
            "name": self.name,
            "description": self.description,
            "type": self.experiment_type.value,
            "status": self.status.value,
            "hypothesis": self.hypothesis.to_dict(),
            "variants": [
                {
                    "variant_id": v.variant_id,
                    "name": v.name,
                    "sample_size": v.sample_size,
                    "results": v.results
                }
                for v in self.variants
            ],
            "samples_collected": self.samples_collected,
            "winner_variant_id": self.winner_variant_id,
            "created_at": self.created_at.isoformat(),
            "started_at": self.started_at.isoformat() if self.started_at else None,
            "completed_at": self.completed_at.isoformat() if self.completed_at else None,
        }


class ExperimentAgent:
    """
    AI agent for planning and managing content experiments

    The agent can:
    - Suggest experiments based on goals and past data
    - Create experiment plans with variants
    - Monitor experiment progress
    - Analyze results and determine winners
    - Provide insights for future experiments
    """

    def __init__(self):
        self.experiments: Dict[str, Experiment] = {}
        logger.info("🤖 Experiment Agent initialized")

    def plan_experiment(
        self,
        name: str,
        description: str,
        hypothesis: Hypothesis,
        variants: List[ExperimentVariant],
        experiment_type: ExperimentType = ExperimentType.AB_TEST,
        total_sample_size: int = 100
    ) -> Experiment:
        """
        Plan a new experiment

        Args:
            name: Experiment name
            description: Description
            hypothesis: Hypothesis to test
            variants: List of variants to test
            experiment_type: Type of experiment
            total_sample_size: Total posts/samples to collect

        Returns:
            Created Experiment
        """
        experiment_id = str(uuid4())

        experiment = Experiment(
            experiment_id=experiment_id,
            name=name,
            description=description,
            experiment_type=experiment_type,
            hypothesis=hypothesis,
            variants=variants,
            total_sample_size=total_sample_size
        )

        self.experiments[experiment_id] = experiment

        logger.success(f"📋 Planned experiment: {name}")

        return experiment

    def start_experiment(self, experiment_id: str) -> bool:
        """
        Start an experiment

        Args:
            experiment_id: ID of experiment to start

        Returns:
            True if started successfully
        """
        experiment = self.experiments.get(experiment_id)

        if not experiment:
            logger.error(f"Experiment {experiment_id} not found")
            return False

        if experiment.status != ExperimentStatus.READY:
            logger.warning(f"Experiment {experiment.name} not ready to start")
            return False

        experiment.start()
        return True

    def collect_result(
        self,
        experiment_id: str,
        variant_id: str,
        metrics: Dict[str, float]
    ) -> None:
        """
        Collect a result for an experiment variant

        Args:
            experiment_id: ID of experiment
            variant_id: ID of variant that was used
            metrics: Metrics collected from this post
        """
        experiment = self.experiments.get(experiment_id)

        if not experiment:
            logger.error(f"Experiment {experiment_id} not found")
            return

        experiment.add_result(variant_id, metrics)

        # Check if experiment is complete
        if experiment.samples_collected >= experiment.total_sample_size:
            experiment.complete()

    def analyze_experiment(self, experiment_id: str) -> Dict[str, Any]:
        """
        Analyze an experiment and determine winner

        Args:
            experiment_id: ID of experiment to analyze

        Returns:
            Analysis results
        """
        experiment = self.experiments.get(experiment_id)

        if not experiment:
            return {"error": "Experiment not found"}

        return experiment.analyze_results()

    def get_active_experiments(self) -> List[Experiment]:
        """Get all running experiments"""
        return [
            exp for exp in self.experiments.values()
            if exp.status == ExperimentStatus.RUNNING
        ]

    def suggest_next_experiment(
        self,
        goal: str,
        historical_data: Optional[Dict[str, Any]] = None
    ) -> Dict[str, Any]:
        """
        AI suggests next experiment to run based on goals and data

        Args:
            goal: Business goal (e.g., "increase engagement", "improve conversions")
            historical_data: Past experiment results

        Returns:
            Experiment suggestion
        """
        # In production, use OpenAI to analyze data and suggest experiments
        # For now, return template suggestions

        suggestions = {
            "increase_engagement": {
                "name": "Hook Format Test",
                "description": "Test question-based vs statement-based hooks",
                "hypothesis": "Question-based hooks increase reply rate by 25%",
                "variants": [
                    {"name": "Question Hook", "description": "Start with a question"},
                    {"name": "Statement Hook", "description": "Start with a bold statement"}
                ]
            },
            "improve_conversions": {
                "name": "CTA Placement Test",
                "description": "Test CTA in first comment vs last slide",
                "hypothesis": "CTA in first comment increases click rate by 15%",
                "variants": [
                    {"name": "First Comment CTA", "description": "Link in first comment"},
                    {"name": "Last Slide CTA", "description": "CTA on last video frame"}
                ]
            }
        }

        goal_key = goal.lower().replace(" ", "_")
        suggestion = suggestions.get(goal_key, suggestions["increase_engagement"])

        logger.info(f"💡 Suggested experiment: {suggestion['name']}")

        return suggestion

    def get_summary_stats(self) -> Dict[str, Any]:
        """Get summary statistics across all experiments"""
        total = len(self.experiments)
        by_status = {status: 0 for status in ExperimentStatus}

        for exp in self.experiments.values():
            by_status[exp.status] += 1

        completed = by_status[ExperimentStatus.COMPLETED]
        winners_found = sum(
            1 for exp in self.experiments.values()
            if exp.winner_variant_id is not None
        )

        return {
            "total_experiments": total,
            "by_status": {status.value: count for status, count in by_status.items()},
            "completed": completed,
            "active": by_status[ExperimentStatus.RUNNING],
            "winners_found": winners_found
        }


# Singleton instance
_agent_instance: Optional[ExperimentAgent] = None


def get_experiment_agent() -> ExperimentAgent:
    """Get or create the experiment agent singleton"""
    global _agent_instance
    if _agent_instance is None:
        _agent_instance = ExperimentAgent()
    return _agent_instance
