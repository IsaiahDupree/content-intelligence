"""Low-cost adapter for the existing Safari research archive and scheduler."""

from __future__ import annotations

import json
import math
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Set

from .base import MarketSource, sanitize
from ..models import MarketContent, MetricCounters, ProviderBatch, SourceState, parse_datetime, stable_hash, utc_now


ID_PATTERNS = {
    "x": re.compile(r"/(?:status)/(\d+)", re.IGNORECASE),
    "threads": re.compile(r"/(?:post)/([A-Za-z0-9_-]+)", re.IGNORECASE),
    "tiktok": re.compile(r"/(?:video)/(\d+)", re.IGNORECASE),
    "instagram": re.compile(r"/(?:reel|reels|p)/([A-Za-z0-9_-]+)", re.IGNORECASE),
    "facebook": re.compile(r"/(?:videos|reel|posts)/([A-Za-z0-9._-]+)", re.IGNORECASE),
}


class LocalResearchSource(MarketSource):
    """Consume structured browser-research receipts and schedule their refresh."""

    metered = False

    def __init__(
        self,
        *args: Any,
        platform: str,
        api_platform: Optional[str] = None,
        base_url: Optional[str] = None,
        archive_root: Optional[Path] = None,
        **kwargs: Any,
    ):
        super().__init__(*args, **kwargs)
        self.platform = platform
        self.api_platform = api_platform or platform
        self.source_id = f"safari-local-research-{platform}"
        self.base_url = (base_url or os.getenv("MARKET_RESEARCH_URL", "http://127.0.0.1:3106")).rstrip("/")
        self.archive_root = archive_root or self.config.local_research_dir

    @property
    def platform_archive_dir(self) -> Path:
        return self.archive_root / self.api_platform

    def credentials_available(self) -> bool:
        return self.platform_archive_dir.is_dir() or self.base_url.startswith("http://127.0.0.1")

    def missing_credentials(self) -> List[str]:
        return [] if self.credentials_available() else ["local Safari research archive or service"]

    def discover(self, max_items: int) -> ProviderBatch:
        started = utc_now()
        try:
            self.preflight()
            items = self._load_archive(max_items)
            scheduler = self._schedule_if_due(max_items)
            batch = self.success_batch(
                started,
                items,
                operation="discover",
                metadata={
                    "archive_dir": str(self.platform_archive_dir),
                    "scheduler": scheduler,
                    "provider_cost_usd": 0.0,
                },
            )
            if scheduler.get("state") == "unavailable" and items:
                batch.receipt.state = SourceState.DEGRADED
                batch.receipt.error_code = "scheduler_unavailable"
                batch.receipt.error_detail = str(scheduler.get("error", ""))[:1000]
            return batch
        except Exception as error:
            return self.blocked_batch(started, error)

    def refresh(self, tracked: Sequence[Dict[str, Any]]) -> ProviderBatch:
        started = utc_now()
        try:
            self.preflight()
            wanted = {str(row["external_id"]) for row in tracked}
            items = self._load_archive(max(len(wanted) * 4, len(wanted)), wanted=wanted)
            return self.success_batch(
                started,
                items,
                operation="refresh",
                metadata={"archive_dir": str(self.platform_archive_dir), "provider_cost_usd": 0.0},
            )
        except Exception as error:
            return self.blocked_batch(started, error)

    def _load_archive(self, max_items: int, wanted: Optional[Set[str]] = None) -> List[MarketContent]:
        if not self.platform_archive_dir.is_dir() or max_items <= 0:
            return []
        files = sorted(
            self.platform_archive_dir.glob("*.json"),
            key=lambda path: path.stat().st_mtime,
            reverse=True,
        )
        output: Dict[str, MarketContent] = {}
        for path in files:
            if len(output) >= max_items or (wanted and wanted.issubset(output)):
                break
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            fallback_observed = datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc)
            for raw, context in _records(payload):
                item = self._normalize(raw, context, path, fallback_observed)
                if item is None or (wanted and item.external_id not in wanted):
                    continue
                existing = output.get(item.external_id)
                if existing is None or item.observed_at > existing.observed_at:
                    output[item.external_id] = item
                if len(output) >= max_items:
                    break
        return list(output.values())[:max_items]

    def _normalize(
        self,
        raw: Dict[str, Any],
        context: Dict[str, Any],
        path: Path,
        fallback_observed: datetime,
    ) -> Optional[MarketContent]:
        url = str(raw.get("url") or raw.get("postUrl") or raw.get("videoUrl") or "").strip()
        external_id = str(raw.get("id") or raw.get("video_id") or raw.get("shortcode") or "").strip()
        if not external_id and url:
            match = ID_PATTERNS[self.platform].search(url)
            external_id = match.group(1) if match else f"url-{stable_hash(url)[:24]}"
        if not external_id:
            return None
        author = str(
            raw.get("author") or raw.get("username") or raw.get("creator_handle") or "unknown"
        ).strip().lstrip("@") or "unknown"
        text = str(
            raw.get("description") or raw.get("caption") or raw.get("text") or raw.get("title") or ""
        )
        observed = parse_datetime(
            raw.get("collectedAt") or raw.get("collected_at") or context.get("collectionFinished")
        ) or fallback_observed
        hashtags = raw.get("hashtags") if isinstance(raw.get("hashtags"), list) else re.findall(r"#([\w-]+)", text)
        media_type = "video" if self.platform in {"tiktok", "instagram"} else (
            "video" if raw.get("hasMedia") or raw.get("has_video") else "post"
        )
        return MarketContent(
            platform=self.platform,
            external_id=external_id,
            creator_external_id=str(raw.get("authorId") or raw.get("creator_id") or author),
            creator_handle=author,
            creator_name=str(raw.get("authorDisplayName") or raw.get("display_name") or ""),
            creator_followers=_count(raw.get("followers") or raw.get("follower_count")),
            published_at=parse_datetime(raw.get("timestamp") or raw.get("published_at") or raw.get("created_at")),
            observed_at=observed,
            source_id=self.source_id,
            metrics=MetricCounters.from_values(
                views=raw.get("views") or raw.get("view_count"),
                likes=raw.get("likes") or raw.get("like_count") or raw.get("reactions"),
                comments=raw.get("comments") or raw.get("comment_count") or raw.get("replies"),
                shares=raw.get("shares") or raw.get("share_count") or raw.get("retweets") or raw.get("reposts"),
                saves=raw.get("saves") or raw.get("bookmarks"),
            ),
            title=str(raw.get("title") or ""),
            caption=text,
            language=str(raw.get("language") or ""),
            url=url,
            thumbnail_url=str(raw.get("thumbnail") or raw.get("thumbnail_url") or ""),
            media_type=media_type,
            duration_seconds=raw.get("duration") or raw.get("duration_seconds"),
            hashtags=list(hashtags or []),
            audio_id=str(raw.get("soundId") or raw.get("sound_id") or ""),
            audio_title=str(raw.get("sound") or raw.get("sound_name") or ""),
            raw_payload={**raw, "_market_tape_source_file": path.name},
            discovery_context={
                "niche": raw.get("niche") or context.get("niche"),
                "query": context.get("query"),
                "source_file": path.name,
            },
        )

    def _schedule_if_due(self, max_items: int) -> Dict[str, Any]:
        if not self.config.local_research_trigger_enabled:
            return {"state": "disabled"}
        latest = max(
            (path.stat().st_mtime for path in self.platform_archive_dir.glob("*.json")),
            default=0.0,
        ) if self.platform_archive_dir.is_dir() else 0.0
        age_seconds = max(0.0, utc_now().timestamp() - latest) if latest else None
        if age_seconds is not None and age_seconds < self.config.local_research_refresh_seconds:
            return {"state": "fresh", "latest_age_seconds": round(age_seconds, 3)}
        try:
            headers = _research_headers()
            self.request_json("GET", f"{self.base_url}/health", headers=headers)
            status = self.request_json("GET", f"{self.base_url}/api/research/status", headers=headers)
            current = status.get("currentJob") or {}
            if current.get("status") in {"queued", "running"}:
                return {"state": "already_running", "job_id": current.get("id")}
            niches = self.config.topics
            posts_per_niche = max(20, math.ceil(max_items / max(1, len(niches))))
            cross_platform = self.platform == "tiktok"
            endpoint = "/api/research/all/full" if cross_platform else f"/api/research/{self.api_platform}/full"
            body: Dict[str, Any] = {
                "niches": niches,
                "config": {
                    "postsPerNiche": posts_per_niche,
                    "creatorsPerNiche": min(100, posts_per_niche),
                },
            }
            if cross_platform:
                body["platforms"] = ["tiktok", "instagram", "twitter", "facebook", "threads"]
            job = self.request_json(
                "POST",
                f"{self.base_url}{endpoint}",
                headers=headers,
                json_body=body,
            )
            return {
                "state": "triggered_all" if cross_platform else "triggered",
                "job_id": job.get("jobId") or job.get("job_id") or (job.get("job") or {}).get("id"),
                "posts_per_niche": posts_per_niche,
            }
        except Exception as error:
            return {"state": "unavailable", "error": sanitize(error)}


def _records(payload: Any) -> Iterable[tuple[Dict[str, Any], Dict[str, Any]]]:
    if not isinstance(payload, dict):
        return
    results = payload.get("results")
    if isinstance(results, list):
        for result in results:
            if not isinstance(result, dict):
                continue
            for key in ("videos", "posts", "tweets"):
                values = result.get(key)
                if isinstance(values, list):
                    for value in values:
                        if isinstance(value, dict):
                            yield value, result
    for key in ("videos", "posts", "tweets"):
        values = payload.get(key)
        if isinstance(values, list):
            for value in values:
                if isinstance(value, dict):
                    yield value, payload


def _research_headers() -> Dict[str, str]:
    token = os.getenv("RESEARCH_API_KEY", "").strip()
    return {"Authorization": f"Bearer {token}"} if token else {}


def _count(value: Any) -> int:
    if value in (None, ""):
        return 0
    text = str(value).strip().lower().replace(",", "")
    multiplier = 1
    if text.endswith("k"):
        multiplier, text = 1000, text[:-1]
    elif text.endswith("m"):
        multiplier, text = 1_000_000, text[:-1]
    try:
        return max(0, int(float(text) * multiplier))
    except (TypeError, ValueError):
        return 0
