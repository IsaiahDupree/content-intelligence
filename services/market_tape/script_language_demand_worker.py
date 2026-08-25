"""One-shot acquisition worker for append-only script-language demands.

Each invocation claims at most one demand, runs one topic-scoped Market Tape
discovery plus local Whisper backfill, and appends one terminal event.  A
completed pipeline is intentionally distinct from meeting the requested
evidence target: an empty but healthy provider response completes the work
attempt with ``goal_met=false`` and is never retried inside the same call.
"""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path
from typing import Any, Callable

from .collector import MarketTapeCollector
from .config import MarketTapeConfig
from .full_pipeline import DEFAULT_TRANSCRIPT_STORAGE_ROOT, run_full_pipeline
from .sources import build_sources
from .sources.base import sanitize
from .store import MarketTapeStore


WORKER_CONTRACT = "market_tape_script_language_demand_worker_v1"
RESULT_CONTRACT = "market_tape_script_language_demand_result_v1"
MAX_DISCOVERY_ITEMS = 50
MAX_TRANSCRIPTS = 10
WHISPER_MODEL = "base"


class _DemandCollector(MarketTapeCollector):
    """Prevent an acquisition demand from spending reads on forecast rechecks."""

    def _forecast_measurement_work_required(self) -> bool:
        return False


