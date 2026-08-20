"""Low-cost adapter for the existing Safari research archive and scheduler."""

from __future__ import annotations

import json
import hashlib
import math
import os
import re
import shutil
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Set

from .base import MarketSource, sanitize
from ..models import (
    MarketContent,
    MetricCounters,
    ProviderBatch,
    QueryAttempt,
    SourceState,
    isoformat,
    parse_datetime,
    stable_hash,
    utc_now,
)


ID_PATTERNS = {
    "x": re.compile(r"/(?:status)/(\d+)", re.IGNORECASE),
    "threads": re.compile(r"/(?:post)/([A-Za-z0-9_-]+)", re.IGNORECASE),
    "tiktok": re.compile(r"/(?:video)/(\d+)", re.IGNORECASE),
    "instagram": re.compile(r"/(?:reel|reels|p)/([A-Za-z0-9_-]+)", re.IGNORECASE),
    "facebook": re.compile(r"/(?:videos|reel|posts)/([A-Za-z0-9._-]+)", re.IGNORECASE),
}

RELEVANCE_STOP_WORDS = {
    "a", "an", "and", "at", "best", "for", "from", "funny", "highlights", "in",
    "of", "on", "or", "the", "tips", "to", "with",
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
        self._archive_qc: Dict[str, int] = {}

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
            self._reset_archive_qc()
            items = self._load_archive(max_items)
            scheduler = self._schedule_if_due(max_items)
            batch = self.success_batch(
                started,
                items,
                operation="discover",
                metadata={
                    "archive_dir": str(self.platform_archive_dir),
                    "archive_qc": self._archive_qc_receipt(),
                    "scheduler": scheduler,
                    "provider_cost_usd": 0.0,
                },
            )
            if scheduler.get("state") in {"unavailable", "blocked_disk_pressure"}:
                batch.receipt.state = SourceState.DEGRADED
                batch.receipt.error_code = str(
                    scheduler.get("error_code") or "scheduler_unavailable"
                )
                batch.receipt.error_detail = str(scheduler.get("error", ""))[:1000]
            batch.query_attempts = self._load_query_attempts()
            return batch
        except Exception as error:
            return self.blocked_batch(started, error)

    def refresh(self, tracked: Sequence[Dict[str, Any]]) -> ProviderBatch:
        started = utc_now()
        try:
            self.preflight()
            self._reset_archive_qc()
            wanted = {str(row["external_id"]) for row in tracked}
            items = self._load_archive(max(len(wanted) * 4, len(wanted)), wanted=wanted)
            return self.success_batch(
                started,
                items,
                operation="refresh",
                metadata={
                    "archive_dir": str(self.platform_archive_dir),
                    "archive_qc": self._archive_qc_receipt(),
                    "provider_cost_usd": 0.0,
                },
            )
        except Exception as error:
            return self.blocked_batch(started, error)

    def archived_query_attempts(self) -> List[QueryAttempt]:
        """Return immutable coverage receipts without scheduling or ingesting content."""
        return self._load_query_attempts()

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
        niche = str(raw.get("niche") or context.get("niche") or "").strip()
        if niche:
            self._archive_qc["evaluated"] += 1
            if not _topic_relevant(text, author, niche):
                self._archive_qc["rejected_irrelevant"] += 1
                return None
            self._archive_qc["accepted_relevant"] += 1
        else:
            self._archive_qc["unscoped"] += 1
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
                "niche": niche,
                "query": context.get("query"),
                "source_file": path.name,
            },
        )

    def _reset_archive_qc(self) -> None:
        self._archive_qc = {
            "evaluated": 0,
            "accepted_relevant": 0,
            "rejected_irrelevant": 0,
            "unscoped": 0,
        }

    def _archive_qc_receipt(self) -> Dict[str, Any]:
        evaluated = self._archive_qc.get("evaluated", 0)
        accepted = self._archive_qc.get("accepted_relevant", 0)
        return {
            **self._archive_qc,
            "precision": round(accepted / evaluated, 6) if evaluated else None,
            "policy": "niche-token-overlap-v1",
        }

    def _load_query_attempts(self) -> List[QueryAttempt]:
        if not self.platform_archive_dir.is_dir():
            return []
        files = sorted(
            self.platform_archive_dir.glob("*.json"),
            key=lambda path: (path.stat().st_mtime, path.name),
        )
        attempts: Dict[str, QueryAttempt] = {}
        for path in files:
            try:
                encoded = path.read_bytes()
                payload = json.loads(encoded)
            except (OSError, ValueError):
                continue
            if not isinstance(payload, dict) or not isinstance(payload.get("results"), list):
                continue
            metadata = payload.get("metadata") if isinstance(payload.get("metadata"), dict) else {}
            generated = parse_datetime(metadata.get("generatedAt")) or datetime.fromtimestamp(
                path.stat().st_mtime, tz=timezone.utc
            )
            artifact_sha256 = hashlib.sha256(encoded).hexdigest()
            for result in payload["results"]:
                if not isinstance(result, dict):
                    continue
                query = str(result.get("query") or result.get("niche") or "").strip()
                if not query:
                    continue
                query_family = str(result.get("niche") or query).strip()
                result_count = sum(
                    len(result.get(key, []))
                    for key in ("videos", "posts", "tweets")
                    if isinstance(result.get(key), list)
                )
                error_detail = sanitize(result.get("error") or "")[:1000]
                state = "failed" if error_detail else ("completed" if result_count else "empty")
                attempted_at = parse_datetime(
                    result.get("collectionStarted")
                    or result.get("collection_started")
                    or result.get("startedAt")
                ) or generated
                finished_at = parse_datetime(
                    result.get("collectionFinished")
                    or result.get("collection_finished")
                    or result.get("finishedAt")
                ) or attempted_at
                attempt_identity = stable_hash({
                    "source_id": self.source_id,
                    "platform": self.platform,
                    "query": " ".join(query.casefold().split()),
                    "attempted_at": isoformat(attempted_at),
                })
                attempt = QueryAttempt(
                    run_id=self.run_id,
                    source_id=self.source_id,
                    platform=self.platform,
                    query=query,
                    attempted_at=attempted_at,
                    finished_at=finished_at,
                    state=state,
                    result_count=result_count,
                    request_count=1,
                    error_code="browser_research_failed" if error_detail else "",
                    error_detail=error_detail,
                    artifact_path=str(path),
                    artifact_sha256=artifact_sha256,
                    metadata={
                        "contract": "safari_research_archive_v1",
                        "source_file": path.name,
                        "query_family": query_family,
                        "query_mode": (metadata.get("config") or {}).get("queryMode", ""),
                        "attempt_identity": attempt_identity,
                    },
                )
                attempts[attempt.attempt_key] = attempt
        return sorted(
            attempts.values(),
            key=lambda attempt: (
                attempt.attempted_at,
                attempt.query.casefold(),
            ),
        )

    def _schedule_if_due(self, max_items: int) -> Dict[str, Any]:
        if not self.config.local_research_trigger_enabled:
            return {"state": "disabled"}
        coordinator = next(
            (
                platform for platform in ("tiktok", "instagram", "x", "facebook", "threads")
                if platform in self.config.platforms
            ),
            None,
        )
        if self.platform != coordinator:
            return {"state": "coordinated", "coordinator": coordinator}

        now = utc_now()
        query_hash = stable_hash([" ".join(topic.casefold().split()) for topic in self.config.topics])
        schedule_state: Dict[str, Any] = {}

        def save_schedule_state(payload: Dict[str, Any]) -> None:
            self.config.local_research_state_path.parent.mkdir(parents=True, exist_ok=True)
            temporary = self.config.local_research_state_path.with_suffix(".tmp")
            temporary.write_text(
                json.dumps(payload, indent=2, sort_keys=True) + "\n",
                encoding="utf-8",
            )
            temporary.replace(self.config.local_research_state_path)

        try:
            if self.config.local_research_state_path.is_file():
                schedule_state = json.loads(
                    self.config.local_research_state_path.read_text(encoding="utf-8")
                )
        except (OSError, ValueError):
            schedule_state = {}
        requested_at = parse_datetime(schedule_state.get("requested_at"))
        age_seconds = (now - requested_at).total_seconds() if requested_at else None
        retry_platforms: List[str] = []
        if (
            age_seconds is not None
            and age_seconds < self.config.local_research_refresh_seconds
        ):
            job_id = str(schedule_state.get("job_id") or "")
            if job_id:
                try:
                    prior_job = self.request_json(
                        "GET",
                        f"{self.base_url}/api/research/status/{job_id}",
                        headers=_research_headers(),
                    )
                except Exception:
                    prior_job = {}
                prior_status = str(prior_job.get("status") or "").casefold()
                if prior_status in {"queued", "running"}:
                    return {"state": "already_running", "job_id": job_id}
                if prior_status in {"failed", "partial"}:
                    failed_at = parse_datetime(prior_job.get("completedAt")) or requested_at
                    failure_age = (now - failed_at).total_seconds() if failed_at else 0.0
                    if failure_age < self.config.local_research_failure_retry_seconds:
                        return {
                            "state": "failed_cooldown",
                            "job_id": job_id,
                            "job_status": prior_status,
                            "retry_in_seconds": round(
                                self.config.local_research_failure_retry_seconds
                                - max(0.0, failure_age),
                                3,
                            ),
                        }
                    retry_platforms = [
                        str(receipt.get("platform"))
                        for receipt in prior_job.get("platformReceipts", [])
                        if isinstance(receipt, dict)
                        and receipt.get("status") == "failed"
                        and receipt.get("platform")
                    ]
                elif prior_status == "completed":
                    return {
                        "state": "recently_completed",
                        "job_id": job_id,
                        "latest_age_seconds": round(max(0.0, age_seconds), 3),
                        "query_count": len(self.config.topics),
                        "query_hash": query_hash,
                    }
                elif not prior_status:
                    missing_since = parse_datetime(schedule_state.get("missing_since"))
                    if missing_since is None:
                        missing_since = now
                        schedule_state["missing_since"] = isoformat(now)
                        schedule_state["missing_reason"] = "provider_job_not_found"
                        save_schedule_state(schedule_state)
                    missing_age = max(0.0, (now - missing_since).total_seconds())
                    if missing_age < self.config.local_research_failure_retry_seconds:
                        return {
                            "state": "missing_job_cooldown",
                            "job_id": job_id,
                            "job_status": "not_found",
                            "retry_in_seconds": round(
                                self.config.local_research_failure_retry_seconds
                                - missing_age,
                                3,
                            ),
                            "query_count": len(self.config.topics),
                            "query_hash": query_hash,
                        }
                    retry_platforms = [
                        str(platform)
                        for platform in schedule_state.get("platforms", [])
                        if str(platform) in {
                            "tiktok", "instagram", "twitter", "facebook", "threads"
                        }
                    ]
            else:
                return {
                    "state": "recently_requested",
                    "job_id": None,
                    "latest_age_seconds": round(max(0.0, age_seconds), 3),
                    "query_count": len(self.config.topics),
                    "query_hash": query_hash,
                }
        temporary_root = Path(tempfile.gettempdir())
        try:
            free_bytes = shutil.disk_usage(temporary_root).free
        except OSError as error:
            return {
                "state": "unavailable",
                "error_code": "disk_preflight_failed",
                "error": sanitize(error),
            }
        minimum_free = max(0, self.config.local_research_min_free_bytes)
        if free_bytes < minimum_free:
            return {
                "state": "blocked_disk_pressure",
                "error_code": "insufficient_temporary_disk",
                "error": (
                    f"browser research requires {minimum_free} free bytes in "
                    f"{temporary_root}; observed {free_bytes}"
                ),
                "temporary_root": str(temporary_root),
                "free_bytes": free_bytes,
                "minimum_free_bytes": minimum_free,
            }
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
                    "queryMode": "trend",
                },
            }
            if cross_platform:
                body["platforms"] = retry_platforms or [
                    "tiktok", "instagram", "twitter", "facebook", "threads"
                ]
            job = self.request_json(
                "POST",
                f"{self.base_url}{endpoint}",
                headers=headers,
                json_body=body,
            )
            job_id = job.get("jobId") or job.get("job_id") or (job.get("job") or {}).get("id")
            state_payload = {
                "requested_at": isoformat(now),
                "job_id": job_id,
                "query_hash": query_hash,
                "query_count": len(niches),
                "platforms": body.get("platforms", [self.platform]),
                "retry_of_job_id": schedule_state.get("job_id") if retry_platforms else None,
            }
            save_schedule_state(state_payload)
            return {
                "state": "triggered_all" if cross_platform else "triggered",
                "job_id": job_id,
                "posts_per_niche": posts_per_niche,
                "query_count": len(niches),
                "query_hash": query_hash,
                "platforms": body.get("platforms", [self.platform]),
                "retry_of_job_id": schedule_state.get("job_id") if retry_platforms else None,
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


def _topic_relevant(text: str, author: str, niche: str) -> bool:
    def tokens(value: str) -> List[str]:
        output = []
        for token in re.findall(r"[a-z0-9]+", value.casefold()):
            normalized = token[:-1] if len(token) > 4 and token.endswith("s") else token
            if len(normalized) >= 3:
                output.append(normalized)
        return output

    niche_tokens = {
        token for token in tokens(niche)
        if token not in RELEVANCE_STOP_WORDS
    }
    if not niche_tokens:
        return False
    document_tokens = set(tokens(f"{text} {author}"))
    required = 1 if len(niche_tokens) == 1 else max(2, math.ceil(len(niche_tokens) * 0.5))
    matched = {
        niche_token for niche_token in niche_tokens
        if any(
            document_token == niche_token
            or (len(niche_token) >= 4 and niche_token in document_token)
            for document_token in document_tokens
        )
    }
    return len(matched) >= required
