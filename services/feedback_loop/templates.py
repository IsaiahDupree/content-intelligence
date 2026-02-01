"""
Starter Template Library for X/Twitter Feedback Loop Agent
25 templates tagged by Awareness × FATE for AI-driven content generation.

Each template supports variables:
- {brand}, {offer}, {icp}, {pain}, {desired_outcome}
- {objection}, {mechanism}, {cta_link}, {proof}
"""

from dataclasses import dataclass, field
from typing import List, Dict, Optional
from enum import Enum


class AwarenessLevel(Enum):
    UNAWARE = "unaware"
    PROBLEM_AWARE = "problem_aware"
    SOLUTION_AWARE = "solution_aware"
    PRODUCT_AWARE = "product_aware"
    MOST_AWARE = "most_aware"


class PostFormat(Enum):
    SINGLE = "single"
    THREAD = "thread"
    QUOTE_TWEET = "quote_tweet"
    REPLY_BAIT = "reply_bait"


class CTAStrength(Enum):
    NONE = "none"
    SOFT = "soft"
    DIRECT = "direct"


class Intent(Enum):
    EDUCATE = "educate"
    STORY = "story"
    TEARDOWN = "teardown"
    CONTRAST = "contrast"
    MYTH = "myth"
    COMPARISON = "comparison"
    DIAGNOSTIC = "diagnostic"
    PROOF = "proof"
    OFFER = "offer"


@dataclass
class FATEWeights:
    """FATE influence stack weights (0-1 each)"""
    focus: float = 0.5      # F - Attention/novelty
    authority: float = 0.5  # A - Credibility/proof
    tribe: float = 0.5      # T - Identity/group
    emotion: float = 0.5    # E - Feeling/visceral
    
    def to_dict(self) -> Dict[str, float]:
        return {
            "focus": self.focus,
            "authority": self.authority,
            "tribe": self.tribe,
            "emotion": self.emotion
        }


@dataclass
class Template:
    """Content generation template"""
    template_id: str
    name: str
    awareness_level: AwarenessLevel
    fate_weights: FATEWeights
    format: PostFormat
    intent: Intent
    cta_strength: CTAStrength
    prompt_text: str
    variables: List[str] = field(default_factory=list)
    example_output: Optional[str] = None
    
    def to_dict(self) -> Dict:
        return {
            "template_id": self.template_id,
            "name": self.name,
            "awareness_level": self.awareness_level.value,
            "fate_weights": self.fate_weights.to_dict(),
            "format": self.format.value,
            "intent": self.intent.value,
            "cta_strength": self.cta_strength.value,
            "prompt_text": self.prompt_text,
            "variables": self.variables,
            "example_output": self.example_output
        }


# =============================================================================
# PROBLEM-AWARE TEMPLATES (8)
# =============================================================================

TEMPLATE_001 = Template(
    template_id="tpl_001",
    name="Symptom Mirror + Why It Keeps Happening",
    awareness_level=AwarenessLevel.PROBLEM_AWARE,
    fate_weights=FATEWeights(focus=0.9, authority=0.4, tribe=0.3, emotion=0.8),
    format=PostFormat.SINGLE,
    intent=Intent.EDUCATE,
    cta_strength=CTAStrength.SOFT,
    prompt_text="""Write a tweet for {icp} about {pain}.

Structure:
1. Open with a specific symptom they recognize (pattern interrupt)
2. Explain WHY this keeps happening (hidden cause)
3. Hint that there's a better approach
4. Soft CTA: "Reply if this sounds familiar" or "DM for the fix"

Tone: Empathetic, insider knowledge, no selling
Max: 280 chars or 2-3 short sentences

Variables provided:
- ICP: {icp}
- Pain: {pain}
- Mechanism hint: {mechanism}
""",
    variables=["icp", "pain", "mechanism"]
)

