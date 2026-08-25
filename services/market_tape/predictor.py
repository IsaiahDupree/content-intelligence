"""Versioned, dependency-free trend predictor training and inference."""

from __future__ import annotations

import hashlib
import json
import math
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Sequence, Tuple

from .config import MarketTapeConfig
from .models import isoformat, stable_hash, utc_now


LEGACY_MODEL_CONTRACT = "market_tape_trend_predictor_v1"
MODEL_CONTRACT = "market_tape_trend_predictor_v2"
LEGACY_ACTIVE_CONTRACT = "market_tape_active_predictor_v1"
ACTIVE_MODEL_CONTRACT = "market_tape_active_predictor_v2"
INFERENCE_CONTRACT = "market_tape_trend_inference_v2"
INFERENCE_POLICY_CONTRACT = "market_tape_standardized_ood_policy_v1"
PROMOTION_GATE_CONTRACT = "market_tape_predictor_promotion_gate_v2"
MODEL_FAMILY = "early-breakout-logistic-walk-forward-v6"
MODEL_PURPOSE = "early_breakout_entry"
OBSERVATION_QUALITY_CONTRACT = (
    "market_tape_accepted_observation_lineage_v2"
)
ENTRY_HORIZON = "enters_breakout_within_6h"
PROGRESSION_HORIZON = "is_or_reaches_breakout_within_6h"
TRAINING_SOURCE_MODELS = (
    "transparent-baseline-v1",
    "transparent-entry-baseline-v2",
    "transparent-entry-baseline-v3",
)
TRAINING_INDEX_VERSION = "trend-strength-v2"
BREAKOUT_STATES = {"breakout", "expanding", "saturating"}
FEATURES = (
    "trend_strength",
    "relative_strength",
    "momentum",
    "acceleration",
    "creator_breadth",
    "platform_breadth",
    "saturation",
)
FEATURE_CLIPS = {
    "trend_strength": (0.0, 100.0),
    "relative_strength": (-10.0, 10.0),
    "momentum": (-10.0, 10.0),
    "acceleration": (-10.0, 10.0),
    "creator_breadth": (0.0, 1.0),
    "platform_breadth": (0.0, 1.0),
    "saturation": (0.0, 1.0),
}
MINIMUM_WALK_FORWARD_FOLDS = 3
MAXIMUM_STANDARDIZED_DISTANCE = 4.0
SUPPORT_MARGIN_STANDARD_DEVIATIONS = 1.0
MINIMUM_INFERENCE_COVERAGE = 0.8
PROBABILITY_BOUNDS = (0.005, 0.995)
MINIMUM_EVIDENCE_VIDEOS = 2
MINIMUM_EVIDENCE_CREATORS = 2


