"""Audited Upwork market-demand ledger, forecasts, and semantic handoff.

The ledger is intentionally separate from the social-video predictor: Upwork is
buyer-demand evidence, not audience-engagement evidence.  Every table is
append-only, every metered request is reserved before provider execution, and
script consumers receive approved aggregate evidence only.
"""

from __future__ import annotations

import gzip
import json
import math
import os
import re
import sqlite3
import statistics
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Sequence
from urllib.parse import urlsplit, urlunsplit
from uuid import uuid4

import httpx

from .config import MarketTapeConfig
from .models import isoformat, parse_datetime, stable_hash, utc_now
from .sources.upwork import UpworkAPIError, UpworkRapidAPIClient


UPWORK_REQUEST_RESERVATION_CONTRACT = (
    "market_tape_upwork_request_reservation_v1"
)
UPWORK_SCAN_CONTRACT = "market_tape_upwork_scan_v1"
UPWORK_JOB_CONTRACT = "market_tape_upwork_job_v1"
UPWORK_JOB_VERSION_CONTRACT = "market_tape_upwork_job_version_v1"
UPWORK_QUERY_OBSERVATION_CONTRACT = (
    "market_tape_upwork_query_observation_v1"
)
UPWORK_JOB_OBSERVATION_CONTRACT = "market_tape_upwork_job_observation_v1"
UPWORK_DEMAND_SNAPSHOT_CONTRACT = "market_tape_upwork_demand_snapshot_v1"
UPWORK_PREDICTION_CONTRACT = "market_tape_upwork_demand_prediction_v1"
UPWORK_PREDICTION_OUTCOME_CONTRACT = (
    "market_tape_upwork_prediction_outcome_v1"
)
UPWORK_SEMANTIC_LINK_CONTRACT = "upwork_market_demand_signal_v1"
UPWORK_SCRIPT_CONTEXT_CONTRACT = "upwork_market_demand_script_context_v2"
UPWORK_PREDICTION_MODEL_VERSION = "upwork-demand-direction-v1"

UPWORK_TABLE_ENTITY_TYPES: tuple[tuple[str, str, str], ...] = (
    (
        "mt_upwork_request_reservations",
        "request_reservation_id",
        "upwork_request_reservation",
    ),
    ("mt_upwork_scan_runs", "scan_run_id", "upwork_scan_run"),
    ("mt_upwork_jobs", "job_id", "upwork_job"),
    ("mt_upwork_job_versions", "job_version_id", "upwork_job_version"),
    (
        "mt_upwork_query_observations",
        "query_observation_id",
        "upwork_query_observation",
    ),
    (
        "mt_upwork_job_observations",
        "job_observation_id",
        "upwork_job_observation",
    ),
    (
        "mt_upwork_demand_snapshots",
        "demand_snapshot_id",
        "upwork_demand_snapshot",
    ),
    ("mt_upwork_predictions", "prediction_id", "upwork_prediction"),
    (
        "mt_upwork_prediction_outcomes",
        "prediction_outcome_id",
        "upwork_prediction_outcome",
    ),
    (
        "mt_upwork_semantic_links",
        "semantic_link_id",
        "upwork_semantic_link",
    ),
)

TOKEN_RE = re.compile(r"[a-z0-9][a-z0-9+#.-]*", re.IGNORECASE)
AI_TERMS = {
    "ai", "artificial intelligence", "chatgpt", "claude", "gemini",
    "llm", "machine learning", "openai", "rag", "stable diffusion",
}
VERTICAL_TERMS = {
    "accounting", "ecommerce", "education", "finance", "healthcare",
    "legal", "marketing", "real estate", "recruiting", "sales",
}


def ensure_upwork_schema(connection: sqlite3.Connection) -> None:
    """Create the local append-only Upwork evidence schema."""

    connection.executescript(
        f"""
        CREATE TABLE IF NOT EXISTS mt_upwork_request_reservations (
            request_reservation_id TEXT PRIMARY KEY,
            contract TEXT NOT NULL CHECK(
                contract = '{UPWORK_REQUEST_RESERVATION_CONTRACT}'
            ),
            reserved_at TEXT NOT NULL,
            usage_date TEXT NOT NULL,
            request_units INTEGER NOT NULL CHECK(request_units > 0),
            query_set_sha256 TEXT NOT NULL CHECK(length(query_set_sha256) = 64),
            reservation_sha256 TEXT NOT NULL UNIQUE CHECK(
                length(reservation_sha256) = 64
            )
        );

        CREATE INDEX IF NOT EXISTS mt_upwork_reservations_usage_idx
            ON mt_upwork_request_reservations(usage_date, reserved_at);

        CREATE TABLE IF NOT EXISTS mt_upwork_scan_runs (
            scan_run_id TEXT PRIMARY KEY,
            contract TEXT NOT NULL CHECK(contract = '{UPWORK_SCAN_CONTRACT}'),
            request_reservation_id TEXT NOT NULL UNIQUE,
            started_at TEXT NOT NULL,
            finished_at TEXT NOT NULL,
            observed_at TEXT NOT NULL,
            query_count INTEGER NOT NULL CHECK(query_count > 0),
            request_units INTEGER NOT NULL CHECK(request_units >= 0),
            accepted_job_count INTEGER NOT NULL CHECK(accepted_job_count >= 0),
            rejected_job_count INTEGER NOT NULL CHECK(rejected_job_count >= 0),
            state TEXT NOT NULL CHECK(state IN ('complete', 'partial', 'failed')),
            raw_archive_sha256 TEXT NOT NULL CHECK(
                raw_archive_sha256 = '' OR length(raw_archive_sha256) = 64
            ),
            error_code TEXT NOT NULL DEFAULT '',
            error_detail TEXT NOT NULL DEFAULT '',
            scan_sha256 TEXT NOT NULL UNIQUE CHECK(length(scan_sha256) = 64),
            FOREIGN KEY(request_reservation_id)
                REFERENCES mt_upwork_request_reservations(request_reservation_id)
        );

        CREATE INDEX IF NOT EXISTS mt_upwork_scans_observed_idx
            ON mt_upwork_scan_runs(observed_at DESC, scan_run_id);

        CREATE TABLE IF NOT EXISTS mt_upwork_jobs (
            job_id TEXT PRIMARY KEY,
            contract TEXT NOT NULL CHECK(contract = '{UPWORK_JOB_CONTRACT}'),
            provider_job_id TEXT NOT NULL UNIQUE,
            canonical_url TEXT NOT NULL UNIQUE,
            first_seen_at TEXT NOT NULL,
            identity_sha256 TEXT NOT NULL UNIQUE CHECK(length(identity_sha256) = 64)
        );

        CREATE TABLE IF NOT EXISTS mt_upwork_job_versions (
            job_version_id TEXT PRIMARY KEY,
            contract TEXT NOT NULL CHECK(
                contract = '{UPWORK_JOB_VERSION_CONTRACT}'
            ),
            job_id TEXT NOT NULL,
            observed_at TEXT NOT NULL,
            title TEXT NOT NULL,
            description TEXT NOT NULL,
            published_at TEXT,
            client_id TEXT NOT NULL DEFAULT '',
            budget_type TEXT NOT NULL DEFAULT '',
            budget_amount REAL,
            budget_currency TEXT NOT NULL DEFAULT '',
            hourly_min REAL,
            hourly_max REAL,
            proposal_count INTEGER,
            experience_level TEXT NOT NULL DEFAULT '',
            country TEXT NOT NULL DEFAULT '',
            skills_json TEXT NOT NULL,
            category TEXT NOT NULL CHECK(category IN (
                'ai_demand', 'ai_enabled_vertical',
                'general_freelancing', 'other'
            )),
            request_intent TEXT NOT NULL,
            raw_archive_sha256 TEXT NOT NULL CHECK(
                length(raw_archive_sha256) = 64
            ),
            payload_sha256 TEXT NOT NULL CHECK(length(payload_sha256) = 64),
            version_sha256 TEXT NOT NULL UNIQUE CHECK(length(version_sha256) = 64),
            FOREIGN KEY(job_id) REFERENCES mt_upwork_jobs(job_id)
        );

        CREATE INDEX IF NOT EXISTS mt_upwork_versions_job_time_idx
            ON mt_upwork_job_versions(job_id, observed_at DESC, job_version_id);

        CREATE TABLE IF NOT EXISTS mt_upwork_query_observations (
            query_observation_id TEXT PRIMARY KEY,
            contract TEXT NOT NULL CHECK(
                contract = '{UPWORK_QUERY_OBSERVATION_CONTRACT}'
            ),
            scan_run_id TEXT NOT NULL,
            query_text TEXT NOT NULL,
            normalized_query TEXT NOT NULL,
            observed_at TEXT NOT NULL,
            returned_count INTEGER NOT NULL CHECK(returned_count >= 0),
            accepted_count INTEGER NOT NULL CHECK(accepted_count >= 0),
            rejected_count INTEGER NOT NULL CHECK(rejected_count >= 0),
            partial_evidence INTEGER NOT NULL CHECK(partial_evidence IN (0, 1)),
            response_sha256 TEXT NOT NULL CHECK(length(response_sha256) = 64),
            observation_sha256 TEXT NOT NULL UNIQUE CHECK(
                length(observation_sha256) = 64
            ),
            FOREIGN KEY(scan_run_id) REFERENCES mt_upwork_scan_runs(scan_run_id)
        );

        CREATE INDEX IF NOT EXISTS mt_upwork_queries_query_time_idx
            ON mt_upwork_query_observations(
                normalized_query, observed_at DESC, query_observation_id
            );

        CREATE TABLE IF NOT EXISTS mt_upwork_job_observations (
            job_observation_id TEXT PRIMARY KEY,
            contract TEXT NOT NULL CHECK(
                contract = '{UPWORK_JOB_OBSERVATION_CONTRACT}'
            ),
            scan_run_id TEXT NOT NULL,
            query_observation_id TEXT NOT NULL,
            job_id TEXT NOT NULL,
            job_version_id TEXT NOT NULL,
            observed_at TEXT NOT NULL,
            is_new_job INTEGER NOT NULL CHECK(is_new_job IN (0, 1)),
            result_position INTEGER NOT NULL CHECK(result_position >= 0),
            observation_sha256 TEXT NOT NULL UNIQUE CHECK(
                length(observation_sha256) = 64
            ),
            UNIQUE(scan_run_id, query_observation_id, job_id),
            FOREIGN KEY(scan_run_id) REFERENCES mt_upwork_scan_runs(scan_run_id),
            FOREIGN KEY(query_observation_id)
                REFERENCES mt_upwork_query_observations(query_observation_id),
            FOREIGN KEY(job_id) REFERENCES mt_upwork_jobs(job_id),
            FOREIGN KEY(job_version_id)
                REFERENCES mt_upwork_job_versions(job_version_id)
        );

        CREATE INDEX IF NOT EXISTS mt_upwork_job_observations_job_time_idx
            ON mt_upwork_job_observations(
                job_id, observed_at DESC, job_observation_id
            );

        CREATE TABLE IF NOT EXISTS mt_upwork_demand_snapshots (
            demand_snapshot_id TEXT PRIMARY KEY,
            contract TEXT NOT NULL CHECK(
                contract = '{UPWORK_DEMAND_SNAPSHOT_CONTRACT}'
            ),
            scan_run_id TEXT NOT NULL,
            cohort_type TEXT NOT NULL CHECK(cohort_type IN (
                'query', 'category', 'skill', 'intent'
            )),
            cohort_key TEXT NOT NULL,
            observed_at TEXT NOT NULL,
            unique_jobs INTEGER NOT NULL CHECK(unique_jobs >= 0),
            new_jobs INTEGER NOT NULL CHECK(
                new_jobs >= 0 AND new_jobs <= unique_jobs
            ),
            unique_clients INTEGER NOT NULL CHECK(
                unique_clients >= 0 AND unique_clients <= unique_jobs
            ),
            fixed_budget_usd_coverage REAL NOT NULL CHECK(
                fixed_budget_usd_coverage BETWEEN 0.0 AND 1.0
            ),
            median_fixed_budget_usd REAL CHECK(
                median_fixed_budget_usd IS NULL OR median_fixed_budget_usd >= 0
            ),
            hourly_rate_usd_coverage REAL NOT NULL CHECK(
                hourly_rate_usd_coverage BETWEEN 0.0 AND 1.0
            ),
            median_hourly_rate_usd REAL CHECK(
                median_hourly_rate_usd IS NULL OR median_hourly_rate_usd >= 0
            ),
            proposal_coverage REAL NOT NULL CHECK(
                proposal_coverage BETWEEN 0.0 AND 1.0
            ),
            median_proposals REAL CHECK(
                median_proposals IS NULL OR median_proposals >= 0
            ),
            velocity REAL NOT NULL,
            acceleration REAL NOT NULL,
            evidence_state TEXT NOT NULL CHECK(evidence_state IN (
                'complete', 'partial', 'insufficient'
            )),
            partial_evidence INTEGER NOT NULL CHECK(partial_evidence IN (0, 1)),
            evidence_sha256 TEXT NOT NULL CHECK(length(evidence_sha256) = 64),
            snapshot_sha256 TEXT NOT NULL UNIQUE CHECK(length(snapshot_sha256) = 64),
            UNIQUE(scan_run_id, cohort_type, cohort_key),
            FOREIGN KEY(scan_run_id) REFERENCES mt_upwork_scan_runs(scan_run_id)
        );

        CREATE INDEX IF NOT EXISTS mt_upwork_snapshots_cohort_time_idx
            ON mt_upwork_demand_snapshots(
                cohort_type, cohort_key, observed_at DESC, demand_snapshot_id
            );

        CREATE TABLE IF NOT EXISTS mt_upwork_predictions (
            prediction_id TEXT PRIMARY KEY,
            contract TEXT NOT NULL CHECK(
                contract = '{UPWORK_PREDICTION_CONTRACT}'
            ),
            demand_snapshot_id TEXT NOT NULL UNIQUE,
            cohort_type TEXT NOT NULL,
            cohort_key TEXT NOT NULL,
            as_of TEXT NOT NULL,
            direction TEXT NOT NULL CHECK(direction IN (
                'rising', 'falling', 'flat', 'abstain'
            )),
            confidence REAL NOT NULL CHECK(confidence BETWEEN 0.0 AND 1.0),
            model_version TEXT NOT NULL,
            history_snapshot_ids_json TEXT NOT NULL,
            input_sha256 TEXT NOT NULL CHECK(length(input_sha256) = 64),
            prediction_sha256 TEXT NOT NULL UNIQUE CHECK(
                length(prediction_sha256) = 64
            ),
            FOREIGN KEY(demand_snapshot_id)
                REFERENCES mt_upwork_demand_snapshots(demand_snapshot_id)
        );

        CREATE INDEX IF NOT EXISTS mt_upwork_predictions_cohort_time_idx
            ON mt_upwork_predictions(cohort_type, cohort_key, as_of DESC);

        CREATE TABLE IF NOT EXISTS mt_upwork_prediction_outcomes (
            prediction_outcome_id TEXT PRIMARY KEY,
            contract TEXT NOT NULL CHECK(
                contract = '{UPWORK_PREDICTION_OUTCOME_CONTRACT}'
            ),
            prediction_id TEXT NOT NULL UNIQUE,
            observed_snapshot_id TEXT NOT NULL,
            evaluated_at TEXT NOT NULL,
            actual_direction TEXT NOT NULL CHECK(actual_direction IN (
                'rising', 'falling', 'flat'
            )),
            directional_correct INTEGER,
            brier_score REAL,
            outcome_sha256 TEXT NOT NULL UNIQUE CHECK(length(outcome_sha256) = 64),
            FOREIGN KEY(prediction_id)
                REFERENCES mt_upwork_predictions(prediction_id),
            FOREIGN KEY(observed_snapshot_id)
                REFERENCES mt_upwork_demand_snapshots(demand_snapshot_id)
        );

        CREATE INDEX IF NOT EXISTS mt_upwork_outcomes_evaluated_idx
            ON mt_upwork_prediction_outcomes(evaluated_at DESC);

        CREATE TABLE IF NOT EXISTS mt_upwork_semantic_links (
            semantic_link_id TEXT PRIMARY KEY,
            contract TEXT NOT NULL CHECK(
                contract = '{UPWORK_SEMANTIC_LINK_CONTRACT}'
            ),
            demand_snapshot_id TEXT NOT NULL,
            signal_id TEXT NOT NULL,
            graph_version_id TEXT NOT NULL,
            cohort_type TEXT NOT NULL,
            cohort_key TEXT NOT NULL,
            created_at TEXT NOT NULL,
            automatic_binding INTEGER NOT NULL CHECK(automatic_binding = 0),
            link_sha256 TEXT NOT NULL UNIQUE CHECK(length(link_sha256) = 64),
            UNIQUE(demand_snapshot_id, signal_id, graph_version_id),
            FOREIGN KEY(demand_snapshot_id)
                REFERENCES mt_upwork_demand_snapshots(demand_snapshot_id),
            FOREIGN KEY(signal_id, graph_version_id)
                REFERENCES mt_topic_signal_candidates(signal_id, graph_version_id),
            FOREIGN KEY(graph_version_id)
                REFERENCES mt_topic_graph_versions(graph_version_id)
        );

        CREATE INDEX IF NOT EXISTS mt_upwork_semantic_links_signal_idx
            ON mt_upwork_semantic_links(
                graph_version_id, signal_id, created_at DESC
            );
        """
    )
    for table, _, _ in UPWORK_TABLE_ENTITY_TYPES:
        connection.executescript(
            f"""
            CREATE TRIGGER IF NOT EXISTS {table}_no_update
            BEFORE UPDATE ON {table}
            BEGIN
                SELECT RAISE(ABORT, '{table} is append-only');
            END;
            CREATE TRIGGER IF NOT EXISTS {table}_no_delete
            BEFORE DELETE ON {table}
            BEGIN
                SELECT RAISE(ABORT, '{table} is append-only');
            END;
            """
        )


