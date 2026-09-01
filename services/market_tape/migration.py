"""Validated Market Tape schema deployment and remote inspection."""

from __future__ import annotations

import hashlib
import json
import os
import re
import time
from typing import Any, Dict, Optional
from urllib.parse import urlparse

import httpx

from .config import REPO_ROOT, load_runtime_environment
from .sources.base import sanitize


MIGRATION_NAME = "market_tape_v8"
MIGRATION_PATHS = (
    REPO_ROOT / "migrations" / "market_tape_v1.sql",
    REPO_ROOT / "migrations" / "market_tape_v2_discovery_attributions.sql",
    REPO_ROOT / "migrations" / "market_tape_v3_query_attempts.sql",
    REPO_ROOT / "migrations" / "market_tape_v4_trend_activity.sql",
    REPO_ROOT / "migrations" / "market_tape_v5_observation_quality.sql",
    REPO_ROOT / "migrations" / "market_tape_v6_semantic_topics.sql",
    REPO_ROOT / "migrations" / "market_tape_v7_software_repository_changes.sql",
    REPO_ROOT / "migrations" / "market_tape_v8_upwork_demand.sql",
)
MIGRATION_PATH = MIGRATION_PATHS[-1]
VERIFICATION_PATH = REPO_ROOT / "migrations" / "verify_market_tape_v8.sql"
MANAGEMENT_API_URL = "https://api.supabase.com"

# The probe columns are deliberately the conflict keys used by the outbox sink.
MARKET_TAPE_TABLES: Dict[str, str] = {
    "actp_market_creators": "creator_id",
    "actp_market_videos": "video_id",
    "actp_market_discovery_attributions": "attribution_key",
    "actp_market_query_attempts": "attempt_key",
    "actp_market_observations": "observation_key",
    "actp_market_observation_quality_flags": "observation_key",
    "actp_content_genomes": "video_id",
    "actp_trends": "trend_id",
    "actp_trend_memberships": "trend_id,video_id",
    "actp_trend_observations": (
        "trend_observation_key,views_new_1h,likes_new_1h,comments_new_1h,"
        "shares_new_1h,counter_delta_videos,activity_coverage,"
        "observation_quality_contract"
    ),
    "actp_market_collection_runs": "run_id",
    "actp_market_source_receipts": "receipt_key",
    "actp_market_source_health": "source_id",
    "actp_market_predictions": "prediction_key",
    "actp_semantic_topic_graph_versions": "graph_version_id,graph_sha256",
    "actp_semantic_topic_nodes": "graph_version_id,topic_id",
    "actp_semantic_topic_edges": "graph_version_id,edge_id",
    "actp_semantic_signal_candidates": "signal_id",
    "actp_semantic_signal_bindings": "binding_id",
    "actp_semantic_resolution_runs": "resolution_run_id",
    "actp_semantic_topic_observations": "topic_observation_key",
    "actp_semantic_atomic_topic_selections": "selection_id",
    "actp_semantic_atomic_selection_sources": (
        "selection_id,binding_id,topic_observation_key"
    ),
    "actp_semantic_content_evidence_receipts": "receipt_id",
    "actp_semantic_lineage_registrations": "registration_id",
    "actp_semantic_content_briefs": "brief_id",
    "actp_semantic_content_assets": "asset_id",
    "actp_semantic_content_lineage": "lineage_link_id",
    "actp_upwork_request_reservations": "request_reservation_id",
    "actp_upwork_scan_runs": "scan_run_id",
    # ``actp_upwork_jobs`` is owned by the legacy proposal/build workflow and
    # has a different UUID/status-oriented schema. Keep the Market Tape
    # append-only identity ledger in its own table.
    "actp_upwork_market_jobs": "job_id",
    "actp_upwork_job_versions": "job_version_id",
    "actp_upwork_query_observations": "query_observation_id",
    "actp_upwork_job_observations": "job_observation_id",
    "actp_upwork_demand_snapshots": "demand_snapshot_id",
    "actp_upwork_predictions": "prediction_id",
    "actp_upwork_prediction_outcomes": "prediction_outcome_id",
    "actp_upwork_semantic_links": "semantic_link_id",
}

