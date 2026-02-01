"""
Flash Generator - Generate video scripts and content from trend clusters
Creates scripts, titles, captions, and comment replies.
"""

import os
from datetime import datetime, timezone
from typing import Dict, List, Optional
from dataclasses import dataclass, field
from uuid import uuid4
from loguru import logger
from sqlalchemy import create_engine, text

try:
    from openai import OpenAI
except ImportError:
    OpenAI = None

from .trend_radar import TrendCluster


DATABASE_URL = os.getenv("DATABASE_URL", "postgresql://postgres:postgres@127.0.0.1:54322/postgres")


@dataclass
class TrendFlashContent:
    """Generated content for a trend cluster."""
    id: str = field(default_factory=lambda: str(uuid4()))
    cluster_id: str = ""
    
    # Script parts
    script_hook: str = ""
    script_context: str = ""
    script_take: str = ""
    script_action: str = ""
    script_cta: str = ""
    script_variant: str = "educational"
    
    # Full script
    full_script: str = ""
    
    # Platform-specific titles
    title_tiktok: str = ""
    title_instagram: str = ""
    title_youtube: str = ""
    title_twitter: str = ""
    
    # Captions (for on-screen text)
    captions: List[Dict] = field(default_factory=list)
    
    # Comment replies
    comment_replies: List[str] = field(default_factory=list)
    follow_up_prompt: str = ""
    
    # Video info
    video_type: str = "remotion"
    video_path: str = ""
    video_url: str = ""
    
    # Status
    status: str = "pending"
    
    def to_dict(self) -> Dict:
        return {
            "id": self.id,
            "cluster_id": self.cluster_id,
            "script": {
                "hook": self.script_hook,
                "context": self.script_context,
                "take": self.script_take,
                "action": self.script_action,
                "cta": self.script_cta,
                "variant": self.script_variant,
                "full": self.full_script
            },
            "titles": {
                "tiktok": self.title_tiktok,
                "instagram": self.title_instagram,
                "youtube": self.title_youtube,
                "twitter": self.title_twitter
            },
            "captions": self.captions,
            "comment_replies": self.comment_replies,
            "follow_up_prompt": self.follow_up_prompt,
            "video_type": self.video_type,
            "video_path": self.video_path,
            "status": self.status
        }


SCRIPT_TEMPLATES = {
    "educational": {
        "hook": "everyone's talking about {trend} today — here's the part they're missing.",
        "context": "it's popping up on {platforms}, and the comments are all saying \"{question}\"",
        "take": "the real move is {rule}.",
        "action": "do this: {step1}, {step2}, {step3}.",
        "cta": "comment \"{keyword}\" and i'll send the exact workflow."
    },
    "contrarian": {
        "hook": "everyone's wrong about {trend} — here's what actually works.",
        "context": "i've seen 100+ comments this week getting this backwards.",
        "take": "forget {common}. instead, {contrarian}.",
        "action": "here's the real play: {step1}, {step2}.",
        "cta": "comment \"{keyword}\" if you want the full breakdown."
    },
    "meme": {
        "hook": "{trend} is everywhere rn and honestly? same.",
        "context": "my timeline is just {observation}.",
        "take": "but real talk, here's what actually matters:",
        "action": "{step1}, {step2}.",
        "cta": "drop a 🔥 if you feel this."
    }
}


