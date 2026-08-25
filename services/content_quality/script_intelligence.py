"""Productized Market Tape -> language cohort -> audited script workflow.

The service keeps four evidence roles separate:

* WHAT: a current, quality-gated Market Tape trend and its exact members.
* HOW: hash-bound Whisper transcripts that demonstrate recurring language.
* WHO: source-bound human moments from those transcripts.
* PACING: observed hook/proof structures plus deterministic attention gates.

Large media remains on the Passport volume.  Queryable lineage, briefs, scripts,
audits, and workflow receipts are persisted in SQLite; Markdown is never used as
runtime state.
"""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import statistics
from collections import Counter, defaultdict
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Sequence

from .ai_relatability import NON_AI_PASS_DECISION
from .contracts import (
    ACCEPTED_OBSERVATION_EVIDENCE_CONTRACT,
    SCRIPT_INTELLIGENCE_BRIEF_CONTRACT,
    SCRIPT_LANGUAGE_DEMAND_CONTRACT,
    TREND_OBSERVATION_QUALITY_CONTRACT,
    is_supported_transcript_audit_contract,
)


UTC = timezone.utc
WORD_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9'’-]*")
STOP_WORDS = {
    "about", "after", "again", "also", "because", "been", "before", "being",
    "can", "could", "did", "does", "doing", "ever", "from", "have", "here",
    "into", "just", "know", "look", "more", "most", "one", "only", "other",
    "over", "should", "some", "than", "that", "their",
    "them", "then", "there", "these", "they", "this", "those", "through", "very",
    "want", "what", "when", "where", "which", "while", "with", "would", "your",
}
HUMAN_TERMS = {
    "alone", "anxious", "anxiety", "burned", "burnout", "burnt", "care",
    "challenge", "challenges", "client", "clients", "customer", "customers",
    "daily", "deadline", "deadlines", "difficult", "email", "emails",
    "exhausted", "fail", "failed", "failing", "fear", "feel", "feeling",
    "frustrated", "hard", "hate", "hopeless", "hour", "hours", "issue",
    "form", "forms", "invoice", "invoices", "issues", "job", "jobs",
    "late", "lead", "leads",
    "meeting", "meetings", "minute", "minutes", "morning", "mornings",
    "must", "need", "needed", "needs",
    "night", "nights", "overwhelmed", "pressure", "problem", "problems",
    "quit", "scattered", "solution", "solutions", "struggle", "struggling",
    "quote", "quotes", "sales", "stuck", "support", "task", "tasks", "team",
    "teams", "time", "tired", "trying", "week",
    "weeks", "wish", "work", "working",
    "worry", "worse",
}
ACTIONABLE_STATES = {"discovering", "emerging", "breakout", "recurring"}
TREND_SELECTION_CONTRACT = "script_intelligence_trend_selection_v3"
CANDIDATE_ASSESSMENT_CONTRACT = "script_intelligence_trend_candidate_assessment_v1"
SCRIPT_VARIANT_SELECTION_CONTRACT = "source_bound_human_moment_variant_v1"
SCRIPT_GENERATION_CONTRACT = "evidence_bound_category_script_v9"
MAX_SCRIPT_VARIANTS = 8


def utc_now() -> str:
    return datetime.now(UTC).isoformat()


