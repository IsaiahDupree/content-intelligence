"""
Engagement Runner

Runs engagement tasks until reaching daily limits per platform.
Can be triggered via pub/sub or run as a scheduled task.

Usage:
    # Run once
    runner = EngagementRunner()
    await runner.run_all_platforms(comments_per_run=5)
    
    # Run until limits reached
    await runner.run_until_limits()
    
    # CLI
    python engagement_runner.py --platform threads --count 10
    python engagement_runner.py --all --count 5
"""

import os
import sys
import asyncio
import logging
import argparse
from datetime import datetime, timezone
from typing import Optional, Dict, List, Any

# Add parent paths for imports
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(__file__))))

from services.engagement.comment_tracker import CommentTracker, get_comment_tracker
from services.engagement.engagement_service import PLATFORMS, DELAY_CONFIG

logger = logging.getLogger(__name__)

# Default comments per run per platform
DEFAULT_COMMENTS_PER_RUN = 5


class EngagementRunner:
    """
    Runs engagement tasks across platforms.
    
    Features:
    - Run engagement on single or all platforms
    - Respect daily limits
    - Track progress and results
    - Random delays between comments
    """
    
    def __init__(self):
        self._tracker = None
        self._modules: Dict[str, Any] = {}
        self._results: Dict[str, Dict[str, int]] = {}
    
    @property
    def tracker(self) -> CommentTracker:
        if self._tracker is None:
            self._tracker = get_comment_tracker()
        return self._tracker
    
    def _get_module(self, platform: str):
        """Get or create engagement module for platform."""
        if platform in self._modules:
            return self._modules[platform]
        
        openai_key = os.environ.get('OPENAI_API_KEY')
        
        if platform == 'threads':
            from scripts.auto_engagement.threads_engagement import ThreadsEngagement
            module = ThreadsEngagement(openai_api_key=openai_key)
        elif platform == 'instagram':
            from scripts.auto_engagement.instagram_engagement import InstagramEngagement
            module = InstagramEngagement(openai_api_key=openai_key)
        elif platform == 'tiktok':
            from scripts.auto_engagement.tiktok_engagement import TikTokEngagement
            module = TikTokEngagement(openai_api_key=openai_key)
        elif platform == 'twitter':
            from scripts.auto_engagement.twitter_engagement import TwitterEngagement
            module = TwitterEngagement(openai_api_key=openai_key)
        else:
            raise ValueError(f"Unknown platform: {platform}")
        
        self._modules[platform] = module
        return module
    
    async def run_single(self, platform: str) -> Dict[str, Any]:
        """
        Run a single engagement on a platform.
        
        Args:
            platform: Platform to engage on
            
        Returns:
            Result dict with success, post_url, comment, etc.
        """
        # Check if enabled
        if not await self.tracker.is_enabled(platform):
            return {'success': False, 'error': 'Platform is paused'}
        
        # Check limit
        if await self.tracker.is_limit_reached(platform):
            return {'success': False, 'error': 'Daily limit reached'}
        
        # Get module and run (TikTok uses engage_with_video, others use engage_with_post)
        module = self._get_module(platform)
        if platform == 'tiktok':
            result = module.engage_with_video()
        else:
            result = module.engage_with_post()
        
        # Track if posted (TikTok doesn't have post_url, use username as identifier)
        post_url = getattr(result, 'post_url', '') or f"https://tiktok.com/@{result.username}" if platform == 'tiktok' else ''
        if result.comment_posted and (post_url or result.username):
            try:
                await self.tracker.record_comment(
                    platform=platform,
                    post_url=post_url or f"https://{platform}.com/@{result.username}",
                    comment_text=result.generated_comment or '',
                    post_username=result.username or '',
                    proof_screenshot=getattr(result, 'proof_screenshot', '')
                )
            except ValueError as e:
                # Duplicate
                logger.warning(f"Duplicate comment: {e}")
        
        return {
            'success': result.success,
            'posted': result.comment_posted,
            'post_url': getattr(result, 'post_url', '') or f"https://{platform}.com/@{result.username}",
            'username': result.username,
            'comment': result.generated_comment,
            'error': getattr(result, 'error', '')
        }
    
    async def run_platform(
        self,
        platform: str,
        count: int = DEFAULT_COMMENTS_PER_RUN,
        delay_between: bool = True
    ) -> Dict[str, Any]:
        """
        Run multiple engagements on a platform.
        
        Args:
            platform: Platform to engage on
            count: Number of comments to post
            delay_between: Whether to delay between comments
            
        Returns:
            Summary dict with posted count, skipped count, etc.
        """
        import random
        
        posted = 0
        skipped = 0
        errors = []
        
        print(f"\n{'='*60}")
        print(f"🚀 Running {count} engagements on {platform.upper()}")
        print(f"{'='*60}")
        
        # Get remaining capacity
        remaining = await self.tracker.get_remaining(platform)
        actual_count = min(count, remaining)
        
        if actual_count == 0:
            print(f"   ⚠️ Daily limit reached for {platform}")
            return {'posted': 0, 'skipped': 0, 'errors': ['Daily limit reached']}
        
        if actual_count < count:
            print(f"   ⚠️ Adjusted count from {count} to {actual_count} (limit)")
        
        for i in range(actual_count):
            print(f"\n[{i+1}/{actual_count}] Engaging on {platform}...")
            
            try:
                result = await self.run_single(platform)
                
                if result.get('posted'):
                    posted += 1
                    print(f"   ✅ Posted: {result.get('comment', '')[:50]}...")
                else:
                    skipped += 1
                    print(f"   ⏭️ Skipped: {result.get('error', 'Unknown')}")
                    if result.get('error'):
                        errors.append(result['error'])
                
                # Delay between comments
                if delay_between and i < actual_count - 1:
                    delay_config = DELAY_CONFIG.get(platform, {'min': 30, 'max': 60})
                    delay = random.randint(delay_config['min'], delay_config['max'])
                    print(f"   ⏳ Waiting {delay}s before next...")
                    await asyncio.sleep(delay)
                    
            except Exception as e:
                skipped += 1
                errors.append(str(e))
                print(f"   ❌ Error: {e}")
        
        summary = {
            'platform': platform,
            'posted': posted,
            'skipped': skipped,
            'errors': errors,
            'remaining': await self.tracker.get_remaining(platform)
        }
        
        print(f"\n📊 {platform.upper()} Summary: {posted} posted, {skipped} skipped")
        
        return summary
    
    async def run_all_platforms(
        self,
        comments_per_platform: int = DEFAULT_COMMENTS_PER_RUN
    ) -> Dict[str, Dict[str, Any]]:
        """
        Run engagements on all enabled platforms.
        
        Args:
            comments_per_platform: Comments to post per platform
            
        Returns:
            Dict mapping platform to summary
        """
        results = {}
        
        print("\n" + "="*60)
        print("🌐 MULTI-PLATFORM ENGAGEMENT")
        print("="*60)
        
        for platform in PLATFORMS:
            if await self.tracker.is_enabled(platform):
                results[platform] = await self.run_platform(
                    platform, 
                    count=comments_per_platform,
                    delay_between=True
                )
            else:
                print(f"\n⏸️ {platform.upper()} is paused, skipping")
                results[platform] = {'posted': 0, 'skipped': 0, 'errors': ['Paused']}
        
        # Print final summary
        print("\n" + "="*60)
        print("📊 FINAL SUMMARY")
        print("="*60)
        total_posted = sum(r.get('posted', 0) for r in results.values())
        total_skipped = sum(r.get('skipped', 0) for r in results.values())
        
        for platform, summary in results.items():
            status = await self.tracker.get_status(platform)
            print(f"   {platform.upper()}: {summary.get('posted', 0)} posted, "
                  f"{status.today_count}/{status.daily_limit} today")
        
        print(f"\n   TOTAL: {total_posted} posted, {total_skipped} skipped")
        
        return results
    
    async def run_until_limits(
        self,
        platforms: List[str] = None,
        batch_size: int = 5
    ) -> Dict[str, Dict[str, Any]]:
        """
        Run engagements until daily limits are reached.
        
        Args:
            platforms: Platforms to run (all if None)
            batch_size: Comments per batch before checking limits
            
        Returns:
            Final summary per platform
        """
        platforms = platforms or PLATFORMS
        results = {p: {'posted': 0, 'skipped': 0, 'errors': []} for p in platforms}
        
        print("\n" + "="*60)
        print("🎯 RUNNING UNTIL DAILY LIMITS")
        print("="*60)
        
        # Show current status
        for platform in platforms:
            status = await self.tracker.get_status(platform)
            print(f"   {platform.upper()}: {status.today_count}/{status.daily_limit} "
                  f"({status.remaining} remaining)")
        
        # Keep running until all limits reached
        while True:
            any_remaining = False
            
            for platform in platforms:
                if not await self.tracker.is_enabled(platform):
                    continue
                    
                remaining = await self.tracker.get_remaining(platform)
                if remaining <= 0:
                    continue
                
                any_remaining = True
                batch = min(batch_size, remaining)
                
                summary = await self.run_platform(platform, count=batch)
                results[platform]['posted'] += summary.get('posted', 0)
                results[platform]['skipped'] += summary.get('skipped', 0)
                results[platform]['errors'].extend(summary.get('errors', []))
            
            if not any_remaining:
                break
        
        # Print final summary
        print("\n" + "="*60)
        print("🏁 LIMITS REACHED - FINAL SUMMARY")
        print("="*60)
        
        for platform in platforms:
            status = await self.tracker.get_status(platform)
            r = results[platform]
            print(f"   {platform.upper()}: {r['posted']} posted today "
                  f"({status.today_count}/{status.daily_limit})")
        
        return results
    
    async def get_status_report(self) -> str:
        """Get a formatted status report for all platforms."""
        lines = ["📊 ENGAGEMENT STATUS REPORT", "="*40]
        
        total_today = 0
        total_limit = 0
        
        for platform in PLATFORMS:
            status = await self.tracker.get_status(platform)
            enabled = "✅" if status.is_enabled else "⏸️"
            lines.append(
                f"{enabled} {platform.upper()}: {status.today_count}/{status.daily_limit} "
                f"({status.remaining} remaining)"
            )
            total_today += status.today_count
            total_limit += status.daily_limit
        
        lines.append("="*40)
        lines.append(f"TOTAL: {total_today}/{total_limit}")
        
        return "\n".join(lines)


async def main():
    """CLI entry point."""
    parser = argparse.ArgumentParser(description='Run engagement tasks')
    parser.add_argument('--platform', '-p', choices=PLATFORMS, help='Platform to engage on')
    parser.add_argument('--all', '-a', action='store_true', help='Run on all platforms')
    parser.add_argument('--count', '-c', type=int, default=5, help='Comments per platform')
    parser.add_argument('--until-limits', '-u', action='store_true', help='Run until limits reached')
    parser.add_argument('--status', '-s', action='store_true', help='Show status report')
    
    args = parser.parse_args()
    
    runner = EngagementRunner()
    
    if args.status:
        report = await runner.get_status_report()
        print(report)
        return
    
    if args.until_limits:
        platforms = [args.platform] if args.platform else None
        await runner.run_until_limits(platforms=platforms, batch_size=args.count)
    elif args.all:
        await runner.run_all_platforms(comments_per_platform=args.count)
    elif args.platform:
        await runner.run_platform(args.platform, count=args.count)
    else:
        parser.print_help()


if __name__ == '__main__':
    asyncio.run(main())