class FlashGenerator:
    """
    Generates video content from trend clusters.
    
    Outputs per trend:
    1. Short video script (trend flash template)
    2. Platform-specific titles (3 options)
    3. On-screen captions
    4. Comment replies (10)
    5. Follow-up prompt
    """
    
    def __init__(self):
        self.engine = create_engine(DATABASE_URL)
        api_key = os.getenv("OPENAI_API_KEY")
        self.client = OpenAI(api_key=api_key) if api_key and OpenAI else None
        self._ensure_tables()
        logger.info("✅ FlashGenerator initialized")
    
    def _ensure_tables(self):
        """Create database tables."""
        with self.engine.connect() as conn:
            conn.execute(text("""
                CREATE TABLE IF NOT EXISTS trend_flash_content (
                    id TEXT PRIMARY KEY,
                    cluster_id TEXT,
                    script_hook TEXT,
                    script_context TEXT,
                    script_take TEXT,
                    script_action TEXT,
                    script_cta TEXT,
                    script_variant TEXT DEFAULT 'educational',
                    full_script TEXT,
                    title_tiktok TEXT,
                    title_instagram TEXT,
                    title_youtube TEXT,
                    title_twitter TEXT,
                    captions JSONB DEFAULT '[]',
                    comment_replies TEXT[] DEFAULT '{}',
                    follow_up_prompt TEXT,
                    video_type TEXT DEFAULT 'remotion',
                    video_path TEXT,
                    video_url TEXT,
                    status TEXT DEFAULT 'pending',
                    created_at TIMESTAMP DEFAULT NOW()
                )
            """))
            conn.commit()
    
    async def generate_content(
        self,
        cluster: TrendCluster,
        variant: str = "educational"
    ) -> TrendFlashContent:
        """
        Generate all content for a trend cluster.
        
        Args:
            cluster: The trend cluster to generate for
            variant: Script variant (educational, contrarian, meme)
        
        Returns:
            TrendFlashContent with all generated content
        """
        logger.info(f"🎬 Generating content for: {cluster.topic}")
        
        content = TrendFlashContent(
            cluster_id=cluster.id,
            script_variant=variant
        )
        
        # Generate script
        script = await self._generate_script(cluster, variant)
        content.script_hook = script.get("hook", "")
        content.script_context = script.get("context", "")
        content.script_take = script.get("take", "")
        content.script_action = script.get("action", "")
        content.script_cta = script.get("cta", "")
        content.full_script = self._build_full_script(script)
        
        # Generate titles
        titles = await self._generate_titles(cluster)
        content.title_tiktok = titles.get("tiktok", "")
        content.title_instagram = titles.get("instagram", "")
        content.title_youtube = titles.get("youtube", "")
        content.title_twitter = titles.get("twitter", "")
        
        # Generate captions
        content.captions = self._generate_captions(script)
        
        # Generate comment replies
        content.comment_replies = await self._generate_replies(cluster)
        
        # Generate follow-up prompt
        content.follow_up_prompt = f"want part 2 on {cluster.topic}?"
        
        # Decide video type based on score
        content.video_type = "sora" if cluster.trend_score > 80 else "remotion"
        
        content.status = "ready"
        
        # Save to database
        self._save_content(content)
        
        logger.info(f"✅ Content generated: {content.id}")
        
        return content
    
    async def _generate_script(self, cluster: TrendCluster, variant: str) -> Dict:
        """Generate script using AI or templates."""
        if self.client:
            return await self._generate_script_ai(cluster, variant)
        return self._generate_script_template(cluster, variant)
    
    async def _generate_script_ai(self, cluster: TrendCluster, variant: str) -> Dict:
        """Generate script using AI."""
        try:
            template = SCRIPT_TEMPLATES.get(variant, SCRIPT_TEMPLATES["educational"])
            
            response = self.client.chat.completions.create(
                model="gpt-4o-mini",
                messages=[
                    {
                        "role": "system",
                        "content": f"""Generate a trend flash video script.

Topic: {cluster.topic}
Top Questions: {', '.join(cluster.top_questions[:3])}
Platforms: {', '.join(cluster.platforms)}
Intent Keywords: {', '.join(cluster.intent_keywords_found)}

Use this {variant} template structure:
- Hook (0-2s): {template['hook']}
- Context (2-7s): {template['context']}
- Take (7-20s): {template['take']}
- Action (20-35s): {template['action']}
- CTA (last 3s): {template['cta']}

Return JSON with: hook, context, take, action, cta
Each should be a complete, ready-to-read sentence.
Keep it casual, punchy, and actionable."""
                    }
                ],
                temperature=0.7,
                response_format={"type": "json_object"}
            )
            
            import json
            return json.loads(response.choices[0].message.content)
            
        except Exception as e:
            logger.warning(f"AI script generation failed: {e}")
            return self._generate_script_template(cluster, variant)
    
    def _generate_script_template(self, cluster: TrendCluster, variant: str) -> Dict:
        """Generate script using templates."""
        template = SCRIPT_TEMPLATES.get(variant, SCRIPT_TEMPLATES["educational"])
        
        # Get platforms string
        platforms = " and ".join(cluster.platforms[:2]) if cluster.platforms else "social media"
        
        # Get question
        question = cluster.top_questions[0] if cluster.top_questions else "how do I do this?"
        
        # Generate keyword CTA
        keyword = cluster.topic.split()[0].lower() if cluster.topic else "workflow"
        
        return {
            "hook": template["hook"].format(trend=cluster.topic),
            "context": template["context"].format(
                platforms=platforms,
                question=question[:50]
            ),
            "take": template["take"].format(
                rule="focus on consistency over perfection",
                common="overthinking it",
                contrarian="just start and iterate"
            ),
            "action": template["action"].format(
                step1="start with what you have",
                step2="post consistently",
                step3="engage with your audience"
            ),
            "cta": template["cta"].format(keyword=keyword)
        }
    
    def _build_full_script(self, script: Dict) -> str:
        """Build full script from parts."""
        parts = [
            script.get("hook", ""),
            script.get("context", ""),
            script.get("take", ""),
            script.get("action", ""),
            script.get("cta", "")
        ]
        return "\n\n".join([p for p in parts if p])
    
    async def _generate_titles(self, cluster: TrendCluster) -> Dict:
        """Generate platform-specific titles."""
        if self.client:
            try:
                response = self.client.chat.completions.create(
                    model="gpt-4o-mini",
                    messages=[
                        {
                            "role": "system",
                            "content": f"""Generate video titles for different platforms.

Topic: {cluster.topic}
Summary: {cluster.summary}

Return JSON with:
- tiktok: casual, emoji, hook-first (max 100 chars)
- instagram: clean, engaging, with 1-2 emojis (max 100 chars)  
- youtube: SEO-focused, searchable (max 100 chars)
- twitter: punchy, no hashtags (max 100 chars)"""
                        }
                    ],
                    temperature=0.7,
                    response_format={"type": "json_object"}
                )
                
                import json
                return json.loads(response.choices[0].message.content)
                
            except Exception as e:
                logger.warning(f"AI title generation failed: {e}")
        
        # Fallback titles
        topic = cluster.topic[:50]
        return {
            "tiktok": f"🔥 {topic} - here's what you're missing",
            "instagram": f"✨ The truth about {topic}",
            "youtube": f"How to {topic} (Step-by-Step Guide)",
            "twitter": f"{topic} — the part everyone gets wrong"
        }
    
    def _generate_captions(self, script: Dict) -> List[Dict]:
        """Generate on-screen captions from script."""
        captions = []
        time = 0
        
        # Hook caption
        if script.get("hook"):
            hook_words = script["hook"].split()[:6]
            captions.append({
                "time": time,
                "duration": 2,
                "text": " ".join(hook_words).upper()
            })
            time += 2
        
        # Context caption
        if script.get("context"):
            captions.append({
                "time": time,
                "duration": 5,
                "text": script["context"][:50]
            })
            time += 5
        
        # Take caption
        if script.get("take"):
            captions.append({
                "time": time,
                "duration": 8,
                "text": script["take"][:60]
            })
            time += 8
        
        # Action captions
        if script.get("action"):
            captions.append({
                "time": time,
                "duration": 10,
                "text": script["action"][:80]
            })
            time += 10
        
        # CTA caption
        if script.get("cta"):
            captions.append({
                "time": time,
                "duration": 3,
                "text": script["cta"].upper()
            })
        
        return captions
    
    async def _generate_replies(self, cluster: TrendCluster) -> List[str]:
        """Generate comment reply templates."""
        if self.client:
            try:
                response = self.client.chat.completions.create(
                    model="gpt-4o-mini",
                    messages=[
                        {
                            "role": "system",
                            "content": f"""Generate 10 comment replies for a video about: {cluster.topic}

Top questions from audience:
{chr(10).join(cluster.top_questions[:5])}

Return JSON with:
{{
    "replies": ["reply1", "reply2", ...]
}}

Each reply should:
- Be 1-2 sentences
- Answer the question or add value
- Feel personal and helpful
- Include a soft CTA when appropriate"""
                        }
                    ],
                    temperature=0.8,
                    response_format={"type": "json_object"}
                )
                
                import json
                result = json.loads(response.choices[0].message.content)
                return result.get("replies", [])[:10]
                
            except Exception as e:
                logger.warning(f"AI reply generation failed: {e}")
        
        # Fallback replies
        return [
            f"great question! the key is {cluster.topic.split()[0] if cluster.topic else 'consistency'}",
            "i cover this in depth — check my bio for the full guide",
            "start simple, then scale. don't overcomplicate it!",
            "exactly this 🔥 glad it resonated",
            "drop a follow and i'll post part 2 this week",
            "the workflow is in my pinned — lmk if you have questions",
            "this took me forever to figure out. happy to help!",
            "you're already ahead just by asking. keep going!",
            "love this energy. stay tuned for more",
            "comment 'workflow' and i'll send you the template"
        ]
    
    def _save_content(self, content: TrendFlashContent):
        """Save content to database."""
        import json
        
        with self.engine.connect() as conn:
            conn.execute(text("""
                INSERT INTO trend_flash_content
                (id, cluster_id, script_hook, script_context, script_take, script_action,
                 script_cta, script_variant, full_script, title_tiktok, title_instagram,
                 title_youtube, title_twitter, captions, comment_replies, follow_up_prompt,
                 video_type, status)
                VALUES (:id, :cluster_id, :hook, :context, :take, :action, :cta, :variant,
                        :full, :tiktok, :instagram, :youtube, :twitter, :captions,
                        :replies, :followup, :video_type, :status)
            """), {
                "id": content.id,
                "cluster_id": content.cluster_id,
                "hook": content.script_hook,
                "context": content.script_context,
                "take": content.script_take,
                "action": content.script_action,
                "cta": content.script_cta,
                "variant": content.script_variant,
                "full": content.full_script,
                "tiktok": content.title_tiktok,
                "instagram": content.title_instagram,
                "youtube": content.title_youtube,
                "twitter": content.title_twitter,
                "captions": json.dumps(content.captions),
                "replies": content.comment_replies,
                "followup": content.follow_up_prompt,
                "video_type": content.video_type,
                "status": content.status
            })
            conn.commit()
    
    def get_content(self, content_id: str) -> Optional[TrendFlashContent]:
        """Get content by ID."""
        with self.engine.connect() as conn:
            result = conn.execute(text("""
                SELECT * FROM trend_flash_content WHERE id = :id
            """), {"id": content_id}).fetchone()
            
            if result:
                return self._row_to_content(result)
        return None
    
    def get_content_list(self, status: str = None, limit: int = 20) -> List[TrendFlashContent]:
        """Get list of generated content."""
        condition = "WHERE status = :status" if status else ""
        params = {"limit": limit}
        if status:
            params["status"] = status
        
        with self.engine.connect() as conn:
            results = conn.execute(text(f"""
                SELECT * FROM trend_flash_content
                {condition}
                ORDER BY created_at DESC
                LIMIT :limit
            """), params).fetchall()
            
            return [self._row_to_content(r) for r in results]
    
    def _row_to_content(self, row) -> TrendFlashContent:
        """Convert database row to TrendFlashContent."""
        import json
        
        return TrendFlashContent(
            id=row[0],
            cluster_id=row[1],
            script_hook=row[2] or "",
            script_context=row[3] or "",
            script_take=row[4] or "",
            script_action=row[5] or "",
            script_cta=row[6] or "",
            script_variant=row[7] or "educational",
            full_script=row[8] or "",
            title_tiktok=row[9] or "",
            title_instagram=row[10] or "",
            title_youtube=row[11] or "",
            title_twitter=row[12] or "",
            captions=json.loads(row[13]) if row[13] else [],
            comment_replies=row[14] or [],
            follow_up_prompt=row[15] or "",
            video_type=row[16] or "remotion",
            video_path=row[17] or "",
            video_url=row[18] or "",
            status=row[19] or "pending"
        )


# =============================================================================
# SINGLETON
# =============================================================================

_generator_instance: Optional[FlashGenerator] = None

def get_flash_generator() -> FlashGenerator:
    """Get singleton instance of FlashGenerator."""
    global _generator_instance
    if _generator_instance is None:
        _generator_instance = FlashGenerator()
    return _generator_instance
