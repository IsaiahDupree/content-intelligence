"""
AI Reasoning Engine for Narrative Scheduling

This module provides the core AI logic for:
1. Analyzing available content against narrative goals
2. Classifying videos into pillars
3. Generating reasoned scheduling decisions
4. Creating transparent justifications
"""

import os
import json
import logging
from typing import List, Dict, Optional, Any, Tuple
from datetime import datetime, date, timedelta
from dataclasses import dataclass

from .models import (
    NarrativeGoal,
    NarrativePillar,
    SchedulingConstraints,
    VideoCandidate,
    ScheduledSlot,
    ReasoningStep,
    WeeklyPlan,
    PerformanceMetrics,
    Learning,
)

logger = logging.getLogger(__name__)


class NarrativeReasoningEngine:
    """
    AI Reasoning Engine that generates justified content schedules
    based on narrative goals, pillars, and constraints.
    """
    
    def __init__(self, openai_api_key: Optional[str] = None):
        self.openai_api_key = openai_api_key or os.getenv("OPENAI_API_KEY")
        self.reasoning_chain: List[ReasoningStep] = []
        self.step_counter = 0
    
    def _add_reasoning_step(
        self, 
        thought: str, 
        decision: str, 
        confidence: float = 0.8,
        data: Optional[Dict] = None
    ) -> ReasoningStep:
        """Add a step to the reasoning chain"""
        self.step_counter += 1
        step = ReasoningStep(
            step_number=self.step_counter,
            thought=thought,
            decision=decision,
            confidence=confidence,
            data_referenced=data
        )
        self.reasoning_chain.append(step)
        logger.info(f"[Reasoning Step {self.step_counter}] {thought} -> {decision}")
        return step
    
    async def _generate_ai_reasoning(
        self,
        phase: str,
        context: Dict[str, Any],
        question: str
    ) -> Dict[str, Any]:
        """Generate AI-powered reasoning for a planning phase using real OpenAI."""
        if not self.openai_api_key:
            return {"thought": question, "decision": "Proceed with analysis", "confidence": 0.7}
        
        try:
            from openai import OpenAI
            client = OpenAI(api_key=self.openai_api_key)
            
            context_str = json.dumps(context, indent=2, default=str)
            
            prompt = f"""You are an AI content strategist reasoning through a {phase} decision.

CONTEXT:
{context_str}

QUESTION: {question}

Think through this step and provide your reasoning. Be specific and reference the data.

Respond in JSON:
{{
    "thought": "Your detailed thought process (1-2 sentences)",
    "decision": "The specific action you're taking",
    "confidence": 0.85,
    "key_insight": "One key insight from this analysis"
}}"""

            response = client.chat.completions.create(
                model="gpt-4o-mini",
                messages=[
                    {"role": "system", "content": "You are a strategic content planner. Be concise and actionable."},
                    {"role": "user", "content": prompt}
                ],
                temperature=0.4,
                response_format={"type": "json_object"}
            )
            
            result = json.loads(response.choices[0].message.content)
            logger.info(f"[AI Reasoning] {phase}: {result.get('thought', '')[:100]}...")
            return result
            
        except Exception as e:
            logger.warning(f"[AI Reasoning] Failed for {phase}: {e}")
            return {"thought": question, "decision": "Proceed with analysis", "confidence": 0.7}
    
    async def generate_weekly_plan(
        self,
        goal: NarrativeGoal,
        pillars: List[NarrativePillar],
        constraints: SchedulingConstraints,
        available_videos: List[VideoCandidate],
        previous_performance: Optional[PerformanceMetrics] = None,
        learnings: Optional[List[Learning]] = None,
    ) -> WeeklyPlan:
        """
        Generate a complete 7-day content plan with full reasoning.
        
        This is the main entry point for the reasoning engine.
        """
        self.reasoning_chain = []
        self.step_counter = 0
        
        logger.info(f"[NarrativeEngine] Starting plan generation for goal: {goal.goal_statement[:50]}...")
        
        # Phase 1: Context Analysis - AI-powered reasoning
        phase1_context = {
            "goal_statement": goal.goal_statement,
            "primary_cta": goal.primary_cta,
            "target_audience": goal.target_audience,
            "time_horizon": goal.time_horizon
        }
        phase1_reasoning = await self._generate_ai_reasoning(
            phase="Goal Analysis",
            context=phase1_context,
            question="What is the strategic intent behind this goal and how should it guide content selection?"
        )
        self._add_reasoning_step(
            thought=phase1_reasoning.get("thought", f"Analyzing narrative goal: '{goal.goal_statement}'"),
            decision=phase1_reasoning.get("decision", "Load goal context for planning"),
            confidence=phase1_reasoning.get("confidence", 0.8),
            data={"goal_id": goal.id, "primary_cta": goal.primary_cta, "ai_insight": phase1_reasoning.get("key_insight")}
        )
        
        # Phase 2: Pillar Analysis - AI-powered reasoning
        active_pillars = [p for p in pillars if p.is_active]
        pillar_summary = {p.name: p.target_percentage for p in active_pillars}
        
        phase2_context = {
            "pillars": pillar_summary,
            "goal_statement": goal.goal_statement,
            "previous_performance": previous_performance.to_dict() if previous_performance else None
        }
        phase2_reasoning = await self._generate_ai_reasoning(
            phase="Pillar Strategy",
            context=phase2_context,
            question="Given these pillars and the goal, how should content be distributed for maximum impact?"
        )
        self._add_reasoning_step(
            thought=phase2_reasoning.get("thought", f"Active pillars: {list(pillar_summary.keys())}"),
            decision=phase2_reasoning.get("decision", f"Use {len(active_pillars)} pillars for categorization"),
            confidence=phase2_reasoning.get("confidence", 0.8),
            data={"pillars": pillar_summary, "ai_insight": phase2_reasoning.get("key_insight")}
        )
        
        # Phase 3: Constraint Analysis - AI-powered
        total_slots = self._calculate_total_slots(constraints)
        
        phase3_context = {
            "max_posts_per_day": constraints.max_posts_per_day,
            "min_posts_per_day": constraints.min_posts_per_day,
            "enabled_platforms": constraints.enabled_platforms,
            "min_score": constraints.min_pre_social_score,
            "total_slots": total_slots
        }
        phase3_reasoning = await self._generate_ai_reasoning(
            phase="Constraint Optimization",
            context=phase3_context,
            question="How should we optimize posting frequency and platform distribution within these constraints?"
        )
        self._add_reasoning_step(
            thought=phase3_reasoning.get("thought", f"Constraints: {constraints.max_posts_per_day} max/day"),
            decision=phase3_reasoning.get("decision", f"Planning for {total_slots} total posts"),
            confidence=phase3_reasoning.get("confidence", 0.8),
            data={"total_slots": total_slots, "platforms": constraints.enabled_platforms, "ai_insight": phase3_reasoning.get("key_insight")}
        )
        
        # Phase 4: Previous Performance Analysis (if available) - AI-enhanced
        if previous_performance:
            await self._analyze_previous_performance_with_ai(previous_performance, active_pillars, goal)
        
        # Phase 5: Apply Learnings (if available)
        if learnings:
            self._apply_learnings(learnings)
        
        # Phase 6: Classify Available Content
        classified_videos = await self._classify_videos(available_videos, active_pillars)
        
        # Phase 7: Select Videos - AI-powered
        selected_videos = await self._select_videos_with_ai(
            classified_videos, 
            active_pillars, 
            constraints, 
            total_slots,
            goal,
            previous_performance
        )
        
        # Phase 8: Generate Schedule - AI-optimized
        schedule = await self._generate_schedule_with_ai(
            selected_videos, 
            constraints, 
            active_pillars,
            goal
        )
        
        # Phase 9: Generate Justification
        justification = self._generate_justification(
            goal, 
            schedule, 
            active_pillars,
            previous_performance
        )
        
        # Create the weekly plan
        week_start = date.today()
        week_end = week_start + timedelta(days=6)
        
        plan = WeeklyPlan(
            goal_id=goal.id,
            week_start=week_start,
            week_end=week_end,
            scheduled_slots=schedule,
            reasoning_chain=self.reasoning_chain,
            total_posts=len(schedule),
            pillar_distribution=self._calculate_pillar_distribution(schedule),
            platform_distribution=self._calculate_platform_distribution(schedule),
            justification_summary=justification,
            status="draft"
        )
        
        logger.info(f"[NarrativeEngine] Plan generated: {plan.total_posts} posts, {len(self.reasoning_chain)} reasoning steps")
        
        return plan
    
    def _calculate_total_slots(self, constraints: SchedulingConstraints) -> int:
        """Calculate total posting slots for 7 days"""
        avg_per_day = (constraints.max_posts_per_day + constraints.min_posts_per_day) // 2
        return avg_per_day * 7
    
    async def _analyze_previous_performance_with_ai(
        self, 
        performance: PerformanceMetrics,
        pillars: List[NarrativePillar],
        goal: NarrativeGoal
    ):
        """Analyze previous week's performance using AI for deeper insights"""
        context = {
            "total_views": performance.total_views,
            "avg_engagement_rate": performance.avg_engagement_rate,
            "followers_gained": performance.followers_gained,
            "pillar_performance": performance.pillar_performance,
            "goal_statement": goal.goal_statement,
            "primary_cta": goal.primary_cta
        }
        
        reasoning = await self._generate_ai_reasoning(
            phase="Performance Analysis",
            context=context,
            question="What patterns do you see in last week's performance? Which pillars should we double down on and which need adjustment?"
        )
        
        self._add_reasoning_step(
            thought=reasoning.get("thought", f"Previous week: {performance.total_views} views, {performance.avg_engagement_rate:.1f}% engagement"),
            decision=reasoning.get("decision", "Apply learnings to this week's plan"),
            confidence=reasoning.get("confidence", 0.85),
            data={"previous_performance": performance.to_dict(), "ai_insight": reasoning.get("key_insight")}
        )
    
    def _analyze_previous_performance(
        self, 
        performance: PerformanceMetrics,
        pillars: List[NarrativePillar]
    ):
        """Fallback: Analyze previous week's performance with rule-based logic"""
        avg_engagement = performance.avg_engagement_rate
        
        pillar_perf = performance.pillar_performance
        if pillar_perf:
            sorted_pillars = sorted(
                pillar_perf.items(),
                key=lambda x: x[1].get('avg_engagement', 0),
                reverse=True
            )
            
            if sorted_pillars:
                top_pillar = sorted_pillars[0]
                bottom_pillar = sorted_pillars[-1]
                
                self._add_reasoning_step(
                    thought=f"Previous week: {performance.total_views} views, {avg_engagement:.1f}% avg engagement. Top: {top_pillar[0]}, Bottom: {bottom_pillar[0]}",
                    decision=f"Increase {top_pillar[0]} allocation, review {bottom_pillar[0]} strategy",
                    confidence=0.85,
                    data={"previous_performance": performance.to_dict()}
                )
    
    def _apply_learnings(self, learnings: List[Learning]):
        """Apply accumulated learnings to planning"""
        applicable = [l for l in learnings if not l.applied and l.confidence > 0.7]
        
        if applicable:
            learning_summary = [l.insight for l in applicable[:3]]
            
            self._add_reasoning_step(
                thought=f"Applying {len(applicable)} learnings from previous schedules: {learning_summary}",
                decision="Incorporate learnings into content selection and scheduling",
                confidence=0.9,
                data={"learnings": [l.to_dict() for l in applicable]}
            )
    
    async def _classify_videos(
        self, 
        videos: List[VideoCandidate],
        pillars: List[NarrativePillar]
    ) -> List[VideoCandidate]:
        """Classify videos into pillars using real OpenAI API calls"""
        classified = []
        
        # Use real OpenAI for classification if API key available
        if self.openai_api_key and videos:
            try:
                classified = await self._classify_videos_with_openai(videos, pillars)
            except Exception as e:
                logger.warning(f"[NarrativeEngine] OpenAI classification failed, using fallback: {e}")
                classified = self._classify_videos_fallback(videos, pillars)
        else:
            classified = self._classify_videos_fallback(videos, pillars)
        
        # Log classification summary
        pillar_counts = {}
        for v in classified:
            if v.primary_pillar:
                pillar_counts[v.primary_pillar] = pillar_counts.get(v.primary_pillar, 0) + 1
        
        self._add_reasoning_step(
            thought=f"Classified {len(classified)} videos into pillars using GPT-4: {pillar_counts}",
            decision="Proceed with video selection from AI-classified pool",
            data={"classification_summary": pillar_counts, "method": "openai"}
        )
        
        return classified
    
    async def _classify_videos_with_openai(
        self,
        videos: List[VideoCandidate],
        pillars: List[NarrativePillar]
    ) -> List[VideoCandidate]:
        """Use real OpenAI API to classify videos into pillars"""
        from openai import OpenAI
        client = OpenAI(api_key=self.openai_api_key)
        
        # Build pillar descriptions
        pillar_desc = "\n".join([
            f"- {p.name}: {p.description} (keywords: {', '.join(p.keywords[:5])})"
            for p in pillars
        ])
        
        # Build video summaries (batch for efficiency)
        video_summaries = []
        for i, v in enumerate(videos[:20]):  # Limit to 20 for API efficiency
            summary = f"{i+1}. '{v.title}' - Topics: {', '.join(v.topics or ['unknown'])} - Score: {v.pre_social_score or 0}"
            video_summaries.append(summary)
        
        prompt = f"""Classify these videos into the most appropriate narrative pillar.

## Available Pillars:
{pillar_desc}

## Videos to Classify:
{chr(10).join(video_summaries)}

For each video, respond with a JSON array:
[
  {{"video_index": 1, "pillar": "pillar name", "confidence": 85, "reason": "brief reason"}},
  ...
]

Classify ALL videos. Use the pillar names exactly as shown."""

        response = client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[
                {"role": "system", "content": "You are a content strategist. Classify videos into narrative pillars. Respond only with valid JSON array."},
                {"role": "user", "content": prompt}
            ],
            temperature=0.3,
            response_format={"type": "json_object"}
        )
        
        # Parse response
        try:
            result = json.loads(response.choices[0].message.content)
            classifications = result if isinstance(result, list) else result.get("classifications", [])
            
            # Apply classifications
            for classification in classifications:
                idx = classification.get("video_index", 0) - 1
                if 0 <= idx < len(videos):
                    videos[idx].primary_pillar = classification.get("pillar")
                    videos[idx].pillar_confidence = classification.get("confidence", 70)
                    videos[idx].selection_reason = classification.get("reason", "AI classified")
        except json.JSONDecodeError:
            logger.warning("[NarrativeEngine] Failed to parse OpenAI classification response")
        
        return videos
    
    def _classify_videos_fallback(
        self,
        videos: List[VideoCandidate],
        pillars: List[NarrativePillar]
    ) -> List[VideoCandidate]:
        """Fallback keyword-based classification"""
        for video in videos:
            pillar, confidence = self._match_to_pillar(video, pillars)
            video.primary_pillar = pillar.name if pillar else None
            video.pillar_confidence = confidence
        return videos
    
    def _match_to_pillar(
        self, 
        video: VideoCandidate, 
        pillars: List[NarrativePillar]
    ) -> Tuple[Optional[NarrativePillar], float]:
        """Match a video to the most appropriate pillar"""
        best_pillar = None
        best_score = 0.0
        
        # Combine video metadata for matching
        video_text = " ".join([
            video.title or "",
            video.transcript or "",
            " ".join(video.topics or []),
            " ".join(video.hooks or []),
        ]).lower()
        
        for pillar in pillars:
            score = 0.0
            keyword_matches = 0
            
            for keyword in pillar.keywords:
                if keyword.lower() in video_text:
                    keyword_matches += 1
            
            if pillar.keywords:
                score = keyword_matches / len(pillar.keywords)
            
            if score > best_score:
                best_score = score
                best_pillar = pillar
        
        return best_pillar, min(best_score * 100, 100)
    
    async def _select_videos_with_ai(
        self,
        classified_videos: List[VideoCandidate],
        pillars: List[NarrativePillar],
        constraints: SchedulingConstraints,
        total_slots: int,
        goal: NarrativeGoal,
        previous_performance: Optional[PerformanceMetrics] = None
    ) -> List[VideoCandidate]:
        """AI-powered video selection considering goal alignment, variety, and strategic fit"""
        
        # Filter by minimum score first
        eligible = [v for v in classified_videos if (v.pre_social_score or 0) >= constraints.min_pre_social_score]
        
        if not eligible or not self.openai_api_key:
            return self._select_videos_fallback(classified_videos, pillars, constraints, total_slots, previous_performance)
        
        # Calculate pillar targets
        pillar_targets = {}
        for pillar in pillars:
            target_posts = int((pillar.target_percentage / 100) * total_slots)
            pillar_targets[pillar.name] = max(pillar.min_posts_per_week, min(target_posts, pillar.max_posts_per_week))
        
        try:
            from openai import OpenAI
            client = OpenAI(api_key=self.openai_api_key)
            
            # Build video summaries for AI
            video_summaries = []
            for i, v in enumerate(eligible[:30]):  # Limit for API efficiency
                video_summaries.append({
                    "index": i,
                    "title": v.title[:50] if v.title else "Untitled",
                    "pillar": v.primary_pillar,
                    "score": v.pre_social_score or 0,
                    "topics": v.topics[:3] if v.topics else [],
                    "hooks": v.hooks[:2] if v.hooks else []
                })
            
            prompt = f"""You are a content strategist selecting videos for a 7-day posting schedule.

GOAL: {goal.goal_statement}
TARGET AUDIENCE: {goal.target_audience}
PRIMARY CTA: {goal.primary_cta}

PILLAR TARGETS (posts needed per pillar):
{json.dumps(pillar_targets, indent=2)}

AVAILABLE VIDEOS:
{json.dumps(video_summaries, indent=2)}

Select the best videos to meet pillar targets. Consider:
1. Goal alignment - does this video support the narrative goal?
2. Content variety - avoid similar topics in a row
3. Quality score - prefer higher scores
4. Audience fit - matches target audience interests

Respond in JSON:
{{
    "selections": [
        {{"index": 0, "reason": "Best fit for X pillar because...", "strategic_value": "high/medium/low"}},
        ...
    ],
    "selection_strategy": "Brief explanation of your selection approach"
}}

Select exactly {total_slots} videos total, distributed across pillars."""

            response = client.chat.completions.create(
                model="gpt-4o-mini",
                messages=[
                    {"role": "system", "content": "You are a strategic content planner. Select videos that best serve the narrative goal."},
                    {"role": "user", "content": prompt}
                ],
                temperature=0.3,
                response_format={"type": "json_object"}
            )
            
            result = json.loads(response.choices[0].message.content)
            selections = result.get("selections", [])
            strategy = result.get("selection_strategy", "AI-optimized selection")
            
            # Apply AI selections
            selected = []
            for sel in selections:
                idx = sel.get("index", -1)
                if 0 <= idx < len(eligible):
                    video = eligible[idx]
                    video.is_selected = True
                    video.selection_reason = sel.get("reason", "AI selected")
                    selected.append(video)
            
            # Add reasoning step
            self._add_reasoning_step(
                thought=f"AI selected {len(selected)} videos using strategy: {strategy[:100]}",
                decision="Finalize AI-optimized video selection",
                confidence=0.9,
                data={"selection_count": len(selected), "strategy": strategy}
            )
            
            logger.info(f"[AI Selection] Selected {len(selected)} videos with AI optimization")
            return selected
            
        except Exception as e:
            logger.warning(f"[AI Selection] Failed, using fallback: {e}")
            return self._select_videos_fallback(classified_videos, pillars, constraints, total_slots, previous_performance)
    
    def _select_videos_fallback(
        self,
        classified_videos: List[VideoCandidate],
        pillars: List[NarrativePillar],
        constraints: SchedulingConstraints,
        total_slots: int,
        previous_performance: Optional[PerformanceMetrics] = None
    ) -> List[VideoCandidate]:
        """Fallback: Select videos based on pillar targets and quality scores"""
        selected = []
        
        pillar_targets = {}
        for pillar in pillars:
            target_posts = int((pillar.target_percentage / 100) * total_slots)
            pillar_targets[pillar.name] = max(pillar.min_posts_per_week, min(target_posts, pillar.max_posts_per_week))
        
        self._add_reasoning_step(
            thought=f"Target posts per pillar: {pillar_targets}. Total needed: {sum(pillar_targets.values())}",
            decision="Select top-scoring videos for each pillar",
            data={"pillar_targets": pillar_targets}
        )
        
        eligible = [v for v in classified_videos if (v.pre_social_score or 0) >= constraints.min_pre_social_score]
        
        self._add_reasoning_step(
            thought=f"{len(eligible)}/{len(classified_videos)} videos meet minimum score threshold",
            decision="Proceed with eligible videos only",
            data={"eligible_count": len(eligible)}
        )
        
        pillar_selections = {p.name: [] for p in pillars}
        
        for pillar in pillars:
            pillar_videos = [v for v in eligible if v.primary_pillar == pillar.name]
            pillar_videos.sort(key=lambda v: v.pre_social_score or 0, reverse=True)
            
            target = pillar_targets.get(pillar.name, 0)
            for video in pillar_videos[:target]:
                video.is_selected = True
                video.selection_reason = f"Top scorer in {pillar.name} pillar (score: {video.pre_social_score})"
                pillar_selections[pillar.name].append(video)
                selected.append(video)
        
        selection_summary = {k: len(v) for k, v in pillar_selections.items()}
        
        self._add_reasoning_step(
            thought=f"Selected {len(selected)} videos across pillars: {selection_summary}",
            decision="Finalize video selection for scheduling",
            confidence=0.9,
            data={"selections": selection_summary}
        )
        
        return selected
    
    async def _generate_schedule_with_ai(
        self,
        selected_videos: List[VideoCandidate],
        constraints: SchedulingConstraints,
        pillars: List[NarrativePillar],
        goal: NarrativeGoal
    ) -> List[ScheduledSlot]:
        """AI-optimized schedule generation with intelligent timing and platform assignment"""
        
        if not selected_videos or not self.openai_api_key:
            return self._generate_schedule_fallback(selected_videos, constraints, pillars)
        
        try:
            from openai import OpenAI
            client = OpenAI(api_key=self.openai_api_key)
            
            # Build video info for AI
            video_info = []
            for i, v in enumerate(selected_videos):
                video_info.append({
                    "index": i,
                    "title": v.title[:40] if v.title else "Untitled",
                    "pillar": v.primary_pillar,
                    "score": v.pre_social_score or 0,
                    "topics": v.topics[:2] if v.topics else []
                })
            
            current_date = date.today()
            week_dates = [(current_date + timedelta(days=i)).strftime("%Y-%m-%d") for i in range(7)]
            
            prompt = f"""You are a social media strategist creating an optimal 7-day posting schedule.

GOAL: {goal.goal_statement}
TARGET AUDIENCE: {goal.target_audience}
PLATFORMS: {constraints.enabled_platforms}
MAX POSTS PER DAY: {constraints.max_posts_per_day}

AVAILABLE DATES: {week_dates}

VIDEOS TO SCHEDULE:
{json.dumps(video_info, indent=2)}

Create an optimal schedule considering:
1. Best posting times per platform (TikTok: 12pm, 6pm; Instagram: 9am, 5pm; YouTube: 2pm)
2. Content variety - don't post same pillar back-to-back
3. Audience activity patterns - weekdays vs weekends
4. Platform-specific optimization

Respond in JSON:
{{
    "schedule": [
        {{"video_index": 0, "date": "2025-12-25", "time": "12:00", "platform": "tiktok", "reason": "Peak engagement time"}},
        ...
    ],
    "scheduling_strategy": "Brief explanation of timing decisions"
}}

Schedule ALL {len(selected_videos)} videos across the 7 days."""

            response = client.chat.completions.create(
                model="gpt-4o-mini",
                messages=[
                    {"role": "system", "content": "You are a social media scheduling expert. Optimize for maximum engagement."},
                    {"role": "user", "content": prompt}
                ],
                temperature=0.3,
                response_format={"type": "json_object"}
            )
            
            result = json.loads(response.choices[0].message.content)
            ai_schedule = result.get("schedule", [])
            strategy = result.get("scheduling_strategy", "AI-optimized timing")
            
            # Build schedule from AI response
            schedule = []
            for item in ai_schedule:
                idx = item.get("video_index", -1)
                if 0 <= idx < len(selected_videos):
                    video = selected_videos[idx]
                    
                    # Parse date
                    try:
                        sched_date = datetime.strptime(item.get("date", ""), "%Y-%m-%d").date()
                    except Exception:
                        sched_date = current_date
                    
                    scheduled_slot = ScheduledSlot(
                        video_id=video.id,
                        video_title=video.title,
                        platform=item.get("platform", "tiktok"),
                        scheduled_date=sched_date,
                        scheduled_time=item.get("time", "12:00"),
                        pillar=video.primary_pillar or "Uncategorized",
                        selection_reason=item.get("reason", video.selection_reason or "AI scheduled"),
                        expected_engagement=self._estimate_engagement(video)
                    )
                    schedule.append(scheduled_slot)
            
            self._add_reasoning_step(
                thought=f"AI optimized schedule: {strategy[:100]}",
                decision=f"Generated {len(schedule)} optimally-timed posts",
                confidence=0.95,
                data={"schedule_count": len(schedule), "strategy": strategy}
            )
            
            logger.info(f"[AI Schedule] Generated {len(schedule)} slots with AI optimization")
            return schedule
            
        except Exception as e:
            logger.warning(f"[AI Schedule] Failed, using fallback: {e}")
            return self._generate_schedule_fallback(selected_videos, constraints, pillars)
    
    def _generate_schedule_fallback(
        self,
        selected_videos: List[VideoCandidate],
        constraints: SchedulingConstraints,
        pillars: List[NarrativePillar]
    ) -> List[ScheduledSlot]:
        """Fallback: Generate schedule with fixed time windows"""
        schedule = []
        
        windows = constraints.posting_windows or {
            "tiktok": ["12:00", "18:00"],
            "instagram": ["09:00", "17:00"],
            "youtube": ["14:00"]
        }
        
        videos_per_day = max(1, len(selected_videos) // 7)
        current_date = date.today()
        video_index = 0
        
        for day in range(7):
            day_date = current_date + timedelta(days=day)
            
            if day_date in constraints.blackout_dates:
                continue
            
            day_platforms = constraints.enabled_platforms.copy()
            
            for slot in range(min(videos_per_day, constraints.max_posts_per_day)):
                if video_index >= len(selected_videos):
                    break
                
                video = selected_videos[video_index]
                platform = day_platforms[slot % len(day_platforms)]
                platform_windows = windows.get(platform, ["12:00"])
                post_time = platform_windows[slot % len(platform_windows)]
                
                scheduled_slot = ScheduledSlot(
                    video_id=video.id,
                    video_title=video.title,
                    platform=platform,
                    scheduled_date=day_date,
                    scheduled_time=post_time,
                    pillar=video.primary_pillar or "Uncategorized",
                    selection_reason=video.selection_reason or "Selected for schedule",
                    expected_engagement=self._estimate_engagement(video)
                )
                
                schedule.append(scheduled_slot)
                video_index += 1
        
        self._add_reasoning_step(
            thought=f"Generated schedule with {len(schedule)} posts over 7 days",
            decision="Schedule complete and ready for review",
            confidence=0.95,
            data={"schedule_count": len(schedule)}
        )
        
        return schedule
    
    def _estimate_engagement(self, video: VideoCandidate) -> float:
        """Estimate expected engagement based on video score"""
        base_rate = 3.0  # Base engagement rate
        
        if video.pre_social_score:
            # Higher scores get higher expected engagement
            score_bonus = (video.pre_social_score - 60) * 0.05
            return round(base_rate + score_bonus, 2)
        
        return base_rate
    
    def _calculate_pillar_distribution(self, schedule: List[ScheduledSlot]) -> Dict[str, int]:
        """Calculate pillar distribution in schedule"""
        dist = {}
        for slot in schedule:
            dist[slot.pillar] = dist.get(slot.pillar, 0) + 1
        return dist
    
    def _calculate_platform_distribution(self, schedule: List[ScheduledSlot]) -> Dict[str, int]:
        """Calculate platform distribution in schedule"""
        dist = {}
        for slot in schedule:
            dist[slot.platform] = dist.get(slot.platform, 0) + 1
        return dist
    
    def _generate_justification(
        self,
        goal: NarrativeGoal,
        schedule: List[ScheduledSlot],
        pillars: List[NarrativePillar],
        previous_performance: Optional[PerformanceMetrics] = None
    ) -> str:
        """Generate a human-readable justification summary"""
        pillar_dist = self._calculate_pillar_distribution(schedule)
        platform_dist = self._calculate_platform_distribution(schedule)
        
        lines = [
            f"## Schedule Justification",
            f"",
            f"### Goal Alignment",
            f"This schedule is designed to achieve: **{goal.goal_statement}**",
            f"",
            f"Primary call-to-action: **{goal.primary_cta}**",
            f"Target audience: {goal.target_audience}",
            f"",
            f"### Content Distribution",
        ]
        
        for pillar_name, count in pillar_dist.items():
            pct = (count / len(schedule)) * 100 if schedule else 0
            lines.append(f"- **{pillar_name}**: {count} posts ({pct:.0f}%)")
        
        lines.extend([
            f"",
            f"### Platform Strategy",
        ])
        
        for platform, count in platform_dist.items():
            lines.append(f"- **{platform.title()}**: {count} posts")
        
        if previous_performance:
            lines.extend([
                f"",
                f"### Based on Previous Performance",
                f"- Last week's engagement: {previous_performance.avg_engagement_rate:.1f}%",
                f"- Adjustments made based on learnings"
            ])
        
        return "\n".join(lines)
    
    async def generate_reflection(
        self,
        plan: WeeklyPlan,
        performance: PerformanceMetrics,
        goal: NarrativeGoal
    ) -> Dict[str, Any]:
        """
        Generate a reflection on schedule performance using real OpenAI API.
        
        This analyzes what worked, what didn't, and generates learnings.
        """
        # Use OpenAI for AI-powered reflection
        if self.openai_api_key:
            try:
                ai_reflection = await self._generate_reflection_with_openai(plan, performance, goal)
                return ai_reflection
            except Exception as e:
                logger.warning(f"[NarrativeEngine] OpenAI reflection failed, using fallback: {e}")
        
        # Fallback to rule-based reflection
        reflection = {
            "period": f"{performance.week_start} to {performance.week_end}",
            "goal_assessment": self._assess_goal_progress(goal, performance),
            "pillar_analysis": self._analyze_pillar_performance(performance),
            "learnings": self._generate_learnings(performance, plan),
            "next_week_adjustments": self._suggest_adjustments(performance, plan)
        }
        
        return reflection
    
    async def _generate_reflection_with_openai(
        self,
        plan: WeeklyPlan,
        performance: PerformanceMetrics,
        goal: NarrativeGoal
    ) -> Dict[str, Any]:
        """Use real OpenAI API to generate insightful reflection"""
        from openai import OpenAI
        client = OpenAI(api_key=self.openai_api_key)
        
        # Build performance summary
        pillar_perf_str = "\n".join([
            f"- {name}: {data.get('posts', 0)} posts, {data.get('avg_views', 0)} avg views, {data.get('avg_engagement', 0):.1f}% engagement"
            for name, data in performance.pillar_performance.items()
        ])
        
        prompt = f"""Analyze this week's content performance and generate actionable insights.

## Goal:
{goal.goal_statement}

## Performance Summary:
- Period: {performance.week_start} to {performance.week_end}
- Total Posts: {plan.total_posts}
- Total Views: {performance.total_views}
- Avg Engagement Rate: {performance.avg_engagement_rate:.1f}%
- Followers Gained: {performance.followers_gained or 'N/A'}

## Pillar Performance:
{pillar_perf_str}

## Task:
1. Assess goal progress honestly
2. Identify what worked well and why
3. Identify what underperformed and why
4. Generate 2-3 specific, actionable learnings
5. Suggest concrete adjustments for next week

Respond in JSON:
{{
    "goal_assessment": {{
        "on_track": true/false,
        "progress_summary": "brief assessment",
        "key_wins": ["win1", "win2"],
        "concerns": ["concern1"]
    }},
    "what_worked": "paragraph explaining what worked",
    "what_didnt_work": "paragraph explaining what didn't",
    "learnings": [
        {{"insight": "...", "confidence": 0.85, "action": "specific action"}},
        ...
    ],
    "next_week_adjustments": ["adjustment1", "adjustment2", "adjustment3"]
}}"""

        response = client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[
                {"role": "system", "content": "You are a social media strategist analyzing content performance. Be specific and actionable."},
                {"role": "user", "content": prompt}
            ],
            temperature=0.5,
            response_format={"type": "json_object"}
        )
        
        result = json.loads(response.choices[0].message.content)
        
        # Add metadata
        result["period"] = f"{performance.week_start} to {performance.week_end}"
        result["powered_by"] = "GPT-4"
        
        logger.info(f"[NarrativeEngine] Generated AI reflection with {len(result.get('learnings', []))} learnings")
        
        return result
    
    def _assess_goal_progress(
        self, 
        goal: NarrativeGoal, 
        performance: PerformanceMetrics
    ) -> Dict[str, Any]:
        """Assess progress toward narrative goal"""
        assessment = {
            "goal": goal.goal_statement,
            "on_track": True,
            "progress": 0.0
        }
        
        # Check different success metrics
        if goal.target_followers and performance.followers_gained:
            progress = (performance.followers_gained / goal.target_followers) * 100
            assessment["followers_target"] = goal.target_followers
            assessment["followers_achieved"] = performance.followers_gained
            assessment["progress"] = progress
            assessment["on_track"] = progress >= 80
        
        if goal.target_conversions and performance.conversions:
            progress = (performance.conversions / goal.target_conversions) * 100
            assessment["conversions_target"] = goal.target_conversions
            assessment["conversions_achieved"] = performance.conversions
            assessment["progress"] = progress
            assessment["on_track"] = progress >= 80
        
        return assessment
    
    def _analyze_pillar_performance(
        self, 
        performance: PerformanceMetrics
    ) -> List[Dict[str, Any]]:
        """Analyze each pillar's performance"""
        analysis = []
        
        avg_engagement = performance.avg_engagement_rate
        
        for pillar_name, data in performance.pillar_performance.items():
            pillar_engagement = data.get('avg_engagement', 0)
            
            verdict = "ON_TARGET"
            if pillar_engagement > avg_engagement * 1.2:
                verdict = "EXCEEDED"
            elif pillar_engagement < avg_engagement * 0.8:
                verdict = "UNDERPERFORMED"
            
            analysis.append({
                "pillar": pillar_name,
                "posts": data.get('posts', 0),
                "avg_views": data.get('avg_views', 0),
                "avg_engagement": pillar_engagement,
                "verdict": verdict,
                "insight": self._generate_pillar_insight(pillar_name, verdict, data)
            })
        
        return analysis
    
    def _generate_pillar_insight(
        self, 
        pillar: str, 
        verdict: str, 
        data: Dict
    ) -> str:
        """Generate insight for a pillar's performance"""
        if verdict == "EXCEEDED":
            return f"{pillar} content resonated strongly with audience. Consider increasing allocation."
        elif verdict == "UNDERPERFORMED":
            return f"{pillar} content needs adjustment. Review format, messaging, or timing."
        else:
            return f"{pillar} performed as expected. Maintain current strategy."
    
    def _generate_learnings(
        self,
        performance: PerformanceMetrics,
        plan: WeeklyPlan
    ) -> List[Learning]:
        """Generate learnings from schedule performance"""
        learnings = []
        
        # Pillar-based learnings
        for pillar_name, data in performance.pillar_performance.items():
            avg_engagement = data.get('avg_engagement', 0)
            
            if avg_engagement > performance.avg_engagement_rate * 1.3:
                learnings.append(Learning(
                    learning_type="pillar_performance",
                    insight=f"{pillar_name} significantly outperformed average",
                    confidence=0.85,
                    action=f"Increase {pillar_name} allocation by 10%",
                    source_schedule_id=plan.id
                ))
            elif avg_engagement < performance.avg_engagement_rate * 0.7:
                learnings.append(Learning(
                    learning_type="pillar_performance",
                    insight=f"{pillar_name} underperformed significantly",
                    confidence=0.80,
                    action=f"Review {pillar_name} content quality and messaging",
                    source_schedule_id=plan.id
                ))
        
        return learnings
    
    def _suggest_adjustments(
        self,
        performance: PerformanceMetrics,
        plan: WeeklyPlan
    ) -> List[str]:
        """Suggest adjustments for next week's plan"""
        adjustments = []
        
        # Analyze pillar performance
        for pillar_name, data in performance.pillar_performance.items():
            avg_engagement = data.get('avg_engagement', 0)
            
            if avg_engagement > performance.avg_engagement_rate * 1.2:
                current_pct = (plan.pillar_distribution.get(pillar_name, 0) / plan.total_posts) * 100
                adjustments.append(f"Increase {pillar_name} from {current_pct:.0f}% to {min(current_pct + 10, 50):.0f}%")
            elif avg_engagement < performance.avg_engagement_rate * 0.8:
                current_pct = (plan.pillar_distribution.get(pillar_name, 0) / plan.total_posts) * 100
                adjustments.append(f"Reduce {pillar_name} from {current_pct:.0f}% to {max(current_pct - 10, 10):.0f}%")
        
        if not adjustments:
            adjustments.append("Maintain current strategy - performance is on target")
        
        return adjustments
