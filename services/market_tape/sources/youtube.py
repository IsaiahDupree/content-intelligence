"""YouTube Data API discovery and high-volume batch statistics."""

from __future__ import annotations

import os
import re
from datetime import timedelta, timezone
from typing import Any, Dict, List, Sequence

from .base import MarketSource, SourceHTTPError
from .counters import first_counter, missing_counter_metadata
from ..models import (
    MarketContent,
    MetricCounters,
    ProviderBatch,
    QueryAttempt,
    SourceState,
    parse_datetime,
    utc_now,
)


ISO_DURATION_RE = re.compile(
    r"P(?:(?P<days>\d+)D)?(?:T(?:(?P<hours>\d+)H)?(?:(?P<minutes>\d+)M)?(?:(?P<seconds>[\d.]+)S)?)?"
)


def duration_seconds(value: str) -> float | None:
    match = ISO_DURATION_RE.fullmatch(value or "")
    if not match:
        return None
    parts = {name: float(number or 0) for name, number in match.groupdict().items()}
    return parts["days"] * 86400 + parts["hours"] * 3600 + parts["minutes"] * 60 + parts["seconds"]


class YouTubeSource(MarketSource):
    source_id = "youtube-data-api-v3"
    platform = "youtube"

    def __init__(self, *args: Any, base_url: str = "https://www.googleapis.com/youtube/v3", **kwargs: Any):
        super().__init__(*args, **kwargs)
        self.base_url = base_url.rstrip("/")
        self.api_key = os.getenv("YOUTUBE_API_KEY", "").strip() or os.getenv("YOUTUBE_DATA_API_KEY", "").strip()

    def credentials_available(self) -> bool:
        return bool(self.api_key)

    def missing_credentials(self) -> List[str]:
        return [] if self.api_key else ["YOUTUBE_API_KEY"]

    def credential_material(self) -> Sequence[str]:
        return (self.api_key,)

    def measurement_refresh_batch_size(self) -> int:
        return 50

    def measurement_request_units_per_batch(self) -> int:
        # The optional batch edge may reject and fall back to /videos, so hold
        # both possible requests until the terminal measurement completes.
        return 2 if self.config.youtube_batch_stats else 1

    def discover_performance(
        self,
        query: str,
        *,
        max_items: int = 25,
        relevance_language: str = "en",
        region: str = "US",
    ) -> ProviderBatch:
        """Discover an auditable, high-view short-video cohort for one query.

        The normal discovery lane prioritizes recency.  Transcript cohorts need a
        separate explicit lane ordered by observed view count so a recent 14-view
        upload can never masquerade as a proven pattern.
        """

        started = utc_now()
        try:
            self.preflight()
            observed = utc_now()
            item_limit = max(1, min(int(max_items), 50))
            search = self.request_json(
                "GET",
                f"{self.base_url}/search",
                params={
                    "part": "snippet",
                    "q": query,
                    "type": "video",
                    "order": "viewCount",
                    "videoDuration": "short",
                    "relevanceLanguage": relevance_language,
                    "regionCode": region,
                    "safeSearch": "moderate",
                    "maxResults": item_limit,
                    "key": self.api_key,
                },
            )
            ids = [
                str(item.get("id", {}).get("videoId") or "")
                for item in search.get("items", [])
                if isinstance(item, dict)
            ]
            ids = [value for value in ids if value]
            details = self.request_json(
                "GET",
                f"{self.base_url}/videos",
                params={
                    "part": "snippet,statistics,contentDetails,topicDetails",
                    "id": ",".join(ids),
                    "key": self.api_key,
                },
            ) if ids else {"items": []}
            items = [
                self._normalize(
                    item,
                    observed,
                    {
                        "lane": "performance_search",
                        "topic": query,
                        "region": region,
                    },
                )
                for item in details.get("items", [])
                if isinstance(item, dict) and item.get("id")
            ]
            batch = self.success_batch(
                started,
                items,
                operation="discover_performance",
                metadata={
                    "query": query,
                    "order": "viewCount",
                    "video_duration": "short",
                    "relevance_language": relevance_language,
                    "region": region,
                    "search_result_count": len(ids),
                },
            )
            batch.query_attempts = [QueryAttempt(
                run_id=self.run_id,
                source_id=self.source_id,
                platform=self.platform,
                query=query,
                attempted_at=started,
                finished_at=batch.receipt.finished_at,
                state="completed" if ids else "empty",
                result_count=len(ids),
                request_count=batch.receipt.request_count,
                metadata={
                    "lane": "performance_search",
                    "order": "viewCount",
                    "query_family": query,
                },
            )]
            return batch
        except Exception as error:
            return self.blocked_batch(started, error)

    def discover(self, max_items: int) -> ProviderBatch:
        started = utc_now()
        try:
            self.preflight()
            observed = utc_now()
            details: Dict[str, Dict[str, Any]] = {}
            contexts: Dict[str, Dict[str, Any]] = {}
            search_requests = 0
            query_request_counts: Dict[str, int] = {}
            query_result_counts: Dict[str, int] = {}
            chart_requests = 0
            chart_category_errors: List[Dict[str, Any]] = []
            known_skipped = 0
            search_requests_used = self.recent_metadata_total("search_requests")
            search_request_limit = max(
                0,
                self.config.youtube_search_daily_limit - search_requests_used,
            )
            termination_error: SourceHTTPError | None = None

            chart_categories = self.config.youtube_chart_categories or ["all"]
            for region in self.config.regions[:4]:
                for category in chart_categories:
                    if len(details) >= max_items or self.request_count >= self.request_budget:
                        break
                    params: Dict[str, Any] = {
                        "part": "snippet,statistics,contentDetails,topicDetails",
                        "chart": "mostPopular",
                        "regionCode": region,
                        "maxResults": min(50, max_items - len(details)),
                        "key": self.api_key,
                    }
                    if category.lower() != "all":
                        params["videoCategoryId"] = category
                    try:
                        data = self.request_json("GET", f"{self.base_url}/videos", params=params)
                    except SourceHTTPError as error:
                        if error.status_code not in {400, 404}:
                            raise
                        chart_category_errors.append({
                            "region": region,
                            "category": category,
                            "status_code": error.status_code,
                            "error_code": error.code,
                        })
                        continue
                    chart_requests += 1
                    chart_items = [
                        item for item in data.get("items", [])
                        if isinstance(item, dict) and item.get("id")
                    ]
                    known = self.known_external_ids([str(item["id"]) for item in chart_items])
                    known_skipped += len(known)
                    for item in chart_items:
                        video_id = str(item["id"])
                        if video_id in known:
                            continue
                        details[video_id] = item
                        contexts[video_id] = {
                            "lane": "most_popular",
                            "region": region,
                            "category": category,
                        }
                if len(details) >= max_items or self.request_count >= self.request_budget:
                    break

            page_tokens = {topic: "" for topic in self.config.topics}
            while (
                page_tokens
                and len(details) < max_items
                and search_requests < search_request_limit
                and self.request_count + 1 < self.request_budget
            ):
                for topic in list(page_tokens):
                    if (
                        len(details) >= max_items
                        or search_requests >= search_request_limit
                        or self.request_count + 1 >= self.request_budget
                    ):
                        break
                    page_token = page_tokens[topic]
                    params: Dict[str, Any] = {
                        "part": "snippet",
                        "q": topic,
                        "type": "video",
                        "order": "date",
                        "maxResults": min(50, max_items - len(details)),
                        "publishedAfter": (observed - timedelta(days=7)).astimezone(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z"),
                        "key": self.api_key,
                    }
                    if page_token:
                        params["pageToken"] = page_token
                    search_requests += 1
                    query_request_counts[topic] = query_request_counts.get(topic, 0) + 1
                    try:
                        search = self.request_json("GET", f"{self.base_url}/search", params=params)
                    except SourceHTTPError as error:
                        if error.status_code not in {403, 429}:
                            raise
                        termination_error = error
                        page_tokens.clear()
                        break
                    ids = [
                        str(item.get("id", {}).get("videoId", ""))
                        for item in search.get("items", []) if isinstance(item, dict)
                    ]
                    ids = [video_id for video_id in ids if video_id and video_id not in details]
                    query_result_counts[topic] = query_result_counts.get(topic, 0) + len(ids)
                    known = self.known_external_ids(ids)
                    known_skipped += len(known)
                    ids = [video_id for video_id in ids if video_id not in known]
                    if ids and self.request_count < self.request_budget:
                        batch = self.request_json("GET", f"{self.base_url}/videos", params={
                            "part": "snippet,statistics,contentDetails,topicDetails",
                            "id": ",".join(ids[:50]),
                            "key": self.api_key,
                        })
                        for item in batch.get("items", []):
                            if isinstance(item, dict) and item.get("id"):
                                video_id = str(item["id"])
                                details[video_id] = item
                                contexts[video_id] = {"lane": "search", "topic": topic}
                    next_token = str(search.get("nextPageToken", ""))
                    if next_token:
                        page_tokens[topic] = next_token
                    else:
                        page_tokens.pop(topic, None)

            items = [
                self._normalize(item, observed, contexts.get(video_id, {"lane": "discovery"}))
                for video_id, item in details.items()
            ]
            batch = self.success_batch(
                started, items[:max_items], operation="discover",
                metadata={
                    "chart_requests": chart_requests,
                    "chart_categories": chart_categories,
                    "chart_regions": self.config.regions[:4],
                    "chart_category_errors": chart_category_errors,
                    "search_requests": search_requests,
                    "search_requests_used_before_run": search_requests_used,
                    "search_quota_remaining": max(
                        0,
                        self.config.youtube_search_daily_limit
                        - search_requests_used
                        - search_requests,
                    ),
                    "known_ids_skipped": known_skipped,
                    "queries_considered": self.config.topics,
                    "batch_stats_supported": True,
                    "terminated_by": (
                        termination_error.code
                        if termination_error
                        else (
                            "search_daily_limit"
                            if search_requests >= search_request_limit
                            else "item_or_request_limit"
                        )
                    ),
                },
            )
            if termination_error:
                batch.receipt.metadata["search_lane_state"] = SourceState.BLOCKED_QUOTA.value
                batch.receipt.metadata["search_lane_error_code"] = termination_error.code
                batch.receipt.metadata["search_lane_error_detail"] = str(termination_error)
            batch.query_attempts = [
                QueryAttempt(
                    run_id=self.run_id,
                    source_id=self.source_id,
                    platform=self.platform,
                    query=topic,
                    attempted_at=started,
                    finished_at=batch.receipt.finished_at,
                    state="completed" if query_result_counts.get(topic, 0) else "empty",
                    result_count=query_result_counts.get(topic, 0),
                    request_count=request_count,
                    metadata={
                        "lane": "recent_search",
                        "order": "date",
                        "query_family": topic,
                    },
                )
                for topic, request_count in query_request_counts.items()
            ]
            return batch
        except Exception as error:
            return self.blocked_batch(started, error)

    def refresh(self, tracked: Sequence[Dict[str, Any]]) -> ProviderBatch:
        started = utc_now()
        try:
            self.preflight()
            observed = utc_now()
            output: List[MarketContent] = []
            missing_counter_count = 0
            for offset in range(0, len(tracked), 50):
                if self.request_count >= self.request_budget:
                    break
                batch_rows = tracked[offset:offset + 50]
                ids = [str(row["external_id"]) for row in batch_rows]
                by_id = {str(row["external_id"]): row for row in batch_rows}
                data: Dict[str, Any]
                if self.config.youtube_batch_stats:
                    try:
                        data = self.request_json("GET", f"{self.base_url}/videos:batchGetStats", params={
                            "part": "id,snippet,statistics,contentDetails",
                            "id": ",".join(ids),
                            "key": self.api_key,
                        })
                    except SourceHTTPError as error:
                        if error.status_code not in {400, 404} or self.request_count >= self.request_budget:
                            raise
                        data = self.request_json("GET", f"{self.base_url}/videos", params={
                            "part": "snippet,statistics,contentDetails",
                            "id": ",".join(ids),
                            "key": self.api_key,
                        })
                else:
                    data = self.request_json("GET", f"{self.base_url}/videos", params={
                        "part": "snippet,statistics,contentDetails",
                        "id": ",".join(ids),
                        "key": self.api_key,
                    })
                for raw in data.get("items", []):
                    if not isinstance(raw, dict) or not raw.get("id"):
                        continue
                    statistics = raw.get("statistics", {}) or {}
                    if (
                        not isinstance(statistics, dict)
                        or first_counter(statistics.get("viewCount")) is None
                    ):
                        missing_counter_count += 1
                        continue
                    output.append(self._normalize(raw, observed, by_id.get(str(raw["id"]), {})))
            return self.success_batch(
                started,
                output,
                operation="refresh",
                metadata=missing_counter_metadata(
                    missing_counter_count,
                    "statistics.viewCount",
                ),
            )
        except Exception as error:
            return self.blocked_batch(started, error)

    def _normalize(self, raw: Dict[str, Any], observed: Any, prior: Dict[str, Any]) -> MarketContent:
        snippet = raw.get("snippet", {}) or {}
        statistics = raw.get("statistics", {}) or {}
        content = raw.get("contentDetails", {}) or {}
        video_id = str(raw.get("id"))
        channel_id = str(snippet.get("channelId") or prior.get("creator_external_id") or "unknown")
        duration = content.get("durationMillis")
        try:
            seconds = float(duration) / 1000 if duration is not None else duration_seconds(str(content.get("duration", "")))
        except (TypeError, ValueError):
            seconds = duration_seconds(str(content.get("duration", "")))
        title = str(snippet.get("title") or prior.get("title") or "")
        description = str(snippet.get("description") or prior.get("description") or "")
        tags = [str(tag).lower() for tag in snippet.get("tags", []) if str(tag).strip()]
        return MarketContent(
            platform=self.platform,
            external_id=video_id,
            creator_external_id=channel_id,
            creator_handle=str(prior.get("creator_handle") or ""),
            creator_name=str(snippet.get("channelTitle") or prior.get("creator_name") or ""),
            published_at=parse_datetime(snippet.get("publishedAt") or snippet.get("publishTime") or prior.get("published_at")),
            observed_at=observed,
            source_id=self.source_id,
            metrics=MetricCounters.from_values(
                views=first_counter(statistics.get("viewCount")),
                likes=first_counter(statistics.get("likeCount")),
                comments=first_counter(statistics.get("commentCount")),
                shares=0,
                saves=0,
            ),
            title=title,
            description=description,
            language=str(snippet.get("defaultLanguage") or snippet.get("defaultAudioLanguage") or prior.get("language") or ""),
            url=f"https://www.youtube.com/watch?v={video_id}",
            thumbnail_url=str((snippet.get("thumbnails", {}).get("high") or snippet.get("thumbnails", {}).get("default") or {}).get("url", "")),
            duration_seconds=seconds,
            hashtags=tags,
            raw_payload=raw,
            discovery_context={
                key: value for key, value in prior.items()
                if key in {"lane", "topic", "region", "category"}
            },
        )
