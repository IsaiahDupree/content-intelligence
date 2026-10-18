"""
Funnel Mapper Service
=====================
Infers sales/marketing funnel structure from competitor bio, CTAs, and content.
Detects lead magnets, offer ladders, and conversion paths.
"""
import os
import json
import re
from datetime import datetime
from typing import Dict, Any, List, Optional
from dataclasses import dataclass, field, asdict
from loguru import logger

from openai import OpenAI
from sqlalchemy import create_engine, text


@dataclass
class EntryPoint:
    """Funnel entry point"""
    type: str  # bio_link, pinned_post, recurring_cta, highlight
    url: Optional[str] = None
    description: str = ""
    post_ids: List[str] = field(default_factory=list)


@dataclass
class LeadMagnet:
    """Identified lead magnet"""
    type: str  # freebie, webinar, newsletter, community, quiz, template
    name: str
    url: Optional[str] = None
    delivery_method: str = ""  # dm, email, link
    evidence_posts: List[str] = field(default_factory=list)


@dataclass
class OfferTier:
    """Inferred offer in the ladder"""
    tier: str  # free, low, mid, high
    name: str
    price_hint: Optional[str] = None  # "$", "$$", "$$$", exact price if mentioned
    description: str = ""
    evidence: List[str] = field(default_factory=list)


@dataclass
class ConversionPath:
    """Detected conversion path"""
    trigger: str  # comment_keyword, dm_request, link_click
    action: str  # what user does
    destination: str  # where it leads
    evidence_posts: List[str] = field(default_factory=list)


@dataclass
class FunnelMap:
    """Complete funnel map for a competitor"""
    account_id: str
    
    # Entry points
    entry_points: List[EntryPoint] = field(default_factory=list)
    
    # Lead magnets
    lead_magnets: List[LeadMagnet] = field(default_factory=list)
    
    # Conversion paths
    conversion_paths: List[ConversionPath] = field(default_factory=list)
    
    # Offer stack
    offer_stack: List[OfferTier] = field(default_factory=list)
    
    # CTA analysis
    top_cta_types: List[str] = field(default_factory=list)
    cta_frequency: Dict[str, int] = field(default_factory=dict)
    
    # Proof assets
    proof_types: List[str] = field(default_factory=list)
    proof_posts: List[str] = field(default_factory=list)
    
    # Scores
    funnel_clarity_score: float = 0.0
    monetization_clarity_score: float = 0.0
    
    # AI reasoning
    funnel_analysis_raw: Optional[Dict] = None
    
    created_at: str = field(default_factory=lambda: datetime.utcnow().isoformat())


