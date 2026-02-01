"""
Shared service client for inter-service communication.
"""
import os
import httpx
from typing import Optional, Dict, Any


class ServiceClient:
    """Client for calling other microservices in the MediaPoster ecosystem."""
    
    def __init__(self):
        self.services = {
            "core": os.getenv("MEDIAPOSTER_URL", "http://localhost:5555"),
            "safari": os.getenv("SAFARI_URL", "http://localhost:6001"),
            "remotion": os.getenv("REMOTION_URL", "http://localhost:6002"),
            "media": os.getenv("MEDIA_PIPELINE_URL", "http://localhost:6004"),
            "ai": os.getenv("CONTENT_INTEL_URL", "http://localhost:6006"),
        }
        self.timeout = 30.0
    
    async def call(
        self, 
        service: str, 
        endpoint: str, 
        data: Optional[Dict[str, Any]] = None,
        method: str = "GET"
    ) -> Dict[str, Any]:
        """Make a request to another service."""
        if service not in self.services:
            raise ValueError(f"Unknown service: {service}")
            
        url = f"{self.services[service]}{endpoint}"
        
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            if method.upper() == "POST":
                response = await client.post(url, json=data)
            elif method.upper() == "PUT":
                response = await client.put(url, json=data)
            elif method.upper() == "DELETE":
                response = await client.delete(url)
            else:
                response = await client.get(url, params=data)
            
            response.raise_for_status()
            return response.json()
    
    async def health_check(self, service: str) -> bool:
        """Check if a service is healthy."""
        try:
            endpoint = "/api/external/health" if service == "core" else "/health"
            result = await self.call(service, endpoint)
            return result.get("status") == "healthy"
        except Exception:
            return False


_client: Optional[ServiceClient] = None


def get_service_client() -> ServiceClient:
    """Get the shared service client instance."""
    global _client
    if _client is None:
        _client = ServiceClient()
    return _client
