"""Append-only transcript experiment identity and outcome telemetry."""

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
EXPERIMENT_CONTRACT = "script_experiment_v1"
METRIC_SNAPSHOT_CONTRACT = "script_metric_snapshot_v1"
METRIC_ROLLUP_CONTRACT = "script_metric_rollup_v1"
FACTOR_VECTOR_CONTRACT = "content_factor_vector_v2"
FACTOR_ROLLUP_CONTRACT = "script_factor_rollup_v2"
IDENTITY_CONTRACT = "script_experiment_identity_v1"
FACTOR_DIMENSIONS = (
    "topic_id",
    "atomic_subject_id",
    "audience_id",
    "audience_intent_id",
    "funnel_stage_id",
    "angle_id",
    "central_idea_id",
    "evidence_set_id",
    "narrative_structure_id",
    "delivery_format_id",
    "platform_id",
    "offer_id",
    "hook_hypothesis_id",
    "hook_id",
    "script_body_id",
    "cta_id",
    "delivery_plan_id",
    "visual_plan_id",
)
REALIZED_FACTOR_DIMENSIONS = {
    "hook_id",
    "script_body_id",
    "cta_id",
    "delivery_plan_id",
    "visual_plan_id",
}
IMMUTABLE_FACTOR_DIMENSIONS = set(FACTOR_DIMENSIONS).difference(
    REALIZED_FACTOR_DIMENSIONS
)
SUPPORTED_PLATFORMS = (
    "facebook",
    "instagram",
    "linkedin",
    "threads",
    "tiktok",
    "x",
    "youtube",
)
COUNT_METRICS = (
    "views",
    "hold_1s_views",
    "hold_3s_views",
    "completed_views",
    "shares",
    "saves",
    "cta_clicks",
    "cta_leads",
    "cta_signups",
    "cta_trials",
    "cta_purchases",
)
RETENTION_METRICS = (
    "hold_1s_views",
    "hold_3s_views",
    "completed_views",
)
CTA_METRICS = (
    "cta_clicks",
    "cta_leads",
    "cta_signups",
    "cta_trials",
    "cta_purchases",
)
DENOMINATOR_BASES = (
    "impressions",
    "not_available",
    "qualified_video_views",
    "shown_in_feed",
    "video_starts",
)
RATE_ELIGIBLE_DENOMINATOR_BASIS = "video_starts"
MAX_FACTOR_ROLLUP_EXPERIMENTS = 5_000
MAX_FACTOR_ROLLUP_SNAPSHOTS = 100_000
PLATFORM_ALIASES = {
    "facebook_reels": "facebook",
    "instagram_reels": "instagram",
    "linked_in": "linkedin",
    "tik_tok": "tiktok",
    "twitter": "x",
    "twitter_x": "x",
    "you_tube": "youtube",
    "you_tube_shorts": "youtube",
    "youtube_shorts": "youtube",
}
COMMON_METRIC_ALIASES = {
    "views": "views",
    "view_count": "views",
    "video_views": "views",
    "play_count": "views",
    "hold_1s": "hold_1s_views",
    "hold_1s_views": "hold_1s_views",
    "one_second_views": "hold_1s_views",
    "one_second_video_views": "hold_1s_views",
    "views_1s": "hold_1s_views",
    "hold_3s": "hold_3s_views",
    "hold_3s_views": "hold_3s_views",
    "three_second_views": "hold_3s_views",
    "three_second_video_views": "hold_3s_views",
    "views_3s": "hold_3s_views",
    "completed_views": "completed_views",
    "complete_views": "completed_views",
    "full_video_views": "completed_views",
    "video_completions": "completed_views",
    "shares": "shares",
    "share_count": "shares",
    "saves": "saves",
    "save_count": "saves",
    "cta_clicks": "cta_clicks",
    "link_clicks": "cta_clicks",
    "outbound_clicks": "cta_clicks",
    "website_clicks": "cta_clicks",
    "cta_leads": "cta_leads",
    "leads": "cta_leads",
    "cta_signups": "cta_signups",
    "signups": "cta_signups",
    "cta_trials": "cta_trials",
    "trials": "cta_trials",
    "cta_purchases": "cta_purchases",
    "purchases": "cta_purchases",
}
PLATFORM_METRIC_ALIASES = {
    "instagram": {"plays": "views", "saved": "saves"},
    "tiktok": {
        "video_view_count": "views",
        "favorites": "saves",
        "favorite_count": "saves",
    },
    "youtube": {"video_view_count": "views"},
    "x": {
        "bookmark_count": "saves",
        "bookmarks": "saves",
    },
    "threads": {"view_count": "views"},
    "facebook": {"plays": "views", "thruplays": "completed_views"},
    "linkedin": {"video_views": "views"},
}
CTA_OUTCOME_ALIASES = {
    "clicks": "cta_clicks",
    "leads": "cta_leads",
    "signups": "cta_signups",
    "trials": "cta_trials",
    "purchases": "cta_purchases",
}


class ExperimentConflict(RuntimeError):
    """Raised when immutable experiment attribution would be changed."""


def _canonical_json(value: Any) -> str:
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    )


def _canonical_sha256(value: Any) -> str:
    return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def _required_text(
    payload: dict[str, Any], field: str, *, maximum: int = 300
) -> str:
    raw = payload.get(field)
    if not isinstance(raw, str) or not raw.strip():
        raise ValueError(f"{field} is required")
    value = raw.strip()
    if len(value) > maximum:
        raise ValueError(f"{field} must be at most {maximum} characters")
    return value


def _optional_text(
    payload: dict[str, Any], field: str, *, maximum: int = 300
) -> str | None:
    raw = payload.get(field)
    if raw is None:
        return None
    if not isinstance(raw, str):
        raise ValueError(f"{field} must be a string")
    value = raw.strip()
    if not value:
        return None
    if len(value) > maximum:
        raise ValueError(f"{field} must be at most {maximum} characters")
    return value


def _utc_timestamp(payload: dict[str, Any], field: str) -> str:
    value = _required_text(payload, field, maximum=80)
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"{field} must be an ISO-8601 timestamp") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError(f"{field} must include a timezone")
    return parsed.astimezone(UTC).isoformat()


def _metadata(payload: dict[str, Any]) -> dict[str, Any]:
    value = payload.get("metadata") or {}
    if not isinstance(value, dict):
        raise ValueError("metadata must be an object")
    if len(_canonical_json(value).encode("utf-8")) > 32_768:
        raise ValueError("metadata must be at most 32768 encoded bytes")
    return value


