#!/usr/bin/env python3
"""Extract real visual genome features for the transcript cohort (MT-007)."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPOSITORY_ROOT))

from services.content_quality.visual_bank import VisualBank  # noqa: E402

DEFAULT_TAPE = (Path.home()
                / "Library/Application Support/ContentIntelligence/data/market-tape.sqlite3")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--tape", type=Path, default=DEFAULT_TAPE)
    parser.add_argument("--limit", type=int, default=5)
    parser.add_argument("--platform", default="youtube")
    args = parser.parse_args()
    print(json.dumps(VisualBank(args.tape).extract_cohort(args.limit, args.platform), indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
