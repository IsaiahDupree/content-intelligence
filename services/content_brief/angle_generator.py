"""
Angle Generator
===============
Generates content angles using niche convergence patterns.
"""

import logging
from typing import List, Dict, Any, Optional
from uuid import uuid4

from .models import TrendCluster, BriefAngle

logger = logging.getLogger(__name__)


class AngleGenerator:
    """
    Generates content angles using convergence patterns.
    
    Patterns:
    1. Problem × Tool: "Creators can't stay consistent" × "automation"
    2. Niche × Constraint: "Ecom" × "low budget"
    3. Trend × Framework: Take a trend, wrap it in a repeatable framework
    4. Competitor Output × Better Outcome: "Everyone's making X" → "here's how to make yours feel premium"
    """
    
    # Convergence patterns
    AUDIENCE_ROLES = [
        "creator", "ecom_owner", "dev", "marketer", "student",
        "solopreneur", "founder", "agency"
    ]
    
    INTENTS = [
        "learn", "compare", "buy", "fix", "copy", "avoid",
        "optimize", "scale", "systemize"
    ]
    
    STAKES = [
        "time", "money", "reputation", "speed", "simplicity",
        "quality", "growth", "consistency"
    ]
    
    FORMATS = [
        "myth_bust", "teardown", "tutorial", "checklist",
        "story", "case_study", "framework", "comparison"
    ]
    
    CONVERGENCE_PATTERNS = [
        "Problem × Tool",
        "Niche × Constraint",
        "Trend × Framework",
        "Competitor Output × Better Outcome"
    ]
    
    def __init__(self):
        """Initialize angle generator."""
        pass
    
    def generate_angles(
        self,
        cluster: TrendCluster,
        count: int = 8
    ) -> List[BriefAngle]:
        """
        Generate content angles for a trend cluster.
        
        Args:
            cluster: Trend cluster
            count: Number of angles to generate
        
        Returns:
            List of content angles
        """
        angles = []
        
        # Generate angles using different patterns
        for i in range(count):
            angle = self._generate_angle(cluster, i)
            if angle:
                angles.append(angle)
        
        return angles
    
    def _generate_angle(
        self,
        cluster: TrendCluster,
        index: int
    ) -> Optional[BriefAngle]:
        """Generate a single angle."""
        # Select components based on index (cycle through options)
        audience_role = self.AUDIENCE_ROLES[index % len(self.AUDIENCE_ROLES)]
        intent = self.INTENTS[index % len(self.INTENTS)]
        stakes = self.STAKES[index % len(self.STAKES)]
        format_type = self.FORMATS[index % len(self.FORMATS)]
        pattern = self.CONVERGENCE_PATTERNS[index % len(self.CONVERGENCE_PATTERNS)]
        
        # Generate promise and unique lens
        promise = self._generate_promise(cluster, audience_role, intent, stakes, format_type)
        unique_lens = self._generate_unique_lens(cluster, pattern, format_type)
        
        return BriefAngle(
            angle_id=str(uuid4()),
            cluster_id=cluster.cluster_id,
            audience_role=audience_role,
            intent=intent,
            stakes=stakes,
            format=format_type,
            promise=promise,
            unique_lens=unique_lens,
            convergence_pattern=pattern
        )
    
    def _generate_promise(
        self,
        cluster: TrendCluster,
        audience_role: str,
        intent: str,
        stakes: str,
        format_type: str
    ) -> str:
        """Generate promise for the angle."""
        # Template-based generation
        templates = [
            f"The {stakes}-focused {format_type} {audience_role}s need",
            f"How {audience_role}s can {intent} without sacrificing {stakes}",
            f"The {format_type} that helps {audience_role}s {intent} faster",
        ]
        
        # Use cluster name in promise
        import random
        template = random.choice(templates)
        return f"{template}: {cluster.name}"
    
    def _generate_unique_lens(
        self,
        cluster: TrendCluster,
        pattern: str,
        format_type: str
    ) -> str:
        """Generate unique lens for the angle."""
        lenses = {
            "Problem × Tool": f"Engineering-style solution to {cluster.name}",
            "Niche × Constraint": f"Budget-friendly approach to {cluster.name}",
            "Trend × Framework": f"Repeatable framework for {cluster.name}",
            "Competitor Output × Better Outcome": f"Premium version of {cluster.name}",
        }
        
        return lenses.get(pattern, f"Unique {format_type} approach to {cluster.name}")

