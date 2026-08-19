from __future__ import annotations

import hashlib
import json
import math
import os
import re
import sqlite3
import subprocess
from contextlib import closing
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Sequence


UTC = timezone.utc
WORD_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9'’-]*")
SENTENCE_RE = re.compile(r"(?<=[.!?])\s+")
STOP_WORDS = {
    "about", "after", "again", "also", "because", "been", "before", "being",
    "could", "does", "doing", "from", "have", "here", "into", "just", "more",
    "most", "only", "other", "over", "should", "some", "than", "that", "their",
    "them", "then", "there", "these", "they", "this", "those", "through", "very",
    "want", "what", "when", "where", "which", "while", "with", "would", "your",
}
JARGON = {
    "api", "automation", "backend", "conversion", "crm", "framework", "frontend",
    "integration", "kpi", "llm", "optimization", "pipeline", "platform", "saas",
    "sdk", "stack", "system", "workflow",
}
PROOF_WORDS = {
    "because", "data", "evidence", "measured", "proof", "receipt", "result",
    "tested", "views", "watched",
}
CHANGE_WORDS = {
    "but", "except", "here's", "instead", "look", "now", "proof", "so", "then",
    "watch", "yet",
}
HUMAN_EXPERIENCE_WORDS = {
    "alone", "anxious", "anxiety", "burned", "burnout", "burnt", "care",
    "exhausted", "fear", "feel", "feeling", "frustrated", "hard", "hate",
    "hopeless", "overwhelmed", "pressure", "quit", "struggle", "struggling",
    "stuck", "tired", "trying", "worry", "worse",
}


def utc_now() -> str:
    return datetime.now(UTC).isoformat()


def stable_id(prefix: str, *parts: Any) -> str:
    raw = "|".join(json.dumps(part, sort_keys=True, default=str) for part in parts)
    return f"{prefix}_{hashlib.sha256(raw.encode('utf-8')).hexdigest()[:20]}"


def words(text: str) -> list[str]:
    return WORD_RE.findall(text or "")


def clamp(value: float, low: float = 0.0, high: float = 100.0) -> float:
    return max(low, min(high, value))


