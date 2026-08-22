"""Evidence-weighted keyword discovery from the market tape itself."""

from __future__ import annotations

import json
import math
import re
import statistics
from collections import defaultdict
from datetime import datetime, timezone
from typing import Any, Dict, Iterable, List, Mapping, Sequence, Tuple

from .models import parse_datetime


TOKEN_RE = re.compile(r"[^\W_][\w'+-]*", re.UNICODE)
HASHTAG_RE = re.compile(r"#([^\W_][\w-]*)", re.UNICODE)

STOP_WORDS = {
    "a", "about", "after", "all", "also", "am", "an", "and", "are", "as", "at", "be",
    "been", "before", "but", "by", "can", "did", "do", "does", "for", "from", "get", "got",
    "had", "has", "have", "he", "her", "here", "hers", "him", "his", "how", "i", "if", "in",
    "into", "is", "it", "its", "just", "me", "more", "my", "new", "no", "not", "now", "of",
    "off", "on", "one", "or", "our", "out", "over", "she", "so", "some", "than", "that", "the",
    "their", "them", "then", "there", "these", "they", "this", "those", "to", "too", "up", "us",
    "was", "we", "were", "what", "when", "where", "which", "who", "why", "will", "with", "you",
    "your",
}

DISCOVERY_NOISE = {
    "fyp", "foryou", "foryoupage", "viral", "trending", "trend", "short", "shorts", "reel", "reels",
    "tiktok", "youtube", "instagram", "facebook", "twitter", "threads", "video", "videos", "watch",
    "follow", "like", "subscribe", "part", "episode", "official", "full", "today", "2025", "2026",
    "http", "https", "www", "com", "amp", "use", "using", "used", "most", "every", "really",
    "still", "thing", "things", "make", "made", "much", "first", "best", "want", "way", "don",
}


