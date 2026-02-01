"""
AI Content Selection Service (NAR-005)
=======================================
Select UGC videos based on narrative goals, pillars, and scheduling constraints.

This service:
1. Analyzes available UGC content in the library
2. Matches content to narrative pillars and goals
3. Applies scheduling constraints (diversity, freshness, performance)
4. Uses AI to reason about best content fit
5. Logs selection reasoning for transparency

Selection Criteria:
- Pillar alignment (keywords, topics, sentiment)
- Goal alignment (CTA, target audience, narrative)
- Content freshness (not recently used)
- Content diversity (avoid repetition)
- Historical performance (if content was used before)
- Length and format constraints

AI Reasoning:
- GPT-4 analyzes content context + goal/pillar
- Provides selection score and reasoning
- Considers strategic narrative fit
- Balances immediate performance vs long-term storytelling

Integration:
- Called by WeeklyCycleExecutor for each slot
- Reads from analyzed_content table
- Writes to content_selections table with reasoning
"""

import asyncio
import os
import json
from datetime import datetime, timedelta, timezone
from typing import Optional, Dict, Any, List, Tuple
from uuid import uuid4
from loguru import logger
from sqlalchemy import create_engine, text
from dataclasses import dataclass, field
from openai import AsyncOpenAI

from services.event_bus import EventBus, Topics


@dataclass
class SelectionCriteria:
    """Criteria for content selection"""
    pillar_id: str
    pillar_name: str
    pillar_type: str  # value, authority, tribe, offer
    pillar_keywords: List[str]
    goal_statement: str
    target_audience: str
    primary_cta: str
    platform: str
    max_duration_seconds: Optional[int] = None
    min_duration_seconds: Optional[int] = None
    exclude_content_ids: List[str] = field(default_factory=list)
    diversity_window_days: int = 7  # Don't reuse content from last 7 days


@dataclass
class ContentCandidate:
    """A candidate piece of content"""
    content_id: str
    title: str
    description: str
    transcript: Optional[str]
    duration_seconds: int
    format: str
    platform: str
    sentiment: Optional[str]
    topics: List[str]
    keywords: List[str]
    created_at: datetime
    last_used: Optional[datetime]
    previous_performance: Optional[Dict[str, Any]]


@dataclass
class SelectionResult:
    """Result of content selection with reasoning"""
    content_id: str
    score: float
    reasoning: str
    pillar_alignment: float
    goal_alignment: float
    freshness_score: float
    diversity_score: float
    metadata: Dict[str, Any]


