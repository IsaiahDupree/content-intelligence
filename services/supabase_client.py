"""Supabase Client Integration - handles database operations for UGC format data."""
import os
from typing import Dict, Any, List, Optional
from datetime import datetime


class SupabaseClient:
    """Client for Supabase database operations."""

    def __init__(self):
        self.url = os.getenv("SUPABASE_URL")
        self.service_key = os.getenv("SUPABASE_SERVICE_KEY")
        self.enabled = bool(self.url and self.service_key)

        if self.enabled:
            try:
                from supabase import create_client
                self.client = create_client(self.url, self.service_key)
            except Exception as e:
                print(f"Failed to initialize Supabase: {e}")
                self.enabled = False

    def store_format_classification(self, content_id: str, format_result: Dict[str, Any]) -> bool:
        """Store UGC format classification in Supabase."""
        if not self.enabled:
            return False

        try:
            data = {
                "ai_ugc_format": {
                    "format": format_result.get("format"),
                    "confidence": format_result.get("confidence"),
                    "all_scores": format_result.get("all_scores", {}),
                    "reasoning": format_result.get("reasoning"),
                    "updated_at": datetime.utcnow().isoformat()
                }
            }

            result = self.client.table("content").update(data).eq("id", content_id).execute()
            return True
        except Exception as e:
            print(f"Error storing format classification: {e}")
            return False

    def store_performance_score(self, content_id: str, score_result: Dict[str, Any]) -> bool:
        """Store performance score in Supabase."""
        if not self.enabled:
            return False

        try:
            data = {
                "ai_performance_score": {
                    "overall_score": score_result.get("overall_score"),
                    "engagement_score": score_result.get("engagement_score"),
                    "retention_score": score_result.get("retention_score"),
                    "conversion_score": score_result.get("conversion_score"),
                    "metrics_breakdown": score_result.get("metrics_breakdown", {}),
                    "timestamp": score_result.get("timestamp")
                }
            }

            result = self.client.table("content").update(data).eq("id", content_id).execute()
            return True
        except Exception as e:
            print(f"Error storing performance score: {e}")
            return False

    def store_repurposing_recommendations(self, content_id: str, recommendations: List[Dict[str, Any]]) -> bool:
        """Store repurposing recommendations in Supabase."""
        if not self.enabled:
            return False

        try:
            data = {
                "ai_repurpose_formats": {
                    "recommendations": [
                        {
                            "format": r.get("format"),
                            "viability_score": r.get("viability_score"),
                            "rationale": r.get("rationale")
                        }
                        for r in recommendations
                    ],
                    "updated_at": datetime.utcnow().isoformat()
                }
            }

            result = self.client.table("content").update(data).eq("id", content_id).execute()
            return True
        except Exception as e:
            print(f"Error storing repurposing recommendations: {e}")
            return False

    def get_content(self, content_id: str) -> Optional[Dict[str, Any]]:
        """Retrieve content record from Supabase."""
        if not self.enabled:
            return None

        try:
            result = self.client.table("content").select("*").eq("id", content_id).execute()
            if result.data:
                return result.data[0]
            return None
        except Exception as e:
            print(f"Error retrieving content: {e}")
            return None

    def store_format_history(self, content_id: str, format_data: Dict[str, Any]) -> bool:
        """Store historical format classification in history table."""
        if not self.enabled:
            return False

        try:
            history_data = {
                "content_id": content_id,
                "format": format_data.get("format"),
                "confidence": format_data.get("confidence"),
                "all_scores": format_data.get("all_scores", {}),
                "timestamp": datetime.utcnow().isoformat()
            }

            result = self.client.table("format_history").insert([history_data]).execute()
            return True
        except Exception as e:
            print(f"Error storing format history: {e}")
            return False


# Create singleton instance
_supabase_client = None


def get_supabase_client() -> SupabaseClient:
    """Get or create Supabase client singleton."""
    global _supabase_client
    if _supabase_client is None:
        _supabase_client = SupabaseClient()
    return _supabase_client
