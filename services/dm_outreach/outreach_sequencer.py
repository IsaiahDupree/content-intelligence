"""
Outreach Sequencer
Manages message cadence and timing for trust-building DM outreach.
"""

import os
import random
from datetime import datetime, timezone, date, timedelta
from typing import Dict, List, Optional
from dataclasses import dataclass, field
from loguru import logger

try:
    from openai import OpenAI
except ImportError:
    OpenAI = None


# =============================================================================
# MESSAGE TEMPLATES
# =============================================================================

TEMPLATES = {
    "introduction": {
        "first_touch": [
            "Hey {name}! Loved your comment on my post about {topic}. What made that resonate with you?",
            "Your content on {their_topic} is 🔥! How long have you been creating?",
            "Saw you're into {interest} too! What got you started?",
            "Hey {name}! Noticed you've been engaging with my content. Really appreciate that 🙏 What's your story?",
            "Your perspective on {topic} caught my attention. Would love to hear more about your journey!",
        ],
        "follow_up": [
            "Hey! Just wanted to follow up - no pressure at all. Hope you're having a great week!",
            "Thought about our last chat. How's everything going on your end?",
        ]
    },
    "value": {
        "share_resource": [
            "Thought you might find this helpful based on what you shared: {resource}",
            "I noticed you're working on {their_goal}. Here's something that helped me: {tip}",
            "Quick tip on {topic} since I know you're interested: {insight}",
        ],
        "check_in": [
            "How's the {their_project} going? Making progress?",
            "Been thinking about what you mentioned about {topic}. Any updates?",
        ]
    },
    "relationship": {
        "celebrate": [
            "Saw your post about {milestone} - congrats! That's huge 🎉",
            "Really happy to see you winning! The {achievement} is well deserved.",
        ],
        "support": [
            "Hey, noticed you're going through {struggle}. Here if you need to talk.",
            "I've been there with {challenge}. Happy to share what worked for me if helpful.",
        ],
        "personal": [
            "How's life treating you lately? Beyond just the {topic} stuff.",
            "Been thinking about what you said about {topic}. Here's my take...",
        ]
    },
    "offer": {
        "soft_intro": [
            "You mentioned struggling with {pain_point}. I actually help people with exactly that if you're ever interested in chatting.",
            "Based on everything you've shared, I think you might benefit from {offer}. No pressure - just thought I'd mention it.",
        ],
        "direct": [
            "I have a {offer_type} that addresses exactly what you're dealing with. Want me to share more details?",
            "Ready to take the next step with {goal}? I've got something that could help.",
        ]
    }
}


@dataclass
class MessageSuggestion:
    """A suggested message to send."""
    content: str
    template_type: str
    phase: str
    personalization_used: List[str] = field(default_factory=list)
    confidence: float = 0.8


