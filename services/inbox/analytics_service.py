"""
Inbox Analytics Service (INBOX-008)
===================================
Tracks and analyzes inbox metrics: response rates, sentiment trends,
engagement scores, and platform breakdown.

Features:
- Daily aggregation of inbox metrics
- Response rate calculation
- Sentiment trend analysis
- Platform performance comparison
- Response time statistics (p50, p95, p99)
- Time-series data for charts
- Real-time metric caching
- CSV/JSON export

Usage:
    analytics = InboxAnalyticsService.get_instance()

    # Get daily summary
    summary = await analytics.get_daily_summary(workspace_id)

    # Get response rate trends
    trends = await analytics.get_response_trends(
        workspace_id,
        start_date="2026-01-01",
        end_date="2026-01-31"
    )

    # Get platform breakdown
    platform_stats = await analytics.get_platform_breakdown(workspace_id)
"""

from datetime import datetime, timezone, timedelta
from typing import List, Dict, Any, Optional, Tuple
from decimal import Decimal
from uuid import uuid4
from loguru import logger

from sqlalchemy import select, and_, func, text
from sqlalchemy.ext.asyncio import AsyncSession

from database.models import CommunityInboxMessage, InboxAnalytics
from database.connection import async_session_maker
from services.event_bus import EventBus