TEMPLATE_002 = Template(
    template_id="tpl_002",
    name="Cost of Doing Nothing",
    awareness_level=AwarenessLevel.PROBLEM_AWARE,
    fate_weights=FATEWeights(focus=0.7, authority=0.6, tribe=0.3, emotion=0.9),
    format=PostFormat.SINGLE,
    intent=Intent.CONTRAST,
    cta_strength=CTAStrength.SOFT,
    prompt_text="""Write a tweet showing {icp} the hidden cost of ignoring {pain}.

Structure:
1. Name the thing they're tolerating
2. Quantify or dramatize the cost (time, money, opportunity)
3. Create urgency without being salesy
4. End with introspective question or "worth thinking about"

Tone: Direct but not preachy, use specific numbers if possible
Max: 280 chars

Variables:
- ICP: {icp}
- Pain: {pain}
- Proof/stat: {proof}
""",
    variables=["icp", "pain", "proof"]
)

TEMPLATE_003 = Template(
    template_id="tpl_003",
    name="My Mistake Story → Lesson",
    awareness_level=AwarenessLevel.PROBLEM_AWARE,
    fate_weights=FATEWeights(focus=0.6, authority=0.7, tribe=0.4, emotion=0.9),
    format=PostFormat.SINGLE,
    intent=Intent.STORY,
    cta_strength=CTAStrength.NONE,
    prompt_text="""Write a personal story tweet about making a mistake related to {pain}.

Structure:
1. "I used to..." or "Last year I..."
2. What went wrong (specific, vulnerable)
3. The insight that changed things
4. No CTA - just value and relatability

Tone: Vulnerable, specific, lesson-focused
Max: 280 chars

Variables:
- Pain context: {pain}
- Lesson/mechanism: {mechanism}
""",
    variables=["pain", "mechanism"]
)

TEMPLATE_004 = Template(
    template_id="tpl_004",
    name="If You've Tried X and It Failed",
    awareness_level=AwarenessLevel.PROBLEM_AWARE,
    fate_weights=FATEWeights(focus=0.8, authority=0.8, tribe=0.4, emotion=0.5),
    format=PostFormat.SINGLE,
    intent=Intent.EDUCATE,
    cta_strength=CTAStrength.SOFT,
    prompt_text="""Write a tweet for {icp} who has tried common solutions for {pain} and failed.

Structure:
1. "If you've tried [common approach] and it's not working..."
2. Reveal WHY it fails (the hidden mechanism)
3. Tease the real fix without naming your product
4. Soft CTA: "This is what actually moves the needle"

Tone: Insider, credible, mechanism-focused
Max: 280 chars

Variables:
- ICP: {icp}
- Pain: {pain}
- Failed approach: {objection}
- Real mechanism: {mechanism}
""",
    variables=["icp", "pain", "objection", "mechanism"]
)

TEMPLATE_005 = Template(
    template_id="tpl_005",
    name="Checklist: You're in This Bucket If...",
    awareness_level=AwarenessLevel.PROBLEM_AWARE,
    fate_weights=FATEWeights(focus=0.9, authority=0.5, tribe=0.6, emotion=0.4),
    format=PostFormat.SINGLE,
    intent=Intent.DIAGNOSTIC,
    cta_strength=CTAStrength.SOFT,
    prompt_text="""Write a quick diagnostic tweet for {icp} to self-identify with {pain}.

Structure:
1. "You might be [in this situation] if:"
2. 3-4 specific symptoms (bullet-style or comma list)
3. End with "Sound familiar?" or similar

Tone: Direct, specific, no fluff
Max: 280 chars

Variables:
- ICP: {icp}
- Pain: {pain}
""",
    variables=["icp", "pain"]
)

TEMPLATE_006 = Template(
    template_id="tpl_006",
    name="Myth Buster: Hard Work Isn't the Fix",
    awareness_level=AwarenessLevel.PROBLEM_AWARE,
    fate_weights=FATEWeights(focus=0.8, authority=0.8, tribe=0.5, emotion=0.6),
    format=PostFormat.SINGLE,
    intent=Intent.MYTH,
    cta_strength=CTAStrength.SOFT,
    prompt_text="""Write a myth-busting tweet about {pain} for {icp}.

Structure:
1. "Most people think [common belief]..."
2. "But [the real truth]..."
3. Quick proof or mechanism
4. Implication for what to do instead

Tone: Contrarian but credible, not arrogant
Max: 280 chars

Variables:
- ICP: {icp}
- Pain: {pain}
- Myth: {objection}
- Truth: {mechanism}
""",
    variables=["icp", "pain", "objection", "mechanism"]
)

