"""Time-series calculations for cumulative social counters."""

from __future__ import annotations

import math
import statistics
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Dict, Iterable, List, Mapping, Optional, Sequence


@dataclass(frozen=True)
class Motion:
    velocity: float = 0.0
    acceleration: float = 0.0
    jerk: float = 0.0


def log_velocity(previous_value: int, current_value: int, elapsed_seconds: float) -> float:
    """Return d(log(counter + 1))/dt in hours."""
    if elapsed_seconds <= 0:
        return 0.0
    elapsed_hours = elapsed_seconds / 3600.0
    return (math.log(max(0, current_value) + 1) - math.log(max(0, previous_value) + 1)) / elapsed_hours


def counter_motion(observations: Sequence[Mapping[str, object]], field: str = "views") -> Motion:
    """Calculate velocity, acceleration and jerk from the latest four observations."""
    if len(observations) < 2:
        return Motion()
    ordered = sorted(observations, key=lambda row: str(row["observed_at"]))[-4:]
    velocities: List[tuple[datetime, float]] = []
    for before, after in zip(ordered, ordered[1:]):
        t0 = _as_datetime(before["observed_at"])
        t1 = _as_datetime(after["observed_at"])
        velocities.append((t1, log_velocity(int(before.get(field, 0)), int(after.get(field, 0)), (t1 - t0).total_seconds())))
    accelerations: List[tuple[datetime, float]] = []
    for before, after in zip(velocities, velocities[1:]):
        hours = (after[0] - before[0]).total_seconds() / 3600.0
        accelerations.append((after[0], (after[1] - before[1]) / hours if hours > 0 else 0.0))
    jerks: List[float] = []
    for before, after in zip(accelerations, accelerations[1:]):
        hours = (after[0] - before[0]).total_seconds() / 3600.0
        jerks.append((after[1] - before[1]) / hours if hours > 0 else 0.0)
    return Motion(
        velocity=velocities[-1][1] if velocities else 0.0,
        acceleration=accelerations[-1][1] if accelerations else 0.0,
        jerk=jerks[-1] if jerks else 0.0,
    )


def zscore(value: float, cohort: Iterable[float]) -> float:
    values = [float(item) for item in cohort]
    if len(values) < 2:
        return 0.0
    deviation = statistics.pstdev(values)
    if deviation == 0:
        return 0.0
    return (value - statistics.mean(values)) / deviation


def age_bucket(published_at: Optional[datetime], now: Optional[datetime] = None) -> str:
    if not published_at:
        return "unknown"
    now = now or datetime.now(timezone.utc)
    if published_at.tzinfo is None:
        published_at = published_at.replace(tzinfo=timezone.utc)
    seconds = max(0.0, (now - published_at).total_seconds())
    limits = [
        (300, "t+0-5m"), (900, "t+5-15m"), (1800, "t+15-30m"),
        (3600, "t+30-60m"), (21600, "t+1-6h"), (43200, "t+6-12h"),
        (86400, "t+12-24h"), (259200, "t+1-3d"), (604800, "t+3-7d"),
        (2592000, "t+7-30d"),
    ]
    for limit, label in limits:
        if seconds <= limit:
            return label
    return "t+30d+"


def poll_interval_seconds(age_seconds: float, hot_mode: bool = False) -> int:
    if age_seconds <= 3600:
        interval = 300
    elif age_seconds <= 21600:
        interval = 900
    elif age_seconds <= 86400:
        interval = 3600
    elif age_seconds <= 604800:
        interval = 21600
    elif age_seconds <= 2592000:
        interval = 86400
    else:
        interval = 604800
    return max(60, interval // 3) if hot_mode else interval


def concentration(values: Iterable[int], top_n: int = 1) -> float:
    ordered = sorted((max(0, int(value)) for value in values), reverse=True)
    total = sum(ordered)
    return sum(ordered[:top_n]) / total if total else 0.0


def trend_state(momentum: float, acceleration: float, saturation: float, creators: int, new_creators: int) -> str:
    if creators == 0:
        return "dead"
    if momentum < -1.0:
        return "declining"
    if saturation >= 0.85 and acceleration <= 0:
        return "saturating"
    if momentum >= 3.0 and acceleration > 0 and creators >= 10:
        return "breakout"
    if momentum >= 1.5 and new_creators > 0:
        return "expanding" if creators >= 25 else "emerging"
    if creators <= 3:
        return "discovering"
    return "emerging" if acceleration > 0 else "recurring"


def trend_strength(components: Dict[str, float]) -> float:
    weights = {
        "relative_view_velocity": 0.25,
        "acceleration": 0.15,
        "creator_adoption_velocity": 0.15,
        "creator_breadth": 0.10,
        "share_velocity": 0.10,
        "cross_platform_diffusion": 0.10,
        "engagement_quality": 0.05,
        "novelty": 0.05,
        "persistence": 0.05,
    }
    normalized = sum(weights[name] * max(0.0, min(1.0, float(components.get(name, 0.0)))) for name in weights)
    return round(normalized * 100.0, 4)


def _as_datetime(value: object) -> datetime:
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
