"""Production health monitoring and alerting system."""
import time
from typing import Dict, Any
from datetime import datetime, timezone
import requests
import os


class HealthMonitor:
    """Monitors service health and triggers alerts."""

    def __init__(self, check_interval_seconds: int = 300, timeout_seconds: int = 10):
        """Initialize health monitor.

        Args:
            check_interval_seconds: How often to check health (default 5 min)
            timeout_seconds: Request timeout for health checks
        """
        self.check_interval_seconds = check_interval_seconds
        self.timeout_seconds = timeout_seconds
        self.health_history: list = []
        self.failures: int = 0
        self.last_check_time: float = 0

    def check_service_health(self, service_url: str = "http://localhost:6006") -> Dict[str, Any]:
        """Check service health endpoint.

        Returns:
            Health status with timestamp and results
        """
        result = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "service_url": service_url,
            "status": "unknown",
            "response_time_ms": 0,
            "endpoint_status": None,
            "supabase_status": None,
            "error": None
        }

        try:
            start = time.time()
            response = requests.get(
                f"{service_url}/health",
                timeout=self.timeout_seconds
            )
            elapsed = (time.time() - start) * 1000

            result["response_time_ms"] = elapsed
            result["status"] = "healthy" if response.status_code == 200 else "degraded"
            result["endpoint_status"] = response.status_code

            # Parse response
            if response.status_code == 200:
                data = response.json()
                result["supabase_status"] = data.get("supabase_connected")
                result["service_info"] = {
                    "name": data.get("service"),
                    "version": data.get("version")
                }

                self.failures = 0
            else:
                self.failures += 1
                result["error"] = f"HTTP {response.status_code}"

        except requests.exceptions.Timeout:
            self.failures += 1
            result["status"] = "unhealthy"
            result["error"] = "Health check timeout"
        except requests.exceptions.ConnectionError:
            self.failures += 1
            result["status"] = "unhealthy"
            result["error"] = "Connection refused"
        except Exception as e:
            self.failures += 1
            result["status"] = "unhealthy"
            result["error"] = str(e)

        # Track history
        self.health_history.append(result)
        self.last_check_time = time.time()

        return result

    def get_health_summary(self) -> Dict[str, Any]:
        """Get overall health summary."""
        if not self.health_history:
            return {
                "status": "unknown",
                "total_checks": 0,
                "recent_failures": 0,
                "uptime_percentage": 0
            }

        recent_checks = self.health_history[-10:]  # Last 10 checks
        failures = len([h for h in recent_checks if h["status"] != "healthy"])
        uptime = ((len(recent_checks) - failures) / len(recent_checks)) * 100 if recent_checks else 0

        return {
            "status": "healthy" if self.failures == 0 else "degraded" if self.failures < 3 else "unhealthy",
            "total_checks": len(self.health_history),
            "recent_failures": failures,
            "consecutive_failures": self.failures,
            "uptime_percentage": uptime,
            "last_check": self.health_history[-1]["timestamp"] if self.health_history else None,
            "response_time_ms": self.health_history[-1]["response_time_ms"] if self.health_history else 0
        }

    def should_alert(self) -> bool:
        """Determine if an alert should be triggered."""
        return self.failures >= 3  # Alert after 3 consecutive failures

    def get_alerts(self) -> list:
        """Get active alerts."""
        alerts = []

        summary = self.get_health_summary()

        if summary["consecutive_failures"] >= 3:
            alerts.append({
                "severity": "critical",
                "message": f"Service unhealthy - {summary['consecutive_failures']} consecutive failures",
                "triggered_at": datetime.now(timezone.utc).isoformat()
            })

        if summary["uptime_percentage"] < 90:
            alerts.append({
                "severity": "warning",
                "message": f"Low uptime: {summary['uptime_percentage']:.1f}%",
                "triggered_at": datetime.now(timezone.utc).isoformat()
            })

        return alerts

    def clear_history(self) -> None:
        """Clear health check history."""
        self.health_history = []
        self.failures = 0


# Singleton instance
_monitor = None


def get_health_monitor() -> HealthMonitor:
    """Get or create health monitor singleton."""
    global _monitor
    if _monitor is None:
        interval = int(os.getenv("HEALTH_CHECK_INTERVAL", 300000)) // 1000
        timeout = int(os.getenv("HEALTH_CHECK_TIMEOUT", 10000)) // 1000
        _monitor = HealthMonitor(interval, timeout)
    return _monitor
