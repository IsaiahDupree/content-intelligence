"""Audited local transcript acquisition for performance-qualified market videos.

This module deliberately bypasses content scoring services.  It reads the Market
Tape SQLite database directly, downloads the selected public video's audio with
``yt-dlp``, transcribes it locally with Whisper, and binds every derived artifact
to the exact Market Tape observation used for qualification.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import shutil
import sqlite3
import subprocess
import uuid
from contextlib import closing
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from functools import lru_cache
from importlib.metadata import PackageNotFoundError, version as package_version
from pathlib import Path
from statistics import median
from typing import Any, Iterable, Sequence

from services.market_tape.source_urls import is_usable_source_url

from .contracts import (
    CURRENT_TRANSCRIPT_AUDIT_CONTRACT,
    is_supported_transcript_audit_contract,
)


UTC = timezone.utc
WORD_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9'’-]*")
STOP_WORDS = {
    "and", "any", "are", "can", "did", "each", "every", "first", "for",
    "had", "has", "how", "its", "know", "not", "now", "our", "out",
    "she", "the", "too", "use", "was", "were", "who", "why", "will",
    "you",
    "about", "after", "again", "also", "because", "been", "before", "being",
    "could", "does", "doing", "from", "have", "here", "into", "just", "more",
    "most", "only", "other", "over", "should", "some", "than", "that", "their",
    "them", "then", "there", "these", "they", "this", "those", "through", "very",
    "want", "what", "when", "where", "which", "while", "with", "would", "your",
}


def utc_now() -> str:
    return datetime.now(UTC).isoformat()


def canonical_sha256(value: Any) -> str:
    encoded = json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def words(text: str) -> list[str]:
    return WORD_RE.findall(text or "")


def topic_terms(topic: str) -> list[str]:
    return list(dict.fromkeys(
        token.lower()
        for token in words(topic)
        if (len(token) >= 3 or token.lower() == "ai")
        and token.lower() not in STOP_WORDS
    ))


@lru_cache(maxsize=None)
def executable_version(command: str) -> str:
    executable = shutil.which(command)
    if not executable:
        raise RuntimeError(f"required executable is unavailable: {command}")
    result = subprocess.run(
        [executable, "-version" if command == "ffmpeg" else "--version"],
        check=True,
        capture_output=True,
        text=True,
        timeout=30,
    )
    return (result.stdout or result.stderr).splitlines()[0].strip()


def installed_package_version(distribution: str) -> str:
    try:
        return package_version(distribution)
    except PackageNotFoundError:
        return "unavailable"


@lru_cache(maxsize=None)
def extractor_provenance() -> dict[str, str]:
    versions: dict[str, str] = {}
    for command in ("yt-dlp", "ffmpeg"):
        try:
            versions[command] = executable_version(command)
        except (OSError, RuntimeError, subprocess.SubprocessError) as exc:
            versions[command] = f"unavailable:{type(exc).__name__}"
    versions["fingerprint"] = canonical_sha256(versions)
    return versions


@lru_cache(maxsize=None)
def whisper_model_provenance(model_name: str) -> dict[str, str]:
    checkpoint = Path.home() / ".cache" / "whisper" / f"{model_name}.pt"
    return {
        "package": "openai-whisper",
        "package_version": installed_package_version("openai-whisper"),
        "torch_version": installed_package_version("torch"),
        "model_name": model_name,
        "checkpoint_path": str(checkpoint) if checkpoint.is_file() else "",
        "checkpoint_sha256": file_sha256(checkpoint) if checkpoint.is_file() else "",
    }


@lru_cache(maxsize=None)
def transcription_runtime_provenance(model_name: str) -> dict[str, Any]:
    return {
        "contract": "local_whisper_runtime_provenance_v1",
        "extractor": extractor_provenance(),
        "decoder": whisper_model_provenance(model_name),
        "decode_parameters": {
            "fp16": False,
            "verbose": False,
            "condition_on_previous_text": True,
        },
    }


def validate_transcription_runtime(model_name: str) -> dict[str, Any]:
    """Return complete, hashable runtime provenance or fail closed."""

    provenance = transcription_runtime_provenance(model_name)
    extractor = provenance["extractor"]
    decoder = provenance["decoder"]
    unavailable = [
        name
        for name in ("yt-dlp", "ffmpeg")
        if str(extractor.get(name) or "").startswith("unavailable:")
    ]
    if unavailable:
        raise RuntimeError(
            "local transcription runtime is unavailable: " + ", ".join(unavailable)
        )
    if decoder["package_version"] == "unavailable":
        raise RuntimeError("local Whisper package provenance is unavailable")
    if decoder["torch_version"] == "unavailable":
        raise RuntimeError("local Torch package provenance is unavailable")
    if not decoder["checkpoint_path"] or not decoder["checkpoint_sha256"]:
        raise RuntimeError(
            f"local Whisper model checkpoint is unavailable: {model_name}"
        )
    return provenance


def atomic_write_json(path: Path, payload: Any) -> None:
    """Durably replace one JSON receipt without exposing a partial file."""

    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        with temporary.open("x", encoding="utf-8") as handle:
            json.dump(
                payload,
                handle,
                indent=2,
                ensure_ascii=False,
                sort_keys=True,
            )
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        directory_fd = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        if temporary.exists():
            temporary.unlink()


@dataclass(frozen=True)
class PerformancePolicy:
    minimum_views: int
    minimum_engagement_rate: float
    maximum_duration_seconds: float = 300.0
    minimum_transcript_words: int = 40


DEFAULT_POLICIES = {
    "youtube": PerformancePolicy(10_000, 0.005, maximum_duration_seconds=720.0),
    "tiktok": PerformancePolicy(100_000, 0.02),
    "instagram": PerformancePolicy(50_000, 0.015),
    "facebook": PerformancePolicy(25_000, 0.01),
}


PERMANENT_FAILURE_PATTERNS = {
    "invalid_source": (
        "refusing unusable source url",
        "is not a valid url",
    ),
    "source_unavailable": (
        "account has been terminated",
        "private video",
        "requested content is not available",
        "this post is no longer available",
        "this video has been removed",
        "video unavailable",
    ),
}
TRANSIENT_FAILURE_PATTERNS = {
    "extractor_unsupported": ("unsupported url",),
    "http_forbidden": ("http error 403", "403: forbidden"),
    "rate_limited": ("http error 429", "rate limit", "too many requests"),
    "timeout": ("timed out", "timeoutexpired", "timeout"),
    "network": (
        "connection reset",
        "network is unreachable",
        "temporary failure",
        "unable to download video data",
    ),
    "access_challenge": (
        "confirm you're not a bot",
        "confirm you’re not a bot",
        "sign in to confirm",
        "login required",
    ),
    "local_runtime": ("local whisper model", "local transcription runtime"),
}
FAILURE_RETRY_BASE_HOURS = {
    "extractor_unsupported": 24,
    "http_forbidden": 24,
    "rate_limited": 12,
    "timeout": 6,
    "network": 6,
    "access_challenge": 72,
    "local_runtime": 1,
    "provider_error": 24,
}
MAX_FAILURE_RETRY_HOURS = 24 * 7
ACQUISITION_CLAIM_HOURS = 6
TRANSCRIPT_LEDGER_SCHEMA_VERSION = 9
PASSPORT_VOLUME_ROOT = Path("/Volumes/My Passport")


def parse_timestamp(value: str) -> datetime:
    parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def classify_acquisition_failure(error_type: str, error: str) -> dict[str, Any]:
    """Classify a real provider failure without pretending it was an outcome.

    Permanent failures are scoped to the exact source URL.  Provider/network
    failures remain retryable, but exponential cooldown prevents the same URL
    from consuming every hourly batch.
    """

    normalized = f"{error_type} {error}".casefold()
    for failure_class, patterns in PERMANENT_FAILURE_PATTERNS.items():
        if any(pattern in normalized for pattern in patterns):
            return {
                "failure_class": failure_class,
                "retryable": False,
                "retry_base_hours": None,
            }
    for failure_class, patterns in TRANSIENT_FAILURE_PATTERNS.items():
        if any(pattern in normalized for pattern in patterns):
            return {
                "failure_class": failure_class,
                "retryable": True,
                "retry_base_hours": FAILURE_RETRY_BASE_HOURS[failure_class],
            }
    return {
        "failure_class": "provider_error",
        "retryable": True,
        "retry_base_hours": FAILURE_RETRY_BASE_HOURS["provider_error"],
    }


def retry_after_timestamp(
    *,
    finished_at: str,
    retry_base_hours: int | None,
    failure_ordinal: int,
) -> str | None:
    if retry_base_hours is None:
        return None
    delay_hours = min(
        retry_base_hours * (2 ** max(0, min(failure_ordinal - 1, 4))),
        MAX_FAILURE_RETRY_HOURS,
    )
    return (parse_timestamp(finished_at) + timedelta(hours=delay_hours)).isoformat()


def storage_mount_error(
    storage_root: str | Path,
    *,
    passport_root: str | Path = PASSPORT_VOLUME_ROOT,
) -> str:
    resolved_storage = Path(storage_root).expanduser().resolve(strict=False)
    resolved_passport = Path(passport_root).expanduser().resolve(strict=False)
    if resolved_storage.is_relative_to(resolved_passport) and not os.path.ismount(
        resolved_passport
    ):
        return f"Passport storage is not a mounted filesystem: {resolved_passport}"
    return ""


@dataclass(frozen=True)
class Candidate:
    video_id: str
    platform: str
    external_id: str
    creator_id: str
    title: str
    caption: str
    description: str
    source_url: str
    duration_seconds: float
    observation_key: str
    observed_at: str
    views: int
    likes: int
    comments: int
    shares: int
    saves: int
    relative_strength: float
    topic_terms: tuple[str, ...]
    topic_matches: tuple[str, ...]

    @property
    def engagement_rate(self) -> float:
        if self.views <= 0:
            return 0.0
        return (self.likes + self.comments + self.shares + self.saves) / self.views

    @property
    def source_metrics(self) -> dict[str, Any]:
        return {
            "observation_key": self.observation_key,
            "observed_at": self.observed_at,
            "views": self.views,
            "likes": self.likes,
            "comments": self.comments,
            "shares": self.shares,
            "saves": self.saves,
            "engagement_rate": round(self.engagement_rate, 8),
            "relative_strength": self.relative_strength,
        }


class TranscriptBank:
    def __init__(self, tape_path: str | Path, storage_root: str | Path):
        self.tape_path = Path(tape_path).expanduser().resolve()
        self.storage_root = Path(storage_root).expanduser().resolve()
        if not self.tape_path.exists():
            raise FileNotFoundError(f"Market Tape database not found: {self.tape_path}")
        mount_error = storage_mount_error(self.storage_root)
        if mount_error:
            raise RuntimeError(mount_error)
        self.storage_root.mkdir(parents=True, exist_ok=True)
        self._initialize()

    def connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.tape_path, timeout=30)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA foreign_keys=ON")
        return connection

    def _initialize(self) -> None:
        with closing(self.connect()) as connection:
            connection.executescript(
                """
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
                """
            )
            attempt_columns = {
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
                if column not in attempt_columns:
                    connection.execute(
                        f"ALTER TABLE mt_transcript_acquisition_attempts "
                        f"ADD COLUMN {column} {definition}"
                    )
            claim_columns = {
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
                if column not in claim_columns:
                    connection.execute(
                        f"ALTER TABLE mt_transcript_acquisition_claims "
                        f"ADD COLUMN {column} {definition}"
                    )
            self._backfill_legacy_attempts(connection)
            if connection.execute(
                """
                SELECT 1 FROM sqlite_master
                WHERE type='table' AND name='mt_meta'
                """
            ).fetchone():
                connection.execute(
                    """
                    INSERT INTO mt_meta(key, value) VALUES('schema_version', ?)
                    ON CONFLICT(key) DO UPDATE SET value = CASE
                        WHEN CAST(mt_meta.value AS INTEGER) < CAST(excluded.value AS INTEGER)
                        THEN excluded.value ELSE mt_meta.value END
                    """,
                    (str(TRANSCRIPT_LEDGER_SCHEMA_VERSION),),
                )
            connection.commit()

    @staticmethod
    def _backfill_legacy_attempts(connection: sqlite3.Connection) -> None:
        """One-time materialization of actual artifacts and failure manifests."""

        migration_id = "transcript_attempt_legacy_history_v2"
        if connection.execute(
            "SELECT 1 FROM mt_transcript_ledger_migrations WHERE migration_id=?",
            (migration_id,),
        ).fetchone():
            return
        videos = {
            str(row["video_id"]): row
            for row in connection.execute(
                "SELECT video_id, platform, external_id, url FROM mt_videos"
            ).fetchall()
        }
        events: list[dict[str, Any]] = []
        runs = connection.execute(
            """
            SELECT run_id, policy_json, failures_json, started_at, finished_at
            FROM mt_transcript_backfill_runs
            ORDER BY started_at, run_id
            """
        ).fetchall()
        for run in runs:
            try:
                policy = json.loads(run["policy_json"] or "{}")
                failures = json.loads(run["failures_json"] or "[]")
            except (TypeError, json.JSONDecodeError):
                continue
            model_name = str(policy.get("model") or "unknown")
            for failure_index, failure in enumerate(failures):
                if not isinstance(failure, dict):
                    continue
                video_id = str(failure.get("video_id") or "")
                video = videos.get(video_id)
                if video is None:
                    continue
                finished_at = str(run["finished_at"] or run["started_at"] or utc_now())
                events.append({
                    "event_type": "failure",
                    "sort_at": finished_at,
                    "run_id": str(run["run_id"]),
                    "failure_index": failure_index,
                    "video_id": video_id,
                    "platform": str(video["platform"] or ""),
                    "external_id": str(
                        failure.get("external_id") or video["external_id"] or ""
                    ),
                    "source_url": str(failure.get("source_url") or video["url"] or ""),
                    "source_url_fallback": not bool(failure.get("source_url")),
                    "model_name": model_name,
                    "error_type": str(failure.get("error_type") or "RuntimeError"),
                    "error": str(failure.get("error") or "")[:500],
                    "attempt_id": str(failure.get("attempt_id") or ""),
                    "started_at": str(run["started_at"] or finished_at),
                    "finished_at": finished_at,
                })
        for artifact in connection.execute(
            """
            SELECT transcript_id, video_id, platform, external_id, source_url,
                   whisper_model, created_at
            FROM mt_transcript_artifacts
            ORDER BY created_at, transcript_id
            """
        ).fetchall():
            events.append({
                "event_type": "success",
                "sort_at": str(artifact["created_at"]),
                "run_id": "legacy_artifact_import",
                "video_id": str(artifact["video_id"]),
                "platform": str(artifact["platform"]),
                "external_id": str(artifact["external_id"]),
                "source_url": str(artifact["source_url"]),
                "source_url_fallback": False,
                "model_name": str(artifact["whisper_model"]),
                "error_type": "",
                "error": "",
                "attempt_id": "",
                "transcript_id": str(artifact["transcript_id"]),
                "started_at": str(artifact["created_at"]),
                "finished_at": str(artifact["created_at"]),
            })

        attempt_ordinals: dict[tuple[str, str], int] = {}
        failure_ordinals: dict[tuple[str, str], int] = {}
        inserted_attempt_ids: list[str] = []
        inserted_failures = 0
        inserted_successes = 0
        for event in sorted(
            events,
            key=lambda item: (
                str(item["sort_at"]),
                str(item["video_id"]),
                str(item["event_type"]),
            ),
        ):
            ordinal_key = (event["video_id"], event["source_url"])
            attempt_ordinals[ordinal_key] = attempt_ordinals.get(ordinal_key, 0) + 1
            attempt_ordinal = attempt_ordinals[ordinal_key]
            if event["event_type"] == "failure":
                failure_ordinals[ordinal_key] = failure_ordinals.get(ordinal_key, 0) + 1
                failure_ordinal = failure_ordinals[ordinal_key]
                classification = classify_acquisition_failure(
                    event["error_type"], event["error"]
                )
                failure_class = classification["failure_class"]
                retryable: bool | None = bool(classification["retryable"])
                retry_after = retry_after_timestamp(
                    finished_at=event["finished_at"],
                    retry_base_hours=classification["retry_base_hours"],
                    failure_ordinal=failure_ordinal,
                )
                receipt_source = (
                    "legacy_backfill_run_current_url_fallback"
                    if event["source_url_fallback"]
                    else "legacy_backfill_run"
                )
            else:
                failure_class = ""
                retryable = None
                retry_after = None
                receipt_source = "legacy_transcript_artifact"
            receipt = {
                "contract": "transcript_acquisition_attempt_v1",
                "run_id": event["run_id"],
                "video_id": event["video_id"],
                "platform": event["platform"],
                "external_id": event["external_id"],
                "source_url": event["source_url"],
                "model_name": event["model_name"],
                "outcome": event["event_type"],
                "failure_class": failure_class,
                "retryable": retryable,
                "retry_after": retry_after,
                "error_type": event["error_type"],
                "error": event["error"],
                "attempt_ordinal": attempt_ordinal,
                "receipt_source": receipt_source,
                "runtime_fingerprint": "",
                "claim_id": "",
                "started_at": event["started_at"],
                "finished_at": event["finished_at"],
            }
            attempt_id = str(event["attempt_id"] or "")
            if not attempt_id:
                attempt_id = "transcript_attempt_" + canonical_sha256({
                    "receipt": receipt,
                    "failure_index": event.get("failure_index"),
                    "transcript_id": event.get("transcript_id"),
                })[:24]
            receipt["attempt_id"] = attempt_id
            receipt_sha256 = canonical_sha256(receipt)
            inserted = connection.execute(
                """
                INSERT OR IGNORE INTO mt_transcript_acquisition_attempts(
                    attempt_id, run_id, video_id, platform, external_id,
                    source_url, model_name, outcome, failure_class,
                    retryable, retry_after, error_type, error,
                    attempt_ordinal, receipt_source, runtime_fingerprint, claim_id,
                    attempt_contract, receipt_sha256, started_at, finished_at
                ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    attempt_id, event["run_id"], event["video_id"], event["platform"],
                    event["external_id"], event["source_url"], event["model_name"],
                    event["event_type"], failure_class,
                    None if retryable is None else int(retryable), retry_after,
                    event["error_type"], event["error"], attempt_ordinal,
                    receipt_source, "", "", receipt["contract"], receipt_sha256,
                    event["started_at"], event["finished_at"],
                ),
            )
            if inserted.rowcount:
                inserted_attempt_ids.append(attempt_id)
                if event["event_type"] == "failure":
                    inserted_failures += 1
                else:
                    inserted_successes += 1
        applied_at = utc_now()
        migration_receipt = {
            "contract": "transcript_legacy_history_migration_v2",
            "migration_id": migration_id,
            "source_failure_events": sum(
                event["event_type"] == "failure" for event in events
            ),
            "source_success_artifacts": sum(
                event["event_type"] == "success" for event in events
            ),
            "inserted_failures": inserted_failures,
            "inserted_successes": inserted_successes,
            "failure_source_url_current_row_fallbacks": sum(
                event["event_type"] == "failure" and event["source_url_fallback"]
                for event in events
            ),
            "inserted_attempt_ids_sha256": canonical_sha256(inserted_attempt_ids),
            "applied_at": applied_at,
        }
        connection.execute(
            """
            INSERT INTO mt_transcript_ledger_migrations(
                migration_id, receipt_json, applied_at
            ) VALUES(?, ?, ?)
            """,
            (
                migration_id,
                json.dumps(migration_receipt, sort_keys=True),
                applied_at,
            ),
        )

    def select_candidates(
        self,
        *,
        topic: str,
        limit: int,
        external_ids: Sequence[str] = (),
        platforms: Sequence[str] = ("youtube", "tiktok", "instagram", "facebook"),
        minimum_topic_matches: int = 1,
    ) -> list[Candidate]:
        terms = topic_terms(topic)
        if not terms:
            raise ValueError("topic must include at least one meaningful term")
        platform_values = tuple(dict.fromkeys(value.lower() for value in platforms))
        platform_marks = ",".join("?" for _ in platform_values)
        clauses = [f"v.platform IN ({platform_marks})"]
        parameters: list[Any] = list(platform_values)
        if external_ids:
            id_values = tuple(dict.fromkeys(str(value) for value in external_ids))
            id_marks = ",".join("?" for _ in id_values)
            clauses.append(f"v.external_id IN ({id_marks})")
            parameters.extend(id_values)
        query = f"""
            WITH latest AS (
                SELECT o.*,
                       ROW_NUMBER() OVER (
                           PARTITION BY o.video_id
                           ORDER BY o.observed_at DESC, o.observation_id DESC
                       ) AS row_number
                FROM mt_accepted_metric_observations_v1 o
                JOIN mt_accepted_full_evidence_v1 accepted
                  ON accepted.observation_id = o.observation_id
            )
            SELECT v.video_id, v.platform, v.external_id, v.creator_id,
                   e.title, e.caption, e.description, e.url,
                   e.duration_seconds,
                   o.observation_key, o.observed_at, o.views, o.likes, o.comments,
                   o.shares, o.saves, o.relative_strength
            FROM mt_videos v
            JOIN latest o ON o.video_id=v.video_id AND o.row_number=1
            JOIN mt_accepted_full_evidence_v1 e
              ON e.observation_id=o.observation_id
            WHERE {' AND '.join(clauses)}
            ORDER BY o.views DESC, o.relative_strength DESC
            LIMIT ?
        """
        parameters.append(max(limit * 20, 200))
        with closing(self.connect()) as connection:
            rows = connection.execute(query, parameters).fetchall()

        candidates: list[Candidate] = []
        for row in rows:
            platform = str(row["platform"])
            policy = DEFAULT_POLICIES.get(platform)
            if policy is None:
                continue
            duration = float(row["duration_seconds"] or 0.0)
            source_text = " ".join(
                str(row[field] or "") for field in ("title", "caption", "description")
            ).lower()
            matches = tuple(term for term in terms if re.search(rf"\b{re.escape(term)}\b", source_text))
            candidate = Candidate(
                video_id=str(row["video_id"]),
                platform=platform,
                external_id=str(row["external_id"]),
                creator_id=str(row["creator_id"]),
                title=str(row["title"] or ""),
                caption=str(row["caption"] or ""),
                description=str(row["description"] or ""),
                source_url=str(row["url"] or ""),
                duration_seconds=duration,
                observation_key=str(row["observation_key"]),
                observed_at=str(row["observed_at"]),
                views=int(row["views"] or 0),
                likes=int(row["likes"] or 0),
                comments=int(row["comments"] or 0),
                shares=int(row["shares"] or 0),
                saves=int(row["saves"] or 0),
                relative_strength=float(row["relative_strength"] or 0.0),
                topic_terms=tuple(terms),
                topic_matches=matches,
            )
            if duration <= 0 or duration > policy.maximum_duration_seconds:
                continue
            if candidate.views < policy.minimum_views:
                continue
            if candidate.engagement_rate < policy.minimum_engagement_rate:
                continue
            if len(matches) < minimum_topic_matches:
                continue
            if not is_usable_source_url(
                candidate.platform, candidate.external_id, candidate.source_url
            ):
                continue
            candidates.append(candidate)
            if len(candidates) >= limit:
                break
        return candidates

    def select_backfill_candidates(
        self,
        *,
        limit: int,
        platforms: Sequence[str] = ("youtube", "tiktok", "instagram", "facebook"),
        topic: str = "",
        trend_ids: Sequence[str] = (),
        exclude_creator_ids: Sequence[str] = (),
    ) -> list[Candidate]:
        """Select high-performing untranscribed videos for a resumable bank fill.

        Eligible rows retain the existing accepted-evidence, source, topic,
        performance, artifact, failure-cooldown, and active-claim gates.  The
        final ordering admits each creator's strongest eligible video before
        considering a second video from any creator.  Callers may also exclude
        creators already represented in an in-progress cohort.
        """

        platform_values = tuple(dict.fromkeys(value.lower() for value in platforms))
        if not platform_values or limit <= 0:
            return []
        marks = ",".join("?" for _ in platform_values)
        trend_values = tuple(dict.fromkeys(str(value) for value in trend_ids if str(value)))
        excluded_creators = tuple(dict.fromkeys(
            str(value).strip()
            for value in (exclude_creator_ids or ())
            if str(value).strip()
        ))
        filter_terms = tuple(topic_terms(topic))
        selection_time = utc_now()
        runtime_fingerprint = extractor_provenance()["fingerprint"]
        trend_clause = ""
        topic_clause = ""
        creator_clause = ""
        topic_parameters: tuple[str, ...] = ()
        creator_parameters: tuple[str, ...] = ()
        if excluded_creators:
            creator_marks = ",".join("?" for _ in excluded_creators)
            creator_clause = f"AND v.creator_id NOT IN ({creator_marks})"
            creator_parameters = excluded_creators
        if trend_values:
            trend_marks = ",".join("?" for _ in trend_values)
            trend_clause = f"""
                AND EXISTS (
                    SELECT 1 FROM mt_accepted_trend_memberships_v1 target
                    WHERE target.video_id = v.video_id
                      AND target.trend_id IN ({trend_marks})
                )
            """
        elif filter_terms:
            topic_marks = " OR ".join(
                "LOWER(e.title || ' ' || e.caption || ' ' || e.description) LIKE ?"
                for _ in filter_terms
            )
            topic_clause = f"AND ({topic_marks})"
            topic_parameters = tuple(f"%{term}%" for term in filter_terms)
        query = f"""
            WITH latest AS (
                SELECT o.*,
                       ROW_NUMBER() OVER (
                           PARTITION BY o.video_id
                           ORDER BY o.observed_at DESC, o.observation_id DESC
                       ) AS row_number
                FROM mt_accepted_metric_observations_v1 o
                JOIN mt_accepted_full_evidence_v1 accepted
                  ON accepted.observation_id = o.observation_id
            )
            SELECT v.video_id, v.platform, v.external_id, v.creator_id,
                   e.title, e.caption, e.description, e.url,
                   e.duration_seconds,
                   o.observation_key, o.observed_at, o.views, o.likes, o.comments,
                   o.shares, o.saves, o.relative_strength
            FROM mt_videos v
            JOIN latest o ON o.video_id=v.video_id AND o.row_number=1
            JOIN mt_accepted_full_evidence_v1 e
              ON e.observation_id=o.observation_id
            LEFT JOIN mt_transcript_artifacts artifact ON artifact.video_id=v.video_id
            WHERE v.platform IN ({marks}) AND artifact.video_id IS NULL
              AND NOT EXISTS (
                  SELECT 1
                  FROM mt_transcript_acquisition_attempts attempt
                  WHERE attempt.video_id=v.video_id
                    AND attempt.source_url=e.url
                    AND attempt.outcome='failure'
                    AND (
                        attempt.retryable=0
                        OR (attempt.retry_after IS NOT NULL AND attempt.retry_after > ?)
                    )
                    AND (
                        attempt.failure_class != 'extractor_unsupported'
                        OR attempt.runtime_fingerprint = ?
                    )
              )
              AND NOT EXISTS (
                  SELECT 1
                  FROM mt_transcript_acquisition_claims claim
                  WHERE claim.video_id=v.video_id
                    AND claim.source_url=e.url
                    AND claim.released_at IS NULL
                    AND claim.expires_at > ?
              )
            {creator_clause}
            {trend_clause}
            {topic_clause}
            ORDER BY
                ROW_NUMBER() OVER (
                    PARTITION BY v.creator_id
                    ORDER BY o.views DESC, o.relative_strength DESC,
                             o.observed_at DESC, v.video_id
                ),
                o.views DESC, o.relative_strength DESC,
                o.observed_at DESC, v.video_id
            LIMIT ?
        """
        with closing(self.connect()) as connection:
            rows = connection.execute(
                query,
                (
                    *platform_values,
                    selection_time,
                    runtime_fingerprint,
                    selection_time,
                    *creator_parameters,
                    *trend_values,
                    *topic_parameters,
                    max(limit * 20, 500),
                ),
            ).fetchall()

        candidates: list[Candidate] = []
        for row in rows:
            platform = str(row["platform"])
            policy = DEFAULT_POLICIES.get(platform)
            if policy is None:
                continue
            duration = float(row["duration_seconds"] or 0.0)
            views_count = int(row["views"] or 0)
            engagement = (
                sum(int(row[field] or 0) for field in ("likes", "comments", "shares", "saves"))
                / views_count
                if views_count else 0.0
            )
            if (
                duration <= 0
                or duration > policy.maximum_duration_seconds
                or views_count < policy.minimum_views
                or engagement < policy.minimum_engagement_rate
                or not is_usable_source_url(platform, row["external_id"], row["url"])
            ):
                continue
            metadata_text = " ".join(
                str(row[field] or "") for field in ("title", "caption", "description")
            )
            metadata_vocabulary = {token.lower() for token in words(metadata_text)}
            filter_matches = tuple(term for term in filter_terms if term in metadata_vocabulary)
            if filter_terms and len(filter_matches) < 2 and not trend_values:
                continue
            terms = filter_terms or tuple(topic_terms(metadata_text)[:40])
            if len(terms) < 2:
                continue
            candidates.append(Candidate(
                video_id=str(row["video_id"]),
                platform=platform,
                external_id=str(row["external_id"]),
                creator_id=str(row["creator_id"]),
                title=str(row["title"] or ""),
                caption=str(row["caption"] or ""),
                description=str(row["description"] or ""),
                source_url=str(row["url"] or ""),
                duration_seconds=duration,
                observation_key=str(row["observation_key"]),
                observed_at=str(row["observed_at"]),
                views=views_count,
                likes=int(row["likes"] or 0),
                comments=int(row["comments"] or 0),
                shares=int(row["shares"] or 0),
                saves=int(row["saves"] or 0),
                relative_strength=float(row["relative_strength"] or 0.0),
                topic_terms=terms,
                topic_matches=filter_matches or (filter_terms if trend_values else terms),
            ))

        first_per_creator: list[Candidate] = []
        remaining: list[Candidate] = []
        represented_creators: set[str] = set()
        for candidate in candidates:
            if candidate.creator_id not in represented_creators:
                represented_creators.add(candidate.creator_id)
                first_per_creator.append(candidate)
            else:
                remaining.append(candidate)
        return [*first_per_creator, *remaining][:limit]

    def claim_candidate(
        self,
        *,
        run_id: str,
        candidate: Candidate,
        allow_existing_artifact: bool = False,
    ) -> dict[str, Any]:
        """Authoritatively claim one candidate immediately before real work."""

        claimed_at = utc_now()
        runtime_fingerprint = extractor_provenance()["fingerprint"]
        expires_at = (
            parse_timestamp(claimed_at) + timedelta(hours=ACQUISITION_CLAIM_HOURS)
        ).isoformat()
        with closing(self.connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                """
                UPDATE mt_transcript_acquisition_claims
                SET released_at=?, release_reason='lease_expired'
                WHERE released_at IS NULL AND expires_at <= ?
                """,
                (claimed_at, claimed_at),
            )
            current = connection.execute(
                """
                WITH latest AS (
                    SELECT o.*,
                           ROW_NUMBER() OVER (
                               PARTITION BY o.video_id
                               ORDER BY o.observed_at DESC,
                                        o.observation_id DESC
                           ) AS row_number
                    FROM mt_accepted_metric_observations_v1 o
                    JOIN mt_accepted_full_evidence_v1 accepted
                      ON accepted.observation_id = o.observation_id
                )
                SELECT v.platform, v.external_id, e.url, e.duration_seconds,
                       o.views, o.likes, o.comments, o.shares, o.saves,
                       artifact.video_id AS artifact_video_id
                FROM mt_videos v
                JOIN latest o ON o.video_id=v.video_id AND o.row_number=1
                JOIN mt_accepted_full_evidence_v1 e
                  ON e.observation_id=o.observation_id
                LEFT JOIN mt_transcript_artifacts artifact ON artifact.video_id=v.video_id
                WHERE v.video_id=?
                """,
                (candidate.video_id,),
            ).fetchone()
            reason = "admitted"
            if current is None:
                reason = "source_missing"
            elif current["artifact_video_id"] is not None and not allow_existing_artifact:
                reason = "artifact_already_exists"
            elif (
                str(current["url"] or "") != candidate.source_url
                or str(current["platform"] or "") != candidate.platform
                or str(current["external_id"] or "") != candidate.external_id
            ):
                reason = "source_identity_changed"
            else:
                policy = DEFAULT_POLICIES.get(candidate.platform)
                views_count = int(current["views"] or 0)
                engagement = (
                    sum(
                        int(current[field] or 0)
                        for field in ("likes", "comments", "shares", "saves")
                    ) / views_count
                    if views_count else 0.0
                )
                duration = float(current["duration_seconds"] or 0.0)
                if (
                    policy is None
                    or duration <= 0
                    or duration > policy.maximum_duration_seconds
                    or views_count < policy.minimum_views
                    or engagement < policy.minimum_engagement_rate
                    or not is_usable_source_url(
                        candidate.platform,
                        candidate.external_id,
                        candidate.source_url,
                    )
                ):
                    reason = "performance_or_source_policy_changed"
            if reason == "admitted":
                blocked_attempt = connection.execute(
                    """
                    SELECT 1 FROM mt_transcript_acquisition_attempts
                    WHERE video_id=? AND source_url=? AND outcome='failure'
                      AND (retryable=0 OR (retry_after IS NOT NULL AND retry_after > ?))
                      AND (
                          failure_class != 'extractor_unsupported'
                          OR runtime_fingerprint=?
                      )
                    LIMIT 1
                    """,
                    (
                        candidate.video_id,
                        candidate.source_url,
                        claimed_at,
                        runtime_fingerprint,
                    ),
                ).fetchone()
                active_claim = connection.execute(
                    """
                    SELECT 1 FROM mt_transcript_acquisition_claims
                    WHERE video_id=? AND source_url=? AND released_at IS NULL
                    LIMIT 1
                    """,
                    (candidate.video_id, candidate.source_url),
                ).fetchone()
                if blocked_attempt is not None:
                    reason = "failure_cooldown_or_permanent"
                elif active_claim is not None:
                    reason = "already_claimed"
            receipt = {
                "contract": "transcript_acquisition_claim_v2",
                "admitted": reason == "admitted",
                "reason": reason,
                "run_id": run_id,
                "video_id": candidate.video_id,
                "platform": candidate.platform,
                "external_id": candidate.external_id,
                "source_url": candidate.source_url,
                "claimed_at": claimed_at,
                "expires_at": expires_at,
                "allow_existing_artifact": allow_existing_artifact,
            }
            if reason != "admitted":
                receipt["receipt_sha256"] = canonical_sha256(receipt)
                connection.commit()
                return receipt
            claim_id = "transcript_claim_" + canonical_sha256(receipt)[:24]
            receipt["claim_id"] = claim_id
            receipt["receipt_sha256"] = canonical_sha256(receipt)
            connection.execute(
                """
                INSERT INTO mt_transcript_acquisition_claims(
                    claim_id, run_id, video_id, platform, external_id,
                    source_url, claim_contract, receipt_sha256,
                    claimed_at, expires_at
                ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    claim_id,
                    run_id,
                    candidate.video_id,
                    candidate.platform,
                    candidate.external_id,
                    candidate.source_url,
                    receipt["contract"],
                    receipt["receipt_sha256"],
                    claimed_at,
                    expires_at,
                ),
            )
            connection.commit()
        return receipt

    def record_acquisition_attempt(
        self,
        *,
        run_id: str,
        candidate: Candidate,
        model_name: str,
        outcome: str,
        started_at: str,
        finished_at: str,
        error_type: str = "",
        error: str = "",
        claim_id: str = "",
        receipt_source: str = "direct_backfill",
    ) -> dict[str, Any]:
        if outcome not in {"success", "failure"}:
            raise ValueError("outcome must be success or failure")
        with closing(self.connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            receipt = self._record_acquisition_attempt_in_connection(
                connection,
                run_id=run_id,
                candidate=candidate,
                model_name=model_name,
                outcome=outcome,
                started_at=started_at,
                finished_at=finished_at,
                error_type=error_type,
                error=error,
                claim_id=claim_id,
                receipt_source=receipt_source,
            )
            connection.commit()
        return receipt

    @staticmethod
    def _record_acquisition_attempt_in_connection(
        connection: sqlite3.Connection,
        *,
        run_id: str,
        candidate: Candidate,
        model_name: str,
        outcome: str,
        started_at: str,
        finished_at: str,
        error_type: str,
        error: str,
        claim_id: str,
        receipt_source: str,
    ) -> dict[str, Any]:
        runtime_fingerprint = extractor_provenance()["fingerprint"]
        attempt_ordinal = int(connection.execute(
            """
            SELECT COUNT(*) + 1
            FROM mt_transcript_acquisition_attempts
            WHERE video_id=? AND source_url=?
            """,
            (candidate.video_id, candidate.source_url),
        ).fetchone()[0])
        if outcome == "failure":
            classification = classify_acquisition_failure(error_type, error)
            failure_class = str(classification["failure_class"])
            failure_ordinal = int(connection.execute(
                """
                SELECT COUNT(*) + 1
                FROM mt_transcript_acquisition_attempts
                WHERE video_id=? AND source_url=? AND outcome='failure'
                  AND failure_class=?
                  AND (
                      failure_class != 'extractor_unsupported'
                      OR runtime_fingerprint=?
                  )
                """,
                (
                    candidate.video_id,
                    candidate.source_url,
                    failure_class,
                    runtime_fingerprint,
                ),
            ).fetchone()[0])
            retry_after = retry_after_timestamp(
                finished_at=finished_at,
                retry_base_hours=classification["retry_base_hours"],
                failure_ordinal=failure_ordinal,
            )
            retryable: bool | None = bool(classification["retryable"])
        else:
            retry_after = None
            failure_class = ""
            retryable = None
        receipt = {
            "contract": "transcript_acquisition_attempt_v1",
            "run_id": run_id,
            "video_id": candidate.video_id,
            "platform": candidate.platform,
            "external_id": candidate.external_id,
            "source_url": candidate.source_url,
            "model_name": model_name,
            "outcome": outcome,
            "failure_class": failure_class,
            "retryable": retryable,
            "retry_after": retry_after,
            "error_type": error_type,
            "error": error[:500],
            "attempt_ordinal": attempt_ordinal,
            "receipt_source": receipt_source,
            "runtime_fingerprint": runtime_fingerprint,
            "claim_id": claim_id,
            "started_at": started_at,
            "finished_at": finished_at,
        }
        attempt_id = "transcript_attempt_" + canonical_sha256(receipt)[:24]
        receipt["attempt_id"] = attempt_id
        receipt["receipt_sha256"] = canonical_sha256(receipt)
        connection.execute(
            """
            INSERT INTO mt_transcript_acquisition_attempts(
                attempt_id, run_id, video_id, platform, external_id,
                source_url, model_name, outcome, failure_class,
                retryable, retry_after, error_type, error,
                attempt_ordinal, receipt_source, runtime_fingerprint, claim_id,
                attempt_contract, receipt_sha256, started_at, finished_at
            ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                attempt_id, run_id, candidate.video_id, candidate.platform,
                candidate.external_id, candidate.source_url, model_name, outcome,
                failure_class, None if retryable is None else int(retryable),
                retry_after, error_type, error[:500], attempt_ordinal,
                receipt_source, runtime_fingerprint, claim_id, receipt["contract"],
                receipt["receipt_sha256"], started_at, finished_at,
            ),
        )
        if claim_id:
            released = connection.execute(
                """
                UPDATE mt_transcript_acquisition_claims
                SET released_at=?, release_reason=?
                WHERE claim_id=? AND run_id=? AND released_at IS NULL
                """,
                (finished_at, outcome, claim_id, run_id),
            )
            if released.rowcount != 1:
                raise RuntimeError(
                    f"acquisition claim was not active for attempt: {claim_id}"
                )
        return receipt

    def release_claim(
        self,
        *,
        run_id: str,
        claim_id: str,
        reason: str,
    ) -> bool:
        with closing(self.connect()) as connection:
            released = connection.execute(
                """
                UPDATE mt_transcript_acquisition_claims
                SET released_at=?, release_reason=?
                WHERE claim_id=? AND run_id=? AND released_at IS NULL
                """,
                (utc_now(), reason[:80], claim_id, run_id),
            )
            connection.commit()
            return released.rowcount == 1

    def attempt_ledger_status(self) -> dict[str, Any]:
        now = utc_now()
        runtime_fingerprint = extractor_provenance()["fingerprint"]
        with closing(self.connect()) as connection:
            aggregate_rows = connection.execute(
                """
                SELECT outcome, failure_class, COUNT(*) AS attempt_count,
                       COUNT(DISTINCT video_id) AS video_count
                FROM mt_transcript_acquisition_attempts
                GROUP BY outcome, failure_class
                ORDER BY outcome, failure_class
                """
            ).fetchall()
            permanent_blocked = int(connection.execute(
                """
                SELECT COUNT(DISTINCT v.video_id)
                FROM mt_videos v
                JOIN mt_accepted_full_evidence_v1 evidence
                  ON evidence.observation_id = (
                      SELECT current.observation_id
                      FROM mt_accepted_full_evidence_v1 current
                      WHERE current.video_id = v.video_id
                      ORDER BY current.accepted_at DESC,
                               current.observation_id DESC
                      LIMIT 1
                  )
                LEFT JOIN mt_transcript_artifacts artifact ON artifact.video_id=v.video_id
                WHERE artifact.video_id IS NULL
                  AND EXISTS (
                      SELECT 1 FROM mt_transcript_acquisition_attempts attempt
                      WHERE attempt.video_id=v.video_id
                        AND attempt.source_url=evidence.url
                        AND attempt.outcome='failure'
                        AND attempt.retryable=0
                  )
                """
            ).fetchone()[0])
            cooldown_deferred = int(connection.execute(
                """
                SELECT COUNT(DISTINCT v.video_id)
                FROM mt_videos v
                JOIN mt_accepted_full_evidence_v1 evidence
                  ON evidence.observation_id = (
                      SELECT current.observation_id
                      FROM mt_accepted_full_evidence_v1 current
                      WHERE current.video_id = v.video_id
                      ORDER BY current.accepted_at DESC,
                               current.observation_id DESC
                      LIMIT 1
                  )
                LEFT JOIN mt_transcript_artifacts artifact ON artifact.video_id=v.video_id
                WHERE artifact.video_id IS NULL
                  AND EXISTS (
                      SELECT 1 FROM mt_transcript_acquisition_attempts attempt
                      WHERE attempt.video_id=v.video_id
                        AND attempt.source_url=evidence.url
                        AND attempt.outcome='failure'
                        AND attempt.retryable=1
                        AND attempt.retry_after > ?
                        AND (
                            attempt.failure_class != 'extractor_unsupported'
                            OR attempt.runtime_fingerprint=?
                        )
                  )
                """,
                (now, runtime_fingerprint),
            ).fetchone()[0])
        return {
            "contract": "transcript_acquisition_ledger_status_v1",
            "as_of": now,
            "append_only": True,
            "aggregates": [dict(row) for row in aggregate_rows],
            "current_source_url_exclusions": {
                "permanent": permanent_blocked,
                "cooldown_deferred": cooldown_deferred,
            },
        }

    def run_backfill(
        self,
        *,
        limit: int,
        platforms: Sequence[str],
        model_name: str,
        topic: str = "",
        trend_ids: Sequence[str] = (),
        exclude_creator_ids: Sequence[str] = (),
        cookies_from_browser: str | None = None,
    ) -> dict[str, Any]:
        started_at = utc_now()
        run_id = f"transcript_run_{uuid.uuid4().hex}"
        candidate_pool = self.select_backfill_candidates(
            limit=min(max(limit * 4, limit), 2_000),
            platforms=platforms,
            topic=topic,
            trend_ids=trend_ids,
            exclude_creator_ids=exclude_creator_ids,
        )
        model: Any = None
        runtime_failure: dict[str, str] | None = None
        if candidate_pool:
            try:
                executable_version("yt-dlp")
                executable_version("ffmpeg")
            except Exception as exc:
                runtime_failure = {
                    "phase": "tool_preflight",
                    "error_type": type(exc).__name__,
                    "error": str(exc)[:500],
                }
        if candidate_pool and runtime_failure is None:
            try:
                model = load_whisper_model(model_name)
                validate_transcription_runtime(model_name)
            except Exception as exc:
                runtime_failure = {
                    "phase": "model_load",
                    "error_type": type(exc).__name__,
                    "error": str(exc)[:500],
                }
        candidates: list[Candidate] = []
        claim_receipts: list[dict[str, Any]] = []
        artifacts: list[dict[str, Any]] = []
        failures: list[dict[str, Any]] = []
        attempts: list[dict[str, Any]] = []
        audit_failures: list[dict[str, Any]] = []
        for candidate in candidate_pool if runtime_failure is None else []:
            if len(attempts) + len(audit_failures) >= limit:
                break
            claim = self.claim_candidate(run_id=run_id, candidate=candidate)
            claim_receipts.append(claim)
            if not claim["admitted"]:
                continue
            candidates.append(candidate)
            attempt_started_at = utc_now()
            try:
                artifact = self.transcribe(
                    candidate,
                    model=model,
                    model_name=model_name,
                    cookies_from_browser=cookies_from_browser,
                    persist=False,
                )
            except Exception as exc:
                try:
                    attempt = self.record_acquisition_attempt(
                        run_id=run_id,
                        candidate=candidate,
                        model_name=model_name,
                        outcome="failure",
                        started_at=attempt_started_at,
                        finished_at=utc_now(),
                        error_type=type(exc).__name__,
                        error=str(exc),
                        claim_id=claim["claim_id"],
                    )
                except Exception as audit_exc:
                    release_error = ""
                    try:
                        self.release_claim(
                            run_id=run_id,
                            claim_id=claim["claim_id"],
                            reason="failure_audit_persistence_error",
                        )
                    except Exception as release_exc:
                        release_error = (
                            f"{type(release_exc).__name__}: {release_exc}"
                        )[:500]
                    audit_failures.append({
                        "phase": "failure_audit_persistence",
                        "video_id": candidate.video_id,
                        "error_type": type(audit_exc).__name__,
                        "error": str(audit_exc)[:500],
                        "claim_release_error": release_error,
                    })
                    break
                attempts.append(attempt)
                failures.append({
                    "attempt_id": attempt["attempt_id"],
                    "video_id": candidate.video_id,
                    "external_id": candidate.external_id,
                    "source_url": candidate.source_url,
                    "error_type": type(exc).__name__,
                    "error": str(exc)[:500],
                    "failure_class": attempt["failure_class"],
                    "retryable": attempt["retryable"],
                    "retry_after": attempt["retry_after"],
                })
                continue
            transcript_text = str(artifact.pop("_transcript_text"))
            try:
                attempt = self.persist_successful_acquisition(
                    artifact=artifact,
                    transcript_text=transcript_text,
                    run_id=run_id,
                    candidate=candidate,
                    model_name=model_name,
                    started_at=attempt_started_at,
                    finished_at=utc_now(),
                    claim_id=claim["claim_id"],
                )
            except Exception as exc:
                release_error = ""
                try:
                    self.release_claim(
                        run_id=run_id,
                        claim_id=claim["claim_id"],
                        reason="success_audit_persistence_error",
                    )
                except Exception as release_exc:
                    release_error = (
                        f"{type(release_exc).__name__}: {release_exc}"
                    )[:500]
                audit_failures.append({
                    "phase": "atomic_success_persistence",
                    "video_id": candidate.video_id,
                    "transcript_id": artifact["transcript_id"],
                    "error_type": type(exc).__name__,
                    "error": str(exc)[:500],
                    "claim_release_error": release_error,
                })
                break
            attempts.append(attempt)
            artifacts.append({
                "transcript_id": artifact["transcript_id"],
                "video_id": artifact["video_id"],
                "external_id": artifact["external_id"],
                "decision": artifact["audit"]["decision"],
                "views": artifact["source_metrics"]["views"],
                "word_count": artifact["word_count"],
                "language": artifact["whisper_language"],
                "audio_sha256": artifact["audio_sha256"],
                "transcript_sha256": artifact["transcript_sha256"],
                "acquisition_attempt_id": attempt["attempt_id"],
            })
        finished_at = utc_now()
        if audit_failures:
            status = "audit_failed"
        elif runtime_failure:
            status = "blocked_runtime"
        elif failures:
            status = "partial" if artifacts else "failed"
        else:
            status = "completed"
        run_failures = list(failures)
        if runtime_failure:
            run_failures.append(runtime_failure)
        run_failures.extend(audit_failures)
        summary = {
            "run_id": run_id,
            "status": status,
            "policy": {
                "contract": "performance_ranked_resumable_whisper_backfill_v4",
                "limit": limit,
                "platforms": list(platforms),
                "model": model_name,
                "topic": topic,
                "trend_ids": list(trend_ids),
                "exclude_creator_ids": sorted({
                    str(value).strip()
                    for value in exclude_creator_ids
                    if str(value).strip()
                }),
                "platform_policies": {
                    platform: asdict(DEFAULT_POLICIES[platform])
                    for platform in platforms if platform in DEFAULT_POLICIES
                },
                "failure_admission": {
                    "contract": "source_url_scoped_exponential_cooldown_v1",
                    "permanent_failure_patterns": sorted(PERMANENT_FAILURE_PATTERNS),
                    "transient_failure_patterns": sorted(TRANSIENT_FAILURE_PATTERNS),
                    "maximum_retry_hours": MAX_FAILURE_RETRY_HOURS,
                },
                "claim_admission": {
                    "contract": "transactional_just_in_time_claim_v2",
                    "lease_hours": ACQUISITION_CLAIM_HOURS,
                },
            },
            "candidate_pool_count": len(candidate_pool),
            "candidate_count": len(candidates),
            "claim_count": sum(receipt["admitted"] for receipt in claim_receipts),
            "claim_receipts": claim_receipts,
            "candidates": [
                {
                    "video_id": item.video_id,
                    "external_id": item.external_id,
                    "platform": item.platform,
                    "title": item.title,
                    "source_metrics": item.source_metrics,
                }
                for item in candidates
            ],
            "artifact_count": len(artifacts),
            "passing_artifact_count": sum(item["decision"] == "PASS" for item in artifacts),
            "artifacts": artifacts,
            "attempt_count": len(attempts),
            "attempts": attempts,
            "failure_count": len(failures),
            "failures": failures,
            "runtime_failure": runtime_failure,
            "audit_failure_count": len(audit_failures),
            "audit_failures": audit_failures,
            "attempt_ledger": self.attempt_ledger_status(),
            "runtime_provenance": (
                transcription_runtime_provenance(model_name)
                if model is not None else None
            ),
            "started_at": started_at,
            "finished_at": finished_at,
        }
        manifest_root = self.storage_root / "runs"
        manifest_root.mkdir(parents=True, exist_ok=True)
        manifest_path = manifest_root / f"{run_id}.json"
        summary["manifest_path"] = str(manifest_path)
        atomic_write_json(manifest_path, summary)
        with closing(self.connect()) as connection:
            connection.execute(
                """
                INSERT INTO mt_transcript_backfill_runs(
                    run_id, status, policy_json, candidate_ids_json, artifact_ids_json,
                    failures_json, manifest_path, started_at, finished_at
                ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    run_id, status, json.dumps(summary["policy"], sort_keys=True),
                    json.dumps([item.video_id for item in candidates]),
                    json.dumps([item["transcript_id"] for item in artifacts]),
                    json.dumps(run_failures, sort_keys=True), str(manifest_path),
                    started_at, finished_at,
                ),
            )
            connection.commit()
        return summary

    def transcribe(
        self,
        candidate: Candidate,
        *,
        model: Any,
        model_name: str,
        force: bool = False,
        cookies_from_browser: str | None = None,
        persist: bool = True,
    ) -> dict[str, Any]:
        if not is_usable_source_url(
            candidate.platform, candidate.external_id, candidate.source_url
        ):
            raise RuntimeError(
                f"refusing unusable source URL for {candidate.video_id}"
            )
        existing = self.latest_artifact(candidate.video_id, model_name)
        if existing and not force:
            payload = json.loads(
                Path(existing["transcript_path"]).read_text(encoding="utf-8")
            )
            existing["audit"] = self._transcript_audit(
                candidate=candidate,
                transcript_text=str(payload.get("text") or ""),
                segments=list(payload.get("segments") or []),
                audio_hash=existing["audio_sha256"],
                transcript_hash=existing["transcript_sha256"],
            )
            with closing(self.connect()) as connection:
                connection.execute(
                    "UPDATE mt_transcript_artifacts SET audit_json=? WHERE transcript_id=?",
                    (
                        json.dumps(existing["audit"], sort_keys=True),
                        existing["transcript_id"],
                    ),
                )
                connection.commit()
            return {**existing, "reused": True}

        video_root = self.storage_root / "videos" / candidate.platform / candidate.external_id
        video_root.mkdir(parents=True, exist_ok=True)
        acquisition_id = f"acq_{uuid.uuid4().hex}"
        source_basename = f"source-{acquisition_id}"
        audio_template = video_root / f"{source_basename}.%(ext)s"
        command = [
            shutil.which("yt-dlp") or "yt-dlp",
            "--no-playlist",
            "--no-warnings",
            "--remote-components",
            "ejs:github",
            "--format",
            "b[protocol^=m3u8][height<=360][language^=en]/"
            "b[protocol^=m3u8][height<=360]/ba[language^=en]/ba/"
            "b[height<=360][acodec!=none][vcodec!=none]/b",
            "--extract-audio",
            "--audio-format",
            "m4a",
            "--audio-quality",
            "3",
            "--write-info-json",
            "--force-overwrites",
            "--output",
            str(audio_template),
        ]
        if cookies_from_browser:
            command.extend(["--cookies-from-browser", cookies_from_browser])
        command.append(candidate.source_url)
        temporary_root = self.storage_root / "_tmp"
        temporary_root.mkdir(parents=True, exist_ok=True)
        process_environment = dict(os.environ)
        process_environment["TMPDIR"] = str(temporary_root)
        runtime_provenance = validate_transcription_runtime(model_name)
        completed = subprocess.run(
            command,
            capture_output=True,
            text=True,
            timeout=300,
            env=process_environment,
        )
        if completed.returncode != 0:
            error_tail = "\n".join(completed.stderr.splitlines()[-12:])
            raise RuntimeError(
                f"yt-dlp failed for {candidate.video_id} with exit "
                f"{completed.returncode}: {error_tail}"
            )
        source_info_path = video_root / f"{source_basename}.info.json"
        if not source_info_path.is_file():
            raise RuntimeError(
                f"yt-dlp produced no source info JSON for {candidate.video_id}"
            )
        try:
            source_info = json.loads(source_info_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise RuntimeError(
                f"yt-dlp source info JSON is invalid for {candidate.video_id}: "
                f"{type(exc).__name__}"
            ) from exc
        provider_media_id = str(source_info.get("id") or "")
        if provider_media_id != candidate.external_id:
            raise RuntimeError(
                f"yt-dlp source identity mismatch for {candidate.video_id}: "
                f"expected {candidate.external_id}, received {provider_media_id}"
            )
        source_info_hash = file_sha256(source_info_path)
        audio_files = sorted(
            path for path in video_root.glob(f"{source_basename}.*")
            if path.suffix.lower() in {".m4a", ".mp3", ".opus", ".wav", ".webm"}
        )
        if not audio_files:
            raise RuntimeError(f"yt-dlp produced no audio file for {candidate.video_id}")
        audio_path = audio_files[0]
        audio_hash = file_sha256(audio_path)

        result = model.transcribe(
            str(audio_path),
            fp16=False,
            verbose=False,
            condition_on_previous_text=True,
        )
        transcript_text = " ".join(str(result.get("text") or "").split())
        segments = [
            {
                "id": int(segment.get("id") or index),
                "start": round(float(segment.get("start") or 0.0), 3),
                "end": round(float(segment.get("end") or 0.0), 3),
                "text": " ".join(str(segment.get("text") or "").split()),
            }
            for index, segment in enumerate(result.get("segments") or [])
            if str(segment.get("text") or "").strip()
        ]
        transcript_payload = {
            "schema_version": 2,
            "video_id": candidate.video_id,
            "platform": candidate.platform,
            "external_id": candidate.external_id,
            "source_url": candidate.source_url,
            "source_observation": candidate.source_metrics,
            "audio_sha256": audio_hash,
            "source_info_sha256": source_info_hash,
            "provider_media_id": provider_media_id,
            "extractor": str(source_info.get("extractor_key") or source_info.get("extractor") or ""),
            "runtime_provenance_sha256": canonical_sha256(runtime_provenance),
            "whisper_model": model_name,
            "language": str(result.get("language") or "unknown"),
            "text": transcript_text,
            "segments": segments,
        }
        transcript_hash = canonical_sha256(transcript_payload)
        transcript_id = f"whisper_{transcript_hash[:24]}"
        transcript_path = video_root / f"{transcript_id}.json"
        atomic_write_json(transcript_path, transcript_payload)
        word_count = len(words(transcript_text))
        created_at = utc_now()
        acquisition = {
            "tool": "yt-dlp",
            "tool_version": runtime_provenance["extractor"]["yt-dlp"],
            "ffmpeg_version": runtime_provenance["extractor"]["ffmpeg"],
            "command_contract": "audio_only_public_source_v2",
            "stdout_tail": completed.stdout.splitlines()[-5:],
            "source_info_path": str(source_info_path),
            "source_info_sha256": source_info_hash,
            "source_info_provider_media_id": provider_media_id,
            "source_info_extractor": transcript_payload["extractor"],
            "runtime_provenance": runtime_provenance,
            "acquisition_id": acquisition_id,
        }
        acquisition["receipt_sha256"] = canonical_sha256(acquisition)
        audit = self._transcript_audit(
            candidate=candidate,
            transcript_text=transcript_text,
            segments=segments,
            audio_hash=audio_hash,
            transcript_hash=transcript_hash,
        )
        artifact = {
            "transcript_id": transcript_id,
            "video_id": candidate.video_id,
            "platform": candidate.platform,
            "external_id": candidate.external_id,
            "source_url": candidate.source_url,
            "observation_key": candidate.observation_key,
            "source_metrics": candidate.source_metrics,
            "audio_path": str(audio_path),
            "audio_sha256": audio_hash,
            "transcript_path": str(transcript_path),
            "transcript_sha256": transcript_hash,
            "whisper_model": model_name,
            "whisper_language": transcript_payload["language"],
            "duration_seconds": candidate.duration_seconds,
            "word_count": word_count,
            "segment_count": len(segments),
            "acquisition": acquisition,
            "audit": audit,
            "created_at": created_at,
            "reused": False,
        }
        if persist:
            self._persist_artifact(artifact, transcript_text)
        else:
            artifact["_transcript_text"] = transcript_text
        return artifact

    @staticmethod
    def _transcript_audit(
        *,
        candidate: Candidate,
        transcript_text: str,
        segments: Sequence[dict[str, Any]],
        audio_hash: str,
        transcript_hash: str,
    ) -> dict[str, Any]:
        policy = DEFAULT_POLICIES[candidate.platform]
        transcript_vocabulary = {token.lower() for token in words(transcript_text)}
        transcript_matches = sorted(
            term for term in candidate.topic_terms if term in transcript_vocabulary
        )
        checks = {
            "audio_file_exists": True,
            "audio_sha256_bound": len(audio_hash) == 64,
            "source_observation_bound": bool(candidate.observation_key),
            "performance_views_floor": candidate.views >= policy.minimum_views,
            "performance_engagement_floor": (
                candidate.engagement_rate >= policy.minimum_engagement_rate
            ),
            "metadata_topic_relevance_present": bool(candidate.topic_matches),
            "transcript_topic_relevance_present": len(transcript_matches) >= 2,
            "transcript_word_floor": (
                len(words(transcript_text)) >= policy.minimum_transcript_words
            ),
            "timestamped_segments_present": bool(segments),
        }
        return {
            "contract": CURRENT_TRANSCRIPT_AUDIT_CONTRACT,
            "decision": "PASS" if all(checks.values()) else "REJECTED",
            "checks": checks,
            "policy": asdict(policy),
            "metadata_topic_matches": list(candidate.topic_matches),
            "transcript_topic_matches": transcript_matches,
            "minimum_transcript_topic_matches": 2,
            "metrics_sha256": canonical_sha256(candidate.source_metrics),
            "transcript_payload_sha256": transcript_hash,
            "audited_at": utc_now(),
        }

    def latest_artifact(self, video_id: str, model_name: str | None = None) -> dict[str, Any] | None:
        query = "SELECT * FROM mt_transcript_artifacts WHERE video_id=?"
        parameters: list[Any] = [video_id]
        if model_name:
            query += " AND whisper_model=?"
            parameters.append(model_name)
        query += " ORDER BY created_at DESC LIMIT 1"
        with closing(self.connect()) as connection:
            row = connection.execute(query, parameters).fetchone()
        return self._artifact_row(row) if row else None

    @staticmethod
    def _artifact_row(row: sqlite3.Row) -> dict[str, Any]:
        return {
            "transcript_id": row["transcript_id"],
            "video_id": row["video_id"],
            "platform": row["platform"],
            "external_id": row["external_id"],
            "source_url": row["source_url"],
            "observation_key": row["observation_key"],
            "source_metrics": json.loads(row["source_metrics_json"]),
            "audio_path": row["audio_path"],
            "audio_sha256": row["audio_sha256"],
            "transcript_path": row["transcript_path"],
            "transcript_sha256": row["transcript_sha256"],
            "whisper_model": row["whisper_model"],
            "whisper_language": row["whisper_language"],
            "duration_seconds": row["duration_seconds"],
            "word_count": row["word_count"],
            "segment_count": row["segment_count"],
            "acquisition": json.loads(row["acquisition_json"]),
            "audit": json.loads(row["audit_json"]),
            "created_at": row["created_at"],
        }

    def _persist_artifact(self, artifact: dict[str, Any], transcript_text: str) -> None:
        with closing(self.connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            self._persist_artifact_rows(connection, artifact, transcript_text)
            connection.commit()

    @staticmethod
    def _persist_artifact_rows(
        connection: sqlite3.Connection,
        artifact: dict[str, Any],
        transcript_text: str,
    ) -> None:
        connection.execute(
            """
            INSERT OR REPLACE INTO mt_transcript_artifacts(
                transcript_id, video_id, platform, external_id, source_url,
                observation_key, source_metrics_json, audio_path, audio_sha256,
                transcript_path, transcript_sha256, whisper_model,
                whisper_language, duration_seconds, word_count, segment_count,
                acquisition_json, audit_json, created_at
            ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                artifact["transcript_id"], artifact["video_id"], artifact["platform"],
                artifact["external_id"], artifact["source_url"], artifact["observation_key"],
                json.dumps(artifact["source_metrics"], sort_keys=True), artifact["audio_path"],
                artifact["audio_sha256"], artifact["transcript_path"],
                artifact["transcript_sha256"], artifact["whisper_model"],
                artifact["whisper_language"], artifact["duration_seconds"],
                artifact["word_count"], artifact["segment_count"],
                json.dumps(artifact["acquisition"], sort_keys=True),
                json.dumps(artifact["audit"], sort_keys=True), artifact["created_at"],
            ),
        )
        connection.execute(
            """
            INSERT INTO mt_content_genomes(
                video_id, transcript, language, opening_words, duration_seconds,
                transcript_embedding_ref, extraction_status, updated_at
            ) VALUES(?, ?, ?, ?, ?, ?, 'whisper_transcribed', ?)
            ON CONFLICT(video_id) DO UPDATE SET
                transcript=excluded.transcript,
                language=excluded.language,
                opening_words=excluded.opening_words,
                duration_seconds=COALESCE(
                    excluded.duration_seconds,
                    mt_content_genomes.duration_seconds
                ),
                transcript_embedding_ref=excluded.transcript_embedding_ref,
                extraction_status='whisper_transcribed',
                updated_at=excluded.updated_at
            """,
            (
                artifact["video_id"], transcript_text, artifact["whisper_language"],
                " ".join(words(transcript_text)[:28]), artifact["duration_seconds"],
                f"sha256:{artifact['transcript_sha256']}", artifact["created_at"],
            ),
        )

    def persist_successful_acquisition(
        self,
        *,
        artifact: dict[str, Any],
        transcript_text: str,
        run_id: str,
        candidate: Candidate,
        model_name: str,
        started_at: str,
        finished_at: str,
        claim_id: str,
        receipt_source: str = "direct_backfill",
    ) -> dict[str, Any]:
        """Commit the transcript rows, success attempt, and claim release atomically."""

        with closing(self.connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            self._persist_artifact_rows(connection, artifact, transcript_text)
            receipt = self._record_acquisition_attempt_in_connection(
                connection,
                run_id=run_id,
                candidate=candidate,
                model_name=model_name,
                outcome="success",
                started_at=started_at,
                finished_at=finished_at,
                error_type="",
                error="",
                claim_id=claim_id,
                receipt_source=receipt_source,
            )
            connection.commit()
        return receipt

    def build_cohort(
        self,
        *,
        topic: str,
        artifacts: Sequence[dict[str, Any]],
        target_language: str = "en",
        minimum_members: int = 5,
        minimum_creators: int = 3,
        minimum_total_views: int = 100_000,
    ) -> dict[str, Any]:
        unique_artifacts: dict[tuple[str, str], dict[str, Any]] = {}
        for item in artifacts:
            transcript_id = str(item.get("transcript_id") or "").strip()
            observation_key = str(item.get("observation_key") or "").strip()
            if not transcript_id or not observation_key:
                continue
            unique_artifacts.setdefault((transcript_id, observation_key), item)
        performance_passing = [
            item for item in unique_artifacts.values()
            if item.get("audit", {}).get("decision") == "PASS"
            and is_supported_transcript_audit_contract(
                item.get("audit", {}).get("contract")
            )
        ]
        passing = [
            item for item in performance_passing
            if str(item.get("whisper_language") or "").lower().startswith(
                target_language.lower(),
            )
        ]
        member_ids = sorted(item["transcript_id"] for item in passing)
        views = [int(item["source_metrics"].get("views") or 0) for item in passing]
        rates = [float(item["source_metrics"].get("engagement_rate") or 0.0) for item in passing]
        with closing(self.connect()) as connection:
            creator_rows = connection.execute(
                f"SELECT DISTINCT creator_id FROM mt_videos WHERE video_id IN ({','.join('?' for _ in passing)})",
                [item["video_id"] for item in passing],
            ).fetchall() if passing else []
        aggregate = {
            "source_artifact_count": len(artifacts),
            "unique_source_artifact_count": len(unique_artifacts),
            "duplicate_source_artifact_count": len(artifacts) - len(unique_artifacts),
            "member_count": len(passing),
            "excluded_language_count": len(performance_passing) - len(passing),
            "creator_count": len(creator_rows),
            "platform_count": len({item["platform"] for item in passing}),
            "total_views": sum(views),
            "median_views": median(views) if views else 0,
            "median_engagement_rate": median(rates) if rates else 0.0,
            "total_transcript_words": sum(int(item["word_count"]) for item in passing),
        }
        checks = {
            "minimum_members": aggregate["member_count"] >= minimum_members,
            "minimum_creators": aggregate["creator_count"] >= minimum_creators,
            "minimum_total_views": aggregate["total_views"] >= minimum_total_views,
            "all_members_performance_qualified": all(
                item.get("audit", {}).get("decision") == "PASS" for item in passing
            ),
            "all_members_target_language": all(
                str(item.get("whisper_language") or "").lower().startswith(
                    target_language.lower(),
                )
                for item in passing
            ),
            "all_members_source_bound": all(
                bool(item.get("observation_key")) and len(str(item.get("audio_sha256") or "")) == 64
                and len(str(item.get("transcript_sha256") or "")) == 64
                for item in passing
            ),
        }
        decision = "PASS" if all(checks.values()) else "INSUFFICIENT_EVIDENCE"
        policy = {
            "minimum_members": minimum_members,
            "minimum_creators": minimum_creators,
            "minimum_total_views": minimum_total_views,
            "target_language": target_language,
            "platform_policies": {
                platform: asdict(policy) for platform, policy in DEFAULT_POLICIES.items()
            },
        }
        created_at = utc_now()
        cohort_input = {
            "topic": topic,
            "member_ids": member_ids,
            "policy": policy,
            "aggregate": aggregate,
        }
        cohort_id = f"cohort_{canonical_sha256(cohort_input)[:24]}"
        audit = {
            "contract": "performance_qualified_transcript_cohort_v1",
            "decision": decision,
            "checks": checks,
            "input_sha256": canonical_sha256(cohort_input),
            "audited_at": created_at,
        }
        manifest = {
            "cohort_id": cohort_id,
            "topic": topic,
            "decision": decision,
            "members": passing,
            "policy": policy,
            "aggregate_metrics": aggregate,
            "audit": audit,
            "created_at": created_at,
        }
        manifest_root = self.storage_root / "cohorts"
        manifest_root.mkdir(parents=True, exist_ok=True)
        manifest_path = manifest_root / f"{cohort_id}.json"
        if manifest_path.is_file():
            existing = json.loads(manifest_path.read_text(encoding="utf-8"))
            return {**existing, "manifest_path": str(manifest_path)}
        atomic_write_json(manifest_path, manifest)
        with closing(self.connect()) as connection:
            connection.execute(
                """
                INSERT INTO mt_transcript_cohorts(
                    cohort_id, topic, decision, member_ids_json, policy_json,
                    aggregate_metrics_json, audit_json, manifest_path, created_at
                ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(cohort_id) DO NOTHING
                """,
                (
                    cohort_id, topic, decision, json.dumps(member_ids),
                    json.dumps(policy, sort_keys=True), json.dumps(aggregate, sort_keys=True),
                    json.dumps(audit, sort_keys=True), str(manifest_path), created_at,
                ),
            )
            connection.commit()
        return {**manifest, "manifest_path": str(manifest_path)}

    def audit_script_against_cohort(
        self,
        *,
        script_id: str,
        script_text: str,
        cohort_manifest_path: str | Path,
    ) -> dict[str, Any]:
        """Predict script relatability from verified high-performing transcripts.

        This is deliberately a prediction, not a claim that viewers found the script
        relatable.  Only post-publication audience behavior can establish that.
        """

        if not script_id.strip() or not script_text.strip():
            raise ValueError("script_id and script_text are required")
        manifest_path = Path(cohort_manifest_path).expanduser().resolve()
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        members = list(manifest.get("members") or [])
        aggregate = dict(manifest.get("aggregate_metrics") or {})
        cohort_audit = dict(manifest.get("audit") or {})

        integrity_failures: list[dict[str, str]] = []
        transcript_documents: list[dict[str, Any]] = []
        for member in members:
            transcript_path = Path(str(member.get("transcript_path") or ""))
            audio_path = Path(str(member.get("audio_path") or ""))
            if not transcript_path.is_file() or not audio_path.is_file():
                integrity_failures.append({
                    "transcript_id": str(member.get("transcript_id") or ""),
                    "error": "bound artifact file missing",
                })
                continue
            try:
                payload = json.loads(transcript_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as exc:
                integrity_failures.append({
                    "transcript_id": str(member.get("transcript_id") or ""),
                    "error": f"invalid transcript payload: {type(exc).__name__}",
                })
                continue
            transcript_hash = canonical_sha256(payload)
            audio_hash = file_sha256(audio_path)
            if transcript_hash != member.get("transcript_sha256"):
                integrity_failures.append({
                    "transcript_id": str(member.get("transcript_id") or ""),
                    "error": "transcript hash mismatch",
                })
                continue
            if audio_hash != member.get("audio_sha256"):
                integrity_failures.append({
                    "transcript_id": str(member.get("transcript_id") or ""),
                    "error": "audio hash mismatch",
                })
                continue
            transcript_documents.append({
                "transcript_id": member["transcript_id"],
                "video_id": member["video_id"],
                "text": str(payload.get("text") or ""),
                "views": int(member.get("source_metrics", {}).get("views") or 0),
                "engagement_rate": float(
                    member.get("source_metrics", {}).get("engagement_rate") or 0.0
                ),
            })

        script_tokens = _significant_tokens(script_text)
        document_tokens = [_significant_tokens(item["text"]) for item in transcript_documents]
        union_tokens = set().union(*(set(value) for value in document_tokens)) if document_tokens else set()
        script_overlap = (
            len(set(script_tokens) & union_tokens) / len(set(script_tokens))
            if script_tokens else 0.0
        )
        cosine_scores = [
            _tfidf_cosine(script_tokens, source_tokens, document_tokens)
            for source_tokens in document_tokens
        ]
        supported_sources = sum(score >= 0.07 for score in cosine_scores)

        recurrence: dict[str, int] = {}
        for source_tokens in document_tokens:
            for token in set(source_tokens):
                recurrence[token] = recurrence.get(token, 0) + 1
        opening_tokens = set(_significant_tokens(" ".join(words(script_text)[:45])))
        recurring_opening_terms = sorted(
            token for token in opening_tokens if recurrence.get(token, 0) >= 2
        )
        human_experience_terms = {
            "alone", "anxious", "anxiety", "burned", "burnout", "burnt", "care",
            "exhausted", "fear", "feel", "feeling", "frustrated", "hard", "hate",
            "overwhelmed", "pressure", "quit", "struggle", "stuck", "tired",
            "trying", "worry", "worse",
        }
        recurring_human_opening_terms = sorted(
            token for token in opening_tokens
            if token in human_experience_terms and recurrence.get(token, 0) >= 2
        )
        lowered_script = script_text.lower()
        pipeline_meta_phrases = (
            "attention gate", "content factory", "human-relatability", "source receipt",
            "transcript pattern", "passes human", "passes attention", "reveal the mechanism",
            "spoken pattern", "test the structure", "recognize themselves",
        )
        pipeline_meta_matches = sorted(
            phrase for phrase in pipeline_meta_phrases if phrase in lowered_script
        )

        source_claim = _extract_source_claim(script_text)
        source_claim_matches = True
        if source_claim:
            source_claim_matches = (
                source_claim["member_count"] == int(aggregate.get("member_count") or 0)
                and source_claim["total_views"] == int(aggregate.get("total_views") or 0)
            )

        checks = {
            "cohort_decision_pass": (
                manifest.get("decision") == "PASS"
                and cohort_audit.get("decision") == "PASS"
            ),
            "cohort_minimum_members": int(aggregate.get("member_count") or 0) >= 5,
            "cohort_minimum_creators": int(aggregate.get("creator_count") or 0) >= 3,
            "cohort_minimum_views": int(aggregate.get("total_views") or 0) >= 100_000,
            "all_artifact_hashes_verified": (
                not integrity_failures and len(transcript_documents) == len(members) and bool(members)
            ),
            "stated_source_claim_matches_cohort": source_claim_matches,
            "script_vocabulary_supported": script_overlap >= 0.18,
            "supported_by_three_transcripts": supported_sources >= 3,
            "opening_uses_recurring_human_language": len(recurring_opening_terms) >= 2,
            "opening_human_experience_backed": bool(recurring_human_opening_terms),
            "audience_facing_not_pipeline_meta": not pipeline_meta_matches,
        }
        weights = {
            "cohort_decision_pass": 10,
            "cohort_minimum_members": 5,
            "cohort_minimum_creators": 5,
            "cohort_minimum_views": 5,
            "all_artifact_hashes_verified": 15,
            "stated_source_claim_matches_cohort": 15,
            "script_vocabulary_supported": 10,
            "supported_by_three_transcripts": 10,
            "opening_uses_recurring_human_language": 5,
            "opening_human_experience_backed": 10,
            "audience_facing_not_pipeline_meta": 10,
        }
        raw_score = sum(weights[name] for name, passed in checks.items() if passed)
        # Transcript precedent can only predict relatability. A score above 85 is
        # reserved for audited retention/comment evidence from this exact script.
        score = min(85.0, float(raw_score))
        hard_requirements = (
            "cohort_decision_pass",
            "all_artifact_hashes_verified",
            "stated_source_claim_matches_cohort",
            "supported_by_three_transcripts",
            "opening_uses_recurring_human_language",
            "opening_human_experience_backed",
            "audience_facing_not_pipeline_meta",
        )
        hard_requirements_pass = all(checks[name] for name in hard_requirements)
        if not hard_requirements_pass:
            score = min(score, 69.0)
        passed = score >= 70 and hard_requirements_pass
        decision = "PASS_PREDICTED_RELATABILITY" if passed else "REJECT_NOT_RELATABLE"
        created_at = utc_now()
        script_hash = canonical_sha256({"script_id": script_id, "text": script_text})
        manifest_hash = canonical_sha256(manifest)
        findings = {
            "contract": "performance_transcript_script_audit_v1",
            "measurement_kind": "prediction_from_source_transcripts",
            "actual_audience_relatability_measured": False,
            "score_cap_without_post_publication_outcomes": 85,
            "checks": checks,
            "failures": [name for name, passed in checks.items() if not passed],
            "hard_requirements": list(hard_requirements),
            "cohort": {
                "cohort_id": manifest.get("cohort_id"),
                "member_count": aggregate.get("member_count"),
                "creator_count": aggregate.get("creator_count"),
                "total_views": aggregate.get("total_views"),
            },
            "source_claim_in_script": source_claim,
            "script_vocabulary_overlap": round(script_overlap, 6),
            "per_source_tfidf_cosine": [round(value, 6) for value in cosine_scores],
            "supported_source_count": supported_sources,
            "recurring_opening_terms": recurring_opening_terms,
            "recurring_human_experience_opening_terms": recurring_human_opening_terms,
            "pipeline_meta_phrases_in_script": pipeline_meta_matches,
            "artifact_integrity_failures": integrity_failures,
            "audited_at": created_at,
        }
        audit_input = {
            "script_id": script_id,
            "script_sha256": script_hash,
            "cohort_id": manifest.get("cohort_id"),
            "cohort_manifest_sha256": manifest_hash,
            "decision": decision,
            "score": score,
            "findings": findings,
        }
        audit_id = f"rel_audit_{canonical_sha256(audit_input)[:24]}"
        receipt = {"audit_id": audit_id, **audit_input, "created_at": created_at}
        receipt_root = self.storage_root / "script-audits"
        receipt_root.mkdir(parents=True, exist_ok=True)
        receipt_path = receipt_root / f"{audit_id}.json"
        receipt["receipt_path"] = str(receipt_path)
        atomic_write_json(receipt_path, receipt)
        with closing(self.connect()) as connection:
            connection.execute(
                """
                INSERT INTO mt_script_relatability_audits(
                    audit_id, script_id, cohort_id, decision, score, script_sha256,
                    cohort_manifest_sha256, findings_json, receipt_path, created_at
                ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(audit_id) DO NOTHING
                """,
                (
                    audit_id, script_id, str(manifest.get("cohort_id") or ""), decision,
                    score, script_hash, manifest_hash, json.dumps(findings, sort_keys=True),
                    str(receipt_path), created_at,
                ),
            )
            connection.commit()
        return receipt


def _significant_tokens(text: str) -> list[str]:
    return [
        token.lower()
        for token in words(text)
        if len(token) >= 3 and token.lower() not in STOP_WORDS
    ]


def _tfidf_cosine(
    left: Sequence[str],
    right: Sequence[str],
    corpus: Sequence[Sequence[str]],
) -> float:
    if not left or not right:
        return 0.0
    documents = [set(document) for document in corpus]
    vocabulary = set(left) | set(right)
    document_count = max(1, len(documents))

    def vector(tokens: Sequence[str]) -> dict[str, float]:
        counts: dict[str, int] = {}
        for token in tokens:
            counts[token] = counts.get(token, 0) + 1
        total = max(1, len(tokens))
        return {
            token: (count / total) * (
                math.log((1 + document_count) / (1 + sum(token in doc for doc in documents))) + 1
            )
            for token, count in counts.items()
        }

    left_vector = vector(left)
    right_vector = vector(right)
    numerator = sum(left_vector.get(token, 0.0) * right_vector.get(token, 0.0) for token in vocabulary)
    left_norm = math.sqrt(sum(value * value for value in left_vector.values()))
    right_norm = math.sqrt(sum(value * value for value in right_vector.values()))
    return numerator / (left_norm * right_norm) if left_norm and right_norm else 0.0


def _extract_source_claim(text: str) -> dict[str, int] | None:
    match = re.search(
        r"reviewed\s+([\d,]+)\s+source transcript patterns?\s+with\s+([\d,]+)\s+observed views",
        text,
        re.IGNORECASE,
    )
    if not match:
        return None
    return {
        "member_count": int(match.group(1).replace(",", "")),
        "total_views": int(match.group(2).replace(",", "")),
    }


def load_whisper_model(model_name: str) -> Any:
    try:
        import whisper
    except ImportError as exc:  # pragma: no cover - environment diagnostic
        raise RuntimeError("openai-whisper is required for local transcription") from exc
    return whisper.load_model(model_name)


def transcribe_cohort(
    *,
    tape_path: str | Path,
    storage_root: str | Path,
    topic: str,
    external_ids: Sequence[str],
    platforms: Sequence[str],
    limit: int,
    model_name: str,
    minimum_topic_matches: int = 1,
    force: bool = False,
    cookies_from_browser: str | None = None,
    target_language: str = "en",
) -> dict[str, Any]:
    bank = TranscriptBank(tape_path, storage_root)
    candidates = bank.select_candidates(
        topic=topic,
        limit=limit,
        external_ids=external_ids,
        platforms=platforms,
        minimum_topic_matches=minimum_topic_matches,
    )
    if not candidates:
        return {
            "decision": "INSUFFICIENT_EVIDENCE",
            "reason": "no candidates passed topic, performance, duration, and engagement floors",
            "candidate_count": 0,
        }
    run_id = f"transcript_cohort_run_{uuid.uuid4().hex}"
    acquisition_required = force or any(
        bank.latest_artifact(candidate.video_id, model_name) is None
        for candidate in candidates
    )
    model: Any = None
    runtime_failure: dict[str, str] | None = None
    if acquisition_required:
        try:
            executable_version("yt-dlp")
            executable_version("ffmpeg")
            model = load_whisper_model(model_name)
            validate_transcription_runtime(model_name)
        except Exception as exc:
            runtime_failure = {
                "phase": "cohort_runtime_preflight",
                "error_type": type(exc).__name__,
                "error": str(exc)[:500],
            }
    artifacts: list[dict[str, Any]] = []
    attempts: list[dict[str, Any]] = []
    claim_receipts: list[dict[str, Any]] = []
    failures: list[dict[str, Any]] = []
    audit_failures: list[dict[str, Any]] = []
    for candidate in candidates:
        existing = bank.latest_artifact(candidate.video_id, model_name)
        if existing is not None and not force:
            try:
                artifacts.append(bank.transcribe(
                    candidate,
                    model=None,
                    model_name=model_name,
                    force=False,
                    cookies_from_browser=cookies_from_browser,
                ))
            except Exception as exc:
                failures.append({
                    "phase": "existing_artifact_reaudit",
                    "video_id": candidate.video_id,
                    "error_type": type(exc).__name__,
                    "error": str(exc)[:500],
                })
            continue
        if runtime_failure is not None:
            continue
        claim = bank.claim_candidate(
            run_id=run_id,
            candidate=candidate,
            allow_existing_artifact=force,
        )
        claim_receipts.append(claim)
        if not claim["admitted"]:
            failures.append({
                "phase": "claim_admission",
                "video_id": candidate.video_id,
                "error_type": "ClaimRejected",
                "error": claim["reason"],
                "claim_receipt_sha256": claim["receipt_sha256"],
            })
            continue
        attempt_started_at = utc_now()
        try:
            artifact = bank.transcribe(
                candidate,
                model=model,
                model_name=model_name,
                force=force,
                cookies_from_browser=cookies_from_browser,
                persist=False,
            )
        except Exception as exc:
            try:
                attempt = bank.record_acquisition_attempt(
                    run_id=run_id,
                    candidate=candidate,
                    model_name=model_name,
                    outcome="failure",
                    started_at=attempt_started_at,
                    finished_at=utc_now(),
                    error_type=type(exc).__name__,
                    error=str(exc),
                    claim_id=claim["claim_id"],
                )
                attempts.append(attempt)
            except Exception as audit_exc:
                audit_failure = {
                    "phase": "cohort_failure_audit_persistence",
                    "video_id": candidate.video_id,
                    "error_type": type(audit_exc).__name__,
                    "error": str(audit_exc)[:500],
                    "claim_release_error": "",
                }
                try:
                    bank.release_claim(
                        run_id=run_id,
                        claim_id=claim["claim_id"],
                        reason="cohort_failure_audit_persistence_error",
                    )
                except Exception as release_exc:
                    audit_failure["claim_release_error"] = (
                        f"{type(release_exc).__name__}: {release_exc}"
                    )[:500]
                audit_failures.append(audit_failure)
            failures.append({
                "video_id": candidate.video_id,
                "error_type": type(exc).__name__,
                "error": str(exc)[:500],
            })
            continue
        transcript_text = str(artifact.pop("_transcript_text"))
        try:
            attempt = bank.persist_successful_acquisition(
                artifact=artifact,
                transcript_text=transcript_text,
                run_id=run_id,
                candidate=candidate,
                model_name=model_name,
                started_at=attempt_started_at,
                finished_at=utc_now(),
                claim_id=claim["claim_id"],
            )
            attempts.append(attempt)
            artifacts.append(artifact)
        except Exception as exc:
            audit_failure = {
                "phase": "cohort_atomic_success_persistence",
                "video_id": candidate.video_id,
                "transcript_id": artifact["transcript_id"],
                "error_type": type(exc).__name__,
                "error": str(exc)[:500],
                "claim_release_error": "",
            }
            try:
                bank.release_claim(
                    run_id=run_id,
                    claim_id=claim["claim_id"],
                    reason="cohort_success_audit_persistence_error",
                )
            except Exception as release_exc:
                audit_failure["claim_release_error"] = (
                    f"{type(release_exc).__name__}: {release_exc}"
                )[:500]
            audit_failures.append(audit_failure)
    cohort = bank.build_cohort(
        topic=topic,
        artifacts=artifacts,
        target_language=target_language,
    )
    return {
        "decision": cohort["decision"],
        "run_id": run_id,
        "candidate_count": len(candidates),
        "transcribed_count": len(artifacts),
        "failure_count": len(failures),
        "failures": failures,
        "runtime_failure": runtime_failure,
        "claim_receipts": claim_receipts,
        "attempts": attempts,
        "audit_failures": audit_failures,
        "cohort": cohort,
    }
