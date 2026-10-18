"""
DM Sender - Safari Automation for Sending DMs
Integrates with the DM outreach pipeline to actually send messages.
"""

import os
import subprocess
import asyncio
from datetime import datetime, timezone
from typing import Dict, List, Optional
from loguru import logger

from .dm_list_manager import get_dm_list_manager, ProspectStatus


class DMSender:
    """
    Sends DMs via Safari automation.
    
    Supports:
    - Instagram DMs
    - Twitter/X DMs
    - TikTok DMs (limited)
    """
    
    def __init__(self):
        self.dm_manager = get_dm_list_manager()
        self.rate_limits = {
            "instagram": {"per_hour": 20, "per_day": 100},
            "twitter": {"per_hour": 30, "per_day": 200},
            "tiktok": {"per_hour": 10, "per_day": 50}
        }
        self.sent_counts = {"instagram": 0, "twitter": 0, "tiktok": 0}
        logger.info("✅ DMSender initialized")
    
    async def send_dm(
        self,
        entry_id: str,
        message: str,
        platform: str = None
    ) -> Dict:
        """
        Send a DM to a prospect via Safari automation.
        
        Args:
            entry_id: DM list entry ID
            message: Message content to send
            platform: Override platform (auto-detected from entry)
        
        Returns:
            Result dict with success status
        """
        try:
            # Get entry details
            entries = self.dm_manager.get_dm_list(limit=100)
            entry = next((e for e in entries if e.id == entry_id), None)
            
            if not entry or not entry.prospect:
                return {"success": False, "error": "Entry not found"}
            
            prospect = entry.prospect
            platform = platform or prospect.platform
            username = prospect.username.replace("@", "")
            
            # Check rate limits
            if not self._check_rate_limit(platform):
                return {
                    "success": False,
                    "error": f"Rate limit exceeded for {platform}"
                }
            
            # Send via platform-specific method
            if platform == "instagram":
                result = await self._send_instagram_dm(username, message)
            elif platform == "twitter":
                result = await self._send_twitter_dm(username, message)
            elif platform == "tiktok":
                result = await self._send_tiktok_dm(username, message)
            else:
                return {"success": False, "error": f"Unsupported platform: {platform}"}
            
            if result.get("success"):
                # Record interaction
                self.dm_manager.record_interaction(entry_id, direction="sent")
                self.sent_counts[platform] = self.sent_counts.get(platform, 0) + 1
                
                logger.info(f"✅ DM sent to @{username} on {platform}")
            
            return result
            
        except Exception as e:
            logger.error(f"DM send failed: {e}")
            return {"success": False, "error": str(e)}
    
    async def _send_instagram_dm(self, username: str, message: str) -> Dict:
        """Send Instagram DM via Safari automation."""
        script = f'''
        tell application "Safari"
            activate
            
            -- Navigate to Instagram DM
            set URL of current tab of front window to "https://www.instagram.com/direct/new/"
            delay 3
            
            -- Search for user
            do JavaScript "
                const searchInput = document.querySelector('input[placeholder*=\\"Search\\"]');
                if (searchInput) {{
                    searchInput.focus();
                    searchInput.value = '{username}';
                    searchInput.dispatchEvent(new Event('input', {{ bubbles: true }}));
                }}
            " in current tab of front window
            delay 2
            
            -- Click on user result
            do JavaScript "
                const results = document.querySelectorAll('[role=\\"button\\"]');
                for (const r of results) {{
                    if (r.textContent.toLowerCase().includes('{username.lower()}')) {{
                        r.click();
                        break;
                    }}
                }}
            " in current tab of front window
            delay 1
            
            -- Click Next/Chat button
            do JavaScript "
                const nextBtn = Array.from(document.querySelectorAll('button')).find(b => b.textContent === 'Next' || b.textContent === 'Chat');
                if (nextBtn) nextBtn.click();
            " in current tab of front window
            delay 2
            
            -- Type message
            do JavaScript "
                const textarea = document.querySelector('textarea[placeholder*=\\"Message\\"]');
                if (textarea) {{
                    textarea.focus();
                    textarea.value = `{message.replace('`', '\\`').replace('"', '\\"')}`;
                    textarea.dispatchEvent(new Event('input', {{ bubbles: true }}));
                }}
            " in current tab of front window
            delay 1
            
            -- Send message
            do JavaScript "
                const sendBtn = document.querySelector('button[type=\\"submit\\"]') || 
                               Array.from(document.querySelectorAll('button')).find(b => b.textContent === 'Send');
                if (sendBtn) sendBtn.click();
            " in current tab of front window
            delay 1
            
            return "sent"
        end tell
        '''
        
        return await self._run_applescript(script)
    
    async def _send_twitter_dm(self, username: str, message: str) -> Dict:
        """Send Twitter/X DM via Safari automation."""
        script = f'''
        tell application "Safari"
            activate
            
            -- Navigate to Twitter DM compose
            set URL of current tab of front window to "https://twitter.com/messages/compose"
            delay 3
            
            -- Search for user
            do JavaScript "
                const searchInput = document.querySelector('input[data-testid=\\"searchPeople\\"]') ||
                                   document.querySelector('input[placeholder*=\\"Search\\"]');
                if (searchInput) {{
                    searchInput.focus();
                    searchInput.value = '{username}';
                    searchInput.dispatchEvent(new Event('input', {{ bubbles: true }}));
                }}
            " in current tab of front window
            delay 2
            
            -- Click on user result
            do JavaScript "
                const userItem = document.querySelector('[data-testid=\\"typeaheadResult\\"]');
                if (userItem) userItem.click();
            " in current tab of front window
            delay 1
            
            -- Click Next
            do JavaScript "
                const nextBtn = document.querySelector('[data-testid=\\"nextButton\\"]');
                if (nextBtn) nextBtn.click();
            " in current tab of front window
            delay 2
            
            -- Type message
            do JavaScript "
                const input = document.querySelector('[data-testid=\\"dmComposerTextInput\\"]');
                if (input) {{
                    input.focus();
                    input.textContent = `{message.replace('`', '\\`').replace('"', '\\"')}`;
                    input.dispatchEvent(new Event('input', {{ bubbles: true }}));
                }}
            " in current tab of front window
            delay 1
            
            -- Send
            do JavaScript "
                const sendBtn = document.querySelector('[data-testid=\\"dmComposerSendButton\\"]');
                if (sendBtn) sendBtn.click();
            " in current tab of front window
            delay 1
            
            return "sent"
        end tell
        '''
        
        return await self._run_applescript(script)
    
    async def _send_tiktok_dm(self, username: str, message: str) -> Dict:
        """Send TikTok DM via Safari automation."""
        script = f'''
        tell application "Safari"
            activate
            
            -- Navigate to TikTok messages
            set URL of current tab of front window to "https://www.tiktok.com/messages"
            delay 3
            
            -- Note: TikTok DM sending is limited
            -- This is a placeholder for the actual implementation
            
            return "tiktok_dm_limited"
        end tell
        '''
        
        return await self._run_applescript(script)
    
    async def _run_applescript(self, script: str) -> Dict:
        """Run AppleScript and return result."""
        try:
            result = await asyncio.to_thread(
                subprocess.run,
                ["osascript", "-e", script],
                capture_output=True,
                text=True,
                timeout=60
            )
            
            if result.returncode == 0:
                return {"success": True, "output": result.stdout.strip()}
            else:
                return {"success": False, "error": result.stderr.strip()}
                
        except subprocess.TimeoutExpired:
            return {"success": False, "error": "Script timeout"}
        except Exception as e:
            return {"success": False, "error": str(e)}
    
    def _check_rate_limit(self, platform: str) -> bool:
        """Check if we're within rate limits for platform."""
        limits = self.rate_limits.get(platform, {"per_hour": 10})
        current = self.sent_counts.get(platform, 0)
        return current < limits["per_hour"]
    
    async def send_batch(
        self,
        entries: List[Dict],
        delay_seconds: int = 60
    ) -> Dict:
        """
        Send DMs to multiple entries with delays.
        
        Args:
            entries: List of {entry_id, message} dicts
            delay_seconds: Delay between sends
        
        Returns:
            Summary of results
        """
        results = {"sent": 0, "failed": 0, "errors": []}
        
        for item in entries:
            entry_id = item.get("entry_id")
            message = item.get("message")
            
            if not entry_id or not message:
                results["failed"] += 1
                results["errors"].append("Missing entry_id or message")
                continue
            
            result = await self.send_dm(entry_id, message)
            
            if result.get("success"):
                results["sent"] += 1
            else:
                results["failed"] += 1
                results["errors"].append(result.get("error"))
            
            # Delay between sends
            if delay_seconds > 0:
                await asyncio.sleep(delay_seconds)
        
        return results
    
    def get_daily_quota(self) -> Dict:
        """Get remaining daily quota per platform."""
        return {
            platform: {
                "sent": self.sent_counts.get(platform, 0),
                "remaining": limits["per_day"] - self.sent_counts.get(platform, 0),
                "limit": limits["per_day"]
            }
            for platform, limits in self.rate_limits.items()
        }


# =============================================================================
# SINGLETON
# =============================================================================

_sender_instance: Optional[DMSender] = None

def get_dm_sender() -> DMSender:
    """Get singleton instance of DMSender."""
    global _sender_instance
    if _sender_instance is None:
        _sender_instance = DMSender()
    return _sender_instance
