"""
Engagement Controller
=====================
Controls automated engagement with start/stop, rate limiting, and idle detection.

Features:
- 30 comments per platform per hour
- Start/Stop control via API
- Auto-resume after 2-3 hours of Mac idle time
- Real-time status updates
- **Database persistence for state survival across uvicorn reloads**

Usage:
    controller = EngagementController.get_instance()
    await controller.start()
    await controller.stop()
"""
import asyncio
import time
import random
import subprocess
import os
from datetime import datetime, timezone, timedelta
from typing import Dict, Optional, List, Any
from dataclasses import dataclass, field
from enum import Enum
from loguru import logger

# Supabase for state persistence
try:
    from supabase import create_client, Client
    SUPABASE_URL = os.environ.get('SUPABASE_URL', 'http://127.0.0.1:54321')
    SUPABASE_KEY = os.environ.get('SUPABASE_ANON_KEY', 'eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJpc3MiOiJzdXBhYmFzZS1kZW1vIiwicm9sZSI6ImFub24iLCJleHAiOjE5ODM4MTI5OTZ9.CRXP1A7WOeoJeXxjNni43kdQwgnWNReilDMblYTn_I0')
    HAS_SUPABASE = True
except ImportError:
    HAS_SUPABASE = False
    logger.warning("Supabase not available - state persistence disabled")


class EngagementState(Enum):
    """Engagement automation states."""
    STOPPED = "stopped"
    RUNNING = "running"
    PAUSED = "paused"
    IDLE_WAITING = "idle_waiting"  # Waiting for idle period to resume


@dataclass
class PlatformStats:
    """Stats for a single platform."""
    platform: str
    comments_this_hour: int = 0
    comments_today: int = 0
    last_comment_at: Optional[datetime] = None
    errors: List[str] = field(default_factory=list)
    is_enabled: bool = True


@dataclass
class EngagementStatus:
    """Current engagement status."""
    state: EngagementState
    started_at: Optional[datetime]
    stopped_at: Optional[datetime]
    total_comments_today: int
    platforms: Dict[str, PlatformStats]
    next_comment_at: Optional[datetime]
    idle_minutes: float
    auto_resume_enabled: bool
    auto_resume_after_hours: float
    comments_per_hour_limit: int
    
    def to_dict(self) -> dict:
        return {
            "state": self.state.value,
            "started_at": self.started_at.isoformat() if self.started_at else None,
            "stopped_at": self.stopped_at.isoformat() if self.stopped_at else None,
            "total_comments_today": self.total_comments_today,
            "platforms": {
                name: {
                    "comments_this_hour": p.comments_this_hour,
                    "comments_today": p.comments_today,
                    "last_comment_at": p.last_comment_at.isoformat() if p.last_comment_at else None,
                    "is_enabled": p.is_enabled,
                    "errors_count": len(p.errors),
                }
                for name, p in self.platforms.items()
            },
            "next_comment_at": self.next_comment_at.isoformat() if self.next_comment_at else None,
            "idle_minutes": round(self.idle_minutes, 1),
            "auto_resume_enabled": self.auto_resume_enabled,
            "auto_resume_after_hours": self.auto_resume_after_hours,
            "comments_per_hour_limit": self.comments_per_hour_limit,
        }


