"""Clock-aware keyword ranking contracts for archived Market Tape rows."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

from services.market_tape.keywords import rank_keywords


def _row(
    *,
    video_id: str,
    query: str,
    published_at: datetime,
    observed_at: datetime,
) -> dict[str, object]:
    return {
        "video_id": video_id,
        "creator_id": f"creator-{video_id}",
        "platform": "youtube",
        "published_at": published_at.isoformat(),
        "observed_at": observed_at.isoformat(),
        "title": "Evidence row",
        "caption": "",
        "description": "",
        "url": f"https://www.youtube.com/watch?v={video_id}",
        "views": 100_000,
        "likes": 5_000,
        "comments": 500,
        "shares": 250,
        "view_velocity": 0.5,
        "hashtags_json": "[]",
        "observation_count": 2,
        "discovery_queries_json": json.dumps([query]),
    }


def test_observation_older_than_window_cannot_masquerade_as_fresh_keyword():
    now = datetime(2026, 8, 22, 15, 0, tzinfo=timezone.utc)
    stale_observed_at = now - timedelta(days=8)
    rows = [
        _row(
            video_id="stale-1",
            query="stale archived demand",
            published_at=stale_observed_at - timedelta(hours=1),
            observed_at=stale_observed_at,
        )
    ]

    ranked = rank_keywords(
        rows,
        now=now,
        window_hours=168,
        candidate_mode="queries",
    )

    assert ranked == []


def test_equal_metrics_rank_current_observation_above_stale_observation():
    now = datetime(2026, 8, 22, 15, 0, tzinfo=timezone.utc)
    rows = [
        _row(
            video_id="older-1",
            query="older demand signal",
            published_at=now - timedelta(hours=49),
            observed_at=now - timedelta(hours=48),
        ),
        _row(
            video_id="current-1",
            query="current demand signal",
            published_at=now - timedelta(hours=1),
            observed_at=now,
        ),
    ]

    ranked = rank_keywords(
        rows,
        now=now,
        window_hours=168,
        candidate_mode="queries",
    )

    assert [row["keyword"] for row in ranked] == [
        "current demand signal",
        "older demand signal",
    ]
    current, older = ranked
    assert current["observation_freshness"] == 1.0
    assert current["median_observation_age_hours"] == 0.0
    assert older["observation_freshness"] < current["observation_freshness"]
    assert older["median_observation_age_hours"] == 48.0
    assert current["examples"][0]["observed_at"] == now.isoformat()

