"""
Hypothesis Engine
=================
Statistical analysis and hypothesis testing.
"""

import math
import logging
from typing import List, Dict, Optional, Tuple
from dataclasses import dataclass

from .models import Hypothesis, HypothesisStatus

logger = logging.getLogger(__name__)


@dataclass
class StatisticalResult:
    """Result of statistical analysis."""
    mean_control: float
    mean_variant: float
    std_control: float
    std_variant: float
    sample_size_control: int
    sample_size_variant: int
    improvement: float
    t_statistic: float
    p_value: float
    is_significant: bool
    confidence_level: float


class HypothesisEngine:
    """
    Statistical engine for hypothesis testing.
    
    Performs:
    - Mean comparison tests
    - Statistical significance calculation
    - Confidence interval estimation
    - Pass/fail determination
    """
    
    def __init__(self, significance_level: float = 0.05):
        self.significance_level = significance_level
    
    def analyze(
        self,
        control_values: List[float],
        variant_values: List[float],
        success_threshold: float = 1.2
    ) -> StatisticalResult:
        """
        Perform statistical analysis on control vs variant.
        
        Args:
            control_values: Metrics from control group
            variant_values: Metrics from variant group
            success_threshold: Required improvement ratio
        
        Returns:
            StatisticalResult with all statistics
        """
        # Calculate means
        mean_control = self._mean(control_values) if control_values else 0
        mean_variant = self._mean(variant_values) if variant_values else 0
        
        # Calculate standard deviations
        std_control = self._std(control_values) if len(control_values) > 1 else 0
        std_variant = self._std(variant_values) if len(variant_values) > 1 else 0
        
        # Sample sizes
        n_control = len(control_values)
        n_variant = len(variant_values)
        
        # Calculate improvement
        if mean_control > 0:
            improvement = mean_variant / mean_control
        else:
            improvement = 1.0 if mean_variant == 0 else float('inf')
        
        # Calculate t-statistic (Welch's t-test)
        t_stat, p_value = self._welch_t_test(
            mean_control, mean_variant,
            std_control, std_variant,
            n_control, n_variant
        )
        
        # Determine significance
        is_significant = p_value < self.significance_level and improvement >= success_threshold
        
        # Calculate confidence level
        confidence = 1 - p_value if p_value < 1 else 0
        
        return StatisticalResult(
            mean_control=mean_control,
            mean_variant=mean_variant,
            std_control=std_control,
            std_variant=std_variant,
            sample_size_control=n_control,
            sample_size_variant=n_variant,
            improvement=improvement,
            t_statistic=t_stat,
            p_value=p_value,
            is_significant=is_significant,
            confidence_level=confidence
        )
    
    def determine_status(
        self,
        result: StatisticalResult,
        min_sample_size: int = 10,
        success_threshold: float = 1.2
    ) -> Tuple[HypothesisStatus, str]:
        """
        Determine hypothesis status from statistical result.
        
        Returns:
            Tuple of (status, reasoning)
        """
        total_samples = result.sample_size_control + result.sample_size_variant
        
        if total_samples < min_sample_size:
            return (
                HypothesisStatus.RUNNING,
                f"Need more data: {total_samples}/{min_sample_size} samples collected"
            )
        
        if result.is_significant and result.improvement >= success_threshold:
            return (
                HypothesisStatus.PASSED,
                f"Significant improvement of {(result.improvement-1)*100:.1f}% "
                f"(p={result.p_value:.4f})"
            )
        
        if result.improvement < 1.0 and result.p_value < self.significance_level:
            return (
                HypothesisStatus.FAILED,
                f"Variant performed worse by {(1-result.improvement)*100:.1f}% "
                f"(p={result.p_value:.4f})"
            )
        
        if result.p_value >= self.significance_level:
            return (
                HypothesisStatus.INCONCLUSIVE,
                f"No significant difference detected (p={result.p_value:.4f})"
            )
        
        return (
            HypothesisStatus.INCONCLUSIVE,
            f"Improvement of {(result.improvement-1)*100:.1f}% below threshold"
        )
    
    def calculate_required_sample_size(
        self,
        expected_effect_size: float = 0.2,
        power: float = 0.8
    ) -> int:
        """
        Calculate required sample size for detecting an effect.
        
        Args:
            expected_effect_size: Expected improvement ratio - 1
            power: Statistical power (default 0.8)
        
        Returns:
            Required sample size per group
        """
        # Simplified calculation
        # Using Cohen's d approximation
        z_alpha = 1.96  # 95% confidence
        z_beta = 0.84   # 80% power
        
        if expected_effect_size <= 0:
            return 100  # Default
        
        n = 2 * ((z_alpha + z_beta) / expected_effect_size) ** 2
        return max(10, int(math.ceil(n)))
    
    def _mean(self, values: List[float]) -> float:
        """Calculate mean."""
        if not values:
            return 0
        return sum(values) / len(values)
    
    def _std(self, values: List[float]) -> float:
        """Calculate standard deviation."""
        if len(values) < 2:
            return 0
        
        mean = self._mean(values)
        variance = sum((x - mean) ** 2 for x in values) / (len(values) - 1)
        return math.sqrt(variance)
    
    def _welch_t_test(
        self,
        mean1: float, mean2: float,
        std1: float, std2: float,
        n1: int, n2: int
    ) -> Tuple[float, float]:
        """
        Perform Welch's t-test.
        
        Returns:
            Tuple of (t_statistic, p_value)
        """
        if n1 == 0 or n2 == 0:
            return 0.0, 1.0
        
        # Pooled standard error
        se1 = (std1 ** 2) / n1 if n1 > 0 else 0
        se2 = (std2 ** 2) / n2 if n2 > 0 else 0
        se = math.sqrt(se1 + se2)
        
        if se == 0:
            return 0.0, 1.0
        
        # T-statistic
        t_stat = (mean2 - mean1) / se
        
        # Degrees of freedom (Welch-Satterthwaite)
        if se1 + se2 > 0:
            df_num = (se1 + se2) ** 2
            df_den = (se1 ** 2) / (n1 - 1) if n1 > 1 else 1
            df_den += (se2 ** 2) / (n2 - 1) if n2 > 1 else 1
            df = df_num / df_den if df_den > 0 else 1
        else:
            df = 1
        
        # Approximate p-value using t-distribution
        # Simplified: using normal approximation for large samples
        p_value = self._t_to_p(abs(t_stat), df)
        
        return t_stat, p_value
    
    def _t_to_p(self, t: float, df: float) -> float:
        """
        Convert t-statistic to p-value.
        
        Uses normal approximation for simplicity.
        """
        # For large df, t-distribution approaches normal
        # Using simple approximation
        if df < 1:
            df = 1
        
        # Approximate using cumulative normal
        z = t / math.sqrt(1 + t**2 / df) if df > 0 else t
        
        # Standard normal CDF approximation
        p = 0.5 * (1 + math.erf(-abs(z) / math.sqrt(2)))
        
        return 2 * p  # Two-tailed