APPEND_ONLY_TABLES = {
    "actp_market_observations",
    "actp_market_observation_quality_flags",
    "actp_market_discovery_attributions",
    "actp_market_query_attempts",
    "actp_trend_observations",
    "actp_semantic_topic_graph_versions",
    "actp_semantic_topic_nodes",
    "actp_semantic_topic_edges",
    "actp_semantic_signal_candidates",
    "actp_semantic_signal_bindings",
    "actp_semantic_resolution_runs",
    "actp_semantic_topic_observations",
    "actp_semantic_atomic_topic_selections",
    "actp_semantic_atomic_selection_sources",
    "actp_semantic_content_evidence_receipts",
    "actp_semantic_lineage_registrations",
    "actp_semantic_content_briefs",
    "actp_semantic_content_assets",
    "actp_semantic_content_lineage",
    "actp_upwork_request_reservations",
    "actp_upwork_scan_runs",
    "actp_upwork_market_jobs",
    "actp_upwork_job_versions",
    "actp_upwork_query_observations",
    "actp_upwork_job_observations",
    "actp_upwork_demand_snapshots",
    "actp_upwork_predictions",
    "actp_upwork_prediction_outcomes",
    "actp_upwork_semantic_links",
}

APPEND_ONLY_TRIGGERS = {
    "actp_market_observations": "actp_market_observations_no_update",
    "actp_market_observation_quality_flags": (
        "actp_market_observation_quality_flags_no_update"
    ),
    "actp_market_discovery_attributions": "actp_market_discovery_attributions_no_update",
    "actp_market_query_attempts": "actp_market_query_attempts_no_update",
    "actp_trend_observations": "actp_trend_observations_no_update",
    "actp_semantic_topic_graph_versions": (
        "actp_semantic_graph_versions_no_update"
    ),
    "actp_semantic_topic_nodes": "actp_semantic_topic_nodes_no_update",
    "actp_semantic_topic_edges": "actp_semantic_topic_edges_no_update",
    "actp_semantic_signal_candidates": (
        "actp_semantic_signal_candidates_no_update"
    ),
    "actp_semantic_signal_bindings": (
        "actp_semantic_signal_bindings_no_update"
    ),
    "actp_semantic_resolution_runs": (
        "actp_semantic_resolution_runs_no_update"
    ),
    "actp_semantic_topic_observations": (
        "actp_semantic_topic_observations_no_update"
    ),
    "actp_semantic_atomic_topic_selections": (
        "actp_semantic_atomic_selections_no_update"
    ),
    "actp_semantic_atomic_selection_sources": (
        "actp_semantic_atomic_sources_no_update"
    ),
    "actp_semantic_content_evidence_receipts": (
        "actp_semantic_evidence_receipts_no_update"
    ),
    "actp_semantic_lineage_registrations": (
        "actp_semantic_lineage_registrations_no_update"
    ),
    "actp_semantic_content_briefs": (
        "actp_semantic_content_briefs_no_update"
    ),
    "actp_semantic_content_assets": (
        "actp_semantic_content_assets_no_update"
    ),
    "actp_semantic_content_lineage": (
        "actp_semantic_content_lineage_no_update"
    ),
    "actp_upwork_request_reservations": (
        "actp_upwork_request_reservations_no_update"
    ),
    "actp_upwork_scan_runs": "actp_upwork_scan_runs_no_update",
    "actp_upwork_market_jobs": "actp_upwork_market_jobs_no_update",
    "actp_upwork_job_versions": "actp_upwork_job_versions_no_update",
    "actp_upwork_query_observations": (
        "actp_upwork_query_observations_no_update"
    ),
    "actp_upwork_job_observations": (
        "actp_upwork_job_observations_no_update"
    ),
    "actp_upwork_demand_snapshots": (
        "actp_upwork_demand_snapshots_no_update"
    ),
    "actp_upwork_predictions": "actp_upwork_predictions_no_update",
    "actp_upwork_prediction_outcomes": (
        "actp_upwork_prediction_outcomes_no_update"
    ),
    "actp_upwork_semantic_links": "actp_upwork_semantic_links_no_update",
}