TEMPLATE_007 = Template(
    template_id="tpl_007",
    name="Identity Callout (People Like Us)",
    awareness_level=AwarenessLevel.PROBLEM_AWARE,
    fate_weights=FATEWeights(focus=0.7, authority=0.4, tribe=0.9, emotion=0.7),
    format=PostFormat.SINGLE,
    intent=Intent.STORY,
    cta_strength=CTAStrength.NONE,
    prompt_text="""Write a tribe-building tweet for {icp} around {pain}.

Structure:
1. Identity label: "If you're the type who..."
2. Shared frustration or value
3. "We" language, us-vs-them framing
4. No CTA - just connection

Tone: Inclusive, slightly rebellious, identity-affirming
Max: 280 chars

Variables:
- ICP: {icp}
- Pain: {pain}
- Shared value: {desired_outcome}
""",
    variables=["icp", "pain", "desired_outcome"]
)

TEMPLATE_008 = Template(
    template_id="tpl_008",
    name="Quick Diagnostic Question",
    awareness_level=AwarenessLevel.PROBLEM_AWARE,
    fate_weights=FATEWeights(focus=0.9, authority=0.3, tribe=0.5, emotion=0.5),
    format=PostFormat.REPLY_BAIT,
    intent=Intent.DIAGNOSTIC,
    cta_strength=CTAStrength.SOFT,
    prompt_text="""Write a question tweet that gets {icp} to engage about {pain}.

Structure:
1. Direct question that triggers self-reflection
2. Make it specific enough to feel personal
3. End with: "Reply with your answer" or "Drop a 🙋 if yes"

Tone: Curious, non-judgmental, engagement-focused
Max: 280 chars

Variables:
- ICP: {icp}
- Pain: {pain}
""",
    variables=["icp", "pain"]
)

# =============================================================================
# SOLUTION-AWARE TEMPLATES (7)
# =============================================================================

TEMPLATE_009 = Template(
    template_id="tpl_009",
    name="3 Approaches Comparison",
    awareness_level=AwarenessLevel.SOLUTION_AWARE,
    fate_weights=FATEWeights(focus=0.8, authority=0.9, tribe=0.3, emotion=0.4),
    format=PostFormat.SINGLE,
    intent=Intent.COMPARISON,
    cta_strength=CTAStrength.SOFT,
    prompt_text="""Write a comparison tweet showing {icp} 3 ways to solve {pain}.

Structure:
1. "3 ways to [solve problem]:"
2. Option A: [approach] → [pro/con in 3-5 words]
3. Option B: [approach] → [pro/con]
4. Option C: [approach] → [pro/con]
5. "Pick based on [your situation]"

Tone: Objective, helpful, decision-enabler
Max: 280 chars

Variables:
- ICP: {icp}
- Pain: {pain}
- Mechanism: {mechanism}
""",
    variables=["icp", "pain", "mechanism"]
)

TEMPLATE_010 = Template(
    template_id="tpl_010",
    name="Framework Breakdown (Steps)",
    awareness_level=AwarenessLevel.SOLUTION_AWARE,
    fate_weights=FATEWeights(focus=0.7, authority=0.9, tribe=0.3, emotion=0.3),
    format=PostFormat.SINGLE,
    intent=Intent.EDUCATE,
    cta_strength=CTAStrength.SOFT,
    prompt_text="""Write a step-by-step framework tweet for {icp} to address {pain}.

Structure:
1. "How to [achieve outcome] in [X] steps:"
2. Step 1: [action] (brief why)
3. Step 2: [action]
4. Step 3: [action]
5. "This is how [desired_outcome] happens"

Tone: Tactical, actionable, credible
Max: 280 chars

Variables:
- ICP: {icp}
- Pain: {pain}
- Desired outcome: {desired_outcome}
- Mechanism: {mechanism}
""",
    variables=["icp", "pain", "desired_outcome", "mechanism"]
)

