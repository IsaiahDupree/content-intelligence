"""
Cluster Service - Group posts into trends and score them
"""
import os
import json
from datetime import datetime, timedelta
from typing import List, Dict, Any, Optional
from collections import defaultdict

from loguru import logger
from sqlalchemy import create_engine, text
from openai import OpenAI

from .models import TrendCluster, TrendScore, ClusterLingo, ClusterType, TrendStatus


DATABASE_URL = os.getenv("DATABASE_URL", "postgresql://postgres:postgres@localhost:54322/postgres")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "")


class ClusterService:
    """
    Service for clustering posts into trends and computing scores.
    
    Pipeline:
    1. Group posts by hashtag/topic/audio
    2. Compute velocity and engagement metrics
    3. Extract lingo patterns using AI
    4. Rank and surface emerging trends
    """
    
    def __init__(self):
        self.engine = create_engine(DATABASE_URL)
        self.openai_client = OpenAI(api_key=OPENAI_API_KEY) if OPENAI_API_KEY else None
    
    # =========================================
    # Clustering Logic
    # =========================================
    
    async def cluster_by_hashtag(
        self,
        workspace_id: str = "00000000-0000-0000-0000-000000000001",
        min_posts: int = 3
    ) -> List[TrendCluster]:
        """Cluster posts by shared hashtags"""
        logger.info("🔍 Clustering posts by hashtag...")
        
        # Get hashtag frequencies
        with self.engine.connect() as conn:
            result = conn.execute(text("""
                SELECT 
                    hashtag,
                    COUNT(*) as post_count,
                    SUM((metrics->>'likes')::int) as total_likes,
                    AVG((metrics->>'likes')::int) as avg_likes,
                    array_agg(id) as post_ids
                FROM posts_raw,
                     jsonb_array_elements_text(hashtags) as hashtag
                WHERE workspace_id = :workspace_id
                  AND posted_at > NOW() - INTERVAL '7 days'
                GROUP BY hashtag
                HAVING COUNT(*) >= :min_posts
                ORDER BY COUNT(*) DESC
                LIMIT 50
            """), {"workspace_id": workspace_id, "min_posts": min_posts})
            
            rows = result.fetchall()
        
        clusters = []
        for row in rows:
            hashtag = row[0]
            post_count = row[1]
            total_likes = row[2] or 0
            avg_likes = row[3] or 0
            post_ids = row[4] or []
            
            # Check if cluster exists
            cluster = await self._get_or_create_cluster(
                workspace_id=workspace_id,
                cluster_type=ClusterType.HASHTAG,
                title=f"#{hashtag}",
                post_ids=post_ids[:20]  # Limit to top 20
            )
            
            if cluster:
                clusters.append(cluster)
        
        logger.success(f"✅ Found {len(clusters)} hashtag clusters")
        return clusters
    
    async def cluster_by_audio(
        self,
        workspace_id: str = "00000000-0000-0000-0000-000000000001",
        min_posts: int = 2
    ) -> List[TrendCluster]:
        """Cluster posts by shared audio/sound"""
        logger.info("🔍 Clustering posts by audio...")
        
        with self.engine.connect() as conn:
            result = conn.execute(text("""
                SELECT 
                    audio_ref->>'sound_id' as sound_id,
                    audio_ref->>'title' as title,
                    COUNT(*) as post_count,
                    array_agg(id) as post_ids
                FROM posts_raw
                WHERE workspace_id = :workspace_id
                  AND audio_ref IS NOT NULL
                  AND audio_ref->>'sound_id' IS NOT NULL
                  AND posted_at > NOW() - INTERVAL '7 days'
                GROUP BY audio_ref->>'sound_id', audio_ref->>'title'
                HAVING COUNT(*) >= :min_posts
                ORDER BY COUNT(*) DESC
                LIMIT 30
            """), {"workspace_id": workspace_id, "min_posts": min_posts})
            
            rows = result.fetchall()
        
        clusters = []
        for row in rows:
            sound_id = row[0]
            title = row[1] or "Unknown Sound"
            post_ids = row[3] or []
            
            cluster = await self._get_or_create_cluster(
                workspace_id=workspace_id,
                cluster_type=ClusterType.SOUND,
                title=f"🎵 {title}",
                post_ids=post_ids[:20]
            )
            
            if cluster:
                clusters.append(cluster)
        
        logger.success(f"✅ Found {len(clusters)} audio clusters")
        return clusters
    
    async def _get_or_create_cluster(
        self,
        workspace_id: str,
        cluster_type: ClusterType,
        title: str,
        post_ids: List[str]
    ) -> Optional[TrendCluster]:
        """Get existing cluster or create new one"""
        with self.engine.connect() as conn:
            # Check if exists
            result = conn.execute(text("""
                SELECT id FROM trend_clusters
                WHERE workspace_id = :workspace_id
                  AND cluster_type = :cluster_type
                  AND title = :title
            """), {
                "workspace_id": workspace_id,
                "cluster_type": cluster_type.value,
                "title": title,
            })
            
            row = result.fetchone()
            
            if row:
                cluster_id = str(row[0])
            else:
                # Create new cluster
                result = conn.execute(text("""
                    INSERT INTO trend_clusters (workspace_id, cluster_type, title, status)
                    VALUES (:workspace_id, :cluster_type, :title, 'emerging')
                    RETURNING id
                """), {
                    "workspace_id": workspace_id,
                    "cluster_type": cluster_type.value,
                    "title": title,
                })
                cluster_id = str(result.fetchone()[0])
                conn.commit()
            
            # Update cluster members
            for post_id in post_ids:
                try:
                    conn.execute(text("""
                        INSERT INTO cluster_members (cluster_id, post_id, weight)
                        VALUES (:cluster_id, :post_id, 1.0)
                        ON CONFLICT (cluster_id, post_id) DO NOTHING
                    """), {"cluster_id": cluster_id, "post_id": post_id})
                except Exception:
                    pass
            
            conn.commit()
            
            return TrendCluster(
                id=cluster_id,
                workspace_id=workspace_id,
                cluster_type=cluster_type,
                title=title,
                post_ids=post_ids,
            )
    
    # =========================================
    # Scoring Logic
    # =========================================
    
    async def compute_scores(
        self,
        workspace_id: str = "00000000-0000-0000-0000-000000000001",
        time_window: str = "24h"
    ) -> List[TrendScore]:
        """Compute velocity and engagement scores for all clusters"""
        logger.info(f"📊 Computing scores for window={time_window}")
        
        # Map window to interval
        intervals = {
            "1h": "1 hour",
            "6h": "6 hours",
            "24h": "24 hours",
            "3d": "3 days",
            "7d": "7 days",
        }
        interval = intervals.get(time_window, "24 hours")
        
        with self.engine.connect() as conn:
            # Get clusters with aggregated metrics
            result = conn.execute(text(f"""
                SELECT 
                    tc.id as cluster_id,
                    tc.title,
                    COUNT(DISTINCT cm.post_id) as mentions,
                    COUNT(DISTINCT pr.author_handle) as creator_count,
                    SUM((pr.metrics->>'likes')::int) as total_engagement,
                    PERCENTILE_CONT(0.5) WITHIN GROUP (
                        ORDER BY (pr.metrics->>'likes')::int
                    ) as engagement_p50
                FROM trend_clusters tc
                JOIN cluster_members cm ON tc.id = cm.cluster_id
                JOIN posts_raw pr ON cm.post_id = pr.id
                WHERE tc.workspace_id = :workspace_id
                  AND pr.posted_at > NOW() - INTERVAL '{interval}'
                GROUP BY tc.id, tc.title
                ORDER BY COUNT(DISTINCT cm.post_id) DESC
            """), {"workspace_id": workspace_id})
            
            rows = result.fetchall()
        
        scores = []
        for row in rows:
            cluster_id = str(row[0])
            mentions = row[2] or 0
            creator_count = row[3] or 0
            total_engagement = row[4] or 0
            engagement_p50 = row[5] or 0
            
            # Calculate velocity (simple: mentions per hour)
            hours = {"1h": 1, "6h": 6, "24h": 24, "3d": 72, "7d": 168}.get(time_window, 24)
            velocity = mentions / hours if hours > 0 else 0
            
            # Creator diversity
            diversity = creator_count / mentions if mentions > 0 else 0
            
            # Saturation (inverse of diversity - high diversity = low saturation)
            saturation = 1 - diversity
            
            # Combined score (weighted formula)
            score = (
                velocity * 0.3 +
                (engagement_p50 / 1000) * 0.3 +  # Normalize engagement
                diversity * 0.2 +
                (mentions / 100) * 0.2  # Normalize mentions
            )
            
            trend_score = TrendScore(
                cluster_id=cluster_id,
                time_window=time_window,
                mentions=mentions,
                velocity=velocity,
                engagement_sum=total_engagement,
                engagement_p50=engagement_p50,
                creator_count=creator_count,
                creator_diversity=diversity,
                saturation=saturation,
                score=score,
                computed_at=datetime.now(),
            )
            
            # Save score
            await self._save_score(trend_score)
            scores.append(trend_score)
            
            # Update cluster status based on velocity
            await self._update_cluster_status(cluster_id, velocity)
        
        logger.success(f"✅ Computed scores for {len(scores)} clusters")
        return scores
    
    async def _save_score(self, score: TrendScore):
        """Save a trend score to database"""
        with self.engine.connect() as conn:
            conn.execute(text("""
                INSERT INTO trend_scores (
                    cluster_id, time_window, mentions, velocity,
                    engagement_p50, creator_count, creator_diversity,
                    saturation, score, computed_at
                ) VALUES (
                    :cluster_id, :time_window, :mentions, :velocity,
                    :engagement_p50, :creator_count, :creator_diversity,
                    :saturation, :score, :computed_at
                )
            """), {
                "cluster_id": score.cluster_id,
                "time_window": score.time_window,
                "mentions": score.mentions,
                "velocity": score.velocity,
                "engagement_p50": score.engagement_p50,
                "creator_count": score.creator_count,
                "creator_diversity": score.creator_diversity,
                "saturation": score.saturation,
                "score": score.score,
                "computed_at": score.computed_at,
            })
            conn.commit()
    
    async def _update_cluster_status(self, cluster_id: str, velocity: float):
        """Update cluster status based on velocity"""
        if velocity > 5:
            status = "peak"
        elif velocity > 2:
            status = "rising"
        elif velocity > 0.5:
            status = "emerging"
        else:
            status = "declining"
        
        with self.engine.connect() as conn:
            conn.execute(text("""
                UPDATE trend_clusters SET status = :status, updated_at = NOW()
                WHERE id = :cluster_id
            """), {"cluster_id": cluster_id, "status": status})
            conn.commit()
    
    # =========================================
    # Lingo Extraction
    # =========================================
    
    async def extract_lingo(
        self,
        cluster_id: str
    ) -> Optional[ClusterLingo]:
        """Extract language patterns from a cluster using AI"""
        if not self.openai_client:
            logger.warning("OpenAI client not configured")
            return None
        
        logger.info(f"✍️ Extracting lingo for cluster {cluster_id}")
        
        # Get sample captions from cluster
        with self.engine.connect() as conn:
            result = conn.execute(text("""
                SELECT pr.caption_text
                FROM cluster_members cm
                JOIN posts_raw pr ON cm.post_id = pr.id
                WHERE cm.cluster_id = :cluster_id
                LIMIT 20
            """), {"cluster_id": cluster_id})
            
            captions = [row[0] for row in result.fetchall() if row[0]]
        
        if not captions:
            return None
        
        # Use AI to extract patterns
        prompt = f"""Analyze these {len(captions)} social media captions from a trending topic.

CAPTIONS:
{chr(10).join(f'- {c[:200]}' for c in captions[:10])}

Extract:
1. KEY_PHRASES: 3-5 phrases that appear repeatedly or are characteristic
2. HOOK_PATTERNS: 2-3 common opening patterns (first 10 words)
3. TONE: One word describing the overall tone (edgy/wholesome/professional/humorous)
4. MEANING: 1-2 sentences explaining what this trend is about
5. STRUCTURE: The typical format (e.g., "hook → problem → solution → CTA")
6. BRAND_SAFETY: Score 0-1 and any flags (profanity, controversial topics)

Return as JSON:
{{
  "key_phrases": ["phrase1", "phrase2"],
  "hook_patterns": ["pattern1", "pattern2"],
  "tone": "word",
  "meaning": "explanation",
  "structure": {{"parts": ["hook", "problem", "solution"]}},
  "brand_safety_score": 0.8,
  "brand_safety_flags": []
}}"""

        try:
            response = self.openai_client.chat.completions.create(
                model="gpt-4o-mini",
                messages=[
                    {"role": "system", "content": "You are a social media trends analyst. Output only valid JSON."},
                    {"role": "user", "content": prompt}
                ],
                max_tokens=500,
                temperature=0.7
            )
            
            content = response.choices[0].message.content.strip()
            
            # Clean markdown if present
            if content.startswith("```"):
                content = content.split("```")[1]
                if content.startswith("json"):
                    content = content[4:]
                content = content.strip()
            
            data = json.loads(content)
            
            lingo = ClusterLingo(
                cluster_id=cluster_id,
                key_phrases=data.get("key_phrases", []),
                hook_patterns=data.get("hook_patterns", []),
                tone=data.get("tone", ""),
                meaning=data.get("meaning", ""),
                structure=data.get("structure", {}),
                brand_safety_score=data.get("brand_safety_score", 0.5),
                brand_safety_flags=data.get("brand_safety_flags", []),
                example_captions=captions[:5],
                updated_at=datetime.now(),
            )
            
            # Save to database
            await self._save_lingo(lingo)
            
            return lingo
            
        except Exception as e:
            logger.error(f"Failed to extract lingo: {e}")
            return None
    
    async def _save_lingo(self, lingo: ClusterLingo):
        """Save cluster lingo to database"""
        with self.engine.connect() as conn:
            conn.execute(text("""
                INSERT INTO cluster_lingo (
                    cluster_id, key_phrases, hook_patterns, usage_notes,
                    meaning, structure, tone, brand_safety_score,
                    brand_safety_flags, updated_at
                ) VALUES (
                    :cluster_id, :key_phrases, :hook_patterns, :usage_notes,
                    :meaning, :structure, :tone, :brand_safety_score,
                    :brand_safety_flags, :updated_at
                )
                ON CONFLICT (cluster_id) DO UPDATE SET
                    key_phrases = :key_phrases,
                    hook_patterns = :hook_patterns,
                    meaning = :meaning,
                    structure = :structure,
                    tone = :tone,
                    brand_safety_score = :brand_safety_score,
                    brand_safety_flags = :brand_safety_flags,
                    updated_at = :updated_at
            """), {
                "cluster_id": lingo.cluster_id,
                "key_phrases": json.dumps(lingo.key_phrases),
                "hook_patterns": json.dumps(lingo.hook_patterns),
                "usage_notes": lingo.usage_notes,
                "meaning": lingo.meaning,
                "structure": json.dumps(lingo.structure),
                "tone": lingo.tone,
                "brand_safety_score": lingo.brand_safety_score,
                "brand_safety_flags": json.dumps(lingo.brand_safety_flags),
                "updated_at": lingo.updated_at,
            })
            conn.commit()
    
    # =========================================
    # Query Methods
    # =========================================
    
    async def get_trending(
        self,
        workspace_id: str = "00000000-0000-0000-0000-000000000001",
        status: Optional[str] = None,
        limit: int = 20
    ) -> List[Dict]:
        """Get trending clusters with scores"""
        with self.engine.connect() as conn:
            query = """
                SELECT 
                    tc.*,
                    ts.score,
                    ts.velocity,
                    ts.mentions,
                    ts.creator_diversity,
                    cl.key_phrases,
                    cl.hook_patterns,
                    cl.meaning
                FROM trend_clusters tc
                LEFT JOIN LATERAL (
                    SELECT * FROM trend_scores
                    WHERE cluster_id = tc.id
                    ORDER BY computed_at DESC
                    LIMIT 1
                ) ts ON true
                LEFT JOIN cluster_lingo cl ON tc.id = cl.cluster_id
                WHERE tc.workspace_id = :workspace_id
            """
            params = {"workspace_id": workspace_id, "limit": limit}
            
            if status:
                query += " AND tc.status = :status"
                params["status"] = status
            
            query += " ORDER BY ts.score DESC NULLS LAST LIMIT :limit"
            
            result = conn.execute(text(query), params)
            rows = result.fetchall()
            
            return [dict(row._mapping) for row in rows]


# Singleton
_cluster_service = None

def get_cluster_service() -> ClusterService:
    global _cluster_service
    if _cluster_service is None:
        _cluster_service = ClusterService()
    return _cluster_service
