"""
Posting Service for PRD2
Handles publication via Blotato and metrics initialization
"""
from datetime import datetime
from uuid import UUID, uuid4
from typing import List, Optional

from models.supabase_models import (
    PostingSchedule, MediaAsset, MediaAnalysis, 
    PostingMetrics, ScheduleStatus, MediaStatus
)

class PostingService:
    """Service to execute posts and initialize tracking"""

    def process_due_posts(self, schedules: List[PostingSchedule], assets: List[MediaAsset], analysis: List[MediaAnalysis]):
        """
        Process pending posts that are due
        In real app, we'd query DB.
        Here we pass lists for simulation.
        """
        now = datetime.utcnow()
        due = [s for s in schedules if s.status == ScheduleStatus.PENDING and s.scheduled_at <= now]
        
        results = []
        for schedule in due:
            asset = next((a for a in assets if a.id == schedule.media_id), None)
            insights = next((a for a in analysis if a.media_id == schedule.media_id), None)
            
            if asset and insights:
                success = self._publish_to_blotato(schedule, asset, insights)
                if success:
                    schedule.status = ScheduleStatus.POSTED
                    schedule.external_post_id = f"ext-{uuid4()}" # Mock ID
                    # Initialize metrics
                    metrics = self._initialize_metrics(schedule.id)
                    results.append((schedule, metrics))
                else:
                    schedule.status = ScheduleStatus.FAILED
            else:
                schedule.status = ScheduleStatus.FAILED
                
        return results

    def _publish_to_blotato(
        self, 
        schedule: PostingSchedule, 
        asset: MediaAsset, 
        analysis: MediaAnalysis
    ) -> bool:
        """Mock call to Blotato API"""
        # Simulate network call
        if "error" in asset.storage_path: # failure simulation hook
            return False
        return True

    def _initialize_metrics(self, schedule_id: UUID) -> PostingMetrics:
        """Create initial metrics row"""
        return PostingMetrics(
            id=uuid4(),
            schedule_id=schedule_id,
            check_time=datetime.utcnow()
        )
