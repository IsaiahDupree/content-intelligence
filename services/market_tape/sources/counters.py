"""Fail-closed helpers for cumulative provider counter contracts."""

from __future__ import annotations

import re
from typing import Any, Dict, Optional


INTEGER_COUNTER_RE = re.compile(r"^[0-9]+$")


def first_counter(*values: Any) -> Optional[int]:
    """Return the first explicit non-negative integer counter, preserving zero."""

    for value in values:
        if isinstance(value, bool) or value is None:
            continue
        if isinstance(value, int):
            if value >= 0:
                return value
            continue
        if isinstance(value, float):
            if value >= 0 and value.is_integer():
                return int(value)
            continue
        if isinstance(value, str):
            text = value.strip()
            if INTEGER_COUNTER_RE.fullmatch(text):
                return int(text)
    return None


def missing_counter_metadata(
    count: int,
    authoritative_metric: str,
) -> Dict[str, Any]:
    """Build the shared receipt proof for suppressed metadata-only refreshes."""

    missing = max(0, int(count))
    return {
        "authoritative_metric": authoritative_metric,
        "metadata_only_count": missing,
        "missing_counter_count": missing,
        "item_failure_code": (
            "engagement_metrics_unavailable" if missing else ""
        ),
    }
