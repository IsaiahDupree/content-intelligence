from __future__ import annotations

import hashlib
import math
from typing import Any, Sequence

from .engine import QualityStore, script_identity_payload
from .reference_corpus import canonical_sha256


BINDING_CONTRACT = "reference_script_quality_binding_v1"
LEGACY_PACKAGE_CONTRACT = "reference_marketing_script_package_v1"
PACKAGE_CONTRACT = "reference_marketing_script_package_v2"
LEGACY_REQUEST_CONTRACT = "reference_marketing_script_request_v1"
REQUEST_CONTRACT = "reference_marketing_script_request_v2"
REQUEST_CONTRACT_BY_PACKAGE = {
    LEGACY_PACKAGE_CONTRACT: LEGACY_REQUEST_CONTRACT,
    PACKAGE_CONTRACT: REQUEST_CONTRACT,
}
AUDIT_CONTRACT = "content_creation_audit_v1"
MINIMUM_TRANSCRIPT_RECEIPTS = 5
MINIMUM_CREATORS = 3
MINIMUM_OBSERVED_VIEWS = 100_000

BEAT_NAMES = {
    "hook": "human_hook",
    "problem": "human_problem",
    "stakes": "stakes",
    "proof": "proof",
    "reframe": "reframe",
    "teaching_step": "teaching_step",
    "takeaway": "payoff",
    "call_to_action": "cta",
}