class ScriptLanguageDemandWorker:
    """Process one queued language-evidence demand, never a polling loop."""

    def __init__(
        self,
        config: MarketTapeConfig | None = None,
        store: MarketTapeStore | None = None,
        *,
        transcript_storage_root: Path | None = None,
        lease_seconds: int = 86_400,
        source_builder: Callable[..., Any] | None = None,
    ) -> None:
        self.config = config or MarketTapeConfig.from_environment()
        self.store = store or MarketTapeStore(self.config)
        self.transcript_storage_root = (
            transcript_storage_root or DEFAULT_TRANSCRIPT_STORAGE_ROOT
        )
        self.lease_seconds = max(300, min(86_400, int(lease_seconds)))
        self.source_builder = source_builder or build_sources

    def run_next(self) -> dict[str, Any]:
        """Claim and terminalize at most one demand with one pipeline call."""

        claim = self.store.claim_next_script_language_demand(
            self.lease_seconds,
            source_service="script-language-demand-worker",
            payload={
                "contract": WORKER_CONTRACT,
                "maximum_discovery_items": MAX_DISCOVERY_ITEMS,
                "maximum_transcripts": MAX_TRANSCRIPTS,
                "whisper_model": WHISPER_MODEL,
                "cycles": 1,
                "same_call_retry": False,
            },
        )
        if claim is None:
            return {
                "contract": WORKER_CONTRACT,
                "state": "idle",
                "claimed": 0,
                "pipeline_invocations": 0,
            }

        request = dict(claim.get("latest_request_payload") or {})
        if not request:
            request = next(
                event["payload"]
                for event in claim["events"]
                if event["event_type"] == "requested"
            )
        policy = request.get("acquisition_policy")
        policy = policy if isinstance(policy, dict) else {}
        discovery_limit = _bounded_int(
            policy.get("discovery_limit"), MAX_DISCOVERY_ITEMS,
            maximum=MAX_DISCOVERY_ITEMS,
        )
        transcript_limit = _bounded_int(
            policy.get("transcript_limit"), 5,
            maximum=MAX_TRANSCRIPTS,
        )
        targets = _targets(claim.get("targets"))
        baseline = _actuals(request.get("actuals"))
        excluded_creators = _passing_transcript_creator_ids(self.store)
        runtime_config = self._bounded_config(
            topic=claim["topic"], discovery_limit=discovery_limit
        )
        collector = _DemandCollector(
            runtime_config,
            self.store,
            source_builder=self.source_builder,
        )
        receipt_id = (
            f"script-language-demand-worker:{claim['demand_id']}:"
            f"{claim['attempt_no']}"
        )
        pipeline_invocations = 0
        try:
            pipeline_invocations = 1
            pipeline = run_full_pipeline(
                config=runtime_config,
                store=self.store,
                collector=collector,
                discovery_mode="discovery",
                transcript_limit=transcript_limit,
                transcript_platforms=("youtube",),
                transcript_model=WHISPER_MODEL,
                topic=claim["topic"],
                exclude_creator_ids=excluded_creators,
                transcript_storage_root=self.transcript_storage_root,
            )
            if int(pipeline["discovery"]["videos_discovered"]) > discovery_limit:
                raise RuntimeError("bounded discovery limit was exceeded")
            if int(pipeline["transcription"]["candidate_count"]) > transcript_limit:
                raise RuntimeError("bounded transcript limit was exceeded")
            progress = _new_transcript_progress(
                self.store, pipeline["fully_vetted_transcript_ids"]
            )
            actuals = {
                "verified_transcripts": (
                    baseline["verified_transcripts"]
                    + progress["verified_transcripts"]
                ),
                "distinct_creators": (
                    baseline["distinct_creators"]
                    + progress["distinct_creators"]
                ),
                "observed_views": (
                    baseline["observed_views"] + progress["observed_views"]
                ),
            }
            goal_met = all(actuals[key] >= targets[key] for key in targets)
            terminal_type = _terminal_type(pipeline)
            result_payload = {
                "contract": RESULT_CONTRACT,
                "pipeline_completed": pipeline["state"] == "completed",
                "goal_met": goal_met,
                "targets": targets,
                "baseline_actuals": baseline,
                "new_evidence": progress,
                "actuals": actuals,
                "bounds": {
                    "cycles": 1,
                    "discovery_limit": discovery_limit,
                    "transcript_limit": transcript_limit,
                    "whisper_model": WHISPER_MODEL,
                    "platforms": ["youtube"],
                    "excluded_creator_count": len(excluded_creators),
                    "same_call_retry": False,
                },
                "pipeline_invocations": pipeline_invocations,
                "in_call_retry_count": 0,
                "pipeline": pipeline,
                "script_readiness_recheck_required": True,
            }
            terminal = self.store.finish_script_language_demand(
                claim["demand_id"],
                claim["attempt_no"],
                terminal_type,
                result_payload,
                source_service="script-language-demand-worker",
                source_receipt_id=receipt_id,
                collection_run_id=pipeline["discovery"]["run_id"],
                transcript_run_id=pipeline["transcription"]["run_id"],
            )
            persisted_result = _persisted_worker_result(
                terminal, result_payload
            )
            return {
                "contract": WORKER_CONTRACT,
                "state": terminal["state"],
                "claimed": 1,
                "demand_id": claim["demand_id"],
                "attempt_no": claim["attempt_no"],
                "pipeline_invocations": pipeline_invocations,
                "goal_met": bool(
                    terminal["state"] == "completed"
                    and persisted_result.get("goal_met")
                ),
                "result": persisted_result,
                "terminal": terminal,
            }
        except Exception as exc:
            failure = {
                "contract": RESULT_CONTRACT,
                "pipeline_completed": False,
                "goal_met": False,
                "pipeline_invocations": pipeline_invocations,
                "in_call_retry_count": 0,
                "error_type": type(exc).__name__,
                "error": sanitize(exc)[:500],
            }
            terminal = self.store.finish_script_language_demand(
                claim["demand_id"],
                claim["attempt_no"],
                "failed",
                failure,
                source_service="script-language-demand-worker",
                source_receipt_id=receipt_id,
            )
            persisted_result = _persisted_worker_result(terminal, failure)
            return {
                "contract": WORKER_CONTRACT,
                "state": terminal["state"],
                "claimed": 1,
                "demand_id": claim["demand_id"],
                "attempt_no": claim["attempt_no"],
                "pipeline_invocations": pipeline_invocations,
                "goal_met": False,
                "result": persisted_result,
                "terminal": terminal,
            }

    def _bounded_config(
        self, *, topic: str, discovery_limit: int
    ) -> MarketTapeConfig:
        global_count = self.store.daily_unique_count()
        youtube_count = self.store.daily_unique_count("youtube")
        targets = dict(self.config.platform_daily_targets)
        targets["youtube"] = youtube_count + discovery_limit
        return replace(
            self.config,
            platforms=["youtube"],
            topics=[str(topic).strip()],
            adaptive_topics_enabled=False,
            adaptive_topic_limit=1,
            daily_unique_target=global_count + discovery_limit,
            platform_daily_targets=targets,
            max_discovery_items_per_source=discovery_limit,
            overflow_platforms=[],
            local_research_trigger_enabled=False,
            prediction_model_dir=(
                self.config.prediction_model_dir
                / "script-language-demand-worker-no-active-model"
            ),
        )


