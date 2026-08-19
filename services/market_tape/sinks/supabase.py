"""Transactional outbox delivery to the shared Supabase control plane."""

from __future__ import annotations

import json
import os
from collections import defaultdict
from typing import Any, Dict, Iterable, List, Optional, Tuple

import httpx

from ..config import MarketTapeConfig
from ..sources.base import sanitize
from ..store import MarketTapeStore


ENTITY_TABLES: Dict[str, Tuple[str, str, bool]] = {
    "creator": ("actp_market_creators", "creator_id", True),
    "video": ("actp_market_videos", "video_id", True),
    "observation": ("actp_market_observations", "observation_key", False),
    "genome": ("actp_content_genomes", "video_id", True),
    "trend": ("actp_trends", "trend_id", True),
    "membership": ("actp_trend_memberships", "trend_id,video_id", True),
    "trend_observation": ("actp_trend_observations", "trend_observation_key", False),
    "run": ("actp_market_collection_runs", "run_id", True),
    "receipt": ("actp_market_source_receipts", "receipt_key", False),
    "source_health": ("actp_market_source_health", "source_id", True),
    "prediction": ("actp_market_predictions", "prediction_key", False),
}

# A batch can begin in the middle of a run's outbox records. Always process parent
# tables before dependent tables instead of relying on the first row's entity type.
ENTITY_SYNC_ORDER = (
    "creator",
    "trend",
    "run",
    "video",
    "observation",
    "genome",
    "membership",
    "trend_observation",
    "prediction",
    "receipt",
    "source_health",
)


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
        for entity_type in ENTITY_SYNC_ORDER:
            group = grouped.get(entity_type, [])
            if not group:
                continue
            table, conflict, merge = ENTITY_TABLES[entity_type]
            ids = [int(row["outbox_id"]) for row in group]
            payload = [_normalize_payload(row["payload"]) for row in group]
            try:
                response = self.client.post(
                    f"{self.rest_base_url}/{table}",
                    params={"on_conflict": conflict},
                    headers={
                        "apikey": self.key,
                        "Authorization": f"Bearer {self.key}",
                        "Content-Type": "application/json",
                        "Prefer": f"resolution={'merge-duplicates' if merge else 'ignore-duplicates'},return=minimal",
                    },
                    json=payload,
                )
                if response.status_code not in {200, 201, 204}:
                    raise RuntimeError(f"{table} returned HTTP {response.status_code}: {sanitize(response.text)[:300]}")
                self.store.mark_outbox_synced(ids)
                synced += len(ids)
            except (httpx.HTTPError, RuntimeError) as error:
                detail = sanitize(error)
                self.store.mark_outbox_failed(ids, detail)
                failed += len(ids)
                errors.append(detail)
        pending = self.store.outbox_pending_count()
        state = "ready" if failed == 0 else "degraded"
        self.store.save_sink_health(state, pending, "; ".join(errors)[:1000])
        return {"state": state, "synced": synced, "failed": failed, "pending": pending, "errors": errors[:5]}

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
