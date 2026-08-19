"""Autonomous discovery, recheck, mapping, and receipt orchestration."""

from __future__ import annotations

import json
from dataclasses import replace
from typing import Any, Dict, Iterable, List, Sequence, Tuple

from .config import MarketTapeConfig
from .models import MarketContent, ProviderBatch, SourceReceipt, SourceState, isoformat, new_run_id, utc_now
from .sources import build_sources
from .sources.local_research import LocalResearchSource
from .sinks import SupabaseSink
from .store import MarketTapeStore


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

    def _run_discovery(self, run_id: str) -> List[Dict[str, Any]]:
        sources = sorted(
            self._build_sources(run_id),
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
                    self._persist_batch(batch, run_id)
                    receipts.append(batch.receipt.to_dict())
        finally:
            for source in sources:
                source.close()
        return receipts

    def _build_sources(self, run_id: str):
        def guarded_budget(source_id: str, daily_limit: int) -> int:
            if self.store.daily_provider_cost() >= self.config.max_daily_provider_cost_usd:
                return 0
            return self.store.remaining_request_budget(source_id, daily_limit)

        sources = self.source_builder(self.config, run_id, guarded_budget)
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

    def _circuit_open_batch(self, source: Any) -> ProviderBatch | None:
        # A browser scheduler outage must never block cost-free archive ingestion.
        # LocalResearchSource reports trigger health inside its own receipt after reading files.
        if isinstance(source, LocalResearchSource):
            return None
        retry = self.store.source_retry_status(source.source_id)
        if not retry.get("blocked"):
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

    def _persist_batch(self, batch: ProviderBatch, run_id: str) -> None:
        accepted = 0
        duplicates = 0
        failures = batch.receipt.failed_count
        for item in batch.items:
            try:
                added, _ = self.store.ingest(item, run_id)
                if added:
                    accepted += 1
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
        self.store.save_receipt(batch.receipt)

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
