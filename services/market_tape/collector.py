"""Autonomous discovery, recheck, mapping, and receipt orchestration."""

from __future__ import annotations

import json
import hashlib
import re
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

from .config import MarketTapeConfig
from .models import (
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
from .sources.base import sanitize
from .sources.local_research import LocalResearchSource
from .sinks import SupabaseSink
from .store import CounterRegressionError, MarketTapeStore
from .predictor import load_active_model
from .rapid_trend import RapidTrendTriggerService


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
        self.rapid_trends = RapidTrendTriggerService(self.config, self.store)
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
        phase_sequence: List[str] = []
        state = "completed"
        error_detail = ""
        prediction_evaluation: Dict[str, Any] = {"state": "not_started"}
        validation_forecast: Dict[str, Any] = {"state": "not_started"}
        rapid_trend_evaluation: Dict[str, Any] = {"state": "not_started"}
        deferred_terminal_noop: Optional[Dict[str, Any]] = None
        try:
            prediction_evaluation = self.store.evaluate_predictions()
            phase_sequence.append("evaluate_predictions")
            measurement_work_required = (
                self._forecast_measurement_work_required()
            )
            if measurement_work_required:
                initial_capabilities = self._measurement_source_capabilities(
                    run_id
                )
                validation_forecast = self.store.forecast_active_trends(
                    run_id=run_id,
                    measurement_sources=initial_capabilities["admitted"],
                    limit=self.config.prediction_validation_cohort_limit,
                )
                validation_forecast[
                    "measurement_sources"
                ] = initial_capabilities
                phase_sequence.append(
                    "reserve_validation_forecast_prior_tape"
                )
            else:
                validation_forecast = {
                    "state": "no_promoted_model_or_open_reservation",
                    "predictions_added": 0,
                    "reservations_added": 0,
                }

            if (
                measurement_work_required
                and mode in {"full", "discovery", "recheck"}
            ):
                terminal_receipts = self._run_rechecks(
                    run_id,
                    phase="forecast_terminal",
                )
                receipts.extend(terminal_receipts)
                phase_sequence.append("forecast_terminal_rechecks")
            elif mode in {"full", "discovery", "recheck"}:
                plan = self.store.due_poll_plan(
                    self.config.max_due_rechecks_per_cycle,
                    forecast_capable_platforms=set(),
                    forecast_capable_source_ids=set(),
                    phase="forecast_terminal",
                )
                terminal_noop = self._save_recheck_planner_receipt(
                    run_id,
                    phase="forecast_terminal",
                    queue_receipt=plan["receipt"],
                    source_capability=[],
                ).to_dict()
                if (
                    mode in {"full", "recheck"}
                    and self._scheduled_poll_work_required()
                ):
                    deferred_terminal_noop = terminal_noop
                phase_sequence.append("forecast_terminal_rechecks")
            if mode in {"full", "discovery"}:
                receipts.extend(self._run_discovery(run_id))
                phase_sequence.append("discovery")
            if mode == "recheck":
                receipts.extend(self._run_local_ingest(run_id))
                phase_sequence.append("local_archive_ingest")
            if mode in {"full", "recheck"}:
                if deferred_terminal_noop is not None:
                    receipts.append(deferred_terminal_noop)
                receipts.extend(self._run_rechecks(
                    run_id,
                    phase="scheduled",
                ))
                phase_sequence.append("scheduled_rechecks")
            trend_observations = self.store.aggregate_trends(run_id=run_id)
            phase_sequence.append("aggregate_trends")
            rapid_trend_evaluation = self._evaluate_rapid_trends(run_id=run_id)
            phase_sequence.append("evaluate_rapid_trends")
            baseline_predictions = self.store.create_predictions(run_id)
            phase_sequence.append("transparent_baselines")
            if (
                load_active_model(self.config) is not None
                and int(validation_forecast.get("predictions_added") or 0) == 0
            ):
                post_capabilities = self._measurement_source_capabilities(
                    run_id
                )
                post_forecast = self.store.forecast_active_trends(
                    run_id=run_id,
                    measurement_sources=post_capabilities["admitted"],
                    limit=self.config.prediction_validation_cohort_limit,
                )
                post_forecast["measurement_sources"] = post_capabilities
                validation_forecast = {
                    **post_forecast,
                    "initial_attempt": validation_forecast,
                    "attempt_phase": "post_aggregate_retry",
                }
                phase_sequence.append("reserve_validation_forecast_post_aggregate")
            active_predictions = int(
                validation_forecast.get("predictions_added") or 0
            )
            predictions = baseline_predictions + active_predictions
        except Exception as error:
            state = "failed"
            error_detail = f"{error.__class__.__name__}: {str(error)[:500]}"
            trend_observations = 0
            predictions = 0
            baseline_predictions = 0
            active_predictions = 0
            rapid_trend_evaluation = {
                "state": "failed",
                "error": "collection_cycle_failed_before_or_during_evaluation",
                "provider_calls_made": 0,
            }
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
            "baseline_predictions_added": baseline_predictions,
            "active_predictions_added": active_predictions,
            "prediction_evaluation": prediction_evaluation,
            "validation_forecast": validation_forecast,
            "rapid_trend_evaluation": rapid_trend_evaluation,
            "phase_sequence": phase_sequence,
            "receipts": receipts,
            "outbox_records": outbox_records,
            "central_sync": sync_result,
            "discovery_topics": self._last_discovery_topics,
            "status": status,
        }
        self._write_heartbeat(result)
        return result

    def run_topic_performance_discovery(
        self,
        topic: str,
        *,
        max_items: int = 50,
        platform: str = "youtube",
    ) -> Dict[str, Any]:
        """Execute one query-only, performance-ranked discovery lane.

        Script-language demand must not spend its bounded batch on unrelated
        chart results before the requested query executes.  This path skips
        charts, adaptive topic expansion, rechecks, forecasts, and in-call
        retries. Provider query attempts and accepted observations still use
        the canonical append-only receipt/ingest path.
        """

        normalized_topic = " ".join(str(topic or "").split())
        if not normalized_topic:
            raise ValueError("topic is required")
        if platform != "youtube":
            raise ValueError("performance-ranked demand discovery supports youtube")
        bounded_items = max(1, min(50, int(max_items)))
        run_id = new_run_id()
        self.store.start_run(run_id, "script_language_demand_discovery")
        sources = self._build_sources(
            run_id, adaptive_topics=False, budget_purpose="discovery"
        )
        receipts: List[Dict[str, Any]] = []
        state = "completed"
        error_detail = ""
        trend_observations = 0
        rapid_trend_evaluation: Dict[str, Any] = {"state": "not_started"}
        try:
            source = next(
                (
                    candidate for candidate in sources
                    if candidate.platform == platform
                    and callable(getattr(candidate, "discover_performance", None))
                ),
                None,
            )
            if source is None:
                state = "failed"
                error_detail = "performance_discovery_source_unavailable"
            else:
                batch = self._circuit_open_batch(source) or source.discover_performance(
                    normalized_topic,
                    max_items=bounded_items,
                    relevance_language=(self.config.languages or ["en"])[0],
                    region=(self.config.regions or ["US"])[0],
                )
                self._persist_batch(batch, run_id)
                receipts.append(batch.receipt.to_dict())
                if batch.receipt.state != SourceState.READY:
                    state = "failed"
                    error_detail = (
                        batch.receipt.error_code
                        or batch.receipt.state.value
                    )
                else:
                    trend_observations = self.store.aggregate_trends(run_id=run_id)
                    rapid_trend_evaluation = self._evaluate_rapid_trends(
                        run_id=run_id
                    )
        except Exception as error:
            state = "failed"
            error_detail = f"{type(error).__name__}: {sanitize(str(error))[:300]}"
        finally:
            for source in sources:
                source.close()
            self.store.finish_run(run_id, state=state, error_detail=error_detail)
        outbox_records = self.store.enqueue_run_for_sync(run_id)
        return {
            "run_id": run_id,
            "mode": "script_language_demand_discovery",
            "state": state,
            "error_detail": error_detail,
            "trend_observations_added": trend_observations,
            "rapid_trend_evaluation": rapid_trend_evaluation,
            "predictions_added": 0,
            "phase_sequence": ["topic_performance_discovery"],
            "receipts": receipts,
            "outbox_records": outbox_records,
            "central_sync": {"state": "deferred_to_scheduler"},
            "discovery_topics": {
                "mode": "explicit_performance_query",
                "topics": [normalized_topic],
                "platforms": [platform],
                "adaptive_topics_enabled": False,
            },
        }

    def reserve_validation_forecasts(
        self,
        *,
        limit: Optional[int] = None,
    ) -> Dict[str, Any]:
        """Manually form a capacity-backed cohort through the same safe path."""

        run_id = new_run_id()
        self.store.start_run(run_id, "validation_forecast")
        state = "completed"
        error_detail = ""
        evaluation: Dict[str, Any] = {"state": "not_started"}
        capabilities: Dict[str, Any] = {"sources": [], "admitted": []}
        forecast: Dict[str, Any] = {"state": "not_started"}
        try:
            evaluation = self.store.evaluate_predictions()
            if load_active_model(self.config) is None:
                forecast = {
                    "state": "no_promoted_model",
                    "predictions_added": 0,
                    "reservations_added": 0,
                }
            else:
                capabilities = self._measurement_source_capabilities(run_id)
                forecast = self.store.forecast_active_trends(
                    run_id=run_id,
                    measurement_sources=capabilities["admitted"],
                    limit=(
                        self.config.prediction_validation_cohort_limit
                        if limit is None else max(1, int(limit))
                    ),
                )
            if forecast.get("state") == "blocked_measurement_capacity":
                state = "degraded"
        except Exception as error:
            state = "failed"
            error_detail = f"{error.__class__.__name__}: {str(error)[:500]}"
        finally:
            self.store.finish_run(
                run_id,
                state=state,
                error_detail=error_detail,
            )
        outbox_records = self.store.enqueue_run_for_sync(run_id)
        sink = SupabaseSink(self.config, self.store)
        try:
            sync_result = sink.flush()
        finally:
            sink.close()
        result = {
            "contract": "market_tape_capacity_backed_forecast_run_v1",
            "run_id": run_id,
            "state": state,
            "error_detail": error_detail,
            "prediction_evaluation": evaluation,
            "measurement_sources": capabilities,
            "forecast": forecast,
            "outbox_records": outbox_records,
            "central_sync": sync_result,
            "status": self.store.status(),
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
            rapid_trend_evaluation = self._evaluate_rapid_trends(run_id=run_id)
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
            "rapid_trend_evaluation": rapid_trend_evaluation,
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
        rapid_trend_evaluation: Dict[str, Any] = {"state": "not_started"}
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
                rapid_trend_evaluation = self._evaluate_rapid_trends(
                    run_id=run_id,
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
            "rapid_trend_evaluation": rapid_trend_evaluation,
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
        rapid_trend_evaluation: Dict[str, Any] = {"state": "not_started"}
        try:
            trend_observations = self.store.aggregate_trends()
            rapid_trend_evaluation = self._evaluate_rapid_trends(run_id=run_id)
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
            "rapid_trend_evaluation": rapid_trend_evaluation,
            "outbox_records": outbox_records,
            "central_sync": sync_result,
            "status": self.store.status(),
        }
        self._write_heartbeat(result)
        return result

    def _evaluate_rapid_trends(
        self,
        *,
        run_id: str,
        trend_ids: Optional[Sequence[str]] = None,
    ) -> Dict[str, Any]:
        """Keep trend detection isolated from the collection transaction.

        A trigger-side defect must be visible, but it must not discard already
        accepted observations or turn a completed provider collection into a
        failed run. This path performs no provider, AI, render, or publish call.
        """

        try:
            return self.rapid_trends.evaluate(
                source_run_id=run_id,
                trend_ids=trend_ids,
            )
        except Exception as error:  # fail closed without losing the tape
            return {
                "status": "error",
                "contract": "market_tape_rapid_trend_evaluation_v1",
                "state": "failed",
                "provider_calls_made": 0,
                "error_code": type(error).__name__,
                "error_detail": sanitize(str(error))[:300],
            }

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
        plan_receipt = self._save_adaptive_query_plan(run_id, sources)
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
                self._attach_adaptive_query_lineage(batch, run_id)
                self._persist_batch(batch, run_id)
                receipts.append(batch.receipt.to_dict())
        finally:
            for source in sources:
                source.close()
        if plan_receipt is not None:
            receipts.append(plan_receipt.to_dict())
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

    def _run_rechecks(
        self,
        run_id: str,
        *,
        phase: str = "all",
    ) -> List[Dict[str, Any]]:
        if phase not in {"all", "forecast_terminal", "scheduled"}:
            raise ValueError(
                "phase must be all, forecast_terminal, or scheduled"
            )
        sources = self._build_sources(
            run_id,
            budget_purpose=(
                "forecast_terminal"
                if phase in {"all", "forecast_terminal"}
                else "scheduled"
            ),
        )
        source_map = {source.source_id: source for source in sources}
        receipts: List[Dict[str, Any]] = []
        circuit_by_source: Dict[str, ProviderBatch | None] = {}
        capable_source_ids: set[str] = set()
        source_capability: List[Dict[str, Any]] = []
        try:
            for source in sources:
                circuit = self._circuit_open_batch(source)
                circuit_by_source[source.source_id] = circuit
                if source.platform not in self.config.platforms:
                    capability_state = "platform_disabled"
                elif isinstance(source, LocalResearchSource):
                    # This adapter can replay a Safari-produced archive, but
                    # ``refresh`` does not trigger a current provider read. It
                    # therefore cannot guarantee an observation inside a
                    # forecast's terminal scoring window and must not make the
                    # coverage planner report a false refresh capability.
                    capability_state = "archive_only_no_terminal_refresh"
                elif not source.terminal_metrics_capable():
                    capability_state = "metadata_only_no_terminal_metrics"
                elif source.request_budget <= 0:
                    capability_state = "request_budget_exhausted"
                elif source.metered and not self.config.allow_metered_reads:
                    capability_state = "metered_reads_disabled"
                elif not source.credentials_available():
                    capability_state = "credentials_unavailable"
                elif circuit is not None:
                    capability_state = "source_circuit_open"
                else:
                    capability_state = "refresh_capable"
                    capable_source_ids.add(source.source_id)
                source_capability.append({
                    "source_id": source.source_id,
                    "platform": source.platform,
                    "state": capability_state,
                    "request_budget_remaining": max(0, int(source.request_budget)),
                    "metered": bool(source.metered),
                })

            plan = self.store.due_poll_plan(
                self.config.max_due_rechecks_per_cycle,
                forecast_capable_platforms={
                    source.platform
                    for source in sources
                    if source.source_id in capable_source_ids
                },
                forecast_capable_source_ids=capable_source_ids,
                claim_run_id=(
                    run_id
                    if phase in {"all", "forecast_terminal"}
                    else None
                ),
                phase=phase,
            )
            due = plan["polls"]
            planner_receipt = self._save_recheck_planner_receipt(
                run_id,
                phase=phase,
                queue_receipt=plan["receipt"],
                source_capability=source_capability,
            )
            receipts.append(planner_receipt.to_dict())
            if not due:
                return receipts

            for platform, rows in due.items():
                grouped: Dict[Tuple[str, str], List[Dict[str, Any]]] = {}
                for row in rows:
                    lane = (
                        "forecast_terminal"
                        if row.get("forecast_coverage")
                        else "scheduled"
                    )
                    grouped.setdefault(
                        (str(row["preferred_source_id"]), lane),
                        [],
                    ).append(row)
                for (source_id, lane), tracked in grouped.items():
                    requires_terminal_refresh = lane == "forecast_terminal"
                    requires_exact_reserved_source = any(
                        row.get("measurement_reservation_ids")
                        for row in tracked
                    )
                    source = source_map.get(source_id)
                    if source is None and not requires_exact_reserved_source:
                        candidates = [candidate for candidate in sources if candidate.platform == platform]
                        source = (
                            next((
                                candidate
                                for candidate in candidates
                                if candidate.source_id in capable_source_ids
                            ), None)
                            if requires_terminal_refresh
                            else next(
                                (
                                    candidate
                                    for candidate in candidates
                                    if candidate.credentials_available()
                                ),
                                candidates[0] if candidates else None,
                            )
                        )
                    if (
                        requires_terminal_refresh
                        and not requires_exact_reserved_source
                        and (
                            source is None
                            or source.source_id not in capable_source_ids
                        )
                    ):
                        alternative = next((
                            candidate
                            for candidate in sources
                            if candidate.platform == platform
                            and candidate.source_id in capable_source_ids
                        ), None)
                        source = alternative
                    if source is None:
                        failure_code = (
                            "reserved_measurement_source_unavailable"
                            if requires_exact_reserved_source
                            else (
                                "forecast_refresh_capability_unavailable"
                                if requires_terminal_refresh
                                else "source_unavailable"
                            )
                        )
                        self.store.mark_poll_failure(
                            (row["video_id"] for row in tracked),
                            failure_code,
                        )
                        if requires_exact_reserved_source:
                            self.store.complete_forecast_measurements(
                                run_id,
                                source_id,
                                (),
                                error_code=failure_code,
                            )
                        continue
                    circuit_batch = circuit_by_source.get(source.source_id)
                    if circuit_batch is not None:
                        alternatives = [candidate for candidate in sources if (
                            candidate.platform == platform
                            and candidate.source_id != source.source_id
                            and (
                                candidate.source_id in capable_source_ids
                                if requires_terminal_refresh
                                else (
                                    candidate.credentials_available()
                                    and circuit_by_source.get(candidate.source_id)
                                    is None
                                )
                            )
                        )]
                        if alternatives and not requires_exact_reserved_source:
                            source = alternatives[0]
                            circuit_batch = None
                    if circuit_batch is not None:
                        self.store.mark_poll_failure(
                            (row["video_id"] for row in tracked), "source_circuit_open"
                        )
                        circuit_batch.receipt.metadata["recheck_queue"] = (
                            _recheck_batch_receipt(
                                tracked,
                                source.source_id,
                                planner_phase=phase,
                                selection_lane=lane,
                            )
                        )
                        self._persist_batch(circuit_batch, run_id)
                        if requires_exact_reserved_source:
                            completion = self.store.complete_forecast_measurements(
                                run_id,
                                source.source_id,
                                (),
                                error_code="source_circuit_open",
                                completed_at=circuit_batch.receipt.finished_at,
                            )
                            circuit_batch.receipt.metadata[
                                "measurement_completion"
                            ] = completion
                        receipts.append(circuit_batch.receipt.to_dict())
                        continue
                    batch = source.refresh(tracked)
                    batch.receipt.metadata["recheck_queue"] = _recheck_batch_receipt(
                        tracked,
                        source.source_id,
                        planner_phase=phase,
                        selection_lane=lane,
                    )
                    returned = {item.video_id for item in batch.items}
                    tracked_ids = {str(row["video_id"]) for row in tracked}
                    missing = sorted(tracked_ids - returned)
                    failure_codes_by_video: Dict[str, str] = {}
                    if missing:
                        item_failure_code = str(
                            batch.receipt.metadata.get("item_failure_code")
                            or batch.receipt.error_code
                            or "provider_item_missing"
                        )
                        batch.receipt.failed_count += len(missing)
                        batch.receipt.metadata.update({
                            "tracked_count": len(tracked_ids),
                            "returned_tracked_count": len(tracked_ids & returned),
                            "missing_tracked_count": len(missing),
                            "item_failure_code": item_failure_code,
                        })
                        self.store.mark_poll_failure(
                            missing,
                            item_failure_code,
                        )
                        failure_codes_by_video.update({
                            video_id: item_failure_code
                            for video_id in missing
                        })
                    accepted_ids = self._persist_batch(batch, run_id)
                    rejected_ids = {
                        str(value)
                        for value in batch.receipt.metadata.get(
                            "rejected_video_ids", []
                        )
                        if str(value)
                    }
                    failure_codes_by_video.update({
                        video_id: "counter_regression"
                        for video_id in rejected_ids
                    })
                    if requires_exact_reserved_source:
                        completion_error = str(
                            batch.receipt.error_code
                            or batch.receipt.metadata.get("item_failure_code")
                            or ""
                        )
                        completion = self.store.complete_forecast_measurements(
                            run_id,
                            source.source_id,
                            accepted_ids,
                            error_code=completion_error,
                            failure_codes_by_video=failure_codes_by_video,
                            completed_at=batch.receipt.finished_at,
                        )
                        batch.receipt.metadata[
                            "measurement_completion"
                        ] = completion
                        # _persist_batch saved the provider receipt before the
                        # reservation transition existed. Persist an immutable
                        # zero-request completion receipt rather than mutating
                        # that historical row in place.
                        completion_receipt = SourceReceipt(
                            run_id=run_id,
                            source_id=(
                                f"{source.source_id}-measurement-completion"
                            ),
                            platform=source.platform,
                            state=(
                                SourceState.READY
                                if completion["assignments_failed"] == 0
                                else SourceState.DEGRADED
                            ),
                            started_at=batch.receipt.finished_at,
                            finished_at=batch.receipt.finished_at,
                            request_count=0,
                            refreshed_count=completion[
                                "assignments_fulfilled"
                            ],
                            failed_count=completion["assignments_failed"],
                            error_code=(
                                completion_error
                                if completion["assignments_failed"]
                                else ""
                            ),
                            metadata={
                                "measurement_lane": "forecast_terminal",
                                "completion": completion,
                            },
                        )
                        self.store.save_receipt(completion_receipt)
                        receipts.append(completion_receipt.to_dict())
                    unchanged = returned - accepted_ids - rejected_ids
                    if unchanged:
                        self.store.defer_unchanged_polls(unchanged)
                    receipts.append(batch.receipt.to_dict())
        finally:
            for source in sources:
                source.close()
        return receipts

    def _save_recheck_planner_receipt(
        self,
        run_id: str,
        *,
        phase: str,
        queue_receipt: Dict[str, Any],
        source_capability: Sequence[Dict[str, Any]],
    ) -> SourceReceipt:
        """Persist an audit receipt for every planner phase, including no-ops."""

        receipt_payload = dict(queue_receipt)
        receipt_payload["source_capability"] = list(source_capability)
        degraded_planner_states = {
            "no_refresh_capable_platform",
            "no_refreshable_forecast_member",
            "refresh_capability_gap",
            "refresh_capability_and_cycle_capacity_gap",
            "cycle_capacity_limited",
        }
        planner_now = utc_now()
        planner_source_id = {
            "all": "market-tape-recheck-planner",
            "forecast_terminal": "market-tape-recheck-planner-terminal",
            "scheduled": "market-tape-recheck-planner-scheduled",
        }[phase]
        degraded = (
            receipt_payload["coverage_state"] in degraded_planner_states
        )
        planner_receipt = SourceReceipt(
            run_id=run_id,
            source_id=planner_source_id,
            platform="all",
            state=SourceState.DEGRADED if degraded else SourceState.READY,
            started_at=planner_now,
            finished_at=planner_now,
            request_count=0,
            discovered_count=0,
            refreshed_count=0,
            error_code=(
                str(receipt_payload["coverage_state"]) if degraded else ""
            ),
            error_detail=(
                "Active-model forecast coverage could not be fully queued"
                if degraded else ""
            ),
            metadata={
                "recheck_phase": phase,
                "selection_lane": receipt_payload["selection_lane"],
                "recheck_plan": receipt_payload,
            },
        )
        self.store.save_receipt(planner_receipt)
        return planner_receipt

    def _build_sources(
        self,
        run_id: str,
        adaptive_topics: bool = False,
        *,
        budget_purpose: str = "general",
    ):
        def guarded_budget(source_id: str, daily_limit: int) -> int:
            if self.store.daily_provider_cost() >= self.config.max_daily_provider_cost_usd:
                return 0
            return self.store.remaining_request_budget(
                source_id,
                daily_limit,
                purpose=budget_purpose,
            )

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

    def _measurement_source_capabilities(
        self,
        run_id: str,
    ) -> Dict[str, Any]:
        """Resolve secret-safe terminal capacity without making provider calls."""

        sources = self._build_sources(
            run_id,
            budget_purpose="reservation",
        )
        capability_receipts: List[Dict[str, Any]] = []
        admitted: List[Dict[str, Any]] = []
        try:
            for source in sources:
                circuit = self._circuit_open_batch(source)
                retry_status = self.store.source_retry_status(
                    source.source_id
                )
                if source.platform not in self.config.platforms:
                    state = "platform_disabled"
                elif not source.terminal_metrics_capable():
                    state = "metadata_only_no_terminal_metrics"
                elif source.metered and not self.config.allow_metered_reads:
                    state = "metered_reads_disabled"
                elif not source.credentials_available():
                    state = "credentials_unavailable"
                elif circuit is not None:
                    state = "source_circuit_open"
                elif not retry_status.get("last_success_at"):
                    state = "refresh_success_unproven"
                elif source.request_budget <= 0:
                    state = "request_budget_exhausted"
                else:
                    state = "refresh_capable"
                receipt = {
                    "source_id": source.source_id,
                    "platform": source.platform,
                    "state": state,
                    "daily_request_limit": self.config.request_limit_for(
                        source.platform
                    ),
                    "request_budget_remaining": max(
                        0, int(source.request_budget)
                    ),
                    "refresh_batch_size": max(
                        1, int(source.measurement_refresh_batch_size())
                    ),
                    "request_units_per_batch": max(
                        1, int(source.measurement_request_units_per_batch())
                    ),
                    "credential_fingerprint": source.credential_fingerprint(),
                    "metered": bool(source.metered),
                    "last_success_at": retry_status.get("last_success_at"),
                }
                capability_receipts.append(receipt)
                if state == "refresh_capable":
                    admitted.append(receipt)
        finally:
            for source in sources:
                source.close()
        return {
            "contract": "market_tape_measurement_source_capabilities_v1",
            "sources": capability_receipts,
            "admitted": admitted,
            "selection_sha256": hashlib.sha256(json.dumps(
                capability_receipts,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")).hexdigest(),
        }

    def _forecast_measurement_work_required(self) -> bool:
        if load_active_model(self.config) is not None:
            return True
        now = isoformat(utc_now())
        with self.store.connect() as connection:
            row = connection.execute(
                """SELECT 1
                   FROM mt_forecast_measurement_reservations
                   WHERE state IN ('reserved', 'claimed')
                     AND deadline_at > ?
                   LIMIT 1""",
                (now,),
            ).fetchone()
        return row is not None

    def _scheduled_poll_work_required(self) -> bool:
        with self.store.connect() as connection:
            return connection.execute(
                "SELECT 1 FROM mt_poll_queue WHERE due_at <= ? LIMIT 1",
                (isoformat(utc_now()),),
            ).fetchone() is not None

    def _adaptive_discovery_config(self) -> MarketTapeConfig:
        selected_at = utc_now()
        limit = max(1, min(100, int(self.config.adaptive_topic_limit)))
        configured = list(dict.fromkeys(
            topic.strip() for topic in self.config.topics if topic.strip()
        ))
        rotated_configured = self._rotated_configured_topics(configured, selected_at)
        if not self.config.adaptive_topics_enabled:
            self._last_discovery_topics = {
                "contract": "market_tape_adaptive_query_feedback_v1",
                "mode": "configured",
                "selected_at": isoformat(selected_at),
                "topics": configured,
                "signals": [],
                "admitted_feedback_signals": [],
                "baseline_topics": configured,
                "selection_sha256": "",
            }
            return self.config

        window_hours = max(1, min(24 * 90, int(
            self.config.adaptive_topic_window_hours
        )))
        minimum_videos = max(2, int(self.config.adaptive_topic_min_videos))
        freshness_cutoff = selected_at - timedelta(hours=window_hours)
        text_signals = self.store.keyword_signals(
            limit=limit * 5,
            window_hours=window_hours,
            min_videos=minimum_videos,
        )
        query_signals = self.store.discovery_query_signals(
            limit=limit * 5,
            window_hours=window_hours,
            min_videos=minimum_videos,
        )
        by_keyword = {
            self._query_family_key(signal.get("keyword")): signal
            for signal in text_signals
            if signal.get("keyword")
        }
        for signal in query_signals:
            if signal.get("keyword"):
                by_keyword[self._query_family_key(signal["keyword"])] = signal
        signals = list(by_keyword.values())
        utc_day_start = selected_at.replace(hour=0, minute=0, second=0, microsecond=0)
        cooldown_hours = max(0, min(24 * 30, int(
            self.config.adaptive_topic_cooldown_hours
        )))
        cooldown_start = selected_at - timedelta(hours=max(1, cooldown_hours))
        daily_usage = self.store.adaptive_query_feedback_usage(utc_day_start)
        cooldown_usage = self.store.adaptive_query_feedback_usage(cooldown_start)
        daily_limit = max(0, min(1000, int(
            self.config.adaptive_topic_daily_feedback_limit
        )))
        family_daily_limit = max(0, min(100, int(
            self.config.adaptive_topic_family_daily_limit
        )))
        daily_used = int(daily_usage.get("feedback_selections") or 0)
        daily_remaining = max(0, daily_limit - daily_used)
        configured_keys = {self._query_family_key(topic) for topic in configured}
        candidates: List[Dict[str, Any]] = []
        exclusions: List[Dict[str, Any]] = []
        for signal in signals:
            keyword = str(signal.get("keyword") or "").strip()
            family = self._query_family_key(keyword)
            reasons: List[str] = []
            latest_observed = parse_datetime(signal.get("latest_observed_at"))
            if not signal.get("query_ready"):
                reasons.append("not_query_ready")
            if int(signal.get("videos_total") or 0) < minimum_videos:
                reasons.append("insufficient_video_breadth")
            if int(signal.get("creators_total") or 0) < 2:
                reasons.append("insufficient_creator_breadth")
            if not latest_observed or latest_observed < freshness_cutoff:
                reasons.append("outside_current_clock_window")
            if family in AUTONOMOUS_QUERY_NOISE:
                reasons.append("autonomous_query_noise")
            if family in configured_keys:
                reasons.append("reserved_configured_baseline")
            family_daily = (daily_usage.get("families") or {}).get(family) or {}
            if family_daily_limit <= 0 or int(family_daily.get("selection_count") or 0) >= family_daily_limit:
                reasons.append("query_family_daily_budget_exhausted")
            family_cooldown = (cooldown_usage.get("families") or {}).get(family) or {}
            if cooldown_hours > 0 and family_cooldown.get("latest_activity_at"):
                reasons.append("query_family_cooldown_active")
            if daily_remaining <= 0:
                reasons.append("adaptive_daily_budget_exhausted")
            if reasons:
                exclusions.append({
                    "keyword": keyword,
                    "keyword_type": str(signal.get("keyword_type") or "keyword"),
                    "reasons": sorted(set(reasons)),
                    "latest_activity_at": family_cooldown.get("latest_activity_at"),
                    "daily_selection_count": int(family_daily.get("selection_count") or 0),
                })
                continue
            candidates.append(signal)
        candidates.sort(key=self._discovery_priority, reverse=True)

        exploration_fraction = max(
            0.0, min(0.5, float(self.config.adaptive_topic_exploration_fraction))
        )
        baseline_target = min(
            len(configured),
            max(1, round(limit * exploration_fraction)) if configured else 0,
        )
        feedback_capacity = min(
            max(0, limit - baseline_target),
            daily_remaining,
        )
        direct_candidates = [
            signal for signal in candidates
            if str(signal.get("keyword_type") or "") == "query"
        ]
        derived_candidates = [
            signal for signal in candidates
            if str(signal.get("keyword_type") or "") != "query"
        ]
        direct_fraction = max(0.0, min(0.75, float(
            self.config.adaptive_topic_direct_query_fraction
        )))
        direct_target = min(
            feedback_capacity,
            len(direct_candidates),
            max(1, round(feedback_capacity * direct_fraction))
            if direct_candidates and feedback_capacity
            else 0,
        )
        direct_selected = self._diverse_keyword_signals(
            direct_candidates,
            direct_target,
        )
        direct_keys = {
            self._query_family_key(signal.get("keyword"))
            for signal in direct_selected
        }
        ordered_feedback = [
            *direct_selected,
            *derived_candidates,
            *(
                signal for signal in direct_candidates
                if self._query_family_key(signal.get("keyword")) not in direct_keys
            ),
        ]
        selected_signals = self._diverse_keyword_signals(
            ordered_feedback,
            feedback_capacity,
        )
        selected_signal_keys = {
            self._query_family_key(signal.get("keyword"))
            for signal in selected_signals
        }
        for signal in candidates:
            family = self._query_family_key(signal.get("keyword"))
            if family in selected_signal_keys:
                continue
            exclusions.append({
                "keyword": str(signal.get("keyword") or ""),
                "keyword_type": str(signal.get("keyword_type") or "keyword"),
                "reasons": ["portfolio_capacity_or_diversity_dedup"],
                "latest_activity_at": None,
                "daily_selection_count": 0,
            })
        feedback_topics = [str(signal["keyword"]) for signal in selected_signals]
        baseline_topics = [
            topic for topic in rotated_configured
            if self._query_family_key(topic) not in selected_signal_keys
        ][:baseline_target]
        topics = [*feedback_topics, *baseline_topics]
        if len(topics) < limit:
            existing = {self._query_family_key(topic) for topic in topics}
            topics.extend(
                topic for topic in rotated_configured
                if self._query_family_key(topic) not in existing
            )
        topics = list(dict.fromkeys(topics))[:limit]
        signal_receipts = [self._adaptive_signal_receipt(signal) for signal in selected_signals]
        direct_count = sum(signal["selection_lane"] == "direct_current_query" for signal in signal_receipts)
        mode = "adaptive" if signal_receipts else "configured_fallback"
        plan = {
            "contract": "market_tape_adaptive_query_feedback_v1",
            "mode": mode,
            "selected_at": isoformat(selected_at),
            "freshness_cutoff": isoformat(freshness_cutoff),
            "topics": topics,
            "adaptive_count": len(signal_receipts),
            "exploration_count": max(0, len(topics) - len(signal_receipts)),
            "direct_current_count": direct_count,
            "derived_feedback_count": len(signal_receipts) - direct_count,
            "window_hours": window_hours,
            "minimum_videos": minimum_videos,
            "baseline_topics": [
                topic for topic in topics
                if self._query_family_key(topic) not in selected_signal_keys
            ],
            "signals": signal_receipts,
            "admitted_feedback_signals": signal_receipts,
            "budgets": {
                "contract": "market_tape_adaptive_query_budget_v1",
                "utc_day": selected_at.date().isoformat(),
                "daily_feedback_limit": daily_limit,
                "daily_feedback_used_before_selection": daily_used,
                "daily_feedback_remaining_before_selection": daily_remaining,
                "daily_feedback_admitted": len(signal_receipts),
                "daily_feedback_remaining_after_selection": max(
                    0, daily_remaining - len(signal_receipts)
                ),
                "query_family_daily_limit": family_daily_limit,
                "cooldown_hours": cooldown_hours,
                "provider_requests": (
                    "bounded independently by each source's existing daily request "
                    "limit and the global provider-cost ceiling"
                ),
            },
            "excluded_candidates": exclusions,
            "excluded_candidates_preview": exclusions[:100],
            "excluded_candidates_total": len(exclusions),
            "selection_sha256": "",
        }
        canonical = json.dumps(plan, sort_keys=True, separators=(",", ":")).encode("utf-8")
        plan["selection_sha256"] = hashlib.sha256(canonical).hexdigest()
        self._last_discovery_topics = plan
        return replace(self.config, topics=topics)

    @staticmethod
    def _rotated_configured_topics(
        configured: Sequence[str], selected_at: datetime
    ) -> List[str]:
        if not configured:
            return []
        offset = selected_at.date().toordinal() % len(configured)
        return [*configured[offset:], *configured[:offset]]

    @staticmethod
    def _query_family_key(value: Any) -> str:
        return " ".join(str(value or "").casefold().split())[:300]

    @staticmethod
    def _adaptive_signal_receipt(signal: Dict[str, Any]) -> Dict[str, Any]:
        examples = [
            {
                key: example.get(key)
                for key in (
                    "video_id", "platform", "title", "views", "observed_at",
                    "observation_age_hours", "url", "contribution",
                )
            }
            for example in signal.get("examples") or []
            if isinstance(example, dict)
        ]
        keyword_type = str(signal.get("keyword_type") or "keyword")
        return {
            "contract": "market_tape_adaptive_query_signal_v1",
            "keyword": str(signal.get("keyword") or ""),
            "keyword_type": keyword_type,
            "selection_lane": (
                "direct_current_query" if keyword_type == "query" else "derived_market_term"
            ),
            "evidence_source": (
                "mt_discovery_attributions"
                if keyword_type == "query"
                else "mt_videos_plus_latest_mt_market_observations"
            ),
            "rank": signal.get("rank"),
            "score": signal.get("score"),
            "confidence": signal.get("confidence"),
            "videos_total": int(signal.get("videos_total") or 0),
            "creators_total": int(signal.get("creators_total") or 0),
            "platforms": list(signal.get("platforms") or []),
            "platforms_total": int(signal.get("platforms_total") or 0),
            "repeated_videos": int(signal.get("repeated_videos") or 0),
            "views_total": int(signal.get("views_total") or 0),
            "latest_observed_at": signal.get("latest_observed_at"),
            "evidence_video_ids": [
                str(example.get("video_id")) for example in examples if example.get("video_id")
            ],
            "evidence_urls": [
                str(example.get("url")) for example in examples if example.get("url")
            ],
            "examples": examples,
        }

    def _save_adaptive_query_plan(
        self,
        run_id: str,
        sources: Sequence[Any],
    ) -> SourceReceipt | None:
        plan = self._last_discovery_topics
        if plan.get("contract") != "market_tape_adaptive_query_feedback_v1":
            return None
        global_remaining = max(
            0,
            self.config.daily_unique_target - self.store.daily_unique_count(),
        )
        executable_sources = [
            source for source in sources
            if self.config.target_for(source.platform)
            > self.store.daily_unique_count(source.platform)
            and source.adaptive_query_execution_capable()
            and int(source.request_budget) > 0
            and source.credentials_available()
            and (not source.metered or self.config.allow_metered_reads)
            and self._circuit_open_batch(source) is None
        ]
        execution_admitted = bool(global_remaining > 0 and executable_sources)
        plan["execution_admitted"] = execution_admitted
        plan["execution_source_ids"] = sorted({
            str(source.source_id) for source in executable_sources
        })
        proposal_signals = list(plan.get("signals") or [])
        proposal_sha256 = str(plan.get("selection_sha256") or "")
        budgets = plan.get("budgets")
        selected_at = parse_datetime(plan.get("selected_at")) or utc_now()
        reservation_at = utc_now()
        cooldown_hours = max(0, min(24 * 30, int(
            self.config.adaptive_topic_cooldown_hours
        )))
        cooldown_boundary = reservation_at - timedelta(hours=cooldown_hours)
        if execution_admitted and proposal_signals and isinstance(budgets, dict):
            atomic_admission = self.store.reserve_adaptive_query_admissions(
                run_id=run_id,
                admitted_at=reservation_at,
                candidates=proposal_signals,
                daily_limit=int(budgets.get("daily_feedback_limit") or 0),
                family_daily_limit=int(budgets.get("query_family_daily_limit") or 0),
                cooldown_boundary=cooldown_boundary,
                cooldown_hours=cooldown_hours,
                proposal_sha256=proposal_sha256,
            )
        else:
            atomic_admission = {
                "contract": "market_tape_adaptive_query_atomic_admission_v1",
                "run_id": run_id,
                "utc_day": reservation_at.date().isoformat(),
                "admitted_at": isoformat(reservation_at),
                "proposal_sha256": proposal_sha256,
                "daily_limit": int((budgets or {}).get("daily_feedback_limit") or 0),
                "family_daily_limit": int((budgets or {}).get("query_family_daily_limit") or 0),
                "cooldown_hours": cooldown_hours,
                "requested_cooldown_boundary": isoformat(cooldown_boundary),
                "cooldown_boundary": isoformat(cooldown_boundary),
                "daily_used_before": int(
                    (budgets or {}).get("daily_feedback_used_before_selection") or 0
                ),
                "daily_used_after": int(
                    (budgets or {}).get("daily_feedback_used_before_selection") or 0
                ),
                "new_admissions": 0,
                "admitted": [],
                "rejected": [
                    {
                        "query_family": self._query_family_key(signal.get("keyword")),
                        "keyword": str(signal.get("keyword") or ""),
                        "selection_lane": str(signal.get("selection_lane") or ""),
                        "reasons": ["no_executable_feedback_source"],
                        "daily_used": int(
                            (budgets or {}).get("daily_feedback_used_before_selection") or 0
                        ),
                        "family_used": 0,
                    }
                    for signal in proposal_signals
                ],
                "state": "no_executable_feedback_source" if proposal_signals else "no_feedback_proposed",
            }
        admitted_keys = {
            self._query_family_key(row.get("query_family"))
            for row in atomic_admission.get("admitted") or []
        }
        admitted_signals = [
            signal for signal in proposal_signals
            if self._query_family_key(signal.get("keyword")) in admitted_keys
        ]
        exclusions = list(plan.get("excluded_candidates") or [])
        exclusions.extend({
            "keyword": str(row.get("keyword") or ""),
            "keyword_type": "atomic_admission",
            "reasons": list(row.get("reasons") or []),
            "latest_activity_at": row.get("latest_cooldown_activity_at"),
            "latest_cooldown_activity_at": row.get(
                "latest_cooldown_activity_at"
            ),
            "cooldown_sources": list(row.get("cooldown_sources") or []),
            "cooldown_hours": int(
                atomic_admission.get("cooldown_hours") or 0
            ),
            "cooldown_boundary": atomic_admission.get("cooldown_boundary"),
            "requested_cooldown_boundary": atomic_admission.get(
                "requested_cooldown_boundary"
            ),
            "daily_selection_count": int(row.get("daily_used") or 0),
        } for row in atomic_admission.get("rejected") or [])
        plan["proposal_sha256"] = proposal_sha256
        plan["proposed_feedback_signals"] = proposal_signals
        plan["signals"] = admitted_signals
        plan["admitted_feedback_signals"] = admitted_signals
        plan["atomic_admission"] = atomic_admission
        plan["excluded_candidates"] = exclusions
        plan["excluded_candidates_preview"] = exclusions[:100]
        plan["excluded_candidates_total"] = len(exclusions)
        if isinstance(budgets, dict):
            admitted = len(admitted_signals)
            daily_limit = int(budgets.get("daily_feedback_limit") or 0)
            daily_used_before = int(
                atomic_admission.get("daily_used_before") or 0
            )
            daily_used_after = int(
                atomic_admission.get("daily_used_after") or 0
            )
            budgets["utc_day"] = str(atomic_admission.get("utc_day") or "")
            budgets["daily_feedback_used_before_selection"] = daily_used_before
            budgets["daily_feedback_remaining_before_selection"] = max(
                0,
                daily_limit - daily_used_before,
            )
            budgets["daily_feedback_admitted"] = admitted
            budgets["daily_feedback_used_after_selection"] = daily_used_after
            budgets["daily_feedback_remaining_after_selection"] = max(
                0,
                daily_limit - daily_used_after,
            )
            budgets["atomic_contract"] = atomic_admission["contract"]
            budgets["atomic_cooldown_boundary"] = atomic_admission.get(
                "cooldown_boundary"
            )
        admitted_families = {
            self._query_family_key(signal.get("keyword")) for signal in admitted_signals
        }
        configured = list(dict.fromkeys(
            topic.strip() for topic in self.config.topics if topic.strip()
        ))
        baseline = [
            topic for topic in plan.get("baseline_topics") or []
            if self._query_family_key(topic) not in admitted_families
        ]
        final_topics = [
            *(str(signal.get("keyword") or "") for signal in admitted_signals),
            *baseline,
        ]
        if self.config.adaptive_topics_enabled:
            maximum = max(1, min(100, int(self.config.adaptive_topic_limit)))
            existing = {self._query_family_key(topic) for topic in final_topics}
            final_topics.extend(
                topic for topic in self._rotated_configured_topics(configured, selected_at)
                if self._query_family_key(topic) not in existing
            )
            final_topics = list(dict.fromkeys(final_topics))[:maximum]
        else:
            final_topics = configured
        plan["topics"] = final_topics
        plan["baseline_topics"] = [
            topic for topic in final_topics
            if self._query_family_key(topic) not in admitted_families
        ]
        plan["adaptive_count"] = len(admitted_signals)
        plan["direct_current_count"] = sum(
            signal.get("selection_lane") == "direct_current_query"
            for signal in admitted_signals
        )
        plan["derived_feedback_count"] = (
            len(admitted_signals) - int(plan["direct_current_count"])
        )
        plan["exploration_count"] = max(0, len(final_topics) - len(admitted_signals))
        for source in sources:
            source.config = replace(source.config, topics=final_topics)
        plan["selection_sha256"] = ""
        canonical = json.dumps(
            plan,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        plan["selection_sha256"] = hashlib.sha256(canonical).hexdigest()
        now = utc_now()
        receipt = SourceReceipt(
            run_id=run_id,
            source_id="market-tape-adaptive-query-planner",
            platform="all",
            state=SourceState.READY,
            started_at=now,
            finished_at=now,
            request_count=0,
            discovered_count=0,
            metadata={"adaptive_query_selection": plan},
        )
        self.store.save_receipt(receipt)
        return receipt

    def _attach_adaptive_query_lineage(
        self,
        batch: ProviderBatch,
        run_id: str,
    ) -> None:
        plan = self._last_discovery_topics
        if plan.get("contract") != "market_tape_adaptive_query_feedback_v1":
            return
        signals = {
            self._query_family_key(signal.get("keyword")): signal
            for signal in plan.get("admitted_feedback_signals") or []
            if isinstance(signal, dict) and signal.get("keyword")
        }
        baseline = {
            self._query_family_key(topic)
            for topic in plan.get("baseline_topics") or []
        }
        batch.receipt.metadata["adaptive_query_plan"] = {
            "contract": plan["contract"],
            "planner_source_id": "market-tape-adaptive-query-planner",
            "planner_run_id": run_id,
            "selection_sha256": plan.get("selection_sha256"),
            "selected_at": plan.get("selected_at"),
            "execution_admitted": bool(plan.get("execution_admitted")),
            "feedback_query_families": sorted(signals),
            "baseline_query_families": sorted(baseline),
        }
        enriched: List[QueryAttempt] = []
        for attempt in batch.query_attempts:
            family = self._query_family_key(
                attempt.metadata.get("query_family") or attempt.query
            )
            signal = signals.get(family)
            if signal:
                origin = {
                    **signal,
                    "planner_run_id": run_id,
                    "selection_sha256": plan.get("selection_sha256"),
                    "selected_at": plan.get("selected_at"),
                }
            elif family in baseline:
                origin = {
                    "contract": "market_tape_adaptive_query_signal_v1",
                    "keyword": family,
                    "keyword_type": "configured",
                    "selection_lane": "configured_baseline",
                    "evidence_source": "configured_market_baseline",
                    "planner_run_id": run_id,
                    "selection_sha256": plan.get("selection_sha256"),
                    "selected_at": plan.get("selected_at"),
                }
            else:
                origin = {
                    "contract": "market_tape_adaptive_query_signal_v1",
                    "keyword": family,
                    "keyword_type": "provider_expansion",
                    "selection_lane": "provider_expansion",
                    "evidence_source": "provider_query_expansion",
                    "planner_run_id": run_id,
                    "selection_sha256": plan.get("selection_sha256"),
                    "selected_at": plan.get("selected_at"),
                }
            enriched.append(replace(
                attempt,
                metadata={
                    **attempt.metadata,
                    "adaptive_query_lineage": origin,
                },
            ))
        batch.query_attempts = enriched

    @staticmethod
    def _diverse_keyword_signals(
        candidates: Sequence[Dict[str, Any]], limit: int
    ) -> List[Dict[str, Any]]:
        maximum = max(0, int(limit))
        if maximum == 0:
            return []
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
            if len(selected) >= maximum:
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
        state = str(retry.get("state", "degraded"))
        current_credential_fingerprint = source.credential_fingerprint()
        credential_sensitive_failure = (
            state == SourceState.BLOCKED_CREDENTIAL.value
            or retry.get("error_code") == "provider_auth_or_quota"
        )
        if (
            credential_sensitive_failure
            and current_credential_fingerprint
            and current_credential_fingerprint
            != str(retry.get("credential_fingerprint") or "")
        ):
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
        provider_item_failures = batch.receipt.failed_count
        ingest_failures = 0
        new_unique_count = 0
        ingest_failure_types: Dict[str, int] = {}
        rejected_video_ids: set[str] = set()
        counter_regressions: List[Dict[str, Any]] = []
        for item in batch.items:
            try:
                observation_added, unique_added = self.store.ingest(item, run_id)
                if observation_added:
                    accepted += 1
                    accepted_ids.add(item.video_id)
                else:
                    duplicates += 1
                if unique_added:
                    new_unique_count += 1
            except CounterRegressionError as error:
                ingest_failures += 1
                rejected_video_ids.add(error.video_id)
                ingest_failure_types["CounterRegressionError"] = (
                    ingest_failure_types.get("CounterRegressionError", 0) + 1
                )
                counter_regressions.append({
                    "video_id": error.video_id,
                    "observation_id": error.observation_id,
                    "views": error.views,
                    "prior_observation_id": error.prior_observation_id,
                    "prior_views": error.prior_views,
                    "error_code": "counter_regression",
                })
                self.store.mark_poll_failure(
                    (error.video_id,),
                    "counter_regression",
                )
            except (TypeError, ValueError, KeyError) as error:
                ingest_failures += 1
                error_type = type(error).__name__
                ingest_failure_types[error_type] = ingest_failure_types.get(error_type, 0) + 1
        batch.receipt.accepted_count = accepted
        batch.receipt.duplicate_count = duplicates
        batch.receipt.failed_count = provider_item_failures + ingest_failures
        batch.receipt.metadata["new_observation_count"] = accepted
        batch.receipt.metadata["new_unique_count"] = new_unique_count
        batch.receipt.metadata["watermark_advanced"] = accepted > 0
        if ingest_failures:
            batch.receipt.metadata["ingest_failure_count"] = ingest_failures
            batch.receipt.metadata["ingest_failure_types"] = ingest_failure_types
        if counter_regressions:
            batch.receipt.metadata.update({
                "counter_regression_count": len(counter_regressions),
                "counter_regressions": counter_regressions,
                "rejected_video_ids": sorted(rejected_video_ids),
            })
            batch.receipt.metadata.setdefault(
                "item_failure_code", "counter_regression"
            )
        only_counter_regressions = bool(
            counter_regressions
            and len(counter_regressions) == ingest_failures
        )
        if (
            batch.receipt.state == SourceState.READY
            and only_counter_regressions
            and not accepted
        ):
            batch.receipt.state = SourceState.DEGRADED
            batch.receipt.error_code = (
                batch.receipt.error_code or "counter_regression"
            )
            batch.receipt.error_detail = (
                batch.receipt.error_detail
                or "Provider cumulative counters moved backwards; raw rows were quarantined"
            )
        if batch.receipt.state == SourceState.READY and ingest_failures and not accepted:
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


def _recheck_batch_receipt(
    tracked: Sequence[Dict[str, Any]],
    selected_source_id: str,
    *,
    planner_phase: str,
    selection_lane: str,
) -> Dict[str, Any]:
    assignments: List[Dict[str, Any]] = []
    reason_counts: Dict[str, int] = {}
    prediction_ids: set[int] = set()
    trend_ids: set[str] = set()
    deadlines: set[str] = set()
    reservation_ids: set[str] = set()
    for row in tracked:
        reason = str(row.get("recheck_reason") or "scheduled_poll_due")
        reason_counts[reason] = reason_counts.get(reason, 0) + 1
        coverage = row.get("forecast_coverage") or []
        assignment_prediction_ids = sorted({
            int(obligation["prediction_id"]) for obligation in coverage
        })
        assignment_trend_ids = sorted({
            str(obligation["trend_id"]) for obligation in coverage
        })
        assignment_deadlines = sorted({
            str(obligation["coverage_deadline_at"]) for obligation in coverage
        })
        prediction_ids.update(assignment_prediction_ids)
        trend_ids.update(assignment_trend_ids)
        deadlines.update(assignment_deadlines)
        assignment_reservation_ids = sorted({
            str(value)
            for value in row.get("measurement_reservation_ids") or []
            if str(value)
        })
        reservation_ids.update(assignment_reservation_ids)
        assignments.append({
            "video_id": str(row.get("video_id") or ""),
            "platform": str(row.get("platform") or ""),
            "preferred_source_id": str(row.get("preferred_source_id") or ""),
            "selected_source_id": selected_source_id,
            "scheduled_due_at": str(row.get("due_at") or ""),
            "recheck_reason": reason,
            "planner_phase": planner_phase,
            "selection_lane": selection_lane,
            "coverage_prediction_ids": assignment_prediction_ids,
            "coverage_trend_ids": assignment_trend_ids,
            "coverage_deadlines": assignment_deadlines,
            "measurement_reservation_ids": assignment_reservation_ids,
        })
    canonical = json.dumps(
        assignments,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return {
        "contract": "market_tape_recheck_batch_receipt_v1",
        "planner_phase": planner_phase,
        "selection_lane": selection_lane,
        "selected_source_id": selected_source_id,
        "tracked_count": len(tracked),
        "reason_counts": dict(sorted(reason_counts.items())),
        "coverage_prediction_count": len(prediction_ids),
        "coverage_trend_count": len(trend_ids),
        "coverage_prediction_ids": sorted(prediction_ids),
        "coverage_trend_ids": sorted(trend_ids),
        "coverage_deadlines": sorted(deadlines),
        "measurement_reservation_ids": sorted(reservation_ids),
        "assignments": assignments,
        "assignments_sha256": hashlib.sha256(canonical).hexdigest(),
    }


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()
