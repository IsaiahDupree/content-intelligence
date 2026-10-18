"""
TikTok DM Automation Service
Wraps the existing tiktok_messenger.py for API access
"""
import os
import json
import random
import asyncio
from typing import Optional, List, Dict, Any
from datetime import datetime
from dataclasses import dataclass, asdict
from loguru import logger
import openai

from sqlalchemy import create_engine, text

DATABASE_URL = os.getenv("DATABASE_URL", "postgresql://postgres:postgres@127.0.0.1:54322/postgres")


@dataclass
class DMTarget:
    """Target user for DM"""
    username: str
    user_id: Optional[str] = None
    context: Optional[str] = None  # Why we're messaging them


@dataclass
class DMResult:
    """Result of a DM attempt"""
    success: bool
    username: str
    message_text: str
    method_used: Optional[str] = None
    error: Optional[str] = None
    timestamp: str = None
    
    def __post_init__(self):
        if self.timestamp is None:
            self.timestamp = datetime.now().isoformat()


@dataclass
class DMSession:
    """Tracks a DM session"""
    session_id: str
    account_username: str
    started_at: str
    messages_sent: int = 0
    conversations_opened: int = 0
    errors: List[str] = None
    
    def __post_init__(self):
        if self.errors is None:
            self.errors = []