def _content_factor_vector(metadata: dict[str, Any]) -> dict[str, Any] | None:
    raw = metadata.get("content_factor_vector")
    if raw is None:
        return None
    if not isinstance(raw, dict):
        raise ValueError("metadata.content_factor_vector must be an object")
    allowed = {
        "contract_type",
        "factors",
        "factor_vector_sha256",
        "planned_factors",
        "planned_factor_vector_sha256",
    }
    unknown_fields = sorted(set(raw) - allowed)
    if unknown_fields:
        raise ValueError(
            "unknown content factor vector fields: "
            + ", ".join(unknown_fields)
        )
    if raw.get("contract_type") != FACTOR_VECTOR_CONTRACT:
        raise ValueError(
            f"content factor contract must be {FACTOR_VECTOR_CONTRACT}"
        )
    factors = raw.get("factors")
    if not isinstance(factors, dict):
        raise ValueError("content factor vector factors must be an object")
    unknown = sorted(set(factors) - set(FACTOR_DIMENSIONS))
    missing = sorted(set(FACTOR_DIMENSIONS) - set(factors))
    if unknown:
        raise ValueError("unsupported factor dimensions: " + ", ".join(unknown))
    if missing:
        raise ValueError("missing factor dimensions: " + ", ".join(missing))
    normalized: dict[str, str] = {}
    for name in FACTOR_DIMENSIONS:
        value = factors[name]
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"factor {name} must be a non-empty string")
        if len(value.strip()) > 240:
            raise ValueError(f"factor {name} must be at most 240 characters")
        normalized[name] = value.strip()
    core = {
        "contract_type": FACTOR_VECTOR_CONTRACT,
        "factors": normalized,
    }
    expected = _canonical_sha256(core)
    supplied = _validated_sha256(
        raw.get("factor_vector_sha256"), "factor_vector_sha256"
    )
    if supplied != expected:
        raise ValueError("factor_vector_sha256 does not match factors")
    result = {**core, "factor_vector_sha256": expected}
    planned = raw.get("planned_factor_vector_sha256")
    planned_factors = raw.get("planned_factors")
    if (planned in (None, "")) != (planned_factors is None):
        raise ValueError(
            "planned factors and their SHA-256 must be supplied together"
        )
    if planned_factors is not None:
        if not isinstance(planned_factors, dict):
            raise ValueError("planned_factors must be an object")
        if set(planned_factors) != set(FACTOR_DIMENSIONS):
            raise ValueError(
                "planned_factors must contain the exact factor dimensions"
            )
        normalized_planned: dict[str, str] = {}
        for name in FACTOR_DIMENSIONS:
            value = planned_factors[name]
            if not isinstance(value, str) or not value.strip():
                raise ValueError(
                    f"planned factor {name} must be a non-empty string"
                )
            if len(value.strip()) > 240:
                raise ValueError(
                    f"planned factor {name} must be at most 240 characters"
                )
            normalized_planned[name] = value.strip()
        planned_sha256 = _validated_sha256(
            planned, "planned_factor_vector_sha256"
        )
        planned_core = {
            "contract_type": FACTOR_VECTOR_CONTRACT,
            "factors": normalized_planned,
        }
        if _canonical_sha256(planned_core) != planned_sha256:
            raise ValueError(
                "planned factor SHA-256 does not match planned_factors"
            )
        changed_immutable = sorted(
            dimension
            for dimension in IMMUTABLE_FACTOR_DIMENSIONS
            if normalized[dimension] != normalized_planned[dimension]
        )
        if changed_immutable:
            raise ValueError(
                "realized factors changed immutable dimensions: "
                + ", ".join(changed_immutable)
            )
        result["planned_factors"] = normalized_planned
        result["planned_factor_vector_sha256"] = planned_sha256
    return result


def _snake_case(value: str) -> str:
    with_boundaries = re.sub(r"(?<!^)(?=[A-Z])", "_", value)
    return re.sub(r"[^a-z0-9]+", "_", with_boundaries.lower()).strip("_")


def normalize_platform(value: Any) -> str:
    platform = _snake_case(str(value or "").strip())
    platform = PLATFORM_ALIASES.get(platform, platform)
    if platform not in SUPPORTED_PLATFORMS:
        raise ValueError(
            "source_platform must be one of: " + ", ".join(SUPPORTED_PLATFORMS)
        )
    return platform


def normalize_denominator_basis(value: Any) -> str:
    basis = _snake_case(str(value or "").strip())
    if basis not in DENOMINATOR_BASES:
        raise ValueError(
            "view_denominator_basis must be one of: "
            + ", ".join(DENOMINATOR_BASES)
        )
    return basis


def _sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _validated_sha256(value: Any, field: str) -> str:
    digest = str(value or "").strip().lower()
    if not re.fullmatch(r"[0-9a-f]{64}", digest):
        raise ValueError(f"{field} must be a lowercase SHA-256 hex digest")
    return digest


def stable_experiment_id(
    *,
    brief_id: str,
    script_id: str,
    script_sha256: str,
    workflow_seed: str,
) -> str:
    """Derive one stable ID from immutable script/workflow lineage."""

    identity = {
        "contract": IDENTITY_CONTRACT,
        "brief_id": str(brief_id).strip(),
        "script_id": str(script_id).strip(),
        "script_sha256": _validated_sha256(script_sha256, "script_sha256"),
        "workflow_seed": str(workflow_seed).strip(),
    }
    for field in ("brief_id", "script_id", "workflow_seed"):
        if not identity[field]:
            raise ValueError(f"{field} is required")
    return "sxp_" + _canonical_sha256(identity)[:24]


def experiment_identity(payload: dict[str, Any]) -> dict[str, str]:
    brief_id = _required_text(payload, "brief_id")
    script_id = _required_text(payload, "script_id")
    workflow_id = _optional_text(payload, "workflow_id")
    workflow_seed = _optional_text(payload, "workflow_seed") or workflow_id
    if not workflow_seed:
        raise ValueError("workflow_seed or workflow_id is required")
    script_text = payload.get("script_text")
    supplied_digest = payload.get("script_sha256")
    if script_text is not None:
        if not isinstance(script_text, str) or not script_text.strip():
            raise ValueError("script_text must be a non-empty string")
        calculated_digest = _sha256_text(script_text)
        if supplied_digest is not None and _validated_sha256(
            supplied_digest, "script_sha256"
        ) != calculated_digest:
            raise ValueError("script_sha256 does not match script_text")
        script_sha256 = calculated_digest
    else:
        script_sha256 = _validated_sha256(supplied_digest, "script_sha256")
    return {
        "brief_id": brief_id,
        "script_id": script_id,
        "script_sha256": script_sha256,
        "workflow_seed": workflow_seed,
        "workflow_id": workflow_id or "",
    }


def _whole_count(value: Any, field: str) -> int:
    if isinstance(value, bool):
        raise ValueError(f"{field} must be a non-negative whole number")
    try:
        numeric = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{field} must be a non-negative whole number") from exc
    if not math.isfinite(numeric) or not numeric.is_integer() or numeric < 0:
        raise ValueError(f"{field} must be a non-negative whole number")
    return int(numeric)


def normalize_metric_counts(
    source_platform: str,
    metrics: Any,
    cta_outcomes: Any = None,
) -> dict[str, int]:
    """Normalize a bounded provider metric projection into explicit counts."""

    normalized, _lineage = normalize_metric_projection(
        source_platform, metrics, cta_outcomes
    )
    return normalized


