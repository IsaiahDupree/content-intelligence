"""
Experiments Scheduler
=====================
Main scheduler service for managing experiments.
"""

import os
import logging
from typing import List, Dict, Optional, Any
from datetime import datetime, timedelta
from uuid import uuid4

from sqlalchemy import create_engine, text

from .models import (
    Experiment,
    Hypothesis,
    ExperimentStatus,
    HypothesisStatus,
    PostOrigin,
    OriginType,
)
from .experiment_agent import ExperimentAgent

logger = logging.getLogger(__name__)

DATABASE_URL = os.getenv("DATABASE_URL", "postgresql://postgres:postgres@127.0.0.1:54322/postgres")


class ExperimentsScheduler:
    """
    Manages the lifecycle of content experiments.
    
    Responsibilities:
    - Create and manage experiments
    - Coordinate with experiment agent
    - Track results and determine pass/fail
    - Identify winners for narrative promotion
    """
    
    def __init__(self):
        self.engine = create_engine(DATABASE_URL)
        self.agent = ExperimentAgent()
    
    async def create_experiment(
        self,
        name: str,
        goal: str,
        description: str = "",
        target_accounts: Optional[List[str]] = None,
        resource_types: Optional[List[str]] = None
    ) -> Experiment:
        """Create a new experiment."""
        experiment = Experiment(
            name=name,
            goal=goal,
            description=description,
            target_accounts=target_accounts or [],
            resource_types=resource_types or ["ugc"],
            status=ExperimentStatus.DRAFT
        )
        
        # Save to database
        with self.engine.connect() as conn:
            conn.execute(text("""
                INSERT INTO experiments (id, name, description, goal, status, 
                    target_accounts, resource_types, type, primary_metric, hypothesis)
                VALUES (:id, :name, :description, :goal, :status, 
                    :target_accounts, :resource_types, :type, :primary_metric, :hypothesis)
            """), {
                "id": experiment.id,
                "name": experiment.name,
                "description": experiment.description,
                "goal": experiment.goal,
                "status": experiment.status.value,
                "target_accounts": experiment.target_accounts,
                "resource_types": experiment.resource_types,
                "type": "content_test",
                "primary_metric": "engagement_rate",
                "hypothesis": experiment.goal
            })
            conn.commit()
        
        logger.info(f"[ExperimentsScheduler] Created experiment: {experiment.id}")
        return experiment
    
    async def add_hypothesis(
        self,
        experiment_id: str,
        hypothesis: Hypothesis
    ) -> Hypothesis:
        """Add a hypothesis to an experiment."""
        hypothesis.experiment_id = experiment_id
        
        with self.engine.connect() as conn:
            conn.execute(text("""
                INSERT INTO hypotheses (id, experiment_id, statement, 
                    independent_variable, dependent_variable, 
                    control_description, variant_description,
                    success_metric, success_threshold, min_sample_size, status)
                VALUES (:id, :experiment_id, :statement, 
                    :independent_variable, :dependent_variable,
                    :control_description, :variant_description,
                    :success_metric, :success_threshold, :min_sample_size, :status)
            """), {
                "id": hypothesis.id,
                "experiment_id": experiment_id,
                "statement": hypothesis.statement,
                "independent_variable": hypothesis.independent_variable,
                "dependent_variable": hypothesis.dependent_variable,
                "control_description": hypothesis.control_description,
                "variant_description": hypothesis.variant_description,
                "success_metric": hypothesis.success_metric,
                "success_threshold": hypothesis.success_threshold,
                "min_sample_size": hypothesis.min_sample_size,
                "status": hypothesis.status.value
            })
            conn.commit()
        
        return hypothesis
    
    async def start_experiment(self, experiment_id: str) -> Experiment:
        """Start an experiment."""
        with self.engine.connect() as conn:
            conn.execute(text("""
                UPDATE experiments 
                SET status = 'active', started_at = NOW()
                WHERE id = :id
            """), {"id": experiment_id})
            
            # Also start all pending hypotheses
            conn.execute(text("""
                UPDATE hypotheses
                SET status = 'running'
                WHERE experiment_id = :id AND status = 'pending'
            """), {"id": experiment_id})
            
            conn.commit()
        
        experiment = await self.get_experiment(experiment_id)
        logger.info(f"[ExperimentsScheduler] Started experiment: {experiment_id}")
        
        return experiment
    
    async def get_experiment(self, experiment_id: str) -> Optional[Experiment]:
        """Get experiment by ID."""
        with self.engine.connect() as conn:
            result = conn.execute(text("""
                SELECT id, name, description, goal, status, started_at, completed_at,
                    success_criteria, target_accounts, resource_types, results, learnings
                FROM experiments WHERE id = :id
            """), {"id": experiment_id}).fetchone()
            
            if not result:
                return None
            
            experiment = Experiment(
                id=str(result[0]),
                name=result[1],
                description=result[2] or "",
                goal=result[3],
                status=ExperimentStatus(result[4]),
                start_date=result[5],
                end_date=result[6],
                success_criteria=result[7] or {},
                target_accounts=result[8] or [],
                resource_types=result[9] or [],
                results=result[10] or {},
                learnings=result[11] or ""
            )
            
            # Load hypotheses
            hyp_result = conn.execute(text("""
                SELECT id, statement, independent_variable, dependent_variable,
                    control_description, variant_description, success_metric,
                    success_threshold, min_sample_size, status, confidence_level,
                    actual_improvement, learnings
                FROM hypotheses WHERE experiment_id = :id
            """), {"id": experiment_id})
            
            for row in hyp_result:
                h = Hypothesis(
                    id=str(row[0]),
                    experiment_id=experiment_id,
                    statement=row[1],
                    independent_variable=row[2] or "",
                    dependent_variable=row[3] or "",
                    control_description=row[4] or "",
                    variant_description=row[5] or "",
                    success_metric=row[6] or "view_count",
                    success_threshold=float(row[7]) if row[7] else 1.2,
                    min_sample_size=row[8] or 10,
                    status=HypothesisStatus(row[9]) if row[9] else HypothesisStatus.PENDING,
                    confidence_level=float(row[10]) if row[10] else 0,
                    actual_improvement=float(row[11]) if row[11] else 0,
                    learnings=row[12] or ""
                )
                experiment.hypotheses.append(h)
        
        return experiment
    
    async def list_experiments(
        self,
        status: Optional[ExperimentStatus] = None,
        limit: int = 20
    ) -> List[Experiment]:
        """List experiments."""
        experiments = []
        
        with self.engine.connect() as conn:
            query = "SELECT id FROM experiments"
            params = {"limit": limit}
            
            if status:
                query += " WHERE status = :status"
                params["status"] = status.value
            
            query += " ORDER BY created_at DESC LIMIT :limit"
            
            result = conn.execute(text(query), params)
            
            for row in result:
                exp = await self.get_experiment(str(row[0]))
                if exp:
                    experiments.append(exp)
        
        return experiments
    
    async def schedule_experiment_posts(
        self,
        experiment_id: str,
        hypothesis_id: str,
        control_video_ids: List[str],
        variant_video_ids: List[str],
        platform: str = "tiktok",
        start_date: Optional[datetime] = None
    ) -> Dict[str, Any]:
        """Schedule posts for an experiment's hypothesis."""
        start = start_date or datetime.now()
        scheduled_control = []
        scheduled_variant = []
        
        with self.engine.connect() as conn:
            # Schedule control posts
            for i, video_id in enumerate(control_video_ids):
                post_id = str(uuid4())
                scheduled_at = start + timedelta(hours=i * 4)  # Spread out
                
                conn.execute(text("""
                    INSERT INTO scheduled_posts (id, video_id, platform, scheduled_at,
                        status, origin_type, experiment_id, hypothesis_id, variant)
                    VALUES (:id, :video_id, :platform, :scheduled_at,
                        'pending', 'experiments', :experiment_id, :hypothesis_id, 'control')
                """), {
                    "id": post_id,
                    "video_id": video_id,
                    "platform": platform,
                    "scheduled_at": scheduled_at,
                    "experiment_id": experiment_id,
                    "hypothesis_id": hypothesis_id
                })
                scheduled_control.append(post_id)
            
            # Schedule variant posts
            for i, video_id in enumerate(variant_video_ids):
                post_id = str(uuid4())
                scheduled_at = start + timedelta(hours=i * 4 + 2)  # Offset from control
                
                conn.execute(text("""
                    INSERT INTO scheduled_posts (id, video_id, platform, scheduled_at,
                        status, origin_type, experiment_id, hypothesis_id, variant)
                    VALUES (:id, :video_id, :platform, :scheduled_at,
                        'pending', 'experiments', :experiment_id, :hypothesis_id, 'variant')
                """), {
                    "id": post_id,
                    "video_id": video_id,
                    "platform": platform,
                    "scheduled_at": scheduled_at,
                    "experiment_id": experiment_id,
                    "hypothesis_id": hypothesis_id
                })
                scheduled_variant.append(post_id)
            
            conn.commit()
        
        return {
            "experiment_id": experiment_id,
            "hypothesis_id": hypothesis_id,
            "control_posts": scheduled_control,
            "variant_posts": scheduled_variant,
            "total_scheduled": len(scheduled_control) + len(scheduled_variant)
        }
    
    async def analyze_hypothesis(self, hypothesis_id: str) -> Hypothesis:
        """Analyze results for a hypothesis."""
        with self.engine.connect() as conn:
            # Get hypothesis
            result = conn.execute(text("""
                SELECT experiment_id, success_metric, success_threshold, min_sample_size
                FROM hypotheses WHERE id = :id
            """), {"id": hypothesis_id}).fetchone()
            
            if not result:
                raise ValueError(f"Hypothesis not found: {hypothesis_id}")
            
            experiment_id = str(result[0])
            success_metric = result[1] or "view_count"
            success_threshold = float(result[2]) if result[2] else 1.2
            min_sample_size = result[3] or 10
            
            # Get control metrics
            control_result = conn.execute(text("""
                SELECT AVG((metrics->>:metric)::float), COUNT(*)
                FROM scheduled_posts
                WHERE hypothesis_id = :hypothesis_id 
                AND variant = 'control'
                AND status = 'posted'
                AND metrics IS NOT NULL
            """), {"hypothesis_id": hypothesis_id, "metric": success_metric}).fetchone()
            
            control_avg = float(control_result[0]) if control_result[0] else 0
            control_count = control_result[1] or 0
            
            # Get variant metrics
            variant_result = conn.execute(text("""
                SELECT AVG((metrics->>:metric)::float), COUNT(*)
                FROM scheduled_posts
                WHERE hypothesis_id = :hypothesis_id 
                AND variant = 'variant'
                AND status = 'posted'
                AND metrics IS NOT NULL
            """), {"hypothesis_id": hypothesis_id, "metric": success_metric}).fetchone()
            
            variant_avg = float(variant_result[0]) if variant_result[0] else 0
            variant_count = variant_result[1] or 0
            
            # Calculate improvement
            if control_avg > 0:
                actual_improvement = variant_avg / control_avg
            else:
                actual_improvement = 1.0
            
            # Determine status
            total_samples = control_count + variant_count
            if total_samples < min_sample_size:
                status = HypothesisStatus.RUNNING
                confidence = total_samples / min_sample_size
            elif actual_improvement >= success_threshold:
                status = HypothesisStatus.PASSED
                confidence = min(0.95, 0.5 + (actual_improvement - 1) * 0.5)
            elif actual_improvement < 1.0:
                status = HypothesisStatus.FAILED
                confidence = 0.8
            else:
                status = HypothesisStatus.INCONCLUSIVE
                confidence = 0.5
            
            # Update hypothesis
            learnings = self._generate_learnings(
                status, actual_improvement, control_avg, variant_avg
            )
            
            conn.execute(text("""
                UPDATE hypotheses
                SET status = :status, confidence_level = :confidence,
                    actual_improvement = :improvement, control_avg = :control_avg,
                    variant_avg = :variant_avg, learnings = :learnings
                WHERE id = :id
            """), {
                "id": hypothesis_id,
                "status": status.value,
                "confidence": confidence,
                "improvement": actual_improvement,
                "control_avg": control_avg,
                "variant_avg": variant_avg,
                "learnings": learnings
            })
            conn.commit()
        
        # Return updated hypothesis
        experiment = await self.get_experiment(experiment_id)
        for h in experiment.hypotheses:
            if h.id == hypothesis_id:
                return h
        
        raise ValueError(f"Hypothesis not found after update: {hypothesis_id}")
    
    def _generate_learnings(
        self,
        status: HypothesisStatus,
        improvement: float,
        control_avg: float,
        variant_avg: float
    ) -> str:
        """Generate learnings text from results."""
        if status == HypothesisStatus.PASSED:
            return f"Hypothesis validated. Variant showed {(improvement-1)*100:.1f}% improvement ({variant_avg:.1f} vs {control_avg:.1f}). Consider applying this pattern more broadly."
        elif status == HypothesisStatus.FAILED:
            return f"Hypothesis rejected. Variant underperformed by {(1-improvement)*100:.1f}% ({variant_avg:.1f} vs {control_avg:.1f}). Avoid this approach."
        elif status == HypothesisStatus.INCONCLUSIVE:
            return f"Results inconclusive. Improvement of {(improvement-1)*100:.1f}% not significant enough. Consider running with larger sample."
        else:
            return "Experiment still running. More data needed."
    
    async def get_analytics_by_origin(self) -> Dict[str, Any]:
        """Get performance analytics by post origin."""
        with self.engine.connect() as conn:
            result = conn.execute(text("""
                SELECT 
                    COALESCE(origin_type, 'user') as origin,
                    COUNT(*) as posts,
                    SUM((metrics->>'views')::int) as total_views,
                    AVG((metrics->>'engagement_rate')::float) as avg_engagement
                FROM scheduled_posts
                WHERE status = 'posted'
                AND metrics IS NOT NULL
                GROUP BY origin_type
            """))
            
            analytics = {}
            for row in result:
                analytics[row[0]] = {
                    "posts": row[1],
                    "total_views": row[2] or 0,
                    "avg_engagement": float(row[3]) if row[3] else 0
                }
        
        return analytics
