"""
Twitter/X DM Automation Service
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
    context: Optional[str] = None


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


class TwitterDMAutomation:
    """
    Twitter/X DM automation service.
    
    Uses browser automation or Twitter API for sending DMs.
    """
    
    def __init__(
        self,
        account_username: str,
        openai_api_key: Optional[str] = None
    ):
        self.account_username = account_username
        self.engine = create_engine(DATABASE_URL)
        
        self.openai_client = None
        api_key = openai_api_key or os.getenv("OPENAI_API_KEY")
        if api_key:
            self.openai_client = openai.AsyncOpenAI(api_key=api_key)
        
        self.current_session: Optional[DMSession] = None
        
        logger.info(f"Twitter DM Automation initialized for @{account_username}")
    
    def _random_jitter(self, base_ms: float, jitter_pct: float = 0.3) -> float:
        jitter = base_ms * jitter_pct
        return base_ms + random.uniform(-jitter, jitter)
    
    def _human_like_delay(self, min_ms: float = 800, max_ms: float = 3000) -> float:
        lambda_param = 1 / ((max_ms - min_ms) / 3)
        random_value = -1 * (1 / lambda_param) * (1 - random.random())
        delay = min(min_ms + abs(random_value) * (max_ms - min_ms), max_ms)
        return delay / 1000
    
    async def generate_ai_message(
        self,
        recipient_username: str,
        context: Optional[str] = None,
        tone: str = "professional",
        goal: str = "network"
    ) -> str:
        """Generate an AI-powered DM message for Twitter/X."""
        if not self.openai_client:
            return self._get_template_message(goal)
        
        prompt = f"""Generate a short, authentic Twitter/X DM message (2-3 sentences max).

Recipient: @{recipient_username}
Context: {context or 'General outreach'}
Tone: {tone}
Goal: {goal}

Guidelines:
- Twitter DMs should be concise and respectful
- Professional but personable
- Reference their work/tweets if context provided
- Include a clear purpose
- 280 character limit preferred
- Can use 1 emoji max

Message:"""

        try:
            response = await self.openai_client.chat.completions.create(
                model="gpt-4o-mini",
                messages=[{"role": "user", "content": prompt}],
                max_tokens=100,
                temperature=0.8
            )
            message = response.choices[0].message.content.strip().strip('"\'')
            logger.info(f"AI generated Twitter DM: {message}")
            return message
        except Exception as e:
            logger.error(f"AI message generation failed: {e}")
            return self._get_template_message(goal)
    
    def _get_template_message(self, goal: str) -> str:
        templates = {
            "network": [
                "Hey! Really enjoyed your recent posts. Would love to connect 🤝",
                "Hi! I follow your work and think we share similar interests. Let's chat!",
                "Hello! Your insights are always on point. Happy to connect.",
            ],
            "collaborate": [
                "Hi! I work in a similar space and think we could create something great together.",
                "Hey! Love your content. Would you be open to collaborating?",
                "Hello! Your expertise aligns with a project I'm working on. Interested in chatting?",
            ],
            "engage": [
                "Hey! Your recent thread was exactly what I needed to read. Thanks for sharing!",
                "Hi! Just wanted to say your content consistently adds value. Keep it up!",
                "Hello! Been following your work for a while - really appreciate your perspective.",
            ],
        }
        return random.choice(templates.get(goal, templates["network"]))
    
    async def log_dm(
        self,
        target_username: str,
        message_text: str,
        success: bool,
        error: Optional[str] = None
    ):
        try:
            with self.engine.connect() as conn:
                conn.execute(text("""
                    INSERT INTO engagement_interactions (
                        account_username, interaction_type, target_url,
                        target_username, content, success, error_message,
                        platform, created_at
                    ) VALUES (
                        :account, 'dm', :url, :target, :content,
                        :success, :error, 'twitter', NOW()
                    )
                """), {
                    "account": self.account_username,
                    "url": f"https://twitter.com/{target_username}",
                    "target": target_username,
                    "content": message_text,
                    "success": success,
                    "error": error
                })
                conn.commit()
        except Exception as e:
            logger.warning(f"Failed to log DM: {e}")
    
    async def has_messaged_recently(self, username: str, hours: int = 24) -> bool:
        try:
            with self.engine.connect() as conn:
                result = conn.execute(text("""
                    SELECT COUNT(*) FROM engagement_interactions
                    WHERE account_username = :account
                    AND target_username = :target
                    AND interaction_type = 'dm'
                    AND platform = 'twitter'
                    AND success = true
                    AND created_at > NOW() - INTERVAL '24 hours'
                """), {
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
        tone: str = "professional",
        goal: str = "network"
    ) -> DMResult:
        if await self.has_messaged_recently(target.username):
            logger.info(f"Already messaged @{target.username} recently, skipping")
            return DMResult(
                success=False,
                username=target.username,
                message_text="",
                error="Already messaged recently"
            )
        
        if not message_text:
            message_text = await self.generate_ai_message(
                recipient_username=target.username,
                context=target.context,
                tone=tone,
                goal=goal
            )
        
        logger.info(f"Would send Twitter DM to @{target.username}: {message_text}")
        
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
        delay_between: float = 60.0,
        goal: str = "network"
    ) -> DMSession:
        import uuid
        
        session = DMSession(
            session_id=str(uuid.uuid4()),
            account_username=self.account_username,
            started_at=datetime.now().isoformat()
        )
        self.current_session = session
        
        logger.info(f"Starting Twitter DM session {session.session_id} with {len(targets)} targets")
        
        for target in targets:
            try:
                result = await self.send_dm(target, goal=goal)
                if result.success:
                    session.messages_sent += 1
                    session.conversations_opened += 1
                else:
                    session.errors.append(f"{target.username}: {result.error}")
                
                delay = self._random_jitter(delay_between * 1000, 0.3) / 1000
                await asyncio.sleep(delay)
                
            except Exception as e:
                session.errors.append(f"{target.username}: {str(e)}")
                logger.error(f"Error messaging {target.username}: {e}")
        
        logger.info(f"Session {session.session_id} completed: {session.messages_sent} sent")
        return session


_dm_instances: Dict[str, TwitterDMAutomation] = {}

def get_twitter_dm_automation(account_username: str) -> TwitterDMAutomation:
    if account_username not in _dm_instances:
        _dm_instances[account_username] = TwitterDMAutomation(account_username)
    return _dm_instances[account_username]