class OutreachSequencer:
    """
    Manages the cadence and content of DM outreach.
    
    Provides:
    - Phase-appropriate message suggestions
    - Timing recommendations
    - Personalization based on prospect data
    - AI-enhanced message generation
    """
    
    def __init__(self):
        api_key = os.getenv("OPENAI_API_KEY")
        self.client = OpenAI(api_key=api_key) if api_key and OpenAI else None
        logger.info("✅ OutreachSequencer initialized")
    
    def get_next_message(
        self,
        phase: str,
        prospect_data: Dict,
        conversation_history: List[Dict] = None,
        context: Dict = None
    ) -> MessageSuggestion:
        """
        Get the next recommended message for a prospect.
        
        Args:
            phase: Current outreach phase
            prospect_data: Information about the prospect
            conversation_history: Previous messages in conversation
            context: Additional context (their recent posts, etc.)
        
        Returns:
            MessageSuggestion with personalized content
        """
        conversation_history = conversation_history or []
        context = context or {}
        
        # Determine template type based on phase and history
        template_type = self._determine_template_type(phase, conversation_history)
        
        # Get base template
        templates = TEMPLATES.get(phase, {}).get(template_type, [])
        if not templates:
            templates = TEMPLATES["introduction"]["first_touch"]
        
        # Pick a random template
        template = random.choice(templates)
        
        # Personalize the message
        personalized = self._personalize_message(template, prospect_data, context)
        
        # Optionally enhance with AI
        if self.client and len(conversation_history) > 0:
            personalized = self._ai_enhance_message(personalized, prospect_data, conversation_history)
        
        return MessageSuggestion(
            content=personalized,
            template_type=template_type,
            phase=phase,
            personalization_used=list(prospect_data.keys()),
            confidence=0.85
        )
    
    def _determine_template_type(self, phase: str, history: List[Dict]) -> str:
        """Determine which template type to use based on context."""
        if not history:
            return "first_touch" if phase == "introduction" else "share_resource"
        
        # Check last message
        last_direction = history[-1].get("direction") if history else None
        
        if phase == "introduction":
            if last_direction == "sent" and len(history) == 1:
                return "follow_up"
            return "first_touch"
        
        elif phase == "value":
            if len(history) < 3:
                return "share_resource"
            return "check_in"
        
        elif phase == "relationship":
            # Alternate between types
            if len(history) % 3 == 0:
                return "celebrate"
            elif len(history) % 3 == 1:
                return "personal"
            return "support"
        
        elif phase == "offer":
            if len(history) < 10:
                return "soft_intro"
            return "direct"
        
        return "first_touch"
    
    def _personalize_message(self, template: str, prospect: Dict, context: Dict) -> str:
        """Replace template variables with actual data."""
        # Extract data
        name = prospect.get("display_name") or prospect.get("username", "").replace("@", "")
        
        replacements = {
            "{name}": name.split()[0] if name else "there",
            "{topic}": context.get("topic", "content creation"),
            "{their_topic}": context.get("their_topic", "what you do"),
            "{interest}": context.get("interest", "creating content"),
            "{resource}": context.get("resource", "this guide I put together"),
            "{their_goal}": context.get("their_goal", "your goals"),
            "{tip}": context.get("tip", "a simple framework that works"),
            "{insight}": context.get("insight", "something that changed the game for me"),
            "{their_project}": context.get("their_project", "project"),
            "{milestone}": context.get("milestone", "recent win"),
            "{achievement}": context.get("achievement", "achievement"),
            "{struggle}": context.get("struggle", "a tough time"),
            "{challenge}": context.get("challenge", "that challenge"),
            "{pain_point}": context.get("pain_point", "that challenge"),
            "{offer}": context.get("offer", "what I do"),
            "{offer_type}": context.get("offer_type", "program"),
            "{goal}": context.get("goal", "your goals")
        }
        
        result = template
        for key, value in replacements.items():
            result = result.replace(key, value)
        
        return result
    
    def _ai_enhance_message(
        self, 
        base_message: str, 
        prospect: Dict, 
        history: List[Dict]
    ) -> str:
        """Use AI to enhance the message based on conversation context."""
        if not self.client:
            return base_message
        
        try:
            # Build conversation context
            convo_summary = "\n".join([
                f"{'Me' if m.get('direction') == 'sent' else 'Them'}: {m.get('content', '')[:100]}"
                for m in history[-5:]  # Last 5 messages
            ])
            
            response = self.client.chat.completions.create(
                model="gpt-4o-mini",
                messages=[
                    {
                        "role": "system",
                        "content": """You're helping craft a DM message that builds genuine connection.
                        
Rules:
- Keep it casual and authentic
- Don't be salesy
- Reference the conversation naturally
- Keep it short (1-3 sentences)
- Match their communication style"""
                    },
                    {
                        "role": "user",
                        "content": f"""Recent conversation:
{convo_summary}

Base message to improve:
{base_message}

Make this message feel more natural and connected to our conversation. Return only the improved message."""
                    }
                ],
                temperature=0.7,
                max_tokens=150
            )
            
            return response.choices[0].message.content.strip()
            
        except Exception as e:
            logger.debug(f"AI enhancement failed: {e}")
            return base_message
    
    def get_recommended_timing(self, phase: str, last_interaction: datetime = None) -> Dict:
        """
        Get recommended timing for next message.
        
        Returns optimal time to send and days to wait.
        """
        now = datetime.now(timezone.utc)
        
        # Phase-based timing
        phase_timing = {
            "introduction": {"min_days": 1, "max_days": 3, "optimal_hours": [9, 12, 18, 20]},
            "value": {"min_days": 3, "max_days": 7, "optimal_hours": [10, 14, 19]},
            "relationship": {"min_days": 5, "max_days": 14, "optimal_hours": [11, 15, 20]},
            "offer": {"min_days": 7, "max_days": 21, "optimal_hours": [10, 14, 17]}
        }
        
        timing = phase_timing.get(phase, phase_timing["introduction"])
        
        # Calculate next action date
        if last_interaction:
            days_since = (now - last_interaction).days
            if days_since < timing["min_days"]:
                days_to_wait = timing["min_days"] - days_since
            elif days_since > timing["max_days"]:
                days_to_wait = 0  # Overdue, contact now
            else:
                days_to_wait = random.randint(1, timing["max_days"] - days_since)
        else:
            days_to_wait = 0  # No previous interaction, can contact now
        
        next_date = date.today() + timedelta(days=days_to_wait)
        optimal_hour = random.choice(timing["optimal_hours"])
        
        return {
            "next_date": next_date.isoformat(),
            "days_to_wait": days_to_wait,
            "optimal_hour": optimal_hour,
            "urgency": "high" if days_to_wait == 0 else "normal"
        }
    
    def should_advance_phase(
        self,
        current_phase: str,
        interaction_count: int,
        trust_score: int,
        last_interaction: datetime = None
    ) -> Dict:
        """
        Determine if prospect should advance to next phase.
        
        Returns recommendation with reasoning.
        """
        phase_requirements = {
            "introduction": {
                "min_interactions": 2,
                "min_trust": 10,
                "next_phase": "value",
                "criteria": "Had meaningful exchange, they've responded positively"
            },
            "value": {
                "min_interactions": 5,
                "min_trust": 30,
                "next_phase": "relationship",
                "criteria": "Engaged with shared content, shows appreciation"
            },
            "relationship": {
                "min_interactions": 10,
                "min_trust": 60,
                "next_phase": "offer",
                "criteria": "Strong rapport, they trust your judgment"
            },
            "offer": {
                "min_interactions": 15,
                "min_trust": 80,
                "next_phase": None,
                "criteria": "Ready to buy or has already converted"
            }
        }
        
        req = phase_requirements.get(current_phase, phase_requirements["introduction"])
        
        should_advance = (
            interaction_count >= req["min_interactions"] and
            trust_score >= req["min_trust"]
        )
        
        return {
            "should_advance": should_advance,
            "next_phase": req["next_phase"],
            "current_phase": current_phase,
            "criteria_met": {
                "interactions": interaction_count >= req["min_interactions"],
                "trust": trust_score >= req["min_trust"]
            },
            "requirements": {
                "min_interactions": req["min_interactions"],
                "min_trust": req["min_trust"],
                "criteria": req["criteria"]
            }
        }
    
    def detect_offer_ready_signals(self, messages: List[Dict]) -> Dict:
        """
        Analyze messages to detect if prospect is ready for an offer.
        
        Looks for buying signals, pain points, and explicit requests.
        """
        ready_signals = [
            "how much", "price", "cost", "what do you charge",
            "can you help", "need help", "struggling with",
            "do you offer", "do you have", "interested in",
            "how do i", "teach me", "show me",
            "ready to", "want to start", "looking for"
        ]
        
        not_ready_signals = [
            "just curious", "maybe later", "no budget",
            "just browsing", "not right now", "too expensive",
            "can't afford", "don't need"
        ]
        
        ready_score = 0
        not_ready_score = 0
        detected_signals = []
        
        for msg in messages:
            if msg.get("direction") != "received":
                continue
            
            content = msg.get("content", "").lower()
            
            for signal in ready_signals:
                if signal in content:
                    ready_score += 1
                    detected_signals.append({"signal": signal, "type": "ready"})
            
            for signal in not_ready_signals:
                if signal in content:
                    not_ready_score += 1
                    detected_signals.append({"signal": signal, "type": "not_ready"})
        
        is_ready = ready_score >= 2 and ready_score > not_ready_score
        
        return {
            "is_ready": is_ready,
            "ready_score": ready_score,
            "not_ready_score": not_ready_score,
            "confidence": min(ready_score / 5, 1.0) if ready_score > 0 else 0,
            "detected_signals": detected_signals[:5],
            "recommendation": "Present offer" if is_ready else "Continue nurturing"
        }


# =============================================================================
# SINGLETON
# =============================================================================

_sequencer_instance: Optional[OutreachSequencer] = None

def get_outreach_sequencer() -> OutreachSequencer:
    """Get singleton instance of OutreachSequencer."""
    global _sequencer_instance
    if _sequencer_instance is None:
        _sequencer_instance = OutreachSequencer()
    return _sequencer_instance