def _canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)


def _row_dict(row: sqlite3.Row | Mapping[str, Any]) -> dict[str, Any]:
    return dict(row)


def _id(prefix: str, value: Any) -> str:
    return f"{prefix}:{stable_hash(value)}"


def _utc_text(value: str | datetime | None) -> str:
    if value in (None, ""):
        return str(isoformat(utc_now()))
    parsed = parse_datetime(value)
    if parsed is None:
        raise ValueError("observed_at must be an ISO-8601 timestamp")
    return str(isoformat(parsed))


def _normalized_text(value: Any) -> str:
    return " ".join(str(value or "").strip().lower().split())


def _number(value: Any) -> float | None:
    if value in (None, "") or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        number = float(value)
        return number if math.isfinite(number) and number >= 0 else None
    text = str(value).replace(",", "").replace("$", "").strip()
    try:
        number = float(text)
    except ValueError:
        return None
    return number if math.isfinite(number) and number >= 0 else None


def _normalize_currency(value: Any) -> str:
    normalized = " ".join(str(value or "").strip().upper().split())
    if normalized in {"$", "US$", "USD", "USD$", "US DOLLAR", "US DOLLARS"}:
        return "USD"
    return normalized


def _integer(value: Any) -> int | None:
    number = _number(value)
    if number is None or not float(number).is_integer():
        return None
    return int(number)


def _first(mapping: Mapping[str, Any], *names: str) -> Any:
    for name in names:
        if name in mapping and mapping[name] not in (None, ""):
            return mapping[name]
    return None


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _nested(mapping: Mapping[str, Any], *names: str) -> Mapping[str, Any]:
    return _mapping(_first(mapping, *names))


def _extract_jobs(response: Mapping[str, Any]) -> list[Any]:
    for candidate in (
        response.get("jobs"),
        response.get("results"),
        response.get("items"),
        response.get("data"),
    ):
        if isinstance(candidate, list):
            return list(candidate)
        if isinstance(candidate, Mapping):
            for key in ("jobs", "results", "items"):
                nested = candidate.get(key)
                if isinstance(nested, list):
                    return list(nested)
    return []


def _canonical_upwork_url(value: Any) -> str:
    text = str(value or "").strip()
    parsed = urlsplit(text)
    hostname = (parsed.hostname or "").lower()
    if parsed.scheme.lower() != "https" or hostname not in {
        "upwork.com",
        "www.upwork.com",
    }:
        raise ValueError("job URL must be a canonical HTTPS Upwork URL")
    path = re.sub(r"/{2,}", "/", parsed.path or "/").rstrip("/") or "/"
    return urlunsplit(("https", "www.upwork.com", path, "", ""))


def _classify(title: str, description: str, skills: Sequence[str]) -> tuple[str, str]:
    haystack = _normalized_text(" ".join((title, description, *skills)))

    def contains(term: str) -> bool:
        phrase = re.escape(_normalized_text(term)).replace(r"\ ", r"\s+")
        return bool(re.search(rf"(?<![a-z0-9]){phrase}(?![a-z0-9])", haystack))

    has_ai = any(contains(term) for term in AI_TERMS)
    has_vertical = any(contains(term) for term in VERTICAL_TERMS)
    if has_ai and has_vertical:
        category = "ai_enabled_vertical"
    elif has_ai:
        category = "ai_demand"
    elif haystack:
        category = "general_freelancing"
    else:
        category = "other"
    if any(contains(term) for term in ("automation", "workflow", "zapier")):
        intent = "build_automation"
    elif has_ai and any(
        contains(term)
        for term in ("app", "agent", "assistant", "product", "saas")
    ):
        intent = "build_ai_product"
    elif any(
        contains(term)
        for term in ("content", "copy", "marketing", "social media")
    ):
        intent = "content_marketing"
    elif any(
        contains(term) for term in ("analytics", "data", "dashboard", "sql")
    ):
        intent = "data_analytics"
    else:
        intent = "general_delivery"
    return category, intent


