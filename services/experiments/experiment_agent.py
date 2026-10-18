"""
Experiment Agent
================
AI-powered agent that plans, executes, and learns from experiments.
"""

import os
import logging
import json
from typing import List, Dict, Optional, Any, Tuple
from dataclasses import dataclass, field
from datetime import datetime
from uuid import uuid4
from enum import Enum

from .models import (
    Experiment,
    Hypothesis,
    ContentPattern,
    ExperimentStatus,
    HypothesisStatus,
    PostOrigin,
    OriginType,
)

logger = logging.getLogger(__name__)


class AgentActionType(str, Enum):
    """Available actions for the experiment agent."""
    
    # Content Discovery
    BROWSE_UGC_LIBRARY = "browse_ugc_library"
    SEARCH_BY_TOPIC = "search_by_topic"
    FILTER_BY_SCORE = "filter_by_score"
    
    # Content Analysis
    ANALYZE_VIDEO_HOOKS = "analyze_video_hooks"
    ANALYZE_PACING = "analyze_pacing"
    ANALYZE_AUDIO = "analyze_audio"
    DETECT_TRENDS = "detect_trends"
    
    # Content Editing
    TRIM_CLIP = "trim_clip"
    ADD_HOOK = "add_hook"
    CHANGE_MUSIC = "change_music"
    ADD_SUBTITLES = "add_subtitles"
    ADJUST_PACING = "adjust_pacing"
    CREATE_THUMBNAIL = "create_thumbnail"
    
    # AI Content Creation
    GENERATE_SCRIPT = "generate_script"
    GENERATE_VOICEOVER = "generate_voiceover"
    GENERATE_B_ROLL = "generate_b_roll"
    REMIX_CONTENT = "remix_content"
    
    # Scheduling
    SCHEDULE_POST = "schedule_post"
    SET_CAPTION = "set_caption"
    SET_HASHTAGS = "set_hashtags"
    TARGET_TIME_SLOT = "target_time_slot"
    
    # Experiment Management
    CREATE_HYPOTHESIS = "create_hypothesis"
    DEFINE_SUCCESS_CRITERIA = "define_success_criteria"
    TAG_EXPERIMENT = "tag_experiment"
    COMPARE_VARIANTS = "compare_variants"
    ANALYZE_RESULTS = "analyze_results"


@dataclass
class AgentAction:
    """An action taken by the experiment agent."""
    id: str = field(default_factory=lambda: str(uuid4()))
    experiment_id: str = ""
    action_type: AgentActionType = AgentActionType.BROWSE_UGC_LIBRARY
    action_params: Dict[str, Any] = field(default_factory=dict)
    
    # Execution
    status: str = "pending"  # pending, executing, completed, failed
    result: Dict[str, Any] = field(default_factory=dict)
    error_message: str = ""
    
    # Reasoning
    reasoning: str = ""
    expected_outcome: str = ""
    
    # Timing
    created_at: datetime = field(default_factory=datetime.now)
    executed_at: Optional[datetime] = None
    completed_at: Optional[datetime] = None
    
    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "experiment_id": self.experiment_id,
            "action_type": self.action_type.value,
            "action_params": self.action_params,
            "status": self.status,
            "result": self.result,
            "error_message": self.error_message,
            "reasoning": self.reasoning,
            "expected_outcome": self.expected_outcome,
            "created_at": self.created_at.isoformat()
        }


