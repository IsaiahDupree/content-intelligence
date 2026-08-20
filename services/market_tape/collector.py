"""Autonomous discovery, recheck, mapping, and receipt orchestration."""

from __future__ import annotations

import json
import hashlib
import re
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Sequence, Tuple

from .config import MarketTapeConfig
from .models import (
    MarketContent,
    ProviderBatch,
    QueryAttempt,
    SourceReceipt,
    SourceState,
    isoformat,
    new_run_id,
    parse_datetime,
    utc_now,
)
from .sources import build_sources
from .sources.local_research import LocalResearchSource
from .sinks import SupabaseSink
from .store import MarketTapeStore


AUTONOMOUS_QUERY_NOISE = {
    "august", "breaking", "breaking news", "breakdown", "cash", "commentary", "family", "friends",
    "game", "gameplay", "games", "gaming", "highlights", "live", "moments", "movie", "music",
    "news", "reveal", "season", "trailer", "trailers", "trial", "game highlights august",
}


class MarketTapeCollector:
    def __init__(
        self,
        config: MarketTapeConfig | None = None,
        store: MarketTapeStore | None = None,
        source_builder=build_sources,
    ):
        self.config = config or MarketTapeConfig.from_environment()
        self.store = store or MarketTapeStore(self.config)
        self.source_builder = source_builder
        self._last_discovery_topics: Dict[str, Any] = {
            "mode": "configured",
            "topics": list(self.config.topics),
            "signals": [],
        }

    def run_cycle(self, mode: str = "full") -> Dict[str, Any]:
        if mode not in {"full", "discovery", "recheck"}:
            raise ValueError("mode must be full, discovery, or recheck")
        run_id = new_run_id()
        self.store.start_run(run_id, mode)
        receipts: List[Dict[str, Any]] = []
        state = "completed"
        error_detail = ""
        try:
            if mode in {"full", "discovery"}:
                receipts.extend(self._run_discovery(run_id))
            if mode == "recheck":
                receipts.extend(self._run_local_ingest(run_id))
            if mode in {"full", "recheck"}:
                receipts.extend(self._run_rechecks(run_id))
            trend_observations = self.store.aggregate_trends(run_id=run_id)
            predictions = self.store.create_predictions(run_id)
        except Exception as error:
            state = "failed"
            error_detail = f"{error.__class__.__name__}: {str(error)[:500]}"
            trend_observations = 0
            predictions = 0
        finally:
            self.store.finish_run(run_id, state=state, error_detail=error_detail)
        outbox_records = self.store.enqueue_run_for_sync(run_id)
        sink = SupabaseSink(self.config, self.store)
        try:
            sync_result = sink.flush()
        finally:
            sink.close()
        status = self.store.status()
        result = {
            "run_id": run_id,
            "mode": mode,
            "state": state,
            "error_detail": error_detail,
            "trend_observations_added": trend_observations,
            "predictions_added": predictions,
            "receipts": receipts,
            "outbox_records": outbox_records,
            "central_sync": sync_result,
            "discovery_topics": self._last_discovery_topics,
            "status": status,
        }
        self._write_heartbeat(result)
        return result

    def bootstrap_local_archive(self, limit_per_platform: int = 10000) -> Dict[str, Any]:
        """Promote existing browser-research records without calling external providers."""
        limit_per_platform = min(100000, max(1, int(limit_per_platform)))
        run_id = new_run_id()
        self.store.start_run(run_id, "archive_bootstrap")
        receipts: List[Dict[str, Any]] = []
        state = "completed"
        error_detail = ""
        local_config = replace(self.config, local_research_trigger_enabled=False)
        sources = [
            LocalResearchSource(
                local_config,
                run_id,
                1,
                platform=platform,
                api_platform="twitter" if platform == "x" else platform,
            )
            for platform in ("tiktok", "instagram", "x", "facebook", "threads")
        ]
        try:
            for source in sources:
                batch = source.discover(limit_per_platform)
                self._persist_batch(batch, run_id)
                receipts.append(batch.receipt.to_dict())
            trend_observations = self.store.aggregate_trends(run_id=run_id)
            predictions = self.store.create_predictions(run_id)
        except Exception as error:
            state = "failed"
            error_detail = f"{error.__class__.__name__}: {str(error)[:500]}"
            trend_observations = 0
            predictions = 0
        finally:
            for source in sources:
                source.close()
            self.store.finish_run(run_id, state=state, error_detail=error_detail)
        outbox_records = self.store.enqueue_run_for_sync(run_id)
        sink = SupabaseSink(self.config, self.store)
        try:
            sync_result = sink.flush()
        finally:
            sink.close()
        result = {
            "run_id": run_id,
            "mode": "archive_bootstrap",
            "state": state,
            "error_detail": error_detail,
            "trend_observations_added": trend_observations,
            "predictions_added": predictions,
            "receipts": receipts,
            "outbox_records": outbox_records,
            "central_sync": sync_result,
            "status": self.store.status(),
        }
        self._write_heartbeat(result)
        return result

    def backfill_query_attempts(self) -> Dict[str, Any]:
        """Import and verify historical keyword-search receipts without provider calls."""
        run_id = new_run_id()
        self.store.start_run(run_id, "query_attempt_backfill")
        started_at = utc_now()
        receipts: List[Dict[str, Any]] = []
        imported = 0
        trend_observations = 0
        context_trend_backfill: Dict[str, Any] = {
            "attributions_scanned": 0,
            "eligible_attributions": 0,
            "invalid_context": 0,
            "trends_inserted": 0,
            "memberships_inserted": 0,
            "affected_trend_ids": [],
        }
        state = "completed"
        error_detail = ""
        local_config = replace(self.config, local_research_trigger_enabled=False)
        sources = [
            LocalResearchSource(
                local_config,
                run_id,
                1,
                platform=platform,
                api_platform="twitter" if platform == "x" else platform,
            )
            for platform in ("tiktok", "instagram", "x", "facebook", "threads")
        ]
        try:
            for source in sources:
                attempts = source.archived_query_attempts()
                inserted = self.store.save_query_attempts(attempts)
                imported += inserted
                receipt = SourceReceipt(
                    run_id=run_id,
                    source_id=f"{source.source_id}-receipt-backfill",
                    platform=source.platform,
                    state=SourceState.READY,
                    started_at=started_at,
                    finished_at=utc_now(),
                    request_count=0,
                    discovered_count=len(attempts),
                    metadata={
                        "contract": "query_attempt_backfill_v1",
                        "archive_dir": str(source.platform_archive_dir),
                        "attempts_inserted": inserted,
                        "attempts_duplicate": len(attempts) - inserted,
                        "provider_calls_made": 0,
                    },
                )
                self.store.save_receipt(receipt)
                receipts.append(receipt.to_dict())

            youtube_attempts = self._youtube_manifest_query_attempts(run_id)
            inserted = self.store.save_query_attempts(youtube_attempts)
            imported += inserted
            youtube_failures = sum(
                attempt.state in {"failed", "timed_out", "artifact_missing", "artifact_mismatch"}
                for attempt in youtube_attempts
            )
            youtube_receipt = SourceReceipt(
                run_id=run_id,
                source_id="youtube-query-receipt-backfill",
                platform="youtube",
                state=SourceState.DEGRADED if youtube_failures else SourceState.READY,
                started_at=started_at,
                finished_at=utc_now(),
                request_count=0,
                discovered_count=len(youtube_attempts),
                failed_count=youtube_failures,
                error_code="historical_artifact_failure" if youtube_failures else "",
                error_detail=(
                    f"{youtube_failures} historical attempts failed or failed artifact verification"
                    if youtube_failures
                    else ""
                ),
                metadata={
                    "contract": "query_attempt_backfill_v1",
                    "archive_dir": str(self.config.youtube_research_dir),
                    "attempts_inserted": inserted,
                    "attempts_duplicate": len(youtube_attempts) - inserted,
                    "provider_calls_made": 0,
                },
            )
            self.store.save_receipt(youtube_receipt)
            receipts.append(youtube_receipt.to_dict())
            context_trend_backfill = self.store.backfill_context_trends()
            affected_trend_ids = context_trend_backfill["affected_trend_ids"]
            if affected_trend_ids:
                trend_observations = self.store.aggregate_trends(
                    trend_ids=affected_trend_ids,
                )
            if youtube_failures:
                state = "degraded"
                error_detail = youtube_receipt.error_detail
        except Exception as error:
            state = "failed"
            error_detail = f"{error.__class__.__name__}: {str(error)[:500]}"
        finally:
            for source in sources:
                source.close()
            self.store.finish_run(run_id, state=state, error_detail=error_detail)
        outbox_records = self.store.enqueue_run_for_sync(run_id)
        missing_outbox_records = 0
        if context_trend_backfill["memberships_inserted"]:
            missing_outbox_records = self.store.enqueue_missing_for_sync()
            outbox_records += missing_outbox_records
        sink = SupabaseSink(self.config, self.store)
        try:
            sync_result = sink.flush()
        finally:
            sink.close()
        result = {
            "run_id": run_id,
            "mode": "query_attempt_backfill",
            "state": state,
            "error_detail": error_detail,
            "attempts_inserted": imported,
            "context_trend_backfill": context_trend_backfill,
            "trend_observations_added": trend_observations,
            "missing_outbox_records": missing_outbox_records,
            "receipts": receipts,
            "outbox_records": outbox_records,
            "central_sync": sync_result,
            "status": self.store.status(),
        }
        self._write_heartbeat(result)
        return result

    def reindex_trends(self, forecast_limit: int = 50000) -> Dict[str, Any]:
        """Recompute the current trend index and baseline forecasts without provider calls."""
        run_id = new_run_id()
        self.store.start_run(run_id, "trend_reindex")
        state = "completed"
        error_detail = ""
        trend_observations = 0
        baseline_forecast: Dict[str, Any] = {
            "state": "not_started",
            "predictions_added": 0,
        }
        try:
            trend_observations = self.store.aggregate_trends()
            baseline_forecast = self.store.forecast_baseline_trends(
                limit=min(100000, max(1, int(forecast_limit))),
                run_id=run_id,
            )
        except Exception as error:
            state = "failed"
            error_detail = f"{error.__class__.__name__}: {str(error)[:500]}"
        finally:
            self.store.finish_run(run_id, state=state, error_detail=error_detail)
        outbox_records = self.store.enqueue_run_for_sync(run_id)
        sink = SupabaseSink(self.config, self.store)
        try:
            sync_result = sink.flush()
        finally:
            sink.close()
        result = {
            "run_id": run_id,
            "mode": "trend_reindex",
            "state": state,
            "error_detail": error_detail,
            "provider_calls_made": 0,
            "trend_observations_added": trend_observations,
            "baseline_forecast": baseline_forecast,
            "outbox_records": outbox_records,
            "central_sync": sync_result,
            "status": self.store.status(),
        }
        self._write_heartbeat(result)
        return result

    def _youtube_manifest_query_attempts(self, run_id: str) -> List[QueryAttempt]:
        attempts: List[QueryAttempt] = []
        root = self.config.youtube_research_dir
        if not root.is_dir():
            return attempts
        for manifest_path in sorted(root.rglob("research-manifest.json")):
            try:
                encoded = manifest_path.read_bytes()
                payload = json.loads(encoded)
            except (OSError, ValueError):
                continue
            if payload.get("contract") != "youtube_query_research_receipt_v1":
                continue
            attempted_at = parse_datetime(payload.get("started_at")) or datetime.fromtimestamp(
                manifest_path.stat().st_mtime,
                tz=timezone.utc,
            )
            finished_at = parse_datetime(payload.get("finished_at")) or attempted_at
            manifest_sha = hashlib.sha256(encoded).hexdigest()
            for raw_receipt in payload.get("receipts") or []:
                if not isinstance(raw_receipt, dict):
                    continue
                query = str(raw_receipt.get("query") or "").strip()
                if not query:
                    continue
                artifact_path = Path(str(raw_receipt.get("output_path") or ""))
                expected_sha = str(raw_receipt.get("output_sha256") or "")
                artifact_exists = artifact_path.is_file() if str(artifact_path) not in {"", "."} else False
                actual_sha = _file_sha256(artifact_path) if artifact_exists else ""
                artifact_verified = bool(expected_sha and actual_sha == expected_sha)
                original_state = str(raw_receipt.get("state") or "unknown")
                state = original_state
                error_code = "youtube_query_failed" if state in {"failed", "timed_out"} else ""
                error_detail = str(raw_receipt.get("error") or "")[:1000]
                if expected_sha and not artifact_exists:
                    state = "artifact_missing"
                    error_code = "artifact_missing"
                    error_detail = f"Expected query artifact is missing: {artifact_path}"
                elif expected_sha and not artifact_verified:
                    state = "artifact_mismatch"
                    error_code = "artifact_sha256_mismatch"
                    error_detail = f"Query artifact SHA-256 does not match manifest: {artifact_path}"
                attempts.append(QueryAttempt(
                    run_id=run_id,
                    source_id="youtube-yt-dlp-signed-search",
                    platform="youtube",
                    query=query,
                    attempted_at=attempted_at,
                    finished_at=finished_at,
                    state=state,
                    result_count=max(0, int(raw_receipt.get("records") or 0)),
                    request_count=1,
                    error_code=error_code,
                    error_detail=error_detail,
                    artifact_path=str(artifact_path) if artifact_exists else "",
                    artifact_sha256=actual_sha or expected_sha,
                    metadata={
                        "contract": "youtube_query_research_receipt_v1",
                        "manifest_path": str(manifest_path),
                        "manifest_sha256": manifest_sha,
                        "artifact_verified": artifact_verified,
                        "original_state": original_state,
                        "query_family": query,
                    },
                ))
        return attempts

    def _run_discovery(self, run_id: str) -> List[Dict[str, Any]]:
        sources = sorted(
            self._build_sources(run_id, adaptive_topics=True),
            key=lambda source: source.platform in self.config.overflow_platforms,
        )
        receipts: List[Dict[str, Any]] = []
        try:
            for source in sources:
                global_remaining = max(
                    0,
                    self.config.daily_unique_target - self.store.daily_unique_count(),
                )
                if global_remaining <= 0:
                    break
                target = self.config.target_for(source.platform)
                acquired = self.store.daily_unique_count(source.platform)
                lane_remaining = max(0, target - acquired)
                remaining = (
                    max(lane_remaining, global_remaining)
                    if source.platform in self.config.overflow_platforms
                    else min(lane_remaining, global_remaining)
                )
                if remaining <= 0:
                    continue
                batch = self._circuit_open_batch(source) or source.discover(min(
                    remaining,
                    self.config.max_discovery_items_per_source,
                ))
                self._persist_batch(batch, run_id)
                receipts.append(batch.receipt.to_dict())
        finally:
            for source in sources:
                source.close()
        return receipts

    def _run_local_ingest(self, run_id: str) -> List[Dict[str, Any]]:
        """Promote files emitted by the asynchronous Safari job on every daemon tick."""
        sources = [
            LocalResearchSource(
                self.config,
                run_id,
                self.store.remaining_request_budget(
                    f"safari-local-research-{platform}",
                    self.config.request_limit_for(platform),
                ),
                platform=platform,
                api_platform="twitter" if platform == "x" else platform,
            )
            for platform in ("tiktok", "instagram", "x", "facebook", "threads")
            if platform in self.config.platforms
        ]
        receipts: List[Dict[str, Any]] = []
        try:
            for source in sources:
                remaining = max(
                    0,
                    self.config.target_for(source.platform)
                    - self.store.daily_unique_count(source.platform),
                )
                if remaining <= 0:
                    continue
                batch = self._circuit_open_batch(source) or source.discover(min(remaining, 2500))
                self._persist_batch(batch, run_id)
                receipts.append(batch.receipt.to_dict())
        finally:
            for source in sources:
                source.close()
        return receipts

    def _run_rechecks(self, run_id: str) -> List[Dict[str, Any]]:
        due = self.store.due_polls(self.config.max_due_rechecks_per_cycle)
        if not due:
            return []
        sources = self._build_sources(run_id)
        source_map = {source.source_id: source for source in sources}
        receipts: List[Dict[str, Any]] = []
        try:
            for platform, rows in due.items():
                grouped: Dict[str, List[Dict[str, Any]]] = {}
                for row in rows:
                    grouped.setdefault(str(row["preferred_source_id"]), []).append(row)
                for source_id, tracked in grouped.items():
                    source = source_map.get(source_id)
                    if source is None:
                        candidates = [candidate for candidate in sources if candidate.platform == platform]
                        source = next((candidate for candidate in candidates if candidate.credentials_available()), candidates[0] if candidates else None)
                    if source is None:
                        self.store.mark_poll_failure((row["video_id"] for row in tracked), "source_unavailable")
                        continue
                    circuit_batch = self._circuit_open_batch(source)
                    if circuit_batch is not None:
                        alternatives = [
                            candidate for candidate in sources
                            if candidate.platform == platform
                            and candidate.source_id != source.source_id
                            and candidate.credentials_available()
                            and self._circuit_open_batch(candidate) is None
                        ]
                        if alternatives:
                            source = alternatives[0]
                            circuit_batch = None
                    if circuit_batch is not None:
                        self.store.mark_poll_failure(
                            (row["video_id"] for row in tracked), "source_circuit_open"
                        )
                        self._persist_batch(circuit_batch, run_id)
                        receipts.append(circuit_batch.receipt.to_dict())
                        continue
                    batch = source.refresh(tracked)
                    returned = {item.video_id for item in batch.items}
                    missing = [row["video_id"] for row in tracked if row["video_id"] not in returned]
                    if missing:
                        batch.receipt.failed_count += len(missing)
                        self.store.mark_poll_failure(missing, batch.receipt.error_code or "provider_item_missing")
                    accepted_ids = self._persist_batch(batch, run_id)
                    unchanged = returned - accepted_ids
                    if unchanged:
                        self.store.defer_unchanged_polls(unchanged)
                    receipts.append(batch.receipt.to_dict())
        finally:
            for source in sources:
                source.close()
        return receipts

    def _build_sources(self, run_id: str, adaptive_topics: bool = False):
        def guarded_budget(source_id: str, daily_limit: int) -> int:
            if self.store.daily_provider_cost() >= self.config.max_daily_provider_cost_usd:
                return 0
            return self.store.remaining_request_budget(source_id, daily_limit)

        source_config = self._adaptive_discovery_config() if adaptive_topics else self.config
        sources = self.source_builder(source_config, run_id, guarded_budget)
        for source in sources:
            source.known_external_ids = (
                lambda external_ids, platform=source.platform:
                self.store.known_external_ids(platform, external_ids)
            )
            source.recent_metadata_total = (
                lambda metadata_key, source_id=source.source_id:
                self.store.recent_source_metadata_total(source_id, metadata_key)
            )
        return sources

    def _adaptive_discovery_config(self) -> MarketTapeConfig:
        if not self.config.adaptive_topics_enabled:
            self._last_discovery_topics = {
                "mode": "configured",
                "topics": list(self.config.topics),
                "signals": [],
            }
            return self.config

        limit = max(1, min(100, int(self.config.adaptive_topic_limit)))
        text_signals = self.store.keyword_signals(
            limit=limit * 5,
            window_hours=self.config.adaptive_topic_window_hours,
            min_videos=self.config.adaptive_topic_min_videos,
        )
        query_signals = self.store.discovery_query_signals(
            limit=limit * 5,
            window_hours=self.config.adaptive_topic_window_hours,
            min_videos=self.config.adaptive_topic_min_videos,
        )
        by_keyword = {
            str(signal.get("keyword") or "").casefold(): signal
            for signal in text_signals
            if signal.get("keyword")
        }
        for signal in query_signals:
            if signal.get("keyword"):
                by_keyword[str(signal["keyword"]).casefold()] = signal
        signals = list(by_keyword.values())
        candidates = [
            signal for signal in signals
            if signal.get("query_ready")
            and int(signal.get("videos_total") or 0) >= 2
            and int(signal.get("creators_total") or 0) >= 2
            and str(signal.get("keyword") or "").casefold() not in AUTONOMOUS_QUERY_NOISE
        ]
        candidates.sort(key=self._discovery_priority, reverse=True)
        if not candidates:
            self._last_discovery_topics = {
                "mode": "configured_fallback",
                "topics": list(self.config.topics),
                "signals": [],
            }
            return self.config

        exploration_fraction = max(
            0.0, min(0.5, float(self.config.adaptive_topic_exploration_fraction))
        )
        exploration_count = min(len(self.config.topics), round(limit * exploration_fraction))
        adaptive_count = max(1, limit - exploration_count)
        selected_signals = self._diverse_keyword_signals(candidates, adaptive_count)
        selected = [str(signal["keyword"]) for signal in selected_signals]

        configured = list(self.config.topics)
        if configured and exploration_count:
            offset = utc_now().date().toordinal() % len(configured)
            rotated = configured[offset:] + configured[:offset]
            selected.extend(
                topic for topic in rotated
                if topic.casefold() not in {value.casefold() for value in selected}
            )
        topics = list(dict.fromkeys(selected))[:limit]
        self._last_discovery_topics = {
            "mode": "adaptive",
            "topics": topics,
            "adaptive_count": len(selected_signals),
            "exploration_count": max(0, len(topics) - len(selected_signals)),
            "window_hours": self.config.adaptive_topic_window_hours,
            "signals": [
                {
                    key: signal[key]
                    for key in (
                        "keyword", "rank", "score", "confidence", "videos_total",
                        "creators_total", "platforms_total", "views_total",
                    )
                }
                for signal in selected_signals
            ],
        }
        return replace(self.config, topics=topics)

    @staticmethod
    def _diverse_keyword_signals(
        candidates: Sequence[Dict[str, Any]], limit: int
    ) -> List[Dict[str, Any]]:
        selected: List[Dict[str, Any]] = []
        token_sets: List[set[str]] = []
        compact_keys: set[str] = set()
        evidence_sets: List[set[str]] = []
        for signal in candidates:
            tokens = MarketTapeCollector._canonical_topic_tokens(
                str(signal.get("keyword", ""))
            )
            if not tokens:
                continue
            compact = "".join(sorted(tokens))
            evidence = {
                str(example.get("video_id"))
                for example in signal.get("examples", [])
                if isinstance(example, dict) and example.get("video_id")
            }
            token_duplicate = any(
                len(tokens & prior) / max(1, min(len(tokens), len(prior))) >= 0.5
                for prior in token_sets
            )
            evidence_duplicate = any(
                evidence
                and prior
                and len(evidence & prior) / max(1, min(len(evidence), len(prior))) >= 0.5
                for prior in evidence_sets
            )
            if compact in compact_keys or token_duplicate or evidence_duplicate:
                continue
            selected.append(signal)
            token_sets.append(tokens)
            compact_keys.add(compact)
            evidence_sets.append(evidence)
            if len(selected) >= limit:
                break
        return selected

    @staticmethod
    def _discovery_priority(signal: Dict[str, Any]) -> tuple[float, float, int]:
        confidence = max(0.0, min(1.0, float(signal.get("confidence") or 0.0)))
        score = max(0.0, float(signal.get("score") or 0.0))
        breadth = min(20, int(signal.get("videos_total") or 0))
        specificity = {
            "query": 1.15,
            "phrase": 1.08,
            "hashtag": 1.04,
        }.get(str(signal.get("keyword_type") or "keyword"), 1.0)
        return score * (0.65 + 0.35 * confidence) * specificity, confidence, breadth

    @staticmethod
    def _canonical_topic_tokens(value: str) -> set[str]:
        tokens = re.findall(r"[a-z0-9]+", value.casefold())
        normalized = {
            token[:-1] if len(token) > 4 and token.endswith("s") else token
            for token in tokens
        }
        compact = "".join(tokens)
        if compact:
            normalized.add(compact[:-1] if len(compact) > 4 and compact.endswith("s") else compact)
        return normalized

    def _circuit_open_batch(self, source: Any) -> ProviderBatch | None:
        # A browser scheduler outage must never block cost-free archive ingestion.
        # LocalResearchSource reports trigger health inside its own receipt after reading files.
        if isinstance(source, LocalResearchSource):
            return None
        retry = self.store.source_retry_status(source.source_id)
        if not retry.get("blocked"):
            return None
        if source.source_id == "youtube-data-api-v3" and retry.get("error_code") in {
            "provider_rate_limited", "provider_auth_or_quota",
        }:
            return None
        state = str(retry.get("state", "degraded"))
        if state == SourceState.BLOCKED_CREDENTIAL.value and source.credentials_available():
            return None
        if state == SourceState.BLOCKED_APPROVAL.value and (
            not source.metered or self.config.allow_metered_reads
        ):
            return None
        try:
            source_state = SourceState(state)
        except ValueError:
            source_state = SourceState.DEGRADED
        now = utc_now()
        return ProviderBatch([], SourceReceipt(
            run_id=source.run_id,
            source_id=source.source_id,
            platform=source.platform,
            state=source_state,
            started_at=now,
            finished_at=now,
            request_count=0,
            quota_remaining=source.request_budget,
            error_code="circuit_open",
            error_detail=f"source is cooling down until {retry.get('next_retry_at')}",
            metadata={
                "metered": source.metered,
                "next_retry_at": retry.get("next_retry_at"),
                "prior_error_code": retry.get("error_code", ""),
                "consecutive_failures": retry.get("consecutive_failures", 0),
            },
        ))

    def _persist_batch(self, batch: ProviderBatch, run_id: str) -> set[str]:
        accepted = 0
        accepted_ids: set[str] = set()
        duplicates = 0
        failures = batch.receipt.failed_count
        for item in batch.items:
            try:
                added, _ = self.store.ingest(item, run_id)
                if added:
                    accepted += 1
                    accepted_ids.add(item.video_id)
                else:
                    duplicates += 1
            except (TypeError, ValueError, KeyError):
                failures += 1
        batch.receipt.accepted_count = accepted
        batch.receipt.duplicate_count = duplicates
        batch.receipt.failed_count = failures
        if batch.receipt.state == SourceState.READY and failures and not accepted:
            batch.receipt.state = SourceState.DEGRADED
            batch.receipt.error_code = batch.receipt.error_code or "normalization_failed"
            batch.receipt.error_detail = batch.receipt.error_detail or "Provider items failed canonical validation"
        self.store.save_query_attempts(batch.query_attempts)
        self.store.save_receipt(batch.receipt)
        return accepted_ids

    def _write_heartbeat(self, result: Dict[str, Any]) -> None:
        self.config.heartbeat_path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "service": "social-market-tape",
            "pid": __import__("os").getpid(),
            "heartbeat_at": isoformat(utc_now()),
            "last_run_id": result["run_id"],
            "last_run_state": result["state"],
            "daily": result["status"]["daily"],
        }
        temporary = self.config.heartbeat_path.with_suffix(".tmp")
        temporary.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        temporary.replace(self.config.heartbeat_path)


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()