def _normalized_skills(raw: Mapping[str, Any]) -> list[str]:
    value = _first(raw, "skills", "skillTags", "skill_tags")
    if isinstance(value, str):
        candidates: Iterable[Any] = value.split(",")
    elif isinstance(value, Sequence):
        candidates = value
    else:
        candidates = ()
    result: set[str] = set()
    for item in candidates:
        if isinstance(item, Mapping):
            item = _first(item, "name", "skill", "label")
        normalized = _normalized_text(item)
        if normalized:
            result.add(normalized[:120])
    return sorted(result)[:30]


def _normalize_job(raw: Mapping[str, Any], observed_at: str) -> dict[str, Any]:
    provider_job_id = str(
        _first(raw, "id", "jobId", "job_id", "ciphertext", "uid") or ""
    ).strip()
    if not provider_job_id:
        raise ValueError("stable provider job id is required")
    title = " ".join(str(_first(raw, "title", "jobTitle", "job_title") or "").split())
    if not title:
        raise ValueError("job title is required")
    canonical_url = _canonical_upwork_url(
        _first(raw, "url", "jobUrl", "job_url", "link")
    )
    raw_description = _first(
        raw, "description", "jobDescription", "job_description", "snippet"
    )
    if isinstance(raw_description, Mapping):
        raw_description = _first(raw_description, "text", "snippet", "value")
    description = str(raw_description or "").strip()
    client = _nested(raw, "client", "buyer")
    budget = _nested(raw, "budget", "amount")
    hourly = _nested(raw, "hourlyBudget", "hourly_budget", "hourly")
    skills = _normalized_skills(raw)
    category, request_intent = _classify(title, description, skills)
    budget_type = _normalized_text(
        _first(raw, "budgetType", "budget_type", "jobType", "job_type")
        or _first(budget, "type")
    )
    budget_amount = _number(
        _first(raw, "budgetAmount", "budget_amount", "fixedPrice", "fixed_price")
        or _first(budget, "amount", "value")
    )
    hourly_min = _number(
        _first(raw, "hourlyMin", "hourly_min") or _first(hourly, "min", "minimum")
    )
    hourly_max = _number(
        _first(raw, "hourlyMax", "hourly_max") or _first(hourly, "max", "maximum")
    )
    published = parse_datetime(
        _first(
            raw,
            "publishedAt",
            "published_at",
            "createdAt",
            "created_at",
            "postedOn",
            "postedText",
        )
    )
    core = {
        "provider_job_id": provider_job_id,
        "canonical_url": canonical_url,
        "observed_at": observed_at,
        "title": title,
        "description": description,
        "published_at": isoformat(published),
        "client_id": str(
            _first(raw, "clientId", "client_id")
            or _first(client, "id", "clientId", "client_id")
            or ""
        ).strip(),
        "budget_type": budget_type,
        "budget_amount": budget_amount,
        "budget_currency": _normalize_currency(
            _first(raw, "currency", "budgetCurrency", "budget_currency")
            or _first(budget, "currency")
            or ""
        ),
        "hourly_min": hourly_min,
        "hourly_max": hourly_max,
        "proposal_count": _integer(
            _first(raw, "proposalCount", "proposal_count", "proposals")
        ),
        "experience_level": str(
            _first(raw, "experienceLevel", "experience_level", "experience") or ""
        ).strip(),
        "country": str(
            _first(raw, "country", "clientCountry", "client_country")
            or _first(client, "country")
            or ""
        ).strip(),
        "skills": skills,
        "category": category,
        "request_intent": request_intent,
        "payload_sha256": stable_hash(raw),
    }
    core["job_id"] = f"upwork:{provider_job_id}"
    core["identity_sha256"] = stable_hash(
        {"provider_job_id": provider_job_id, "canonical_url": canonical_url}
    )
    core["version_sha256"] = stable_hash(
        {key: value for key, value in core.items() if key != "observed_at"}
    )
    core["job_version_id"] = _id(
        "upwork-job-version",
        {"job_id": core["job_id"], "version_sha256": core["version_sha256"]},
    )
    return core


def _median(values: Sequence[float | int]) -> float | None:
    return float(statistics.median(values)) if values else None


def _table_exists(connection: sqlite3.Connection, table: str) -> bool:
    return connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (table,)
    ).fetchone() is not None


def _enqueue_outbox(
    connection: sqlite3.Connection,
    entity_type: str,
    entity_key: str,
    payload: Mapping[str, Any],
) -> None:
    if not _table_exists(connection, "mt_sync_outbox"):
        return
    now = str(isoformat(utc_now()))
    connection.execute(
        """INSERT INTO mt_sync_outbox(
               entity_type, entity_key, payload_json, created_at, next_attempt_at
           ) VALUES(?, ?, ?, ?, ?)
           ON CONFLICT(entity_type, entity_key) DO NOTHING""",
        (entity_type, entity_key, _canonical_json(dict(payload)), now, now),
    )


def _enqueue_row(
    connection: sqlite3.Connection,
    table: str,
    key_column: str,
    entity_type: str,
    key: str,
) -> None:
    row = connection.execute(
        f"SELECT * FROM {table} WHERE {key_column} = ?", (key,)
    ).fetchone()
    if row is not None:
        _enqueue_outbox(connection, entity_type, key, _row_dict(row))