TEMPLATE_011 = Template(
    template_id="tpl_011",
    name="Decision Tree",
    awareness_level=AwarenessLevel.SOLUTION_AWARE,
    fate_weights=FATEWeights(focus=0.8, authority=0.8, tribe=0.3, emotion=0.3),
    format=PostFormat.SINGLE,
    intent=Intent.EDUCATE,
    cta_strength=CTAStrength.SOFT,
    prompt_text="""Write a decision-tree tweet helping {icp} choose the right approach for {pain}.

Structure:
1. "Trying to [solve X]? Here's how to pick:"
2. If [situation A] → do [this]
3. If [situation B] → do [that]
4. If [situation C] → do [other]

Tone: Clear, helpful, no judgment
Max: 280 chars

Variables:
- ICP: {icp}
- Pain: {pain}
""",
    variables=["icp", "pain"]
)

TEMPLATE_012 = Template(
    template_id="tpl_012",
    name="Tool Stack Recommendation",
    awareness_level=AwarenessLevel.SOLUTION_AWARE,
    fate_weights=FATEWeights(focus=0.7, authority=0.9, tribe=0.4, emotion=0.3),
    format=PostFormat.SINGLE,
    intent=Intent.EDUCATE,
    cta_strength=CTAStrength.SOFT,
    prompt_text="""Write a tool/resource recommendation tweet for {icp} dealing with {pain}.

Structure:
1. "If you're [dealing with problem], here's what actually works:"
2. Tool/approach 1 - for [use case]
3. Tool/approach 2 - for [use case]
4. "Start with [X] if you're just beginning"

Tone: Practical, opinionated, helpful
Max: 280 chars

Variables:
- ICP: {icp}
- Pain: {pain}
- Offer hint: {offer}
""",
    variables=["icp", "pain", "offer"]
)

TEMPLATE_013 = Template(
    template_id="tpl_013",
    name="Do This Before You Buy Anything",
    awareness_level=AwarenessLevel.SOLUTION_AWARE,
    fate_weights=FATEWeights(focus=0.8, authority=0.8, tribe=0.4, emotion=0.5),
    format=PostFormat.SINGLE,
    intent=Intent.EDUCATE,
    cta_strength=CTAStrength.NONE,
    prompt_text="""Write a "do this first" tweet for {icp} before they invest in solving {pain}.

Structure:
1. "Before you [buy/hire/invest in] anything for [problem]..."
2. Do [this free/simple thing] first
3. Why it matters
4. No CTA - pure value

Tone: Generous, trust-building, contrarian
Max: 280 chars

Variables:
- ICP: {icp}
- Pain: {pain}
- Mechanism: {mechanism}
""",
    variables=["icp", "pain", "mechanism"]
)

TEMPLATE_014 = Template(
    template_id="tpl_014",
    name="Anonymous Case Study",
    awareness_level=AwarenessLevel.SOLUTION_AWARE,
    fate_weights=FATEWeights(focus=0.7, authority=0.8, tribe=0.4, emotion=0.7),
    format=PostFormat.SINGLE,
    intent=Intent.STORY,
    cta_strength=CTAStrength.SOFT,
    prompt_text="""Write a case study tweet (without naming your product) for {icp} about {pain}.

Structure:
1. "A [type of person] came to me with [problem]..."
2. What we did (mechanism, not product name)
3. Result (specific if possible)
4. Takeaway anyone can apply

Tone: Story-driven, proof-focused, humble
Max: 280 chars

Variables:
- ICP: {icp}
- Pain: {pain}
- Desired outcome: {desired_outcome}
- Proof: {proof}
""",
    variables=["icp", "pain", "desired_outcome", "proof"]
)

