"""One audited, read-only snapshot of keyword and trend intelligence."""

from __future__ import annotations

from datetime import timedelta
from time import perf_counter
from typing import Any, Callable, Dict, List, TypeVar

from .config import MarketTapeConfig
from .dataset import MarketTapeDatasetManager
from .keywords import rank_keywords
from .models import isoformat, utc_now
from .predictor import MarketTapePredictor
from .store import MarketTapeStore


INTELLIGENCE_CONTRACT = "market_tape_intelligence_snapshot_v1"
INTELLIGENCE_PERFORMANCE_CONTRACT = "market_tape_intelligence_performance_v1"
MAXIMUM_KEYWORD_SOURCE_ROWS = 10_000
MINIMUM_KEYWORD_SOURCE_ROWS = 5_000
KEYWORD_ROWS_PER_RESULT = 100

T = TypeVar("T")


def build_intelligence_snapshot(
    config: MarketTapeConfig,
    store: MarketTapeStore,
    *,
    limit: int = 25,
    window_hours: int = 168,
    minimum_videos: int = 2,
) -> Dict[str, Any]:
    """Compose existing measured surfaces without triggering collection or training."""

    started = perf_counter()
    component_ms: Dict[str, float] = {}

    def measured(name: str, operation: Callable[[], T]) -> T:
        component_started = perf_counter()
        result = operation()
        component_ms[name] = round(
            (perf_counter() - component_started) * 1000.0,
            3,
        )
        return result

    bounded_limit = min(100, max(1, int(limit)))
    bounded_window = min(24 * 90, max(1, int(window_hours)))
    bounded_minimum = min(1000, max(1, int(minimum_videos)))
    keyword_source_limit = min(
        MAXIMUM_KEYWORD_SOURCE_ROWS,
        max(MINIMUM_KEYWORD_SOURCE_ROWS, bounded_limit * KEYWORD_ROWS_PER_RESULT),
    )
    service_status = measured("service_status", store.status)
    opportunities = measured(
        "trend_opportunities",
        lambda: store.trend_opportunities(
            limit=bounded_limit,
            min_videos=bounded_minimum,
            min_measured_videos=bounded_minimum,
            candidate_scan_limit=max(500, bounded_limit * 20),
        ),
    )
    admission = opportunities.get("model_admission") or {}
    forecast_admitted = bool(admission.get("admitted_for_ranking"))
    predictor = measured(
        "predictor_status",
        lambda: MarketTapePredictor(config, store).status(),
    )
    keyword_rows = measured(
        "keyword_source_rows",
        lambda: _bounded_keyword_rows(
            store,
            window_hours=bounded_window,
            row_limit=keyword_source_limit,
        ),
    )
    ranking_now = utc_now()
    derived_terms = measured(
        "derived_term_ranking",
        lambda: rank_keywords(
            keyword_rows,
            limit=bounded_limit,
            window_hours=bounded_window,
            min_videos=bounded_minimum,
            now=ranking_now,
        ),
    )
    exact_queries = measured(
        "exact_query_ranking",
        lambda: rank_keywords(
            keyword_rows,
            limit=bounded_limit,
            window_hours=bounded_window,
            min_videos=bounded_minimum,
            now=ranking_now,
            candidate_mode="queries",
        ),
    )
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
    dataset_status = measured(
        "dataset_status",
        lambda: MarketTapeDatasetManager(config, store).status(),
    )
    backtest = measured("prediction_backtest", store.prediction_backtest)
    snapshot = {
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
            "passport_dataset": dataset_status,
        },
        "keywords": {
            "derived_terms": derived_terms,
            "exact_discovery_queries": exact_queries,
            "source_rows_considered": len(keyword_rows),
            "source_row_limit": keyword_source_limit,
            "source_rows_truncated": len(keyword_rows) >= keyword_source_limit,
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
            "backtest": backtest,
            "score_semantics": (
                "opportunity_score is never probability; a probability appears only "
                "for an unexpired exact-active-model forecast after prospective "
                "label, class, independent-subject, time-batch, Brier-skill, and "
                "calibration gates pass"
            ),
        },
    }
    snapshot["performance"] = {
        "contract": INTELLIGENCE_PERFORMANCE_CONTRACT,
        "elapsed_ms": round((perf_counter() - started) * 1000.0, 3),
        "component_ms": component_ms,
        "bounded_inputs": {
            "keyword_source_rows": keyword_source_limit,
            "opportunity_candidate_rows": max(500, bounded_limit * 20),
        },
    }
    return snapshot


def _bounded_keyword_rows(
    store: MarketTapeStore,
    *,
    window_hours: int,
    row_limit: int,
) -> List[Dict[str, Any]]:
    """Read one bounded latest-video rowset for both keyword rankings."""

    cutoff = isoformat(utc_now() - timedelta(hours=max(1, int(window_hours))))
    maximum = min(
        MAXIMUM_KEYWORD_SOURCE_ROWS,
        max(1, int(row_limit)),
    )
    with store.connect() as connection:
        return [dict(row) for row in connection.execute(
            """SELECT video.video_id, video.creator_id, video.platform,
                      video.published_at, video.title, video.caption,
                      video.description, video.url, latest.observed_at,
                      latest.views, latest.likes, latest.comments,
                      latest.shares, latest.view_velocity,
                      genome.hashtags_json,
                      (SELECT COUNT(*) FROM mt_market_observations counted
                       WHERE counted.video_id = video.video_id) AS observation_count,
                      COALESCE((
                          SELECT json_group_array(attribution.query)
                          FROM (
                              SELECT DISTINCT query
                              FROM mt_discovery_attributions
                              WHERE video_id = video.video_id AND query != ''
                          ) attribution
                      ), '[]') AS discovery_queries_json
               FROM mt_videos video
               JOIN mt_market_observations latest
                 ON latest.observation_id = (
                     SELECT current.observation_id
                     FROM mt_market_observations current
                     WHERE current.video_id = video.video_id
                     ORDER BY current.observed_at DESC,
                              current.observation_id DESC
                     LIMIT 1
                 )
               LEFT JOIN mt_content_genomes genome
                 ON genome.video_id = video.video_id
               WHERE video.published_at IS NOT NULL
                 AND video.published_at >= ?
                 AND latest.observed_at >= ?
               ORDER BY latest.observed_at DESC,
                        latest.observation_id DESC
               LIMIT ?""",
            (cutoff, cutoff, maximum),
        ).fetchall()]


__all__ = [
    "INTELLIGENCE_CONTRACT",
    "INTELLIGENCE_PERFORMANCE_CONTRACT",
    "build_intelligence_snapshot",
]
