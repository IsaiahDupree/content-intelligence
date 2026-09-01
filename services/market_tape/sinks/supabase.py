"""Transactional outbox delivery to the shared Supabase control plane."""

from __future__ import annotations

import json
import os
from collections import defaultdict
from typing import Any, Dict, List, Optional, Tuple

import httpx

from ..config import MarketTapeConfig
from ..sources.base import sanitize
from ..store import MarketTapeStore


ENTITY_TABLES: Dict[str, Tuple[str, str, bool]] = {
    "upwork_request_reservation": (
        "actp_upwork_request_reservations", "request_reservation_id", False,
    ),
    "upwork_scan_run": (
        "actp_upwork_scan_runs", "scan_run_id", False,
    ),
    "upwork_job": ("actp_upwork_market_jobs", "job_id", False),
    "upwork_job_version": (
        "actp_upwork_job_versions", "job_version_id", False,
    ),
    "upwork_query_observation": (
        "actp_upwork_query_observations", "query_observation_id", False,
    ),
    "upwork_job_observation": (
        "actp_upwork_job_observations", "job_observation_id", False,
    ),
    "upwork_demand_snapshot": (
        "actp_upwork_demand_snapshots", "demand_snapshot_id", False,
    ),
    "upwork_prediction": (
        "actp_upwork_predictions", "prediction_id", False,
    ),
    "upwork_prediction_outcome": (
        "actp_upwork_prediction_outcomes", "prediction_outcome_id", False,
    ),
    "upwork_semantic_link": (
        "actp_upwork_semantic_links", "semantic_link_id", False,
    ),
    "semantic_graph_version": (
        "actp_semantic_topic_graph_versions", "graph_version_id", False,
    ),
    "semantic_topic_node": (
        "actp_semantic_topic_nodes", "graph_version_id,topic_id", False,
    ),
    "semantic_topic_edge": (
        "actp_semantic_topic_edges", "graph_version_id,edge_id", False,
    ),
    "semantic_signal_candidate": (
        "actp_semantic_signal_candidates", "signal_id", False,
    ),
    "semantic_signal_binding": (
        "actp_semantic_signal_bindings", "binding_id", False,
    ),
    "semantic_resolution_run": (
        "actp_semantic_resolution_runs", "resolution_run_id", False,
    ),
    "semantic_topic_observation": (
        "actp_semantic_topic_observations", "topic_observation_key", False,
    ),
    "semantic_atomic_selection": (
        "actp_semantic_atomic_topic_selections", "selection_id", False,
    ),
    "semantic_atomic_selection_source": (
        "actp_semantic_atomic_selection_sources",
        "selection_id,binding_id,topic_observation_key",
        False,
    ),
    "semantic_evidence_receipt": (
        "actp_semantic_content_evidence_receipts", "receipt_id", False,
    ),
    "semantic_lineage_registration": (
        "actp_semantic_lineage_registrations", "registration_id", False,
    ),
    "semantic_content_brief": (
        "actp_semantic_content_briefs", "brief_id", False,
    ),
    "semantic_content_asset": (
        "actp_semantic_content_assets", "asset_id", False,
    ),
    "semantic_content_lineage": (
        "actp_semantic_content_lineage", "lineage_link_id", False,
    ),
    "creator": ("actp_market_creators", "creator_id", True),
    "video": ("actp_market_videos", "video_id", True),
    "discovery_attribution": (
        "actp_market_discovery_attributions", "attribution_key", False,
    ),
    "query_attempt": ("actp_market_query_attempts", "attempt_key", False),
    "observation": ("actp_market_observations", "observation_key", False),
    "observation_quality_flag": (
        "actp_market_observation_quality_flags", "observation_key", False,
    ),
    "genome": ("actp_content_genomes", "video_id", True),
    "trend": ("actp_trends", "trend_id", True),
    "membership": ("actp_trend_memberships", "trend_id,video_id", True),
    "trend_observation": ("actp_trend_observations", "trend_observation_key", False),
    "run": ("actp_market_collection_runs", "run_id", True),
    "receipt": ("actp_market_source_receipts", "receipt_key", False),
    "source_health": ("actp_market_source_health", "source_id", True),
    "prediction": ("actp_market_predictions", "prediction_key", True),
}

