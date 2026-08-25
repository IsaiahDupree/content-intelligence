"""Append-only SQLite spool and content-addressed raw archive for Market Tape V1."""

from __future__ import annotations

import gzip
import heapq
import json
import math
import re
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

from .config import MarketTapeConfig
from .keywords import rank_keywords
from .math import age_bucket, concentration, counter_motion, log_velocity, poll_interval_seconds, trend_state, trend_strength, zscore
from .models import MarketContent, QueryAttempt, SourceReceipt, isoformat, stable_hash, utc_now
from .predictor import (
    ENTRY_HORIZON,
    OBSERVATION_QUALITY_CONTRACT,
    PROGRESSION_HORIZON,
    eligible_for_early_entry,
    load_active_model,
    model_accepts_features,
    model_prediction_horizon,
    model_purpose,
    predict_trend_snapshot,
)


SCHEMA_VERSION = 15
COUNTER_REGRESSION_FLAG_PREFIX = "counter-regression:"
ACCEPTED_OBSERVATION_EVIDENCE_CONTRACT = (
    "market_tape_accepted_observation_evidence_v1"
)
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
SCRIPT_LANGUAGE_DEMAND_CONTRACT = "market_tape_script_language_demand_v1"
SCRIPT_LANGUAGE_DEMAND_EVENT_CONTRACT = (
    "market_tape_script_language_demand_event_v1"
)
SCRIPT_LANGUAGE_DEMAND_SNAPSHOT_LINEAGE_CONTRACT = (
    "market_tape_script_language_demand_snapshot_lineage_v1"
)
SCRIPT_LANGUAGE_DEMAND_EVENT_TYPES = {
    "requested",
    "claimed",
    "completed",
    "partial",
    "blocked",
    "failed",
}
SCRIPT_LANGUAGE_DEMAND_TERMINAL_EVENTS = {
    "completed",
    "partial",
    "blocked",
    "failed",
}
# ``partial`` finishes exactly one bounded acquisition attempt but leaves the
# demand eligible for another explicitly triggered run.  Only these events
# close the demand itself.
SCRIPT_LANGUAGE_DEMAND_FINAL_EVENTS = {
    "completed",
    "blocked",
    "failed",
}


class ScriptLanguageDemandClaimConflict(ValueError):
    """A caller-bound demand is not the next atomically claimable demand."""

    def __init__(
        self,
        expected_demand_id: str,
        next_demand_id: str | None,
    ) -> None:
        self.expected_demand_id = expected_demand_id
        self.next_demand_id = next_demand_id
        super().__init__(
            "expected_demand_id does not match the next claimable demand"
        )

    def payload(self) -> Dict[str, Any]:
        return {
            "status": "error",
            "state": "conflict",
            "code": "SCRIPT_LANGUAGE_DEMAND_CLAIM_CONFLICT",
            "error": str(self),
            "expected_demand_id": self.expected_demand_id,
            "next_demand_id": self.next_demand_id,
            "mutation_applied": False,
        }


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


class CounterRegressionError(ValueError):
    """A cumulative provider counter moved backwards.

    The raw observation is still committed with an immutable quality flag so
    the provider response remains auditable, but callers must not count it as
    an accepted measurement.
    """

    def __init__(
        self,
        *,
        video_id: str,
        observation_id: int,
        views: int,
        prior_observation_id: int,
        prior_views: int,
    ) -> None:
        self.video_id = video_id
        self.observation_id = observation_id
        self.views = views
        self.prior_observation_id = prior_observation_id
        self.prior_views = prior_views
        super().__init__(
            "cumulative views regressed for "
            f"{video_id}: {views} < {prior_views}"
        )


def _counter_regression_flag_id(observation_key: Any) -> str:
    """Return the globally stable identity for one quarantined observation."""

    canonical_observation_key = str(observation_key or "").strip()
    if not canonical_observation_key:
        raise ValueError("observation_key is required for a counter-regression flag")
    return f"{COUNTER_REGRESSION_FLAG_PREFIX}{canonical_observation_key}"


