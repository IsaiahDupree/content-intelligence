"""
Sample Brand/Offer/ICP Configuration for Twitter Feedback Loop Agent.
Includes Isaiah's brands: EverReach, MatrixLoop, KeywordRadar, BlankLogo.
"""

from .models import (
    CreatorProfile,
    Brand,
    Offer,
    ICP,
    generate_id
)


# =============================================================================
# CREATOR PROFILE (Isaiah Dupree - Personal Brand)
# =============================================================================

CREATOR_ISAIAH = CreatorProfile(
    creator_id="creator_isaiah",
    name="Isaiah Dupree",
    voice_rules={
        "tone": ["direct", "practical", "slightly contrarian"],
        "style": "short sentences, specific examples, no fluff",
        "hook_style": "pattern interrupt or specific symptom",
        "proof_style": "mechanisms > claims, show the how",
        "cta_style": "soft invitation, not pushy"
    },
    banned_phrases=[
        "game changer",
        "revolutionary",
        "unlock your potential",
        "hustle",
        "grind",
        "10x",
        "skyrocket",
        "explode your growth",
        "crushing it",
        "living my best life"
    ],
    tone_descriptors=[
        "direct",
        "practical",
        "builder mindset",
        "no-BS",
        "specific over vague"
    ],
    proof_sources=[
        "personal experience building apps",
        "metrics from own products",
        "specific user stories (anonymized)"
    ]
)


# =============================================================================
# BRAND: EVERREACH
# =============================================================================

ICP_EVERREACH_FOUNDER = ICP(
    icp_id="icp_everreach_founder",
    offer_id="offer_everreach_waitlist",
    name="Solo Founders Building Their First App",
    description="First-time founders who are technical but struggle with distribution",
    pains=[
        "Building in public but getting no traction",
        "Product is ready but nobody knows it exists",
        "Spending all time on features, none on marketing",
        "Don't know where to find their first 100 users",
        "Posting content but it's not converting",
        "Overwhelmed by too many marketing channels"
    ],
    desired_outcomes=[
        "Predictable flow of interested users",
        "Content that converts to signups",
        "Know exactly what to post and when",
        "First 100 paying customers",
        "Marketing that doesn't feel like marketing"
    ],
    objections=[
        "I'm not a marketer, I'm a builder",
        "I don't have time for content",
        "My product isn't ready yet",
        "I've tried posting before and it didn't work"
    ],
    awareness_distribution={
        "unaware": 0.1,
        "problem_aware": 0.4,
        "solution_aware": 0.3,
        "product_aware": 0.15,
        "most_aware": 0.05
    },
    language_to_use=[
        "ship",
        "build in public",
        "distribution",
        "traction",
        "users",
        "launch"
    ],
    language_to_avoid=[
        "followers",
        "influencer",
        "growth hack",
        "viral"
    ],
    example_hooks=[
        "You shipped the product. Now what?",
        "Building ≠ marketing. Here's the gap nobody talks about.",
        "I spent 6 months building. Got 3 signups. Here's what I learned."
    ]
)

OFFER_EVERREACH_WAITLIST = Offer(
    offer_id="offer_everreach_waitlist",
    brand_id="brand_everreach",
    name="EverReach Waitlist",
    offer_type="waitlist",
    promise="AI-powered content engine that turns your product updates into distribution",
    cta_primary="Join the waitlist",
    cta_secondary="Get early access",
    landing_url="https://everreach.ai",
    shortlink_domain="https://r.everreach.ai",
    who_for="Solo founders who can build but struggle to distribute",
    who_not_for="Agencies, influencers, people who just want more followers",
    price="Free beta → paid later",
    pricing_model="usage-based",
    icps=[ICP_EVERREACH_FOUNDER]
)

BRAND_EVERREACH = Brand(
    brand_id="brand_everreach",
    name="EverReach",
    positioning="Content engine for solo founders — ship updates, get users",
    tagline="From build mode to distribution mode",
    allowed_topics=[
        "content strategy for founders",
        "building in public",
        "distribution tactics",
        "first 100 users",
        "founder marketing"
    ],
    disallowed_topics=[
        "influencer marketing",
        "paid ads",
        "SEO",
        "podcast guesting"
    ],
    offers=[OFFER_EVERREACH_WAITLIST]
)


# =============================================================================
# BRAND: MATRIXLOOP
# =============================================================================

