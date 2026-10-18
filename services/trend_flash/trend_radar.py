"""
Trend Radar - Real-time trend detection and clustering
Detects trending topics every 15-60 minutes across platforms.
"""

import os
from datetime import datetime, timezone, timedelta
from typing import Dict, List, Optional, Tuple
from dataclasses import dataclass, field
from uuid import uuid4
from collections import Counter
from loguru import logger
from sqlalchemy import create_engine, text

try:
    from openai import OpenAI
except ImportError:
    OpenAI = None


DATABASE_URL = os.getenv("DATABASE_URL", "postgresql://postgres:postgres@127.0.0.1:54322/postgres")

INTENT_KEYWORDS = [
    "how do i", "how to", "what tool", "which app",
    "tutorial", "template", "workflow", "step by step",
    "show me", "teach me", "help me", "guide",
    "what's the best", "recommend", "tips for",
    "can you explain", "where do i", "what do you use"
]


@dataclass
class TrendCluster:
    """A detected trend cluster."""
    id: str = field(default_factory=lambda: str(uuid4()))
    topic: str = ""
    keywords: List[str] = field(default_factory=list)
    summary: str = ""
    
    # Velocity metrics
    velocity: float = 0.0  # mentions per hour
    mentions_count: int = 0
    unique_authors: int = 0
    
    # Cross-platform
    platforms: List[str] = field(default_factory=list)
    platform_count: int = 1
    
    # Intent signals
    top_questions: List[str] = field(default_factory=list)
    intent_keywords_found: List[str] = field(default_factory=list)
    intent_score: float = 1.0
    
    # Scoring
    trend_score: float = 0.0
    cross_platform_multiplier: float = 1.0
    intent_multiplier: float = 1.0
    
    # Status
    status: str = "detected"
    shipped_at: Optional[datetime] = None
    
    # Timestamps
    first_seen_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    last_seen_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    
    def to_dict(self) -> Dict:
        return {
            "id": self.id,
            "topic": self.topic,
            "keywords": self.keywords,
            "summary": self.summary,
            "velocity": self.velocity,
            "mentions_count": self.mentions_count,
            "unique_authors": self.unique_authors,
            "platforms": self.platforms,
            "platform_count": self.platform_count,
            "top_questions": self.top_questions,
            "intent_keywords_found": self.intent_keywords_found,
            "intent_score": self.intent_score,
            "trend_score": self.trend_score,
            "cross_platform_multiplier": self.cross_platform_multiplier,
            "intent_multiplier": self.intent_multiplier,
            "status": self.status,
            "first_seen_at": self.first_seen_at.isoformat() if self.first_seen_at else None,
            "last_seen_at": self.last_seen_at.isoformat() if self.last_seen_at else None
        }