def _canonical_observation_quality_sync_payload(
    payload: Dict[str, Any],
) -> Dict[str, Any]:
    """Remap database-local legacy flag ids at the remote-sync boundary."""

    canonical = dict(payload)
    canonical["flag_id"] = _counter_regression_flag_id(
        canonical.get("observation_key")
    )
    return canonical


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
            quality_table_preexisting = connection.execute(
                """SELECT 1 FROM sqlite_master
                   WHERE type = 'table'
                     AND name = 'mt_observation_quality_flags'"""
            ).fetchone() is not None
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
                CREATE INDEX IF NOT EXISTS mt_query_attempts_time_idx
                    ON mt_query_attempts(attempted_at DESC);

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

                CREATE TABLE IF NOT EXISTS mt_adaptive_query_admissions (
                    admission_key TEXT PRIMARY KEY,
                    run_id TEXT NOT NULL,
                    utc_day TEXT NOT NULL,
                    query_family TEXT NOT NULL,
                    keyword TEXT NOT NULL,
                    selection_lane TEXT NOT NULL,
                    admitted_at TEXT NOT NULL,
                    proposal_sha256 TEXT NOT NULL,
                    evidence_json TEXT NOT NULL DEFAULT '{}',
                    UNIQUE(run_id, query_family),
                    FOREIGN KEY(run_id) REFERENCES mt_collection_runs(run_id)
                );

                CREATE INDEX IF NOT EXISTS mt_adaptive_query_admissions_day_family_idx
                    ON mt_adaptive_query_admissions(utc_day, query_family, admitted_at DESC);
                CREATE INDEX IF NOT EXISTS mt_adaptive_query_admissions_run_idx
                    ON mt_adaptive_query_admissions(run_id, admitted_at DESC);
                CREATE INDEX IF NOT EXISTS mt_adaptive_query_admissions_time_idx
                    ON mt_adaptive_query_admissions(admitted_at DESC);

                CREATE TRIGGER IF NOT EXISTS mt_adaptive_query_admissions_no_update
                BEFORE UPDATE ON mt_adaptive_query_admissions
                BEGIN
                    SELECT RAISE(ABORT, 'adaptive query admissions are append-only');
                END;

                CREATE TRIGGER IF NOT EXISTS mt_adaptive_query_admissions_no_delete
                BEFORE DELETE ON mt_adaptive_query_admissions
                BEGIN
                    SELECT RAISE(ABORT, 'adaptive query admissions are append-only');
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

                CREATE TABLE IF NOT EXISTS mt_observation_quality_flags (
                    flag_id TEXT PRIMARY KEY,
                    observation_id INTEGER NOT NULL UNIQUE,
                    run_id TEXT NOT NULL,
                    video_id TEXT NOT NULL,
                    source_id TEXT NOT NULL,
                    detected_at TEXT NOT NULL,
                    observed_at TEXT NOT NULL,
                    views INTEGER NOT NULL,
                    prior_observation_id INTEGER NOT NULL,
                    prior_observed_at TEXT NOT NULL,
                    prior_views INTEGER NOT NULL,
                    error_code TEXT NOT NULL,
                    raw_sha256 TEXT NOT NULL,
                    metadata_json TEXT NOT NULL DEFAULT '{}',
                    FOREIGN KEY(observation_id)
                        REFERENCES mt_market_observations(observation_id),
                    FOREIGN KEY(prior_observation_id)
                        REFERENCES mt_market_observations(observation_id),
                    FOREIGN KEY(video_id) REFERENCES mt_videos(video_id),
                    FOREIGN KEY(raw_sha256) REFERENCES mt_raw_objects(raw_sha256)
                );

                CREATE INDEX IF NOT EXISTS mt_observation_quality_video_time_idx
                    ON mt_observation_quality_flags(video_id, observed_at DESC);
                CREATE INDEX IF NOT EXISTS mt_observation_quality_error_time_idx
                    ON mt_observation_quality_flags(error_code, detected_at DESC);

                CREATE TRIGGER IF NOT EXISTS mt_observation_quality_flags_no_update
                BEFORE UPDATE ON mt_observation_quality_flags
                BEGIN
                    SELECT RAISE(ABORT, 'observation quality flags are append-only');
                END;

                CREATE TRIGGER IF NOT EXISTS mt_observation_quality_flags_no_delete
                BEFORE DELETE ON mt_observation_quality_flags
                BEGIN
                    SELECT RAISE(ABORT, 'observation quality flags are append-only');
                END;

                CREATE TABLE IF NOT EXISTS mt_accepted_observation_evidence (
                    evidence_id TEXT PRIMARY KEY,
                    observation_id INTEGER NOT NULL,
                    observation_key TEXT NOT NULL,
                    video_id TEXT NOT NULL,
                    creator_id TEXT NOT NULL,
                    accepted_at TEXT NOT NULL,
                    contract TEXT NOT NULL,
                    evidence_scope TEXT NOT NULL CHECK(
                        evidence_scope IN ('metric_only', 'full')
                    ),
                    published_at TEXT,
                    title TEXT NOT NULL DEFAULT '',
                    caption TEXT NOT NULL DEFAULT '',
                    description TEXT NOT NULL DEFAULT '',
                    language TEXT NOT NULL DEFAULT '',
                    url TEXT NOT NULL DEFAULT '',
                    thumbnail_url TEXT NOT NULL DEFAULT '',
                    media_type TEXT NOT NULL DEFAULT 'video',
                    duration_seconds REAL,
                    hashtags_json TEXT NOT NULL DEFAULT '[]',
                    discovery_queries_json TEXT NOT NULL DEFAULT '[]',
                    discovery_context_json TEXT NOT NULL DEFAULT '{}',
                    UNIQUE(observation_id, evidence_scope),
                    FOREIGN KEY(observation_id)
                        REFERENCES mt_market_observations(observation_id),
                    FOREIGN KEY(video_id) REFERENCES mt_videos(video_id),
                    FOREIGN KEY(creator_id) REFERENCES mt_creators(creator_id)
                );

                CREATE INDEX IF NOT EXISTS mt_accepted_evidence_video_time_idx
                    ON mt_accepted_observation_evidence(video_id, accepted_at DESC);
                CREATE INDEX IF NOT EXISTS mt_accepted_evidence_observation_idx
                    ON mt_accepted_observation_evidence(observation_id, evidence_scope);

                CREATE TRIGGER IF NOT EXISTS mt_accepted_observation_evidence_no_update
                BEFORE UPDATE ON mt_accepted_observation_evidence
                BEGIN
                    SELECT RAISE(ABORT, 'accepted observation evidence is append-only');
                END;

                CREATE TRIGGER IF NOT EXISTS mt_accepted_observation_evidence_no_delete
                BEFORE DELETE ON mt_accepted_observation_evidence
                BEGIN
                    SELECT RAISE(ABORT, 'accepted observation evidence is append-only');
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

                CREATE TABLE IF NOT EXISTS mt_transcript_payload_snapshots (
                    transcript_id TEXT PRIMARY KEY,
                    transcript_sha256 TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE TRIGGER IF NOT EXISTS mt_transcript_payload_snapshots_no_update
                BEFORE UPDATE ON mt_transcript_payload_snapshots
                BEGIN
                    SELECT RAISE(ABORT, 'transcript payload snapshots are append-only');
                END;
                CREATE TRIGGER IF NOT EXISTS mt_transcript_payload_snapshots_no_delete
                BEFORE DELETE ON mt_transcript_payload_snapshots
                BEGIN
                    SELECT RAISE(ABORT, 'transcript payload snapshots are append-only');
                END;

                CREATE TABLE IF NOT EXISTS mt_transcript_payload_snapshot_backfill_runs (
                    run_id TEXT PRIMARY KEY,
                    contract TEXT NOT NULL,
                    requested_json TEXT NOT NULL,
                    result_json TEXT NOT NULL,
                    started_at TEXT NOT NULL,
                    finished_at TEXT NOT NULL
                );
                CREATE TRIGGER IF NOT EXISTS
                    mt_transcript_payload_snapshot_backfill_runs_no_update
                BEFORE UPDATE ON mt_transcript_payload_snapshot_backfill_runs
                BEGIN
                    SELECT RAISE(ABORT, 'transcript payload snapshot backfills are append-only');
                END;
                CREATE TRIGGER IF NOT EXISTS
                    mt_transcript_payload_snapshot_backfill_runs_no_delete
                BEFORE DELETE ON mt_transcript_payload_snapshot_backfill_runs
                BEGIN
                    SELECT RAISE(ABORT, 'transcript payload snapshot backfills are append-only');
                END;

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

                CREATE TABLE IF NOT EXISTS mt_transcript_acquisition_attempts (
                    attempt_id TEXT PRIMARY KEY,
                    run_id TEXT NOT NULL,
                    video_id TEXT NOT NULL,
                    platform TEXT NOT NULL,
                    external_id TEXT NOT NULL,
                    source_url TEXT NOT NULL,
                    model_name TEXT NOT NULL,
                    outcome TEXT NOT NULL CHECK(outcome IN ('success', 'failure')),
                    failure_class TEXT NOT NULL DEFAULT '',
                    retryable INTEGER,
                    retry_after TEXT,
                    error_type TEXT NOT NULL DEFAULT '',
                    error TEXT NOT NULL DEFAULT '',
                    attempt_ordinal INTEGER NOT NULL,
                    receipt_source TEXT NOT NULL,
                    runtime_fingerprint TEXT NOT NULL DEFAULT '',
                    claim_id TEXT NOT NULL DEFAULT '',
                    attempt_contract TEXT NOT NULL DEFAULT 'transcript_acquisition_attempt_v1',
                    receipt_sha256 TEXT NOT NULL DEFAULT '',
                    started_at TEXT NOT NULL,
                    finished_at TEXT NOT NULL,
                    FOREIGN KEY(video_id) REFERENCES mt_videos(video_id)
                );
                CREATE INDEX IF NOT EXISTS mt_transcript_attempt_video_idx
                    ON mt_transcript_acquisition_attempts(video_id, source_url, finished_at DESC);
                CREATE INDEX IF NOT EXISTS mt_transcript_attempt_retry_idx
                    ON mt_transcript_acquisition_attempts(outcome, retryable, retry_after);

                CREATE TABLE IF NOT EXISTS mt_transcript_acquisition_claims (
                    claim_id TEXT PRIMARY KEY,
                    run_id TEXT NOT NULL,
                    video_id TEXT NOT NULL,
                    platform TEXT NOT NULL,
                    external_id TEXT NOT NULL,
                    source_url TEXT NOT NULL,
                    claim_contract TEXT NOT NULL DEFAULT 'transcript_acquisition_claim_v2',
                    receipt_sha256 TEXT NOT NULL DEFAULT '',
                    claimed_at TEXT NOT NULL,
                    expires_at TEXT NOT NULL,
                    released_at TEXT,
                    release_reason TEXT NOT NULL DEFAULT '',
                    FOREIGN KEY(video_id) REFERENCES mt_videos(video_id)
                );
                CREATE UNIQUE INDEX IF NOT EXISTS mt_transcript_active_claim_idx
                    ON mt_transcript_acquisition_claims(video_id, source_url)
                    WHERE released_at IS NULL;
                CREATE INDEX IF NOT EXISTS mt_transcript_claim_expiry_idx
                    ON mt_transcript_acquisition_claims(released_at, expires_at);

                CREATE TABLE IF NOT EXISTS mt_transcript_ledger_migrations (
                    migration_id TEXT PRIMARY KEY,
                    receipt_json TEXT NOT NULL,
                    applied_at TEXT NOT NULL
                );

                CREATE TRIGGER IF NOT EXISTS mt_transcript_attempts_no_update
                BEFORE UPDATE ON mt_transcript_acquisition_attempts
                BEGIN
                    SELECT RAISE(ABORT, 'transcript acquisition attempts are append-only');
                END;
                CREATE TRIGGER IF NOT EXISTS mt_transcript_attempts_no_delete
                BEFORE DELETE ON mt_transcript_acquisition_attempts
                BEGIN
                    SELECT RAISE(ABORT, 'transcript acquisition attempts are append-only');
                END;

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

                CREATE TABLE IF NOT EXISTS mt_trend_membership_lineage (
                    trend_id TEXT NOT NULL,
                    video_id TEXT NOT NULL,
                    observation_id INTEGER NOT NULL,
                    linked_at TEXT NOT NULL,
                    contract TEXT NOT NULL,
                    PRIMARY KEY(trend_id, video_id, observation_id),
                    FOREIGN KEY(trend_id, video_id)
                        REFERENCES mt_trend_memberships(trend_id, video_id),
                    FOREIGN KEY(observation_id)
                        REFERENCES mt_market_observations(observation_id)
                );

                CREATE INDEX IF NOT EXISTS mt_trend_membership_lineage_observation_idx
                    ON mt_trend_membership_lineage(observation_id);

                CREATE TRIGGER IF NOT EXISTS mt_trend_membership_lineage_no_update
                BEFORE UPDATE ON mt_trend_membership_lineage
                BEGIN
                    SELECT RAISE(ABORT, 'trend membership lineage is append-only');
                END;

                CREATE TRIGGER IF NOT EXISTS mt_trend_membership_lineage_no_delete
                BEFORE DELETE ON mt_trend_membership_lineage
                BEGIN
                    SELECT RAISE(ABORT, 'trend membership lineage is append-only');
                END;

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
                    observation_quality_contract TEXT NOT NULL DEFAULT
                        'legacy_unverified',
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

                CREATE INDEX IF NOT EXISTS mt_source_receipts_source_time_idx
                    ON mt_source_receipts(source_id, finished_at DESC);

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

                CREATE TABLE IF NOT EXISTS mt_forecast_measurement_reservations (
                    reservation_id TEXT PRIMARY KEY,
                    cohort_key TEXT NOT NULL,
                    created_run_id TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    model_version TEXT NOT NULL,
                    horizon TEXT NOT NULL,
                    source_id TEXT NOT NULL,
                    platform TEXT NOT NULL,
                    window_open_at TEXT NOT NULL,
                    deadline_at TEXT NOT NULL,
                    usage_date TEXT NOT NULL,
                    reserved_request_units INTEGER NOT NULL
                        CHECK(reserved_request_units > 0),
                    refresh_batch_size INTEGER NOT NULL
                        CHECK(refresh_batch_size > 0),
                    credential_fingerprint TEXT NOT NULL DEFAULT '',
                    state TEXT NOT NULL CHECK(state IN (
                        'reserved', 'claimed', 'fulfilled', 'partial',
                        'failed', 'released', 'expired'
                    )),
                    claim_run_id TEXT,
                    claimed_at TEXT,
                    claim_expires_at TEXT,
                    completed_at TEXT,
                    error_code TEXT NOT NULL DEFAULT '',
                    selection_sha256 TEXT NOT NULL,
                    capability_json TEXT NOT NULL DEFAULT '{}',
                    completion_json TEXT NOT NULL DEFAULT '{}',
                    UNIQUE(cohort_key, source_id),
                    FOREIGN KEY(created_run_id) REFERENCES mt_collection_runs(run_id),
                    FOREIGN KEY(claim_run_id) REFERENCES mt_collection_runs(run_id)
                );

                CREATE INDEX IF NOT EXISTS mt_forecast_measurement_due_idx
                    ON mt_forecast_measurement_reservations(
                        state, window_open_at, deadline_at, source_id
                    );
                CREATE INDEX IF NOT EXISTS mt_forecast_measurement_usage_idx
                    ON mt_forecast_measurement_reservations(
                        source_id, usage_date, state
                    );
                CREATE INDEX IF NOT EXISTS mt_forecast_measurement_cohort_idx
                    ON mt_forecast_measurement_reservations(
                        model_version, horizon, created_at DESC
                    );

                CREATE TABLE IF NOT EXISTS mt_forecast_measurement_assignments (
                    reservation_id TEXT NOT NULL,
                    prediction_id INTEGER NOT NULL UNIQUE,
                    trend_id TEXT NOT NULL,
                    video_id TEXT NOT NULL,
                    state TEXT NOT NULL CHECK(state IN (
                        'reserved', 'claimed', 'fulfilled', 'failed',
                        'released', 'expired'
                    )),
                    completed_at TEXT,
                    error_code TEXT NOT NULL DEFAULT '',
                    PRIMARY KEY(reservation_id, prediction_id),
                    FOREIGN KEY(reservation_id)
                        REFERENCES mt_forecast_measurement_reservations(reservation_id),
                    FOREIGN KEY(prediction_id) REFERENCES mt_predictions(prediction_id),
                    FOREIGN KEY(trend_id) REFERENCES mt_trends(trend_id),
                    FOREIGN KEY(video_id) REFERENCES mt_videos(video_id)
                );

                CREATE INDEX IF NOT EXISTS mt_forecast_measurement_assignment_video_idx
                    ON mt_forecast_measurement_assignments(video_id, state);
                CREATE INDEX IF NOT EXISTS mt_forecast_measurement_assignment_trend_idx
                    ON mt_forecast_measurement_assignments(trend_id, state);

                -- MT-009: calibration metrics are RECORDED after horizons
                -- close, not only computed on demand. Append-only snapshots.
                CREATE TABLE IF NOT EXISTS mt_prediction_calibration (
                    calibration_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    computed_at TEXT NOT NULL,
                    subject_type TEXT NOT NULL,
                    model_version TEXT NOT NULL,
                    horizon TEXT NOT NULL,
                    observation_quality_contract TEXT NOT NULL
                        DEFAULT 'legacy_unverified',
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

                CREATE TABLE IF NOT EXISTS mt_script_language_demand_events (
                    event_id TEXT PRIMARY KEY,
                    demand_id TEXT NOT NULL,
                    event_type TEXT NOT NULL CHECK(event_type IN (
                        'requested', 'claimed', 'completed', 'partial',
                        'blocked', 'failed'
                    )),
                    attempt_no INTEGER NOT NULL CHECK(attempt_no >= 0),
                    request_sha256 TEXT NOT NULL,
                    source_service TEXT NOT NULL,
                    source_receipt_id TEXT NOT NULL,
                    topic TEXT NOT NULL,
                    audience TEXT NOT NULL,
                    objective TEXT NOT NULL,
                    evidence_trend_id TEXT NOT NULL DEFAULT '',
                    snapshot_id TEXT NOT NULL,
                    lease_until TEXT,
                    collection_run_id TEXT NOT NULL DEFAULT '',
                    transcript_run_id TEXT NOT NULL DEFAULT '',
                    payload_json TEXT NOT NULL DEFAULT '{}',
                    created_at TEXT NOT NULL,
                    UNIQUE(demand_id, event_type, attempt_no)
                );

                CREATE INDEX IF NOT EXISTS
                    mt_script_language_demand_events_demand_time_idx
                    ON mt_script_language_demand_events(
                        demand_id, created_at, event_id
                    );
                CREATE INDEX IF NOT EXISTS
                    mt_script_language_demand_events_type_time_idx
                    ON mt_script_language_demand_events(
                        event_type, created_at, demand_id
                    );
                CREATE INDEX IF NOT EXISTS
                    mt_script_language_demand_events_snapshot_time_idx
                    ON mt_script_language_demand_events(
                        snapshot_id, created_at, demand_id
                    );

                CREATE TRIGGER IF NOT EXISTS
                    mt_script_language_demand_events_no_update
                BEFORE UPDATE ON mt_script_language_demand_events
                BEGIN
                    SELECT RAISE(
                        ABORT,
                        'script language demand events are append-only'
                    );
                END;

                CREATE TRIGGER IF NOT EXISTS
                    mt_script_language_demand_events_no_delete
                BEFORE DELETE ON mt_script_language_demand_events
                BEGIN
                    SELECT RAISE(
                        ABORT,
                        'script language demand events are append-only'
                    );
                END;

                CREATE TABLE IF NOT EXISTS mt_script_language_demand_semantics (
                    semantic_key TEXT PRIMARY KEY,
                    contract TEXT NOT NULL,
                    normalized_topic TEXT NOT NULL,
                    normalized_audience TEXT NOT NULL,
                    normalized_objective TEXT NOT NULL,
                    targets_json TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );

                CREATE TRIGGER IF NOT EXISTS
                    mt_script_language_demand_semantics_no_update
                BEFORE UPDATE ON mt_script_language_demand_semantics
                BEGIN
                    SELECT RAISE(
                        ABORT,
                        'script language demand semantics are append-only'
                    );
                END;

                CREATE TRIGGER IF NOT EXISTS
                    mt_script_language_demand_semantics_no_delete
                BEFORE DELETE ON mt_script_language_demand_semantics
                BEGIN
                    SELECT RAISE(
                        ABORT,
                        'script language demand semantics are append-only'
                    );
                END;

                CREATE TABLE IF NOT EXISTS
                    mt_script_language_demand_snapshot_lineage (
                        lineage_sequence INTEGER PRIMARY KEY AUTOINCREMENT,
                        lineage_id TEXT NOT NULL UNIQUE,
                        demand_id TEXT NOT NULL,
                        semantic_key TEXT NOT NULL,
                        snapshot_id TEXT NOT NULL,
                        request_sha256 TEXT NOT NULL,
                        source_service TEXT NOT NULL,
                        source_receipt_id TEXT NOT NULL,
                        evidence_trend_id TEXT NOT NULL DEFAULT '',
                        payload_json TEXT NOT NULL DEFAULT '{}',
                        created_at TEXT NOT NULL,
                        UNIQUE(demand_id, request_sha256)
                    );

                CREATE INDEX IF NOT EXISTS
                    mt_script_language_demand_snapshot_lineage_demand_idx
                    ON mt_script_language_demand_snapshot_lineage(
                        demand_id, lineage_sequence
                    );
                CREATE INDEX IF NOT EXISTS
                    mt_script_language_demand_snapshot_lineage_semantic_idx
                    ON mt_script_language_demand_snapshot_lineage(
                        semantic_key, lineage_sequence
                    );

                CREATE TRIGGER IF NOT EXISTS
                    mt_script_language_demand_snapshot_lineage_no_update
                BEFORE UPDATE ON mt_script_language_demand_snapshot_lineage
                BEGIN
                    SELECT RAISE(
                        ABORT,
                        'script language demand snapshot lineage is append-only'
                    );
                END;

                CREATE TRIGGER IF NOT EXISTS
                    mt_script_language_demand_snapshot_lineage_no_delete
                BEFORE DELETE ON mt_script_language_demand_snapshot_lineage
                BEGIN
                    SELECT RAISE(
                        ABORT,
                        'script language demand snapshot lineage is append-only'
                    );
                END;

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
            _backfill_script_language_demand_lineage(connection)
            transcript_attempt_columns = {
                row[1]
                for row in connection.execute(
                    "PRAGMA table_info(mt_transcript_acquisition_attempts)"
                ).fetchall()
            }
            for column, definition in {
                "runtime_fingerprint": "TEXT NOT NULL DEFAULT ''",
                "claim_id": "TEXT NOT NULL DEFAULT ''",
                "attempt_contract": (
                    "TEXT NOT NULL DEFAULT 'transcript_acquisition_attempt_v1'"
                ),
                "receipt_sha256": "TEXT NOT NULL DEFAULT ''",
            }.items():
                if column not in transcript_attempt_columns:
                    connection.execute(
                        f"ALTER TABLE mt_transcript_acquisition_attempts "
                        f"ADD COLUMN {column} {definition}"
                    )
            transcript_claim_columns = {
                row[1]
                for row in connection.execute(
                    "PRAGMA table_info(mt_transcript_acquisition_claims)"
                ).fetchall()
            }
            for column, definition in {
                "claim_contract": (
                    "TEXT NOT NULL DEFAULT 'transcript_acquisition_claim_v2'"
                ),
                "receipt_sha256": "TEXT NOT NULL DEFAULT ''",
            }.items():
                if column not in transcript_claim_columns:
                    connection.execute(
                        f"ALTER TABLE mt_transcript_acquisition_claims "
                        f"ADD COLUMN {column} {definition}"
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
            if "observation_quality_contract" not in trend_observation_columns:
                connection.execute(
                    """ALTER TABLE mt_trend_observations
                       ADD COLUMN observation_quality_contract TEXT NOT NULL
                       DEFAULT 'legacy_unverified'"""
                )
            calibration_columns = {
                row[1]
                for row in connection.execute(
                    "PRAGMA table_info(mt_prediction_calibration)"
                ).fetchall()
            }
            if "observation_quality_contract" not in calibration_columns:
                connection.execute(
                    """ALTER TABLE mt_prediction_calibration
                       ADD COLUMN observation_quality_contract TEXT NOT NULL
                       DEFAULT 'legacy_unverified'"""
                )
            quality_backfill = connection.execute(
                """SELECT value FROM mt_meta
                   WHERE key = 'counter_regression_backfill_v1'"""
            ).fetchone()
            if not quality_table_preexisting or quality_backfill is None:
                self._flag_counter_regressions(
                    connection,
                    detected_at=isoformat(utc_now()),
                    migration_backfill=True,
                )
                connection.execute(
                    """INSERT INTO mt_meta(key, value)
                       VALUES('counter_regression_backfill_v1', ?)
                       ON CONFLICT(key) DO UPDATE SET value = excluded.value""",
                    (isoformat(utc_now()),),
                )
            evidence_columns = {
                row[1]
                for row in connection.execute(
                    "PRAGMA table_info(mt_accepted_observation_evidence)"
                ).fetchall()
            }
            if "evidence_id" not in evidence_columns:
                scope_expression = (
                    "evidence_scope"
                    if "evidence_scope" in evidence_columns
                    else "'metric_only'"
                )
                legacy_count = int(connection.execute(
                    "SELECT COUNT(*) FROM mt_accepted_observation_evidence"
                ).fetchone()[0])
                connection.execute(
                    """ALTER TABLE mt_accepted_observation_evidence
                       RENAME TO mt_accepted_observation_evidence_v10_archive"""
                )
                connection.executescript(
                    """CREATE TABLE mt_accepted_observation_evidence (
                           evidence_id TEXT PRIMARY KEY,
                           observation_id INTEGER NOT NULL,
                           observation_key TEXT NOT NULL,
                           video_id TEXT NOT NULL,
                           creator_id TEXT NOT NULL,
                           accepted_at TEXT NOT NULL,
                           contract TEXT NOT NULL,
                           evidence_scope TEXT NOT NULL CHECK(
                               evidence_scope IN ('metric_only', 'full')
                           ),
                           published_at TEXT,
                           title TEXT NOT NULL DEFAULT '',
                           caption TEXT NOT NULL DEFAULT '',
                           description TEXT NOT NULL DEFAULT '',
                           language TEXT NOT NULL DEFAULT '',
                           url TEXT NOT NULL DEFAULT '',
                           thumbnail_url TEXT NOT NULL DEFAULT '',
                           media_type TEXT NOT NULL DEFAULT 'video',
                           duration_seconds REAL,
                           hashtags_json TEXT NOT NULL DEFAULT '[]',
                           discovery_queries_json TEXT NOT NULL DEFAULT '[]',
                           discovery_context_json TEXT NOT NULL DEFAULT '{}',
                           UNIQUE(observation_id, evidence_scope),
                           FOREIGN KEY(observation_id)
                               REFERENCES mt_market_observations(observation_id),
                           FOREIGN KEY(video_id) REFERENCES mt_videos(video_id),
                           FOREIGN KEY(creator_id) REFERENCES mt_creators(creator_id)
                       );
                       CREATE INDEX mt_accepted_evidence_v11_video_time_idx
                           ON mt_accepted_observation_evidence(
                               video_id, accepted_at DESC
                           );
                       CREATE INDEX mt_accepted_evidence_v11_observation_idx
                           ON mt_accepted_observation_evidence(
                               observation_id, evidence_scope
                           );"""
                )
                connection.execute(
                    f"""INSERT INTO mt_accepted_observation_evidence(
                            evidence_id, observation_id, observation_key,
                            video_id, creator_id, accepted_at, contract,
                            evidence_scope, published_at, title, caption,
                            description, language, url, thumbnail_url,
                            media_type, duration_seconds, hashtags_json,
                            discovery_queries_json, discovery_context_json
                        )
                        SELECT 'accepted:' || observation_key || ':' ||
                                   {scope_expression},
                               observation_id, observation_key, video_id,
                               creator_id, accepted_at, contract,
                               {scope_expression}, published_at, title, caption,
                               description, language, url, thumbnail_url,
                               media_type, duration_seconds, hashtags_json,
                               discovery_queries_json, discovery_context_json
                        FROM mt_accepted_observation_evidence_v10_archive"""
                )
                connection.executescript(
                    """CREATE TRIGGER
                           mt_accepted_observation_evidence_v11_no_update
                       BEFORE UPDATE ON mt_accepted_observation_evidence
                       BEGIN
                           SELECT RAISE(
                               ABORT,
                               'accepted observation evidence is append-only'
                           );
                       END;
                       CREATE TRIGGER
                           mt_accepted_observation_evidence_v11_no_delete
                       BEFORE DELETE ON mt_accepted_observation_evidence
                       BEGIN
                           SELECT RAISE(
                               ABORT,
                               'accepted observation evidence is append-only'
                           );
                       END;"""
                )
                connection.execute(
                    """INSERT INTO mt_meta(key, value)
                       VALUES('accepted_observation_scope_identity_migration_v1', ?)
                       ON CONFLICT(key) DO NOTHING""",
                    (json.dumps({
                        "contract": (
                            "market_tape_accepted_evidence_scope_migration_v1"
                        ),
                        "recorded_at": isoformat(utc_now()),
                        "rows_preserved": legacy_count,
                        "archived_table": (
                            "mt_accepted_observation_evidence_v10_archive"
                        ),
                        "append_only_per_scope": True,
                    }, sort_keys=True),),
                )
            metric_backfill = connection.execute(
                """SELECT value FROM mt_meta
                   WHERE key = 'accepted_observation_metric_backfill_v1'"""
            ).fetchone()
            if metric_backfill is None:
                before = int(connection.execute(
                    "SELECT COUNT(*) FROM mt_accepted_observation_evidence"
                ).fetchone()[0])
                connection.execute(
                    """INSERT INTO mt_accepted_observation_evidence(
                           evidence_id, observation_id, observation_key,
                           video_id, creator_id, accepted_at, contract,
                           evidence_scope, published_at, title, caption,
                           description, language, url, thumbnail_url,
                           media_type, duration_seconds, hashtags_json,
                           discovery_queries_json, discovery_context_json
                       )
                       SELECT 'accepted:' || observation.observation_key ||
                                  ':metric_only',
                              observation.observation_id,
                              observation.observation_key,
                              observation.video_id,
                              observation.creator_id,
                              observation.observed_at,
                              ?, 'metric_only', NULL, '', '', '', '', '', '',
                              'video', NULL, '[]', '[]', '{}'
                       FROM mt_market_observations observation
                       WHERE observation.source_confidence > 0
                         AND NOT EXISTS (
                             SELECT 1
                             FROM mt_observation_quality_flags quality
                             WHERE quality.observation_id =
                                   observation.observation_id
                         )
                       ON CONFLICT(observation_id, evidence_scope)
                       DO NOTHING""",
                    (ACCEPTED_OBSERVATION_EVIDENCE_CONTRACT,),
                )
                after = int(connection.execute(
                    "SELECT COUNT(*) FROM mt_accepted_observation_evidence"
                ).fetchone()[0])
                connection.execute(
                    """INSERT INTO mt_meta(key, value)
                       VALUES('accepted_observation_metric_backfill_v1', ?)""",
                    (json.dumps({
                        "contract": (
                            "market_tape_accepted_metric_backfill_receipt_v1"
                        ),
                        "recorded_at": isoformat(utc_now()),
                        "rows_added": max(0, after - before),
                        "descriptive_state_backfilled": False,
                        "trend_membership_lineage_backfilled": False,
                        "reason": (
                            "legacy mutable derived state has no exact raw "
                            "observation lineage"
                        ),
                    }, sort_keys=True),),
                )
            connection.executescript(
                """CREATE VIEW IF NOT EXISTS
                           mt_accepted_metric_observations_v1 AS
                       SELECT observation.*
                       FROM mt_market_observations observation
                       WHERE observation.source_confidence > 0
                         AND EXISTS (
                             SELECT 1
                             FROM mt_accepted_observation_evidence evidence
                             WHERE evidence.observation_id =
                                   observation.observation_id
                               AND evidence.contract =
                                   'market_tape_accepted_observation_evidence_v1'
                         )
                         AND NOT EXISTS (
                             SELECT 1
                             FROM mt_observation_quality_flags quality
                             WHERE quality.observation_id =
                                   observation.observation_id
                         );

                   CREATE VIEW IF NOT EXISTS mt_accepted_full_evidence_v1 AS
                       SELECT evidence.*
                       FROM mt_accepted_observation_evidence evidence
                       WHERE evidence.contract =
                                 'market_tape_accepted_observation_evidence_v1'
                         AND evidence.evidence_scope = 'full'
                         AND NOT EXISTS (
                             SELECT 1
                             FROM mt_observation_quality_flags quality
                             WHERE quality.observation_id =
                                   evidence.observation_id
                         );

                   CREATE VIEW IF NOT EXISTS
                           mt_accepted_trend_memberships_v1 AS
                       SELECT membership.*
                       FROM mt_trend_memberships membership
                       WHERE EXISTS (
                           SELECT 1
                           FROM mt_trend_membership_lineage lineage
                           JOIN mt_accepted_full_evidence_v1 evidence
                             ON evidence.observation_id =
                                lineage.observation_id
                           WHERE lineage.trend_id = membership.trend_id
                             AND lineage.video_id = membership.video_id
                             AND lineage.contract =
                                 'market_tape_accepted_observation_evidence_v1'
                       );"""
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
            for row in connection.execute(
                """SELECT quality.*, observation.observation_key,
                          prior.observation_key AS prior_observation_key
                   FROM mt_observation_quality_flags quality
                   JOIN mt_market_observations observation
                     ON observation.observation_id = quality.observation_id
                   JOIN mt_market_observations prior
                     ON prior.observation_id = quality.prior_observation_id
                   WHERE quality.run_id = ?""",
                (run_id,),
            ).fetchall():
                payload = dict(row)
                payload.pop("observation_id", None)
                payload.pop("prior_observation_id", None)
                payload = _canonical_observation_quality_sync_payload(payload)
                records.append((
                    "observation_quality_flag",
                    payload["flag_id"],
                    payload,
                ))
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

            # A live insertion can deterministically flag an older same-time
            # observation, and schema initialization can backfill flags for
            # completed runs. Carry any such evidence into the next ordinary
            # run's outbox instead of requiring a manual global reconcile.
            queued_quality_keys = {
                _canonical_observation_quality_sync_payload(
                    json.loads(row["payload_json"])
                )["flag_id"]
                for row in connection.execute(
                    """SELECT payload_json FROM mt_sync_outbox
                       WHERE entity_type = 'observation_quality_flag'"""
                ).fetchall()
            }
            records = [
                record
                for record in records
                if not (
                    record[0] == "observation_quality_flag"
                    and record[1] in queued_quality_keys
                )
            ]
            record_keys = {
                (entity_type, entity_key)
                for entity_type, entity_key, _payload in records
            }

            def add_record_if_absent(
                entity_type: str,
                entity_key: str,
                payload: Dict[str, Any],
            ) -> None:
                key = (entity_type, entity_key)
                if key in record_keys:
                    return
                record_keys.add(key)
                records.append((entity_type, entity_key, payload))

            missing_quality: List[Tuple[Dict[str, Any], Dict[str, Any]]] = []
            for row in connection.execute(
                """SELECT quality.*, observation.observation_key,
                          prior.observation_key AS prior_observation_key
                   FROM mt_observation_quality_flags quality
                   JOIN mt_market_observations observation
                     ON observation.observation_id = quality.observation_id
                   JOIN mt_market_observations prior
                     ON prior.observation_id = quality.prior_observation_id"""
            ).fetchall():
                local = dict(row)
                payload = dict(local)
                payload.pop("observation_id", None)
                payload.pop("prior_observation_id", None)
                payload = _canonical_observation_quality_sync_payload(payload)
                key = str(payload["flag_id"])
                if (
                    key in queued_quality_keys
                    or ("observation_quality_flag", key) in record_keys
                ):
                    continue
                missing_quality.append((local, payload))

            if missing_quality:
                observation_ids = sorted({
                    int(local[column])
                    for local, _payload in missing_quality
                    for column in ("observation_id", "prior_observation_id")
                })
                parent_observations = _select_in(
                    connection,
                    "mt_market_observations",
                    "observation_id",
                    observation_ids,
                )
                parent_video_ids = sorted({
                    str(row["video_id"]) for row in parent_observations
                })
                parent_creator_ids = sorted({
                    str(row["creator_id"]) for row in parent_observations
                })
                parent_run_ids = sorted({
                    str(local["run_id"]) for local, _payload in missing_quality
                })
                for row in _select_in(
                    connection, "mt_creators", "creator_id", parent_creator_ids
                ):
                    add_record_if_absent("creator", row["creator_id"], row)
                for row in _select_in(
                    connection, "mt_collection_runs", "run_id", parent_run_ids
                ):
                    add_record_if_absent("run", row["run_id"], row)
                for row in _select_in(
                    connection, "mt_videos", "video_id", parent_video_ids
                ):
                    add_record_if_absent("video", row["video_id"], row)
                for row in parent_observations:
                    payload = dict(row)
                    payload.pop("observation_id", None)
                    add_record_if_absent(
                        "observation", payload["observation_key"], payload
                    )
                for _local, payload in missing_quality:
                    add_record_if_absent(
                        "observation_quality_flag", payload["flag_id"], payload
                    )

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
                """SELECT entity_type, entity_key FROM mt_sync_outbox
                   WHERE entity_type != 'observation_quality_flag'"""
            ).fetchall():
                entity_type = str(row["entity_type"])
                entity_key = str(row["entity_key"])
                existing.setdefault(entity_type, set()).add(entity_key)
            for row in connection.execute(
                """SELECT payload_json FROM mt_sync_outbox
                   WHERE entity_type = 'observation_quality_flag'"""
            ).fetchall():
                payload = _canonical_observation_quality_sync_payload(
                    json.loads(row["payload_json"])
                )
                existing.setdefault("observation_quality_flag", set()).add(
                    str(payload["flag_id"])
                )

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
            for row in connection.execute(
                """SELECT quality.*, observation.observation_key,
                          prior.observation_key AS prior_observation_key
                   FROM mt_observation_quality_flags quality
                   JOIN mt_market_observations observation
                     ON observation.observation_id = quality.observation_id
                   JOIN mt_market_observations prior
                     ON prior.observation_id = quality.prior_observation_id"""
            ).fetchall():
                payload = dict(row)
                payload.pop("observation_id", None)
                payload.pop("prior_observation_id", None)
                payload = _canonical_observation_quality_sync_payload(payload)
                add(
                    "observation_quality_flag",
                    payload["flag_id"],
                    payload,
                )
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
            payload = json.loads(value.pop("payload_json"))
            if value["entity_type"] == "observation_quality_flag":
                payload = _canonical_observation_quality_sync_payload(payload)
                value["entity_key"] = payload["flag_id"]
            value["payload"] = payload
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

    @staticmethod
    def _flag_counter_regressions(
        connection: sqlite3.Connection,
        *,
        detected_at: str,
        migration_backfill: bool,
        video_id: Optional[str] = None,
    ) -> int:
        """Append flags for rows below the preceding cumulative maximum.

        Comparing with the preceding maximum, rather than only the immediately
        previous raw row, keeps a bad zero from making the next still-regressed
        value look healthy. Observation keys break equal-time ties identically
        across independent SQLite spools; row ids remain local foreign keys.
        """

        video_filter = "WHERE observation.video_id = ?" if video_id else ""
        filter_parameters: Tuple[Any, ...] = (video_id,) if video_id else ()
        cursor = connection.execute(
            f"""WITH ordered AS (
                     SELECT observation.*,
                            MAX(observation.views) OVER (
                                PARTITION BY observation.video_id
                                ORDER BY observation.observed_at,
                                         observation.observation_key
                                ROWS BETWEEN UNBOUNDED PRECEDING
                                         AND 1 PRECEDING
                            ) AS prior_max_views
                     FROM mt_market_observations observation
                     {video_filter}
                 ), regressions AS (
                     SELECT ordered.*,
                            (
                                SELECT prior.observation_id
                                FROM mt_market_observations prior
                                WHERE prior.video_id = ordered.video_id
                                  AND prior.views = ordered.prior_max_views
                                  AND (
                                      prior.observed_at < ordered.observed_at
                                      OR (
                                          prior.observed_at = ordered.observed_at
                                          AND prior.observation_key
                                              < ordered.observation_key
                                      )
                                  )
                                ORDER BY prior.observed_at DESC,
                                         prior.observation_key DESC
                                LIMIT 1
                            ) AS prior_anchor_id
                     FROM ordered
                     WHERE prior_max_views IS NOT NULL
                       AND views < prior_max_views
                 )
                 INSERT INTO mt_observation_quality_flags(
                     flag_id, observation_id, run_id, video_id, source_id,
                     detected_at, observed_at, views, prior_observation_id,
                     prior_observed_at, prior_views, error_code, raw_sha256,
                     metadata_json
                 )
                 SELECT ? || regression.observation_key,
                        regression.observation_id, regression.run_id,
                        regression.video_id, regression.source_id, ?,
                        regression.observed_at, regression.views,
                        prior.observation_id, prior.observed_at, prior.views,
                        'counter_regression', regression.raw_sha256, ?
                 FROM regressions regression
                 JOIN mt_market_observations prior
                   ON prior.observation_id = regression.prior_anchor_id
                 ON CONFLICT(observation_id) DO NOTHING""",
            (
                *filter_parameters,
                COUNTER_REGRESSION_FLAG_PREFIX,
                detected_at,
                json.dumps({
                    "contract": "market_tape_observation_quality_flag_v1",
                    "detector": "monotonic_cumulative_views_v1",
                    "migration_backfill": bool(migration_backfill),
                    "raw_observation_retained": True,
                    "excluded_from_analytics": True,
                }, sort_keys=True),
            ),
        )
        return max(0, int(cursor.rowcount))

    def ingest(self, item: MarketContent, run_id: str) -> Tuple[bool, bool]:
        """Append an observation. Returns (observation_added, unique_video_added)."""
        raw_sha, raw_path, raw_bytes = self._archive_raw(item)
        observed = isoformat(item.observed_at)
        published = isoformat(item.published_at)
        age_seconds = max(0.0, (item.observed_at - item.published_at).total_seconds()) if item.published_at else None
        bucket = age_bucket(item.published_at, item.observed_at)
        regression: Optional[Dict[str, Any]] = None
        result = (False, False)

        with self.connect() as connection:
            prior_rows = [dict(row) for row in connection.execute(
                """SELECT observation.observed_at, observation.views
                   FROM mt_market_observations observation
                   WHERE observation.video_id = ?
                     AND (
                         observation.observed_at < ?
                         OR (
                             observation.observed_at = ?
                             AND observation.observation_key < ?
                         )
                     )
                     AND NOT EXISTS (
                         SELECT 1 FROM mt_observation_quality_flags quality
                         WHERE quality.observation_id = observation.observation_id
                     )
                     AND EXISTS (
                         SELECT 1 FROM mt_accepted_observation_evidence accepted
                         WHERE accepted.observation_id = observation.observation_id
                           AND accepted.contract = ?
                     )
                   ORDER BY observation.observed_at DESC,
                            observation.observation_key DESC
                   LIMIT 3""",
                (
                    item.video_id, observed, observed, item.observation_key,
                    ACCEPTED_OBSERVATION_EVIDENCE_CONTRACT,
                ),
            ).fetchall()][::-1]
            motion_rows = prior_rows + [{"observed_at": observed, "views": item.metrics.views}]
            motion = counter_motion(motion_rows)
            cohort = [row[0] for row in connection.execute(
                """SELECT observation.view_velocity
                   FROM mt_market_observations observation
                   WHERE observation.platform = ?
                     AND observation.video_age_bucket = ?
                     AND observation.observed_at >= ?
                     AND NOT EXISTS (
                         SELECT 1 FROM mt_observation_quality_flags quality
                         WHERE quality.observation_id = observation.observation_id
                     )
                     AND EXISTS (
                         SELECT 1 FROM mt_accepted_observation_evidence accepted
                         WHERE accepted.observation_id = observation.observation_id
                           AND accepted.contract = ?
                     )
                   ORDER BY observation.observed_at DESC LIMIT 500""",
                (
                    item.platform, bucket,
                    isoformat(item.observed_at - timedelta(days=30)),
                    ACCEPTED_OBSERVATION_EVIDENCE_CONTRACT,
                ),
            ).fetchall()]
            relative_strength = zscore(motion.velocity, cohort)
            prior_anchor = connection.execute(
                """SELECT observation.observation_id,
                          observation.observed_at, observation.views
                   FROM mt_market_observations observation
                   WHERE observation.video_id = ?
                     AND (
                         observation.observed_at < ?
                         OR (
                             observation.observed_at = ?
                             AND observation.observation_key < ?
                         )
                     )
                     AND NOT EXISTS (
                         SELECT 1 FROM mt_observation_quality_flags quality
                         WHERE quality.observation_id = observation.observation_id
                     )
                     AND EXISTS (
                         SELECT 1 FROM mt_accepted_observation_evidence accepted
                         WHERE accepted.observation_id = observation.observation_id
                           AND accepted.contract = ?
                     )
                   ORDER BY observation.views DESC,
                            observation.observed_at DESC,
                            observation.observation_key DESC
                   LIMIT 1""",
                (
                    item.video_id, observed, observed, item.observation_key,
                    ACCEPTED_OBSERVATION_EVIDENCE_CONTRACT,
                ),
            ).fetchone()
            regresses_prior_counter = bool(
                prior_anchor
                and int(item.metrics.views) < int(prior_anchor["views"])
            )

            connection.execute(
                """INSERT INTO mt_raw_objects(raw_sha256, object_path, bytes_compressed, first_seen_at, source_id)
                   VALUES(?, ?, ?, ?, ?) ON CONFLICT(raw_sha256) DO NOTHING""",
                (raw_sha, raw_path, raw_bytes, observed, item.source_id),
            )
            connection.execute(
                """INSERT INTO mt_creators(
                       creator_id, platform, external_id, handle, display_name, followers, first_seen_at, last_seen_at
                   ) VALUES(?, ?, ?, '', '', 0, ?, ?)
                   ON CONFLICT(creator_id) DO NOTHING""",
                (
                    item.creator_id, item.platform, item.creator_external_id,
                    observed, observed,
                ),
            )
            exists = connection.execute("SELECT 1 FROM mt_videos WHERE video_id = ?", (item.video_id,)).fetchone()
            connection.execute(
                """INSERT INTO mt_videos(
                       video_id, platform, external_id, creator_id, published_at, first_seen_at, last_seen_at,
                       title, caption, description, language, url, thumbnail_url, media_type,
                       duration_seconds, source_first_seen
                   ) VALUES(?, ?, ?, ?, NULL, ?, ?, '', '', '', '', '', '', ?, NULL, ?)
                   ON CONFLICT(video_id) DO NOTHING""",
                (
                    item.video_id, item.platform, item.external_id,
                    item.creator_id, observed, observed, item.media_type, run_id,
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
                    raw_sha, 0.0 if regresses_prior_counter else 1.0,
                ),
            )
            added = cursor.rowcount == 1
            observation_row = connection.execute(
                """SELECT observation_id, observation_key, observed_at,
                          video_id, creator_id, platform, source_id, views,
                          likes, comments, shares, saves, raw_sha256,
                          source_confidence
                   FROM mt_market_observations
                   WHERE observation_key = ?""",
                (item.observation_key,),
            ).fetchone()
            if observation_row is None:
                raise RuntimeError("observation insert did not persist")
            observation_id = int(observation_row["observation_id"])
            if added:
                self._flag_counter_regressions(
                    connection,
                    detected_at=isoformat(utc_now()),
                    migration_backfill=False,
                    video_id=item.video_id,
                )
            flagged = connection.execute(
                """SELECT quality.observation_id, quality.views,
                          quality.prior_observation_id, quality.prior_views
                   FROM mt_observation_quality_flags quality
                   WHERE quality.observation_id = ?
                     AND quality.error_code = 'counter_regression'""",
                (observation_id,),
            ).fetchone()
            exact_replay = bool(
                str(observation_row["observation_key"]) == item.observation_key
                and str(observation_row["observed_at"]) == observed
                and str(observation_row["video_id"]) == item.video_id
                and str(observation_row["creator_id"]) == item.creator_id
                and str(observation_row["platform"]) == item.platform
                and str(observation_row["source_id"]) == item.source_id
                and int(observation_row["views"]) == item.metrics.views
                and int(observation_row["likes"]) == item.metrics.likes
                and int(observation_row["comments"]) == item.metrics.comments
                and int(observation_row["shares"]) == item.metrics.shares
                and int(observation_row["saves"]) == item.metrics.saves
                and str(observation_row["raw_sha256"]) == raw_sha
                and float(observation_row["source_confidence"]) > 0
            )
            full_evidence_exists = connection.execute(
                """SELECT 1 FROM mt_accepted_observation_evidence
                   WHERE observation_id = ?
                     AND evidence_scope = 'full'
                     AND contract = ?""",
                (observation_id, ACCEPTED_OBSERVATION_EVIDENCE_CONTRACT),
            ).fetchone()
            promote_full_evidence = bool(
                flagged is None
                and exact_replay
                and full_evidence_exists is None
            )
            if promote_full_evidence:
                self._update_descriptive_state(
                    connection, item, observed, published
                )
                self._record_accepted_observation_evidence(
                    connection, item, observation_id, observed, published
                )
                self._record_discovery_attributions(connection, item, run_id, observed)
                self._upsert_genome(connection, item, observed)
                self._map_trends(
                    connection, item, observed, observation_id
                )
                self._schedule_next(
                    connection,
                    item,
                    age_seconds or 0.0,
                    motion.acceleration > 0.1 or relative_strength >= 2.0,
                )
            if flagged is not None:
                regression = dict(flagged)
            result = (added, not bool(exists))

        if regression is not None:
            raise CounterRegressionError(
                video_id=item.video_id,
                observation_id=int(regression["observation_id"]),
                views=int(regression["views"]),
                prior_observation_id=int(regression["prior_observation_id"]),
                prior_views=int(regression["prior_views"]),
            )
        return result

    @staticmethod
    def _update_descriptive_state(
        connection: sqlite3.Connection,
        item: MarketContent,
        observed: str,
        published: Optional[str],
    ) -> None:
        """Apply mutable descriptive state only after the row is accepted."""

        connection.execute(
            """UPDATE mt_creators
               SET handle = ?, display_name = ?, followers = ?, last_seen_at = ?
               WHERE creator_id = ?""",
            (
                item.creator_handle,
                item.creator_name,
                item.creator_followers,
                observed,
                item.creator_id,
            ),
        )
        connection.execute(
            """UPDATE mt_videos
               SET last_seen_at = ?, published_at = ?, title = ?, caption = ?,
                   description = ?, language = ?, url = ?, thumbnail_url = ?,
                   media_type = ?, duration_seconds = ?
               WHERE video_id = ?""",
            (
                observed,
                published,
                item.title,
                item.caption,
                item.description,
                item.language,
                item.url,
                item.thumbnail_url,
                item.media_type,
                item.duration_seconds,
                item.video_id,
            ),
        )

    @staticmethod
    def _record_accepted_observation_evidence(
        connection: sqlite3.Connection,
        item: MarketContent,
        observation_id: int,
        observed: str,
        published: Optional[str],
    ) -> None:
        """Persist the immutable accepted projection used by all analytics."""

        context = (
            item.discovery_context
            if isinstance(item.discovery_context, dict)
            else {}
        )
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
        connection.execute(
            """INSERT INTO mt_accepted_observation_evidence(
                   evidence_id, observation_id, observation_key, video_id,
                   creator_id, accepted_at, contract, evidence_scope,
                   published_at, title, caption, description, language, url,
                   thumbnail_url, media_type, duration_seconds, hashtags_json,
                   discovery_queries_json, discovery_context_json
               ) VALUES(?, ?, ?, ?, ?, ?, ?, 'full', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(observation_id, evidence_scope) DO NOTHING""",
            (
                stable_hash({
                    "contract": ACCEPTED_OBSERVATION_EVIDENCE_CONTRACT,
                    "observation_key": item.observation_key,
                    "evidence_scope": "full",
                }),
                observation_id,
                item.observation_key,
                item.video_id,
                item.creator_id,
                observed,
                ACCEPTED_OBSERVATION_EVIDENCE_CONTRACT,
                published,
                item.title,
                item.caption,
                item.description,
                item.language,
                item.url,
                item.thumbnail_url,
                item.media_type,
                item.duration_seconds,
                json.dumps(item.hashtags, sort_keys=True),
                json.dumps(queries, sort_keys=True),
                json.dumps(context, sort_keys=True, default=str),
            ),
        )

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

    def _map_trends(
        self,
        connection: sqlite3.Connection,
        item: MarketContent,
        observed: str,
        observation_id: int,
    ) -> None:
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
            connection.execute(
                """INSERT INTO mt_trend_membership_lineage(
                       trend_id, video_id, observation_id, linked_at, contract
                   ) VALUES(?, ?, ?, ?, ?)
                   ON CONFLICT(trend_id, video_id, observation_id) DO NOTHING""",
                (
                    trend_id,
                    item.video_id,
                    observation_id,
                    observed,
                    ACCEPTED_OBSERVATION_EVIDENCE_CONTRACT,
                ),
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
                """SELECT evidence.observation_id, evidence.observation_key,
                          evidence.video_id, evidence.accepted_at,
                          evidence.discovery_context_json
                   FROM mt_accepted_observation_evidence evidence
                   WHERE evidence.contract = ?
                     AND evidence.evidence_scope = 'full'
                     AND NOT EXISTS (
                         SELECT 1 FROM mt_observation_quality_flags quality
                         WHERE quality.observation_id = evidence.observation_id
                     )
                   ORDER BY evidence.accepted_at, evidence.observation_id""",
                (ACCEPTED_OBSERVATION_EVIDENCE_CONTRACT,),
            ).fetchall()
            for row in rows:
                scanned += 1
                try:
                    context = json.loads(
                        str(row["discovery_context_json"] or "{}")
                    )
                except (TypeError, ValueError, json.JSONDecodeError):
                    invalid_context += 1
                    continue
                key = _context_trend_key(context)
                if not key:
                    continue
                eligible += 1
                observed = str(row["accepted_at"])
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
                            "source_observation_key": str(
                                row["observation_key"]
                            ),
                            "contract": (
                                "discovery-context-trend-backfill-v2"
                            ),
                            "type": "topic",
                            "value": key,
                        }, sort_keys=True),
                        observed,
                    ),
                )
                membership_added = int(cursor.rowcount == 1)
                memberships_inserted += membership_added
                lineage_cursor = connection.execute(
                    """INSERT INTO mt_trend_membership_lineage(
                           trend_id, video_id, observation_id, linked_at,
                           contract
                       ) VALUES(?, ?, ?, ?, ?)
                       ON CONFLICT(trend_id, video_id, observation_id)
                       DO NOTHING""",
                    (
                        trend_id,
                        str(row["video_id"]),
                        int(row["observation_id"]),
                        observed,
                        ACCEPTED_OBSERVATION_EVIDENCE_CONTRACT,
                    ),
                )
                if membership_added or lineage_cursor.rowcount == 1:
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

    def due_polls(
        self,
        limit: int,
        *,
        as_of: Optional[datetime] = None,
        forecast_capable_platforms: Optional[Iterable[str]] = None,
        phase: str = "all",
    ) -> Dict[str, List[Dict[str, Any]]]:
        """Return the bounded refresh queue while retaining the legacy shape.

        ``due_poll_plan`` owns the auditable selection contract.  This wrapper
        keeps existing callers source-compatible while ensuring forecast
        terminal-coverage work receives the same priority everywhere.
        """

        return self.due_poll_plan(
            limit,
            as_of=as_of,
            forecast_capable_platforms=forecast_capable_platforms,
            phase=phase,
        )["polls"]

    def due_poll_plan(
        self,
        limit: int,
        *,
        as_of: Optional[datetime] = None,
        forecast_capable_platforms: Optional[Iterable[str]] = None,
        forecast_capable_source_ids: Optional[Iterable[str]] = None,
        claim_run_id: Optional[str] = None,
        phase: str = "all",
    ) -> Dict[str, Any]:
        """Plan rechecks needed to preserve prospective forecast labels.

        A trend forecast is only scorable when a measured trend observation
        lands in the final 30 minutes of its horizon.  Normal age-based polling
        can schedule every member past that narrow window.  The planner therefore
        admits one real member refresh for each uncovered exact-active-model trend
        when that window is open, even if its ordinary ``due_at`` is later.

        ``phase=forecast_terminal`` selects only active-model label coverage;
        ``phase=scheduled`` selects only ordinary due polls without claiming to
        evaluate forecast coverage; and the default ``phase=all`` preserves the
        combined recheck-mode behavior.

        Forecast work remains inside the existing per-cycle limit.  Candidate
        rows are restricted to platforms for which the collector reports a
        refresh-capable source; provider request ceilings are still enforced by
        those source instances.  One video may cover several trend forecasts, so
        a deterministic greedy set cover minimizes provider work without
        fabricating observations or changing outcome semantics.
        """

        if phase not in {"all", "forecast_terminal", "scheduled"}:
            raise ValueError(
                "phase must be all, forecast_terminal, or scheduled"
            )
        coverage_evaluated = phase in {"all", "forecast_terminal"}
        scheduled_evaluated = phase in {"all", "scheduled"}
        maximum = max(1, int(limit))
        selected_at = _as_datetime(as_of or utc_now()).astimezone(timezone.utc)
        selected_at_iso = isoformat(selected_at)
        if forecast_capable_platforms is None:
            capable_platforms = sorted({
                str(platform).strip().casefold()
                for platform in self.config.platforms
                if str(platform).strip()
            })
        else:
            capable_platforms = sorted({
                str(platform).strip().casefold()
                for platform in forecast_capable_platforms
                if str(platform).strip()
            })
        capable_source_ids = sorted({
            str(source_id).strip()
            for source_id in (forecast_capable_source_ids or ())
            if str(source_id).strip()
        })

        active_model = load_active_model(self.config) if coverage_evaluated else None
        coverage_obligations: List[Dict[str, Any]] = []
        coverage_candidates: List[Dict[str, Any]] = []
        reserved_forecast_rows: List[Dict[str, Any]] = []
        active_model_version = ""
        active_horizon = ""
        claim_receipt: Optional[Dict[str, Any]] = None
        if coverage_evaluated and claim_run_id and capable_source_ids:
            claim_receipt = self.claim_due_forecast_measurements(
                claim_run_id,
                as_of=selected_at,
                capable_source_ids=capable_source_ids,
                limit=maximum,
            )
        with self.connect() as connection:
            normal_rows = [dict(row) for row in connection.execute(
                """WITH ranked AS (
                       SELECT q.video_id, q.platform, q.external_id,
                              q.preferred_source_id, q.hot_mode, q.due_at,
                              q.failure_count, q.last_observed_at,
                              q.last_error_code,
                              v.published_at, v.title, v.caption, v.description,
                              v.language, v.url, v.thumbnail_url,
                              v.duration_seconds,
                              c.external_id AS creator_external_id,
                              c.handle AS creator_handle,
                              c.display_name AS creator_name,
                              c.followers AS creator_followers,
                              ROW_NUMBER() OVER (
                                  PARTITION BY q.platform
                                  ORDER BY q.due_at, q.failure_count, q.video_id
                              ) AS platform_rank
                       FROM mt_poll_queue q
                       JOIN mt_videos v ON v.video_id = q.video_id
                       JOIN mt_creators c ON c.creator_id = v.creator_id
                       WHERE q.due_at <= ?
                   )
                   SELECT * FROM ranked
                   WHERE platform_rank <= ?
                   ORDER BY due_at, platform, video_id""",
                (selected_at_iso, maximum),
            ).fetchall()] if scheduled_evaluated else []
            for row in normal_rows:
                row.pop("platform_rank", None)

            if coverage_evaluated:
                (
                    reserved_forecast_rows,
                    reserved_obligations,
                ) = _reserved_measurement_poll_rows(
                    connection,
                    selected_at_iso=selected_at_iso,
                    capable_source_ids=capable_source_ids,
                    capable_platforms=capable_platforms,
                    claim_run_id=claim_run_id,
                    limit=maximum,
                )
                coverage_obligations.extend(reserved_obligations)

            if coverage_evaluated and active_model is not None:
                active_model_version = str(active_model["model_version"])
                active_horizon = model_prediction_horizon(active_model)
                horizon_seconds = max(
                    0.0,
                    _prediction_horizon_hours(active_horizon) * 3600.0,
                )
                tolerance_seconds = min(
                    horizon_seconds,
                    TREND_OUTCOME_COVERAGE_TOLERANCE.total_seconds(),
                )
                lower_prediction_at = isoformat(
                    selected_at - timedelta(seconds=horizon_seconds)
                )
                upper_prediction_at = isoformat(
                    selected_at
                    - timedelta(seconds=max(0.0, horizon_seconds - tolerance_seconds))
                )
                coverage_params: Tuple[Any, ...] = (
                    active_model_version,
                    active_horizon,
                    lower_prediction_at,
                    upper_prediction_at,
                    (horizon_seconds - tolerance_seconds) / 86400.0,
                    horizon_seconds / 86400.0,
                )
                prediction_rows = connection.execute(
                    """SELECT prediction_id, subject_id AS trend_id,
                              predicted_at, model_version, horizon
                       FROM mt_predictions prediction
                       JOIN mt_trends trend
                         ON trend.trend_id = prediction.subject_id
                       WHERE subject_type = 'trend'
                         AND model_version = ?
                         AND horizon = ?
                         AND lower(trend.trend_type) != 'format'
                         AND outcome_json IS NULL
                         AND json_extract(
                                 prediction.features_json,
                                 '$.observation_quality_contract'
                             ) = 'market_tape_accepted_observation_lineage_v2'
                         AND NOT EXISTS (
                             SELECT 1
                             FROM mt_forecast_measurement_assignments assignment
                             WHERE assignment.prediction_id = prediction.prediction_id
                         )
                         AND julianday(predicted_at) > julianday(?)
                         AND julianday(predicted_at) <= julianday(?)
                         AND NOT EXISTS (
                             SELECT 1
                             FROM mt_trend_observations observation
                             WHERE observation.trend_id = prediction.subject_id
                               AND observation.observation_quality_contract =
                                   'market_tape_accepted_observation_lineage_v2'
                               AND julianday(observation.observed_at)
                                   > julianday(prediction.predicted_at)
                               AND julianday(observation.observed_at)
                                   >= julianday(prediction.predicted_at) + ?
                               AND julianday(observation.observed_at)
                                   <= julianday(prediction.predicted_at) + ?
                         )
                       ORDER BY predicted_at, prediction_id""",
                    coverage_params,
                ).fetchall()
                for raw in prediction_rows:
                    row = dict(raw)
                    predicted_at = _as_datetime(row["predicted_at"])
                    target_at = predicted_at + timedelta(seconds=horizon_seconds)
                    coverage_obligations.append({
                        "prediction_id": int(row["prediction_id"]),
                        "trend_id": str(row["trend_id"]),
                        "model_version": str(row["model_version"]),
                        "horizon": str(row["horizon"]),
                        "predicted_at": isoformat(predicted_at),
                        "coverage_window_open_at": isoformat(
                            target_at - timedelta(seconds=tolerance_seconds)
                        ),
                        "coverage_deadline_at": isoformat(target_at),
                    })

                if coverage_obligations and capable_platforms:
                    placeholders = ",".join("?" for _ in capable_platforms)
                    coverage_candidates = [dict(row) for row in connection.execute(
                        f"""WITH coverage_predictions AS (
                               SELECT DISTINCT subject_id AS trend_id
                               FROM mt_predictions prediction
                               JOIN mt_trends trend
                                 ON trend.trend_id = prediction.subject_id
                               WHERE subject_type = 'trend'
                                 AND model_version = ?
                                 AND horizon = ?
                                 AND lower(trend.trend_type) != 'format'
                                 AND outcome_json IS NULL
                                 AND json_extract(
                                         prediction.features_json,
                                         '$.observation_quality_contract'
                                     ) =
                                         'market_tape_accepted_observation_lineage_v2'
                                 AND NOT EXISTS (
                                     SELECT 1
                                     FROM mt_forecast_measurement_assignments assignment
                                     WHERE assignment.prediction_id = prediction.prediction_id
                                 )
                                 AND julianday(predicted_at) > julianday(?)
                                 AND julianday(predicted_at) <= julianday(?)
                                 AND NOT EXISTS (
                                     SELECT 1
                                     FROM mt_trend_observations observation
                                     WHERE observation.trend_id = prediction.subject_id
                                       AND observation.observation_quality_contract =
                                           'market_tape_accepted_observation_lineage_v2'
                                       AND julianday(observation.observed_at)
                                           > julianday(prediction.predicted_at)
                                       AND julianday(observation.observed_at)
                                           >= julianday(prediction.predicted_at) + ?
                                       AND julianday(observation.observed_at)
                                           <= julianday(prediction.predicted_at) + ?
                                 )
                           )
                           SELECT membership.trend_id,
                                  q.video_id, q.platform, q.external_id,
                                  q.preferred_source_id, q.hot_mode, q.due_at,
                                  q.failure_count, q.last_observed_at,
                                  q.last_error_code,
                                  v.published_at, v.title, v.caption,
                                  v.description, v.language, v.url,
                                  v.thumbnail_url, v.duration_seconds,
                                  c.external_id AS creator_external_id,
                                  c.handle AS creator_handle,
                                  c.display_name AS creator_name,
                                  c.followers AS creator_followers
                           FROM coverage_predictions forecast
                           JOIN mt_accepted_trend_memberships_v1 membership
                             ON membership.trend_id = forecast.trend_id
                           JOIN mt_poll_queue q
                             ON q.video_id = membership.video_id
                           JOIN mt_videos v ON v.video_id = q.video_id
                           JOIN mt_creators c ON c.creator_id = v.creator_id
                           WHERE q.platform IN ({placeholders})
                           ORDER BY q.failure_count,
                                    CASE WHEN q.due_at <= ? THEN 0 ELSE 1 END,
                                    q.due_at, q.video_id, membership.trend_id""",
                        (
                            *coverage_params,
                            *capable_platforms,
                            selected_at_iso,
                        ),
                    ).fetchall()]

        obligations_by_trend: Dict[str, List[Dict[str, Any]]] = {}
        for obligation in coverage_obligations:
            obligations_by_trend.setdefault(
                str(obligation["trend_id"]), []
            ).append(obligation)
        reserved_prediction_ids = {
            int(obligation["prediction_id"])
            for row in reserved_forecast_rows
            for obligation in row.get("forecast_coverage", [])
        }
        reserved_trend_ids = {
            str(obligation["trend_id"])
            for row in reserved_forecast_rows
            for obligation in row.get("forecast_coverage", [])
        }
        legacy_obligations_by_trend: Dict[str, List[Dict[str, Any]]] = {}
        for trend_id, obligations in obligations_by_trend.items():
            legacy = [
                obligation for obligation in obligations
                if int(obligation["prediction_id"]) not in reserved_prediction_ids
            ]
            if legacy:
                legacy_obligations_by_trend[trend_id] = legacy

        # An exact reservation and a pre-reservation (legacy) forecast can point
        # at the same trend member.  Keep the reservation's immutable source
        # assignment, but let that one provider observation cover both sets of
        # forecast obligations instead of spending a second queue slot/request.
        # ``coverage_candidates`` is deterministic, as is the reservation query,
        # so a legacy trend shared by several reserved videos is attached once.
        reserved_row_by_video: Dict[str, Dict[str, Any]] = {}
        for row in reserved_forecast_rows:
            reserved_row_by_video.setdefault(str(row["video_id"]), row)
        legacy_trends_covered_by_reserved: set[str] = set()
        for candidate in coverage_candidates:
            trend_id = str(candidate["trend_id"])
            if (
                trend_id in legacy_trends_covered_by_reserved
                or trend_id not in legacy_obligations_by_trend
            ):
                continue
            reserved_row = reserved_row_by_video.get(str(candidate["video_id"]))
            if reserved_row is None:
                continue
            existing_prediction_ids = {
                int(obligation["prediction_id"])
                for obligation in reserved_row.get("forecast_coverage", [])
            }
            reserved_row.setdefault("forecast_coverage", []).extend(
                obligation
                for obligation in legacy_obligations_by_trend[trend_id]
                if int(obligation["prediction_id"])
                not in existing_prediction_ids
            )
            reserved_row["forecast_coverage"].sort(
                key=lambda obligation: (
                    str(obligation["coverage_deadline_at"]),
                    int(obligation["prediction_id"]),
                )
            )
            legacy_trends_covered_by_reserved.add(trend_id)
        for trend_id in legacy_trends_covered_by_reserved:
            legacy_obligations_by_trend.pop(trend_id, None)

        legacy_forecast_rows, legacy_covered_trend_ids = (
            _select_forecast_rechecks(
                coverage_candidates,
                legacy_obligations_by_trend,
                max(0, maximum - len(reserved_forecast_rows)),
                selected_at_iso,
            )
            if coverage_evaluated
            else ([], set())
        )
        forecast_rows = [
            *reserved_forecast_rows,
            *legacy_forecast_rows,
        ]
        covered_trend_ids = (
            reserved_trend_ids
            | legacy_trends_covered_by_reserved
            | legacy_covered_trend_ids
        )
        selected: List[Dict[str, Any]] = list(forecast_rows)
        selected_video_ids = {str(row["video_id"]) for row in selected}

        normal_by_platform: Dict[str, List[Dict[str, Any]]] = {}
        for row in normal_rows:
            if str(row["video_id"]) in selected_video_ids:
                continue
            payload = dict(row)
            payload.update({
                "queue_contract": "market_tape_recheck_queue_v2",
                "queue_selected_at": selected_at_iso,
                "recheck_priority": 1,
                "recheck_reason": "scheduled_poll_due",
                "forecast_coverage": [],
            })
            normal_by_platform.setdefault(str(row["platform"]), []).append(
                payload
            )
        platforms = sorted(
            normal_by_platform,
            key=lambda platform: (
                str(normal_by_platform[platform][0]["due_at"]),
                platform,
            ),
        )
        while scheduled_evaluated and len(selected) < maximum:
            advanced = False
            for platform in platforms:
                if normal_by_platform[platform]:
                    selected.append(normal_by_platform[platform].pop(0))
                    advanced = True
                    if len(selected) >= maximum:
                        break
            if not advanced:
                break

        grouped: Dict[str, List[Dict[str, Any]]] = {}
        for row in selected:
            grouped.setdefault(str(row["platform"]), []).append(row)

        selected_obligations = [
            obligation
            for row in forecast_rows
            for obligation in row.get("forecast_coverage", [])
        ]
        due_prediction_ids = {
            int(obligation["prediction_id"])
            for obligation in coverage_obligations
        }
        selected_prediction_ids = {
            int(obligation["prediction_id"])
            for obligation in selected_obligations
        }
        candidate_trend_ids = {
            str(row["trend_id"]) for row in coverage_candidates
        } | reserved_trend_ids
        due_trend_ids = set(obligations_by_trend)
        selected_assignments = [_poll_assignment_receipt(row) for row in selected]
        if not coverage_evaluated:
            coverage_state = "not_evaluated_in_scheduled_phase"
        elif active_model is None and not coverage_obligations:
            coverage_state = "no_active_model"
        elif not coverage_obligations:
            coverage_state = "no_open_coverage_window"
        elif not capable_platforms:
            coverage_state = "no_refresh_capable_platform"
        elif not candidate_trend_ids:
            coverage_state = "no_refreshable_forecast_member"
        elif (
            due_trend_ids - candidate_trend_ids
            and (due_trend_ids & candidate_trend_ids) - covered_trend_ids
        ):
            coverage_state = "refresh_capability_and_cycle_capacity_gap"
        elif due_trend_ids - candidate_trend_ids:
            coverage_state = "refresh_capability_gap"
        elif selected_prediction_ids != due_prediction_ids:
            coverage_state = "cycle_capacity_limited"
        else:
            coverage_state = "queued"
        selection_policy = {
            "all": (
                "active_model_terminal_coverage_set_cover_then_platform_fair_due"
            ),
            "forecast_terminal": "active_model_terminal_coverage_set_cover_only",
            "scheduled": "platform_fair_scheduled_due_only",
        }[phase]
        receipt = {
            "contract": (
                "market_tape_forecast_recheck_plan_v1"
                if phase == "all"
                else "market_tape_recheck_phase_plan_v1"
            ),
            "selected_at": selected_at_iso,
            "phase": phase,
            "selection_lane": (
                "combined"
                if phase == "all"
                else phase
            ),
            "coverage_evaluated": coverage_evaluated,
            "scheduled_due_evaluated": scheduled_evaluated,
            "queue_limit": maximum,
            "selection_policy": selection_policy,
            "deduplication_key": "video_id",
            "provider_budget_policy": (
                "selection_never_exceeds_cycle_limit_and_sources_enforce_remaining_budget"
            ),
            "selected_total": len(selected),
            "selected_forecast_coverage": len(forecast_rows),
            "selected_scheduled_due": len(selected) - len(forecast_rows),
            "normal_due_candidates_loaded": len(normal_rows),
            "forecast_capable_platforms": (
                capable_platforms if coverage_evaluated else []
            ),
            "forecast_capable_source_ids": (
                capable_source_ids if coverage_evaluated else []
            ),
            "measurement_claim": claim_receipt,
            "reserved_assignments_selected": len(reserved_prediction_ids),
            "legacy_assignments_selected": (
                len(selected_prediction_ids) - len(reserved_prediction_ids)
            ),
            "active_model_version": active_model_version,
            "active_horizon": active_horizon,
            "coverage_state": coverage_state,
            "coverage_predictions_due": len(due_prediction_ids),
            "coverage_trends_due": len(due_trend_ids),
            "coverage_candidate_videos": len({
                str(row["video_id"]) for row in coverage_candidates
            }),
            "coverage_candidate_trends": len(candidate_trend_ids),
            "coverage_predictions_selected": len(selected_prediction_ids),
            "coverage_trends_selected": len(covered_trend_ids),
            "coverage_predictions_unselected": len(
                due_prediction_ids - selected_prediction_ids
            ),
            "coverage_trends_without_refreshable_member": len(
                due_trend_ids - candidate_trend_ids
            ),
            "coverage_trends_unselected_for_capacity": len(
                (due_trend_ids & candidate_trend_ids) - covered_trend_ids
            ),
            "sampling_window": {
                "terminal_tolerance_seconds": round(
                    TREND_OUTCOME_COVERAGE_TOLERANCE.total_seconds(), 3
                ),
                "closes_at_forecast_target": True,
                "post_target_refresh_does_not_create_a_label": True,
            },
            "selected_assignments": selected_assignments,
            "selection_sha256": stable_hash(selected_assignments),
        }
        return {
            "contract": "market_tape_recheck_plan_v1",
            "phase": phase,
            "polls": grouped,
            "receipt": receipt,
        }

    def remaining_request_budget(
        self,
        source_id: str,
        daily_limit: int,
        *,
        purpose: str = "legacy",
        as_of: Optional[datetime] = None,
        validation_floor: Optional[int] = None,
    ) -> int:
        """Return operation-aware provider capacity without spending it.

        Existing callers retain the historical ``legacy`` behavior until they
        explicitly identify their lane. Discovery and ordinary scheduled work
        protect the larger of durable outstanding reservations or the unused
        validation floor. Reservation formation can consume that protected
        floor, while a terminal claim sees real unused provider capacity; the
        exact claimed assignments remain its separate upper bound.
        """

        measured_at = _as_datetime(as_of or utc_now()).astimezone(timezone.utc)
        floor = (
            self.config.prediction_validation_request_floor
            if validation_floor is None
            else validation_floor
        )
        normalized_purpose = str(purpose or "legacy").strip().casefold()
        if (
            normalized_purpose in {"general", "discovery", "scheduled"}
            and load_active_model(self.config) is None
        ):
            # Preserve cold-start discovery capacity until a promoted model
            # creates a real validation obligation. Durable reservations, when
            # present, remain protected independently by the budget helper.
            floor = 0
        with self.connect() as connection:
            return _remaining_request_budget_with_connection(
                connection,
                source_id=str(source_id),
                daily_limit=max(0, int(daily_limit)),
                purpose=purpose,
                usage_date=measured_at.date().isoformat(),
                validation_floor=max(0, int(floor)),
                as_of_iso=isoformat(measured_at),
            )

    def claim_due_forecast_measurements(
        self,
        run_id: str,
        *,
        as_of: Optional[datetime] = None,
        capable_source_ids: Iterable[str],
        limit: int = 1000,
    ) -> Dict[str, Any]:
        """Atomically lease exact terminal-measurement reservations."""

        claimed_at = _as_datetime(as_of or utc_now()).astimezone(timezone.utc)
        claimed_at_iso = isoformat(claimed_at)
        source_ids = sorted({
            str(value).strip() for value in capable_source_ids
            if str(value).strip()
        })
        maximum = max(1, int(limit))
        if not source_ids:
            return {
                "contract": "market_tape_forecast_measurement_claim_v1",
                "state": "no_capable_source",
                "run_id": run_id,
                "claimed_at": claimed_at_iso,
                "reservations_claimed": 0,
                "assignments_claimed": 0,
                "reserved_request_units": 0,
                "reservation_ids": [],
                "assignments": [],
                "selection_sha256": stable_hash([]),
            }
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            if connection.execute(
                "SELECT 1 FROM mt_collection_runs WHERE run_id = ?",
                (run_id,),
            ).fetchone() is None:
                raise ValueError(f"collection run does not exist: {run_id}")

            expired_ids = [str(row[0]) for row in connection.execute(
                """SELECT reservation_id
                   FROM mt_forecast_measurement_reservations
                   WHERE state IN ('reserved', 'claimed')
                     AND deadline_at <= ?""",
                (claimed_at_iso,),
            ).fetchall()]
            if expired_ids:
                _update_measurement_assignments_state(
                    connection,
                    expired_ids,
                    state="expired",
                    completed_at=claimed_at_iso,
                    error_code="measurement_deadline_closed",
                )
                _update_measurement_reservations_state(
                    connection,
                    expired_ids,
                    state="expired",
                    completed_at=claimed_at_iso,
                    error_code="measurement_deadline_closed",
                )

            reclaimable_ids = [str(row[0]) for row in connection.execute(
                """SELECT reservation_id
                   FROM mt_forecast_measurement_reservations
                   WHERE state = 'claimed'
                     AND claim_expires_at <= ?
                     AND deadline_at > ?""",
                (claimed_at_iso, claimed_at_iso),
            ).fetchall()]
            if reclaimable_ids:
                _update_measurement_assignments_state(
                    connection,
                    reclaimable_ids,
                    state="reserved",
                    completed_at=None,
                    error_code="",
                )
                placeholders = ",".join("?" for _ in reclaimable_ids)
                connection.execute(
                    f"""UPDATE mt_forecast_measurement_reservations
                        SET state = 'reserved', claim_run_id = NULL,
                            claimed_at = NULL, claim_expires_at = NULL,
                            completed_at = NULL, error_code = '',
                            completion_json = '{{}}'
                        WHERE reservation_id IN ({placeholders})""",
                    reclaimable_ids,
                )

            placeholders = ",".join("?" for _ in source_ids)
            rows = [dict(row) for row in connection.execute(
                f"""SELECT reservation.*
                     FROM mt_forecast_measurement_reservations reservation
                     WHERE reservation.source_id IN ({placeholders})
                       AND reservation.window_open_at <= ?
                       AND reservation.deadline_at > ?
                       AND (
                           reservation.state = 'reserved'
                           OR (
                               reservation.state = 'claimed'
                               AND reservation.claim_run_id = ?
                           )
                       )
                       AND EXISTS (
                           SELECT 1
                           FROM mt_forecast_measurement_assignments assignment
                           JOIN mt_predictions prediction
                             ON prediction.prediction_id =
                                assignment.prediction_id
                           WHERE assignment.reservation_id = reservation.reservation_id
                             AND assignment.state IN ('reserved', 'claimed')
                             AND json_extract(
                                     prediction.features_json,
                                     '$.observation_quality_contract'
                                 ) =
                                     'market_tape_accepted_observation_lineage_v2'
                             AND EXISTS (
                                 SELECT 1
                                 FROM mt_accepted_trend_memberships_v1 membership
                                 WHERE membership.trend_id = assignment.trend_id
                                   AND membership.video_id = assignment.video_id
                             )
                       )
                     ORDER BY CASE reservation.state
                                  WHEN 'claimed' THEN 0 ELSE 1 END,
                              reservation.deadline_at,
                              reservation.created_at,
                              reservation.reservation_id""",
                (*source_ids, claimed_at_iso, claimed_at_iso, run_id),
            ).fetchall()]
            selected: List[Dict[str, Any]] = []
            selected_videos = 0
            ttl_seconds = max(
                60, int(self.config.prediction_measurement_claim_ttl_seconds)
            )
            for row in rows:
                remaining_video_capacity = maximum - selected_videos
                if remaining_video_capacity <= 0:
                    break
                reservation_id = str(row["reservation_id"])
                pending = [dict(assignment) for assignment in connection.execute(
                    """SELECT assignment.prediction_id, assignment.video_id,
                              assignment.state
                       FROM mt_forecast_measurement_assignments assignment
                       JOIN mt_predictions prediction
                         ON prediction.prediction_id = assignment.prediction_id
                       WHERE assignment.reservation_id = ?
                         AND assignment.state IN ('reserved', 'claimed')
                         AND json_extract(
                                 prediction.features_json,
                                 '$.observation_quality_contract'
                             ) = 'market_tape_accepted_observation_lineage_v2'
                         AND EXISTS (
                             SELECT 1
                             FROM mt_accepted_trend_memberships_v1 membership
                             WHERE membership.trend_id = assignment.trend_id
                               AND membership.video_id = assignment.video_id
                         )
                       ORDER BY CASE assignment.state
                                    WHEN 'claimed' THEN 0 ELSE 1 END,
                                assignment.video_id,
                                assignment.prediction_id""",
                    (reservation_id,),
                ).fetchall()]
                already_claimed_videos = list(dict.fromkeys(
                    str(assignment["video_id"])
                    for assignment in pending
                    if assignment["state"] == "claimed"
                ))
                if len(already_claimed_videos) > remaining_video_capacity:
                    # A prior lease was formed with a larger limit. Do not
                    # silently return only part of that existing lease.
                    continue
                selected_video_ids = list(already_claimed_videos)
                for video_id in dict.fromkeys(
                    str(assignment["video_id"])
                    for assignment in pending
                    if assignment["state"] == "reserved"
                ):
                    if len(selected_video_ids) >= remaining_video_capacity:
                        break
                    if video_id not in selected_video_ids:
                        selected_video_ids.append(video_id)
                if not selected_video_ids:
                    continue
                video_placeholders = ",".join("?" for _ in selected_video_ids)
                connection.execute(
                    f"""UPDATE mt_forecast_measurement_assignments
                        SET state = 'claimed', completed_at = NULL,
                            error_code = ''
                        WHERE reservation_id = ? AND state = 'reserved'
                          AND video_id IN ({video_placeholders})""",
                    (reservation_id, *selected_video_ids),
                )
                selected_assignments = [
                    assignment for assignment in pending
                    if str(assignment["video_id"]) in selected_video_ids
                ]
                deadline_at = _as_datetime(row["deadline_at"])
                claim_expires_at = min(
                    deadline_at,
                    claimed_at + timedelta(seconds=ttl_seconds),
                )
                connection.execute(
                    """UPDATE mt_forecast_measurement_reservations
                       SET state = 'claimed', claim_run_id = ?, claimed_at = ?,
                           claim_expires_at = ?, completed_at = NULL,
                           error_code = ''
                       WHERE reservation_id = ?
                         AND (
                             state = 'reserved'
                             OR (state = 'claimed' AND claim_run_id = ?)
                         )""",
                    (
                        run_id,
                        claimed_at_iso,
                        isoformat(claim_expires_at),
                        reservation_id,
                        run_id,
                    ),
                )
                try:
                    capability = json.loads(row.get("capability_json") or "{}")
                except (TypeError, ValueError, json.JSONDecodeError):
                    capability = {}
                units_per_batch = max(
                    1, int(capability.get("request_units_per_batch") or 1)
                )
                claim_units = (
                    math.ceil(
                        len(selected_video_ids)
                        / max(1, int(row["refresh_batch_size"]))
                    )
                    * units_per_batch
                )
                selected.append({
                    **row,
                    "video_count": len(selected_video_ids),
                    "assignment_count": len(selected_assignments),
                    "claim_request_units": claim_units,
                    "claimed_video_ids": selected_video_ids,
                    "claimed_prediction_ids": [
                        int(assignment["prediction_id"])
                        for assignment in selected_assignments
                    ],
                })
                selected_videos += len(selected_video_ids)

            reservation_ids = [
                str(row["reservation_id"]) for row in selected
            ]
            assignments_claimed = sum(
                int(row.get("assignment_count") or 0) for row in selected
            )
            reserved_units = sum(
                int(row.get("claim_request_units") or 0)
                for row in selected
            )
            claimed_selection = [{
                "reservation_id": str(row["reservation_id"]),
                "video_ids": list(row["claimed_video_ids"]),
                "prediction_ids": list(row["claimed_prediction_ids"]),
            } for row in selected]
        return {
            "contract": "market_tape_forecast_measurement_claim_v1",
            "state": "claimed" if reservation_ids else "nothing_due",
            "run_id": run_id,
            "claimed_at": claimed_at_iso,
            "claim_ttl_seconds": max(
                60, int(self.config.prediction_measurement_claim_ttl_seconds)
            ),
            "reservations_claimed": len(reservation_ids),
            "assignments_claimed": assignments_claimed,
            "reserved_request_units": reserved_units,
            "reservation_ids": reservation_ids,
            "assignments": claimed_selection,
            "selection_sha256": stable_hash(claimed_selection),
        }

    def complete_forecast_measurements(
        self,
        run_id: str,
        source_id: str,
        accepted_video_ids: Iterable[str],
        *,
        error_code: str = "",
        failure_codes_by_video: Optional[Dict[str, str]] = None,
        completed_at: Optional[datetime] = None,
    ) -> Dict[str, Any]:
        """Close one source's claimed assignments from factual ingest results."""

        completed = _as_datetime(completed_at or utc_now()).astimezone(
            timezone.utc
        )
        completed_iso = isoformat(completed)
        accepted = {
            str(value) for value in accepted_video_ids if str(value)
        }
        item_failure_codes = {
            str(video_id): str(code or "provider_item_missing")[:100]
            for video_id, code in (failure_codes_by_video or {}).items()
            if str(video_id)
        }
        completed_reservations: List[Dict[str, Any]] = []
        assignments_fulfilled = 0
        assignments_failed = 0
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            reservations = [dict(row) for row in connection.execute(
                """SELECT * FROM mt_forecast_measurement_reservations
                   WHERE state = 'claimed' AND claim_run_id = ?
                     AND source_id = ?
                   ORDER BY deadline_at, reservation_id""",
                (run_id, source_id),
            ).fetchall()]
            for reservation in reservations:
                reservation_id = str(reservation["reservation_id"])
                assignments = [dict(row) for row in connection.execute(
                    """SELECT * FROM mt_forecast_measurement_assignments
                       WHERE reservation_id = ? AND state = 'claimed'
                       ORDER BY prediction_id""",
                    (reservation_id,),
                ).fetchall()]
                fulfilled = 0
                failed = 0
                for assignment in assignments:
                    measured = str(assignment["video_id"]) in accepted
                    assignment_error = "" if measured else (
                        item_failure_codes.get(
                            str(assignment["video_id"]),
                            str(error_code or "provider_item_missing")[:100],
                        )
                    )
                    connection.execute(
                        """UPDATE mt_forecast_measurement_assignments
                           SET state = ?, completed_at = ?, error_code = ?
                           WHERE reservation_id = ? AND prediction_id = ?
                             AND state = 'claimed'""",
                        (
                            "fulfilled" if measured else "failed",
                            completed_iso,
                            assignment_error,
                            reservation_id,
                            int(assignment["prediction_id"]),
                        ),
                    )
                    fulfilled += int(measured)
                    failed += int(not measured)
                state_counts = {
                    str(row["state"]): int(row["assignment_count"])
                    for row in connection.execute(
                        """SELECT state, COUNT(*) AS assignment_count
                           FROM mt_forecast_measurement_assignments
                           WHERE reservation_id = ? GROUP BY state""",
                        (reservation_id,),
                    ).fetchall()
                }
                remaining_assignments = sum(
                    state_counts.get(state_name, 0)
                    for state_name in ("reserved", "claimed")
                )
                remaining_videos = int(connection.execute(
                    """SELECT COUNT(DISTINCT video_id)
                       FROM mt_forecast_measurement_assignments
                       WHERE reservation_id = ?
                         AND state IN ('reserved', 'claimed')""",
                    (reservation_id,),
                ).fetchone()[0] or 0)
                total_fulfilled = state_counts.get("fulfilled", 0)
                total_failed = state_counts.get("failed", 0)
                if remaining_assignments:
                    state = "reserved"
                    reservation_completed_at = None
                    reservation_error = ""
                    try:
                        capability = json.loads(
                            reservation.get("capability_json") or "{}"
                        )
                    except (TypeError, ValueError, json.JSONDecodeError):
                        capability = {}
                    units_per_batch = max(
                        1,
                        int(capability.get("request_units_per_batch") or 1),
                    )
                    remaining_request_units = (
                        math.ceil(
                            remaining_videos
                            / max(1, int(reservation["refresh_batch_size"]))
                        )
                        * units_per_batch
                    )
                else:
                    reservation_completed_at = completed_iso
                    remaining_request_units = int(
                        reservation["reserved_request_units"]
                    )
                    if total_fulfilled and total_failed:
                        state = "partial"
                    elif total_fulfilled:
                        state = "fulfilled"
                    else:
                        state = "failed"
                    reservation_error = str(error_code or (
                        "provider_item_missing" if total_failed else ""
                    ))[:100]
                completion = {
                    "contract": "market_tape_forecast_measurement_completion_v1",
                    "run_id": run_id,
                    "source_id": source_id,
                    "completed_at": completed_iso,
                    "assignments_fulfilled": fulfilled,
                    "assignments_failed": failed,
                    "cumulative_assignments_fulfilled": total_fulfilled,
                    "cumulative_assignments_failed": total_failed,
                    "assignments_remaining": remaining_assignments,
                    "videos_remaining": remaining_videos,
                    "error_code": str(error_code or "")[:100],
                    "failure_codes_by_video": item_failure_codes,
                }
                connection.execute(
                    """UPDATE mt_forecast_measurement_reservations
                       SET state = ?, completed_at = ?, error_code = ?,
                           completion_json = ?, claim_run_id = NULL,
                           claimed_at = NULL, claim_expires_at = NULL,
                           reserved_request_units = ?
                       WHERE reservation_id = ? AND state = 'claimed'
                         AND claim_run_id = ?""",
                    (
                        state,
                        reservation_completed_at,
                        reservation_error,
                        json.dumps(completion, sort_keys=True),
                        remaining_request_units,
                        reservation_id,
                        run_id,
                    ),
                )
                assignments_fulfilled += fulfilled
                assignments_failed += failed
                completed_reservations.append({
                    "reservation_id": reservation_id,
                    "state": state,
                    "assignments_fulfilled": fulfilled,
                    "assignments_failed": failed,
                    "assignments_remaining": remaining_assignments,
                })
        return {
            "contract": "market_tape_forecast_measurement_completion_v1",
            "state": "completed" if completed_reservations else "nothing_claimed",
            "run_id": run_id,
            "source_id": source_id,
            "completed_at": completed_iso,
            "reservations_completed": len(completed_reservations),
            "assignments_fulfilled": assignments_fulfilled,
            "assignments_failed": assignments_failed,
            "reservations": completed_reservations,
        }

    def reserve_adaptive_query_admissions(
        self,
        *,
        run_id: str,
        admitted_at: datetime,
        candidates: Sequence[Dict[str, Any]],
        daily_limit: int,
        family_daily_limit: int,
        cooldown_boundary: datetime,
        cooldown_hours: int,
        proposal_sha256: str,
    ) -> Dict[str, Any]:
        """Atomically reserve measured query families across all processes.

        ``BEGIN IMMEDIATE`` serializes the count-and-insert transaction at the
        SQLite database boundary. Cooldown evidence and UTC-day ceilings are
        both read only after that lock is held, so separate daemons and manual
        invocations observe one durable admission decision instead of relying
        on the collector's earlier advisory preflight.
        """

        if admitted_at.tzinfo is None:
            admitted_at = admitted_at.replace(tzinfo=timezone.utc)
        admitted_at = admitted_at.astimezone(timezone.utc)
        if cooldown_boundary.tzinfo is None:
            cooldown_boundary = cooldown_boundary.replace(tzinfo=timezone.utc)
        requested_cooldown_boundary = cooldown_boundary.astimezone(timezone.utc)
        utc_day = admitted_at.date().isoformat()
        bounded_daily = max(0, min(1000, int(daily_limit)))
        bounded_family = max(0, min(100, int(family_daily_limit)))
        bounded_cooldown = max(0, min(24 * 30, int(cooldown_hours)))
        required_cooldown_boundary = admitted_at - timedelta(hours=bounded_cooldown)
        effective_cooldown_boundary = min(
            requested_cooldown_boundary,
            required_cooldown_boundary,
        )
        normalized: List[Tuple[str, Dict[str, Any]]] = []
        seen: set[str] = set()
        for candidate in candidates:
            family = _query_family_key(candidate.get("keyword"))
            if not family or family in seen:
                continue
            seen.add(family)
            normalized.append((family, candidate))

        admitted: List[Dict[str, Any]] = []
        rejected: List[Dict[str, Any]] = []
        new_admissions = 0
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            cooldown_activity: Dict[str, Dict[str, Any]] = {}

            def record_cooldown(
                family: str,
                activity_at: str,
                source: str,
            ) -> None:
                if not family:
                    return
                activity = cooldown_activity.setdefault(family, {
                    "latest_activity_at": "",
                    "sources": set(),
                })
                activity["latest_activity_at"] = max(
                    str(activity["latest_activity_at"] or ""),
                    str(activity_at or ""),
                )
                activity["sources"].add(source)

            if bounded_cooldown > 0:
                cooldown_cutoff = isoformat(effective_cooldown_boundary)
                for row in connection.execute(
                    """SELECT query_family, admitted_at
                       FROM mt_adaptive_query_admissions
                       WHERE admitted_at >= ?""",
                    (cooldown_cutoff,),
                ).fetchall():
                    record_cooldown(
                        _query_family_key(row["query_family"]),
                        str(row["admitted_at"]),
                        "adaptive_admission",
                    )
                for row in connection.execute(
                    """SELECT query, attempted_at, metadata_json
                       FROM mt_query_attempts
                       WHERE attempted_at >= ?""",
                    (cooldown_cutoff,),
                ).fetchall():
                    try:
                        metadata = json.loads(row["metadata_json"])
                    except (TypeError, ValueError, json.JSONDecodeError):
                        metadata = {}
                    if not isinstance(metadata, dict):
                        metadata = {}
                    record_cooldown(
                        _query_family_key(
                            metadata.get("query_family") or row["query"]
                        ),
                        str(row["attempted_at"]),
                        "query_attempt",
                    )
            existing = {
                str(row["query_family"]): dict(row)
                for row in connection.execute(
                    """SELECT * FROM mt_adaptive_query_admissions
                       WHERE run_id = ?""",
                    (run_id,),
                ).fetchall()
            }
            daily_used_before = int(connection.execute(
                """SELECT COUNT(*) FROM mt_adaptive_query_admissions
                   WHERE utc_day = ?""",
                (utc_day,),
            ).fetchone()[0])
            daily_used = daily_used_before
            for family, candidate in normalized:
                prior = existing.get(family)
                if prior is not None:
                    admitted.append({
                        "admission_key": str(prior["admission_key"]),
                        "query_family": family,
                        "keyword": str(prior["keyword"]),
                        "selection_lane": str(prior["selection_lane"]),
                        "state": "already_reserved",
                    })
                    continue
                family_used = int(connection.execute(
                    """SELECT COUNT(*) FROM mt_adaptive_query_admissions
                       WHERE utc_day = ? AND query_family = ?""",
                    (utc_day, family),
                ).fetchone()[0])
                reasons: List[str] = []
                if daily_used >= bounded_daily:
                    reasons.append("adaptive_daily_budget_exhausted_atomic")
                if family_used >= bounded_family:
                    reasons.append("query_family_daily_budget_exhausted_atomic")
                cooldown = cooldown_activity.get(family)
                if bounded_cooldown > 0 and cooldown is not None:
                    reasons.append("query_family_cooldown_active_atomic")
                if reasons:
                    rejected.append({
                        "query_family": family,
                        "keyword": str(candidate.get("keyword") or ""),
                        "selection_lane": str(candidate.get("selection_lane") or ""),
                        "reasons": reasons,
                        "daily_used": daily_used,
                        "family_used": family_used,
                        "cooldown_sources": sorted(
                            cooldown["sources"] if cooldown else []
                        ),
                        "latest_cooldown_activity_at": (
                            str(cooldown["latest_activity_at"])
                            if cooldown
                            else None
                        ),
                    })
                    continue
                admission_key = stable_hash({
                    "contract": "market_tape_adaptive_query_admission_v1",
                    "run_id": run_id,
                    "utc_day": utc_day,
                    "query_family": family,
                })
                connection.execute(
                    """INSERT INTO mt_adaptive_query_admissions(
                           admission_key, run_id, utc_day, query_family, keyword,
                           selection_lane, admitted_at, proposal_sha256, evidence_json
                       ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        admission_key,
                        run_id,
                        utc_day,
                        family,
                        str(candidate.get("keyword") or ""),
                        str(candidate.get("selection_lane") or "derived_market_term"),
                        isoformat(admitted_at),
                        str(proposal_sha256 or ""),
                        json.dumps(candidate, sort_keys=True, default=str),
                    ),
                )
                daily_used += 1
                new_admissions += 1
                admitted.append({
                    "admission_key": admission_key,
                    "query_family": family,
                    "keyword": str(candidate.get("keyword") or ""),
                    "selection_lane": str(candidate.get("selection_lane") or ""),
                    "state": "reserved",
                })
        return {
            "contract": "market_tape_adaptive_query_atomic_admission_v1",
            "run_id": run_id,
            "utc_day": utc_day,
            "admitted_at": isoformat(admitted_at),
            "proposal_sha256": str(proposal_sha256 or ""),
            "daily_limit": bounded_daily,
            "family_daily_limit": bounded_family,
            "cooldown_hours": bounded_cooldown,
            "requested_cooldown_boundary": isoformat(
                requested_cooldown_boundary
            ),
            "cooldown_boundary": isoformat(effective_cooldown_boundary),
            "daily_used_before": daily_used_before,
            "daily_used_after": daily_used_before + new_admissions,
            "new_admissions": new_admissions,
            "admitted": admitted,
            "rejected": rejected,
        }

    def adaptive_query_feedback_usage(self, since: datetime) -> Dict[str, Any]:
        """Audit measured-query admissions and actual attempts after ``since``.

        The append-only admission table is the atomic budget ledger: one selected
        family is counted once even when several provider adapters fan it out.
        Planner receipts mirror admission keys through the normal outbox, while
        query attempts remain separate proof of the calls each source made.
        """

        if since.tzinfo is None:
            since = since.replace(tzinfo=timezone.utc)
        cutoff = isoformat(since.astimezone(timezone.utc))
        with self.connect() as connection:
            admission_rows = connection.execute(
                """SELECT admission_key, run_id, utc_day, query_family, keyword,
                          selection_lane, admitted_at, proposal_sha256
                   FROM mt_adaptive_query_admissions
                   WHERE admitted_at >= ?
                   ORDER BY admitted_at, admission_key""",
                (cutoff,),
            ).fetchall()
            planner_rows = connection.execute(
                """SELECT finished_at, metadata_json
                   FROM mt_source_receipts
                   WHERE source_id = 'market-tape-adaptive-query-planner'
                     AND finished_at >= ?
                   ORDER BY finished_at""",
                (cutoff,),
            ).fetchall()
            attempt_rows = connection.execute(
                """SELECT source_id, platform, query, attempted_at, request_count,
                          result_count, metadata_json
                   FROM mt_query_attempts
                   WHERE attempted_at >= ?
                   ORDER BY attempted_at""",
                (cutoff,),
            ).fetchall()

        families: Dict[str, Dict[str, Any]] = {}

        def family_record(key: str) -> Dict[str, Any]:
            return families.setdefault(key, {
                "query_family": key,
                "selection_count": 0,
                "attempt_count": 0,
                "request_count": 0,
                "result_count": 0,
                "latest_selected_at": None,
                "latest_attempted_at": None,
                "platforms": set(),
                "source_ids": set(),
            })

        planner_receipts = 0
        feedback_selections = 0
        for row in admission_rows:
            key = _query_family_key(row["query_family"])
            if not key:
                continue
            record = family_record(key)
            record["selection_count"] += 1
            record["latest_selected_at"] = max(
                str(record["latest_selected_at"] or ""),
                str(row["admitted_at"]),
            ) or None
            feedback_selections += 1
        for row in planner_rows:
            try:
                metadata = json.loads(row["metadata_json"])
            except (TypeError, ValueError, json.JSONDecodeError):
                continue
            plan = metadata.get("adaptive_query_selection")
            if not isinstance(plan, dict) or plan.get("contract") != "market_tape_adaptive_query_feedback_v1":
                continue
            planner_receipts += 1

        query_attempts = 0
        query_requests = 0
        for row in attempt_rows:
            try:
                metadata = json.loads(row["metadata_json"])
            except (TypeError, ValueError, json.JSONDecodeError):
                metadata = {}
            key = _query_family_key(metadata.get("query_family") or row["query"])
            if not key:
                continue
            record = family_record(key)
            requests = max(0, int(row["request_count"] or 0))
            record["attempt_count"] += 1
            record["request_count"] += requests
            record["result_count"] += max(0, int(row["result_count"] or 0))
            record["latest_attempted_at"] = max(
                str(record["latest_attempted_at"] or ""),
                str(row["attempted_at"]),
            ) or None
            record["platforms"].add(str(row["platform"]))
            record["source_ids"].add(str(row["source_id"]))
            query_attempts += 1
            query_requests += requests

        serialized: Dict[str, Dict[str, Any]] = {}
        for key, record in sorted(families.items()):
            payload = dict(record)
            payload["platforms"] = sorted(record["platforms"])
            payload["source_ids"] = sorted(record["source_ids"])
            payload["latest_activity_at"] = max(
                str(record["latest_selected_at"] or ""),
                str(record["latest_attempted_at"] or ""),
            ) or None
            serialized[key] = payload
        return {
            "contract": "market_tape_adaptive_query_usage_v1",
            "since": cutoff,
            "admission_rows": len(admission_rows),
            "planner_receipts": planner_receipts,
            "feedback_selections": feedback_selections,
            "query_attempts": query_attempts,
            "query_requests": query_requests,
            "families": serialized,
        }

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
                       FROM mt_accepted_trend_memberships_v1 m
                       JOIN mt_accepted_metric_observations_v1 o
                         ON o.video_id = m.video_id
                       WHERE o.run_id = ?
                         AND NOT EXISTS (
                             SELECT 1
                             FROM mt_observation_quality_flags quality
                             WHERE quality.observation_id = o.observation_id
                         )""",
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
                     FROM mt_accepted_trend_memberships_v1 m
                     JOIN mt_videos v ON v.video_id = m.video_id
                     JOIN mt_accepted_metric_observations_v1 o
                       ON o.observation_id = (
                         SELECT observation_id
                         FROM mt_accepted_metric_observations_v1 latest
                         WHERE latest.video_id = v.video_id
                           AND NOT EXISTS (
                               SELECT 1
                               FROM mt_observation_quality_flags quality
                               WHERE quality.observation_id = latest.observation_id
                           )
                         ORDER BY latest.observed_at DESC, latest.observation_id DESC LIMIT 1
                     )
                     LEFT JOIN mt_accepted_metric_observations_v1 prior
                       ON prior.observation_id = (
                           SELECT previous.observation_id
                           FROM mt_accepted_metric_observations_v1 previous
                           WHERE previous.video_id = v.video_id
                             AND previous.observation_id != o.observation_id
                             AND previous.observed_at <= o.observed_at
                             AND NOT EXISTS (
                                 SELECT 1
                                 FROM mt_observation_quality_flags quality
                                 WHERE quality.observation_id = previous.observation_id
                             )
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
                           AND observation_quality_contract =
                               'market_tape_accepted_observation_lineage_v2'
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
                     AND observation_quality_contract =
                         'market_tape_accepted_observation_lineage_v2'
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
                           saturation, trend_strength, index_version,
                           observation_quality_contract, state
                       ) VALUES(
                           ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                           ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                           ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?
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
                        TREND_INDEX_VERSION, OBSERVATION_QUALITY_CONTRACT, state,
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
                          consecutive_failures, next_retry_at, error_code,
                          receipt_json
                   FROM mt_source_health WHERE source_id = ?""",
                (source_id,),
            ).fetchone()
        if row is None:
            return {"source_id": source_id, "blocked": False}
        result = dict(row)
        try:
            receipt = json.loads(result.pop("receipt_json") or "{}")
        except (TypeError, ValueError):
            receipt = {}
        metadata = receipt.get("metadata") if isinstance(receipt.get("metadata"), dict) else {}
        result["credential_fingerprint"] = str(
            metadata.get("credential_fingerprint") or ""
        )
        retry_at = datetime.fromisoformat(result["next_retry_at"]) if result.get("next_retry_at") else None
        result["blocked"] = bool(retry_at and retry_at > utc_now())
        return result

    def create_predictions(self, run_id: str, predicted_at: Optional[datetime] = None) -> int:
        """Persist transparent baselines without writing the promoted model.

        Promoted-model forecasts have stricter freshness, lineage, and outcome
        coverage requirements.  ``forecast_active_trends`` is their exclusive
        writer; keeping this run-local helper baseline-only prevents collection
        cycles from silently creating an unbounded, unmeasured validation set.
        """
        predicted_at = predicted_at or utc_now()
        predicted = isoformat(predicted_at)
        inserted = 0
        with self.connect() as connection:
            video_rows = connection.execute(
                """SELECT o.*, v.published_at
                   FROM mt_accepted_metric_observations_v1 o
                   JOIN mt_videos v ON v.video_id = o.video_id
                   WHERE o.run_id = ?
                     AND NOT EXISTS (
                         SELECT 1 FROM mt_observation_quality_flags quality
                         WHERE quality.observation_id = o.observation_id
                     )
                     AND o.observation_id = (
                       SELECT MAX(current.observation_id)
                       FROM mt_accepted_metric_observations_v1 current
                       WHERE current.run_id = o.run_id AND current.video_id = o.video_id
                         AND NOT EXISTS (
                             SELECT 1 FROM mt_observation_quality_flags quality
                             WHERE quality.observation_id = current.observation_id
                         )
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
                    "observation_quality_contract": (
                        OBSERVATION_QUALITY_CONTRACT
                    ),
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
                       FROM mt_accepted_trend_memberships_v1 membership
                       JOIN mt_accepted_metric_observations_v1 observation
                         ON observation.video_id = membership.video_id
                       WHERE observation.run_id = ?
                         AND NOT EXISTS (
                             SELECT 1 FROM mt_observation_quality_flags quality
                             WHERE quality.observation_id = observation.observation_id
                         )
                   ) AND trend.observation_quality_contract =
                       'market_tape_accepted_observation_lineage_v2'
                     AND trend.trend_observation_id = (
                       SELECT MAX(current.trend_observation_id) FROM mt_trend_observations current
                       WHERE current.trend_id = trend.trend_id
                         AND current.observation_quality_contract =
                             'market_tape_accepted_observation_lineage_v2'
                   )""",
                (run_id,),
            ).fetchall()
            for row in trend_rows:
                features = {
                    "run_id": run_id,
                    "observation_quality_contract": row[
                        "observation_quality_contract"
                    ],
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
                     AND observation.observation_quality_contract =
                         'market_tape_accepted_observation_lineage_v2'
                     AND observation.state != 'dead'
                     AND observation.trend_observation_id = (
                         SELECT MAX(current.trend_observation_id)
                         FROM mt_trend_observations current
                         WHERE current.trend_id = observation.trend_id
                           AND current.observation_quality_contract =
                               'market_tape_accepted_observation_lineage_v2'
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
                    "observation_quality_contract": row[
                        "observation_quality_contract"
                    ],
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
        *,
        run_id: str = "",
        measurement_sources: Optional[Sequence[Dict[str, Any]]] = None,
    ) -> Dict[str, Any]:
        """Atomically admit a bounded, measurable promoted-model cohort.

        Every written prediction receives one exact provider/video measurement
        assignment in the same ``BEGIN IMMEDIATE`` transaction. Calls without
        an existing collection run and explicit refresh-capability receipts fail
        closed with zero predictions. This is intentionally the sole writer for
        the promoted model.
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
        capabilities = _normalize_measurement_capabilities(
            measurement_sources or ()
        )
        if not str(run_id).strip() or not capabilities:
            return {
                "state": "blocked_measurement_capacity",
                "reason": (
                    "missing_run_id"
                    if not str(run_id).strip()
                    else "no_refresh_capable_measurement_source"
                ),
                "model_version": active_model["model_version"],
                "horizon": model_prediction_horizon(active_model),
                "predictions_added": 0,
                "reservations_added": 0,
                "assignments_added": 0,
                "skipped_no_measurement_capacity": 0,
                "outbox_records": 0,
            }
        predicted = isoformat(predicted_at)
        inserted_ids: List[int] = []
        skipped_ineligible = 0
        skipped_insufficient_support = 0
        skipped_stale = 0
        skipped_duplicate = 0
        skipped_subject_cooldown = 0
        skipped_no_refreshable_member = 0
        skipped_no_measurement_capacity = 0
        abstentions: List[Dict[str, Any]] = []
        abstention_reasons: Dict[str, int] = {}
        model_horizon = model_prediction_horizon(active_model)
        horizon_hours = _prediction_horizon_hours(model_horizon)
        source_max_age = min(
            TREND_OUTCOME_COVERAGE_TOLERANCE,
            timedelta(hours=max(0.0, horizon_hours)),
        )
        cohort_limit = min(
            100,
            max(1, int(self.config.prediction_validation_cohort_limit)),
            max(1, int(self.config.max_due_rechecks_per_cycle)),
            max(1, int(limit)),
        )
        cohort_interval_seconds = max(
            60,
            int(self.config.prediction_validation_interval_seconds),
        )
        target_at = predicted_at + timedelta(hours=horizon_hours)
        window_open_at = target_at - source_max_age
        usage_date = window_open_at.date().isoformat()
        cohort_key = "mt-cohort-" + stable_hash({
            "model_version": active_model["model_version"],
            "horizon": model_horizon,
            "predicted_at": predicted,
        })
        reservation_receipts: List[Dict[str, Any]] = []
        with self.connect() as connection:
            # Serialise cohort cooldown, daily capacity, prediction, reservation,
            # and assignment writes across daemon/manual processes.
            connection.execute("BEGIN IMMEDIATE")
            run = connection.execute(
                "SELECT run_id FROM mt_collection_runs WHERE run_id = ?",
                (str(run_id),),
            ).fetchone()
            if run is None:
                return {
                    "state": "blocked_measurement_capacity",
                    "reason": "collection_run_not_found",
                    "model_version": active_model["model_version"],
                    "horizon": model_horizon,
                    "predictions_added": 0,
                    "reservations_added": 0,
                    "assignments_added": 0,
                    "skipped_no_measurement_capacity": 0,
                    "outbox_records": 0,
                }
            latest_cohort = connection.execute(
                """SELECT created_at
                   FROM mt_forecast_measurement_reservations
                   WHERE model_version = ? AND horizon = ?
                   ORDER BY created_at DESC, reservation_id DESC LIMIT 1""",
                (active_model["model_version"], model_horizon),
            ).fetchone()
            if latest_cohort is not None:
                latest_created_at = _as_datetime(latest_cohort["created_at"])
                next_cohort_at = latest_created_at + timedelta(
                    seconds=cohort_interval_seconds
                )
                if predicted_at < next_cohort_at:
                    return {
                        "state": "cohort_interval_active",
                        "reason": "validation_cohort_once_per_interval",
                        "model_version": active_model["model_version"],
                        "horizon": model_horizon,
                        "predictions_added": 0,
                        "reservations_added": 0,
                        "assignments_added": 0,
                        "next_cohort_at": isoformat(next_cohort_at),
                        "cohort_interval_seconds": cohort_interval_seconds,
                        "outbox_records": 0,
                    }

            available_capabilities: Dict[str, Dict[str, Any]] = {}
            for source_id, capability in capabilities.items():
                available_units = _remaining_request_budget_with_connection(
                    connection,
                    source_id=source_id,
                    daily_limit=int(capability["daily_request_limit"]),
                    purpose="reservation",
                    usage_date=usage_date,
                    validation_floor=max(
                        0, int(self.config.prediction_validation_request_floor)
                    ),
                    as_of_iso=predicted,
                )
                declared_remaining = capability.get("request_budget_remaining")
                declared_budget_date = str(
                    capability.get("request_budget_date") or ""
                )
                if declared_remaining is not None and (
                    declared_budget_date == usage_date
                    or (
                        not declared_budget_date
                        and usage_date == predicted_at.date().isoformat()
                    )
                ):
                    available_units = min(
                        available_units,
                        max(0, int(declared_remaining)),
                    )
                if available_units < int(capability["request_units_per_batch"]):
                    continue
                available_capabilities[source_id] = {
                    **capability,
                    "available_request_units": available_units,
                }
            if not available_capabilities:
                return {
                    "state": "blocked_measurement_capacity",
                    "reason": "daily_measurement_capacity_unavailable",
                    "model_version": active_model["model_version"],
                    "horizon": model_horizon,
                    "predictions_added": 0,
                    "reservations_added": 0,
                    "assignments_added": 0,
                    "skipped_no_measurement_capacity": 0,
                    "outbox_records": 0,
                }

            rows = connection.execute(
                """SELECT observation.*
                   FROM mt_trend_observations observation
                   JOIN mt_trends trend
                     ON trend.trend_id = observation.trend_id
                   WHERE observation.observed_at <= ?
                     AND observation.state != 'dead'
                     AND observation.observation_quality_contract =
                         'market_tape_accepted_observation_lineage_v2'
                     AND lower(trend.trend_type) != 'format'
                     AND observation.trend_observation_id = (
                         SELECT current.trend_observation_id
                         FROM mt_trend_observations current
                         WHERE current.trend_id = observation.trend_id
                           AND current.observed_at <= ?
                           AND current.observation_quality_contract =
                               'market_tape_accepted_observation_lineage_v2'
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
                    min(20000, max(cohort_limit, cohort_limit * 20)),
                ),
            ).fetchall()
            existing_by_subject: Dict[str, List[Dict[str, Any]]] = {}
            for prediction in connection.execute(
                """SELECT subject_id, predicted_at, features_json
                   FROM mt_predictions
                   WHERE subject_type = 'trend'
                     AND horizon = ?
                     AND json_extract(
                             features_json,
                             '$.observation_quality_contract'
                         ) = 'market_tape_accepted_observation_lineage_v2'
                     AND (
                         model_version = ?
                         OR json_extract(
                             features_json, '$.forecast_source'
                         ) = 'active_trend_snapshot'
                         OR EXISTS (
                             SELECT 1
                             FROM mt_forecast_measurement_assignments assignment
                             WHERE assignment.prediction_id = mt_predictions.prediction_id
                               AND assignment.state IN ('reserved', 'claimed')
                         )
                     )""",
                (model_horizon, active_model["model_version"]),
            ).fetchall():
                existing_by_subject.setdefault(
                    str(prediction["subject_id"]), []
                ).append(dict(prediction))
            candidates: List[Dict[str, Any]] = []
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
                prior_subject_predictions = existing_by_subject.get(
                    str(row["trend_id"]), []
                )
                if _subject_on_forecast_cooldown(
                    prior_subject_predictions,
                    predicted_at,
                    timedelta(hours=max(0.0, horizon_hours)),
                ):
                    skipped_subject_cooldown += 1
                    continue
                features = {
                    "forecast_source": "active_trend_snapshot",
                    "observation_quality_contract": row[
                        "observation_quality_contract"
                    ],
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
                candidates.append({
                    "trend_id": str(row["trend_id"]),
                    "probability": probability,
                    "features": features,
                    "trend_strength": float(row["trend_strength"]),
                    "previously_forecast": bool(prior_subject_predictions),
                })

            membership_by_trend: Dict[str, List[Dict[str, Any]]] = {}
            candidate_ids = [candidate["trend_id"] for candidate in candidates]
            for offset in range(0, len(candidate_ids), 400):
                chunk = candidate_ids[offset:offset + 400]
                placeholders = ",".join("?" for _ in chunk)
                for membership in connection.execute(
                    f"""SELECT membership.trend_id, q.video_id, q.platform,
                               q.external_id, q.preferred_source_id, q.hot_mode,
                               q.due_at, q.failure_count, q.last_observed_at,
                               q.last_error_code, v.published_at, v.title,
                               v.caption, v.description, v.language, v.url,
                               v.thumbnail_url, v.duration_seconds,
                               c.external_id AS creator_external_id,
                               c.handle AS creator_handle,
                               c.display_name AS creator_name,
                               c.followers AS creator_followers
                        FROM mt_accepted_trend_memberships_v1 membership
                        JOIN mt_poll_queue q ON q.video_id = membership.video_id
                        JOIN mt_videos v ON v.video_id = q.video_id
                        JOIN mt_creators c ON c.creator_id = v.creator_id
                        WHERE membership.trend_id IN ({placeholders})
                        ORDER BY q.failure_count, q.due_at, q.video_id""",
                    chunk,
                ).fetchall():
                    payload = dict(membership)
                    membership_by_trend.setdefault(
                        str(payload["trend_id"]), []
                    ).append(payload)

            selected, selection_diagnostics = _select_measurement_cohort(
                candidates,
                membership_by_trend,
                available_capabilities,
                cohort_limit,
            )
            skipped_no_refreshable_member = int(
                selection_diagnostics["no_refreshable_member"]
            )
            skipped_no_measurement_capacity = int(
                selection_diagnostics["no_measurement_capacity"]
            )
            selected_by_source: Dict[str, List[Dict[str, Any]]] = {}
            for selection in selected:
                selected_by_source.setdefault(
                    str(selection["source_id"]), []
                ).append(selection)

            for source_id, selections in sorted(selected_by_source.items()):
                capability = available_capabilities[source_id]
                unique_videos = sorted({
                    str(selection["video_id"]) for selection in selections
                })
                batch_size = int(capability["refresh_batch_size"])
                request_units = (
                    math.ceil(len(unique_videos) / batch_size)
                    * int(capability["request_units_per_batch"])
                )
                canonical_selection = [{
                    "trend_id": str(selection["trend_id"]),
                    "video_id": str(selection["video_id"]),
                } for selection in sorted(
                    selections,
                    key=lambda value: (value["trend_id"], value["video_id"]),
                )]
                selection_sha256 = stable_hash(canonical_selection)
                reservation_id = "mt-reservation-" + stable_hash({
                    "cohort_key": cohort_key,
                    "source_id": source_id,
                    "selection_sha256": selection_sha256,
                })
                safe_capability = {
                    key: capability[key]
                    for key in (
                        "source_id", "platform", "daily_request_limit",
                        "refresh_batch_size", "request_units_per_batch",
                        "credential_fingerprint", "available_request_units",
                        "request_budget_date",
                    )
                }
                connection.execute(
                    """INSERT INTO mt_forecast_measurement_reservations(
                           reservation_id, cohort_key, created_run_id, created_at,
                           model_version, horizon, source_id, platform,
                           window_open_at, deadline_at, usage_date,
                           reserved_request_units, refresh_batch_size,
                           credential_fingerprint, state, selection_sha256,
                           capability_json
                       ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                                'reserved', ?, ?)""",
                    (
                        reservation_id,
                        cohort_key,
                        str(run_id),
                        predicted,
                        active_model["model_version"],
                        model_horizon,
                        source_id,
                        capability["platform"],
                        isoformat(window_open_at),
                        isoformat(target_at),
                        usage_date,
                        request_units,
                        batch_size,
                        capability["credential_fingerprint"],
                        selection_sha256,
                        json.dumps(safe_capability, sort_keys=True),
                    ),
                )
                for selection in selections:
                    probability = float(selection["probability"])
                    peak_hours = max(0.5, 6.0 * (1.0 - probability))
                    features = {
                        **selection["features"],
                        "forecast_cohort_key": cohort_key,
                        "measurement_reservation_id": reservation_id,
                        "measurement_source_id": source_id,
                        "measurement_video_id": selection["video_id"],
                        "measurement_window_open_at": isoformat(window_open_at),
                        "measurement_deadline_at": isoformat(target_at),
                    }
                    cursor = connection.execute(
                        """INSERT INTO mt_predictions(
                               subject_type, subject_id, model_version,
                               predicted_at, horizon, probability,
                               expected_peak_at, expected_remaining_life_hours,
                               features_json
                           ) VALUES('trend', ?, ?, ?, ?, ?, ?, ?, ?)""",
                        (
                            selection["trend_id"],
                            active_model["model_version"],
                            predicted,
                            model_horizon,
                            probability,
                            isoformat(
                                predicted_at + timedelta(hours=peak_hours)
                            ),
                            round(6.0 + 42.0 * probability, 3),
                            json.dumps(features, sort_keys=True),
                        ),
                    )
                    prediction_id = int(cursor.lastrowid)
                    inserted_ids.append(prediction_id)
                    connection.execute(
                        """INSERT INTO mt_forecast_measurement_assignments(
                               reservation_id, prediction_id, trend_id,
                               video_id, state
                           ) VALUES(?, ?, ?, ?, 'reserved')""",
                        (
                            reservation_id,
                            prediction_id,
                            selection["trend_id"],
                            selection["video_id"],
                        ),
                    )
                reservation_receipts.append({
                    "reservation_id": reservation_id,
                    "source_id": source_id,
                    "platform": capability["platform"],
                    "prediction_count": len(selections),
                    "unique_video_count": len(unique_videos),
                    "reserved_request_units": request_units,
                    "selection_sha256": selection_sha256,
                })
        queued = self.enqueue_prediction_updates(inserted_ids)
        if candidates and not inserted_ids:
            state = "blocked_measurement_capacity"
            reason = "no_refreshable_member_or_request_capacity"
        else:
            state = "completed"
            reason = ""
        return {
            "state": state,
            "reason": reason,
            "model_version": active_model["model_version"],
            "model_purpose": model_purpose(active_model),
            "horizon": model_horizon,
            "predicted_at": predicted,
            "predictions_added": len(inserted_ids),
            "reservations_added": len(reservation_receipts),
            "assignments_added": len(inserted_ids),
            "cohort_key": cohort_key,
            "cohort_limit": cohort_limit,
            "cohort_interval_seconds": cohort_interval_seconds,
            "measurement_window_open_at": isoformat(window_open_at),
            "measurement_deadline_at": isoformat(target_at),
            "reservations": reservation_receipts,
            "skipped_ineligible": skipped_ineligible,
            "skipped_insufficient_support": skipped_insufficient_support,
            "skipped_stale": skipped_stale,
            "skipped_duplicate": skipped_duplicate,
            "skipped_subject_cooldown": skipped_subject_cooldown,
            "skipped_no_refreshable_member": skipped_no_refreshable_member,
            "skipped_no_measurement_capacity": skipped_no_measurement_capacity,
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
            "measurement_policy": {
                "contract": "market_tape_forecast_measurement_reservation_v1",
                "active_model_writer": "forecast_active_trends",
                "prediction_without_assignment_allowed": False,
                "maximum_cohort_size": 100,
                "subject_cooldown_seconds": round(
                    max(0.0, horizon_hours) * 3600.0, 3
                ),
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
                     AND json_extract(
                             features_json,
                             '$.observation_quality_contract'
                         ) = 'market_tape_accepted_observation_lineage_v2'
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
                   FROM mt_accepted_metric_observations_v1 observation
                   WHERE video_id IN ({placeholders})
                     AND NOT EXISTS (
                         SELECT 1 FROM mt_observation_quality_flags quality
                         WHERE quality.observation_id = observation.observation_id
                     )
                   ORDER BY video_id, observed_at, observation_id""",
                video_ids,
            )
            trend_observations = _grouped_rows(
                connection,
                """SELECT trend_id AS subject_id, observed_at, state, trend_strength
                   FROM mt_trend_observations
                   WHERE trend_id IN ({placeholders})
                     AND observation_quality_contract =
                         'market_tape_accepted_observation_lineage_v2'
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
                """SELECT subject_type, model_version, horizon, probability,
                          outcome_json
                   FROM mt_predictions
                   WHERE outcome_json IS NOT NULL
                     AND json_extract(
                             features_json,
                             '$.observation_quality_contract'
                         ) = 'market_tape_accepted_observation_lineage_v2'"""
            ).fetchall()]
            pending = int(connection.execute(
                """SELECT COUNT(*) FROM mt_predictions
                   WHERE outcome_json IS NULL
                     AND json_extract(
                             features_json,
                             '$.observation_quality_contract'
                         ) = 'market_tape_accepted_observation_lineage_v2'"""
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
                           computed_at, subject_type, model_version, horizon,
                           observation_quality_contract, state,
                           labels, positives, brier_score, brier_skill_score, log_loss,
                           expected_calibration_error, roc_auc, calibration_bins_json)
                       VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (
                        computed_at, model["subject_type"], model["model_version"],
                        model["horizon"], OBSERVATION_QUALITY_CONTRACT,
                        model["state"], model["labels"],
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
                   WHERE observation_quality_contract = ?
                   ORDER BY computed_at DESC, calibration_id DESC LIMIT ?""",
                (OBSERVATION_QUALITY_CONTRACT, limit),
            ).fetchall()
        history = []
        for row in rows:
            item = dict(row)
            item["calibration_bins"] = json.loads(item.pop("calibration_bins_json"))
            history.append(item)
        return history

    def enqueue_script_language_demand(
        self,
        payload: Dict[str, Any],
    ) -> Dict[str, Any]:
        """Append one deterministic script-language demand request.

        One non-final demand represents each normalized semantic request.
        Refreshed evidence snapshots append lineage to that active demand.
        After it becomes final, a later snapshot can create new bounded work.
        """

        if not isinstance(payload, dict):
            raise TypeError("script language demand payload must be an object")
        contract = _required_script_demand_text(
            payload.get("contract") or SCRIPT_LANGUAGE_DEMAND_CONTRACT,
            "contract",
            200,
        )
        if contract != SCRIPT_LANGUAGE_DEMAND_CONTRACT:
            raise ValueError(
                f"contract must be {SCRIPT_LANGUAGE_DEMAND_CONTRACT}"
            )
        topic = _required_script_demand_text(payload.get("topic"), "topic", 500)
        audience = _required_script_demand_text(
            payload.get("audience"), "audience", 1000
        )
        objective = _required_script_demand_text(
            payload.get("objective"), "objective", 1000
        )
        snapshot_id = _required_script_demand_text(
            payload.get("snapshot_id"), "snapshot_id", 500
        )
        source_service = _required_script_demand_text(
            payload.get("source_service"), "source_service", 200
        )
        source_receipt_id = _required_script_demand_text(
            payload.get("source_receipt_id"), "source_receipt_id", 500
        )
        targets = _normalize_script_demand_targets(payload.get("targets"))
        if not isinstance(targets, dict):
            raise ValueError("targets must be an object")
        for field in ("verified_transcripts", "distinct_creators"):
            try:
                target_value = int(targets.get(field))
            except (TypeError, ValueError) as exc:
                raise ValueError(f"targets.{field} must be a positive integer") from exc
            if target_value < 1:
                raise ValueError(f"targets.{field} must be a positive integer")
            targets[field] = target_value
        if "observed_views" in targets:
            try:
                observed_views = int(targets["observed_views"])
            except (TypeError, ValueError) as exc:
                raise ValueError(
                    "targets.observed_views must be a positive integer"
                ) from exc
            if observed_views < 1:
                raise ValueError(
                    "targets.observed_views must be a positive integer"
                )
            targets["observed_views"] = observed_views
        evidence_trend_id = " ".join(
            str(payload.get("evidence_trend_id") or "").split()
        )[:500]
        collection_run_id = " ".join(
            str(payload.get("collection_run_id") or "").split()
        )[:500]
        transcript_run_id = " ".join(
            str(payload.get("transcript_run_id") or "").split()
        )[:500]
        requested_at = _script_demand_timestamp(
            payload.get("requested_at") or payload.get("created_at") or utc_now()
        )
        semantic_key = _script_language_demand_semantic_key(
            contract=contract,
            topic=topic,
            audience=audience,
            objective=objective,
            targets=targets,
        )
        new_demand_identity = {
            "contract": contract,
            "topic": _normalize_script_demand_identity_text(topic),
            "audience": _normalize_script_demand_identity_text(audience),
            "objective": _normalize_script_demand_identity_text(objective),
            "snapshot_id": snapshot_id,
            "targets": targets,
        }
        candidate_demand_id = (
            f"script-language-demand:{stable_hash(new_demand_identity)}"
        )
        request_payload = dict(payload)
        request_payload.update({
            "contract": contract,
            "topic": topic,
            "audience": audience,
            "objective": objective,
            "snapshot_id": snapshot_id,
            "targets": targets,
            "source_service": source_service,
            "source_receipt_id": source_receipt_id,
            "evidence_trend_id": evidence_trend_id,
            "collection_run_id": collection_run_id,
            "transcript_run_id": transcript_run_id,
        })
        request_hash_payload = dict(request_payload)
        request_hash_payload.pop("requested_at", None)
        request_hash_payload.pop("created_at", None)
        request_sha256 = stable_hash(request_hash_payload)
        demand_id = candidate_demand_id
        event_id = _script_language_demand_event_id(
            demand_id, "requested", 0
        )
        with self.connect() as connection:
            connection.execute(
                """INSERT INTO mt_script_language_demand_semantics(
                       semantic_key, contract, normalized_topic,
                       normalized_audience, normalized_objective,
                       targets_json, created_at
                   ) VALUES(?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(semantic_key) DO NOTHING""",
                (
                    semantic_key,
                    contract,
                    _normalize_script_demand_identity_text(topic),
                    _normalize_script_demand_identity_text(audience),
                    _normalize_script_demand_identity_text(objective),
                    json.dumps(targets, sort_keys=True, default=str),
                    requested_at,
                ),
            )
            coalesced_demand_id = (
                _queued_script_language_demand_for_semantic_connection(
                    connection, semantic_key
                )
            )
            demand_id = coalesced_demand_id or candidate_demand_id
            if coalesced_demand_id is None:
                prior_generation = connection.execute(
                    """SELECT request_sha256
                       FROM mt_script_language_demand_events
                       WHERE demand_id = ? AND event_type = 'requested'
                       LIMIT 1""",
                    (candidate_demand_id,),
                ).fetchone()
                if (
                    prior_generation is not None
                    and str(prior_generation["request_sha256"])
                    != request_sha256
                ):
                    demand_id = "script-language-demand:" + stable_hash({
                        "base_demand_id": candidate_demand_id,
                        "request_sha256": request_sha256,
                    })
            event_id = _script_language_demand_event_id(
                demand_id, "requested", 0
            )
            cursor = connection.execute(
                """INSERT INTO mt_script_language_demand_events(
                       event_id, demand_id, event_type, attempt_no,
                       request_sha256, source_service, source_receipt_id,
                       topic, audience, objective, evidence_trend_id,
                       snapshot_id, lease_until, collection_run_id,
                       transcript_run_id, payload_json, created_at
                   ) VALUES(?, ?, 'requested', 0, ?, ?, ?, ?, ?, ?, ?, ?,
                            NULL, ?, ?, ?, ?)
                   ON CONFLICT(demand_id, event_type, attempt_no) DO NOTHING""",
                (
                    event_id,
                    demand_id,
                    request_sha256,
                    source_service,
                    source_receipt_id,
                    topic,
                    audience,
                    objective,
                    evidence_trend_id,
                    snapshot_id,
                    collection_run_id,
                    transcript_run_id,
                    json.dumps(request_payload, sort_keys=True, default=str),
                    requested_at,
                ),
            )
            lineage_id = (
                "script-language-demand-snapshot-lineage:"
                + stable_hash({
                    "contract": (
                        SCRIPT_LANGUAGE_DEMAND_SNAPSHOT_LINEAGE_CONTRACT
                    ),
                    "demand_id": demand_id,
                    "request_sha256": request_sha256,
                })
            )
            lineage_cursor = connection.execute(
                """INSERT INTO mt_script_language_demand_snapshot_lineage(
                       lineage_id, demand_id, semantic_key, snapshot_id,
                       request_sha256, source_service, source_receipt_id,
                       evidence_trend_id, payload_json, created_at
                   ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(demand_id, request_sha256) DO NOTHING""",
                (
                    lineage_id,
                    demand_id,
                    semantic_key,
                    snapshot_id,
                    request_sha256,
                    source_service,
                    source_receipt_id,
                    evidence_trend_id,
                    json.dumps(request_payload, sort_keys=True, default=str),
                    requested_at,
                ),
            )
            result = _script_language_demand_from_connection(
                connection, demand_id, requested_at
            )
            inserted = bool(cursor.rowcount == 1)
            lineage_appended = bool(lineage_cursor.rowcount == 1)
        if result is None:
            raise RuntimeError("script language demand enqueue was not durable")
        result["enqueued"] = inserted
        result["deduplicated"] = not result["enqueued"]
        result["idempotent"] = bool(not inserted and not lineage_appended)
        result["coalesced"] = bool(coalesced_demand_id)
        result["snapshot_lineage_appended"] = lineage_appended
        return result

    def script_language_demand(
        self,
        demand_id: str,
        *,
        as_of: Optional[datetime] = None,
    ) -> Optional[Dict[str, Any]]:
        """Return one demand and its complete append-only event lineage."""

        canonical_id = str(demand_id or "").strip()
        if not canonical_id:
            raise ValueError("demand_id is required")
        measured_at = _script_demand_timestamp(as_of or utc_now())
        with self.connect() as connection:
            return _script_language_demand_from_connection(
                connection, canonical_id, measured_at
            )

    def list_script_language_demands(
        self,
        limit: int = 100,
        state: Optional[str] = None,
        *,
        as_of: Optional[datetime] = None,
    ) -> List[Dict[str, Any]]:
        """List bounded demand summaries with full event lineage."""

        normalized_state = str(state or "").strip().casefold()
        allowed_states = SCRIPT_LANGUAGE_DEMAND_EVENT_TYPES
        if normalized_state and normalized_state not in allowed_states:
            raise ValueError(
                "state must be requested, claimed, completed, partial, "
                "blocked, or failed"
            )
        maximum = min(500, max(1, int(limit)))
        measured_at = _script_demand_timestamp(as_of or utc_now())
        with self.connect() as connection:
            demand_ids = [str(row["demand_id"]) for row in connection.execute(
                """WITH final AS (
                       SELECT demand_id, event_type,
                              ROW_NUMBER() OVER (
                                  PARTITION BY demand_id
                                  ORDER BY attempt_no DESC, created_at DESC,
                                           event_id DESC
                              ) AS row_number
                       FROM mt_script_language_demand_events
                       WHERE event_type IN ('completed', 'blocked', 'failed')
                   ), latest_partial AS (
                       SELECT demand_id, attempt_no,
                              ROW_NUMBER() OVER (
                                  PARTITION BY demand_id
                                  ORDER BY attempt_no DESC, created_at DESC,
                                           event_id DESC
                              ) AS row_number
                       FROM mt_script_language_demand_events
                       WHERE event_type = 'partial'
                   ), latest_claim AS (
                       SELECT demand_id, attempt_no, lease_until,
                              ROW_NUMBER() OVER (
                                  PARTITION BY demand_id
                                  ORDER BY attempt_no DESC, created_at DESC,
                                           event_id DESC
                              ) AS row_number
                       FROM mt_script_language_demand_events
                       WHERE event_type = 'claimed'
                   ), current AS (
                       SELECT request.demand_id, request.created_at,
                              CASE
                                  WHEN final.event_type IS NOT NULL
                                      THEN final.event_type
                                  WHEN latest_claim.lease_until > ?
                                   AND (
                                       latest_partial.attempt_no IS NULL
                                       OR latest_partial.attempt_no <
                                          latest_claim.attempt_no
                                   )
                                      THEN 'claimed'
                                  WHEN latest_partial.attempt_no IS NOT NULL
                                   AND latest_partial.attempt_no =
                                       latest_claim.attempt_no
                                      THEN 'partial'
                                  ELSE 'requested'
                              END AS state
                       FROM mt_script_language_demand_events request
                       LEFT JOIN final
                         ON final.demand_id = request.demand_id
                        AND final.row_number = 1
                       LEFT JOIN latest_partial
                         ON latest_partial.demand_id = request.demand_id
                        AND latest_partial.row_number = 1
                       LEFT JOIN latest_claim
                         ON latest_claim.demand_id = request.demand_id
                        AND latest_claim.row_number = 1
                       WHERE request.event_type = 'requested'
                   )
                   SELECT demand_id
                   FROM current
                   WHERE (? = '' OR state = ?)
                   ORDER BY created_at DESC, demand_id DESC
                   LIMIT ?""",
                (
                    measured_at,
                    normalized_state,
                    normalized_state,
                    maximum,
                ),
            ).fetchall()]
            return [
                demand
                for demand_id in demand_ids
                if (
                    demand := _script_language_demand_from_connection(
                        connection, demand_id, measured_at
                    )
                ) is not None
            ]

    def script_language_demand_acquisition_history(
        self,
        demand_id: str,
    ) -> Dict[str, Any]:
        """Return terminal acquisition results across one semantic lineage.

        A refreshed database snapshot may create a new authoritative demand
        generation for the same topic/audience/objective/targets.  Query-frontier
        history belongs to that semantic request, not only to its newest demand
        ID; otherwise every refresh starts again at the base query and can repeat
        provider reads indefinitely.
        """

        canonical_id = str(demand_id or "").strip()
        if not canonical_id:
            raise ValueError("demand_id is required")
        with self.connect() as connection:
            lineage = connection.execute(
                """SELECT semantic_key
                   FROM mt_script_language_demand_snapshot_lineage
                   WHERE demand_id = ?
                   ORDER BY lineage_sequence DESC
                   LIMIT 1""",
                (canonical_id,),
            ).fetchone()
            if lineage is None:
                return {
                    "contract": (
                        "market_tape_script_language_demand_"
                        "acquisition_history_v1"
                    ),
                    "semantic_key": "",
                    "demand_ids": [],
                    "events": [],
                }
            semantic_key = str(lineage["semantic_key"])
            demand_ids = [
                str(row["demand_id"])
                for row in connection.execute(
                    """SELECT DISTINCT demand_id
                       FROM mt_script_language_demand_snapshot_lineage
                       WHERE semantic_key = ?
                       ORDER BY demand_id""",
                    (semantic_key,),
                ).fetchall()
            ]
            rows = connection.execute(
                """SELECT event.*
                   FROM mt_script_language_demand_events event
                   WHERE event.demand_id IN (
                       SELECT DISTINCT peer.demand_id
                       FROM mt_script_language_demand_snapshot_lineage peer
                       WHERE peer.semantic_key = ?
                   )
                     AND event.event_type IN (
                         'completed', 'partial', 'blocked', 'failed'
                     )
                   ORDER BY event.created_at, event.event_id""",
                (semantic_key,),
            ).fetchall()
        events: List[Dict[str, Any]] = []
        for raw in rows:
            event = dict(raw)
            raw_payload = event.pop("payload_json", "{}")
            try:
                payload = json.loads(raw_payload)
            except (TypeError, ValueError, json.JSONDecodeError):
                payload = {"invalid_payload_json": True}
            event["payload"] = payload
            events.append(event)
        return {
            "contract": (
                "market_tape_script_language_demand_acquisition_history_v1"
            ),
            "semantic_key": semantic_key,
            "demand_ids": demand_ids,
            "events": events,
        }

    def claim_next_script_language_demand(
        self,
        lease_seconds: int = 300,
        *,
        expected_demand_id: str | None = None,
        as_of: Optional[datetime] = None,
        source_service: str = "script-language-demand-worker",
        source_receipt_id: str = "",
        collection_run_id: str = "",
        transcript_run_id: str = "",
        payload: Optional[Dict[str, Any]] = None,
    ) -> Optional[Dict[str, Any]]:
        """Atomically claim the oldest available demand.

        An expired claim is never mutated. It becomes historical lineage and
        the reclaim is appended with the next attempt number. When an expected
        ID is supplied, selection and comparison happen inside the same
        ``BEGIN IMMEDIATE`` transaction and a mismatch appends no claim event.
        """

        ttl = max(1, min(86400, int(lease_seconds)))
        claimed_at = _as_datetime(as_of or utc_now()).astimezone(timezone.utc)
        claimed_at_iso = isoformat(claimed_at)
        lease_until = isoformat(claimed_at + timedelta(seconds=ttl))
        expected_id = " ".join(str(expected_demand_id or "").split())
        claim_context = dict(payload or {})
        claim_source_service = " ".join(str(
            source_service
            or claim_context.get("source_service")
            or "script-language-demand-worker"
        ).split())[:200]
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            request = connection.execute(
                """SELECT request.*
                   FROM mt_script_language_demand_events request
                   WHERE request.event_type = 'requested'
                     AND request.demand_id = COALESCE((
                         SELECT candidate.demand_id
                         FROM mt_script_language_demand_snapshot_lineage
                              candidate
                         WHERE candidate.semantic_key = (
                             SELECT own.semantic_key
                             FROM mt_script_language_demand_snapshot_lineage own
                             WHERE own.demand_id = request.demand_id
                             ORDER BY own.lineage_sequence DESC
                             LIMIT 1
                         )
                         GROUP BY candidate.demand_id
                         ORDER BY MAX(candidate.lineage_sequence) DESC,
                                  candidate.demand_id DESC
                         LIMIT 1
                     ), request.demand_id)
                     AND NOT EXISTS (
                         SELECT 1
                         FROM mt_script_language_demand_events final
                         WHERE final.demand_id = request.demand_id
                           AND final.event_type IN (
                               'completed', 'blocked', 'failed'
                           )
                     )
                     AND NOT EXISTS (
                         SELECT 1
                         FROM mt_script_language_demand_events active_claim
                         WHERE active_claim.demand_id = request.demand_id
                           AND active_claim.event_type = 'claimed'
                           AND active_claim.lease_until > ?
                           AND NOT EXISTS (
                               SELECT 1
                               FROM mt_script_language_demand_events resolved
                               WHERE resolved.demand_id =
                                     active_claim.demand_id
                                 AND resolved.attempt_no =
                                     active_claim.attempt_no
                                 AND resolved.event_type IN (
                                     'completed', 'partial', 'blocked', 'failed'
                                 )
                           )
                     )
                   ORDER BY request.created_at, request.demand_id
                   LIMIT 1""",
                (claimed_at_iso,),
            ).fetchone()
            next_demand_id = (
                str(request["demand_id"]) if request is not None else None
            )
            if expected_id and next_demand_id != expected_id:
                raise ScriptLanguageDemandClaimConflict(
                    expected_id, next_demand_id
                )
            if request is None:
                return None
            demand_id = next_demand_id
            queued_request = _script_language_demand_from_connection(
                connection, demand_id, claimed_at_iso
            )
            if queued_request is None:
                raise RuntimeError("queued demand lost its request")
            prior_claim = connection.execute(
                """SELECT *
                   FROM mt_script_language_demand_events
                   WHERE demand_id = ? AND event_type = 'claimed'
                   ORDER BY attempt_no DESC, created_at DESC, event_id DESC
                   LIMIT 1""",
                (demand_id,),
            ).fetchone()
            attempt_no = (
                int(prior_claim["attempt_no"]) + 1 if prior_claim else 1
            )
            claim_source_receipt = " ".join(str(
                source_receipt_id
                or claim_context.get("source_receipt_id")
                or f"claim:{demand_id}:{attempt_no}"
            ).split())[:500]
            latest_snapshot_lineage = dict(
                queued_request.get("latest_snapshot_lineage") or {}
            )
            request_lineage_binding = {
                field: latest_snapshot_lineage.get(field)
                for field in (
                    "lineage_sequence",
                    "lineage_id",
                    "semantic_key",
                    "snapshot_id",
                    "request_sha256",
                    "source_service",
                    "source_receipt_id",
                    "evidence_trend_id",
                    "created_at",
                )
            }
            event_payload = {
                "contract": SCRIPT_LANGUAGE_DEMAND_EVENT_CONTRACT,
                "event_type": "claimed",
                "lease_seconds": ttl,
                "snapshot_lineage_count": queued_request[
                    "snapshot_lineage_count"
                ],
                "snapshot_id": queued_request["snapshot_id"],
                "request_lineage": request_lineage_binding,
                "reclaimed_expired_lease": bool(prior_claim),
                "prior_attempt_no": (
                    int(prior_claim["attempt_no"]) if prior_claim else None
                ),
                "claim": claim_context,
            }
            event_id = _script_language_demand_event_id(
                demand_id, "claimed", attempt_no
            )
            connection.execute(
                """INSERT INTO mt_script_language_demand_events(
                       event_id, demand_id, event_type, attempt_no,
                       request_sha256, source_service, source_receipt_id,
                       topic, audience, objective, evidence_trend_id,
                       snapshot_id, lease_until, collection_run_id,
                       transcript_run_id, payload_json, created_at
                   ) VALUES(?, ?, 'claimed', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                            ?, ?, ?, ?)""",
                (
                    event_id,
                    demand_id,
                    attempt_no,
                    queued_request["request_sha256"],
                    claim_source_service,
                    claim_source_receipt,
                    queued_request["topic"],
                    queued_request["audience"],
                    queued_request["objective"],
                    queued_request["evidence_trend_id"],
                    queued_request["snapshot_id"],
                    lease_until,
                    str(
                        collection_run_id
                        or queued_request["collection_run_id"]
                        or ""
                    )[:500],
                    str(
                        transcript_run_id
                        or queued_request["transcript_run_id"]
                        or ""
                    )[:500],
                    json.dumps(event_payload, sort_keys=True, default=str),
                    claimed_at_iso,
                ),
            )
            return _script_language_demand_from_connection(
                connection, demand_id, claimed_at_iso
            )

    def finish_script_language_demand(
        self,
        demand_id: str,
        attempt_no: int,
        event_type: str,
        payload: Optional[Dict[str, Any]] = None,
        *,
        as_of: Optional[datetime] = None,
        source_service: str = "",
        source_receipt_id: str = "",
        collection_run_id: str = "",
        transcript_run_id: str = "",
    ) -> Dict[str, Any]:
        """Append one idempotent attempt result for the active lease.

        ``partial`` resolves this attempt while deliberately keeping the
        demand claimable by a later explicit worker call.  Completed, blocked,
        and failed results close the demand.
        """

        canonical_id = str(demand_id or "").strip()
        if not canonical_id:
            raise ValueError("demand_id is required")
        canonical_event_type = str(event_type or "").strip().casefold()
        if canonical_event_type not in SCRIPT_LANGUAGE_DEMAND_TERMINAL_EVENTS:
            raise ValueError(
                "event_type must be completed, partial, blocked, or failed"
            )
        canonical_attempt = int(attempt_no)
        if canonical_attempt < 1:
            raise ValueError("attempt_no must be at least 1")
        finished_at = _script_demand_timestamp(as_of or utc_now())
        terminal_payload = dict(payload or {})
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            existing_final = connection.execute(
                """SELECT *
                   FROM mt_script_language_demand_events
                   WHERE demand_id = ?
                     AND event_type IN ('completed', 'blocked', 'failed')
                   ORDER BY attempt_no DESC, created_at DESC, event_id DESC
                   LIMIT 1""",
                (canonical_id,),
            ).fetchone()
            if existing_final is not None:
                if (
                    str(existing_final["event_type"]) == canonical_event_type
                    and int(existing_final["attempt_no"]) == canonical_attempt
                ):
                    result = _script_language_demand_from_connection(
                        connection, canonical_id, finished_at
                    )
                    if result is None:
                        raise RuntimeError(
                            "terminal script language demand lost its request"
                        )
                    result["appended"] = False
                    result["deduplicated"] = True
                    return result
                raise ValueError("script language demand is already final")
            existing_attempt = connection.execute(
                """SELECT *
                   FROM mt_script_language_demand_events
                   WHERE demand_id = ? AND attempt_no = ?
                     AND event_type IN (
                         'completed', 'partial', 'blocked', 'failed'
                     )
                   ORDER BY created_at DESC, event_id DESC
                   LIMIT 1""",
                (canonical_id, canonical_attempt),
            ).fetchone()
            if existing_attempt is not None:
                try:
                    existing_attempt_payload = json.loads(str(
                        existing_attempt["payload_json"] or "{}"
                    ))
                except (TypeError, ValueError, json.JSONDecodeError):
                    existing_attempt_payload = {}
                coerced_request_type = (
                    existing_attempt_payload.get("result", {}).get(
                        "requested_terminal_event_type"
                    )
                    if isinstance(existing_attempt_payload.get("result"), dict)
                    else None
                )
                if (
                    str(existing_attempt["event_type"]) == canonical_event_type
                    or coerced_request_type == canonical_event_type
                ):
                    result = _script_language_demand_from_connection(
                        connection, canonical_id, finished_at
                    )
                    if result is None:
                        raise RuntimeError(
                            "finished script language demand lost its request"
                        )
                    result["appended"] = False
                    result["deduplicated"] = True
                    return result
                raise ValueError("script language demand attempt is already finished")
            claim = connection.execute(
                """SELECT *
                   FROM mt_script_language_demand_events
                   WHERE demand_id = ? AND event_type = 'claimed'
                   ORDER BY attempt_no DESC, created_at DESC, event_id DESC
                   LIMIT 1""",
                (canonical_id,),
            ).fetchone()
            if claim is None:
                raise ValueError("script language demand has not been claimed")
            if int(claim["attempt_no"]) != canonical_attempt:
                raise ValueError("attempt_no does not own the latest claim")
            if not claim["lease_until"] or claim["lease_until"] <= finished_at:
                raise ValueError("script language demand claim lease has expired")
            try:
                claim_payload = json.loads(str(claim["payload_json"] or "{}"))
            except (TypeError, ValueError, json.JSONDecodeError):
                claim_payload = {}
            claimed_request_lineage = claim_payload.get("request_lineage")
            claimed_request_lineage = (
                claimed_request_lineage
                if isinstance(claimed_request_lineage, dict)
                else {}
            )
            current_snapshot_lineage = (
                _script_language_demand_snapshot_lineage_from_connection(
                    connection, canonical_id
                )
            )
            latest_request_lineage = max(
                current_snapshot_lineage,
                key=lambda item: int(item["lineage_sequence"]),
                default={},
            )
            if not claimed_request_lineage.get("lineage_id"):
                matching_claim_lineage = [
                    item for item in current_snapshot_lineage
                    if str(item.get("request_sha256") or "")
                    == str(claim["request_sha256"] or "")
                    and str(item.get("snapshot_id") or "")
                    == str(claim["snapshot_id"] or "")
                ]
                if matching_claim_lineage:
                    matched = max(
                        matching_claim_lineage,
                        key=lambda item: int(item["lineage_sequence"]),
                    )
                    claimed_request_lineage = {
                        field: matched.get(field)
                        for field in (
                            "lineage_sequence",
                            "lineage_id",
                            "semantic_key",
                            "snapshot_id",
                            "request_sha256",
                            "source_service",
                            "source_receipt_id",
                            "evidence_trend_id",
                            "created_at",
                        )
                    }
            claimed_lineage_id = str(
                claimed_request_lineage.get("lineage_id") or ""
            )
            latest_lineage_id = str(
                latest_request_lineage.get("lineage_id") or ""
            )
            requested_terminal_event_type = canonical_event_type
            if latest_lineage_id and claimed_lineage_id != latest_lineage_id:
                canonical_event_type = "partial"
                terminal_payload.update({
                    "goal_met": False,
                    "retry_required": True,
                    "failure_code": (
                        "NEWER_SNAPSHOT_QUEUED_DURING_CLAIM"
                    ),
                    "requested_terminal_event_type": (
                        requested_terminal_event_type
                    ),
                    "claimed_snapshot_lineage_id": claimed_lineage_id,
                    "latest_snapshot_lineage_id": latest_lineage_id,
                })
            event_payload = {
                "contract": SCRIPT_LANGUAGE_DEMAND_EVENT_CONTRACT,
                "event_type": canonical_event_type,
                "request_lineage": claimed_request_lineage,
                "latest_request_lineage_at_finish": {
                    field: latest_request_lineage.get(field)
                    for field in (
                        "lineage_sequence",
                        "lineage_id",
                        "semantic_key",
                        "snapshot_id",
                        "request_sha256",
                        "source_service",
                        "source_receipt_id",
                        "evidence_trend_id",
                        "created_at",
                    )
                },
                "result": terminal_payload,
            }
            event_id = _script_language_demand_event_id(
                canonical_id, canonical_event_type, canonical_attempt
            )
            terminal_source_service = " ".join(str(
                source_service or claim["source_service"]
            ).split())[:200]
            terminal_source_receipt = " ".join(str(
                source_receipt_id
                or f"terminal:{canonical_id}:{canonical_attempt}:"
                   f"{canonical_event_type}"
            ).split())[:500]
            cursor = connection.execute(
                """INSERT INTO mt_script_language_demand_events(
                       event_id, demand_id, event_type, attempt_no,
                       request_sha256, source_service, source_receipt_id,
                       topic, audience, objective, evidence_trend_id,
                       snapshot_id, lease_until, collection_run_id,
                       transcript_run_id, payload_json, created_at
                   ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, ?, ?,
                            ?, ?)
                   ON CONFLICT(demand_id, event_type, attempt_no) DO NOTHING""",
                (
                    event_id,
                    canonical_id,
                    canonical_event_type,
                    canonical_attempt,
                    claim["request_sha256"],
                    terminal_source_service,
                    terminal_source_receipt,
                    claim["topic"],
                    claim["audience"],
                    claim["objective"],
                    claim["evidence_trend_id"],
                    claim["snapshot_id"],
                    str(collection_run_id or claim["collection_run_id"] or "")[:500],
                    str(transcript_run_id or claim["transcript_run_id"] or "")[:500],
                    json.dumps(event_payload, sort_keys=True, default=str),
                    finished_at,
                ),
            )
            result = _script_language_demand_from_connection(
                connection, canonical_id, finished_at
            )
            appended = bool(cursor.rowcount == 1)
        if result is None:
            raise RuntimeError("script language demand terminal event was not durable")
        result["appended"] = appended
        result["deduplicated"] = not result["appended"]
        return result

    def status(self) -> Dict[str, Any]:
        today = datetime.now(timezone.utc).date().isoformat()
        with self.connect() as connection:
            schema_row = connection.execute(
                "SELECT value FROM mt_meta WHERE key = 'schema_version'"
            ).fetchone()
            database_schema_version = int(schema_row[0]) if schema_row else 0
            totals = {
                "creators": connection.execute("SELECT COUNT(*) FROM mt_creators").fetchone()[0],
                "videos": connection.execute("SELECT COUNT(*) FROM mt_videos").fetchone()[0],
                "observations": connection.execute("SELECT COUNT(*) FROM mt_market_observations").fetchone()[0],
                "observation_quality_flags": connection.execute(
                    "SELECT COUNT(*) FROM mt_observation_quality_flags"
                ).fetchone()[0],
                "analytics_eligible_observations": connection.execute(
                    "SELECT COUNT(*) FROM mt_accepted_metric_observations_v1"
                ).fetchone()[0],
                "accepted_full_observations": connection.execute(
                    "SELECT COUNT(*) FROM mt_accepted_full_evidence_v1"
                ).fetchone()[0],
                "accepted_metric_scope_evidence_rows": connection.execute(
                    """SELECT COUNT(*)
                       FROM mt_accepted_observation_evidence evidence
                       WHERE evidence.contract = ?
                         AND evidence.evidence_scope = 'metric_only'
                         AND NOT EXISTS (
                             SELECT 1
                             FROM mt_observation_quality_flags quality
                             WHERE quality.observation_id =
                                   evidence.observation_id
                         )""",
                    (ACCEPTED_OBSERVATION_EVIDENCE_CONTRACT,),
                ).fetchone()[0],
                "metric_observations_without_full_projection": connection.execute(
                    """SELECT COUNT(*)
                       FROM mt_accepted_metric_observations_v1 observation
                       WHERE NOT EXISTS (
                           SELECT 1 FROM mt_accepted_full_evidence_v1 evidence
                           WHERE evidence.observation_id =
                                 observation.observation_id
                       )"""
                ).fetchone()[0],
                "metric_videos_without_full_projection": connection.execute(
                    """SELECT COUNT(*) FROM (
                           SELECT DISTINCT observation.video_id
                           FROM mt_accepted_metric_observations_v1 observation
                           WHERE NOT EXISTS (
                               SELECT 1
                               FROM mt_accepted_full_evidence_v1 evidence
                               WHERE evidence.video_id = observation.video_id
                           )
                       )"""
                ).fetchone()[0],
                "trends": connection.execute("SELECT COUNT(*) FROM mt_trends").fetchone()[0],
                "trend_observations": connection.execute("SELECT COUNT(*) FROM mt_trend_observations").fetchone()[0],
                "quality_gated_trend_observations": connection.execute(
                    """SELECT COUNT(*) FROM mt_trend_observations
                       WHERE observation_quality_contract =
                           'market_tape_accepted_observation_lineage_v2'"""
                ).fetchone()[0],
                "trend_memberships": connection.execute(
                    "SELECT COUNT(*) FROM mt_trend_memberships"
                ).fetchone()[0],
                "accepted_trend_memberships": connection.execute(
                    "SELECT COUNT(*) FROM mt_accepted_trend_memberships_v1"
                ).fetchone()[0],
                "trend_membership_lineage_gap": connection.execute(
                    """SELECT
                           (SELECT COUNT(*) FROM mt_trend_memberships) -
                           (SELECT COUNT(*)
                            FROM mt_accepted_trend_memberships_v1)"""
                ).fetchone()[0],
                "predictions": connection.execute("SELECT COUNT(*) FROM mt_predictions").fetchone()[0],
                "quality_gated_predictions": connection.execute(
                    """SELECT COUNT(*) FROM mt_predictions
                       WHERE json_extract(
                               features_json,
                               '$.observation_quality_contract'
                           ) = 'market_tape_accepted_observation_lineage_v2'"""
                ).fetchone()[0],
                "query_attempts": connection.execute("SELECT COUNT(*) FROM mt_query_attempts").fetchone()[0],
                "adaptive_query_admissions": connection.execute(
                    "SELECT COUNT(*) FROM mt_adaptive_query_admissions"
                ).fetchone()[0],
                "script_language_demand_events": connection.execute(
                    "SELECT COUNT(*) FROM mt_script_language_demand_events"
                ).fetchone()[0],
                "due_polls": connection.execute("SELECT COUNT(*) FROM mt_poll_queue WHERE due_at <= ?", (isoformat(utc_now()),)).fetchone()[0],
            }
            demand_state_rows = connection.execute(
                """WITH final AS (
                       SELECT demand_id, event_type,
                              ROW_NUMBER() OVER (
                                  PARTITION BY demand_id
                                  ORDER BY attempt_no DESC, created_at DESC,
                                           event_id DESC
                              ) AS row_number
                       FROM mt_script_language_demand_events
                       WHERE event_type IN ('completed', 'blocked', 'failed')
                   ), latest_partial AS (
                       SELECT demand_id, attempt_no,
                              ROW_NUMBER() OVER (
                                  PARTITION BY demand_id
                                  ORDER BY attempt_no DESC, created_at DESC,
                                           event_id DESC
                              ) AS row_number
                       FROM mt_script_language_demand_events
                       WHERE event_type = 'partial'
                   ), latest_claim AS (
                       SELECT demand_id, attempt_no, lease_until,
                              ROW_NUMBER() OVER (
                                  PARTITION BY demand_id
                                  ORDER BY attempt_no DESC, created_at DESC,
                                           event_id DESC
                              ) AS row_number
                       FROM mt_script_language_demand_events
                       WHERE event_type = 'claimed'
                   ), current AS (
                       SELECT request.demand_id,
                              CASE
                                  WHEN final.event_type IS NOT NULL
                                      THEN final.event_type
                                  WHEN latest_claim.lease_until > ?
                                   AND (
                                       latest_partial.attempt_no IS NULL
                                       OR latest_partial.attempt_no <
                                          latest_claim.attempt_no
                                   )
                                      THEN 'claimed'
                                  WHEN latest_partial.attempt_no IS NOT NULL
                                   AND latest_partial.attempt_no =
                                       latest_claim.attempt_no
                                      THEN 'partial'
                                  ELSE 'requested'
                              END AS state
                       FROM mt_script_language_demand_events request
                       LEFT JOIN final
                         ON final.demand_id = request.demand_id
                        AND final.row_number = 1
                       LEFT JOIN latest_partial
                         ON latest_partial.demand_id = request.demand_id
                        AND latest_partial.row_number = 1
                       LEFT JOIN latest_claim
                         ON latest_claim.demand_id = request.demand_id
                        AND latest_claim.row_number = 1
                       WHERE request.event_type = 'requested'
                   )
                   SELECT state, COUNT(*) AS count
                   FROM current GROUP BY state ORDER BY state""",
                (isoformat(utc_now()),),
            ).fetchall()
            platform_rows = connection.execute(
                """SELECT observation.platform,
                          COUNT(DISTINCT observation.video_id) AS count
                   FROM mt_accepted_full_evidence_v1 evidence
                   JOIN mt_accepted_metric_observations_v1 observation
                     ON observation.observation_id = evidence.observation_id
                   WHERE substr(evidence.accepted_at, 1, 10) = ?
                   GROUP BY observation.platform""",
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
        for source in sources:
            try:
                receipt = json.loads(source.get("receipt_json") or "{}")
            except (TypeError, ValueError):
                receipt = {}
            metadata = receipt.get("metadata") if isinstance(receipt.get("metadata"), dict) else {}
            source["operation_state"] = source.get("state")
            for field in (
                "data_mode",
                "acquisition_state",
                "archive_readable",
                "latest_artifact_at",
                "latest_observed_at",
                "archive_age_seconds",
                "artifact_age_seconds",
                "archive_freshness_basis",
                "archive_fresh",
                "archive_stale_after_seconds",
                "scheduler",
                "watermark_advanced",
                "new_unique_count",
                "new_observation_count",
            ):
                source[field] = metadata.get(field)
        target_status = {
            platform: {
                "target": self.config.target_for(platform),
                "acquired": by_platform.get(platform, 0),
                "remaining": max(0, self.config.target_for(platform) - by_platform.get(platform, 0)),
            }
            for platform in self.config.platforms
        }
        acquired = sum(by_platform.values())
        schema_parity = database_schema_version == SCHEMA_VERSION
        demand_by_state = {
            str(row["state"]): int(row["count"]) for row in demand_state_rows
        }
        return {
            "schema_version": SCHEMA_VERSION,
            "code_schema_version": SCHEMA_VERSION,
            "database_schema_version": database_schema_version,
            "schema_parity": schema_parity,
            "service": "social-market-tape",
            "state": (
                "degraded_schema_mismatch"
                if not schema_parity
                else "running" if run and run["state"] == "running" else "ready"
            ),
            "checked_at": isoformat(utc_now()),
            "daemon": self.daemon_health(),
            "database_path": str(self.config.db_path),
            "script_language_demands": {
                "contract": SCRIPT_LANGUAGE_DEMAND_CONTRACT,
                "append_only": True,
                "total": sum(demand_by_state.values()),
                "by_state": demand_by_state,
            },
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
        query = """WITH ranked AS (
                       SELECT observation.*,
                              ROW_NUMBER() OVER (
                                  PARTITION BY observation.video_id
                                  ORDER BY observation.observed_at DESC,
                                           observation.observation_id DESC
                              ) AS row_number
                       FROM mt_accepted_metric_observations_v1 observation
                       JOIN mt_accepted_full_evidence_v1 evidence
                         ON evidence.observation_id = observation.observation_id
                   )
                   SELECT video.video_id, video.platform, video.external_id,
                          video.creator_id, evidence.published_at,
                          video.first_seen_at, video.last_seen_at,
                          evidence.title, evidence.caption,
                          evidence.description, evidence.language, evidence.url,
                          evidence.thumbnail_url, evidence.media_type,
                          evidence.duration_seconds, video.source_first_seen,
                          latest.views, latest.likes, latest.comments,
                          latest.shares, latest.saves, latest.view_velocity,
                          latest.view_acceleration, latest.relative_strength,
                          latest.observed_at
                   FROM ranked latest
                   JOIN mt_videos video ON video.video_id = latest.video_id
                   JOIN mt_accepted_full_evidence_v1 evidence
                     ON evidence.observation_id = latest.observation_id
                   WHERE latest.row_number = 1"""
        params: List[Any] = []
        if platform:
            query += " AND video.platform = ?"
            params.append(platform)
        query += " ORDER BY latest.observed_at DESC LIMIT ?"
        params.append(min(max(1, limit), 1000))
        with self.connect() as connection:
            return [dict(row) for row in connection.execute(query, params).fetchall()]

    def list_trends(self, limit: int = 100, state: Optional[str] = None) -> List[Dict[str, Any]]:
        query = """SELECT t.*, o.* FROM mt_trends t
                   JOIN mt_trend_observations o ON o.trend_observation_id = (
                       SELECT trend_observation_id FROM mt_trend_observations
                       WHERE trend_id = t.trend_id
                         AND observation_quality_contract =
                             'market_tape_accepted_observation_lineage_v2'
                       ORDER BY observed_at DESC, trend_observation_id DESC LIMIT 1
                   )"""
        params: List[Any] = []
        if state:
            query += " WHERE o.state = ?"
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
                       FROM mt_accepted_metric_observations_v1 observation
                       JOIN mt_accepted_full_evidence_v1 accepted
                         ON accepted.observation_id = observation.observation_id
                   ), observation_counts AS (
                       SELECT video_id, COUNT(*) AS observation_count
                       FROM mt_accepted_metric_observations_v1 observation
                       GROUP BY video_id
                   )
                   SELECT latest.video_id, latest.creator_id, latest.platform,
                          accepted.published_at, accepted.title,
                          accepted.caption, accepted.description, accepted.url,
                          latest.observed_at, latest.views, latest.likes, latest.comments,
                          latest.shares, latest.view_velocity,
                          accepted.hashtags_json,
                          observation_counts.observation_count,
                          accepted.discovery_queries_json
                   FROM latest
                   JOIN observation_counts ON observation_counts.video_id = latest.video_id
                   JOIN mt_accepted_full_evidence_v1 accepted
                     ON accepted.observation_id = latest.observation_id
                   WHERE latest.row_number = 1
                     AND accepted.published_at IS NOT NULL
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
        query = """SELECT * FROM mt_predictions
                   WHERE json_extract(
                           features_json,
                           '$.observation_quality_contract'
                       ) = 'market_tape_accepted_observation_lineage_v2'"""
        params: List[Any] = []
        if subject_type:
            query += " AND subject_type = ?"
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
                           AND current.observation_quality_contract =
                               'market_tape_accepted_observation_lineage_v2'
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
                           AND current.observation_quality_contract =
                               'market_tape_accepted_observation_lineage_v2'
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
                           AND json_extract(
                                   current.features_json,
                                   '$.observation_quality_contract'
                               ) =
                                   'market_tape_accepted_observation_lineage_v2'
                           AND current.predicted_at >= ?
                           AND current.predicted_at <= ?
                         ORDER BY current.predicted_at DESC,
                                  current.prediction_id DESC
                         LIMIT 1
                     )
                   WHERE observation.index_version = ?
                     AND observation.observation_quality_contract =
                         'market_tape_accepted_observation_lineage_v2'
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
                                evidence.title, evidence.caption,
                                evidence.description
                         FROM mt_accepted_trend_memberships_v1 membership
                         JOIN mt_trend_membership_lineage lineage
                           ON lineage.trend_id = membership.trend_id
                          AND lineage.video_id = membership.video_id
                          AND lineage.observation_id = (
                              SELECT current_lineage.observation_id
                              FROM mt_trend_membership_lineage current_lineage
                              JOIN mt_accepted_full_evidence_v1 current_evidence
                                ON current_evidence.observation_id =
                                   current_lineage.observation_id
                              WHERE current_lineage.trend_id =
                                    membership.trend_id
                                AND current_lineage.video_id =
                                    membership.video_id
                                AND current_lineage.contract =
                                    'market_tape_accepted_observation_evidence_v1'
                              ORDER BY current_lineage.linked_at DESC,
                                       current_lineage.observation_id DESC
                              LIMIT 1
                          )
                         JOIN mt_accepted_full_evidence_v1 evidence
                           ON evidence.observation_id = lineage.observation_id
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
                                video.platform, video.external_id,
                                evidence.title, evidence.caption, evidence.url,
                                evidence.published_at,
                                creator.handle AS creator_handle,
                                observation.observed_at, observation.views,
                                observation.likes, observation.comments,
                                observation.shares, observation.view_velocity,
                                observation.view_acceleration,
                                observation.relative_strength
                         FROM mt_accepted_trend_memberships_v1 membership
                         JOIN mt_trend_membership_lineage lineage
                           ON lineage.trend_id = membership.trend_id
                          AND lineage.video_id = membership.video_id
                          AND lineage.observation_id = (
                              SELECT current_lineage.observation_id
                              FROM mt_trend_membership_lineage current_lineage
                              JOIN mt_accepted_full_evidence_v1 current_evidence
                                ON current_evidence.observation_id =
                                   current_lineage.observation_id
                              WHERE current_lineage.trend_id =
                                    membership.trend_id
                                AND current_lineage.video_id =
                                    membership.video_id
                                AND current_lineage.contract =
                                    'market_tape_accepted_observation_evidence_v1'
                              ORDER BY current_lineage.linked_at DESC,
                                       current_lineage.observation_id DESC
                              LIMIT 1
                          )
                         JOIN mt_videos video ON video.video_id = membership.video_id
                         JOIN mt_creators creator ON creator.creator_id = video.creator_id
                         JOIN mt_accepted_full_evidence_v1 evidence
                           ON evidence.observation_id = lineage.observation_id
                         LEFT JOIN mt_accepted_metric_observations_v1 observation
                           ON observation.observation_id = (
                               SELECT current.observation_id
                               FROM mt_accepted_metric_observations_v1 current
                               WHERE current.video_id = video.video_id
                                 AND NOT EXISTS (
                                     SELECT 1
                                     FROM mt_observation_quality_flags quality
                                     WHERE quality.observation_id = current.observation_id
                                 )
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
                   FROM mt_accepted_metric_observations_v1 observation
                   WHERE observed_at >= ?"""
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


def _required_script_demand_text(
    value: Any,
    field: str,
    maximum_length: int,
) -> str:
    normalized = " ".join(str(value or "").split())
    if not normalized:
        raise ValueError(f"{field} is required")
    return normalized[:maximum_length]


def _normalize_script_demand_identity_text(value: Any) -> str:
    return " ".join(str(value or "").casefold().split())


def _script_language_demand_semantic_key(
    *,
    contract: str,
    topic: str,
    audience: str,
    objective: str,
    targets: Any,
) -> str:
    """Identify equivalent bounded work without snapshot provenance."""

    return "script-language-demand-semantic:" + stable_hash({
        "contract": contract,
        "topic": _normalize_script_demand_identity_text(topic),
        "audience": _normalize_script_demand_identity_text(audience),
        "objective": _normalize_script_demand_identity_text(objective),
        "targets": _normalize_script_demand_targets(targets),
    })


def _normalize_script_demand_targets(value: Any) -> Any:
    """Canonicalize targets as a set while retaining structured settings."""

    if value in (None, ""):
        return []
    if isinstance(value, dict):
        return _normalize_script_demand_target_value(value)
    raw_targets = value if isinstance(value, (list, tuple, set)) else [value]
    canonical: List[Any] = []
    for target in raw_targets:
        normalized = _normalize_script_demand_target_value(target)
        if normalized in (None, "", [], {}):
            continue
        canonical.append(normalized)
    unique = {
        json.dumps(item, sort_keys=True, separators=(",", ":"), default=str): item
        for item in canonical
    }
    return [unique[key] for key in sorted(unique)]


def _normalize_script_demand_target_value(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            str(key).strip().casefold(): _normalize_script_demand_target_value(item)
            for key, item in sorted(value.items(), key=lambda pair: str(pair[0]))
        }
    if isinstance(value, (list, tuple, set)):
        normalized = [
            _normalize_script_demand_target_value(item) for item in value
        ]
        unique = {
            json.dumps(item, sort_keys=True, separators=(",", ":"), default=str): item
            for item in normalized
            if item not in (None, "", [], {})
        }
        return [unique[key] for key in sorted(unique)]
    if isinstance(value, str):
        return _normalize_script_demand_identity_text(value)
    return value


def _script_demand_timestamp(value: Any) -> str:
    return isoformat(_as_datetime(value).astimezone(timezone.utc))


def _script_language_demand_event_id(
    demand_id: str,
    event_type: str,
    attempt_no: int,
) -> str:
    return "script-language-demand-event:" + stable_hash({
        "contract": SCRIPT_LANGUAGE_DEMAND_EVENT_CONTRACT,
        "demand_id": demand_id,
        "event_type": event_type,
        "attempt_no": int(attempt_no),
    })


def _backfill_script_language_demand_lineage(
    connection: sqlite3.Connection,
) -> int:
    """Seed the additive lineage registry from immutable V12 requests."""

    rows = connection.execute(
        """SELECT request.*
           FROM mt_script_language_demand_events request
           WHERE request.event_type = 'requested'
             AND NOT EXISTS (
                 SELECT 1
                 FROM mt_script_language_demand_snapshot_lineage lineage
                 WHERE lineage.demand_id = request.demand_id
                   AND lineage.request_sha256 = request.request_sha256
             )
           ORDER BY request.created_at, request.demand_id"""
    ).fetchall()
    inserted = 0
    for row in rows:
        raw_payload = str(row["payload_json"] or "{}")
        try:
            request_payload = json.loads(raw_payload)
        except (TypeError, ValueError, json.JSONDecodeError):
            continue
        if not isinstance(request_payload, dict):
            continue
        targets = _normalize_script_demand_targets(
            request_payload.get("targets")
        )
        semantic_key = _script_language_demand_semantic_key(
            contract=SCRIPT_LANGUAGE_DEMAND_CONTRACT,
            topic=str(row["topic"]),
            audience=str(row["audience"]),
            objective=str(row["objective"]),
            targets=targets,
        )
        connection.execute(
            """INSERT INTO mt_script_language_demand_semantics(
                   semantic_key, contract, normalized_topic,
                   normalized_audience, normalized_objective,
                   targets_json, created_at
               ) VALUES(?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(semantic_key) DO NOTHING""",
            (
                semantic_key,
                SCRIPT_LANGUAGE_DEMAND_CONTRACT,
                _normalize_script_demand_identity_text(row["topic"]),
                _normalize_script_demand_identity_text(row["audience"]),
                _normalize_script_demand_identity_text(row["objective"]),
                json.dumps(targets, sort_keys=True, default=str),
                str(row["created_at"]),
            ),
        )
        lineage_id = (
            "script-language-demand-snapshot-lineage:"
            + stable_hash({
                "contract": SCRIPT_LANGUAGE_DEMAND_SNAPSHOT_LINEAGE_CONTRACT,
                "demand_id": str(row["demand_id"]),
                "request_sha256": str(row["request_sha256"]),
            })
        )
        result = connection.execute(
            """INSERT INTO mt_script_language_demand_snapshot_lineage(
                   lineage_id, demand_id, semantic_key, snapshot_id,
                   request_sha256, source_service, source_receipt_id,
                   evidence_trend_id, payload_json, created_at
               ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(demand_id, request_sha256) DO NOTHING""",
            (
                lineage_id,
                str(row["demand_id"]),
                semantic_key,
                str(row["snapshot_id"]),
                str(row["request_sha256"]),
                str(row["source_service"]),
                str(row["source_receipt_id"]),
                str(row["evidence_trend_id"]),
                raw_payload,
                str(row["created_at"]),
            ),
        )
        inserted += int(result.rowcount == 1)
    return inserted


def _queued_script_language_demand_for_semantic_connection(
    connection: sqlite3.Connection,
    semantic_key: str,
) -> Optional[str]:
    """Return the authoritative semantic generation only while unresolved.

    V12 could create one demand per snapshot.  V13 backfills all of those
    requests into the semantic lineage registry.  Selecting only among
    unresolved rows would let an older duplicate resurrect after the newest
    generation closes, so authority is first resolved across *all* generations
    and only then checked for a final event.
    """

    latest = _latest_script_language_demand_for_semantic_connection(
        connection, semantic_key
    )
    if latest is None:
        return None
    demand_id = str(latest["demand_id"])
    final = connection.execute(
        """SELECT 1
           FROM mt_script_language_demand_events
           WHERE demand_id = ?
             AND event_type IN ('completed', 'blocked', 'failed')
           LIMIT 1""",
        (demand_id,),
    ).fetchone()
    return demand_id if final is None else None


def _latest_script_language_demand_for_semantic_connection(
    connection: sqlite3.Connection,
    semantic_key: str,
) -> Optional[Dict[str, Any]]:
    """Resolve the newest append-only lineage generation for one semantic."""

    row = connection.execute(
        """SELECT demand_id, semantic_key,
                  MAX(lineage_sequence) AS latest_lineage_sequence
           FROM mt_script_language_demand_snapshot_lineage
           WHERE semantic_key = ?
           GROUP BY demand_id, semantic_key
           ORDER BY latest_lineage_sequence DESC, demand_id DESC
           LIMIT 1""",
        (semantic_key,),
    ).fetchone()
    return dict(row) if row is not None else None


def _script_language_demand_snapshot_lineage_from_connection(
    connection: sqlite3.Connection,
    demand_id: str,
) -> List[Dict[str, Any]]:
    rows = connection.execute(
        """SELECT *
           FROM mt_script_language_demand_snapshot_lineage
           WHERE demand_id = ?
           ORDER BY lineage_sequence""",
        (demand_id,),
    ).fetchall()
    lineage: List[Dict[str, Any]] = []
    for row in rows:
        item = dict(row)
        raw_payload = item.pop("payload_json", "{}")
        try:
            parsed_payload = json.loads(raw_payload)
        except (TypeError, ValueError, json.JSONDecodeError):
            parsed_payload = {"invalid_payload_json": True}
        item["payload"] = parsed_payload
        lineage.append(item)
    return lineage


def _script_language_demand_from_connection(
    connection: sqlite3.Connection,
    demand_id: str,
    as_of: Any,
) -> Optional[Dict[str, Any]]:
    rows = [dict(row) for row in connection.execute(
        """SELECT *
           FROM mt_script_language_demand_events
           WHERE demand_id = ?
           ORDER BY attempt_no,
                    CASE event_type
                        WHEN 'requested' THEN 0
                        WHEN 'claimed' THEN 1
                        ELSE 2
                    END,
                    created_at, event_id""",
        (demand_id,),
    ).fetchall()]
    if not rows:
        return None
    events: List[Dict[str, Any]] = []
    for row in rows:
        event = dict(row)
        raw_payload = event.pop("payload_json", "{}")
        try:
            parsed_payload = json.loads(raw_payload)
        except (TypeError, ValueError, json.JSONDecodeError):
            parsed_payload = {"invalid_payload_json": True}
        event["payload"] = parsed_payload
        events.append(event)
    request = next(
        event for event in events if event["event_type"] == "requested"
    )
    claims = [event for event in events if event["event_type"] == "claimed"]
    attempt_results = [
        event
        for event in events
        if event["event_type"] in SCRIPT_LANGUAGE_DEMAND_TERMINAL_EVENTS
    ]
    final_events = [
        event
        for event in attempt_results
        if event["event_type"] in SCRIPT_LANGUAGE_DEMAND_FINAL_EVENTS
    ]
    latest_claim = max(
        claims,
        key=lambda event: (
            int(event["attempt_no"]),
            str(event["created_at"]),
            str(event["event_id"]),
        ),
        default=None,
    )
    latest_attempt_result = max(
        attempt_results,
        key=lambda event: (
            int(event["attempt_no"]),
            str(event["created_at"]),
            str(event["event_id"]),
        ),
        default=None,
    )
    latest_final = max(
        final_events,
        key=lambda event: (
            int(event["attempt_no"]),
            str(event["created_at"]),
            str(event["event_id"]),
        ),
        default=None,
    )
    measured_at = _script_demand_timestamp(as_of)
    latest_claim_resolved = bool(
        latest_claim
        and latest_attempt_result
        and int(latest_attempt_result["attempt_no"])
            == int(latest_claim["attempt_no"])
    )
    lease_active = bool(
        latest_claim
        and latest_claim.get("lease_until")
        and str(latest_claim["lease_until"]) > measured_at
        and latest_final is None
        and not latest_claim_resolved
    )
    lease_expired = bool(
        latest_claim
        and not lease_active
        and latest_final is None
        and not latest_claim_resolved
    )
    if latest_final is not None:
        state = str(latest_final["event_type"])
    elif lease_active:
        state = "claimed"
    elif (
        latest_attempt_result is not None
        and latest_attempt_result["event_type"] == "partial"
        and latest_claim is not None
        and int(latest_attempt_result["attempt_no"])
            == int(latest_claim["attempt_no"])
    ):
        state = "partial"
    else:
        state = "requested"
    snapshot_lineage = (
        _script_language_demand_snapshot_lineage_from_connection(
            connection, demand_id
        )
    )
    latest_snapshot = max(
        snapshot_lineage,
        key=lambda item: int(item["lineage_sequence"]),
        default=None,
    )
    semantic_key = str(
        latest_snapshot["semantic_key"] if latest_snapshot else ""
    )
    semantic_authority = (
        _latest_script_language_demand_for_semantic_connection(
            connection, semantic_key
        )
        if semantic_key else None
    )
    semantic_authority_demand_id = str(
        semantic_authority["demand_id"]
        if semantic_authority else demand_id
    )
    superseded = semantic_authority_demand_id != demand_id
    supersession = (
        {
            "contract": "market_tape_script_language_demand_supersession_v1",
            "reason": "newer_semantic_snapshot_lineage",
            "semantic_key": semantic_key,
            "superseded_demand_id": demand_id,
            "authoritative_demand_id": semantic_authority_demand_id,
            "authoritative_lineage_sequence": int(
                semantic_authority["latest_lineage_sequence"]
            ),
        }
        if superseded and semantic_authority else None
    )
    original_request_payload = request.get("payload")
    latest_request_payload = (
        latest_snapshot.get("payload") if latest_snapshot else None
    )
    request_payload = (
        latest_request_payload
        if isinstance(latest_request_payload, dict)
        and not latest_request_payload.get("invalid_payload_json")
        else original_request_payload
    )
    targets = request_payload.get("targets", []) if isinstance(
        request_payload, dict
    ) else []
    latest_lineage = max(
        [event for event in (latest_attempt_result, latest_claim) if event],
        key=lambda event: (
            int(event["attempt_no"]),
            str(event["created_at"]),
            str(event["event_id"]),
        ),
        default=request,
    )
    return {
        "contract": SCRIPT_LANGUAGE_DEMAND_CONTRACT,
        "event_contract": SCRIPT_LANGUAGE_DEMAND_EVENT_CONTRACT,
        "demand_id": demand_id,
        "state": state,
        "request_sha256": str(
            latest_snapshot["request_sha256"]
            if latest_snapshot else request["request_sha256"]
        ),
        "source_service": str(
            latest_snapshot["source_service"]
            if latest_snapshot else request["source_service"]
        ),
        "source_receipt_id": str(
            latest_snapshot["source_receipt_id"]
            if latest_snapshot else request["source_receipt_id"]
        ),
        "topic": str(
            request_payload.get("topic")
            if isinstance(request_payload, dict)
            and request_payload.get("topic")
            else request["topic"]
        ),
        "audience": str(
            request_payload.get("audience")
            if isinstance(request_payload, dict)
            and request_payload.get("audience")
            else request["audience"]
        ),
        "objective": str(
            request_payload.get("objective")
            if isinstance(request_payload, dict)
            and request_payload.get("objective")
            else request["objective"]
        ),
        "evidence_trend_id": str(
            latest_snapshot["evidence_trend_id"]
            if latest_snapshot else request["evidence_trend_id"]
        ),
        "snapshot_id": str(
            latest_snapshot["snapshot_id"]
            if latest_snapshot else request["snapshot_id"]
        ),
        "latest_snapshot_id": str(
            latest_snapshot["snapshot_id"]
            if latest_snapshot else request["snapshot_id"]
        ),
        "latest_source_service": str(
            latest_snapshot["source_service"]
            if latest_snapshot else request["source_service"]
        ),
        "latest_source_receipt_id": str(
            latest_snapshot["source_receipt_id"]
            if latest_snapshot else request["source_receipt_id"]
        ),
        "latest_evidence_trend_id": str(
            latest_snapshot["evidence_trend_id"]
            if latest_snapshot else request["evidence_trend_id"]
        ),
        "semantic_key": semantic_key,
        "semantic_generation_role": (
            "superseded" if superseded else "authoritative"
        ),
        "semantic_authority_demand_id": semantic_authority_demand_id,
        "semantic_authority_lineage_sequence": (
            int(semantic_authority["latest_lineage_sequence"])
            if semantic_authority else None
        ),
        "superseded": superseded,
        "superseded_by_demand_id": (
            semantic_authority_demand_id if superseded else None
        ),
        "supersession": supersession,
        "effective_state": "superseded" if superseded else state,
        "targets": targets,
        "collection_run_id": str(
            latest_lineage.get("collection_run_id")
            or (
                request_payload.get("collection_run_id")
                if isinstance(request_payload, dict) else ""
            )
            or request["collection_run_id"]
        ),
        "transcript_run_id": str(
            latest_lineage.get("transcript_run_id")
            or (
                request_payload.get("transcript_run_id")
                if isinstance(request_payload, dict) else ""
            )
            or request["transcript_run_id"]
        ),
        "requested_at": str(request["created_at"]),
        "latest_snapshot_at": (
            str(latest_snapshot["created_at"]) if latest_snapshot else None
        ),
        "latest_request_payload": request_payload,
        "latest_snapshot_lineage": latest_snapshot,
        "snapshot_lineage": snapshot_lineage,
        "snapshot_lineage_count": len(snapshot_lineage),
        "attempt_count": len(claims),
        "attempt_no": (
            int(latest_claim["attempt_no"]) if latest_claim else 0
        ),
        "lease_until": (
            str(latest_claim["lease_until"]) if latest_claim else None
        ),
        "lease_active": lease_active,
        "lease_expired": lease_expired,
        "retry_eligible": (
            not superseded and state in {"requested", "partial"}
        ),
        "claimable": (
            not superseded and state in {"requested", "partial"}
        ),
        "terminal_at": (
            str(latest_final["created_at"]) if latest_final else None
        ),
        "last_attempt_at": (
            str(latest_attempt_result["created_at"])
            if latest_attempt_result else None
        ),
        "events": events,
    }


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


def _normalize_measurement_capabilities(
    values: Sequence[Dict[str, Any]],
) -> Dict[str, Dict[str, Any]]:
    """Validate and secret-strip collector-provided refresh capabilities."""

    normalized: Dict[str, Dict[str, Any]] = {}
    for raw in values:
        if not isinstance(raw, dict):
            continue
        state = str(raw.get("state") or "").strip().casefold()
        if state not in {"ready", "refresh_capable"}:
            continue
        source_id = str(raw.get("source_id") or "").strip()
        platform = str(raw.get("platform") or "").strip().casefold()
        try:
            daily_limit = int(
                raw.get("daily_request_limit", raw.get("daily_limit", 0))
            )
            batch_size = int(raw.get("refresh_batch_size", 1))
            units_per_batch = int(
                raw.get(
                    "request_units_per_batch",
                    raw.get("max_request_units_per_batch", 1),
                )
            )
        except (TypeError, ValueError):
            continue
        if (
            not source_id
            or not platform
            or daily_limit <= 0
            or batch_size <= 0
            or units_per_batch <= 0
        ):
            continue
        remaining = raw.get("request_budget_remaining")
        try:
            request_budget_remaining = (
                max(0, int(remaining)) if remaining is not None else None
            )
        except (TypeError, ValueError):
            continue
        normalized[source_id] = {
            "source_id": source_id,
            "platform": platform,
            "daily_request_limit": daily_limit,
            "refresh_batch_size": batch_size,
            "request_units_per_batch": units_per_batch,
            "request_budget_remaining": request_budget_remaining,
            "request_budget_date": str(
                raw.get("request_budget_date") or ""
            )[:10],
            "credential_fingerprint": str(
                raw.get("credential_fingerprint") or ""
            )[:128],
        }
    return normalized


def _subject_on_forecast_cooldown(
    predictions: Sequence[Dict[str, Any]],
    predicted_at: datetime,
    cooldown: timedelta,
) -> bool:
    cutoff = predicted_at - max(timedelta(0), cooldown)
    for prediction in predictions:
        try:
            prior = _as_datetime(prediction["predicted_at"])
        except (KeyError, TypeError, ValueError):
            continue
        if cutoff < prior <= predicted_at:
            return True
    return False


def _reserved_measurement_poll_rows(
    connection: sqlite3.Connection,
    *,
    selected_at_iso: str,
    capable_source_ids: Sequence[str],
    capable_platforms: Sequence[str],
    claim_run_id: Optional[str],
    limit: int,
) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    """Load exact durable assignments without depending on today's model."""

    filter_params: List[Any] = []
    if capable_source_ids:
        placeholders = ",".join("?" for _ in capable_source_ids)
        capability_sql = f"reservation.source_id IN ({placeholders})"
        filter_params.extend(capable_source_ids)
    elif capable_platforms:
        placeholders = ",".join("?" for _ in capable_platforms)
        capability_sql = f"reservation.platform IN ({placeholders})"
        filter_params.extend(capable_platforms)
    else:
        return [], []
    if claim_run_id:
        reservation_state_sql = (
            "reservation.state = 'claimed' "
            "AND reservation.claim_run_id = ?"
        )
        state_params: List[Any] = [claim_run_id]
        assignment_state = "claimed"
    else:
        reservation_state_sql = "reservation.state = 'reserved'"
        state_params = []
        assignment_state = "reserved"
    exact_rows = [dict(row) for row in connection.execute(
        f"""SELECT reservation.reservation_id,
                   reservation.source_id AS reserved_source_id,
                   reservation.window_open_at, reservation.deadline_at,
                   assignment.prediction_id, assignment.trend_id,
                   q.video_id, q.platform, q.external_id,
                   q.preferred_source_id, q.hot_mode, q.due_at,
                   q.failure_count, q.last_observed_at, q.last_error_code,
                   v.published_at, v.title, v.caption, v.description,
                   v.language, v.url, v.thumbnail_url, v.duration_seconds,
                   c.external_id AS creator_external_id,
                   c.handle AS creator_handle,
                   c.display_name AS creator_name,
                   c.followers AS creator_followers,
                   prediction.predicted_at, prediction.model_version,
                   prediction.horizon
            FROM mt_forecast_measurement_reservations reservation
            JOIN mt_forecast_measurement_assignments assignment
              ON assignment.reservation_id = reservation.reservation_id
            JOIN mt_predictions prediction
              ON prediction.prediction_id = assignment.prediction_id
            JOIN mt_poll_queue q ON q.video_id = assignment.video_id
            JOIN mt_videos v ON v.video_id = q.video_id
            JOIN mt_creators c ON c.creator_id = v.creator_id
            WHERE reservation.window_open_at <= ?
              AND reservation.deadline_at > ?
              AND {reservation_state_sql}
              AND assignment.state = ?
              AND prediction.outcome_json IS NULL
              AND json_extract(
                      prediction.features_json,
                      '$.observation_quality_contract'
                  ) = 'market_tape_accepted_observation_lineage_v2'
              AND EXISTS (
                  SELECT 1
                  FROM mt_accepted_trend_memberships_v1 membership
                  WHERE membership.trend_id = assignment.trend_id
                    AND membership.video_id = assignment.video_id
              )
              AND {capability_sql}
            ORDER BY reservation.deadline_at, reservation.reservation_id,
                     q.video_id, assignment.prediction_id""",
        (
            selected_at_iso,
            selected_at_iso,
            *state_params,
            assignment_state,
            *filter_params,
        ),
    ).fetchall()]
    exact_by_video: Dict[Tuple[str, str], Dict[str, Any]] = {}
    obligations: List[Dict[str, Any]] = []
    for exact in exact_rows:
        key = (str(exact["reserved_source_id"]), str(exact["video_id"]))
        payload = exact_by_video.get(key)
        if payload is None:
            payload = dict(exact)
            payload["preferred_source_id"] = str(exact["reserved_source_id"])
            payload.update({
                "queue_contract": "market_tape_recheck_queue_v3",
                "queue_selected_at": selected_at_iso,
                "recheck_priority": 0,
                "recheck_reason": (
                    "reserved_active_model_forecast_terminal_coverage"
                ),
                "forecast_coverage": [],
                "measurement_reservation_ids": [],
            })
            exact_by_video[key] = payload
        obligation = {
            "prediction_id": int(exact["prediction_id"]),
            "trend_id": str(exact["trend_id"]),
            "model_version": str(exact["model_version"]),
            "horizon": str(exact["horizon"]),
            "predicted_at": str(exact["predicted_at"]),
            "coverage_window_open_at": str(exact["window_open_at"]),
            "coverage_deadline_at": str(exact["deadline_at"]),
            "measurement_reservation_id": str(exact["reservation_id"]),
        }
        payload["forecast_coverage"].append(obligation)
        reservation_id = str(exact["reservation_id"])
        if reservation_id not in payload["measurement_reservation_ids"]:
            payload["measurement_reservation_ids"].append(reservation_id)
        obligations.append(obligation)
    return list(exact_by_video.values())[:max(0, int(limit))], obligations


def _select_measurement_cohort(
    candidates: Sequence[Dict[str, Any]],
    membership_by_trend: Dict[str, List[Dict[str, Any]]],
    capabilities: Dict[str, Dict[str, Any]],
    limit: int,
) -> Tuple[List[Dict[str, Any]], Dict[str, int]]:
    """Choose unique subjects and exact provider members within held capacity."""

    sources_by_platform: Dict[str, List[str]] = {}
    source_usage: Dict[str, Dict[str, Any]] = {}
    for source_id, capability in capabilities.items():
        sources_by_platform.setdefault(
            str(capability["platform"]), []
        ).append(source_id)
        source_usage[source_id] = {
            "videos": set(),
            "request_units": 0,
        }
    for source_ids in sources_by_platform.values():
        source_ids.sort()

    ordered = sorted(
        candidates,
        key=lambda candidate: (
            bool(candidate.get("previously_forecast")),
            -float(candidate.get("trend_strength") or 0.0),
            str(candidate.get("trend_id") or ""),
        ),
    )
    selected: List[Dict[str, Any]] = []
    selected_trends: set[str] = set()
    no_refreshable_member = 0
    no_measurement_capacity = 0
    for candidate in ordered:
        if len(selected) >= max(0, int(limit)):
            break
        trend_id = str(candidate.get("trend_id") or "")
        if not trend_id or trend_id in selected_trends:
            continue
        members = membership_by_trend.get(trend_id, [])
        refreshable_options = 0
        options: List[Tuple[Any, ...]] = []
        for member in members:
            platform = str(member.get("platform") or "").casefold()
            for source_id in sources_by_platform.get(platform, []):
                refreshable_options += 1
                capability = capabilities[source_id]
                usage = source_usage[source_id]
                video_id = str(member.get("video_id") or "")
                if not video_id:
                    continue
                new_video = video_id not in usage["videos"]
                incremental_units = 0
                if new_video and len(usage["videos"]) % int(
                    capability["refresh_batch_size"]
                ) == 0:
                    incremental_units = int(
                        capability["request_units_per_batch"]
                    )
                if (
                    int(usage["request_units"]) + incremental_units
                    > int(capability["available_request_units"])
                ):
                    continue
                preferred_penalty = int(
                    str(member.get("preferred_source_id") or "") != source_id
                )
                options.append((
                    incremental_units,
                    preferred_penalty,
                    max(0, int(member.get("failure_count") or 0)),
                    str(member.get("due_at") or ""),
                    source_id,
                    video_id,
                    member,
                ))
        if not options:
            if refreshable_options:
                no_measurement_capacity += 1
            else:
                no_refreshable_member += 1
            continue
        (
            incremental_units,
            _preferred_penalty,
            _failure_count,
            _due_at,
            source_id,
            video_id,
            member,
        ) = min(options, key=lambda option: option[:-1])
        usage = source_usage[source_id]
        usage["request_units"] = int(usage["request_units"]) + int(
            incremental_units
        )
        usage["videos"].add(video_id)
        selected_trends.add(trend_id)
        selected.append({
            **candidate,
            "source_id": source_id,
            "platform": str(member.get("platform") or ""),
            "video_id": video_id,
        })
    return selected, {
        "no_refreshable_member": no_refreshable_member,
        "no_measurement_capacity": no_measurement_capacity,
    }


def _remaining_request_budget_with_connection(
    connection: sqlite3.Connection,
    *,
    source_id: str,
    daily_limit: int,
    purpose: str,
    usage_date: str,
    validation_floor: int,
    as_of_iso: Optional[str] = None,
) -> int:
    normalized_purpose = str(purpose or "legacy").strip().casefold()
    if normalized_purpose not in {
        "legacy",
        "general",
        "discovery",
        "scheduled",
        "reservation",
        "forecast_terminal",
    }:
        raise ValueError(
            "purpose must be legacy, general, discovery, scheduled, "
            "reservation, or forecast_terminal"
        )
    actual_row = connection.execute(
        """SELECT requests FROM mt_daily_usage
           WHERE usage_date = ? AND source_id = ?""",
        (usage_date, source_id),
    ).fetchone()
    actual = int(actual_row[0] or 0) if actual_row else 0
    if normalized_purpose in {"legacy", "forecast_terminal"}:
        return max(0, daily_limit - actual)

    outstanding_row = connection.execute(
        """SELECT COALESCE(SUM(reserved_request_units), 0)
           FROM mt_forecast_measurement_reservations
           WHERE source_id = ? AND usage_date = ?
             AND state IN ('reserved', 'claimed')
             AND (? IS NULL OR deadline_at > ?)""",
        (source_id, usage_date, as_of_iso, as_of_iso),
    ).fetchone()
    outstanding = int(outstanding_row[0] or 0) if outstanding_row else 0
    if normalized_purpose == "reservation":
        return max(0, daily_limit - actual - outstanding)

    measured_row = connection.execute(
        """SELECT COALESCE(SUM(request_count), 0)
           FROM mt_source_receipts
           WHERE source_id = ?
             AND substr(finished_at, 1, 10) = ?
             AND (
                 json_extract(metadata_json, '$.recheck_queue.selection_lane')
                     = 'forecast_terminal'
                 OR json_extract(metadata_json, '$.measurement_lane')
                     = 'forecast_terminal'
             )""",
        (source_id, usage_date),
    ).fetchone()
    validation_used = int(measured_row[0] or 0) if measured_row else 0
    floor_remaining = max(0, validation_floor - validation_used)
    protected = max(outstanding, floor_remaining)
    return max(0, daily_limit - actual - protected)


def _update_measurement_assignments_state(
    connection: sqlite3.Connection,
    reservation_ids: Sequence[str],
    *,
    state: str,
    completed_at: Optional[str],
    error_code: str,
) -> None:
    for offset in range(0, len(reservation_ids), 400):
        chunk = list(reservation_ids[offset:offset + 400])
        placeholders = ",".join("?" for _ in chunk)
        connection.execute(
            f"""UPDATE mt_forecast_measurement_assignments
                SET state = ?, completed_at = ?, error_code = ?
                WHERE reservation_id IN ({placeholders})
                  AND state IN ('reserved', 'claimed')""",
            (state, completed_at, error_code[:100], *chunk),
        )


def _update_measurement_reservations_state(
    connection: sqlite3.Connection,
    reservation_ids: Sequence[str],
    *,
    state: str,
    completed_at: Optional[str],
    error_code: str,
) -> None:
    for offset in range(0, len(reservation_ids), 400):
        chunk = list(reservation_ids[offset:offset + 400])
        placeholders = ",".join("?" for _ in chunk)
        connection.execute(
            f"""UPDATE mt_forecast_measurement_reservations
                SET state = ?, completed_at = ?, error_code = ?,
                    claim_expires_at = NULL
                WHERE reservation_id IN ({placeholders})
                  AND state IN ('reserved', 'claimed')""",
            (state, completed_at, error_code[:100], *chunk),
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
        """SELECT prediction.subject_id, prediction.predicted_at,
                  prediction.probability, prediction.outcome_json
           FROM mt_predictions prediction
           LEFT JOIN mt_trends trend
             ON trend.trend_id = prediction.subject_id
           WHERE prediction.subject_type = 'trend'
             AND prediction.model_version = ?
             AND prediction.horizon = ?
             AND json_extract(
                     prediction.features_json,
                     '$.observation_quality_contract'
                 ) = 'market_tape_accepted_observation_lineage_v2'
             AND COALESCE(lower(trend.trend_type), '') != 'format'""",
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


def _select_forecast_rechecks(
    candidate_rows: Sequence[Dict[str, Any]],
    obligations_by_trend: Dict[str, List[Dict[str, Any]]],
    limit: int,
    selected_at: str,
) -> Tuple[List[Dict[str, Any]], set[str]]:
    """Choose a deterministic minimum-work cover for open forecast windows."""

    row_by_video: Dict[str, Dict[str, Any]] = {}
    trends_by_video: Dict[str, set[str]] = {}
    for raw in candidate_rows:
        video_id = str(raw.get("video_id") or "")
        trend_id = str(raw.get("trend_id") or "")
        if not video_id or trend_id not in obligations_by_trend:
            continue
        payload = dict(raw)
        payload.pop("trend_id", None)
        row_by_video.setdefault(video_id, payload)
        trends_by_video.setdefault(video_id, set()).add(trend_id)

    uncovered = set(obligations_by_trend)
    heap: List[Tuple[int, int, int, str, str]] = []
    for video_id, trend_ids in trends_by_video.items():
        row = row_by_video[video_id]
        gain = sum(len(obligations_by_trend[trend_id]) for trend_id in trend_ids)
        scheduled_due = 0 if str(row.get("due_at") or "") <= selected_at else 1
        heapq.heappush(heap, (
            -gain,
            scheduled_due,
            max(0, int(row.get("failure_count") or 0)),
            str(row.get("due_at") or ""),
            video_id,
        ))

    selected: List[Dict[str, Any]] = []
    covered: set[str] = set()
    maximum = max(0, int(limit))
    while heap and uncovered and len(selected) < maximum:
        negative_gain, scheduled_due, failures, due_at, video_id = heapq.heappop(
            heap
        )
        newly_covered = trends_by_video[video_id] & uncovered
        current_gain = sum(
            len(obligations_by_trend[trend_id])
            for trend_id in newly_covered
        )
        if current_gain <= 0:
            continue
        if current_gain != -negative_gain:
            heapq.heappush(heap, (
                -current_gain,
                scheduled_due,
                failures,
                due_at,
                video_id,
            ))
            continue
        obligations = sorted(
            (
                obligation
                for trend_id in newly_covered
                for obligation in obligations_by_trend[trend_id]
            ),
            key=lambda obligation: (
                str(obligation["coverage_deadline_at"]),
                int(obligation["prediction_id"]),
            ),
        )
        payload = dict(row_by_video[video_id])
        payload.update({
            "queue_contract": "market_tape_recheck_queue_v2",
            "queue_selected_at": selected_at,
            "recheck_priority": 0,
            "recheck_reason": "active_model_forecast_terminal_coverage",
            "forecast_coverage": obligations,
        })
        selected.append(payload)
        uncovered.difference_update(newly_covered)
        covered.update(newly_covered)
    return selected, covered


def _poll_assignment_receipt(row: Dict[str, Any]) -> Dict[str, Any]:
    coverage = row.get("forecast_coverage") or []
    return {
        "video_id": str(row.get("video_id") or ""),
        "platform": str(row.get("platform") or ""),
        "preferred_source_id": str(row.get("preferred_source_id") or ""),
        "scheduled_due_at": str(row.get("due_at") or ""),
        "recheck_reason": str(row.get("recheck_reason") or "scheduled_poll_due"),
        "coverage_prediction_ids": [
            int(obligation["prediction_id"]) for obligation in coverage
        ],
        "coverage_trend_ids": sorted({
            str(obligation["trend_id"]) for obligation in coverage
        }),
        "coverage_deadlines": sorted({
            str(obligation["coverage_deadline_at"]) for obligation in coverage
        }),
        "measurement_reservation_ids": sorted({
            str(obligation.get("measurement_reservation_id") or "")
            for obligation in coverage
            if obligation.get("measurement_reservation_id")
        }),
    }


def _query_family_key(value: Any) -> str:
    """Return the stable family key shared by planner and provider receipts."""

    return " ".join(str(value or "").casefold().split())[:300]


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