class ContentSelector:
    """
    AI Content Selection Service (NAR-005)

    Intelligently selects UGC content for each slot in the narrative schedule
    based on goals, pillars, and constraints.

    Usage:
        selector = ContentSelector.get_instance()
        await selector.start()

        # Select content for a slot
        selection = await selector.select_content_for_slot({
            "pillar_id": "abc123",
            "pillar_name": "Pain Points",
            "goal_statement": "Position as DIY expert",
            "platform": "tiktok"
        })

        if selection:
            print(f"Selected: {selection['content_id']}")
            print(f"Reasoning: {selection['reasoning']}")
    """

    _instance: Optional["ContentSelector"] = None

    def __init__(self):
        """Initialize content selector"""
        if ContentSelector._instance is not None:
            raise RuntimeError("Use ContentSelector.get_instance()")

        self.event_bus = EventBus.get_instance()
        self.event_bus.set_source("content-selector")

        # Database connection
        self.database_url = os.getenv(
            "DATABASE_URL",
            "postgresql://postgres:postgres@localhost:54322/postgres"
        )
        self.engine = create_engine(self.database_url)

        # OpenAI client for AI reasoning
        self.openai_client = AsyncOpenAI(api_key=os.getenv("OPENAI_API_KEY"))

        # Configuration
        self.model = os.getenv("OPENAI_MODEL", "gpt-4o-mini")
        self.min_selection_score = 0.6  # Minimum score to select content
        self.candidate_pool_size = 20  # Max candidates to analyze

        ContentSelector._instance = self
        logger.info("🎯 ContentSelector initialized")

    @classmethod
    def get_instance(cls) -> "ContentSelector":
        """Get singleton instance"""
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance

    async def start(self) -> None:
        """Start the content selector service"""
        await self._ensure_tables_exist()

        logger.success("✓ ContentSelector started")

    async def stop(self) -> None:
        """Stop the content selector service"""
        logger.info("🛑 ContentSelector stopped")

    async def select_content_for_slot(
        self,
        slot: Dict[str, Any]
    ) -> Optional[Dict[str, Any]]:
        """
        Select the best content for a given slot

        Args:
            slot: Slot data with pillar, goal, platform, etc.

        Returns:
            Selected content with reasoning, or None if no suitable content
        """
        logger.info(f"🔍 Selecting content for pillar: {slot.get('pillar_name')}")

        # Build selection criteria
        criteria = await self._build_criteria(slot)

        # Get candidate content
        candidates = await self._get_candidates(criteria)
        if not candidates:
            logger.warning(f"⚠️  No candidates found for pillar {criteria.pillar_name}")
            return None

        logger.info(f"📋 Analyzing {len(candidates)} candidates...")

        # Score and rank candidates
        scored_candidates = []
        for candidate in candidates:
            score_result = await self._score_candidate(candidate, criteria)
            scored_candidates.append((candidate, score_result))

        # Sort by score
        scored_candidates.sort(key=lambda x: x[1].score, reverse=True)

        # Get best candidate
        best_candidate, best_score = scored_candidates[0]

        if best_score.score < self.min_selection_score:
            logger.warning(
                f"⚠️  Best candidate score {best_score.score:.2f} below threshold {self.min_selection_score}"
            )
            return None

        # Log selection
        await self._log_selection(slot, best_candidate, best_score)

        # Publish event
        await self.event_bus.publish(
            "content.selected",
            {
                "slot_id": slot.get("id"),
                "content_id": best_candidate.content_id,
                "score": best_score.score,
                "pillar": criteria.pillar_name
            }
        )

        logger.success(
            f"✅ Selected content '{best_candidate.title}' (score: {best_score.score:.2f})"
        )

        return {
            "content_id": best_candidate.content_id,
            "title": best_candidate.title,
            "score": best_score.score,
            "reasoning": best_score.reasoning,
            "pillar_alignment": best_score.pillar_alignment,
            "goal_alignment": best_score.goal_alignment,
            "metadata": best_score.metadata
        }

    async def _build_criteria(self, slot: Dict[str, Any]) -> SelectionCriteria:
        """Build selection criteria from slot data"""
        # Get pillar details
        pillar = await self._get_pillar(slot.get("pillar_id"))
        if not pillar:
            raise ValueError(f"Pillar not found: {slot.get('pillar_id')}")

        # Get goal details
        goal = await self._get_goal(pillar["goal_id"])
        if not goal:
            raise ValueError(f"Goal not found: {pillar['goal_id']}")

        # Get recently used content to exclude
        recently_used = await self._get_recently_used_content(days=7)

        return SelectionCriteria(
            pillar_id=pillar["id"],
            pillar_name=pillar["name"],
            pillar_type=pillar["pillar_type"],
            pillar_keywords=pillar.get("keywords", []),
            goal_statement=goal["goal_statement"],
            target_audience=goal.get("target_audience", ""),
            primary_cta=goal["primary_cta"],
            platform=slot.get("platform", "tiktok"),
            max_duration_seconds=slot.get("max_duration"),
            min_duration_seconds=slot.get("min_duration"),
            exclude_content_ids=recently_used
        )

    async def _get_candidates(self, criteria: SelectionCriteria) -> List[ContentCandidate]:
        """Get candidate content matching criteria"""
        with self.engine.connect() as conn:
            # Build query
            query = """
                SELECT
                    v.id,
                    v.title,
                    v.description,
                    a.transcript,
                    v.duration_seconds,
                    v.format,
                    v.platform,
                    a.sentiment,
                    a.topics,
                    a.keywords,
                    v.created_at,
                    cs.last_used_at,
                    cs.performance_data
                FROM videos v
                LEFT JOIN analyzed_content a ON v.id = a.video_id
                LEFT JOIN (
                    SELECT content_id, MAX(created_at) as last_used_at,
                           jsonb_agg(metrics) as performance_data
                    FROM content_selections
                    GROUP BY content_id
                ) cs ON v.id = cs.content_id
                WHERE v.id NOT IN :exclude_ids
                  AND v.status = 'analyzed'
            """

            params = {"exclude_ids": tuple(criteria.exclude_content_ids) if criteria.exclude_content_ids else (None,)}

            # Add duration filters
            if criteria.min_duration_seconds:
                query += " AND v.duration_seconds >= :min_duration"
                params["min_duration"] = criteria.min_duration_seconds
            if criteria.max_duration_seconds:
                query += " AND v.duration_seconds <= :max_duration"
                params["max_duration"] = criteria.max_duration_seconds

            query += " ORDER BY v.created_at DESC LIMIT :limit"
            params["limit"] = self.candidate_pool_size

            result = conn.execute(text(query), params)

            candidates = []
            for row in result:
                candidates.append(ContentCandidate(
                    content_id=str(row[0]),
                    title=row[1] or "",
                    description=row[2] or "",
                    transcript=row[3],
                    duration_seconds=row[4] or 0,
                    format=row[5] or "unknown",
                    platform=row[6] or "unknown",
                    sentiment=row[7],
                    topics=row[8] or [],
                    keywords=row[9] or [],
                    created_at=row[10],
                    last_used=row[11],
                    previous_performance=row[12]
                ))

            return candidates

    async def _score_candidate(
        self,
        candidate: ContentCandidate,
        criteria: SelectionCriteria
    ) -> SelectionResult:
        """
        Score a candidate using AI reasoning

        Combines:
        1. Pillar alignment (keyword/topic matching)
        2. Goal alignment (AI analysis)
        3. Freshness (not recently used)
        4. Diversity (different from recent posts)
        """
        # Calculate component scores
        pillar_score = await self._calculate_pillar_alignment(candidate, criteria)
        goal_score = await self._calculate_goal_alignment(candidate, criteria)
        freshness_score = self._calculate_freshness(candidate)
        diversity_score = 0.8  # TODO: Implement actual diversity calculation

        # AI reasoning for overall fit
        reasoning = await self._get_ai_reasoning(candidate, criteria)

        # Weighted final score
        final_score = (
            pillar_score * 0.35 +
            goal_score * 0.35 +
            freshness_score * 0.15 +
            diversity_score * 0.15
        )

        return SelectionResult(
            content_id=candidate.content_id,
            score=final_score,
            reasoning=reasoning,
            pillar_alignment=pillar_score,
            goal_alignment=goal_score,
            freshness_score=freshness_score,
            diversity_score=diversity_score,
            metadata={
                "candidate_title": candidate.title,
                "pillar": criteria.pillar_name,
                "goal": criteria.goal_statement
            }
        )

    async def _calculate_pillar_alignment(
        self,
        candidate: ContentCandidate,
        criteria: SelectionCriteria
    ) -> float:
        """Calculate how well content aligns with pillar"""
        # Keyword matching
        candidate_keywords = set(candidate.keywords + candidate.topics)
        pillar_keywords = set(criteria.pillar_keywords)

        if not pillar_keywords:
            return 0.5  # Neutral if no keywords defined

        overlap = candidate_keywords.intersection(pillar_keywords)
        keyword_score = len(overlap) / len(pillar_keywords) if pillar_keywords else 0.0

        # Pillar type alignment
        type_score = 0.5  # TODO: Implement type-specific scoring

        # Weighted combination
        return keyword_score * 0.7 + type_score * 0.3

    async def _calculate_goal_alignment(
        self,
        candidate: ContentCandidate,
        criteria: SelectionCriteria
    ) -> float:
        """Use AI to calculate goal alignment"""
        try:
            # Build prompt
            prompt = f"""Analyze how well this content aligns with the narrative goal.

GOAL: {criteria.goal_statement}
TARGET AUDIENCE: {criteria.target_audience}
PRIMARY CTA: {criteria.primary_cta}

CONTENT:
Title: {candidate.title}
Description: {candidate.description}
Transcript: {(candidate.transcript or '')[:500]}...

Rate alignment from 0.0 to 1.0 based on:
- Relevance to goal
- Audience fit
- CTA compatibility

Return ONLY a number between 0.0 and 1.0."""

            response = await self.openai_client.chat.completions.create(
                model=self.model,
                messages=[
                    {"role": "system", "content": "You are a content strategy analyst."},
                    {"role": "user", "content": prompt}
                ],
                temperature=0.3,
                max_tokens=10
            )

            score_text = response.choices[0].message.content.strip()
            score = float(score_text)
            return max(0.0, min(1.0, score))

        except Exception as e:
            logger.warning(f"⚠️  AI goal alignment failed: {e}, using fallback")
            return 0.5

    def _calculate_freshness(self, candidate: ContentCandidate) -> float:
        """Calculate freshness score (higher = less recently used)"""
        if not candidate.last_used:
            return 1.0  # Never used = maximum freshness

        days_since_use = (datetime.now(timezone.utc) - candidate.last_used).days

        # Decay function: freshness decreases as days_since_use decreases
        if days_since_use >= 30:
            return 1.0
        elif days_since_use >= 14:
            return 0.8
        elif days_since_use >= 7:
            return 0.6
        elif days_since_use >= 3:
            return 0.3
        else:
            return 0.1

    async def _get_ai_reasoning(
        self,
        candidate: ContentCandidate,
        criteria: SelectionCriteria
    ) -> str:
        """Get AI reasoning for why this content fits"""
        try:
            prompt = f"""Explain why this content is a good fit for this narrative pillar.

PILLAR: {criteria.pillar_name} ({criteria.pillar_type})
KEYWORDS: {', '.join(criteria.pillar_keywords[:5])}

CONTENT:
{candidate.title}
{candidate.description}

Provide a 1-sentence strategic reasoning for selection."""

            response = await self.openai_client.chat.completions.create(
                model=self.model,
                messages=[
                    {"role": "system", "content": "You are a content strategist providing selection reasoning."},
                    {"role": "user", "content": prompt}
                ],
                temperature=0.7,
                max_tokens=100
            )

            return response.choices[0].message.content.strip()

        except Exception as e:
            logger.warning(f"⚠️  AI reasoning failed: {e}")
            return f"Selected based on {criteria.pillar_name} keyword alignment"

    async def _log_selection(
        self,
        slot: Dict[str, Any],
        candidate: ContentCandidate,
        score: SelectionResult
    ) -> None:
        """Log selection to database for tracking"""
        with self.engine.connect() as conn:
            conn.execute(text("""
                INSERT INTO content_selections (
                    id, slot_id, content_id, pillar_id, score,
                    reasoning, pillar_alignment, goal_alignment,
                    freshness_score, diversity_score, metadata
                ) VALUES (
                    :id, :slot_id, :content_id, :pillar_id, :score,
                    :reasoning, :pillar_alignment, :goal_alignment,
                    :freshness_score, :diversity_score, :metadata
                )
            """), {
                "id": str(uuid4()),
                "slot_id": slot.get("id"),
                "content_id": candidate.content_id,
                "pillar_id": slot.get("pillar_id"),
                "score": score.score,
                "reasoning": score.reasoning,
                "pillar_alignment": score.pillar_alignment,
                "goal_alignment": score.goal_alignment,
                "freshness_score": score.freshness_score,
                "diversity_score": score.diversity_score,
                "metadata": json.dumps(score.metadata)
            })
            conn.commit()

    # Helper methods

    async def _ensure_tables_exist(self) -> None:
        """Ensure content_selections table exists"""
        with self.engine.connect() as conn:
            conn.execute(text("""
                CREATE TABLE IF NOT EXISTS content_selections (
                    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
                    slot_id TEXT,
                    content_id TEXT NOT NULL,
                    pillar_id UUID,
                    score FLOAT,
                    reasoning TEXT,
                    pillar_alignment FLOAT,
                    goal_alignment FLOAT,
                    freshness_score FLOAT,
                    diversity_score FLOAT,
                    metadata JSONB,
                    created_at TIMESTAMPTZ DEFAULT NOW()
                );

                CREATE INDEX IF NOT EXISTS idx_content_selections_content
                ON content_selections(content_id);

                CREATE INDEX IF NOT EXISTS idx_content_selections_pillar
                ON content_selections(pillar_id);
            """))
            conn.commit()

    async def _get_pillar(self, pillar_id: str) -> Optional[Dict[str, Any]]:
        """Get pillar details"""
        with self.engine.connect() as conn:
            result = conn.execute(text("""
                SELECT id, goal_id, name, pillar_type, keywords
                FROM narrative_pillars
                WHERE id = :pillar_id
            """), {"pillar_id": pillar_id})

            row = result.fetchone()
            if row:
                return {
                    "id": str(row[0]),
                    "goal_id": str(row[1]),
                    "name": row[2],
                    "pillar_type": row[3],
                    "keywords": row[4] or []
                }
            return None

    async def _get_goal(self, goal_id: str) -> Optional[Dict[str, Any]]:
        """Get goal details"""
        with self.engine.connect() as conn:
            result = conn.execute(text("""
                SELECT id, goal_statement, primary_cta, target_audience
                FROM narrative_goals
                WHERE id = :goal_id
            """), {"goal_id": goal_id})

            row = result.fetchone()
            if row:
                return {
                    "id": str(row[0]),
                    "goal_statement": row[1],
                    "primary_cta": row[2],
                    "target_audience": row[3]
                }
            return None

    async def _get_recently_used_content(self, days: int = 7) -> List[str]:
        """Get content IDs used in the last N days"""
        cutoff = datetime.now(timezone.utc) - timedelta(days=days)

        with self.engine.connect() as conn:
            result = conn.execute(text("""
                SELECT DISTINCT content_id
                FROM content_selections
                WHERE created_at >= :cutoff
            """), {"cutoff": cutoff})

            return [str(row[0]) for row in result]
