"""Provider-free rapid-breakout detection and audited generation handoff.

The detector consumes only canonical Market Tape trend observations.  It
starts evidence acquisition and semantic review, but it never approves a
topic, calls an AI provider, renders, or publishes content.
"""

from __future__ import annotations

import json
import sqlite3
from collections import Counter
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING, Any, Dict, Mapping, Optional, Sequence

from .models import isoformat, stable_hash, utc_now
from .semantic import (
    SIGNAL_TYPES,
    SemanticContractError,
    SemanticTopicService,
)
from .store import (
    OBSERVATION_QUALITY_CONTRACT,
    SCRIPT_LANGUAGE_DEMAND_CONTRACT,
    TREND_INDEX_VERSION,
    MarketTapeStore,
    _opportunity_exclusion_reason,
    rapid_trend_trigger_sync_payload,
)

if TYPE_CHECKING:  # pragma: no cover
    from .config import MarketTapeConfig


RAPID_TREND_POLICY_CONTRACT = "market_tape_rapid_trend_policy_v1"
RAPID_TREND_POLICY_VERSION = "rapid-breakout-crossing-v1"
RAPID_TREND_TRIGGER_CONTRACT = "market_tape_rapid_trend_trigger_v1"
RAPID_TREND_EVIDENCE_CONTRACT = "market_tape_rapid_trend_evidence_v1"
RAPID_TREND_EVENT_CONTRACT = "market_tape_rapid_trend_trigger_event_v1"
RAPID_TREND_LIST_CONTRACT = "market_tape_rapid_trend_trigger_list_v1"
RAPID_TREND_EVALUATION_CONTRACT = "market_tape_rapid_trend_evaluation_v1"
RAPID_TREND_SCRIPT_REQUEST_CONTRACT = "rapid_trend_foundry_script_request_v1"
RAPID_TREND_SCRIPT_RESPONSE_CONTRACT = (
    "rapid_trend_foundry_script_request_response_v1"
)

_BREAKOUT_STATE = "breakout"
_MAX_FAILURE_ATTEMPTS_PER_STAGE = 3
_SNAPSHOT_FIELDS = (
    "trend_observation_id",
    "observed_at",
    "videos_total",
    "videos_new_1h",
    "creators_total",
    "creators_new_1h",
    "platforms_total",
    "top1_concentration",
    "views_total",
    "likes_total",
    "comments_total",
    "shares_total",
    "views_new_1h",
    "counter_delta_videos",
    "activity_coverage",
    "momentum",
    "acceleration",
    "relative_strength",
    "saturation",
    "trend_strength",
    "index_version",
    "observation_quality_contract",
    "state",
)
_SEMANTIC_METRIC_FIELDS = (
    "videos_total",
    "creators_total",
    "platforms_total",
    "views_total",
    "likes_total",
    "comments_total",
    "shares_total",
    "trend_strength",
)


def _canonical_json(value: Any) -> str:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        default=str,
    )


