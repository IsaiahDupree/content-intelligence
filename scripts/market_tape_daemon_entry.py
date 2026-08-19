#!/usr/bin/env python3
"""File-based launchd entrypoint that avoids Python module-path startup stalls."""

from __future__ import annotations

import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from services.market_tape.config import MarketTapeConfig  # noqa: E402
from services.market_tape.daemon import MarketTapeDaemon  # noqa: E402


if __name__ == "__main__":
    MarketTapeDaemon(MarketTapeConfig.from_environment()).run()