# Cover the semantic foreign-key/access paths flagged by Supabase's index
# advisor.  These names are deployment invariants checked by verify_database.
REQUIRED_INDEXES: Dict[str, str] = {
    "actp_semantic_atomic_sources_observation_idx": (
        "actp_semantic_atomic_selection_sources"
    ),
    "actp_semantic_atomic_sources_signal_idx": (
        "actp_semantic_atomic_selection_sources"
    ),
    "actp_semantic_assets_graph_atomic_idx": "actp_semantic_content_assets",
    "actp_semantic_assets_parent_idx": "actp_semantic_content_assets",
    "actp_semantic_briefs_selection_idx": "actp_semantic_content_briefs",
    "actp_semantic_briefs_registration_idx": "actp_semantic_content_briefs",
    "actp_semantic_lineage_graph_atomic_idx": "actp_semantic_content_lineage",
    "actp_semantic_lineage_signal_graph_idx": "actp_semantic_content_lineage",
    "actp_semantic_lineage_observation_idx": "actp_semantic_content_lineage",
    "actp_semantic_resolution_graph_selected_idx": (
        "actp_semantic_resolution_runs"
    ),
    "actp_semantic_resolution_signal_graph_idx": (
        "actp_semantic_resolution_runs"
    ),
    "actp_semantic_bindings_signal_graph_idx": "actp_semantic_signal_bindings",
    "actp_semantic_signals_source_trend_idx": "actp_semantic_signal_candidates",
    "actp_semantic_observations_binding_fk_idx": (
        "actp_semantic_topic_observations"
    ),
    "actp_semantic_observations_signal_graph_idx": (
        "actp_semantic_topic_observations"
    ),
    "actp_upwork_reservations_usage_idx": (
        "actp_upwork_request_reservations"
    ),
    "actp_upwork_scans_observed_idx": "actp_upwork_scan_runs",
    "actp_upwork_versions_job_time_idx": "actp_upwork_job_versions",
    "actp_upwork_queries_query_time_idx": (
        "actp_upwork_query_observations"
    ),
    "actp_upwork_query_observations_scan_idx": (
        "actp_upwork_query_observations"
    ),
    "actp_upwork_job_observations_job_time_idx": (
        "actp_upwork_job_observations"
    ),
    "actp_upwork_job_observations_query_idx": (
        "actp_upwork_job_observations"
    ),
    "actp_upwork_job_observations_version_idx": (
        "actp_upwork_job_observations"
    ),
    "actp_upwork_snapshots_cohort_time_idx": (
        "actp_upwork_demand_snapshots"
    ),
    "actp_upwork_predictions_cohort_time_idx": "actp_upwork_predictions",
    "actp_upwork_outcomes_evaluated_idx": (
        "actp_upwork_prediction_outcomes"
    ),
    "actp_upwork_prediction_outcomes_snapshot_idx": (
        "actp_upwork_prediction_outcomes"
    ),
    "actp_upwork_semantic_links_signal_idx": "actp_upwork_semantic_links",
    "actp_upwork_semantic_links_signal_graph_idx": (
        "actp_upwork_semantic_links"
    ),
}


def migration_sql() -> str:
    return "\n\n".join(path.read_text(encoding="utf-8") for path in MIGRATION_PATHS)


def migration_sha256(sql: Optional[str] = None) -> str:
    return hashlib.sha256((sql if sql is not None else migration_sql()).encode("utf-8")).hexdigest()


def verification_sql() -> str:
    return VERIFICATION_PATH.read_text(encoding="utf-8")


def project_ref_from_url(url: str) -> str:
    host = (urlparse(url).hostname or "").lower()
    suffix = ".supabase.co"
    if not host.endswith(suffix):
        return ""
    project_ref = host[: -len(suffix)]
    return project_ref if re.fullmatch(r"[a-z0-9]{20}", project_ref) else ""