class QualityStore:
    def __init__(self, path: str | Path):
        self.path = Path(path).expanduser()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    def connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=15)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA foreign_keys=ON")
        return connection

    def _initialize(self) -> None:
        with closing(self.connect()) as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS cq_receipts (
                    receipt_id TEXT PRIMARY KEY,
                    receipt_type TEXT NOT NULL,
                    source_type TEXT NOT NULL,
                    source_id TEXT,
                    source_url TEXT,
                    payload_json TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS cq_scripts (
                    script_id TEXT PRIMARY KEY,
                    topic TEXT NOT NULL,
                    objective TEXT NOT NULL,
                    source_receipts_json TEXT NOT NULL,
                    script_json TEXT NOT NULL,
                    status TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS cq_audits (
                    audit_id TEXT PRIMARY KEY,
                    audit_type TEXT NOT NULL,
                    subject_id TEXT,
                    decision TEXT NOT NULL,
                    score REAL NOT NULL,
                    findings_json TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS cq_retention (
                    receipt_id TEXT PRIMARY KEY,
                    platform TEXT NOT NULL,
                    source_id TEXT,
                    payload_json TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_cq_receipts_type_created
                    ON cq_receipts(receipt_type, created_at DESC);
                CREATE INDEX IF NOT EXISTS idx_cq_audits_type_created
                    ON cq_audits(audit_type, created_at DESC);
                """
            )
            connection.commit()

    def put_receipt(
        self,
        receipt_type: str,
        source_type: str,
        source_id: str | None,
        source_url: str | None,
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        receipt_id = stable_id("rcpt", receipt_type, source_type, source_id, payload)
        created_at = utc_now()
        with closing(self.connect()) as connection:
            connection.execute(
                """
                INSERT INTO cq_receipts
                    (receipt_id, receipt_type, source_type, source_id, source_url, payload_json, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(receipt_id) DO UPDATE SET
                    source_url=excluded.source_url,
                    payload_json=excluded.payload_json
                """,
                (
                    receipt_id,
                    receipt_type,
                    source_type,
                    source_id,
                    source_url,
                    json.dumps(payload, sort_keys=True),
                    created_at,
                ),
            )
            connection.commit()
        return {
            "receipt_id": receipt_id,
            "receipt_type": receipt_type,
            "source_type": source_type,
            "source_id": source_id,
            "source_url": source_url,
            "payload": payload,
            "created_at": created_at,
        }

    def receipts(self, receipt_ids: Sequence[str] | None = None, limit: int = 50) -> list[dict[str, Any]]:
        with closing(self.connect()) as connection:
            if receipt_ids:
                marks = ",".join("?" for _ in receipt_ids)
                rows = connection.execute(
                    f"SELECT * FROM cq_receipts WHERE receipt_id IN ({marks}) ORDER BY created_at DESC",
                    tuple(receipt_ids),
                ).fetchall()
            else:
                rows = connection.execute(
                    "SELECT * FROM cq_receipts ORDER BY created_at DESC LIMIT ?", (max(1, min(limit, 500)),)
                ).fetchall()
        return [self._receipt_row(row) for row in rows]

    @staticmethod
    def _receipt_row(row: sqlite3.Row) -> dict[str, Any]:
        return {
            "receipt_id": row["receipt_id"],
            "receipt_type": row["receipt_type"],
            "source_type": row["source_type"],
            "source_id": row["source_id"],
            "source_url": row["source_url"],
            "payload": json.loads(row["payload_json"]),
            "created_at": row["created_at"],
        }

    def put_script(self, script: dict[str, Any]) -> None:
        with closing(self.connect()) as connection:
            connection.execute(
                """
                INSERT OR REPLACE INTO cq_scripts
                    (script_id, topic, objective, source_receipts_json, script_json, status, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    script["script_id"],
                    script["topic"],
                    script["objective"],
                    json.dumps(script["source_receipt_ids"], sort_keys=True),
                    json.dumps(script, sort_keys=True),
                    script["status"],
                    script["created_at"],
                ),
            )
            connection.commit()

    def script(self, script_id: str) -> dict[str, Any] | None:
        with closing(self.connect()) as connection:
            row = connection.execute(
                "SELECT script_json FROM cq_scripts WHERE script_id=?", (script_id,)
            ).fetchone()
        return json.loads(row["script_json"]) if row else None

    def scripts(self, limit: int = 50) -> list[dict[str, Any]]:
        with closing(self.connect()) as connection:
            rows = connection.execute(
                "SELECT script_json FROM cq_scripts ORDER BY created_at DESC LIMIT ?",
                (max(1, min(limit, 500)),),
            ).fetchall()
        return [json.loads(row["script_json"]) for row in rows]

    def script_gate_summary(self, script_id: str) -> dict[str, Any]:
        with closing(self.connect()) as connection:
            rows = connection.execute(
                """
                SELECT audit_type, decision, score, audit_id, created_at
                FROM cq_audits WHERE subject_id=? ORDER BY created_at DESC
                """,
                (script_id,),
            ).fetchall()
        latest: dict[str, dict[str, Any]] = {}
        for row in rows:
            if row["audit_type"] not in latest:
                latest[row["audit_type"]] = dict(row)
        required = {
            "relatability_script": "PASS",
            "attention_script": "PASS",
            "attention_video_preflight": "PASS",
        }
        return {
            "ready_for_render": all(latest.get(kind, {}).get("decision") == decision for kind, decision in required.items()),
            "required_decisions": required,
            "latest_audits": latest,
        }

    def put_audit(
        self,
        audit_type: str,
        subject_id: str | None,
        decision: str,
        score: float,
        findings: dict[str, Any],
    ) -> dict[str, Any]:
        audit_id = stable_id("audit", audit_type, subject_id, decision, score, findings)
        created_at = utc_now()
        with closing(self.connect()) as connection:
            connection.execute(
                """
                INSERT OR REPLACE INTO cq_audits
                    (audit_id, audit_type, subject_id, decision, score, findings_json, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (audit_id, audit_type, subject_id, decision, score, json.dumps(findings, sort_keys=True), created_at),
            )
            connection.commit()
        return {
            "audit_id": audit_id,
            "audit_type": audit_type,
            "subject_id": subject_id,
            "decision": decision,
            "score": round(score, 1),
            "findings": findings,
            "created_at": created_at,
        }

    def counts(self) -> dict[str, int]:
        with closing(self.connect()) as connection:
            return {
                table: int(connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
                for table in ("cq_receipts", "cq_scripts", "cq_audits", "cq_retention")
            }


class MarketTapeReader:
    def __init__(self, path: str | Path):
        self.path = Path(path).expanduser()

    def health(self) -> dict[str, Any]:
        if not self.path.exists():
            return {"status": "down", "path": str(self.path), "error": "market_tape_database_missing"}
        try:
            with closing(self.connect()) as connection:
                videos = connection.execute("SELECT COUNT(*) FROM mt_videos").fetchone()[0]
                observations = connection.execute("SELECT COUNT(*) FROM mt_market_observations").fetchone()[0]
                transcripts = connection.execute(
                    "SELECT COUNT(*) FROM mt_content_genomes WHERE length(trim(COALESCE(transcript, ''))) > 0"
                ).fetchone()[0]
            return {
                "status": "up",
                "path": str(self.path),
                "videos": int(videos),
                "observations": int(observations),
                "transcripts": int(transcripts),
            }
        except (sqlite3.Error, OSError) as exc:
            return {"status": "down", "path": str(self.path), "error": str(exc)}

    def connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(f"file:{self.path}?mode=ro", uri=True, timeout=15)
        connection.row_factory = sqlite3.Row
        return connection

    def candidates(self, topic: str, limit: int = 20) -> list[dict[str, Any]]:
        tokens = [
            token.lower() for token in words(topic)
            if (len(token) > 2 or token.lower() == "ai") and token.lower() not in STOP_WORDS
        ][:6]
        where = ""
        params: list[Any] = []
        if tokens:
            clauses = []
            for token in tokens:
                clauses.append(
                    "lower(COALESCE(v.title,'') || ' ' || COALESCE(v.caption,'') || ' ' || "
                    "COALESCE(v.description,'') || ' ' || COALESCE(g.transcript,'')) LIKE ?"
                )
                params.append(f"%{token}%")
            where = "WHERE (" + " OR ".join(clauses) + ")"
        # Pull a wider ranked window, then enforce multi-token topic relevance in Python.
        # A raw OR query let globally strong but unrelated videos through on words such as
        # "for"; those could become persisted evidence receipts. Stop words are excluded
        # above and multi-word topics must match at least two meaningful terms.
        params.append(max(1, min(limit * 5, 500)))
        query = f"""
            WITH latest AS (
                SELECT o.*
                FROM mt_market_observations o
                JOIN (
                    SELECT video_id, MAX(observed_at) AS observed_at
                    FROM mt_market_observations GROUP BY video_id
                ) pick ON pick.video_id=o.video_id AND pick.observed_at=o.observed_at
            )
            SELECT v.video_id, v.platform, v.external_id, v.creator_id, v.title, v.caption,
                   v.description, v.url, v.duration_seconds, v.first_seen_at,
                   COALESCE(g.transcript, '') AS transcript,
                   COALESCE(g.opening_words, '') AS opening_words,
                   COALESCE(g.hook_type, '') AS hook_type,
                   COALESCE(o.views, 0) AS views, COALESCE(o.likes, 0) AS likes,
                   COALESCE(o.comments, 0) AS comments, COALESCE(o.shares, 0) AS shares,
                   COALESCE(o.view_velocity, 0) AS velocity,
                   COALESCE(o.view_acceleration, 0) AS acceleration,
                   COALESCE(o.relative_strength, 0) AS relative_strength,
                   o.observation_key, o.observed_at
            FROM mt_videos v
            LEFT JOIN mt_content_genomes g ON g.video_id=v.video_id
            LEFT JOIN latest o ON o.video_id=v.video_id
            {where}
            ORDER BY CASE WHEN length(trim(COALESCE(g.transcript, ''))) > 0 THEN 0 ELSE 1 END,
                     COALESCE(o.relative_strength, 0) DESC,
                     COALESCE(o.view_velocity, 0) DESC,
                     COALESCE(o.views, 0) DESC
            LIMIT ?
        """
        with closing(self.connect()) as connection:
            rows = connection.execute(query, params).fetchall()
        result = [dict(row) for row in rows]
        if tokens:
            minimum_matches = 1 if len(tokens) == 1 else 2
            qualified = []
            for row in result:
                source_words = {
                    token.lower() for token in words(" ".join(
                        str(row.get(field) or "")
                        for field in ("title", "caption", "description", "transcript")
                    ))
                }
                match_count = sum(token in source_words for token in tokens)
                if match_count >= minimum_matches:
                    row["topic_match_count"] = match_count
                    qualified.append(row)
            result = qualified
        return result[:limit]

    def transcript_artifact(self, video_id: str) -> dict[str, Any] | None:
        """Return the latest local Whisper artifact for a video, if one exists."""

        try:
            with closing(self.connect()) as connection:
                row = connection.execute(
                    """
                    SELECT * FROM mt_transcript_artifacts
                    WHERE video_id=? ORDER BY created_at DESC LIMIT 1
                    """,
                    (video_id,),
                ).fetchone()
        except sqlite3.Error:
            return None
        if not row:
            return None
        result = dict(row)
        for source, target in (
            ("source_metrics_json", "source_metrics"),
            ("acquisition_json", "acquisition"),
            ("audit_json", "audit"),
        ):
            result[target] = json.loads(result.pop(source))
        return result


@dataclass
class TranscriptDocument:
    text: str
    segments: list[dict[str, Any]]
    source: str


class ViralTranscriptService:
    def __init__(self, tape: MarketTapeReader, store: QualityStore):
        self.tape = tape
        self.store = store

    @staticmethod
    def _fetch_youtube(external_id: str) -> TranscriptDocument:
        from youtube_transcript_api import YouTubeTranscriptApi

        fetched = YouTubeTranscriptApi().fetch(external_id, languages=("en", "en-US", "en-GB"))
        raw = fetched.to_raw_data() if hasattr(fetched, "to_raw_data") else list(fetched)
        segments: list[dict[str, Any]] = []
        for item in raw:
            if isinstance(item, dict):
                text = str(item.get("text") or "").strip()
                start = float(item.get("start") or 0.0)
                duration = float(item.get("duration") or 0.0)
            else:
                text = str(getattr(item, "text", "")).strip()
                start = float(getattr(item, "start", 0.0))
                duration = float(getattr(item, "duration", 0.0))
            if text:
                segments.append({"text": text, "start": start, "duration": duration})
        return TranscriptDocument(
            text=" ".join(item["text"] for item in segments),
            segments=segments,
            source="youtube_transcript_api",
        )

    @staticmethod
    def _pattern(document: TranscriptDocument, row: dict[str, Any]) -> dict[str, Any]:
        transcript_words = words(document.text)
        lowered = [word.lower() for word in transcript_words]
        duration = float(row.get("duration_seconds") or 0.0)
        if not duration and document.segments:
            last = document.segments[-1]
            duration = float(last["start"]) + float(last["duration"])
        proof_positions = [index for index, word in enumerate(lowered) if word in PROOF_WORDS]
        change_positions = [index for index, word in enumerate(lowered) if word in CHANGE_WORDS]
        first_proof_seconds = None
        if proof_positions and transcript_words and duration:
            first_proof_seconds = round(duration * proof_positions[0] / len(transcript_words), 1)
        hook_text = " ".join(transcript_words[:28])
        return {
            "opening_word_count": min(28, len(transcript_words)),
            "opening_shape": classify_hook(hook_text),
            "proof_marker_count": len(proof_positions),
            "first_proof_seconds": first_proof_seconds,
            "pattern_interrupt_count": len(change_positions),
            "estimated_words_per_second": round(len(transcript_words) / duration, 2) if duration else None,
            "duration_seconds": round(duration, 1) if duration else None,
            "structure": infer_structure(document.text),
            "source_metrics": {
                "views": int(row.get("views") or 0),
                "likes": int(row.get("likes") or 0),
                "comments": int(row.get("comments") or 0),
                "shares": int(row.get("shares") or 0),
                "velocity": float(row.get("velocity") or 0.0),
                "relative_strength": float(row.get("relative_strength") or 0.0),
                "observed_at": row.get("observed_at"),
            },
            "transcript_sha256": hashlib.sha256(document.text.encode("utf-8")).hexdigest(),
            "transcript_word_count": len(transcript_words),
        }

    def discover(self, topic: str, limit: int = 5) -> dict[str, Any]:
        if not topic.strip():
            raise ValueError("topic is required")
        # Artifact coverage is intentionally sparse during backfill. Search a wide
        # metadata window, then accept only exact local Whisper/audit bindings.
        rows = self.tape.candidates(topic, limit=max(limit * 50, 200))
        receipts: list[dict[str, Any]] = []
        failures: list[dict[str, str]] = []
        for row in rows:
            if len(receipts) >= limit:
                break
            artifact = self.tape.transcript_artifact(str(row["video_id"]))
            if not artifact:
                failures.append({
                    "source_id": str(row.get("external_id") or row["video_id"]),
                    "error": "local_whisper_artifact_missing",
                })
                continue
            artifact_audit = dict(artifact.get("audit") or {})
            if (
                artifact_audit.get("decision") != "PASS"
                or artifact_audit.get("contract") != "performance_bound_whisper_transcript_v3"
                or artifact.get("observation_key") != row.get("observation_key")
            ):
                failures.append({
                    "source_id": str(row.get("external_id") or row["video_id"]),
                    "error": "whisper_artifact_audit_or_observation_mismatch",
                })
                continue
            text = str(row.get("transcript") or "").strip()
            if not text:
                failures.append({
                    "source_id": str(row.get("external_id") or row["video_id"]),
                    "error": "associated_transcript_missing",
                })
                continue
            document = TranscriptDocument(text=text, segments=[], source="local_whisper")
            if len(words(document.text)) < 40:
                failures.append({"source_id": str(row.get("external_id") or row["video_id"]), "error": "transcript_too_short"})
                continue
            pattern = self._pattern(document, row)
            transcript_keywords = sorted({
                token.lower() for token in words(document.text)
                if len(token) >= 4 and token.lower() not in STOP_WORDS
            })[:300]
            payload = {
                "topic": topic,
                "platform": row.get("platform"),
                "title": row.get("title"),
                "creator_id": row.get("creator_id"),
                "transcript_source": document.source,
                "transcript_id": artifact.get("transcript_id"),
                "observation_key": artifact.get("observation_key"),
                "audio_sha256": artifact.get("audio_sha256"),
                "transcript_sha256": artifact.get("transcript_sha256"),
                "performance_qualification": {
                    "audit_contract": artifact_audit.get("contract"),
                    "audit_decision": artifact_audit.get("decision"),
                    "checks": artifact_audit.get("checks"),
                },
                "transcript_keywords": transcript_keywords,
                "pattern": pattern,
            }
            receipts.append(
                self.store.put_receipt(
                    "viral_transcript_pattern",
                    str(row.get("platform") or "unknown"),
                    str(row.get("external_id") or row["video_id"]),
                    row.get("url"),
                    payload,
                )
            )
        return {
            "status": "complete" if receipts else "no_qualified_transcripts",
            "topic": topic,
            "candidate_count": len(rows),
            "receipt_count": len(receipts),
            "receipts": receipts,
            "failures": failures[:20],
        }


def classify_hook(text: str) -> str:
    lowered = text.lower().strip()
    if "?" in text or lowered.startswith(("what", "why", "how", "do you", "have you")):
        return "question"
    if any(token in lowered for token in ("i tried", "i spent", "i made", "i lost", "i was")):
        return "personal_receipt"
    if any(token in lowered for token in ("stop", "don't", "never", "mistake", "wrong")):
        return "contrarian_warning"
    if any(char.isdigit() for char in text):
        return "specific_result"
    return "direct_claim"


def infer_structure(text: str) -> list[str]:
    lowered = text.lower()
    structure = ["hook"]
    if any(token in lowered for token in ("struggle", "problem", "mistake", "hard", "stuck")):
        structure.append("human_problem")
    if any(token in lowered for token in PROOF_WORDS):
        structure.append("evidence")
    if any(token in lowered for token in ("step", "first", "second", "here's how", "do this")):
        structure.append("method")
    if any(token in lowered for token in ("follow", "subscribe", "comment", "download", "try")):
        structure.append("call_to_action")
    if structure[-1] != "payoff":
        structure.insert(-1 if structure[-1] == "call_to_action" else len(structure), "payoff")
    return structure


class AudienceIntelligenceService:
    def __init__(self, tape: MarketTapeReader, store: QualityStore):
        self.tape = tape
        self.store = store

    def human_moments(self, topic: str, audience: str, limit: int = 8) -> dict[str, Any]:
        if not topic.strip() or not audience.strip():
            raise ValueError("topic and audience are required")
        candidates = self.tape.candidates(topic, limit=60)
        cues = (
            "anxious", "anxiety", "burned", "burnout", "burnt", "can't", "exhausted",
            "feel", "feeling", "hopeless", "overwhelmed", "pressure", "struggle",
            "struggling", "stuck", "tired", "worry",
        )
        moments: list[dict[str, Any]] = []
        for row in candidates:
            artifact = self.tape.transcript_artifact(str(row["video_id"]))
            if not artifact:
                continue
            audit = artifact.get("audit") or {}
            if (
                audit.get("decision") != "PASS"
                or audit.get("contract") != "performance_bound_whisper_transcript_v3"
                or artifact.get("observation_key") != row.get("observation_key")
                or not str(artifact.get("whisper_language") or "").lower().startswith("en")
            ):
                continue
            source = str(row.get("transcript") or "")
            if not source.strip():
                continue
            sentences = SENTENCE_RE.split(source)
            for sentence in sentences:
                clean = " ".join(words(sentence))
                if 7 <= len(words(clean)) <= 40 and any(cue in clean.lower() for cue in cues):
                    moment_id = stable_id("moment", row.get("video_id"), clean)
                    moments.append(
                        {
                            "moment_id": moment_id,
                            "situation": clean,
                            "audience": audience,
                            "source_video_id": row.get("video_id"),
                            "source_transcript_id": artifact.get("transcript_id"),
                            "source_url": row.get("url"),
                            "basis": "performance_qualified_local_whisper_transcript",
                        }
                    )
                    break
            if len(moments) >= limit:
                break
        receipt = self.store.put_receipt(
            "audience_human_moments",
            "market_tape",
            stable_id("audience", audience, topic),
            None,
            {
                "topic": topic,
                "audience": audience,
                "candidate_count": len(candidates),
                "moments": moments,
                "note": "Moments are extracted from observed source language; none are invented when evidence is absent.",
            },
        )
        return {
            "status": "complete" if moments else "insufficient_observed_human_moments",
            "topic": topic,
            "audience": audience,
            "moments": moments,
            "receipt": receipt,
        }


class ScriptService:
    def __init__(self, store: QualityStore):
        self.store = store

    def generate(self, payload: dict[str, Any]) -> dict[str, Any]:
        topic = str(payload.get("topic") or "").strip()
        audience = str(payload.get("audience") or "").strip()
        objective = str(payload.get("objective") or "qualified_attention").strip()
        claim = str(payload.get("claim") or "").strip()
        human = payload.get("human_moment") or {}
        situation = str(human.get("situation") or "").strip()
        stakes = str(human.get("stakes") or "").strip()
        receipt_ids = [str(item) for item in payload.get("receipt_ids") or []]
        proof = [str(item).strip() for item in payload.get("owned_proof") or [] if str(item).strip()]
        missing = [
            name
            for name, value in (("topic", topic), ("audience", audience), ("claim", claim), ("human_moment.situation", situation), ("human_moment.stakes", stakes))
            if not value
        ]
        if missing:
            raise ValueError("missing required evidence context: " + ", ".join(missing))
        if not receipt_ids:
            return {
                "status": "rejected",
                "code": "REJECT_NO_RECEIPTS",
                "reason": "At least one persisted source receipt is required before script generation.",
            }
        receipts = self.store.receipts(receipt_ids)
        found_ids = {item["receipt_id"] for item in receipts}
        unknown = sorted(set(receipt_ids) - found_ids)
        if unknown:
            return {"status": "rejected", "code": "REJECT_UNKNOWN_RECEIPTS", "unknown_receipt_ids": unknown}
        conversion_objectives = {"conversion", "purchase", "signup", "trial", "sale"}
        if objective.lower() in conversion_objectives and not proof:
            return {
                "status": "rejected",
                "code": "REJECT_CONVERSION_UNPROVEN",
                "reason": "A conversion objective requires owned proof supplied by the operator.",
            }

        pattern_receipts = [item for item in receipts if item["receipt_type"] == "viral_transcript_pattern"]
        verified_patterns = [
            item for item in pattern_receipts
            if item["payload"].get("transcript_source") == "local_whisper"
            and item["payload"].get("performance_qualification", {}).get("audit_decision") == "PASS"
            and item["payload"].get("performance_qualification", {}).get("audit_contract")
            == "performance_bound_whisper_transcript_v3"
            and item["payload"].get("observation_key")
            and len(str(item["payload"].get("audio_sha256") or "")) == 64
            and len(str(item["payload"].get("transcript_sha256") or "")) == 64
        ]
        if len(verified_patterns) < 5:
            return {
                "status": "rejected",
                "code": "REJECT_INSUFFICIENT_TRANSCRIPT_COHORT",
                "reason": "At least five performance-qualified local Whisper transcript receipts are required.",
                "verified_transcript_count": len(verified_patterns),
            }
        creators = {
            str(item["payload"].get("creator_id") or "")
            for item in verified_patterns
            if item["payload"].get("creator_id")
        }
        metrics = [item["payload"].get("pattern", {}).get("source_metrics", {}) for item in verified_patterns]
        observed_views = sum(int(item.get("views") or 0) for item in metrics)
        source_count = len(verified_patterns)
        if len(creators) < 3 or observed_views < 100_000:
            return {
                "status": "rejected",
                "code": "REJECT_INSUFFICIENT_TRANSCRIPT_COHORT",
                "reason": "The cohort requires at least three creators and 100,000 observed views.",
                "verified_transcript_count": source_count,
                "creator_count": len(creators),
                "observed_views_snapshot": observed_views,
            }
        human_term_groups = {
            "feel": "feeling stuck",
            "feeling": "feeling stuck",
            "hard": "the work getting harder",
            "tired": "exhaustion",
            "trying": "trying harder",
            "worse": "things getting worse",
        }
        human_term_sources: dict[str, set[str]] = {}
        for item in verified_patterns:
            source_terms = {
                str(token).lower()
                for token in item["payload"].get("transcript_keywords") or []
            }
            for term in HUMAN_EXPERIENCE_WORDS & source_terms:
                display = human_term_groups.get(term, term)
                human_term_sources.setdefault(display, set()).add(item["receipt_id"])
        recurring_human_terms = sorted(
            (term for term, sources in human_term_sources.items() if len(sources) >= 2),
            key=lambda term: (-len(human_term_sources[term]), term),
        )
        if not recurring_human_terms:
            return {
                "status": "rejected",
                "code": "REJECT_NO_RECURRING_HUMAN_LANGUAGE",
                "reason": "At least one human-experience term must recur across two transcripts.",
            }
        named_terms = recurring_human_terms[:4]
        if len(named_terms) == 1:
            term_phrase = named_terms[0]
        else:
            term_phrase = ", ".join(named_terms[:-1]) + f", and {named_terms[-1]}"
        proof_line = proof[0] if proof else (
            f"Across these stories, the same signs keep showing up: {term_phrase}."
        )
        hook_text = situation.strip()
        if hook_text[-1] not in ".?!":
            hook_text += "."
        timeline = [
            {"start": 0.0, "end": 3.0, "beat": "human_hook", "text": hook_text},
            {"start": 3.0, "end": 8.0, "beat": "stakes", "text": f"It matters because {stakes.rstrip('.')}."},
            {"start": 8.0, "end": 15.0, "beat": "claim", "text": claim.rstrip(".") + "."},
            {"start": 15.0, "end": 23.0, "beat": "proof", "text": proof_line.rstrip(".") + "."},
            {"start": 23.0, "end": 31.0, "beat": "method", "text": "Choose the smallest pressure you can remove today, then make the next step easier to begin."},
            {"start": 31.0, "end": 38.0, "beat": "payoff", "text": "The point is not to force momentum; it is to make the work feel possible again."},
            {"start": 38.0, "end": 43.0, "beat": "cta", "text": "Which part feels heaviest right now?"},
        ]
        full_text = " ".join(beat["text"] for beat in timeline)
        script_id = stable_id("script", topic, objective, receipt_ids, full_text)
        result = {
            "script_id": script_id,
            "status": "generated_pending_gates",
            "topic": topic,
            "audience": audience,
            "objective": objective,
            "source_receipt_ids": receipt_ids,
            "evidence_summary": {
                "viral_transcript_patterns": source_count,
                "creator_count": len(creators),
                "observed_views_snapshot": observed_views,
                "recurring_human_terms": recurring_human_terms,
                "owned_proof_count": len(proof),
            },
            "timeline": timeline,
            "text": full_text,
            "created_at": utc_now(),
        }
        self.store.put_script(result)
        return result


class RelatabilityService:
    def __init__(self, store: QualityStore):
        self.store = store

    def audit(self, payload: dict[str, Any]) -> dict[str, Any]:
        text = str(payload.get("text") or "").strip()
        timeline = payload.get("timeline") or []
        subject_id = str(payload.get("script_id") or stable_id("subject", text))
        if not text:
            raise ValueError("text is required")
        token_list = [item.lower() for item in words(text)]
        significant_tokens = {
            token for token in token_list if len(token) >= 3 and token not in STOP_WORDS
        }
        opening_tokens = token_list[:45]
        opening = " ".join(opening_tokens)
        first_sentence = SENTENCE_RE.split(text)[0].lower()
        receipt_ids = [str(item) for item in payload.get("source_receipt_ids") or []]
        receipts = self.store.receipts(receipt_ids) if receipt_ids else []
        patterns = [
            item for item in receipts
            if item["receipt_type"] == "viral_transcript_pattern"
        ]
        verified_patterns = [
            item for item in patterns
            if item["payload"].get("transcript_source") == "local_whisper"
            and item["payload"].get("performance_qualification", {}).get("audit_decision") == "PASS"
            and item["payload"].get("performance_qualification", {}).get("audit_contract")
            == "performance_bound_whisper_transcript_v3"
            and item["payload"].get("observation_key")
        ]
        creators = {
            str(item["payload"].get("creator_id") or "")
            for item in verified_patterns if item["payload"].get("creator_id")
        }
        observed_views = sum(
            int(item["payload"].get("pattern", {}).get("source_metrics", {}).get("views") or 0)
            for item in verified_patterns
        )
        keyword_sets = [
            {str(token).lower() for token in item["payload"].get("transcript_keywords") or []}
            for item in verified_patterns
        ]
        union_keywords = set().union(*keyword_sets) if keyword_sets else set()
        vocabulary_overlap = (
            len(significant_tokens & union_keywords) / len(significant_tokens)
            if significant_tokens else 0.0
        )
        supported_sources = sum(
            len(significant_tokens & source_keywords) >= 3 for source_keywords in keyword_sets
        )
        opening_human_terms = sorted(
            set(opening_tokens) & HUMAN_EXPERIENCE_WORDS & union_keywords
        )
        pipeline_meta_phrases = (
            "attention gate", "content factory", "human-relatability", "source receipt",
            "transcript pattern", "passes human", "passes attention", "reveal the mechanism",
            "spoken pattern", "test the structure", "recognize themselves",
        )
        pipeline_meta_matches = sorted(
            phrase for phrase in pipeline_meta_phrases if phrase in text.lower()
        )
        source_claim = re.search(
            r"reviewed\s+([\d,]+)\s+source transcript patterns?\s+with\s+([\d,]+)\s+observed views",
            text,
            re.IGNORECASE,
        )
        source_claim_matches = not source_claim or (
            int(source_claim.group(1).replace(",", "")) == len(verified_patterns)
            and int(source_claim.group(2).replace(",", "")) == observed_views
        )
        checks = {
            "performance_transcript_cohort": (
                len(verified_patterns) >= 5 and len(creators) >= 3 and observed_views >= 100_000
            ),
            "all_receipts_artifact_verified": len(verified_patterns) == len(patterns) and bool(patterns),
            "source_claim_matches_evidence": source_claim_matches,
            "human_experience_in_opening": bool(opening_human_terms),
            "audience_facing_not_pipeline_meta": not pipeline_meta_matches,
            "script_vocabulary_supported": vocabulary_overlap >= 0.18,
            "supported_by_three_transcripts": supported_sources >= 3,
            "concrete_stakes_present": any(token in text.lower() for token in ("because", "cost", "lose", "matters", "stuck", "waste", "without")),
            "audience_language_present": any(token in token_list for token in ("you", "your", "we", "i")),
            "not_product_first": not any(token in first_sentence for token in ("app", "platform", "product", "service", "software", "tool")),
            "not_jargon_dense_opening": sum(token in JARGON for token in words(opening)) <= 2,
            "timeline_has_human_hook": bool(timeline and timeline[0].get("beat") == "human_hook"),
        }
        failures = [name for name, passed in checks.items() if not passed]
        hard_requirements = (
            "performance_transcript_cohort",
            "all_receipts_artifact_verified",
            "source_claim_matches_evidence",
            "human_experience_in_opening",
            "audience_facing_not_pipeline_meta",
            "supported_by_three_transcripts",
        )
        score = min(85.0, 100.0 * sum(checks.values()) / len(checks))
        hard_requirements_pass = all(checks[name] for name in hard_requirements)
        if not hard_requirements_pass:
            score = min(score, 69.0)
        decision = (
            "PASS" if score >= 70 and hard_requirements_pass
            else "REJECT_NOT_RELATABLE"
        )
        return self.store.put_audit(
            "relatability_script",
            subject_id,
            decision,
            score,
            {
                "contract": "performance_transcript_script_gate_v1",
                "measurement_kind": "prediction_from_source_transcripts",
                "actual_audience_relatability_measured": False,
                "score_cap_without_post_publication_outcomes": 85,
                "checks": checks,
                "failures": failures,
                "hard_requirements": list(hard_requirements),
                "threshold": 70,
                "verified_transcript_count": len(verified_patterns),
                "creator_count": len(creators),
                "observed_views_snapshot": observed_views,
                "script_vocabulary_overlap": round(vocabulary_overlap, 6),
                "supported_source_count": supported_sources,
                "opening_human_terms": opening_human_terms,
                "pipeline_meta_phrases_in_script": pipeline_meta_matches,
            },
        )


class AttentionService:
    def __init__(self, store: QualityStore):
        self.store = store

    def script_audit(self, payload: dict[str, Any]) -> dict[str, Any]:
        text = str(payload.get("text") or "").strip()
        timeline = payload.get("timeline") or []
        subject_id = str(payload.get("script_id") or stable_id("subject", text))
        if not text or not timeline:
            raise ValueError("text and timeline are required")
        beats = [str(item.get("beat") or "") for item in timeline]
        durations = [float(item.get("end") or 0) - float(item.get("start") or 0) for item in timeline]
        proof_start = next((float(item.get("start") or 0) for item in timeline if item.get("beat") == "proof"), math.inf)
        cta_index = next((index for index, item in enumerate(timeline) if item.get("beat") == "cta"), -1)
        total_duration = max(float(item.get("end") or 0) for item in timeline)
        checks = {
            "hook_by_3_seconds": float(timeline[0].get("start") or 0) == 0 and float(timeline[0].get("end") or 0) <= 3.5,
            "proof_by_20_seconds": proof_start <= 20,
            "no_dead_beat_over_10_seconds": max(durations) <= 10,
            "at_least_five_semantic_beats": len(timeline) >= 5,
            "payoff_present": "payoff" in beats,
            "cta_after_payoff": cta_index > beats.index("payoff") if "payoff" in beats and cta_index >= 0 else False,
            "pattern_changes_present": len(set(beats)) >= 5,
            "cta_in_final_third": cta_index >= 0 and float(timeline[cta_index].get("start") or 0) >= total_duration * 0.66,
        }
        score = 100.0 * sum(checks.values()) / len(checks)
        decision = "PASS" if score >= 85 and checks["hook_by_3_seconds"] and checks["proof_by_20_seconds"] else "REVISE_ATTENTION"
        return self.store.put_audit(
            "attention_script",
            subject_id,
            decision,
            score,
            {"checks": checks, "failures": [name for name, passed in checks.items() if not passed], "threshold": 85},
        )

    def video_preflight(self, payload: dict[str, Any]) -> dict[str, Any]:
        timeline = payload.get("timeline") or []
        subject_id = str(payload.get("script_id") or payload.get("video_id") or stable_id("timeline", timeline))
        if not timeline:
            raise ValueError("semantic timeline is required; pixel-change guesses cannot substitute for planned meaning")
        required = ("start", "end", "beat", "text")
        complete = all(all(key in item and item[key] not in (None, "") for key in required) for item in timeline)
        ordered = all(
            float(item["start"]) < float(item["end"]) and (index == 0 or float(item["start"]) >= float(timeline[index - 1]["end"]) - 0.05)
            for index, item in enumerate(timeline)
        ) if complete else False
        durations = [float(item["end"]) - float(item["start"]) for item in timeline] if complete else []
        checks = {
            "semantic_timeline_complete": complete,
            "timeline_ordered": ordered,
            "no_semantic_dead_zone_over_10_seconds": bool(durations) and max(durations) <= 10,
            "visual_or_semantic_change_density": len(timeline) >= 5,
            "proof_beat_present": any(item.get("beat") == "proof" for item in timeline),
        }
        score = 100.0 * sum(checks.values()) / len(checks)
        decision = "PASS" if all(checks.values()) else "REVISE_VIDEO_PREFLIGHT"
        return self.store.put_audit(
            "attention_video_preflight",
            subject_id,
            decision,
            score,
            {"checks": checks, "failures": [name for name, passed in checks.items() if not passed]},
        )

    def video_file_audit(self, payload: dict[str, Any]) -> dict[str, Any]:
        video_path = Path(str(payload.get("video_path") or "")).expanduser()
        timeline = payload.get("timeline") or []
        if not video_path.is_file():
            raise ValueError(f"video file does not exist: {video_path}")
        if not timeline:
            raise ValueError("semantic timeline is required; an actual-video audit cannot pass on pixel changes alone")
        probe = probe_media(video_path)
        frame_report = sample_frame_changes(video_path, float(probe.get("duration_seconds") or 0.0))
        preflight = self.video_preflight({"video_id": str(video_path), "timeline": timeline})
        video_duration = float(probe.get("duration_seconds") or 0.0)
        timeline_duration = max(float(item.get("end") or 0.0) for item in timeline)
        coverage_tolerance = max(1.0, video_duration * 0.05)
        checks = {
            "decodable_video": bool(probe.get("has_video")),
            "audio_present": bool(probe.get("has_audio")),
            "positive_duration": video_duration > 0,
            "semantic_preflight_passed": preflight["decision"] == "PASS",
            "semantic_timeline_covers_video": abs(timeline_duration - video_duration) <= coverage_tolerance,
            "frames_sampled": frame_report["sample_count"] >= 3,
            "observable_visual_changes": frame_report["change_count"] >= 1,
        }
        score = 100.0 * sum(checks.values()) / len(checks)
        decision = "PASS" if all(checks.values()) else "REVISE_ACTUAL_VIDEO"
        return self.store.put_audit(
            "attention_video_file",
            str(video_path),
            decision,
            score,
            {
                "checks": checks,
                "failures": [name for name, passed in checks.items() if not passed],
                "media_probe": probe,
                "frame_change_report": frame_report,
                "semantic_preflight_audit_id": preflight["audit_id"],
                "policy": "Pixel differences are supporting evidence only; semantic timeline and media validity are mandatory.",
            },
        )


def probe_media(path: Path) -> dict[str, Any]:
    command = [
        os.environ.get("FFPROBE_BIN", "/opt/homebrew/bin/ffprobe"),
        "-v", "error", "-show_entries", "format=duration:stream=codec_type,codec_name,width,height,r_frame_rate",
        "-of", "json", str(path),
    ]
    try:
        completed = subprocess.run(command, check=True, capture_output=True, text=True, timeout=30)
    except subprocess.CalledProcessError as exc:
        detail = (exc.stderr or exc.stdout or "ffprobe failed").strip().splitlines()[-1:]
        raise ValueError("media probe failed: " + (detail[0][:240] if detail else "ffprobe failed")) from exc
    except subprocess.TimeoutExpired as exc:
        raise ValueError("media probe timed out after 30 seconds") from exc
    payload = json.loads(completed.stdout)
    streams = payload.get("streams") or []
    video = next((item for item in streams if item.get("codec_type") == "video"), {})
    return {
        "duration_seconds": round(float((payload.get("format") or {}).get("duration") or 0.0), 3),
        "has_video": bool(video),
        "has_audio": any(item.get("codec_type") == "audio" for item in streams),
        "video_codec": video.get("codec_name"),
        "width": video.get("width"),
        "height": video.get("height"),
        "frame_rate": video.get("r_frame_rate"),
    }


def sample_frame_changes(path: Path, duration: float) -> dict[str, Any]:
    import cv2

    capture = cv2.VideoCapture(str(path))
    if not capture.isOpened():
        return {"sample_count": 0, "change_count": 0, "mean_change": None, "error": "video_decode_failed"}
    sample_count = max(3, min(24, int(math.ceil(duration / 2.5)))) if duration else 3
    # Avoid sampling exactly at EOF, where valid files commonly return no frame.
    sample_span = duration * 0.98 if duration else 0.0
    times = [sample_span * index / max(sample_count - 1, 1) for index in range(sample_count)]
    previous = None
    changes: list[float] = []
    successful = 0
    for second in times:
        capture.set(cv2.CAP_PROP_POS_MSEC, second * 1000)
        ok, frame = capture.read()
        if not ok:
            continue
        gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
        gray = cv2.resize(gray, (160, 90))
        successful += 1
        if previous is not None:
            changes.append(float(cv2.absdiff(previous, gray).mean()))
        previous = gray
    capture.release()
    return {
        "sample_count": successful,
        "change_count": sum(value >= 3.0 for value in changes),
        "mean_change": round(sum(changes) / len(changes), 3) if changes else None,
        "threshold": 3.0,
    }


class RetentionService:
    def __init__(self, store: QualityStore):
        self.store = store

    def normalize(self, payload: dict[str, Any]) -> dict[str, Any]:
        platform = str(payload.get("platform") or "").lower()
        source_id = str(payload.get("source_id") or "") or None
        duration = float(payload.get("duration_seconds") or 0.0)
        curve = payload.get("curve") or []
        if not platform:
            raise ValueError("platform is required")
        if platform in {"instagram", "facebook", "tiktok"} and not curve:
            normalized = {
                "kind": "aggregate_only",
                "average_watch_seconds": payload.get("average_watch_seconds"),
                "completion_rate": payload.get("completion_rate"),
                "note": "No retention curve was fabricated from aggregate platform metrics.",
            }
        else:
            if duration <= 0 or not curve:
                raise ValueError("duration_seconds and a real curve are required for curve normalization")
            points = []
            for item in curve:
                elapsed = item.get("elapsed_seconds")
                if elapsed is None and item.get("elapsed_ratio") is not None:
                    elapsed = float(item["elapsed_ratio"]) * duration
                retained = item.get("retained_percent")
                if elapsed is None or retained is None:
                    raise ValueError("each curve point requires elapsed_seconds or elapsed_ratio plus retained_percent")
                points.append({"elapsed_seconds": round(float(elapsed), 3), "retained_percent": round(float(retained), 3)})
            points.sort(key=lambda item: item["elapsed_seconds"])
            normalized = {"kind": "retention_curve", "duration_seconds": duration, "points": points}
        receipt = self.store.put_receipt(
            "post_publish_retention",
            platform,
            source_id,
            payload.get("source_url"),
            normalized,
        )
        return {"status": "normalized", "platform": platform, "normalized": normalized, "receipt": receipt}

    def classify(self, payload: dict[str, Any]) -> dict[str, Any]:
        points = (payload.get("normalized") or {}).get("points") or payload.get("points") or []
        if len(points) < 2:
            return {"status": "aggregate_only", "events": [], "note": "A real curve with at least two points is required."}
        events = []
        for before, after in zip(points, points[1:]):
            drop = float(before["retained_percent"]) - float(after["retained_percent"])
            if drop >= 8:
                elapsed = float(after["elapsed_seconds"])
                events.append(
                    {
                        "elapsed_seconds": elapsed,
                        "drop_percent": round(drop, 2),
                        "classification": "opening_mismatch" if elapsed <= 5 else "attention_drop",
                    }
                )
        return {"status": "classified", "events": events, "event_count": len(events)}


class ContentQualityEngine:
    def __init__(self, market_tape_path: str | Path, quality_db_path: str | Path):
        self.store = QualityStore(quality_db_path)
        self.tape = MarketTapeReader(market_tape_path)
        self.viral = ViralTranscriptService(self.tape, self.store)
        self.audience = AudienceIntelligenceService(self.tape, self.store)
        self.scripts = ScriptService(self.store)
        self.relatability = RelatabilityService(self.store)
        self.attention = AttentionService(self.store)
        self.retention = RetentionService(self.store)

    def health(self) -> dict[str, Any]:
        tape = self.tape.health()
        return {
            "status": "healthy" if tape["status"] == "up" else "degraded",
            "service": "content-quality",
            "market_tape": tape,
            "learning_store": {"status": "up", "path": str(self.store.path), "counts": self.store.counts()},
            "capabilities": [
                "audience-intelligence", "viral-transcripts", "evidence-first-scripts",
                "relatability", "attention", "retention", "learning-memory",
            ],
            "checked_at": utc_now(),
        }