TEMPLATE_015 = Template(
    template_id="tpl_015",
    name="What 'Good' Looks Like (Benchmarks)",
    awareness_level=AwarenessLevel.SOLUTION_AWARE,
    fate_weights=FATEWeights(focus=0.7, authority=0.9, tribe=0.3, emotion=0.4),
    format=PostFormat.SINGLE,
    intent=Intent.EDUCATE,
    cta_strength=CTAStrength.SOFT,
    prompt_text="""Write a benchmark tweet showing {icp} what "good" looks like for {desired_outcome}.

Structure:
1. "Here's what [good/great] looks like for [outcome]:"
2. Benchmark 1
3. Benchmark 2
4. "If you're not here yet, [do X]"

Tone: Authoritative, data-informed, aspirational
Max: 280 chars

Variables:
- ICP: {icp}
- Desired outcome: {desired_outcome}
- Proof/benchmarks: {proof}
""",
    variables=["icp", "desired_outcome", "proof"]
)

# =============================================================================
# PRODUCT-AWARE TEMPLATES (6)
# =============================================================================

TEMPLATE_016 = Template(
    template_id="tpl_016",
    name="Why We Built It Differently",
    awareness_level=AwarenessLevel.PRODUCT_AWARE,
    fate_weights=FATEWeights(focus=0.7, authority=0.9, tribe=0.5, emotion=0.5),
    format=PostFormat.SINGLE,
    intent=Intent.PROOF,
    cta_strength=CTAStrength.DIRECT,
    prompt_text="""Write a differentiation tweet explaining why {brand} is built differently for {icp}.

Structure:
1. "Most [solutions] do [common approach]..."
2. "We built {brand} to [different approach]"
3. Why this matters for {icp}
4. CTA: {cta_link}

Tone: Confident, mechanism-focused, not bashing competitors
Max: 280 chars

Variables:
- Brand: {brand}
- ICP: {icp}
- Mechanism: {mechanism}
- CTA link: {cta_link}
""",
    variables=["brand", "icp", "mechanism", "cta_link"]
)

TEMPLATE_017 = Template(
    template_id="tpl_017",
    name="Feature → Outcome Mapping",
    awareness_level=AwarenessLevel.PRODUCT_AWARE,
    fate_weights=FATEWeights(focus=0.8, authority=0.8, tribe=0.3, emotion=0.5),
    format=PostFormat.SINGLE,
    intent=Intent.PROOF,
    cta_strength=CTAStrength.DIRECT,
    prompt_text="""Write a feature-benefit tweet for {brand} targeting {icp}.

Structure:
1. Feature: "{brand} does [specific thing]"
2. So what: "Which means you can [outcome]"
3. Proof or specificity
4. CTA: {cta_link}

Tone: Clear, benefit-focused, specific
Max: 280 chars

Variables:
- Brand: {brand}
- Offer: {offer}
- ICP: {icp}
- Desired outcome: {desired_outcome}
- CTA link: {cta_link}
""",
    variables=["brand", "offer", "icp", "desired_outcome", "cta_link"]
)

TEMPLATE_018 = Template(
    template_id="tpl_018",
    name="Objection Handler",
    awareness_level=AwarenessLevel.PRODUCT_AWARE,
    fate_weights=FATEWeights(focus=0.7, authority=0.8, tribe=0.4, emotion=0.6),
    format=PostFormat.SINGLE,
    intent=Intent.PROOF,
    cta_strength=CTAStrength.DIRECT,
    prompt_text="""Write an objection-handling tweet for {brand} addressing {objection}.

Structure:
1. "You might think {objection}..."
2. "But here's what actually happens:"
3. Evidence or mechanism
4. CTA: {cta_link}

Tone: Understanding, not defensive, proof-focused
Max: 280 chars

Variables:
- Brand: {brand}
- Objection: {objection}
- Proof: {proof}
- CTA link: {cta_link}
""",
    variables=["brand", "objection", "proof", "cta_link"]
)