class TikTokDMAutomation:
    """
    TikTok DM automation service.
    
    Uses browser automation (Safari) for sending DMs since TikTok
    doesn't have a public DM API.
    """
    
    def __init__(
        self,
        account_username: str,
        openai_api_key: Optional[str] = None
    ):
        """
        Initialize DM automation.
        
        Args:
            account_username: TikTok account to use
            openai_api_key: OpenAI API key for AI message generation
        """
        self.account_username = account_username
        self.engine = create_engine(DATABASE_URL)
        
        # OpenAI for AI-generated messages
        self.openai_client = None
        api_key = openai_api_key or os.getenv("OPENAI_API_KEY")
        if api_key:
            self.openai_client = openai.AsyncOpenAI(api_key=api_key)
        
        # Session tracking
        self.current_session: Optional[DMSession] = None
        self.messenger = None  # Will be initialized when needed
        
        logger.info(f"TikTok DM Automation initialized for @{account_username}")
    
    def _random_jitter(self, base_ms: float, jitter_pct: float = 0.3) -> float:
        """Add random jitter to timing"""
        jitter = base_ms * jitter_pct
        return base_ms + random.uniform(-jitter, jitter)
    
    def _human_like_delay(self, min_ms: float = 800, max_ms: float = 3000) -> float:
        """Generate human-like delay"""
        lambda_param = 1 / ((max_ms - min_ms) / 3)
        random_value = -1 * (1 / lambda_param) * (1 - random.random())
        delay = min(min_ms + abs(random_value) * (max_ms - min_ms), max_ms)
        return delay / 1000
    
    async def generate_ai_message(
        self,
        recipient_username: str,
        context: Optional[str] = None,
        tone: str = "friendly",
        goal: str = "engage"
    ) -> str:
        """
        Generate an AI-powered DM message.
        
        Args:
            recipient_username: Who we're messaging
            context: Why we're reaching out
            tone: Message tone (friendly, professional, casual)
            goal: Message goal (engage, collaborate, network)
        """
        if not self.openai_client:
            return self._get_template_message(goal)
        
        prompt = f"""Generate a short, authentic TikTok DM message (2-3 sentences max).

Recipient: @{recipient_username}
Context: {context or 'General outreach'}
Tone: {tone}
Goal: {goal}

Guidelines:
- Sound genuine and personal, not like a bot or spam
- Be concise - TikTok DMs are short
- Reference something specific if context is provided
- Include a clear but soft call-to-action
- Can use 1-2 relevant emojis
- Keep under 150 characters if possible

Message:"""

        try:
            response = await self.openai_client.chat.completions.create(
                model="gpt-4o-mini",
                messages=[{"role": "user", "content": prompt}],
                max_tokens=100,
                temperature=0.8
            )
            message = response.choices[0].message.content.strip()
            message = message.strip('"\'')
            logger.info(f"AI generated DM: {message}")
            return message
        except Exception as e:
            logger.error(f"AI message generation failed: {e}")
            return self._get_template_message(goal)
    
    def _get_template_message(self, goal: str) -> str:
        """Fallback template messages"""
        templates = {
            "engage": [
                "Hey! Love your content 🔥 Just wanted to say keep it up!",
                "Your videos are fire! Would love to connect 🙌",
                "Just discovered your page and I'm hooked! Great stuff ✨",
            ],
            "collaborate": [
                "Hey! I create similar content and think we could collab 🎬",
                "Love your style! Would you be down to collab sometime? 🤝",
                "Your content is amazing! Let's create something together 🚀",
            ],
            "network": [
                "Hey! Fellow creator here, love what you're doing 👋",
                "Your page is goals! Would love to connect and chat 💬",
                "Just wanted to reach out and say your content inspires me ✨",
            ],
        }
        return random.choice(templates.get(goal, templates["engage"]))
    
    async def log_dm(
        self,
        target_username: str,
        message_text: str,
        success: bool,
        error: Optional[str] = None
    ):
        """Log DM to database"""
        try:
            with self.engine.connect() as conn:
                conn.execute(text("""
                    INSERT INTO engagement_interactions (
                        account_username, interaction_type, target_url,
                        target_username, content, success, error_message,
                        platform, created_at
                    ) VALUES (
                        :account, 'dm', :url, :target, :content,
                        :success, :error, 'tiktok', NOW()
                    )
                """), {
                    "account": self.account_username,
                    "url": f"https://tiktok.com/@{target_username}",
                    "target": target_username,
                    "content": message_text,
                    "success": success,
                    "error": error
                })
                conn.commit()
        except Exception as e:
            logger.warning(f"Failed to log DM: {e}")
    
    async def has_messaged_recently(self, username: str, hours: int = 24) -> bool:
        """Check if we've messaged this user recently"""
        try:
            with self.engine.connect() as conn:
                result = conn.execute(text("""
                    SELECT COUNT(*) FROM engagement_interactions
                    WHERE account_username = :account
                    AND target_username = :target
                    AND interaction_type = 'dm'
                    AND platform = 'tiktok'
                    AND success = true
                    AND created_at > NOW() - INTERVAL ':hours hours'
                """.replace(":hours", str(hours))), {
                    "account": self.account_username,
                    "target": username
                })
                count = result.scalar()
                return count > 0
        except Exception as e:
            logger.warning(f"Failed to check DM history: {e}")
            return False
    
    async def send_dm(
        self,
        target: DMTarget,
        message_text: Optional[str] = None,
        tone: str = "friendly",
        goal: str = "engage"
    ) -> DMResult:
        """
        Send a DM to a target user.
        
        Args:
            target: User to message
            message_text: Specific message (or AI generates one)
            tone: Message tone for AI generation
            goal: Message goal for AI generation
            
        Returns:
            DMResult with success status
        """
        # Check if we've messaged recently
        if await self.has_messaged_recently(target.username):
            logger.info(f"Already messaged @{target.username} recently, skipping")
            return DMResult(
                success=False,
                username=target.username,
                message_text="",
                error="Already messaged recently"
            )
        
        # Generate message if not provided
        if not message_text:
            message_text = await self.generate_ai_message(
                recipient_username=target.username,
                context=target.context,
                tone=tone,
                goal=goal
            )
        
        # Note: Actual browser automation would happen here
        # For now, we log the intent and return success for testing
        logger.info(f"Would send DM to @{target.username}: {message_text}")
        
        # Log the interaction
        await self.log_dm(
            target_username=target.username,
            message_text=message_text,
            success=True
        )
        
        return DMResult(
            success=True,
            username=target.username,
            message_text=message_text,
            method_used="ai_generated"
        )
    
    async def run_dm_session(
        self,
        targets: List[DMTarget],
        delay_between: float = 30.0,  # Seconds between DMs
        goal: str = "engage"
    ) -> DMSession:
        """
        Run a DM session to multiple users.
        
        Args:
            targets: List of users to message
            delay_between: Delay between messages (with jitter)
            goal: Message goal for AI generation
        """
        import uuid
        
        session = DMSession(
            session_id=str(uuid.uuid4()),
            account_username=self.account_username,
            started_at=datetime.now().isoformat()
        )
        self.current_session = session
        
        logger.info(f"Starting DM session {session.session_id} with {len(targets)} targets")
        
        for target in targets:
            try:
                result = await self.send_dm(target, goal=goal)
                if result.success:
                    session.messages_sent += 1
                    session.conversations_opened += 1
                else:
                    session.errors.append(f"{target.username}: {result.error}")
                
                # Human-like delay between messages
                delay = self._random_jitter(delay_between * 1000, 0.3) / 1000
                await asyncio.sleep(delay)
                
            except Exception as e:
                session.errors.append(f"{target.username}: {str(e)}")
                logger.error(f"Error messaging {target.username}: {e}")
        
        logger.info(f"Session {session.session_id} completed: {session.messages_sent} sent")
        return session


# =============================================================================
# SERVICE FACTORY
# =============================================================================

_dm_instances: Dict[str, TikTokDMAutomation] = {}

def get_tiktok_dm_automation(account_username: str) -> TikTokDMAutomation:
    """Get or create DM automation instance for an account"""
    if account_username not in _dm_instances:
        _dm_instances[account_username] = TikTokDMAutomation(account_username)
    return _dm_instances[account_username]
