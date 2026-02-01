"""
Competitor Deep Audit Service
=============================
AI-powered analysis of competitor content for hooks, CTAs, style fingerprints,
and strategic insights. This is Tier C - AI Inference.
"""
import os
import json
from datetime import datetime
from typing import Dict, Any, List, Optional
from dataclasses import dataclass, field, asdict
from loguru import logger

from openai import OpenAI
from sqlalchemy import create_engine, text


@dataclass
class HookAnalysis:
    """Analyzed hook pattern"""
    archetype: str  # "Stop doing X", "3 mistakes", "Nobody tells you"
    text: str
    strength_score: float
    pattern_elements: List[str] = field(default_factory=list)


@dataclass
class CTAAnalysis:
    """Analyzed CTA pattern"""
    cta_type: str  # comment_keyword, link_bio, follow_part2, dm_me
    text: str
    placement: str  # opening, middle, closing
    effectiveness_score: float


@dataclass
class StyleFingerprint:
    """Visual/editing style analysis"""
    caption_style: str  # fast_captions, minimal, subtitles_only
    cut_density: str  # high, medium, low (cuts per minute)
    color_scheme: str
    motion_presets: List[str] = field(default_factory=list)
    pattern_interrupts: List[str] = field(default_factory=list)
    text_animations: List[str] = field(default_factory=list)


@dataclass
class BeatSheetEntry:
    """Single beat in content structure"""
    role: str  # hook, problem, solution, proof, cta
    start_sec: float
    end_sec: float
    summary: str
    emotion: Optional[str] = None


@dataclass
class PostDeepAudit:
    """Complete deep audit of a single post"""
    post_id: str
    
    # Content classification
    hook: HookAnalysis
    cta: CTAAnalysis
    angle_type: str  # tutorial, teardown, myth-bust, case-study, listicle
    content_pillar: str
    topic_tags: List[str] = field(default_factory=list)
    
    # Structure
    beat_sheet: List[BeatSheetEntry] = field(default_factory=list)
    
    # Style
    style_fingerprint: Optional[StyleFingerprint] = None
    
    # Positioning
    emotional_promise: str = ""
    target_audience: str = ""
    
    # Scores
    hook_score: float = 0.0
    retention_tactics_score: float = 0.0
    viral_potential_score: float = 0.0
    
    # Raw AI response
    ai_analysis_raw: Optional[Dict] = None
    
    created_at: str = field(default_factory=lambda: datetime.utcnow().isoformat())


@dataclass
class AccountDeepAudit:
    """Aggregated deep audit across all posts for an account"""
    account_id: str
    posts_analyzed: int
    
    # Content pillars distribution
    content_pillars: Dict[str, float] = field(default_factory=dict)  # {pillar: percentage}
    
    # Hook patterns
    hook_archetypes: Dict[str, int] = field(default_factory=dict)  # {archetype: count}
    top_hooks: List[str] = field(default_factory=list)
    
    # CTA patterns
    cta_types: Dict[str, int] = field(default_factory=dict)
    most_effective_cta: Optional[str] = None
    
    # Angle distribution
    angle_distribution: Dict[str, float] = field(default_factory=dict)
    
    # Style consistency
    dominant_style: Optional[StyleFingerprint] = None
    style_consistency_score: float = 0.0
    
    # Positioning
    positioning_statement: str = ""  # "They help X achieve Y using Z"
    differentiators: List[str] = field(default_factory=list)
    emotional_promise: str = ""
    credibility_signals: List[str] = field(default_factory=list)
    
    # Retention tactics
    retention_tactics: List[str] = field(default_factory=list)
    avg_retention_score: float = 0.0
    
    created_at: str = field(default_factory=lambda: datetime.utcnow().isoformat())