class UpworkDemandService:
    """Own the append-only Upwork acquisition and demand-analysis boundary."""

    def __init__(
        self,
        config: MarketTapeConfig,
        *,
        client: httpx.Client | None = None,
        clock: Callable[[], datetime] | None = None,
        test_base_url: str | None = None,
        allow_loopback_test_transport: bool = False,
    ) -> None:
        self.config = config
        self.config.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.config.object_dir.mkdir(parents=True, exist_ok=True)
        self._clock = clock or utc_now
        self.source = UpworkRapidAPIClient(
            config,
            client=client,
            test_base_url=test_base_url,
            allow_loopback_test_transport=allow_loopback_test_transport,
        )
        with self._connect() as connection:
            ensure_upwork_schema(connection)

    def close(self) -> None:
        self.source.close()

    def __enter__(self) -> UpworkDemandService:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.config.db_path, timeout=30.0)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 30000")
        return connection

    def health(self) -> dict[str, Any]:
        """Return credit-free provider readiness plus local ledger integrity."""

        now = self._clock()
        usage_date = now.date().isoformat()
        with self._connect() as connection:
            counts = {
                table.removeprefix("mt_upwork_"): int(
                    connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
                )
                for table, _, _ in UPWORK_TABLE_ENTITY_TYPES
            }
            used = int(
                connection.execute(
                    """SELECT COALESCE(SUM(request_units), 0)
                       FROM mt_upwork_request_reservations WHERE usage_date = ?""",
                    (usage_date,),
                ).fetchone()[0]
            )
            unfulfilled = int(
                connection.execute(
                    """SELECT COUNT(*)
                       FROM mt_upwork_request_reservations reservation
                       LEFT JOIN mt_upwork_scan_runs scan
                         ON scan.request_reservation_id = reservation.request_reservation_id
                       WHERE scan.scan_run_id IS NULL"""
                ).fetchone()[0]
            )
        provider = self.source.health()
        daily_limit = int(getattr(self.config, "upwork_daily_request_limit", 10))
        if not provider["configured"]:
            status = "blocked_credential"
        elif unfulfilled:
            status = "degraded_audit"
        else:
            status = "ready"
        return {
            "contract": "market_tape_upwork_health_v1",
            "status": status,
            "provider": provider,
            "credit_free": True,
            "ledger": {
                "usage_date": usage_date,
                "daily_request_limit": daily_limit,
                "reserved_request_units": used,
                "remaining_request_units": max(0, daily_limit - used),
                "unfulfilled_reservations": unfulfilled,
                "table_counts": counts,
                "append_only": True,
            },
        }

    def scan(
        self,
        *,
        queries: Sequence[str] | None = None,
        execute_metered_reads: bool = False,
        max_jobs_per_query: int = 50,
        sort: str = "recency",
    ) -> dict[str, Any]:
        """Run one bounded, crash-audited search scan.

        A durable reservation is committed before the first provider request.
        Known provider failures receive a terminal scan row; process crashes and
        ``BaseException`` leave a visible, budget-consuming orphan reservation.
        """

        if not execute_metered_reads:
            raise UpworkAPIError(
                "metered RapidAPI reads require execute_metered_reads=true",
                code="metered_reads_disabled",
                status_code=403,
            )
        if not self.config.allow_metered_reads:
            raise UpworkAPIError(
                "MARKET_TAPE_ALLOW_METERED_READS is disabled",
                code="metered_reads_not_configured",
                status_code=403,
            )
        if not self.source.health()["configured"]:
            raise UpworkAPIError(
                "Upwork RapidAPI credentials are not configured",
                code="not_configured",
                status_code=503,
            )
        normalized_queries = self._normalize_queries(queries)
        max_queries = int(getattr(self.config, "upwork_max_queries_per_scan", 5))
        if len(normalized_queries) > max_queries:
            raise ValueError(f"at most {max_queries} queries may be scanned at once")
        if not 1 <= int(max_jobs_per_query) <= 1000:
            raise ValueError("max_jobs_per_query must be between 1 and 1000")
        captured_at = self._clock()
        observed_text = _utc_text(captured_at)
        started_at = observed_text
        reservation = self._reserve_requests(normalized_queries, started_at)
        query_results: list[dict[str, Any]] = []
        request_units = 0
        terminal_error: UpworkAPIError | None = None
        for query in normalized_queries:
            try:
                response = self.source.search_jobs(
                    keyword=query,
                    sort=sort,
                    execute_metered_reads=True,
                )
                request_units += 1
                provider_jobs = _extract_jobs(response)
                local_truncation = len(provider_jobs) > int(max_jobs_per_query)
                raw_jobs = provider_jobs[: int(max_jobs_per_query)]
                normalized_jobs: list[dict[str, Any]] = []
                rejections: list[dict[str, str]] = []
                seen_job_ids: set[str] = set()
                for raw in raw_jobs:
                    if not isinstance(raw, Mapping):
                        rejections.append(
                            {
                                "payload_sha256": stable_hash(raw),
                                "reason": "provider job item must be an object",
                            }
                        )
                        continue
                    try:
                        normalized_job = _normalize_job(raw, observed_text)
                    except ValueError as error:
                        rejections.append(
                            {
                                "payload_sha256": stable_hash(raw),
                                "reason": str(error),
                            }
                        )
                        continue
                    job_id = str(normalized_job["job_id"])
                    if job_id in seen_job_ids:
                        rejections.append(
                            {
                                "payload_sha256": stable_hash(raw),
                                "reason": (
                                    "duplicate provider job id within query"
                                ),
                            }
                        )
                        continue
                    seen_job_ids.add(job_id)
                    normalized_jobs.append(normalized_job)
                query_results.append(
                    {
                        "query": query,
                        "response": response,
                        # The payload itself is authoritative for how many
                        # records were returned. Provider metadata remains in
                        # the raw archive but cannot corrupt ledger counts.
                        "returned_count": len(provider_jobs),
                        "jobs": normalized_jobs,
                        "rejections": rejections,
                        "partial_evidence": bool(
                            response.get("partial")
                            or response.get("truncated")
                            or local_truncation
                            or rejections
                        ),
                        "error": None,
                    }
                )
            except UpworkAPIError as error:
                # A provider call was attempted even if no HTTP response arrived.
                request_units += 1
                terminal_error = error
                query_results.append(
                    {
                        "query": query,
                        "response": error.as_dict(),
                        "returned_count": 0,
                        "jobs": [],
                        "rejections": [],
                        "partial_evidence": True,
                        "error": error,
                    }
                )
                # Auth/quota failures make the remaining reserved calls unsafe.
                if error.status_code in {401, 403, 429, 503}:
                    break
        archive_sha256 = self._archive_scan_payload(
            {
                "contract": UPWORK_SCAN_CONTRACT,
                "reservation_id": reservation["request_reservation_id"],
                "observed_at": observed_text,
                "queries": [
                    {"query": result["query"], "response": result["response"]}
                    for result in query_results
                ],
            }
        )
        return self._persist_scan(
            reservation=reservation,
            queries=normalized_queries,
            query_results=query_results,
            observed_at=observed_text,
            started_at=started_at,
            finished_at=str(isoformat(self._clock())),
            request_units=request_units,
            raw_archive_sha256=archive_sha256,
            terminal_error=terminal_error,
        )

    def _normalize_queries(self, queries: Sequence[str] | None) -> list[str]:
        configured = getattr(
            self.config,
            "upwork_default_queries",
            ["ai automation", "openai", "ai agents", "freelance automation"],
        )
        values = configured if queries is None else queries
        result: list[str] = []
        seen: set[str] = set()
        for value in values:
            query = " ".join(str(value or "").strip().split())
            normalized = query.lower()
            if query and normalized not in seen:
                seen.add(normalized)
                result.append(query)
        if not result:
            raise ValueError("at least one Upwork query is required")
        return result

    def _reserve_requests(
        self,
        queries: Sequence[str],
        reserved_at: str,
    ) -> dict[str, Any]:
        request_units = len(queries)
        usage_date = parse_datetime(reserved_at).date().isoformat()  # type: ignore[union-attr]
        query_set_sha256 = stable_hash([_normalized_text(query) for query in queries])
        request_reservation_id = f"upwork-reservation:{uuid4()}"
        core = {
            "contract": UPWORK_REQUEST_RESERVATION_CONTRACT,
            "request_reservation_id": request_reservation_id,
            "reserved_at": reserved_at,
            "usage_date": usage_date,
            "request_units": request_units,
            "query_set_sha256": query_set_sha256,
        }
        core["reservation_sha256"] = stable_hash(core)
        daily_limit = int(getattr(self.config, "upwork_daily_request_limit", 10))
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            used = int(
                connection.execute(
                    """SELECT COALESCE(SUM(request_units), 0)
                       FROM mt_upwork_request_reservations WHERE usage_date = ?""",
                    (usage_date,),
                ).fetchone()[0]
            )
            if used + request_units > daily_limit:
                raise UpworkAPIError(
                    "daily Upwork RapidAPI request ceiling reached",
                    code="request_budget_exhausted",
                    status_code=429,
                )
            connection.execute(
                """INSERT INTO mt_upwork_request_reservations(
                       request_reservation_id, contract, reserved_at, usage_date,
                       request_units, query_set_sha256, reservation_sha256
                   ) VALUES(?, ?, ?, ?, ?, ?, ?)""",
                (
                    core["request_reservation_id"],
                    core["contract"],
                    core["reserved_at"],
                    core["usage_date"],
                    core["request_units"],
                    core["query_set_sha256"],
                    core["reservation_sha256"],
                ),
            )
            _enqueue_row(
                connection,
                "mt_upwork_request_reservations",
                "request_reservation_id",
                "upwork_request_reservation",
                request_reservation_id,
            )
        return core

    def _archive_scan_payload(self, payload: Mapping[str, Any]) -> str:
        encoded = _canonical_json(payload).encode("utf-8")
        digest = stable_hash(payload)
        directory = Path(self.config.object_dir) / "upwork" / digest[:2]
        directory.mkdir(parents=True, exist_ok=True)
        destination = directory / f"{digest}.json.gz"
        if not destination.exists():
            temporary = directory / f".{digest}.{uuid4().hex}.tmp"
            with temporary.open("wb") as raw_stream:
                with gzip.GzipFile(
                    filename="", mode="wb", fileobj=raw_stream, mtime=0
                ) as stream:
                    stream.write(encoded)
            os.replace(temporary, destination)
        return digest

    def _persist_scan(
        self,
        *,
        reservation: Mapping[str, Any],
        queries: Sequence[str],
        query_results: Sequence[Mapping[str, Any]],
        observed_at: str,
        started_at: str,
        finished_at: str,
        request_units: int,
        raw_archive_sha256: str,
        terminal_error: UpworkAPIError | None,
    ) -> dict[str, Any]:
        accepted_count = sum(len(result["jobs"]) for result in query_results)
        rejected_count = sum(len(result["rejections"]) for result in query_results)
        successful_queries = sum(1 for result in query_results if result["error"] is None)
        partial = len(query_results) != len(queries) or any(
            bool(result["partial_evidence"]) for result in query_results
        )
        state = (
            "failed"
            if successful_queries == 0 and terminal_error is not None
            else "partial"
            if partial
            else "complete"
        )
        scan_core = {
            "contract": UPWORK_SCAN_CONTRACT,
            "request_reservation_id": reservation["request_reservation_id"],
            "started_at": started_at,
            "finished_at": finished_at,
            "observed_at": observed_at,
            "query_count": len(queries),
            "request_units": request_units,
            "accepted_job_count": accepted_count,
            "rejected_job_count": rejected_count,
            "state": state,
            "raw_archive_sha256": raw_archive_sha256,
            "error_code": terminal_error.code if terminal_error else "",
            "error_detail": str(terminal_error)[:1000] if terminal_error else "",
        }
        scan_core["scan_sha256"] = stable_hash(scan_core)
        scan_run_id = _id("upwork-scan", scan_core)
        inserted_job_ids: set[str] = set()
        query_observation_ids: list[str] = []
        job_observation_ids: list[str] = []
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            self._register_raw_archive(
                connection,
                raw_archive_sha256,
                first_seen_at=observed_at,
            )
            connection.execute(
                """INSERT INTO mt_upwork_scan_runs(
                       scan_run_id, contract, request_reservation_id, started_at,
                       finished_at, observed_at, query_count, request_units,
                       accepted_job_count, rejected_job_count, state,
                       raw_archive_sha256, error_code, error_detail, scan_sha256
                   ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    scan_run_id,
                    scan_core["contract"],
                    scan_core["request_reservation_id"],
                    scan_core["started_at"],
                    scan_core["finished_at"],
                    scan_core["observed_at"],
                    scan_core["query_count"],
                    scan_core["request_units"],
                    scan_core["accepted_job_count"],
                    scan_core["rejected_job_count"],
                    scan_core["state"],
                    scan_core["raw_archive_sha256"],
                    scan_core["error_code"],
                    scan_core["error_detail"],
                    scan_core["scan_sha256"],
                ),
            )
            _enqueue_row(
                connection,
                "mt_upwork_scan_runs",
                "scan_run_id",
                "upwork_scan_run",
                scan_run_id,
            )
            for query_result in query_results:
                query_text = str(query_result["query"])
                normalized_query = _normalized_text(query_text)
                query_core = {
                    "contract": UPWORK_QUERY_OBSERVATION_CONTRACT,
                    "scan_run_id": scan_run_id,
                    "query_text": query_text,
                    "normalized_query": normalized_query,
                    "observed_at": observed_at,
                    "returned_count": int(query_result["returned_count"]),
                    "accepted_count": len(query_result["jobs"]),
                    "rejected_count": len(query_result["rejections"]),
                    "partial_evidence": int(bool(query_result["partial_evidence"])),
                    "response_sha256": stable_hash(query_result["response"]),
                }
                query_core["observation_sha256"] = stable_hash(query_core)
                query_observation_id = _id("upwork-query-observation", query_core)
                query_observation_ids.append(query_observation_id)
                connection.execute(
                    """INSERT INTO mt_upwork_query_observations(
                           query_observation_id, contract, scan_run_id,
                           query_text, normalized_query, observed_at,
                           returned_count, accepted_count, rejected_count,
                           partial_evidence, response_sha256, observation_sha256
                       ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        query_observation_id,
                        query_core["contract"],
                        query_core["scan_run_id"],
                        query_core["query_text"],
                        query_core["normalized_query"],
                        query_core["observed_at"],
                        query_core["returned_count"],
                        query_core["accepted_count"],
                        query_core["rejected_count"],
                        query_core["partial_evidence"],
                        query_core["response_sha256"],
                        query_core["observation_sha256"],
                    ),
                )
                _enqueue_row(
                    connection,
                    "mt_upwork_query_observations",
                    "query_observation_id",
                    "upwork_query_observation",
                    query_observation_id,
                )
                for position, job in enumerate(query_result["jobs"]):
                    existing = connection.execute(
                        "SELECT * FROM mt_upwork_jobs WHERE job_id = ?",
                        (job["job_id"],),
                    ).fetchone()
                    should_insert_job = existing is None
                    is_new = should_insert_job or str(job["job_id"]) in inserted_job_ids
                    if existing is not None and (
                        existing["identity_sha256"] != job["identity_sha256"]
                        or existing["canonical_url"] != job["canonical_url"]
                    ):
                        raise ValueError(
                            f"provider identity collision for {job['job_id']}"
                        )
                    if should_insert_job:
                        connection.execute(
                            """INSERT INTO mt_upwork_jobs(
                                   job_id, contract, provider_job_id,
                                   canonical_url, first_seen_at, identity_sha256
                               ) VALUES(?, ?, ?, ?, ?, ?)""",
                            (
                                job["job_id"],
                                UPWORK_JOB_CONTRACT,
                                job["provider_job_id"],
                                job["canonical_url"],
                                observed_at,
                                job["identity_sha256"],
                            ),
                        )
                        inserted_job_ids.add(str(job["job_id"]))
                        _enqueue_row(
                            connection,
                            "mt_upwork_jobs",
                            "job_id",
                            "upwork_job",
                            str(job["job_id"]),
                        )
                    version_inserted = connection.execute(
                        """INSERT INTO mt_upwork_job_versions(
                               job_version_id, contract, job_id, observed_at,
                               title, description, published_at, client_id,
                               budget_type, budget_amount, budget_currency,
                               hourly_min, hourly_max, proposal_count,
                               experience_level, country, skills_json, category,
                               request_intent, raw_archive_sha256, payload_sha256,
                               version_sha256
                           ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                           ON CONFLICT(job_version_id) DO NOTHING""",
                        (
                            job["job_version_id"],
                            UPWORK_JOB_VERSION_CONTRACT,
                            job["job_id"],
                            observed_at,
                            job["title"],
                            job["description"],
                            job["published_at"],
                            job["client_id"],
                            job["budget_type"],
                            job["budget_amount"],
                            job["budget_currency"],
                            job["hourly_min"],
                            job["hourly_max"],
                            job["proposal_count"],
                            job["experience_level"],
                            job["country"],
                            _canonical_json(job["skills"]),
                            job["category"],
                            job["request_intent"],
                            raw_archive_sha256,
                            job["payload_sha256"],
                            job["version_sha256"],
                        ),
                    ).rowcount
                    if version_inserted:
                        _enqueue_row(
                            connection,
                            "mt_upwork_job_versions",
                            "job_version_id",
                            "upwork_job_version",
                            str(job["job_version_id"]),
                        )
                    observation_core = {
                        "contract": UPWORK_JOB_OBSERVATION_CONTRACT,
                        "scan_run_id": scan_run_id,
                        "query_observation_id": query_observation_id,
                        "job_id": job["job_id"],
                        "job_version_id": job["job_version_id"],
                        "observed_at": observed_at,
                        "is_new_job": int(is_new),
                        "result_position": position,
                    }
                    observation_core["observation_sha256"] = stable_hash(
                        observation_core
                    )
                    job_observation_id = _id(
                        "upwork-job-observation", observation_core
                    )
                    connection.execute(
                        """INSERT INTO mt_upwork_job_observations(
                               job_observation_id, contract, scan_run_id,
                               query_observation_id, job_id, job_version_id,
                               observed_at, is_new_job, result_position,
                               observation_sha256
                           ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                        (
                            job_observation_id,
                            observation_core["contract"],
                            observation_core["scan_run_id"],
                            observation_core["query_observation_id"],
                            observation_core["job_id"],
                            observation_core["job_version_id"],
                            observation_core["observed_at"],
                            observation_core["is_new_job"],
                            observation_core["result_position"],
                            observation_core["observation_sha256"],
                        ),
                    )
                    job_observation_ids.append(job_observation_id)
                    _enqueue_row(
                        connection,
                        "mt_upwork_job_observations",
                        "job_observation_id",
                        "upwork_job_observation",
                        job_observation_id,
                    )
            snapshot_ids = self._materialize_snapshots(
                connection,
                scan_run_id=scan_run_id,
                observed_at=observed_at,
                scan_partial=partial,
            )
        return {
            "contract": UPWORK_SCAN_CONTRACT,
            "scan_run_id": scan_run_id,
            "request_reservation_id": reservation["request_reservation_id"],
            "state": state,
            "observed_at": observed_at,
            "queries_requested": len(queries),
            "queries_attempted": len(query_results),
            "request_units_reserved": int(reservation["request_units"]),
            "request_units_executed": request_units,
            "accepted_job_observations": accepted_count,
            "unique_jobs_inserted": len(inserted_job_ids),
            "rejected_jobs": rejected_count,
            "query_observation_ids": query_observation_ids,
            "job_observation_ids": job_observation_ids,
            "demand_snapshot_ids": snapshot_ids,
            "raw_archive_sha256": raw_archive_sha256,
            "error": terminal_error.as_dict()["error"] if terminal_error else None,
        }

    def _register_raw_archive(
        self,
        connection: sqlite3.Connection,
        raw_sha256: str,
        *,
        first_seen_at: str,
    ) -> None:
        """Register the archive for the existing Passport raw-object mirror."""

        if not _table_exists(connection, "mt_raw_objects"):
            return
        relative = Path("upwork") / raw_sha256[:2] / f"{raw_sha256}.json.gz"
        destination = Path(self.config.object_dir) / relative
        connection.execute(
            """INSERT INTO mt_raw_objects(
                   raw_sha256, object_path, bytes_compressed,
                   first_seen_at, source_id
               ) VALUES(?, ?, ?, ?, ?)
               ON CONFLICT(raw_sha256) DO NOTHING""",
            (
                raw_sha256,
                relative.as_posix(),
                destination.stat().st_size,
                first_seen_at,
                "rapidapi_upwork",
            ),
        )

    def _materialize_snapshots(
        self,
        connection: sqlite3.Connection,
        *,
        scan_run_id: str,
        observed_at: str,
        scan_partial: bool,
    ) -> list[str]:
        rows = connection.execute(
            """SELECT observation.job_observation_id,
                      observation.query_observation_id,
                      observation.job_id, observation.is_new_job,
                      query.normalized_query, query.partial_evidence,
                      version.client_id, version.budget_type,
                      version.budget_amount, version.budget_currency,
                      version.hourly_min, version.hourly_max,
                      version.proposal_count, version.category,
                      version.request_intent, version.skills_json
               FROM mt_upwork_job_observations observation
               JOIN mt_upwork_query_observations query
                 ON query.query_observation_id = observation.query_observation_id
               JOIN mt_upwork_job_versions version
                 ON version.job_version_id = observation.job_version_id
               WHERE observation.scan_run_id = ?""",
            (scan_run_id,),
        ).fetchall()
        cohorts: dict[tuple[str, str], dict[str, dict[str, Any]]] = defaultdict(dict)
        for row in rows:
            item = dict(row)
            keys: list[tuple[str, str]] = [
                ("query", str(row["normalized_query"])),
                ("category", str(row["category"])),
                ("intent", str(row["request_intent"])),
            ]
            keys.extend(
                ("skill", str(skill))
                for skill in json.loads(str(row["skills_json"]))
            )
            for key in keys:
                current = cohorts[key].get(str(row["job_id"]))
                if current is None:
                    cohorts[key][str(row["job_id"])] = item
                else:
                    current["is_new_job"] = max(
                        int(current["is_new_job"]), int(row["is_new_job"])
                    )
                    current["partial_evidence"] = max(
                        int(current["partial_evidence"]),
                        int(row["partial_evidence"]),
                    )
        # Empty query cohorts remain important evidence of zero returned demand.
        query_rows = connection.execute(
            """SELECT normalized_query, partial_evidence
               FROM mt_upwork_query_observations WHERE scan_run_id = ?""",
            (scan_run_id,),
        ).fetchall()
        for row in query_rows:
            cohorts.setdefault(("query", str(row["normalized_query"])), {})
        snapshot_ids: list[str] = []
        for (cohort_type, cohort_key), jobs in sorted(cohorts.items()):
            items = list(jobs.values())
            unique_jobs = len(items)
            new_jobs = sum(int(item["is_new_job"]) for item in items)
            clients = {str(item["client_id"]) for item in items if item["client_id"]}
            fixed_budgets_usd: list[float] = []
            hourly_rates_usd: list[float] = []
            proposals: list[int] = []
            observation_ids: list[str] = []
            cohort_partial = bool(scan_partial)
            for item in items:
                observation_ids.append(str(item["job_observation_id"]))
                is_usd = item["budget_currency"] == "USD"
                budget_type = _normalized_text(item["budget_type"])
                is_fixed = "fixed" in budget_type or (
                    not budget_type
                    and item["budget_amount"] is not None
                    and item["hourly_min"] is None
                    and item["hourly_max"] is None
                )
                if is_usd and is_fixed and item["budget_amount"] is not None:
                    fixed_budgets_usd.append(float(item["budget_amount"]))
                is_hourly = "hour" in budget_type or (
                    not budget_type and item["hourly_min"] is not None
                )
                if is_usd and is_hourly and item["hourly_min"] is not None:
                    maximum = (
                        item["hourly_max"]
                        if item["hourly_max"] is not None
                        else item["hourly_min"]
                    )
                    hourly_rates_usd.append(
                        (float(item["hourly_min"]) + float(maximum)) / 2
                    )
                if item["proposal_count"] is not None:
                    proposals.append(int(item["proposal_count"]))
                cohort_partial = cohort_partial or bool(item["partial_evidence"])
            previous = connection.execute(
                """SELECT * FROM mt_upwork_demand_snapshots
                   WHERE cohort_type = ? AND cohort_key = ? AND observed_at < ?
                   ORDER BY observed_at DESC, demand_snapshot_id DESC LIMIT 1""",
                (cohort_type, cohort_key, observed_at),
            ).fetchone()
            velocity = 0.0
            acceleration = 0.0
            if previous is not None:
                elapsed = max(
                    1 / 3600,
                    (
                        parse_datetime(observed_at) - parse_datetime(previous["observed_at"])
                    ).total_seconds()
                    / 3600,
                )
                # Demand velocity is the arrival rate of newly observed jobs,
                # not movement in a bounded top-N result-set size.
                velocity = new_jobs / elapsed
                acceleration = (velocity - float(previous["velocity"])) / elapsed
            evidence_state = (
                "insufficient"
                if unique_jobs == 0
                else "partial"
                if cohort_partial
                else "complete"
            )
            evidence = {
                "scan_run_id": scan_run_id,
                "cohort_type": cohort_type,
                "cohort_key": cohort_key,
                "observation_ids": sorted(observation_ids),
                "aggregate_only": True,
            }
            snapshot_core = {
                "contract": UPWORK_DEMAND_SNAPSHOT_CONTRACT,
                "scan_run_id": scan_run_id,
                "cohort_type": cohort_type,
                "cohort_key": cohort_key,
                "observed_at": observed_at,
                "unique_jobs": unique_jobs,
                "new_jobs": new_jobs,
                "unique_clients": len(clients),
                "fixed_budget_usd_coverage": (
                    len(fixed_budgets_usd) / unique_jobs if unique_jobs else 0.0
                ),
                "median_fixed_budget_usd": _median(fixed_budgets_usd),
                "hourly_rate_usd_coverage": (
                    len(hourly_rates_usd) / unique_jobs if unique_jobs else 0.0
                ),
                "median_hourly_rate_usd": _median(hourly_rates_usd),
                "proposal_coverage": len(proposals) / unique_jobs if unique_jobs else 0.0,
                "median_proposals": _median(proposals),
                "velocity": velocity,
                "acceleration": acceleration,
                "evidence_state": evidence_state,
                "partial_evidence": int(cohort_partial),
                "evidence_sha256": stable_hash(evidence),
            }
            snapshot_core["snapshot_sha256"] = stable_hash(snapshot_core)
            snapshot_id = _id("upwork-demand-snapshot", snapshot_core)
            connection.execute(
                """INSERT INTO mt_upwork_demand_snapshots(
                       demand_snapshot_id, contract, scan_run_id, cohort_type,
                       cohort_key, observed_at, unique_jobs, new_jobs,
                       unique_clients, fixed_budget_usd_coverage,
                       median_fixed_budget_usd, hourly_rate_usd_coverage,
                       median_hourly_rate_usd,
                       proposal_coverage, median_proposals, velocity,
                       acceleration, evidence_state, partial_evidence,
                       evidence_sha256, snapshot_sha256
                   ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    snapshot_id,
                    snapshot_core["contract"],
                    snapshot_core["scan_run_id"],
                    snapshot_core["cohort_type"],
                    snapshot_core["cohort_key"],
                    snapshot_core["observed_at"],
                    snapshot_core["unique_jobs"],
                    snapshot_core["new_jobs"],
                    snapshot_core["unique_clients"],
                    snapshot_core["fixed_budget_usd_coverage"],
                    snapshot_core["median_fixed_budget_usd"],
                    snapshot_core["hourly_rate_usd_coverage"],
                    snapshot_core["median_hourly_rate_usd"],
                    snapshot_core["proposal_coverage"],
                    snapshot_core["median_proposals"],
                    snapshot_core["velocity"],
                    snapshot_core["acceleration"],
                    snapshot_core["evidence_state"],
                    snapshot_core["partial_evidence"],
                    snapshot_core["evidence_sha256"],
                    snapshot_core["snapshot_sha256"],
                ),
            )
            snapshot_ids.append(snapshot_id)
            _enqueue_row(
                connection,
                "mt_upwork_demand_snapshots",
                "demand_snapshot_id",
                "upwork_demand_snapshot",
                snapshot_id,
            )
            self._evaluate_predictions(connection, snapshot_id)
            self._insert_prediction(connection, snapshot_id)
        return snapshot_ids

    def _evaluate_predictions(
        self,
        connection: sqlite3.Connection,
        observed_snapshot_id: str,
    ) -> None:
        current = connection.execute(
            "SELECT * FROM mt_upwork_demand_snapshots WHERE demand_snapshot_id = ?",
            (observed_snapshot_id,),
        ).fetchone()
        if current is None:
            return
        if bool(current["partial_evidence"]) or current["evidence_state"] != "complete":
            # Outcomes require a complete later observation. A partial/empty
            # snapshot cannot become ground truth for an earlier prediction.
            return
        pending = connection.execute(
            """SELECT prediction.*, source.velocity AS source_velocity
               FROM mt_upwork_predictions prediction
               JOIN mt_upwork_demand_snapshots source
                 ON source.demand_snapshot_id = prediction.demand_snapshot_id
               LEFT JOIN mt_upwork_prediction_outcomes outcome
                 ON outcome.prediction_id = prediction.prediction_id
               WHERE prediction.cohort_type = ? AND prediction.cohort_key = ?
                 AND prediction.as_of < ? AND outcome.prediction_id IS NULL
               ORDER BY prediction.as_of ASC, prediction.prediction_id ASC""",
            (current["cohort_type"], current["cohort_key"], current["observed_at"]),
        ).fetchall()
        for prediction in pending:
            change = float(current["velocity"]) - float(prediction["source_velocity"])
            actual_direction = "rising" if change > 0 else "falling" if change < 0 else "flat"
            predicted = str(prediction["direction"])
            confidence = float(prediction["confidence"])
            if predicted == "abstain":
                directional_correct: int | None = None
                brier_score: float | None = None
            else:
                directional_correct = int(predicted == actual_direction)
                probability_rising = (
                    0.5 + confidence / 2
                    if predicted == "rising"
                    else 0.5 - confidence / 2
                    if predicted == "falling"
                    else 0.5
                )
                actual_rising = 1.0 if actual_direction == "rising" else 0.0
                brier_score = (probability_rising - actual_rising) ** 2
            core = {
                "contract": UPWORK_PREDICTION_OUTCOME_CONTRACT,
                "prediction_id": prediction["prediction_id"],
                "observed_snapshot_id": observed_snapshot_id,
                "evaluated_at": current["observed_at"],
                "actual_direction": actual_direction,
                "directional_correct": directional_correct,
                "brier_score": brier_score,
            }
            core["outcome_sha256"] = stable_hash(core)
            outcome_id = _id("upwork-prediction-outcome", core)
            connection.execute(
                """INSERT INTO mt_upwork_prediction_outcomes(
                       prediction_outcome_id, contract, prediction_id,
                       observed_snapshot_id, evaluated_at, actual_direction,
                       directional_correct, brier_score, outcome_sha256
                   ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    outcome_id,
                    core["contract"],
                    core["prediction_id"],
                    core["observed_snapshot_id"],
                    core["evaluated_at"],
                    core["actual_direction"],
                    core["directional_correct"],
                    core["brier_score"],
                    core["outcome_sha256"],
                ),
            )
            _enqueue_row(
                connection,
                "mt_upwork_prediction_outcomes",
                "prediction_outcome_id",
                "upwork_prediction_outcome",
                outcome_id,
            )

    def _insert_prediction(
        self,
        connection: sqlite3.Connection,
        demand_snapshot_id: str,
    ) -> str:
        snapshot = connection.execute(
            "SELECT * FROM mt_upwork_demand_snapshots WHERE demand_snapshot_id = ?",
            (demand_snapshot_id,),
        ).fetchone()
        if snapshot is None:
            raise ValueError("demand snapshot was not found")
        history = connection.execute(
            """SELECT * FROM mt_upwork_demand_snapshots
               WHERE cohort_type = ? AND cohort_key = ? AND observed_at <= ?
               ORDER BY observed_at ASC, demand_snapshot_id ASC""",
            (snapshot["cohort_type"], snapshot["cohort_key"], snapshot["observed_at"]),
        ).fetchall()
        minimum = max(3, int(getattr(self.config, "upwork_prediction_min_snapshots", 3)))
        current_partial = bool(snapshot["partial_evidence"])
        complete_history = [
            row
            for row in history
            if not bool(row["partial_evidence"])
            and row["evidence_state"] == "complete"
        ]
        if current_partial or len(complete_history) < minimum:
            direction = "abstain"
            confidence = 0.0
        else:
            recent = complete_history[-max(minimum, 5) :]
            change = float(recent[-1]["velocity"]) - float(recent[0]["velocity"])
            direction = "rising" if change > 0 else "falling" if change < 0 else "flat"
            scale = max(1.0, max(abs(float(row["velocity"])) for row in recent))
            confidence = min(1.0, abs(change) / scale)
            if direction == "flat":
                confidence = min(1.0, len(recent) / max(minimum, 5))
        history_ids = [
            str(row["demand_snapshot_id"]) for row in complete_history
        ]
        input_record = {
            "cohort_type": snapshot["cohort_type"],
            "cohort_key": snapshot["cohort_key"],
            "as_of": snapshot["observed_at"],
            "history_snapshot_ids": history_ids,
            "arrival_velocity_values": [
                float(row["velocity"]) for row in complete_history
            ],
            "current_partial_evidence": current_partial,
            "excluded_partial_snapshot_ids": [
                str(row["demand_snapshot_id"])
                for row in history
                if bool(row["partial_evidence"])
            ],
        }
        core = {
            "contract": UPWORK_PREDICTION_CONTRACT,
            "demand_snapshot_id": demand_snapshot_id,
            "cohort_type": snapshot["cohort_type"],
            "cohort_key": snapshot["cohort_key"],
            "as_of": snapshot["observed_at"],
            "direction": direction,
            "confidence": confidence,
            "model_version": UPWORK_PREDICTION_MODEL_VERSION,
            "history_snapshot_ids_json": _canonical_json(history_ids),
            "input_sha256": stable_hash(input_record),
        }
        core["prediction_sha256"] = stable_hash(core)
        prediction_id = _id("upwork-prediction", core)
        connection.execute(
            """INSERT INTO mt_upwork_predictions(
                   prediction_id, contract, demand_snapshot_id, cohort_type,
                   cohort_key, as_of, direction, confidence, model_version,
                   history_snapshot_ids_json, input_sha256, prediction_sha256
               ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                prediction_id,
                core["contract"],
                core["demand_snapshot_id"],
                core["cohort_type"],
                core["cohort_key"],
                core["as_of"],
                core["direction"],
                core["confidence"],
                core["model_version"],
                core["history_snapshot_ids_json"],
                core["input_sha256"],
                core["prediction_sha256"],
            ),
        )
        _enqueue_row(
            connection,
            "mt_upwork_predictions",
            "prediction_id",
            "upwork_prediction",
            prediction_id,
        )
        return prediction_id

    def list_jobs(
        self,
        *,
        limit: int = 100,
        query: str | None = None,
    ) -> dict[str, Any]:
        """List deduplicated jobs without exposing stored descriptions/raw payloads."""

        bounded_limit = max(1, min(int(limit), 500))
        normalized_query = _normalized_text(query)
        parameters: list[Any] = []
        query_filter = ""
        if normalized_query:
            query_filter = """AND EXISTS (
                SELECT 1 FROM mt_upwork_job_observations observation
                JOIN mt_upwork_query_observations query_observation
                  ON query_observation.query_observation_id =
                     observation.query_observation_id
                WHERE observation.job_id = job.job_id
                  AND query_observation.normalized_query = ?
            )"""
            parameters.append(normalized_query)
        parameters.append(bounded_limit)
        with self._connect() as connection:
            rows = connection.execute(
                f"""SELECT job.job_id, job.provider_job_id, job.canonical_url,
                           job.first_seen_at, version.job_version_id,
                           version.observed_at, version.title,
                           version.published_at, version.client_id,
                           version.budget_type, version.budget_amount,
                           version.budget_currency, version.hourly_min,
                           version.hourly_max, version.proposal_count,
                           version.experience_level, version.country,
                           version.skills_json, version.category,
                           version.request_intent
                    FROM mt_upwork_jobs job
                    JOIN mt_upwork_job_versions version
                      ON version.job_version_id = (
                          SELECT candidate.job_version_id
                          FROM mt_upwork_job_versions candidate
                          WHERE candidate.job_id = job.job_id
                          ORDER BY candidate.observed_at DESC,
                                   candidate.job_version_id DESC LIMIT 1
                      )
                    WHERE 1 = 1 {query_filter}
                    ORDER BY version.observed_at DESC, job.job_id
                    LIMIT ?""",
                parameters,
            ).fetchall()
        jobs: list[dict[str, Any]] = []
        for row in rows:
            job = dict(row)
            job["skills"] = json.loads(job.pop("skills_json"))
            jobs.append(job)
        return {
            "contract": "market_tape_upwork_job_list_v1",
            "query": normalized_query or None,
            "count": len(jobs),
            "jobs": jobs,
            "description_included": False,
            "raw_payload_included": False,
        }

    def demand_report(
        self,
        *,
        cohort_type: str | None = None,
        cohort_key: str | None = None,
        limit: int = 100,
    ) -> dict[str, Any]:
        """Return latest aggregate demand snapshots and their forecasts."""

        if cohort_type not in (None, "query", "category", "skill", "intent"):
            raise ValueError("invalid cohort_type")
        bounded_limit = max(1, min(int(limit), 500))
        clauses: list[str] = []
        parameters: list[Any] = []
        if cohort_type:
            clauses.append("snapshot.cohort_type = ?")
            parameters.append(cohort_type)
        if cohort_key:
            clauses.append("snapshot.cohort_key = ?")
            parameters.append(_normalized_text(cohort_key))
        where = " AND ".join(clauses) or "1 = 1"
        parameters.append(bounded_limit)
        with self._connect() as connection:
            rows = connection.execute(
                f"""SELECT snapshot.*, prediction.direction,
                           prediction.confidence,
                           prediction.model_version,
                           prediction.as_of AS prediction_as_of
                    FROM mt_upwork_demand_snapshots snapshot
                    JOIN mt_upwork_predictions prediction
                      ON prediction.demand_snapshot_id = snapshot.demand_snapshot_id
                    WHERE {where}
                    ORDER BY snapshot.observed_at DESC,
                             snapshot.cohort_type, snapshot.cohort_key
                    LIMIT ?""",
                parameters,
            ).fetchall()
        snapshots = [self._cohort_projection(dict(row)) for row in rows]
        return {
            "contract": "market_tape_upwork_demand_report_v1",
            "generated_at": str(isoformat(utc_now())),
            "count": len(snapshots),
            "cohorts": snapshots,
            "prediction_model": UPWORK_PREDICTION_MODEL_VERSION,
            "separate_from_social_trend_predictor": True,
        }

    def backtest_report(
        self,
        *,
        cohort_type: str | None = None,
        cohort_key: str | None = None,
    ) -> dict[str, Any]:
        """Return outcomes produced strictly from later snapshots."""

        if cohort_type not in (None, "query", "category", "skill", "intent"):
            raise ValueError("invalid cohort_type")
        clauses = ["outcome.prediction_id IS NOT NULL"]
        parameters: list[Any] = []
        if cohort_type:
            clauses.append("prediction.cohort_type = ?")
            parameters.append(cohort_type)
        if cohort_key:
            clauses.append("prediction.cohort_key = ?")
            parameters.append(_normalized_text(cohort_key))
        with self._connect() as connection:
            rows = connection.execute(
                f"""SELECT prediction.cohort_type, prediction.cohort_key,
                           prediction.direction, prediction.confidence,
                           outcome.actual_direction,
                           outcome.directional_correct, outcome.brier_score,
                           outcome.evaluated_at
                    FROM mt_upwork_prediction_outcomes outcome
                    JOIN mt_upwork_predictions prediction
                      ON prediction.prediction_id = outcome.prediction_id
                    WHERE {' AND '.join(clauses)}
                    ORDER BY outcome.evaluated_at DESC""",
                parameters,
            ).fetchall()
        scored = [row for row in rows if row["directional_correct"] is not None]
        brier = [float(row["brier_score"]) for row in rows if row["brier_score"] is not None]
        return {
            "contract": "market_tape_upwork_prediction_backtest_v1",
            "prediction_model": UPWORK_PREDICTION_MODEL_VERSION,
            "outcome_count": len(rows),
            "scored_count": len(scored),
            "directional_accuracy": (
                sum(int(row["directional_correct"]) for row in scored) / len(scored)
                if scored
                else None
            ),
            "mean_brier_score": sum(brier) / len(brier) if brier else None,
            "outcomes": [dict(row) for row in rows],
            "future_leakage": False,
        }

    def materialize_signals(
        self,
        *,
        graph_version_id: str | None = None,
        limit: int = 100,
    ) -> dict[str, Any]:
        """Create review-only semantic candidates from aggregate snapshots."""

        bounded_limit = max(1, min(int(limit), 500))
        created_signal_ids: list[str] = []
        created_link_ids: list[str] = []
        with self._connect() as connection:
            if not _table_exists(connection, "mt_topic_graph_versions"):
                raise ValueError("semantic topic graph is not initialized")
            if graph_version_id is None:
                row = connection.execute(
                    """SELECT graph_version_id FROM mt_topic_graph_versions
                       ORDER BY imported_at DESC, graph_version_id DESC LIMIT 1"""
                ).fetchone()
                if row is None:
                    raise ValueError("no semantic topic graph has been imported")
                graph_version_id = str(row["graph_version_id"])
            elif connection.execute(
                """SELECT 1 FROM mt_topic_graph_versions
                   WHERE graph_version_id = ?""",
                (graph_version_id,),
            ).fetchone() is None:
                raise ValueError("graph_version_id does not exist")
            snapshots = connection.execute(
                """SELECT snapshot.*, prediction.direction,
                          prediction.confidence, prediction.model_version,
                          prediction.as_of AS prediction_as_of,
                          scan.state AS scan_state
                   FROM mt_upwork_demand_snapshots snapshot
                   JOIN mt_upwork_predictions prediction
                     ON prediction.demand_snapshot_id = snapshot.demand_snapshot_id
                   JOIN mt_upwork_scan_runs scan
                     ON scan.scan_run_id = snapshot.scan_run_id
                   LEFT JOIN mt_upwork_semantic_links link
                     ON link.demand_snapshot_id = snapshot.demand_snapshot_id
                    AND link.graph_version_id = ?
                   WHERE link.semantic_link_id IS NULL
                     AND scan.state IN ('complete', 'partial')
                     AND snapshot.evidence_state IN ('complete', 'partial')
                     AND snapshot.unique_jobs > 0
                   ORDER BY snapshot.observed_at DESC,
                            snapshot.demand_snapshot_id DESC LIMIT ?""",
                (graph_version_id, bounded_limit),
            ).fetchall()
            created_at = str(isoformat(utc_now()))
            for snapshot in snapshots:
                evidence = {
                    "contract": UPWORK_SEMANTIC_LINK_CONTRACT,
                    "demand_source": "upwork_rapidapi",
                    "demand_snapshot_id": snapshot["demand_snapshot_id"],
                    "cohort": self._cohort_projection(dict(snapshot)),
                    "audience_evidence_only": True,
                    "automatic_binding": False,
                    "raw_job_text_included": False,
                }
                evidence_sha256 = stable_hash(evidence)
                signal_identity = {
                    "graph_version_id": graph_version_id,
                    "source_entity_id": snapshot["demand_snapshot_id"],
                    "source_observed_at": snapshot["observed_at"],
                    "evidence_sha256": evidence_sha256,
                }
                signal_id = _id("upwork-demand-signal", signal_identity)
                signal_text = f"Upwork demand: {snapshot['cohort_type']} {snapshot['cohort_key']}"
                signal_created = connection.execute(
                    """INSERT INTO mt_topic_signal_candidates(
                           signal_id, graph_version_id, signal_type, source_kind,
                           source_entity_id, source_trend_id, source_observed_at,
                           signal_text, normalized_signal_text,
                           source_receipt_id, evidence_sha256, evidence_json,
                           ingested_at
                       ) VALUES(?, ?, 'topic', 'external_signal', ?, NULL, ?, ?, ?, ?, ?, ?, ?)
                       ON CONFLICT(signal_id) DO NOTHING""",
                    (
                        signal_id,
                        graph_version_id,
                        snapshot["demand_snapshot_id"],
                        snapshot["observed_at"],
                        signal_text,
                        _normalized_text(signal_text),
                        snapshot["scan_run_id"],
                        evidence_sha256,
                        _canonical_json(evidence),
                        created_at,
                    ),
                ).rowcount
                signal_row = connection.execute(
                    "SELECT * FROM mt_topic_signal_candidates WHERE signal_id = ?",
                    (signal_id,),
                ).fetchone()
                if signal_row is None:
                    raise RuntimeError("Upwork semantic signal was not durable")
                _enqueue_outbox(
                    connection,
                    "semantic_signal_candidate",
                    signal_id,
                    dict(signal_row),
                )
                link_core = {
                    "contract": UPWORK_SEMANTIC_LINK_CONTRACT,
                    "demand_snapshot_id": snapshot["demand_snapshot_id"],
                    "signal_id": signal_id,
                    "graph_version_id": graph_version_id,
                    "cohort_type": snapshot["cohort_type"],
                    "cohort_key": snapshot["cohort_key"],
                    "created_at": created_at,
                    "automatic_binding": 0,
                }
                link_core["link_sha256"] = stable_hash(link_core)
                semantic_link_id = _id("upwork-semantic-link", link_core)
                link_created = connection.execute(
                    """INSERT INTO mt_upwork_semantic_links(
                           semantic_link_id, contract, demand_snapshot_id,
                           signal_id, graph_version_id, cohort_type, cohort_key,
                           created_at, automatic_binding, link_sha256
                       ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                       ON CONFLICT(semantic_link_id) DO NOTHING""",
                    (
                        semantic_link_id,
                        link_core["contract"],
                        link_core["demand_snapshot_id"],
                        link_core["signal_id"],
                        link_core["graph_version_id"],
                        link_core["cohort_type"],
                        link_core["cohort_key"],
                        link_core["created_at"],
                        link_core["automatic_binding"],
                        link_core["link_sha256"],
                    ),
                ).rowcount
                _enqueue_row(
                    connection,
                    "mt_upwork_semantic_links",
                    "semantic_link_id",
                    "upwork_semantic_link",
                    semantic_link_id,
                )
                if signal_created:
                    created_signal_ids.append(signal_id)
                if link_created:
                    created_link_ids.append(semantic_link_id)
        return {
            "contract": UPWORK_SEMANTIC_LINK_CONTRACT,
            "graph_version_id": graph_version_id,
            "created_signal_ids": created_signal_ids,
            "created_semantic_link_ids": created_link_ids,
            "created": len(created_link_ids),
            "automatic_binding": False,
            "next_step": "review and approve bindings through the semantic service",
        }

    def script_context(
        self,
        *,
        selection_id: str | None = None,
        limit: int = 20,
    ) -> dict[str, Any]:
        """Return approved, aggregate-only Upwork evidence for script generation.

        The method fails closed: an approved atomic-topic selection must include
        topic-observation sources whose signal IDs resolve through an Upwork
        semantic link to durable demand snapshots.
        """

        bounded_limit = max(1, min(int(limit), 100))
        generated_at = str(isoformat(utc_now()))
        selection: dict[str, Any] | None = None
        cohorts: list[dict[str, Any]] = []
        blockers: list[str] = []
        with self._connect() as connection:
            required_tables = {
                "mt_atomic_topic_selections",
                "mt_atomic_topic_selection_sources",
                "mt_topic_observations",
            }
            if not all(_table_exists(connection, table) for table in required_tables):
                blockers.append("semantic_selection_schema_unavailable")
            else:
                if selection_id:
                    selection_row = connection.execute(
                        """SELECT * FROM mt_atomic_topic_selections
                           WHERE selection_id = ?""",
                        (selection_id,),
                    ).fetchone()
                else:
                    selection_row = connection.execute(
                        """SELECT selection.*
                           FROM mt_atomic_topic_selections selection
                           WHERE EXISTS (
                               SELECT 1
                               FROM mt_atomic_topic_selection_sources source
                               JOIN mt_upwork_semantic_links link
                                 ON link.signal_id = source.signal_id
                                AND link.graph_version_id = selection.graph_version_id
                               WHERE source.selection_id = selection.selection_id
                           )
                           ORDER BY selection.reviewed_at DESC,
                                    selection.selection_id DESC LIMIT 1"""
                    ).fetchone()
                if selection_row is None:
                    blockers.append("approved_upwork_selection_not_found")
                else:
                    reviewed_at = parse_datetime(selection_row["reviewed_at"])
                    if reviewed_at is None:
                        blockers.append("selection_reviewed_at_invalid")
                    else:
                        # This context is content-addressed and is fetched twice:
                        # once by the caller and once by Foundry as the authority
                        # check.  Anchor the timestamp to immutable selection
                        # evidence so two reads of the same state are identical.
                        generated_at = str(isoformat(reviewed_at))
                    source_rows = connection.execute(
                        """SELECT source.topic_observation_key,
                                  source.signal_id,
                                  link.semantic_link_id,
                                  link.demand_snapshot_id,
                                  snapshot.*,
                                  prediction.direction,
                                  prediction.confidence,
                                  prediction.model_version,
                                  prediction.as_of AS prediction_as_of,
                                  scan.state AS scan_state
                           FROM mt_atomic_topic_selection_sources source
                           JOIN mt_upwork_semantic_links link
                             ON link.signal_id = source.signal_id
                            AND link.graph_version_id = ?
                           JOIN mt_upwork_demand_snapshots snapshot
                             ON snapshot.demand_snapshot_id = link.demand_snapshot_id
                           JOIN mt_upwork_scan_runs scan
                             ON scan.scan_run_id = snapshot.scan_run_id
                           JOIN mt_upwork_predictions prediction
                             ON prediction.demand_snapshot_id =
                                snapshot.demand_snapshot_id
                           JOIN mt_topic_observations topic_observation
                             ON topic_observation.topic_observation_key =
                                source.topic_observation_key
                            AND topic_observation.signal_id = source.signal_id
                           WHERE source.selection_id = ?
                           ORDER BY snapshot.observed_at DESC,
                                    link.semantic_link_id DESC,
                                    source.topic_observation_key ASC""",
                        (
                            selection_row["graph_version_id"],
                            selection_row["selection_id"],
                        ),
                    ).fetchall()
                    binding_disposition_rows = connection.execute(
                        """WITH selected_pairs AS (
                               SELECT DISTINCT selected_binding.signal_id,
                                               selected_binding.topic_id
                               FROM mt_atomic_topic_selection_sources source
                               JOIN mt_topic_signal_bindings selected_binding
                                 ON selected_binding.binding_id = source.binding_id
                               WHERE source.selection_id = ?
                           ), ranked AS (
                               SELECT current_binding.*,
                                      ROW_NUMBER() OVER (
                                          PARTITION BY current_binding.signal_id,
                                                       current_binding.topic_id
                                          ORDER BY current_binding.reviewed_at DESC,
                                                   current_binding.binding_id DESC
                                      ) AS row_number
                               FROM mt_topic_signal_bindings current_binding
                               JOIN selected_pairs selected
                                 ON selected.signal_id = current_binding.signal_id
                                AND selected.topic_id = current_binding.topic_id
                           )
                           SELECT binding_id, signal_id, topic_id, decision,
                                  reviewed_at
                           FROM ranked WHERE row_number = 1
                           ORDER BY signal_id, topic_id, binding_id""",
                        (selection_row["selection_id"],),
                    ).fetchall()
                    binding_dispositions = [
                        {
                            "binding_id": str(row["binding_id"]),
                            "signal_id": str(row["signal_id"]),
                            "topic_id": str(row["topic_id"]),
                            "decision": str(row["decision"]),
                            "reviewed_at": str(row["reviewed_at"]),
                        }
                        for row in binding_disposition_rows
                    ]
                    stale_binding_count = sum(
                        disposition["decision"] != "approved"
                        for disposition in binding_dispositions
                    )
                    if not binding_dispositions:
                        blockers.append("selection_binding_dispositions_required")
                    if stale_binding_count:
                        blockers.append("selection_binding_no_longer_approved")
                    eligible_rows = [
                        row
                        for row in source_rows
                        if row["evidence_state"] in {"complete", "partial"}
                        and int(row["unique_jobs"]) > 0
                        and row["scan_state"] in {"complete", "partial"}
                    ]
                    if len(eligible_rows) != len(source_rows):
                        blockers.append("insufficient_demand_evidence")
                        eligible_rows = []
                    if stale_binding_count:
                        eligible_rows = []
                    semantic_link_ids = sorted(
                        {str(row["semantic_link_id"]) for row in eligible_rows}
                    )
                    observation_ids = sorted(
                        {str(row["topic_observation_key"]) for row in eligible_rows}
                    )
                    selection = {
                        "selection_id": str(selection_row["selection_id"]),
                        "review_status": str(selection_row["status"]),
                        "atomic_topic_id": str(selection_row["atomic_topic_id"]),
                        "semantic_link_ids": semantic_link_ids,
                        "observation_ids": observation_ids,
                        "binding_dispositions": binding_dispositions,
                    }
                    unique_snapshot_rows: dict[str, sqlite3.Row] = {}
                    for row in eligible_rows:
                        unique_snapshot_rows.setdefault(
                            str(row["demand_snapshot_id"]), row
                        )
                    evidence_times = [reviewed_at] if reviewed_at is not None else []
                    evidence_timestamps_valid = True
                    for disposition in binding_dispositions:
                        disposition_time = parse_datetime(
                            disposition["reviewed_at"]
                        )
                        if disposition_time is None:
                            evidence_timestamps_valid = False
                        else:
                            evidence_times.append(disposition_time)
                    for row in unique_snapshot_rows.values():
                        for field in ("observed_at", "prediction_as_of"):
                            evidence_time = parse_datetime(row[field])
                            if evidence_time is None:
                                evidence_timestamps_valid = False
                            else:
                                evidence_times.append(evidence_time)
                    if not evidence_timestamps_valid:
                        blockers.append("demand_evidence_timestamp_invalid")
                    if evidence_times:
                        # The authority response is content-addressed and may be
                        # fetched more than once. Derive its timestamp only from
                        # immutable selection/evidence rows while ensuring every
                        # included prediction already existed by generated_at.
                        generated_at = str(isoformat(max(evidence_times)))
                    if evidence_timestamps_valid:
                        cohorts = [
                            self._script_cohort_projection(dict(row))
                            for row in list(unique_snapshot_rows.values())[
                                :bounded_limit
                            ]
                        ]
                    if selection["review_status"] != "approved":
                        blockers.append("selection_not_approved")
                    if not semantic_link_ids:
                        blockers.append("semantic_link_ids_required")
                    if not observation_ids:
                        blockers.append("topic_observation_ids_required")
                    if not cohorts:
                        blockers.append("approved_demand_cohorts_required")
        generation_authorized = not blockers and selection is not None
        core = {
            "contract": UPWORK_SCRIPT_CONTEXT_CONTRACT,
            "demand_source": "upwork_rapidapi",
            "generated_at": generated_at,
            "selection": selection,
            "cohorts": cohorts,
            "policy": {
                "aggregate_only": True,
                "raw_job_text_included": False,
                "automatic_binding": False,
                "claims_require_receipts": True,
            },
            "generation_authorized": generation_authorized,
            "blockers": blockers,
        }
        return {**core, "context_sha256": stable_hash(core)}

    @staticmethod
    def _script_cohort_projection(row: Mapping[str, Any]) -> dict[str, Any]:
        cohort = UpworkDemandService._cohort_projection(row)
        cohort_type = cohort["cohort_type"]
        if cohort_type not in {"query", "category", "skill", "intent"}:
            raise ValueError("script cohort_type is outside the fixed enum")
        if cohort["evidence_state"] not in {"complete", "partial"}:
            raise ValueError("script evidence_state is outside the fixed enum")
        if cohort["prediction"]["direction"] not in {
            "rising",
            "falling",
            "flat",
            "abstain",
        }:
            raise ValueError("script prediction direction is outside the fixed enum")
        if cohort["prediction"]["model"] != UPWORK_PREDICTION_MODEL_VERSION:
            raise ValueError("script prediction model is not the approved model")
        observed = parse_datetime(cohort["observed_at"])
        predicted_as_of = parse_datetime(cohort["prediction"]["as_of"])
        if observed is None or predicted_as_of is None:
            raise ValueError("script cohort timestamps must be ISO-8601")
        cohort["observed_at"] = str(isoformat(observed))
        cohort["prediction"]["as_of"] = str(isoformat(predicted_as_of))
        internal_identity = {
            "cohort_type": cohort_type,
            "cohort_key": _normalized_text(cohort["cohort_key"]),
        }
        cohort["cohort_key"] = (
            f"upwork-cohort:{cohort_type}:{stable_hash(internal_identity)}"
        )
        return cohort

    @staticmethod
    def _cohort_projection(row: Mapping[str, Any]) -> dict[str, Any]:
        """Return the exact aggregate-only contract accepted by Foundry."""

        return {
            "cohort_type": str(row["cohort_type"]),
            "cohort_key": str(row["cohort_key"]),
            "observed_at": str(row["observed_at"]),
            "unique_jobs": int(row["unique_jobs"]),
            "new_jobs": int(row["new_jobs"]),
            "unique_clients": int(row["unique_clients"]),
            "velocity": float(row["velocity"]),
            "acceleration": float(row["acceleration"]),
            "fixed_budget_usd_coverage": float(
                row["fixed_budget_usd_coverage"]
            ),
            "median_fixed_budget_usd": (
                float(row["median_fixed_budget_usd"])
                if row.get("median_fixed_budget_usd") is not None
                else None
            ),
            "hourly_rate_usd_coverage": float(
                row["hourly_rate_usd_coverage"]
            ),
            "median_hourly_rate_usd": (
                float(row["median_hourly_rate_usd"])
                if row.get("median_hourly_rate_usd") is not None
                else None
            ),
            "proposal_coverage": float(row["proposal_coverage"]),
            "median_proposals": (
                float(row["median_proposals"])
                if row.get("median_proposals") is not None
                else None
            ),
            "evidence_state": str(row["evidence_state"]),
            "partial_evidence": bool(row["partial_evidence"]),
            "prediction": {
                "direction": str(row["direction"]),
                "confidence": float(row["confidence"]),
                "model": str(row["model_version"]),
                "as_of": str(row["prediction_as_of"]),
            },
        }