class EngagementController:
    """
    Controls engagement automation with rate limiting and idle detection.
    
    Rate: 30 comments per platform per hour
    Auto-resume: After 2-3 hours of Mac idle time
    """
    
    _instance: Optional["EngagementController"] = None
    
    # Configuration
    COMMENTS_PER_HOUR_PER_PLATFORM = 30
    MIN_DELAY_BETWEEN_COMMENTS = 60  # 1 minute minimum
    MAX_DELAY_BETWEEN_COMMENTS = 180  # 3 minutes maximum
    AUTO_RESUME_IDLE_HOURS = 0.25  # Resume after 15 minutes idle
    IDLE_CHECK_INTERVAL = 60  # Check idle every 1 minute
    
    PLATFORMS = ['threads', 'instagram', 'tiktok', 'twitter']
    
    def __init__(self):
        self.state = EngagementState.STOPPED
        self.started_at: Optional[datetime] = None
        self.stopped_at: Optional[datetime] = None
        
        # Platform stats
        self.platform_stats: Dict[str, PlatformStats] = {
            p: PlatformStats(platform=p) for p in self.PLATFORMS
        }
        
        # Tasks
        self._engagement_task: Optional[asyncio.Task] = None
        self._idle_monitor_task: Optional[asyncio.Task] = None
        
        # Control
        self._should_stop = False
        self._next_comment_at: Optional[datetime] = None
        
        # Auto-resume settings
        self.auto_resume_enabled = True
        self.auto_resume_after_hours = self.AUTO_RESUME_IDLE_HOURS
        
        # Event callbacks
        self._on_comment_posted: List[callable] = []
        self._on_state_changed: List[callable] = []
        
        # Supabase client for persistence
        self._supabase: Optional[Any] = None
        
        # Load persisted state on init
        self._load_persisted_state()
    
    def _get_supabase(self):
        """Get or create Supabase client."""
        if self._supabase is None and HAS_SUPABASE:
            try:
                self._supabase = create_client(SUPABASE_URL, SUPABASE_KEY)
            except Exception as e:
                logger.warning(f"Failed to create Supabase client: {e}")
        return self._supabase
    
    def _load_persisted_state(self):
        """Load state from database on startup (survives uvicorn reload)."""
        try:
            supabase = self._get_supabase()
            if not supabase:
                return
            
            result = supabase.table('engagement_state').select('*').eq('id', 'singleton').execute()
            
            if result.data and len(result.data) > 0:
                data = result.data[0]
                persisted_state = data.get('state', 'stopped')
                started_at_str = data.get('started_at')
                
                # Only restore RUNNING state if it was running
                if persisted_state == 'running' and started_at_str:
                    self.state = EngagementState.RUNNING
                    self.started_at = datetime.fromisoformat(started_at_str.replace('Z', '+00:00'))
                    self._should_stop = False
                    
                    # Restore platform enabled states
                    platforms_data = data.get('platforms', {})
                    for p, stats in platforms_data.items():
                        if p in self.platform_stats:
                            self.platform_stats[p].is_enabled = stats.get('is_enabled', True)
                    
                    logger.info(f"🔄 Restored RUNNING state from DB (started at {self.started_at})")
                    
                    # Auto-restart the engagement loop
                    asyncio.create_task(self._restart_after_reload())
                else:
                    logger.debug("Persisted state was stopped, starting fresh")
                    
        except Exception as e:
            logger.warning(f"Failed to load persisted state: {e}")
    
    async def _restart_after_reload(self):
        """Restart engagement loop after uvicorn reload."""
        await asyncio.sleep(1)  # Brief delay to let startup complete
        
        if self.state == EngagementState.RUNNING and not self._engagement_task:
            logger.info("🔄 Restarting engagement loop after reload...")
            self._engagement_task = asyncio.create_task(self._engagement_loop())
            
            if self.auto_resume_enabled and not self._idle_monitor_task:
                self._idle_monitor_task = asyncio.create_task(self._idle_monitor_loop())
    
    def _save_state(self):
        """Persist current state to database."""
        try:
            supabase = self._get_supabase()
            if not supabase:
                return
            
            # Build platforms data
            platforms_data = {}
            for p, stats in self.platform_stats.items():
                platforms_data[p] = {
                    'is_enabled': stats.is_enabled,
                    'comments_this_hour': stats.comments_this_hour,
                    'comments_today': stats.comments_today
                }
            
            data = {
                'id': 'singleton',
                'state': self.state.value,
                'started_at': self.started_at.isoformat() if self.started_at else None,
                'stopped_at': self.stopped_at.isoformat() if self.stopped_at else None,
                'platforms': platforms_data,
                'auto_resume_enabled': self.auto_resume_enabled,
                'updated_at': datetime.now(timezone.utc).isoformat()
            }
            
            supabase.table('engagement_state').upsert(data).execute()
            logger.debug(f"Saved state to DB: {self.state.value}")
            
        except Exception as e:
            logger.warning(f"Failed to save state: {e}")
    
    @classmethod
    def get_instance(cls) -> "EngagementController":
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance
    
    @classmethod
    def reset_instance(cls):
        """Reset singleton (for testing)."""
        if cls._instance:
            asyncio.create_task(cls._instance.stop())
        cls._instance = None
    
    # =========================================================================
    # MAC IDLE DETECTION
    # =========================================================================
    
    def get_mac_idle_time(self) -> float:
        """
        Get Mac idle time in seconds using ioreg.
        
        Returns:
            Idle time in seconds, or 0 if detection fails
        """
        try:
            result = subprocess.run(
                ['ioreg', '-c', 'IOHIDSystem'],
                capture_output=True,
                text=True,
                timeout=5
            )
            
            for line in result.stdout.split('\n'):
                if 'HIDIdleTime' in line:
                    # Extract the number (in nanoseconds)
                    parts = line.split('=')
                    if len(parts) >= 2:
                        idle_ns = int(parts[-1].strip())
                        return idle_ns / 1_000_000_000  # Convert to seconds
            
            return 0
        except Exception as e:
            logger.warning(f"Failed to get idle time: {e}")
            return 0
    
    def get_idle_minutes(self) -> float:
        """Get idle time in minutes."""
        return self.get_mac_idle_time() / 60
    
    def get_idle_hours(self) -> float:
        """Get idle time in hours."""
        return self.get_mac_idle_time() / 3600
    
    # =========================================================================
    # RATE LIMITING
    # =========================================================================
    
    def _reset_hourly_counts(self):
        """Reset hourly comment counts (called at top of each hour)."""
        for stats in self.platform_stats.values():
            stats.comments_this_hour = 0
    
    def _can_post_on_platform(self, platform: str) -> tuple[bool, str]:
        """
        Check if we can post on a platform.
        
        Returns:
            (can_post, reason)
        """
        stats = self.platform_stats.get(platform)
        if not stats:
            return False, f"Unknown platform: {platform}"
        
        if not stats.is_enabled:
            return False, "Platform is disabled"
        
        if stats.comments_this_hour >= self.COMMENTS_PER_HOUR_PER_PLATFORM:
            return False, f"Hourly limit reached ({self.COMMENTS_PER_HOUR_PER_PLATFORM}/hr)"
        
        return True, "OK"
    
    def _get_next_delay(self) -> int:
        """Get random delay before next comment."""
        return random.randint(
            self.MIN_DELAY_BETWEEN_COMMENTS,
            self.MAX_DELAY_BETWEEN_COMMENTS
        )
    
    # =========================================================================
    # CONTROL METHODS
    # =========================================================================
    
    async def start(self) -> dict:
        """
        Start engagement automation.
        
        Returns:
            Status dict
        """
        if self.state == EngagementState.RUNNING:
            return {"success": False, "error": "Already running"}
        
        logger.info("="*60)
        logger.info("🚀 STARTING ENGAGEMENT AUTOMATION")
        logger.info("="*60)
        logger.info(f"   Time: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
        logger.info(f"   Platforms: {', '.join(self.PLATFORMS)}")
        logger.info(f"   Rate limit: {self.COMMENTS_PER_HOUR_PER_PLATFORM} comments/hr/platform")
        logger.info(f"   Delay range: {self.MIN_DELAY_BETWEEN_COMMENTS}-{self.MAX_DELAY_BETWEEN_COMMENTS}s")
        logger.info(f"   Auto-resume: {self.auto_resume_enabled} (after {self.auto_resume_after_hours}h idle)")
        logger.info("="*60)
        
        self.state = EngagementState.RUNNING
        self.started_at = datetime.now(timezone.utc)
        self.stopped_at = None
        self._should_stop = False
        
        # Start engagement loop
        self._engagement_task = asyncio.create_task(self._engagement_loop())
        
        # Start idle monitor (for auto-resume)
        if self.auto_resume_enabled:
            self._idle_monitor_task = asyncio.create_task(self._idle_monitor_loop())
        
        # Notify state change
        await self._notify_state_changed()
        
        # Persist state to DB (survives uvicorn reload)
        self._save_state()
        
        logger.success("✅ Engagement automation started")
        
        return {
            "success": True,
            "state": self.state.value,
            "started_at": self.started_at.isoformat()
        }
    
    async def stop(self) -> dict:
        """
        Stop engagement automation.
        
        Returns:
            Status dict
        """
        if self.state == EngagementState.STOPPED:
            return {"success": False, "error": "Already stopped"}
        
        logger.info("🛑 Stopping engagement automation...")
        
        self._should_stop = True
        self.state = EngagementState.STOPPED
        self.stopped_at = datetime.now(timezone.utc)
        
        # Cancel tasks
        if self._engagement_task:
            self._engagement_task.cancel()
            try:
                await self._engagement_task
            except asyncio.CancelledError:
                pass
        
        if self._idle_monitor_task:
            self._idle_monitor_task.cancel()
            try:
                await self._idle_monitor_task
            except asyncio.CancelledError:
                pass
        
        # Notify state change
        await self._notify_state_changed()
        
        # Persist state to DB (survives uvicorn reload)
        self._save_state()
        
        logger.success("✅ Engagement automation stopped")
        
        return {
            "success": True,
            "state": self.state.value,
            "stopped_at": self.stopped_at.isoformat()
        }
    
    async def pause(self) -> dict:
        """Pause engagement (can resume later)."""
        if self.state != EngagementState.RUNNING:
            return {"success": False, "error": "Not running"}
        
        self.state = EngagementState.PAUSED
        await self._notify_state_changed()
        
        return {"success": True, "state": self.state.value}
    
    async def resume(self) -> dict:
        """Resume paused engagement."""
        if self.state != EngagementState.PAUSED:
            return {"success": False, "error": "Not paused"}
        
        self.state = EngagementState.RUNNING
        await self._notify_state_changed()
        
        return {"success": True, "state": self.state.value}
    
    def enable_platform(self, platform: str, enabled: bool = True):
        """Enable or disable a platform."""
        if platform in self.platform_stats:
            self.platform_stats[platform].is_enabled = enabled
            self._save_state()  # Persist platform enable state
    
    def set_auto_resume(self, enabled: bool, hours: float = 2.5):
        """Configure auto-resume settings."""
        self.auto_resume_enabled = enabled
        self.auto_resume_after_hours = hours
    
    # =========================================================================
    # STATUS
    # =========================================================================
    
    def get_status(self) -> EngagementStatus:
        """Get current engagement status."""
        total_today = sum(s.comments_today for s in self.platform_stats.values())
        
        return EngagementStatus(
            state=self.state,
            started_at=self.started_at,
            stopped_at=self.stopped_at,
            total_comments_today=total_today,
            platforms=self.platform_stats,
            next_comment_at=self._next_comment_at,
            idle_minutes=self.get_idle_minutes(),
            auto_resume_enabled=self.auto_resume_enabled,
            auto_resume_after_hours=self.auto_resume_after_hours,
            comments_per_hour_limit=self.COMMENTS_PER_HOUR_PER_PLATFORM,
        )
    
    # =========================================================================
    # MAIN LOOPS
    # =========================================================================
    
    async def _engagement_loop(self):
        """Main engagement loop - posts comments with rate limiting."""
        logger.info("="*60)
        logger.info("📢 ENGAGEMENT LOOP STARTED")
        logger.info("="*60)
        
        current_hour = datetime.now().hour
        loop_iteration = 0
        
        while not self._should_stop:
            try:
                loop_iteration += 1
                now = datetime.now()
                
                # Log status every 10 iterations
                if loop_iteration % 10 == 1:
                    self._log_status_summary()
                
                # Check for new hour (reset counts)
                if now.hour != current_hour:
                    self._reset_hourly_counts()
                    current_hour = now.hour
                    logger.info("="*60)
                    logger.info(f"⏰ NEW HOUR: {current_hour}:00 - Hourly counts reset")
                    logger.info("="*60)
                
                # Skip if paused
                if self.state == EngagementState.PAUSED:
                    logger.debug(f"[{now.strftime('%H:%M:%S')}] ⏸️ Paused - waiting...")
                    await asyncio.sleep(10)
                    continue
                
                # Find a platform that can accept comments
                platform = self._select_next_platform()
                
                if platform:
                    logger.info(f"[{now.strftime('%H:%M:%S')}] 🎯 Selected platform: {platform}")
                    await self._post_comment(platform)
                    
                    # Calculate next comment time
                    delay = self._get_next_delay()
                    self._next_comment_at = datetime.now(timezone.utc) + timedelta(seconds=delay)
                    next_time = (datetime.now() + timedelta(seconds=delay)).strftime('%H:%M:%S')
                    
                    logger.info(f"[{now.strftime('%H:%M:%S')}] ⏳ Next comment in {delay}s (at {next_time})")
                    await asyncio.sleep(delay)
                else:
                    # All platforms at limit, wait and check again
                    logger.info(f"[{now.strftime('%H:%M:%S')}] ⏸️ All platforms at hourly limit")
                    self._log_platform_limits()
                    await asyncio.sleep(60)
                    
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Engagement loop error: {e}")
                await asyncio.sleep(30)
        
        logger.info("📢 Engagement loop stopped")
    
    async def _idle_monitor_loop(self):
        """Monitor Mac idle time (for logging only - automation runs regardless of user activity)."""
        logger.info("="*60)
        logger.info("👀 IDLE MONITOR STARTED")
        logger.info(f"   Mode: Passive (runs regardless of user activity)")
        logger.info(f"   Check interval: {self.IDLE_CHECK_INTERVAL}s")
        logger.info("="*60)
        
        while not self._should_stop:
            try:
                await asyncio.sleep(self.IDLE_CHECK_INTERVAL)
                
                idle_hours = self.get_idle_hours()
                # Just log for info, don't pause based on user activity
                if idle_hours < 0.01:
                    logger.debug(f"User active (idle: {idle_hours*60:.0f}s) - automation continues")
                else:
                    logger.debug(f"Mac idle time: {idle_hours:.2f} hours")
                
                # Auto-resume from IDLE_WAITING state (if manually paused)
                if self.state == EngagementState.IDLE_WAITING:
                    if idle_hours >= self.auto_resume_after_hours:
                        logger.info(f"🔄 Auto-resuming after {idle_hours:.1f}h idle")
                        self.state = EngagementState.RUNNING
                        await self._notify_state_changed()
                
                # NOTE: We do NOT pause when user is active anymore
                # Automation runs continuously regardless of user activity
                    
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Idle monitor error: {e}")
        
        logger.info("👀 Idle monitor stopped")
    
    def _select_next_platform(self) -> Optional[str]:
        """Select next platform to post on (round-robin with capacity check)."""
        # Sort by fewest comments this hour
        available = []
        for platform, stats in self.platform_stats.items():
            can_post, _ = self._can_post_on_platform(platform)
            if can_post:
                available.append((platform, stats.comments_this_hour))
        
        if not available:
            return None
        
        # Pick the one with fewest comments (balance across platforms)
        available.sort(key=lambda x: x[1])
        return available[0][0]
    
    def _log_status_summary(self):
        """Log a summary of current status."""
        now = datetime.now()
        total_today = sum(s.comments_today for s in self.platform_stats.values())
        total_this_hour = sum(s.comments_this_hour for s in self.platform_stats.values())
        
        logger.info("-"*60)
        logger.info(f"📊 STATUS SUMMARY @ {now.strftime('%H:%M:%S')}")
        logger.info(f"   State: {self.state.value}")
        logger.info(f"   Total today: {total_today} | This hour: {total_this_hour}")
        logger.info(f"   Mac idle: {self.get_idle_minutes():.1f} minutes")
        for p, s in self.platform_stats.items():
            status = "✅" if s.is_enabled else "❌"
            logger.info(f"   {status} {p}: {s.comments_this_hour}/{self.COMMENTS_PER_HOUR_PER_PLATFORM}/hr | {s.comments_today} today")
        logger.info("-"*60)
    
    def _log_platform_limits(self):
        """Log platform limit details."""
        for p, s in self.platform_stats.items():
            if s.is_enabled:
                remaining = self.COMMENTS_PER_HOUR_PER_PLATFORM - s.comments_this_hour
                logger.debug(f"   {p}: {s.comments_this_hour}/{self.COMMENTS_PER_HOUR_PER_PLATFORM} (remaining: {remaining})")
    
    async def _post_comment(self, platform: str):
        """Post a single comment on a platform."""
        now = datetime.now()
        stats = self.platform_stats[platform]
        
        logger.info(f"[{now.strftime('%H:%M:%S')}] 💬 POSTING TO {platform.upper()}")
        logger.info(f"   Platform stats: {stats.comments_this_hour}/{self.COMMENTS_PER_HOUR_PER_PLATFORM} this hour, {stats.comments_today} today")
        
        try:
            # Note: Safari session check disabled - let engagement modules handle login
            # The engagement modules already verify login state internally
            logger.debug(f"Starting engagement on {platform} (session check delegated to module)")
            
            # Import and run engagement
            from services.engagement.engagement_runner import EngagementRunner
            
            runner = EngagementRunner()
            result = await runner.run_single(platform)
            
            if result.get('posted'):
                stats.comments_this_hour += 1
                stats.comments_today += 1
                stats.last_comment_at = datetime.now(timezone.utc)
                
                logger.success(f"[{datetime.now().strftime('%H:%M:%S')}] ✅ SUCCESS on {platform.upper()}")
                logger.info(f"   Comment: {result.get('comment', '')[:80]}...")
                logger.info(f"   Post URL: {result.get('post_url', 'N/A')}")
                logger.info(f"   Platform total: {stats.comments_this_hour}/{self.COMMENTS_PER_HOUR_PER_PLATFORM} this hour")
                
                # Notify callbacks
                for callback in self._on_comment_posted:
                    try:
                        await callback(platform, result)
                    except:
                        pass
            else:
                error = result.get('error', 'Unknown error')
                stats.errors.append(error)
                logger.warning(f"[{datetime.now().strftime('%H:%M:%S')}] ⚠️ FAILED on {platform.upper()}")
                logger.warning(f"   Error: {error}")
                logger.warning(f"   Total errors: {len(stats.errors)}")
                
        except Exception as e:
            stats.errors.append(str(e))
            logger.error(f"[{datetime.now().strftime('%H:%M:%S')}] ❌ EXCEPTION on {platform.upper()}")
            logger.error(f"   Error: {e}")
            import traceback
            logger.error(f"   Traceback: {traceback.format_exc()[:500]}")
    
    async def _notify_state_changed(self):
        """Notify state change callbacks."""
        for callback in self._on_state_changed:
            try:
                await callback(self.state)
            except:
                pass
    
    # =========================================================================
    # CALLBACKS
    # =========================================================================
    
    def on_comment_posted(self, callback: callable):
        """Register callback for when comment is posted."""
        self._on_comment_posted.append(callback)
    
    def on_state_changed(self, callback: callable):
        """Register callback for state changes."""
        self._on_state_changed.append(callback)


# Singleton getter
def get_engagement_controller() -> EngagementController:
    return EngagementController.get_instance()
