"""One-shot consumer for append-only script-language evidence demands."""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path
from typing import Any

from services.content_quality.contracts import (
    SUPPORTED_TRANSCRIPT_AUDIT_CONTRACTS,
)
from services.content_quality.transcript_bank import (
    LEGACY_PAYLOAD_READ_TIMEOUT_SECONDS,
    TranscriptBank,
    canonical_sha256,
    file_sha256_bounded,
    read_legacy_json_payload_bounded,
    topic_terms,
    words,
)

from .collector import MarketTapeCollector
from .config import MarketTapeConfig
from .full_pipeline import DEFAULT_TRANSCRIPT_STORAGE_ROOT, run_full_pipeline
from .store import MarketTapeStore


SCRIPT_LANGUAGE_DEMAND_RUN_CONTRACT = "market_tape_script_language_demand_run_v1"
SCRIPT_LANGUAGE_TARGET_LANGUAGE = "en"


class _TopicOnlyTranscriptBank(TranscriptBank):
    """Keep demand acquisition scoped by language, never trend membership.

    ``run_full_pipeline`` normally resolves an empty ``trend_ids`` sequence to
    matching historical Market Tape trends.  That behavior is useful for an
    explicit trend backfill, but it is wrong for a script-language demand:
    newly discovered topic evidence cannot be required to belong to an older
    immutable trend snapshot.  The originating ``evidence_trend_id`` remains
    in the demand/run receipts as lineage; it is never an eligibility filter.
    """

    def run_backfill(self, **kwargs: Any) -> dict[str, Any]:
        kwargs["trend_ids"] = ()
        return super().run_backfill(**kwargs)