TEMPLATE_019 = Template(
    template_id="tpl_019",
    name="Before/After Flow",
    awareness_level=AwarenessLevel.PRODUCT_AWARE,
    fate_weights=FATEWeights(focus=0.8, authority=0.7, tribe=0.4, emotion=0.7),
    format=PostFormat.SINGLE,
    intent=Intent.CONTRAST,
    cta_strength=CTAStrength.DIRECT,
    prompt_text="""Write a before/after tweet for {brand} showing transformation for {icp}.

Structure:
1. Before: [pain state - specific]
2. After: [desired state - specific]
3. What made the difference (hint at {brand})
4. CTA: {cta_link}

Tone: Transformational, specific, aspirational
Max: 280 chars

Variables:
- Brand: {brand}
- ICP: {icp}
- Pain: {pain}
- Desired outcome: {desired_outcome}
- CTA link: {cta_link}
""",
    variables=["brand", "icp", "pain", "desired_outcome", "cta_link"]
)

TEMPLATE_020 = Template(
    template_id="tpl_020",
    name="Demo in Words / Walkthrough",
    awareness_level=AwarenessLevel.PRODUCT_AWARE,
    fate_weights=FATEWeights(focus=0.8, authority=0.9, tribe=0.3, emotion=0.4),
    format=PostFormat.SINGLE,
    intent=Intent.EDUCATE,
    cta_strength=CTAStrength.DIRECT,
    prompt_text="""Write a "demo in words" tweet showing how {brand} works for {icp}.

Structure:
1. "Here's how {brand} works:"
2. Step 1: You [do this]
3. Step 2: It [does this]
4. Result: You get [outcome]
5. CTA: {cta_link}

Tone: Simple, clear, outcome-focused
Max: 280 chars

Variables:
- Brand: {brand}
- ICP: {icp}
- Desired outcome: {desired_outcome}
- CTA link: {cta_link}
""",
    variables=["brand", "icp", "desired_outcome", "cta_link"]
)

TEMPLATE_021 = Template(
    template_id="tpl_021",
    name="Competitive Positioning (Unnamed)",
    awareness_level=AwarenessLevel.PRODUCT_AWARE,
    fate_weights=FATEWeights(focus=0.8, authority=0.8, tribe=0.5, emotion=0.5),
    format=PostFormat.SINGLE,
    intent=Intent.COMPARISON,
    cta_strength=CTAStrength.DIRECT,
    prompt_text="""Write a competitive positioning tweet for {brand} without naming competitors.

Structure:
1. "Most [category] tools do [common approach]..."
2. "{brand} does [different approach] instead"
3. Why this matters for {icp}
4. CTA: {cta_link}

Tone: Confident, factual, not bashing
Max: 280 chars

Variables:
- Brand: {brand}
- ICP: {icp}
- Mechanism: {mechanism}
- CTA link: {cta_link}
""",
    variables=["brand", "icp", "mechanism", "cta_link"]
)

# =============================================================================
# MOST-AWARE TEMPLATES (4)
# =============================================================================

TEMPLATE_022 = Template(
    template_id="tpl_022",
    name="Offer Reminder + Fast Start",
    awareness_level=AwarenessLevel.MOST_AWARE,
    fate_weights=FATEWeights(focus=0.9, authority=0.6, tribe=0.4, emotion=0.6),
    format=PostFormat.SINGLE,
    intent=Intent.OFFER,
    cta_strength=CTAStrength.DIRECT,
    prompt_text="""Write an offer reminder tweet for {brand} / {offer}.

Structure:
1. Quick reminder of what {offer} gives them
2. How fast they can start (time to value)
3. Simple next step
4. CTA: {cta_link}

Tone: Friendly reminder, no pressure, clear action
Max: 280 chars

Variables:
- Brand: {brand}
- Offer: {offer}
- CTA link: {cta_link}
""",
    variables=["brand", "offer", "cta_link"]
)

TEMPLATE_023 = Template(
    template_id="tpl_023",
    name="Limited Bonus / Deadline",
    awareness_level=AwarenessLevel.MOST_AWARE,
    fate_weights=FATEWeights(focus=0.9, authority=0.5, tribe=0.4, emotion=0.8),
    format=PostFormat.SINGLE,
    intent=Intent.OFFER,
    cta_strength=CTAStrength.DIRECT,
    prompt_text="""Write a limited-time offer tweet for {brand} / {offer}.

Structure:
1. What's available (bonus, discount, slots)
2. When it ends (deadline)
3. Why now matters
4. CTA: {cta_link}

Tone: Urgent but not desperate, honest scarcity
Max: 280 chars

Variables:
- Brand: {brand}
- Offer: {offer}
- CTA link: {cta_link}
""",
    variables=["brand", "offer", "cta_link"]
)

