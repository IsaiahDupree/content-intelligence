"""
Trend Flash Package
Real-time trend detection → video generation pipeline.
"""

from .trend_radar import (
    TrendRadar,
    TrendCluster,
    get_trend_radar,
    INTENT_KEYWORDS
)

from .flash_generator import (
    FlashGenerator,
    TrendFlashContent,
    get_flash_generator,
    SCRIPT_TEMPLATES
)

from .remotion_shipper import (
    RemotionShipper,
    RenderJob,
    get_remotion_shipper
)

__all__ = [
    # Radar
    "TrendRadar",
    "TrendCluster",
    "get_trend_radar",
    "INTENT_KEYWORDS",
    
    # Generator
    "FlashGenerator",
    "TrendFlashContent",
    "get_flash_generator",
    "SCRIPT_TEMPLATES",
    
    # Shipper
    "RemotionShipper",
    "RenderJob",
    "get_remotion_shipper"
]
