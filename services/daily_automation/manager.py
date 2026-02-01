"""
Daily Automation Manager - Coordinates Sora and Twitter schedulers
"""
import asyncio
from datetime import datetime, timezone
from typing import Dict, Optional
from loguru import logger

from services.event_bus import EventBus
from .sora_scheduler import SoraScheduler
from .twitter_scheduler import TwitterOfferScheduler


class DailyAutomationManager:
    """Singleton manager for daily automation tasks."""
    
    _instance: Optional["DailyAutomationManager"] = None
    
    def __init__(self, event_bus: Optional[EventBus] = None):
        self.event_bus = event_bus or EventBus.get_instance()
        self.sora_scheduler = SoraScheduler(self.event_bus)
        self.twitter_scheduler = TwitterOfferScheduler(self.event_bus)
        self.initialized = False
        self.started_at: Optional[datetime] = None
        
    @classmethod
    def get_instance(cls, event_bus: Optional[EventBus] = None) -> "DailyAutomationManager":
        """Get singleton instance."""
        if cls._instance is None:
            cls._instance = cls(event_bus)
        return cls._instance
    
    @classmethod
    def reset_instance(cls):
        """Reset singleton (for testing)."""
        cls._instance = None
        
    async def initialize(self):
        """Initialize and start all schedulers on backend startup."""
        if self.initialized:
            logger.warning("DailyAutomationManager already initialized")
            return
            
        logger.info("🚀 Initializing Daily Automation Manager")
        self.started_at = datetime.now(timezone.utc)
        
        try:
            # Start Sora scheduler (checks credits first)
            await self.sora_scheduler.start()
            
            # Start Twitter offer scheduler
            await self.twitter_scheduler.start()
            
            self.initialized = True
            
            await self.event_bus.publish(
                "daily.automation.started",
                {
                    "sora_credits": self.sora_scheduler.credits.remaining,
                    "twitter_interval_hours": self.twitter_scheduler.POST_INTERVAL_HOURS
                },
                source="DailyAutomationManager"
            )
            
            logger.info("✅ Daily Automation Manager initialized")
            
        except Exception as e:
            logger.error(f"❌ Failed to initialize automation: {e}")
            # Don't crash the server, just log the error
            
    async def shutdown(self):
        """Stop all schedulers."""
        logger.info("🛑 Shutting down Daily Automation Manager")
        await self.sora_scheduler.stop()
        await self.twitter_scheduler.stop()
        self.initialized = False
        
    async def manual_start(self):
        """Manually trigger start (for API endpoint)."""
        if not self.initialized:
            await self.initialize()
        else:
            # Re-check credits and restart if needed
            await self.sora_scheduler.check_credits()
            
    def get_status(self) -> Dict:
        """Get combined status of all schedulers."""
        return {
            "initialized": self.initialized,
            "started_at": self.started_at.isoformat() if self.started_at else None,
            "sora": self.sora_scheduler.get_status(),
            "twitter": self.twitter_scheduler.get_status()
        }