def _bounded_int(value: Any, default: int, *, maximum: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        parsed = default
    return max(1, min(maximum, parsed))


def _persisted_worker_result(
    demand: dict[str, Any], fallback: dict[str, Any]
) -> dict[str, Any]:
    for event in reversed(list(demand.get("events") or [])):
        if event.get("event_type") in {"completed", "partial", "blocked", "failed"}:
            result = (event.get("payload") or {}).get("result")
            if isinstance(result, dict):
                return result
    return fallback


def _targets(value: Any) -> dict[str, int]:
    source = value if isinstance(value, dict) else {}
    return {
        "verified_transcripts": _nonnegative_int(
            source.get("verified_transcripts"), 5
        ),
        "distinct_creators": _nonnegative_int(
            source.get("distinct_creators"), 3
        ),
        "observed_views": _nonnegative_int(source.get("observed_views"), 100_000),
    }


def _actuals(value: Any) -> dict[str, int]:
    source = value if isinstance(value, dict) else {}
    return {
        key: _nonnegative_int(source.get(key), 0)
        for key in ("verified_transcripts", "distinct_creators", "observed_views")
    }


def _nonnegative_int(value: Any, default: int) -> int:
    try:
        return max(0, int(value))
    except (TypeError, ValueError):
        return default


def _passing_transcript_creator_ids(store: MarketTapeStore) -> list[str]:
    with store.connect() as connection:
        rows = connection.execute(
            """SELECT DISTINCT video.creator_id, artifact.audit_json
               FROM mt_transcript_artifacts artifact
               JOIN mt_videos video ON video.video_id=artifact.video_id"""
        ).fetchall()
    creators = set()
    for row in rows:
        try:
            audit = json.loads(str(row["audit_json"] or "{}"))
        except (TypeError, ValueError, json.JSONDecodeError):
            continue
        if audit.get("decision") == "PASS" and row["creator_id"]:
            creators.add(str(row["creator_id"]))
    return sorted(creators)


def _new_transcript_progress(
    store: MarketTapeStore, transcript_ids: list[str]
) -> dict[str, Any]:
    canonical = list(dict.fromkeys(str(value) for value in transcript_ids if value))
    if not canonical:
        return {
            "verified_transcripts": 0,
            "distinct_creators": 0,
            "observed_views": 0,
            "transcript_ids": [],
        }
    marks = ",".join("?" for _ in canonical)
    with store.connect() as connection:
        rows = connection.execute(
            f"""SELECT artifact.transcript_id, artifact.source_metrics_json,
                       video.creator_id
                FROM mt_transcript_artifacts artifact
                JOIN mt_videos video ON video.video_id=artifact.video_id
                WHERE artifact.transcript_id IN ({marks})""",
            tuple(canonical),
        ).fetchall()
    creators = {str(row["creator_id"]) for row in rows if row["creator_id"]}
    views = 0
    for row in rows:
        try:
            metrics = json.loads(str(row["source_metrics_json"] or "{}"))
        except (TypeError, ValueError, json.JSONDecodeError):
            metrics = {}
        views += _nonnegative_int(metrics.get("views"), 0)
    found = sorted({str(row["transcript_id"]) for row in rows})
    return {
        "verified_transcripts": len(found),
        "distinct_creators": len(creators),
        "observed_views": views,
        "transcript_ids": found,
    }


def _terminal_type(pipeline: dict[str, Any]) -> str:
    discovery = pipeline.get("discovery") or {}
    transcription = pipeline.get("transcription") or {}
    if discovery.get("state") == "failed":
        return "failed"
    blocked_states = {
        str(receipt.get("state") or "")
        for receipt in discovery.get("receipts") or []
        if receipt.get("platform") == "youtube"
    }
    if any(state.startswith("blocked_") for state in blocked_states):
        return "blocked"
    if transcription.get("status") == "blocked_runtime":
        return "blocked"
    if transcription.get("status") in {"failed", "audit_failed"}:
        return "failed"
    if pipeline.get("state") == "partial":
        return "partial"
    return "completed"


__all__ = ["ScriptLanguageDemandWorker", "WORKER_CONTRACT"]