class FunnelMapper:
    """
    Maps competitor's marketing/sales funnel from available data.
    Infers lead capture, offer ladder, and conversion paths.
    """
    
    # Common lead magnet patterns
    LEAD_MAGNET_PATTERNS = [
        (r"free\s+(guide|ebook|template|checklist|course|training)", "freebie"),
        (r"dm\s+['\"]?(\w+)['\"]?\s+to\s+get", "dm_trigger"),
        (r"comment\s+['\"]?(\w+)['\"]?\s+for", "comment_trigger"),
        (r"link\s+in\s+bio", "link_bio"),
        (r"join\s+(my|our)\s+(community|newsletter|list)", "community"),
        (r"sign\s+up\s+for", "signup"),
        (r"webinar|masterclass|workshop", "webinar"),
        (r"quiz|assessment|calculator", "quiz")
    ]
    
    # CTA pattern detection
    CTA_PATTERNS = {
        "comment_keyword": r"comment\s+['\"]?(\w+)['\"]?",
        "dm_me": r"dm\s+(me|us)|message\s+(me|us)",
        "link_bio": r"link\s+in\s+(bio|profile)",
        "follow_part2": r"follow\s+(for|to)\s+(part|more)",
        "save_this": r"save\s+this",
        "share_with": r"share\s+(this|with)",
        "subscribe": r"subscribe|hit\s+subscribe"
    }
    
    def __init__(
        self,
        db_url: Optional[str] = None,
        openai_api_key: Optional[str] = None
    ):
        self.db_url = db_url or os.getenv("DATABASE_URL", "postgresql://postgres:postgres@127.0.0.1:54322/postgres")
        self.engine = create_engine(self.db_url)
        
        self.openai_api_key = openai_api_key or os.getenv("OPENAI_API_KEY")
        self.client = OpenAI(api_key=self.openai_api_key) if self.openai_api_key else None
    
    def extract_ctas_from_text(self, text: str) -> Dict[str, List[str]]:
        """Extract CTA patterns from caption/bio text"""
        text_lower = text.lower()
        found_ctas = {}
        
        for cta_type, pattern in self.CTA_PATTERNS.items():
            matches = re.findall(pattern, text_lower)
            if matches:
                found_ctas[cta_type] = matches if isinstance(matches[0], str) else [m[0] for m in matches]
        
        return found_ctas
    
    def detect_lead_magnets_from_text(self, text: str) -> List[Dict[str, str]]:
        """Detect lead magnet mentions in text"""
        text_lower = text.lower()
        magnets = []
        
        for pattern, magnet_type in self.LEAD_MAGNET_PATTERNS:
            matches = re.findall(pattern, text_lower)
            if matches:
                magnets.append({
                    "type": magnet_type,
                    "match": matches[0] if matches else "",
                    "context": text[:200]
                })
        
        return magnets
    
    async def map_funnel(
        self,
        account_id: str,
        bio_text: str,
        linkout_urls: List[str],
        post_captions: List[Dict[str, str]],  # [{post_id, caption}]
        cta_counts: Dict[str, int]
    ) -> FunnelMap:
        """
        Map the complete funnel from available data.
        
        Args:
            account_id: Database account ID
            bio_text: Profile bio
            linkout_urls: Links from bio
            post_captions: List of {post_id, caption} dicts
            cta_counts: Aggregated CTA type counts
        """
        funnel = FunnelMap(account_id=account_id)
        
        # 1. Analyze bio for entry points
        if bio_text:
            bio_ctas = self.extract_ctas_from_text(bio_text)
            bio_magnets = self.detect_lead_magnets_from_text(bio_text)
            
            for magnet in bio_magnets:
                funnel.lead_magnets.append(LeadMagnet(
                    type=magnet["type"],
                    name=magnet.get("match", "Unknown"),
                    description=magnet.get("context", "")
                ))
        
        # 2. Add bio links as entry points
        for url in linkout_urls:
            funnel.entry_points.append(EntryPoint(
                type="bio_link",
                url=url,
                description=self._categorize_url(url)
            ))
        
        # 3. Analyze post captions for CTA patterns
        cta_by_type: Dict[str, List[str]] = {}
        for post in post_captions:
            caption = post.get("caption", "")
            post_id = post.get("post_id", "")
            
            # Extract CTAs
            ctas = self.extract_ctas_from_text(caption)
            for cta_type, matches in ctas.items():
                if cta_type not in cta_by_type:
                    cta_by_type[cta_type] = []
                cta_by_type[cta_type].append(post_id)
            
            # Detect lead magnets
            magnets = self.detect_lead_magnets_from_text(caption)
            for magnet in magnets:
                existing = next((m for m in funnel.lead_magnets if m.type == magnet["type"]), None)
                if existing:
                    existing.evidence_posts.append(post_id)
                else:
                    funnel.lead_magnets.append(LeadMagnet(
                        type=magnet["type"],
                        name=magnet.get("match", "Unknown"),
                        evidence_posts=[post_id]
                    ))
        
        # 4. Build conversion paths from CTA patterns
        for cta_type, post_ids in cta_by_type.items():
            if len(post_ids) >= 2:  # Only if used multiple times
                funnel.conversion_paths.append(ConversionPath(
                    trigger=cta_type,
                    action=self._cta_to_action(cta_type),
                    destination=self._cta_to_destination(cta_type),
                    evidence_posts=post_ids[:5]
                ))
        
        # 5. Set CTA frequency
        funnel.cta_frequency = cta_counts
        funnel.top_cta_types = sorted(cta_counts.keys(), key=lambda k: cta_counts[k], reverse=True)[:3]
        
        # 6. Use AI for deeper inference
        if self.client:
            ai_analysis = await self._ai_funnel_analysis(bio_text, post_captions, linkout_urls)
            funnel.offer_stack = ai_analysis.get("offer_stack", [])
            funnel.proof_types = ai_analysis.get("proof_types", [])
            funnel.funnel_clarity_score = ai_analysis.get("funnel_clarity_score", 0)
            funnel.monetization_clarity_score = ai_analysis.get("monetization_clarity_score", 0)
            funnel.funnel_analysis_raw = ai_analysis
        
        return funnel
    
    def _categorize_url(self, url: str) -> str:
        """Categorize a URL by its likely purpose"""
        url_lower = url.lower()
        
        if "linktree" in url_lower or "linktr.ee" in url_lower:
            return "link_aggregator"
        elif "calendly" in url_lower or "cal.com" in url_lower:
            return "booking"
        elif "stan.store" in url_lower or "gumroad" in url_lower or "shopify" in url_lower:
            return "store"
        elif "substack" in url_lower or "convertkit" in url_lower or "beehiiv" in url_lower:
            return "newsletter"
        elif "discord" in url_lower or "skool" in url_lower or "circle" in url_lower:
            return "community"
        elif "youtube" in url_lower:
            return "youtube"
        elif "tiktok" in url_lower:
            return "tiktok"
        else:
            return "website"
    
    def _cta_to_action(self, cta_type: str) -> str:
        """Map CTA type to user action"""
        mapping = {
            "comment_keyword": "User comments keyword",
            "dm_me": "User sends DM",
            "link_bio": "User clicks bio link",
            "follow_part2": "User follows for more",
            "save_this": "User saves post",
            "share_with": "User shares post",
            "subscribe": "User subscribes"
        }
        return mapping.get(cta_type, "User engages")
    
    def _cta_to_destination(self, cta_type: str) -> str:
        """Map CTA type to likely destination"""
        mapping = {
            "comment_keyword": "DM automation / lead capture",
            "dm_me": "Conversation / lead qualification",
            "link_bio": "Landing page / offer",
            "follow_part2": "Continued content consumption",
            "save_this": "Algorithm boost / future reference",
            "share_with": "Viral distribution",
            "subscribe": "Email list / channel"
        }
        return mapping.get(cta_type, "Unknown destination")
    
    async def _ai_funnel_analysis(
        self,
        bio_text: str,
        post_captions: List[Dict[str, str]],
        linkout_urls: List[str]
    ) -> Dict[str, Any]:
        """Use AI to infer deeper funnel structure"""
        
        # Sample captions
        sample_captions = [p.get("caption", "")[:300] for p in post_captions[:10]]
        
        prompt = f"""Analyze this creator's marketing funnel based on their bio and content.

BIO:
{bio_text}

LINKS IN BIO:
{json.dumps(linkout_urls)}

SAMPLE POST CAPTIONS:
{json.dumps(sample_captions, indent=2)}

Infer their funnel structure and return JSON:
{{
    "offer_stack": [
        {{"tier": "free", "name": "lead magnet name", "description": "what it is"}},
        {{"tier": "low", "name": "entry offer", "price_hint": "$"}},
        {{"tier": "mid", "name": "core offer", "price_hint": "$$"}},
        {{"tier": "high", "name": "premium offer", "price_hint": "$$$"}}
    ],
    "proof_types": ["testimonials", "case_studies", "results_screenshots", "before_after"],
    "funnel_clarity_score": 0-100,
    "monetization_clarity_score": 0-100,
    "funnel_summary": "one paragraph explaining their funnel strategy",
    "gaps_identified": ["things they could improve in their funnel"]
}}

Only include offer tiers you can reasonably infer. Be analytical."""

        try:
            response = self.client.chat.completions.create(
                model="gpt-4o-mini",
                messages=[
                    {"role": "system", "content": "You are a marketing strategist analyzing creator funnels."},
                    {"role": "user", "content": prompt}
                ],
                response_format={"type": "json_object"},
                temperature=0.4
            )
            
            result = json.loads(response.choices[0].message.content)
            
            # Convert offer stack to dataclass format
            offer_stack = []
            for offer in result.get("offer_stack", []):
                offer_stack.append(OfferTier(
                    tier=offer.get("tier", "unknown"),
                    name=offer.get("name", ""),
                    price_hint=offer.get("price_hint"),
                    description=offer.get("description", "")
                ))
            result["offer_stack"] = offer_stack
            
            return result
            
        except Exception as e:
            logger.error(f"AI funnel analysis failed: {e}")
            return {}
    
    async def save_funnel_map(self, funnel: FunnelMap) -> str:
        """Save funnel map to database"""
        try:
            with self.engine.connect() as conn:
                result = conn.execute(text("""
                    INSERT INTO competitor_funnel_map (
                        account_id, entry_points, lead_magnets, conversion_paths,
                        offer_stack, top_cta_types, cta_frequency,
                        proof_types, funnel_clarity_score, funnel_analysis_raw
                    ) VALUES (
                        :account_id, :entry_points, :lead_magnets, :conversion_paths,
                        :offer_stack, :top_cta_types, :cta_frequency,
                        :proof_types, :funnel_clarity_score, :funnel_analysis_raw
                    )
                    ON CONFLICT (account_id) DO UPDATE SET
                        entry_points = :entry_points,
                        lead_magnets = :lead_magnets,
                        conversion_paths = :conversion_paths,
                        offer_stack = :offer_stack,
                        funnel_clarity_score = :funnel_clarity_score,
                        funnel_analysis_raw = :funnel_analysis_raw,
                        updated_at = NOW()
                    RETURNING funnel_id
                """), {
                    "account_id": funnel.account_id,
                    "entry_points": [asdict(e) for e in funnel.entry_points],
                    "lead_magnets": [asdict(m) for m in funnel.lead_magnets],
                    "conversion_paths": [asdict(c) for c in funnel.conversion_paths],
                    "offer_stack": [asdict(o) for o in funnel.offer_stack],
                    "top_cta_types": funnel.top_cta_types,
                    "cta_frequency": funnel.cta_frequency,
                    "proof_types": funnel.proof_types,
                    "funnel_clarity_score": funnel.funnel_clarity_score,
                    "funnel_analysis_raw": funnel.funnel_analysis_raw
                })
                conn.commit()
                row = result.fetchone()
                return str(row[0])
        except Exception as e:
            logger.error(f"Failed to save funnel map: {e}")
            raise