def normalize_metric_projection(
    source_platform: str,
    metrics: Any,
    cta_outcomes: Any = None,
) -> tuple[dict[str, int], dict[str, list[str]]]:
    """Return canonical counts plus the exact provider field-name lineage."""

    if not isinstance(metrics, dict):
        raise ValueError("metrics must be an object")
    aliases = {
        **COMMON_METRIC_ALIASES,
        **PLATFORM_METRIC_ALIASES.get(source_platform, {}),
    }
    normalized: dict[str, int] = {}
    provider_metric_names: dict[str, list[str]] = {}

    def add(raw_key: str, raw_value: Any, canonical_key: str | None) -> None:
        if canonical_key is None:
            raise ValueError(f"unsupported metric field: {raw_key}")
        value = _whole_count(raw_value, raw_key)
        if canonical_key in normalized and normalized[canonical_key] != value:
            raise ValueError(
                f"conflicting values supplied for metric {canonical_key}"
            )
        normalized[canonical_key] = value
        names = provider_metric_names.setdefault(canonical_key, [])
        if raw_key not in names:
            names.append(raw_key)

    for raw_key, raw_value in metrics.items():
        if not isinstance(raw_key, str):
            raise ValueError("metric field names must be strings")
        add(raw_key, raw_value, aliases.get(_snake_case(raw_key)))

    if cta_outcomes is not None:
        if not isinstance(cta_outcomes, dict):
            raise ValueError("cta_outcomes must be an object")
        for raw_key, raw_value in cta_outcomes.items():
            if not isinstance(raw_key, str):
                raise ValueError("cta_outcome field names must be strings")
            add(
                f"cta_outcomes.{raw_key}",
                raw_value,
                CTA_OUTCOME_ALIASES.get(_snake_case(raw_key)),
            )

    if not normalized:
        raise ValueError(
            "at least one supported metric count is required: "
            + ", ".join(COUNT_METRICS)
        )
    views = normalized.get("views")
    if views is not None:
        for field in RETENTION_METRICS:
            if normalized.get(field, 0) > views:
                raise ValueError(f"{field} cannot exceed views in the same snapshot")
    first = normalized.get("hold_1s_views")
    third = normalized.get("hold_3s_views")
    if first is not None and third is not None and third > first:
        raise ValueError("hold_3s_views cannot exceed hold_1s_views")
    ordered = {
        field: normalized[field] for field in COUNT_METRICS if field in normalized
    }
    return ordered, {
        field: provider_metric_names[field]
        for field in COUNT_METRICS
        if field in provider_metric_names
    }


SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS cq_script_experiments (
    experiment_id TEXT PRIMARY KEY,
    contract TEXT NOT NULL,
    brief_id TEXT NOT NULL,
    script_id TEXT NOT NULL,
    script_sha256 TEXT NOT NULL,
    workflow_seed TEXT NOT NULL,
    workflow_id TEXT,
    generation_contract TEXT,
    identity_sha256 TEXT NOT NULL,
    payload_sha256 TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS cq_script_experiment_posts (
    source_platform TEXT NOT NULL,
    provider_post_id TEXT NOT NULL,
    experiment_id TEXT NOT NULL,
    script_id TEXT NOT NULL,
    assigned_at TEXT NOT NULL,
    PRIMARY KEY(source_platform, provider_post_id),
    FOREIGN KEY(experiment_id) REFERENCES cq_script_experiments(experiment_id)
);
CREATE TABLE IF NOT EXISTS cq_script_metric_snapshots (
    snapshot_id TEXT PRIMARY KEY,
    contract TEXT NOT NULL,
    idempotency_key TEXT NOT NULL UNIQUE,
    experiment_id TEXT NOT NULL,
    script_id TEXT NOT NULL,
    source_platform TEXT NOT NULL,
    provider_post_id TEXT NOT NULL,
    provider_account_id TEXT,
    provider_event_id TEXT,
    provider_receipt_id TEXT NOT NULL,
    view_denominator_basis TEXT NOT NULL CHECK(
        view_denominator_basis IN (
            'impressions', 'not_available', 'qualified_video_views',
            'shown_in_feed', 'video_starts'
        )
    ),
    measurement_scope TEXT NOT NULL CHECK(measurement_scope = 'lifetime_cumulative'),
    observed_at TEXT NOT NULL,
    metrics_json TEXT NOT NULL,
    provider_metric_names_json TEXT NOT NULL,
    metadata_json TEXT NOT NULL,
    payload_sha256 TEXT NOT NULL,
    created_at TEXT NOT NULL,
    FOREIGN KEY(experiment_id) REFERENCES cq_script_experiments(experiment_id)
);
CREATE INDEX IF NOT EXISTS idx_cq_script_experiments_script
    ON cq_script_experiments(script_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_cq_script_experiments_workflow
    ON cq_script_experiments(workflow_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_cq_script_metric_experiment
    ON cq_script_metric_snapshots(
        experiment_id, source_platform, provider_post_id, observed_at DESC
    );
CREATE INDEX IF NOT EXISTS idx_cq_script_metric_script
    ON cq_script_metric_snapshots(script_id, observed_at DESC);
CREATE TRIGGER IF NOT EXISTS cq_script_experiments_no_update
BEFORE UPDATE ON cq_script_experiments
BEGIN
    SELECT RAISE(ABORT, 'script experiments are append-only');
END;
CREATE TRIGGER IF NOT EXISTS cq_script_experiments_no_delete
BEFORE DELETE ON cq_script_experiments
BEGIN
    SELECT RAISE(ABORT, 'script experiments are append-only');
END;
CREATE TRIGGER IF NOT EXISTS cq_script_experiment_posts_no_update
BEFORE UPDATE ON cq_script_experiment_posts
BEGIN
    SELECT RAISE(ABORT, 'script experiment post attributions are append-only');
END;
CREATE TRIGGER IF NOT EXISTS cq_script_experiment_posts_no_delete
BEFORE DELETE ON cq_script_experiment_posts
BEGIN
    SELECT RAISE(ABORT, 'script experiment post attributions are append-only');
END;
CREATE TRIGGER IF NOT EXISTS cq_script_metric_snapshots_no_update
BEFORE UPDATE ON cq_script_metric_snapshots
BEGIN
    SELECT RAISE(ABORT, 'script metric snapshots are append-only');
END;
CREATE TRIGGER IF NOT EXISTS cq_script_metric_snapshots_no_delete
BEFORE DELETE ON cq_script_metric_snapshots
BEGIN
    SELECT RAISE(ABORT, 'script metric snapshots are append-only');
END;
"""


class ScriptExperimentTelemetry:
    def __init__(self, database_path: str | Path):
        self.path = Path(database_path).expanduser().resolve()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.apply_schema()

    def connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=30.0)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA busy_timeout=30000")
        return connection

    def apply_schema(self) -> None:
        with closing(self.connect()) as connection:
            connection.executescript(SCHEMA_SQL)
            connection.commit()

    @staticmethod
    def _decode_experiment(row: sqlite3.Row) -> dict[str, Any]:
        stored = json.loads(str(row["payload_json"]))
        stored["created_at"] = str(row["created_at"])
        return stored

    @staticmethod
    def _decode_snapshot(row: sqlite3.Row) -> dict[str, Any]:
        return {
            "snapshot_id": str(row["snapshot_id"]),
            "contract": str(row["contract"]),
            "idempotency_key": str(row["idempotency_key"]),
            "experiment_id": str(row["experiment_id"]),
            "script_id": str(row["script_id"]),
            "source_platform": str(row["source_platform"]),
            "provider_post_id": str(row["provider_post_id"]),
            "provider_account_id": row["provider_account_id"],
            "provider_event_id": row["provider_event_id"],
            "provider_receipt_id": str(row["provider_receipt_id"]),
            "view_denominator_basis": str(row["view_denominator_basis"]),
            "measurement_scope": str(row["measurement_scope"]),
            "observed_at": str(row["observed_at"]),
            "metrics": json.loads(str(row["metrics_json"])),
            "provider_metric_names": json.loads(
                str(row["provider_metric_names_json"])
            ),
            "metadata": json.loads(str(row["metadata_json"])),
            "payload_sha256": str(row["payload_sha256"]),
            "created_at": str(row["created_at"]),
        }

    def health(self) -> dict[str, Any]:
        with closing(self.connect()) as connection:
            experiment_count = int(
                connection.execute(
                    "SELECT COUNT(*) FROM cq_script_experiments"
                ).fetchone()[0]
            )
            snapshot_count = int(
                connection.execute(
                    "SELECT COUNT(*) FROM cq_script_metric_snapshots"
                ).fetchone()[0]
            )
        return {
            "status": "healthy",
            "service": "script-experiment-telemetry",
            "schema_contract": EXPERIMENT_CONTRACT,
            "experiment_count": experiment_count,
            "metric_snapshot_count": snapshot_count,
            "supported_metrics": list(COUNT_METRICS),
        }

    def register_experiment(self, payload: dict[str, Any]) -> dict[str, Any]:
        if not isinstance(payload, dict):
            raise ValueError("JSON body must be an object")
        identity = experiment_identity(payload)
        experiment_id = stable_experiment_id(
            brief_id=identity["brief_id"],
            script_id=identity["script_id"],
            script_sha256=identity["script_sha256"],
            workflow_seed=identity["workflow_seed"],
        )
        supplied_id = _optional_text(payload, "experiment_id")
        if supplied_id and supplied_id != experiment_id:
            raise ValueError(
                "experiment_id does not match immutable script/workflow lineage"
            )
        metadata = _metadata(payload)
        generation_contract = _optional_text(
            payload, "generation_contract", maximum=160
        )
        raw_record = {
            "contract": EXPERIMENT_CONTRACT,
            "experiment_id": experiment_id,
            "brief_id": identity["brief_id"],
            "script_id": identity["script_id"],
            "script_sha256": identity["script_sha256"],
            "workflow_seed": identity["workflow_seed"],
            "workflow_id": identity["workflow_id"] or None,
            "generation_contract": generation_contract,
            "metadata": metadata,
        }
        raw_payload_sha256 = _sha256_text(_canonical_json(raw_record))
        with closing(self.connect()) as connection:
            current = connection.execute(
                "SELECT * FROM cq_script_experiments WHERE experiment_id=?",
                (experiment_id,),
            ).fetchone()
        if (
            current is not None
            and str(current["payload_sha256"]) == raw_payload_sha256
        ):
            return {
                "status": "idempotent_replay",
                "created": False,
                "experiment": self._decode_experiment(current),
            }
        factor_vector = _content_factor_vector(metadata)
        if factor_vector is not None:
            metadata = {**metadata, "content_factor_vector": factor_vector}
        record = {
            "contract": EXPERIMENT_CONTRACT,
            "experiment_id": experiment_id,
            "brief_id": identity["brief_id"],
            "script_id": identity["script_id"],
            "script_sha256": identity["script_sha256"],
            "workflow_seed": identity["workflow_seed"],
            "workflow_id": identity["workflow_id"] or None,
            "generation_contract": generation_contract,
            "metadata": metadata,
        }
        identity_sha256 = _canonical_sha256(
            {
                "contract": IDENTITY_CONTRACT,
                **{
                    key: record[key]
                    for key in (
                        "brief_id",
                        "script_id",
                        "script_sha256",
                        "workflow_seed",
                    )
                },
            }
        )
        payload_json = _canonical_json(record)
        payload_sha256 = _sha256_text(payload_json)
        created_at = datetime.now(UTC).isoformat()
        with closing(self.connect()) as connection:
            cursor = connection.execute(
                """
                INSERT OR IGNORE INTO cq_script_experiments(
                    experiment_id, contract, brief_id, script_id,
                    script_sha256, workflow_seed, workflow_id,
                    generation_contract, identity_sha256, payload_sha256,
                    payload_json, created_at
                ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    experiment_id,
                    EXPERIMENT_CONTRACT,
                    record["brief_id"],
                    record["script_id"],
                    record["script_sha256"],
                    record["workflow_seed"],
                    record["workflow_id"],
                    record["generation_contract"],
                    identity_sha256,
                    payload_sha256,
                    payload_json,
                    created_at,
                ),
            )
            connection.commit()
            stored = connection.execute(
                "SELECT * FROM cq_script_experiments WHERE experiment_id=?",
                (experiment_id,),
            ).fetchone()
            created = cursor.rowcount == 1
        assert stored is not None
        if str(stored["payload_sha256"]) != payload_sha256:
            raise ExperimentConflict(
                "experiment lineage already exists with different immutable payload"
            )
        return {
            "status": "created" if created else "idempotent_replay",
            "created": created,
            "experiment": self._decode_experiment(stored),
        }

    def experiment(self, experiment_id: str) -> dict[str, Any] | None:
        with closing(self.connect()) as connection:
            row = connection.execute(
                "SELECT * FROM cq_script_experiments WHERE experiment_id=?",
                (experiment_id,),
            ).fetchone()
        return self._decode_experiment(row) if row is not None else None

    @staticmethod
    def _filters(
        payload: dict[str, Any], *, require_scope: bool = True
    ) -> dict[str, str]:
        filters: dict[str, str] = {}
        for field in ("experiment_id", "script_id", "brief_id", "workflow_id"):
            value = _optional_text(payload, field)
            if value:
                filters[field] = value
        platform = _optional_text(payload, "source_platform", maximum=80)
        if platform:
            filters["source_platform"] = normalize_platform(platform)
        post_id = _optional_text(payload, "provider_post_id")
        if post_id:
            filters["provider_post_id"] = post_id
        if require_scope and not any(
            filters.get(field)
            for field in ("experiment_id", "script_id", "workflow_id")
        ):
            raise ValueError("experiment_id, script_id, or workflow_id is required")
        return filters

    def experiments(
        self, filters: dict[str, Any], *, limit: int = 100
    ) -> list[dict[str, Any]]:
        scope = self._filters(filters)
        clauses: list[str] = []
        values: list[Any] = []
        for field in ("experiment_id", "script_id", "brief_id", "workflow_id"):
            if field in scope:
                clauses.append(f"{field}=?")
                values.append(scope[field])
        bounded_limit = max(1, min(500, int(limit)))
        with closing(self.connect()) as connection:
            rows = connection.execute(
                f"""SELECT * FROM cq_script_experiments
                    WHERE {' AND '.join(clauses)}
                    ORDER BY created_at DESC LIMIT ?""",
                (*values, bounded_limit),
            ).fetchall()
        return [self._decode_experiment(row) for row in rows]

    def ingest_metric_snapshot(self, payload: dict[str, Any]) -> dict[str, Any]:
        if not isinstance(payload, dict):
            raise ValueError("JSON body must be an object")
        idempotency_key = _required_text(payload, "idempotency_key")
        experiment_id = _required_text(payload, "experiment_id")
        source_platform = normalize_platform(payload.get("source_platform"))
        provider_post_id = _required_text(payload, "provider_post_id")
        provider_receipt_id = _required_text(payload, "provider_receipt_id")
        view_denominator_basis = normalize_denominator_basis(
            payload.get("view_denominator_basis")
        )
        observed_at = _utc_timestamp(payload, "observed_at")
        measurement_scope = str(
            payload.get("measurement_scope") or "lifetime_cumulative"
        ).strip().lower()
        if measurement_scope != "lifetime_cumulative":
            raise ValueError("measurement_scope must be lifetime_cumulative")
        metrics, provider_metric_names = normalize_metric_projection(
            source_platform,
            payload.get("metrics"),
            payload.get("cta_outcomes"),
        )
        provider_account_id = _optional_text(payload, "provider_account_id")
        provider_event_id = _optional_text(payload, "provider_event_id")
        metadata = _metadata(payload)
        created_at = datetime.now(UTC).isoformat()

        with closing(self.connect()) as connection:
            experiment = connection.execute(
                "SELECT * FROM cq_script_experiments WHERE experiment_id=?",
                (experiment_id,),
            ).fetchone()
            if experiment is None:
                raise ValueError("experiment_id is not registered")
            script_id = str(experiment["script_id"])
            record = {
                "contract": METRIC_SNAPSHOT_CONTRACT,
                "idempotency_key": idempotency_key,
                "experiment_id": experiment_id,
                "script_id": script_id,
                "source_platform": source_platform,
                "provider_post_id": provider_post_id,
                "provider_account_id": provider_account_id,
                "provider_event_id": provider_event_id,
                "provider_receipt_id": provider_receipt_id,
                "view_denominator_basis": view_denominator_basis,
                "measurement_scope": measurement_scope,
                "observed_at": observed_at,
                "metrics": metrics,
                "provider_metric_names": provider_metric_names,
                "metadata": metadata,
            }
            payload_sha256 = _canonical_sha256(record)
            snapshot_id = "sms_" + hashlib.sha256(
                idempotency_key.encode("utf-8")
            ).hexdigest()[:24]
            existing = connection.execute(
                "SELECT * FROM cq_script_metric_snapshots WHERE idempotency_key=?",
                (idempotency_key,),
            ).fetchone()
            if existing is not None:
                if str(existing["payload_sha256"]) != payload_sha256:
                    raise ExperimentConflict(
                        "idempotency_key already exists with a different metric snapshot"
                    )
                connection.rollback()
                return {
                    "status": "idempotent_replay",
                    "created": False,
                    "snapshot": self._decode_snapshot(existing),
                }
            connection.execute(
                """INSERT OR IGNORE INTO cq_script_experiment_posts(
                       source_platform, provider_post_id, experiment_id,
                       script_id, assigned_at
                   ) VALUES(?, ?, ?, ?, ?)""",
                (
                    source_platform,
                    provider_post_id,
                    experiment_id,
                    script_id,
                    created_at,
                ),
            )
            assigned = connection.execute(
                """SELECT experiment_id FROM cq_script_experiment_posts
                   WHERE source_platform=? AND provider_post_id=?""",
                (source_platform, provider_post_id),
            ).fetchone()
            if (
                assigned is None
                or str(assigned["experiment_id"]) != experiment_id
            ):
                raise ExperimentConflict(
                    "provider post is already attributed to a different experiment"
                )
            snapshot_cursor = connection.execute(
                """
                INSERT OR IGNORE INTO cq_script_metric_snapshots(
                    snapshot_id, contract, idempotency_key, experiment_id,
                    script_id, source_platform, provider_post_id,
                    provider_account_id, provider_event_id,
                    provider_receipt_id, view_denominator_basis,
                    measurement_scope, observed_at,
                    metrics_json, provider_metric_names_json, metadata_json,
                    payload_sha256, created_at
                ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    snapshot_id,
                    METRIC_SNAPSHOT_CONTRACT,
                    idempotency_key,
                    experiment_id,
                    script_id,
                    source_platform,
                    provider_post_id,
                    provider_account_id,
                    provider_event_id,
                    provider_receipt_id,
                    view_denominator_basis,
                    measurement_scope,
                    observed_at,
                    _canonical_json(metrics),
                    _canonical_json(provider_metric_names),
                    _canonical_json(metadata),
                    payload_sha256,
                    created_at,
                ),
            )
            stored = connection.execute(
                "SELECT * FROM cq_script_metric_snapshots WHERE idempotency_key=?",
                (idempotency_key,),
            ).fetchone()
            if stored is None:
                connection.rollback()
                raise ExperimentConflict("metric snapshot could not be persisted")
            if str(stored["payload_sha256"]) != payload_sha256:
                connection.rollback()
                raise ExperimentConflict(
                    "idempotency_key already exists with a different metric snapshot"
                )
            created = snapshot_cursor.rowcount == 1
            connection.commit()
        assert stored is not None
        return {
            "status": "created" if created else "idempotent_replay",
            "created": created,
            "snapshot": self._decode_snapshot(stored),
        }

    @staticmethod
    def _snapshot_query(scope: dict[str, str]) -> tuple[str, list[str]]:
        clauses: list[str] = []
        values: list[str] = []
        for field in (
            "experiment_id",
            "script_id",
            "source_platform",
            "provider_post_id",
        ):
            if field in scope:
                clauses.append(f"snapshots.{field}=?")
                values.append(scope[field])
        for field in ("brief_id", "workflow_id"):
            if field in scope:
                clauses.append(f"experiments.{field}=?")
                values.append(scope[field])
        return " AND ".join(clauses), values

    def metric_snapshots(
        self, filters: dict[str, Any], *, limit: int = 500
    ) -> list[dict[str, Any]]:
        scope = self._filters(filters)
        where, values = self._snapshot_query(scope)
        bounded_limit = max(1, min(2000, int(limit)))
        with closing(self.connect()) as connection:
            rows = connection.execute(
                f"""SELECT snapshots.*
                    FROM cq_script_metric_snapshots snapshots
                    JOIN cq_script_experiments experiments
                      ON experiments.experiment_id=snapshots.experiment_id
                    WHERE {where}
                    ORDER BY snapshots.observed_at DESC,
                             snapshots.created_at DESC
                    LIMIT ?""",
                (*values, bounded_limit),
            ).fetchall()
        return [self._decode_snapshot(row) for row in rows]

    @staticmethod
    def _rate(
        posts: dict[tuple[str, str, str], dict[str, int]],
        bases: dict[tuple[str, str, str], dict[str, str]],
        numerator_metric: str,
        denominator_metric: str = "views",
    ) -> dict[str, Any]:
        candidates = [
            (key, values)
            for key, values in posts.items()
            if numerator_metric in values and denominator_metric in values
        ]
        invalid_counts = [
            (key, values)
            for key, values in candidates
            if numerator_metric in RETENTION_METRICS
            and values[numerator_metric] > values[denominator_metric]
        ]
        invalid_keys = {key for key, _ in invalid_counts}
        eligible = [
            values
            for key, values in candidates
            if key not in invalid_keys
            if bases.get(key, {}).get(numerator_metric)
            == RATE_ELIGIBLE_DENOMINATOR_BASIS
            and bases.get(key, {}).get(denominator_metric)
            == RATE_ELIGIBLE_DENOMINATOR_BASIS
        ]
        numerator = sum(values[numerator_metric] for values in eligible)
        denominator = sum(values[denominator_metric] for values in eligible)
        if denominator:
            status = "observed"
        elif eligible:
            status = "zero_eligible_denominator"
        elif invalid_counts:
            status = "invalid_metric_counts"
        elif candidates:
            status = "denominator_basis_not_eligible"
        else:
            status = "metric_not_reported_with_denominator"
        result = {
            "status": status,
            "numerator": numerator,
            "eligible_denominator": denominator,
            "denominator_metric": denominator_metric,
            "rate": round(numerator / denominator, 6) if denominator else None,
            "posts_with_numerator_and_denominator": len(eligible),
            "posts_excluded_for_denominator_basis": (
                len(candidates) - len(invalid_counts) - len(eligible)
            ),
            "required_denominator_basis": RATE_ELIGIBLE_DENOMINATOR_BASIS,
            "aggregation": "denominator_weighted",
            "causal_claim": False,
        }
        if invalid_counts:
            result["posts_excluded_for_invalid_counts"] = len(invalid_counts)
        return result

    def rollup(self, filters: dict[str, Any]) -> dict[str, Any]:
        scope = self._filters(filters)
        where, values = self._snapshot_query(scope)
        with closing(self.connect()) as connection:
            rows = connection.execute(
                f"""SELECT snapshots.*
                    FROM cq_script_metric_snapshots snapshots
                    JOIN cq_script_experiments experiments
                      ON experiments.experiment_id=snapshots.experiment_id
                    WHERE {where}
                    ORDER BY snapshots.observed_at ASC,
                             snapshots.created_at ASC,
                             snapshots.snapshot_id ASC""",
                values,
            ).fetchall()

        latest_by_post: dict[tuple[str, str, str], dict[str, int]] = {}
        latest_at_by_metric: dict[tuple[str, str, str], dict[str, str]] = {}
        latest_basis_by_metric: dict[tuple[str, str, str], dict[str, str]] = {}
        latest_observed_at: str | None = None
        for row in rows:
            key = (
                str(row["experiment_id"]),
                str(row["source_platform"]),
                str(row["provider_post_id"]),
            )
            values_by_metric = latest_by_post.setdefault(key, {})
            timestamps = latest_at_by_metric.setdefault(key, {})
            bases = latest_basis_by_metric.setdefault(key, {})
            observed_at = str(row["observed_at"])
            for metric, value in json.loads(str(row["metrics_json"])).items():
                if observed_at >= timestamps.get(metric, ""):
                    values_by_metric[metric] = int(value)
                    timestamps[metric] = observed_at
                    bases[metric] = str(row["view_denominator_basis"])
            latest_observed_at = max(latest_observed_at or observed_at, observed_at)

        totals = {
            metric: sum(
                metric_values.get(metric, 0)
                for metric_values in latest_by_post.values()
            )
            for metric in COUNT_METRICS
        }
        rates = {
            "hold_1s": self._rate(
                latest_by_post, latest_basis_by_metric, "hold_1s_views"
            ),
            "hold_3s": self._rate(
                latest_by_post, latest_basis_by_metric, "hold_3s_views"
            ),
            "hold_3s_from_1s": self._rate(
                latest_by_post,
                latest_basis_by_metric,
                "hold_3s_views",
                "hold_1s_views",
            ),
            "completion": self._rate(
                latest_by_post, latest_basis_by_metric, "completed_views"
            ),
            "cta": {
                metric.removeprefix("cta_"): self._rate(
                    latest_by_post, latest_basis_by_metric, metric
                )
                for metric in CTA_METRICS
            },
        }
        coverage = {
            metric: {
                "posts_reporting": sum(
                    1
                    for metric_values in latest_by_post.values()
                    if metric in metric_values
                ),
                "total_posts": len(latest_by_post),
            }
            for metric in COUNT_METRICS
        }
        quality_warnings: list[dict[str, Any]] = []
        for key, metric_values in latest_by_post.items():
            views = metric_values.get("views")
            if views is None:
                continue
            for metric in RETENTION_METRICS:
                if metric_values.get(metric, 0) > views:
                    quality_warnings.append(
                        {
                            "experiment_id": key[0],
                            "source_platform": key[1],
                            "provider_post_id": key[2],
                            "metric": metric,
                            "code": "RETENTION_COUNT_EXCEEDS_VIEWS",
                        }
                    )

        experiment_ids = sorted({str(row["experiment_id"]) for row in rows})
        script_ids = sorted({str(row["script_id"]) for row in rows})
        if not rows:
            experiments = self.experiments(scope, limit=500)
            experiment_ids = sorted(
                str(item["experiment_id"]) for item in experiments
            )
            script_ids = sorted({str(item["script_id"]) for item in experiments})
        return {
            "status": "ok",
            "contract": METRIC_ROLLUP_CONTRACT,
            "scope": scope,
            "experiment_ids": experiment_ids,
            "script_ids": script_ids,
            "measurement": (
                "latest_lifetime_cumulative_snapshot_per_post_and_metric"
            ),
            "metric_snapshot_count": len(rows),
            "post_count": len(latest_by_post),
            "latest_observed_at": latest_observed_at,
            "totals": totals,
            "rates": rates,
            "coverage": coverage,
            "data_quality": {
                "status": "pass" if not quality_warnings else "review",
                "warnings": quality_warnings,
            },
            "causal_policy": {
                "causal_claim": False,
                "note": (
                    "These are descriptive observed outcomes. Attribute causal "
                    "lift only through a separately designed experiment."
                ),
            },
        }

    def factor_rollup(self, filters: dict[str, Any]) -> dict[str, Any]:
        """Group descriptive outcomes by one immutable content factor."""

        if not isinstance(filters, dict):
            raise ValueError("factor rollup filters must be an object")
        dimension = _required_text(filters, "dimension", maximum=80)
        if dimension not in FACTOR_DIMENSIONS:
            raise ValueError(
                "dimension must be one of: " + ", ".join(FACTOR_DIMENSIONS)
            )
        scope = self._filters(
            {key: value for key, value in filters.items() if key != "dimension"}
        )
        experiment_clauses: list[str] = []
        experiment_values: list[str] = []
        for field in (
            "experiment_id", "script_id", "brief_id", "workflow_id"
        ):
            if field in scope:
                experiment_clauses.append(f"{field}=?")
                experiment_values.append(scope[field])
        with closing(self.connect()) as connection:
            experiment_rows = connection.execute(
                f"""SELECT * FROM cq_script_experiments
                    WHERE {' AND '.join(experiment_clauses)}
                    ORDER BY created_at DESC
                    LIMIT ?""",
                (*experiment_values, MAX_FACTOR_ROLLUP_EXPERIMENTS + 1),
            ).fetchall()
        if len(experiment_rows) > MAX_FACTOR_ROLLUP_EXPERIMENTS:
            raise ValueError(
                "factor rollup scope exceeds the experiment limit; "
                "narrow the workflow, script, or experiment scope"
            )
        experiments = [
            self._decode_experiment(row) for row in experiment_rows
        ]
        factor_by_experiment: dict[str, str] = {}
        missing_factor_experiment_ids: list[str] = []
        invalid_factor_experiments: list[dict[str, str]] = []
        for experiment in experiments:
            experiment_id = str(experiment["experiment_id"])
            try:
                vector = _content_factor_vector(
                    experiment.get("metadata") or {}
                )
            except ValueError as exc:
                invalid_factor_experiments.append({
                    "experiment_id": experiment_id,
                    "reason": str(exc),
                })
                continue
            if vector is None:
                missing_factor_experiment_ids.append(experiment_id)
                continue
            factor_by_experiment[experiment_id] = vector["factors"][dimension]

        where, values = self._snapshot_query(scope)
        with closing(self.connect()) as connection:
            snapshot_count = int(connection.execute(
                f"""SELECT COUNT(*)
                    FROM cq_script_metric_snapshots snapshots
                    JOIN cq_script_experiments experiments
                      ON experiments.experiment_id=snapshots.experiment_id
                    WHERE {where}""",
                values,
            ).fetchone()[0])
            if snapshot_count > MAX_FACTOR_ROLLUP_SNAPSHOTS:
                raise ValueError(
                    "factor rollup scope exceeds the snapshot limit; "
                    "narrow the workflow, script, or experiment scope"
                )
            rows = connection.execute(
                f"""SELECT snapshots.*
                    FROM cq_script_metric_snapshots snapshots
                    JOIN cq_script_experiments experiments
                      ON experiments.experiment_id=snapshots.experiment_id
                    WHERE {where}
                    ORDER BY snapshots.observed_at ASC,
                             snapshots.created_at ASC,
                             snapshots.snapshot_id ASC""",
                values,
            ).fetchall()

        latest_by_post: dict[tuple[str, str, str], dict[str, int]] = {}
        latest_at_by_metric: dict[tuple[str, str, str], dict[str, str]] = {}
        latest_basis_by_metric: dict[
            tuple[str, str, str], dict[str, str]
        ] = {}
        for row in rows:
            experiment_id = str(row["experiment_id"])
            if experiment_id not in factor_by_experiment:
                continue
            key = (
                experiment_id,
                str(row["source_platform"]),
                str(row["provider_post_id"]),
            )
            values_by_metric = latest_by_post.setdefault(key, {})
            timestamps = latest_at_by_metric.setdefault(key, {})
            bases = latest_basis_by_metric.setdefault(key, {})
            observed_at = str(row["observed_at"])
            for metric, value in json.loads(str(row["metrics_json"])).items():
                if observed_at >= timestamps.get(metric, ""):
                    values_by_metric[metric] = int(value)
                    timestamps[metric] = observed_at
                    bases[metric] = str(row["view_denominator_basis"])

        grouped_experiments: dict[str, list[str]] = {}
        for experiment_id, factor_value in factor_by_experiment.items():
            grouped_experiments.setdefault(factor_value, []).append(experiment_id)
        groups: list[dict[str, Any]] = []
        for factor_value in sorted(grouped_experiments):
            experiment_ids = sorted(grouped_experiments[factor_value])
            selected_ids = set(experiment_ids)
            posts = {
                key: value
                for key, value in latest_by_post.items()
                if key[0] in selected_ids
            }
            bases = {
                key: value
                for key, value in latest_basis_by_metric.items()
                if key[0] in selected_ids
            }
            totals = {
                metric: sum(
                    item.get(metric, 0) for item in posts.values()
                )
                for metric in COUNT_METRICS
            }
            quality_warnings: list[dict[str, str]] = []
            for key, item in posts.items():
                views = item.get("views")
                if views is None:
                    continue
                for metric in RETENTION_METRICS:
                    if item.get(metric, 0) > views:
                        quality_warnings.append({
                            "experiment_id": key[0],
                            "source_platform": key[1],
                            "provider_post_id": key[2],
                            "metric": metric,
                            "code": "RETENTION_COUNT_EXCEEDS_VIEWS",
                        })
            groups.append(
                {
                    "factor_value": factor_value,
                    "experiment_ids": experiment_ids,
                    "experiment_count": len(experiment_ids),
                    "metric_snapshot_count": sum(
                        str(row["experiment_id"]) in selected_ids for row in rows
                    ),
                    "post_count": len(posts),
                    "totals": totals,
                    "rates": {
                        "hold_1s": self._rate(
                            posts, bases, "hold_1s_views"
                        ),
                        "hold_3s": self._rate(
                            posts, bases, "hold_3s_views"
                        ),
                        "hold_3s_from_1s": self._rate(
                            posts,
                            bases,
                            "hold_3s_views",
                            "hold_1s_views",
                        ),
                        "completion": self._rate(
                            posts, bases, "completed_views"
                        ),
                        "cta": {
                            metric.removeprefix("cta_"): self._rate(
                                posts, bases, metric
                            )
                            for metric in CTA_METRICS
                        },
                    },
                    "coverage": {
                        metric: {
                            "posts_reporting": sum(
                                metric in item for item in posts.values()
                            ),
                            "total_posts": len(posts),
                        }
                        for metric in COUNT_METRICS
                    },
                    "data_quality": {
                        "status": (
                            "pass" if not quality_warnings else "review"
                        ),
                        "warnings": quality_warnings,
                    },
                }
            )
        return {
            "status": "ok",
            "contract": FACTOR_ROLLUP_CONTRACT,
            "scope": scope,
            "dimension": dimension,
            "measurement": (
                "latest_lifetime_cumulative_snapshot_per_post_and_metric"
            ),
            "groups": groups,
            "group_count": len(groups),
            "experiment_count": len(experiments),
            "missing_factor_experiment_ids": sorted(
                missing_factor_experiment_ids
            ),
            "invalid_factor_experiments": invalid_factor_experiments,
            "limits": {
                "maximum_experiments": MAX_FACTOR_ROLLUP_EXPERIMENTS,
                "maximum_snapshots": MAX_FACTOR_ROLLUP_SNAPSHOTS,
            },
            "causal_policy": {
                "causal_claim": False,
                "attribution_type": "descriptive_observational",
                "note": (
                    "Factor groups describe observed outcomes only. A separate "
                    "controlled design is required to attribute lift."
                ),
            },
        }


def register_script_experiment_routes(
    app: Flask,
    service: ScriptExperimentTelemetry,
    *,
    json_body: Callable[[], dict[str, Any]],
    require_auth: Callable[[], Any],
    audited_response: Callable[..., Any],
    invalid_response: Callable[..., Any],
) -> None:
    """Install authenticated telemetry routes on the ContentQuality app."""

    import time
    clock = time.monotonic
    emit = audited_response
    reject = invalid_response

    def conflict_result(error: Exception) -> dict[str, Any]:
        return {
            "status": "error",
            "code": "IMMUTABLE_EXPERIMENT_CONFLICT",
            "error": str(error),
        }

    def script_experiment_health():
        denied = require_auth()
        if denied:
            return denied
        started = clock()
        return emit(
            "script_experiment_health", {}, service.health(), started_at=started
        )

    def register_script_experiment():
        denied = require_auth()
        if denied:
            return denied
        started = clock()
        payload = json_body()
        try:
            result = service.register_experiment(payload)
        except ExperimentConflict as error:
            return emit(
                "register_script_experiment",
                payload,
                conflict_result(error),
                409,
                started,
            )
        except ValueError as error:
            return reject(
                "register_script_experiment", payload, error, started
            )
        return emit(
            "register_script_experiment",
            payload,
            result,
            201 if result["created"] else 200,
            started,
        )

    def list_script_experiments():
        denied = require_auth()
        if denied:
            return denied
        started = clock()
        parameters = request.args.to_dict(flat=True)
        try:
            limit = max(1, min(500, int(request.args.get("limit", "100"))))
            experiments = service.experiments(parameters, limit=limit)
        except ValueError as error:
            return reject(
                "list_script_experiments", parameters, error, started
            )
        return emit(
            "list_script_experiments",
            parameters,
            {
                "status": "ok",
                "contract": EXPERIMENT_CONTRACT,
                "experiments": experiments,
                "count": len(experiments),
                "limit": limit,
            },
            started_at=started,
        )

    def get_script_experiment(experiment_id: str):
        denied = require_auth()
        if denied:
            return denied
        started = clock()
        experiment = service.experiment(experiment_id)
        result = (
            {"status": "ok", "experiment": experiment}
            if experiment is not None
            else {
                "status": "error",
                "code": "SCRIPT_EXPERIMENT_NOT_FOUND",
                "experiment_id": experiment_id,
            }
        )
        return emit(
            "get_script_experiment",
            {"experiment_id": experiment_id},
            result,
            200 if experiment is not None else 404,
            started,
        )

    def ingest_script_experiment_metrics():
        denied = require_auth()
        if denied:
            return denied
        started = clock()
        payload = json_body()
        try:
            result = service.ingest_metric_snapshot(payload)
        except ExperimentConflict as error:
            return emit(
                "ingest_script_experiment_metrics",
                payload,
                conflict_result(error),
                409,
                started,
            )
        except ValueError as error:
            return reject(
                "ingest_script_experiment_metrics", payload, error, started
            )
        return emit(
            "ingest_script_experiment_metrics",
            payload,
            result,
            201 if result["created"] else 200,
            started,
        )

    def list_script_experiment_metrics():
        denied = require_auth()
        if denied:
            return denied
        started = clock()
        parameters = request.args.to_dict(flat=True)
        try:
            limit = max(1, min(2000, int(request.args.get("limit", "500"))))
            snapshots = service.metric_snapshots(parameters, limit=limit)
        except ValueError as error:
            return reject(
                "list_script_experiment_metrics", parameters, error, started
            )
        return emit(
            "list_script_experiment_metrics",
            parameters,
            {
                "status": "ok",
                "contract": METRIC_SNAPSHOT_CONTRACT,
                "snapshots": snapshots,
                "count": len(snapshots),
                "limit": limit,
            },
            started_at=started,
        )

    def script_experiment_rollup():
        denied = require_auth()
        if denied:
            return denied
        started = clock()
        parameters = request.args.to_dict(flat=True)
        try:
            result = service.rollup(parameters)
        except ValueError as error:
            return reject(
                "script_experiment_rollup", parameters, error, started
            )
        return emit(
            "script_experiment_rollup", parameters, result, started_at=started
        )

    def script_factor_rollup():
        denied = require_auth()
        if denied:
            return denied
        started = clock()
        parameters = request.args.to_dict(flat=True)
        try:
            result = service.factor_rollup(parameters)
        except ValueError as error:
            return reject("script_factor_rollup", parameters, error, started)
        return emit(
            "script_factor_rollup", parameters, result, started_at=started
        )

    routes = (
        (
            "/api/script-experiments/health",
            "script_experiment_health",
            script_experiment_health,
            ["GET"],
        ),
        (
            "/api/script-experiments",
            "register_script_experiment",
            register_script_experiment,
            ["POST"],
        ),
        (
            "/api/script-experiments",
            "list_script_experiments",
            list_script_experiments,
            ["GET"],
        ),
        (
            "/api/script-experiments/<experiment_id>",
            "get_script_experiment",
            get_script_experiment,
            ["GET"],
        ),
        (
            "/api/script-experiments/metrics",
            "ingest_script_experiment_metrics",
            ingest_script_experiment_metrics,
            ["POST"],
        ),
        (
            "/api/script-experiments/metrics",
            "list_script_experiment_metrics",
            list_script_experiment_metrics,
            ["GET"],
        ),
        (
            "/api/script-experiments/rollup",
            "script_experiment_rollup",
            script_experiment_rollup,
            ["GET"],
        ),
        (
            "/api/v2/script-experiments/factor-rollup",
            "script_factor_rollup",
            script_factor_rollup,
            ["GET"],
        ),
    )
    for rule, endpoint, view_func, methods in routes:
        app.add_url_rule(
            rule, endpoint=endpoint, view_func=view_func, methods=methods
        )
