"""
AI Providers Module
====================
Configurable AI providers for various services.

Supports:
- OpenAI (GPT-4, GPT-4o-mini)
- Anthropic (Claude)
- Local/Mock (for testing)

Configuration via environment variables:
    AI_PROVIDER=openai|anthropic|mock
    OPENAI_API_KEY=sk-...
    ANTHROPIC_API_KEY=sk-ant-...
    AI_MODEL=gpt-4o-mini

Usage:
    from services.ai_providers import get_ai_provider, AIProvider
    
    provider = get_ai_provider()
    response = await provider.analyze_transcript(transcript)
"""

from .base import AIProvider, AIProviderConfig
from .openai_provider import OpenAIProvider
from .mock_provider import MockAIProvider

__all__ = [
    'AIProvider',
    'AIProviderConfig', 
    'OpenAIProvider',
    'MockAIProvider',
    'get_ai_provider',
]


def get_ai_provider(provider_name: str = None) -> AIProvider:
    """
    Get configured AI provider based on environment or parameter.
    
    Args:
        provider_name: Override provider (openai, anthropic, mock)
    
    Returns:
        Configured AIProvider instance
    """
    import os
    
    provider = provider_name or os.getenv("AI_PROVIDER", "openai")
    
    if provider == "openai":
        return OpenAIProvider()
    elif provider == "mock":
        return MockAIProvider()
    else:
        # Default to OpenAI
        return OpenAIProvider()