ICP_MATRIXLOOP_CREATOR = ICP(
    icp_id="icp_matrixloop_creator",
    offer_id="offer_matrixloop_beta",
    name="Content Creators Overwhelmed by Multi-Platform",
    description="Creators posting to 3+ platforms, losing track of what works",
    pains=[
        "Posting everywhere but can't track what works",
        "Repurposing is manual and time-consuming",
        "No idea which platform actually drives results",
        "Content calendar is a mess",
        "Analytics scattered across 5 different dashboards"
    ],
    desired_outcomes=[
        "One dashboard for all platforms",
        "Know exactly what's working",
        "Automated repurposing",
        "Data-driven posting decisions",
        "Reclaim hours of content management time"
    ],
    objections=[
        "I already use Hootsuite/Buffer/etc",
        "My workflow is fine",
        "I don't need more tools",
        "Analytics don't tell me what to DO"
    ],
    language_to_use=[
        "repurpose",
        "multi-platform",
        "analytics",
        "dashboard",
        "workflow"
    ],
    language_to_avoid=[
        "viral",
        "algorithm hack",
        "growth hack"
    ],
    example_hooks=[
        "You're posting to 5 platforms. How many are actually working?",
        "Repurposing is the strategy. But it's also the time-suck.",
        "I track 4 platforms from one screen. Here's the stack."
    ]
)

OFFER_MATRIXLOOP_BETA = Offer(
    offer_id="offer_matrixloop_beta",
    brand_id="brand_matrixloop",
    name="MatrixLoop Beta Access",
    offer_type="trial",
    promise="Unified analytics + automated repurposing for multi-platform creators",
    cta_primary="Get beta access",
    cta_secondary="See it in action",
    landing_url="https://matrixloop.app",
    shortlink_domain="https://r.matrixloop.app",
    who_for="Creators posting to 3+ platforms who want data-driven decisions",
    who_not_for="Single-platform creators, beginners just starting out",
    icps=[ICP_MATRIXLOOP_CREATOR]
)

BRAND_MATRIXLOOP = Brand(
    brand_id="brand_matrixloop",
    name="MatrixLoop",
    positioning="Multi-platform analytics + repurposing for serious creators",
    tagline="One dashboard. All platforms. Real insights.",
    allowed_topics=[
        "multi-platform strategy",
        "content analytics",
        "repurposing",
        "creator workflows",
        "data-driven content"
    ],
    disallowed_topics=[
        "algorithm hacks",
        "viral tricks",
        "follower count obsession"
    ],
    offers=[OFFER_MATRIXLOOP_BETA]
)


# =============================================================================
# BRAND: KEYWORDRADAR
# =============================================================================

ICP_KEYWORDRADAR_INDIE = ICP(
    icp_id="icp_keywordradar_indie",
    offer_id="offer_keywordradar_waitlist",
    name="Indie Hackers Looking for Validated Ideas",
    description="Builders who want to find demand before building",
    pains=[
        "Building things nobody wants",
        "No way to validate demand before coding",
        "Keyword research tools are too expensive",
        "Don't know what people are searching for",
        "Wasted months on ideas with no market"
    ],
    desired_outcomes=[
        "Find validated demand before building",
        "Know exactly what people are searching for",
        "Cheap/free alternative to expensive tools",
        "Confidence in product-market fit",
        "Ideas backed by real search data"
    ],
    objections=[
        "I can just use Google Trends",
        "Ahrefs/SEMrush already does this",
        "I don't do SEO",
        "Keyword data is just one signal"
    ],
    language_to_use=[
        "validate",
        "demand",
        "search volume",
        "product-market fit",
        "before you build"
    ],
    language_to_avoid=[
        "SEO",
        "ranking",
        "backlinks"
    ],
    example_hooks=[
        "I wasted 3 months on an idea with 0 search volume. Never again.",
        "Before you code: 5 min demand check that saves months.",
        "Your next product idea should come from search data."
    ]
)

OFFER_KEYWORDRADAR_WAITLIST = Offer(
    offer_id="offer_keywordradar_waitlist",
    brand_id="brand_keywordradar",
    name="KeywordRadar Early Access",
    offer_type="waitlist",
    promise="Demand validation for builders — find what people search for before you build",
    cta_primary="Join waitlist",
    cta_secondary="Get early access",
    landing_url="https://keywordradar.app",
    shortlink_domain="https://r.keywordradar.app",
    who_for="Indie hackers and solo founders validating ideas",
    who_not_for="SEO agencies, content marketers focused on ranking",
    icps=[ICP_KEYWORDRADAR_INDIE]
)