# A batch can begin in the middle of a run's outbox records. Always process parent
# tables before dependent tables instead of relying on the first row's entity type.
ENTITY_SYNC_ORDER = (
    "upwork_request_reservation",
    "upwork_scan_run",
    "upwork_job",
    "upwork_job_version",
    "upwork_query_observation",
    "upwork_job_observation",
    "upwork_demand_snapshot",
    "upwork_prediction",
    "upwork_prediction_outcome",
    "semantic_graph_version",
    "semantic_topic_node",
    "semantic_topic_edge",
    "semantic_signal_candidate",
    "semantic_signal_binding",
    "semantic_resolution_run",
    "semantic_topic_observation",
    "semantic_atomic_selection",
    "semantic_atomic_selection_source",
    "semantic_evidence_receipt",
    "semantic_lineage_registration",
    "semantic_content_brief",
    "semantic_content_asset",
    "semantic_content_lineage",
    "upwork_semantic_link",
    "creator",
    "trend",
    "run",
    "video",
    "query_attempt",
    "discovery_attribution",
    "observation",
    "observation_quality_flag",
    "genome",
    "membership",
    "trend_observation",
    "prediction",
    "receipt",
    "source_health",
)

def _required_parent_entities(
    entity_type: str,
    payload: Dict[str, Any],
) -> frozenset[Tuple[str, str]]:
    """Return only FK parents whose exact outbox identities are derivable."""

    references: set[Tuple[str, str]] = set()

    def add(parent_type: str, field: str) -> None:
        value = payload.get(field)
        if value is not None and str(value).strip():
            references.add((parent_type, str(value).strip()))

    def topic(field: str = "topic_id") -> None:
        graph = payload.get("graph_version_id")
        topic_id = payload.get(field)
        if graph and topic_id:
            references.add(("semantic_topic_node", f"{graph}|{topic_id}"))

    rules = {
        "upwork_scan_run": (("upwork_request_reservation", "request_reservation_id"),),
        "upwork_job_version": (("upwork_job", "job_id"),),
        "upwork_query_observation": (("upwork_scan_run", "scan_run_id"),),
        "upwork_job_observation": (
            ("upwork_scan_run", "scan_run_id"),
            ("upwork_query_observation", "query_observation_id"),
            ("upwork_job", "job_id"),
            ("upwork_job_version", "job_version_id"),
        ),
        "upwork_demand_snapshot": (("upwork_scan_run", "scan_run_id"),),
        "upwork_prediction": (("upwork_demand_snapshot", "demand_snapshot_id"),),
        "upwork_prediction_outcome": (
            ("upwork_prediction", "prediction_id"),
            ("upwork_demand_snapshot", "observed_snapshot_id"),
        ),
        "upwork_semantic_link": (
            ("upwork_demand_snapshot", "demand_snapshot_id"),
            ("semantic_graph_version", "graph_version_id"),
            ("semantic_signal_candidate", "signal_id"),
        ),
        "semantic_topic_node": (("semantic_graph_version", "graph_version_id"),),
        "semantic_topic_edge": (("semantic_graph_version", "graph_version_id"),),
        "semantic_signal_candidate": (
            ("semantic_graph_version", "graph_version_id"),
            ("trend", "source_trend_id"),
        ),
        "semantic_signal_binding": (("semantic_signal_candidate", "signal_id"),),
        "semantic_resolution_run": (("semantic_signal_candidate", "signal_id"),),
        "semantic_topic_observation": (
            ("semantic_signal_candidate", "signal_id"),
            ("semantic_signal_binding", "binding_id"),
        ),
        "semantic_atomic_selection": (("semantic_graph_version", "graph_version_id"),),
        "semantic_atomic_selection_source": (
            ("semantic_atomic_selection", "selection_id"),
            ("semantic_signal_binding", "binding_id"),
            ("semantic_topic_observation", "topic_observation_key"),
            ("semantic_signal_candidate", "signal_id"),
        ),
        "semantic_evidence_receipt": (("semantic_atomic_selection", "selection_id"),),
        "semantic_content_brief": (
            ("semantic_lineage_registration", "registration_id"),
            ("semantic_graph_version", "graph_version_id"),
            ("semantic_atomic_selection", "atomic_selection_id"),
        ),
        "semantic_content_asset": (("semantic_content_brief", "brief_id"),),
        "semantic_content_lineage": (
            ("semantic_signal_candidate", "signal_id"),
            ("semantic_signal_binding", "binding_id"),
            ("semantic_topic_observation", "topic_observation_key"),
            ("semantic_content_brief", "brief_id"),
            ("semantic_content_asset", "asset_id"),
        ),
        "video": (("creator", "creator_id"),),
        "query_attempt": (("run", "run_id"),),
        "discovery_attribution": (("video", "video_id"),),
        "observation": (("video", "video_id"), ("creator", "creator_id")),
        "observation_quality_flag": (
            ("observation", "observation_key"),
            ("observation", "prior_observation_key"),
            ("run", "run_id"),
            ("video", "video_id"),
        ),
        "genome": (("video", "video_id"),),
        "membership": (("trend", "trend_id"), ("video", "video_id")),
        "trend_observation": (("trend", "trend_id"),),
        "receipt": (("run", "run_id"),),
    }
    for parent_type, field in rules.get(entity_type, ()):
        add(parent_type, field)
    if entity_type == "semantic_topic_node":
        topic("canonical_parent_id")
    elif entity_type == "semantic_topic_edge":
        topic("source_topic_id")
        topic("target_topic_id")
    elif entity_type in {
        "semantic_signal_binding", "semantic_topic_observation",
        "semantic_atomic_selection", "semantic_content_lineage",
    }:
        topic()
        if entity_type == "semantic_content_lineage":
            topic("atomic_topic_id")
    if entity_type in {
        "semantic_atomic_selection",
        "semantic_content_brief",
        "semantic_content_asset",
    }:
        topic("atomic_topic_id")
    if entity_type == "semantic_resolution_run":
        topic("selected_topic_id")
    if entity_type == "semantic_content_asset":
        add("semantic_content_asset", "parent_asset_id")
    return frozenset(references)