class TrendRadar:
    """
    Real-time trend detection and clustering.
    
    Pipeline:
    1. Collect messages from all platforms
    2. Extract topics and cluster similar ones
    3. Track velocity (mentions/hour)
    4. Score based on cross-platform presence and intent signals
    """
    
    def __init__(self):
        self.engine = create_engine(DATABASE_URL)
        api_key = os.getenv("OPENAI_API_KEY")
        self.client = OpenAI(api_key=api_key) if api_key and OpenAI else None
        self._ensure_tables()
        logger.info("✅ TrendRadar initialized")
    
    def _ensure_tables(self):
        """Create database tables if they don't exist."""
        with self.engine.connect() as conn:
            conn.execute(text("""
                CREATE TABLE IF NOT EXISTS trend_clusters (
                    id TEXT PRIMARY KEY,
                    topic TEXT NOT NULL,
                    keywords TEXT[] DEFAULT '{}',
                    summary TEXT,
                    velocity FLOAT DEFAULT 0,
                    mentions_count INTEGER DEFAULT 0,
                    unique_authors INTEGER DEFAULT 0,
                    platforms TEXT[] DEFAULT '{}',
                    platform_count INTEGER DEFAULT 1,
                    top_questions TEXT[] DEFAULT '{}',
                    intent_keywords_found TEXT[] DEFAULT '{}',
                    intent_score FLOAT DEFAULT 1.0,
                    trend_score FLOAT DEFAULT 0,
                    cross_platform_multiplier FLOAT DEFAULT 1.0,
                    intent_multiplier FLOAT DEFAULT 1.0,
                    status TEXT DEFAULT 'detected',
                    shipped_at TIMESTAMP,
                    first_seen_at TIMESTAMP DEFAULT NOW(),
                    last_seen_at TIMESTAMP DEFAULT NOW(),
                    created_at TIMESTAMP DEFAULT NOW()
                )
            """))
            
            conn.execute(text("""
                CREATE INDEX IF NOT EXISTS idx_clusters_score 
                ON trend_clusters(trend_score DESC)
            """))
            
            conn.execute(text("""
                CREATE INDEX IF NOT EXISTS idx_clusters_status 
                ON trend_clusters(status)
            """))
            
            conn.commit()
        
        logger.info("✅ Trend radar tables created")
    
    async def detect_trends(self, hours_back: int = 1) -> List[TrendCluster]:
        """
        Run a full detection cycle.
        
        1. Collect recent messages from all platforms
        2. Extract topics using AI
        3. Cluster similar topics
        4. Calculate velocity and scores
        5. Save to database
        
        Returns:
            List of detected trend clusters
        """
        logger.info("🔍 Starting trend detection cycle...")
        
        # Step 1: Collect messages
        messages = await self._collect_messages(hours_back)
        logger.info(f"📥 Collected {len(messages)} messages")
        
        if not messages:
            return []
        
        # Step 2: Extract topics
        topics = await self._extract_topics(messages)
        logger.info(f"📊 Extracted {len(topics)} topics")
        
        # Step 3: Cluster similar topics
        clusters = self._cluster_topics(topics, messages)
        logger.info(f"🔗 Created {len(clusters)} clusters")
        
        # Step 4: Calculate scores
        for cluster in clusters:
            self._calculate_score(cluster)
        
        # Step 5: Save to database
        for cluster in clusters:
            self._save_cluster(cluster)
        
        # Sort by score
        clusters.sort(key=lambda c: c.trend_score, reverse=True)
        
        logger.info(f"✅ Detection complete: {len(clusters)} trends found")
        
        return clusters
    
    async def _collect_messages(self, hours_back: int = 1) -> List[Dict]:
        """Collect recent messages from all platforms."""
        messages = []
        cutoff = datetime.now(timezone.utc) - timedelta(hours=hours_back)
        
        try:
            # Get from inbox
            from services.inbox import get_inbox_service
            inbox = get_inbox_service()
            
            inbox_messages = inbox.get_messages(limit=500)
            
            for msg in inbox_messages:
                messages.append({
                    "id": msg.id,
                    "platform": msg.platform,
                    "content": msg.content,
                    "sender": msg.sender_username,
                    "timestamp": msg.received_at
                })
            
        except Exception as e:
            logger.warning(f"Inbox collection failed: {e}")
        
        try:
            # Get from DM outreach trends
            from services.sora_daily import get_trend_collector
            collector = get_trend_collector()
            
            trends = collector.get_recent_trends(limit=50)
            for trend in trends:
                messages.append({
                    "id": trend.id,
                    "platform": trend.source_type,
                    "content": trend.topic,
                    "sender": "trend",
                    "timestamp": trend.collected_at
                })
                
        except Exception as e:
            logger.debug(f"Trend collection skipped: {e}")
        
        return messages
    
    async def _extract_topics(self, messages: List[Dict]) -> List[Dict]:
        """Extract topics from messages using AI."""
        if not messages:
            return []
        
        if not self.client:
            # Fallback: simple keyword extraction
            return self._extract_topics_simple(messages)
        
        try:
            # Combine messages for AI analysis
            combined = "\n".join([
                f"[{m.get('platform', 'unknown')}] {m.get('content', '')[:200]}"
                for m in messages[:100]
            ])
            
            response = self.client.chat.completions.create(
                model="gpt-4o-mini",
                messages=[
                    {
                        "role": "system",
                        "content": """Extract trending topics from these social media comments/messages.

Return JSON with:
{
    "topics": [
        {
            "topic": "short topic name",
            "keywords": ["keyword1", "keyword2"],
            "summary": "one sentence summary",
            "questions": ["top question 1", "top question 2"],
            "platforms": ["platform1", "platform2"],
            "mention_count": estimated_count
        }
    ]
}

Focus on:
- Topics with high engagement/questions
- Topics appearing across multiple platforms
- Topics with intent signals (how to, what tool, etc.)"""
                    },
                    {
                        "role": "user",
                        "content": combined
                    }
                ],
                temperature=0.3,
                response_format={"type": "json_object"}
            )
            
            import json
            result = json.loads(response.choices[0].message.content)
            return result.get("topics", [])
            
        except Exception as e:
            logger.error(f"AI topic extraction failed: {e}")
            return self._extract_topics_simple(messages)
    
    def _extract_topics_simple(self, messages: List[Dict]) -> List[Dict]:
        """Simple keyword-based topic extraction."""
        topics = []
        
        # Count word frequencies
        word_counts = Counter()
        platform_words = {}
        
        for msg in messages:
            content = msg.get("content", "").lower()
            platform = msg.get("platform", "unknown")
            
            # Simple word extraction
            words = content.split()
            for word in words:
                if len(word) > 4:
                    word_counts[word] += 1
                    if word not in platform_words:
                        platform_words[word] = set()
                    platform_words[word].add(platform)
        
        # Get top words as topics
        for word, count in word_counts.most_common(10):
            topics.append({
                "topic": word,
                "keywords": [word],
                "summary": f"Topic about {word}",
                "questions": [],
                "platforms": list(platform_words.get(word, [])),
                "mention_count": count
            })
        
        return topics
    
    def _cluster_topics(self, topics: List[Dict], messages: List[Dict]) -> List[TrendCluster]:
        """Cluster similar topics and create TrendCluster objects."""
        clusters = []
        
        for topic_data in topics:
            # Find matching messages
            topic_lower = topic_data.get("topic", "").lower()
            keywords = [k.lower() for k in topic_data.get("keywords", [])]
            
            matching_messages = []
            authors = set()
            platforms = set()
            
            for msg in messages:
                content = msg.get("content", "").lower()
                if topic_lower in content or any(k in content for k in keywords):
                    matching_messages.append(msg)
                    authors.add(msg.get("sender", ""))
                    platforms.add(msg.get("platform", ""))
            
            # Find intent keywords
            intent_found = []
            questions = []
            
            for msg in matching_messages:
                content = msg.get("content", "").lower()
                
                for intent in INTENT_KEYWORDS:
                    if intent in content and intent not in intent_found:
                        intent_found.append(intent)
                
                if "?" in msg.get("content", ""):
                    questions.append(msg.get("content", "")[:100])
            
            # Create cluster
            cluster = TrendCluster(
                topic=topic_data.get("topic", ""),
                keywords=topic_data.get("keywords", []),
                summary=topic_data.get("summary", ""),
                mentions_count=len(matching_messages),
                unique_authors=len(authors),
                platforms=list(platforms) or topic_data.get("platforms", []),
                platform_count=len(platforms) or len(topic_data.get("platforms", [])),
                top_questions=questions[:5] or topic_data.get("questions", []),
                intent_keywords_found=intent_found[:5]
            )
            
            # Calculate velocity (mentions per hour)
            cluster.velocity = cluster.mentions_count  # Simplified for 1-hour window
            
            clusters.append(cluster)
        
        return clusters
    
    def _calculate_score(self, cluster: TrendCluster):
        """Calculate trend score using the formula."""
        # Cross-platform multiplier
        if cluster.platform_count >= 3:
            cluster.cross_platform_multiplier = 1.6  # +60%
        elif cluster.platform_count == 2:
            cluster.cross_platform_multiplier = 1.3  # +30%
        else:
            cluster.cross_platform_multiplier = 1.0
        
        # Intent multiplier
        intent_count = len(cluster.intent_keywords_found)
        if intent_count >= 3:
            cluster.intent_multiplier = 1.8  # +80%
        elif intent_count >= 1:
            cluster.intent_multiplier = 1.5  # +50%
        else:
            cluster.intent_multiplier = 1.0
        
        # Calculate intent score
        cluster.intent_score = cluster.intent_multiplier
        
        # Final score
        cluster.trend_score = (
            cluster.velocity * 
            cluster.cross_platform_multiplier * 
            cluster.intent_multiplier
        )
    
    def _save_cluster(self, cluster: TrendCluster):
        """Save cluster to database."""
        with self.engine.connect() as conn:
            conn.execute(text("""
                INSERT INTO trend_clusters 
                (id, topic, keywords, summary, velocity, mentions_count, unique_authors,
                 platforms, platform_count, top_questions, intent_keywords_found,
                 intent_score, trend_score, cross_platform_multiplier, intent_multiplier,
                 status, first_seen_at, last_seen_at)
                VALUES (:id, :topic, :keywords, :summary, :velocity, :mentions_count, :unique_authors,
                        :platforms, :platform_count, :top_questions, :intent_keywords_found,
                        :intent_score, :trend_score, :cross_platform_multiplier, :intent_multiplier,
                        :status, :first_seen_at, :last_seen_at)
                ON CONFLICT (id) DO UPDATE SET
                    velocity = EXCLUDED.velocity,
                    mentions_count = EXCLUDED.mentions_count,
                    trend_score = EXCLUDED.trend_score,
                    last_seen_at = EXCLUDED.last_seen_at
            """), {
                "id": cluster.id,
                "topic": cluster.topic,
                "keywords": cluster.keywords,
                "summary": cluster.summary,
                "velocity": cluster.velocity,
                "mentions_count": cluster.mentions_count,
                "unique_authors": cluster.unique_authors,
                "platforms": cluster.platforms,
                "platform_count": cluster.platform_count,
                "top_questions": cluster.top_questions,
                "intent_keywords_found": cluster.intent_keywords_found,
                "intent_score": cluster.intent_score,
                "trend_score": cluster.trend_score,
                "cross_platform_multiplier": cluster.cross_platform_multiplier,
                "intent_multiplier": cluster.intent_multiplier,
                "status": cluster.status,
                "first_seen_at": cluster.first_seen_at,
                "last_seen_at": cluster.last_seen_at
            })
            conn.commit()
    
    def get_top_clusters(self, limit: int = 3) -> List[TrendCluster]:
        """Get top-scored clusters ready for content generation."""
        with self.engine.connect() as conn:
            results = conn.execute(text("""
                SELECT * FROM trend_clusters
                WHERE status = 'detected'
                ORDER BY trend_score DESC
                LIMIT :limit
            """), {"limit": limit}).fetchall()
            
            return [self._row_to_cluster(r) for r in results]
    
    def get_clusters(
        self,
        status: Optional[str] = None,
        min_score: float = 0,
        limit: int = 20
    ) -> List[TrendCluster]:
        """Get clusters with filters."""
        conditions = ["trend_score >= :min_score"]
        params = {"min_score": min_score, "limit": limit}
        
        if status:
            conditions.append("status = :status")
            params["status"] = status
        
        with self.engine.connect() as conn:
            results = conn.execute(text(f"""
                SELECT * FROM trend_clusters
                WHERE {' AND '.join(conditions)}
                ORDER BY trend_score DESC
                LIMIT :limit
            """), params).fetchall()
            
            return [self._row_to_cluster(r) for r in results]
    
    def update_cluster_status(self, cluster_id: str, status: str):
        """Update cluster status."""
        with self.engine.connect() as conn:
            conn.execute(text("""
                UPDATE trend_clusters 
                SET status = :status,
                    shipped_at = CASE WHEN :status = 'shipped' THEN NOW() ELSE shipped_at END
                WHERE id = :id
            """), {"id": cluster_id, "status": status})
            conn.commit()
    
    def _row_to_cluster(self, row) -> TrendCluster:
        """Convert database row to TrendCluster."""
        return TrendCluster(
            id=row[0],
            topic=row[1],
            keywords=row[2] or [],
            summary=row[3] or "",
            velocity=row[4] or 0,
            mentions_count=row[5] or 0,
            unique_authors=row[6] or 0,
            platforms=row[7] or [],
            platform_count=row[8] or 1,
            top_questions=row[9] or [],
            intent_keywords_found=row[10] or [],
            intent_score=row[11] or 1.0,
            trend_score=row[12] or 0,
            cross_platform_multiplier=row[13] or 1.0,
            intent_multiplier=row[14] or 1.0,
            status=row[15] or "detected",
            shipped_at=row[16],
            first_seen_at=row[17],
            last_seen_at=row[18]
        )


# =============================================================================
# SINGLETON
# =============================================================================

_radar_instance: Optional[TrendRadar] = None

def get_trend_radar() -> TrendRadar:
    """Get singleton instance of TrendRadar."""
    global _radar_instance
    if _radar_instance is None:
        _radar_instance = TrendRadar()
    return _radar_instance