BRAND_KEYWORDRADAR = Brand(
    brand_id="brand_keywordradar",
    name="KeywordRadar",
    positioning="Demand validation for builders — find what people actually search for",
    tagline="Validate demand before you build",
    allowed_topics=[
        "idea validation",
        "demand discovery",
        "search trends",
        "product-market fit",
        "building with data"
    ],
    disallowed_topics=[
        "SEO tactics",
        "ranking strategies",
        "link building"
    ],
    offers=[OFFER_KEYWORDRADAR_WAITLIST]
)


# =============================================================================
# BRAND: BLANKLOGO
# =============================================================================

ICP_BLANKLOGO_FOUNDER = ICP(
    icp_id="icp_blanklogo_founder",
    offer_id="offer_blanklogo_service",
    name="Founders Who Need a Brand Fast",
    description="Technical founders who need professional branding without the agency process",
    pains=[
        "Logo looks like it was made in 5 minutes",
        "Brand inconsistency across platforms",
        "Can't afford a $10k agency",
        "Don't have time for 6-week design processes",
        "DIY tools make everything look the same"
    ],
    desired_outcomes=[
        "Professional brand that builds trust",
        "Complete brand kit in days, not weeks",
        "Affordable without looking cheap",
        "Consistent look across all touchpoints",
        "Brand that scales with the company"
    ],
    objections=[
        "I'll just use Canva",
        "I can hire on Fiverr",
        "My product matters more than my brand",
        "I'll rebrand later when we have money"
    ],
    language_to_use=[
        "brand",
        "professional",
        "trust",
        "first impression",
        "credibility"
    ],
    language_to_avoid=[
        "cheap",
        "template",
        "DIY"
    ],
    example_hooks=[
        "Your landing page converts 2x better with a real brand. Data.",
        "Fiverr logos vs founder-grade branding: the difference.",
        "Most founders wait until Series A to invest in brand. That's backwards."
    ]
)

OFFER_BLANKLOGO_SERVICE = Offer(
    offer_id="offer_blanklogo_service",
    brand_id="brand_blanklogo",
    name="BlankLogo Brand Package",
    offer_type="service",
    promise="Founder-grade branding in 5 days — logo, colors, type, guidelines",
    cta_primary="Get a quote",
    cta_secondary="See examples",
    landing_url="https://blanklogo.co",
    shortlink_domain="https://r.blanklogo.co",
    who_for="Technical founders launching products",
    who_not_for="Enterprises, agencies, anyone who wants a 3-month process",
    price="$1,500 - $3,000",
    pricing_model="fixed project",
    icps=[ICP_BLANKLOGO_FOUNDER]
)

BRAND_BLANKLOGO = Brand(
    brand_id="brand_blanklogo",
    name="BlankLogo",
    positioning="Founder-grade branding, delivered in days, not months",
    tagline="Professional brand in 5 days",
    allowed_topics=[
        "founder branding",
        "brand design",
        "visual identity",
        "trust signals",
        "brand investment"
    ],
    disallowed_topics=[
        "enterprise rebrands",
        "brand strategy consulting",
        "marketing campaigns"
    ],
    offers=[OFFER_BLANKLOGO_SERVICE]
)


# =============================================================================
# ALL BRANDS
# =============================================================================

ALL_BRANDS = [
    BRAND_EVERREACH,
    BRAND_MATRIXLOOP,
    BRAND_KEYWORDRADAR,
    BRAND_BLANKLOGO
]

ALL_OFFERS = [
    OFFER_EVERREACH_WAITLIST,
    OFFER_MATRIXLOOP_BETA,
    OFFER_KEYWORDRADAR_WAITLIST,
    OFFER_BLANKLOGO_SERVICE
]

ALL_ICPS = [
    ICP_EVERREACH_FOUNDER,
    ICP_MATRIXLOOP_CREATOR,
    ICP_KEYWORDRADAR_INDIE,
    ICP_BLANKLOGO_FOUNDER
]


def get_brand_by_id(brand_id: str) -> Brand:
    """Get a brand by ID."""
    for brand in ALL_BRANDS:
        if brand.brand_id == brand_id:
            return brand
    return None


def get_offer_by_id(offer_id: str) -> Offer:
    """Get an offer by ID."""
    for offer in ALL_OFFERS:
        if offer.offer_id == offer_id:
            return offer
    return None


def get_icp_by_id(icp_id: str) -> ICP:
    """Get an ICP by ID."""
    for icp in ALL_ICPS:
        if icp.icp_id == icp_id:
            return icp
    return None