ADAPTIVE_SPLIT_HTTP_STATUSES = {408, 413, 502, 503, 504, 524}


class SupabaseSink:
    sink_id = "supabase"

    def __init__(
        self,
        config: MarketTapeConfig,
        store: MarketTapeStore,
        *,
        client: Optional[httpx.Client] = None,
        rest_base_url: Optional[str] = None,
    ):
        self.config = config
        self.store = store
        self.url = os.getenv("SUPABASE_URL", "").strip().rstrip("/")
        candidates = [
            os.getenv("SUPABASE_SERVICE_KEY", "").strip(),
            os.getenv("SUPABASE_SERVICE_ROLE_KEY", "").strip(),
        ]
        self.key = next((value for value in candidates if _valid_service_key(value)), "")
        self.rest_base_url = (rest_base_url or (f"{self.url}/rest/v1" if self.url else "")).rstrip("/")
        self.client = client or httpx.Client(timeout=config.request_timeout_seconds)
        self._owns_client = client is None

    def close(self) -> None:
        if self._owns_client:
            self.client.close()

    def credentials_available(self) -> bool:
        return self.url.startswith("https://") and _valid_service_key(self.key)

    def flush(self, limit: Optional[int] = None) -> Dict[str, Any]:
        if not self.config.supabase_sync_enabled:
            pending = self.store.outbox_pending_count()
            self.store.save_sink_health("disabled", pending)
            return {"state": "disabled", "synced": 0, "failed": 0, "pending": pending}
        if not self.credentials_available():
            pending = self.store.outbox_pending_count()
            detail = "SUPABASE_URL or SUPABASE_SERVICE_KEY is unavailable"
            self.store.save_sink_health("blocked_credential", pending, detail)
            return {"state": "blocked_credential", "synced": 0, "failed": 0, "pending": pending}

        rows = self.store.pending_outbox(
            limit or self.config.supabase_sync_batch_size,
            entity_order=ENTITY_SYNC_ORDER,
        )
        grouped: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
        for row in rows:
            grouped[row["entity_type"]].append(row)
        synced = 0
        failed = 0
        errors: List[str] = []
        for entity_type in sorted(set(grouped) - set(ENTITY_TABLES)):
            ids = [int(row["outbox_id"]) for row in grouped[entity_type]]
            detail = f"unregistered outbox entity type: {sanitize(entity_type)}"
            self.store.mark_outbox_failed(ids, detail)
            failed += len(ids)
            errors.append(detail)
        dependency_blocked_by = ""
        deferred_entity_types: list[str] = []
        deferred = 0
        for entity_type in ENTITY_SYNC_ORDER:
            group = grouped.get(entity_type, [])
            if not group:
                continue
            eligible_group: List[Dict[str, Any]] = []
            for row in group:
                payload = _normalize_payload(row["payload"])
                dependency_state = self.store.defer_outbox_for_unsynced_dependencies(
                    [int(row["outbox_id"])],
                    _required_parent_entities(entity_type, payload),
                    "sync deferred because an exact required parent is unsynced",
                )
                if dependency_state["deferred"]:
                    deferred += int(dependency_state["deferred"])
                    if entity_type not in deferred_entity_types:
                        deferred_entity_types.append(entity_type)
                    if not dependency_blocked_by:
                        dependency_blocked_by = ",".join(
                            dependency_state["parent_entity_types"]
                        )
                else:
                    eligible_group.append(row)
            if not eligible_group:
                continue
            table, conflict, merge = ENTITY_TABLES[entity_type]
            shaped: Dict[Tuple[str, ...], List[Tuple[int, Dict[str, Any]]]] = (
                defaultdict(list)
            )
            for row in eligible_group:
                payload = _normalize_payload(row["payload"])
                shaped[tuple(sorted(payload))].append(
                    (int(row["outbox_id"]), payload)
                )
            entity_failed = False
            post_batch_size = max(
                1, min(1000, int(self.config.supabase_sync_post_batch_size))
            )
            for signature in sorted(shaped):
                records = shaped[signature]
                for offset in range(0, len(records), post_batch_size):
                    batch = records[offset: offset + post_batch_size]
                    result = self._sync_record_batch(
                        table=table,
                        conflict=conflict,
                        merge=merge,
                        records=batch,
                    )
                    synced += result["synced"]
                    failed += result["failed"]
                    errors.extend(result["errors"])
                    entity_failed = entity_failed or result["failed"] > 0
            if entity_failed:
                if not dependency_blocked_by:
                    dependency_blocked_by = entity_type
        pending = self.store.outbox_pending_count()
        state = "degraded" if failed else "deferred" if deferred else "ready"
        self.store.save_sink_health(state, pending, "; ".join(errors)[:1000])
        return {
            "state": state,
            "synced": synced,
            "failed": failed,
            "deferred": deferred,
            "pending": pending,
            "errors": errors[:5],
            "dependency_blocked_by": dependency_blocked_by,
            "dependency_deferred_entities": deferred_entity_types,
        }

    def _sync_record_batch(
        self,
        *,
        table: str,
        conflict: str,
        merge: bool,
        records: List[Tuple[int, Dict[str, Any]]],
    ) -> Dict[str, Any]:
        """Post one idempotent subset, splitting ambiguous timeouts safely."""

        ids = [outbox_id for outbox_id, _ in records]
        payload = [record for _, record in records]
        try:
            response = self.client.post(
                f"{self.rest_base_url}/{table}",
                params={"on_conflict": conflict},
                headers={
                    "apikey": self.key,
                    "Authorization": f"Bearer {self.key}",
                    "Content-Type": "application/json",
                    "Prefer": (
                        "resolution="
                        f"{'merge-duplicates' if merge else 'ignore-duplicates'},"
                        "return=minimal"
                    ),
                },
                json=payload,
            )
            if response.status_code not in {200, 201, 204}:
                if (
                    response.status_code in ADAPTIVE_SPLIT_HTTP_STATUSES
                    and len(records) > 1
                ):
                    return self._split_record_batch(
                        table=table,
                        conflict=conflict,
                        merge=merge,
                        records=records,
                    )
                raise RuntimeError(
                    f"{table} returned HTTP {response.status_code}: "
                    f"{sanitize(response.text)[:300]}"
                )
            self.store.mark_outbox_synced(ids)
            return {"synced": len(ids), "failed": 0, "errors": []}
        except httpx.ReadTimeout as error:
            if len(records) > 1:
                return self._split_record_batch(
                    table=table,
                    conflict=conflict,
                    merge=merge,
                    records=records,
                )
            return self._fail_record_batch(ids, error)
        except (httpx.HTTPError, RuntimeError) as error:
            return self._fail_record_batch(ids, error)

    def _split_record_batch(
        self,
        *,
        table: str,
        conflict: str,
        merge: bool,
        records: List[Tuple[int, Dict[str, Any]]],
    ) -> Dict[str, Any]:
        midpoint = max(1, len(records) // 2)
        totals: Dict[str, Any] = {"synced": 0, "failed": 0, "errors": []}
        for subset in (records[:midpoint], records[midpoint:]):
            if not subset:
                continue
            result = self._sync_record_batch(
                table=table,
                conflict=conflict,
                merge=merge,
                records=subset,
            )
            totals["synced"] += result["synced"]
            totals["failed"] += result["failed"]
            totals["errors"].extend(result["errors"])
        return totals

    def _fail_record_batch(
        self,
        ids: List[int],
        error: Exception,
    ) -> Dict[str, Any]:
        detail = sanitize(error)
        self.store.mark_outbox_failed(ids, detail)
        return {"synced": 0, "failed": len(ids), "errors": [detail]}

    def drain(self, max_batches: int = 250) -> Dict[str, Any]:
        """Flush bounded batches until empty or the queue stops making progress."""
        batch_limit = max(1, min(1000, int(max_batches)))
        total_synced = 0
        total_failed = 0
        batches = 0
        last: Dict[str, Any] = {
            "state": "ready",
            "synced": 0,
            "failed": 0,
            "pending": self.store.outbox_pending_count(),
        }
        while batches < batch_limit and int(last.get("pending", 0)) > 0:
            last = self.flush()
            batches += 1
            total_synced += int(last.get("synced", 0))
            total_failed += int(last.get("failed", 0))
            if int(last.get("pending", 0)) == 0:
                break
            if int(last.get("synced", 0)) == 0:
                break
        pending = self.store.outbox_pending_count()
        if pending == 0:
            state = "ready"
        elif total_synced == 0:
            state = str(last.get("state") or "blocked_no_progress")
            if state == "ready":
                state = "blocked_no_progress"
        else:
            state = "partial"
        return {
            "state": state,
            "batches": batches,
            "synced": total_synced,
            "failed": total_failed,
            "pending": pending,
            "last_batch": last,
        }


def _normalize_payload(payload: Dict[str, Any]) -> Dict[str, Any]:
    output = dict(payload)
    for key, value in list(output.items()):
        if key.endswith("_json") and isinstance(value, str):
            try:
                output[key] = json.loads(value)
            except ValueError:
                output[key] = value
    return output


def _valid_service_key(value: str) -> bool:
    lowered = value.lower()
    return len(value) > 40 and not lowered.startswith(("your_", "replace_", "<"))