class MarketTapePredictor:
    """Train grouped cross-validated candidates and preserve every decision receipt."""

    def __init__(self, config: MarketTapeConfig, store: Any):
        self.config = config
        self.store = store

    def train(self) -> Dict[str, Any]:
        rows = self._training_rows()
        labels = len(rows)
        positives = sum(row["actual"] for row in rows)
        negatives = labels - positives
        subjects = len({row["subject_id"] for row in rows})
        dataset_hash = stable_hash([
            {
                "subject_id": row["subject_id"],
                "predicted_at": row["predicted_at"],
                "label_available_at": row["label_available_at"],
                "actual": row["actual"],
                "features": row["features"],
            }
            for row in rows
        ])
        model_version = f"{MODEL_FAMILY}-{dataset_hash[:12]}"
        artifact_path = self.config.prediction_model_dir / f"{model_version}.json"
        if artifact_path.is_file():
            artifact = _read_json(artifact_path)
            artifact["operation"] = "unchanged"
            return artifact

        minimum_labels = max(1, self.config.prediction_min_backtest_labels)
        minimum_positives = max(2, self.config.prediction_min_positive_labels)
        enough_classes = positives >= minimum_positives and negatives >= minimum_positives
        folds = min(5, positives, negatives) if enough_classes else 0
        cross_validation: Dict[str, Any] = {
            "state": "insufficient_labels",
            "folds": folds,
            "labels": labels,
            "positives": positives,
            "negatives": negatives,
            "minimum_labels": minimum_labels,
            "minimum_positive_labels": minimum_positives,
        }
        model: Dict[str, Any] | None = None
        status = "collecting_labels"
        if labels >= minimum_labels and folds >= 2:
            cross_validation = self._cross_validate(rows, folds)
            model = _fit_logistic(rows)
            status = "rejected"

        promotion_gate = _promotion_gate(
            cross_validation,
            labels=labels,
            positives=positives,
            negatives=negatives,
            minimum_labels=minimum_labels,
            minimum_positive_labels=minimum_positives,
        )
        if model is not None and promotion_gate["passed"]:
            status = "promoted"

        artifact = {
            "contract": MODEL_CONTRACT,
            "schema_version": 2,
            "model_family": MODEL_FAMILY,
            "model_purpose": MODEL_PURPOSE,
            "model_version": model_version,
            "status": status,
            "trained_at": isoformat(utc_now()),
            "training_dataset_sha256": dataset_hash,
            "training": {
                "source_model_versions": list(TRAINING_SOURCE_MODELS),
                "index_version": TRAINING_INDEX_VERSION,
                "observation_quality_contract": (
                    OBSERVATION_QUALITY_CONTRACT
                ),
                "horizon": ENTRY_HORIZON,
                "eligibility": {
                    "initial_states_excluded": sorted(BREAKOUT_STATES),
                    "maximum_initial_trend_strength_exclusive": 70.0,
                    "minimum_videos": MINIMUM_EVIDENCE_VIDEOS,
                    "minimum_creators": MINIMUM_EVIDENCE_CREATORS,
                },
                "labels": labels,
                "positives": positives,
                "negatives": negatives,
                "subjects": subjects,
                "grouping": "subject_id_purged_from_each_validation_fold",
                "ordering": "predicted_at_ascending",
                "label_availability": "prediction_horizon_closed_before_validation",
            },
            "cross_validation": cross_validation,
            "promotion_gate": promotion_gate,
            "parameters": {
                "features": list(FEATURES),
                "feature_clips": {
                    key: list(value) for key, value in FEATURE_CLIPS.items()
                },
                "l2": 0.05,
                "learning_rate": 0.1,
                "iterations": 2000,
            },
            "inference_policy": _inference_policy(model),
            "model": model,
            "retestable": True,
        }
        self.config.prediction_model_dir.mkdir(parents=True, exist_ok=True)
        _atomic_json(artifact_path, artifact)
        if status == "promoted":
            if not _safe_v2_artifact(artifact):
                raise ValueError("refusing to activate predictor without the v2 safety gate")
            artifact_sha = _file_sha256(artifact_path)
            _atomic_json(self.config.prediction_model_dir / "active.json", {
                "contract": ACTIVE_MODEL_CONTRACT,
                "model_version": model_version,
                "artifact_file": artifact_path.name,
                "artifact_sha256": artifact_sha,
                "promoted_at": artifact["trained_at"],
            })
        artifact["operation"] = "trained"
        return artifact

    def status(self) -> Dict[str, Any]:
        active = load_active_model(self.config)
        active_status = (
            {**active, "model_purpose": model_purpose(active)}
            if active is not None else None
        )
        registry = []
        if self.config.prediction_model_dir.is_dir():
            for path in sorted(self.config.prediction_model_dir.glob("*.json")):
                if path.name == "active.json":
                    continue
                try:
                    artifact = _read_json(path)
                except (OSError, ValueError, json.JSONDecodeError):
                    continue
                if artifact.get("contract") not in {
                    LEGACY_MODEL_CONTRACT,
                    MODEL_CONTRACT,
                }:
                    continue
                registry.append({
                    "contract": artifact.get("contract"),
                    "model_version": artifact.get("model_version"),
                    "model_family": artifact.get("model_family"),
                    "model_purpose": model_purpose(artifact),
                    "status": artifact.get("status"),
                    "activation_safety": _activation_safety(artifact),
                    "trained_at": artifact.get("trained_at"),
                    "labels": (artifact.get("training") or {}).get("labels"),
                    "cross_validation": artifact.get("cross_validation"),
                    "artifact_path": str(path),
                })
        registry.sort(key=lambda row: (
            str(row.get("trained_at") or ""),
            str(row.get("model_version") or ""),
        ), reverse=True)
        return {
            "contract": "market_tape_predictor_registry_v2",
            "state": "active" if active else "no_promoted_model",
            "active_model": active_status,
            "models": registry,
        }

    def _training_rows(self) -> List[Dict[str, Any]]:
        with self.store.connect() as connection:
            raw_rows = [dict(row) for row in connection.execute(
                """SELECT prediction_id, subject_id, predicted_at, horizon,
                          features_json, outcome_json
                   FROM mt_predictions
                   WHERE subject_type = 'trend'
                     AND model_version IN (?, ?, ?)
                     AND outcome_json IS NOT NULL
                   ORDER BY predicted_at, prediction_id""",
                TRAINING_SOURCE_MODELS,
            ).fetchall()]
        rows: List[Dict[str, Any]] = []
        for row in raw_rows:
            try:
                outcome = json.loads(row["outcome_json"])
                features = json.loads(row["features_json"])
            except (TypeError, ValueError, json.JSONDecodeError):
                continue
            if outcome.get("state") != "scored":
                continue
            if features.get("index_version") != TRAINING_INDEX_VERSION:
                continue
            if features.get("observation_quality_contract") != (
                OBSERVATION_QUALITY_CONTRACT
            ):
                continue
            if not eligible_for_early_entry(features):
                continue
            predicted_at = _as_utc(row["predicted_at"])
            label_available_at = _label_available_at(
                predicted_at,
                str(row.get("horizon") or ENTRY_HORIZON),
                outcome,
            )
            rows.append({
                "subject_id": str(row["subject_id"]),
                "predicted_at": isoformat(predicted_at),
                "label_available_at": isoformat(label_available_at),
                "actual": int(bool(outcome.get("actual"))),
                "features": _feature_vector(features),
            })
        return rows

    @staticmethod
    def _cross_validate(rows: Sequence[Dict[str, Any]], folds: int) -> Dict[str, Any]:
        ordered = sorted(
            rows,
            key=lambda row: (
                _as_utc(row["predicted_at"]),
                str(row["subject_id"]),
            ),
        )
        timestamps = sorted({_as_utc(row["predicted_at"]) for row in ordered})
        predictions: List[Tuple[float, int]] = []
        baselines: List[Tuple[float, int]] = []
        fold_receipts: List[Dict[str, Any]] = []
        validation_rows_considered = 0
        validation_rows_abstained = 0
        if len(timestamps) < folds + 1:
            return {
                "state": "insufficient_walk_forward_folds",
                "method": "purged_grouped_walk_forward",
                "requested_folds": folds,
                "folds": 0,
                "labels": 0,
                "positives": 0,
                "brier_score": None,
                "baseline_brier_score": None,
                "brier_skill_score": None,
                "roc_auc": None,
                "prediction_coverage": 0.0,
                "chronological_order_passed": False,
                "group_isolation_passed": False,
                "label_embargo_passed": False,
                "fold_receipts": [],
            }

        boundary_indices = [
            min(len(timestamps) - 1, (len(timestamps) * step) // (folds + 1))
            for step in range(1, folds + 1)
        ]
        boundaries = [timestamps[index] for index in boundary_indices]
        for fold, validation_start in enumerate(boundaries):
            validation_end = (
                boundaries[fold + 1]
                if fold + 1 < len(boundaries)
                else None
            )
            validation = [
                row for row in ordered
                if _as_utc(row["predicted_at"]) >= validation_start
                and (
                    validation_end is None
                    or _as_utc(row["predicted_at"]) < validation_end
                )
            ]
            validation_subjects = {row["subject_id"] for row in validation}
            chronological_training = [
                row for row in ordered
                if _as_utc(row["predicted_at"]) < validation_start
            ]
            mature_training = [
                row for row in chronological_training
                if _as_utc(row["label_available_at"]) <= validation_start
            ]
            training = [
                row for row in mature_training
                if row["subject_id"] not in validation_subjects
            ]
            training_subjects = {row["subject_id"] for row in training}
            overlap = sorted(training_subjects & validation_subjects)
            receipt: Dict[str, Any] = {
                "fold": fold,
                "state": "measured",
                "training_rows": len(training),
                "validation_rows": len(validation),
                "training_subjects": len(training_subjects),
                "validation_subjects": len(validation_subjects),
                "validation_positives": sum(row["actual"] for row in validation),
                "validation_start": isoformat(validation_start),
                "validation_end_exclusive": (
                    isoformat(validation_end) if validation_end is not None else None
                ),
                "training_max_predicted_at": _maximum_timestamp(
                    training,
                    "predicted_at",
                ),
                "training_max_label_available_at": _maximum_timestamp(
                    training,
                    "label_available_at",
                ),
                "purged_unmatured_rows": (
                    len(chronological_training) - len(mature_training)
                ),
                "purged_overlapping_group_rows": (
                    len(mature_training) - len(training)
                ),
                "group_overlap_count": len(overlap),
                "chronological_order_passed": bool(
                    training
                    and max(_as_utc(row["predicted_at"]) for row in training)
                    < min(_as_utc(row["predicted_at"]) for row in validation)
                ) if validation else False,
                "label_embargo_passed": bool(
                    training
                    and max(_as_utc(row["label_available_at"]) for row in training)
                    <= min(_as_utc(row["predicted_at"]) for row in validation)
                ) if validation else False,
            }
            training_classes = {int(row["actual"]) for row in training}
            validation_classes = {int(row["actual"]) for row in validation}
            if not training or not validation:
                receipt.update({"state": "skipped", "reason": "empty_temporal_partition"})
                fold_receipts.append(receipt)
                continue
            if training_classes != {0, 1}:
                receipt.update({"state": "skipped", "reason": "training_class_missing"})
                fold_receipts.append(receipt)
                continue
            if validation_classes != {0, 1}:
                receipt.update({"state": "skipped", "reason": "validation_class_missing"})
                fold_receipts.append(receipt)
                continue
            if overlap:
                receipt.update({"state": "skipped", "reason": "group_overlap"})
                fold_receipts.append(receipt)
                continue
            if not receipt["chronological_order_passed"]:
                receipt.update({"state": "skipped", "reason": "chronology_violation"})
                fold_receipts.append(receipt)
                continue
            if not receipt["label_embargo_passed"]:
                receipt.update({"state": "skipped", "reason": "label_embargo_violation"})
                fold_receipts.append(receipt)
                continue

            model = _fit_logistic(training)
            prevalence = sum(row["actual"] for row in training) / len(training)
            fold_predictions: List[Tuple[float, int]] = []
            fold_abstentions = 0
            validation_rows_considered += len(validation)
            validation_artifact = {
                "contract": MODEL_CONTRACT,
                "model": model,
                "inference_policy": _inference_policy(model),
            }
            for row in validation:
                decision = predict_trend_snapshot(
                    validation_artifact,
                    row["features"],
                )
                if decision["state"] == "abstained":
                    fold_abstentions += 1
                    continue
                fold_predictions.append(
                    (float(decision["probability"]), int(row["actual"]))
                )
            validation_rows_abstained += fold_abstentions
            predictions.extend(fold_predictions)
            baselines.extend(
                (prevalence, actual) for _, actual in fold_predictions
            )
            receipt.update({
                "predictions": len(fold_predictions),
                "abstentions": fold_abstentions,
                "prediction_coverage": round(
                    len(fold_predictions) / len(validation),
                    6,
                ),
            })
            fold_receipts.append(receipt)

        measured_folds = [
            receipt for receipt in fold_receipts
            if receipt.get("state") == "measured"
        ]
        if not predictions or not baselines:
            return {
                "state": "insufficient_walk_forward_folds",
                "method": "purged_grouped_walk_forward",
                "requested_folds": folds,
                "folds": len(measured_folds),
                "labels": 0,
                "positives": 0,
                "brier_score": None,
                "baseline_brier_score": None,
                "brier_skill_score": None,
                "roc_auc": None,
                "prediction_coverage": 0.0,
                "chronological_order_passed": all(
                    receipt.get("chronological_order_passed", False)
                    for receipt in measured_folds
                ) and bool(measured_folds),
                "group_isolation_passed": all(
                    receipt.get("group_overlap_count") == 0
                    for receipt in measured_folds
                ) and bool(measured_folds),
                "label_embargo_passed": all(
                    receipt.get("label_embargo_passed", False)
                    for receipt in measured_folds
                ) and bool(measured_folds),
                "fold_receipts": fold_receipts,
            }
        brier = _brier(predictions)
        baseline_brier = _brier(baselines)
        skill = 1.0 - brier / baseline_brier if baseline_brier > 0 else 0.0
        coverage = (
            (validation_rows_considered - validation_rows_abstained)
            / validation_rows_considered
            if validation_rows_considered else 0.0
        )
        return {
            "state": (
                "measured"
                if len(measured_folds) >= MINIMUM_WALK_FORWARD_FOLDS
                else "insufficient_walk_forward_folds"
            ),
            "method": "purged_grouped_walk_forward",
            "requested_folds": folds,
            "folds": len(measured_folds),
            "labels": len(predictions),
            "positives": sum(actual for _, actual in predictions),
            "brier_score": round(brier, 6),
            "baseline_brier_score": round(baseline_brier, 6),
            "brier_skill_score": round(skill, 6),
            "roc_auc": _roc_auc(predictions),
            "prediction_coverage": round(coverage, 6),
            "abstentions": validation_rows_abstained,
            "chronological_order_passed": all(
                receipt["chronological_order_passed"] for receipt in measured_folds
            ) and bool(measured_folds),
            "group_isolation_passed": all(
                receipt["group_overlap_count"] == 0 for receipt in measured_folds
            ) and bool(measured_folds),
            "label_embargo_passed": all(
                receipt["label_embargo_passed"] for receipt in measured_folds
            ) and bool(measured_folds),
            "mean_probability": round(
                sum(probability for probability, _ in predictions) / len(predictions),
                6,
            ),
            "fold_receipts": fold_receipts,
        }


def load_active_model(config: MarketTapeConfig) -> Dict[str, Any] | None:
    pointer_path = config.prediction_model_dir / "active.json"
    if not pointer_path.is_file():
        return None
    try:
        pointer = _read_json(pointer_path)
        if pointer.get("contract") not in {
            LEGACY_ACTIVE_CONTRACT,
            ACTIVE_MODEL_CONTRACT,
        }:
            return None
        artifact_file = Path(str(pointer["artifact_file"])).name
        artifact_path = config.prediction_model_dir / artifact_file
        if _file_sha256(artifact_path) != pointer["artifact_sha256"]:
            return None
        artifact = _read_json(artifact_path)
    except (OSError, KeyError, TypeError, ValueError, json.JSONDecodeError):
        return None
    if artifact.get("status") != "promoted":
        return None
    if (artifact.get("training") or {}).get(
        "observation_quality_contract"
    ) != OBSERVATION_QUALITY_CONTRACT:
        return None
    contract = artifact.get("contract")
    if contract == MODEL_CONTRACT and not _safe_v2_artifact(artifact):
        return None
    if contract not in {LEGACY_MODEL_CONTRACT, MODEL_CONTRACT}:
        return None
    return artifact


def model_purpose(artifact: Dict[str, Any]) -> str:
    explicit = str(artifact.get("model_purpose") or "").strip()
    if explicit:
        return explicit
    family = str(artifact.get("model_family") or "")
    if family.startswith("grouped-logistic-v2"):
        return "market_state_progression"
    return "unspecified"


def model_prediction_horizon(artifact: Dict[str, Any]) -> str:
    if model_purpose(artifact) == MODEL_PURPOSE:
        return ENTRY_HORIZON
    return PROGRESSION_HORIZON


def eligible_for_early_entry(features: Dict[str, Any]) -> bool:
    state = str(features.get("state") or "discovering").casefold()
    try:
        strength = float(features.get("trend_strength") or 0.0)
    except (TypeError, ValueError):
        strength = 0.0
    try:
        videos = int(features.get("videos_total") or 0)
        creators = int(features.get("creators_total") or 0)
    except (TypeError, ValueError):
        return False
    return (
        state not in BREAKOUT_STATES
        and strength < 70.0
        and videos >= MINIMUM_EVIDENCE_VIDEOS
        and creators >= MINIMUM_EVIDENCE_CREATORS
    )


def model_accepts_features(artifact: Dict[str, Any], features: Dict[str, Any]) -> bool:
    if (artifact.get("training") or {}).get(
        "observation_quality_contract"
    ) != OBSERVATION_QUALITY_CONTRACT:
        return False
    if features.get("observation_quality_contract") != (
        OBSERVATION_QUALITY_CONTRACT
    ):
        return False
    training_index_version = str(
        (artifact.get("training") or {}).get("index_version")
        or "trend-strength-v1"
    )
    if str(features.get("index_version") or "trend-strength-v1") != training_index_version:
        return False
    if model_purpose(artifact) == MODEL_PURPOSE:
        return eligible_for_early_entry(features)
    return True


def predict_trend_snapshot(
    artifact: Dict[str, Any],
    features: Dict[str, Any] | Sequence[float],
) -> Dict[str, Any]:
    """Return a bounded probability or an auditable OOD abstention."""
    model = artifact.get("model") or {}
    means = model.get("means") or []
    standard_deviations = model.get("standard_deviations") or []
    coefficients = model.get("coefficients") or []
    if not (
        len(means) == len(FEATURES)
        and len(standard_deviations) == len(FEATURES)
        and len(coefficients) == len(FEATURES)
    ):
        raise ValueError("predictor artifact has invalid coefficient dimensions")

    policy = _resolved_inference_policy(artifact)
    raw_values, input_issues = _validated_feature_values(features)
    diagnostics: Dict[str, Any] = {
        "policy_contract": policy["contract"],
        "policy_source": policy["source"],
        "model_contract": artifact.get("contract") or "unspecified",
        "model_version": artifact.get("model_version"),
        "reasons": list(input_issues),
        "ood_features": [],
        "standardized_features": {},
        "maximum_absolute_standardized_value": 0.0,
        "probability_bounds": list(policy["probability_bounds"]),
    }
    if input_issues:
        return {
            "contract": INFERENCE_CONTRACT,
            "state": "abstained",
            "probability": None,
            "diagnostics": diagnostics,
        }

    standardized: List[float] = []
    profiles = model.get("feature_profiles") or {}
    for index, feature in enumerate(FEATURES):
        value = raw_values[index]
        lower, upper = policy["feature_bounds"][feature]
        scale = max(1e-12, float(standard_deviations[index]))
        standardized_value = (value - float(means[index])) / scale
        diagnostics["standardized_features"][feature] = round(
            standardized_value,
            6,
        )
        diagnostics["maximum_absolute_standardized_value"] = max(
            diagnostics["maximum_absolute_standardized_value"],
            abs(standardized_value),
        )
        feature_reasons: List[str] = []
        if value < float(lower) or value > float(upper):
            feature_reasons.append("outside_contract_bounds")
        if abs(standardized_value) > float(
            policy["maximum_absolute_standardized_value"]
        ):
            feature_reasons.append("standardized_distance_exceeded")
        profile = profiles.get(feature) or {}
        if profile:
            support_margin = (
                float(policy["support_margin_standard_deviations"]) * scale
            )
            support_lower = float(profile["minimum"]) - support_margin
            support_upper = float(profile["maximum"]) + support_margin
            if value < support_lower or value > support_upper:
                feature_reasons.append("outside_training_support")
        if feature_reasons:
            diagnostics["ood_features"].append({
                "feature": feature,
                "value": round(value, 12),
                "standardized_value": round(standardized_value, 6),
                "reasons": feature_reasons,
            })
        standardized.append(max(
            -float(policy["maximum_absolute_standardized_value"]),
            min(
                float(policy["maximum_absolute_standardized_value"]),
                standardized_value,
            ),
        ))

    diagnostics["maximum_absolute_standardized_value"] = round(
        diagnostics["maximum_absolute_standardized_value"],
        6,
    )
    if diagnostics["ood_features"]:
        diagnostics["reasons"].append("out_of_distribution")
        return {
            "contract": INFERENCE_CONTRACT,
            "state": "abstained",
            "probability": None,
            "diagnostics": diagnostics,
        }

    score = float(model["intercept"]) + sum(
        float(coefficient) * value
        for coefficient, value in zip(coefficients, standardized)
    )
    raw_probability = _sigmoid(score)
    probability_lower, probability_upper = policy["probability_bounds"]
    bounded_probability = min(
        float(probability_upper),
        max(float(probability_lower), raw_probability),
    )
    diagnostics.update({
        "raw_probability": round(raw_probability, 12),
        "probability_was_bounded": not math.isclose(
            raw_probability,
            bounded_probability,
            rel_tol=0.0,
            abs_tol=1e-12,
        ),
    })
    return {
        "contract": INFERENCE_CONTRACT,
        "state": "predicted",
        "probability": round(bounded_probability, 6),
        "diagnostics": diagnostics,
    }


def predict_probability(
    artifact: Dict[str, Any],
    features: Dict[str, Any] | Sequence[float],
) -> float:
    decision = predict_trend_snapshot(artifact, features)
    if decision["state"] != "predicted":
        reasons = ",".join(decision["diagnostics"]["reasons"])
        raise ValueError(f"predictor abstained: {reasons}")
    return float(decision["probability"])


def _promotion_gate(
    cross_validation: Dict[str, Any],
    *,
    labels: int,
    positives: int,
    negatives: int,
    minimum_labels: int,
    minimum_positive_labels: int,
) -> Dict[str, Any]:
    checks = {
        "minimum_labels": labels >= minimum_labels,
        "minimum_class_labels": (
            positives >= minimum_positive_labels
            and negatives >= minimum_positive_labels
        ),
        "validation_method": (
            cross_validation.get("method") == "purged_grouped_walk_forward"
        ),
        "minimum_walk_forward_folds": (
            int(cross_validation.get("folds") or 0)
            >= MINIMUM_WALK_FORWARD_FOLDS
        ),
        "chronological_order": bool(
            cross_validation.get("chronological_order_passed")
        ),
        "group_isolation": bool(
            cross_validation.get("group_isolation_passed")
        ),
        "label_embargo": bool(cross_validation.get("label_embargo_passed")),
        "inference_coverage": (
            float(cross_validation.get("prediction_coverage") or 0.0)
            >= MINIMUM_INFERENCE_COVERAGE
        ),
        "positive_brier_skill": (
            cross_validation.get("brier_skill_score") is not None
            and float(cross_validation["brier_skill_score"]) > 0.05
        ),
        "minimum_roc_auc": (
            cross_validation.get("roc_auc") is not None
            and float(cross_validation["roc_auc"]) >= 0.65
        ),
    }
    return {
        "contract": PROMOTION_GATE_CONTRACT,
        "passed": all(checks.values()),
        "checks": checks,
        "failure_reasons": [
            name for name, passed in checks.items() if not passed
        ],
        "thresholds": {
            "minimum_walk_forward_folds": MINIMUM_WALK_FORWARD_FOLDS,
            "minimum_inference_coverage": MINIMUM_INFERENCE_COVERAGE,
            "minimum_brier_skill_score_exclusive": 0.05,
            "minimum_roc_auc": 0.65,
        },
    }


def _safe_v2_artifact(artifact: Dict[str, Any]) -> bool:
    gate = artifact.get("promotion_gate") or {}
    validation = artifact.get("cross_validation") or {}
    policy = artifact.get("inference_policy") or {}
    checks = gate.get("checks") or {}
    fold_receipts = [
        receipt for receipt in validation.get("fold_receipts") or []
        if receipt.get("state") == "measured"
    ]
    return bool(
        artifact.get("contract") == MODEL_CONTRACT
        and int(artifact.get("schema_version") or 0) == 2
        and artifact.get("model_family") == MODEL_FAMILY
        and (artifact.get("training") or {}).get(
            "observation_quality_contract"
        ) == OBSERVATION_QUALITY_CONTRACT
        and artifact.get("status") == "promoted"
        and gate.get("contract") == PROMOTION_GATE_CONTRACT
        and gate.get("passed") is True
        and checks
        and all(value is True for value in checks.values())
        and validation.get("method") == "purged_grouped_walk_forward"
        and validation.get("state") == "measured"
        and len(fold_receipts) >= MINIMUM_WALK_FORWARD_FOLDS
        and all(receipt.get("group_overlap_count") == 0 for receipt in fold_receipts)
        and all(receipt.get("chronological_order_passed") is True for receipt in fold_receipts)
        and all(receipt.get("label_embargo_passed") is True for receipt in fold_receipts)
        and policy.get("contract") == INFERENCE_POLICY_CONTRACT
        and artifact.get("model")
    )


def _activation_safety(artifact: Dict[str, Any]) -> str:
    if artifact.get("contract") == MODEL_CONTRACT:
        return "walk_forward_verified" if _safe_v2_artifact(artifact) else "not_activatable"
    if artifact.get("contract") == LEGACY_MODEL_CONTRACT:
        return "legacy_compatible_not_newly_promotable"
    return "unsupported"


def _inference_policy(model: Dict[str, Any] | None) -> Dict[str, Any]:
    return {
        "contract": INFERENCE_POLICY_CONTRACT,
        "source": "artifact_v2",
        "required_features": list(FEATURES),
        "feature_bounds": {
            feature: list(bounds) for feature, bounds in FEATURE_CLIPS.items()
        },
        "standardization": "training_mean_and_scale",
        "maximum_absolute_standardized_value": MAXIMUM_STANDARDIZED_DISTANCE,
        "support_margin_standard_deviations": SUPPORT_MARGIN_STANDARD_DEVIATIONS,
        "out_of_distribution_action": "abstain",
        "probability_bounds": list(PROBABILITY_BOUNDS),
        "training_profile_present": bool(
            model and model.get("feature_profiles")
        ),
    }


def _resolved_inference_policy(artifact: Dict[str, Any]) -> Dict[str, Any]:
    configured = artifact.get("inference_policy") or {}
    if (
        artifact.get("contract") == MODEL_CONTRACT
        and configured.get("contract") == INFERENCE_POLICY_CONTRACT
    ):
        policy = dict(configured)
        policy["source"] = "artifact_v2"
        return policy
    policy = _inference_policy(artifact.get("model") or {})
    policy.update({
        "source": "legacy_derived",
        "support_margin_standard_deviations": 0.0,
    })
    return policy


def _validated_feature_values(
    features: Dict[str, Any] | Sequence[float],
) -> Tuple[List[float], List[str]]:
    values: List[float] = []
    issues: List[str] = []
    if not isinstance(features, dict) and len(features) != len(FEATURES):
        return [], ["invalid_feature_count"]
    for index, feature in enumerate(FEATURES):
        if isinstance(features, dict) and feature not in features:
            issues.append(f"missing_feature:{feature}")
            values.append(0.0)
            continue
        try:
            raw = features[feature] if isinstance(features, dict) else features[index]
            value = float(raw)
        except (IndexError, KeyError, TypeError, ValueError):
            issues.append(f"invalid_feature:{feature}")
            values.append(0.0)
            continue
        if not math.isfinite(value):
            issues.append(f"non_finite_feature:{feature}")
            value = 0.0
        values.append(value)
    return values, issues


def _label_available_at(
    predicted_at: datetime,
    horizon: str,
    outcome: Dict[str, Any],
) -> datetime:
    available_at = predicted_at + timedelta(hours=_horizon_hours(horizon))
    follow_up_at = outcome.get("follow_up_at")
    if follow_up_at:
        try:
            available_at = max(available_at, _as_utc(follow_up_at))
        except (TypeError, ValueError):
            pass
    return available_at


def _horizon_hours(horizon: str) -> float:
    match = re.search(r"within_(\d+(?:\.\d+)?)h", horizon)
    if match:
        return max(0.0, float(match.group(1)))
    return 6.0


def _as_utc(value: Any) -> datetime:
    if isinstance(value, datetime):
        parsed = value
    else:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _maximum_timestamp(
    rows: Sequence[Dict[str, Any]],
    field: str,
) -> str | None:
    if not rows:
        return None
    return isoformat(max(_as_utc(row[field]) for row in rows))


def _fit_logistic(rows: Sequence[Dict[str, Any]]) -> Dict[str, Any]:
    vectors = [row["features"] for row in rows]
    labels = [int(row["actual"]) for row in rows]
    width = len(FEATURES)
    means = [sum(vector[index] for vector in vectors) / len(vectors) for index in range(width)]
    deviations = [
        max(
            1e-6 * (FEATURE_CLIPS[FEATURES[index]][1] - FEATURE_CLIPS[FEATURES[index]][0]),
            math.sqrt(sum(
                (vector[index] - means[index]) ** 2 for vector in vectors
            ) / len(vectors)),
        )
        for index in range(width)
    ]
    standardized = [
        [(vector[index] - means[index]) / deviations[index] for index in range(width)]
        for vector in vectors
    ]
    prevalence = (sum(labels) + 0.5) / (len(labels) + 1.0)
    intercept = math.log(prevalence / (1.0 - prevalence))
    coefficients = [0.0] * width
    for iteration in range(2000):
        intercept_gradient = 0.0
        gradients = [0.0] * width
        for vector, actual in zip(standardized, labels):
            probability = _sigmoid(
                intercept + sum(
                    coefficient * value
                    for coefficient, value in zip(coefficients, vector)
                )
            )
            error = probability - actual
            intercept_gradient += error
            for index, value in enumerate(vector):
                gradients[index] += error * value
        rate = 0.1 / (1.0 + iteration / 500.0)
        intercept -= rate * intercept_gradient / len(labels)
        for index in range(width):
            coefficients[index] -= rate * (
                gradients[index] / len(labels) + 0.05 * coefficients[index]
            )
    return {
        "features": list(FEATURES),
        "means": [round(value, 12) for value in means],
        "standard_deviations": [round(value, 12) for value in deviations],
        "feature_profiles": {
            feature: {
                "minimum": round(min(vector[index] for vector in vectors), 12),
                "maximum": round(max(vector[index] for vector in vectors), 12),
                "mean": round(means[index], 12),
                "standard_deviation": round(deviations[index], 12),
            }
            for index, feature in enumerate(FEATURES)
        },
        "coefficients": [round(value, 12) for value in coefficients],
        "intercept": round(intercept, 12),
    }


def _feature_vector(features: Dict[str, Any] | Sequence[float]) -> List[float]:
    vector = []
    for index, feature in enumerate(FEATURES):
        lower, upper = FEATURE_CLIPS[feature]
        try:
            raw = (
                features.get(feature, 0.0)
                if isinstance(features, dict)
                else features[index]
            )
            value = float(raw or 0.0)
        except (IndexError, TypeError, ValueError):
            value = 0.0
        vector.append(min(upper, max(lower, value)))
    return vector


def _stratified_group_folds(
    rows: Sequence[Dict[str, Any]],
    folds: int,
) -> Dict[str, int]:
    group_labels: Dict[str, int] = {}
    for row in rows:
        group = row["subject_id"]
        group_labels[group] = max(group_labels.get(group, 0), int(row["actual"]))
    assignments: Dict[str, int] = {}
    for label in (0, 1):
        groups = sorted(
            (group for group, actual in group_labels.items() if actual == label),
            key=lambda group: stable_hash({"model": MODEL_FAMILY, "subject_id": group}),
        )
        for index, group in enumerate(groups):
            assignments[group] = index % folds
    return assignments


def _brier(values: Sequence[Tuple[float, int]]) -> float:
    return sum((probability - actual) ** 2 for probability, actual in values) / len(values)


def _roc_auc(values: Sequence[Tuple[float, int]]) -> float | None:
    positives = [probability for probability, actual in values if actual]
    negatives = [probability for probability, actual in values if not actual]
    if not positives or not negatives:
        return None
    score = sum(
        int(positive > negative) + 0.5 * int(positive == negative)
        for positive in positives
        for negative in negatives
    ) / (len(positives) * len(negatives))
    return round(score, 6)


def _sigmoid(value: float) -> float:
    bounded = max(-30.0, min(30.0, value))
    return 1.0 / (1.0 + math.exp(-bounded))


def _read_json(path: Path) -> Dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _atomic_json(path: Path, payload: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(payload, indent=2, sort_keys=True, default=str) + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)
