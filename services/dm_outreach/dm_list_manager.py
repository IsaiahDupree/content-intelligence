"""
DM List Manager
Manages DM outreach lists and prospect tracking.
"""

import os
from datetime import datetime, timezone, date, timedelta
from typing import Dict, List, Optional, Any
from dataclasses import dataclass, field
from enum import Enum
from uuid import uuid4
from loguru import logger
from sqlalchemy import create_engine, text


DATABASE_URL = os.getenv("DATABASE_URL", "postgresql://postgres:postgres@127.0.0.1:54322/postgres")


class ProspectStatus(str, Enum):
    NEW = "new"
    CONTACTED = "contacted"
    REPLIED = "replied"
    NURTURING = "nurturing"
    OFFER_READY = "offer_ready"
    CONVERTED = "converted"
    ARCHIVED = "archived"


class OutreachPhase(str, Enum):
    INTRODUCTION = "introduction"
    VALUE = "value"
    RELATIONSHIP = "relationship"
    OFFER = "offer"


class DiscoverySource(str, Enum):
    COMMENT = "comment"
    FOLLOWER = "follower"
    COMPETITOR = "competitor"
    HASHTAG = "hashtag"
    LIKER = "liker"
    VIEWER = "viewer"
    MANUAL = "manual"


@dataclass
class Prospect:
    """A potential DM target."""
    id: str = field(default_factory=lambda: str(uuid4()))
    platform: str = ""
    account_id: int = 0
    username: str = ""
    display_name: str = ""
    bio: str = ""
    follower_count: int = 0
    following_count: int = 0
    post_count: int = 0
    profile_url: str = ""
    avatar_url: str = ""
    
    source: str = DiscoverySource.MANUAL.value
    source_post_id: Optional[str] = None
    source_comment: Optional[str] = None
    discovered_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    
    fit_score: int = 0
    offer_match: Optional[str] = None
    qualified: bool = False
    
    def to_dict(self) -> Dict:
        return {
            "id": self.id,
            "platform": self.platform,
            "account_id": self.account_id,
            "username": self.username,
            "display_name": self.display_name,
            "bio": self.bio,
            "follower_count": self.follower_count,
            "profile_url": self.profile_url,
            "source": self.source,
            "source_comment": self.source_comment,
            "fit_score": self.fit_score,
            "offer_match": self.offer_match,
            "qualified": self.qualified,
            "discovered_at": self.discovered_at.isoformat() if self.discovered_at else None
        }


@dataclass
class DMListEntry:
    """An entry in the DM outreach list."""
    id: str = field(default_factory=lambda: str(uuid4()))
    prospect_id: str = ""
    
    status: str = ProspectStatus.NEW.value
    phase: str = OutreachPhase.INTRODUCTION.value
    
    first_contact_at: Optional[datetime] = None
    last_interaction_at: Optional[datetime] = None
    next_action_date: Optional[date] = None
    interaction_count: int = 0
    
    trust_score: int = 0
    response_rate: float = 0.0
    
    notes: str = ""
    tags: List[str] = field(default_factory=list)
    assigned_to: Optional[str] = None
    
    # Joined prospect data
    prospect: Optional[Prospect] = None
    
    def to_dict(self) -> Dict:
        return {
            "id": self.id,
            "prospect_id": self.prospect_id,
            "status": self.status,
            "phase": self.phase,
            "first_contact_at": self.first_contact_at.isoformat() if self.first_contact_at else None,
            "last_interaction_at": self.last_interaction_at.isoformat() if self.last_interaction_at else None,
            "next_action_date": self.next_action_date.isoformat() if self.next_action_date else None,
            "interaction_count": self.interaction_count,
            "trust_score": self.trust_score,
            "response_rate": self.response_rate,
            "notes": self.notes,
            "tags": self.tags,
            "assigned_to": self.assigned_to,
            "prospect": self.prospect.to_dict() if self.prospect else None
        }