def _required_object(value: Any, field: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError(f"{field} must be an object")
    return value


def _required_text(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field} is required")
    return value.strip()


def _sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _validate_self_hash(
    value: dict[str, Any], field: str, *, excluded: Sequence[str] = ()
) -> str:
    claimed = _required_text(value.get(field), field).lower()
    core = {
        key: item for key, item in value.items()
        if key != field and key not in excluded
    }
    if canonical_sha256(core) != claimed:
        raise ValueError(f"{field} is invalid")
    return claimed


def validate_reference_package(package: dict[str, Any]) -> dict[str, Any]:
    """Validate the immutable approvals needed before quality-store import."""

    package = _required_object(package, "package")
    package_contract = package.get("contract")
    if package_contract not in REQUEST_CONTRACT_BY_PACKAGE:
        raise ValueError("reference package contract is invalid")
    expected_request_contract = REQUEST_CONTRACT_BY_PACKAGE[package_contract]
    if package.get("status") != "approved":
        raise ValueError("reference package is not approved")
    result_sha256 = _validate_self_hash(package, "result_sha256")

    request = _required_object(package.get("request"), "request")
    if request.get("contract") != expected_request_contract:
        raise ValueError("reference request contract is invalid")
    if expected_request_contract == REQUEST_CONTRACT:
        if not str(request.get("content_role") or "").strip():
            raise ValueError("v2 reference request content_role is required")
        if request.get("topic_distance_from_offer") is None:
            raise ValueError(
                "v2 reference request topic_distance_from_offer is required"
            )
    request_sha256 = _required_text(
        package.get("request_sha256"), "request_sha256"
    ).lower()
    if canonical_sha256(request) != request_sha256:
        raise ValueError("request_sha256 is invalid")
    if package.get("request_contract") != expected_request_contract:
        raise ValueError("request_contract is invalid")
    for field in ("script_id", "corpus_id", "context_id", "created_at"):
        _required_text(package.get(field), field)

    marketing_logic = _required_object(
        package.get("marketing_logic"), "marketing_logic"
    )
    for field in ("content_role", "topic_ladder_id"):
        if marketing_logic.get(field) != request.get(field):
            raise ValueError(f"marketing_logic.{field} does not match the request")
    distance = marketing_logic.get("topic_distance_from_offer")
    if (
        isinstance(distance, bool)
        or not isinstance(distance, int)
        or not 0 <= distance <= 5
        or distance != request.get("topic_distance_from_offer")
    ):
        raise ValueError(
            "marketing_logic.topic_distance_from_offer does not match the request"
        )

    lineage = _required_object(package.get("lineage"), "lineage")
    if str(lineage.get("request_sha256") or "").lower() != request_sha256:
        raise ValueError("lineage request_sha256 does not match the request")

    script = _required_object(package.get("script"), "script")
    transcript = _required_text(script.get("transcript"), "script.transcript")
    beats = script.get("beats")
    if not isinstance(beats, list) or len(beats) < 5:
        raise ValueError("script.beats must contain at least five beats")
    joined = " ".join(
        _required_text(beat.get("text"), "script.beats[].text")
        for beat in beats
        if isinstance(beat, dict)
    )
    if len(joined.split()) == 0 or joined != transcript:
        raise ValueError("script transcript does not equal the ordered beat text")
    transcript_sha256 = _sha256_text(transcript)

    quality = _required_object(package.get("quality"), "quality")
    if quality.get("status") != "pass" or quality.get("failed_checks") != []:
        raise ValueError("reference package quality gate did not pass")
    owner_quality = _required_object(
        quality.get("owner_calibrated"), "quality.owner_calibrated"
    )
    if (
        owner_quality.get("decision") != "PASS"
        or owner_quality.get("failure_codes") != []
    ):
        raise ValueError("owner-calibrated quality gate did not pass")

    proof_gate = _required_object(
        package.get("proof_evidence_gate"), "proof_evidence_gate"
    )
    if proof_gate.get("passed") is not True or proof_gate.get("failure_codes") != []:
        raise ValueError("proof evidence gate did not pass")

    audit = _required_object(package.get("corpus_audit"), "corpus_audit")
    if audit.get("contract") != AUDIT_CONTRACT or audit.get("status") != "pass":
        raise ValueError("reference corpus audit did not pass")
    audit_result_sha256 = _validate_self_hash(
        audit, "result_sha256", excluded=("request_sha256",)
    )
    if str(lineage.get("audit_result_sha256") or "").lower() != audit_result_sha256:
        raise ValueError("lineage audit hash does not match the corpus audit")
    copy_gate = _required_object(audit.get("copy_gate"), "corpus_audit.copy_gate")
    if copy_gate.get("passed") is not True or copy_gate.get("failure_codes") != []:
        raise ValueError("substantive-copy gate did not pass")
    provenance_gate = _required_object(
        copy_gate.get("provenance_gate"),
        "corpus_audit.copy_gate.provenance_gate",
    )
    if (
        provenance_gate.get("passed") is not True
        or provenance_gate.get("failure_codes") != []
        or str(provenance_gate.get("candidate_sha256") or "").lower()
        != transcript_sha256
    ):
        raise ValueError("copy provenance is not bound to the exact transcript")

    rights = _required_object(package.get("rights"), "rights")
    required_rights = {
        "state": "public_reference_analysis_only",
        "source_clips_used": False,
        "direct_use_allowed": False,
        "identity_imitation_allowed": False,
        "voice_imitation_allowed": False,
        "exact_draft_copy_gate_passed": True,
    }
    if any(rights.get(key) != expected for key, expected in required_rights.items()):
        raise ValueError("reference package rights gate did not pass")

    return {
        "package_result_sha256": result_sha256,
        "request_sha256": request_sha256,
        "audit_result_sha256": audit_result_sha256,
        "transcript_sha256": transcript_sha256,
        "copy_audit_id": _required_text(audit.get("audit_id"), "corpus_audit.audit_id"),
        "provenance_sha256": _required_text(
            provenance_gate.get("provenance_sha256"), "provenance_sha256"
        ).lower(),
    }


class ReferenceScriptQualityBinder:
    """Import an approved reference script into the immutable quality store."""

    def __init__(self, store: QualityStore):
        self.store = store

    @staticmethod
    def _normalized_identity(script: dict[str, Any]) -> dict[str, Any]:
        normalized = dict(script)
        evidence = normalized.get("evidence_summary")
        if (
            isinstance(evidence, dict)
            and evidence.get("contract")
            == "reference_script_performance_evidence_v1"
        ):
            normalized_evidence = dict(evidence)
            normalized_evidence.setdefault(
                "requires_in_timeline_attribution", False
            )
            normalized_evidence.setdefault(
                "performance_evidence_scope", "relatability_prediction_only"
            )
            normalized["evidence_summary"] = normalized_evidence
        return script_identity_payload(normalized)

    def _performance_cohort(
        self, receipt_ids: Sequence[str]
    ) -> tuple[list[str], dict[str, Any]]:
        if isinstance(receipt_ids, (str, bytes)) or not isinstance(
            receipt_ids, Sequence
        ):
            raise ValueError("source_receipt_ids must be an array")
        clean_ids = [str(value).strip() for value in receipt_ids]
        if (
            len(clean_ids) < MINIMUM_TRANSCRIPT_RECEIPTS
            or any(not value for value in clean_ids)
            or len(set(clean_ids)) != len(clean_ids)
        ):
            raise ValueError(
                "source_receipt_ids must contain at least five distinct IDs"
            )
        rows = {row["receipt_id"]: row for row in self.store.receipts(clean_ids)}
        if set(rows) != set(clean_ids):
            raise ValueError("one or more source receipts were not found")
        creators: set[str] = set()
        observed_views = 0
        for receipt_id in clean_ids:
            receipt = rows[receipt_id]
            payload = _required_object(receipt.get("payload"), "receipt.payload")
            qualification = _required_object(
                payload.get("performance_qualification"),
                "receipt.payload.performance_qualification",
            )
            checks = qualification.get("checks")
            if (
                receipt.get("receipt_type") != "viral_transcript_pattern"
                or qualification.get("audit_decision") != "PASS"
                or not isinstance(checks, dict)
                or not checks
                or not all(value is True for value in checks.values())
                or payload.get("transcript_source") != "local_whisper"
                or not payload.get("transcript_id")
                or not payload.get("transcript_sha256")
                or not payload.get("observation_key")
            ):
                raise ValueError(
                    f"source receipt is not performance-bound transcript evidence: {receipt_id}"
                )
            creator_id = _required_text(
                payload.get("creator_id"), "receipt.payload.creator_id"
            )
            creators.add(creator_id)
            pattern = _required_object(
                payload.get("pattern"), "receipt.payload.pattern"
            )
            metrics = _required_object(
                pattern.get("source_metrics"),
                "receipt.payload.pattern.source_metrics",
            )
            views = metrics.get("views")
            if isinstance(views, bool) or not isinstance(views, int) or views < 0:
                raise ValueError("receipt observed views must be a non-negative integer")
            observed_views += views
        if len(creators) < MINIMUM_CREATORS:
            raise ValueError("performance cohort must include at least three creators")
        if observed_views < MINIMUM_OBSERVED_VIEWS:
            raise ValueError("performance cohort has insufficient observed views")
        return clean_ids, {
            "accepted_transcript_count": len(clean_ids),
            "creator_count": len(creators),
            "observed_views_snapshot": observed_views,
            "actual_audience_relatability_measured": False,
        }

    def _human_moment(
        self, receipt_id: str, moment_id: str
    ) -> dict[str, Any]:
        clean_receipt_id = _required_text(receipt_id, "source_moment_receipt_id")
        clean_moment_id = _required_text(moment_id, "source_moment_id")
        receipt = self.store.receipt(clean_receipt_id)
        if not isinstance(receipt, dict):
            raise ValueError("source moment receipt was not found")
        if receipt.get("receipt_type") != "audience_human_moments":
            raise ValueError("source moment receipt type is invalid")
        moments = (receipt.get("payload") or {}).get("moments")
        if not isinstance(moments, list):
            raise ValueError("source moment receipt has no moments")
        selected = next(
            (
                item for item in moments
                if isinstance(item, dict) and item.get("moment_id") == clean_moment_id
            ),
            None,
        )
        if not isinstance(selected, dict):
            raise ValueError("source moment was not found in the bound receipt")
        required = (
            "moment_id", "situation", "audience", "stakes",
            "source_transcript_id", "source_observation_key",
            "stakes_source_moment_id", "stakes_source_transcript_id",
            "stakes_source_observation_key",
        )
        for field in required:
            _required_text(selected.get(field), f"source moment {field}")
        return {**selected, "source_moment_receipt_id": clean_receipt_id}

    @staticmethod
    def _timeline(package: dict[str, Any]) -> list[dict[str, Any]]:
        timeline: list[dict[str, Any]] = []
        previous_end = 0.0
        for index, row in enumerate(package["script"]["beats"]):
            if not isinstance(row, dict):
                raise ValueError("script beat must be an object")
            block = _required_text(row.get("block"), "script beat block")
            beat = BEAT_NAMES.get(block)
            if not beat:
                raise ValueError(f"unsupported reference script beat: {block}")
            try:
                start = float(row.get("start_seconds"))
                end = float(row.get("end_seconds"))
            except (TypeError, ValueError) as error:
                raise ValueError("script beat timing must be numeric") from error
            if (
                not math.isfinite(start)
                or not math.isfinite(end)
                or start < 0
                or end <= start
                or (index and start < previous_end - 0.001)
            ):
                raise ValueError("script beat timing is invalid or out of order")
            timeline.append({
                "start": start,
                "end": end,
                "beat": beat,
                "text": _required_text(row.get("text"), "script beat text"),
                "reference_node_id": str(row.get("node_id") or ""),
                "reference_block": block,
            })
            previous_end = end
        return timeline

    def bind(
        self,
        package: dict[str, Any],
        *,
        source_receipt_ids: Sequence[str],
        source_moment_receipt_id: str,
        source_moment_id: str,
    ) -> dict[str, Any]:
        validation = validate_reference_package(package)
        cohort_ids, cohort = self._performance_cohort(source_receipt_ids)
        human_moment = self._human_moment(
            source_moment_receipt_id, source_moment_id
        )
        request = package["request"]
        marketing_logic = package["marketing_logic"]
        script_package = package["script"]
        timeline = self._timeline(package)
        experiment = (
            (package.get("script_experiment_registration") or {}).get("experiment")
            or {}
        )
        binding = {
            "contract": BINDING_CONTRACT,
            "package_contract": package["contract"],
            "package_result_sha256": validation["package_result_sha256"],
            "request_sha256": validation["request_sha256"],
            "transcript_sha256": validation["transcript_sha256"],
            "corpus_id": package["corpus_id"],
            "context_id": package["context_id"],
            "copy_audit_id": validation["copy_audit_id"],
            "copy_audit_result_sha256": validation["audit_result_sha256"],
            "copy_provenance_sha256": validation["provenance_sha256"],
            "source_moment_receipt_id": source_moment_receipt_id,
            "source_moment_id": source_moment_id,
            "performance_cohort": cohort,
        }
        stored_before = self.store.script(str(package["script_id"]))
        script = {
            "script_id": _required_text(package.get("script_id"), "script_id"),
            "topic": _required_text(request.get("topic"), "request.topic"),
            "audience": _required_text(request.get("audience"), "request.audience"),
            "objective": _required_text(request.get("objective"), "request.objective"),
            "brief_id": str(experiment.get("brief_id") or "") or None,
            "trend_id": str(request.get("topic_ladder_id") or "") or None,
            "content_role": _required_text(
                marketing_logic.get("content_role"), "marketing_logic.content_role"
            ),
            "topic_distance_from_offer": marketing_logic.get(
                "topic_distance_from_offer"
            ),
            "topic_ladder_id": str(marketing_logic.get("topic_ladder_id") or ""),
            "reference_package_binding": binding,
            "source_receipt_ids": cohort_ids,
            "evidence_binding_receipt_ids": [
                *cohort_ids, source_moment_receipt_id
            ],
            "human_moment": human_moment,
            "evidence_summary": {
                "contract": "reference_script_performance_evidence_v1",
                **cohort,
                "source_receipt_ids": cohort_ids,
                "source_moment_receipt_id": source_moment_receipt_id,
                "source_moment_id": source_moment_id,
                "requires_in_timeline_attribution": bool(
                    package["proof_evidence_gate"].get("required")
                ),
                "performance_evidence_scope": (
                    "relatability_prediction_only"
                    if not package["proof_evidence_gate"].get("required")
                    else "script_claim_support"
                ),
                "observed_views_are_exposure_not_causality": True,
                "first_owned_click_observed": False,
                "first_owned_retention_event_observed": False,
            },
            "rhetorical_structure": marketing_logic.get("rhetorical_structure") or {},
            "owner_quality_contract": (
                package["quality"]["owner_calibrated"].get("contract")
            ),
            "owner_quality": package["quality"]["owner_calibrated"],
            "quality_revision": package.get("revision") or {},
            "delivery_visual_plan": script_package.get("delivery_visual_plan") or {},
            "timeline": timeline,
            "text": script_package["transcript"],
            "status": "approved",
            "created_at": package["created_at"],
        }
        if stored_before is not None:
            if self._normalized_identity(stored_before) != self._normalized_identity(
                script
            ):
                raise ValueError(
                    "script_id already exists with different immutable content"
                )
            stored = stored_before
        else:
            stored = self.store.put_script(script)
        script_sha256 = self.store.script_audit_sha256(stored)
        gate_summary = self.store.script_gate_summary(stored["script_id"])
        existing_owner_audit = gate_summary["latest_audits"].get(
            "owner_calibrated_quality"
        )
        if (
            isinstance(existing_owner_audit, dict)
            and existing_owner_audit.get("decision") == "PASS"
            and existing_owner_audit.get("stored_script_binding_valid") is True
        ):
            owner_quality_audit = existing_owner_audit
        else:
            owner_quality_audit = self.store.put_audit(
                "owner_calibrated_quality",
                stored["script_id"],
                "PASS",
                float(stored["owner_quality"].get("score") or 0.0),
                {
                    "quality": stored["owner_quality"],
                    "revision": stored["quality_revision"],
                    "source": BINDING_CONTRACT,
                    "reference_package_binding": binding,
                    "input_binding": {
                        "contract": "stored_script_audit_binding_v1",
                        "stored_script_bound": True,
                        "script_id": stored["script_id"],
                        "script_sha256": script_sha256,
                    },
                },
            )
        receipt = self.store.put_receipt(
            "reference_script_quality_binding",
            "content_reference_corpus",
            stored["script_id"],
            None,
            {
                **binding,
                "script_id": stored["script_id"],
                "script_sha256": script_sha256,
                "source_receipt_ids": cohort_ids,
            },
        )
        return {
            "status": "created" if stored_before is None else "idempotent_replay",
            "created": stored_before is None,
            "contract": BINDING_CONTRACT,
            "script_id": stored["script_id"],
            "script_sha256": script_sha256,
            "binding_receipt": receipt,
            "owner_quality_audit": owner_quality_audit,
            "script": stored,
        }
