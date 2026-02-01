"""
Pattern Learner
================
Learns content patterns from successful experiments.
"""

import os
import logging
from typing import List, Dict, Optional, Any
from datetime import datetime
from uuid import uuid4

from sqlalchemy import create_engine, text

from .models import ContentPattern, ContentFramework, HypothesisStatus

logger = logging.getLogger(__name__)

DATABASE_URL = os.getenv("DATABASE_URL", "postgresql://postgres:postgres@127.0.0.1:54322/postgres")


class PatternLearner:
    """
    Learns and evolves content patterns from experiment results.
    
    Responsibilities:
    - Extract patterns from successful hypotheses
    - Track pattern performance over time
    - Generate content frameworks from patterns
    - Provide recommendations based on learned patterns
    """
    
    def __init__(self):
        self.engine = create_engine(DATABASE_URL)
    
    async def extract_patterns_from_experiment(
        self,
        experiment_id: str
    ) -> List[ContentPattern]:
        """
        Extract content patterns from a completed experiment.
        
        Analyzes passed hypotheses to identify reusable patterns.
        """
        patterns = []
        
        with self.engine.connect() as conn:
            # Get passed hypotheses
            result = conn.execute(text("""
                SELECT id, statement, independent_variable, dependent_variable,
                       variant_description, actual_improvement, confidence_level,
                       learnings
                FROM hypotheses
                WHERE experiment_id = :experiment_id
                AND status = 'passed'
            """), {"experiment_id": experiment_id})
            
            for row in result:
                pattern = ContentPattern(
                    pattern_type=self._classify_pattern_type(row[2]),  # independent_variable
                    category=row[2],  # independent_variable as category
                    name=self._generate_pattern_name(row[1]),  # from statement
                    description=row[4],  # variant_description
                    success_rate=float(row[6]) if row[6] else 0.7,  # confidence_level
                    avg_improvement=float(row[5]) if row[5] else 1.0,  # actual_improvement
                    confidence=float(row[6]) if row[6] else 0.5,
                    supporting_experiments=[experiment_id],
                    sample_size=1,
                    when_to_use=f"When targeting {row[3]}",  # dependent_variable
                    when_to_avoid="When brand guidelines conflict",
                    first_discovered=datetime.now()
                )
                
                # Check if similar pattern exists
                existing = await self._find_similar_pattern(pattern)
                if existing:
                    # Update existing pattern
                    await self._update_pattern(existing.id, pattern)
                    patterns.append(existing)
                else:
                    # Save new pattern
                    await self._save_pattern(pattern)
                    patterns.append(pattern)
        
        logger.info(f"[PatternLearner] Extracted {len(patterns)} patterns from experiment {experiment_id}")
        return patterns
    
    def _classify_pattern_type(self, variable: str) -> str:
        """Classify pattern type from variable name."""
        variable_lower = variable.lower() if variable else ""
        
        if any(x in variable_lower for x in ["hook", "opening", "intro"]):
            return "hook"
        elif any(x in variable_lower for x in ["format", "style", "layout"]):
            return "format"
        elif any(x in variable_lower for x in ["time", "schedule", "posting"]):
            return "timing"
        elif any(x in variable_lower for x in ["caption", "text", "copy"]):
            return "caption"
        elif any(x in variable_lower for x in ["audio", "music", "sound"]):
            return "audio"
        elif any(x in variable_lower for x in ["subtitle", "text overlay"]):
            return "subtitle"
        else:
            return "general"
    
    def _generate_pattern_name(self, statement: str) -> str:
        """Generate a concise pattern name from hypothesis statement."""
        # Extract key action from statement
        words = statement.split()[:6]
        return " ".join(words) + "..." if len(words) >= 6 else statement
    
    async def _find_similar_pattern(self, pattern: ContentPattern) -> Optional[ContentPattern]:
        """Find an existing similar pattern."""
        with self.engine.connect() as conn:
            result = conn.execute(text("""
                SELECT id, pattern_type, category, name, description,
                       success_rate, avg_improvement, confidence,
                       supporting_experiments, sample_size
                FROM content_patterns
                WHERE pattern_type = :type
                AND category = :category
                AND is_active = TRUE
                LIMIT 1
            """), {
                "type": pattern.pattern_type,
                "category": pattern.category
            }).fetchone()
            
            if result:
                return ContentPattern(
                    id=str(result[0]),
                    pattern_type=result[1],
                    category=result[2],
                    name=result[3],
                    description=result[4] or "",
                    success_rate=float(result[5]) if result[5] else 0,
                    avg_improvement=float(result[6]) if result[6] else 1.0,
                    confidence=float(result[7]) if result[7] else 0.5,
                    supporting_experiments=result[8] or [],
                    sample_size=result[9] or 0
                )
        
        return None
    
    async def _save_pattern(self, pattern: ContentPattern):
        """Save a new pattern to the database."""
        with self.engine.connect() as conn:
            conn.execute(text("""
                INSERT INTO content_patterns (
                    id, pattern_type, category, name, description,
                    success_rate, avg_improvement, confidence,
                    supporting_experiments, sample_size,
                    when_to_use, when_to_avoid
                ) VALUES (
                    :id, :type, :category, :name, :description,
                    :success_rate, :improvement, :confidence,
                    :experiments, :sample_size,
                    :when_use, :when_avoid
                )
            """), {
                "id": pattern.id,
                "type": pattern.pattern_type,
                "category": pattern.category,
                "name": pattern.name,
                "description": pattern.description,
                "success_rate": pattern.success_rate,
                "improvement": pattern.avg_improvement,
                "confidence": pattern.confidence,
                "experiments": pattern.supporting_experiments,
                "sample_size": pattern.sample_size,
                "when_use": pattern.when_to_use,
                "when_avoid": pattern.when_to_avoid
            })
            conn.commit()
    
    async def _update_pattern(self, pattern_id: str, new_data: ContentPattern):
        """Update an existing pattern with new evidence."""
        with self.engine.connect() as conn:
            # Get current data
            result = conn.execute(text("""
                SELECT success_rate, avg_improvement, sample_size, 
                       supporting_experiments, times_applied
                FROM content_patterns WHERE id = :id
            """), {"id": pattern_id}).fetchone()
            
            if result:
                old_rate = float(result[0]) if result[0] else 0.5
                old_improvement = float(result[1]) if result[1] else 1.0
                old_sample = result[2] or 0
                old_experiments = result[3] or []
                
                # Running average
                new_sample = old_sample + 1
                new_rate = (old_rate * old_sample + new_data.success_rate) / new_sample
                new_improvement = (old_improvement * old_sample + new_data.avg_improvement) / new_sample
                
                # Add experiment to list
                experiments = list(set(old_experiments + new_data.supporting_experiments))
                
                conn.execute(text("""
                    UPDATE content_patterns
                    SET success_rate = :rate,
                        avg_improvement = :improvement,
                        sample_size = :sample,
                        supporting_experiments = :experiments,
                        last_validated = NOW(),
                        confidence = :confidence
                    WHERE id = :id
                """), {
                    "id": pattern_id,
                    "rate": new_rate,
                    "improvement": new_improvement,
                    "sample": new_sample,
                    "experiments": experiments,
                    "confidence": min(0.95, new_rate + 0.1 * new_sample)
                })
                conn.commit()
    
    async def get_patterns(
        self,
        pattern_type: Optional[str] = None,
        min_confidence: float = 0.5,
        limit: int = 20
    ) -> List[ContentPattern]:
        """Get learned patterns."""
        patterns = []
        
        with self.engine.connect() as conn:
            query = """
                SELECT id, pattern_type, category, name, description,
                       success_rate, avg_improvement, confidence,
                       when_to_use, when_to_avoid, times_applied,
                       sample_size, best_for_pillars
                FROM content_patterns
                WHERE is_active = TRUE
                AND confidence >= :min_confidence
            """
            params = {"min_confidence": min_confidence, "limit": limit}
            
            if pattern_type:
                query += " AND pattern_type = :type"
                params["type"] = pattern_type
            
            query += " ORDER BY confidence DESC, sample_size DESC LIMIT :limit"
            
            result = conn.execute(text(query), params)
            
            for row in result:
                pattern = ContentPattern(
                    id=str(row[0]),
                    pattern_type=row[1],
                    category=row[2] or "",
                    name=row[3] or "",
                    description=row[4] or "",
                    success_rate=float(row[5]) if row[5] else 0,
                    avg_improvement=float(row[6]) if row[6] else 1.0,
                    confidence=float(row[7]) if row[7] else 0.5,
                    when_to_use=row[8] or "",
                    when_to_avoid=row[9] or "",
                    times_applied=row[10] or 0,
                    sample_size=row[11] or 0,
                    best_for_pillars=row[12] or []
                )
                patterns.append(pattern)
        
        return patterns
    
    async def generate_framework(
        self,
        name: str,
        pattern_ids: List[str],
        pillars: Optional[List[str]] = None
    ) -> ContentFramework:
        """
        Generate a content framework from multiple patterns.
        """
        patterns = []
        
        with self.engine.connect() as conn:
            for pid in pattern_ids:
                result = conn.execute(text("""
                    SELECT name, description, avg_improvement
                    FROM content_patterns WHERE id = :id
                """), {"id": pid}).fetchone()
                
                if result:
                    patterns.append({
                        "step": result[0],
                        "description": result[1],
                        "impact": float(result[2]) if result[2] else 1.0
                    })
        
        # Calculate aggregate performance
        avg_lift = sum(p.get("impact", 1.0) for p in patterns) / len(patterns) if patterns else 1.0
        
        framework = ContentFramework(
            name=name,
            description=f"Framework combining {len(patterns)} proven patterns",
            structure=patterns,
            pillars=pillars or [],
            avg_performance_lift=avg_lift,
            source_patterns=pattern_ids
        )
        
        # Save framework
        with self.engine.connect() as conn:
            conn.execute(text("""
                INSERT INTO content_frameworks (
                    id, name, description, structure, pillars,
                    avg_performance_lift, source_patterns
                ) VALUES (
                    :id, :name, :description, :structure::jsonb, :pillars,
                    :lift, :patterns
                )
            """), {
                "id": framework.id,
                "name": framework.name,
                "description": framework.description,
                "structure": str(framework.structure).replace("'", '"'),
                "pillars": framework.pillars,
                "lift": framework.avg_performance_lift,
                "patterns": framework.source_patterns
            })
            conn.commit()
        
        logger.info(f"[PatternLearner] Created framework: {name}")
        return framework
    
    async def recommend_patterns_for_content(
        self,
        content_type: str,
        pillar: Optional[str] = None,
        target_metric: str = "engagement_rate"
    ) -> List[ContentPattern]:
        """
        Recommend patterns for creating new content.
        """
        # Get patterns that match content type and have good performance
        patterns = await self.get_patterns(
            pattern_type=content_type,
            min_confidence=0.6,
            limit=5
        )
        
        # Filter by pillar if specified
        if pillar:
            patterns = [
                p for p in patterns
                if not p.best_for_pillars or pillar in p.best_for_pillars
            ]
        
        # Sort by improvement for target metric
        patterns.sort(key=lambda p: p.avg_improvement, reverse=True)
        
        return patterns[:3]  # Top 3 recommendations
    
    async def record_pattern_application(
        self,
        pattern_id: str,
        successful: bool
    ):
        """Record when a pattern is applied and whether it succeeded."""
        with self.engine.connect() as conn:
            if successful:
                conn.execute(text("""
                    UPDATE content_patterns
                    SET times_applied = times_applied + 1,
                        times_successful = times_successful + 1,
                        success_rate = (times_successful + 1.0) / (times_applied + 1.0)
                    WHERE id = :id
                """), {"id": pattern_id})
            else:
                conn.execute(text("""
                    UPDATE content_patterns
                    SET times_applied = times_applied + 1,
                        success_rate = times_successful::float / (times_applied + 1.0)
                    WHERE id = :id
                """), {"id": pattern_id})
            conn.commit()