def validate_migration(sql: Optional[str] = None) -> Dict[str, Any]:
    source = sql if sql is not None else migration_sql()
    lowered = source.lower()
    created = set(re.findall(
        r"create\s+table\s+if\s+not\s+exists\s+public\.([a-z0-9_]+)",
        lowered,
    ))
    expected = set(MARKET_TAPE_TABLES)
    missing_tables = sorted(expected - created)
    missing_rls = sorted(
        table
        for table in expected
        if f"alter table public.{table} enable row level security" not in lowered
    )
    missing_append_only = sorted(
        table
        for table in APPEND_ONLY_TABLES
        if not re.search(
            rf"before\s+update\s+or\s+delete\s+on\s+public\.{re.escape(table)}",
            lowered,
        )
    )
    declared_indexes = set(re.findall(
        r"create\s+index\s+if\s+not\s+exists\s+([a-z0-9_]+)",
        lowered,
    ))
    missing_required_indexes = sorted(set(REQUIRED_INDEXES) - declared_indexes)
    destructive_statements = sorted(set(re.findall(
        r"\b(drop\s+table|truncate\s+table|delete\s+from)\b",
        lowered,
    )))
    state = "ready" if not any(
        (
            missing_tables,
            missing_rls,
            missing_append_only,
            missing_required_indexes,
            destructive_statements,
        )
    ) else "invalid"
    return {
        "state": state,
        "migration": MIGRATION_NAME,
        "paths": [str(path) for path in MIGRATION_PATHS],
        "sha256": migration_sha256(source),
        "tables_expected": len(expected),
        "tables_declared": len(created & expected),
        "missing_tables": missing_tables,
        "missing_rls": missing_rls,
        "missing_append_only": missing_append_only,
        "required_indexes_expected": len(REQUIRED_INDEXES),
        "missing_required_indexes": missing_required_indexes,
        "destructive_statements": destructive_statements,
    }


