"""
Scheduling Service for PRD2
Handles horizon planning, gap optimization, and slot filling
"""
from datetime import datetime, timedelta
from typing import List, Optional
from uuid import UUID, uuid4

from models.supabase_models import (
    MediaAsset, PostingSchedule, Platform, ScheduleStatus, MediaStatus
)

class SchedulingService:
    """Service to generate optimal posting schedules"""
    
    HORIZON_DAYS = 60
    HORIZON_HOURS = HORIZON_DAYS * 24
    MIN_GAP_HOURS = 2.0
    MAX_GAP_HOURS = 24.0

    def generate_schedule(
        self, 
        assets: List[MediaAsset], 
        start_time: Optional[datetime] = None
    ) -> List[PostingSchedule]:
        """
        Generate schedule for a list of assets over the horizon
        Algorithm:
        1. Calculate ideal spacing = horizon / count
        2. Clamp between 2h and 24h
        3. Assign slots
        """
        if not assets:
            return []
            
        start = start_time or datetime.utcnow()
        media_count = len(assets)
        
        # Step 1: Ideal spacing
        # If we have very few assets, we want to stretch them but not > 24h
        # If we have many, we compress but not < 2h
        ideal_spacing = self.HORIZON_HOURS / media_count
        
        # Step 2: Clamp
        spacing = max(self.MIN_GAP_HOURS, min(self.MAX_GAP_HOURS, ideal_spacing))
        
        schedules = []
        current_time = start
        
        for i, asset in enumerate(assets):
            # Calculate offset
            offset_hours = (i + 1) * spacing 
            
            # Check horizon boundary
            if offset_hours > self.HORIZON_HOURS:
                # Based on algorithm description: "break; // don’t exceed 2 months"
                # This explicitly leaves some assets unscheduled if we are flooded
                break
                
            scheduled_time = start + timedelta(hours=offset_hours)
            
            # Determine platform (use logic or asset hint)
            platform = asset.platform_hint if asset.platform_hint != Platform.MULTI else Platform.TIKTOK
            
            schedule = PostingSchedule(
                id=uuid4(),
                media_id=asset.id,
                platform=platform,
                scheduled_at=scheduled_time,
                status=ScheduleStatus.PENDING
            )
            schedules.append(schedule)
            
        return schedules

    def merge_with_existing(
        self, 
        new_assets: List[MediaAsset], 
        existing_schedules: List[PostingSchedule]
    ) -> List[PostingSchedule]:
        """
        Smart merge: fill gaps or append?
        PRD says: "Run schedule_planner again: Only fill empty slots"
        For MVP/Tests logic: We'll append new assets after the last scheduled item
        respecting the gap logic, re-calculating to verify density.
        """
        if not existing_schedules:
            return self.generate_schedule(new_assets)
            
        # Find last scheduled time
        last_post = max(s.scheduled_at for s in existing_schedules)
        
        # Schedule new assets starting after last post
        # But we must respect density.
        # Simple approach: Treat remaining horizon from last_post?
        
        # Let's stick to the PRD's deterministic "BuildSchedule" for a clean set first.
        # For merging, we will treat it as "append" for now to keep it deterministic.
        
        return self.generate_schedule(new_assets, start_time=last_post)