class CompetitorDeepAuditService:
    """
    AI-powered deep analysis of competitor content.
    Uses GPT-4 to extract hooks, CTAs, style patterns, and strategic insights.
    """
    
    HOOK_ARCHETYPES = [
        "Stop doing X",
        "X mistakes you're making",
        "Nobody tells you about X",
        "I tried X for Y days",
        "The truth about X",
        "Why X doesn't work",
        "How I X in Y time",
        "X secrets they don't want you to know",
        "What happens when X",
        "Before vs After X",
        "POV: X",
        "If you X, watch this",
        "This changed everything",
        "Unpopular opinion: X",
        "Hot take: X"
    ]
    
    ANGLE_TYPES = [
        "tutorial",
        "teardown",
        "myth-bust",
        "case-study",
        "listicle",
        "behind-the-scenes",
        "day-in-the-life",
        "transformation",
        "reaction",
        "comparison",
        "story-time",
        "hot-take"
    ]
    
    CTA_TYPES = [
        "comment_keyword",  # "Comment X to get..."
        "link_bio",         # "Link in bio"
        "follow_part2",     # "Follow for part 2"
        "dm_me",            # "DM me X"
        "save_this",        # "Save this for later"
        "share_with",       # "Share with someone who..."
        "subscribe",        # YouTube subscribe
        "like_if",          # "Like if you agree"
        "none"
    ]
    
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
            logger.warning("OpenAI not configured - deep audit will not work")
    
    async def audit_post(
        self,
        post_id: str,
        caption_text: str,
        transcript: Optional[str] = None,
        thumbnail_url: Optional[str] = None,
        duration_sec: Optional[float] = None
    ) -> PostDeepAudit:
        """
        Perform deep AI analysis on a single post.
        
        Args:
            post_id: Database post ID
            caption_text: Post caption
            transcript: Video transcript if available
            thumbnail_url: Thumbnail for visual analysis
            duration_sec: Video duration
        """
        if not self.client:
            logger.error("OpenAI not configured")
            return PostDeepAudit(
                post_id=post_id,
                hook=HookAnalysis(archetype="unknown", text="", strength_score=0),
                cta=CTAAnalysis(cta_type="none", text="", placement="none", effectiveness_score=0),
                angle_type="unknown",
                content_pillar="unknown"
            )
        
        # Build analysis prompt
        content_to_analyze = f"CAPTION:\n{caption_text}\n"
        if transcript:
            content_to_analyze += f"\nTRANSCRIPT:\n{transcript}\n"
        if duration_sec:
            content_to_analyze += f"\nDURATION: {duration_sec} seconds\n"
        
        prompt = f"""Analyze this social media post for content strategy patterns.

{content_to_analyze}

Analyze and return JSON with:
{{
    "hook_analysis": {{
        "archetype": "one of: {', '.join(self.HOOK_ARCHETYPES[:8])}...",
        "text": "the actual hook text/phrase",
        "strength_score": 0-100,
        "pattern_elements": ["curiosity_gap", "specificity", "controversy", etc.]
    }},
    "cta_analysis": {{
        "cta_type": "one of: {', '.join(self.CTA_TYPES)}",
        "text": "the actual CTA text",
        "placement": "opening|middle|closing",
        "effectiveness_score": 0-100
    }},
    "angle_type": "one of: {', '.join(self.ANGLE_TYPES)}",
    "content_pillar": "main topic category",
    "topic_tags": ["tag1", "tag2", "tag3"],
    "beat_sheet": [
        {{"role": "hook", "start_sec": 0, "end_sec": 3, "summary": "attention grabber", "emotion": "curiosity"}}
    ],
    "style_indicators": {{
        "caption_style": "fast_captions|minimal|subtitles|none",
        "energy_level": "high|medium|low",
        "pattern_interrupts": ["zoom", "sound_effect", "text_pop"]
    }},
    "emotional_promise": "what feeling/outcome they promise",
    "target_audience": "who this content is for",
    "viral_potential_score": 0-100,
    "retention_tactics": ["open_loop", "curiosity_gap", "proof_early", "pattern_interrupt"]
}}

Be specific and analytical. Base all scores on content quality indicators."""

        try:
            response = self.client.chat.completions.create(
                model="gpt-4o-mini",
                messages=[
                    {
                        "role": "system",
                        "content": "You are an expert content strategist analyzing social media posts for viral patterns, hooks, and strategic elements."
                    },
                    {"role": "user", "content": prompt}
                ],
                response_format={"type": "json_object"},
                temperature=0.3
            )
            
            analysis = json.loads(response.choices[0].message.content)
            
            # Parse into dataclasses
            hook_data = analysis.get("hook_analysis", {})
            cta_data = analysis.get("cta_analysis", {})
            style_data = analysis.get("style_indicators", {})
            
            hook = HookAnalysis(
                archetype=hook_data.get("archetype", "unknown"),
                text=hook_data.get("text", ""),
                strength_score=hook_data.get("strength_score", 0),
                pattern_elements=hook_data.get("pattern_elements", [])
            )
            
            cta = CTAAnalysis(
                cta_type=cta_data.get("cta_type", "none"),
                text=cta_data.get("text", ""),
                placement=cta_data.get("placement", "none"),
                effectiveness_score=cta_data.get("effectiveness_score", 0)
            )
            
            style = StyleFingerprint(
                caption_style=style_data.get("caption_style", "unknown"),
                cut_density=style_data.get("energy_level", "medium"),
                color_scheme="",
                pattern_interrupts=style_data.get("pattern_interrupts", [])
            )
            
            # Parse beat sheet
            beat_sheet = []
            for beat in analysis.get("beat_sheet", []):
                beat_sheet.append(BeatSheetEntry(
                    role=beat.get("role", "unknown"),
                    start_sec=beat.get("start_sec", 0),
                    end_sec=beat.get("end_sec", 0),
                    summary=beat.get("summary", ""),
                    emotion=beat.get("emotion")
                ))
            
            return PostDeepAudit(
                post_id=post_id,
                hook=hook,
                cta=cta,
                angle_type=analysis.get("angle_type", "unknown"),
                content_pillar=analysis.get("content_pillar", "unknown"),
                topic_tags=analysis.get("topic_tags", []),
                beat_sheet=beat_sheet,
                style_fingerprint=style,
                emotional_promise=analysis.get("emotional_promise", ""),
                target_audience=analysis.get("target_audience", ""),
                hook_score=hook.strength_score,
                retention_tactics_score=len(analysis.get("retention_tactics", [])) * 20,
                viral_potential_score=analysis.get("viral_potential_score", 0),
                ai_analysis_raw=analysis
            )
            
        except Exception as e:
            logger.error(f"Post audit failed: {e}")
            return PostDeepAudit(
                post_id=post_id,
                hook=HookAnalysis(archetype="error", text="", strength_score=0),
                cta=CTAAnalysis(cta_type="none", text="", placement="none", effectiveness_score=0),
                angle_type="error",
                content_pillar="error"
            )
    
    async def audit_account(
        self,
        account_id: str,
        post_audits: List[PostDeepAudit]
    ) -> AccountDeepAudit:
        """
        Aggregate post audits into account-level insights.
        
        Args:
            account_id: Database account ID
            post_audits: List of individual post audits
        """
        if not post_audits:
            return AccountDeepAudit(account_id=account_id, posts_analyzed=0)
        
        # Aggregate content pillars
        pillar_counts: Dict[str, int] = {}
        hook_counts: Dict[str, int] = {}
        cta_counts: Dict[str, int] = {}
        angle_counts: Dict[str, int] = {}
        all_hooks: List[str] = []
        retention_scores: List[float] = []
        
        for audit in post_audits:
            # Count pillars
            pillar = audit.content_pillar
            pillar_counts[pillar] = pillar_counts.get(pillar, 0) + 1
            
            # Count hooks
            hook_type = audit.hook.archetype
            hook_counts[hook_type] = hook_counts.get(hook_type, 0) + 1
            if audit.hook.text:
                all_hooks.append(audit.hook.text)
            
            # Count CTAs
            cta_type = audit.cta.cta_type
            cta_counts[cta_type] = cta_counts.get(cta_type, 0) + 1
            
            # Count angles
            angle = audit.angle_type
            angle_counts[angle] = angle_counts.get(angle, 0) + 1
            
            # Collect retention scores
            retention_scores.append(audit.retention_tactics_score)
        
        total = len(post_audits)
        
        # Convert to percentages
        content_pillars = {k: v / total * 100 for k, v in pillar_counts.items()}
        angle_distribution = {k: v / total * 100 for k, v in angle_counts.items()}
        
        # Find most effective CTA
        most_effective_cta = max(cta_counts.keys(), key=lambda k: cta_counts[k]) if cta_counts else None
        
        # Generate positioning statement
        positioning = await self._generate_positioning(account_id, post_audits)
        
        return AccountDeepAudit(
            account_id=account_id,
            posts_analyzed=total,
            content_pillars=content_pillars,
            hook_archetypes=hook_counts,
            top_hooks=all_hooks[:5],
            cta_types=cta_counts,
            most_effective_cta=most_effective_cta,
            angle_distribution=angle_distribution,
            positioning_statement=positioning.get("statement", ""),
            differentiators=positioning.get("differentiators", []),
            emotional_promise=positioning.get("emotional_promise", ""),
            credibility_signals=positioning.get("credibility_signals", []),
            retention_tactics=positioning.get("retention_tactics", []),
            avg_retention_score=sum(retention_scores) / len(retention_scores) if retention_scores else 0
        )
    
    async def _generate_positioning(
        self,
        account_id: str,
        post_audits: List[PostDeepAudit]
    ) -> Dict[str, Any]:
        """Generate positioning statement from aggregated audits"""
        if not self.client:
            return {}
        
        # Collect sample content for analysis
        sample_content = []
        for audit in post_audits[:10]:
            sample_content.append({
                "hook": audit.hook.text,
                "pillar": audit.content_pillar,
                "audience": audit.target_audience,
                "promise": audit.emotional_promise
            })
        
        prompt = f"""Based on these content samples from a creator, determine their positioning:

CONTENT SAMPLES:
{json.dumps(sample_content, indent=2)}

Return JSON with:
{{
    "statement": "They help [WHO] achieve [WHAT] using [HOW]",
    "differentiators": ["what makes them unique", "contrarian beliefs", "signature approach"],
    "emotional_promise": "the feeling/transformation they sell",
    "credibility_signals": ["experience", "results", "authority markers"],
    "retention_tactics": ["common tactics they use to keep viewers watching"]
}}"""

        try:
            response = self.client.chat.completions.create(
                model="gpt-4o-mini",
                messages=[
                    {"role": "system", "content": "You are a brand strategist analyzing creator positioning."},
                    {"role": "user", "content": prompt}
                ],
                response_format={"type": "json_object"},
                temperature=0.5
            )
            return json.loads(response.choices[0].message.content)
        except Exception as e:
            logger.error(f"Positioning generation failed: {e}")
            return {}
    
    async def save_post_audit(self, audit: PostDeepAudit) -> str:
        """Save post audit to database"""
        try:
            with self.engine.connect() as conn:
                result = conn.execute(text("""
                    INSERT INTO competitor_deep_audit (
                        post_id, audit_type, audit_version,
                        hook_archetype, hook_text, angle_type, content_pillar,
                        topic_tags, cta_type, cta_text, cta_placement,
                        beat_sheet, visual_fingerprint,
                        emotional_promise, positioning_statement,
                        hook_score, retention_tactics_score,
                        ai_analysis_raw
                    ) VALUES (
                        :post_id, 'post', '1.0',
                        :hook_archetype, :hook_text, :angle_type, :content_pillar,
                        :topic_tags, :cta_type, :cta_text, :cta_placement,
                        :beat_sheet, :visual_fingerprint,
                        :emotional_promise, :positioning,
                        :hook_score, :retention_score,
                        :ai_raw
                    )
                    RETURNING audit_id
                """), {
                    "post_id": audit.post_id,
                    "hook_archetype": audit.hook.archetype,
                    "hook_text": audit.hook.text,
                    "angle_type": audit.angle_type,
                    "content_pillar": audit.content_pillar,
                    "topic_tags": audit.topic_tags,
                    "cta_type": audit.cta.cta_type,
                    "cta_text": audit.cta.text,
                    "cta_placement": audit.cta.placement,
                    "beat_sheet": [asdict(b) for b in audit.beat_sheet],
                    "visual_fingerprint": asdict(audit.style_fingerprint) if audit.style_fingerprint else None,
                    "emotional_promise": audit.emotional_promise,
                    "positioning": audit.target_audience,
                    "hook_score": audit.hook_score,
                    "retention_score": audit.retention_tactics_score,
                    "ai_raw": audit.ai_analysis_raw
                })
                conn.commit()
                row = result.fetchone()
                return str(row[0])
        except Exception as e:
            logger.error(f"Failed to save post audit: {e}")
            raise
