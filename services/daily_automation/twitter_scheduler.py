"""
Twitter Offer Scheduler - Post offers every 2 hours using Safari automation
"""
import asyncio
from datetime import datetime, timezone
from typing import Dict, Optional, List
from loguru import logger

from services.event_bus import EventBus


class TwitterOfferScheduler:
    """
    Posts about offers every 2 hours (12 posts/day) using Safari automation.
    
    Strategy:
    - Generate AI-powered tweets about offers
    - Use SafariTwitterPoster for reliable posting
    - Rotate through different awareness stages
    - Track success/failure for optimization
    """
    
    POST_INTERVAL_HOURS = 2
    POSTS_PER_DAY = 12
    
    def __init__(self, event_bus: Optional[EventBus] = None):
        self.event_bus = event_bus or EventBus.get_instance()
        self.running = False
        self.posts_today = 0
        self.last_post_at: Optional[datetime] = None
        self._task: Optional[asyncio.Task] = None
        self._poster = None  # Lazy-loaded SafariTwitterPoster
        
    def _get_poster(self):
        """Lazy load SafariTwitterPoster to avoid import issues."""
        if self._poster is None:
            try:
                from automation.safari_twitter_poster import SafariTwitterPoster
                self._poster = SafariTwitterPoster(use_x_domain=True)
            except ImportError as e:
                logger.error(f"Failed to import SafariTwitterPoster: {e}")
        return self._poster
        
    async def start(self):
        """Start the scheduler."""
        if self.running:
            return
        self.running = True
        logger.info("🐦 Starting Twitter Offer Scheduler (every 2 hours)")
        self._task = asyncio.create_task(self._posting_loop())
        
    async def stop(self):
        """Stop the scheduler."""
        self.running = False
        if self._task:
            self._task.cancel()
        logger.info("🛑 Twitter Offer Scheduler stopped")
        
    async def _posting_loop(self):
        """Main posting loop - runs every 2 hours."""
        while self.running:
            try:
                await self._post_offer()
                # Sleep for 2 hours
                await asyncio.sleep(self.POST_INTERVAL_HOURS * 3600)
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Twitter posting error: {e}")
                await asyncio.sleep(300)  # Retry in 5 min on error
                
    async def _post_offer(self):
        """Generate and post an offer tweet."""
        from services.twitter_campaign_service import TwitterCampaignService
        
        logger.info("📝 Generating offer tweet...")
        
        try:
            service = TwitterCampaignService()
            
            # Get current offer/product to promote
            offer = self._get_current_offer()
            
            # Generate tweet using AI
            tweet_text = await self._generate_offer_tweet(service, offer)
            
            # Post via Safari automation
            success = await self._post_to_twitter(tweet_text)
            
            if success:
                self.posts_today += 1
                self.last_post_at = datetime.now(timezone.utc)
                logger.info(f"✅ Posted offer tweet ({self.posts_today}/12 today)")
                
                await self.event_bus.publish(
                    "daily.twitter.post_completed",
                    {"tweet": tweet_text[:100], "posts_today": self.posts_today},
                    source="TwitterOfferScheduler"
                )
            else:
                logger.error("❌ Failed to post tweet")
                
        except Exception as e:
            logger.error(f"Offer posting failed: {e}")
            
    def _get_current_offer(self) -> Dict:
        """Get current offer to promote."""
        # Rotate through offers
        offers = [
            {
                "name": "Blotato",
                "url": "https://blotato.com",
                "tagline": "Post to all platforms with one click"
            },
            {
                "name": "AI Automation Course",
                "url": "https://example.com/course",
                "tagline": "Learn to automate your content creation"
            }
        ]
        return offers[self.posts_today % len(offers)]
    
    async def _generate_offer_tweet(self, service, offer: Dict) -> str:
        """Generate tweet copy using AI."""
        from services.twitter_campaign_service import AwarenessStage, ContentType, Product
        
        # Create product from offer
        product = Product(
            id=offer["name"].lower().replace(" ", "_"),
            name=offer["name"],
            slug=offer["name"].lower().replace(" ", "-"),
            description=offer["tagline"],
            website_url=offer["url"],
            tagline=offer["tagline"],
            key_features=["automation", "efficiency"],
            target_audience="content creators",
            voice_style="casual, direct"
        )
        
        # Rotate awareness stages
        stages = list(AwarenessStage)
        stage = stages[self.posts_today % len(stages)]
        
        # Rotate content types
        types = list(ContentType)
        content_type = types[self.posts_today % len(types)]
        
        return service.generate_tweet(product, stage, content_type)
    
    async def _post_to_twitter(self, text: str) -> bool:
        """Post tweet via Safari automation using SafariTwitterPoster."""
        poster = self._get_poster()
        
        if poster:
            try:
                # Check login status first
                poster.open_twitter()
                login_status = poster.check_login_status()
                
                if not login_status.get('logged_in'):
                    logger.warning(f"⚠️ Not logged into Twitter: {login_status.get('reason')}")
                    return False
                
                # Open compose and type tweet
                poster.open_compose()
                
                # Type the tweet using JS injection (more reliable)
                if not poster.type_tweet_via_js(text):
                    logger.error("Failed to type tweet")
                    return False
                
                # Click post button
                if not poster.click_post_button_via_js():
                    logger.error("Failed to click post button")
                    return False
                
                # Verify success
                result = poster.verify_post_success(max_wait=10)
                
                if result.get('posted'):
                    logger.success(f"✅ Tweet posted! URL: {result.get('tweet_url')}")
                    return True
                else:
                    logger.error(f"Post verification failed: {result}")
                    return False
                    
            except Exception as e:
                logger.error(f"SafariTwitterPoster error: {e}")
                return await self._post_to_twitter_fallback(text)
        else:
            return await self._post_to_twitter_fallback(text)
    
    async def _post_to_twitter_fallback(self, text: str) -> bool:
        """Fallback posting method using direct AppleScript."""
        import subprocess
        
        # Escape text for AppleScript
        escaped = text.replace('"', '\\"').replace("'", "\\'").replace('\n', '\\n')
        
        script = f'''
        tell application "Safari"
            activate
            if (count of windows) = 0 then
                make new document
            end if
            set URL of current tab of window 1 to "https://x.com/compose/post"
            delay 3
        end tell
        
        tell application "System Events"
            tell process "Safari"
                delay 1
                keystroke "{escaped}"
                delay 0.5
                keystroke return using {{command down}}
                delay 2
            end tell
        end tell
        '''
        
        try:
            result = subprocess.run(['osascript', '-e', script], capture_output=True, timeout=30)
            return result.returncode == 0
        except Exception as e:
            logger.error(f"Fallback Safari posting failed: {e}")
            return False
    
    def get_status(self) -> Dict:
        """Get scheduler status."""
        return {
            "running": self.running,
            "posts_today": self.posts_today,
            "last_post_at": self.last_post_at.isoformat() if self.last_post_at else None,
            "next_post_in_hours": self.POST_INTERVAL_HOURS
        }
