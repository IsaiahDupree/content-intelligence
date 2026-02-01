"""
Trend Cards Library
Manages content format templates and pattern detection
"""
import os
from typing import List, Dict, Optional
from loguru import logger
from sqlalchemy import create_engine, text
from datetime import datetime

DATABASE_URL = os.getenv("DATABASE_URL", "postgresql://postgres:postgres@localhost:54322/postgres")


class TrendCardsLibrary:
    """
    Manages the library of content format templates (Trend Cards).
    
    Each card represents a proven content format like:
    - Text-Hook Short-Form
    - POV (Point of View)
    - Tutorial/How-To
    - Storytelling
    - Behind the Scenes
    - Transformation
    - Day in the Life
    - Overhead/Flat Lay
    """
    
    def __init__(self):
        self.engine = create_engine(DATABASE_URL)
        logger.info("Trend Cards Library initialized")
    
    def seed_initial_cards(self):
        """
        Seed the database with initial trend card templates.
        """
        logger.info("Seeding initial trend cards")
        
        initial_cards = [
            {
                "name": "Text-Hook Short-Form",
                "description": "Short video with text overlay hook that grabs attention in first 3 seconds. Common phrases: 'Wait for it', 'Watch till end', 'You won't believe'",
                "format_type": "hook_style"
            },
            {
                "name": "POV (Point of View)",
                "description": "First-person perspective content starting with 'POV:' showing relatable scenarios or experiences",
                "format_type": "pov"
            },
            {
                "name": "Tutorial/How-To",
                "description": "Step-by-step instructional content teaching a skill or process. Clear progression and educational value",
                "format_type": "tutorial"
            },
            {
                "name": "Storytelling",
                "description": "Narrative-driven content with beginning, middle, and end. Often starts with 'Story time' or 'Let me tell you'",
                "format_type": "storytelling"
            },
            {
                "name": "Behind the Scenes",
                "description": "Exclusive look at the process, setup, or preparation. Shows the 'making of' content",
                "format_type": "behind-the-scenes"
            },
            {
                "name": "Transformation",
                "description": "Before and after content showing dramatic change. Includes makeovers, renovations, glow-ups",
                "format_type": "transformation"
            },
            {
                "name": "Day in the Life",
                "description": "Vlog-style content following someone through their daily routine or activities",
                "format_type": "day-in-life"
            },
            {
                "name": "Overhead/Flat Lay",
                "description": "Top-down camera angle showing products, food, or items arranged aesthetically",
                "format_type": "overhead-flat-lay"
            },
            {
                "name": "Reaction Video",
                "description": "Content reacting to another video, trend, or event. Shows genuine emotional response",
                "format_type": "reaction"
            },
            {
                "name": "Challenge/Trend Participation",
                "description": "Participating in viral challenges or trending formats. Often uses trending audio",
                "format_type": "challenge"
            },
            {
                "name": "Product Showcase",
                "description": "Highlighting product features, benefits, or use cases. Often includes demo or unboxing",
                "format_type": "product-showcase"
            },
            {
                "name": "Motivational/Inspirational",
                "description": "Uplifting content with motivational messages, quotes, or success stories",
                "format_type": "motivational"
            },
            {
                "name": "Comedy/Humor",
                "description": "Funny skits, jokes, or humorous takes on everyday situations",
                "format_type": "comedy"
            },
            {
                "name": "Educational Facts",
                "description": "Quick facts, statistics, or educational content. 'Did you know' style",
                "format_type": "educational"
            },
            {
                "name": "Aesthetic/Visual",
                "description": "Visually stunning content focused on cinematography, colors, and aesthetics",
                "format_type": "aesthetic"
            },
            {
                "name": "Time-Lapse",
                "description": "Sped-up footage showing a process or transformation over time",
                "format_type": "time-lapse"
            },
            {
                "name": "Q&A/FAQ",
                "description": "Answering audience questions or addressing common queries",
                "format_type": "qa"
            },
            {
                "name": "Comparison",
                "description": "Side-by-side comparison of products, methods, or options. 'This vs That'",
                "format_type": "comparison"
            },
            {
                "name": "Unboxing",
                "description": "Opening and revealing new products or packages. First impressions",
                "format_type": "unboxing"
            },
            {
                "name": "Life Hack/Tip",
                "description": "Quick tips, tricks, or hacks to make life easier. Practical advice",
                "format_type": "life-hack"
            }
        ]
        
        with self.engine.connect() as conn:
            for card in initial_cards:
                try:
                    conn.execute(text("""
                        INSERT INTO trend_cards (
                            name, description, format_type
                        )
                        VALUES (:name, :description, :format_type)
                        ON CONFLICT DO NOTHING
                    """), card)
                except Exception as e:
                    logger.warning(f"Failed to insert card {card['name']}: {e}")
            
            conn.commit()
        
        logger.info(f"Seeded {len(initial_cards)} trend cards")
        return len(initial_cards)
    
    def get_all_cards(self) -> List[Dict]:
        """Get all trend cards from database"""
        with self.engine.connect() as conn:
            result = conn.execute(text("""
                SELECT 
                    id, name, description, format_type,
                    velocity_7d, trending_score, region
                FROM trend_cards
                ORDER BY trending_score DESC NULLS LAST
            """))
            
            return [
                {
                    "id": str(row[0]),
                    "name": row[1],
                    "description": row[2],
                    "format_type": row[3],
                    "velocity_7d": float(row[4]) if row[4] else 0,
                    "trending_score": float(row[5]) if row[5] else 0,
                    "region": row[6]
                }
                for row in result.fetchall()
            ]
    
    def get_card_by_format_type(self, format_type: str) -> Optional[Dict]:
        """Get a specific trend card by format type"""
        with self.engine.connect() as conn:
            result = conn.execute(text("""
                SELECT 
                    id, name, description, format_type,
                    velocity_7d, trending_score
                FROM trend_cards
                WHERE format_type = :format_type
                LIMIT 1
            """), {"format_type": format_type}).fetchone()
            
            if not result:
                return None
            
            return {
                "id": str(result[0]),
                "name": result[1],
                "description": result[2],
                "format_type": result[3],
                "velocity_7d": float(result[4]) if result[4] else 0,
                "trending_score": float(result[5]) if result[5] else 0
            }
    
    def match_content_to_cards(self, caption: str, hashtags: List[str]) -> List[Dict]:
        """
        Match content to trend cards based on caption and hashtags.
        
        Returns list of matching cards with confidence scores
        """
        caption_lower = caption.lower() if caption else ""
        matches = []
        
        # Define matching patterns for each format type
        patterns = {
            "hook_style": ["wait for it", "watch till end", "wait until", "you won't believe"],
            "pov": ["pov:", "point of view"],
            "tutorial": ["how to", "tutorial", "step by step", "guide", "learn"],
            "storytelling": ["story time", "storytime", "let me tell you", "once upon"],
            "behind-the-scenes": ["bts", "behind the scenes", "backstage", "making of"],
            "transformation": ["before and after", "transformation", "glow up", "makeover"],
            "day-in-life": ["day in", "vlog", "daily routine", "morning routine"],
            "overhead-flat-lay": ["overhead", "flat lay", "top view"],
            "reaction": ["reaction", "reacting to", "my reaction"],
            "challenge": ["challenge", "trend", "viral"],
            "product-showcase": ["review", "showcase", "unboxing", "product"],
            "motivational": ["motivation", "inspire", "believe", "success"],
            "comedy": ["funny", "lol", "comedy", "humor", "joke"],
            "educational": ["did you know", "fact", "learn", "educational"],
            "aesthetic": ["aesthetic", "vibes", "mood"],
            "time-lapse": ["time lapse", "timelapse", "sped up"],
            "qa": ["q&a", "questions", "ask me", "faq"],
            "comparison": ["vs", "versus", "comparison", "this or that"],
            "unboxing": ["unboxing", "unbox", "opening"],
            "life-hack": ["hack", "tip", "trick", "life hack"]
        }
        
        # Check each pattern
        for format_type, keywords in patterns.items():
            confidence = 0
            matched_keywords = []
            
            for keyword in keywords:
                if keyword in caption_lower:
                    confidence += 0.3
                    matched_keywords.append(keyword)
            
            # Check hashtags
            for hashtag in hashtags:
                hashtag_lower = hashtag.lower()
                for keyword in keywords:
                    if keyword.replace(" ", "") in hashtag_lower:
                        confidence += 0.2
                        matched_keywords.append(f"#{hashtag}")
            
            if confidence > 0:
                card = self.get_card_by_format_type(format_type)
                if card:
                    matches.append({
                        **card,
                        "confidence": min(confidence, 1.0),
                        "matched_keywords": matched_keywords
                    })
        
        # Sort by confidence
        matches.sort(key=lambda x: x["confidence"], reverse=True)
        return matches
    
    def update_card_examples(self, card_id: str, media_id: str):
        """Add a media example to a trend card"""
        with self.engine.connect() as conn:
            conn.execute(text("""
                UPDATE trend_cards
                SET example_media_ids = array_append(
                    COALESCE(example_media_ids, ARRAY[]::TEXT[]),
                    :media_id
                )
                WHERE id = :card_id
                  AND NOT (:media_id = ANY(COALESCE(example_media_ids, ARRAY[]::TEXT[])))
            """), {"card_id": card_id, "media_id": media_id})
            conn.commit()


# Singleton instance
_library_instance = None

def get_trend_cards_library() -> TrendCardsLibrary:
    """Get or create trend cards library singleton"""
    global _library_instance
    if _library_instance is None:
        _library_instance = TrendCardsLibrary()
    return _library_instance
