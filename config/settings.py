"""
Content Intelligence service configuration.
"""
import os
from pathlib import Path

# Service settings
SERVICE_NAME = "content-intelligence"
SERVICE_VERSION = "1.0.0"
SERVICE_PORT = int(os.getenv("PORT", 6006))

# Paths
BASE_DIR = Path(__file__).parent.parent
SERVICES_DIR = BASE_DIR / "services"

# External services
MEDIAPOSTER_URL = os.getenv("MEDIAPOSTER_URL", "http://localhost:5555")
MEDIA_PIPELINE_URL = os.getenv("MEDIA_PIPELINE_URL", "http://localhost:6004")

# AI Provider settings
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "")
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "")
GROQ_API_KEY = os.getenv("GROQ_API_KEY", "")
GOOGLE_API_KEY = os.getenv("GOOGLE_API_KEY", "")

# Default AI provider
DEFAULT_PROVIDER = os.getenv("DEFAULT_AI_PROVIDER", "groq")

# Rate limiting
MAX_REQUESTS_PER_MINUTE = int(os.getenv("MAX_REQUESTS_PER_MINUTE", 60))
