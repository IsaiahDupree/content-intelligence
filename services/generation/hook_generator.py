"""
Hook Idea Generator
===================
Generates new hook ideas by combining competitor patterns with user's own content.
Uses GPT-4 to create variations based on successful patterns.
"""

import os
from typing import Dict, Any, List, Optional
from dataclasses import dataclass, field
from loguru import logger

from openai import OpenAI
from sqlalchemy import create_engine, text


@dataclass
class HookIdea:
    """Generated hook idea"""
    hook_text: str
    archetype: str  # Hook pattern type
    confidence_score: float  # 0-100, how likely to perform well
    source_patterns: List[str]  # Which competitor patterns inspired this
    variation_type: str  # "direct", "inspired", "hybrid"
    reasoning: str  # Why this hook should work


@dataclass
class HookGenerationResult:
    """Result of hook generation"""
    account_id: Optional[str] = None
    competitor_patterns_analyzed: int = 0
    user_content_analyzed: int = 0
    hooks_generated: List[HookIdea] = field(default_factory=list)
    top_archetypes: List[str] = field(default_factory=list)
    recommendations: List[str] = field(default_factory=list)


class HookGenerator:
    """
    Generates hook ideas by combining competitor patterns with user content.
    Analyzes successful hooks from competitors and creates variations.
    """
    
    def __init__(
        self,
        db_url: Optional[str] = None,
        openai_api_key: Optional[str] = None
    ):
        self.db_url = db_url or os.getenv("DATABASE_URL", "postgresql://postgres:postgres@127.0.0.1:54322/postgres")
        self.engine = create_engine(self.db_url)
        
        self.openai_api_key = openai_api_key or os.getenv("OPENAI_API_KEY")
        self.client = OpenAI(api_key=self.openai_api_key) if self.openai_api_key else None
        
        if not self.client:
            logger.warning("OpenAI not configured - hook generation will not work")
    
    async def generate_hooks(
        self,
        competitor_account_ids: List[str],
        user_account_id: Optional[str] = None,
        num_hooks: int = 10,
        min_confidence: float = 70.0
    ) -> HookGenerationResult:
        """
        Generate hook ideas by combining competitor patterns with user content.
        
        Args:
            competitor_account_ids: List of competitor account IDs to analyze
            user_account_id: Optional user account ID to combine with
            num_hooks: Number of hooks to generate
            min_confidence: Minimum confidence score (0-100)
        
        Returns:
            HookGenerationResult with generated hooks
        """
        if not self.client:
            logger.error("OpenAI not configured")
            return HookGenerationResult()
        
        # 1. Collect competitor hook patterns
        competitor_hooks = await self._collect_competitor_hooks(competitor_account_ids)
        
        # 2. Collect user content (if provided)
        user_hooks = []
        if user_account_id:
            user_hooks = await self._collect_user_hooks(user_account_id)
        
        # 3. Analyze patterns
        top_archetypes = self._identify_top_archetypes(competitor_hooks)
        
        # 4. Generate hooks using GPT-4
        hooks = await self._generate_with_ai(
            competitor_hooks=competitor_hooks,
            user_hooks=user_hooks,
            top_archetypes=top_archetypes,
            num_hooks=num_hooks
        )
        
        # 5. Filter by confidence
        filtered_hooks = [h for h in hooks if h.confidence_score >= min_confidence]
        
        # 6. Generate recommendations
        recommendations = self._generate_recommendations(
            filtered_hooks,
            top_archetypes,
            len(competitor_hooks),
            len(user_hooks)
        )
        
        return HookGenerationResult(
            account_id=user_account_id,
            competitor_patterns_analyzed=len(competitor_hooks),
            user_content_analyzed=len(user_hooks),
            hooks_generated=filtered_hooks,
            top_archetypes=top_archetypes,
            recommendations=recommendations
        )
    
    async def _collect_competitor_hooks(
        self,
        account_ids: List[str]
    ) -> List[Dict[str, Any]]:
        """Collect hook patterns from competitor accounts."""
        hooks = []
        
        for account_id in account_ids:
            query = text("""
                SELECT 
                    da.hook_archetype,
                    da.hook_text,
                    da.hook_score,
                    cp.views,
                    cp.likes,
                    cp.comments,
                    cp.posted_at
                FROM competitor_deep_audits da
                JOIN competitor_posts cp ON da.post_id = cp.id
                WHERE cp.account_id = CAST(:account_id AS uuid)
                    AND da.hook_archetype IS NOT NULL
                    AND da.hook_text IS NOT NULL
                    AND da.hook_score > 60
                ORDER BY da.hook_score DESC, cp.views DESC
                LIMIT 20
            """)
            
            with self.engine.connect() as conn:
                result = conn.execute(query, {"account_id": account_id})
                rows = result.fetchall()
            
            for row in rows:
                hooks.append({
                    "archetype": row[0],
                    "text": row[1],
                    "score": row[2] or 0,
                    "views": row[3] or 0,
                    "likes": row[4] or 0,
                    "comments": row[5] or 0,
                    "posted_at": row[6]
                })
        
        return hooks
    
    async def _collect_user_hooks(
        self,
        account_id: str
    ) -> List[Dict[str, Any]]:
        """Collect hooks from user's own content."""
        # This would query the user's own posts/analysis
        # For now, return empty - can be extended to query video_analysis table
        return []
    
    def _identify_top_archetypes(
        self,
        hooks: List[Dict[str, Any]]
    ) -> List[str]:
        """Identify the most successful hook archetypes."""
        archetype_scores: Dict[str, List[float]] = {}
        
        for hook in hooks:
            archetype = hook.get("archetype", "unknown")
            score = hook.get("score", 0)
            
            if archetype not in archetype_scores:
                archetype_scores[archetype] = []
            archetype_scores[archetype].append(score)
        
        # Calculate average scores
        archetype_avg_scores = {
            arch: sum(scores) / len(scores)
            for arch, scores in archetype_scores.items()
        }
        
        # Sort by average score
        sorted_archetypes = sorted(
            archetype_avg_scores.items(),
            key=lambda x: x[1],
            reverse=True
        )
        
        return [arch for arch, _ in sorted_archetypes[:5]]
    
    async def _generate_with_ai(
        self,
        competitor_hooks: List[Dict[str, Any]],
        user_hooks: List[Dict[str, Any]],
        top_archetypes: List[str],
        num_hooks: int
    ) -> List[HookIdea]:
        """Use GPT-4 to generate hook variations."""
        
        # Build prompt
        competitor_examples = "\n".join([
            f"- [{h['archetype']}] {h['text']} (Score: {h['score']}, Views: {h['views']})"
            for h in competitor_hooks[:15]
        ])
        
        user_context = ""
        if user_hooks:
            user_examples = "\n".join([
                f"- {h.get('text', '')}"
                for h in user_hooks[:10]
            ])
            user_context = f"\n\nUser's existing hooks:\n{user_examples}"
        
        prompt = f"""You are a content strategy expert. Analyze these successful competitor hooks and generate new hook ideas.

Top performing hook archetypes: {', '.join(top_archetypes)}

Competitor hooks (successful patterns):
{competitor_examples}
{user_context}

Generate {num_hooks} new hook ideas that:
1. Use the most successful archetypes
2. Are fresh and original (not direct copies)
3. Would work well for similar content
4. Have high viral potential

For each hook, provide:
- The hook text
- The archetype it uses
- Confidence score (0-100) based on pattern similarity
- Which competitor patterns inspired it
- Brief reasoning for why it should work

Return as JSON array with this structure:
[
  {{
    "hook_text": "...",
    "archetype": "...",
    "confidence_score": 85,
    "source_patterns": ["pattern1", "pattern2"],
    "variation_type": "inspired",
    "reasoning": "..."
  }}
]
"""
        
        try:
            response = self.client.chat.completions.create(
                model="gpt-4-turbo-preview",
                messages=[
                    {"role": "system", "content": "You are an expert content strategist who creates high-performing social media hooks."},
                    {"role": "user", "content": prompt}
                ],
                temperature=0.8,
                response_format={"type": "json_object"}
            )
            
            import json
            content = response.choices[0].message.content
            data = json.loads(content)
            
            # Handle both array and object with "hooks" key
            if isinstance(data, list):
                hooks_data = data
            elif "hooks" in data:
                hooks_data = data["hooks"]
            else:
                hooks_data = [data]
            
            hooks = []
            for h in hooks_data:
                hooks.append(HookIdea(
                    hook_text=h.get("hook_text", ""),
                    archetype=h.get("archetype", "unknown"),
                    confidence_score=float(h.get("confidence_score", 0)),
                    source_patterns=h.get("source_patterns", []),
                    variation_type=h.get("variation_type", "inspired"),
                    reasoning=h.get("reasoning", "")
                ))
            
            return hooks
            
        except Exception as e:
            logger.error(f"Error generating hooks with AI: {e}")
            return []
    
    def _generate_recommendations(
        self,
        hooks: List[HookIdea],
        top_archetypes: List[str],
        competitor_count: int,
        user_count: int
    ) -> List[str]:
        """Generate actionable recommendations."""
        recommendations = []
        
        if not hooks:
            recommendations.append("No hooks generated. Ensure competitor accounts have analyzed posts.")
            return recommendations
        
        # Top archetype recommendation
        if top_archetypes:
            recommendations.append(
                f"Focus on '{top_archetypes[0]}' archetype - it's the most successful pattern "
                f"across {competitor_count} competitor hooks analyzed."
            )
        
        # High confidence hooks
        high_confidence = [h for h in hooks if h.confidence_score >= 85]
        if high_confidence:
            recommendations.append(
                f"{len(high_confidence)} hooks have high confidence scores (85+). "
                "Test these first for best results."
            )
        
        # Variation types
        variation_counts = {}
        for hook in hooks:
            vt = hook.variation_type
            variation_counts[vt] = variation_counts.get(vt, 0) + 1
        
        if variation_counts:
            top_variation = max(variation_counts.items(), key=lambda x: x[1])
            recommendations.append(
                f"Most hooks are '{top_variation[0]}' variations - these balance "
                "originality with proven patterns."
            )
        
        # Testing recommendation
        recommendations.append(
            "Test 2-3 hooks per week and track performance. "
            "Focus on hooks with confidence scores above 75."
        )
        
        return recommendations