def canonical_sha256(value: Any) -> str:
    encoded = json.dumps(
        value, sort_keys=True, separators=(",", ":"), default=str
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _words(value: str) -> list[str]:
    return WORD_RE.findall(value or "")


def _terms(value: str) -> list[str]:
    return list(dict.fromkeys(
        token.lower()
        for token in _words(value)
        if (len(token) > 2 or token.lower() == "ai")
        and token.lower() not in STOP_WORDS
    ))[:10]


def _json_list(value: Any) -> list[str]:
    if isinstance(value, list):
        return [str(item) for item in value]
    try:
        parsed = json.loads(str(value or "[]"))
    except (TypeError, ValueError, json.JSONDecodeError):
        return []
    return [str(item) for item in parsed] if isinstance(parsed, list) else []


def _lower_first(value: str) -> str:
    value = value.strip()
    return value[:1].lower() + value[1:] if value else value


class ScriptIntelligenceService:
    """One bounded interface for evidence selection, writing, and all gates."""

    def __init__(
        self,
        *,
        tape: Any,
        store: Any,
        viral: Any,
        audience: Any,
        style_guides: Any,
        scripts: Any,
        relatability: Any,
        ai_relatability: Any,
        attention: Any,
        transcript_storage_root: str | Path,
        demand_enqueuer: Callable[[dict[str, Any]], dict[str, Any]] | None = None,
    ) -> None:
        self.tape = tape
        self.store = store
        self.viral = viral
        self.audience = audience
        self.style_guides = style_guides
        self.scripts = scripts
        self.relatability = relatability
        self.ai_relatability = ai_relatability
        self.attention = attention
        self.transcript_storage_root = Path(transcript_storage_root).expanduser()
        self.demand_enqueuer = demand_enqueuer

    @staticmethod
    def _variant_index(payload: dict[str, Any]) -> int:
        """Return a strict, bounded zero-based source-moment selector."""

        if "variant_index" not in payload:
            return 0
        value = payload.get("variant_index")
        if type(value) is not int:
            raise ValueError("variant_index must be a JSON integer")
        if value < 0 or value >= MAX_SCRIPT_VARIANTS:
            raise ValueError(
                f"variant_index must be between 0 and {MAX_SCRIPT_VARIANTS - 1}"
            )
        return value

    @staticmethod
    def _distinct_source_moments(
        moments: Sequence[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        """Keep only moments that can produce text-distinct source-bound scripts."""

        distinct: list[dict[str, Any]] = []
        seen: set[tuple[str, str]] = set()
        for moment in moments:
            situation = " ".join(_words(str(moment.get("situation") or ""))).casefold()
            stakes = " ".join(_words(str(moment.get("stakes") or ""))).casefold()
            key = (situation, stakes)
            if not all(key) or key in seen:
                continue
            seen.add(key)
            distinct.append(dict(moment))
        return distinct

    def readiness(self) -> dict[str, Any]:
        if not self.tape.path.is_file():
            return {
                "status": "not_ready",
                "gaps": ["market_tape_database_missing"],
                "database_path": str(self.tape.path),
            }
        try:
            with closing(self.tape.connect()) as connection:
                objects = {
                    str(row[0]) for row in connection.execute(
                        "SELECT name FROM sqlite_master WHERE type IN ('table','view')"
                    ).fetchall()
                }
                schema_row = (
                    connection.execute(
                        "SELECT value FROM mt_meta WHERE key='schema_version'"
                    ).fetchone()
                    if "mt_meta" in objects else None
                )
                schema_version = int(schema_row[0]) if schema_row else 0

                def count(table: str, where: str = "", params: Sequence[Any] = ()) -> int:
                    if table not in objects:
                        return 0
                    query = f"SELECT COUNT(*) FROM {table}"
                    if where:
                        query += f" WHERE {where}"
                    return int(connection.execute(query, tuple(params)).fetchone()[0])

                full_evidence = count("mt_accepted_full_evidence_v1")
                memberships = count("mt_accepted_trend_memberships_v1")
                trend_observations = count(
                    "mt_trend_observations",
                    "observation_quality_contract=?",
                    (TREND_OBSERVATION_QUALITY_CONTRACT,),
                )
                artifacts = count("mt_transcript_artifacts")
                watermark = {
                    "accepted_full_evidence_at": connection.execute(
                        "SELECT MAX(accepted_at) FROM mt_accepted_full_evidence_v1"
                    ).fetchone()[0] if full_evidence else None,
                    "trend_observed_at": connection.execute(
                        "SELECT MAX(observed_at) FROM mt_trend_observations "
                        "WHERE observation_quality_contract=?",
                        (TREND_OBSERVATION_QUALITY_CONTRACT,),
                    ).fetchone()[0] if trend_observations else None,
                }
        except (OSError, ValueError, sqlite3.Error) as exc:
            return {"status": "not_ready", "gaps": [f"database_error:{exc}"]}

        gaps: list[str] = []
        if schema_version < 12:
            gaps.append("market_tape_schema_12_required")
        if not full_evidence:
            gaps.append("accepted_full_evidence_empty")
        if not memberships:
            gaps.append("accepted_trend_memberships_empty")
        if not trend_observations:
            gaps.append("quality_gated_trend_observations_empty")
        if not artifacts:
            gaps.append("transcript_artifacts_empty")
        snapshot_material = {
            "schema_version": schema_version,
            "accepted_full_evidence": full_evidence,
            "accepted_trend_memberships": memberships,
            "quality_gated_trend_observations": trend_observations,
            "transcript_artifacts": artifacts,
            "watermark": watermark,
        }
        return {
            "status": "ready" if not gaps else "not_ready",
            "contract": "script_intelligence_readiness_v1",
            "gaps": gaps,
            **snapshot_material,
            "snapshot_id": "mtsnap_" + canonical_sha256(snapshot_material)[:24],
            "score_is_probability": False,
            "forecast_probabilities_admitted": False,
        }

    def _trend_groups(self, topic: str, limit: int = 20) -> list[dict[str, Any]]:
        with closing(self.tape.connect()) as connection:
            rows = connection.execute(
                """
                WITH latest_signal_time AS (
                    SELECT observation.trend_id,
                           MAX(observation.observed_at) AS observed_at
                    FROM mt_trend_observations observation
                    WHERE observation.observation_quality_contract=?
                    GROUP BY observation.trend_id
                ),
                latest_signal AS (
                    SELECT observation.*
                    FROM mt_trend_observations observation
                    JOIN latest_signal_time latest
                      ON latest.trend_id=observation.trend_id
                     AND latest.observed_at=observation.observed_at
                    WHERE observation.trend_observation_id=(
                        SELECT MAX(tied.trend_observation_id)
                        FROM mt_trend_observations tied
                        WHERE tied.trend_id=observation.trend_id
                          AND tied.observed_at=observation.observed_at
                          AND tied.observation_quality_contract=
                              observation.observation_quality_contract
                    )
                ),
                accepted_evidence AS MATERIALIZED (
                    SELECT evidence.*
                    FROM mt_accepted_observation_evidence evidence
                    LEFT JOIN mt_observation_quality_flags quality
                      ON quality.observation_id=evidence.observation_id
                    WHERE evidence.contract=?
                      AND evidence.evidence_scope='full'
                      AND quality.observation_id IS NULL
                ),
                latest_accepted_lineage AS (
                    SELECT ranked.* FROM (
                        SELECT lineage.trend_id, lineage.video_id,
                               lineage.observation_id, lineage.linked_at,
                               ROW_NUMBER() OVER (
                                   PARTITION BY lineage.trend_id, lineage.video_id
                                   ORDER BY lineage.linked_at DESC,
                                            lineage.observation_id DESC
                               ) AS row_number
                        FROM mt_trend_membership_lineage lineage
                        JOIN accepted_evidence accepted
                          ON accepted.observation_id=lineage.observation_id
                        WHERE lineage.contract=?
                    ) ranked WHERE ranked.row_number=1
                )
                SELECT trend.trend_id, trend.trend_type, trend.canonical_key,
                       trend.display_name, trend.first_seen_at, trend.last_seen_at,
                       signal.observed_at AS trend_observed_at, signal.state,
                       signal.trend_strength, signal.relative_strength,
                       signal.momentum, signal.acceleration, signal.saturation,
                       signal.videos_total, signal.creators_total,
                       signal.platforms_total, signal.views_total,
                       signal.counter_delta_videos, signal.activity_coverage,
                       membership.confidence AS membership_confidence,
                       video.video_id, video.platform, video.external_id,
                       video.creator_id, evidence.observation_id,
                       evidence.observation_key, evidence.accepted_at,
                       evidence.published_at, evidence.title, evidence.caption,
                       evidence.description, evidence.url,
                       evidence.hashtags_json, evidence.discovery_queries_json,
                       metric.observed_at, metric.views, metric.likes,
                       metric.comments, metric.shares, metric.saves,
                       metric.view_velocity, metric.view_acceleration,
                       metric.relative_strength AS video_relative_strength,
                       (SELECT COUNT(*) FROM mt_accepted_metric_observations_v1 counted
                        WHERE counted.video_id=video.video_id) AS observation_count
                FROM mt_trends trend
                JOIN latest_signal signal ON signal.trend_id=trend.trend_id
                JOIN mt_trend_memberships membership
                  ON membership.trend_id=trend.trend_id
                JOIN latest_accepted_lineage lineage
                  ON lineage.trend_id=membership.trend_id
                 AND lineage.video_id=membership.video_id
                JOIN accepted_evidence evidence
                  ON evidence.observation_id=lineage.observation_id
                JOIN mt_market_observations metric
                  ON metric.observation_id=evidence.observation_id
                 AND metric.source_confidence > 0
                JOIN mt_videos video ON video.video_id=membership.video_id
                WHERE lower(signal.state) IN ('discovering','emerging','breakout','recurring')
                ORDER BY signal.trend_strength DESC, signal.videos_total DESC,
                         signal.observed_at DESC, trend.trend_id, video.video_id
                LIMIT 5000
                """,
                (
                    TREND_OBSERVATION_QUALITY_CONTRACT,
                    ACCEPTED_OBSERVATION_EVIDENCE_CONTRACT,
                    ACCEPTED_OBSERVATION_EVIDENCE_CONTRACT,
                ),
            ).fetchall()
        grouped: dict[str, dict[str, Any]] = {}
        for raw in rows:
            row = dict(raw)
            trend_id = str(row["trend_id"])
            group = grouped.setdefault(trend_id, {
                "trend_id": trend_id,
                "trend_type": row["trend_type"],
                "canonical_key": row["canonical_key"],
                "display_name": row["display_name"],
                "state": row["state"],
                "observed_at": row["trend_observed_at"],
                "signals": {
                    "trend_strength": float(row["trend_strength"] or 0),
                    "relative_strength": float(row["relative_strength"] or 0),
                    "momentum": float(row["momentum"] or 0),
                    "acceleration": float(row["acceleration"] or 0),
                    "saturation": float(row["saturation"] or 0),
                    "activity_coverage": float(row["activity_coverage"] or 0),
                },
                "evidence": {
                    "videos_total": int(row["videos_total"] or 0),
                    "creators_total": int(row["creators_total"] or 0),
                    "platforms_total": int(row["platforms_total"] or 0),
                    "views_total": int(row["views_total"] or 0),
                    "counter_delta_videos": int(row["counter_delta_videos"] or 0),
                },
                "members": [],
            })
            group["members"].append({
                key: row.get(key)
                for key in (
                    "video_id", "platform", "external_id", "creator_id",
                    "observation_id", "observation_key", "accepted_at",
                    "published_at", "title", "caption", "description", "url",
                    "hashtags_json", "discovery_queries_json", "observed_at",
                    "views", "likes", "comments", "shares", "saves",
                    "view_velocity", "view_acceleration", "video_relative_strength",
                    "observation_count", "membership_confidence",
                )
            })

        requested = set(_terms(topic))
        candidates: list[dict[str, Any]] = []
        for group in grouped.values():
            label_corpus = " ".join([
                str(group["display_name"] or ""),
                str(group["canonical_key"] or ""),
            ])
            member_corpus = " ".join([
                *[
                    " ".join(str(member.get(field) or "") for field in (
                        "title", "caption", "description",
                    ))
                    for member in group["members"]
                ],
            ])
            label_words = {term.casefold() for term in _words(label_corpus)}
            corpus_words = label_words | {
                term.casefold() for term in _words(member_corpus)
            }
            group["label_topic_matches"] = sorted(
                term for term in requested if term in label_words
            )
            group["topic_matches"] = sorted(
                term for term in requested if term in corpus_words
            )
            if requested and not group["topic_matches"]:
                continue
            # A trend whose own immutable label names the requested topic is a
            # stronger topical relationship than an unrelated label that merely
            # shares one member video.  Keep both visible, but evaluate the direct
            # trend first so a globally strong co-occurrence such as "ice cream"
            # cannot outrank "automation can" for an automation script.
            group["topic_label_match_priority"] = (
                0 if not requested or group["label_topic_matches"] else 1
            )
            trend_type = str(group.get("trend_type") or "").casefold()
            if not requested:
                topic_affinity_priority = 0
                topic_affinity = "unconstrained"
            elif trend_type == "topic":
                topic_affinity_priority = 0
                topic_affinity = "topic_like"
            elif trend_type == "format":
                topic_affinity_priority = 2
                topic_affinity = "generic_format"
            else:
                topic_affinity_priority = 1
                topic_affinity = "non_format_signal"
            group["topic_affinity"] = topic_affinity
            group["topic_affinity_priority"] = topic_affinity_priority
            group["platform_distribution"] = dict(sorted(Counter(
                str(member["platform"]) for member in group["members"]
            ).items()))
            candidates.append(group)
        candidates.sort(key=lambda item: (
            int(item["topic_label_match_priority"]),
            int(item["topic_affinity_priority"]),
            -len(item["topic_matches"]),
            -float(item["signals"]["trend_strength"]),
            -int(item["evidence"]["videos_total"]),
            str(item["trend_id"]),
        ))
        for rank, item in enumerate(candidates[:limit], start=1):
            item["rank"] = rank
            item["ranking_contract"] = TREND_SELECTION_CONTRACT
            item["score_is_probability"] = False
            item["prediction"] = None
        return candidates[:limit]

    @staticmethod
    def _keyword_signals(members: Sequence[dict[str, Any]]) -> list[dict[str, Any]]:
        try:
            from services.market_tape.keywords import rank_keywords
        except ImportError:
            return []
        rows = []
        for member in members:
            rows.append({
                **member,
                "hashtags_json": member.get("hashtags_json") or "[]",
                "discovery_queries_json": member.get("discovery_queries_json") or "[]",
            })
        evidence_times: list[datetime] = []
        for row in rows:
            value = str(row.get("observed_at") or "").strip()
            if not value:
                continue
            try:
                parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
            except ValueError:
                continue
            evidence_times.append(
                parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)
            )
        return rank_keywords(
            rows,
            window_hours=24 * 30,
            min_videos=1,
            limit=20,
            now=max(evidence_times) if evidence_times else None,
            candidate_mode="all",
        )

    def _record_failed_attempt(
        self,
        *,
        code: str,
        payload: dict[str, Any],
        detail: dict[str, Any],
    ) -> dict[str, Any]:
        attempt = {
            "contract": "script_intelligence_attempt_v1",
            "status": "not_ready",
            "code": code,
            "request": {
                "topic": payload.get("topic"),
                "audience": payload.get("audience"),
                "objective": payload.get("objective"),
                "variant_index": payload.get("variant_index", 0),
            },
            "detail": detail,
            "created_at": utc_now(),
        }
        receipt = self.store.put_receipt(
            "script_intelligence_attempt", "market_tape", code, None, attempt
        )
        result = {**attempt, "attempt_receipt_id": receipt["receipt_id"]}
        if code not in {
            "NO_QUALITY_GATED_TREND_MATCH",
            "NO_SCRIPT_READY_TREND_CANDIDATE",
        }:
            return result

        assessments = list(detail.get("candidate_assessments") or [])
        candidate = assessments[0] if assessments else {}
        gates = candidate.get("gates") or {}
        minimums = detail.get("minimums") or {
            "verified_transcripts": 5,
            "distinct_creators": 3,
            "observed_views": 100_000,
        }
        targets = {
            "verified_transcripts": int(
                minimums.get("verified_transcripts") or 5
            ),
            "distinct_creators": int(minimums.get("distinct_creators") or 3),
            "observed_views": int(minimums.get("observed_views") or 100_000),
        }
        actuals = {
            "verified_transcripts": int(
                (gates.get("minimum_verified_transcripts") or {}).get("actual")
                or 0
            ),
            "distinct_creators": int(
                (gates.get("minimum_distinct_creators") or {}).get("actual")
                or 0
            ),
            "observed_views": int(
                (gates.get("minimum_observed_views") or {}).get("actual") or 0
            ),
        }
        topic = str(
            payload.get("topic")
            or candidate.get("language_query")
            or candidate.get("display_name")
            or ""
        ).strip()
        audience = str(payload.get("audience") or "").strip()
        if not topic or not audience:
            result["demand_feedback"] = {
                "status": "not_enqueued",
                "code": "SCRIPT_LANGUAGE_DEMAND_REQUIRES_TOPIC_AND_AUDIENCE",
            }
            return result
        transcript_deficit = max(
            0, targets["verified_transcripts"] - actuals["verified_transcripts"]
        )
        creator_deficit = max(
            0, targets["distinct_creators"] - actuals["distinct_creators"]
        )
        demand = {
            "contract": SCRIPT_LANGUAGE_DEMAND_CONTRACT,
            "source_service": "content-quality",
            "source_receipt_id": receipt["receipt_id"],
            "topic": topic,
            "audience": audience,
            "objective": str(
                payload.get("objective") or "qualified_attention"
            ).strip(),
            # The trend ID scopes evidence lineage only. It is never treated as
            # a permission to copy language or as a predicted probability.
            "evidence_trend_id": str(candidate.get("trend_id") or ""),
            "snapshot_id": str(detail.get("snapshot_id") or ""),
            "targets": targets,
            "actuals": actuals,
            "deficits": {
                "verified_transcripts": transcript_deficit,
                "distinct_creators": creator_deficit,
                "observed_views": max(
                    0, targets["observed_views"] - actuals["observed_views"]
                ),
            },
            "acquisition_policy": {
                "cycles": 1,
                "platforms": ["youtube", "tiktok", "instagram", "facebook"],
                "discovery_limit": 50,
                "transcript_limit": min(
                    10, max(1, 2 * max(transcript_deficit, creator_deficit))
                ),
                "whisper_model": "base",
                "creator_diverse": True,
                "same_call_retry": False,
            },
            "failure_code": code,
            "candidate_assessments": assessments,
        }
        if self.demand_enqueuer is None:
            result["demand_feedback"] = {
                "status": "not_configured",
                "code": "MARKET_TAPE_DEMAND_CLIENT_NOT_CONFIGURED",
            }
            return result
        try:
            queued = self.demand_enqueuer(demand)
            feedback = {
                "status": "queued",
                "demand_id": queued.get("demand_id"),
                "state": queued.get("state") or "requested",
                "idempotent": bool(queued.get("idempotent")),
            }
        except Exception as exc:  # submission failure must not erase the refusal receipt
            feedback = {
                "status": "unavailable",
                "code": str(
                    getattr(exc, "code", "SCRIPT_LANGUAGE_DEMAND_ENQUEUE_FAILED")
                ),
            }
            http_status = getattr(exc, "http_status", None)
            if http_status is not None:
                feedback["http_status"] = int(http_status)
        feedback_receipt = self.store.put_receipt(
            "script_language_demand_enqueue",
            "market_tape",
            str(feedback.get("demand_id") or receipt["receipt_id"]),
            None,
            {
                "attempt_receipt_id": receipt["receipt_id"],
                "request_contract": SCRIPT_LANGUAGE_DEMAND_CONTRACT,
                "request_sha256": canonical_sha256(demand),
                "feedback": feedback,
                "created_at": utc_now(),
            },
        )
        result["demand_feedback"] = {
            **feedback,
            "receipt_id": feedback_receipt["receipt_id"],
        }
        return result

    @staticmethod
    def _candidate_language_query(
        trend: dict[str, Any],
        requested_topic: str,
    ) -> tuple[str, list[str]]:
        """Build a deterministic, trend-specific query for language evidence.

        The requested topic constrains which trends can be considered.  The selected
        trend's own name then constrains semantic language precedents, so a generic
        topic corpus cannot make every matching trend appear script-ready.
        """

        requested_terms = _terms(requested_topic)
        trend_label = str(
            trend.get("display_name") or trend.get("canonical_key") or ""
        ).strip()
        trend_terms = _terms(trend_label)
        query_terms = list(dict.fromkeys([*requested_terms, *trend_terms]))
        language_query = " ".join(query_terms) or requested_topic or trend_label
        requested_set = set(requested_terms)
        differentiating_terms = [
            term for term in trend_terms if term not in requested_set
        ]
        return language_query, differentiating_terms

    def _assess_trend_candidate(
        self,
        *,
        trend: dict[str, Any],
        requested_topic: str,
        audience_name: str,
        minimum_transcripts: int,
        style_platform: str,
        semantic_candidates: Sequence[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        """Evaluate one ranked trend without weakening any script evidence gate."""

        topic = requested_topic or str(trend["display_name"])
        language_query, differentiating_terms = self._candidate_language_query(
            trend, requested_topic
        )
        exact_video_ids = sorted({
            str(member["video_id"]) for member in trend["members"]
        })
        exact_video_id_set = set(exact_video_ids)
        semantic = (
            list(semantic_candidates)
            if semantic_candidates is not None
            else self.tape.candidates(language_query, limit=500)
        )
        if semantic_candidates is not None:
            # Shared semantic rows already came from the artifact-bound language
            # lane. Resolve only this trend's exact members, then merge by stable
            # video identity instead of re-querying the whole shared cohort for
            # every trend candidate.
            artifact_by_video = {
                str(row["video_id"]): dict(row) for row in semantic
            }
            artifact_by_video.update({
                str(row["video_id"]): dict(row)
                for row in self.tape.artifact_bound_candidates(exact_video_ids)
            })
            artifact_rows = list(artifact_by_video.values())
        else:
            candidate_ids = [*exact_video_ids, *[
                str(row["video_id"]) for row in semantic
                if str(row["video_id"]) not in exact_video_id_set
            ]]
            artifact_rows = self.tape.artifact_bound_candidates(candidate_ids)
        language_terms = set(_terms(language_query))
        differentiating_set = set(differentiating_terms)
        qualified_rows: list[dict[str, Any]] = []
        for raw_row in artifact_rows:
            row = dict(raw_row)
            searchable = " ".join(
                str(row.get(field) or "")
                for field in ("title", "caption", "description", "transcript")
            ).lower()
            matches = sorted(term for term in language_terms if term in searchable)
            differentiating_matches = sorted(
                term for term in differentiating_set if term in searchable
            )
            is_exact = str(row["video_id"]) in exact_video_id_set
            if is_exact or (matches and (
                not differentiating_set or differentiating_matches
            )):
                row["topic_matches"] = matches
                row["trend_specific_matches"] = differentiating_matches
                qualified_rows.append(row)
        qualified_rows.sort(key=lambda row: (
            0 if str(row["video_id"]) in exact_video_id_set else 1,
            -len(row.get("trend_specific_matches") or []),
            -len(row.get("topic_matches") or []),
            -float(row.get("relative_strength") or 0),
            -int(row.get("views") or 0),
            str(row["video_id"]),
        ))
        discovery = self.viral.discover_for_videos(
            language_query,
            [str(row["video_id"]) for row in qualified_rows],
            limit=max(15, minimum_transcripts * 3),
        )
        unique_receipts: dict[tuple[str, str], dict[str, Any]] = {}
        for receipt in discovery.get("receipts") or []:
            source = receipt.get("payload") or {}
            identity = (
                str(source.get("transcript_id") or ""),
                str(source.get("observation_key") or ""),
            )
            if all(identity):
                unique_receipts.setdefault(identity, receipt)
        receipts = list(unique_receipts.values())
        creator_ids = {
            str(receipt["payload"].get("creator_id") or "")
            for receipt in receipts if receipt["payload"].get("creator_id")
        }
        total_views = sum(
            int(receipt["payload"].get("pattern", {}).get(
                "source_metrics", {}
            ).get("views") or 0)
            for receipt in receipts
        )
        discovery_failure_reasons = dict(sorted(Counter(
            str(row.get("error") or "unknown")
            for row in discovery.get("failures") or []
        ).items()))
        assessment: dict[str, Any] = {
            "contract": CANDIDATE_ASSESSMENT_CONTRACT,
            "trend_id": trend["trend_id"],
            "trend_type": trend["trend_type"],
            "display_name": trend["display_name"],
            "rank": trend["rank"],
            "ranking_contract": trend["ranking_contract"],
            "topic_affinity": trend["topic_affinity"],
            "topic_affinity_priority": trend["topic_affinity_priority"],
            "topic_matches": trend["topic_matches"],
            "trend_strength": trend["signals"]["trend_strength"],
            "score_is_probability": False,
            "language_query": language_query,
            "trend_specific_terms": differentiating_terms,
            "exact_trend_member_count": len(exact_video_ids),
            "qualified_language_candidate_count": len(qualified_rows),
            "discovery_failure_reasons": discovery_failure_reasons,
            "gates": {
                "minimum_verified_transcripts": {
                    "actual": len(receipts),
                    "minimum": minimum_transcripts,
                    "pass": len(receipts) >= minimum_transcripts,
                },
                "minimum_distinct_creators": {
                    "actual": len(creator_ids), "minimum": 3,
                    "pass": len(creator_ids) >= 3,
                },
                "minimum_observed_views": {
                    "actual": total_views, "minimum": 100_000,
                    "pass": total_views >= 100_000,
                },
                "transcript_cohort_audit": {"status": "not_evaluated"},
                "source_bound_human_moment": {"status": "not_evaluated"},
                "cross_creator_human_language": {"status": "not_evaluated"},
                "aggregate_transcript_style_guide": {
                    "status": "not_evaluated"
                },
            },
        }

        failed_quantitative_gates = [
            name for name in (
                "minimum_verified_transcripts",
                "minimum_distinct_creators",
                "minimum_observed_views",
            )
            if not assessment["gates"][name]["pass"]
        ]
        if failed_quantitative_gates:
            assessment.update({
                "decision": "REJECT",
                "code": "INSUFFICIENT_VERIFIED_LANGUAGE_COHORT",
                "failed_gates": failed_quantitative_gates,
            })
            return {"eligible": False, "assessment": assessment}

        artifacts = []
        for receipt in receipts:
            source = receipt["payload"]
            artifact = self.tape.transcript_artifact(
                str(source["video_id"]), str(source["observation_key"])
            )
            if artifact:
                artifacts.append(artifact)
        from .transcript_bank import TranscriptBank

        bank = TranscriptBank(self.tape.path, self.transcript_storage_root)
        cohort = bank.build_cohort(
            topic=language_query,
            artifacts=artifacts,
            minimum_members=minimum_transcripts,
            minimum_creators=3,
            minimum_total_views=100_000,
        )
        assessment["gates"]["transcript_cohort_audit"] = {
            "status": "evaluated",
            "pass": cohort.get("decision") == "PASS",
            "decision": cohort.get("decision"),
            "cohort_id": cohort.get("cohort_id"),
            "checks": cohort.get("audit", {}).get("checks"),
            "aggregate_metrics": cohort.get("aggregate_metrics"),
        }
        if cohort.get("decision") != "PASS":
            assessment.update({
                "decision": "REJECT",
                "code": "TRANSCRIPT_COHORT_AUDIT_FAILED",
                "failed_gates": ["transcript_cohort_audit"],
            })
            return {"eligible": False, "assessment": assessment}

        moments = self.audience.human_moments(
            language_query,
            audience_name,
            limit=8,
            video_ids=[str(receipt["payload"]["video_id"]) for receipt in receipts],
        )
        assessment["gates"]["source_bound_human_moment"] = {
            "status": "evaluated",
            "actual": len(moments.get("moments") or []),
            "minimum": 1,
            "pass": bool(moments.get("moments")),
        }
        if not moments.get("moments"):
            assessment.update({
                "decision": "REJECT",
                "code": "NO_SOURCE_BOUND_HUMAN_MOMENT",
                "failed_gates": ["source_bound_human_moment"],
            })
            return {"eligible": False, "assessment": assessment}

        keyword_signals = self._keyword_signals(trend["members"])
        keyword_video_ids = {
            str(example.get("video_id") or "")
            for signal in keyword_signals
            for example in signal.get("examples") or []
        }
        relationship_by_video: dict[str, str] = {}
        for row in qualified_rows:
            video_id = str(row["video_id"])
            relationship_by_video[video_id] = (
                "exact_trend_member" if video_id in exact_video_id_set
                else "keyword_match" if video_id in keyword_video_ids
                else "semantic_precedent"
            )

        term_creators: dict[str, set[str]] = defaultdict(set)
        hook_families: Counter[str] = Counter()
        structures: Counter[str] = Counter()
        proof_seconds: list[float] = []
        words_per_second: list[float] = []
        transcript_sources: list[dict[str, Any]] = []
        for receipt in receipts:
            source = receipt["payload"]
            creator_id = str(source.get("creator_id") or "")
            for term in source.get("transcript_keywords") or []:
                term = str(term).lower()
                if term:
                    term_creators[term].add(creator_id)
            pattern = source.get("pattern") or {}
            hook_families[str(pattern.get("opening_shape") or "unknown")] += 1
            for structure in pattern.get("structure") or []:
                structures[str(structure)] += 1
            if pattern.get("first_proof_seconds") is not None:
                proof_seconds.append(float(pattern["first_proof_seconds"]))
            if pattern.get("estimated_words_per_second") is not None:
                words_per_second.append(float(pattern["estimated_words_per_second"]))
            video_id = str(source["video_id"])
            transcript_sources.append({
                "receipt_id": receipt["receipt_id"],
                "relationship": relationship_by_video.get(
                    video_id, "semantic_precedent"
                ),
                "video_id": video_id,
                "creator_id": creator_id,
                "platform": source.get("platform"),
                "source_url": receipt.get("source_url"),
                "transcript_id": source.get("transcript_id"),
                "observation_key": source.get("observation_key"),
                "audio_sha256": source.get("audio_sha256"),
                "transcript_sha256": source.get("transcript_sha256"),
                "audit_contract": source.get(
                    "performance_qualification", {}
                ).get("audit_contract"),
                "metrics": pattern.get("source_metrics") or {},
            })
        recurring_terms = sorted(
            (term for term, creators in term_creators.items() if len(creators) >= 2),
            key=lambda term: (-len(term_creators[term]), term),
        )
        recurring_human_terms = [
            term for term in recurring_terms if term in HUMAN_TERMS
        ]
        assessment["gates"]["cross_creator_human_language"] = {
            "contract": "cross_creator_everyday_human_language_v1",
            "evidence_kind": "non_ai_source_language_recurrence",
            "status": "evaluated",
            "actual": len(recurring_human_terms),
            "minimum": 1,
            "pass": bool(recurring_human_terms),
            "terms": [
                {
                    "term": term,
                    "distinct_creator_count": len(term_creators[term]),
                    "creator_ids": sorted(term_creators[term]),
                }
                for term in recurring_human_terms
            ],
            "ai_relatability_verdict": "not_evaluated",
        }
        if not recurring_human_terms:
            assessment.update({
                "decision": "REJECT",
                "code": "NO_CROSS_CREATOR_HUMAN_LANGUAGE",
                "failed_gates": ["cross_creator_human_language"],
            })
            return {"eligible": False, "assessment": assessment}

        style_result = self.style_guides.build({
            "topic": language_query,
            "platform": style_platform,
            "receipt_ids": [receipt["receipt_id"] for receipt in receipts],
            "minimum_transcripts": minimum_transcripts,
            "minimum_creators": 3,
            "minimum_observed_views": 100_000,
        })
        style_ready = style_result.get("status") == "ready"
        assessment["gates"]["aggregate_transcript_style_guide"] = {
            "status": "evaluated",
            "pass": style_ready,
            "platform": style_platform,
            "code": style_result.get("code"),
            "guide_id": (
                (style_result.get("guide") or {}).get("guide_id")
            ),
            "receipt_id": (
                (style_result.get("receipt") or {}).get("receipt_id")
            ),
        }
        if not style_ready:
            assessment.update({
                "decision": "REJECT",
                "code": style_result.get("code") or "STYLE_GUIDE_NOT_READY",
                "failed_gates": ["aggregate_transcript_style_guide"],
                "style_guide_result": style_result,
            })
            return {"eligible": False, "assessment": assessment}

        assessment.update({
            "decision": "PASS",
            "code": "SCRIPT_READY_TREND_CANDIDATE",
            "failed_gates": [],
        })
        return {
            "eligible": True,
            "assessment": assessment,
            "topic": topic,
            "language_query": language_query,
            "receipts": receipts,
            "creator_ids": creator_ids,
            "total_views": total_views,
            "cohort": cohort,
            "moments": moments,
            "keyword_signals": keyword_signals,
            "transcript_sources": transcript_sources,
            "recurring_terms": recurring_terms,
            "recurring_human_terms": recurring_human_terms,
            "hook_families": hook_families,
            "structures": structures,
            "proof_seconds": proof_seconds,
            "words_per_second": words_per_second,
            "style_guide": style_result,
        }

    def build_brief(self, payload: dict[str, Any]) -> dict[str, Any]:
        audience_name = str(payload.get("audience") or "").strip()
        requested_topic = str(payload.get("topic") or "").strip()
        objective = str(payload.get("objective") or "qualified_attention").strip()
        style_platform = str(
            payload.get("style_platform")
            or payload.get("platform")
            or "cross_platform"
        ).strip().lower()
        variant_index = self._variant_index(payload)
        minimum_transcripts = max(5, min(20, int(payload.get("minimum_transcripts") or 5)))
        if not audience_name:
            raise ValueError("audience is required")
        ready = self.readiness()
        if ready["status"] != "ready":
            return self._record_failed_attempt(
                code="MARKET_TAPE_SCRIPT_LINEAGE_NOT_READY",
                payload=payload,
                detail={"readiness": ready},
            )
        trends = self._trend_groups(requested_topic, limit=20)
        if not trends:
            return self._record_failed_attempt(
                code="NO_QUALITY_GATED_TREND_MATCH",
                payload=payload,
                detail={
                    "topic": requested_topic or None,
                    "snapshot_id": ready.get("snapshot_id"),
                    "minimums": {
                        "verified_transcripts": minimum_transcripts,
                        "distinct_creators": 3,
                        "observed_views": 100_000,
                    },
                },
            )
        candidate_assessments: list[dict[str, Any]] = []
        selected: dict[str, Any] | None = None
        trend: dict[str, Any] | None = None
        # One broad, accepted-evidence query feeds every candidate assessment.
        # Re-running the multi-GB ranked-window query once per trend made one
        # bounded product call exceed two minutes as the tape grew.
        shared_semantic_candidates = (
            self.tape.transcript_candidates(requested_topic, limit=500)
            if requested_topic else []
        )
        for candidate in trends:
            result = self._assess_trend_candidate(
                trend=candidate,
                requested_topic=requested_topic,
                audience_name=audience_name,
                minimum_transcripts=minimum_transcripts,
                style_platform=style_platform,
                semantic_candidates=shared_semantic_candidates,
            )
            candidate_assessments.append(result["assessment"])
            if result["eligible"]:
                selected = result
                trend = candidate
                break
        if selected is None or trend is None:
            return self._record_failed_attempt(
                code="NO_SCRIPT_READY_TREND_CANDIDATE",
                payload=payload,
                detail={
                    "selection_contract": TREND_SELECTION_CONTRACT,
                    "snapshot_id": ready.get("snapshot_id"),
                    "candidate_count": len(trends),
                    "assessed_candidate_count": len(candidate_assessments),
                    "semantic_candidate_query_count": 1 if requested_topic else 0,
                    "semantic_candidate_source": "accepted_whisper_artifacts",
                    "shared_semantic_candidate_count": len(
                        shared_semantic_candidates
                    ),
                    "minimums": {
                        "verified_transcripts": minimum_transcripts,
                        "distinct_creators": 3,
                        "observed_views": 100_000,
                        "source_bound_human_moments": 1,
                        "cross_creator_human_terms": 1,
                    },
                    "candidate_assessments": candidate_assessments,
                },
            )

        topic = selected["topic"]
        language_query = selected["language_query"]
        receipts = selected["receipts"]
        cohort = selected["cohort"]
        moments = selected["moments"]
        keyword_signals = selected["keyword_signals"]
        transcript_sources = selected["transcript_sources"]
        recurring_terms = selected["recurring_terms"]
        recurring_human_terms = selected["recurring_human_terms"]
        hook_families = selected["hook_families"]
        structures = selected["structures"]
        proof_seconds = selected["proof_seconds"]
        words_per_second = selected["words_per_second"]
        style_result = selected["style_guide"]
        style_guide = style_result["guide"]
        style_receipt = style_result["receipt"]
        cohort_manifest_path = Path(cohort["manifest_path"]).expanduser().resolve()
        cohort_manifest_payload = json.loads(
            cohort_manifest_path.read_text(encoding="utf-8")
        )
        cohort_manifest_sha256 = canonical_sha256(cohort_manifest_payload)
        variant_moments = self._distinct_source_moments(moments["moments"])
        if variant_index >= len(variant_moments):
            return self._record_failed_attempt(
                code="SCRIPT_VARIANT_INDEX_NOT_AVAILABLE",
                payload=payload,
                detail={
                    "selection_contract": SCRIPT_VARIANT_SELECTION_CONTRACT,
                    "requested_variant_index": variant_index,
                    "available_variant_count": len(variant_moments),
                    "available_variant_indexes": list(range(len(variant_moments))),
                    "reason": (
                        "Only text-distinct, source-bound human moments may become "
                        "script variants. No generated wording is used to fill a gap."
                    ),
                },
            )
        selected_moment = variant_moments[variant_index]
        stakes = str(selected_moment["stakes"])
        claim = (
            f"For {audience_name}, useful {topic} completes one task where the "
            "work already happens instead of adding another app"
        )
        receipt_ids = [receipt["receipt_id"] for receipt in receipts]
        source_material = {
            "trend_id": trend["trend_id"],
            "trend_observed_at": trend["observed_at"],
            "language_query": language_query,
            "observation_keys": sorted(
                str(member["observation_key"]) for member in trend["members"]
            ),
            "transcripts": sorted(
                (str(row["transcript_id"]), str(row["transcript_sha256"]))
                for row in transcript_sources
            ),
            "keyword_signals": keyword_signals,
            "cohort_id": cohort["cohort_id"],
            "cohort_manifest_sha256": cohort_manifest_sha256,
            "style_guide_id": style_guide["guide_id"],
            "style_guide_receipt_id": style_receipt["receipt_id"],
            "style_source_material_sha256": style_guide[
                "source_material_sha256"
            ],
        }
        evidence_sha256 = canonical_sha256(source_material)
        created_at = utc_now()
        brief_id = "brief_" + canonical_sha256({
            "contract": SCRIPT_INTELLIGENCE_BRIEF_CONTRACT,
            "generation_contract": SCRIPT_GENERATION_CONTRACT,
            "audience": audience_name,
            "objective": objective,
            "evidence_sha256": evidence_sha256,
            "variant_selection_contract": SCRIPT_VARIANT_SELECTION_CONTRACT,
            "variant_index": variant_index,
            "source_moment_id": selected_moment["moment_id"],
            "stakes_source_moment_id": selected_moment[
                "stakes_source_moment_id"
            ],
        })[:24]
        brief = {
            "brief_id": brief_id,
            "contract": SCRIPT_INTELLIGENCE_BRIEF_CONTRACT,
            "generation_contract": SCRIPT_GENERATION_CONTRACT,
            "status": "ready",
            "topic": topic,
            "audience": audience_name,
            "objective": objective,
            "database_snapshot": {
                "snapshot_id": ready["snapshot_id"],
                "schema_version": ready["schema_version"],
                "watermark": ready["watermark"],
                "evidence_sha256": evidence_sha256,
            },
            "selection_audit": {
                "contract": TREND_SELECTION_CONTRACT,
                "selected_trend_id": trend["trend_id"],
                "assessed_candidate_count": len(candidate_assessments),
                "semantic_candidate_query_count": 1 if requested_topic else 0,
                "semantic_candidate_source": "accepted_whisper_artifacts",
                "shared_semantic_candidate_count": len(
                    shared_semantic_candidates
                ),
                "candidate_assessments": candidate_assessments,
            },
            "trend": {
                key: trend[key] for key in (
                    "trend_id", "trend_type", "canonical_key", "display_name",
                    "state", "observed_at", "rank", "ranking_contract",
                    "score_is_probability", "prediction", "signals", "evidence",
                    "platform_distribution", "topic_matches",
                    "label_topic_matches", "topic_label_match_priority",
                    "topic_affinity", "topic_affinity_priority",
                )
            },
            "keywords": keyword_signals,
            "language": {
                "query": language_query,
                "cohort_id": cohort["cohort_id"],
                "cohort_contract": cohort["audit"]["contract"],
                "cohort_manifest_path": str(cohort_manifest_path),
                "cohort_manifest_sha256": cohort_manifest_sha256,
                "aggregate_metrics": cohort["aggregate_metrics"],
                "recurring_terms": recurring_terms[:30],
                "recurring_human_terms": recurring_human_terms[:10],
                "top_hook_families": hook_families.most_common(5),
                "top_structures": structures.most_common(8),
                "median_first_proof_seconds": (
                    round(statistics.median(proof_seconds), 3) if proof_seconds else None
                ),
                "median_words_per_second": (
                    round(statistics.median(words_per_second), 3) if words_per_second else None
                ),
                "sources": transcript_sources,
                "style_guide": {
                    "guide_id": style_guide["guide_id"],
                    "receipt_id": style_receipt["receipt_id"],
                    "contract": style_guide["contract"],
                    "platform": style_guide["platform"],
                    "evidence": style_guide["evidence"],
                    "speech": style_guide["speech"],
                    "hooks": style_guide["hooks"],
                    "structure": style_guide["structure"],
                    "delivery": style_guide["delivery"],
                    "rights_and_originality": style_guide[
                        "rights_and_originality"
                    ],
                },
                "relationship_policy": {
                    "exact_trend_member": "proves membership in the selected trend",
                    "keyword_match": "supports wording for a measured keyword, not trend rise",
                    "semantic_precedent": "supports how to say it, not that the trend is rising",
                },
            },
            "human_context": {
                "selected_moment": selected_moment,
                "moment_receipt_id": moments["receipt"]["receipt_id"],
                "source_bound": True,
                "variant_selection": {
                    "contract": SCRIPT_VARIANT_SELECTION_CONTRACT,
                    "variant_index": variant_index,
                    "available_variant_count": len(variant_moments),
                    "selection_basis": "distinct_stored_source_moment_text",
                    "generated_fillers_allowed": False,
                },
            },
            "pacing": {
                "hook_deadline_seconds": 3.5,
                "proof_deadline_seconds": 20.0,
                "maximum_semantic_beat_seconds": 10.0,
                "owned_retention_status": "no_owned_outcomes",
                "note": "No retention curve or causal drop reason is fabricated when owned outcomes are absent.",
            },
            "generation_input": {
                "brief_id": brief_id,
                "trend_id": trend["trend_id"],
                "topic": topic,
                "audience": audience_name,
                "objective": objective,
                "variant_index": variant_index,
                "variant_selection_contract": SCRIPT_VARIANT_SELECTION_CONTRACT,
                "generation_contract": SCRIPT_GENERATION_CONTRACT,
                "style_platform": style_guide["platform"],
                "style_guide_id": style_guide["guide_id"],
                "style_guide_receipt_id": style_receipt["receipt_id"],
                "claim": claim,
                "human_moment": {
                    **selected_moment,
                    "source_moment_receipt_id": moments["receipt"]["receipt_id"],
                },
                "receipt_ids": receipt_ids,
            },
            "originality_policy": (
                "Reuse abstract structure and recurring vocabulary; do not copy "
                "distinctive transcript passages or source footage."
            ),
            "created_at": created_at,
        }
        brief_receipt = self.store.put_receipt(
            "script_intelligence_brief",
            "market_tape",
            brief_id,
            None,
            brief,
        )
        stored = self.store.put_script_brief(brief, brief_receipt["receipt_id"])
        return {**stored, "receipt_id": brief_receipt["receipt_id"]}

    def generate_and_audit(self, payload: dict[str, Any]) -> dict[str, Any]:
        brief_id = str(payload.get("brief_id") or "").strip()
        if not brief_id:
            raise ValueError("brief_id is required")
        forbidden_overrides = sorted(
            key for key in (
                "topic", "trend_id", "audience", "objective", "claim",
                "human_moment", "receipt_ids", "source_receipt_ids",
                "style_platform", "style_guide_id",
                "style_guide_receipt_id",
            )
            if key in payload
        )
        if forbidden_overrides:
            return {
                "status": "rejected",
                "code": "REJECT_SCRIPT_BRIEF_OVERRIDE",
                "brief_id": brief_id,
                "forbidden_fields": forbidden_overrides,
                "reason": (
                    "Trend, language, audience, and evidence IDs are immutable in "
                    "the selected brief. Build a new brief instead of overriding them."
                ),
            }
        brief = self.store.script_brief(brief_id)
        if brief is None:
            return {"status": "rejected", "code": "UNKNOWN_SCRIPT_BRIEF", "brief_id": brief_id}
        if brief.get("status") != "ready":
            return {"status": "rejected", "code": "SCRIPT_BRIEF_NOT_READY", "brief_id": brief_id}
        generation_input = dict(brief["generation_input"])
        if payload.get("owned_proof"):
            generation_input["owned_proof"] = [
                str(value) for value in payload.get("owned_proof") or []
                if str(value).strip()
            ]
        generated = self.scripts.generate(generation_input)
        created_at = utc_now()
        request_sha256 = canonical_sha256({
            "brief_id": brief_id,
            "owned_proof": generation_input.get("owned_proof") or [],
        })
        if generated.get("status") == "rejected":
            workflow_id = "workflow_" + canonical_sha256({
                "brief_id": brief_id,
                "request_sha256": request_sha256,
                "result": generated,
                "created_at": created_at,
            })[:24]
            run = {
                "workflow_id": workflow_id,
                "brief_id": brief_id,
                "script_id": None,
                "state": "rejected",
                "stage_receipts": {},
                "result": generated,
                "created_at": created_at,
            }
            self.store.put_workflow_run(run)
            return {**generated, "workflow_id": workflow_id, "brief_id": brief_id}

        relatability = self.relatability.audit(generated)
        qualitative_relatability = self.ai_relatability.audit(generated)
        style_fit = self.style_guides.audit({
            "script_id": generated["script_id"],
            "style_guide_id": generated["style_guide_id"],
            "style_guide_receipt_id": generated[
                "style_guide_receipt_id"
            ],
            "target_duration_seconds": generated["timeline"][-1]["end"],
        })
        attention = self.attention.script_audit(generated)
        preflight = self.attention.video_preflight(generated)

        from .transcript_bank import TranscriptBank

        bank = TranscriptBank(self.tape.path, self.transcript_storage_root)
        cohort_audit = bank.audit_script_against_cohort(
            script_id=generated["script_id"],
            script_text=generated["text"],
            cohort_manifest_path=brief["language"]["cohort_manifest_path"],
            expected_cohort_id=brief["language"]["cohort_id"],
            expected_cohort_manifest_sha256=brief["language"][
                "cohort_manifest_sha256"
            ],
        )
        cohort_pass = cohort_audit.get("decision") == "PASS_PREDICTED_RELATABILITY"
        cohort_quality_audit = self.store.put_audit(
            "relatability_transcript_cohort",
            generated["script_id"],
            "PASS" if cohort_pass else "REJECT_NOT_RELATABLE",
            float(cohort_audit.get("score") or 0.0),
            {
                "cohort_id": brief["language"]["cohort_id"],
                "expected_cohort_id": cohort_audit.get("expected_cohort_id"),
                "actual_cohort_id": cohort_audit.get("actual_cohort_id"),
                "market_tape_audit_id": cohort_audit.get("audit_id"),
                "script_sha256": cohort_audit.get("script_sha256"),
                "cohort_manifest_sha256": cohort_audit.get("cohort_manifest_sha256"),
                "expected_cohort_manifest_sha256": cohort_audit.get(
                    "expected_cohort_manifest_sha256"
                ),
                "actual_cohort_manifest_sha256": cohort_audit.get(
                    "actual_cohort_manifest_sha256"
                ),
                "cohort_manifest_binding_valid": cohort_audit.get(
                    "cohort_manifest_binding_valid"
                ),
                "findings": cohort_audit.get("findings") or {},
                "input_binding": {
                    "contract": "stored_script_audit_binding_v1",
                    "stored_script_bound": True,
                    "script_id": generated["script_id"],
                    "script_sha256": self.store.script_audit_sha256(generated),
                },
            },
        )
        decisions = {
            "narrative": generated.get("narrative_coherence", {}).get("decision") == "PASS",
            "relatability": relatability["decision"] == "PASS",
            "qualitative_relatability": (
                qualitative_relatability["decision"]
                in {"PASS", NON_AI_PASS_DECISION}
            ),
            "cohort_integrity": cohort_pass,
            "transcript_style": style_fit["decision"] == "PASS",
            "attention": attention["decision"] == "PASS",
            "video_preflight": preflight["decision"] == "PASS",
        }
        approved = all(decisions.values())
        workflow_id = "workflow_" + canonical_sha256({
            "brief_id": brief_id,
            "script_id": generated["script_id"],
            "request_sha256": request_sha256,
            "audit_ids": [
                relatability["audit_id"], qualitative_relatability["audit_id"],
                attention["audit_id"], preflight["audit_id"],
                cohort_quality_audit["audit_id"], style_fit["audit_id"],
            ],
        })[:24]
        stage_receipts = {
            "brief_receipt_id": self.store.script_brief_receipt_id(brief_id),
            "narrative_audit_id": self.store.script_gate_summary(
                generated["script_id"]
            )["latest_audits"].get("narrative_coherence", {}).get("audit_id"),
            "relatability_audit_id": relatability["audit_id"],
            "qualitative_relatability_audit_id": qualitative_relatability[
                "audit_id"
            ],
            "cohort_relatability_audit_id": cohort_quality_audit["audit_id"],
            "transcript_style_audit_id": style_fit["audit_id"],
            "attention_audit_id": attention["audit_id"],
            "video_preflight_audit_id": preflight["audit_id"],
        }
        result = {
            "status": "approved" if approved else "revise",
            "brief_id": brief_id,
            "workflow_id": workflow_id,
            "script": generated,
            "decisions": decisions,
            "audits": {
                "relatability": relatability,
                "qualitative_relatability": qualitative_relatability,
                "transcript_cohort_relatability": cohort_quality_audit,
                "transcript_style": style_fit,
                "attention": attention,
                "video_preflight": preflight,
            },
            "ready_for_render": approved,
            "owned_retention_status": "no_owned_outcomes",
        }
        self.store.put_workflow_run({
            "workflow_id": workflow_id,
            "brief_id": brief_id,
            "script_id": generated["script_id"],
            "state": result["status"],
            "stage_receipts": stage_receipts,
            "result": result,
            "created_at": created_at,
        })
        return result