def rank_keywords(
    rows: Sequence[Mapping[str, Any]],
    *,
    window_hours: int = 168,
    min_videos: int = 1,
    limit: int = 100,
    now: datetime | None = None,
    candidate_mode: str = "all",
) -> List[Dict[str, Any]]:
    """Rank query terms by fresh performance, breadth, and repeat-observation evidence."""

    if candidate_mode not in {"all", "queries"}:
        raise ValueError("candidate_mode must be all or queries")
    now = now or datetime.now(timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    horizon = max(1, int(window_hours))
    minimum = max(1, int(min_videos))
    accumulators: Dict[str, Dict[str, Any]] = defaultdict(_new_accumulator)

    for row in rows:
        published = parse_datetime(row.get("published_at"))
        observed = parse_datetime(row.get("observed_at")) or now
        if not published:
            continue
        age_at_observation_hours = (observed - published).total_seconds() / 3600.0
        age_now_hours = (now - published).total_seconds() / 3600.0
        observation_age_hours = max(
            0.0,
            (now - observed).total_seconds() / 3600.0,
        )
        if (
            age_at_observation_hours < 0
            or age_now_hours < 0
            or age_now_hours > horizon
            or observation_age_hours > horizon
        ):
            continue
        views = max(0, int(row.get("views") or 0))
        likes = max(0, int(row.get("likes") or 0))
        comments = max(0, int(row.get("comments") or 0))
        shares = max(0, int(row.get("shares") or 0))
        if views == 0 and likes == 0 and comments == 0 and shares == 0:
            continue

        hashtags = _json_list(row.get("hashtags_json"))
        text = " ".join(str(row.get(field) or "") for field in ("title", "caption")).strip()
        if not text:
            text = str(row.get("description") or "")
        discovery_queries = _json_list(row.get("discovery_queries_json"))
        query_candidates = [
            (normalized, "query")
            for query in discovery_queries
            if (normalized := _normalize(query)) and _usable(normalized)
        ]
        candidates = list(query_candidates)
        if candidate_mode == "all":
            candidates.extend(extract_candidates(text, hashtags))
        candidates = list(dict.fromkeys(candidates))
        if not candidates:
            continue

        implied_views_per_hour = views / max(1.0, age_at_observation_hours)
        actual_log_velocity = max(0.0, float(row.get("view_velocity") or 0.0))
        decay_hours = max(12.0, horizon / 3.0)
        content_freshness = math.exp(-age_now_hours / decay_hours)
        observation_freshness = math.exp(-observation_age_hours / decay_hours)
        freshness = content_freshness * observation_freshness
        engagement_rate = min(0.5, (likes + comments + shares) / max(1, views))
        contribution = freshness * (
            math.log1p(views)
            + 0.9 * math.log1p(implied_views_per_hour)
            + 2.5 * actual_log_velocity
            + 4.0 * engagement_rate
        )
        video_id = str(row.get("video_id") or "")
        creator_id = str(row.get("creator_id") or "")
        platform = str(row.get("platform") or "")
        repeated = int(row.get("observation_count") or 0) >= 2
        example = {
            "video_id": video_id,
            "platform": platform,
            "title": str(row.get("title") or row.get("caption") or "")[:240],
            "views": views,
            "age_hours_at_observation": round(age_at_observation_hours, 3),
            "age_hours_now": round(age_now_hours, 3),
            "observed_at": observed.astimezone(timezone.utc).isoformat(),
            "observation_age_hours": round(observation_age_hours, 3),
            "observation_freshness": round(observation_freshness, 6),
            "implied_views_per_hour": round(implied_views_per_hour, 3),
            "url": str(row.get("url") or ""),
            "contribution": round(contribution, 6),
        }

        for keyword, keyword_type in candidates:
            aggregate = accumulators[keyword]
            if video_id in aggregate["videos"]:
                continue
            aggregate["keyword_type"] = _preferred_type(aggregate["keyword_type"], keyword_type)
            aggregate["videos"].add(video_id)
            aggregate["creators"].add(creator_id)
            aggregate["platforms"].add(platform)
            aggregate["repeated_videos"] += int(repeated)
            aggregate["views_total"] += views
            aggregate["engagement_total"] += likes + comments + shares
            aggregate["rates"].append(implied_views_per_hour)
            aggregate["velocities"].append(actual_log_velocity)
            aggregate["freshness"].append(freshness)
            aggregate["observation_freshness"].append(observation_freshness)
            aggregate["observation_ages"].append(observation_age_hours)
            aggregate["observed_at"].append(observed)
            aggregate["contributions"].append(contribution)
            aggregate["examples"].append(example)

    ranked: List[Dict[str, Any]] = []
    for keyword, aggregate in accumulators.items():
        videos = len(aggregate["videos"])
        if videos < minimum:
            continue
        creators = len(aggregate["creators"])
        platforms = len(aggregate["platforms"])
        contributions = sorted(aggregate["contributions"], reverse=True)
        performance = statistics.fmean(contributions[: min(5, len(contributions))])
        median_rate = statistics.median(aggregate["rates"])
        p75_rate = _percentile(aggregate["rates"], 0.75)
        concentration = max(
            (example["views"] for example in aggregate["examples"]), default=0
        ) / max(1, aggregate["views_total"])
        raw_score = (
            performance
            + 1.25 * math.log1p(median_rate)
            + 0.75 * math.log1p(p75_rate)
            + 0.75 * math.log1p(aggregate["views_total"])
            + 0.75 * math.log1p(min(videos, 20))
            + 0.50 * math.log1p(min(creators, 20))
            + 0.35 * math.log1p(platforms)
            - max(0.0, concentration - 0.85) * 3.0
        )
        confidence = min(1.0, (
            0.40 * min(1.0, videos / 8.0)
            + 0.25 * min(1.0, creators / 6.0)
            + 0.15 * min(1.0, platforms / 3.0)
            + 0.20 * min(1.0, aggregate["repeated_videos"] / 3.0)
        ))
        examples = sorted(
            aggregate["examples"], key=lambda value: value["contribution"], reverse=True
        )[:3]
        ranked.append({
            "keyword": keyword,
            "keyword_type": aggregate["keyword_type"],
            "videos_total": videos,
            "creators_total": creators,
            "platforms": sorted(aggregate["platforms"]),
            "platforms_total": platforms,
            "repeated_videos": aggregate["repeated_videos"],
            "views_total": aggregate["views_total"],
            "engagement_total": aggregate["engagement_total"],
            "median_implied_views_per_hour": round(median_rate, 3),
            "p75_implied_views_per_hour": round(p75_rate, 3),
            "max_implied_views_per_hour": round(max(aggregate["rates"], default=0.0), 3),
            "median_log_velocity": round(statistics.median(aggregate["velocities"]), 6),
            "freshness": round(statistics.fmean(aggregate["freshness"]), 6),
            "observation_freshness": round(
                statistics.fmean(aggregate["observation_freshness"]),
                6,
            ),
            "median_observation_age_hours": round(
                statistics.median(aggregate["observation_ages"]),
                3,
            ),
            "latest_observed_at": max(aggregate["observed_at"]).astimezone(
                timezone.utc
            ).isoformat(),
            "top1_view_concentration": round(concentration, 6),
            "confidence": round(confidence, 6),
            "raw_score": round(raw_score, 6),
            "query_ready": videos >= max(2, minimum) and creators >= 2,
            "examples": examples,
        })

    type_priority = {"keyword": 0, "hashtag": 1, "phrase": 2, "query": 3}
    ranked.sort(
        key=lambda value: (
            value["raw_score"],
            value["confidence"],
            type_priority.get(value["keyword_type"], 0),
            len(value["keyword"].split()),
        ),
        reverse=True,
    )
    maximum = ranked[0]["raw_score"] if ranked else 0.0
    for index, signal in enumerate(ranked, start=1):
        signal["rank"] = index
        signal["score"] = round(100.0 * signal["raw_score"] / maximum, 4) if maximum > 0 else 0.0
    return ranked[: max(1, min(1000, int(limit)))]


def extract_candidates(text: str, hashtags: Iterable[str] = ()) -> List[Tuple[str, str]]:
    candidates: List[Tuple[str, str]] = []
    for hashtag in [*hashtags, *HASHTAG_RE.findall(text or "")]:
        normalized = _normalize(str(hashtag).lstrip("#"))
        if _usable(normalized):
            candidates.append((normalized, "hashtag"))

    words = [
        normalized
        for token in TOKEN_RE.findall(text or "")[:32]
        if (normalized := _normalize(token)) and _usable(normalized)
    ]
    candidates.extend((word, "keyword") for word in words[:16])
    candidates.extend((f"{first} {second}", "phrase") for first, second in zip(words[:12], words[1:13]))
    candidates.extend(
        (f"{first} {second} {third}", "phrase")
        for first, second, third in zip(words[:8], words[1:9], words[2:10])
    )
    return list(dict.fromkeys(candidates))[:40]


def _normalize(value: str) -> str:
    return " ".join(TOKEN_RE.findall(value.lower().strip()))[:80]


def _usable(value: str) -> bool:
    if not value or len(value) < 3 or value.isdigit():
        return False
    return value not in STOP_WORDS and value not in DISCOVERY_NOISE


def _json_list(value: Any) -> List[str]:
    if isinstance(value, list):
        return [str(item) for item in value]
    try:
        parsed = json.loads(str(value or "[]"))
    except (TypeError, ValueError, json.JSONDecodeError):
        return []
    return [str(item) for item in parsed] if isinstance(parsed, list) else []


def _new_accumulator() -> Dict[str, Any]:
    return {
        "keyword_type": "keyword",
        "videos": set(),
        "creators": set(),
        "platforms": set(),
        "repeated_videos": 0,
        "views_total": 0,
        "engagement_total": 0,
        "rates": [],
        "velocities": [],
        "freshness": [],
        "observation_freshness": [],
        "observation_ages": [],
        "observed_at": [],
        "contributions": [],
        "examples": [],
    }


def _preferred_type(current: str, incoming: str) -> str:
    priority = {"keyword": 0, "phrase": 1, "hashtag": 2, "query": 3}
    return incoming if priority.get(incoming, 0) > priority.get(current, 0) else current


def _percentile(values: Sequence[float], fraction: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(float(value) for value in values)
    position = max(0.0, min(1.0, fraction)) * (len(ordered) - 1)
    lower = int(math.floor(position))
    upper = int(math.ceil(position))
    if lower == upper:
        return ordered[lower]
    return ordered[lower] + (ordered[upper] - ordered[lower]) * (position - lower)
