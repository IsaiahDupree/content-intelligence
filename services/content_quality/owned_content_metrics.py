from __future__ import annotations

import hashlib
import json
import math
import re
import sqlite3
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from flask import Flask, request


UTC = timezone.utc
SNAPSHOT_CONTRACT = "owned_content_metric_snapshot_v1"
BATCH_CONTRACT = "owned_content_metric_snapshot_batch_v1"
SUMMARY_CONTRACT = "owned_content_metric_summary_v1"
MEASUREMENT_STATUSES = ("observed", "unavailable", "not_supported", "deleted")
MEASUREMENT_SCOPES = ("post", "redirect", "account")
MAX_BATCH_SIZE = 500
MAX_LIST_SIZE = 2_000

PLATFORM_ALIASES = {
    "facebook_reels": "facebook",
    "fb": "facebook",
    "ig": "instagram",
    "instagram_reels": "instagram",
    "threads": "threads",
    "tiktok": "tiktok",
    "twitter": "x",
    "twitter_x": "x",
    "x": "x",
    "youtube_shorts": "youtube",
    "yt": "youtube",
}

COUNT_METRICS = {
    "views",
    "impressions",
    "reach",
    "likes",
    "comments",
    "shares",
    "saves",
    "link_clicks",
    "profile_visits",
    "follows",
}
RATE_METRICS = {
    "completion_rate",
    "hold_3s_rate",
    "engagement_rate",
    "click_through_rate",
}
DURATION_METRICS = {
    "watch_time_seconds",
    "average_watch_time_seconds",
}
MONEY_METRICS = {"revenue_usd"}
SUPPORTED_METRICS = COUNT_METRICS | RATE_METRICS | DURATION_METRICS | MONEY_METRICS

METRIC_ALIASES = {
    "average_watch_time": "average_watch_time_seconds",
    "average_watch_time_seconds": "average_watch_time_seconds",
    "avg_watch_time": "average_watch_time_seconds",
    "click_count": "link_clicks",
    "click_through_rate": "click_through_rate",
    "clicks": "link_clicks",
    "comment_count": "comments",
    "comments": "comments",
    "completion_rate": "completion_rate",
    "engagement_rate": "engagement_rate",
    "follows": "follows",
    "hold_3s_rate": "hold_3s_rate",
    "impressions": "impressions",
    "like_count": "likes",
    "likes": "likes",
    "link_clicks": "link_clicks",
    "plays": "views",
    "profile_visits": "profile_visits",
    "reach": "reach",
    "redirect_clicks": "link_clicks",
    "revenue_usd": "revenue_usd",
    "save_count": "saves",
    "saved": "saves",
    "saves": "saves",
    "share_count": "shares",
    "shares": "shares",
    "total_views": "views",
    "video_views": "views",
    "view_count": "views",
    "views": "views",
    "watch_time_seconds": "watch_time_seconds",
}

FILTER_FIELDS = {
    "account_id",
    "campaign_id",
    "content_id",
    "experiment_id",
    "measurement_status",
    "offer_id",
    "scope",
    "source_id",
    "source_platform",
    "variant_id",
}


class OwnedContentMetricConflict(RuntimeError):
    pass


def _utc_now() -> str:
    return datetime.now(UTC).isoformat()


def _canonical_json(value: Any) -> str:
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=True
    )


def _sha256(value: Any) -> str:
    return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def _snake_case(value: str) -> str:
    value = re.sub(r"(?<!^)(?=[A-Z])", "_", value)
    return re.sub(r"[^a-z0-9]+", "_", value.lower()).strip("_")


def _text(
    payload: dict[str, Any],
    name: str,
    *,
    required: bool = False,
    maximum: int = 500,
) -> str | None:
    value = payload.get(name)
    if value is None or value == "":
        if required:
            raise ValueError(f"{name} is required")
        return None
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be a non-empty string")
    normalized = value.strip()
    if len(normalized) > maximum:
        raise ValueError(f"{name} must be at most {maximum} characters")
    return normalized


