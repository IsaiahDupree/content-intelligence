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
from .math import age_bucket, concentration, counter_motion, log_velocity, poll_interval_seconds, trend_state, trend_strength, zscore
from .models import MarketContent, QueryAttempt, SourceReceipt, isoformat, stable_hash, utc_now
from .predictor import (
    ENTRY_HORIZON,
    PROGRESSION_HORIZON,
    eligible_for_early_entry,
    load_active_model,
    model_accepts_features,
    model_prediction_horizon,
    model_purpose,
    predict_trend_snapshot,
)


SCHEMA_VERSION = 7
WORD_RE = re.compile(r"[a-z0-9][a-z0-9'+-]*", re.IGNORECASE)
STOP_WORDS = {
    "a", "an", "and", "are", "as", "at", "be", "but", "by", "for", "from", "how",
    "i", "in", "is", "it", "my", "of", "on", "or", "our", "that", "the", "this", "to",
    "was", "we", "what", "when", "where", "why", "with", "you", "your",
}
OPPORTUNITY_CONTRACT = "market_tape_actionable_opportunities_v1"
OPPORTUNITY_RANKER_VERSION = "actionable-opportunity-v6"
TREND_MODEL_ADMISSION_CONTRACT = "market_tape_trend_model_admission_v1"
TREND_INDEX_VERSION = "trend-strength-v2"
TREND_OUTCOME_COVERAGE_TOLERANCE = timedelta(minutes=30)
TREND_OUTCOME_COVERAGE_GRACE = timedelta(hours=2)
ACTIONABLE_TREND_STATES = {"discovering", "emerging", "breakout", "recurring"}
GENERIC_TREND_LABELS = {
    "1", "best tool", "business", "businessgrowth", "clips", "content",
    "creatoreconomy", "digitalmarketing", "edit", "edits", "explore",
    "explorepage", "fact", "facts", "foryou", "foryoupage", "funny",
    "funny moments", "futureofwork",
    "fyp", "fypシ", "gaming", "growth", "instagram", "learning", "longform",
    "love", "most useful", "movies", "music", "new", "news", "breaking news",
    "reel", "reels", "science",
    "short", "shorts", "sports", "taking over", "technology", "tiktok",
    "trending", "video", "video longform", "video short", "viral", "viralshorts",
    "youtube", "youtube shorts", "entertainment",
}
VAGUE_PHRASE_LEADERS = {
    "about", "brand", "does", "dont", "get", "getting", "guys", "hey", "how",
    "instagram", "isn", "just", "last", "not", "people", "really", "saying",
    "more", "someone", "something", "things", "thinking", "threads", "tiktok", "top",
    "shorts", "want", "what", "when", "where", "who", "why", "year", "youtube",
}
VAGUE_PHRASE_TRAILERS = {
    "about", "does", "get", "getting", "guys", "isn", "just", "new", "not",
    "instagram", "people", "really", "saying", "someone", "something", "things",
    "than", "thinking", "threads", "tiktok", "want", "what", "when", "where", "who",
    "why", "year", "youtube",
}
GENERIC_HOOK_TOKENS = {
    "actually", "always", "anybody", "anyone", "believe", "did", "does",
    "doing", "everybody", "everyone", "finally", "gets", "getting", "going",
    "got", "happened", "happens", "just", "know", "knows", "made", "make",
    "makes", "need", "needs", "never", "no", "nobody", "one", "people",
    "really", "somebody", "someone", "thing", "things", "think", "thinks",
    "want", "wanted", "wants", "watch", "you",
}
CONTEXT_BOILERPLATE_TOKENS = {
    "about", "bio", "channel", "com", "connect", "discord", "facebook",
    "follow", "group", "groups", "http", "https", "info", "instagram",
    "join", "link", "member", "members", "profile", "subscribe", "tiktok",
    "twitter", "user", "users", "www", "youtube",
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

                CREATE TABLE IF NOT EXISTS mt_query_attempts (
                    attempt_key TEXT PRIMARY KEY,
                    run_id TEXT NOT NULL,
                    source_id TEXT NOT NULL,
                    platform TEXT NOT NULL,
                    query TEXT NOT NULL,
                    attempted_at TEXT NOT NULL,
                    finished_at TEXT NOT NULL,
                    state TEXT NOT NULL,
                    result_count INTEGER NOT NULL DEFAULT 0,
                    request_count INTEGER NOT NULL DEFAULT 0,
                    error_code TEXT NOT NULL DEFAULT '',
                    error_detail TEXT NOT NULL DEFAULT '',
                    artifact_path TEXT NOT NULL DEFAULT '',
                    artifact_sha256 TEXT NOT NULL DEFAULT '',
                    metadata_json TEXT NOT NULL DEFAULT '{}'
                );

                CREATE INDEX IF NOT EXISTS mt_query_attempts_query_time_idx
                    ON mt_query_attempts(query, attempted_at DESC);
                CREATE INDEX IF NOT EXISTS mt_query_attempts_platform_time_idx
                    ON mt_query_attempts(platform, attempted_at DESC);

                CREATE TRIGGER IF NOT EXISTS mt_query_attempts_no_update
                BEFORE UPDATE ON mt_query_attempts
                BEGIN
                    SELECT RAISE(ABORT, 'query attempts are append-only');
                END;

                CREATE TRIGGER IF NOT EXISTS mt_query_attempts_no_delete
                BEFORE DELETE ON mt_query_attempts
                BEGIN
                    SELECT RAISE(ABORT, 'query attempts are append-only');
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
                    views_new_1h INTEGER NOT NULL DEFAULT 0,
                    likes_new_1h INTEGER NOT NULL DEFAULT 0,
                    comments_new_1h INTEGER NOT NULL DEFAULT 0,
                    shares_new_1h INTEGER NOT NULL DEFAULT 0,
                    counter_delta_videos INTEGER NOT NULL DEFAULT 0,
                    activity_coverage REAL NOT NULL DEFAULT 0,
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

                CREATE INDEX IF NOT EXISTS mt_predictions_subject_time_idx
                    ON mt_predictions(subject_type, subject_id, predicted_at DESC);
                CREATE INDEX IF NOT EXISTS mt_predictions_outcome_idx
                    ON mt_predictions(outcome_json, predicted_at);
                CREATE INDEX IF NOT EXISTS mt_predictions_forecast_lineage_idx
                    ON mt_predictions(
                        subject_type, model_version, horizon, subject_id,
                        predicted_at DESC
                    );

                -- MT-009: calibration metrics are RECORDED after horizons
                -- close, not only computed on demand. Append-only snapshots.
                CREATE TABLE IF NOT EXISTS mt_prediction_calibration (
                    calibration_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    computed_at TEXT NOT NULL,
                    subject_type TEXT NOT NULL,
                    model_version TEXT NOT NULL,
                    horizon TEXT NOT NULL,
                    state TEXT NOT NULL,
                    labels INTEGER NOT NULL,
                    positives INTEGER NOT NULL,
                    brier_score REAL NOT NULL,
                    brier_skill_score REAL,
                    log_loss REAL NOT NULL,
                    expected_calibration_error REAL NOT NULL,
                    roc_auc REAL,
                    calibration_bins_json TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS mt_prediction_calibration_model_idx
                    ON mt_prediction_calibration(model_version, horizon, computed_at DESC);

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
            trend_observation_columns = {
                row[1]
                for row in connection.execute(
                    "PRAGMA table_info(mt_trend_observations)"
                ).fetchall()
            }
            trend_activity_columns = {
                "views_new_1h": "INTEGER NOT NULL DEFAULT 0",
                "likes_new_1h": "INTEGER NOT NULL DEFAULT 0",
                "comments_new_1h": "INTEGER NOT NULL DEFAULT 0",
                "shares_new_1h": "INTEGER NOT NULL DEFAULT 0",
                "counter_delta_videos": "INTEGER NOT NULL DEFAULT 0",
                "activity_coverage": "REAL NOT NULL DEFAULT 0",
            }
            for column, definition in trend_activity_columns.items():
                if column not in trend_observation_columns:
                    connection.execute(
                        f"ALTER TABLE mt_trend_observations ADD COLUMN {column} {definition}"
                    )
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
            for row in connection.execute(
                "SELECT * FROM mt_query_attempts WHERE run_id = ?",
                (run_id,),
            ).fetchall():
                payload = dict(row)
                records.append(("query_attempt", payload["attempt_key"], payload))
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
            for row in connection.execute("SELECT * FROM mt_query_attempts").fetchall():
                payload = dict(row)
                add("query_attempt", payload["attempt_key"], payload)
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

    def save_query_attempts(self, attempts: Sequence[QueryAttempt]) -> int:
        """Persist query coverage even when a provider returned zero content."""
        if not attempts:
            return 0
        inserted = 0
        with self.connect() as connection:
            for attempt in attempts:
                data = attempt.to_dict()
                cursor = connection.execute(
                    """INSERT INTO mt_query_attempts(
                           attempt_key, run_id, source_id, platform, query, attempted_at,
                           finished_at, state, result_count, request_count, error_code,
                           error_detail, artifact_path, artifact_sha256, metadata_json
                       ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                       ON CONFLICT(attempt_key) DO NOTHING""",
                    (
                        data["attempt_key"], attempt.run_id, attempt.source_id,
                        attempt.platform, " ".join(attempt.query.split())[:300],
                        data["attempted_at"], data["finished_at"], attempt.state,
                        max(0, int(attempt.result_count)), max(0, int(attempt.request_count)),
                        attempt.error_code[:100], attempt.error_detail[:1000],
                        attempt.artifact_path, attempt.artifact_sha256,
                        json.dumps(attempt.metadata, sort_keys=True, default=str),
                    ),
                )
                inserted += int(cursor.rowcount == 1)
        return inserted

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
                self._record_discovery_attributions(connection, item, run_id, observed)
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
        context_key = _context_trend_key(item.discovery_context)
        if context_key:
            candidates.append(("topic", context_key, context_key.title(), 0.82))
        if len(words) >= 2:
            key = " ".join(words[:3])
            if _is_specific_trend_phrase(key, "hook"):
                candidates.append(("hook", key, key.title(), 0.72))
        for first, second in list(zip(words[:6], words[1:7]))[:2]:
            if first != second:
                key = f"{first} {second}"
                if _is_specific_trend_phrase(key, "topic"):
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

    def backfill_context_trends(self) -> Dict[str, Any]:
        """Recover topic memberships from immutable discovery context without provider calls."""
        scanned = 0
        eligible = 0
        invalid_context = 0
        trends_inserted = 0
        memberships_inserted = 0
        affected_trend_ids: set[str] = set()
        with self.connect() as connection:
            rows = connection.execute(
                """SELECT attribution_key, video_id, discovered_at, context_json
                   FROM mt_discovery_attributions
                   ORDER BY discovered_at, attribution_key"""
            ).fetchall()
            for row in rows:
                scanned += 1
                try:
                    context = json.loads(str(row["context_json"] or "{}"))
                except (TypeError, ValueError, json.JSONDecodeError):
                    invalid_context += 1
                    continue
                key = _context_trend_key(context)
                if not key:
                    continue
                eligible += 1
                observed = str(row["discovered_at"])
                trend_id = f"trend:topic:{stable_hash(key)[:16]}"
                cursor = connection.execute(
                    """INSERT INTO mt_trends(
                           trend_id, trend_type, canonical_key, display_name,
                           first_seen_at, last_seen_at
                       ) VALUES(?, 'topic', ?, ?, ?, ?)
                       ON CONFLICT(trend_id) DO NOTHING""",
                    (trend_id, key, key.title(), observed, observed),
                )
                trends_inserted += int(cursor.rowcount == 1)
                cursor = connection.execute(
                    """INSERT INTO mt_trend_memberships(
                           trend_id, video_id, confidence, evidence_json, first_seen_at
                       ) VALUES(?, ?, 0.82, ?, ?)
                       ON CONFLICT(trend_id, video_id) DO NOTHING""",
                    (
                        trend_id,
                        str(row["video_id"]),
                        json.dumps({
                            "attribution_key": str(row["attribution_key"]),
                            "contract": "discovery-context-trend-backfill-v1",
                            "type": "topic",
                            "value": key,
                        }, sort_keys=True),
                        observed,
                    ),
                )
                membership_added = int(cursor.rowcount == 1)
                memberships_inserted += membership_added
                if membership_added:
                    affected_trend_ids.add(trend_id)
        return {
            "attributions_scanned": scanned,
            "eligible_attributions": eligible,
            "invalid_context": invalid_context,
            "trends_inserted": trends_inserted,
            "memberships_inserted": memberships_inserted,
            "affected_trend_ids": sorted(affected_trend_ids),
        }

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

    def defer_unchanged_polls(
        self,
        video_ids: Iterable[str],
        delay_seconds: int = 3600,
    ) -> int:
        ids = sorted(set(str(value) for value in video_ids if str(value)))
        if not ids:
            return 0
        due = isoformat(
            utc_now() + timedelta(seconds=max(300, int(delay_seconds)))
        )
        with self.connect() as connection:
            connection.executemany(
                """UPDATE mt_poll_queue
                   SET due_at = ?, last_error_code = 'unchanged_source_snapshot'
                   WHERE video_id = ?""",
                [(due, video_id) for video_id in ids],
            )
        return len(ids)

    def due_polls(self, limit: int) -> Dict[str, List[Dict[str, Any]]]:
        maximum = max(1, int(limit))
        with self.connect() as connection:
            rows = connection.execute(
                """WITH ranked AS (
                       SELECT q.video_id, q.platform, q.external_id,
                              q.preferred_source_id, q.hot_mode, q.due_at,
                              v.published_at, v.title, v.caption, v.description,
                              v.language, v.url, v.thumbnail_url,
                              v.duration_seconds,
                              c.external_id AS creator_external_id,
                              c.handle AS creator_handle,
                              c.display_name AS creator_name,
                              c.followers AS creator_followers,
                              ROW_NUMBER() OVER (
                                  PARTITION BY q.platform ORDER BY q.due_at, q.video_id
                              ) AS platform_rank
                       FROM mt_poll_queue q
                       JOIN mt_videos v ON v.video_id = q.video_id
                       JOIN mt_creators c ON c.creator_id = v.creator_id
                       WHERE q.due_at <= ?
                   )
                   SELECT * FROM ranked
                   WHERE platform_rank <= ?
                   ORDER BY due_at, platform, video_id""",
                (isoformat(utc_now()), maximum),
            ).fetchall()
        available: Dict[str, List[Dict[str, Any]]] = {}
        for row in rows:
            payload = dict(row)
            payload.pop("platform_rank", None)
            available.setdefault(str(row["platform"]), []).append(payload)
        selected: List[Dict[str, Any]] = []
        platforms = sorted(
            available,
            key=lambda platform: (
                str(available[platform][0]["due_at"]),
                platform,
            ),
        )
        while len(selected) < maximum:
            advanced = False
            for platform in platforms:
                if available[platform]:
                    selected.append(available[platform].pop(0))
                    advanced = True
                    if len(selected) >= maximum:
                        break
            if not advanced:
                break
        grouped: Dict[str, List[Dict[str, Any]]] = {}
        for row in selected:
            grouped.setdefault(str(row["platform"]), []).append(row)
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

    def aggregate_trends(
        self,
        observed_at: Optional[datetime] = None,
        run_id: Optional[str] = None,
        trend_ids: Optional[Sequence[str]] = None,
    ) -> int:
        observed_at = observed_at or utc_now()
        if observed_at.tzinfo is None:
            observed_at = observed_at.replace(tzinfo=timezone.utc)
        observed = isoformat(observed_at)
        since = isoformat(observed_at - timedelta(hours=1))
        with self.connect() as connection:
            if trend_ids is not None:
                trend_ids = sorted(set(str(value) for value in trend_ids if str(value)))
            elif run_id:
                trends = connection.execute(
                    """SELECT DISTINCT m.trend_id
                       FROM mt_trend_memberships m
                       JOIN mt_market_observations o ON o.video_id = m.video_id
                       WHERE o.run_id = ?""",
                    (run_id,),
                ).fetchall()
                trend_ids = [str(row["trend_id"]) for row in trends]
            else:
                trends = connection.execute("SELECT trend_id FROM mt_trends").fetchall()
                trend_ids = [str(row["trend_id"]) for row in trends]
            if not trend_ids:
                return 0
            placeholders = ",".join("?" for _ in trend_ids)
            latest_by_trend: Dict[str, List[sqlite3.Row]] = {}
            latest_rows = connection.execute(
                f"""SELECT m.trend_id, v.video_id, v.creator_id, v.platform,
                            v.first_seen_at, v.published_at,
                            o.observed_at, o.views, o.likes, o.comments, o.shares,
                            o.view_velocity, o.view_acceleration,
                            prior.observed_at AS prior_observed_at,
                            prior.views AS prior_views, prior.likes AS prior_likes,
                            prior.comments AS prior_comments,
                            prior.shares AS prior_shares
                     FROM mt_trend_memberships m
                     JOIN mt_videos v ON v.video_id = m.video_id
                     JOIN mt_market_observations o ON o.observation_id = (
                         SELECT observation_id FROM mt_market_observations latest
                         WHERE latest.video_id = v.video_id
                         ORDER BY latest.observed_at DESC, latest.observation_id DESC LIMIT 1
                     )
                     LEFT JOIN mt_market_observations prior
                       ON prior.observation_id = (
                           SELECT previous.observation_id
                           FROM mt_market_observations previous
                           WHERE previous.video_id = v.video_id
                             AND previous.observation_id != o.observation_id
                             AND previous.observed_at <= o.observed_at
                           ORDER BY previous.observed_at DESC,
                                    previous.observation_id DESC LIMIT 1
                       )
                     WHERE m.trend_id IN ({placeholders})""",
                trend_ids,
            ).fetchall()
            for row in latest_rows:
                latest_by_trend.setdefault(str(row["trend_id"]), []).append(row)
            previous_by_trend: Dict[str, List[Dict[str, Any]]] = {}
            previous_rows = connection.execute(
                f"""WITH ranked AS (
                         SELECT trend_id, observed_at, momentum, acceleration,
                                ROW_NUMBER() OVER (
                                    PARTITION BY trend_id ORDER BY observed_at DESC, trend_observation_id DESC
                                ) AS row_number
                         FROM mt_trend_observations
                         WHERE trend_id IN ({placeholders})
                           AND index_version = 'trend-strength-v2'
                     )
                     SELECT trend_id, observed_at, momentum, acceleration FROM ranked
                     WHERE row_number <= 3 ORDER BY trend_id, observed_at""",
                trend_ids,
            ).fetchall()
            for row in previous_rows:
                previous_by_trend.setdefault(str(row["trend_id"]), []).append({
                    "observed_at": row["observed_at"],
                    "momentum": row["momentum"],
                    "acceleration": row["acceleration"],
                })
            cohort = [float(row[0]) for row in connection.execute(
                """SELECT momentum FROM mt_trend_observations
                   WHERE observed_at >= ? AND index_version = 'trend-strength-v2'
                   ORDER BY observed_at DESC LIMIT 5000""",
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
                activity = [_recent_counter_activity(row, observed_at) for row in latest]
                measured_activity = [value for value in activity if value["measured"]]
                velocities = sorted(
                    float(value["view_velocity"])
                    for value in measured_activity
                )
                accelerations = sorted(
                    float(value["view_acceleration"])
                    for value in measured_activity
                )
                median_velocity = _percentile(velocities, 0.5)
                p90_velocity = _percentile(velocities, 0.9)
                p90_acceleration = _percentile(accelerations, 0.9)
                previous = previous_by_trend.get(trend_id, [])
                views_total = sum(int(row["views"]) for row in latest)
                views_new_1h = sum(int(value["views"]) for value in activity)
                likes_new_1h = sum(int(value["likes"]) for value in activity)
                comments_new_1h = sum(int(value["comments"]) for value in activity)
                shares_new_1h = sum(int(value["shares"]) for value in activity)
                activity_coverage = len(measured_activity) / max(1, len(latest))
                relative = zscore(p90_velocity, cohort)
                recently_published = [
                    row for row in latest
                    if _in_observation_window(row["published_at"], observed_at, hours=1)
                ]
                new_videos = len(recently_published)
                new_creator_ids = {row["creator_id"] for row in recently_published}
                saturation = min(1.0, len(latest) / 1000.0)
                state = trend_state(
                    relative,
                    p90_acceleration,
                    saturation,
                    len(creators),
                    len(new_creator_ids),
                )
                breadth = min(1.0, len(creators) / max(1, len(latest)))
                platform_breadth = min(1.0, len(platforms) / max(1, len(self.config.platforms)))
                recent_engagement = likes_new_1h + comments_new_1h + shares_new_1h
                strength = trend_strength({
                    "relative_view_velocity": _sigmoid(relative),
                    "acceleration": _sigmoid(p90_acceleration),
                    "creator_adoption_velocity": min(1.0, len(new_creator_ids) / 25.0),
                    "creator_breadth": breadth,
                    "share_velocity": min(1.0, shares_new_1h / max(1, views_new_1h) * 20),
                    "cross_platform_diffusion": platform_breadth,
                    "engagement_quality": min(1.0, recent_engagement / max(1, views_new_1h) * 10),
                    "novelty": max(0.0, 1.0 - saturation),
                    "persistence": min(1.0, len(previous) / 3.0),
                })
                connection.execute(
                    """INSERT INTO mt_trend_observations(
                           trend_id, observed_at, videos_total, videos_new_1h, creators_total, creators_new_1h,
                           platforms_total, views_total, likes_total, comments_total, shares_total,
                           views_new_1h, likes_new_1h, comments_new_1h, shares_new_1h,
                           counter_delta_videos, activity_coverage,
                           median_video_velocity, p90_video_velocity, creator_breadth, platform_breadth,
                           top1_concentration, top10_concentration, momentum, acceleration, relative_strength,
                           saturation, trend_strength, index_version, state
                       ) VALUES(
                           ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                           ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                           ?, ?, ?, ?, ?, ?, ?, ?, ?, ?
                       )""",
                    (
                        trend_id, observed, len(latest), new_videos, len(creators), len(new_creator_ids),
                        len(platforms), views_total, sum(int(row["likes"]) for row in latest),
                        sum(int(row["comments"]) for row in latest), sum(int(row["shares"]) for row in latest),
                        views_new_1h, likes_new_1h, comments_new_1h, shares_new_1h,
                        len(measured_activity), activity_coverage,
                        median_velocity, p90_velocity, breadth, platform_breadth,
                        concentration(creator_views.values(), 1), concentration(creator_views.values(), 10),
                        p90_velocity, p90_acceleration, relative, saturation, strength,
                        TREND_INDEX_VERSION, state,
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
        active_trend_model = load_active_model(self.config)
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
                    "videos_total": int(row["videos_total"]),
                    "creators_total": int(row["creators_total"]),
                    "saturation": float(row["saturation"]),
                    "state": row["state"],
                    "index_version": row["index_version"],
                    "views_new_1h": int(row["views_new_1h"]),
                    "activity_coverage": float(row["activity_coverage"]),
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
                if eligible_for_early_entry(features):
                    connection.execute(
                        """INSERT INTO mt_predictions(
                               subject_type, subject_id, model_version, predicted_at,
                               horizon, probability, expected_peak_at,
                               expected_remaining_life_hours, features_json
                           ) VALUES('trend', ?, 'transparent-entry-baseline-v3', ?, ?, ?, ?, ?, ?)""",
                        (
                            row["trend_id"], predicted, ENTRY_HORIZON, probability,
                            isoformat(predicted_at + timedelta(hours=peak_hours)),
                            round(6.0 + 42.0 * probability, 3),
                            json.dumps(features, sort_keys=True),
                        ),
                    )
                    inserted += 1
                if (
                    active_trend_model is not None
                    and model_accepts_features(active_trend_model, features)
                ):
                    inference = predict_trend_snapshot(
                        active_trend_model,
                        features,
                    )
                    if inference["state"] == "abstained":
                        continue
                    model_probability = float(inference["probability"])
                    model_peak_hours = max(0.5, 6.0 * (1.0 - model_probability))
                    model_horizon = model_prediction_horizon(active_trend_model)
                    model_features = {
                        **features,
                        "model_purpose": model_purpose(active_trend_model),
                        "training_dataset_sha256": active_trend_model[
                            "training_dataset_sha256"
                        ],
                        "inference_diagnostics": inference["diagnostics"],
                    }
                    connection.execute(
                        """INSERT INTO mt_predictions(
                               subject_type, subject_id, model_version, predicted_at,
                               horizon, probability, expected_peak_at,
                               expected_remaining_life_hours, features_json
                           ) VALUES('trend', ?, ?, ?, ?, ?, ?, ?, ?)""",
                        (
                            row["trend_id"],
                            active_trend_model["model_version"],
                            predicted,
                            model_horizon,
                            model_probability,
                            isoformat(predicted_at + timedelta(hours=model_peak_hours)),
                            round(6.0 + 42.0 * model_probability, 3),
                            json.dumps(model_features, sort_keys=True),
                        ),
                    )
                    inserted += 1
        return inserted

    def forecast_baseline_trends(
        self,
        predicted_at: Optional[datetime] = None,
        limit: int = 50000,
        run_id: str = "",
    ) -> Dict[str, Any]:
        """Create deterministic early-entry forecasts for the current trend index."""
        predicted_at = predicted_at or utc_now()
        predicted = isoformat(predicted_at)
        cutoff = isoformat(predicted_at - timedelta(hours=24))
        inserted_ids: List[int] = []
        skipped_ineligible = 0
        with self.connect() as connection:
            rows = connection.execute(
                """SELECT observation.*
                   FROM mt_trend_observations observation
                   WHERE observation.observed_at >= ?
                     AND observation.index_version = ?
                     AND observation.state != 'dead'
                     AND observation.trend_observation_id = (
                         SELECT MAX(current.trend_observation_id)
                         FROM mt_trend_observations current
                         WHERE current.trend_id = observation.trend_id
                     )
                   ORDER BY observation.trend_strength DESC,
                            observation.observed_at DESC
                   LIMIT ?""",
                (
                    cutoff,
                    TREND_INDEX_VERSION,
                    min(100000, max(1, int(limit))),
                ),
            ).fetchall()
            for row in rows:
                features = {
                    "forecast_source": "transparent_trend_snapshot",
                    "run_id": run_id,
                    "trend_strength": float(row["trend_strength"]),
                    "relative_strength": float(row["relative_strength"]),
                    "momentum": float(row["momentum"]),
                    "acceleration": float(row["acceleration"]),
                    "creator_breadth": float(row["creator_breadth"]),
                    "platform_breadth": float(row["platform_breadth"]),
                    "videos_total": int(row["videos_total"]),
                    "creators_total": int(row["creators_total"]),
                    "saturation": float(row["saturation"]),
                    "state": row["state"],
                    "index_version": row["index_version"],
                    "views_new_1h": int(row["views_new_1h"]),
                    "activity_coverage": float(row["activity_coverage"]),
                }
                if not eligible_for_early_entry(features):
                    skipped_ineligible += 1
                    continue
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
                cursor = connection.execute(
                    """INSERT INTO mt_predictions(
                           subject_type, subject_id, model_version, predicted_at,
                           horizon, probability, expected_peak_at,
                           expected_remaining_life_hours, features_json
                       ) VALUES('trend', ?, 'transparent-entry-baseline-v3', ?, ?, ?, ?, ?, ?)""",
                    (
                        row["trend_id"],
                        predicted,
                        ENTRY_HORIZON,
                        probability,
                        isoformat(predicted_at + timedelta(hours=peak_hours)),
                        round(6.0 + 42.0 * probability, 3),
                        json.dumps(features, sort_keys=True),
                    ),
                )
                inserted_ids.append(int(cursor.lastrowid))
        queued = self.enqueue_prediction_updates(inserted_ids)
        return {
            "state": "completed",
            "model_version": "transparent-entry-baseline-v3",
            "model_purpose": "deterministic_early_entry_baseline",
            "index_version": TREND_INDEX_VERSION,
            "horizon": ENTRY_HORIZON,
            "predicted_at": predicted,
            "predictions_added": len(inserted_ids),
            "skipped_ineligible": skipped_ineligible,
            "outbox_records": queued,
        }

    def forecast_active_trends(
        self,
        predicted_at: Optional[datetime] = None,
        limit: int = 5000,
    ) -> Dict[str, Any]:
        """Apply the promoted model only to fresh, previously unused snapshots.

        The source snapshot must be within the same 30-minute tolerance used to
        prove outcome coverage (and never older than its prediction horizon).
        The exact observation id and timestamp are durable feature lineage, so
        another invocation cannot forecast the same evidence again.
        """
        predicted_at = _as_datetime(predicted_at or utc_now()).astimezone(
            timezone.utc
        )
        active_model = load_active_model(self.config)
        if active_model is None:
            return {
                "state": "no_promoted_model",
                "predictions_added": 0,
                "skipped_insufficient_support": 0,
                "skipped_stale": 0,
                "skipped_duplicate": 0,
            }
        predicted = isoformat(predicted_at)
        inserted_ids: List[int] = []
        skipped_ineligible = 0
        skipped_insufficient_support = 0
        skipped_stale = 0
        skipped_duplicate = 0
        abstentions: List[Dict[str, Any]] = []
        abstention_reasons: Dict[str, int] = {}
        model_horizon = model_prediction_horizon(active_model)
        horizon_hours = _prediction_horizon_hours(model_horizon)
        source_max_age = min(
            TREND_OUTCOME_COVERAGE_TOLERANCE,
            timedelta(hours=max(0.0, horizon_hours)),
        )
        with self.connect() as connection:
            # Serialise the lineage check and inserts. Without this lock, two
            # CLI callers could both observe no prediction and write the same
            # source snapshot.
            connection.execute("BEGIN IMMEDIATE")
            rows = connection.execute(
                """SELECT observation.*
                   FROM mt_trend_observations observation
                   WHERE observation.observed_at <= ?
                     AND observation.state != 'dead'
                     AND observation.trend_observation_id = (
                         SELECT current.trend_observation_id
                         FROM mt_trend_observations current
                         WHERE current.trend_id = observation.trend_id
                           AND current.observed_at <= ?
                         ORDER BY current.observed_at DESC,
                                  current.trend_observation_id DESC
                         LIMIT 1
                     )
                   ORDER BY observation.trend_strength DESC,
                            observation.observed_at DESC
                   LIMIT ?""",
                (
                    predicted,
                    predicted,
                    min(20000, max(1, int(limit))),
                ),
            ).fetchall()
            existing_by_subject: Dict[str, List[Dict[str, Any]]] = {}
            for prediction in connection.execute(
                """SELECT subject_id, predicted_at, features_json
                   FROM mt_predictions
                   WHERE subject_type = 'trend'
                     AND model_version = ?
                     AND horizon = ?""",
                (active_model["model_version"], model_horizon),
            ).fetchall():
                existing_by_subject.setdefault(
                    str(prediction["subject_id"]), []
                ).append(dict(prediction))
            for row in rows:
                source_observed_at = _as_datetime(row["observed_at"])
                source_age = predicted_at - source_observed_at
                if source_age < timedelta(0) or source_age > source_max_age:
                    skipped_stale += 1
                    continue
                if (
                    int(row["videos_total"]) < 2
                    or int(row["creators_total"]) < 2
                ):
                    skipped_insufficient_support += 1
                    continue
                source_observation_id = int(row["trend_observation_id"])
                if _has_source_prediction(
                    existing_by_subject.get(str(row["trend_id"]), []),
                    source_observation_id=source_observation_id,
                    source_observed_at=source_observed_at,
                ):
                    skipped_duplicate += 1
                    continue
                features = {
                    "forecast_source": "active_trend_snapshot",
                    "source_observation_type": "trend_observation",
                    "source_observation_id": source_observation_id,
                    "source_observed_at": row["observed_at"],
                    "source_observation_age_seconds": round(
                        source_age.total_seconds(), 3
                    ),
                    "source_max_age_seconds": round(
                        source_max_age.total_seconds(), 3
                    ),
                    "trend_strength": float(row["trend_strength"]),
                    "relative_strength": float(row["relative_strength"]),
                    "momentum": float(row["momentum"]),
                    "acceleration": float(row["acceleration"]),
                    "creator_breadth": float(row["creator_breadth"]),
                    "videos_total": int(row["videos_total"]),
                    "creators_total": int(row["creators_total"]),
                    "platform_breadth": float(row["platform_breadth"]),
                    "saturation": float(row["saturation"]),
                    "state": row["state"],
                    "index_version": row["index_version"],
                    "views_new_1h": int(row["views_new_1h"]),
                    "activity_coverage": float(row["activity_coverage"]),
                    "training_dataset_sha256": active_model[
                        "training_dataset_sha256"
                    ],
                }
                if not model_accepts_features(active_model, features):
                    skipped_ineligible += 1
                    continue
                features["model_purpose"] = model_purpose(active_model)
                inference = predict_trend_snapshot(active_model, features)
                if inference["state"] == "abstained":
                    reasons = inference["diagnostics"].get("reasons") or [
                        "unspecified"
                    ]
                    for reason in reasons:
                        abstention_reasons[str(reason)] = (
                            abstention_reasons.get(str(reason), 0) + 1
                        )
                    abstentions.append({
                        "trend_id": row["trend_id"],
                        "observed_at": row["observed_at"],
                        "contract": inference["contract"],
                        "state": inference["state"],
                        "probability": inference["probability"],
                        "diagnostics": inference["diagnostics"],
                    })
                    continue
                probability = float(inference["probability"])
                features["inference_diagnostics"] = inference["diagnostics"]
                peak_hours = max(0.5, 6.0 * (1.0 - probability))
                cursor = connection.execute(
                    """INSERT INTO mt_predictions(
                           subject_type, subject_id, model_version, predicted_at,
                           horizon, probability, expected_peak_at,
                           expected_remaining_life_hours, features_json
                       ) VALUES('trend', ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        row["trend_id"],
                        active_model["model_version"],
                        predicted,
                        model_horizon,
                        probability,
                        isoformat(predicted_at + timedelta(hours=peak_hours)),
                        round(6.0 + 42.0 * probability, 3),
                        json.dumps(features, sort_keys=True),
                    ),
                )
                prediction_id = int(cursor.lastrowid)
                inserted_ids.append(prediction_id)
                existing_by_subject.setdefault(str(row["trend_id"]), []).append({
                    "predicted_at": predicted,
                    "features_json": json.dumps(features, sort_keys=True),
                })
        queued = self.enqueue_prediction_updates(inserted_ids)
        return {
            "state": "completed",
            "model_version": active_model["model_version"],
            "model_purpose": model_purpose(active_model),
            "horizon": model_horizon,
            "predicted_at": predicted,
            "predictions_added": len(inserted_ids),
            "skipped_ineligible": skipped_ineligible,
            "skipped_insufficient_support": skipped_insufficient_support,
            "skipped_stale": skipped_stale,
            "skipped_duplicate": skipped_duplicate,
            "source_freshness_policy": {
                "maximum_age_seconds": round(
                    source_max_age.total_seconds(), 3
                ),
                "prediction_horizon_hours": horizon_hours,
                "coverage_tolerance_seconds": round(
                    TREND_OUTCOME_COVERAGE_TOLERANCE.total_seconds(), 3
                ),
                "future_observations_allowed": False,
                "minimum_videos": 2,
                "minimum_creators": 2,
            },
            "abstained_out_of_distribution": len(abstentions),
            "abstention_reasons": abstention_reasons,
            "abstentions": abstentions,
            "outbox_records": queued,
        }

    def evaluate_predictions(self, as_of: Optional[datetime] = None) -> Dict[str, Any]:
        """Attach measured outcomes or terminal, non-binary coverage receipts.

        Trend forecasts without sufficient future tape remain pending through
        a two-hour grace window. Once it closes they become unscorable, never a
        guessed negative label, so calibration continues to use measured
        binary outcomes only.
        """
        as_of = _as_datetime(as_of or utc_now()).astimezone(timezone.utc)
        pending: List[Dict[str, Any]] = []
        with self.connect() as connection:
            pending = [dict(row) for row in connection.execute(
                """SELECT * FROM mt_predictions
                   WHERE outcome_json IS NULL AND predicted_at <= ?
                   ORDER BY predicted_at, prediction_id""",
                (isoformat(as_of),),
            ).fetchall()]
            video_ids = sorted({
                str(row["subject_id"])
                for row in pending
                if row["subject_type"] == "video"
            })
            trend_ids = sorted({
                str(row["subject_id"])
                for row in pending
                if row["subject_type"] == "trend"
            })
            video_observations = _grouped_rows(
                connection,
                """SELECT video_id AS subject_id, observed_at, views, creator_followers
                   FROM mt_market_observations WHERE video_id IN ({placeholders})
                   ORDER BY video_id, observed_at, observation_id""",
                video_ids,
            )
            trend_observations = _grouped_rows(
                connection,
                """SELECT trend_id AS subject_id, observed_at, state, trend_strength
                   FROM mt_trend_observations WHERE trend_id IN ({placeholders})
                   ORDER BY trend_id, observed_at, trend_observation_id""",
                trend_ids,
            )

            updates: List[Tuple[str, int]] = []
            pending_due = 0
            unscorable = 0
            missing_future_trend_coverage = 0
            for prediction in pending:
                predicted_at = _as_datetime(prediction["predicted_at"])
                horizon_hours = _prediction_horizon_hours(str(prediction["horizon"]))
                target_at = predicted_at + timedelta(hours=horizon_hours)
                if as_of < target_at:
                    continue
                pending_due += 1
                outcome: Optional[Dict[str, Any]] = None
                if prediction["subject_type"] == "video":
                    rows = video_observations.get(str(prediction["subject_id"]), [])
                    baseline = [row for row in rows if _as_datetime(row["observed_at"]) <= predicted_at]
                    followers = int(baseline[-1]["creator_followers"]) if baseline else 0
                    follow_up = next((
                        row for row in rows
                        if target_at <= _as_datetime(row["observed_at"]) <= target_at + timedelta(hours=2)
                    ), None)
                    if followers <= 0:
                        outcome = {
                            "state": "unscorable",
                            "reason": "missing_creator_follower_baseline",
                            "evaluated_at": isoformat(as_of),
                            "target_at": isoformat(target_at),
                        }
                        unscorable += 1
                    elif follow_up is not None:
                        threshold = followers * 10
                        outcome = {
                            "state": "scored",
                            "actual": int(int(follow_up["views"]) >= threshold),
                            "observed_views": int(follow_up["views"]),
                            "threshold_views": threshold,
                            "follow_up_at": follow_up["observed_at"],
                            "evaluated_at": isoformat(as_of),
                            "target_at": isoformat(target_at),
                        }
                else:
                    rows = trend_observations.get(str(prediction["subject_id"]), [])
                    baseline = [
                        row for row in rows
                        if _as_datetime(row["observed_at"]) <= predicted_at
                    ]
                    baseline_row = baseline[-1] if baseline else None
                    baseline_hot = bool(
                        baseline_row
                        and (
                            str(baseline_row["state"]).casefold()
                            in {"breakout", "expanding", "saturating"}
                            or float(baseline_row["trend_strength"]) >= 70.0
                        )
                    )
                    if prediction["horizon"] == ENTRY_HORIZON and baseline_row is None:
                        outcome = {
                            "state": "unscorable",
                            "reason": "missing_pre_breakout_baseline",
                            "evaluated_at": isoformat(as_of),
                            "target_at": isoformat(target_at),
                        }
                        unscorable += 1
                    elif prediction["horizon"] == ENTRY_HORIZON and baseline_hot:
                        outcome = {
                            "state": "unscorable",
                            "reason": "already_breakout_at_prediction",
                            "initial_state": str(baseline_row["state"]),
                            "initial_trend_strength": float(baseline_row["trend_strength"]),
                            "evaluated_at": isoformat(as_of),
                            "target_at": isoformat(target_at),
                        }
                        unscorable += 1
                    window = [
                        row for row in rows
                        if predicted_at < _as_datetime(row["observed_at"]) <= target_at
                    ]
                    coverage = bool(
                        window
                        and _as_datetime(window[-1]["observed_at"])
                        >= target_at - TREND_OUTCOME_COVERAGE_TOLERANCE
                    )
                    if outcome is None and coverage:
                        breakout_states = {"breakout", "expanding", "saturating"}
                        future_breakout = any(
                            str(row["state"]).casefold() in breakout_states
                            or float(row["trend_strength"]) >= 70.0
                            for row in window
                        )
                        actual = (
                            baseline_hot or future_breakout
                            if prediction["horizon"] == PROGRESSION_HORIZON
                            else future_breakout
                        )
                        outcome = {
                            "state": "scored",
                            "actual": int(actual),
                            "observations_in_horizon": len(window),
                            "max_trend_strength": round(max(
                                float(row["trend_strength"]) for row in window
                            ), 6),
                            "terminal_state": str(window[-1]["state"]),
                            "initial_state": (
                                str(baseline_row["state"])
                                if baseline_row is not None else None
                            ),
                            "initial_trend_strength": (
                                float(baseline_row["trend_strength"])
                                if baseline_row is not None else None
                            ),
                            "follow_up_at": window[-1]["observed_at"],
                            "evaluated_at": isoformat(as_of),
                            "target_at": isoformat(target_at),
                        }
                    elif (
                        outcome is None
                        and as_of >= target_at + TREND_OUTCOME_COVERAGE_GRACE
                    ):
                        observations_during_grace = [
                            row for row in rows
                            if predicted_at < _as_datetime(row["observed_at"])
                            <= target_at + TREND_OUTCOME_COVERAGE_GRACE
                        ]
                        outcome = {
                            "state": "unscorable",
                            "reason": "missing_future_trend_coverage",
                            "observations_in_horizon": len(window),
                            "observations_during_grace": len(
                                observations_during_grace
                            ),
                            "latest_follow_up_at": (
                                observations_during_grace[-1]["observed_at"]
                                if observations_during_grace
                                else None
                            ),
                            "required_terminal_observation_at_or_after": isoformat(
                                target_at - TREND_OUTCOME_COVERAGE_TOLERANCE
                            ),
                            "coverage_grace_closed_at": isoformat(
                                target_at + TREND_OUTCOME_COVERAGE_GRACE
                            ),
                            "evaluated_at": isoformat(as_of),
                            "target_at": isoformat(target_at),
                        }
                        unscorable += 1
                        missing_future_trend_coverage += 1
                if outcome is not None:
                    updates.append((json.dumps(outcome, sort_keys=True), int(prediction["prediction_id"])))

            if updates:
                connection.executemany(
                    "UPDATE mt_predictions SET outcome_json = ? WHERE prediction_id = ?",
                    updates,
                )

        updated_ids = [prediction_id for _, prediction_id in updates]
        queued = self.enqueue_prediction_updates(updated_ids)
        report = self.prediction_backtest()
        report.update({
            "evaluated_at": isoformat(as_of),
            "pending_due": pending_due - len(updates),
            "newly_labeled": len(updates),
            "newly_unscorable": unscorable,
            "newly_missing_future_trend_coverage": (
                missing_future_trend_coverage
            ),
            "trend_coverage_grace_hours": round(
                TREND_OUTCOME_COVERAGE_GRACE.total_seconds() / 3600.0,
                3,
            ),
            "outbox_records": queued,
        })
        if updates:
            # MT-009: once new horizons close, record the calibration snapshot.
            report["calibration"] = self.record_calibration()
        return report

    def enqueue_prediction_updates(self, prediction_ids: Sequence[int]) -> int:
        ids = sorted({int(value) for value in prediction_ids})
        if not ids:
            return 0
        created_at = isoformat(utc_now())
        with self.connect() as connection:
            rows = _select_in(connection, "mt_predictions", "prediction_id", ids)
            for row in rows:
                payload = dict(row)
                payload.pop("prediction_id", None)
                key = _prediction_key(payload)
                payload["prediction_key"] = key
                connection.execute(
                    """INSERT INTO mt_sync_outbox(
                           entity_type, entity_key, payload_json, created_at, next_attempt_at
                       ) VALUES('prediction', ?, ?, ?, ?)
                       ON CONFLICT(entity_type, entity_key) DO UPDATE SET
                           payload_json = excluded.payload_json,
                           next_attempt_at = excluded.next_attempt_at,
                           synced_at = NULL,
                           error_detail = ''""",
                    (key, json.dumps(payload, sort_keys=True), created_at, created_at),
                )
        return len(rows)

    def prediction_backtest(self) -> Dict[str, Any]:
        with self.connect() as connection:
            rows = [dict(row) for row in connection.execute(
                """SELECT subject_type, model_version, horizon, probability, outcome_json
                   FROM mt_predictions WHERE outcome_json IS NOT NULL"""
            ).fetchall()]
            pending = int(connection.execute(
                "SELECT COUNT(*) FROM mt_predictions WHERE outcome_json IS NULL"
            ).fetchone()[0])
        grouped: Dict[Tuple[str, str, str], List[Tuple[float, int]]] = {}
        unscorable = 0
        unscorable_by_reason: Dict[str, int] = {}
        for row in rows:
            try:
                outcome = json.loads(row["outcome_json"])
            except (TypeError, ValueError, json.JSONDecodeError):
                continue
            if outcome.get("state") != "scored":
                unscorable += 1
                reason = str(
                    outcome.get("reason")
                    or outcome.get("state")
                    or "unspecified"
                )
                unscorable_by_reason[reason] = (
                    unscorable_by_reason.get(reason, 0) + 1
                )
                continue
            key = (str(row["subject_type"]), str(row["model_version"]), str(row["horizon"]))
            grouped.setdefault(key, []).append((
                max(0.0, min(1.0, float(row["probability"]))),
                int(bool(outcome.get("actual"))),
            ))
        models = []
        for (subject_type, model_version, horizon), values in sorted(grouped.items()):
            probabilities = [value[0] for value in values]
            actuals = [value[1] for value in values]
            labels = len(values)
            positive_rate = sum(actuals) / labels
            brier = sum((probability - actual) ** 2 for probability, actual in values) / labels
            baseline_brier = positive_rate * (1.0 - positive_rate)
            log_loss = -sum(
                actual * math.log(max(1e-9, probability))
                + (1 - actual) * math.log(max(1e-9, 1.0 - probability))
                for probability, actual in values
            ) / labels
            calibration_bins = _calibration_bins(values)
            ece = sum(
                bucket["count"] / labels * abs(bucket["mean_probability"] - bucket["positive_rate"])
                for bucket in calibration_bins
            )
            has_two_classes = 0 < sum(actuals) < labels
            enough_labels = labels >= max(1, self.config.prediction_min_backtest_labels)
            skill = (
                1.0 - brier / baseline_brier
                if baseline_brier > 0
                else None
            )
            if not enough_labels or not has_two_classes:
                state = "collecting_labels"
            elif skill is not None and skill > 0:
                state = "validated"
            else:
                state = "measured_not_validated"
            models.append({
                "subject_type": subject_type,
                "model_version": model_version,
                "horizon": horizon,
                "state": state,
                "labels": labels,
                "positives": sum(actuals),
                "positive_rate": round(positive_rate, 6),
                "mean_probability": round(sum(probabilities) / labels, 6),
                "brier_score": round(brier, 6),
                "baseline_brier_score": round(baseline_brier, 6),
                "brier_skill_score": round(skill, 6) if skill is not None else None,
                "log_loss": round(log_loss, 6),
                "accuracy_at_0_5": round(sum(
                    int((probability >= 0.5) == bool(actual))
                    for probability, actual in values
                ) / labels, 6),
                "roc_auc": _roc_auc(values),
                "expected_calibration_error": round(ece, 6),
                "calibration_bins": calibration_bins,
            })
        return {
            "state": (
                "validated"
                if models and all(model["state"] == "validated" for model in models)
                else "collecting_or_unvalidated"
            ),
            "minimum_labels": self.config.prediction_min_backtest_labels,
            "scored_labels": sum(model["labels"] for model in models),
            "unscorable": unscorable,
            "unscorable_by_reason": unscorable_by_reason,
            "pending": pending,
            "models": models,
        }

    def record_calibration(self) -> Dict[str, Any]:
        """MT-009: persist the current backtest as an append-only calibration
        snapshot per (subject_type, model_version, horizon). Observed outcomes
        are already written by evaluate_predictions once a horizon closes;
        this records the resulting calibration metrics alongside them."""
        backtest = self.prediction_backtest()
        computed_at = isoformat(datetime.now(timezone.utc))
        recorded = 0
        with self.connect() as connection:
            for model in backtest["models"]:
                connection.execute(
                    """INSERT INTO mt_prediction_calibration(
                           computed_at, subject_type, model_version, horizon, state,
                           labels, positives, brier_score, brier_skill_score, log_loss,
                           expected_calibration_error, roc_auc, calibration_bins_json)
                       VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (
                        computed_at, model["subject_type"], model["model_version"],
                        model["horizon"], model["state"], model["labels"],
                        model["positives"], model["brier_score"],
                        model["brier_skill_score"], model["log_loss"],
                        model["expected_calibration_error"], model["roc_auc"],
                        json.dumps(model["calibration_bins"]),
                    ),
                )
                recorded += 1
            connection.commit()
        return {"computed_at": computed_at, "recorded": recorded,
                "state": backtest["state"], "scored_labels": backtest["scored_labels"]}

    def calibration_history(self, limit: int = 50) -> List[Dict[str, Any]]:
        with self.connect() as connection:
            rows = connection.execute(
                """SELECT * FROM mt_prediction_calibration
                   ORDER BY computed_at DESC, calibration_id DESC LIMIT ?""",
                (limit,),
            ).fetchall()
        history = []
        for row in rows:
            item = dict(row)
            item["calibration_bins"] = json.loads(item.pop("calibration_bins_json"))
            history.append(item)
        return history

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
                "query_attempts": connection.execute("SELECT COUNT(*) FROM mt_query_attempts").fetchone()[0],
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

    def list_query_attempts(
        self,
        limit: int = 100,
        platform: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        query = "SELECT * FROM mt_query_attempts"
        params: List[Any] = []
        if platform:
            query += " WHERE platform = ?"
            params.append(platform)
        query += " ORDER BY attempted_at DESC, query LIMIT ?"
        params.append(min(max(1, limit), 5000))
        with self.connect() as connection:
            rows = [dict(row) for row in connection.execute(query, params).fetchall()]
        for row in rows:
            row["metadata"] = json.loads(row.pop("metadata_json"))
        return rows

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

    def trend_opportunities(
        self,
        limit: int = 100,
        max_saturation: float = 0.75,
        min_videos: int = 2,
        min_measured_videos: int = 2,
        candidate_scan_limit: Optional[int] = None,
    ) -> Dict[str, Any]:
        """Rank specific, evidenced trends without conflating rank with probability."""
        active_model = load_active_model(self.config)
        generated_at = utc_now()
        maximum = min(500, max(1, int(limit)))
        saturation_ceiling = min(1.0, max(0.0, float(max_saturation)))
        minimum_videos = min(10000, max(1, int(min_videos)))
        minimum_measured_videos = min(
            10000,
            max(1, int(min_measured_videos)),
        )
        bounded_candidate_scan = min(
            5000,
            max(
                500,
                maximum * 20,
                int(candidate_scan_limit or 0),
            ),
        )
        cutoff = isoformat(generated_at - timedelta(hours=24))
        generated = isoformat(generated_at)
        active_horizon = (
            model_prediction_horizon(active_model)
            if active_model is not None
            else ""
        )
        coarse_suppressed: Dict[str, int] = {}
        coarse_candidates_considered = 0
        with self.connect() as connection:
            model_admission = _prospective_model_admission(
                connection,
                active_model=active_model,
                horizon=active_horizon,
                minimum_labels=max(
                    1, self.config.prediction_min_backtest_labels
                ),
                minimum_class_labels=max(
                    2, self.config.prediction_min_positive_labels
                ),
            )
            coarse = dict(connection.execute(
                """SELECT COUNT(*) AS candidates_considered,
                          SUM(CASE WHEN observation.index_version != :index_version
                              THEN 1 ELSE 0 END) AS stale_index_version,
                          SUM(CASE WHEN observation.index_version = :index_version
                                        AND lower(trend.trend_type) = 'format'
                              THEN 1 ELSE 0 END) AS format_aggregate,
                          SUM(CASE WHEN observation.index_version = :index_version
                                        AND lower(trend.trend_type) != 'format'
                                        AND lower(observation.state) NOT IN (
                                            'discovering', 'emerging', 'breakout', 'recurring'
                                        )
                              THEN 1 ELSE 0 END) AS non_actionable_state,
                          SUM(CASE WHEN observation.index_version = :index_version
                                        AND lower(trend.trend_type) != 'format'
                                        AND lower(observation.state) IN (
                                            'discovering', 'emerging', 'breakout', 'recurring'
                                        )
                                        AND observation.saturation > :saturation
                              THEN 1 ELSE 0 END) AS above_saturation_ceiling,
                          SUM(CASE WHEN observation.index_version = :index_version
                                        AND lower(trend.trend_type) != 'format'
                                        AND lower(observation.state) IN (
                                            'discovering', 'emerging', 'breakout', 'recurring'
                                        )
                                        AND observation.saturation <= :saturation
                                        AND observation.videos_total < :min_videos
                              THEN 1 ELSE 0 END) AS insufficient_video_evidence,
                          SUM(CASE WHEN observation.index_version = :index_version
                                        AND lower(trend.trend_type) != 'format'
                                        AND lower(observation.state) IN (
                                            'discovering', 'emerging', 'breakout', 'recurring'
                                        )
                                        AND observation.saturation <= :saturation
                                        AND observation.videos_total >= :min_videos
                                        AND observation.counter_delta_videos < :min_measured
                              THEN 1 ELSE 0 END) AS insufficient_measured_activity,
                          SUM(CASE WHEN observation.index_version = :index_version
                                        AND lower(trend.trend_type) != 'format'
                                        AND lower(observation.state) IN (
                                            'discovering', 'emerging', 'breakout', 'recurring'
                                        )
                                        AND observation.saturation <= :saturation
                                        AND observation.videos_total >= :min_videos
                                        AND observation.counter_delta_videos >= :min_measured
                              THEN 1 ELSE 0 END) AS coarse_eligible_candidates
                   FROM mt_trends trend
                   JOIN mt_trend_observations observation
                     ON observation.trend_observation_id = (
                         SELECT current.trend_observation_id
                         FROM mt_trend_observations current
                         WHERE current.trend_id = trend.trend_id
                           AND current.observed_at >= :cutoff
                         ORDER BY current.observed_at DESC,
                                  current.trend_observation_id DESC
                         LIMIT 1
                     )""",
                {
                    "cutoff": cutoff,
                    "index_version": TREND_INDEX_VERSION,
                    "saturation": saturation_ceiling,
                    "min_videos": minimum_videos,
                    "min_measured": minimum_measured_videos,
                },
            ).fetchone())
            coarse_candidates_considered = int(
                coarse.pop("candidates_considered") or 0
            )
            coarse_eligible_candidates = int(
                coarse.pop("coarse_eligible_candidates") or 0
            )
            coarse_suppressed = {
                reason: int(count)
                for reason, count in coarse.items()
                if int(count or 0) > 0
            }
            rows = [dict(row) for row in connection.execute(
                """SELECT trend.trend_id, trend.trend_type, trend.canonical_key,
                          trend.display_name, trend.first_seen_at, trend.last_seen_at,
                          observation.observed_at, observation.videos_total,
                          observation.videos_new_1h, observation.creators_total,
                          observation.creators_new_1h, observation.platforms_total,
                          observation.views_total, observation.likes_total,
                          observation.comments_total, observation.shares_total,
                          observation.views_new_1h, observation.likes_new_1h,
                          observation.comments_new_1h, observation.shares_new_1h,
                          observation.counter_delta_videos,
                          observation.activity_coverage,
                          observation.median_video_velocity,
                          observation.p90_video_velocity,
                          observation.creator_breadth, observation.platform_breadth,
                          observation.top1_concentration, observation.top10_concentration,
                          observation.momentum, observation.acceleration,
                          observation.relative_strength, observation.saturation,
                          observation.trend_strength, observation.index_version,
                          observation.state, prediction.model_version,
                          prediction.predicted_at, prediction.horizon,
                          prediction.probability, prediction.expected_peak_at,
                          prediction.expected_remaining_life_hours
                   FROM mt_trends trend
                   JOIN mt_trend_observations observation
                     ON observation.trend_observation_id = (
                         SELECT current.trend_observation_id
                         FROM mt_trend_observations current
                         WHERE current.trend_id = trend.trend_id
                           AND current.observed_at >= ?
                         ORDER BY current.observed_at DESC,
                                  current.trend_observation_id DESC
                         LIMIT 1
                     )
                   LEFT JOIN mt_predictions prediction
                     ON prediction.prediction_id = (
                         SELECT current.prediction_id
                         FROM mt_predictions current
                         WHERE current.subject_type = 'trend'
                           AND current.subject_id = trend.trend_id
                           AND current.model_version = ?
                           AND current.horizon = ?
                           AND current.predicted_at >= ?
                           AND current.predicted_at <= ?
                         ORDER BY current.predicted_at DESC,
                                  current.prediction_id DESC
                         LIMIT 1
                     )
                   WHERE observation.index_version = ?
                     AND lower(trend.trend_type) != 'format'
                     AND lower(observation.state) IN (
                         'discovering', 'emerging', 'breakout', 'recurring'
                     )
                     AND observation.saturation <= ?
                     AND observation.videos_total >= ?
                     AND observation.counter_delta_videos >= ?
                   ORDER BY observation.trend_strength DESC,
                            observation.videos_total DESC,
                            observation.observed_at DESC,
                            trend.trend_id ASC
                   LIMIT ?""",
                (
                    cutoff,
                    str(active_model.get("model_version") or "")
                    if active_model is not None else "",
                    active_horizon,
                    cutoff,
                    generated,
                    TREND_INDEX_VERSION,
                    saturation_ceiling,
                    minimum_videos,
                    minimum_measured_videos,
                    bounded_candidate_scan,
                ),
            ).fetchall()]

        candidates: List[Dict[str, Any]] = []
        suppressed: Dict[str, int] = dict(coarse_suppressed)
        purpose = model_purpose(active_model) if active_model is not None else "none"
        model_index_version = str(
            ((active_model or {}).get("training") or {}).get("index_version")
            or "trend-strength-v1"
        )
        artifact_index_compatible = bool(
            active_model is not None
            and model_index_version == TREND_INDEX_VERSION
        )
        unexpired_predictions = sum(
            int(_prediction_is_unexpired(row, generated_at))
            for row in rows
            if row.get("model_version") and row.get("probability") is not None
        )
        model_ready = bool(
            artifact_index_compatible
            and model_admission["prospective_validation_passed"]
            and unexpired_predictions > 0
        )
        model_admission.update({
            "artifact_index_compatible": artifact_index_compatible,
            "unexpired_predictions": unexpired_predictions,
            "admitted_for_ranking": model_ready,
        })
        if not artifact_index_compatible and active_model is not None:
            model_admission["admission_reason"] = "index_version_mismatch"
        elif (
            model_admission["prospective_validation_passed"]
            and unexpired_predictions == 0
        ):
            model_admission["admission_reason"] = "no_unexpired_predictions"
        if model_ready and purpose == "early_breakout_entry":
            weights = {
                "model_probability": 0.25,
                "trend_strength": 0.10,
                "relative_strength": 0.08,
                "momentum": 0.08,
                "acceleration": 0.07,
                "breadth": 0.10,
                "unsaturated": 0.05,
                "evidence_reliability": 0.07,
                "activity_coverage": 0.08,
                "activity_volume": 0.12,
            }
        elif model_ready:
            weights = {
                "model_probability": 0.05,
                "trend_strength": 0.12,
                "relative_strength": 0.12,
                "momentum": 0.12,
                "acceleration": 0.10,
                "breadth": 0.12,
                "unsaturated": 0.08,
                "evidence_reliability": 0.08,
                "activity_coverage": 0.08,
                "activity_volume": 0.13,
            }
        else:
            weights = {
                "model_probability": 0.0,
                "trend_strength": 0.13,
                "relative_strength": 0.13,
                "momentum": 0.13,
                "acceleration": 0.08,
                "breadth": 0.13,
                "unsaturated": 0.08,
                "evidence_reliability": 0.10,
                "activity_coverage": 0.10,
                "activity_volume": 0.12,
            }
        for row in rows:
            reason = _opportunity_exclusion_reason(
                row,
                saturation_ceiling=saturation_ceiling,
                minimum_videos=minimum_videos,
                minimum_measured_videos=minimum_measured_videos,
            )
            if reason:
                suppressed[reason] = suppressed.get(reason, 0) + 1
                continue
            prediction_available = bool(
                model_ready
                and row.get("model_version")
                and row.get("probability") is not None
                and _prediction_is_unexpired(row, generated_at)
            )
            probability = (
                min(1.0, max(0.0, float(row["probability"])))
                if prediction_available else 0.0
            )
            strength = min(1.0, max(0.0, float(row["trend_strength"]) / 70.0))
            activity_coverage = min(
                1.0,
                max(0.0, float(row["activity_coverage"])),
            )
            activity_reliability = math.sqrt(activity_coverage)
            activity_volume = min(
                1.0,
                math.log1p(max(0, int(row["views_new_1h"])))
                / math.log1p(100000),
            )
            relative = _centered_signal(
                float(row["relative_strength"])
            ) * activity_reliability
            momentum = _centered_signal(
                float(row["momentum"])
            ) * activity_reliability
            acceleration = _centered_signal(
                float(row["acceleration"])
            ) * activity_reliability
            breadth = (
                min(1.0, max(0.0, float(row["creator_breadth"])))
                + min(1.0, max(0.0, float(row["platform_breadth"])))
            ) / 2.0
            unsaturated = 1.0 - min(1.0, max(0.0, float(row["saturation"])))
            evidence_reliability = (
                min(1.0, math.log1p(int(row["videos_total"])) / math.log1p(50))
                + min(1.0, int(row["creators_total"]) / 10.0)
                + min(1.0, int(row["platforms_total"]) / 3.0)
            ) / 3.0
            components = {
                "model_probability": round(probability, 6),
                "trend_strength": round(strength, 6),
                "relative_strength": round(relative, 6),
                "momentum": round(momentum, 6),
                "acceleration": round(acceleration, 6),
                "breadth": round(breadth, 6),
                "unsaturated": round(unsaturated, 6),
                "evidence_reliability": round(evidence_reliability, 6),
                "activity_coverage": round(activity_coverage, 6),
                "activity_volume": round(activity_volume, 6),
            }
            score = 100.0 * sum(
                weights[name] * components[name] for name in weights
            )
            candidates.append({
                "trend_id": row["trend_id"],
                "trend_type": row["trend_type"],
                "display_name": row["display_name"],
                "state": row["state"],
                "opportunity_class": _opportunity_class(str(row["state"])),
                "evidence_grade": _opportunity_evidence_grade(row),
                "opportunity_score": round(score, 3),
                "ranking_components": components,
                "prediction": ({
                    "model_version": row["model_version"],
                    "model_purpose": purpose,
                    "horizon": row["horizon"],
                    "probability": probability,
                    "predicted_at": row["predicted_at"],
                    "expected_peak_at": row["expected_peak_at"],
                    "expected_remaining_life_hours": row[
                        "expected_remaining_life_hours"
                    ],
                } if prediction_available else None),
                "signals": {
                    "index_version": row["index_version"],
                    "trend_strength": float(row["trend_strength"]),
                    "relative_strength": float(row["relative_strength"]),
                    "momentum": float(row["momentum"]),
                    "acceleration": float(row["acceleration"]),
                    "saturation": float(row["saturation"]),
                    "median_video_velocity": float(row["median_video_velocity"]),
                    "p90_video_velocity": float(row["p90_video_velocity"]),
                },
                "evidence": {
                    "videos_total": int(row["videos_total"]),
                    "videos_new_1h": int(row["videos_new_1h"]),
                    "creators_total": int(row["creators_total"]),
                    "creators_new_1h": int(row["creators_new_1h"]),
                    "platforms_total": int(row["platforms_total"]),
                    "views_total": int(row["views_total"]),
                    "likes_total": int(row["likes_total"]),
                    "comments_total": int(row["comments_total"]),
                    "shares_total": int(row["shares_total"]),
                    "views_new_1h": int(row["views_new_1h"]),
                    "likes_new_1h": int(row["likes_new_1h"]),
                    "comments_new_1h": int(row["comments_new_1h"]),
                    "shares_new_1h": int(row["shares_new_1h"]),
                    "counter_delta_videos": int(row["counter_delta_videos"]),
                    "activity_coverage": activity_coverage,
                    "observed_at": row["observed_at"],
                },
            })
        candidates.sort(key=lambda row: (
            -float(row["opportunity_score"]),
            -int(row["evidence"]["videos_total"]),
            str(row["display_name"]).casefold(),
        ))
        memberships: Dict[str, set[str]] = {
            str(candidate["trend_id"]): set() for candidate in candidates
        }
        contexts: Dict[str, List[set[str]]] = {
            str(candidate["trend_id"]): [] for candidate in candidates
        }
        label_tokens = {
            str(candidate["trend_id"]): _trend_label_tokens(
                str(candidate["display_name"])
            )
            for candidate in candidates
        }
        candidate_ids = list(memberships)
        with self.connect() as connection:
            for offset in range(0, len(candidate_ids), 400):
                chunk = candidate_ids[offset:offset + 400]
                placeholders = ",".join("?" for _ in chunk)
                for row in connection.execute(
                    f"""SELECT membership.trend_id, membership.video_id,
                                video.title, video.caption, video.description
                         FROM mt_trend_memberships membership
                         JOIN mt_videos video ON video.video_id = membership.video_id
                         WHERE membership.trend_id IN ({placeholders})
                         ORDER BY membership.trend_id, membership.video_id""",
                    chunk,
                ).fetchall():
                    trend_id = str(row["trend_id"])
                    memberships[trend_id].add(str(row["video_id"]))
                    context_tokens = _opportunity_context_tokens(
                        " ".join(
                            str(row[field] or "")
                            for field in ("title", "caption", "description")
                        ),
                        label_tokens[trend_id],
                    )
                    if context_tokens:
                        contexts[trend_id].append(context_tokens)
        coherent_candidates: List[Dict[str, Any]] = []
        for candidate in candidates:
            trend_id = str(candidate["trend_id"])
            context_cohesion = _opportunity_context_cohesion(contexts[trend_id])
            context_summary = _opportunity_context_summary(contexts[trend_id])
            candidate["evidence"]["context_documents"] = len(contexts[trend_id])
            candidate["evidence"]["context_cohesion"] = context_cohesion
            candidate["evidence"]["context_summary"] = context_summary
            candidate["resolved_display_name"] = str(candidate["display_name"])
            if len(label_tokens[trend_id]) == 1 and context_summary:
                candidate["resolved_display_name"] = (
                    f'{candidate["display_name"]} · {context_summary}'
                )
            if (
                str(candidate["trend_type"]).casefold() == "hashtag"
                and len(label_tokens[trend_id]) == 1
                and len(contexts[trend_id]) >= 2
                and context_cohesion < 0.15
            ):
                suppressed["low_context_cohesion"] = (
                    suppressed.get("low_context_cohesion", 0) + 1
                )
                continue
            coherent_candidates.append(candidate)
        candidates = coherent_candidates
        selected: List[Dict[str, Any]] = []
        selected_tokens: List[set[str]] = []
        selected_memberships: List[set[str]] = []
        type_counts: Dict[str, int] = {}
        class_counts: Dict[str, int] = {}
        type_limit = max(1, math.ceil(maximum * 0.5))
        class_limit = max(1, math.ceil(maximum * 0.6))
        for candidate in candidates:
            tokens = _trend_label_tokens(str(candidate["display_name"]))
            if any(_token_overlap(tokens, existing) >= 0.8 for existing in selected_tokens):
                suppressed["near_duplicate_label"] = (
                    suppressed.get("near_duplicate_label", 0) + 1
                )
                continue
            candidate_memberships = memberships[str(candidate["trend_id"])]
            if any(
                _membership_overlap(candidate_memberships, existing) >= 0.4
                for existing in selected_memberships
            ):
                suppressed["near_duplicate_evidence"] = (
                    suppressed.get("near_duplicate_evidence", 0) + 1
                )
                continue
            trend_type = str(candidate["trend_type"])
            if type_counts.get(trend_type, 0) >= type_limit:
                suppressed["portfolio_type_limit"] = (
                    suppressed.get("portfolio_type_limit", 0) + 1
                )
                continue
            opportunity_class = str(candidate["opportunity_class"])
            if class_counts.get(opportunity_class, 0) >= class_limit:
                suppressed["portfolio_class_limit"] = (
                    suppressed.get("portfolio_class_limit", 0) + 1
                )
                continue
            candidate["rank"] = len(selected) + 1
            selected.append(candidate)
            selected_tokens.append(tokens)
            selected_memberships.append(candidate_memberships)
            type_counts[trend_type] = type_counts.get(trend_type, 0) + 1
            class_counts[opportunity_class] = class_counts.get(opportunity_class, 0) + 1
            if len(selected) >= maximum:
                break
        detail = self._opportunity_content_details(
            [str(candidate["trend_id"]) for candidate in selected],
            examples_per_trend=3,
        )
        for candidate in selected:
            trend_detail = detail.get(str(candidate["trend_id"]), {})
            candidate["platform_distribution"] = trend_detail.get("platforms", {})
            candidate["representative_content"] = trend_detail.get("examples", [])
        return {
            "contract": OPPORTUNITY_CONTRACT,
            "ranker_version": OPPORTUNITY_RANKER_VERSION,
            "state": "ready",
            "generated_at": generated,
            "active_model": ({
                "model_version": active_model["model_version"],
                "model_purpose": purpose,
                "training_index_version": model_index_version,
                "compatible_with_current_index": artifact_index_compatible,
            } if active_model is not None else None),
            "model_admission": model_admission,
            "current_index_version": TREND_INDEX_VERSION,
            "score_is_probability": False,
            "ranking_weights": weights,
            "filters": {
                "states": sorted(ACTIONABLE_TREND_STATES),
                "maximum_saturation": saturation_ceiling,
                "minimum_videos": minimum_videos,
                "minimum_measured_videos": minimum_measured_videos,
                "candidate_scan_limit": bounded_candidate_scan,
                "format_aggregates_excluded": True,
                "generic_distribution_labels_excluded": True,
                "generic_hook_phrases_excluded": True,
                "single_token_hashtag_minimum_context_cohesion": 0.15,
                "crawler_expansion_excluded_from_activity": True,
                "near_duplicate_token_overlap": 0.8,
                "near_duplicate_membership_overlap": 0.4,
                "maximum_trend_type_share": 0.5,
                "maximum_opportunity_class_share": 0.6,
            },
            "candidates_considered": coarse_candidates_considered,
            "coarse_eligible_candidates": coarse_eligible_candidates,
            "candidate_rows_loaded": len(rows),
            "candidate_scan_truncated": coarse_eligible_candidates > len(rows),
            "candidate_preselection": {
                "model_neutral": True,
                "order": [
                    "trend_strength_desc",
                    "videos_total_desc",
                    "observed_at_desc",
                    "trend_id_asc",
                ],
                "maximum_loaded_trend_strength": (
                    max(float(row["trend_strength"]) for row in rows)
                    if rows else None
                ),
                "minimum_loaded_trend_strength": (
                    min(float(row["trend_strength"]) for row in rows)
                    if rows else None
                ),
                "first_loaded_trend_id": (
                    str(rows[0]["trend_id"]) if rows else None
                ),
                "last_loaded_trend_id": (
                    str(rows[-1]["trend_id"]) if rows else None
                ),
            },
            "ranking_scope": (
                "bounded_top_strength_candidates"
                if coarse_eligible_candidates > len(rows)
                else "all_coarse_eligible_candidates"
            ),
            "eligible_candidates": len(candidates),
            "suppressed_by_reason": dict(sorted(suppressed.items())),
            "opportunities": selected,
        }

    def _opportunity_content_details(
        self,
        trend_ids: Sequence[str],
        examples_per_trend: int,
    ) -> Dict[str, Dict[str, Any]]:
        detail: Dict[str, Dict[str, Any]] = {
            trend_id: {"platforms": {}, "examples": []} for trend_id in trend_ids
        }
        if not trend_ids:
            return detail
        with self.connect() as connection:
            for offset in range(0, len(trend_ids), 400):
                chunk = list(trend_ids[offset:offset + 400])
                placeholders = ",".join("?" for _ in chunk)
                rows = connection.execute(
                    f"""SELECT membership.trend_id, video.video_id,
                                video.platform, video.external_id, video.title,
                                video.caption, video.url, video.published_at,
                                creator.handle AS creator_handle,
                                observation.observed_at, observation.views,
                                observation.likes, observation.comments,
                                observation.shares, observation.view_velocity,
                                observation.view_acceleration,
                                observation.relative_strength
                         FROM mt_trend_memberships membership
                         JOIN mt_videos video ON video.video_id = membership.video_id
                         JOIN mt_creators creator ON creator.creator_id = video.creator_id
                         LEFT JOIN mt_market_observations observation
                           ON observation.observation_id = (
                               SELECT current.observation_id
                               FROM mt_market_observations current
                               WHERE current.video_id = video.video_id
                               ORDER BY current.observed_at DESC,
                                        current.observation_id DESC
                               LIMIT 1
                           )
                         WHERE membership.trend_id IN ({placeholders})
                         ORDER BY membership.trend_id,
                                  COALESCE(observation.relative_strength, 0) DESC,
                                  COALESCE(observation.view_velocity, 0) DESC,
                                  COALESCE(observation.views, 0) DESC""",
                    chunk,
                ).fetchall()
                for raw in rows:
                    row = dict(raw)
                    trend_detail = detail[str(row["trend_id"])]
                    platform = str(row["platform"])
                    platforms = trend_detail["platforms"]
                    platforms[platform] = platforms.get(platform, 0) + 1
                    examples = trend_detail["examples"]
                    if len(examples) >= examples_per_trend:
                        continue
                    examples.append({
                        "video_id": row["video_id"],
                        "platform": platform,
                        "external_id": row["external_id"],
                        "creator_handle": row["creator_handle"],
                        "title": row["title"],
                        "caption_excerpt": str(row["caption"] or "")[:240],
                        "url": row["url"],
                        "published_at": row["published_at"],
                        "observed_at": row["observed_at"],
                        "views": int(row["views"] or 0),
                        "likes": int(row["likes"] or 0),
                        "comments": int(row["comments"] or 0),
                        "shares": int(row["shares"] or 0),
                        "view_velocity": float(row["view_velocity"] or 0.0),
                        "view_acceleration": float(
                            row["view_acceleration"] or 0.0
                        ),
                        "relative_strength": float(row["relative_strength"] or 0.0),
                    })
        return detail

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


def _recent_counter_activity(row: Any, observed_at: datetime) -> Dict[str, Any]:
    window_start = observed_at - timedelta(hours=1)
    latest_at = _as_datetime(row["observed_at"])
    empty: Dict[str, Any] = {
        "measured": False,
        "views": 0,
        "likes": 0,
        "comments": 0,
        "shares": 0,
        "view_velocity": 0.0,
        "view_acceleration": 0.0,
    }
    if latest_at < window_start or latest_at > observed_at:
        return empty

    prior_value = row["prior_observed_at"]
    if prior_value:
        prior_at = _as_datetime(prior_value)
        elapsed_seconds = (latest_at - prior_at).total_seconds()
        covered_seconds = (
            latest_at - max(prior_at, window_start)
        ).total_seconds()
        if elapsed_seconds <= 0 or covered_seconds <= 0:
            return empty
        fraction = min(1.0, covered_seconds / elapsed_seconds)
        result = dict(empty)
        result["measured"] = True
        for field in ("views", "likes", "comments", "shares"):
            delta = max(0, int(row[field]) - int(row[f"prior_{field}"]))
            result[field] = max(0, round(delta * fraction))
        result["view_velocity"] = float(row["view_velocity"])
        result["view_acceleration"] = float(row["view_acceleration"])
        return result

    published_value = row["published_at"]
    if not published_value:
        return empty
    published_at = _as_datetime(published_value)
    if not (window_start <= published_at <= latest_at):
        return empty
    result = dict(empty)
    result["measured"] = True
    for field in ("views", "likes", "comments", "shares"):
        result[field] = max(0, int(row[field]))
    result["view_velocity"] = log_velocity(
        0,
        int(row["views"]),
        max(1.0, (latest_at - published_at).total_seconds()),
    )
    return result


def _in_observation_window(value: Any, observed_at: datetime, hours: int) -> bool:
    if not value:
        return False
    timestamp = _as_datetime(value)
    return observed_at - timedelta(hours=max(1, hours)) <= timestamp <= observed_at


def _percentile(values: Sequence[float], percentile: float) -> float:
    if not values:
        return 0.0
    index = min(len(values) - 1, max(0, math.ceil(percentile * len(values)) - 1))
    return float(values[index])


def _sigmoid(value: float) -> float:
    return 1.0 / (1.0 + math.exp(-max(-20.0, min(20.0, value))))


def _as_datetime(value: Any) -> datetime:
    parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _prediction_horizon_hours(horizon: str) -> float:
    if horizon == "exceeds_10x_creator_baseline_within_24h":
        return 24.0
    if horizon in {
        "reaches_breakout_within_6h",
        ENTRY_HORIZON,
        PROGRESSION_HORIZON,
    }:
        return 6.0
    match = re.search(r"within_(\d+(?:\.\d+)?)h", horizon)
    return float(match.group(1)) if match else 24.0


def _prediction_is_unexpired(
    prediction: Dict[str, Any],
    as_of: datetime,
) -> bool:
    try:
        predicted_at = _as_datetime(prediction["predicted_at"])
        horizon_hours = _prediction_horizon_hours(str(prediction["horizon"]))
    except (KeyError, TypeError, ValueError):
        return False
    return predicted_at <= as_of <= predicted_at + timedelta(
        hours=max(0.0, horizon_hours)
    )


def _prospective_model_admission(
    connection: sqlite3.Connection,
    *,
    active_model: Optional[Dict[str, Any]],
    horizon: str,
    minimum_labels: int,
    minimum_class_labels: int,
) -> Dict[str, Any]:
    """Admit probabilities only after exact-model prospective calibration."""
    thresholds = {
        "minimum_labels": int(minimum_labels),
        "minimum_unique_subjects": int(minimum_labels),
        "minimum_forecast_time_batches": 3,
        "minimum_positive_labels": int(minimum_class_labels),
        "minimum_negative_labels": int(minimum_class_labels),
        "minimum_brier_skill_score_exclusive": 0.05,
        "maximum_expected_calibration_error": 0.15,
    }
    receipt: Dict[str, Any] = {
        "contract": TREND_MODEL_ADMISSION_CONTRACT,
        "model_version": (
            str(active_model.get("model_version") or "")
            if active_model is not None
            else None
        ),
        "horizon": horizon or None,
        "prospective_validation_passed": False,
        "admitted_for_ranking": False,
        "admission_reason": "no_active_model",
        "thresholds": thresholds,
        "prospective_metrics": None,
    }
    if active_model is None:
        return receipt
    rows = [dict(row) for row in connection.execute(
        """SELECT subject_id, predicted_at, probability, outcome_json
           FROM mt_predictions
           WHERE subject_type = 'trend'
             AND model_version = ?
             AND horizon = ?""",
        (str(active_model["model_version"]), horizon),
    ).fetchall()]
    measured: List[Tuple[float, int, str, str]] = []
    unscorable = 0
    pending = 0
    for row in rows:
        if not row.get("outcome_json"):
            pending += 1
            continue
        try:
            outcome = json.loads(str(row["outcome_json"]))
        except (TypeError, ValueError, json.JSONDecodeError):
            unscorable += 1
            continue
        if outcome.get("state") != "scored":
            unscorable += 1
            continue
        measured.append((
            min(1.0, max(0.0, float(row["probability"]))),
            int(bool(outcome.get("actual"))),
            str(row["subject_id"]),
            str(row["predicted_at"]),
        ))
    labels = len(measured)
    positives = sum(row[1] for row in measured)
    negatives = labels - positives
    unique_subjects = len({row[2] for row in measured})
    forecast_time_batches = len({
        _as_datetime(row[3]).astimezone(timezone.utc).strftime("%Y-%m-%dT%H")
        for row in measured
    })
    brier = (
        sum((row[0] - row[1]) ** 2 for row in measured)
        / labels
        if labels
        else None
    )
    prevalence = positives / labels if labels else None
    baseline_brier = (
        prevalence * (1.0 - prevalence)
        if prevalence is not None
        else None
    )
    brier_skill = (
        1.0 - float(brier) / float(baseline_brier)
        if brier is not None and baseline_brier
        else None
    )
    binary_values = [(row[0], row[1]) for row in measured]
    bins = _calibration_bins(binary_values) if measured else []
    expected_calibration_error = (
        sum(
            bucket["count"] / labels
            * abs(bucket["mean_probability"] - bucket["positive_rate"])
            for bucket in bins
        )
        if labels
        else None
    )
    checks = {
        "minimum_labels": labels >= minimum_labels,
        "minimum_unique_subjects": unique_subjects >= minimum_labels,
        "minimum_forecast_time_batches": forecast_time_batches >= 3,
        "minimum_positive_labels": positives >= minimum_class_labels,
        "minimum_negative_labels": negatives >= minimum_class_labels,
        "positive_brier_skill": (
            brier_skill is not None and brier_skill > 0.05
        ),
        "calibration_error": (
            expected_calibration_error is not None
            and expected_calibration_error <= 0.15
        ),
    }
    reason_by_check = {
        "minimum_labels": "insufficient_prospective_labels",
        "minimum_unique_subjects": "insufficient_unique_subjects",
        "minimum_forecast_time_batches": "insufficient_forecast_time_batches",
        "minimum_positive_labels": "insufficient_positive_labels",
        "minimum_negative_labels": "insufficient_negative_labels",
        "positive_brier_skill": "insufficient_brier_skill",
        "calibration_error": "excessive_calibration_error",
    }
    failed_checks = [name for name, passed in checks.items() if not passed]
    reason = (
        "no_prospective_labels"
        if labels == 0
        else reason_by_check[failed_checks[0]]
        if failed_checks
        else "prospective_validation_passed"
    )
    receipt.update({
        "prospective_validation_passed": not failed_checks,
        "admission_reason": reason,
        "checks": checks,
        "prospective_metrics": {
            "labels": labels,
            "unique_subjects": unique_subjects,
            "forecast_time_batches": forecast_time_batches,
            "positives": positives,
            "negatives": negatives,
            "pending": pending,
            "unscorable": unscorable,
            "brier_score": round(brier, 6) if brier is not None else None,
            "naive_prevalence_brier_score": (
                round(baseline_brier, 6)
                if baseline_brier is not None
                else None
            ),
            "brier_skill_score": (
                round(brier_skill, 6)
                if brier_skill is not None
                else None
            ),
            "expected_calibration_error": (
                round(expected_calibration_error, 6)
                if expected_calibration_error is not None
                else None
            ),
            "roc_auc": _roc_auc(binary_values) if measured else None,
        },
    })
    return receipt


def _has_source_prediction(
    predictions: Sequence[Dict[str, Any]],
    *,
    source_observation_id: int,
    source_observed_at: datetime,
) -> bool:
    """Match durable lineage, with a fail-closed legacy fallback.

    Forecasts written before source lineage was added can still be identified:
    if their prediction timestamp is at or after the candidate snapshot, that
    snapshot was already the latest evidence available to the old writer.
    Explicit lineage always wins, allowing a newer observation to be forecast.
    """
    for prediction in predictions:
        features: Dict[str, Any] = {}
        try:
            parsed = json.loads(str(prediction.get("features_json") or "{}"))
            if isinstance(parsed, dict):
                features = parsed
        except (TypeError, ValueError, json.JSONDecodeError):
            features = {}
        explicit_id = features.get("source_observation_id")
        if explicit_id is not None:
            try:
                if int(explicit_id) == int(source_observation_id):
                    return True
                continue
            except (TypeError, ValueError):
                # Invalid claimed lineage is not safe evidence for another
                # forecast; fall through to the timestamp check.
                pass
        try:
            if _as_datetime(prediction["predicted_at"]) >= source_observed_at:
                return True
        except (KeyError, TypeError, ValueError):
            # An existing same-subject/model/horizon row with unreadable
            # lineage cannot safely authorize a second prediction.
            return True
    return False


def _opportunity_exclusion_reason(
    row: Dict[str, Any],
    saturation_ceiling: float,
    minimum_videos: int,
    minimum_measured_videos: int,
) -> Optional[str]:
    if str(row["index_version"]) != TREND_INDEX_VERSION:
        return "stale_index_version"
    if str(row["trend_type"]).casefold() == "format":
        return "format_aggregate"
    if str(row["state"]).casefold() not in ACTIONABLE_TREND_STATES:
        return "non_actionable_state"
    if float(row["saturation"]) > saturation_ceiling:
        return "above_saturation_ceiling"
    if int(row["videos_total"]) < minimum_videos:
        return "insufficient_video_evidence"
    if int(row["counter_delta_videos"]) < minimum_measured_videos:
        return "insufficient_measured_activity"
    normalized = " ".join(
        token.casefold()
        for token in WORD_RE.findall(str(row["display_name"]).lstrip("#"))
    )
    compact = re.sub(r"[^a-z0-9]+", "", normalized)
    if normalized in GENERIC_TREND_LABELS or compact in GENERIC_TREND_LABELS:
        return "generic_distribution_label"
    if _is_generic_hook_phrase(normalized, str(row["trend_type"])):
        return "generic_hook_phrase"
    if not compact or compact.isdigit() or len(compact) < 3:
        return "non_specific_label"
    if not _is_specific_trend_phrase(str(row["display_name"]), str(row["trend_type"])):
        return "incomplete_phrase"
    return None


def _trend_label_tokens(value: str) -> set[str]:
    return {
        token.casefold()
        for token in WORD_RE.findall(value.lstrip("#"))
        if token.casefold() not in STOP_WORDS
    }


def _is_generic_hook_phrase(value: str, trend_type: str) -> bool:
    if trend_type.casefold() not in {"topic", "hook"}:
        return False
    tokens = [token.casefold() for token in WORD_RE.findall(value)]
    return 2 <= len(tokens) <= 5 and all(
        token in GENERIC_HOOK_TOKENS or token in STOP_WORDS
        for token in tokens
    )


def _opportunity_context_tokens(
    value: str,
    label_tokens: set[str],
) -> set[str]:
    cleaned = re.sub(
        r"(?:https?://|www\.)\S+|@[a-z0-9_.-]+",
        " ",
        value,
        flags=re.IGNORECASE,
    )
    return {
        token.casefold()
        for token in WORD_RE.findall(cleaned)
        if len(token) >= 3
        and token.casefold() not in STOP_WORDS
        and token.casefold() not in label_tokens
        and token.casefold() not in GENERIC_TREND_LABELS
        and token.casefold() not in GENERIC_HOOK_TOKENS
        and token.casefold() not in CONTEXT_BOILERPLATE_TOKENS
        and sum(character.isdigit() for character in token) < 4
    }


def _opportunity_context_cohesion(documents: Sequence[set[str]]) -> float:
    if len(documents) < 2:
        return 0.0
    nearest_neighbor_scores: List[float] = []
    for index, document in enumerate(documents):
        peers = [
            _membership_overlap(document, other)
            for peer_index, other in enumerate(documents)
            if peer_index != index
        ]
        nearest_neighbor_scores.append(max(peers, default=0.0))
    return round(
        sum(nearest_neighbor_scores) / len(nearest_neighbor_scores),
        6,
    )


def _opportunity_context_summary(documents: Sequence[set[str]]) -> str:
    if len(documents) < 2:
        return ""
    counts: Dict[str, int] = {}
    for document in documents:
        for token in document:
            counts[token] = counts.get(token, 0) + 1
    shared = sorted(
        (token for token, count in counts.items() if count >= 2),
        key=lambda token: (-counts[token], any(char.isdigit() for char in token), token),
    )[:3]
    acronyms = {"ai", "mlb", "nba", "nfl", "nhl", "ufc", "wnba"}
    return " ".join(
        token.upper() if token in acronyms else token.title()
        for token in shared
    )


def _context_trend_key(context: Any) -> str:
    if not isinstance(context, dict):
        return ""
    raw_value = next((
        str(context.get(field) or "").strip()
        for field in ("query_family", "topic", "niche")
        if str(context.get(field) or "").strip()
    ), "")
    words = [
        word.casefold()
        for word in WORD_RE.findall(raw_value)
        if word.casefold() not in STOP_WORDS
    ][:6]
    key = " ".join(words)
    if _is_specific_trend_phrase(key, "topic"):
        return key
    if len(words) == 1 and len(words[0]) >= 4 and words[0] not in GENERIC_TREND_LABELS:
        return words[0]
    return ""


def _is_specific_trend_phrase(value: str, trend_type: str) -> bool:
    if trend_type.casefold() not in {"topic", "hook"}:
        return True
    tokens = [token.casefold() for token in WORD_RE.findall(value)]
    if len(tokens) < 2:
        return False
    return (
        tokens[0] not in VAGUE_PHRASE_LEADERS
        and tokens[-1] not in VAGUE_PHRASE_TRAILERS
    )


def _token_overlap(left: set[str], right: set[str]) -> float:
    if not left or not right:
        return 0.0
    return len(left & right) / len(left | right)


def _membership_overlap(left: set[str], right: set[str]) -> float:
    if not left or not right:
        return 0.0
    return len(left & right) / min(len(left), len(right))


def _centered_signal(value: float) -> float:
    return _sigmoid(max(-8.0, min(8.0, value)))


def _opportunity_class(state: str) -> str:
    normalized = state.casefold()
    if normalized in {"discovering", "emerging"}:
        return "early"
    if normalized == "breakout":
        return "active_breakout"
    return "recurring_wave"


def _opportunity_evidence_grade(row: Dict[str, Any]) -> str:
    measured = int(row["counter_delta_videos"])
    platforms = int(row["platforms_total"])
    recent_views = int(row["views_new_1h"])
    if measured >= 5 and platforms >= 2 and recent_views >= 10000:
        return "high"
    if measured >= 3 and recent_views >= 1000:
        return "medium"
    return "provisional"


def _prediction_key(payload: Dict[str, Any]) -> str:
    return stable_hash({
        "subject_type": payload["subject_type"],
        "subject_id": payload["subject_id"],
        "model_version": payload["model_version"],
        "predicted_at": payload["predicted_at"],
        "horizon": payload["horizon"],
    })


def _grouped_rows(
    connection: sqlite3.Connection,
    query_template: str,
    subject_ids: Sequence[str],
) -> Dict[str, List[Dict[str, Any]]]:
    grouped: Dict[str, List[Dict[str, Any]]] = {}
    for offset in range(0, len(subject_ids), 400):
        chunk = list(subject_ids[offset:offset + 400])
        placeholders = ",".join("?" for _ in chunk)
        for row in connection.execute(
            query_template.format(placeholders=placeholders),
            chunk,
        ).fetchall():
            payload = dict(row)
            grouped.setdefault(str(payload["subject_id"]), []).append(payload)
    return grouped


def _calibration_bins(values: Sequence[Tuple[float, int]]) -> List[Dict[str, Any]]:
    output: List[Dict[str, Any]] = []
    for index in range(10):
        lower = index / 10.0
        upper = (index + 1) / 10.0
        bucket = [
            (probability, actual) for probability, actual in values
            if lower <= probability < upper or (index == 9 and probability == 1.0)
        ]
        if not bucket:
            continue
        output.append({
            "lower": lower,
            "upper": upper,
            "count": len(bucket),
            "mean_probability": round(sum(value[0] for value in bucket) / len(bucket), 6),
            "positive_rate": round(sum(value[1] for value in bucket) / len(bucket), 6),
        })
    return output


def _roc_auc(values: Sequence[Tuple[float, int]]) -> Optional[float]:
    ordered = sorted(values, key=lambda value: value[0])
    positives = sum(actual for _, actual in ordered)
    negatives = len(ordered) - positives
    if positives == 0 or negatives == 0:
        return None
    positive_rank_sum = 0.0
    index = 0
    while index < len(ordered):
        end = index + 1
        while end < len(ordered) and ordered[end][0] == ordered[index][0]:
            end += 1
        average_rank = ((index + 1) + end) / 2.0
        positive_rank_sum += average_rank * sum(
            actual for _, actual in ordered[index:end]
        )
        index = end
    auc = (
        positive_rank_sum - positives * (positives + 1) / 2.0
    ) / (positives * negatives)
    return round(auc, 6)


def _select_in(
    connection: sqlite3.Connection, table: str, key: str, values: Sequence[str]
) -> List[Dict[str, Any]]:
    if not values:
        return []
    placeholders = ",".join("?" for _ in values)
    return [dict(row) for row in connection.execute(
        f"SELECT * FROM {table} WHERE {key} IN ({placeholders})", list(values)
    ).fetchall()]
