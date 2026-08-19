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
from datetime import datetime, timezone
from pathlib import Path
from statistics import median
from typing import Any, Iterable, Sequence


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


def executable_version(command: str) -> str:
    executable = shutil.which(command)
    if not executable:
        raise RuntimeError(f"required executable is unavailable: {command}")
    result = subprocess.run(
        [executable, "--version"],
        check=True,
        capture_output=True,
        text=True,
        timeout=30,
    )
    return (result.stdout or result.stderr).splitlines()[0].strip()


@dataclass(frozen=True)
class PerformancePolicy:
    minimum_views: int
    minimum_engagement_rate: float
    maximum_duration_seconds: float = 300.0
    minimum_transcript_words: int = 40


DEFAULT_POLICIES = {
    "youtube": PerformancePolicy(10_000, 0.005, maximum_duration_seconds=600.0),
    "tiktok": PerformancePolicy(100_000, 0.02),
    "instagram": PerformancePolicy(50_000, 0.015),
    "facebook": PerformancePolicy(25_000, 0.01),
}


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
                """
            )
            connection.commit()

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
                SELECT o.*
                FROM mt_market_observations o
                JOIN (
                    SELECT video_id, MAX(observed_at) AS observed_at
                    FROM mt_market_observations
                    GROUP BY video_id
                ) pick
                  ON pick.video_id=o.video_id AND pick.observed_at=o.observed_at
            )
            SELECT v.video_id, v.platform, v.external_id, v.creator_id,
                   v.title, v.caption, v.description, v.url, v.duration_seconds,
                   o.observation_key, o.observed_at, o.views, o.likes, o.comments,
                   o.shares, o.saves, o.relative_strength
            FROM mt_videos v
            JOIN latest o ON o.video_id=v.video_id
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
            if not candidate.source_url:
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
    ) -> list[Candidate]:
        """Select highest-performing untranscribed videos for a resumable bank fill."""

        platform_values = tuple(dict.fromkeys(value.lower() for value in platforms))
        marks = ",".join("?" for _ in platform_values)
        query = f"""
            WITH latest AS (
                SELECT o.*
                FROM mt_market_observations o
                JOIN (
                    SELECT video_id, MAX(observed_at) AS observed_at
                    FROM mt_market_observations GROUP BY video_id
                ) pick ON pick.video_id=o.video_id AND pick.observed_at=o.observed_at
            )
            SELECT v.video_id, v.platform, v.external_id, v.creator_id,
                   v.title, v.caption, v.description, v.url, v.duration_seconds,
                   o.observation_key, o.observed_at, o.views, o.likes, o.comments,
                   o.shares, o.saves, o.relative_strength
            FROM mt_videos v
            JOIN latest o ON o.video_id=v.video_id
            LEFT JOIN mt_transcript_artifacts artifact ON artifact.video_id=v.video_id
            WHERE v.platform IN ({marks}) AND artifact.video_id IS NULL
            ORDER BY o.views DESC, o.relative_strength DESC
            LIMIT ?
        """
        with closing(self.connect()) as connection:
            rows = connection.execute(query, (*platform_values, max(limit * 20, 500))).fetchall()

        filter_terms = tuple(topic_terms(topic))
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
                or not row["url"]
            ):
                continue
            metadata_text = " ".join(
                str(row[field] or "") for field in ("title", "caption", "description")
            )
            metadata_vocabulary = {token.lower() for token in words(metadata_text)}
            filter_matches = tuple(term for term in filter_terms if term in metadata_vocabulary)
            if filter_terms and len(filter_matches) < 2:
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
                topic_matches=filter_matches or terms,
            ))
            if len(candidates) >= limit:
                break
        return candidates

    def run_backfill(
        self,
        *,
        limit: int,
        platforms: Sequence[str],
        model_name: str,
        topic: str = "",
        cookies_from_browser: str | None = None,
    ) -> dict[str, Any]:
        started_at = utc_now()
        run_id = f"transcript_run_{uuid.uuid4().hex}"
        candidates = self.select_backfill_candidates(
            limit=limit, platforms=platforms, topic=topic
        )
        model = load_whisper_model(model_name) if candidates else None
        artifacts: list[dict[str, Any]] = []
        failures: list[dict[str, str]] = []
        for candidate in candidates:
            try:
                artifact = self.transcribe(
                    candidate,
                    model=model,
                    model_name=model_name,
                    cookies_from_browser=cookies_from_browser,
                )
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
                })
            except (OSError, RuntimeError, subprocess.SubprocessError) as exc:
                failures.append({
                    "video_id": candidate.video_id,
                    "external_id": candidate.external_id,
                    "error_type": type(exc).__name__,
                    "error": str(exc)[:500],
                })
        finished_at = utc_now()
        status = "completed" if not failures else ("partial" if artifacts else "failed")
        summary = {
            "run_id": run_id,
            "status": status,
            "policy": {
                "contract": "performance_ranked_resumable_whisper_backfill_v1",
                "limit": limit,
                "platforms": list(platforms),
                "model": model_name,
                "topic": topic,
                "platform_policies": {
                    platform: asdict(DEFAULT_POLICIES[platform])
                    for platform in platforms if platform in DEFAULT_POLICIES
                },
            },
            "candidate_count": len(candidates),
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
            "failure_count": len(failures),
            "failures": failures,
            "started_at": started_at,
            "finished_at": finished_at,
        }
        manifest_root = self.storage_root / "runs"
        manifest_root.mkdir(parents=True, exist_ok=True)
        manifest_path = manifest_root / f"{run_id}.json"
        summary["manifest_path"] = str(manifest_path)
        manifest_path.write_text(
            json.dumps(summary, indent=2, ensure_ascii=False, sort_keys=True) + "\n",
            encoding="utf-8",
        )
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
                    json.dumps(failures, sort_keys=True), str(manifest_path), started_at, finished_at,
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
    ) -> dict[str, Any]:
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
        audio_template = video_root / "source.%(ext)s"
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
        audio_files = sorted(
            path for path in video_root.glob("source.*")
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
            "schema_version": 1,
            "video_id": candidate.video_id,
            "platform": candidate.platform,
            "external_id": candidate.external_id,
            "source_url": candidate.source_url,
            "source_observation": candidate.source_metrics,
            "audio_sha256": audio_hash,
            "whisper_model": model_name,
            "language": str(result.get("language") or "unknown"),
            "text": transcript_text,
            "segments": segments,
        }
        transcript_hash = canonical_sha256(transcript_payload)
        transcript_id = f"whisper_{transcript_hash[:24]}"
        transcript_path = video_root / f"{transcript_id}.json"
        transcript_path.write_text(
            json.dumps(transcript_payload, indent=2, ensure_ascii=False, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        word_count = len(words(transcript_text))
        created_at = utc_now()
        acquisition = {
            "tool": "yt-dlp",
            "tool_version": executable_version("yt-dlp"),
            "command_contract": "audio_only_public_source_v1",
            "stdout_tail": completed.stdout.splitlines()[-5:],
        }
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
        self._persist_artifact(artifact, transcript_text)
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
            "contract": "performance_bound_whisper_transcript_v3",
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
                    duration_seconds=COALESCE(excluded.duration_seconds, mt_content_genomes.duration_seconds),
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
            connection.commit()

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
        performance_passing = [
            item for item in artifacts
            if item.get("audit", {}).get("decision") == "PASS"
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
        manifest_path.write_text(
            json.dumps(manifest, indent=2, ensure_ascii=False, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        with closing(self.connect()) as connection:
            connection.execute(
                """
                INSERT OR REPLACE INTO mt_transcript_cohorts(
                    cohort_id, topic, decision, member_ids_json, policy_json,
                    aggregate_metrics_json, audit_json, manifest_path, created_at
                ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?)
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
        receipt_path.write_text(
            json.dumps(receipt, indent=2, ensure_ascii=False, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        with closing(self.connect()) as connection:
            connection.execute(
                """
                INSERT OR REPLACE INTO mt_script_relatability_audits(
                    audit_id, script_id, cohort_id, decision, score, script_sha256,
                    cohort_manifest_sha256, findings_json, receipt_path, created_at
                ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
    model = load_whisper_model(model_name)
    artifacts: list[dict[str, Any]] = []
    failures: list[dict[str, str]] = []
    for candidate in candidates:
        try:
            artifacts.append(bank.transcribe(
                candidate,
                model=model,
                model_name=model_name,
                force=force,
                cookies_from_browser=cookies_from_browser,
            ))
        except (OSError, RuntimeError, subprocess.SubprocessError) as exc:
            failures.append({
                "video_id": candidate.video_id,
                "error_type": type(exc).__name__,
                "error": str(exc)[:500],
            })
    cohort = bank.build_cohort(
        topic=topic,
        artifacts=artifacts,
        target_language=target_language,
    )
    return {
        "decision": cohort["decision"],
        "candidate_count": len(candidates),
        "transcribed_count": len(artifacts),
        "failure_count": len(failures),
        "failures": failures,
        "cohort": cohort,
    }