def _bounded_int(value: Any, default: int, *, minimum: int, maximum: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        parsed = default
    return max(minimum, min(maximum, parsed))


def _uses_youtube_performance_discovery(platforms: tuple[str, ...]) -> bool:
    """Select the provider lane that can truthfully exercise the policy."""
    return platforms == ("youtube",)


def _acquisition_query_frontier(
    request_payload: dict[str, Any], topic: str
) -> tuple[str, ...]:
    """Return bounded, topic-preserving queries in deterministic rank order."""
    normalized_topic = " ".join(str(topic or "").split())
    required_terms = set(topic_terms(normalized_topic))
    assessments = [
        item for item in request_payload.get("candidate_assessments") or []
        if isinstance(item, dict)
    ]
    # This frontier exists to close a verified-language coverage deficit, not
    # to restate the trend leaderboard. Prefer queries already supported by a
    # broad exact membership and qualified transcript-candidate surface; use
    # trend rank only after those acquisition signals. This keeps a noisy,
    # high-strength phrase from outranking a well-supported language family.
    assessments.sort(key=lambda item: (
        -_bounded_int(
            item.get("exact_trend_member_count"), 0,
            minimum=0, maximum=10**9,
        ),
        -_bounded_int(
            item.get("qualified_language_candidate_count"), 0,
            minimum=0, maximum=10**9,
        ),
        _bounded_int(item.get("rank"), 10**9, minimum=0, maximum=10**9),
        str(item.get("trend_id") or ""),
    ))
    candidates = [normalized_topic]
    candidates.extend(
        " ".join(str(item.get("language_query") or "").split())
        for item in assessments
    )
    admitted: list[str] = []
    seen: set[str] = set()
    for query in candidates:
        normalized = query.casefold()
        if not query or normalized in seen:
            continue
        if required_terms and not required_terms.issubset(set(topic_terms(query))):
            continue
        seen.add(normalized)
        admitted.append(query)
    return tuple(admitted)


def _next_acquisition_query(
    claim: dict[str, Any],
    request_payload: dict[str, Any],
    topic: str,
    platforms: tuple[str, ...],
    *,
    semantic_events: list[dict[str, Any]] | None = None,
) -> str:
    """Advance a finite query frontier without repeating an attempted scope."""
    requested_scope = tuple(sorted(platforms))
    used: set[str] = set()
    events = (
        list(semantic_events)
        if semantic_events is not None
        else list(claim.get("events") or [])
    )
    for event in events:
        payload = event.get("payload") or {}
        result = payload.get("result") if isinstance(payload, dict) else None
        if not isinstance(result, dict):
            continue
        policy = result.get("policy") or {}
        event_scope = tuple(sorted(
            str(value).strip().casefold()
            for value in policy.get("platforms") or []
            if str(value).strip()
        ))
        if event_scope != requested_scope:
            continue
        query = " ".join(str(result.get("acquisition_query") or topic).split())
        if query:
            used.add(query.casefold())
    return next(
        (
            query for query in _acquisition_query_frontier(request_payload, topic)
            if query.casefold() not in used
        ),
        "",
    )


def _goal_checks(
    snapshot: dict[str, Any], targets: dict[str, Any]
) -> dict[str, dict[str, int | bool]]:
    """Compare one immutable cohort snapshot with the demand's bounded goals."""
    return {
        field: {
            "actual": int(snapshot.get(field) or 0),
            "minimum": _bounded_int(
                targets.get(field), 0, minimum=0, maximum=10**12
            ),
            "pass": int(snapshot.get(field) or 0) >= _bounded_int(
                targets.get(field), 0, minimum=0, maximum=10**12
            ),
        }
        for field in (
            "verified_transcripts", "distinct_creators", "observed_views"
        )
        if field in targets
    }


def _finished_attempt_result(
    demand: dict[str, Any], fallback: dict[str, Any]
) -> dict[str, Any]:
    for event in reversed(list(demand.get("events") or [])):
        if event.get("event_type") in {"completed", "partial", "blocked", "failed"}:
            result = (event.get("payload") or {}).get("result")
            if isinstance(result, dict):
                return result
    return fallback


class ScriptLanguageDemandWorker:
    """Claim and finish at most one demand per call.

    Pipeline completion and evidence-goal completion are deliberately separate.
    A provider can return successfully with zero candidates; that is a terminal
    bounded attempt with ``goal_met=false``, never proof that the cohort exists.
    """

    def __init__(
        self,
        config: MarketTapeConfig,
        store: MarketTapeStore | None = None,
        *,
        collector: MarketTapeCollector | None = None,
        transcript_storage_root: str | Path | None = None,
    ) -> None:
        self.config = config
        self.store = store or MarketTapeStore(config)
        self.collector = collector
        self.transcript_storage_root = Path(
            transcript_storage_root or DEFAULT_TRANSCRIPT_STORAGE_ROOT
        ).expanduser()

    def cohort_snapshot(
        self,
        *,
        topic: str,
        evidence_trend_id: str = "",
        read_timeout_seconds: float = LEGACY_PAYLOAD_READ_TIMEOUT_SECONDS,
    ) -> dict[str, Any]:
        terms = tuple(topic_terms(topic))
        clauses: list[str] = []
        parameters: list[Any] = [
            *sorted(SUPPORTED_TRANSCRIPT_AUDIT_CONTRACTS),
        ]
        for term in terms:
            clauses.append(
                "LOWER(evidence.title || ' ' || evidence.caption || ' ' || "
                "evidence.description) LIKE ?"
            )
            parameters.append(f"%{term}%")
        # The LIKE predicates are an inexpensive prefilter only.  Exact token
        # matching below prevents short terms such as ``ai`` from matching an
        # unrelated substring such as ``chair``.
        topic_filter = (
            f"AND ({' OR '.join(clauses)})" if clauses else "AND 0"
        )
        contract_marks = ",".join(
            "?" for _ in SUPPORTED_TRANSCRIPT_AUDIT_CONTRACTS
        )
        with self.store.connect() as connection:
            rows = connection.execute(
                f"""SELECT artifact.transcript_id, artifact.observation_key,
                           artifact.video_id, video.creator_id,
                           evidence.title, evidence.caption,
                           evidence.description, metric.views,
                           metric.observed_at AS observation_observed_at,
                           metric.source_confidence,
                           evidence.accepted_at,
                           artifact.audio_path, artifact.audio_sha256,
                           artifact.transcript_path, artifact.transcript_sha256,
                           artifact.audit_json,
                           artifact.created_at AS artifact_created_at,
                           artifact.word_count, artifact.segment_count,
                           artifact.whisper_language
                    FROM mt_transcript_artifacts artifact
                    JOIN mt_videos video
                      ON video.video_id = artifact.video_id
                    JOIN mt_accepted_full_evidence_v1 evidence
                      ON evidence.video_id = artifact.video_id
                     AND evidence.observation_key = artifact.observation_key
                    JOIN mt_accepted_metric_observations_v1 metric
                      ON metric.observation_id = evidence.observation_id
                    WHERE json_extract(artifact.audit_json, '$.decision') = 'PASS'
                      AND json_extract(artifact.audit_json, '$.contract')
                          IN ({contract_marks})
                    {topic_filter}
                    ORDER BY metric.observed_at DESC,
                             metric.source_confidence DESC,
                             evidence.accepted_at DESC,
                             artifact.created_at DESC,
                             artifact.word_count DESC,
                             artifact.segment_count DESC,
                             artifact.transcript_id DESC,
                             artifact.observation_key DESC""",
                parameters,
            ).fetchall()
        # Rows are newest accepted observation first, then deterministically
        # ranked by accepted-evidence and artifact quality.  Validation happens
        # before selection so an invalid newest artifact falls back to the next
        # newest valid artifact for that video.  A video can never contribute
        # more than one transcript or one cumulative view counter.
        unique: dict[str, dict[str, Any]] = {}
        minimum_topic_matches = min(2, len(terms))
        rejected = {
            "metadata_topic_mismatch": 0,
            "transcript_language_mismatch": 0,
            "artifact_outside_storage_root": 0,
            "artifact_file_missing": 0,
            "artifact_read_timeout": 0,
            "artifact_payload_invalid": 0,
            "artifact_hash_mismatch": 0,
            "transcript_topic_mismatch": 0,
            "metadata_transcript_topic_match_disjoint": 0,
            "valid_artifact_candidates_not_selected_same_video": 0,
        }
        storage_root = self.transcript_storage_root.resolve()
        for row in rows:
            item = dict(row)
            source_vocabulary = {
                token.lower()
                for token in words(" ".join(
                    str(item.get(field) or "")
                    for field in ("title", "caption", "description")
                ))
            }
            matches = tuple(term for term in terms if term in source_vocabulary)
            if len(matches) < minimum_topic_matches:
                rejected["metadata_topic_mismatch"] += 1
                continue
            if not str(item.get("whisper_language") or "").casefold().startswith(
                SCRIPT_LANGUAGE_TARGET_LANGUAGE
            ):
                rejected["transcript_language_mismatch"] += 1
                continue
            transcript_path = Path(str(item.get("transcript_path") or ""))
            audio_path = Path(str(item.get("audio_path") or ""))
            try:
                transcript_path.resolve().relative_to(storage_root)
                audio_path.resolve().relative_to(storage_root)
            except (OSError, ValueError):
                rejected["artifact_outside_storage_root"] += 1
                continue
            if not transcript_path.is_file() or not audio_path.is_file():
                rejected["artifact_file_missing"] += 1
                continue
            try:
                transcript_payload = read_legacy_json_payload_bounded(
                    transcript_path,
                    timeout_seconds=read_timeout_seconds,
                )
                transcript_hash = canonical_sha256(transcript_payload)
                audio_hash = file_sha256_bounded(
                    audio_path,
                    timeout_seconds=read_timeout_seconds,
                )
                audit = json.loads(str(item.get("audit_json") or "{}"))
                if not isinstance(audit, dict):
                    raise ValueError("artifact audit must be an object")
            except TimeoutError:
                rejected["artifact_read_timeout"] += 1
                continue
            except (OSError, ValueError, json.JSONDecodeError):
                rejected["artifact_payload_invalid"] += 1
                continue
            if (
                transcript_hash != str(item.get("transcript_sha256") or "")
                or audio_hash != str(item.get("audio_sha256") or "")
                or (
                    audit.get("transcript_payload_sha256")
                    and audit.get("transcript_payload_sha256") != transcript_hash
                )
            ):
                rejected["artifact_hash_mismatch"] += 1
                continue
            transcript_vocabulary = {
                token.lower()
                for token in words(str(transcript_payload.get("text") or ""))
            }
            transcript_matches = tuple(
                term for term in terms if term in transcript_vocabulary
            )
            if len(transcript_matches) < minimum_topic_matches:
                rejected["transcript_topic_mismatch"] += 1
                continue
            shared_matches = tuple(
                term for term in matches if term in transcript_matches
            )
            if len(shared_matches) < minimum_topic_matches:
                rejected[
                    "metadata_transcript_topic_match_disjoint"
                ] += 1
                continue
            item["metadata_topic_matches"] = list(matches)
            item["transcript_topic_matches"] = list(transcript_matches)
            item["shared_topic_matches"] = list(shared_matches)
            video_id = str(item["video_id"])
            if video_id in unique:
                rejected[
                    "valid_artifact_candidates_not_selected_same_video"
                ] += 1
                continue
            unique[video_id] = item
        members = list(unique.values())
        creator_ids = sorted({
            str(item["creator_id"]) for item in members if item["creator_id"]
        })
        return {
            "contract": "script_language_quantitative_cohort_snapshot_v1",
            "topic": topic,
            "evidence_trend_id": evidence_trend_id,
            "evidence_trend_id_role": "lineage_only",
            "historical_trend_membership_required": False,
            "matching_basis": (
                "accepted_full_evidence_shared_exact_metadata_and_verified_"
                "artifact_transcript_topic_vocabulary"
            ),
            "minimum_topic_matches": minimum_topic_matches,
            "target_language": SCRIPT_LANGUAGE_TARGET_LANGUAGE,
            "artifact_integrity_required": True,
            "artifact_read_timeout_seconds": max(
                0.05, float(read_timeout_seconds)
            ),
            "source_deduplication_key": ["video_id"],
            "observation_selection_policy": (
                "newest_accepted_observation_then_source_confidence_then_"
                "newest_largest_artifact_with_stable_id_tiebreak"
            ),
            "rejected_candidates": rejected,
            "verified_transcripts": len(members),
            "distinct_creators": len(creator_ids),
            "observed_views": sum(int(item["views"] or 0) for item in members),
            "creator_ids": creator_ids,
            "transcript_ids": sorted(str(item["transcript_id"]) for item in members),
            "observation_keys": sorted(
                str(item["observation_key"]) for item in members
            ),
            "source_observation_lineage": sorted(
                (
                    {
                        "video_id": str(item["video_id"]),
                        "creator_id": str(item["creator_id"]),
                        "observation_key": str(item["observation_key"]),
                        "observed_at": str(item["observation_observed_at"]),
                        "transcript_id": str(item["transcript_id"]),
                        "views": int(item["views"] or 0),
                    }
                    for item in members
                ),
                key=lambda item: (item["video_id"], item["observation_key"]),
            ),
            "qualitative_script_gate_pending": True,
            "qualitative_script_gate_status": "not_run",
        }

    def run_next(
        self,
        *,
        lease_seconds: int = 7200,
        expected_demand_id: str | None = None,
    ) -> dict[str, Any]:
        claim = self.store.claim_next_script_language_demand(
            max(300, min(int(lease_seconds), 86400)),
            expected_demand_id=expected_demand_id,
            source_service="script-language-demand-worker",
            payload={
                "contract": SCRIPT_LANGUAGE_DEMAND_RUN_CONTRACT,
                "one_cycle": True,
                "same_call_retry": False,
            },
        )
        if claim is None:
            return {
                "contract": SCRIPT_LANGUAGE_DEMAND_RUN_CONTRACT,
                "state": "idle",
                "processed": 0,
                "goal_met": False,
            }

        request_payload = dict(
            claim.get("latest_request_payload")
            or claim["events"][0].get("payload")
            or {}
        )
        policy = dict(request_payload.get("acquisition_policy") or {})
        requested_platforms = [
            str(value).strip().casefold()
            for value in (policy.get("platforms") or ["youtube"])
            if str(value).strip()
        ]
        platforms = tuple(
            value for value in dict.fromkeys(requested_platforms)
            if value in self.config.platforms
        )
        topic = str(claim["topic"])
        trend_id = str(claim.get("evidence_trend_id") or "")
        targets = dict(claim.get("targets") or {})
        try:
            before = self.cohort_snapshot(
                topic=topic, evidence_trend_id=trend_id
            )
        except Exception as exc:
            terminal_payload = {
                "contract": SCRIPT_LANGUAGE_DEMAND_RUN_CONTRACT,
                "one_cycle": True,
                "same_call_retry": False,
                "goal_met": False,
                "failure_code": f"COHORT_SNAPSHOT_{type(exc).__name__.upper()}",
                "qualitative_script_gate_pending": True,
                "qualitative_script_gate_status": "not_run",
            }
            finished = self.store.finish_script_language_demand(
                str(claim["demand_id"]), int(claim["attempt_no"]), "failed",
                terminal_payload,
                source_service="script-language-demand-worker",
            )
            result_payload = _finished_attempt_result(
                finished, terminal_payload
            )
            return {
                "contract": SCRIPT_LANGUAGE_DEMAND_RUN_CONTRACT,
                "state": finished["state"], "processed": 1,
                "goal_met": False,
                "demand": finished, "result": result_payload,
            }
        transcript_limit = _bounded_int(
            policy.get("transcript_limit"), 10, minimum=1, maximum=10
        )
        discovery_limit = _bounded_int(
            policy.get("discovery_limit"), 50, minimum=1, maximum=50
        )
        before_goal_checks = _goal_checks(before, targets)
        if before_goal_checks and all(
            check["pass"] for check in before_goal_checks.values()
        ):
            # A queued demand may become stale while another bounded run or the
            # regular scheduler grows the same topic cohort. Claim and close
            # exactly this demand from persisted evidence; do not spend another
            # provider request or Whisper cycle once its goal already exists.
            terminal_payload = {
                "contract": SCRIPT_LANGUAGE_DEMAND_RUN_CONTRACT,
                "one_cycle": True,
                "same_call_retry": False,
                "evidence_trend_id": trend_id,
                "evidence_trend_id_role": "lineage_only",
                "goal_met": True,
                "already_satisfied_before_acquisition": True,
                "goal_checks": before_goal_checks,
                "before": before,
                "after": before,
                "policy": {
                    "platforms": list(platforms),
                    "discovery_limit": discovery_limit,
                    "transcript_limit": transcript_limit,
                    "whisper_model": "base",
                    "excluded_creator_count": len(before["creator_ids"]),
                },
                "pipeline": None,
                "failure_code": "",
                "qualitative_script_gate_pending": True,
                "qualitative_script_gate_status": "not_run",
            }
            finished = self.store.finish_script_language_demand(
                str(claim["demand_id"]),
                int(claim["attempt_no"]),
                "completed",
                terminal_payload,
                source_service="script-language-demand-worker",
                source_receipt_id=(
                    f"demand-run:{claim['demand_id']}:{claim['attempt_no']}"
                ),
            )
            result_payload = _finished_attempt_result(
                finished, terminal_payload
            )
            persisted_goal_met = bool(
                finished["state"] == "completed"
                and result_payload.get("goal_met")
            )
            return {
                "contract": SCRIPT_LANGUAGE_DEMAND_RUN_CONTRACT,
                "state": finished["state"],
                "processed": 1,
                "goal_met": persisted_goal_met,
                "demand": finished,
                "result": result_payload,
            }
        pipeline: dict[str, Any] | None = None
        collection_run_id = ""
        transcript_run_id = ""
        failure_code = ""
        acquisition_history = (
            self.store.script_language_demand_acquisition_history(
                str(claim["demand_id"])
            )
        )
        acquisition_query = _next_acquisition_query(
            claim,
            request_payload,
            topic,
            platforms,
            semantic_events=acquisition_history["events"],
        )
        if not platforms:
            failure_code = "NO_REQUESTED_PLATFORM_ENABLED"
        elif not acquisition_query:
            failure_code = "ACQUISITION_QUERY_FRONTIER_EXHAUSTED"
        else:
            try:
                scoped_config = replace(
                    self.config,
                    max_discovery_items_per_source=min(
                        discovery_limit,
                        self.config.max_discovery_items_per_source,
                    ),
                )
                pipeline = run_full_pipeline(
                    config=scoped_config,
                    store=self.store,
                    collector=self.collector,
                    discovery_mode="discovery",
                    transcript_limit=transcript_limit,
                    transcript_platforms=platforms,
                    transcript_model="base",
                    topic=acquisition_query,
                    # The originating trend is provenance, not an acquisition
                    # filter. Newly discovered topic matches cannot already be
                    # members of a historical trend snapshot, so scoping by
                    # that ID would guarantee a zero-candidate feedback loop.
                    transcript_trend_ids=(),
                    exclude_creator_ids=before["creator_ids"],
                    # The performance-query lane is a YouTube capability.
                    # A multi-platform demand must use the standard exact-topic
                    # API sources once, otherwise its non-YouTube policy would
                    # be recorded but never exercised.
                    performance_discovery=_uses_youtube_performance_discovery(
                        platforms
                    ),
                    transcript_storage_root=self.transcript_storage_root,
                    bank_factory=_TopicOnlyTranscriptBank,
                )
                auto_resolved_trend_ids = list(
                    pipeline.get("transcription", {}).get("trend_ids") or []
                )
                pipeline["transcription"]["trend_ids"] = []
                pipeline["transcription"]["selection_scope"] = (
                    "accepted_full_evidence_exact_topic_vocabulary"
                )
                pipeline["transcription"][
                    "historical_trend_membership_required"
                ] = False
                pipeline["transcription"][
                    "auto_resolved_trend_ids_ignored"
                ] = auto_resolved_trend_ids
                collection_run_id = str(
                    pipeline.get("discovery", {}).get("run_id") or ""
                )
                transcript_run_id = str(
                    pipeline.get("transcription", {}).get("run_id") or ""
                )
            except Exception as exc:
                failure_code = f"PIPELINE_{type(exc).__name__.upper()}"

        try:
            after = self.cohort_snapshot(topic=topic, evidence_trend_id=trend_id)
        except Exception as exc:
            failure_code = (
                failure_code
                or f"COHORT_SNAPSHOT_{type(exc).__name__.upper()}"
            )
            after = before
        goal_checks = _goal_checks(after, targets)
        goal_met = bool(goal_checks) and all(
            check["pass"] for check in goal_checks.values()
        )
        passing_added = int(
            (pipeline or {}).get("transcription", {}).get(
                "passing_artifact_count", 0
            ) or 0
        )
        discovered = int(
            (pipeline or {}).get("discovery", {}).get("videos_discovered", 0)
            or 0
        )
        discovery_receipts = list(
            (pipeline or {}).get("discovery", {}).get("receipts") or []
        )
        provider_blocked = bool(discovery_receipts) and all(
            str(receipt.get("state") or "").startswith("blocked_")
            or str(receipt.get("error_code") or "") in {
                "circuit_open", "credential_missing",
                "request_budget_exhausted", "provider_rate_limited",
                "provider_auth_or_quota",
            }
            for receipt in discovery_receipts
        )
        if failure_code:
            terminal_type = "failed"
        elif pipeline and pipeline.get("state") == "failed":
            terminal_type = "blocked" if provider_blocked else "failed"
        elif goal_met:
            terminal_type = "completed"
        elif passing_added or discovered:
            terminal_type = "partial"
        else:
            terminal_type = "blocked"
        terminal_payload = {
            "contract": SCRIPT_LANGUAGE_DEMAND_RUN_CONTRACT,
            "one_cycle": True,
            "same_call_retry": False,
            "evidence_trend_id": trend_id,
            "evidence_trend_id_role": "lineage_only",
            "goal_met": goal_met,
            "goal_checks": goal_checks,
            "before": before,
            "after": after,
            "acquisition_query": acquisition_query,
            "acquisition_history": {
                "contract": acquisition_history["contract"],
                "semantic_key": acquisition_history["semantic_key"],
                "demand_generation_count": len(
                    acquisition_history["demand_ids"]
                ),
                "terminal_event_count": len(
                    acquisition_history["events"]
                ),
            },
            "policy": {
                "platforms": list(platforms),
                "discovery_limit": discovery_limit,
                "transcript_limit": transcript_limit,
                "whisper_model": "base",
                "excluded_creator_count": len(before["creator_ids"]),
            },
            "pipeline": ({
                "state": pipeline.get("state"),
                "discovery_state": pipeline.get("discovery", {}).get("state"),
                "discovery_lane": pipeline.get("discovery", {}).get(
                    "scope", {}
                ).get("lane"),
                "discovery_error_code": pipeline.get("discovery", {}).get(
                    "error_detail"
                ),
                "videos_discovered": discovered,
                "transcription_status": pipeline.get("transcription", {}).get("status"),
                "trend_ids": list(
                    pipeline.get("transcription", {}).get("trend_ids") or []
                ),
                "selection_scope": pipeline.get("transcription", {}).get(
                    "selection_scope"
                ),
                "historical_trend_membership_required": bool(
                    pipeline.get("transcription", {}).get(
                        "historical_trend_membership_required", False
                    )
                ),
                "auto_resolved_trend_ids_ignored": list(
                    pipeline.get("transcription", {}).get(
                        "auto_resolved_trend_ids_ignored"
                    ) or []
                ),
                "candidate_count": int(
                    pipeline.get("transcription", {}).get("candidate_count", 0)
                    or 0
                ),
                "artifact_count": int(
                    pipeline.get("transcription", {}).get("artifact_count", 0)
                    or 0
                ),
                "passing_artifact_count": passing_added,
                "failure_count": int(
                    pipeline.get("transcription", {}).get("failure_count", 0)
                    or 0
                ),
            } if pipeline else None),
            "failure_code": failure_code,
            "qualitative_script_gate_pending": True,
            "qualitative_script_gate_status": "not_run",
        }
        finished = self.store.finish_script_language_demand(
            str(claim["demand_id"]),
            int(claim["attempt_no"]),
            terminal_type,
            terminal_payload,
            source_service="script-language-demand-worker",
            source_receipt_id=(
                f"demand-run:{claim['demand_id']}:{claim['attempt_no']}"
            ),
            collection_run_id=collection_run_id,
            transcript_run_id=transcript_run_id,
        )
        result_payload = _finished_attempt_result(finished, terminal_payload)
        persisted_goal_met = bool(
            finished["state"] == "completed"
            and result_payload.get("goal_met")
        )
        return {
            "contract": SCRIPT_LANGUAGE_DEMAND_RUN_CONTRACT,
            "state": finished["state"],
            "processed": 1,
            "goal_met": persisted_goal_met,
            "demand": finished,
            "result": result_payload,
        }


__all__ = ["SCRIPT_LANGUAGE_DEMAND_RUN_CONTRACT", "ScriptLanguageDemandWorker"]