class ExperimentAgent:
    """
    AI agent that plans and executes content experiments.
    
    The agent:
    - Creates hypotheses based on goals
    - Selects content and tools to test hypotheses
    - Schedules variants with proper tagging
    - Analyzes results and determines pass/fail
    - Learns patterns for future experiments
    """
    
    def __init__(self, openai_api_key: Optional[str] = None):
        self.openai_api_key = openai_api_key or os.getenv("OPENAI_API_KEY")
        self.action_history: List[AgentAction] = []
    
    async def plan_experiment(
        self,
        goal: str,
        available_resources: Dict[str, Any],
        constraints: Optional[Dict[str, Any]] = None
    ) -> Experiment:
        """
        Plan an experiment to achieve a goal.
        
        Args:
            goal: What we want to learn or achieve
            available_resources: Content, tools, accounts available
            constraints: Budget, time, risk limits
        
        Returns:
            Experiment with hypotheses and execution plan
        """
        experiment = Experiment(
            name=f"Experiment: {goal[:50]}",
            goal=goal,
            status=ExperimentStatus.DRAFT,
            resource_types=available_resources.get("types", ["ugc"])
        )
        
        # Generate hypotheses using AI
        hypotheses = await self._generate_hypotheses(goal, available_resources)
        experiment.hypotheses = hypotheses
        
        # Define success criteria
        experiment.success_criteria = {
            "min_improvement": 0.2,  # 20% improvement
            "min_confidence": 0.8,   # 80% confidence
            "min_sample_size": 10
        }
        
        logger.info(f"[ExperimentAgent] Planned experiment with {len(hypotheses)} hypotheses")
        
        return experiment
    
    async def _generate_hypotheses(
        self,
        goal: str,
        resources: Dict[str, Any]
    ) -> List[Hypothesis]:
        """Generate testable hypotheses for a goal."""
        
        if not self.openai_api_key:
            return self._generate_basic_hypotheses(goal)
        
        try:
            import openai
            client = openai.OpenAI(api_key=self.openai_api_key)
            
            prompt = f"""Generate 3 testable hypotheses for this content experiment goal:

GOAL: {goal}

AVAILABLE RESOURCES:
- Content types: {resources.get('types', ['ugc'])}
- Tools: clip editing, subtitle generation, AI voiceover, thumbnail creation
- Platforms: TikTok, Instagram

For each hypothesis, provide:
1. Statement (what you're testing)
2. Independent variable (what you'll change)
3. Dependent variable (what you'll measure)
4. Control approach (baseline)
5. Variant approach (test)
6. Success metric (views, engagement_rate, watch_time)
7. Success threshold (e.g., 1.3 = 30% improvement)

Return as JSON array:
[
  {{
    "statement": "Videos with question hooks get more views",
    "independent_variable": "hook_type",
    "dependent_variable": "view_count",
    "control_description": "Statement hook",
    "variant_description": "Question hook",
    "success_metric": "view_count",
    "success_threshold": 1.3
  }}
]"""

            response = client.chat.completions.create(
                model="gpt-4o-mini",
                messages=[
                    {"role": "system", "content": "You are an expert at designing content experiments."},
                    {"role": "user", "content": prompt}
                ],
                temperature=0.7,
                max_tokens=1000
            )
            
            result_text = response.choices[0].message.content
            
            # Parse JSON
            if "```json" in result_text:
                result_text = result_text.split("```json")[1].split("```")[0]
            elif "```" in result_text:
                result_text = result_text.split("```")[1].split("```")[0]
            
            hypotheses_data = json.loads(result_text.strip())
            
            hypotheses = []
            for data in hypotheses_data:
                h = Hypothesis(
                    statement=data.get("statement", ""),
                    independent_variable=data.get("independent_variable", ""),
                    dependent_variable=data.get("dependent_variable", ""),
                    control_description=data.get("control_description", ""),
                    variant_description=data.get("variant_description", ""),
                    success_metric=data.get("success_metric", "view_count"),
                    success_threshold=float(data.get("success_threshold", 1.2))
                )
                hypotheses.append(h)
            
            return hypotheses
            
        except Exception as e:
            logger.error(f"AI hypothesis generation failed: {e}")
            return self._generate_basic_hypotheses(goal)
    
    def _generate_basic_hypotheses(self, goal: str) -> List[Hypothesis]:
        """Generate basic hypotheses without AI."""
        return [
            Hypothesis(
                statement="Question hooks increase engagement",
                independent_variable="hook_type",
                dependent_variable="engagement_rate",
                control_description="Statement opening",
                variant_description="Question opening",
                success_metric="engagement_rate",
                success_threshold=1.2
            ),
            Hypothesis(
                statement="Subtitles improve watch time",
                independent_variable="subtitles",
                dependent_variable="avg_watch_time",
                control_description="No subtitles",
                variant_description="Word-level subtitles",
                success_metric="avg_watch_time",
                success_threshold=1.15
            )
        ]
    
    async def select_content_for_experiment(
        self,
        hypothesis: Hypothesis,
        content_pool: List[Dict[str, Any]]
    ) -> Tuple[List[str], List[str]]:
        """
        Select content for control and variant groups.
        
        Returns:
            Tuple of (control_video_ids, variant_video_ids)
        """
        # For A/B testing, we need similar content for both groups
        # Sort by score and alternate assignment
        sorted_content = sorted(
            content_pool,
            key=lambda x: x.get("score", 0),
            reverse=True
        )
        
        control_ids = []
        variant_ids = []
        
        for i, content in enumerate(sorted_content[:20]):  # Top 20
            if i % 2 == 0:
                control_ids.append(content.get("id"))
            else:
                variant_ids.append(content.get("id"))
        
        return control_ids, variant_ids
    
    async def execute_action(
        self,
        action: AgentAction
    ) -> AgentAction:
        """Execute an agent action."""
        action.status = "executing"
        action.executed_at = datetime.now()
        
        try:
            # Route to appropriate handler
            if action.action_type == AgentActionType.BROWSE_UGC_LIBRARY:
                action.result = await self._browse_ugc_library(action.action_params)
            
            elif action.action_type == AgentActionType.SEARCH_BY_TOPIC:
                action.result = await self._search_by_topic(action.action_params)
            
            elif action.action_type == AgentActionType.SCHEDULE_POST:
                action.result = await self._schedule_post(action.action_params)
            
            elif action.action_type == AgentActionType.ANALYZE_RESULTS:
                action.result = await self._analyze_results(action.action_params)
            
            elif action.action_type == AgentActionType.ADD_SUBTITLES:
                action.result = await self._add_subtitles(action.action_params)
            
            elif action.action_type == AgentActionType.TRIM_CLIP:
                action.result = await self._trim_clip(action.action_params)
            
            elif action.action_type == AgentActionType.ADD_HOOK:
                action.result = await self._add_hook(action.action_params)
            
            elif action.action_type == AgentActionType.GENERATE_SCRIPT:
                action.result = await self._generate_script(action.action_params)
            
            elif action.action_type == AgentActionType.DETECT_TRENDS:
                action.result = await self._detect_trends(action.action_params)
            
            elif action.action_type == AgentActionType.CREATE_THUMBNAIL:
                action.result = await self._create_thumbnail(action.action_params)
            
            else:
                # Generic handler for unimplemented actions
                action.result = {"status": "not_implemented", "action": action.action_type.value}
            
            action.status = "completed"
            
        except Exception as e:
            action.status = "failed"
            action.error_message = str(e)
            logger.error(f"Action failed: {action.action_type.value} - {e}")
        
        action.completed_at = datetime.now()
        self.action_history.append(action)
        
        return action
    
    async def _browse_ugc_library(self, params: Dict[str, Any]) -> Dict[str, Any]:
        """Browse UGC library for content."""
        from sqlalchemy import create_engine, text
        
        DATABASE_URL = os.getenv("DATABASE_URL", "postgresql://postgres:postgres@127.0.0.1:54322/postgres")
        engine = create_engine(DATABASE_URL)
        
        min_score = params.get("min_score", 60)
        limit = params.get("limit", 20)
        
        with engine.connect() as conn:
            result = conn.execute(text("""
                SELECT v.id, v.title, va.pre_social_score, va.topics
                FROM videos v
                JOIN video_analysis va ON va.video_id = v.id
                WHERE va.pre_social_score >= :min_score
                ORDER BY va.pre_social_score DESC
                LIMIT :limit
            """), {"min_score": min_score, "limit": limit})
            
            videos = [
                {
                    "id": str(row[0]),
                    "title": row[1],
                    "score": row[2],
                    "topics": row[3]
                }
                for row in result
            ]
        
        return {"videos": videos, "count": len(videos)}
    
    async def _search_by_topic(self, params: Dict[str, Any]) -> Dict[str, Any]:
        """Search content by topic."""
        topic = params.get("topic", "")
        # Implementation would search video_analysis.topics
        return {"videos": [], "topic": topic}
    
    async def _schedule_post(self, params: Dict[str, Any]) -> Dict[str, Any]:
        """Schedule a post with experiment tagging."""
        from sqlalchemy import create_engine, text
        
        DATABASE_URL = os.getenv("DATABASE_URL", "postgresql://postgres:postgres@127.0.0.1:54322/postgres")
        engine = create_engine(DATABASE_URL)
        
        video_id = params.get("video_id")
        experiment_id = params.get("experiment_id")
        hypothesis_id = params.get("hypothesis_id")
        variant = params.get("variant", "control")
        platform = params.get("platform", "tiktok")
        scheduled_at = params.get("scheduled_at", datetime.now().isoformat())
        
        post_id = str(uuid4())
        
        with engine.connect() as conn:
            conn.execute(text("""
                INSERT INTO scheduled_posts (id, video_id, platform, scheduled_at, 
                    status, origin_type, experiment_id, hypothesis_id, variant)
                VALUES (:id, :video_id, :platform, :scheduled_at, 
                    'pending', 'experiments', :experiment_id, :hypothesis_id, :variant)
            """), {
                "id": post_id,
                "video_id": video_id,
                "platform": platform,
                "scheduled_at": scheduled_at,
                "experiment_id": experiment_id,
                "hypothesis_id": hypothesis_id,
                "variant": variant
            })
            conn.commit()
        
        return {"post_id": post_id, "scheduled": True}
    
    async def _analyze_results(self, params: Dict[str, Any]) -> Dict[str, Any]:
        """Analyze experiment results with AI insights."""
        experiment_id = params.get("experiment_id")
        hypothesis_id = params.get("hypothesis_id")
        
        if not self.openai_api_key:
            return {
                "success": False,
                "error": "OpenAI API key required for results analysis"
            }
        
        try:
            from sqlalchemy import create_engine, text
            from .hypothesis_engine import HypothesisEngine
            from .scheduler import ExperimentsScheduler
            
            DATABASE_URL = os.getenv("DATABASE_URL", "postgresql://postgres:postgres@127.0.0.1:54322/postgres")
            engine = create_engine(DATABASE_URL)
            
            # Fetch hypothesis data
            with engine.connect() as conn:
                hypothesis_result = conn.execute(text("""
                    SELECT id, statement, independent_variable, dependent_variable,
                           control_description, variant_description, success_metric, success_threshold
                    FROM hypotheses
                    WHERE id = :hypothesis_id
                """), {"hypothesis_id": hypothesis_id})
                
                hyp_row = hypothesis_result.fetchone()
                if not hyp_row:
                    return {"success": False, "error": f"Hypothesis {hypothesis_id} not found"}
                
                hypothesis = {
                    "id": str(hyp_row[0]),
                    "statement": hyp_row[1],
                    "independent_variable": hyp_row[2],
                    "dependent_variable": hyp_row[3],
                    "control_description": hyp_row[4],
                    "variant_description": hyp_row[5],
                    "success_metric": hyp_row[6],
                    "success_threshold": float(hyp_row[7]) if hyp_row[7] else 1.2
                }
                
                # Fetch metrics for control and variant groups
                # Join scheduled_posts with posted_content to get metrics
                # Try both approaches: direct from scheduled_posts or via posted_content
                control_metrics = conn.execute(text("""
                    SELECT COALESCE(pc.views, 0) as views, 
                           COALESCE(pc.likes, 0) as likes, 
                           COALESCE(pc.comments, 0) as comments, 
                           COALESCE(pc.shares, 0) as shares, 
                           COALESCE(pc.saves, 0) as saves, 
                           COALESCE(pc.engagement_rate, 0.0) as engagement_rate
                    FROM scheduled_posts sp
                    LEFT JOIN posted_content pc ON pc.media_id::text = sp.clip_id::text 
                        OR pc.platform_post_id = sp.platform_post_id
                    WHERE sp.experiment_id = :experiment_id 
                      AND sp.hypothesis_id = :hypothesis_id
                      AND (sp.variant = 'control' OR sp.origin_type = 'experiments')
                      AND (pc.views > 0 OR sp.status = 'posted')
                    LIMIT 100
                """), {
                    "experiment_id": experiment_id,
                    "hypothesis_id": hypothesis_id
                }).fetchall()
                
                variant_metrics = conn.execute(text("""
                    SELECT COALESCE(pc.views, 0) as views, 
                           COALESCE(pc.likes, 0) as likes, 
                           COALESCE(pc.comments, 0) as comments, 
                           COALESCE(pc.shares, 0) as shares, 
                           COALESCE(pc.saves, 0) as saves, 
                           COALESCE(pc.engagement_rate, 0.0) as engagement_rate
                    FROM scheduled_posts sp
                    LEFT JOIN posted_content pc ON pc.media_id::text = sp.clip_id::text 
                        OR pc.platform_post_id = sp.platform_post_id
                    WHERE sp.experiment_id = :experiment_id 
                      AND sp.hypothesis_id = :hypothesis_id
                      AND (sp.variant = 'variant' OR sp.origin_type = 'experiments')
                      AND (pc.views > 0 OR sp.status = 'posted')
                    LIMIT 100
                """), {
                    "experiment_id": experiment_id,
                    "hypothesis_id": hypothesis_id
                }).fetchall()
                
                # If no metrics found, try alternative: check experiment_variants table
                if not control_metrics and not variant_metrics:
                    # Fallback: use experiment_variants if it exists
                    try:
                        variant_data = conn.execute(text("""
                            SELECT control_metrics, variant_metrics
                            FROM experiment_variants
                            WHERE hypothesis_id = :hypothesis_id
                            LIMIT 1
                        """), {"hypothesis_id": hypothesis_id}).fetchone()
                        
                        if variant_data and variant_data[0] and variant_data[1]:
                            # Parse JSONB metrics if stored that way
                            import json
                            control_metrics = [[v] for v in json.loads(variant_data[0]).get(hypothesis["success_metric"], [])]
                            variant_metrics = [[v] for v in json.loads(variant_data[1]).get(hypothesis["success_metric"], [])]
                    except Exception:
                        pass
            
            # Extract metric values based on success_metric
            metric_map = {
                "view_count": 0,  # views column index
                "likes": 1,
                "comments": 2,
                "shares": 3,
                "saves": 4,
                "engagement_rate": 5
            }
            
            metric_idx = metric_map.get(hypothesis["success_metric"], 0)
            
            control_values = [float(row[metric_idx]) for row in control_metrics if row[metric_idx] is not None]
            variant_values = [float(row[metric_idx]) for row in variant_metrics if row[metric_idx] is not None]
            
            if not control_values or not variant_values:
                return {
                    "success": False,
                    "error": "Insufficient data: need metrics from both control and variant groups"
                }
            
            # Statistical analysis
            engine_stats = HypothesisEngine()
            stats = engine_stats.analyze(
                control_values=control_values,
                variant_values=variant_values,
                success_threshold=hypothesis["success_threshold"]
            )
            
            # Determine status
            status, reasoning = engine_stats.determine_status(
                stats,
                min_sample_size=10,
                success_threshold=hypothesis["success_threshold"]
            )
            
            # AI interpretation
            import openai
            client = openai.OpenAI(api_key=self.openai_api_key)
            
            prompt = f"""Analyze this A/B test experiment result and provide strategic insights:

HYPOTHESIS: {hypothesis['statement']}
TESTING: {hypothesis['independent_variable']} → {hypothesis['dependent_variable']}

CONTROL: {hypothesis['control_description']}
VARIANT: {hypothesis['variant_description']}

RESULTS:
- Control mean: {stats.mean_control:.2f} ({len(control_values)} samples)
- Variant mean: {stats.mean_variant:.2f} ({len(variant_values)} samples)
- Improvement: {(stats.improvement-1)*100:.1f}%
- P-value: {stats.p_value:.4f}
- Statistically significant: {stats.is_significant}
- Status: {status.value}
- Reasoning: {reasoning}

Provide:
1. **Interpretation**: What do these results mean in plain language?
2. **Why**: Why might this have happened? What factors could explain the outcome?
3. **Actionable Recommendations**: What should we do next?
4. **Next Experiments**: What related hypotheses should we test?
5. **Confidence**: How confident should we be in these findings?

Format as JSON with keys: interpretation, why, recommendations (array), next_experiments (array), confidence_level (0-1).
"""
            
            response = client.chat.completions.create(
                model="gpt-4o-mini",
                messages=[
                    {"role": "system", "content": "You are an expert at analyzing A/B test results and providing actionable insights for content strategy."},
                    {"role": "user", "content": prompt}
                ],
                temperature=0.7,
                response_format={"type": "json_object"}
            )
            
            ai_insights = json.loads(response.choices[0].message.content)
            
            return {
                "success": True,
                "experiment_id": experiment_id,
                "hypothesis_id": hypothesis_id,
                "statistical_result": {
                    "improvement": stats.improvement,
                    "improvement_percent": (stats.improvement - 1) * 100,
                    "p_value": stats.p_value,
                    "is_significant": stats.is_significant,
                    "confidence_level": stats.confidence_level,
                    "mean_control": stats.mean_control,
                    "mean_variant": stats.mean_variant,
                    "sample_size_control": stats.sample_size_control,
                    "sample_size_variant": stats.sample_size_variant
                },
                "status": status.value,
                "reasoning": reasoning,
                "ai_insights": ai_insights
            }
            
        except json.JSONDecodeError as e:
            logger.error(f"Failed to parse AI response: {e}")
            return {"success": False, "error": f"Failed to parse AI insights: {e}"}
        except Exception as e:
            logger.error(f"Results analysis failed: {e}")
            return {"success": False, "error": str(e)}
    
    async def _add_subtitles(self, params: Dict[str, Any]) -> Dict[str, Any]:
        """Add subtitles to a video."""
        from services.clip_extraction import SubtitleGenerator, SubtitleConfig
        
        video_path = params.get("video_path")
        text = params.get("text", "")
        
        config = SubtitleConfig(words_per_subtitle=4)
        generator = SubtitleGenerator(config=config)
        
        success, output_path = await generator.add_subtitles_to_clip(
            video_path=video_path,
            text=text,
            start_time=0,
            end_time=30
        )
        
        return {"success": success, "output_path": output_path}
    
    async def _trim_clip(self, params: Dict[str, Any]) -> Dict[str, Any]:
        """Trim a video clip to specified start/end times."""
        import subprocess
        from pathlib import Path
        
        video_path = params.get("video_path")
        start_time = params.get("start_time", 0)
        end_time = params.get("end_time", 30)
        
        if not video_path or not Path(video_path).exists():
            return {"success": False, "error": "Video file not found"}
        
        output_path = str(Path(video_path).with_suffix(".trimmed.mp4"))
        
        cmd = [
            "ffmpeg", "-y",
            "-ss", str(start_time),
            "-i", video_path,
            "-t", str(end_time - start_time),
            "-c", "copy",
            output_path
        ]
        
        try:
            subprocess.run(cmd, check=True, capture_output=True)
            return {"success": True, "output_path": output_path}
        except subprocess.CalledProcessError as e:
            return {"success": False, "error": str(e)}
    
    async def _add_hook(self, params: Dict[str, Any]) -> Dict[str, Any]:
        """Add a hook/intro to a video using AI-generated text overlay."""
        hook_type = params.get("hook_type", "question")
        topic = params.get("topic", "")
        video_id = params.get("video_id")
        
        # Generate hook text using AI
        hooks = {
            "question": f"Have you ever wondered about {topic}?",
            "statement": f"Here's the truth about {topic}",
            "challenge": f"I bet you didn't know this about {topic}",
            "tease": f"Wait for it... {topic}",
            "pov": f"POV: You just discovered {topic}"
        }
        
        hook_text = hooks.get(hook_type, hooks["question"])
        
        # If OpenAI available, generate better hook
        if self.openai_api_key and topic:
            try:
                import openai
                client = openai.OpenAI(api_key=self.openai_api_key)
                
                response = client.chat.completions.create(
                    model="gpt-4o-mini",
                    messages=[{
                        "role": "user",
                        "content": f"Generate a short, engaging {hook_type} hook for a video about: {topic}. Max 10 words."
                    }],
                    max_tokens=50
                )
                hook_text = response.choices[0].message.content.strip()
            except Exception:
                pass
        
        return {
            "success": True,
            "hook_text": hook_text,
            "hook_type": hook_type,
            "video_id": video_id
        }
    
    async def _generate_script(self, params: Dict[str, Any]) -> Dict[str, Any]:
        """Generate a video script using AI."""
        topic = params.get("topic", "")
        duration = params.get("duration", 30)
        tone = params.get("tone", "educational")
        pillar = params.get("pillar", "Process/How-To")
        
        if not self.openai_api_key:
            # Basic script template
            return {
                "success": True,
                "script": f"Here's how to master {topic}. First, understand the basics. Then, practice consistently. Finally, apply what you've learned.",
                "word_count": 20,
                "estimated_duration": 8
            }
        
        try:
            import openai
            client = openai.OpenAI(api_key=self.openai_api_key)
            
            prompt = f"""Generate a {duration}-second video script about: {topic}

Tone: {tone}
Content Pillar: {pillar}
Target: Short-form vertical video (TikTok/Reels)

Requirements:
- Hook in first 2 seconds
- Clear value delivery
- Strong CTA at end
- Approximately {duration * 2.5} words

Return ONLY the script text, no stage directions."""

            response = client.chat.completions.create(
                model="gpt-4o-mini",
                messages=[{"role": "user", "content": prompt}],
                max_tokens=300
            )
            
            script = response.choices[0].message.content.strip()
            word_count = len(script.split())
            
            return {
                "success": True,
                "script": script,
                "word_count": word_count,
                "estimated_duration": word_count / 2.5
            }
        except Exception as e:
            return {"success": False, "error": str(e)}
    
    async def _detect_trends(self, params: Dict[str, Any]) -> Dict[str, Any]:
        """Detect trending topics and formats using AI analysis of recent content."""
        platform = params.get("platform", "tiktok")
        category = params.get("category", "general")
        
        if not self.openai_api_key:
            return {
                "success": False,
                "error": "OpenAI API key required for trend detection"
            }
        
        try:
            from sqlalchemy import create_engine, text
            
            DATABASE_URL = os.getenv("DATABASE_URL", "postgresql://postgres:postgres@127.0.0.1:54322/postgres")
            engine = create_engine(DATABASE_URL)
            
            # Fetch recent high-performing content
            with engine.connect() as conn:
                recent_content = conn.execute(text("""
                    SELECT 
                        v.title,
                        va.pre_social_score,
                        va.hook_rate_3s,
                        va.avg_percent_viewed,
                        va.topics,
                        va.hooks,
                        pc.views,
                        pc.likes,
                        pc.engagement_rate,
                        pc.posted_at
                    FROM videos v
                    JOIN video_analysis va ON va.video_id = v.id
                    LEFT JOIN posted_content pc ON pc.media_id = v.id
                    WHERE va.pre_social_score IS NOT NULL
                      AND (pc.posted_at >= NOW() - INTERVAL '30 days' OR pc.posted_at IS NULL)
                    ORDER BY COALESCE(pc.views, va.pre_social_score * 100) DESC
                    LIMIT 50
                """)).fetchall()
            
            if not recent_content:
                return {
                    "success": False,
                    "error": "No recent content available for trend analysis"
                }
            
            # Format content for AI analysis
            content_summary = []
            for row in recent_content[:30]:  # Top 30 for analysis
                title = row[0] or "Untitled"
                score = row[1] or 0
                hook_rate = row[2] or 0
                watch_pct = row[3] or 0
                topics = row[4] or []
                hooks = row[5] or []
                views = row[6] or 0
                engagement = row[7] or 0
                
                content_summary.append({
                    "title": title[:100],
                    "score": f"{score:.1f}%",
                    "hook_rate": f"{hook_rate:.2f}%",
                    "watch_percent": f"{watch_pct:.2f}%",
                    "topics": topics[:5] if topics else [],
                    "hooks": hooks[:3] if hooks else [],
                    "views": views,
                    "engagement": engagement
                })
            
            # Build AI prompt
            prompt = f"""Analyze these recent high-performing {platform} videos and identify trending topics, formats, and patterns:

RECENT TOP PERFORMERS (Last 30 days):
{json.dumps(content_summary, indent=2)}

Identify:
1. **Emerging Topics**: Topics gaining momentum (appearing more frequently in top performers)
2. **Format Trends**: What content formats/styles are working best
3. **Hook Patterns**: Successful opening patterns and styles
4. **Content Angles**: Unique approaches that are resonating
5. **Timing Insights**: Any patterns in posting times or content timing

For each trend, provide:
- topic: The trend name/topic
- momentum: 0-1 score indicating growth (0.8+ = strong upward trend)
- relevance: 0-1 score indicating relevance to this account/category
- evidence: Brief explanation of why this is trending
- opportunity: How to leverage this trend

Return as JSON:
{{
  "trends": [
    {{
      "topic": "...",
      "momentum": 0.85,
      "relevance": 0.9,
      "evidence": "...",
      "opportunity": "..."
    }}
  ]
}}
"""
            
            import openai
            client = openai.OpenAI(api_key=self.openai_api_key)
            response = client.chat.completions.create(
                model="gpt-4o-mini",
                messages=[
                    {"role": "system", "content": "You are an expert at identifying social media trends and content patterns. Analyze performance data to spot emerging trends."},
                    {"role": "user", "content": prompt}
                ],
                temperature=0.7,
                response_format={"type": "json_object"}
            )
            
            result = json.loads(response.choices[0].message.content)
            trends = result.get("trends", [])
            
            # Validate trends
            validated_trends = []
            for trend in trends:
                if not all(k in trend for k in ['topic', 'momentum', 'relevance']):
                    continue
                validated_trends.append({
                    "topic": trend.get("topic", ""),
                    "momentum": float(trend.get("momentum", 0)),
                    "relevance": float(trend.get("relevance", 0)),
                    "evidence": trend.get("evidence", ""),
                    "opportunity": trend.get("opportunity", "")
                })
            
            return {
                "success": True,
                "platform": platform,
                "category": category,
                "trends": validated_trends,
                "analyzed_content_count": len(recent_content)
            }
            
        except json.JSONDecodeError as e:
            logger.error(f"Failed to parse trend detection response: {e}")
            return {
                "success": False,
                "error": f"Failed to parse AI response: {e}"
            }
        except Exception as e:
            logger.error(f"Trend detection failed: {e}")
            return {
                "success": False,
                "error": str(e)
            }
    
    async def _create_thumbnail(self, params: Dict[str, Any]) -> Dict[str, Any]:
        """Create a thumbnail for a video."""
        video_path = params.get("video_path")
        timestamp = params.get("timestamp", 1.0)
        
        if not video_path:
            return {"success": False, "error": "Video path required"}
        
        from pathlib import Path
        import subprocess
        
        output_path = str(Path(video_path).with_suffix(".thumb.jpg"))
        
        cmd = [
            "ffmpeg", "-y",
            "-ss", str(timestamp),
            "-i", video_path,
            "-vframes", "1",
            "-q:v", "2",
            output_path
        ]
        
        try:
            subprocess.run(cmd, check=True, capture_output=True)
            return {"success": True, "thumbnail_path": output_path}
        except subprocess.CalledProcessError as e:
            return {"success": False, "error": str(e)}
    
    def get_available_actions(self) -> List[Dict[str, str]]:
        """Get list of available actions with descriptions."""
        return [
            {"action": a.value, "category": self._get_action_category(a)}
            for a in AgentActionType
        ]
    
    def _get_action_category(self, action: AgentActionType) -> str:
        """Get category for an action type."""
        categories = {
            "discovery": ["browse_ugc_library", "search_by_topic", "filter_by_score"],
            "analysis": ["analyze_video_hooks", "analyze_pacing", "analyze_audio", "detect_trends"],
            "editing": ["trim_clip", "add_hook", "change_music", "add_subtitles", "adjust_pacing", "create_thumbnail"],
            "creation": ["generate_script", "generate_voiceover", "generate_b_roll", "remix_content"],
            "scheduling": ["schedule_post", "set_caption", "set_hashtags", "target_time_slot"],
            "experiment": ["create_hypothesis", "define_success_criteria", "tag_experiment", "compare_variants", "analyze_results"]
        }
        
        for category, actions in categories.items():
            if action.value in actions:
                return category
        return "other"
