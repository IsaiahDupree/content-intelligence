"""
Prospect Finder
Discovers potential DM targets across platforms.
"""

import os
from datetime import datetime, timezone
from typing import Dict, List, Optional
from loguru import logger

from .dm_list_manager import (
    Prospect, 
    DiscoverySource, 
    get_dm_list_manager
)


# Account mapping with offers
ACCOUNT_OFFERS = {
    "instagram": {
        807: {"username": "@the_isaiah_dupree", "offers": ["coaching", "course"]},
        670: {"username": "@the_isaiah_dupree_", "offers": ["affiliate"]},
        1369: {"username": "@dupree_isaiah_", "offers": ["course"]},
        4508: {"username": "@dupree_isaiah", "offers": ["coaching"]},
    },
    "tiktok": {
        710: {"username": "@isaiah_dupree", "offers": ["coaching"]},
        243: {"username": "@the_isaiah_dupree", "offers": ["course"]},
        4508: {"username": "@dupree_isaiah", "offers": ["affiliate"]},
        571: {"username": "@soursides_is_sour", "offers": ["coaching"]},
    },
    "twitter": {
        4151: {"username": "@IsaiahDupree7", "offers": ["consulting", "coaching"]},
    },
    "threads": {
        173: {"username": "@the_isaiah_dupree_", "offers": ["course"]},
        201: {"username": "@the_isaiah_dupree", "offers": ["coaching"]},
    },
    "youtube": {
        228: {"username": "Isaiah Dupree", "offers": ["course", "coaching"]},
    }
}


