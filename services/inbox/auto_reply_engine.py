"""
Auto-Reply Rules Engine (INBOX-006)
===================================
Configures and executes automatic replies based on keywords/patterns.

Features:
- Keyword and regex pattern matching
- Sentiment-based filtering
- Platform-specific rules
- Message type filtering
- Daily usage quotas
- Variable substitution ({name}, {username}, etc.)
- Priority-based rule execution
- AI suggestion tracking

Usage:
    engine = AutoReplyEngine.get_instance()

    # Check if any rules match a message and apply auto-reply
    reply_text = await engine.check_and_apply_rules(message_id)

    # Create a custom rule
    await engine.create_rule(
        name="FAQ: Pricing",
        keywords=["price", "cost", "pricing"],
        reply_template="Check our pricing at {offer_url}",
        priority=10
    )
"""

import re
import asyncio
from datetime import datetime, timezone, timedelta
from typing import List, Dict, Any, Optional
from uuid import uuid4
from enum import Enum
from loguru import logger

from sqlalchemy import select, and_, func, update
from sqlalchemy.ext.asyncio import AsyncSession

from database.models import CommunityInboxMessage, InboxAutoReplyRule
from database.connection import async_session_maker
from services.event_bus import EventBus
from services.sentiment_analyzer import get_sentiment_analyzer


class KeywordMatchType(str, Enum):
    """Types of keyword matching"""
    EXACT = "exact"
    CONTAINS = "contains"
    STARTS_WITH = "starts_with"
    REGEX = "regex"


