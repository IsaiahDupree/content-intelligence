"""Append-only SQLite spool and content-addressed raw archive for Market Tape V1."""

from __future__ import annotations

import gzip
import json
import math
import re
import sqlite3
from dataclasses import asdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

from .config import MarketTapeConfig
from .keywords import rank_keywords
from .math import age_bucket, concentration, counter_motion, poll_interval_seconds, trend_state, trend_strength, zscore
from .models import MarketContent, SourceReceipt, isoformat, stable_hash, utc_now


SCHEMA_VERSION = 5
WORD_RE = re.compile(r"[a-z0-9][a-z0-9'+-]*", re.IGNORECASE)
STOP_WORDS = {
    "a", "an", "and", "are", "as", "at", "be", "but", "by", "for", "from", "how",
    "i", "in", "is", "it", "my", "of", "on", "or", "our", "that", "the", "this", "to",
    "was", "we", "what", "when", "where", "why", "with", "you", "your",
}


class ClosingSQLiteConnection(sqlite3.Connection):
    """Commit or roll back a context block, then release its file descriptor."""

    def __exit__(self, exc_type: Any, exc_value: Any, traceback: Any) -> bool:
        try:
            return bool(super().__exit__(exc_type, exc_value, traceback))
        finally:
            self.close()


class MarketTapeStore:
    def __init__(self, config: MarketTapeConfig):
        self.config = config
        self.config.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.config.object_dir.mkdir(parents=True, exist_ok=True)
        self.initialize()

    def connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(
            self.config.db_path,
            timeout=30.0,
            factory=ClosingSQLiteConnection,
        )
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 30000")
        return connection

    def initialize(self) -> None:
        with self.connect() as connection:
            connection.executescript(
                """
                PRAGMA journal_mode = WAL;
                PRAGMA synchronous = NORMAL;

                CREATE TABLE IF NOT EXISTS mt_meta (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS mt_creators (
                    creator_id TEXT PRIMARY KEY,
                    platform TEXT NOT NULL,
                    external_id TEXT NOT NULL,
                    handle TEXT NOT NULL DEFAULT '',
                    display_name TEXT NOT NULL DEFAULT '',
                    followers INTEGER NOT NULL DEFAULT 0,
                    first_seen_at TEXT NOT NULL,
                    last_seen_at TEXT NOT NULL,
                    UNIQUE(platform, external_id)
                );

                CREATE TABLE IF NOT EXISTS mt_videos (
                    video_id TEXT PRIMARY KEY,
                    platform TEXT NOT NULL,
                    external_id TEXT NOT NULL,
                    creator_id TEXT NOT NULL,
                    published_at TEXT,
                    first_seen_at TEXT NOT NULL,
                    last_seen_at TEXT NOT NULL,
                    title TEXT NOT NULL DEFAULT '',
                    caption TEXT NOT NULL DEFAULT '',
                    description TEXT NOT NULL DEFAULT '',
                    language TEXT NOT NULL DEFAULT '',
                    url TEXT NOT NULL DEFAULT '',
                    thumbnail_url TEXT NOT NULL DEFAULT '',
                    media_type TEXT NOT NULL DEFAULT 'video',
                    duration_seconds REAL,
                    source_first_seen TEXT NOT NULL,
                    FOREIGN KEY(creator_id) REFERENCES mt_creators(creator_id),
                    UNIQUE(platform, external_id)
                );

                CREATE TABLE IF NOT EXISTS mt_discovery_attributions (
                    attribution_key TEXT PRIMARY KEY,
                    run_id TEXT NOT NULL,
                    video_id TEXT NOT NULL,
                    source_id TEXT NOT NULL,
                    discovered_at TEXT NOT NULL,
                    surface TEXT NOT NULL DEFAULT '',
                    query TEXT NOT NULL,
                    context_json TEXT NOT NULL DEFAULT '{}',
                    FOREIGN KEY(video_id) REFERENCES mt_videos(video_id)
                );

                CREATE INDEX IF NOT EXISTS mt_discovery_attribution_query_idx
                    ON mt_discovery_attributions(query, discovered_at DESC);
                CREATE INDEX IF NOT EXISTS mt_discovery_attribution_video_idx
                    ON mt_discovery_attributions(video_id, discovered_at DESC);

                CREATE TRIGGER IF NOT EXISTS mt_discovery_attributions_no_update
                BEFORE UPDATE ON mt_discovery_attributions
                BEGIN
                    SELECT RAISE(ABORT, 'discovery attributions are append-only');
                END;

                CREATE TRIGGER IF NOT EXISTS mt_discovery_attributions_no_delete
                BEFORE DELETE ON mt_discovery_attributions
                BEGIN
                    SELECT RAISE(ABORT, 'discovery attributions are append-only');
                END;

                CREATE TABLE IF NOT EXISTS mt_raw_objects (
                    raw_sha256 TEXT PRIMARY KEY,
                    object_path TEXT NOT NULL,
                    bytes_compressed INTEGER NOT NULL,
                    first_seen_at TEXT NOT NULL,
                    source_id TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS mt_market_observations (
                    observation_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    observation_key TEXT NOT NULL UNIQUE,
                    run_id TEXT NOT NULL,
                    observed_at TEXT NOT NULL,
                    wall_clock_date TEXT NOT NULL,
                    video_id TEXT NOT NULL,
                    creator_id TEXT NOT NULL,
                    platform TEXT NOT NULL,
                    source_id TEXT NOT NULL,
                    video_age_seconds REAL,
                    video_age_bucket TEXT NOT NULL,
                    views INTEGER NOT NULL DEFAULT 0,
                    likes INTEGER NOT NULL DEFAULT 0,
                    comments INTEGER NOT NULL DEFAULT 0,
                    shares INTEGER NOT NULL DEFAULT 0,
                    saves INTEGER NOT NULL DEFAULT 0,
                    creator_followers INTEGER NOT NULL DEFAULT 0,
                    view_velocity REAL NOT NULL DEFAULT 0,
                    view_acceleration REAL NOT NULL DEFAULT 0,
                    view_jerk REAL NOT NULL DEFAULT 0,
                    relative_strength REAL NOT NULL DEFAULT 0,
                    raw_sha256 TEXT NOT NULL,
                    source_confidence REAL NOT NULL DEFAULT 1.0,
                    FOREIGN KEY(video_id) REFERENCES mt_videos(video_id),
                    FOREIGN KEY(creator_id) REFERENCES mt_creators(creator_id),
                    FOREIGN KEY(raw_sha256) REFERENCES mt_raw_objects(raw_sha256)
                );

                CREATE INDEX IF NOT EXISTS mt_observation_video_time_idx
                    ON mt_market_observations(video_id, observed_at DESC);
                CREATE INDEX IF NOT EXISTS mt_observation_platform_time_idx
                    ON mt_market_observations(platform, observed_at DESC);
                CREATE INDEX IF NOT EXISTS mt_observation_context_idx
                    ON mt_market_observations(platform, video_age_bucket, view_velocity);

                CREATE TRIGGER IF NOT EXISTS mt_market_observations_no_update
                BEFORE UPDATE ON mt_market_observations
                BEGIN
                    SELECT RAISE(ABORT, 'market observations are append-only');
                END;

                CREATE TRIGGER IF NOT EXISTS mt_market_observations_no_delete
                BEFORE DELETE ON mt_market_observations
                BEGIN
                    SELECT RAISE(ABORT, 'market observations are append-only');
                END;

                CREATE TABLE IF NOT EXISTS mt_content_genomes (
                    video_id TEXT PRIMARY KEY,
                    schema_version INTEGER NOT NULL DEFAULT 1,
                    title TEXT NOT NULL DEFAULT '',
                    caption TEXT NOT NULL DEFAULT '',
                    description TEXT NOT NULL DEFAULT '',
                    hashtags_json TEXT NOT NULL DEFAULT '[]',
                    transcript TEXT NOT NULL DEFAULT '',
                    language TEXT NOT NULL DEFAULT '',
                    hook_type TEXT NOT NULL DEFAULT '',
                    opening_words TEXT NOT NULL DEFAULT '',
                    duration_seconds REAL,
                    aspect_ratio TEXT NOT NULL DEFAULT '',
                    cut_rate REAL,
                    caption_style TEXT NOT NULL DEFAULT '',
                    face_present INTEGER,
                    people_count INTEGER,
                    camera_motion TEXT NOT NULL DEFAULT '',
                    audio_id TEXT NOT NULL DEFAULT '',
                    audio_signature TEXT NOT NULL DEFAULT '',
                    topic_terms_json TEXT NOT NULL DEFAULT '[]',
                    text_embedding_ref TEXT NOT NULL DEFAULT '',
                    transcript_embedding_ref TEXT NOT NULL DEFAULT '',
                    visual_embedding_ref TEXT NOT NULL DEFAULT '',
                    audio_embedding_ref TEXT NOT NULL DEFAULT '',
                    extraction_status TEXT NOT NULL DEFAULT 'metadata_complete',
                    updated_at TEXT NOT NULL,
                    FOREIGN KEY(video_id) REFERENCES mt_videos(video_id)
                );

                CREATE TABLE IF NOT EXISTS mt_transcript_artifacts (
                    transcript_id TEXT PRIMARY KEY,
                    video_id TEXT NOT NULL,
                    platform TEXT NOT NULL,
                    external_id TEXT NOT NULL,
                    source_url TEXT NOT NULL,
                    observation_key TEXT NOT NULL,
                    source_metrics_json TEXT NOT NULL,
                    audio_path TEXT NOT NULL,
                    audio_sha256 TEXT NOT NULL,
                    transcript_path TEXT NOT NULL,
                    transcript_sha256 TEXT NOT NULL,
                    whisper_model TEXT NOT NULL,
                    whisper_language TEXT NOT NULL,
                    duration_seconds REAL NOT NULL,
                    word_count INTEGER NOT NULL,
                    segment_count INTEGER NOT NULL,
                    acquisition_json TEXT NOT NULL,
                    audit_json TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    FOREIGN KEY(video_id) REFERENCES mt_videos(video_id)
                );
                CREATE INDEX IF NOT EXISTS mt_transcript_artifacts_video_idx
                    ON mt_transcript_artifacts(video_id, created_at DESC);
                CREATE INDEX IF NOT EXISTS mt_transcript_artifacts_platform_idx
                    ON mt_transcript_artifacts(platform, created_at DESC);

                CREATE TABLE IF NOT EXISTS mt_transcript_cohorts (
                    cohort_id TEXT PRIMARY KEY,
                    topic TEXT NOT NULL,
                    decision TEXT NOT NULL,
                    member_ids_json TEXT NOT NULL,
                    policy_json TEXT NOT NULL,
                    aggregate_metrics_json TEXT NOT NULL,
                    audit_json TEXT NOT NULL,
                    manifest_path TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS mt_script_relatability_audits (
                    audit_id TEXT PRIMARY KEY,
                    script_id TEXT NOT NULL,
                    cohort_id TEXT NOT NULL,
                    decision TEXT NOT NULL,
                    score REAL NOT NULL,
                    script_sha256 TEXT NOT NULL,
                    cohort_manifest_sha256 TEXT NOT NULL,
                    findings_json TEXT NOT NULL,
                    receipt_path TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    FOREIGN KEY(cohort_id) REFERENCES mt_transcript_cohorts(cohort_id)
                );
                CREATE INDEX IF NOT EXISTS mt_script_relatability_script_idx
                    ON mt_script_relatability_audits(script_id, created_at DESC);

                CREATE TABLE IF NOT EXISTS mt_transcript_backfill_runs (
                    run_id TEXT PRIMARY KEY,
                    status TEXT NOT NULL,
                    policy_json TEXT NOT NULL,
                    candidate_ids_json TEXT NOT NULL,
                    artifact_ids_json TEXT NOT NULL,
                    failures_json TEXT NOT NULL,
                    manifest_path TEXT NOT NULL,
                    started_at TEXT NOT NULL,
                    finished_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS mt_trends (
                    trend_id TEXT PRIMARY KEY,
                    trend_type TEXT NOT NULL,
                    canonical_key TEXT NOT NULL,
                    display_name TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'discovering',
                    first_seen_at TEXT NOT NULL,
                    last_seen_at TEXT NOT NULL,
                    UNIQUE(trend_type, canonical_key)
                );

                CREATE TABLE IF NOT EXISTS mt_trend_memberships (
                    trend_id TEXT NOT NULL,
                    video_id TEXT NOT NULL,
                    confidence REAL NOT NULL,
                    evidence_json TEXT NOT NULL,
                    first_seen_at TEXT NOT NULL,
                    PRIMARY KEY(trend_id, video_id),
                    FOREIGN KEY(trend_id) REFERENCES mt_trends(trend_id),
                    FOREIGN KEY(video_id) REFERENCES mt_videos(video_id)
                );

                CREATE TABLE IF NOT EXISTS mt_trend_observations (
                    trend_observation_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    trend_id TEXT NOT NULL,
                    observed_at TEXT NOT NULL,
                    videos_total INTEGER NOT NULL,
                    videos_new_1h INTEGER NOT NULL,
                    creators_total INTEGER NOT NULL,
                    creators_new_1h INTEGER NOT NULL,
                    platforms_total INTEGER NOT NULL,
                    views_total INTEGER NOT NULL,
                    likes_total INTEGER NOT NULL,
                    comments_total INTEGER NOT NULL,
                    shares_total INTEGER NOT NULL,
                    median_video_velocity REAL NOT NULL,
                    p90_video_velocity REAL NOT NULL,
                    creator_breadth REAL NOT NULL,
                    platform_breadth REAL NOT NULL,
                    top1_concentration REAL NOT NULL,
                    top10_concentration REAL NOT NULL,
                    momentum REAL NOT NULL,
                    acceleration REAL NOT NULL,
                    relative_strength REAL NOT NULL,
                    saturation REAL NOT NULL,
                    trend_strength REAL NOT NULL,
                    index_version TEXT NOT NULL,
                    state TEXT NOT NULL,
                    FOREIGN KEY(trend_id) REFERENCES mt_trends(trend_id)
                );

                CREATE INDEX IF NOT EXISTS mt_trend_observation_time_idx
                    ON mt_trend_observations(trend_id, observed_at DESC);

                CREATE TRIGGER IF NOT EXISTS mt_trend_observations_no_update
                BEFORE UPDATE ON mt_trend_observations
                BEGIN
                    SELECT RAISE(ABORT, 'trend observations are append-only');
                END;

                CREATE TRIGGER IF NOT EXISTS mt_trend_observations_no_delete
                BEFORE DELETE ON mt_trend_observations
                BEGIN
                    SELECT RAISE(ABORT, 'trend observations are append-only');
                END;

                CREATE TABLE IF NOT EXISTS mt_poll_queue (
                    video_id TEXT PRIMARY KEY,
                    platform TEXT NOT NULL,
                    external_id TEXT NOT NULL,
                    preferred_source_id TEXT NOT NULL,
                    due_at TEXT NOT NULL,
                    hot_mode INTEGER NOT NULL DEFAULT 0,
                    failure_count INTEGER NOT NULL DEFAULT 0,
                    last_observed_at TEXT,
                    last_error_code TEXT NOT NULL DEFAULT '',
                    FOREIGN KEY(video_id) REFERENCES mt_videos(video_id)
                );

                CREATE INDEX IF NOT EXISTS mt_poll_due_idx ON mt_poll_queue(due_at, platform);

                CREATE TABLE IF NOT EXISTS mt_collection_runs (
                    run_id TEXT PRIMARY KEY,
                    mode TEXT NOT NULL,
                    started_at TEXT NOT NULL,
                    finished_at TEXT,
                    state TEXT NOT NULL,
                    items_seen INTEGER NOT NULL DEFAULT 0,
                    observations_added INTEGER NOT NULL DEFAULT 0,
                    unique_videos_added INTEGER NOT NULL DEFAULT 0,
                    requests INTEGER NOT NULL DEFAULT 0,
                    estimated_cost_usd REAL NOT NULL DEFAULT 0,
                    error_detail TEXT NOT NULL DEFAULT ''
                );

                CREATE TABLE IF NOT EXISTS mt_source_receipts (
                    receipt_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    run_id TEXT NOT NULL,
                    source_id TEXT NOT NULL,
                    platform TEXT NOT NULL,
                    state TEXT NOT NULL,
                    started_at TEXT NOT NULL,
                    finished_at TEXT NOT NULL,
                    request_count INTEGER NOT NULL,
                    discovered_count INTEGER NOT NULL,
                    refreshed_count INTEGER NOT NULL,
                    accepted_count INTEGER NOT NULL,
                    duplicate_count INTEGER NOT NULL,
                    failed_count INTEGER NOT NULL,
                    quota_remaining INTEGER,
                    estimated_cost_usd REAL NOT NULL,
                    error_code TEXT NOT NULL,
                    error_detail TEXT NOT NULL,
                    cursor TEXT NOT NULL,
                    metadata_json TEXT NOT NULL,
                    FOREIGN KEY(run_id) REFERENCES mt_collection_runs(run_id)
                );

                CREATE TABLE IF NOT EXISTS mt_source_health (
                    source_id TEXT PRIMARY KEY,
                    platform TEXT NOT NULL,
                    state TEXT NOT NULL,
                    checked_at TEXT NOT NULL,
                    last_success_at TEXT,
                    consecutive_failures INTEGER NOT NULL DEFAULT 0,
                    next_retry_at TEXT,
                    error_code TEXT NOT NULL DEFAULT '',
                    error_detail TEXT NOT NULL DEFAULT '',
                    receipt_json TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS mt_daily_usage (
                    usage_date TEXT NOT NULL,
                    source_id TEXT NOT NULL,
                    platform TEXT NOT NULL,
                    requests INTEGER NOT NULL DEFAULT 0,
                    items_seen INTEGER NOT NULL DEFAULT 0,
                    observations_added INTEGER NOT NULL DEFAULT 0,
                    unique_videos_added INTEGER NOT NULL DEFAULT 0,
                    estimated_cost_usd REAL NOT NULL DEFAULT 0,
                    PRIMARY KEY(usage_date, source_id)
                );

                CREATE TABLE IF NOT EXISTS mt_predictions (
                    prediction_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    subject_type TEXT NOT NULL,
                    subject_id TEXT NOT NULL,
                    model_version TEXT NOT NULL,
                    predicted_at TEXT NOT NULL,
                    horizon TEXT NOT NULL,
                    probability REAL NOT NULL,
                    expected_peak_at TEXT,
                    expected_remaining_life_hours REAL,
                    features_json TEXT NOT NULL,
                    outcome_json TEXT
                );

                CREATE TABLE IF NOT EXISTS mt_sync_outbox (
                    outbox_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    entity_type TEXT NOT NULL,
                    entity_key TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    attempts INTEGER NOT NULL DEFAULT 0,
                    next_attempt_at TEXT NOT NULL,
                    synced_at TEXT,
                    error_detail TEXT NOT NULL DEFAULT '',
                    UNIQUE(entity_type, entity_key)
                );

                CREATE INDEX IF NOT EXISTS mt_sync_outbox_pending_idx
                    ON mt_sync_outbox(synced_at, next_attempt_at, outbox_id);

                CREATE TABLE IF NOT EXISTS mt_sink_health (
                    sink_id TEXT PRIMARY KEY,
                    state TEXT NOT NULL,
                    checked_at TEXT NOT NULL,
                    last_success_at TEXT,
                    pending_count INTEGER NOT NULL DEFAULT 0,
                    error_detail TEXT NOT NULL DEFAULT ''
                );
                """
            )
            source_health_columns = {
                row[1] for row in connection.execute("PRAGMA table_info(mt_source_health)").fetchall()
            }
            if "consecutive_failures" not in source_health_columns:
                connection.execute(
                    "ALTER TABLE mt_source_health ADD COLUMN consecutive_failures INTEGER NOT NULL DEFAULT 0"
                )
            if "next_retry_at" not in source_health_columns:
                connection.execute("ALTER TABLE mt_source_health ADD COLUMN next_retry_at TEXT")
            connection.execute(
                "INSERT INTO mt_meta(key, value) VALUES('schema_version', ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (str(SCHEMA_VERSION),),
            )

    def start_run(self, run_id: str, mode: str) -> None:
        with self.connect() as connection:
            connection.execute(
                "INSERT INTO mt_collection_runs(run_id, mode, started_at, state) VALUES(?, ?, ?, 'running')",
                (run_id, mode, isoformat(utc_now())),
            )

    def finish_run(self, run_id: str, state: str = "completed", error_detail: str = "") -> None:
        with self.connect() as connection:
            totals = connection.execute(
                """SELECT COALESCE(SUM(discovered_count + refreshed_count), 0) AS items_seen,
                          COALESCE(SUM(accepted_count), 0) AS accepted,
                          COALESCE(SUM(request_count), 0) AS requests,
                          COALESCE(SUM(estimated_cost_usd), 0) AS cost
                   FROM mt_source_receipts WHERE run_id = ?""",
                (run_id,),
            ).fetchone()
            unique_added = connection.execute(
                "SELECT COUNT(*) FROM mt_videos WHERE source_first_seen = ?",
                (run_id,),
            ).fetchone()[0]
            connection.execute(
                """UPDATE mt_collection_runs
                   SET finished_at = ?, state = ?, items_seen = ?, observations_added = ?,
                       unique_videos_added = ?, requests = ?, estimated_cost_usd = ?, error_detail = ?
                   WHERE run_id = ?""",
                (
                    isoformat(utc_now()), state, int(totals["items_seen"]), int(totals["accepted"]),
                    int(unique_added), int(totals["requests"]), float(totals["cost"]), error_detail[:1000], run_id,
                ),
            )

    def enqueue_run_for_sync(self, run_id: str) -> int:
        """Build an idempotent remote-sync outbox after local commit completes."""
        created_at = isoformat(utc_now())
        records: List[Tuple[str, str, Dict[str, Any]]] = []
        with self.connect() as connection:
            observations = [dict(row) for row in connection.execute(
                "SELECT * FROM mt_market_observations WHERE run_id = ?", (run_id,)
            ).fetchall()]
            video_ids = sorted({row["video_id"] for row in observations})
            creator_ids = sorted({row["creator_id"] for row in observations})
            for row in _select_in(connection, "mt_creators", "creator_id", creator_ids):
                records.append(("creator", row["creator_id"], row))
            for row in _select_in(connection, "mt_videos", "video_id", video_ids):
                records.append(("video", row["video_id"], row))
            for row in connection.execute(
                "SELECT * FROM mt_discovery_attributions WHERE run_id = ?",
                (run_id,),
            ).fetchall():
                payload = dict(row)
                records.append((
                    "discovery_attribution",
                    payload["attribution_key"],
                    payload,
                ))
            for row in observations:
                row.pop("observation_id", None)
                records.append(("observation", row["observation_key"], row))
            for row in _select_in(connection, "mt_content_genomes", "video_id", video_ids):
                records.append(("genome", row["video_id"], row))
            if video_ids:
                placeholders = ",".join("?" for _ in video_ids)
                memberships = [dict(row) for row in connection.execute(
                    f"SELECT * FROM mt_trend_memberships WHERE video_id IN ({placeholders})", video_ids
                ).fetchall()]
                trend_ids = sorted({row["trend_id"] for row in memberships})
                for row in _select_in(connection, "mt_trends", "trend_id", trend_ids):
                    records.append(("trend", row["trend_id"], row))
                for row in memberships:
                    records.append(("membership", f"{row['trend_id']}|{row['video_id']}", row))
            run = connection.execute("SELECT * FROM mt_collection_runs WHERE run_id = ?", (run_id,)).fetchone()
            if run:
                run_payload = dict(run)
                records.append(("run", run_id, run_payload))
                if run_payload.get("started_at"):
                    trend_rows = [dict(row) for row in connection.execute(
                        """SELECT * FROM mt_trend_observations
                           WHERE observed_at >= ? AND observed_at <= COALESCE(?, observed_at)""",
                        (run_payload["started_at"], run_payload.get("finished_at")),
                    ).fetchall()]
                    for row in trend_rows:
                        row.pop("trend_observation_id", None)
                        key = stable_hash({"trend_id": row["trend_id"], "observed_at": row["observed_at"]})
                        row["trend_observation_key"] = key
                        records.append(("trend_observation", key, row))
                    prediction_rows = [dict(row) for row in connection.execute(
                        """SELECT * FROM mt_predictions
                           WHERE predicted_at >= ? AND predicted_at <= COALESCE(?, predicted_at)""",
                        (run_payload["started_at"], run_payload.get("finished_at")),
                    ).fetchall()]
                    for row in prediction_rows:
                        row.pop("prediction_id", None)
                        key = stable_hash({
                            "subject_type": row["subject_type"], "subject_id": row["subject_id"],
                            "model_version": row["model_version"], "predicted_at": row["predicted_at"],
                            "horizon": row["horizon"],
                        })
                        row["prediction_key"] = key
                        records.append(("prediction", key, row))
            source_ids: List[str] = []
            for row in connection.execute("SELECT * FROM mt_source_receipts WHERE run_id = ?", (run_id,)).fetchall():
                payload = dict(row)
                payload.pop("receipt_id", None)
                source_ids.append(str(payload["source_id"]))
                key = stable_hash({
                    "run_id": payload["run_id"], "source_id": payload["source_id"],
                    "started_at": payload["started_at"], "finished_at": payload["finished_at"],
                })
                payload["receipt_key"] = key
                records.append(("receipt", key, payload))
            for row in _select_in(connection, "mt_source_health", "source_id", sorted(set(source_ids))):
                records.append(("source_health", row["source_id"], row))

            for entity_type, entity_key, payload in records:
                connection.execute(
                    """INSERT INTO mt_sync_outbox(
                           entity_type, entity_key, payload_json, created_at, next_attempt_at
                       ) VALUES(?, ?, ?, ?, ?)
                       ON CONFLICT(entity_type, entity_key) DO UPDATE SET
                           payload_json = excluded.payload_json, next_attempt_at = excluded.next_attempt_at,
                           synced_at = NULL, error_detail = ''""",
                    (entity_type, entity_key, json.dumps(payload, sort_keys=True, default=str), created_at, created_at),
                )
        return len(records)

    def enqueue_missing_for_sync(self) -> int:
        """Queue local canonical records that never entered the durable outbox."""
        created_at = isoformat(utc_now())
        records: List[Tuple[str, str, Dict[str, Any]]] = []
        with self.connect() as connection:
            existing: Dict[str, set[str]] = {}
            for row in connection.execute(
                "SELECT entity_type, entity_key FROM mt_sync_outbox"
            ).fetchall():
                existing.setdefault(str(row["entity_type"]), set()).add(str(row["entity_key"]))

            def add(entity_type: str, entity_key: str, payload: Dict[str, Any]) -> None:
                keys = existing.setdefault(entity_type, set())
                if entity_key in keys:
                    return
                keys.add(entity_key)
                records.append((entity_type, entity_key, payload))

            for row in connection.execute("SELECT * FROM mt_creators").fetchall():
                payload = dict(row)
                add("creator", payload["creator_id"], payload)
            for row in connection.execute("SELECT * FROM mt_trends").fetchall():
                payload = dict(row)
                add("trend", payload["trend_id"], payload)
            for row in connection.execute("SELECT * FROM mt_collection_runs").fetchall():
                payload = dict(row)
                add("run", payload["run_id"], payload)
            for row in connection.execute("SELECT * FROM mt_videos").fetchall():
                payload = dict(row)
                add("video", payload["video_id"], payload)
            for row in connection.execute("SELECT * FROM mt_discovery_attributions").fetchall():
                payload = dict(row)
                add("discovery_attribution", payload["attribution_key"], payload)
            for row in connection.execute("SELECT * FROM mt_market_observations").fetchall():
                payload = dict(row)
                payload.pop("observation_id", None)
                add("observation", payload["observation_key"], payload)
            for row in connection.execute("SELECT * FROM mt_content_genomes").fetchall():
                payload = dict(row)
                add("genome", payload["video_id"], payload)
            for row in connection.execute("SELECT * FROM mt_trend_memberships").fetchall():
                payload = dict(row)
                add("membership", f"{payload['trend_id']}|{payload['video_id']}", payload)
            for row in connection.execute("SELECT * FROM mt_trend_observations").fetchall():
                payload = dict(row)
                payload.pop("trend_observation_id", None)
                key = stable_hash({
                    "trend_id": payload["trend_id"],
                    "observed_at": payload["observed_at"],
                })
                payload["trend_observation_key"] = key
                add("trend_observation", key, payload)
            for row in connection.execute("SELECT * FROM mt_predictions").fetchall():
                payload = dict(row)
                payload.pop("prediction_id", None)
                key = stable_hash({
                    "subject_type": payload["subject_type"],
                    "subject_id": payload["subject_id"],
                    "model_version": payload["model_version"],
                    "predicted_at": payload["predicted_at"],
                    "horizon": payload["horizon"],
                })
                payload["prediction_key"] = key
                add("prediction", key, payload)
            for row in connection.execute("SELECT * FROM mt_source_receipts").fetchall():
                payload = dict(row)
                payload.pop("receipt_id", None)
                key = stable_hash({
                    "run_id": payload["run_id"],
                    "source_id": payload["source_id"],
                    "started_at": payload["started_at"],
                    "finished_at": payload["finished_at"],
                })
                payload["receipt_key"] = key
                add("receipt", key, payload)
            for row in connection.execute("SELECT * FROM mt_source_health").fetchall():
                payload = dict(row)
                add("source_health", payload["source_id"], payload)

            connection.executemany(
                """INSERT INTO mt_sync_outbox(
                       entity_type, entity_key, payload_json, created_at, next_attempt_at
                   ) VALUES(?, ?, ?, ?, ?)""",
                [
                    (
                        entity_type,
                        entity_key,
                        json.dumps(payload, sort_keys=True, default=str),
                        created_at,
                        created_at,
                    )
                    for entity_type, entity_key, payload in records
                ],
            )
        return len(records)

    def pending_outbox(
        self,
        limit: int,
        entity_order: Optional[Sequence[str]] = None,
    ) -> List[Dict[str, Any]]:
        order = tuple(entity_order or ())
        if order:
            rank_sql = " ".join(f"WHEN ? THEN {index}" for index, _ in enumerate(order))
            order_sql = f"CASE entity_type {rank_sql} ELSE {len(order)} END, outbox_id ASC"
        else:
            order_sql = "outbox_id ASC"
        row_limit = min(max(1, limit), 5000)
        with self.connect() as connection:
            rows = connection.execute(
                f"""SELECT outbox_id, entity_type, entity_key, payload_json, attempts
                    FROM mt_sync_outbox
                    WHERE synced_at IS NULL AND next_attempt_at <= ?
                    ORDER BY {order_sql} LIMIT ?""",
                (isoformat(utc_now()), *order, row_limit),
            ).fetchall()
        output = []
        for row in rows:
            value = dict(row)
            value["payload"] = json.loads(value.pop("payload_json"))
            output.append(value)
        return output

    def outbox_pending_count(self) -> int:
        with self.connect() as connection:
            return int(connection.execute(
                "SELECT COUNT(*) FROM mt_sync_outbox WHERE synced_at IS NULL"
            ).fetchone()[0])

    def make_outbox_due(self) -> int:
        now = isoformat(utc_now())
        with self.connect() as connection:
            cursor = connection.execute(
                "UPDATE mt_sync_outbox SET next_attempt_at = ? WHERE synced_at IS NULL",
                (now,),
            )
            return int(cursor.rowcount)

    def mark_outbox_synced(self, outbox_ids: Iterable[int]) -> None:
        now = isoformat(utc_now())
        with self.connect() as connection:
            connection.executemany(
                "UPDATE mt_sync_outbox SET synced_at = ?, error_detail = '' WHERE outbox_id = ?",
                [(now, int(outbox_id)) for outbox_id in outbox_ids],
            )

    def mark_outbox_failed(self, outbox_ids: Iterable[int], error_detail: str) -> None:
        with self.connect() as connection:
            for outbox_id in outbox_ids:
                row = connection.execute(
                    "SELECT attempts FROM mt_sync_outbox WHERE outbox_id = ?", (int(outbox_id),)
                ).fetchone()
                attempts = int(row[0]) + 1 if row else 1
                delay = min(21600, 60 * (2 ** min(8, attempts - 1)))
                connection.execute(
                    """UPDATE mt_sync_outbox
                       SET attempts = ?, next_attempt_at = ?, error_detail = ? WHERE outbox_id = ?""",
                    (attempts, isoformat(utc_now() + timedelta(seconds=delay)), error_detail[:1000], int(outbox_id)),
                )

    def save_sink_health(self, state: str, pending: int, error_detail: str = "") -> None:
        now = isoformat(utc_now())
        with self.connect() as connection:
            previous = connection.execute(
                "SELECT last_success_at FROM mt_sink_health WHERE sink_id = 'supabase'"
            ).fetchone()
            last_success = now if state == "ready" else (previous[0] if previous else None)
            connection.execute(
                """INSERT INTO mt_sink_health(sink_id, state, checked_at, last_success_at, pending_count, error_detail)
                   VALUES('supabase', ?, ?, ?, ?, ?)
                   ON CONFLICT(sink_id) DO UPDATE SET
                       state = excluded.state, checked_at = excluded.checked_at,
                       last_success_at = COALESCE(excluded.last_success_at, mt_sink_health.last_success_at),
                       pending_count = excluded.pending_count, error_detail = excluded.error_detail""",
                (state, now, last_success, pending, error_detail[:1000]),
            )

    def save_receipt(self, receipt: SourceReceipt) -> None:
        data = receipt.to_dict()
        usage_date = receipt.finished_at.astimezone(timezone.utc).date().isoformat()
        with self.connect() as connection:
            connection.execute(
                """INSERT INTO mt_source_receipts(
                       run_id, source_id, platform, state, started_at, finished_at, request_count,
                       discovered_count, refreshed_count, accepted_count, duplicate_count, failed_count,
                       quota_remaining, estimated_cost_usd, error_code, error_detail, cursor, metadata_json
                   ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    receipt.run_id, receipt.source_id, receipt.platform, receipt.state.value,
                    data["started_at"], data["finished_at"], receipt.request_count,
                    receipt.discovered_count, receipt.refreshed_count, receipt.accepted_count,
                    receipt.duplicate_count, receipt.failed_count, receipt.quota_remaining,
                    receipt.estimated_cost_usd, receipt.error_code, receipt.error_detail[:1000],
                    receipt.cursor, json.dumps(receipt.metadata, sort_keys=True),
                ),
            )
            previous = connection.execute(
                """SELECT last_success_at, consecutive_failures, next_retry_at
                   FROM mt_source_health WHERE source_id = ?""",
                (receipt.source_id,),
            ).fetchone()
            if receipt.error_code != "circuit_open":
                ready = receipt.state.value == "ready"
                previous_failures = int(previous[1] or 0) if previous else 0
                consecutive_failures = 0 if ready else previous_failures + 1
                last_success = data["finished_at"] if ready else (previous[0] if previous else None)
                next_retry = None
                if not ready:
                    next_retry = isoformat(
                        receipt.finished_at + timedelta(
                            seconds=self._source_backoff_seconds(receipt, consecutive_failures)
                        )
                    )
                connection.execute(
                    """INSERT INTO mt_source_health(
                           source_id, platform, state, checked_at, last_success_at,
                           consecutive_failures, next_retry_at, error_code, error_detail, receipt_json
                       ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                       ON CONFLICT(source_id) DO UPDATE SET
                           platform = excluded.platform, state = excluded.state, checked_at = excluded.checked_at,
                           last_success_at = COALESCE(excluded.last_success_at, mt_source_health.last_success_at),
                           consecutive_failures = excluded.consecutive_failures,
                           next_retry_at = excluded.next_retry_at,
                           error_code = excluded.error_code, error_detail = excluded.error_detail,
                           receipt_json = excluded.receipt_json""",
                    (
                        receipt.source_id, receipt.platform, receipt.state.value, data["finished_at"],
                        last_success, consecutive_failures, next_retry, receipt.error_code,
                        receipt.error_detail[:1000], json.dumps(data, sort_keys=True),
                    ),
                )
            connection.execute(
                """INSERT INTO mt_daily_usage(
                       usage_date, source_id, platform, requests, items_seen, observations_added,
                       unique_videos_added, estimated_cost_usd
                   ) VALUES(?, ?, ?, ?, ?, ?, 0, ?)
                   ON CONFLICT(usage_date, source_id) DO UPDATE SET
                       requests = requests + excluded.requests,
                       items_seen = items_seen + excluded.items_seen,
                       observations_added = observations_added + excluded.observations_added,
                       estimated_cost_usd = estimated_cost_usd + excluded.estimated_cost_usd""",
                (
                    usage_date, receipt.source_id, receipt.platform, receipt.request_count,
                    receipt.discovered_count + receipt.refreshed_count, receipt.accepted_count,
                    receipt.estimated_cost_usd,
                ),
            )

    def ingest(self, item: MarketContent, run_id: str) -> Tuple[bool, bool]:
        """Append an observation. Returns (observation_added, unique_video_added)."""
        raw_sha, raw_path, raw_bytes = self._archive_raw(item)
        observed = isoformat(item.observed_at)
        published = isoformat(item.published_at)
        age_seconds = max(0.0, (item.observed_at - item.published_at).total_seconds()) if item.published_at else None
        bucket = age_bucket(item.published_at, item.observed_at)

        with self.connect() as connection:
            prior_rows = [dict(row) for row in connection.execute(
                """SELECT observed_at, views FROM mt_market_observations
                   WHERE video_id = ? ORDER BY observed_at DESC LIMIT 3""",
                (item.video_id,),
            ).fetchall()][::-1]
            motion_rows = prior_rows + [{"observed_at": observed, "views": item.metrics.views}]
            motion = counter_motion(motion_rows)
            cohort = [row[0] for row in connection.execute(
                """SELECT view_velocity FROM mt_market_observations
                   WHERE platform = ? AND video_age_bucket = ? AND observed_at >= ?
                   ORDER BY observed_at DESC LIMIT 500""",
                (item.platform, bucket, isoformat(item.observed_at - timedelta(days=30))),
            ).fetchall()]
            relative_strength = zscore(motion.velocity, cohort)

            connection.execute(
                """INSERT INTO mt_raw_objects(raw_sha256, object_path, bytes_compressed, first_seen_at, source_id)
                   VALUES(?, ?, ?, ?, ?) ON CONFLICT(raw_sha256) DO NOTHING""",
                (raw_sha, raw_path, raw_bytes, observed, item.source_id),
            )
            connection.execute(
                """INSERT INTO mt_creators(
                       creator_id, platform, external_id, handle, display_name, followers, first_seen_at, last_seen_at
                   ) VALUES(?, ?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(creator_id) DO UPDATE SET
                       handle = CASE WHEN excluded.handle != '' THEN excluded.handle ELSE mt_creators.handle END,
                       display_name = CASE WHEN excluded.display_name != '' THEN excluded.display_name ELSE mt_creators.display_name END,
                       followers = MAX(mt_creators.followers, excluded.followers), last_seen_at = excluded.last_seen_at""",
                (
                    item.creator_id, item.platform, item.creator_external_id, item.creator_handle,
                    item.creator_name, item.creator_followers, observed, observed,
                ),
            )
            exists = connection.execute("SELECT 1 FROM mt_videos WHERE video_id = ?", (item.video_id,)).fetchone()
            connection.execute(
                """INSERT INTO mt_videos(
                       video_id, platform, external_id, creator_id, published_at, first_seen_at, last_seen_at,
                       title, caption, description, language, url, thumbnail_url, media_type,
                       duration_seconds, source_first_seen
                   ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(video_id) DO UPDATE SET
                       last_seen_at = excluded.last_seen_at,
                       published_at = COALESCE(mt_videos.published_at, excluded.published_at),
                       title = CASE WHEN excluded.title != '' THEN excluded.title ELSE mt_videos.title END,
                       caption = CASE WHEN excluded.caption != '' THEN excluded.caption ELSE mt_videos.caption END,
                       description = CASE WHEN excluded.description != '' THEN excluded.description ELSE mt_videos.description END,
                       language = CASE WHEN excluded.language != '' THEN excluded.language ELSE mt_videos.language END,
                       url = CASE WHEN excluded.url != '' THEN excluded.url ELSE mt_videos.url END,
                       thumbnail_url = CASE WHEN excluded.thumbnail_url != '' THEN excluded.thumbnail_url ELSE mt_videos.thumbnail_url END,
                       duration_seconds = COALESCE(excluded.duration_seconds, mt_videos.duration_seconds)""",
                (
                    item.video_id, item.platform, item.external_id, item.creator_id, published, observed, observed,
                    item.title, item.caption, item.description, item.language, item.url, item.thumbnail_url,
                    item.media_type, item.duration_seconds, run_id,
                ),
            )
            self._record_discovery_attributions(connection, item, run_id, observed)
            cursor = connection.execute(
                """INSERT INTO mt_market_observations(
                       observation_key, run_id, observed_at, wall_clock_date, video_id, creator_id, platform,
                       source_id, video_age_seconds, video_age_bucket, views, likes, comments, shares, saves,
                       creator_followers, view_velocity, view_acceleration, view_jerk, relative_strength,
                       raw_sha256, source_confidence
                   ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(observation_key) DO NOTHING""",
                (
                    item.observation_key, run_id, observed, item.observed_at.date().isoformat(), item.video_id,
                    item.creator_id, item.platform, item.source_id, age_seconds, bucket, item.metrics.views,
                    item.metrics.likes, item.metrics.comments, item.metrics.shares, item.metrics.saves,
                    item.creator_followers, motion.velocity, motion.acceleration, motion.jerk, relative_strength,
                    raw_sha, 1.0,
                ),
            )
            added = cursor.rowcount == 1
            if added:
                self._upsert_genome(connection, item, observed)
                self._map_trends(connection, item, observed)
                self._schedule_next(connection, item, age_seconds or 0.0, motion.acceleration > 0.1 or relative_strength >= 2.0)
            return added, not bool(exists)

    def _record_discovery_attributions(
        self,
        connection: sqlite3.Connection,
        item: MarketContent,
        run_id: str,
        observed: str,
    ) -> None:
        context = item.discovery_context if isinstance(item.discovery_context, dict) else {}
        raw_queries: List[Any] = []
        configured_queries = context.get("queries")
        if isinstance(configured_queries, list):
            raw_queries.extend(configured_queries)
        for key in ("query", "topic", "niche"):
            if context.get(key):
                raw_queries.append(context[key])
        queries = list(dict.fromkeys(
            " ".join(str(value).split())[:200]
            for value in raw_queries
            if str(value).strip()
        ))
        if not queries:
            return
        surface = str(context.get("surface") or context.get("lane") or "")[:100]
        context_json = json.dumps(context, sort_keys=True, default=str)
        for query in queries:
            attribution_key = stable_hash({
                "run_id": run_id,
                "video_id": item.video_id,
                "source_id": item.source_id,
                "query": query.casefold(),
            })
            connection.execute(
                """INSERT INTO mt_discovery_attributions(
                       attribution_key, run_id, video_id, source_id, discovered_at,
                       surface, query, context_json
                   ) VALUES(?, ?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(attribution_key) DO NOTHING""",
                (
                    attribution_key, run_id, item.video_id, item.source_id, observed,
                    surface, query, context_json,
                ),
            )

    def _archive_raw(self, item: MarketContent) -> Tuple[str, str, int]:
        payload = item.raw_payload or {"empty_payload": True, "video_id": item.video_id}
        canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8")
        raw_sha = stable_hash(payload)
        day = item.observed_at.astimezone(timezone.utc)
        relative = Path(str(day.year), f"{day.month:02d}", f"{day.day:02d}", f"{raw_sha}.json.gz")
        path = self.config.object_dir / relative
        if self.config.archive_raw_payloads and not path.exists():
            path.parent.mkdir(parents=True, exist_ok=True)
            temporary = path.with_suffix(path.suffix + ".tmp")
            with gzip.open(temporary, "wb", compresslevel=6) as handle:
                handle.write(canonical)
            temporary.replace(path)
        size = path.stat().st_size if path.exists() else len(canonical)
        return raw_sha, str(relative), size

    def _upsert_genome(self, connection: sqlite3.Connection, item: MarketContent, observed: str) -> None:
        text = " ".join(value for value in (item.title, item.caption, item.description) if value)
        words = [word.lower() for word in WORD_RE.findall(text) if word.lower() not in STOP_WORDS]
        topics = list(dict.fromkeys(item.hashtags + words[:12]))[:20]
        opening = " ".join(WORD_RE.findall(text)[:12])
        hook_type = self._hook_type(opening)
        connection.execute(
            """INSERT INTO mt_content_genomes(
                   video_id, title, caption, description, hashtags_json, language, hook_type, opening_words,
                   duration_seconds, audio_id, audio_signature, topic_terms_json, extraction_status, updated_at
               ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'metadata_complete', ?)
               ON CONFLICT(video_id) DO UPDATE SET
                   title = excluded.title, caption = excluded.caption, description = excluded.description,
                   hashtags_json = excluded.hashtags_json, language = excluded.language,
                   hook_type = excluded.hook_type, opening_words = excluded.opening_words,
                   duration_seconds = COALESCE(excluded.duration_seconds, mt_content_genomes.duration_seconds),
                   audio_id = CASE WHEN excluded.audio_id != '' THEN excluded.audio_id ELSE mt_content_genomes.audio_id END,
                   audio_signature = CASE WHEN excluded.audio_signature != '' THEN excluded.audio_signature ELSE mt_content_genomes.audio_signature END,
                   topic_terms_json = excluded.topic_terms_json, updated_at = excluded.updated_at""",
            (
                item.video_id, item.title, item.caption, item.description, json.dumps(item.hashtags),
                item.language, hook_type, opening, item.duration_seconds, item.audio_id,
                item.audio_id or item.audio_title, json.dumps(topics), observed,
            ),
        )

    def _map_trends(self, connection: sqlite3.Connection, item: MarketContent, observed: str) -> None:
        text = " ".join(value for value in (item.title, item.caption) if value)
        words = [word.lower() for word in WORD_RE.findall(text) if word.lower() not in STOP_WORDS and len(word) > 2]
        candidates: List[Tuple[str, str, str, float]] = []
        for hashtag in item.hashtags[:4]:
            candidates.append(("hashtag", hashtag, f"#{hashtag}", 0.95))
        if len(words) >= 2:
            key = " ".join(words[:3])
            candidates.append(("hook", key, key.title(), 0.72))
        for first, second in list(zip(words[:6], words[1:7]))[:2]:
            if first != second:
                key = f"{first} {second}"
                candidates.append(("topic", key, key.title(), 0.62))
        if item.audio_id:
            candidates.append(("audio", item.audio_id, item.audio_title or item.audio_id, 0.9))
        format_key = "short" if (item.duration_seconds or 0) <= 60 else "longform"
        candidates.append(("format", f"{item.media_type}:{format_key}", f"{item.media_type.title()} {format_key}", 0.55))

        seen = set()
        for trend_type, key, display, confidence in candidates[:9]:
            signature = (trend_type, key)
            if not key or signature in seen:
                continue
            seen.add(signature)
            trend_id = f"trend:{trend_type}:{stable_hash(key)[:16]}"
            connection.execute(
                """INSERT INTO mt_trends(
                       trend_id, trend_type, canonical_key, display_name, first_seen_at, last_seen_at
                   ) VALUES(?, ?, ?, ?, ?, ?)
                   ON CONFLICT(trend_id) DO UPDATE SET last_seen_at = excluded.last_seen_at""",
                (trend_id, trend_type, key, display[:200], observed, observed),
            )
            connection.execute(
                """INSERT INTO mt_trend_memberships(
                       trend_id, video_id, confidence, evidence_json, first_seen_at
                   ) VALUES(?, ?, ?, ?, ?) ON CONFLICT(trend_id, video_id) DO NOTHING""",
                (trend_id, item.video_id, confidence, json.dumps({"type": trend_type, "value": key}), observed),
            )

    def _schedule_next(
        self, connection: sqlite3.Connection, item: MarketContent, age_seconds: float, hot_mode: bool
    ) -> None:
        due = item.observed_at + timedelta(seconds=poll_interval_seconds(age_seconds, hot_mode))
        connection.execute(
            """INSERT INTO mt_poll_queue(
                   video_id, platform, external_id, preferred_source_id, due_at, hot_mode, last_observed_at
               ) VALUES(?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(video_id) DO UPDATE SET
                   preferred_source_id = excluded.preferred_source_id, due_at = excluded.due_at,
                   hot_mode = excluded.hot_mode, failure_count = 0, last_observed_at = excluded.last_observed_at,
                   last_error_code = ''""",
            (
                item.video_id, item.platform, item.external_id, item.source_id,
                isoformat(due), int(hot_mode), isoformat(item.observed_at),
            ),
        )

    def mark_poll_failure(self, video_ids: Iterable[str], error_code: str) -> None:
        due = isoformat(utc_now() + timedelta(hours=1))
        with self.connect() as connection:
            connection.executemany(
                """UPDATE mt_poll_queue SET failure_count = failure_count + 1, due_at = ?, last_error_code = ?
                   WHERE video_id = ?""",
                [(due, error_code[:80], video_id) for video_id in video_ids],
            )

    def due_polls(self, limit: int) -> Dict[str, List[Dict[str, Any]]]:
        with self.connect() as connection:
            rows = connection.execute(
                """SELECT q.video_id, q.platform, q.external_id, q.preferred_source_id, q.hot_mode,
                          v.published_at, v.title, v.caption, v.description, v.language, v.url,
                          v.thumbnail_url, v.duration_seconds, c.external_id AS creator_external_id,
                          c.handle AS creator_handle, c.display_name AS creator_name, c.followers AS creator_followers
                   FROM mt_poll_queue q
                   JOIN mt_videos v ON v.video_id = q.video_id
                   JOIN mt_creators c ON c.creator_id = v.creator_id
                   WHERE q.due_at <= ? ORDER BY q.due_at ASC LIMIT ?""",
                (isoformat(utc_now()), limit),
            ).fetchall()
        grouped: Dict[str, List[Dict[str, Any]]] = {}
        for row in rows:
            grouped.setdefault(row["platform"], []).append(dict(row))
        return grouped

    def remaining_request_budget(self, source_id: str, daily_limit: int) -> int:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT requests FROM mt_daily_usage WHERE usage_date = ? AND source_id = ?",
                (datetime.now(timezone.utc).date().isoformat(), source_id),
            ).fetchone()
        return max(0, daily_limit - (int(row[0]) if row else 0))

    def daily_provider_cost(self) -> float:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT COALESCE(SUM(estimated_cost_usd), 0) FROM mt_daily_usage WHERE usage_date = ?",
                (datetime.now(timezone.utc).date().isoformat(),),
            ).fetchone()
        return float(row[0])

    def daily_unique_count(self, platform: Optional[str] = None) -> int:
        start = datetime.now(timezone.utc).date().isoformat()
        query = "SELECT COUNT(*) FROM mt_videos WHERE substr(first_seen_at, 1, 10) = ?"
        params: List[Any] = [start]
        if platform:
            query += " AND platform = ?"
            params.append(platform)
        with self.connect() as connection:
            return int(connection.execute(query, params).fetchone()[0])

    def known_external_ids(self, platform: str, external_ids: Sequence[str]) -> set[str]:
        """Return canonical IDs already on tape without exceeding SQLite bind limits."""
        candidates = list(dict.fromkeys(str(value) for value in external_ids if str(value)))
        known: set[str] = set()
        if not candidates:
            return known
        with self.connect() as connection:
            for offset in range(0, len(candidates), 400):
                chunk = candidates[offset:offset + 400]
                placeholders = ",".join("?" for _ in chunk)
                rows = connection.execute(
                    f"SELECT external_id FROM mt_videos WHERE platform = ? AND external_id IN ({placeholders})",
                    [platform, *chunk],
                ).fetchall()
                known.update(str(row["external_id"]) for row in rows)
        return known

    def recent_source_metadata_total(
        self,
        source_id: str,
        metadata_key: str,
        hours: int = 24,
    ) -> int:
        """Sum a numeric receipt field over a conservative rolling quota window."""
        since = isoformat(utc_now() - timedelta(hours=max(1, hours)))
        with self.connect() as connection:
            rows = connection.execute(
                "SELECT metadata_json FROM mt_source_receipts WHERE source_id = ? AND finished_at >= ?",
                (source_id, since),
            ).fetchall()
        total = 0
        for row in rows:
            try:
                total += max(0, int(json.loads(row["metadata_json"]).get(metadata_key, 0)))
            except (TypeError, ValueError, json.JSONDecodeError):
                continue
        return total

    def seconds_since_discovery(self) -> Optional[float]:
        with self.connect() as connection:
            row = connection.execute(
                """SELECT finished_at FROM mt_collection_runs
                   WHERE mode IN ('full', 'discovery') AND state = 'completed' AND finished_at IS NOT NULL
                   ORDER BY finished_at DESC LIMIT 1"""
            ).fetchone()
        if not row:
            return None
        finished = datetime.fromisoformat(str(row[0]).replace("Z", "+00:00"))
        if finished.tzinfo is None:
            finished = finished.replace(tzinfo=timezone.utc)
        return max(0.0, (utc_now() - finished).total_seconds())

    def aggregate_trends(self, observed_at: Optional[datetime] = None, run_id: Optional[str] = None) -> int:
        observed_at = observed_at or utc_now()
        observed = isoformat(observed_at)
        since = isoformat(observed_at - timedelta(hours=1))
        with self.connect() as connection:
            if run_id:
                trends = connection.execute(
                    """SELECT DISTINCT m.trend_id
                       FROM mt_trend_memberships m
                       JOIN mt_market_observations o ON o.video_id = m.video_id
                       WHERE o.run_id = ?""",
                    (run_id,),
                ).fetchall()
            else:
                trends = connection.execute("SELECT trend_id FROM mt_trends").fetchall()
            trend_ids = [str(row["trend_id"]) for row in trends]
            if not trend_ids:
                return 0
            placeholders = ",".join("?" for _ in trend_ids)
            latest_by_trend: Dict[str, List[sqlite3.Row]] = {}
            latest_rows = connection.execute(
                f"""SELECT m.trend_id, v.video_id, v.creator_id, v.platform, v.first_seen_at,
                            o.views, o.likes, o.comments, o.shares, o.view_velocity
                     FROM mt_trend_memberships m
                     JOIN mt_videos v ON v.video_id = m.video_id
                     JOIN mt_market_observations o ON o.observation_id = (
                         SELECT observation_id FROM mt_market_observations latest
                         WHERE latest.video_id = v.video_id
                         ORDER BY latest.observed_at DESC, latest.observation_id DESC LIMIT 1
                     )
                     WHERE m.trend_id IN ({placeholders})""",
                trend_ids,
            ).fetchall()
            for row in latest_rows:
                latest_by_trend.setdefault(str(row["trend_id"]), []).append(row)
            previous_by_trend: Dict[str, List[Dict[str, Any]]] = {}
            previous_rows = connection.execute(
                f"""WITH ranked AS (
                         SELECT trend_id, observed_at, views_total AS views,
                                ROW_NUMBER() OVER (
                                    PARTITION BY trend_id ORDER BY observed_at DESC, trend_observation_id DESC
                                ) AS row_number
                         FROM mt_trend_observations
                         WHERE trend_id IN ({placeholders})
                     )
                     SELECT trend_id, observed_at, views FROM ranked
                     WHERE row_number <= 3 ORDER BY trend_id, observed_at""",
                trend_ids,
            ).fetchall()
            for row in previous_rows:
                previous_by_trend.setdefault(str(row["trend_id"]), []).append({
                    "observed_at": row["observed_at"], "views": row["views"],
                })
            cohort = [float(row[0]) for row in connection.execute(
                """SELECT momentum FROM mt_trend_observations
                   WHERE observed_at >= ? ORDER BY observed_at DESC LIMIT 5000""",
                (isoformat(observed_at - timedelta(days=30)),),
            ).fetchall()]
            inserted = 0
            for trend_id in trend_ids:
                latest = latest_by_trend.get(trend_id, [])
                if not latest:
                    continue
                creators = {row["creator_id"] for row in latest}
                platforms = {row["platform"] for row in latest}
                creator_views: Dict[str, int] = {}
                for row in latest:
                    creator_views[row["creator_id"]] = creator_views.get(row["creator_id"], 0) + int(row["views"])
                velocities = sorted(float(row["view_velocity"]) for row in latest)
                median_velocity = _percentile(velocities, 0.5)
                p90_velocity = _percentile(velocities, 0.9)
                previous = previous_by_trend.get(trend_id, [])
                views_total = sum(int(row["views"]) for row in latest)
                motion = counter_motion(previous + [{"observed_at": observed, "views": views_total}])
                relative = zscore(motion.velocity, cohort)
                new_videos = sum(1 for row in latest if row["first_seen_at"] >= since)
                new_creator_ids = {row["creator_id"] for row in latest if row["first_seen_at"] >= since}
                saturation = min(1.0, len(latest) / 1000.0)
                state = trend_state(relative, motion.acceleration, saturation, len(creators), len(new_creator_ids))
                breadth = min(1.0, len(creators) / max(1, len(latest)))
                platform_breadth = min(1.0, len(platforms) / max(1, len(self.config.platforms)))
                engagement = sum(int(row["likes"]) + int(row["comments"]) + int(row["shares"]) for row in latest)
                strength = trend_strength({
                    "relative_view_velocity": _sigmoid(relative),
                    "acceleration": _sigmoid(motion.acceleration),
                    "creator_adoption_velocity": min(1.0, len(new_creator_ids) / 25.0),
                    "creator_breadth": breadth,
                    "share_velocity": min(1.0, sum(int(row["shares"]) for row in latest) / max(1, views_total) * 20),
                    "cross_platform_diffusion": platform_breadth,
                    "engagement_quality": min(1.0, engagement / max(1, views_total) * 10),
                    "novelty": max(0.0, 1.0 - saturation),
                    "persistence": min(1.0, len(previous) / 3.0),
                })
                connection.execute(
                    """INSERT INTO mt_trend_observations(
                           trend_id, observed_at, videos_total, videos_new_1h, creators_total, creators_new_1h,
                           platforms_total, views_total, likes_total, comments_total, shares_total,
                           median_video_velocity, p90_video_velocity, creator_breadth, platform_breadth,
                           top1_concentration, top10_concentration, momentum, acceleration, relative_strength,
                           saturation, trend_strength, index_version, state
                       ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'trend-strength-v1', ?)""",
                    (
                        trend_id, observed, len(latest), new_videos, len(creators), len(new_creator_ids),
                        len(platforms), views_total, sum(int(row["likes"]) for row in latest),
                        sum(int(row["comments"]) for row in latest), sum(int(row["shares"]) for row in latest),
                        median_velocity, p90_velocity, breadth, platform_breadth,
                        concentration(creator_views.values(), 1), concentration(creator_views.values(), 10),
                        motion.velocity, motion.acceleration, relative, saturation, strength, state,
                    ),
                )
                connection.execute(
                    "UPDATE mt_trends SET status = ?, last_seen_at = ? WHERE trend_id = ?",
                    (state, observed, trend_id),
                )
                inserted += 1
            return inserted

    def _source_backoff_seconds(self, receipt: SourceReceipt, consecutive_failures: int) -> int:
        if receipt.state.value == "blocked_credential":
            return max(1, self.config.source_auth_backoff_seconds)
        if receipt.state.value == "blocked_quota":
            return max(1, self.config.source_quota_backoff_seconds)
        if receipt.state.value == "blocked_approval":
            return max(1, self.config.source_approval_backoff_seconds)
        exponential = self.config.source_failure_backoff_seconds * (2 ** max(0, consecutive_failures - 1))
        return max(1, min(self.config.source_max_backoff_seconds, exponential))

    def source_retry_status(self, source_id: str) -> Dict[str, Any]:
        with self.connect() as connection:
            row = connection.execute(
                """SELECT source_id, platform, state, checked_at, last_success_at,
                          consecutive_failures, next_retry_at, error_code
                   FROM mt_source_health WHERE source_id = ?""",
                (source_id,),
            ).fetchone()
        if row is None:
            return {"source_id": source_id, "blocked": False}
        result = dict(row)
        retry_at = datetime.fromisoformat(result["next_retry_at"]) if result.get("next_retry_at") else None
        result["blocked"] = bool(retry_at and retry_at > utc_now())
        return result

    def create_predictions(self, run_id: str, predicted_at: Optional[datetime] = None) -> int:
        """Persist transparent baseline forecasts; later model versions append new rows."""
        predicted_at = predicted_at or utc_now()
        predicted = isoformat(predicted_at)
        inserted = 0
        with self.connect() as connection:
            video_rows = connection.execute(
                """SELECT o.*, v.published_at
                   FROM mt_market_observations o
                   JOIN mt_videos v ON v.video_id = o.video_id
                   WHERE o.run_id = ? AND o.observation_id = (
                       SELECT MAX(current.observation_id) FROM mt_market_observations current
                       WHERE current.run_id = o.run_id AND current.video_id = o.video_id
                   )""",
                (run_id,),
            ).fetchall()
            for row in video_rows:
                views = max(1, int(row["views"]))
                engagement_rate = (
                    int(row["likes"]) + int(row["comments"]) + int(row["shares"])
                ) / views
                creator_lift = views / max(1, int(row["creator_followers"]))
                features = {
                    "run_id": run_id,
                    "relative_strength": float(row["relative_strength"]),
                    "view_velocity": float(row["view_velocity"]),
                    "view_acceleration": float(row["view_acceleration"]),
                    "engagement_rate": engagement_rate,
                    "creator_lift": creator_lift,
                    "video_age_bucket": row["video_age_bucket"],
                }
                score = (
                    -1.8
                    + 0.7 * features["relative_strength"]
                    + 0.35 * min(8.0, max(-8.0, features["view_velocity"]))
                    + 0.2 * min(8.0, max(-8.0, features["view_acceleration"]))
                    + 1.5 * min(1.0, engagement_rate * 10.0)
                    + 0.45 * min(4.0, creator_lift)
                )
                probability = round(_sigmoid(score), 6)
                peak_hours = max(1.0, 24.0 * (1.0 - probability))
                connection.execute(
                    """INSERT INTO mt_predictions(
                           subject_type, subject_id, model_version, predicted_at, horizon, probability,
                           expected_peak_at, expected_remaining_life_hours, features_json
                       ) VALUES('video', ?, 'transparent-baseline-v1', ?, ?, ?, ?, ?, ?)""",
                    (
                        row["video_id"], predicted, "exceeds_10x_creator_baseline_within_24h", probability,
                        isoformat(predicted_at + timedelta(hours=peak_hours)), round(24.0 + 48.0 * probability, 3),
                        json.dumps(features, sort_keys=True),
                    ),
                )
                inserted += 1

            trend_rows = connection.execute(
                """SELECT trend.* FROM mt_trend_observations trend
                   WHERE trend.trend_id IN (
                       SELECT DISTINCT membership.trend_id
                       FROM mt_trend_memberships membership
                       JOIN mt_market_observations observation
                         ON observation.video_id = membership.video_id
                       WHERE observation.run_id = ?
                   ) AND trend.trend_observation_id = (
                       SELECT MAX(current.trend_observation_id) FROM mt_trend_observations current
                       WHERE current.trend_id = trend.trend_id
                   )""",
                (run_id,),
            ).fetchall()
            for row in trend_rows:
                features = {
                    "run_id": run_id,
                    "trend_strength": float(row["trend_strength"]),
                    "relative_strength": float(row["relative_strength"]),
                    "momentum": float(row["momentum"]),
                    "acceleration": float(row["acceleration"]),
                    "creator_breadth": float(row["creator_breadth"]),
                    "platform_breadth": float(row["platform_breadth"]),
                    "saturation": float(row["saturation"]),
                    "state": row["state"],
                }
                score = (
                    (features["trend_strength"] - 55.0) / 15.0
                    + 0.45 * features["relative_strength"]
                    + 0.2 * min(6.0, max(-6.0, features["acceleration"]))
                    + features["creator_breadth"]
                    + features["platform_breadth"]
                    - 1.5 * features["saturation"]
                )
                probability = round(_sigmoid(score), 6)
                peak_hours = max(0.5, 6.0 * (1.0 - probability))
                connection.execute(
                    """INSERT INTO mt_predictions(
                           subject_type, subject_id, model_version, predicted_at, horizon, probability,
                           expected_peak_at, expected_remaining_life_hours, features_json
                       ) VALUES('trend', ?, 'transparent-baseline-v1', ?, ?, ?, ?, ?, ?)""",
                    (
                        row["trend_id"], predicted, "reaches_breakout_within_6h", probability,
                        isoformat(predicted_at + timedelta(hours=peak_hours)), round(6.0 + 42.0 * probability, 3),
                        json.dumps(features, sort_keys=True),
                    ),
                )
                inserted += 1
        return inserted

    def status(self) -> Dict[str, Any]:
        today = datetime.now(timezone.utc).date().isoformat()
        with self.connect() as connection:
            totals = {
                "creators": connection.execute("SELECT COUNT(*) FROM mt_creators").fetchone()[0],
                "videos": connection.execute("SELECT COUNT(*) FROM mt_videos").fetchone()[0],
                "observations": connection.execute("SELECT COUNT(*) FROM mt_market_observations").fetchone()[0],
                "trends": connection.execute("SELECT COUNT(*) FROM mt_trends").fetchone()[0],
                "trend_observations": connection.execute("SELECT COUNT(*) FROM mt_trend_observations").fetchone()[0],
                "predictions": connection.execute("SELECT COUNT(*) FROM mt_predictions").fetchone()[0],
                "due_polls": connection.execute("SELECT COUNT(*) FROM mt_poll_queue WHERE due_at <= ?", (isoformat(utc_now()),)).fetchone()[0],
            }
            platform_rows = connection.execute(
                """SELECT platform, COUNT(*) AS count FROM mt_videos
                   WHERE substr(first_seen_at, 1, 10) = ? GROUP BY platform""",
                (today,),
            ).fetchall()
            run = connection.execute(
                "SELECT * FROM mt_collection_runs ORDER BY started_at DESC LIMIT 1"
            ).fetchone()
            sources = [dict(row) for row in connection.execute(
                "SELECT * FROM mt_source_health ORDER BY platform, source_id"
            ).fetchall()]
            cost = connection.execute(
                "SELECT COALESCE(SUM(estimated_cost_usd), 0) FROM mt_daily_usage WHERE usage_date = ?",
                (today,),
            ).fetchone()[0]
            outbox_pending = connection.execute(
                "SELECT COUNT(*) FROM mt_sync_outbox WHERE synced_at IS NULL"
            ).fetchone()[0]
            sink = connection.execute(
                "SELECT * FROM mt_sink_health WHERE sink_id = 'supabase'"
            ).fetchone()
        by_platform = {row["platform"]: int(row["count"]) for row in platform_rows}
        target_status = {
            platform: {
                "target": self.config.target_for(platform),
                "acquired": by_platform.get(platform, 0),
                "remaining": max(0, self.config.target_for(platform) - by_platform.get(platform, 0)),
            }
            for platform in self.config.platforms
        }
        acquired = sum(by_platform.values())
        return {
            "schema_version": SCHEMA_VERSION,
            "service": "social-market-tape",
            "state": "running" if run and run["state"] == "running" else "ready",
            "checked_at": isoformat(utc_now()),
            "daemon": self.daemon_health(),
            "database_path": str(self.config.db_path),
            "daily": {
                "date": today,
                "target": self.config.daily_unique_target,
                "acquired": acquired,
                "remaining": max(0, self.config.daily_unique_target - acquired),
                "estimated_provider_cost_usd": round(float(cost), 6),
                "max_provider_cost_usd": self.config.max_daily_provider_cost_usd,
                "platforms": target_status,
            },
            "totals": {key: int(value) for key, value in totals.items()},
            "latest_run": dict(run) if run else None,
            "sources": sources,
            "central_sync": {
                "enabled": self.config.supabase_sync_enabled,
                "pending": int(outbox_pending),
                "health": dict(sink) if sink else None,
            },
        }

    def daemon_health(self) -> Dict[str, Any]:
        try:
            heartbeat = json.loads(self.config.heartbeat_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {
                "state": "starting",
                "heartbeat_at": None,
                "age_seconds": None,
                "supervision_expected": True,
            }
        try:
            heartbeat_at = datetime.fromisoformat(str(heartbeat["heartbeat_at"]).replace("Z", "+00:00"))
            if heartbeat_at.tzinfo is None:
                heartbeat_at = heartbeat_at.replace(tzinfo=timezone.utc)
            age_seconds = max(0.0, (utc_now() - heartbeat_at).total_seconds())
        except (KeyError, TypeError, ValueError):
            age_seconds = None
        stale_after = max(120, self.config.cycle_seconds * 2)
        state = "healthy" if age_seconds is not None and age_seconds <= stale_after else "stale"
        return {
            "state": state,
            "heartbeat_at": heartbeat.get("heartbeat_at"),
            "age_seconds": round(age_seconds, 3) if age_seconds is not None else None,
            "stale_after_seconds": stale_after,
            "last_run_id": heartbeat.get("last_run_id"),
            "last_run_state": heartbeat.get("last_run_state"),
            "pid": heartbeat.get("pid"),
            "supervision_expected": True,
        }

    def list_videos(self, limit: int = 100, platform: Optional[str] = None) -> List[Dict[str, Any]]:
        query = """SELECT v.*, o.views, o.likes, o.comments, o.shares, o.saves,
                          o.view_velocity, o.view_acceleration, o.relative_strength, o.observed_at
                   FROM mt_videos v
                   LEFT JOIN mt_market_observations o ON o.observation_id = (
                       SELECT observation_id FROM mt_market_observations
                       WHERE video_id = v.video_id ORDER BY observed_at DESC LIMIT 1
                   )"""
        params: List[Any] = []
        if platform:
            query += " WHERE v.platform = ?"
            params.append(platform)
        query += " ORDER BY v.last_seen_at DESC LIMIT ?"
        params.append(min(max(1, limit), 1000))
        with self.connect() as connection:
            return [dict(row) for row in connection.execute(query, params).fetchall()]

    def list_trends(self, limit: int = 100, state: Optional[str] = None) -> List[Dict[str, Any]]:
        query = """SELECT t.*, o.* FROM mt_trends t
                   LEFT JOIN mt_trend_observations o ON o.trend_observation_id = (
                       SELECT trend_observation_id FROM mt_trend_observations
                       WHERE trend_id = t.trend_id ORDER BY observed_at DESC LIMIT 1
                   )"""
        params: List[Any] = []
        if state:
            query += " WHERE t.status = ?"
            params.append(state)
        query += " ORDER BY COALESCE(o.trend_strength, 0) DESC, t.last_seen_at DESC LIMIT ?"
        params.append(min(max(1, limit), 1000))
        with self.connect() as connection:
            return [dict(row) for row in connection.execute(query, params).fetchall()]

    def keyword_signals(
        self,
        limit: int = 100,
        window_hours: int = 168,
        min_videos: int = 1,
    ) -> List[Dict[str, Any]]:
        """Mine fresh query candidates without relying on the configured seed vocabulary."""

        return rank_keywords(
            self._keyword_signal_rows(),
            limit=limit,
            window_hours=window_hours,
            min_videos=min_videos,
        )

    def discovery_query_signals(
        self,
        limit: int = 100,
        window_hours: int = 168,
        min_videos: int = 1,
    ) -> List[Dict[str, Any]]:
        """Rank exact discovery queries independently from extracted text fragments."""

        return rank_keywords(
            self._keyword_signal_rows(),
            limit=limit,
            window_hours=window_hours,
            min_videos=min_videos,
            candidate_mode="queries",
        )

    def _keyword_signal_rows(self) -> List[Dict[str, Any]]:
        with self.connect() as connection:
            return [dict(row) for row in connection.execute(
                """WITH latest AS (
                       SELECT observation.*,
                              ROW_NUMBER() OVER (
                                  PARTITION BY observation.video_id
                                  ORDER BY observation.observed_at DESC, observation.observation_id DESC
                              ) AS row_number
                       FROM mt_market_observations observation
                   ), observation_counts AS (
                       SELECT video_id, COUNT(*) AS observation_count
                       FROM mt_market_observations GROUP BY video_id
                   )
                   SELECT video.video_id, video.creator_id, video.platform, video.published_at,
                          video.title, video.caption, video.description, video.url,
                          latest.observed_at, latest.views, latest.likes, latest.comments,
                          latest.shares, latest.view_velocity,
                          genome.hashtags_json, observation_counts.observation_count,
                          COALESCE((
                              SELECT json_group_array(attribution.query)
                              FROM (
                                  SELECT DISTINCT query
                                  FROM mt_discovery_attributions
                                  WHERE video_id = video.video_id AND query != ''
                              ) attribution
                          ), '[]') AS discovery_queries_json
                   FROM latest
                   JOIN mt_videos video ON video.video_id = latest.video_id
                   JOIN observation_counts ON observation_counts.video_id = latest.video_id
                   LEFT JOIN mt_content_genomes genome ON genome.video_id = video.video_id
                   WHERE latest.row_number = 1 AND video.published_at IS NOT NULL
                   ORDER BY latest.observed_at DESC LIMIT 50000"""
            ).fetchall()]

    def list_runs(self, limit: int = 50) -> List[Dict[str, Any]]:
        with self.connect() as connection:
            return [dict(row) for row in connection.execute(
                "SELECT * FROM mt_collection_runs ORDER BY started_at DESC LIMIT ?",
                (min(max(1, limit), 500),),
            ).fetchall()]

    def list_predictions(
        self, limit: int = 100, subject_type: Optional[str] = None
    ) -> List[Dict[str, Any]]:
        query = "SELECT * FROM mt_predictions"
        params: List[Any] = []
        if subject_type:
            query += " WHERE subject_type = ?"
            params.append(subject_type)
        query += " ORDER BY predicted_at DESC, prediction_id DESC LIMIT ?"
        params.append(min(max(1, limit), 1000))
        with self.connect() as connection:
            rows = [dict(row) for row in connection.execute(query, params).fetchall()]
        for row in rows:
            row["features"] = json.loads(row.pop("features_json"))
            if row.get("outcome_json"):
                row["outcome"] = json.loads(row.pop("outcome_json"))
        return rows

    def social_candles(
        self, window_minutes: int = 15, limit: int = 96, platform: Optional[str] = None
    ) -> List[Dict[str, Any]]:
        """Derive OHLC-like social activity windows from immutable cumulative counters."""
        window_minutes = min(1440, max(1, int(window_minutes)))
        limit = min(1000, max(1, int(limit)))
        start = isoformat(utc_now() - timedelta(minutes=window_minutes * (limit + 1)))
        query = """SELECT observation_id, observed_at, video_id, creator_id, platform,
                          views, likes, comments, shares, view_velocity, view_acceleration,
                          relative_strength
                   FROM mt_market_observations WHERE observed_at >= ?"""
        params: List[Any] = [start]
        if platform:
            query += " AND platform = ?"
            params.append(platform)
        query += " ORDER BY video_id, observed_at, observation_id"
        with self.connect() as connection:
            rows = [dict(row) for row in connection.execute(query, params).fetchall()]

        previous: Dict[str, Dict[str, Any]] = {}
        buckets: Dict[int, Dict[str, Any]] = {}
        window_seconds = window_minutes * 60
        for row in rows:
            timestamp = datetime.fromisoformat(str(row["observed_at"]).replace("Z", "+00:00"))
            bucket_epoch = int(timestamp.timestamp()) // window_seconds * window_seconds
            bucket = buckets.setdefault(bucket_epoch, {
                "video_ids": set(), "creator_ids": set(), "platforms": set(), "new_videos": 0,
                "new_views": 0, "new_likes": 0, "new_comments": 0, "new_shares": 0,
                "velocities": [], "accelerations": [], "relative_strengths": [], "ticks": 0,
            })
            prior = previous.get(str(row["video_id"]))
            if prior is None:
                bucket["new_videos"] += 1
            for field, output in (
                ("views", "new_views"), ("likes", "new_likes"),
                ("comments", "new_comments"), ("shares", "new_shares"),
            ):
                if prior is not None:
                    bucket[output] += max(0, int(row[field]) - int(prior[field]))
            bucket["video_ids"].add(row["video_id"])
            bucket["creator_ids"].add(row["creator_id"])
            bucket["platforms"].add(row["platform"])
            bucket["velocities"].append(float(row["view_velocity"]))
            bucket["accelerations"].append(float(row["view_acceleration"]))
            bucket["relative_strengths"].append(float(row["relative_strength"]))
            bucket["ticks"] += 1
            previous[str(row["video_id"])] = row

        output: List[Dict[str, Any]] = []
        for epoch in sorted(buckets)[-limit:]:
            bucket = buckets[epoch]
            velocities = sorted(bucket.pop("velocities"))
            accelerations = bucket.pop("accelerations")
            strengths = bucket.pop("relative_strengths")
            videos = len(bucket.pop("video_ids"))
            creators = len(bucket.pop("creator_ids"))
            platforms = len(bucket.pop("platforms"))
            output.append({
                "window_started_at": isoformat(datetime.fromtimestamp(epoch, tz=timezone.utc)),
                "window_minutes": window_minutes,
                **bucket,
                "videos_observed": videos,
                "creators_observed": creators,
                "platforms_observed": platforms,
                "median_video_velocity": _percentile(velocities, 0.5),
                "p90_video_velocity": _percentile(velocities, 0.9),
                "creator_breadth": min(1.0, creators / max(1, videos)),
                "platform_breadth": min(1.0, platforms / max(1, len(self.config.platforms))),
                "momentum": sum(strengths) / max(1, len(strengths)),
                "acceleration": sum(accelerations) / max(1, len(accelerations)),
            })
        return output

    @staticmethod
    def _hook_type(opening: str) -> str:
        lower = opening.lower()
        if lower.startswith(("how ", "here's how", "here is how")):
            return "how_to"
        if lower.startswith(("why ", "the reason")):
            return "why"
        if lower.startswith(("stop ", "don't ", "do not ")):
            return "warning"
        if lower.startswith(("i ", "we ", "my ")):
            return "personal_proof"
        if "?" in opening:
            return "question"
        return "statement"


def _percentile(values: Sequence[float], percentile: float) -> float:
    if not values:
        return 0.0
    index = min(len(values) - 1, max(0, math.ceil(percentile * len(values)) - 1))
    return float(values[index])


def _sigmoid(value: float) -> float:
    return 1.0 / (1.0 + math.exp(-max(-20.0, min(20.0, value))))


def _select_in(
    connection: sqlite3.Connection, table: str, key: str, values: Sequence[str]
) -> List[Dict[str, Any]]:
    if not values:
        return []
    placeholders = ",".join("?" for _ in values)
    return [dict(row) for row in connection.execute(
        f"SELECT * FROM {table} WHERE {key} IN ({placeholders})", list(values)
    ).fetchall()]
