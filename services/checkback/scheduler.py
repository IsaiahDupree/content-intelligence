"""Scheduled checkback jobs for periodic content re-evaluation."""
from typing import Dict, List, Any, Optional
from datetime import datetime, timedelta, timezone
import json


class CheckbackScheduler:
    """Schedules and manages periodic content re-evaluation jobs."""

    def __init__(self):
        self.scheduled_jobs: Dict[str, Dict[str, Any]] = {}
        self.job_counter = 0

    def schedule_checkback(self, content_id: str, interval_days: int = 7,
                          job_type: str = "full") -> str:
        """Schedule a checkback job for content.

        Args:
            content_id: Content to re-evaluate
            interval_days: Days between checkbacks
            job_type: "full" (all metrics) or "quick" (performance only)

        Returns:
            job_id
        """
        self.job_counter += 1
        job_id = f"checkback_{self.job_counter}_{content_id}"

        scheduled_at = datetime.now(timezone.utc)
        next_run = scheduled_at + timedelta(days=interval_days)

        self.scheduled_jobs[job_id] = {
            "content_id": content_id,
            "job_type": job_type,
            "interval_days": interval_days,
            "scheduled_at": scheduled_at.isoformat(),
            "next_run": next_run.isoformat(),
            "last_run": None,
            "status": "scheduled",
            "runs": 0,
            "errors": []
        }

        return job_id

    def get_due_jobs(self) -> List[str]:
        """Get all jobs that are due to run."""
        due_jobs = []
        now = datetime.now(timezone.utc)

        for job_id, job in self.scheduled_jobs.items():
            if job["status"] == "scheduled":
                next_run = datetime.fromisoformat(job["next_run"])
                if now >= next_run:
                    due_jobs.append(job_id)

        return due_jobs

    def execute_job(self, job_id: str) -> Dict[str, Any]:
        """Execute a checkback job.

        Returns:
            {job_id, content_id, status, result, error}
        """
        if job_id not in self.scheduled_jobs:
            return {"error": f"Job {job_id} not found"}

        job = self.scheduled_jobs[job_id]

        try:
            # Simulate job execution
            result = {
                "job_id": job_id,
                "content_id": job["content_id"],
                "job_type": job["job_type"],
                "status": "completed",
                "executed_at": datetime.now(timezone.utc).isoformat(),
                "next_run": (
                    datetime.now(timezone.utc) +
                    timedelta(days=job["interval_days"])
                ).isoformat()
            }

            # Update job
            job["last_run"] = result["executed_at"]
            job["next_run"] = result["next_run"]
            job["runs"] += 1
            job["status"] = "scheduled"  # Ready for next run

            return result

        except Exception as e:
            error = str(e)
            job["errors"].append({
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "error": error
            })
            job["status"] = "error"

            return {
                "job_id": job_id,
                "status": "failed",
                "error": error
            }

    def get_job_status(self, job_id: str) -> Optional[Dict[str, Any]]:
        """Get status of a checkback job."""
        return self.scheduled_jobs.get(job_id)

    def cancel_job(self, job_id: str) -> bool:
        """Cancel a scheduled job."""
        if job_id in self.scheduled_jobs:
            del self.scheduled_jobs[job_id]
            return True
        return False

    def list_jobs(self, status: Optional[str] = None) -> List[Dict[str, Any]]:
        """List all checkback jobs, optionally filtered by status."""
        jobs = list(self.scheduled_jobs.values())

        if status:
            jobs = [j for j in jobs if j["status"] == status]

        return jobs

    def stats(self) -> Dict[str, Any]:
        """Get checkback scheduler statistics."""
        jobs = list(self.scheduled_jobs.values())

        return {
            "total_jobs": len(jobs),
            "scheduled": len([j for j in jobs if j["status"] == "scheduled"]),
            "running": len([j for j in jobs if j["status"] == "running"]),
            "completed": len([j for j in jobs if j["status"] == "completed"]),
            "errors": len([j for j in jobs if j["status"] == "error"]),
            "due_now": len(self.get_due_jobs()),
            "total_runs": sum(j.get("runs", 0) for j in jobs)
        }


# Singleton instance
_scheduler = None


def get_checkback_scheduler() -> CheckbackScheduler:
    """Get or create checkback scheduler singleton."""
    global _scheduler
    if _scheduler is None:
        _scheduler = CheckbackScheduler()
    return _scheduler