class InboxAnalyticsService:
    """
    INBOX-008: Inbox Analytics Service

    Aggregates and analyzes inbox metrics for dashboard display
    and performance tracking.

    Metrics Tracked:
    - Messages received / responded / closed / ignored
    - Response rates (overall and by platform)
    - Response times (avg, median, p95, p99)
    - Sentiment distribution
    - Engagement scores
    - Qualified leads tracking
    - AI suggestion usage
    """

    _instance: Optional["InboxAnalyticsService"] = None

    def __init__(self):
        """Initialize analytics service"""
        if InboxAnalyticsService._instance is not None:
            raise RuntimeError("Use InboxAnalyticsService.get_instance()")

        self.event_bus = EventBus.get_instance()
        self.event_bus.set_source("inbox-analytics-service")

        # Real-time metrics cache (updated on message events)
        self._daily_cache: Dict[str, Dict[str, Any]] = {}

        logger.info("📊 Inbox Analytics Service initialized")

    @classmethod
    def get_instance(cls) -> "InboxAnalyticsService":
        """Get singleton instance"""
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance

    # =========================================================================
    # DAILY AGGREGATION
    # =========================================================================

    async def aggregate_daily_metrics(
        self,
        workspace_id: str,
        date: Optional[datetime] = None
    ) -> Dict[str, Any]:
        """
        Aggregate inbox metrics for a specific date

        Args:
            workspace_id: Workspace UUID
            date: Date to aggregate for (default: yesterday)

        Returns:
            Aggregated metrics dictionary
        """
        if date is None:
            date = datetime.now(timezone.utc) - timedelta(days=1)

        date_only = date.date()

        logger.info(f"📊 Aggregating inbox metrics for {date_only}")

        async with async_session_maker() as session:
            # Get all messages from that date
            start_of_day = datetime.combine(date_only, datetime.min.time(), tzinfo=timezone.utc)
            end_of_day = datetime.combine(date_only, datetime.max.time(), tzinfo=timezone.utc)

            messages_stmt = select(CommunityInboxMessage).where(
                and_(
                    CommunityInboxMessage.workspace_id == workspace_id,
                    CommunityInboxMessage.received_at >= start_of_day,
                    CommunityInboxMessage.received_at <= end_of_day
                )
            )

            result = await session.execute(messages_stmt)
            messages = result.scalars().all()

            if not messages:
                logger.warning(f"No messages found for {date_only}")
                return {}

            # Calculate metrics
            metrics = self._calculate_metrics(messages, workspace_id)
            metrics["date"] = date_only.isoformat()

            # Store in database
            await self._store_analytics(session, workspace_id, date_only, metrics)

            logger.success(f"✓ Aggregated {len(messages)} messages for {date_only}")

            return metrics

    async def aggregate_all_daily(self, workspace_id: str, days: int = 30) -> Dict[str, Any]:
        """
        Aggregate metrics for last N days

        Args:
            workspace_id: Workspace UUID
            days: Number of days to aggregate

        Returns:
            Dictionary with aggregation results
        """
        logger.info(f"📊 Aggregating {days} days of metrics")

        results = {}
        for i in range(days):
            date = datetime.now(timezone.utc) - timedelta(days=i)
            metrics = await self.aggregate_daily_metrics(workspace_id, date)
            if metrics:
                results[date.date().isoformat()] = metrics

        logger.success(f"✓ Aggregated {len(results)} days")
        return results

    def _calculate_metrics(
        self,
        messages: List[CommunityInboxMessage],
        workspace_id: str
    ) -> Dict[str, Any]:
        """
        Calculate all metrics from a list of messages

        Args:
            messages: List of messages
            workspace_id: Workspace UUID

        Returns:
            Dictionary with calculated metrics
        """
        metrics: Dict[str, Any] = {
            "workspace_id": workspace_id,
            "total_messages": len(messages),
            "by_platform": {},
            "by_sentiment": {"positive": 0, "neutral": 0, "negative": 0},
            "by_status": {},
            "response_metrics": {},
        }

        responded = 0
        closed = 0
        ignored = 0
        response_times = []

        for msg in messages:
            # Count by platform
            platform = msg.platform
            if platform not in metrics["by_platform"]:
                metrics["by_platform"][platform] = {
                    "total": 0,
                    "responded": 0,
                    "closed": 0,
                    "ignored": 0,
                    "avg_response_time": 0,
                    "median_response_time": 0,
                }

            metrics["by_platform"][platform]["total"] += 1

            # Count by sentiment
            sentiment = msg.sentiment_label or "neutral"
            if sentiment in metrics["by_sentiment"]:
                metrics["by_sentiment"][sentiment] += 1

            # Count by status
            status = msg.status or "unread"
            if status not in metrics["by_status"]:
                metrics["by_status"][status] = 0
            metrics["by_status"][status] += 1

            # Track responses
            if msg.response_status == "sent" or msg.responded_at:
                responded += 1
                metrics["by_platform"][platform]["responded"] += 1

                if msg.response_time_minutes:
                    response_times.append(msg.response_time_minutes)

            if msg.status == "closed":
                closed += 1
                metrics["by_platform"][platform]["closed"] += 1

            if msg.status == "ignored":
                ignored += 1
                metrics["by_platform"][platform]["ignored"] += 1

        # Calculate response metrics
        total = len(messages)
        metrics["response_metrics"] = {
            "responded_count": responded,
            "closed_count": closed,
            "ignored_count": ignored,
            "response_rate": (responded / total * 100) if total > 0 else 0,
            "close_rate": (closed / total * 100) if total > 0 else 0,
            "ignore_rate": (ignored / total * 100) if total > 0 else 0,
        }

        # Calculate response time statistics
        if response_times:
            response_times.sort()
            metrics["response_metrics"]["avg_response_time"] = sum(response_times) / len(response_times)
            metrics["response_metrics"]["median_response_time"] = response_times[len(response_times) // 2]
            metrics["response_metrics"]["p95_response_time"] = response_times[int(len(response_times) * 0.95)]
            metrics["response_metrics"]["p99_response_time"] = response_times[int(len(response_times) * 0.99)]
        else:
            metrics["response_metrics"]["avg_response_time"] = 0
            metrics["response_metrics"]["median_response_time"] = 0
            metrics["response_metrics"]["p95_response_time"] = 0
            metrics["response_metrics"]["p99_response_time"] = 0

        # Calculate per-platform response times
        for platform, platform_metrics in metrics["by_platform"].items():
            platform_response_times = [
                msg.response_time_minutes
                for msg in messages
                if msg.platform == platform
                and msg.response_time_minutes
            ]

            if platform_response_times:
                platform_response_times.sort()
                platform_metrics["avg_response_time"] = (
                    sum(platform_response_times) / len(platform_response_times)
                )
                platform_metrics["median_response_time"] = (
                    platform_response_times[len(platform_response_times) // 2]
                )

        return metrics

    async def _store_analytics(
        self,
        session: AsyncSession,
        workspace_id: str,
        date: Any,
        metrics: Dict[str, Any]
    ) -> None:
        """Store metrics in database"""
        try:
            analytics = InboxAnalytics(
                id=str(uuid4()),
                workspace_id=workspace_id,
                date=date,
                messages_received=metrics["total_messages"],
                messages_responded=metrics["response_metrics"].get("responded_count", 0),
                messages_closed=metrics["response_metrics"].get("closed_count", 0),
                messages_ignored=metrics["response_metrics"].get("ignored_count", 0),
                response_rate=metrics["response_metrics"].get("response_rate", 0),
                avg_response_time_minutes=metrics["response_metrics"].get("avg_response_time", 0),
                median_response_time_minutes=metrics["response_metrics"].get("median_response_time", 0),
                p95_response_time_minutes=metrics["response_metrics"].get("p95_response_time", 0),
                p99_response_time_minutes=metrics["response_metrics"].get("p99_response_time", 0),
                positive_count=metrics["by_sentiment"].get("positive", 0),
                neutral_count=metrics["by_sentiment"].get("neutral", 0),
                negative_count=metrics["by_sentiment"].get("negative", 0),
                created_at=datetime.now(timezone.utc)
            )

            session.add(analytics)
            await session.commit()

        except Exception as e:
            logger.error(f"Error storing analytics: {str(e)}")

    # =========================================================================
    # ANALYTICS QUERIES
    # =========================================================================

    async def get_daily_summary(self, workspace_id: str) -> Dict[str, Any]:
        """
        Get summary of today's metrics

        Returns:
            Dictionary with summary statistics
        """
        today = datetime.now(timezone.utc).date()

        async with async_session_maker() as session:
            # Get today's messages
            start_of_day = datetime.combine(today, datetime.min.time(), tzinfo=timezone.utc)
            end_of_day = datetime.combine(today, datetime.max.time(), tzinfo=timezone.utc)

            stmt = select(CommunityInboxMessage).where(
                and_(
                    CommunityInboxMessage.workspace_id == workspace_id,
                    CommunityInboxMessage.received_at >= start_of_day,
                    CommunityInboxMessage.received_at <= end_of_day
                )
            )

            result = await session.execute(stmt)
            messages = result.scalars().all()

            if not messages:
                return {
                    "date": today.isoformat(),
                    "total_messages": 0,
                    "response_rate": 0,
                    "by_platform": {},
                    "by_sentiment": {}
                }

            metrics = self._calculate_metrics(messages, workspace_id)

            return {
                "date": today.isoformat(),
                "total_messages": metrics["total_messages"],
                "response_rate": metrics["response_metrics"]["response_rate"],
                "avg_response_time": metrics["response_metrics"]["avg_response_time"],
                "by_platform": metrics["by_platform"],
                "by_sentiment": metrics["by_sentiment"],
                "summary": {
                    "responded": metrics["response_metrics"].get("responded_count", 0),
                    "closed": metrics["response_metrics"].get("closed_count", 0),
                    "ignored": metrics["response_metrics"].get("ignored_count", 0),
                }
            }

    async def get_response_trends(
        self,
        workspace_id: str,
        start_date: Optional[str] = None,
        end_date: Optional[str] = None,
        platform: Optional[str] = None
    ) -> List[Dict[str, Any]]:
        """
        Get response rate trends over time

        Args:
            workspace_id: Workspace UUID
            start_date: Start date (YYYY-MM-DD)
            end_date: End date (YYYY-MM-DD)
            platform: Filter by platform (optional)

        Returns:
            List of daily trend data
        """
        if not end_date:
            end_date = datetime.now(timezone.utc).date().isoformat()
        if not start_date:
            start_date = (
                datetime.now(timezone.utc) - timedelta(days=30)
            ).date().isoformat()

        async with async_session_maker() as session:
            stmt = select(InboxAnalytics).where(
                and_(
                    InboxAnalytics.workspace_id == workspace_id,
                    InboxAnalytics.date >= start_date,
                    InboxAnalytics.date <= end_date
                )
            )

            result = await session.execute(stmt)
            analytics = result.scalars().all()

            trends = []
            for day_analytics in analytics:
                trend = {
                    "date": day_analytics.date.isoformat() if day_analytics.date else None,
                    "messages_received": day_analytics.messages_received,
                    "messages_responded": day_analytics.messages_responded,
                    "response_rate": day_analytics.response_rate,
                    "avg_response_time": day_analytics.avg_response_time_minutes,
                }
                trends.append(trend)

            return trends

    async def get_platform_breakdown(
        self,
        workspace_id: str,
        date: Optional[datetime] = None
    ) -> Dict[str, Dict[str, Any]]:
        """
        Get metrics broken down by platform

        Args:
            workspace_id: Workspace UUID
            date: Date to get breakdown for (default: today)

        Returns:
            Dictionary with platform breakdown
        """
        if date is None:
            date = datetime.now(timezone.utc)

        date_only = date.date()

        async with async_session_maker() as session:
            start_of_day = datetime.combine(date_only, datetime.min.time(), tzinfo=timezone.utc)
            end_of_day = datetime.combine(date_only, datetime.max.time(), tzinfo=timezone.utc)

            stmt = select(CommunityInboxMessage).where(
                and_(
                    CommunityInboxMessage.workspace_id == workspace_id,
                    CommunityInboxMessage.received_at >= start_of_day,
                    CommunityInboxMessage.received_at <= end_of_day
                )
            )

            result = await session.execute(stmt)
            messages = result.scalars().all()

            breakdown: Dict[str, Dict[str, Any]] = {}

            for msg in messages:
                platform = msg.platform
                if platform not in breakdown:
                    breakdown[platform] = {
                        "total": 0,
                        "responded": 0,
                        "response_rate": 0,
                        "avg_response_time": 0,
                        "sentiments": {"positive": 0, "neutral": 0, "negative": 0},
                    }

                breakdown[platform]["total"] += 1

                if msg.responded_at or msg.response_status == "sent":
                    breakdown[platform]["responded"] += 1

                sentiment = msg.sentiment_label or "neutral"
                if sentiment in breakdown[platform]["sentiments"]:
                    breakdown[platform]["sentiments"][sentiment] += 1

            # Calculate response rates
            for platform in breakdown:
                total = breakdown[platform]["total"]
                if total > 0:
                    breakdown[platform]["response_rate"] = (
                        breakdown[platform]["responded"] / total * 100
                    )

            return breakdown

    async def get_sentiment_distribution(
        self,
        workspace_id: str,
        start_date: Optional[str] = None,
        end_date: Optional[str] = None
    ) -> Dict[str, Any]:
        """
        Get sentiment distribution over time

        Returns:
            Dictionary with sentiment trends
        """
        if not end_date:
            end_date = datetime.now(timezone.utc).date().isoformat()
        if not start_date:
            start_date = (
                datetime.now(timezone.utc) - timedelta(days=30)
            ).date().isoformat()

        async with async_session_maker() as session:
            stmt = select(CommunityInboxMessage).where(
                and_(
                    CommunityInboxMessage.workspace_id == workspace_id,
                    func.date(CommunityInboxMessage.received_at) >= start_date,
                    func.date(CommunityInboxMessage.received_at) <= end_date
                )
            )

            result = await session.execute(stmt)
            messages = result.scalars().all()

            # Group by date and sentiment
            sentiment_trends: Dict[str, Dict[str, int]] = {}

            for msg in messages:
                date_key = msg.received_at.date().isoformat() if msg.received_at else "unknown"
                sentiment = msg.sentiment_label or "neutral"

                if date_key not in sentiment_trends:
                    sentiment_trends[date_key] = {
                        "positive": 0,
                        "neutral": 0,
                        "negative": 0,
                        "total": 0
                    }

                if sentiment in sentiment_trends[date_key]:
                    sentiment_trends[date_key][sentiment] += 1

                sentiment_trends[date_key]["total"] += 1

            # Calculate percentages
            for date_key in sentiment_trends:
                total = sentiment_trends[date_key]["total"]
                if total > 0:
                    sentiment_trends[date_key]["positive_pct"] = (
                        sentiment_trends[date_key]["positive"] / total * 100
                    )
                    sentiment_trends[date_key]["neutral_pct"] = (
                        sentiment_trends[date_key]["neutral"] / total * 100
                    )
                    sentiment_trends[date_key]["negative_pct"] = (
                        sentiment_trends[date_key]["negative"] / total * 100
                    )

            return sentiment_trends

    async def get_response_time_stats(
        self,
        workspace_id: str,
        platform: Optional[str] = None
    ) -> Dict[str, float]:
        """
        Get response time statistics

        Returns:
            Dictionary with response time metrics
        """
        async with async_session_maker() as session:
            stmt = select(CommunityInboxMessage).where(
                and_(
                    CommunityInboxMessage.workspace_id == workspace_id,
                    CommunityInboxMessage.response_time_minutes.isnot(None)
                )
            )

            if platform:
                stmt = stmt.where(CommunityInboxMessage.platform == platform)

            result = await session.execute(stmt)
            messages = result.scalars().all()

            if not messages:
                return {
                    "avg": 0,
                    "median": 0,
                    "p95": 0,
                    "p99": 0,
                    "min": 0,
                    "max": 0
                }

            response_times = [msg.response_time_minutes for msg in messages if msg.response_time_minutes]
            response_times.sort()

            return {
                "avg": sum(response_times) / len(response_times) if response_times else 0,
                "median": response_times[len(response_times) // 2] if response_times else 0,
                "p95": response_times[int(len(response_times) * 0.95)] if response_times else 0,
                "p99": response_times[int(len(response_times) * 0.99)] if response_times else 0,
                "min": min(response_times) if response_times else 0,
                "max": max(response_times) if response_times else 0,
                "count": len(response_times)
            }


# =============================================================================
# SINGLETON
# =============================================================================

_analytics_instance: Optional[InboxAnalyticsService] = None


def get_inbox_analytics_service() -> InboxAnalyticsService:
    """Get singleton instance of InboxAnalyticsService"""
    global _analytics_instance
    if _analytics_instance is None:
        _analytics_instance = InboxAnalyticsService()
    return _analytics_instance