class SupabaseMigrationManager:
    """Apply DDL through Supabase's Management API and verify it through PostgREST."""

    def __init__(
        self,
        *,
        supabase_url: Optional[str] = None,
        service_role_key: Optional[str] = None,
        access_token: Optional[str] = None,
        management_api_url: str = MANAGEMENT_API_URL,
        rest_base_url: Optional[str] = None,
        client: Optional[httpx.Client] = None,
        request_timeout_seconds: float = 30.0,
    ):
        load_runtime_environment()
        self.supabase_url = (supabase_url or os.getenv("SUPABASE_URL", "")).strip().rstrip("/")
        self.service_role_key = (
            service_role_key
            or os.getenv("SUPABASE_SERVICE_KEY", "")
            or os.getenv("SUPABASE_SERVICE_ROLE_KEY", "")
        ).strip()
        self.access_token = (access_token or os.getenv("SUPABASE_ACCESS_TOKEN", "")).strip()
        self.management_api_url = management_api_url.rstrip("/")
        self.rest_base_url = (
            rest_base_url or (f"{self.supabase_url}/rest/v1" if self.supabase_url else "")
        ).rstrip("/")
        self.client = client or httpx.Client(timeout=request_timeout_seconds)
        self._owns_client = client is None

    @property
    def project_ref(self) -> str:
        return project_ref_from_url(self.supabase_url)

    def close(self) -> None:
        if self._owns_client:
            self.client.close()

    def inspect(self, expected_project_ref: Optional[str] = None) -> Dict[str, Any]:
        target_error = self._target_error(expected_project_ref)
        if target_error:
            return target_error
        if not _valid_secret(self.service_role_key):
            return self._blocked("blocked_credential", "Supabase service-role credential is unavailable")

        tables: Dict[str, Dict[str, Any]] = {}
        headers = {
            "apikey": self.service_role_key,
            "Authorization": f"Bearer {self.service_role_key}",
        }
        for table, columns in MARKET_TAPE_TABLES.items():
            try:
                response = self.client.get(
                    f"{self.rest_base_url}/{table}",
                    params={"select": columns, "limit": "0"},
                    headers=headers,
                )
                tables[table] = {
                    "state": "ready" if response.status_code == 200 else "missing",
                    "http_status": response.status_code,
                    "detail": "" if response.status_code == 200 else sanitize(response.text)[:240],
                }
            except httpx.HTTPError as error:
                tables[table] = {
                    "state": "unreachable",
                    "http_status": 0,
                    "detail": sanitize(error)[:240],
                }
        ready = sum(value["state"] == "ready" for value in tables.values())
        return {
            "state": "ready" if ready == len(MARKET_TAPE_TABLES) else "incomplete",
            "project_ref": self.project_ref,
            "migration": MIGRATION_NAME,
            "sha256": migration_sha256(),
            "tables_ready": ready,
            "tables_expected": len(MARKET_TAPE_TABLES),
            "tables": tables,
        }

    def apply(
        self,
        expected_project_ref: str,
        *,
        sql: Optional[str] = None,
        verify_attempts: int = 8,
        verify_delay_seconds: float = 1.0,
    ) -> Dict[str, Any]:
        target_error = self._target_error(expected_project_ref)
        if target_error:
            return target_error
        if not _valid_secret(self.access_token):
            return self._blocked(
                "blocked_management_credential",
                "SUPABASE_ACCESS_TOKEN with database_write permission is unavailable",
            )
        source = sql if sql is not None else migration_sql()
        validation = validate_migration(source)
        if validation["state"] != "ready":
            return {"state": "invalid_migration", "validation": validation}

        try:
            response = self.client.post(
                f"{self.management_api_url}/v1/projects/{expected_project_ref}/database/query",
                headers={
                    "Authorization": f"Bearer {self.access_token}",
                    "Content-Type": "application/json",
                },
                json={"query": source, "read_only": False},
            )
        except httpx.HTTPError as error:
            return self._blocked("apply_failed", sanitize(error)[:500])
        if response.status_code not in {200, 201}:
            return self._blocked(
                "apply_failed",
                f"Management API returned HTTP {response.status_code}: {sanitize(response.text)[:400]}",
            )

        inspection: Dict[str, Any] = {"state": "incomplete"}
        for attempt in range(max(1, verify_attempts)):
            inspection = self.inspect(expected_project_ref)
            if inspection["state"] == "ready":
                break
            if attempt + 1 < verify_attempts:
                time.sleep(max(0.0, verify_delay_seconds))
        return {
            "state": "applied" if inspection["state"] == "ready" else "applied_unverified",
            "project_ref": expected_project_ref,
            "migration": MIGRATION_NAME,
            "sha256": validation["sha256"],
            "management_http_status": response.status_code,
            "inspection": inspection,
        }

    def verify_database(
        self,
        expected_project_ref: str,
        *,
        sql: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Verify schema security and append-only invariants through a read-only query."""
        query = self._read_only_rows(
            expected_project_ref,
            sql if sql is not None else verification_sql(),
        )
        if query["state"] != "ready":
            return query
        rows = query["rows"]

        by_table = {
            str(row.get("table_name")): row
            for row in rows
            if isinstance(row, dict) and row.get("table_name")
        }
        missing_tables = sorted(
            table
            for table in MARKET_TAPE_TABLES
            if not bool(by_table.get(table, {}).get("relation_exists"))
        )
        rls_disabled = sorted(
            table
            for table in MARKET_TAPE_TABLES
            if table in by_table and not bool(by_table[table].get("rls_enabled"))
        )
        unexpected_rls_policies = {
            table: int(by_table[table].get("policy_count") or 0)
            for table in MARKET_TAPE_TABLES
            if table in by_table and int(by_table[table].get("policy_count") or 0) > 0
        }
        missing_append_only_triggers = []
        for table, trigger_name in APPEND_ONLY_TRIGGERS.items():
            names = by_table.get(table, {}).get("trigger_names") or []
            if isinstance(names, str):
                try:
                    names = json.loads(names)
                except ValueError:
                    names = []
            if trigger_name not in names:
                missing_append_only_triggers.append(table)
        index_names_by_table: Dict[str, set[str]] = {}
        for table in MARKET_TAPE_TABLES:
            names = by_table.get(table, {}).get("index_names") or []
            if isinstance(names, str):
                try:
                    names = json.loads(names)
                except ValueError:
                    names = []
            index_names_by_table[table] = {
                str(name) for name in names if isinstance(name, str)
            }
        missing_required_indexes = sorted(
            index_name
            for index_name, table in REQUIRED_INDEXES.items()
            if index_name not in index_names_by_table.get(table, set())
        )

        violations = (
            missing_tables,
            rls_disabled,
            unexpected_rls_policies,
            missing_append_only_triggers,
            missing_required_indexes,
        )
        return {
            "state": "ready" if not any(violations) else "incomplete",
            "project_ref": expected_project_ref,
            "migration": MIGRATION_NAME,
            "sha256": migration_sha256(),
            "management_http_status": query["management_http_status"],
            "tables_verified": len(MARKET_TAPE_TABLES) - len(missing_tables),
            "tables_expected": len(MARKET_TAPE_TABLES),
            "missing_tables": missing_tables,
            "rls_disabled": rls_disabled,
            "unexpected_rls_policies": unexpected_rls_policies,
            "missing_append_only_triggers": missing_append_only_triggers,
            "required_indexes_expected": len(REQUIRED_INDEXES),
            "missing_required_indexes": missing_required_indexes,
        }

    def remote_counts(self, expected_project_ref: str) -> Dict[str, Any]:
        """Return authoritative row counts for every Market Tape mirror table."""
        statements = [
            f"select '{table}' as table_name, count(*)::bigint as row_count from public.{table}"
            for table in MARKET_TAPE_TABLES
        ]
        query = self._read_only_rows(expected_project_ref, "\nunion all\n".join(statements))
        if query["state"] != "ready":
            return query
        counts = {
            str(row.get("table_name")): int(row.get("row_count") or 0)
            for row in query["rows"]
            if isinstance(row, dict) and row.get("table_name") in MARKET_TAPE_TABLES
        }
        missing_tables = sorted(set(MARKET_TAPE_TABLES) - set(counts))
        return {
            "state": "ready" if not missing_tables else "incomplete",
            "project_ref": expected_project_ref,
            "migration": MIGRATION_NAME,
            "management_http_status": query["management_http_status"],
            "tables_counted": len(counts),
            "tables_expected": len(MARKET_TAPE_TABLES),
            "missing_tables": missing_tables,
            "counts": dict(sorted(counts.items())),
            "total_rows": sum(counts.values()),
        }

    def _read_only_rows(self, expected_project_ref: str, sql: str) -> Dict[str, Any]:
        target_error = self._target_error(expected_project_ref)
        if target_error:
            return target_error
        if not _valid_secret(self.access_token):
            return self._blocked(
                "blocked_management_credential",
                "SUPABASE_ACCESS_TOKEN with database_read permission is unavailable",
            )
        try:
            response = self.client.post(
                f"{self.management_api_url}/v1/projects/{expected_project_ref}/database/query",
                headers={
                    "Authorization": f"Bearer {self.access_token}",
                    "Content-Type": "application/json",
                },
                json={"query": sql, "read_only": True},
            )
        except httpx.HTTPError as error:
            return self._blocked("verification_failed", sanitize(error)[:500])
        if response.status_code not in {200, 201}:
            return self._blocked(
                "verification_failed",
                f"Management API returned HTTP {response.status_code}: {sanitize(response.text)[:400]}",
            )
        try:
            payload = response.json()
        except ValueError:
            return self._blocked("verification_failed", "Management API returned invalid JSON")
        if isinstance(payload, list):
            rows = payload
        elif isinstance(payload, dict):
            rows = payload.get("result", [])
        else:
            rows = None
        if not isinstance(rows, list):
            return self._blocked("verification_failed", "Management API returned an invalid result shape")
        return {
            "state": "ready",
            "project_ref": expected_project_ref,
            "management_http_status": response.status_code,
            "rows": rows,
        }

    def _target_error(self, expected_project_ref: Optional[str]) -> Optional[Dict[str, Any]]:
        if not self.project_ref:
            return self._blocked("blocked_target", "SUPABASE_URL is missing or invalid")
        if expected_project_ref and expected_project_ref != self.project_ref:
            return self._blocked(
                "blocked_target_mismatch",
                f"Requested project {expected_project_ref} does not match configured project {self.project_ref}",
            )
        return None

    def _blocked(self, state: str, detail: str) -> Dict[str, Any]:
        return {
            "state": state,
            "project_ref": self.project_ref,
            "migration": MIGRATION_NAME,
            "detail": detail,
        }


def _valid_secret(value: str) -> bool:
    lowered = value.lower()
    return len(value) > 40 and not lowered.startswith(("your_", "replace_", "<"))
