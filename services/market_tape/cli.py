"""Documented command surface for Market Tape V1."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from .collector import MarketTapeCollector
from .config import MarketTapeConfig
from .daemon import MarketTapeDaemon
from .dataset import MarketTapeDatasetManager
from .intelligence import build_intelligence_snapshot
from .predictor import MarketTapePredictor
from .models import stable_hash
from .semantic import (
    GRAPH_IMPORT_CONTRACT,
    SemanticContractError,
    SemanticTopicService,
    normalize_text,
    validate_topic_graph,
)
from .sources import build_sources
from .sources.upwork import UpworkAPIError
from .sinks import SupabaseSink
from .store import MarketTapeStore
from .upwork_demand import UpworkDemandService


def main() -> int:
    parser = argparse.ArgumentParser(prog="market-tape", description="Autonomous cross-platform social market tape")
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("init")
    cycle = subparsers.add_parser("cycle")
    cycle.add_argument("--mode", choices=["full", "discovery", "recheck"], default="full")
    bootstrap = subparsers.add_parser("bootstrap-local")
    bootstrap.add_argument("--limit-per-platform", type=int, default=10000)
    subparsers.add_parser("backfill-query-attempts")
    reindex = subparsers.add_parser("reindex-trends")
    reindex.add_argument("--forecast-limit", type=int, default=50000)
    daemon = subparsers.add_parser("daemon")
    daemon.add_argument("--once", action="store_true")
    subparsers.add_parser("status")
    intelligence = subparsers.add_parser("intelligence")
    intelligence.add_argument("--limit", type=int, default=25)
    intelligence.add_argument("--window-hours", type=int, default=168)
    intelligence.add_argument("--min-videos", type=int, default=2)
    sync = subparsers.add_parser("sync")
    sync.add_argument("--force", action="store_true", help="Retry backed-off outbox rows immediately")
    sync.add_argument("--reconcile", action="store_true", help="Queue local records missing from the outbox")
    sync.add_argument("--drain", action="store_true", help="Drain bounded batches until empty or blocked")
    sync.add_argument("--max-batches", type=int, default=250)
    subparsers.add_parser("doctor")
    videos = subparsers.add_parser("videos")
    videos.add_argument("--platform")
    videos.add_argument("--limit", type=int, default=100)
    trends = subparsers.add_parser("trends")
    trends.add_argument("--state")
    trends.add_argument("--limit", type=int, default=100)
    keywords = subparsers.add_parser("keywords")
    keywords.add_argument("--limit", type=int, default=100)
    keywords.add_argument("--window-hours", type=int, default=168)
    keywords.add_argument("--min-videos", type=int, default=1)
    query_frontier = subparsers.add_parser("query-frontier")
    query_frontier.add_argument("--limit", type=int, default=100)
    query_frontier.add_argument("--window-hours", type=int, default=168)
    query_frontier.add_argument("--min-videos", type=int, default=2)
    predictions = subparsers.add_parser("predictions")
    predictions.add_argument("--subject-type", choices=["video", "trend"])
    predictions.add_argument("--limit", type=int, default=100)
    query_attempts = subparsers.add_parser("query-attempts")
    query_attempts.add_argument("--platform")
    query_attempts.add_argument("--limit", type=int, default=100)
    subparsers.add_parser("prediction-backtest")
    subparsers.add_parser("evaluate-predictions")
    subparsers.add_parser("train-predictor")
    subparsers.add_parser("predictor-status")
    forecast = subparsers.add_parser("forecast-trends")
    forecast.add_argument("--limit", type=int, default=5000)
    opportunities = subparsers.add_parser("opportunities")
    opportunities.add_argument("--limit", type=int, default=100)
    opportunities.add_argument("--max-saturation", type=float, default=0.75)
    opportunities.add_argument("--min-videos", type=int, default=2)
    opportunities.add_argument("--min-measured-videos", type=int, default=2)
    certify = subparsers.add_parser("certify-dataset")
    certify.add_argument("--date")
    subparsers.add_parser("dataset-status")
    candles = subparsers.add_parser("candles")
    candles.add_argument("--platform")
    candles.add_argument("--window-minutes", type=int, default=15)
    candles.add_argument("--limit", type=int, default=96)
    semantic_graph = subparsers.add_parser("semantic-graph-import")
    semantic_graph.add_argument("--path", required=True)
    semantic_graph.add_argument("--source-service", required=True)
    semantic_graph.add_argument("--source-receipt-id", required=True)
    semantic_graph.add_argument("--imported-by", required=True)
    semantic_graph.add_argument("--apply", action="store_true")
    semantic_extract = subparsers.add_parser("semantic-extract")
    semantic_extract.add_argument("--graph-version-id")
    semantic_extract.add_argument("--state")
    semantic_extract.add_argument("--limit", type=int, default=100)
    semantic_extract.add_argument("--apply", action="store_true")
    semantic_resolve = subparsers.add_parser("semantic-resolve")
    semantic_resolve.add_argument("--signal-id", required=True)
    semantic_resolve.add_argument("--max-candidates", type=int, default=8)
    semantic_resolve.add_argument("--ai", action="store_true")
    semantic_resolve.add_argument("--apply", action="store_true")
    semantic_review = subparsers.add_parser("semantic-review")
    semantic_review.add_argument(
        "--kind", choices=["binding", "atomic-selection"], required=True
    )
    semantic_review.add_argument("--path", required=True)
    semantic_review.add_argument("--apply", action="store_true")
    semantic_status = subparsers.add_parser("semantic-status")
    semantic_status.add_argument("--graph-version-id")
    semantic_status.add_argument("--signal-type")
    semantic_status.add_argument("--limit", type=int, default=25)
    subparsers.add_parser("upwork-health")
    upwork_scan = subparsers.add_parser("upwork-scan")
    upwork_scan.add_argument("--query", action="append", dest="queries")
    upwork_scan.add_argument("--execute-metered-reads", action="store_true")
    upwork_scan.add_argument("--max-jobs-per-query", type=int, default=50)
    upwork_scan.add_argument("--sort", default="recency")
    upwork_jobs = subparsers.add_parser("upwork-jobs")
    upwork_jobs.add_argument("--query")
    upwork_jobs.add_argument("--limit", type=int, default=100)
    upwork_demand = subparsers.add_parser("upwork-demand")
    upwork_demand.add_argument("--cohort-type")
    upwork_demand.add_argument("--cohort-key")
    upwork_demand.add_argument("--limit", type=int, default=100)
    upwork_backtest = subparsers.add_parser("upwork-backtest")
    upwork_backtest.add_argument("--cohort-type")
    upwork_backtest.add_argument("--cohort-key")
    upwork_context = subparsers.add_parser("upwork-script-context")
    upwork_context.add_argument("--selection-id")
    upwork_context.add_argument("--limit", type=int, default=20)
    upwork_materialize = subparsers.add_parser("upwork-materialize-signals")
    upwork_materialize.add_argument("--graph-version-id")
    upwork_materialize.add_argument("--limit", type=int, default=100)
    args = parser.parse_args()

    config = MarketTapeConfig.from_environment()
    store = MarketTapeStore(config)
    semantic = SemanticTopicService(store)
    if args.command == "init":
        return _print({"state": "initialized", "status": store.status()})
    if args.command == "cycle":
        return _print(MarketTapeCollector(config, store).run_cycle(args.mode))
    if args.command == "bootstrap-local":
        return _print(MarketTapeCollector(config, store).bootstrap_local_archive(args.limit_per_platform))
    if args.command == "backfill-query-attempts":
        return _print(MarketTapeCollector(config, store).backfill_query_attempts())
    if args.command == "reindex-trends":
        return _print(MarketTapeCollector(config, store).reindex_trends(args.forecast_limit))
    if args.command == "daemon":
        MarketTapeDaemon(config).run(once=args.once)
        return 0
    if args.command == "status":
        return _print(store.status())
    if args.command == "intelligence":
        return _print(build_intelligence_snapshot(
            config,
            store,
            limit=args.limit,
            window_hours=args.window_hours,
            minimum_videos=args.min_videos,
        ))
    if args.command == "sync":
        reconciled = store.enqueue_missing_for_sync() if args.reconcile else 0
        if args.force:
            store.make_outbox_due()
        sink = SupabaseSink(config, store)
        try:
            result = sink.drain(args.max_batches) if args.drain else sink.flush()
            result["reconciled_records"] = reconciled
            return _print(result)
        finally:
            sink.close()
    if args.command == "videos":
        return _print({"videos": store.list_videos(args.limit, args.platform)})
    if args.command == "trends":
        return _print({"trends": store.list_trends(args.limit, args.state)})
    if args.command == "keywords":
        return _print({
            "keywords": store.keyword_signals(
                args.limit,
                args.window_hours,
                args.min_videos,
            )
        })
    if args.command == "query-frontier":
        return _print({
            "queries": store.discovery_query_signals(
                args.limit,
                args.window_hours,
                args.min_videos,
            )
        })
    if args.command == "predictions":
        return _print({"predictions": store.list_predictions(args.limit, args.subject_type)})
    if args.command == "query-attempts":
        return _print({"attempts": store.list_query_attempts(args.limit, args.platform)})
    if args.command == "prediction-backtest":
        return _print(store.prediction_backtest())
    if args.command == "evaluate-predictions":
        return _print(store.evaluate_predictions())
    if args.command == "train-predictor":
        return _print(MarketTapePredictor(config, store).train())
    if args.command == "predictor-status":
        return _print(MarketTapePredictor(config, store).status())
    if args.command == "forecast-trends":
        return _print(MarketTapeCollector(
            config,
            store,
        ).reserve_validation_forecasts(limit=args.limit))
    if args.command == "opportunities":
        return _print(store.trend_opportunities(
            limit=args.limit,
            max_saturation=args.max_saturation,
            min_videos=args.min_videos,
            min_measured_videos=args.min_measured_videos,
        ))
    if args.command == "certify-dataset":
        return _print(MarketTapeDatasetManager(config, store).certify(args.date))
    if args.command == "dataset-status":
        return _print(MarketTapeDatasetManager(config, store).status())
    if args.command == "candles":
        return _print({"candles": store.social_candles(args.window_minutes, args.limit, args.platform)})
    if args.command == "semantic-graph-import":
        raw = _load_json(args.path)
        graph = raw.get("graph") if isinstance(raw, dict) and "graph" in raw else raw
        validated = validate_topic_graph(graph)
        payload = {
            "contract": GRAPH_IMPORT_CONTRACT,
            "source_service": args.source_service,
            "source_receipt_id": args.source_receipt_id,
            "imported_by": args.imported_by,
            "graph": validated,
        }
        if args.apply:
            return _print(semantic.import_graph(payload))
        return _print({
            "status": "ok",
            "contract": "market_tape_semantic_cli_dry_run_v1",
            "operation": "graph_import",
            "dry_run": True,
            "mutation_applied": False,
            "graph_version_id": "topic-graph:" + validated["graph_sha256"][:24],
            "graph_sha256": validated["graph_sha256"],
            "node_count": validated["inventory"]["node_count"],
            "edge_count": validated["inventory"]["relationship_count"],
        })
    if args.command == "semantic-extract":
        limit = min(500, max(1, int(args.limit)))
        if args.apply:
            return _print(semantic.materialize_trend_signals(
                graph_version_id=args.graph_version_id,
                limit=limit,
                state=args.state,
            ))
        return _print(_semantic_extract_preview(
            store,
            semantic,
            graph_version_id=args.graph_version_id,
            limit=limit,
            state=args.state,
        ))
    if args.command == "semantic-resolve":
        maximum = min(12, max(2, int(args.max_candidates)))
        if args.apply:
            return _print(semantic.resolve_signal(
                args.signal_id,
                use_ai=bool(args.ai),
                max_candidates=maximum,
            ))
        preview = semantic.preview_resolution(
            args.signal_id, max_candidates=maximum
        )
        preview["ai_requested_on_apply"] = bool(args.ai)
        return _print(preview)
    if args.command == "semantic-review":
        payload = _load_json(args.path)
        if not isinstance(payload, dict):
            raise SemanticContractError("review payload must be an object")
        if args.apply:
            result = (
                semantic.record_binding(payload)
                if args.kind == "binding"
                else semantic.record_atomic_selection(payload)
            )
            return _print(result)
        return _print({
            "status": "ok",
            "contract": "market_tape_semantic_cli_dry_run_v1",
            "operation": args.kind,
            "dry_run": True,
            "mutation_applied": False,
            "input_sha256": stable_hash(payload),
            "signal_id": payload.get("signal_id"),
            "topic_id": payload.get("topic_id") or payload.get("atomic_topic_id"),
            "binding_ids": payload.get("binding_ids") or [],
            "reviewer_type": payload.get("reviewer_type"),
            "decision": payload.get("decision"),
        })
    if args.command == "semantic-status":
        return _print({
            "status": "ok",
            "contract": "market_tape_semantic_cli_status_v1",
            "graph_summary": semantic.graph_summary(args.graph_version_id),
            "mapping_health": semantic.mapping_health(
                graph_version_id=args.graph_version_id,
                signal_type=args.signal_type,
                limit=min(100, max(1, int(args.limit))),
            ),
        })
    if args.command.startswith("upwork-"):
        upwork = UpworkDemandService(config)
        try:
            if args.command == "upwork-health":
                return _print(upwork.health())
            if args.command == "upwork-scan":
                return _print(upwork.scan(
                    queries=args.queries,
                    execute_metered_reads=bool(args.execute_metered_reads),
                    max_jobs_per_query=min(
                        100, max(1, int(args.max_jobs_per_query))
                    ),
                    sort=args.sort,
                ))
            if args.command == "upwork-jobs":
                return _print(upwork.list_jobs(
                    limit=min(500, max(1, int(args.limit))),
                    query=args.query,
                ))
            if args.command == "upwork-demand":
                return _print(upwork.demand_report(
                    cohort_type=args.cohort_type,
                    cohort_key=args.cohort_key,
                    limit=min(500, max(1, int(args.limit))),
                ))
            if args.command == "upwork-backtest":
                return _print(upwork.backtest_report(
                    cohort_type=args.cohort_type,
                    cohort_key=args.cohort_key,
                ))
            if args.command == "upwork-script-context":
                return _print(upwork.script_context(
                    selection_id=args.selection_id,
                    limit=min(100, max(1, int(args.limit))),
                ))
            if args.command == "upwork-materialize-signals":
                return _print(upwork.materialize_signals(
                    graph_version_id=args.graph_version_id,
                    limit=min(500, max(1, int(args.limit))),
                ))
        except UpworkAPIError as exc:
            return _print({
                "status": "error",
                "code": exc.code,
                "error": str(exc),
            }, exit_code=2)
        except (TypeError, ValueError, RuntimeError) as exc:
            return _print({
                "status": "error",
                "code": "upwork_command_failed",
                "error": str(exc),
            }, exit_code=2)
        finally:
            upwork.close()
    if args.command == "doctor":
        sources = build_sources(config, "doctor", store.remaining_request_budget)
        try:
            result = {
                "database": str(config.db_path),
                "object_store": str(config.object_dir),
                "daily_unique_target": config.daily_unique_target,
                "metered_reads_approved": config.allow_metered_reads,
                "supabase_sync_enabled": config.supabase_sync_enabled,
                "sources": [
                    {
                        "source_id": source.source_id,
                        "platform": source.platform,
                        "credential_ready": source.credentials_available(),
                        "missing": source.missing_credentials(),
                        "request_budget_remaining": source.request_budget,
                        "metered": source.metered,
                    }
                    for source in sources
                ],
            }
        finally:
            for source in sources:
                source.close()
        return _print(result)
    return 1


def _print(value: Any, *, exit_code: int = 0) -> int:
    print(json.dumps(value, indent=2, sort_keys=True, default=str))
    return exit_code


def _load_json(path: str) -> Any:
    source = Path(path).expanduser()
    if not source.is_file():
        raise SemanticContractError(f"JSON file does not exist: {source}")
    try:
        return json.loads(source.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise SemanticContractError(f"JSON file could not be read: {source}") from exc


def _semantic_extract_preview(
    store: MarketTapeStore,
    semantic: SemanticTopicService,
    *,
    graph_version_id: str | None,
    limit: int,
    state: str | None,
) -> dict[str, Any]:
    graph = semantic.graph_summary(graph_version_id)
    if graph["state"] != "ready":
        raise SemanticContractError("no semantic topic graph has been imported")
    graph_id = str(graph["graph"]["graph_version_id"])
    where = " WHERE observation.state = ?" if state else ""
    parameters: list[Any] = [state] if state else []
    parameters.append(min(500, max(1, int(limit))))
    with store.connect() as connection:
        rows = [dict(row) for row in connection.execute(
            """SELECT trend.trend_id, trend.trend_type, trend.canonical_key,
                      trend.display_name, trend.status, trend.first_seen_at,
                      trend.last_seen_at, observation.trend_observation_id,
                      observation.observed_at, observation.videos_total,
                      observation.creators_total, observation.platforms_total,
                      observation.views_total, observation.likes_total,
                      observation.comments_total, observation.shares_total,
                      observation.trend_strength,
                      observation.state AS observed_state,
                      observation.index_version,
                      observation.observation_quality_contract
               FROM mt_trends trend
               JOIN mt_trend_observations observation
                 ON observation.trend_observation_id = (
                     SELECT nested.trend_observation_id
                     FROM mt_trend_observations nested
                     WHERE nested.trend_id = trend.trend_id
                     ORDER BY nested.observed_at DESC,
                              nested.trend_observation_id DESC LIMIT 1
                 )""" + where +
            " ORDER BY observation.observed_at DESC LIMIT ?",
            parameters,
        )]
    candidates = []
    for row in rows:
        raw_type = str(row["trend_type"] or "").lower()
        signal_type = raw_type if raw_type in {
            "topic", "keyword", "query", "question", "problem", "objection",
            "claim", "angle", "hook", "title", "format", "platform", "offer",
            "hashtag", "audio", "opportunity", "other",
        } else "other"
        evidence = {
            "contract": "market_tape_semantic_trend_evidence_v1",
            "trend": {key: row[key] for key in (
                "trend_id", "trend_type", "canonical_key", "display_name",
                "status", "first_seen_at", "last_seen_at",
            )},
            "observation": {key: row[key] for key in (
                "trend_observation_id", "observed_at", "videos_total",
                "creators_total", "platforms_total", "views_total",
                "likes_total", "comments_total", "shares_total",
                "trend_strength", "observed_state", "index_version",
                "observation_quality_contract",
            )},
            "metrics": {key: row[key] for key in (
                "videos_total", "creators_total", "platforms_total",
                "views_total", "likes_total", "comments_total", "shares_total",
                "trend_strength",
            )},
        }
        evidence_sha = stable_hash(evidence)
        identity = {
            "graph_version_id": graph_id,
            "signal_type": signal_type,
            "source_kind": "market_tape_trend",
            "source_entity_id": row["trend_id"],
            "source_observed_at": row["observed_at"],
            "normalized_signal_text": normalize_text(row["display_name"]),
            "evidence_sha256": evidence_sha,
        }
        candidates.append({
            "signal_id": "topic-signal:" + stable_hash(identity),
            "source_trend_id": row["trend_id"],
            "trend_observation_id": row["trend_observation_id"],
            "signal_type": signal_type,
            "signal_text": row["display_name"],
            "evidence_sha256": evidence_sha,
        })
    return {
        "status": "ok",
        "contract": "market_tape_semantic_signal_extraction_preview_v1",
        "dry_run": True,
        "mutation_applied": False,
        "graph_version_id": graph_id,
        "limit": limit,
        "count": len(candidates),
        "candidates": candidates,
    }


if __name__ == "__main__":
    raise SystemExit(main())
