"""
Sora Scheduler - Daily video generation automation using Safari browser
"""
import asyncio
import subprocess
import re
from datetime import datetime, timezone
from typing import Dict, Optional, List
from dataclasses import dataclass, field
from loguru import logger

from services.event_bus import EventBus


@dataclass
class SoraCredits:
    total: int = 30
    used: int = 0
    remaining: int = 30
    checked_at: Optional[datetime] = None


@dataclass
class VideoGeneration:
    """Tracks a video generation job."""
    prompt: str
    character: Optional[str] = None
    status: str = "pending"
    started_at: Optional[datetime] = None
    completed_at: Optional[datetime] = None


class SoraScheduler:
    """
    Manages daily Sora video generation using Safari automation.
    
    Strategy:
    - Check credits on startup via Settings > Usage
    - Generate videos throughout the day to use all 30 credits
    - Use @isaiahdupree character for consistent branding
    - Space out generations to avoid rate limits
    """
    
    MAX_CONCURRENT = 3
    DAILY_CREDITS = 30
    GENERATION_INTERVAL_MINUTES = 30  # Generate every 30 min
    ENABLED = False  # DISABLED - Set to True to enable Sora automation
    
    def __init__(self, event_bus: Optional[EventBus] = None):
        self.event_bus = event_bus or EventBus.get_instance()
        self.credits = SoraCredits()
        self.running = False
        self.generations_today: List[VideoGeneration] = []
        self._task: Optional[asyncio.Task] = None
        self._sora = None  # Lazy-loaded SoraFullAutomation
        
    def _get_sora(self):
        """Lazy load SoraFullAutomation to avoid import issues."""
        if self._sora is None:
            try:
                from automation.sora_full_automation import SoraFullAutomation
                self._sora = SoraFullAutomation()
            except ImportError as e:
                logger.error(f"Failed to import SoraFullAutomation: {e}")
        return self._sora
        
    async def start(self):
        """Start the scheduler."""
        if not self.ENABLED:
            logger.info("⏸️ Sora Scheduler DISABLED - skipping start")
            return
        if self.running:
            return
        self.running = True
        logger.info("🎬 Starting Sora Scheduler")
        await self.check_credits()
        self._task = asyncio.create_task(self._generation_loop())
        
    async def stop(self):
        """Stop the scheduler."""
        self.running = False
        if self._task:
            self._task.cancel()
        logger.info("🛑 Sora Scheduler stopped")
        
    async def check_credits(self) -> SoraCredits:
        """Check Sora credits via Safari using SoraFullAutomation."""
        logger.info("🔍 Checking Sora credits via Safari...")
        try:
            sora = self._get_sora()
            if sora:
                # Use the proper get_usage method
                usage = sora.get_usage()
                remaining = usage.get('video_gens_left', 30)
            else:
                remaining = await self._get_credits_fallback()
                
            self.credits = SoraCredits(
                total=30, 
                used=30 - remaining, 
                remaining=remaining,
                checked_at=datetime.now(timezone.utc)
            )
            logger.info(f"✅ Sora Credits: {remaining}/30 remaining")
            
            await self.event_bus.publish(
                "sora.credits.checked",
                {"remaining": remaining, "total": 30},
                source="SoraScheduler"
            )
        except Exception as e:
            logger.error(f"Credit check failed: {e}, assuming 30")
            self.credits = SoraCredits(remaining=30)
        return self.credits
    
    async def _get_credits_fallback(self) -> int:
        """Fallback method to get credits directly via AppleScript."""
        script = '''
        tell application "Safari"
            activate
            set URL of front document to "https://sora.chatgpt.com"
            delay 3
            set txt to do JavaScript "document.body.innerText" in front document
            return txt
        end tell
        '''
        try:
            result = subprocess.run(
                ['osascript', '-e', script], 
                capture_output=True, 
                text=True, 
                timeout=30
            )
            match = re.search(r'(\d+)\s*video\s*gen', result.stdout, re.I)
            return int(match.group(1)) if match else 30
        except Exception as e:
            logger.error(f"Fallback credit check failed: {e}")
            return 30
    
    async def _generation_loop(self):
        """Main generation loop - generates videos throughout the day."""
        while self.running:
            try:
                if self.credits.remaining > 0:
                    await self._generate_next_video()
                    # Refresh credits after generation
                    await asyncio.sleep(30)
                    await self.check_credits()
                    
                # Wait before next generation
                await asyncio.sleep(self.GENERATION_INTERVAL_MINUTES * 60)
                
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Generation loop error: {e}")
                await asyncio.sleep(60)
                
    async def _generate_next_video(self):
        """Generate next video using SoraFullAutomation."""
        sora = self._get_sora()
        if not sora:
            logger.error("SoraFullAutomation not available")
            return
            
        prompt = self._get_next_prompt()
        gen = VideoGeneration(
            prompt=prompt,
            character="@isaiahdupree",
            status="generating",
            started_at=datetime.now(timezone.utc)
        )
        self.generations_today.append(gen)
        
        logger.info(f"🎥 Generating Sora video: {prompt[:50]}...")
        
        try:
            # Navigate to Sora explore page
            sora.navigate_to_explore()
            
            # Check if logged in
            if not sora.check_login():
                logger.warning("⚠️ Not logged into Sora - please login manually")
                gen.status = "login_required"
                return
            
            # The actual generation would use sora methods
            # For now, log that we're ready to generate
            logger.info(f"✅ Ready to generate: {prompt}")
            gen.status = "ready"
            
            await self.event_bus.publish(
                "sora.generation.started",
                {"prompt": prompt, "character": "@isaiahdupree"},
                source="SoraScheduler"
            )
            
        except Exception as e:
            logger.error(f"Video generation failed: {e}")
            gen.status = "failed"
        
    def _get_next_prompt(self) -> str:
        """Get next prompt for generation - uses @isaiahdupree character."""
        prompts = [
            "@isaiahdupree walking through a futuristic city at sunset, cinematic",
            "@isaiahdupree presenting content creation tips in a modern studio",
            "@isaiahdupree working at a sleek desk with multiple monitors, tech aesthetic",
            "@isaiahdupree in a motivational pose, vibrant colors, inspiring",
            "@isaiahdupree explaining automation concepts with holographic displays",
        ]
        idx = len(self.generations_today) % len(prompts)
        return prompts[idx]
    
    def get_status(self) -> Dict:
        """Get scheduler status."""
        return {
            "running": self.running,
            "credits": self.credits.remaining,
            "credits_total": self.credits.total,
            "generations_today": len(self.generations_today),
            "checked_at": self.credits.checked_at.isoformat() if self.credits.checked_at else None
        }