class AutoReplyEngine:
    """
    INBOX-006: Auto-Reply Rules Engine

    Automatically responds to inbox messages based on configured rules.
    Supports keyword matching, sentiment filtering, and variable substitution.

    Usage:
        engine = AutoReplyEngine.get_instance()

        # Create a rule
        rule_id = await engine.create_rule(
            name="Welcome New Followers",
            keywords=["hello", "hi", "hey"],
            reply_template="Thanks for reaching out! 👋 How can I help?",
            max_uses_per_day=100
        )

        # Apply rules to a message
        reply = await engine.check_and_apply_rules(message_id)
        if reply:
            print(f"Auto-reply sent: {reply}")
    """

    _instance: Optional["AutoReplyEngine"] = None

    def __init__(self):
        """Initialize auto-reply engine"""
        if AutoReplyEngine._instance is not None:
            raise RuntimeError("Use AutoReplyEngine.get_instance()")

        self.event_bus = EventBus.get_instance()
        self.event_bus.set_source("auto-reply-engine")
        self.sentiment_analyzer = get_sentiment_analyzer()

        # Cache of daily usage per rule (rule_id -> count)
        # Reset daily at midnight UTC
        self._daily_usage: Dict[str, int] = {}
        self._usage_reset_time: Optional[datetime] = None

        logger.info("⚙️ Auto-Reply Engine initialized")

    @classmethod
    def get_instance(cls) -> "AutoReplyEngine":
        """Get singleton instance"""
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance

    # =========================================================================
    # RULE MANAGEMENT
    # =========================================================================

    async def create_rule(
        self,
        name: str,
        reply_template: str,
        keywords: Optional[List[str]] = None,
        keyword_match_type: str = KeywordMatchType.CONTAINS,
        sentiment_filter: str = "any",
        platforms: Optional[List[str]] = None,
        message_types: Optional[List[str]] = None,
        max_uses_per_day: int = 1000,
        priority: int = 50,
        workspace_id: Optional[str] = None
    ) -> str:
        """
        Create a new auto-reply rule

        Args:
            name: Rule name for display
            reply_template: Template text with {variable} placeholders
            keywords: List of keywords to match
            keyword_match_type: How to match keywords (exact, contains, starts_with, regex)
            sentiment_filter: Filter by sentiment (any, positive, neutral, negative)
            platforms: Platforms to apply rule to (empty = all)
            message_types: Message types to apply to (empty = all)
            max_uses_per_day: Maximum times rule can trigger daily
            priority: Priority for rule execution (0-100, higher = executed first)
            workspace_id: Workspace UUID

        Returns:
            Rule ID
        """
        rule_id = str(uuid4())

        async with async_session_maker() as session:
            rule = InboxAutoReplyRule(
                id=rule_id,
                workspace_id=workspace_id,
                name=name,
                reply_template=reply_template,
                keyword_triggers=keywords or [],
                keyword_match_type=keyword_match_type,
                sentiment_filter=sentiment_filter,
                platforms=platforms or [],
                message_types=message_types or [],
                max_uses_per_day=max_uses_per_day,
                priority=priority,
                is_active=True,
                uses_today=0,
                created_at=datetime.now(timezone.utc)
            )

            session.add(rule)
            await session.commit()

            logger.info(f"✓ Auto-reply rule created: {name} (ID: {rule_id})")

        return rule_id

    async def update_rule(
        self,
        rule_id: str,
        **kwargs
    ) -> bool:
        """Update an existing rule"""
        async with async_session_maker() as session:
            stmt = select(InboxAutoReplyRule).where(InboxAutoReplyRule.id == rule_id)
            result = await session.execute(stmt)
            rule = result.scalars().first()

            if not rule:
                logger.warning(f"Rule not found: {rule_id}")
                return False

            for key, value in kwargs.items():
                if hasattr(rule, key):
                    setattr(rule, key, value)

            await session.commit()
            logger.info(f"✓ Auto-reply rule updated: {rule_id}")

        return True

    async def delete_rule(self, rule_id: str) -> bool:
        """Delete a rule"""
        async with async_session_maker() as session:
            stmt = select(InboxAutoReplyRule).where(InboxAutoReplyRule.id == rule_id)
            result = await session.execute(stmt)
            rule = result.scalars().first()

            if not rule:
                logger.warning(f"Rule not found: {rule_id}")
                return False

            await session.delete(rule)
            await session.commit()
            logger.info(f"✓ Auto-reply rule deleted: {rule_id}")

        return True

    async def get_active_rules(self, workspace_id: Optional[str] = None) -> List[InboxAutoReplyRule]:
        """Get all active rules"""
        async with async_session_maker() as session:
            stmt = select(InboxAutoReplyRule).where(
                InboxAutoReplyRule.is_active == True
            )

            if workspace_id:
                stmt = stmt.where(InboxAutoReplyRule.workspace_id == workspace_id)

            # Sort by priority (higher first) then creation time
            stmt = stmt.order_by(
                InboxAutoReplyRule.priority.desc(),
                InboxAutoReplyRule.created_at.asc()
            )

            result = await session.execute(stmt)
            return result.scalars().all()

    # =========================================================================
    # RULE MATCHING & EXECUTION
    # =========================================================================

    async def check_and_apply_rules(
        self,
        message_id: str,
        auto_send: bool = True
    ) -> Optional[str]:
        """
        Check if any rules match a message and apply auto-reply

        Args:
            message_id: Message ID to check
            auto_send: If True, send the reply; if False, just return text

        Returns:
            Reply text if matched, None otherwise
        """
        async with async_session_maker() as session:
            # Get message
            stmt = select(CommunityInboxMessage).where(
                CommunityInboxMessage.id == message_id
            )
            result = await session.execute(stmt)
            message = result.scalars().first()

            if not message:
                logger.warning(f"Message not found: {message_id}")
                return None

            # Get active rules
            rules = await self.get_active_rules(message.workspace_id)

            # Check each rule in priority order
            for rule in rules:
                if await self._matches_rule(message, rule, session):
                    # Check daily limit
                    if not await self._can_apply_rule(rule, session):
                        logger.debug(f"Rule daily limit reached: {rule.name}")
                        continue

                    # Execute rule
                    reply_text = await self._execute_rule(rule, message)

                    if auto_send:
                        # Record the reply
                        await self._record_reply(
                            session,
                            message_id,
                            reply_text,
                            rule.id,
                            "auto_rule"
                        )

                        # Increment usage
                        await self._increment_rule_usage(rule.id, session)

                        # Publish event
                        await self.event_bus.publish(
                            "inbox.auto_reply.executed",
                            {
                                "message_id": message_id,
                                "rule_id": rule.id,
                                "rule_name": rule.name,
                                "platform": message.platform,
                                "timestamp": datetime.now(timezone.utc).isoformat()
                            }
                        )

                        logger.info(
                            f"✓ Auto-reply sent for '{rule.name}' "
                            f"(platform: {message.platform})"
                        )

                    return reply_text

        return None

    async def _matches_rule(
        self,
        message: CommunityInboxMessage,
        rule: InboxAutoReplyRule,
        session: AsyncSession
    ) -> bool:
        """Check if message matches rule conditions"""
        # Check platform filter
        if rule.platforms and message.platform not in rule.platforms:
            return False

        # Check message type filter
        if rule.message_types and message.message_type not in rule.message_types:
            return False

        # Check sentiment filter
        if rule.sentiment_filter != "any":
            if not self._match_sentiment(message.sentiment_label, rule.sentiment_filter):
                return False

        # Check keyword triggers
        if rule.keyword_triggers:
            if not self._match_keywords(
                message.content,
                rule.keyword_triggers,
                rule.keyword_match_type
            ):
                return False

        return True

    def _match_keywords(
        self,
        text: str,
        keywords: List[str],
        match_type: str = KeywordMatchType.CONTAINS
    ) -> bool:
        """Match text against keywords based on match type"""
        text_lower = text.lower()

        for keyword in keywords:
            keyword_lower = keyword.lower()

            if match_type == KeywordMatchType.EXACT:
                if keyword_lower == text_lower:
                    return True

            elif match_type == KeywordMatchType.CONTAINS:
                if keyword_lower in text_lower:
                    return True

            elif match_type == KeywordMatchType.STARTS_WITH:
                if text_lower.startswith(keyword_lower):
                    return True

            elif match_type == KeywordMatchType.REGEX:
                try:
                    if re.search(keyword_lower, text_lower, re.IGNORECASE):
                        return True
                except re.error:
                    logger.warning(f"Invalid regex: {keyword}")
                    continue

        return False

    def _match_sentiment(self, message_sentiment: str, filter_sentiment: str) -> bool:
        """Check if message sentiment matches filter"""
        if filter_sentiment == "any":
            return True

        return message_sentiment.lower() == filter_sentiment.lower()

    async def _execute_rule(
        self,
        rule: InboxAutoReplyRule,
        message: CommunityInboxMessage
    ) -> str:
        """Execute rule and substitute variables"""
        reply_text = rule.reply_template

        # Variable substitution
        variables = {
            "name": message.sender_name or message.sender_username,
            "username": message.sender_username,
            "platform": message.platform,
            "message_type": message.message_type,
        }

        for var_name, var_value in variables.items():
            reply_text = reply_text.replace(f"{{{var_name}}}", str(var_value))

        return reply_text

    async def _can_apply_rule(
        self,
        rule: InboxAutoReplyRule,
        session: AsyncSession
    ) -> bool:
        """Check if rule has remaining daily uses"""
        # Ensure daily usage cache is initialized
        await self._ensure_daily_reset()

        # Get current usage
        current_usage = self._daily_usage.get(rule.id, 0)

        return current_usage < rule.max_uses_per_day

    async def _increment_rule_usage(
        self,
        rule_id: str,
        session: AsyncSession
    ) -> None:
        """Increment rule usage count"""
        await self._ensure_daily_reset()

        self._daily_usage[rule_id] = self._daily_usage.get(rule_id, 0) + 1

        # Also update database
        stmt = update(InboxAutoReplyRule).where(
            InboxAutoReplyRule.id == rule_id
        ).values(
            uses_today=InboxAutoReplyRule.uses_today + 1
        )

        await session.execute(stmt)
        await session.commit()

    async def _ensure_daily_reset(self) -> None:
        """Reset daily usage counter if needed"""
        now = datetime.now(timezone.utc)

        # Check if we need to reset (midnight UTC passed)
        if self._usage_reset_time is None or now.date() > self._usage_reset_time.date():
            async with async_session_maker() as session:
                # Reset database usage counters
                stmt = update(InboxAutoReplyRule).values(uses_today=0)
                await session.execute(stmt)
                await session.commit()

            # Reset in-memory cache
            self._daily_usage = {}
            self._usage_reset_time = now

            logger.info("🔄 Daily auto-reply usage counter reset")

    async def _record_reply(
        self,
        session: AsyncSession,
        message_id: str,
        reply_text: str,
        source_id: str,
        source_type: str
    ) -> None:
        """Record a reply to the message"""
        # Update message status
        stmt = select(CommunityInboxMessage).where(
            CommunityInboxMessage.id == message_id
        )
        result = await session.execute(stmt)
        message = result.scalars().first()

        if message:
            message.response_text = reply_text
            message.response_method = source_type
            message.response_source_id = source_id
            message.response_status = "sent"
            message.responded_at = datetime.now(timezone.utc)
            message.status = "replied"

            # Calculate response time
            if message.received_at:
                response_time = datetime.now(timezone.utc) - message.received_at
                message.response_time_minutes = int(response_time.total_seconds() / 60)

            await session.commit()

    # =========================================================================
    # TESTING & VALIDATION
    # =========================================================================

    async def test_rule(
        self,
        rule_id: str,
        test_message_text: str,
        test_platform: str = "instagram",
        test_sentiment: str = "neutral"
    ) -> Dict[str, Any]:
        """
        Test a rule against sample message text

        Returns:
            Result dictionary with matched, reply_text, etc.
        """
        async with async_session_maker() as session:
            # Get rule
            stmt = select(InboxAutoReplyRule).where(InboxAutoReplyRule.id == rule_id)
            result = await session.execute(stmt)
            rule = result.scalars().first()

            if not rule:
                return {"matched": False, "error": "Rule not found"}

            # Create mock message
            mock_message = CommunityInboxMessage(
                id="test_message",
                workspace_id=rule.workspace_id,
                platform=test_platform,
                message_type="comment",
                sender_username="test_user",
                sender_name="Test User",
                content=test_message_text,
                sentiment_label=test_sentiment,
                received_at=datetime.now(timezone.utc),
                status="unread"
            )

            # Test matching
            matched = await self._matches_rule(mock_message, rule, session)

            result_dict = {
                "matched": matched,
                "rule_name": rule.name,
                "platform_filter": rule.platforms,
                "keyword_triggers": rule.keyword_triggers,
                "sentiment_filter": rule.sentiment_filter
            }

            if matched:
                reply_text = await self._execute_rule(rule, mock_message)
                result_dict["reply_text"] = reply_text

            return result_dict


# =============================================================================
# SINGLETON
# =============================================================================

_engine_instance: Optional[AutoReplyEngine] = None


def get_auto_reply_engine() -> AutoReplyEngine:
    """Get singleton instance of AutoReplyEngine"""
    global _engine_instance
    if _engine_instance is None:
        _engine_instance = AutoReplyEngine()
    return _engine_instance