@dataclass
class Offer:
    """An offer that can be presented to prospects."""
    id: str = field(default_factory=lambda: str(uuid4()))
    name: str = ""
    description: str = ""
    price_range: str = ""
    offer_type: str = ""  # coaching, course, affiliate, consulting
    fit_signals: List[str] = field(default_factory=list)
    disqualifiers: List[str] = field(default_factory=list)
    is_active: bool = True
    
    def to_dict(self) -> Dict:
        return {
            "id": self.id,
            "name": self.name,
            "description": self.description,
            "price_range": self.price_range,
            "offer_type": self.offer_type,
            "fit_signals": self.fit_signals,
            "disqualifiers": self.disqualifiers,
            "is_active": self.is_active
        }


class DMListManager:
    """
    Manages DM outreach lists and prospect tracking.
    
    Provides CRUD operations for:
    - Prospects (potential DM targets)
    - DM List entries (outreach status)
    - Offers (products/services to present)
    """
    
    def __init__(self):
        self.engine = create_engine(DATABASE_URL)
        self._ensure_tables()
        logger.info("✅ DMListManager initialized")
    
    def _ensure_tables(self):
        """Create database tables if they don't exist."""
        with self.engine.connect() as conn:
            conn.execute(text("""
                CREATE TABLE IF NOT EXISTS dm_prospects (
                    id TEXT PRIMARY KEY,
                    platform TEXT NOT NULL,
                    account_id INTEGER NOT NULL,
                    username TEXT NOT NULL,
                    display_name TEXT,
                    bio TEXT,
                    follower_count INTEGER DEFAULT 0,
                    following_count INTEGER DEFAULT 0,
                    post_count INTEGER DEFAULT 0,
                    profile_url TEXT,
                    avatar_url TEXT,
                    source TEXT NOT NULL,
                    source_post_id TEXT,
                    source_comment TEXT,
                    discovered_at TIMESTAMP DEFAULT NOW(),
                    fit_score INTEGER DEFAULT 0,
                    offer_match TEXT,
                    qualified BOOLEAN DEFAULT FALSE,
                    created_at TIMESTAMP DEFAULT NOW(),
                    updated_at TIMESTAMP DEFAULT NOW(),
                    UNIQUE(platform, username)
                )
            """))
            
            conn.execute(text("""
                CREATE TABLE IF NOT EXISTS dm_list (
                    id TEXT PRIMARY KEY,
                    prospect_id TEXT REFERENCES dm_prospects(id),
                    status TEXT DEFAULT 'new',
                    phase TEXT DEFAULT 'introduction',
                    first_contact_at TIMESTAMP,
                    last_interaction_at TIMESTAMP,
                    next_action_date DATE,
                    interaction_count INTEGER DEFAULT 0,
                    trust_score INTEGER DEFAULT 0,
                    response_rate FLOAT DEFAULT 0,
                    notes TEXT,
                    tags TEXT[] DEFAULT '{}',
                    assigned_to TEXT,
                    created_at TIMESTAMP DEFAULT NOW(),
                    updated_at TIMESTAMP DEFAULT NOW()
                )
            """))
            
            conn.execute(text("""
                CREATE TABLE IF NOT EXISTS dm_messages (
                    id TEXT PRIMARY KEY,
                    dm_list_id TEXT REFERENCES dm_list(id),
                    direction TEXT NOT NULL,
                    content TEXT NOT NULL,
                    message_type TEXT DEFAULT 'text',
                    template_id TEXT,
                    phase TEXT,
                    platform_message_id TEXT,
                    sent_at TIMESTAMP DEFAULT NOW(),
                    read_at TIMESTAMP,
                    replied_at TIMESTAMP
                )
            """))
            
            conn.execute(text("""
                CREATE TABLE IF NOT EXISTS dm_offers (
                    id TEXT PRIMARY KEY,
                    name TEXT NOT NULL,
                    description TEXT,
                    price_range TEXT,
                    offer_type TEXT,
                    fit_signals TEXT[] DEFAULT '{}',
                    disqualifiers TEXT[] DEFAULT '{}',
                    is_active BOOLEAN DEFAULT TRUE,
                    created_at TIMESTAMP DEFAULT NOW()
                )
            """))
            
            conn.execute(text("""
                CREATE TABLE IF NOT EXISTS account_offers (
                    id TEXT PRIMARY KEY,
                    platform TEXT NOT NULL,
                    account_id INTEGER NOT NULL,
                    offer_id TEXT REFERENCES dm_offers(id),
                    priority INTEGER DEFAULT 1,
                    created_at TIMESTAMP DEFAULT NOW(),
                    UNIQUE(platform, account_id, offer_id)
                )
            """))
            
            conn.execute(text("""
                CREATE INDEX IF NOT EXISTS idx_prospects_platform 
                ON dm_prospects(platform, account_id)
            """))
            
            conn.execute(text("""
                CREATE INDEX IF NOT EXISTS idx_dm_list_status 
                ON dm_list(status)
            """))
            
            conn.commit()
        
        logger.info("✅ DM outreach tables created")
    
    # -------------------------------------------------------------------------
    # PROSPECT MANAGEMENT
    # -------------------------------------------------------------------------
    
    def add_prospect(self, prospect: Prospect) -> Prospect:
        """Add a new prospect to the database."""
        with self.engine.connect() as conn:
            conn.execute(text("""
                INSERT INTO dm_prospects 
                (id, platform, account_id, username, display_name, bio, 
                 follower_count, following_count, post_count, profile_url, avatar_url,
                 source, source_post_id, source_comment, fit_score, offer_match, qualified)
                VALUES (:id, :platform, :account_id, :username, :display_name, :bio,
                        :follower_count, :following_count, :post_count, :profile_url, :avatar_url,
                        :source, :source_post_id, :source_comment, :fit_score, :offer_match, :qualified)
                ON CONFLICT (platform, username) DO UPDATE SET
                    follower_count = EXCLUDED.follower_count,
                    bio = EXCLUDED.bio,
                    updated_at = NOW()
            """), {
                "id": prospect.id,
                "platform": prospect.platform,
                "account_id": prospect.account_id,
                "username": prospect.username,
                "display_name": prospect.display_name,
                "bio": prospect.bio,
                "follower_count": prospect.follower_count,
                "following_count": prospect.following_count,
                "post_count": prospect.post_count,
                "profile_url": prospect.profile_url,
                "avatar_url": prospect.avatar_url,
                "source": prospect.source,
                "source_post_id": prospect.source_post_id,
                "source_comment": prospect.source_comment,
                "fit_score": prospect.fit_score,
                "offer_match": prospect.offer_match,
                "qualified": prospect.qualified
            })
            conn.commit()
        
        return prospect
    
    def get_prospect(self, prospect_id: str) -> Optional[Prospect]:
        """Get a prospect by ID."""
        with self.engine.connect() as conn:
            result = conn.execute(text("""
                SELECT * FROM dm_prospects WHERE id = :id
            """), {"id": prospect_id}).fetchone()
            
            if result:
                return self._row_to_prospect(result)
        return None
    
    def get_prospects(
        self,
        platform: Optional[str] = None,
        account_id: Optional[int] = None,
        source: Optional[str] = None,
        qualified: Optional[bool] = None,
        limit: int = 50,
        offset: int = 0
    ) -> List[Prospect]:
        """Get prospects with filters."""
        conditions = ["1=1"]
        params = {"limit": limit, "offset": offset}
        
        if platform:
            conditions.append("platform = :platform")
            params["platform"] = platform
        if account_id:
            conditions.append("account_id = :account_id")
            params["account_id"] = account_id
        if source:
            conditions.append("source = :source")
            params["source"] = source
        if qualified is not None:
            conditions.append("qualified = :qualified")
            params["qualified"] = qualified
        
        with self.engine.connect() as conn:
            results = conn.execute(text(f"""
                SELECT * FROM dm_prospects
                WHERE {' AND '.join(conditions)}
                ORDER BY fit_score DESC, discovered_at DESC
                LIMIT :limit OFFSET :offset
            """), params).fetchall()
            
            return [self._row_to_prospect(r) for r in results]
    
    def update_prospect_score(self, prospect_id: str, fit_score: int, offer_match: str = None):
        """Update prospect's fit score and offer match."""
        with self.engine.connect() as conn:
            conn.execute(text("""
                UPDATE dm_prospects 
                SET fit_score = :score, offer_match = :offer, qualified = :qualified, updated_at = NOW()
                WHERE id = :id
            """), {
                "id": prospect_id,
                "score": fit_score,
                "offer": offer_match,
                "qualified": fit_score >= 50
            })
            conn.commit()
    
    def _row_to_prospect(self, row) -> Prospect:
        """Convert database row to Prospect."""
        return Prospect(
            id=row[0],
            platform=row[1],
            account_id=row[2],
            username=row[3],
            display_name=row[4] or "",
            bio=row[5] or "",
            follower_count=row[6] or 0,
            following_count=row[7] or 0,
            post_count=row[8] or 0,
            profile_url=row[9] or "",
            avatar_url=row[10] or "",
            source=row[11],
            source_post_id=row[12],
            source_comment=row[13],
            discovered_at=row[14],
            fit_score=row[15] or 0,
            offer_match=row[16],
            qualified=row[17] or False
        )
    
    # -------------------------------------------------------------------------
    # DM LIST MANAGEMENT
    # -------------------------------------------------------------------------
    
    def add_to_dm_list(self, prospect_id: str, assigned_to: str = None) -> DMListEntry:
        """Add a prospect to the DM outreach list."""
        entry = DMListEntry(
            prospect_id=prospect_id,
            assigned_to=assigned_to,
            next_action_date=date.today()
        )
        
        with self.engine.connect() as conn:
            conn.execute(text("""
                INSERT INTO dm_list 
                (id, prospect_id, status, phase, next_action_date, assigned_to)
                VALUES (:id, :prospect_id, :status, :phase, :next_action_date, :assigned_to)
                ON CONFLICT DO NOTHING
            """), {
                "id": entry.id,
                "prospect_id": prospect_id,
                "status": entry.status,
                "phase": entry.phase,
                "next_action_date": entry.next_action_date,
                "assigned_to": assigned_to
            })
            conn.commit()
        
        return entry
    
    def get_dm_list(
        self,
        status: Optional[str] = None,
        phase: Optional[str] = None,
        platform: Optional[str] = None,
        limit: int = 50
    ) -> List[DMListEntry]:
        """Get DM list entries with filters."""
        conditions = ["1=1"]
        params = {"limit": limit}
        
        if status:
            conditions.append("dl.status = :status")
            params["status"] = status
        if phase:
            conditions.append("dl.phase = :phase")
            params["phase"] = phase
        if platform:
            conditions.append("p.platform = :platform")
            params["platform"] = platform
        
        with self.engine.connect() as conn:
            results = conn.execute(text(f"""
                SELECT dl.*, p.* 
                FROM dm_list dl
                JOIN dm_prospects p ON dl.prospect_id = p.id
                WHERE {' AND '.join(conditions)}
                ORDER BY dl.next_action_date ASC NULLS LAST, p.fit_score DESC
                LIMIT :limit
            """), params).fetchall()
            
            entries = []
            for r in results:
                entry = DMListEntry(
                    id=r[0],
                    prospect_id=r[1],
                    status=r[2],
                    phase=r[3],
                    first_contact_at=r[4],
                    last_interaction_at=r[5],
                    next_action_date=r[6],
                    interaction_count=r[7] or 0,
                    trust_score=r[8] or 0,
                    response_rate=r[9] or 0,
                    notes=r[10] or "",
                    tags=r[11] or [],
                    assigned_to=r[12]
                )
                # Prospect starts at index 15 (after dm_list columns)
                entry.prospect = self._row_to_prospect(r[15:])
                entries.append(entry)
            
            return entries
    
    def get_ready_to_contact(self, limit: int = 20) -> List[DMListEntry]:
        """Get prospects ready to contact today."""
        return self.get_dm_list(status=ProspectStatus.NEW.value, limit=limit)
    
    def update_status(self, entry_id: str, status: str) -> bool:
        """Update DM list entry status."""
        with self.engine.connect() as conn:
            result = conn.execute(text("""
                UPDATE dm_list 
                SET status = :status, updated_at = NOW()
                WHERE id = :id
            """), {"id": entry_id, "status": status})
            conn.commit()
            return result.rowcount > 0
    
    def update_phase(self, entry_id: str, phase: str) -> bool:
        """Update DM list entry phase."""
        with self.engine.connect() as conn:
            result = conn.execute(text("""
                UPDATE dm_list 
                SET phase = :phase, updated_at = NOW()
                WHERE id = :id
            """), {"id": entry_id, "phase": phase})
            conn.commit()
            return result.rowcount > 0
    
    def record_interaction(self, entry_id: str, direction: str = "sent"):
        """Record an interaction (sent or received message)."""
        with self.engine.connect() as conn:
            now = datetime.now(timezone.utc)
            
            # Update list entry
            if direction == "sent":
                conn.execute(text("""
                    UPDATE dm_list 
                    SET interaction_count = interaction_count + 1,
                        last_interaction_at = :now,
                        first_contact_at = COALESCE(first_contact_at, :now),
                        status = CASE WHEN status = 'new' THEN 'contacted' ELSE status END,
                        updated_at = NOW()
                    WHERE id = :id
                """), {"id": entry_id, "now": now})
            else:
                # They replied - update status and calculate response rate
                conn.execute(text("""
                    UPDATE dm_list 
                    SET last_interaction_at = :now,
                        status = CASE WHEN status = 'contacted' THEN 'replied' ELSE status END,
                        trust_score = trust_score + 10,
                        updated_at = NOW()
                    WHERE id = :id
                """), {"id": entry_id, "now": now})
            
            conn.commit()
    
    def add_note(self, entry_id: str, note: str):
        """Add a note to a DM list entry."""
        with self.engine.connect() as conn:
            timestamp = datetime.now().strftime("%Y-%m-%d %H:%M")
            conn.execute(text("""
                UPDATE dm_list 
                SET notes = COALESCE(notes, '') || :note,
                    updated_at = NOW()
                WHERE id = :id
            """), {"id": entry_id, "note": f"\n[{timestamp}] {note}"})
            conn.commit()
    
    def schedule_next_action(self, entry_id: str, days_from_now: int = 3):
        """Schedule the next outreach action."""
        next_date = date.today() + timedelta(days=days_from_now)
        with self.engine.connect() as conn:
            conn.execute(text("""
                UPDATE dm_list 
                SET next_action_date = :next_date, updated_at = NOW()
                WHERE id = :id
            """), {"id": entry_id, "next_date": next_date})
            conn.commit()
    
    # -------------------------------------------------------------------------
    # OFFER MANAGEMENT
    # -------------------------------------------------------------------------
    
    def create_offer(self, offer: Offer) -> Offer:
        """Create a new offer."""
        with self.engine.connect() as conn:
            conn.execute(text("""
                INSERT INTO dm_offers 
                (id, name, description, price_range, offer_type, fit_signals, disqualifiers, is_active)
                VALUES (:id, :name, :description, :price_range, :offer_type, :fit_signals, :disqualifiers, :is_active)
            """), {
                "id": offer.id,
                "name": offer.name,
                "description": offer.description,
                "price_range": offer.price_range,
                "offer_type": offer.offer_type,
                "fit_signals": offer.fit_signals,
                "disqualifiers": offer.disqualifiers,
                "is_active": offer.is_active
            })
            conn.commit()
        return offer
    
    def get_offers(self, active_only: bool = True) -> List[Offer]:
        """Get all offers."""
        with self.engine.connect() as conn:
            condition = "WHERE is_active = TRUE" if active_only else ""
            results = conn.execute(text(f"""
                SELECT * FROM dm_offers {condition}
                ORDER BY created_at DESC
            """)).fetchall()
            
            return [
                Offer(
                    id=r[0],
                    name=r[1],
                    description=r[2] or "",
                    price_range=r[3] or "",
                    offer_type=r[4] or "",
                    fit_signals=r[5] or [],
                    disqualifiers=r[6] or [],
                    is_active=r[7]
                )
                for r in results
            ]
    
    def assign_offer_to_account(self, platform: str, account_id: int, offer_id: str, priority: int = 1):
        """Assign an offer to a platform account."""
        with self.engine.connect() as conn:
            conn.execute(text("""
                INSERT INTO account_offers (id, platform, account_id, offer_id, priority)
                VALUES (:id, :platform, :account_id, :offer_id, :priority)
                ON CONFLICT (platform, account_id, offer_id) DO UPDATE SET priority = EXCLUDED.priority
            """), {
                "id": str(uuid4()),
                "platform": platform,
                "account_id": account_id,
                "offer_id": offer_id,
                "priority": priority
            })
            conn.commit()
    
    def get_account_offers(self, platform: str, account_id: int) -> List[Offer]:
        """Get offers assigned to a specific account."""
        with self.engine.connect() as conn:
            results = conn.execute(text("""
                SELECT o.* FROM dm_offers o
                JOIN account_offers ao ON o.id = ao.offer_id
                WHERE ao.platform = :platform AND ao.account_id = :account_id AND o.is_active = TRUE
                ORDER BY ao.priority
            """), {"platform": platform, "account_id": account_id}).fetchall()
            
            return [
                Offer(
                    id=r[0],
                    name=r[1],
                    description=r[2] or "",
                    price_range=r[3] or "",
                    offer_type=r[4] or "",
                    fit_signals=r[5] or [],
                    disqualifiers=r[6] or [],
                    is_active=r[7]
                )
                for r in results
            ]
    
    # -------------------------------------------------------------------------
    # STATISTICS
    # -------------------------------------------------------------------------
    
    def get_stats(self) -> Dict:
        """Get outreach statistics."""
        with self.engine.connect() as conn:
            # Status breakdown
            status_results = conn.execute(text("""
                SELECT status, COUNT(*) FROM dm_list GROUP BY status
            """)).fetchall()
            
            by_status = {r[0]: r[1] for r in status_results}
            
            # Phase breakdown
            phase_results = conn.execute(text("""
                SELECT phase, COUNT(*) FROM dm_list GROUP BY phase
            """)).fetchall()
            
            by_phase = {r[0]: r[1] for r in phase_results}
            
            # Platform breakdown
            platform_results = conn.execute(text("""
                SELECT p.platform, COUNT(*) 
                FROM dm_list dl
                JOIN dm_prospects p ON dl.prospect_id = p.id
                GROUP BY p.platform
            """)).fetchall()
            
            by_platform = {r[0]: r[1] for r in platform_results}
            
            # Totals
            total = conn.execute(text("SELECT COUNT(*) FROM dm_list")).fetchone()[0]
            prospects_total = conn.execute(text("SELECT COUNT(*) FROM dm_prospects")).fetchone()[0]
            
            return {
                "total_in_list": total,
                "total_prospects": prospects_total,
                "by_status": by_status,
                "by_phase": by_phase,
                "by_platform": by_platform,
                "conversion_rate": by_status.get("converted", 0) / total * 100 if total > 0 else 0
            }


# =============================================================================
# SINGLETON
# =============================================================================

_dm_list_manager_instance: Optional[DMListManager] = None

def get_dm_list_manager() -> DMListManager:
    """Get singleton instance of DMListManager."""
    global _dm_list_manager_instance
    if _dm_list_manager_instance is None:
        _dm_list_manager_instance = DMListManager()
    return _dm_list_manager_instance