TEMPLATE_024 = Template(
    template_id="tpl_024",
    name="Guarantee / Risk Reversal",
    awareness_level=AwarenessLevel.MOST_AWARE,
    fate_weights=FATEWeights(focus=0.7, authority=0.9, tribe=0.4, emotion=0.6),
    format=PostFormat.SINGLE,
    intent=Intent.PROOF,
    cta_strength=CTAStrength.DIRECT,
    prompt_text="""Write a guarantee/risk-reversal tweet for {brand} / {offer}.

Structure:
1. "Worried about [objection]?"
2. Here's our guarantee: [specific promise]
3. Why we can offer this
4. CTA: {cta_link}

Tone: Confident, de-risking, trustworthy
Max: 280 chars

Variables:
- Brand: {brand}
- Offer: {offer}
- Objection: {objection}
- CTA link: {cta_link}
""",
    variables=["brand", "offer", "objection", "cta_link"]
)

TEMPLATE_025 = Template(
    template_id="tpl_025",
    name="Here's Exactly What You Get",
    awareness_level=AwarenessLevel.MOST_AWARE,
    fate_weights=FATEWeights(focus=0.8, authority=0.8, tribe=0.3, emotion=0.5),
    format=PostFormat.SINGLE,
    intent=Intent.OFFER,
    cta_strength=CTAStrength.DIRECT,
    prompt_text="""Write a "what you get" clarity tweet for {brand} / {offer}.

Structure:
1. "Here's exactly what you get with {offer}:"
2. Item 1
3. Item 2
4. Item 3
5. CTA: {cta_link}

Tone: Clear, no fluff, easy to scan
Max: 280 chars

Variables:
- Brand: {brand}
- Offer: {offer}
- CTA link: {cta_link}
""",
    variables=["brand", "offer", "cta_link"]
)


# =============================================================================
# TEMPLATE LIBRARY
# =============================================================================

TEMPLATE_LIBRARY = [
    # Problem-Aware (8)
    TEMPLATE_001, TEMPLATE_002, TEMPLATE_003, TEMPLATE_004,
    TEMPLATE_005, TEMPLATE_006, TEMPLATE_007, TEMPLATE_008,
    # Solution-Aware (7)
    TEMPLATE_009, TEMPLATE_010, TEMPLATE_011, TEMPLATE_012,
    TEMPLATE_013, TEMPLATE_014, TEMPLATE_015,
    # Product-Aware (6)
    TEMPLATE_016, TEMPLATE_017, TEMPLATE_018, TEMPLATE_019,
    TEMPLATE_020, TEMPLATE_021,
    # Most-Aware (4)
    TEMPLATE_022, TEMPLATE_023, TEMPLATE_024, TEMPLATE_025,
]


def get_templates_by_awareness(level: AwarenessLevel) -> List[Template]:
    """Get all templates for a specific awareness level."""
    return [t for t in TEMPLATE_LIBRARY if t.awareness_level == level]


def get_template_by_id(template_id: str) -> Optional[Template]:
    """Get a specific template by ID."""
    for t in TEMPLATE_LIBRARY:
        if t.template_id == template_id:
            return t
    return None


def get_templates_by_cta_strength(strength: CTAStrength) -> List[Template]:
    """Get all templates with a specific CTA strength."""
    return [t for t in TEMPLATE_LIBRARY if t.cta_strength == strength]


def get_high_authority_templates() -> List[Template]:
    """Get templates with high authority weight (>0.7)."""
    return [t for t in TEMPLATE_LIBRARY if t.fate_weights.authority > 0.7]


def get_high_emotion_templates() -> List[Template]:
    """Get templates with high emotion weight (>0.7)."""
    return [t for t in TEMPLATE_LIBRARY if t.fate_weights.emotion > 0.7]
