"""
Brief Service - Generate content briefs from trends
"""
import os
import json
from datetime import datetime
from typing import List, Dict, Any, Optional

from loguru import logger
from sqlalchemy import create_engine, text
from openai import OpenAI

from .models import Brief, BriefStatus


DATABASE_URL = os.getenv("DATABASE_URL", "postgresql://postgres:postgres@localhost:54322/postgres")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "")


class BriefService:
    """
    Service for generating content briefs from trend clusters.
    
    A brief is a content-ready pack that includes:
    - Hook options
    - Script outline
    - Caption templates
    - Shotlist (b-roll, on-screen text)
    - CTA options
    """
    
    def __init__(self):
        self.engine = create_engine(DATABASE_URL)
        self.openai_client = OpenAI(api_key=OPENAI_API_KEY) if OPENAI_API_KEY else None
    
    async def generate_brief(
        self,
        cluster_id: str,
        platform_target: str = "tiktok",
        tone: Dict[str, Any] = None,
        brand_safe_mode: bool = True,
        workspace_id: str = "00000000-0000-0000-0000-000000000001"
    ) -> Optional[Brief]:
        """Generate a content brief from a trend cluster"""
        if not self.openai_client:
            logger.warning("OpenAI client not configured")
            return None
        
        logger.info(f"📝 Generating brief for cluster {cluster_id}")
        
        # Get cluster info and lingo
        cluster_data = await self._get_cluster_with_lingo(cluster_id)
        if not cluster_data:
            logger.warning(f"Cluster {cluster_id} not found")
            return None
        
        # Get sample posts
        sample_posts = await self._get_sample_posts(cluster_id, limit=10)
        
        # Generate brief using AI
        brief = await self._generate_with_ai(
            cluster_data=cluster_data,
            sample_posts=sample_posts,
            platform_target=platform_target,
            tone=tone or {"style": "engaging", "energy": "high"},
            brand_safe_mode=brand_safe_mode,
            workspace_id=workspace_id,
        )
        
        if brief:
            # Save to database
            await self._save_brief(brief)
            logger.success(f"✅ Generated brief: {brief.id}")
        
        return brief
    
    async def _get_cluster_with_lingo(self, cluster_id: str) -> Optional[Dict]:
        """Get cluster details with lingo"""
        with self.engine.connect() as conn:
            result = conn.execute(text("""
                SELECT 
                    tc.id, tc.title, tc.cluster_type, tc.status,
                    cl.key_phrases, cl.hook_patterns, cl.meaning,
                    cl.structure, cl.tone, cl.brand_safety_score
                FROM trend_clusters tc
                LEFT JOIN cluster_lingo cl ON tc.id = cl.cluster_id
                WHERE tc.id = :cluster_id
            """), {"cluster_id": cluster_id})
            
            row = result.fetchone()
            if row:
                return dict(row._mapping)
        return None
    
    async def _get_sample_posts(self, cluster_id: str, limit: int = 10) -> List[Dict]:
        """Get sample posts from cluster"""
        with self.engine.connect() as conn:
            result = conn.execute(text("""
                SELECT 
                    pr.caption_text,
                    pr.metrics,
                    pr.author_handle
                FROM cluster_members cm
                JOIN posts_raw pr ON cm.post_id = pr.id
                WHERE cm.cluster_id = :cluster_id
                ORDER BY (pr.metrics->>'likes')::int DESC
                LIMIT :limit
            """), {"cluster_id": cluster_id, "limit": limit})
            
            return [dict(row._mapping) for row in result.fetchall()]
    
    async def _generate_with_ai(
        self,
        cluster_data: Dict,
        sample_posts: List[Dict],
        platform_target: str,
        tone: Dict,
        brand_safe_mode: bool,
        workspace_id: str
    ) -> Optional[Brief]:
        """Use AI to generate a comprehensive content brief"""
        
        title = cluster_data.get("title", "Trend")
        meaning = cluster_data.get("meaning", "")
        key_phrases = cluster_data.get("key_phrases", [])
        hook_patterns = cluster_data.get("hook_patterns", [])
        
        captions = [p.get("caption_text", "")[:200] for p in sample_posts if p.get("caption_text")]
        
        prompt = f"""Create a content brief for this trending topic.

TREND: {title}
MEANING: {meaning}
KEY PHRASES: {key_phrases}
EXISTING HOOKS: {hook_patterns}
PLATFORM: {platform_target}
TONE: {tone}
BRAND SAFE: {brand_safe_mode}

SAMPLE CAPTIONS FROM TOP POSTS:
{chr(10).join(f'- {c}' for c in captions[:5])}

Generate a complete content brief with:

1. HOOKS (3 options): Scroll-stopping openers that work for this trend
2. SCRIPT_OUTLINE: Structure for a 15-30 second video
   - opening (0-3s)
   - problem/setup (3-10s)
   - solution/payoff (10-20s)
   - cta (20-30s)
3. CAPTION_TEMPLATES (3 options): Ready-to-use captions with [PLACEHOLDER] for customization
4. ANGLES (3 options): Different ways to approach this trend
5. SHOTLIST: Visual elements needed
   - broll suggestions
   - on_screen_text
   - transitions
6. CTA: Call to action options
7. MUST_INCLUDE: Elements that make this trend work
8. DIFFERENTIATION: How to stand out from others using this trend

Return as JSON:
{{
  "hooks": ["hook1", "hook2", "hook3"],
  "script_outline": {{
    "opening": "...",
    "setup": "...",
    "payoff": "...",
    "cta": "..."
  }},
  "caption_templates": ["caption1", "caption2", "caption3"],
  "angles": ["angle1", "angle2", "angle3"],
  "shotlist": {{
    "broll": ["suggestion1", "suggestion2"],
    "on_screen_text": ["text1", "text2"],
    "transitions": ["cut", "zoom"]
  }},
  "cta": {{
    "primary": "...",
    "alternatives": ["...", "..."]
  }},
  "must_include": ["element1", "element2"],
  "differentiation": "..."
}}"""

        try:
            response = self.openai_client.chat.completions.create(
                model="gpt-4o-mini",
                messages=[
                    {"role": "system", "content": "You are a viral content strategist. Create actionable content briefs. Output only valid JSON."},
                    {"role": "user", "content": prompt}
                ],
                max_tokens=1000,
                temperature=0.8
            )
            
            content = response.choices[0].message.content.strip()
            
            # Clean markdown
            if content.startswith("```"):
                content = content.split("```")[1]
                if content.startswith("json"):
                    content = content[4:]
                content = content.strip()
            
            data = json.loads(content)
            
            return Brief(
                workspace_id=workspace_id,
                cluster_id=cluster_data.get("id"),
                title=f"Brief: {title}",
                platform_target=platform_target,
                format_type="reel" if platform_target in ["tiktok", "instagram"] else "video",
                tone=tone,
                hooks=data.get("hooks", []),
                script_outline=data.get("script_outline", {}),
                caption_templates=data.get("caption_templates", []),
                angles=data.get("angles", []),
                shotlist=[data.get("shotlist", {})],
                cta=data.get("cta", {}),
                must_include=data.get("must_include", []),
                differentiation=data.get("differentiation", ""),
                status=BriefStatus.READY,
                created_at=datetime.now(),
            )
            
        except Exception as e:
            logger.error(f"Failed to generate brief: {e}")
            return None
    
    async def _save_brief(self, brief: Brief) -> str:
        """Save brief to database"""
        with self.engine.connect() as conn:
            result = conn.execute(text("""
                INSERT INTO briefs (
                    workspace_id, cluster_id, title, platform_target,
                    format_type, tone, hooks, script_outline,
                    caption_templates, angles, shotlist, cta, status
                ) VALUES (
                    :workspace_id, :cluster_id, :title, :platform_target,
                    :format_type, :tone, :hooks, :script_outline,
                    :caption_templates, :angles, :shotlist, :cta, :status
                )
                RETURNING id
            """), {
                "workspace_id": brief.workspace_id,
                "cluster_id": brief.cluster_id,
                "title": brief.title,
                "platform_target": brief.platform_target,
                "format_type": brief.format_type,
                "tone": json.dumps(brief.tone),
                "hooks": json.dumps(brief.hooks),
                "script_outline": json.dumps(brief.script_outline),
                "caption_templates": json.dumps(brief.caption_templates),
                "angles": json.dumps(brief.angles),
                "shotlist": json.dumps(brief.shotlist),
                "cta": json.dumps(brief.cta),
                "status": brief.status.value,
            })
            
            brief.id = str(result.fetchone()[0])
            conn.commit()
            
            return brief.id
    
    async def get_brief(self, brief_id: str) -> Optional[Dict]:
        """Get a brief by ID"""
        with self.engine.connect() as conn:
            result = conn.execute(text("""
                SELECT * FROM briefs WHERE id = :brief_id
            """), {"brief_id": brief_id})
            
            row = result.fetchone()
            if row:
                return dict(row._mapping)
        return None
    
    async def list_briefs(
        self,
        workspace_id: str = "00000000-0000-0000-0000-000000000001",
        status: Optional[str] = None,
        limit: int = 20
    ) -> List[Dict]:
        """List briefs for a workspace"""
        with self.engine.connect() as conn:
            query = "SELECT * FROM briefs WHERE workspace_id = :workspace_id"
            params = {"workspace_id": workspace_id, "limit": limit}
            
            if status:
                query += " AND status = :status"
                params["status"] = status
            
            query += " ORDER BY created_at DESC LIMIT :limit"
            
            result = conn.execute(text(query), params)
            return [dict(row._mapping) for row in result.fetchall()]


# Singleton
_brief_service = None

def get_brief_service() -> BriefService:
    global _brief_service
    if _brief_service is None:
        _brief_service = BriefService()
    return _brief_service