def _timestamp(payload: dict[str, Any], name: str) -> str:
    raw = _text(payload, name, required=True, maximum=80)
    try:
        parsed = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"{name} must be an ISO-8601 timestamp") from exc
    if parsed.tzinfo is None:
        raise ValueError(f"{name} must include a timezone")
    return parsed.astimezone(UTC).isoformat()


def _platform(value: str) -> str:
    normalized = _snake_case(value)
    return PLATFORM_ALIASES.get(normalized, normalized)


def _metadata(payload: dict[str, Any]) -> dict[str, Any]:
    value = payload.get("metadata") or {}
    if not isinstance(value, dict):
        raise ValueError("metadata must be an object")
    try:
        encoded = _canonical_json(value)
    except (TypeError, ValueError) as exc:
        raise ValueError("metadata must be JSON serializable") from exc
    if len(encoded.encode("utf-8")) > 128_000:
        raise ValueError("metadata must be at most 128 KB")
    return value


def _normalize_metrics(
    payload: Any,
) -> tuple[dict[str, int | float], dict[str, list[str]]]:
    if payload is None:
        payload = {}
    if not isinstance(payload, dict):
        raise ValueError("metrics must be an object")
    normalized: dict[str, int | float] = {}
    provider_names: dict[str, list[str]] = {}
    unknown: list[str] = []
    for raw_name, raw_value in payload.items():
        name = METRIC_ALIASES.get(_snake_case(str(raw_name)))
        if name is None:
            unknown.append(str(raw_name))
            continue
        if isinstance(raw_value, bool) or not isinstance(raw_value, (int, float)):
            raise ValueError(f"metric {raw_name} must be numeric")
        number = float(raw_value)
        if not math.isfinite(number) or number < 0:
            raise ValueError(
                f"metric {raw_name} must be finite and non-negative"
            )
        if name in COUNT_METRICS:
            if not number.is_integer():
                raise ValueError(f"count metric {raw_name} must be an integer")
            value: int | float = int(number)
        else:
            value = number
        if name in RATE_METRICS and value > 1:
            raise ValueError(f"rate metric {raw_name} must be between 0 and 1")
        if name in normalized and normalized[name] != value:
            raise ValueError(
                f"conflicting aliases were supplied for metric {name}"
            )
        normalized[name] = value
        provider_names.setdefault(name, []).append(str(raw_name))
    if unknown:
        raise ValueError("unsupported metrics: " + ", ".join(sorted(unknown)))
    return normalized, provider_names


