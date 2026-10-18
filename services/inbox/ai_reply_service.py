"""
AI Reply Service for Community Inbox
Generates AI-powered reply suggestions for inbox messages.
"""

import os
import json
from typing import Dict, List, Optional, Any
from dataclasses import dataclass
from loguru import logger
from openai import OpenAI


@dataclass
class ReplySuggestion:
    """AI-generated reply suggestion."""
    content: str
    tone: str  # friendly, professional, casual, enthusiastic
    intent: str  # thank, answer, redirect, engage
    confidence: float
    variables_used: List[str]


class AIReplyService:
    """
    AI-powered reply suggestion service.
    
    Generates contextual reply suggestions based on:
    - Message content and sentiment
    - Sender profile and engagement history
    - Platform-specific best practices
    """
    
    def __init__(self):
        api_key = os.getenv("OPENAI_API_KEY")
        self.client = OpenAI(api_key=api_key) if api_key else None
        logger.info("✅ AIReplyService initialized")
    
    async def generate_suggestions(
        self,
        message_content: str,
        sender_username: str,
        platform: str,
        message_type: str = "comment",
        context: Optional[Dict] = None,
        num_suggestions: int = 3
    ) -> List[ReplySuggestion]:
        """
        Generate reply suggestions for a message.
        
        Args:
            message_content: The incoming message text
            sender_username: Username of the sender
            platform: Platform the message came from
            message_type: Type of message (comment, dm, mention)
            context: Additional context (post caption, previous messages, etc.)
            num_suggestions: Number of suggestions to generate
        
        Returns:
            List of ReplySuggestion objects
        """
        if not self.client:
            # Return default suggestions if no API key
            return self._get_default_suggestions(message_content, sender_username)
        
        try:
            system_prompt = self._build_system_prompt(platform, message_type)
            user_prompt = self._build_user_prompt(
                message_content, sender_username, context
            )
            
            response = self.client.chat.completions.create(
                model="gpt-4o-mini",
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_prompt}
                ],
                temperature=0.8,
                response_format={"type": "json_object"}
            )
            
            result = json.loads(response.choices[0].message.content)
            suggestions = result.get("suggestions", [])
            
            return [
                ReplySuggestion(
                    content=s.get("content", ""),
                    tone=s.get("tone", "friendly"),
                    intent=s.get("intent", "engage"),
                    confidence=s.get("confidence", 0.8),
                    variables_used=s.get("variables", [])
                )
                for s in suggestions[:num_suggestions]
            ]
            
        except Exception as e:
            logger.error(f"AI reply generation failed: {e}")
            return self._get_default_suggestions(message_content, sender_username)
    
    def _build_system_prompt(self, platform: str, message_type: str) -> str:
        """Build system prompt based on platform and message type."""
        platform_guidelines = {
            "instagram": "Keep replies casual, use emojis sparingly, be authentic",
            "tiktok": "Keep it fun and energetic, match TikTok's playful vibe",
            "twitter": "Be concise (under 280 chars if possible), witty, engaging",
            "youtube": "Be helpful and informative, encourage engagement",
            "threads": "Conversational, thoughtful, community-focused"
        }
        
        type_guidelines = {
            "comment": "Reply publicly - keep it positive and engaging",
            "dm": "This is a private conversation - be more personal",
            "mention": "They tagged you - acknowledge and engage",
            "story_reply": "Casual and personal, they're starting a conversation"
        }
        
        return f"""You are an AI assistant helping generate reply suggestions for social media messages.

Platform: {platform}
Platform Guidelines: {platform_guidelines.get(platform, "Be friendly and authentic")}

Message Type: {message_type}
Type Guidelines: {type_guidelines.get(message_type, "Be engaging and helpful")}

Generate {3} different reply suggestions with varying tones:
1. Friendly/Casual
2. Professional/Helpful  
3. Enthusiastic/Engaging

Return JSON with format:
{{
    "suggestions": [
        {{
            "content": "reply text",
            "tone": "friendly|professional|casual|enthusiastic",
            "intent": "thank|answer|redirect|engage|support",
            "confidence": 0.0-1.0,
            "variables": ["name", "topic"] // any dynamic parts
        }}
    ]
}}

Keep replies:
- Authentic and human-sounding
- Appropriate for the platform
- Under 500 characters each
- Free of excessive emojis or hashtags"""
    
    def _build_user_prompt(
        self,
        message_content: str,
        sender_username: str,
        context: Optional[Dict]
    ) -> str:
        """Build user prompt with message context."""
        prompt = f"""Generate reply suggestions for this message:

From: @{sender_username}
Message: "{message_content}"
"""
        
        if context:
            if context.get("post_caption"):
                prompt += f"\nOriginal Post: \"{context['post_caption'][:200]}...\""
            
            if context.get("sender_follower_count"):
                followers = context["sender_follower_count"]
                if followers > 100000:
                    prompt += f"\nNote: Sender has {followers:,} followers (influencer)"
                elif followers > 10000:
                    prompt += f"\nNote: Sender has {followers:,} followers (creator)"
            
            if context.get("previous_interactions"):
                prompt += f"\nNote: You've interacted {context['previous_interactions']} times before"
        
        prompt += "\n\nGenerate 3 different reply suggestions."
        return prompt
    
    def _get_default_suggestions(
        self,
        message_content: str,
        sender_username: str
    ) -> List[ReplySuggestion]:
        """Return default suggestions when AI is unavailable."""
        return [
            ReplySuggestion(
                content=f"Thanks for reaching out @{sender_username}! 🙌",
                tone="friendly",
                intent="thank",
                confidence=0.7,
                variables_used=["username"]
            ),
            ReplySuggestion(
                content="Appreciate you! Let me know if you have any questions.",
                tone="professional",
                intent="engage",
                confidence=0.6,
                variables_used=[]
            ),
            ReplySuggestion(
                content="Love this! Thanks for sharing 💯",
                tone="enthusiastic",
                intent="thank",
                confidence=0.6,
                variables_used=[]
            )
        ]
    
    async def analyze_sentiment(self, message_content: str) -> Dict:
        """Analyze message sentiment and intent."""
        if not self.client:
            return {
                "sentiment": "neutral",
                "intent": "general",
                "priority": "medium",
                "is_question": "?" in message_content,
                "needs_response": True
            }
        
        try:
            response = self.client.chat.completions.create(
                model="gpt-4o-mini",
                messages=[
                    {
                        "role": "system",
                        "content": """Analyze this social media message and return JSON:
{
    "sentiment": "positive|neutral|negative|question",
    "intent": "praise|complaint|question|request|spam|general",
    "priority": "high|medium|low",
    "is_question": true/false,
    "needs_response": true/false,
    "suggested_action": "reply|thank|address|ignore|escalate"
}"""
                    },
                    {
                        "role": "user",
                        "content": message_content
                    }
                ],
                temperature=0.3,
                response_format={"type": "json_object"}
            )
            
            return json.loads(response.choices[0].message.content)
            
        except Exception as e:
            logger.error(f"Sentiment analysis failed: {e}")
            return {
                "sentiment": "neutral",
                "intent": "general",
                "priority": "medium",
                "is_question": "?" in message_content,
                "needs_response": True
            }
    
    async def generate_quick_reply(
        self,
        message_content: str,
        reply_type: str = "thank"
    ) -> str:
        """Generate a quick one-liner reply."""
        quick_replies = {
            "thank": [
                "Thank you! 🙏",
                "Appreciate it! 💯",
                "Thanks so much! 🙌",
                "You're the best! ❤️"
            ],
            "acknowledge": [
                "Got it! ✅",
                "Noted! Thanks for sharing",
                "Thanks for letting me know!",
                "Appreciate the feedback!"
            ],
            "engage": [
                "Love this! Tell me more 👀",
                "This is great! What inspired you?",
                "So cool! How'd you come up with that?",
                "Amazing! Keep them coming 🔥"
            ],
            "support": [
                "I hear you! Here if you need anything",
                "Totally understand! Happy to help",
                "That makes sense! Let me know how I can support",
                "I got you! DM me if you need anything"
            ]
        }
        
        import random
        replies = quick_replies.get(reply_type, quick_replies["thank"])
        return random.choice(replies)


# =============================================================================
# SINGLETON
# =============================================================================

_ai_reply_instance: Optional[AIReplyService] = None

def get_ai_reply_service() -> AIReplyService:
    """Get singleton instance of AIReplyService."""
    global _ai_reply_instance
    if _ai_reply_instance is None:
        _ai_reply_instance = AIReplyService()
    return _ai_reply_instance