class ProspectFinder:
    """
    Finds potential DM targets from various sources.
    
    Discovery sources:
    - Comment engagers (people who commented on your posts)
    - Post likers (high-engagement likers)
    - New followers (with aligned interests)
    - Competitor followers
    - Hashtag searchers
    """
    
    def __init__(self):
        self.dm_manager = get_dm_list_manager()
        logger.info("✅ ProspectFinder initialized")
    
    def get_account_info(self, platform: str, account_id: int) -> Dict:
        """Get account info and associated offers."""
        platform_accounts = ACCOUNT_OFFERS.get(platform, {})
        return platform_accounts.get(account_id, {"username": "unknown", "offers": []})
    
    async def discover_from_comments(
        self,
        platform: str,
        account_id: int,
        limit: int = 50
    ) -> List[Prospect]:
        """
        Find prospects from people who commented on your posts.
        """
        prospects = []
        
        try:
            # Get recent comments from inbox
            from services.inbox import get_inbox_service
            inbox = get_inbox_service()
            
            messages = inbox.get_messages(
                platform=platform,
                message_type="comment",
                limit=limit
            )
            
            account_info = self.get_account_info(platform, account_id)
            
            for msg in messages:
                # Create prospect from commenter
                prospect = Prospect(
                    platform=platform,
                    account_id=account_id,
                    username=msg.sender_username,
                    display_name=msg.sender_name or "",
                    follower_count=msg.sender_follower_count,
                    source=DiscoverySource.COMMENT.value,
                    source_comment=msg.content,
                    source_post_id=msg.post_id
                )
                
                # Calculate fit score
                prospect.fit_score = self._calculate_fit_score(prospect, msg.content)
                
                # Match to best offer
                prospect.offer_match = self._match_offer(
                    prospect.fit_score,
                    msg.content,
                    account_info.get("offers", [])
                )
                
                prospect.qualified = prospect.fit_score >= 50
                
                # Save to database
                self.dm_manager.add_prospect(prospect)
                prospects.append(prospect)
            
            logger.info(f"📥 Discovered {len(prospects)} prospects from {platform} comments")
            
        except Exception as e:
            logger.error(f"Comment discovery failed: {e}")
        
        return prospects
    
    async def discover_from_followers(
        self,
        platform: str,
        account_id: int,
        limit: int = 50
    ) -> List[Prospect]:
        """
        Find prospects from new followers.
        
        Note: Requires platform API or scraping to get follower data.
        """
        prospects = []
        
        try:
            # This would integrate with platform APIs
            # For now, we'll return empty and let the user add manually
            logger.info(f"Follower discovery for {platform}/{account_id} - requires platform API")
            
        except Exception as e:
            logger.error(f"Follower discovery failed: {e}")
        
        return prospects
    
    async def discover_from_engagement(
        self,
        platform: str,
        account_id: int,
        limit: int = 50
    ) -> List[Prospect]:
        """
        Find prospects from people who engaged with your content.
        Includes likers, repliers, and sharers.
        """
        prospects = []
        
        try:
            # Get from relationship CRM contacts
            from services.relationship_crm import get_relationship_crm
            crm = get_relationship_crm()
            
            contacts = crm.get_contacts(limit=limit)
            account_info = self.get_account_info(platform, account_id)
            
            for contact in contacts:
                # Only include if they're from this platform
                if contact.platform and contact.platform.lower() != platform.lower():
                    continue
                
                prospect = Prospect(
                    platform=platform,
                    account_id=account_id,
                    username=contact.handle or contact.name,
                    display_name=contact.name,
                    bio=contact.context.building if contact.context else "",
                    follower_count=0,
                    source=DiscoverySource.COMMENT.value
                )
                
                # Use relationship health as fit score base
                prospect.fit_score = min(contact.health_score, 100)
                prospect.offer_match = self._match_offer(
                    prospect.fit_score,
                    contact.context.struggles if contact.context else "",
                    account_info.get("offers", [])
                )
                prospect.qualified = prospect.fit_score >= 50
                
                self.dm_manager.add_prospect(prospect)
                prospects.append(prospect)
            
            logger.info(f"📥 Discovered {len(prospects)} prospects from CRM")
            
        except Exception as e:
            logger.error(f"Engagement discovery failed: {e}")
        
        return prospects
    
    def add_manual_prospect(
        self,
        platform: str,
        account_id: int,
        username: str,
        display_name: str = "",
        bio: str = "",
        follower_count: int = 0,
        source_note: str = ""
    ) -> Prospect:
        """
        Manually add a prospect.
        """
        account_info = self.get_account_info(platform, account_id)
        
        prospect = Prospect(
            platform=platform,
            account_id=account_id,
            username=username,
            display_name=display_name,
            bio=bio,
            follower_count=follower_count,
            source=DiscoverySource.MANUAL.value,
            source_comment=source_note
        )
        
        prospect.fit_score = self._calculate_fit_score(prospect, bio + " " + source_note)
        prospect.offer_match = self._match_offer(
            prospect.fit_score,
            bio + " " + source_note,
            account_info.get("offers", [])
        )
        prospect.qualified = prospect.fit_score >= 50
        
        self.dm_manager.add_prospect(prospect)
        
        return prospect
    
    def _calculate_fit_score(self, prospect: Prospect, context: str = "") -> int:
        """
        Calculate fit score for a prospect.
        
        Factors:
        - Engagement quality (25%): Thoughtful comments vs emoji-only
        - Follower count (15%): Sweet spot 500-50K
        - Bio alignment (20%): Keywords matching offers
        - Activity signals (20%): Buying intent keywords
        - Previous interaction (20%): Already engaged
        """
        score = 0
        context_lower = context.lower()
        
        # Engagement quality (25 points)
        if len(context) > 100:
            score += 25  # Long, thoughtful comment
        elif len(context) > 50:
            score += 20
        elif len(context) > 20:
            score += 15
        elif len(context) > 5:
            score += 10
        else:
            score += 5
        
        # Follower count (15 points) - sweet spot 500-50K
        followers = prospect.follower_count
        if 500 <= followers <= 50000:
            score += 15  # Ideal range
        elif 100 <= followers < 500:
            score += 10  # Still good
        elif 50000 < followers <= 200000:
            score += 8  # Might be harder to reach
        elif followers > 200000:
            score += 3  # Very hard to reach
        else:
            score += 5  # Unknown or very small
        
        # Bio/context alignment (20 points)
        interest_keywords = [
            "creator", "entrepreneur", "coach", "building",
            "growing", "learning", "business", "content",
            "marketing", "brand", "course", "mentor"
        ]
        
        matches = sum(1 for kw in interest_keywords if kw in context_lower)
        score += min(matches * 4, 20)
        
        # Activity/buying signals (20 points)
        buying_signals = [
            "how do you", "can you help", "need help",
            "struggling", "want to learn", "looking for",
            "recommend", "advice", "tip"
        ]
        
        signal_matches = sum(1 for sig in buying_signals if sig in context_lower)
        score += min(signal_matches * 5, 20)
        
        # Previous interaction bonus (20 points)
        # This would check if they've interacted before
        # For now, give partial credit for engaging
        if prospect.source == DiscoverySource.COMMENT.value:
            score += 15
        elif prospect.source == DiscoverySource.FOLLOWER.value:
            score += 10
        
        return min(score, 100)
    
    def _match_offer(self, fit_score: int, context: str, available_offers: List[str]) -> Optional[str]:
        """Match prospect to the best available offer."""
        if not available_offers:
            return None
        
        context_lower = context.lower()
        
        # Offer-specific signals
        offer_signals = {
            "coaching": ["help", "stuck", "struggling", "guidance", "mentor", "1-on-1", "personal"],
            "course": ["learn", "how to", "beginner", "start", "training", "tutorial"],
            "consulting": ["business", "agency", "team", "scale", "strategy", "growth"],
            "affiliate": ["tool", "software", "recommend", "use", "app"]
        }
        
        best_offer = None
        best_score = 0
        
        for offer in available_offers:
            signals = offer_signals.get(offer, [])
            matches = sum(1 for sig in signals if sig in context_lower)
            
            if matches > best_score:
                best_score = matches
                best_offer = offer
        
        # Default to first offer if no signals matched
        return best_offer or available_offers[0]
    
    async def run_discovery(
        self,
        platform: str = None,
        account_id: int = None,
        sources: List[str] = None
    ) -> Dict:
        """
        Run full discovery process.
        
        Args:
            platform: Specific platform or None for all
            account_id: Specific account or None for all
            sources: List of sources to check or None for all
        
        Returns:
            Summary of discovered prospects
        """
        sources = sources or ["comments", "engagement"]
        total_prospects = []
        
        # Determine which platform/accounts to process
        platforms_to_check = {}
        
        if platform and account_id:
            platforms_to_check[platform] = [account_id]
        elif platform:
            platforms_to_check[platform] = list(ACCOUNT_OFFERS.get(platform, {}).keys())
        else:
            platforms_to_check = {p: list(accts.keys()) for p, accts in ACCOUNT_OFFERS.items()}
        
        for plat, account_ids in platforms_to_check.items():
            for acc_id in account_ids:
                if "comments" in sources:
                    prospects = await self.discover_from_comments(plat, acc_id)
                    total_prospects.extend(prospects)
                
                if "engagement" in sources:
                    prospects = await self.discover_from_engagement(plat, acc_id)
                    total_prospects.extend(prospects)
                
                if "followers" in sources:
                    prospects = await self.discover_from_followers(plat, acc_id)
                    total_prospects.extend(prospects)
        
        qualified = [p for p in total_prospects if p.qualified]
        
        return {
            "total_discovered": len(total_prospects),
            "qualified": len(qualified),
            "platforms_checked": list(platforms_to_check.keys()),
            "sources_used": sources
        }


# =============================================================================
# SINGLETON
# =============================================================================

_finder_instance: Optional[ProspectFinder] = None

def get_prospect_finder() -> ProspectFinder:
    """Get singleton instance of ProspectFinder."""
    global _finder_instance
    if _finder_instance is None:
        _finder_instance = ProspectFinder()
    return _finder_instance