class OwnedContentMetricTelemetry:
    def __init__(self, path: str | Path):
        self.path = Path(path).expanduser()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    def connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=30)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA busy_timeout=30000")
        return connection

    def _initialize(self) -> None:
        with closing(self.connect()) as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS cq_owned_content_metric_snapshots (
                    snapshot_id TEXT PRIMARY KEY,
                    contract_type TEXT NOT NULL,
                    idempotency_key TEXT NOT NULL UNIQUE,
                    payload_sha256 TEXT NOT NULL,
                    measurement_status TEXT NOT NULL,
                    scope TEXT NOT NULL,
                    content_id TEXT NOT NULL,
                    campaign_id TEXT NOT NULL,
                    offer_id TEXT,
                    source_platform TEXT NOT NULL,
                    source_id TEXT NOT NULL,
                    account_id TEXT,
                    iteration_id TEXT,
                    experiment_id TEXT,
                    variant_id TEXT,
                    permalink TEXT,
                    tracked_url TEXT,
                    provider_name TEXT NOT NULL,
                    provider_receipt_id TEXT,
                    observed_at TEXT NOT NULL,
                    metrics_json TEXT NOT NULL,
                    provider_metric_names_json TEXT NOT NULL,
                    unavailable_reason TEXT,
                    metadata_json TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    CHECK (measurement_status IN (
                        'observed', 'unavailable', 'not_supported', 'deleted'
                    )),
                    CHECK (scope IN ('post', 'redirect', 'account'))
                );
                CREATE INDEX IF NOT EXISTS cq_owned_content_metric_scope_idx
                    ON cq_owned_content_metric_snapshots(
                        scope, source_platform, source_id, observed_at
                    );
                CREATE INDEX IF NOT EXISTS cq_owned_content_metric_content_idx
                    ON cq_owned_content_metric_snapshots(
                        content_id, campaign_id, observed_at
                    );
                CREATE INDEX IF NOT EXISTS cq_owned_content_metric_experiment_idx
                    ON cq_owned_content_metric_snapshots(
                        experiment_id, variant_id, observed_at
                    );
                CREATE TRIGGER IF NOT EXISTS cq_owned_content_metric_no_update
                BEFORE UPDATE ON cq_owned_content_metric_snapshots
                BEGIN
                    SELECT RAISE(
                        ABORT,
                        'owned content metric snapshots are append-only'
                    );
                END;
                CREATE TRIGGER IF NOT EXISTS cq_owned_content_metric_no_delete
                BEFORE DELETE ON cq_owned_content_metric_snapshots
                BEGIN
                    SELECT RAISE(
                        ABORT,
                        'owned content metric snapshots are append-only'
                    );
                END;
                """
            )
            connection.commit()

    def normalize(self, payload: dict[str, Any]) -> dict[str, Any]:
        if not isinstance(payload, dict):
            raise ValueError("metric snapshot must be an object")
        allowed = {
            "contract_type",
            "idempotency_key",
            "measurement_status",
            "scope",
            "attribution",
            "permalink",
            "tracked_url",
            "provider_name",
            "provider_receipt_id",
            "observed_at",
            "metrics",
            "unavailable_reason",
            "metadata",
        }
        unknown = sorted(set(payload) - allowed)
        if unknown:
            raise ValueError(
                "unknown metric snapshot fields: " + ", ".join(unknown)
            )
        contract = payload.get("contract_type") or SNAPSHOT_CONTRACT
        if contract != SNAPSHOT_CONTRACT:
            raise ValueError(f"contract_type must be {SNAPSHOT_CONTRACT}")
        status = _text(
            payload, "measurement_status", required=True, maximum=40
        )
        if status not in MEASUREMENT_STATUSES:
            raise ValueError(
                "measurement_status must be one of: "
                + ", ".join(MEASUREMENT_STATUSES)
            )
        scope = _text(payload, "scope", required=True, maximum=40)
        if scope not in MEASUREMENT_SCOPES:
            raise ValueError(
                "scope must be one of: " + ", ".join(MEASUREMENT_SCOPES)
            )
        attribution = payload.get("attribution")
        if not isinstance(attribution, dict):
            raise ValueError("attribution must be an object")
        attribution_fields = {
            "content_id",
            "campaign_id",
            "offer_id",
            "source_platform",
            "source_id",
            "account_id",
            "iteration_id",
            "experiment_id",
            "variant_id",
        }
        unknown_attribution = sorted(set(attribution) - attribution_fields)
        if unknown_attribution:
            raise ValueError(
                "unknown attribution fields: "
                + ", ".join(unknown_attribution)
            )
        metrics, provider_names = _normalize_metrics(payload.get("metrics"))
        reason = _text(payload, "unavailable_reason", maximum=2_000)
        if status == "observed":
            if not metrics:
                raise ValueError(
                    "observed snapshots require at least one metric"
                )
            if reason is not None:
                raise ValueError(
                    "observed snapshots cannot include unavailable_reason"
                )
        else:
            if metrics:
                raise ValueError(f"{status} snapshots cannot include metrics")
            if reason is None:
                raise ValueError(
                    f"{status} snapshots require unavailable_reason"
                )
        source_platform = _text(
            attribution,
            "source_platform",
            required=True,
            maximum=100,
        )
        record = {
            "contract_type": SNAPSHOT_CONTRACT,
            "idempotency_key": _text(
                payload, "idempotency_key", required=True, maximum=500
            ),
            "measurement_status": status,
            "scope": scope,
            "attribution": {
                "content_id": _text(
                    attribution, "content_id", required=True, maximum=500
                ),
                "campaign_id": _text(
                    attribution, "campaign_id", required=True, maximum=500
                ),
                "offer_id": _text(attribution, "offer_id", maximum=500),
                "source_platform": _platform(str(source_platform)),
                "source_id": _text(
                    attribution, "source_id", required=True, maximum=1_000
                ),
                "account_id": _text(
                    attribution, "account_id", maximum=500
                ),
                "iteration_id": _text(
                    attribution, "iteration_id", maximum=500
                ),
                "experiment_id": _text(
                    attribution, "experiment_id", maximum=500
                ),
                "variant_id": _text(
                    attribution, "variant_id", maximum=500
                ),
            },
            "permalink": _text(payload, "permalink", maximum=4_000),
            "tracked_url": _text(payload, "tracked_url", maximum=4_000),
            "provider_name": _text(
                payload, "provider_name", required=True, maximum=200
            ),
            "provider_receipt_id": _text(
                payload, "provider_receipt_id", maximum=1_000
            ),
            "observed_at": _timestamp(payload, "observed_at"),
            "metrics": metrics,
            "provider_metric_names": provider_names,
            "unavailable_reason": reason,
            "metadata": _metadata(payload),
        }
        record["payload_sha256"] = _sha256(record)
        record["snapshot_id"] = "ocm_" + hashlib.sha256(
            str(record["idempotency_key"]).encode("utf-8")
        ).hexdigest()[:32]
        return record

    @staticmethod
    def _decode(row: sqlite3.Row) -> dict[str, Any]:
        return {
            "contract_type": str(row["contract_type"]),
            "snapshot_id": str(row["snapshot_id"]),
            "idempotency_key": str(row["idempotency_key"]),
            "payload_sha256": str(row["payload_sha256"]),
            "measurement_status": str(row["measurement_status"]),
            "scope": str(row["scope"]),
            "attribution": {
                "content_id": str(row["content_id"]),
                "campaign_id": str(row["campaign_id"]),
                "offer_id": row["offer_id"],
                "source_platform": str(row["source_platform"]),
                "source_id": str(row["source_id"]),
                "account_id": row["account_id"],
                "iteration_id": row["iteration_id"],
                "experiment_id": row["experiment_id"],
                "variant_id": row["variant_id"],
            },
            "permalink": row["permalink"],
            "tracked_url": row["tracked_url"],
            "provider_name": str(row["provider_name"]),
            "provider_receipt_id": row["provider_receipt_id"],
            "observed_at": str(row["observed_at"]),
            "metrics": json.loads(str(row["metrics_json"])),
            "provider_metric_names": json.loads(
                str(row["provider_metric_names_json"])
            ),
            "unavailable_reason": row["unavailable_reason"],
            "metadata": json.loads(str(row["metadata_json"])),
            "created_at": str(row["created_at"]),
        }

    def _insert(
        self,
        connection: sqlite3.Connection,
        record: dict[str, Any],
    ) -> tuple[bool, sqlite3.Row]:
        current = connection.execute(
            """SELECT * FROM cq_owned_content_metric_snapshots
               WHERE idempotency_key=?""",
            (record["idempotency_key"],),
        ).fetchone()
        if current is not None:
            if str(current["payload_sha256"]) != record["payload_sha256"]:
                raise OwnedContentMetricConflict(
                    "idempotency key already exists with different metric evidence"
                )
            return False, current
        attribution = record["attribution"]
        connection.execute(
            """INSERT INTO cq_owned_content_metric_snapshots (
                   snapshot_id, contract_type, idempotency_key, payload_sha256,
                   measurement_status, scope, content_id, campaign_id, offer_id,
                   source_platform, source_id, account_id, iteration_id,
                   experiment_id, variant_id, permalink, tracked_url,
                   provider_name, provider_receipt_id, observed_at, metrics_json,
                   provider_metric_names_json, unavailable_reason, metadata_json,
                   created_at
               ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                         ?, ?, ?, ?, ?, ?, ?)""",
            (
                record["snapshot_id"], record["contract_type"],
                record["idempotency_key"], record["payload_sha256"],
                record["measurement_status"], record["scope"],
                attribution["content_id"], attribution["campaign_id"],
                attribution["offer_id"], attribution["source_platform"],
                attribution["source_id"], attribution["account_id"],
                attribution["iteration_id"], attribution["experiment_id"],
                attribution["variant_id"], record["permalink"],
                record["tracked_url"], record["provider_name"],
                record["provider_receipt_id"], record["observed_at"],
                _canonical_json(record["metrics"]),
                _canonical_json(record["provider_metric_names"]),
                record["unavailable_reason"],
                _canonical_json(record["metadata"]), _utc_now(),
            ),
        )
        row = connection.execute(
            """SELECT * FROM cq_owned_content_metric_snapshots
               WHERE snapshot_id=?""",
            (record["snapshot_id"],),
        ).fetchone()
        return True, row

    def ingest(self, payload: dict[str, Any]) -> dict[str, Any]:
        record = self.normalize(payload)
        with closing(self.connect()) as connection:
            created, row = self._insert(connection, record)
            connection.commit()
        return {
            "status": "created" if created else "idempotent_replay",
            "created": created,
            "snapshot": self._decode(row),
        }

    def ingest_batch(
        self, payloads: list[dict[str, Any]]
    ) -> dict[str, Any]:
        if not isinstance(payloads, list) or not payloads:
            raise ValueError("snapshots must be a non-empty array")
        if len(payloads) > MAX_BATCH_SIZE:
            raise ValueError(
                f"snapshots must contain at most {MAX_BATCH_SIZE} rows"
            )
        records = [self.normalize(payload) for payload in payloads]
        keys = [str(record["idempotency_key"]) for record in records]
        if len(keys) != len(set(keys)):
            raise ValueError("batch idempotency keys must be unique")
        results: list[dict[str, Any]] = []
        with closing(self.connect()) as connection:
            try:
                for record in records:
                    created, row = self._insert(connection, record)
                    results.append(
                        {"created": created, "snapshot": self._decode(row)}
                    )
                connection.commit()
            except Exception:
                connection.rollback()
                raise
        created_count = sum(bool(item["created"]) for item in results)
        return {
            "status": "ingested",
            "contract_type": BATCH_CONTRACT,
            "requested": len(records),
            "created": created_count,
            "idempotent_replays": len(records) - created_count,
            "snapshots": [item["snapshot"] for item in results],
        }

    def _filters(self, filters: dict[str, Any]) -> tuple[str, list[str]]:
        unknown = sorted(set(filters) - FILTER_FIELDS - {"limit"})
        if unknown:
            raise ValueError("unsupported filters: " + ", ".join(unknown))
        clauses: list[str] = []
        values: list[str] = []
        for name in sorted(FILTER_FIELDS):
            raw = filters.get(name)
            if raw is None or raw == "":
                continue
            value = str(raw).strip()
            if name == "source_platform":
                value = _platform(value)
            if (
                name == "measurement_status"
                and value not in MEASUREMENT_STATUSES
            ):
                raise ValueError("unsupported measurement_status")
            if name == "scope" and value not in MEASUREMENT_SCOPES:
                raise ValueError("unsupported scope")
            clauses.append(f"{name}=?")
            values.append(value)
        return (" AND ".join(clauses) or "1=1"), values

    def snapshots(
        self,
        filters: dict[str, Any],
        *,
        limit: int = 500,
    ) -> list[dict[str, Any]]:
        if limit < 1 or limit > MAX_LIST_SIZE:
            raise ValueError(f"limit must be between 1 and {MAX_LIST_SIZE}")
        where, values = self._filters(filters)
        with closing(self.connect()) as connection:
            rows = connection.execute(
                f"""SELECT * FROM cq_owned_content_metric_snapshots
                    WHERE {where}
                    ORDER BY observed_at DESC, created_at DESC,
                             snapshot_id DESC
                    LIMIT ?""",
                (*values, limit),
            ).fetchall()
        return [self._decode(row) for row in rows]

    def summary(
        self, filters: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        where, values = self._filters(filters or {})
        with closing(self.connect()) as connection:
            rows = connection.execute(
                f"""SELECT * FROM cq_owned_content_metric_snapshots
                    WHERE {where}
                    ORDER BY observed_at ASC, created_at ASC,
                             snapshot_id ASC""",
                values,
            ).fetchall()
        latest: dict[tuple[str, str, str], sqlite3.Row] = {}
        for row in rows:
            key = (
                str(row["scope"]),
                str(row["source_platform"]),
                str(row["source_id"]),
            )
            latest[key] = row
        decoded = [self._decode(row) for row in latest.values()]
        status_counts = {status: 0 for status in MEASUREMENT_STATUSES}
        totals: dict[str, int | float] = {
            metric: 0 for metric in sorted(SUPPORTED_METRICS)
        }
        observed: list[dict[str, Any]] = []
        for item in decoded:
            status_counts[item["measurement_status"]] += 1
            if item["measurement_status"] != "observed":
                continue
            observed.append(item)
            for metric, value in item["metrics"].items():
                totals[metric] += value
        totals = {
            key: value
            for key, value in totals.items()
            if value != 0
            or any(key in item["metrics"] for item in observed)
        }
        posts = [item for item in decoded if item["scope"] == "post"]
        redirects = [
            item for item in decoded if item["scope"] == "redirect"
        ]

        def ratio(numerator: int, denominator: int) -> float | None:
            return round(numerator / denominator, 6) if denominator else None

        top = sorted(
            observed,
            key=lambda item: (
                float(item["metrics"].get("views", 0)),
                float(item["metrics"].get("link_clicks", 0)),
            ),
            reverse=True,
        )[:20]
        post_permalink_count = sum(bool(item["permalink"]) for item in posts)
        tracked_url_count = sum(
            bool(item["tracked_url"]) for item in redirects
        )
        offer_count = sum(
            bool(item["attribution"]["offer_id"]) for item in decoded
        )
        experiment_count = sum(
            bool(item["attribution"]["experiment_id"]) for item in decoded
        )
        coverage = {
            "post_entity_count": len(posts),
            "post_permalink_count": post_permalink_count,
            "post_permalink_rate": ratio(post_permalink_count, len(posts)),
            "redirect_entity_count": len(redirects),
            "tracked_url_count": tracked_url_count,
            "tracked_url_rate": ratio(tracked_url_count, len(redirects)),
            "offer_lineage_count": offer_count,
            "offer_lineage_rate": ratio(offer_count, len(decoded)),
            "experiment_lineage_count": experiment_count,
            "experiment_lineage_rate": ratio(
                experiment_count, len(decoded)
            ),
        }
        top_content = [
            {
                "attribution": item["attribution"],
                "scope": item["scope"],
                "permalink": item["permalink"],
                "tracked_url": item["tracked_url"],
                "metrics": item["metrics"],
                "observed_at": item["observed_at"],
            }
            for item in top
        ]
        return {
            "status": (
                "ready" if observed else ("partial" if decoded else "no_data")
            ),
            "contract_type": SUMMARY_CONTRACT,
            "score_is_probability": False,
            "snapshot_count": len(rows),
            "entity_count": len(decoded),
            "latest_entity_statuses": status_counts,
            "observed_entity_count": len(observed),
            "latest_observed_at": max(
                (item["observed_at"] for item in observed), default=None
            ),
            "latest_evidence_at": max(
                (item["observed_at"] for item in decoded), default=None
            ),
            "totals": totals,
            "coverage": coverage,
            "top_content": top_content,
        }

    def health(self) -> dict[str, Any]:
        summary = self.summary()
        return {
            "status": "healthy",
            "data_state": summary["status"],
            "service": "owned-content-metrics",
            "snapshot_contract": SNAPSHOT_CONTRACT,
            "snapshot_count": summary["snapshot_count"],
            "entity_count": summary["entity_count"],
            "observed_entity_count": summary["observed_entity_count"],
            "latest_observed_at": summary["latest_observed_at"],
            "latest_evidence_at": summary["latest_evidence_at"],
            "latest_entity_statuses": summary["latest_entity_statuses"],
            "totals": summary["totals"],
            "coverage": summary["coverage"],
            "score_is_probability": False,
        }


def register_owned_content_metric_routes(
    app: Flask,
    service: OwnedContentMetricTelemetry,
    *,
    json_body: Callable[[], dict[str, Any]],
    require_auth: Callable[[], Any],
    audited_response: Callable[..., Any],
    invalid_response: Callable[..., Any],
) -> None:
    import time

    def conflict_result(error: Exception) -> dict[str, Any]:
        return {
            "status": "error",
            "code": "IDEMPOTENCY_KEY_CONFLICT",
            "error": str(error),
        }

    def metric_health():
        denied = require_auth()
        if denied:
            return denied
        started = time.monotonic()
        return audited_response(
            "owned_content_metric_health",
            {},
            service.health(),
            started_at=started,
        )

    def ingest_metric():
        denied = require_auth()
        if denied:
            return denied
        started = time.monotonic()
        payload = json_body()
        try:
            result = service.ingest(payload)
        except OwnedContentMetricConflict as error:
            return audited_response(
                "ingest_owned_content_metric",
                payload,
                conflict_result(error),
                409,
                started,
            )
        except ValueError as error:
            return invalid_response(
                "ingest_owned_content_metric", payload, error, started
            )
        return audited_response(
            "ingest_owned_content_metric",
            payload,
            result,
            201 if result["created"] else 200,
            started,
        )

    def ingest_metric_batch():
        denied = require_auth()
        if denied:
            return denied
        started = time.monotonic()
        payload = json_body()
        snapshots = payload.get("snapshots") if isinstance(payload, dict) else None
        try:
            result = service.ingest_batch(snapshots)
        except OwnedContentMetricConflict as error:
            return audited_response(
                "ingest_owned_content_metric_batch",
                payload,
                conflict_result(error),
                409,
                started,
            )
        except ValueError as error:
            return invalid_response(
                "ingest_owned_content_metric_batch", payload, error, started
            )
        return audited_response(
            "ingest_owned_content_metric_batch",
            payload,
            result,
            201 if result["created"] else 200,
            started,
        )

    def list_metrics():
        denied = require_auth()
        if denied:
            return denied
        started = time.monotonic()
        parameters = request.args.to_dict(flat=True)
        try:
            limit = max(
                1,
                min(MAX_LIST_SIZE, int(parameters.pop("limit", "500"))),
            )
            snapshots = service.snapshots(parameters, limit=limit)
        except ValueError as error:
            return invalid_response(
                "list_owned_content_metrics", parameters, error, started
            )
        result = {
            "status": "ok",
            "contract_type": SNAPSHOT_CONTRACT,
            "count": len(snapshots),
            "limit": limit,
            "snapshots": snapshots,
        }
        return audited_response(
            "list_owned_content_metrics",
            parameters,
            result,
            started_at=started,
        )

    def metric_summary():
        denied = require_auth()
        if denied:
            return denied
        started = time.monotonic()
        parameters = request.args.to_dict(flat=True)
        try:
            result = service.summary(parameters)
        except ValueError as error:
            return invalid_response(
                "summarize_owned_content_metrics", parameters, error, started
            )
        return audited_response(
            "summarize_owned_content_metrics",
            parameters,
            result,
            started_at=started,
        )

    routes = (
        (
            "/api/owned-content-metrics/health",
            "owned_content_metric_health",
            metric_health,
            ["GET"],
        ),
        (
            "/api/owned-content-metrics/snapshots",
            "ingest_owned_content_metric",
            ingest_metric,
            ["POST"],
        ),
        (
            "/api/owned-content-metrics/snapshots",
            "list_owned_content_metrics",
            list_metrics,
            ["GET"],
        ),
        (
            "/api/owned-content-metrics/snapshots/batch",
            "ingest_owned_content_metric_batch",
            ingest_metric_batch,
            ["POST"],
        ),
        (
            "/api/owned-content-metrics/summary",
            "summarize_owned_content_metrics",
            metric_summary,
            ["GET"],
        ),
    )
    for rule, endpoint, view_func, methods in routes:
        app.add_url_rule(
            rule,
            endpoint=endpoint,
            view_func=view_func,
            methods=methods,
        )
