"""Versioned, dependency-free trend predictor training and inference."""

from __future__ import annotations

import hashlib
import json
import math
from pathlib import Path
from typing import Any, Dict, List, Sequence, Tuple

from .config import MarketTapeConfig
from .models import isoformat, stable_hash, utc_now


MODEL_CONTRACT = "market_tape_trend_predictor_v1"
MODEL_FAMILY = "early-breakout-logistic-v3"
MODEL_PURPOSE = "early_breakout_entry"
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
        dataset_hash = stable_hash([
            {
                "subject_id": row["subject_id"],
                "predicted_at": row["predicted_at"],
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
            qualified = (
                cross_validation["brier_skill_score"] > 0.05
                and cross_validation["roc_auc"] is not None
                and cross_validation["roc_auc"] >= 0.65
            )
            status = "promoted" if qualified else "rejected"

        artifact = {
            "contract": MODEL_CONTRACT,
            "schema_version": 1,
            "model_family": MODEL_FAMILY,
            "model_purpose": MODEL_PURPOSE,
            "model_version": model_version,
            "status": status,
            "trained_at": isoformat(utc_now()),
            "training_dataset_sha256": dataset_hash,
            "training": {
                "source_model_versions": list(TRAINING_SOURCE_MODELS),
                "index_version": TRAINING_INDEX_VERSION,
                "horizon": ENTRY_HORIZON,
                "eligibility": {
                    "initial_states_excluded": sorted(BREAKOUT_STATES),
                    "maximum_initial_trend_strength_exclusive": 70.0,
                },
                "labels": labels,
                "positives": positives,
                "negatives": negatives,
                "grouping": "subject_id",
            },
            "cross_validation": cross_validation,
            "parameters": {
                "features": list(FEATURES),
                "feature_clips": {
                    key: list(value) for key, value in FEATURE_CLIPS.items()
                },
                "l2": 0.05,
                "learning_rate": 0.1,
                "iterations": 2000,
            },
            "model": model,
            "retestable": True,
        }
        self.config.prediction_model_dir.mkdir(parents=True, exist_ok=True)
        _atomic_json(artifact_path, artifact)
        if status == "promoted":
            artifact_sha = _file_sha256(artifact_path)
            _atomic_json(self.config.prediction_model_dir / "active.json", {
                "contract": "market_tape_active_predictor_v1",
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
                if artifact.get("contract") != MODEL_CONTRACT:
                    continue
                registry.append({
                    "model_version": artifact.get("model_version"),
                    "model_family": artifact.get("model_family"),
                    "model_purpose": model_purpose(artifact),
                    "status": artifact.get("status"),
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
            "contract": "market_tape_predictor_registry_v1",
            "state": "active" if active else "no_promoted_model",
            "active_model": active_status,
            "models": registry,
        }

    def _training_rows(self) -> List[Dict[str, Any]]:
        with self.store.connect() as connection:
            raw_rows = [dict(row) for row in connection.execute(
                """SELECT subject_id, predicted_at, features_json, outcome_json
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
            if not eligible_for_early_entry(features):
                continue
            rows.append({
                "subject_id": str(row["subject_id"]),
                "predicted_at": str(row["predicted_at"]),
                "actual": int(bool(outcome.get("actual"))),
                "features": _feature_vector(features),
            })
        return rows

    @staticmethod
    def _cross_validate(rows: Sequence[Dict[str, Any]], folds: int) -> Dict[str, Any]:
        assignments = _stratified_group_folds(rows, folds)
        predictions: List[Tuple[float, int]] = []
        baselines: List[Tuple[float, int]] = []
        fold_receipts = []
        for fold in range(folds):
            training = [row for row in rows if assignments[row["subject_id"]] != fold]
            validation = [row for row in rows if assignments[row["subject_id"]] == fold]
            model = _fit_logistic(training)
            prevalence = sum(row["actual"] for row in training) / len(training)
            fold_predictions = [
                (predict_probability({"model": model}, row["features"]), row["actual"])
                for row in validation
            ]
            predictions.extend(fold_predictions)
            baselines.extend((prevalence, row["actual"]) for row in validation)
            fold_receipts.append({
                "fold": fold,
                "training_rows": len(training),
                "validation_rows": len(validation),
                "validation_positives": sum(row["actual"] for row in validation),
            })
        brier = _brier(predictions)
        baseline_brier = _brier(baselines)
        skill = 1.0 - brier / baseline_brier if baseline_brier > 0 else 0.0
        return {
            "state": "measured",
            "method": "deterministic_stratified_group_kfold",
            "folds": folds,
            "labels": len(predictions),
            "positives": sum(actual for _, actual in predictions),
            "brier_score": round(brier, 6),
            "baseline_brier_score": round(baseline_brier, 6),
            "brier_skill_score": round(skill, 6),
            "roc_auc": _roc_auc(predictions),
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
        artifact_file = Path(str(pointer["artifact_file"])).name
        artifact_path = config.prediction_model_dir / artifact_file
        if _file_sha256(artifact_path) != pointer["artifact_sha256"]:
            return None
        artifact = _read_json(artifact_path)
    except (OSError, KeyError, TypeError, ValueError, json.JSONDecodeError):
        return None
    if artifact.get("contract") != MODEL_CONTRACT or artifact.get("status") != "promoted":
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
    return state not in BREAKOUT_STATES and strength < 70.0


def model_accepts_features(artifact: Dict[str, Any], features: Dict[str, Any]) -> bool:
    training_index_version = str(
        (artifact.get("training") or {}).get("index_version")
        or "trend-strength-v1"
    )
    if str(features.get("index_version") or "trend-strength-v1") != training_index_version:
        return False
    if model_purpose(artifact) == MODEL_PURPOSE:
        return eligible_for_early_entry(features)
    return True


def predict_probability(
    artifact: Dict[str, Any],
    features: Dict[str, Any] | Sequence[float],
) -> float:
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
    vector = _feature_vector(features)
    score = float(model["intercept"])
    for index, value in enumerate(vector):
        score += float(coefficients[index]) * (
            (value - float(means[index]))
            / max(1e-9, float(standard_deviations[index]))
        )
    return round(_sigmoid(score), 6)


def _fit_logistic(rows: Sequence[Dict[str, Any]]) -> Dict[str, Any]:
    vectors = [row["features"] for row in rows]
    labels = [int(row["actual"]) for row in rows]
    width = len(FEATURES)
    means = [sum(vector[index] for vector in vectors) / len(vectors) for index in range(width)]
    deviations = [
        max(1e-9, math.sqrt(sum(
            (vector[index] - means[index]) ** 2 for vector in vectors
        ) / len(vectors)))
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