def _as_utc(value: Any) -> datetime:
    if isinstance(value, datetime):
        parsed = value
    else:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _bounded_int(value: Any, minimum: int, maximum: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        parsed = minimum
    return max(minimum, min(maximum, parsed))


def _bounded_float(value: Any, minimum: float, maximum: float) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        parsed = minimum
    return max(minimum, min(maximum, parsed))


class RapidTrendTriggerService:
    """Detect one bounded set of trustworthy breakout crossings per call."""

    def __init__(
        self,
        config: "MarketTapeConfig",
        store: MarketTapeStore | None = None,
    ) -> None:
        self.config = config
        self.store = store or MarketTapeStore(config)
        self.semantic = SemanticTopicService(self.store)

    def policy(self) -> Dict[str, Any]:
        policy = {
            "contract": RAPID_TREND_POLICY_CONTRACT,
            "policy_version": RAPID_TREND_POLICY_VERSION,
            "crossing": {
                "from_state": "non_breakout",
                "to_state": _BREAKOUT_STATE,
                "maximum_observation_age_seconds": _bounded_int(
                    self.config.rapid_trend_max_observation_age_seconds,
                    60,
                    86400,
                ),
                "maximum_crossing_window_seconds": _bounded_int(
                    self.config.rapid_trend_max_crossing_window_seconds,
                    60,
                    86400,
                ),
                "minimum_relative_strength": 3.0,
                "minimum_acceleration_exclusive": 0.0,
                "minimum_creators": 10,
                "minimum_platforms": 2,
                "maximum_top1_concentration": 0.60,
                "minimum_videos": _bounded_int(
                    self.config.rapid_trend_min_videos, 1, 10000
                ),
                "minimum_measured_videos": _bounded_int(
                    self.config.rapid_trend_min_measured_videos, 1, 10000
                ),
                "minimum_activity_coverage": _bounded_float(
                    self.config.rapid_trend_min_activity_coverage, 0.0, 1.0
                ),
                "minimum_views_new_1h": _bounded_int(
                    self.config.rapid_trend_min_views_new_1h, 1, 10**12
                ),
                "maximum_saturation": _bounded_float(
                    self.config.rapid_trend_max_saturation, 0.0, 1.0
                ),
                "index_version": TREND_INDEX_VERSION,
                "observation_quality_contract": (
                    OBSERVATION_QUALITY_CONTRACT
                ),
            },
            "bounds": {
                "maximum_new_triggers_per_cycle": _bounded_int(
                    self.config.rapid_trend_max_per_cycle, 1, 100
                ),
                "maximum_new_triggers_per_utc_day": _bounded_int(
                    self.config.rapid_trend_max_per_day, 1, 1000
                ),
                "generation_ttl_seconds": _bounded_int(
                    self.config.rapid_trend_generation_ttl_seconds,
                    300,
                    604800,
                ),
            },
            "evidence_demand": {
                "verified_transcripts": _bounded_int(
                    self.config.rapid_trend_demand_verified_transcripts,
                    1,
                    100,
                ),
                "distinct_creators": _bounded_int(
                    self.config.rapid_trend_demand_distinct_creators,
                    1,
                    100,
                ),
                "observed_views": _bounded_int(
                    self.config.rapid_trend_demand_observed_views,
                    1,
                    10**12,
                ),
                "audience": " ".join(self.config.rapid_trend_audience.split()),
                "objective": " ".join(self.config.rapid_trend_objective.split()),
                "cycles": 1,
                "same_call_retry": False,
            },
            "automatic_actions": {
                "semantic_signal_materialization": True,
                "evidence_demand_enqueue": True,
                "semantic_approval": False,
                "ai_script_generation": False,
                "video_render": False,
                "publish": False,
            },
        }
        return {**policy, "policy_sha256": stable_hash(policy)}

    def evaluate(
        self,
        *,
        source_run_id: str = "",
        as_of: Optional[datetime] = None,
        trend_ids: Optional[Sequence[str]] = None,
    ) -> Dict[str, Any]:
        """Evaluate current observations without collecting or calling providers."""

        evaluated_at = _as_utc(as_of or utc_now())
        policy = self.policy()
        if not self.config.rapid_trend_trigger_enabled:
            return {
                "status": "ok",
                "contract": RAPID_TREND_EVALUATION_CONTRACT,
                "state": "disabled",
                "evaluated_at": isoformat(evaluated_at),
                "provider_calls_made": 0,
                "policy": policy,
                "examined": 0,
                "eligible": 0,
                "created": 0,
                "idempotent": 0,
                "triggers": [],
                "suppressed_by_reason": {},
            }

        candidates, suppressed, examined = self._eligible_crossings(
            evaluated_at, policy, trend_ids
        )
        maximum_cycle = int(policy["bounds"]["maximum_new_triggers_per_cycle"])
        maximum_day = int(policy["bounds"]["maximum_new_triggers_per_utc_day"])
        created = 0
        duplicates = 0
        selected_ids: list[str] = []
        with self.store.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            day_count = int(connection.execute(
                """SELECT COUNT(*) FROM mt_rapid_trend_triggers
                   WHERE substr(detected_at, 1, 10) = ?""",
                (evaluated_at.date().isoformat(),),
            ).fetchone()[0])
            for candidate in candidates:
                identity = {
                    "contract": RAPID_TREND_TRIGGER_CONTRACT,
                    "policy_sha256": policy["policy_sha256"],
                    "trend_id": candidate["trend_id"],
                    "baseline_trend_observation_id": candidate["baseline"][
                        "trend_observation_id"
                    ],
                    "trigger_trend_observation_id": candidate["current"][
                        "trend_observation_id"
                    ],
                }
                trigger_id = "rapid-trend-trigger:" + stable_hash(identity)
                existing = connection.execute(
                    "SELECT trigger_id FROM mt_rapid_trend_triggers WHERE trigger_id = ?",
                    (trigger_id,),
                ).fetchone()
                if existing is not None:
                    duplicates += 1
                    selected_ids.append(trigger_id)
                    continue
                if created >= maximum_cycle:
                    suppressed["cycle_trigger_cap"] += 1
                    continue
                if day_count + created >= maximum_day:
                    suppressed["daily_trigger_cap"] += 1
                    continue
                detected_at = isoformat(evaluated_at)
                expires_at = isoformat(
                    evaluated_at
                    + timedelta(seconds=int(policy["bounds"]["generation_ttl_seconds"]))
                )
                evidence = self._trigger_evidence(candidate, policy)
                evidence_sha = stable_hash(evidence)
                source_receipt_id = (
                    "market-tape-trend-observation:"
                    + str(candidate["current"]["trend_observation_id"])
                )
                trigger_core = {
                    **identity,
                    "trigger_id": trigger_id,
                    "policy_version": RAPID_TREND_POLICY_VERSION,
                    "source_run_id": " ".join(str(source_run_id or "").split())[:500],
                    "source_receipt_id": source_receipt_id,
                    "evidence_sha256": evidence_sha,
                    "evidence": evidence,
                    "detected_at": detected_at,
                    "expires_at": expires_at,
                }
                trigger_sha = stable_hash(trigger_core)
                cursor = connection.execute(
                    """INSERT INTO mt_rapid_trend_triggers(
                           trigger_id, contract, trigger_sha256,
                           policy_version, policy_sha256, trend_id,
                           baseline_trend_observation_id,
                           trigger_trend_observation_id, source_run_id,
                           source_receipt_id, evidence_sha256, evidence_json,
                           detected_at, expires_at
                       ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                       ON CONFLICT(trigger_id) DO NOTHING""",
                    (
                        trigger_id,
                        RAPID_TREND_TRIGGER_CONTRACT,
                        trigger_sha,
                        RAPID_TREND_POLICY_VERSION,
                        policy["policy_sha256"],
                        candidate["trend_id"],
                        candidate["baseline"]["trend_observation_id"],
                        candidate["current"]["trend_observation_id"],
                        trigger_core["source_run_id"],
                        source_receipt_id,
                        evidence_sha,
                        _canonical_json(evidence),
                        detected_at,
                        expires_at,
                    ),
                )
                if cursor.rowcount != 1:
                    duplicates += 1
                    selected_ids.append(trigger_id)
                    continue
                trigger_row = connection.execute(
                    "SELECT * FROM mt_rapid_trend_triggers WHERE trigger_id = ?",
                    (trigger_id,),
                ).fetchone()
                if trigger_row is None:  # pragma: no cover - transaction invariant
                    raise RuntimeError("rapid trend trigger insert was not durable")
                self._enqueue_outbox(
                    connection,
                    "rapid_trend_trigger",
                    trigger_id,
                    rapid_trend_trigger_sync_payload(trigger_row),
                    created_at=detected_at,
                )
                created += 1
                selected_ids.append(trigger_id)
                self._append_event(
                    connection,
                    trigger_id=trigger_id,
                    event_type="detected",
                    source_service="market-tape-rapid-trend",
                    source_receipt_id=source_receipt_id,
                    payload={
                        "contract": RAPID_TREND_EVENT_CONTRACT,
                        "trigger_id": trigger_id,
                        "trigger_sha256": trigger_sha,
                        "evidence_sha256": evidence_sha,
                        "policy_sha256": policy["policy_sha256"],
                        "provider_calls_made": 0,
                    },
                    created_at=detected_at,
                )

        processed = [self._complete_provider_free_start(trigger_id) for trigger_id in selected_ids]
        return {
            "status": "ok",
            "contract": RAPID_TREND_EVALUATION_CONTRACT,
            "state": "completed",
            "evaluated_at": isoformat(evaluated_at),
            "provider_calls_made": 0,
            "policy": policy,
            "examined": examined,
            "eligible": len(candidates),
            "created": created,
            "idempotent": duplicates,
            "triggers": processed,
            "suppressed_by_reason": dict(sorted(suppressed.items())),
        }

    def list_triggers(
        self,
        *,
        limit: int = 100,
        state: Optional[str] = None,
    ) -> Dict[str, Any]:
        bounded = min(500, max(1, int(limit)))
        with self.store.connect() as connection:
            rows = [dict(row) for row in connection.execute(
                """SELECT * FROM mt_rapid_trend_triggers
                   ORDER BY detected_at DESC, trigger_id DESC LIMIT ?""",
                (500 if state else bounded,),
            ).fetchall()]
            triggers = [self._decode_trigger(connection, row) for row in rows]
        if state:
            triggers = [row for row in triggers if row["state"] == state][:bounded]
        return {
            "status": "ok",
            "contract": RAPID_TREND_LIST_CONTRACT,
            "count": len(triggers),
            "limit": bounded,
            "state_filter": state or None,
            "triggers": triggers,
        }

    def get_trigger(self, trigger_id: str) -> Optional[Dict[str, Any]]:
        """Return one immutable trigger plus its append-only event history."""

        canonical_id = " ".join(str(trigger_id or "").split())
        return self._trigger(canonical_id) if canonical_id else None

    def script_request(
        self,
        trigger_id: str,
        *,
        as_of: Optional[datetime] = None,
    ) -> tuple[Dict[str, Any], int]:
        canonical_id = " ".join(str(trigger_id or "").split())
        now = _as_utc(as_of or utc_now())
        with self.store.connect() as connection:
            row = connection.execute(
                "SELECT * FROM mt_rapid_trend_triggers WHERE trigger_id = ?",
                (canonical_id,),
            ).fetchone()
            if row is None:
                return ({
                    "status": "error",
                    "contract": RAPID_TREND_SCRIPT_RESPONSE_CONTRACT,
                    "code": "RAPID_TREND_TRIGGER_NOT_FOUND",
                    "trigger_id": canonical_id,
                    "generation_authorized": False,
                    "blockers": ["trigger_not_found"],
                    "script_request": None,
                }, 404)
            trigger = self._decode_trigger(connection, dict(row))
            signal_id = str(
                ((trigger.get("semantic_signal") or {}).get("signal_id")) or ""
            )
            selection_ids = [
                str(item["selection_id"])
                for item in connection.execute(
                    """SELECT selection.selection_id
                       FROM mt_atomic_topic_selection_sources source
                       JOIN mt_atomic_topic_selections selection
                         ON selection.selection_id = source.selection_id
                       WHERE source.signal_id = ?
                       ORDER BY selection.reviewed_at DESC,
                                selection.selection_id DESC""",
                    (signal_id,),
                ).fetchall()
            ] if signal_id else []

        blockers: list[str] = []
        if now >= _as_utc(trigger["expires_at"]):
            blockers.append("trigger_expired")
        if not signal_id:
            blockers.append("semantic_signal_pending")
        if signal_id and not selection_ids:
            blockers.append("approved_atomic_selection_pending")

        handoff: Optional[Dict[str, Any]] = None
        selection_id = ""
        if not blockers:
            for candidate_selection_id in selection_ids:
                try:
                    candidate = self.semantic.generation_handoff(
                        candidate_selection_id
                    )
                except SemanticContractError:
                    continue
                handoff = candidate
                selection_id = candidate_selection_id
                break
            if handoff is None:
                blockers.append("semantic_generation_handoff_not_ready")

        if blockers:
            return ({
                "status": "blocked",
                "contract": RAPID_TREND_SCRIPT_RESPONSE_CONTRACT,
                "trigger_id": canonical_id,
                "trigger_sha256": trigger["trigger_sha256"],
                "generation_authorized": False,
                "blockers": blockers,
                "script_request": None,
            }, 409)

        assert handoff is not None  # guarded above
        request_core = {
            "contract": RAPID_TREND_SCRIPT_REQUEST_CONTRACT,
            "trigger": {
                key: trigger[key]
                for key in (
                    "trigger_id",
                    "trigger_sha256",
                    "policy_sha256",
                    "evidence_sha256",
                    "detected_at",
                    "expires_at",
                )
            },
            "semantic": {
                "signal_id": signal_id,
                "atomic_selection_id": selection_id,
                "generation_handoff_contract": handoff["contract"],
                "plan_request_base": handoff["plan_request_base"],
            },
            "generation_policy": {
                "script": True,
                "video": True,
                "publish": False,
            },
            "requested_at": trigger["detected_at"],
        }
        request_sha = stable_hash(request_core)
        script_request = {
            **request_core,
            "request_id": "rapid-trend-script-request:" + request_sha,
            "request_sha256": request_sha,
        }
        return ({
            "status": "ok",
            "contract": RAPID_TREND_SCRIPT_RESPONSE_CONTRACT,
            "trigger_id": canonical_id,
            "trigger_sha256": trigger["trigger_sha256"],
            "generation_authorized": True,
            "blockers": [],
            "script_request": script_request,
        }, 200)

    def _eligible_crossings(
        self,
        evaluated_at: datetime,
        policy: Mapping[str, Any],
        trend_ids: Optional[Sequence[str]],
    ) -> tuple[list[Dict[str, Any]], Counter[str], int]:
        crossing = policy["crossing"]
        clauses = [
            "observation.observed_at <= ?",
        ]
        parameters: list[Any] = [
            isoformat(evaluated_at),
        ]
        canonical_trend_ids = sorted({
            str(value).strip() for value in (trend_ids or []) if str(value).strip()
        })
        if canonical_trend_ids:
            clauses.append(
                "trend.trend_id IN (" + ",".join("?" for _ in canonical_trend_ids) + ")"
            )
            parameters.extend(canonical_trend_ids)
        with self.store.connect() as connection:
            rows = [dict(row) for row in connection.execute(
                f"""WITH ranked AS (
                         SELECT trend.trend_id, trend.trend_type,
                                trend.display_name,
                                observation.*,
                                ROW_NUMBER() OVER (
                                    PARTITION BY trend.trend_id
                                    ORDER BY observation.observed_at DESC,
                                             observation.trend_observation_id DESC
                                ) AS row_number
                         FROM mt_trends trend
                         JOIN mt_trend_observations observation
                           ON observation.trend_id = trend.trend_id
                         WHERE {' AND '.join(clauses)}
                     )
                     SELECT * FROM ranked WHERE row_number = 1
                     ORDER BY acceleration DESC, relative_strength DESC,
                              views_new_1h DESC, trend_id ASC""",
                parameters,
            ).fetchall()]
            output: list[Dict[str, Any]] = []
            suppressed: Counter[str] = Counter()
            for current in rows:
                reasons = self._current_exclusion_reasons(
                    current, evaluated_at, crossing
                )
                if reasons:
                    suppressed.update(reasons)
                    continue
                prior_row = connection.execute(
                    """SELECT * FROM mt_trend_observations
                       WHERE trend_id = ?
                         AND (
                             observed_at < ? OR (
                                 observed_at = ?
                                 AND trend_observation_id < ?
                             )
                         )
                       ORDER BY observed_at DESC,
                                trend_observation_id DESC LIMIT 1""",
                    (
                        current["trend_id"],
                        current["observed_at"],
                        current["observed_at"],
                        current["trend_observation_id"],
                    ),
                ).fetchone()
                if prior_row is None:
                    suppressed["baseline_observation_missing"] += 1
                    continue
                prior = dict(prior_row)
                if (
                    str(prior["index_version"]) != TREND_INDEX_VERSION
                    or str(prior["observation_quality_contract"])
                    != OBSERVATION_QUALITY_CONTRACT
                ):
                    suppressed["baseline_observation_not_accepted"] += 1
                    continue
                elapsed = (
                    _as_utc(current["observed_at"])
                    - _as_utc(prior["observed_at"])
                ).total_seconds()
                if elapsed <= 0:
                    suppressed["non_positive_crossing_window"] += 1
                    continue
                if elapsed > int(crossing["maximum_crossing_window_seconds"]):
                    suppressed["crossing_window_too_old"] += 1
                    continue
                if str(prior["state"]).casefold() == _BREAKOUT_STATE:
                    suppressed["already_breakout"] += 1
                    continue
                output.append({
                    "trend_id": str(current["trend_id"]),
                    "trend_type": str(current["trend_type"]),
                    "display_name": str(current["display_name"]),
                    "window_seconds": elapsed,
                    "baseline": {key: prior[key] for key in _SNAPSHOT_FIELDS},
                    "current": {key: current[key] for key in _SNAPSHOT_FIELDS},
                })
        return output, suppressed, len(rows)

    def _current_exclusion_reasons(
        self,
        row: Dict[str, Any],
        evaluated_at: datetime,
        crossing: Mapping[str, Any],
    ) -> list[str]:
        reasons: list[str] = []
        if str(row["index_version"]) != TREND_INDEX_VERSION:
            reasons.append("stale_index_version")
        if (
            str(row["observation_quality_contract"])
            != OBSERVATION_QUALITY_CONTRACT
        ):
            reasons.append("unaccepted_observation_quality")
        if str(row["state"]).casefold() != _BREAKOUT_STATE:
            reasons.append("not_breakout")
        if (
            evaluated_at - _as_utc(row["observed_at"])
        ).total_seconds() > int(crossing["maximum_observation_age_seconds"]):
            reasons.append("stale_current_observation")
        opportunity_reason = _opportunity_exclusion_reason(
            row,
            saturation_ceiling=float(crossing["maximum_saturation"]),
            minimum_videos=int(crossing["minimum_videos"]),
            minimum_measured_videos=int(crossing["minimum_measured_videos"]),
        )
        if opportunity_reason:
            reasons.append(opportunity_reason)
        if float(row["relative_strength"]) < float(
            crossing["minimum_relative_strength"]
        ):
            reasons.append("relative_strength_below_breakout_floor")
        if float(row["acceleration"]) <= float(
            crossing["minimum_acceleration_exclusive"]
        ):
            reasons.append("non_positive_acceleration")
        if int(row["creators_total"]) < int(crossing["minimum_creators"]):
            reasons.append("insufficient_creator_breadth")
        if int(row["platforms_total"]) < int(crossing["minimum_platforms"]):
            reasons.append("insufficient_cross_platform_evidence")
        if float(row["top1_concentration"]) > float(
            crossing["maximum_top1_concentration"]
        ):
            reasons.append("excessive_top_creator_concentration")
        if float(row["activity_coverage"]) < float(
            crossing["minimum_activity_coverage"]
        ):
            reasons.append("insufficient_activity_coverage")
        if int(row["views_new_1h"]) < int(crossing["minimum_views_new_1h"]):
            reasons.append("insufficient_recent_views")
        return sorted(set(reasons))

    def _trigger_evidence(
        self,
        candidate: Mapping[str, Any],
        policy: Mapping[str, Any],
    ) -> Dict[str, Any]:
        baseline = dict(candidate["baseline"])
        current = dict(candidate["current"])
        return {
            "contract": RAPID_TREND_EVIDENCE_CONTRACT,
            "policy": dict(policy),
            "trend": {
                "trend_id": candidate["trend_id"],
                "trend_type": candidate["trend_type"],
                "display_name": candidate["display_name"],
            },
            "crossing": {
                "window_seconds": candidate["window_seconds"],
                "baseline_state": baseline["state"],
                "trigger_state": current["state"],
                "trend_strength_delta": (
                    float(current["trend_strength"])
                    - float(baseline["trend_strength"])
                ),
                "relative_strength_delta": (
                    float(current["relative_strength"])
                    - float(baseline["relative_strength"])
                ),
                "acceleration_delta": (
                    float(current["acceleration"])
                    - float(baseline["acceleration"])
                ),
            },
            "baseline_observation": baseline,
            "trigger_observation": current,
            "raw_source_content_included": False,
            "provider_calls_made": 0,
            "generation_authorized": False,
        }

    def _complete_provider_free_start(self, trigger_id: str) -> Dict[str, Any]:
        trigger = self._trigger(trigger_id)
        if trigger is None:  # pragma: no cover - transaction invariant
            raise RuntimeError("rapid trend trigger was not durable")
        events = {event["event_type"] for event in trigger["events"]}

        if "semantic_materialized" not in events:
            graph_id = self._latest_graph_version_id()
            if graph_id:
                try:
                    trigger_observation = trigger["evidence"][
                        "trigger_observation"
                    ]
                    evidence = {
                        "contract": RAPID_TREND_EVIDENCE_CONTRACT,
                        "rapid_trend_trigger_id": trigger_id,
                        "trigger_sha256": trigger["trigger_sha256"],
                        "policy_sha256": trigger["policy_sha256"],
                        "evidence_sha256": trigger["evidence_sha256"],
                        "trend": trigger["evidence"]["trend"],
                        "crossing": trigger["evidence"]["crossing"],
                        "trigger_observation": trigger_observation,
                        "metrics": {
                            key: trigger_observation[key]
                            for key in _SEMANTIC_METRIC_FIELDS
                        },
                        "generation_authorized": False,
                        "raw_source_content_included": False,
                    }
                    trend_type = str(trigger["evidence"]["trend"]["trend_type"])
                    signal = self.semantic.ingest_signal({
                        "graph_version_id": graph_id,
                        "signal_type": trend_type if trend_type in SIGNAL_TYPES else "other",
                        "source_kind": "market_tape_trend",
                        "source_entity_id": trigger["trend_id"],
                        "source_trend_id": trigger["trend_id"],
                        "source_observed_at": trigger["evidence"][
                            "trigger_observation"
                        ]["observed_at"],
                        "signal_text": trigger["evidence"]["trend"]["display_name"],
                        "source_receipt_id": trigger_id,
                        "evidence": evidence,
                    })
                    self._record_event(
                        trigger_id,
                        "semantic_materialized",
                        {
                            "contract": RAPID_TREND_EVENT_CONTRACT,
                            "graph_version_id": graph_id,
                            "signal_id": signal["signal_id"],
                            "signal_evidence_sha256": signal["evidence_sha256"],
                            "idempotent": signal["idempotent"],
                            "generation_authorized": False,
                        },
                    )
                except (SemanticContractError, RuntimeError, sqlite3.Error) as exc:
                    self._record_stage_failure(trigger_id, "semantic", exc)
            else:
                self._record_event(
                    trigger_id,
                    "blocked",
                    {
                        "contract": RAPID_TREND_EVENT_CONTRACT,
                        "stage": "semantic",
                        "code": "semantic_graph_unavailable",
                        "retryable": True,
                    },
                )

        trigger = self._trigger(trigger_id) or trigger
        events = {event["event_type"] for event in trigger["events"]}
        if "evidence_demand_enqueued" not in events:
            try:
                demand_policy = self.policy()["evidence_demand"]
                demand = self.store.enqueue_script_language_demand({
                    "contract": SCRIPT_LANGUAGE_DEMAND_CONTRACT,
                    "source_service": "market-tape-rapid-trend",
                    "source_receipt_id": trigger_id,
                    "topic": trigger["evidence"]["trend"]["display_name"],
                    "audience": demand_policy["audience"],
                    "objective": demand_policy["objective"],
                    "evidence_trend_id": trigger["trend_id"],
                    "snapshot_id": trigger_id,
                    "targets": {
                        "verified_transcripts": demand_policy[
                            "verified_transcripts"
                        ],
                        "distinct_creators": demand_policy["distinct_creators"],
                        "observed_views": demand_policy["observed_views"],
                    },
                    "acquisition_policy": {
                        "cycles": 1,
                        "platforms": ["youtube"],
                        "discovery_limit": 50,
                        "transcript_limit": 6,
                        "whisper_model": "base",
                        "creator_diverse": True,
                        "same_call_retry": False,
                    },
                    "rapid_trend_trigger_id": trigger_id,
                    "rapid_trend_trigger_sha256": trigger["trigger_sha256"],
                    "rapid_trend_evidence_sha256": trigger["evidence_sha256"],
                })
                self._record_event(
                    trigger_id,
                    "evidence_demand_enqueued",
                    {
                        "contract": RAPID_TREND_EVENT_CONTRACT,
                        "demand_id": demand["demand_id"],
                        "demand_state": demand["state"],
                        "snapshot_id": trigger_id,
                        "idempotent": demand["idempotent"],
                        "provider_calls_made": 0,
                    },
                )
            except (TypeError, ValueError, RuntimeError, sqlite3.Error) as exc:
                self._record_stage_failure(trigger_id, "evidence_demand", exc)
        return self._trigger(trigger_id) or trigger

    def _latest_graph_version_id(self) -> str:
        with self.store.connect() as connection:
            row = connection.execute(
                """SELECT graph_version_id FROM mt_topic_graph_versions
                   ORDER BY imported_at DESC, graph_version_id DESC LIMIT 1"""
            ).fetchone()
        return str(row[0]) if row is not None else ""

    def _record_stage_failure(
        self, trigger_id: str, stage: str, error: Exception
    ) -> None:
        with self.store.connect() as connection:
            existing = int(connection.execute(
                """SELECT COUNT(*) FROM mt_rapid_trend_trigger_events
                   WHERE trigger_id = ? AND event_type = 'failed'
                     AND json_extract(payload_json, '$.stage') = ?""",
                (trigger_id, stage),
            ).fetchone()[0])
        if existing >= _MAX_FAILURE_ATTEMPTS_PER_STAGE:
            return
        self._record_event(
            trigger_id,
            "failed",
            {
                "contract": RAPID_TREND_EVENT_CONTRACT,
                "stage": stage,
                "code": type(error).__name__,
                "retryable": existing + 1 < _MAX_FAILURE_ATTEMPTS_PER_STAGE,
            },
            attempt_no=existing,
        )

    def _record_event(
        self,
        trigger_id: str,
        event_type: str,
        payload: Mapping[str, Any],
        *,
        attempt_no: int = 0,
    ) -> None:
        trigger = self._trigger(trigger_id)
        if trigger is None:
            raise ValueError("trigger_id does not exist")
        with self.store.connect() as connection:
            self._append_event(
                connection,
                trigger_id=trigger_id,
                event_type=event_type,
                source_service="market-tape-rapid-trend",
                source_receipt_id=trigger_id,
                payload=payload,
                attempt_no=attempt_no,
                created_at=isoformat(utc_now()),
            )

    @staticmethod
    def _append_event(
        connection: sqlite3.Connection,
        *,
        trigger_id: str,
        event_type: str,
        source_service: str,
        source_receipt_id: str,
        payload: Mapping[str, Any],
        created_at: str,
        attempt_no: int = 0,
    ) -> None:
        canonical_payload = dict(payload)
        payload_sha = stable_hash(canonical_payload)
        identity = {
            "trigger_id": trigger_id,
            "event_type": event_type,
            "attempt_no": int(attempt_no),
            "payload_sha256": payload_sha,
        }
        event_id = "rapid-trend-event:" + stable_hash(identity)
        cursor = connection.execute(
            """INSERT INTO mt_rapid_trend_trigger_events(
                   event_id, trigger_id, event_type, attempt_no,
                   source_service, source_receipt_id, payload_sha256,
                   payload_json, created_at
               ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(trigger_id, event_type, attempt_no) DO NOTHING""",
            (
                event_id,
                trigger_id,
                event_type,
                int(attempt_no),
                source_service,
                source_receipt_id,
                payload_sha,
                _canonical_json(canonical_payload),
                created_at,
            ),
        )
        if cursor.rowcount == 1:
            row = connection.execute(
                "SELECT * FROM mt_rapid_trend_trigger_events WHERE event_id = ?",
                (event_id,),
            ).fetchone()
            if row is None:  # pragma: no cover - transaction invariant
                raise RuntimeError("rapid trend event insert was not durable")
            RapidTrendTriggerService._enqueue_outbox(
                connection,
                "rapid_trend_trigger_event",
                event_id,
                dict(row),
                created_at=created_at,
            )

    @staticmethod
    def _enqueue_outbox(
        connection: sqlite3.Connection,
        entity_type: str,
        entity_key: str,
        payload: Mapping[str, Any],
        *,
        created_at: str,
    ) -> None:
        connection.execute(
            """INSERT INTO mt_sync_outbox(
                   entity_type, entity_key, payload_json,
                   created_at, next_attempt_at
               ) VALUES(?, ?, ?, ?, ?)
               ON CONFLICT(entity_type, entity_key) DO NOTHING""",
            (
                entity_type,
                entity_key,
                _canonical_json(dict(payload)),
                created_at,
                created_at,
            ),
        )

    def _trigger(self, trigger_id: str) -> Optional[Dict[str, Any]]:
        with self.store.connect() as connection:
            row = connection.execute(
                "SELECT * FROM mt_rapid_trend_triggers WHERE trigger_id = ?",
                (trigger_id,),
            ).fetchone()
            return self._decode_trigger(connection, dict(row)) if row else None

    @staticmethod
    def _decode_trigger(
        connection: sqlite3.Connection, row: Dict[str, Any]
    ) -> Dict[str, Any]:
        trigger = dict(row)
        trigger["evidence"] = json.loads(trigger.pop("evidence_json"))
        events = []
        for event_row in connection.execute(
            """SELECT * FROM mt_rapid_trend_trigger_events
               WHERE trigger_id = ?
               ORDER BY created_at, event_type, attempt_no, event_id""",
            (trigger["trigger_id"],),
        ).fetchall():
            event = dict(event_row)
            event["payload"] = json.loads(event.pop("payload_json"))
            events.append(event)
        by_type = {event["event_type"]: event for event in events}
        semantic = (by_type.get("semantic_materialized") or {}).get("payload")
        demand = (by_type.get("evidence_demand_enqueued") or {}).get("payload")
        if semantic and demand:
            state = "context_acquisition_queued"
        elif semantic:
            state = "semantic_materialized"
        elif demand:
            state = "evidence_demand_enqueued"
        elif any(event["event_type"] == "failed" for event in events):
            state = "failed"
        elif any(event["event_type"] == "blocked" for event in events):
            state = "blocked"
        else:
            state = "detected"
        trigger.update({
            "state": state,
            "current_state": state,
            "events": events,
            "semantic_signal": semantic,
            "signal_id": str((semantic or {}).get("signal_id") or ""),
            "evidence_demand": demand,
            "evidence_demand_id": str((demand or {}).get("demand_id") or ""),
            "generation_authorized": False,
            "blockers": [
                "approved_atomic_selection_pending",
                "semantic_generation_handoff_pending",
            ],
        })
        return trigger


__all__ = [
    "RAPID_TREND_EVALUATION_CONTRACT",
    "RAPID_TREND_EVENT_CONTRACT",
    "RAPID_TREND_LIST_CONTRACT",
    "RAPID_TREND_POLICY_CONTRACT",
    "RAPID_TREND_POLICY_VERSION",
    "RAPID_TREND_SCRIPT_REQUEST_CONTRACT",
    "RAPID_TREND_SCRIPT_RESPONSE_CONTRACT",
    "RAPID_TREND_TRIGGER_CONTRACT",
    "RapidTrendTriggerService",
]
