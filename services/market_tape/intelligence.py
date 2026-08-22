"""One audited, read-only snapshot of keyword and trend intelligence."""

from __future__ import annotations

from typing import Any, Dict

from .config import MarketTapeConfig
from .dataset import MarketTapeDatasetManager
from .models import isoformat, utc_now
from .predictor import MarketTapePredictor
from .store import MarketTapeStore


INTELLIGENCE_CONTRACT = "market_tape_intelligence_snapshot_v1"


def build_intelligence_snapshot(
    config: MarketTapeConfig,
    store: MarketTapeStore,
    *,
    limit: int = 25,
    window_hours: int = 168,
    minimum_videos: int = 2,
) -> Dict[str, Any]:
    """Compose existing measured surfaces without triggering collection or training."""

    bounded_limit = min(100, max(1, int(limit)))
    bounded_window = min(24 * 90, max(1, int(window_hours)))
    bounded_minimum = min(1000, max(1, int(minimum_videos)))
    service_status = store.status()
    opportunities = store.trend_opportunities(
        limit=bounded_limit,
        min_videos=bounded_minimum,
        min_measured_videos=bounded_minimum,
    )
    admission = opportunities.get("model_admission") or {}
    forecast_admitted = bool(admission.get("admitted_for_ranking"))
    predictor = MarketTapePredictor(config, store).status()
    active = predictor.get("active_model") or {}
    active_summary = {
        key: active.get(key)
        for key in (
            "contract",
            "model_version",
            "model_family",
            "model_purpose",
            "status",
            "trained_at",
            "training_dataset_sha256",
        )
        if active.get(key) is not None
    }
    return {
        "contract": INTELLIGENCE_CONTRACT,
        "state": "forecast_ready" if forecast_admitted else "observed_only",
        "generated_at": isoformat(utc_now()),
        "read_only": True,
        "parameters": {
            "limit": bounded_limit,
            "window_hours": bounded_window,
            "minimum_videos": bounded_minimum,
        },
        "lineage": {
            "live_database_path": str(config.db_path),
            "live_schema_version": service_status.get("schema_version"),
            "live_totals": service_status.get("totals") or {},
            "latest_collection_run": service_status.get("latest_run"),
            "passport_dataset": MarketTapeDatasetManager(config, store).status(),
        },
        "keywords": {
            "derived_terms": store.keyword_signals(
                bounded_limit,
                bounded_window,
                bounded_minimum,
            ),
            "exact_discovery_queries": store.discovery_query_signals(
                bounded_limit,
                bounded_window,
                bounded_minimum,
            ),
            "score_semantics": (
                "relative evidence rank, not probability; current-clock freshness, "
                "creator/platform breadth, repeat observations, performance, and "
                "concentration are retained with examples"
            ),
        },
        "trends": opportunities,
        "forecast": {
            "probability_admitted": forecast_admitted,
            "admission": admission,
            "active_model": active_summary or None,
            "registry_contract": predictor.get("contract"),
            "backtest": store.prediction_backtest(),
            "score_semantics": (
                "opportunity_score is never probability; a probability appears only "
                "for an unexpired exact-active-model forecast after prospective "
                "label, class, independent-subject, time-batch, Brier-skill, and "
                "calibration gates pass"
            ),
        },
    }


__all__ = ["INTELLIGENCE_CONTRACT", "build_intelligence_snapshot"]

