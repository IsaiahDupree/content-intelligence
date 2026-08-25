"""One-shot consumer for append-only script-language evidence demands."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from typing import Any, Sequence

from services.content_quality.contracts import (
    SUPPORTED_TRANSCRIPT_AUDIT_CONTRACTS,
)
from services.content_quality.transcript_bank import topic_terms

from .collector import MarketTapeCollector
from .config import MarketTapeConfig
from .full_pipeline import DEFAULT_TRANSCRIPT_STORAGE_ROOT, run_full_pipeline
from .store import MarketTapeStore


SCRIPT_LANGUAGE_DEMAND_RUN_CONTRACT = "market_tape_script_language_demand_run_v1"


def _bounded_int(value: Any, default: int, *, minimum: int, maximum: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        parsed = default
    return max(minimum, min(maximum, parsed))


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
        transcript_storage_root: str | Path = DEFAULT_TRANSCRIPT_STORAGE_ROOT,
    ) -> None:
        self.config = config
        self.store = store or MarketTapeStore(config)
        self.collector = collector
        self.transcript_storage_root = Path(transcript_storage_root).expanduser()

    def cohort_snapshot(
        self,
        *,
        topic: str,
        evidence_trend_id: str = "",
    ) -> dict[str, Any]:
        terms = tuple(topic_terms(topic))
        clauses: list[str] = []
        parameters: list[Any] = [
            *sorted(SUPPORTED_TRANSCRIPT_AUDIT_CONTRACTS),
        ]
        if evidence_trend_id:
            clauses.append(
                """EXISTS (
                       SELECT 1
                       FROM mt_accepted_trend_memberships_v1 membership
                       WHERE membership.video_id = artifact.video_id
                         AND membership.trend_id = ?
                   )"""
            )
            parameters.append(evidence_trend_id)
        for term in terms:
            clauses.append(
                "LOWER(evidence.title || ' ' || evidence.caption || ' ' || "
                "evidence.description) LIKE ?"
            )
            parameters.append(f"%{term}%")
        topic_filter = f"AND ({' OR '.join(clauses)})" if clauses else ""
        contract_marks = ",".join(
            "?" for _ in SUPPORTED_TRANSCRIPT_AUDIT_CONTRACTS
        )
        with self.store.connect() as connection:
            rows = connection.execute(
                f"""SELECT artifact.transcript_id, artifact.observation_key,
                           artifact.video_id, video.creator_id,
                           metric.views
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
                    ORDER BY metric.views DESC, artifact.transcript_id""",
                parameters,
            ).fetchall()
        unique: dict[tuple[str, str], dict[str, Any]] = {}
        for row in rows:
            item = dict(row)
            unique.setdefault(
                (str(item["transcript_id"]), str(item["observation_key"])),
                item,
            )
        members = list(unique.values())
        creator_ids = sorted({
            str(item["creator_id"]) for item in members if item["creator_id"]
        })
        return {
            "contract": "script_language_quantitative_cohort_snapshot_v1",
            "topic": topic,
            "evidence_trend_id": evidence_trend_id,
            "verified_transcripts": len(members),
            "distinct_creators": len(creator_ids),
            "observed_views": sum(int(item["views"] or 0) for item in members),
            "creator_ids": creator_ids,
            "transcript_ids": sorted(str(item["transcript_id"]) for item in members),
            "qualitative_script_gate_pending": True,
        }

    def run_next(self, *, lease_seconds: int = 7200) -> dict[str, Any]:
        claim = self.store.claim_next_script_language_demand(
            max(300, min(int(lease_seconds), 86400)),
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

        request_payload = dict(claim["events"][0].get("payload") or {})
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
            }
            finished = self.store.finish_script_language_demand(
                str(claim["demand_id"]), int(claim["attempt_no"]), "failed",
                terminal_payload,
                source_service="script-language-demand-worker",
            )
            return {
                "contract": SCRIPT_LANGUAGE_DEMAND_RUN_CONTRACT,
                "state": "failed", "processed": 1, "goal_met": False,
                "demand": finished, "result": terminal_payload,
            }
        transcript_limit = _bounded_int(
            policy.get("transcript_limit"), 10, minimum=1, maximum=10
        )
        discovery_limit = _bounded_int(
            policy.get("discovery_limit"), 50, minimum=1, maximum=50
        )
        targets = dict(claim.get("targets") or {})
        pipeline: dict[str, Any] | None = None
        collection_run_id = ""
        transcript_run_id = ""
        failure_code = ""
        if not platforms:
            failure_code = "NO_REQUESTED_PLATFORM_ENABLED"
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
                    topic=topic,
                    # The originating trend is provenance, not an acquisition
                    # filter. Newly discovered topic matches cannot already be
                    # members of a historical trend snapshot, so scoping by
                    # that ID would guarantee a zero-candidate feedback loop.
                    transcript_trend_ids=(),
                    exclude_creator_ids=before["creator_ids"],
                    performance_discovery=True,
                    transcript_storage_root=self.transcript_storage_root,
                )
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
        goal_checks = {
            field: {
                "actual": int(after.get(field) or 0),
                "minimum": _bounded_int(
                    targets.get(field), 0, minimum=0, maximum=10**12
                ),
                "pass": int(after.get(field) or 0) >= _bounded_int(
                    targets.get(field), 0, minimum=0, maximum=10**12
                ),
            }
            for field in (
                "verified_transcripts", "distinct_creators", "observed_views"
            )
            if field in targets
        }
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
            "goal_met": goal_met,
            "goal_checks": goal_checks,
            "before": before,
            "after": after,
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
                "discovery_error_code": pipeline.get("discovery", {}).get(
                    "error_detail"
                ),
                "videos_discovered": discovered,
                "transcription_status": pipeline.get("transcription", {}).get("status"),
                "trend_ids": list(
                    pipeline.get("transcription", {}).get("trend_ids") or []
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
            "qualitative_script_gate_pending": not goal_met,
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
        return {
            "contract": SCRIPT_LANGUAGE_DEMAND_RUN_CONTRACT,
            "state": terminal_type,
            "processed": 1,
            "goal_met": goal_met,
            "demand": finished,
            "result": terminal_payload,
        }


__all__ = ["SCRIPT_LANGUAGE_DEMAND_RUN_CONTRACT", "ScriptLanguageDemandWorker"]
