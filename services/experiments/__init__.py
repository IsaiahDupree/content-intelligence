"""
Experiments Scheduler Service
=============================
AI-powered content experimentation and learning system.

Features:
- Hypothesis testing framework
- Post origin tagging (experiments/narrative/user)
- Winner detection and promotion
- Content pattern learning
- Integration with clip extraction and AI tools
"""

from .models import (
    Experiment,
    Hypothesis,
    ContentPattern,
    ContentFramework,
    ExperimentWinner,
    PostOrigin,
    ExperimentStatus,
    HypothesisStatus,
)

from .experiment_agent import (
    ExperimentAgent,
    AgentAction,
    AgentActionType,
)

from .scheduler import ExperimentsScheduler

from .hypothesis_engine import HypothesisEngine

from .winner_detector import WinnerDetector

from .pattern_learner import PatternLearner

from .autonomous_runner import (
    AutonomousExperimentRunner,
    get_runner,
    start_autonomous_runner,
    stop_autonomous_runner
)

__all__ = [
    # Models
    'Experiment',
    'Hypothesis',
    'ContentPattern',
    'ContentFramework',
    'ExperimentWinner',
    'PostOrigin',
    'ExperimentStatus',
    'HypothesisStatus',
    
    # Agent
    'ExperimentAgent',
    'AgentAction',
    'AgentActionType',
    
    # Services
    'ExperimentsScheduler',
    'HypothesisEngine',
    'WinnerDetector',
    'PatternLearner',
    
    # Autonomous Runner
    'AutonomousExperimentRunner',
    'get_runner',
    'start_autonomous_runner',
    'stop_autonomous_runner',
]
