from __future__ import annotations

import json
import hashlib
import hmac
import os
import re
import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from flask import Flask, jsonify, request

from .ai_relatability import NON_AI_PASS_DECISION, openai_relatability_runner
from .contracts import SCRIPT_INTELLIGENCE_BRIEF_CONTRACT
from .demand_client import MarketTapeDemandClient
from .engine import ContentQualityEngine, IdempotencyConflict
from .marketing_scripts import MarketingScriptCompiler
from .narrative_coherence import default_llm_runner, openai_llm_runner
from .reference_corpus import (
    MAX_CORPUS_ITEMS,
    MAX_ITEM_PAGE_SIZE,
    ReferenceCorpusService,
    instagram_source_reader_from_env,
)
from .script_experiments import register_script_experiment_routes


DEFAULT_MARKET_TAPE = Path.home() / "Library/Application Support/ContentIntelligence/data/market-tape.sqlite3"
DEFAULT_QUALITY_DB = Path.home() / "Library/Application Support/ContentQuality/data/content-quality.sqlite3"


def create_content_quality_app(config: dict[str, Any] | None = None) -> Flask:
    app = Flask("content-quality")
    app.config.update(config or {})
    # Narrative coherence judge: "openai" in production (owner decision
    # 2026-08-22), "off" under TESTING unless a runner is injected. Rule
    # enforcement never turns off; only the judgment pass is configurable.
    llm_runner = app.config.get("NARRATIVE_LLM_RUNNER")
    if llm_runner is None:
        mode = app.config.get("NARRATIVE_COHERENCE_LLM") or (
            "off" if app.config.get("TESTING") else os.getenv("NARRATIVE_COHERENCE_LLM", "openai")
        )
        if mode == "openai":
            llm_runner = openai_llm_runner
        elif mode == "claude":
            llm_runner = default_llm_runner
    relatability_llm_runner = app.config.get("RELATABILITY_LLM_RUNNER")
    if relatability_llm_runner is None:
        relatability_mode = app.config.get("RELATABILITY_JUDGE") or (
            "off"
            if app.config.get("TESTING")
            else os.getenv("RELATABILITY_JUDGE", "openai")
        )
        if relatability_mode == "openai":
            relatability_llm_runner = openai_relatability_runner
    demand_client = app.config.get("MARKET_TAPE_DEMAND_CLIENT")
    if demand_client is None:
        demand_client = MarketTapeDemandClient.from_environment()
    engine = ContentQualityEngine(
        app.config.get("MARKET_TAPE_DB") or os.getenv("MARKET_TAPE_DB") or DEFAULT_MARKET_TAPE,
        app.config.get("CONTENT_QUALITY_DB") or os.getenv("CONTENT_QUALITY_DB") or DEFAULT_QUALITY_DB,
        narrative_llm_runner=llm_runner,
        relatability_llm_runner=relatability_llm_runner,
        transcript_storage_root=(
            app.config.get("TRANSCRIPT_BANK_ROOT")
            or os.getenv("TRANSCRIPT_BANK_ROOT")
        ),
        script_language_demand_enqueuer=(
            demand_client.enqueue if demand_client is not None else None
        ),
    )
    app.extensions["market_tape_demand_client"] = demand_client
    app.extensions["content_quality_engine"] = engine
    script_experiments = engine.script_experiments
    app.extensions["script_experiment_telemetry"] = script_experiments
    reference_reader = app.config.get("REFERENCE_SOURCE_READER")
    if reference_reader is None:
        reference_reader = instagram_source_reader_from_env()
    reference_corpus = ReferenceCorpusService(
        app.config.get("CONTENT_REFERENCE_ROOT")
        or os.getenv("CONTENT_REFERENCE_ROOT"),
        source_reader=reference_reader,
    )
    app.extensions["reference_corpus"] = reference_corpus
    marketing_script_compiler = MarketingScriptCompiler(
        reference_corpus,
        script_experiments=script_experiments,
    )
    app.extensions["marketing_script_compiler"] = marketing_script_compiler

    @app.errorhandler(ValueError)
    def invalid_request(error: ValueError):
        return jsonify({"status": "error", "code": "INVALID_REQUEST", "error": str(error)}), 400

    @app.errorhandler(404)
    def not_found(_error):
        return jsonify({"status": "error", "code": "NOT_FOUND"}), 404

    # engine.health() is a full COUNT(*) sweep over a multi-GB market tape, and the Ops
    # Console probes all eight capability endpoints on every refresh. Uncached, one console
    # cycle triggered eight concurrent sweeps; each held its SQLite connections and thread
    # for the duration, and the server climbed past 400 open tape handles and 360 threads
    # until every request failed with OSError 24 (too many open files) and then hung.
    # One sweep per TTL window, computed under the lock so simultaneous probes collapse into
    # a single pass and the rest read the result instead of starting their own.
    # Explicit None check, not `or`: HEALTH_CACHE_SECONDS=0 means "never cache" and must
    # not fall through to the default the way a falsy 0 would.
    _ttl = app.config.get("HEALTH_CACHE_SECONDS")
    if _ttl is None:
        _ttl = os.getenv("CQ_HEALTH_CACHE_SECONDS", "300")
    health_ttl = float(_ttl)
    health_lock = threading.Lock()        # guards the cache dict only, never held over a sweep
    sweep_lock = threading.Lock()         # at most one tape sweep in flight, ever
    bootstrap_report = {
        "status": "starting",
        "service": "content-quality",
        "market_tape": {"status": "checking"},
        "learning_store": {"status": "up", "counts": engine.store.counts()},
        "data_readiness": {
            "script_intelligence": {"status": "checking", "gaps": ["health_sweep_pending"]},
            "script_language_demand_feedback": {"status": "checking"},
            "owned_retention": {"status": "checking"},
        },
        "ai_readiness": {
            "narrative_judge_configured": False,
            "relatability_judge_configured": False,
            "deterministic_services_available": True,
            "note": "Health sweep pending.",
        },
        "checked_at": datetime.now(timezone.utc).isoformat(),
    }
    health_cache: dict[str, Any] = {
        # Production callers get an immediate, honest starting response while
        # exactly one background scan warms the cache. Tests retain synchronous
        # behavior so assertions never race a daemon thread.
        "report": None if app.config.get("TESTING") else bootstrap_report,
        "at": 0.0,
        "refreshing": False,
    }

    def _sweep() -> dict[str, Any]:
        with sweep_lock:
            # Another caller may have filled the cache while this one waited for the lock —
            # on a cold start every probe arrives at once, and they must not all sweep.
            with health_lock:
                cached, at = health_cache["report"], health_cache["at"]
            if cached is not None and (time.monotonic() - at) < health_ttl:
                with health_lock:
                    health_cache["refreshing"] = False
                return cached
            report = engine.health()
            with health_lock:
                health_cache["report"] = report
                health_cache["at"] = time.monotonic()
                health_cache["refreshing"] = False
            return report

    def _refresh_in_background() -> None:
        with health_lock:
            if health_cache.get("refreshing"):
                return
            health_cache["refreshing"] = True
        threading.Thread(target=_sweep, name="cq-health-refresh", daemon=True).start()

    def health_report() -> dict[str, Any]:
        """Never block a caller on the tape sweep after the first one.

        The Ops Console probes with a 5s timeout while a cold sweep over the multi-GB
        tape may take more than one minute. A five-minute TTL prevents continuous
        competition between frequent probes and the sweep. Serve the last snapshot
        immediately and refresh behind it; `checked_at` in the payload always says how
        fresh the answer really is.
        """
        with health_lock:
            cached = health_cache["report"]
            fresh = cached is not None and (time.monotonic() - health_cache["at"]) < health_ttl
        if cached is None:
            return _sweep()                 # cold start: one caller pays for it
        if not fresh:
            _refresh_in_background()
        return cached

    app.extensions["content_quality_health_report"] = health_report

    @app.get("/health")
    def health():
        report = health_report()
        return jsonify(report), 200 if report["status"] == "healthy" else 503

    def capability_health(name: str, dependency: str | None = None) -> tuple[Any, int]:
        report = health_report()
        if dependency == "market_tape":
            dependency_status = report["market_tape"]["status"]
        elif dependency == "script_intelligence":
            dependency_status = report["data_readiness"]["script_intelligence"]["status"]
        elif dependency == "owned_retention":
            dependency_status = report["data_readiness"]["owned_retention"]["status"]
        elif dependency == "ai_judge":
            dependency_status = (
                "ready" if report["ai_readiness"]["narrative_judge_configured"]
                else "not_configured"
            )
        elif dependency == "relatability_ai_judge":
            dependency_status = (
                "ready" if report["ai_readiness"]["relatability_judge_configured"]
                else "not_configured"
            )
        else:
            dependency_status = "up"
        status = "healthy" if dependency_status in {"up", "ready"} else "degraded"
        return jsonify({
            "status": status,
            "process_status": "up",
            "data_readiness": dependency_status,
            "service": name,
            "dependency": dependency,
            "dependency_status": dependency_status,
            "checked_at": report["checked_at"],
        }), 200 if status == "healthy" else 503

    health_routes = {
        "/api/audience-intelligence/health": ("audience-intelligence", "market_tape"),
        "/api/viral-transcripts/health": ("viral-transcripts", "market_tape"),
        "/api/transcript-style-guides/health": (
            "transcript-style-guides", "market_tape"
        ),
        "/api/scripts/health": ("evidence-first-scripts", "script_intelligence"),
        "/api/script-intelligence/health": ("script-intelligence", "script_intelligence"),
        "/api/relatability/health": ("relatability", "script_intelligence"),
        "/api/relatability/qualitative-health": (
            "relatability-ai-qualitative", "relatability_ai_judge"
        ),
        "/api/attention/health": ("attention", None),
        "/api/retention/health": ("post-publish-retention", "owned_retention"),
        "/api/learning/health": ("learning-memory", None),
        "/api/narrative-coherence/health": ("narrative-coherence", "ai_judge"),
    }
    for index, (route, (name, dependency)) in enumerate(health_routes.items()):
        app.add_url_rule(
            route,
            endpoint=f"capability_health_{index}",
            view_func=lambda name=name, dependency=dependency: capability_health(name, dependency),
            methods=["GET"],
        )

    def json_body() -> dict[str, Any]:
        return request.get_json(silent=True) or {}

    control_token = str(
        app.config.get("CONTENT_QUALITY_CONTROL_TOKEN")
        or os.getenv("CONTENT_QUALITY_CONTROL_TOKEN")
        or ""
    ).strip()

    def require_agent_auth() -> tuple[Any, int] | None:
        if app.config.get("TESTING") and not control_token:
            return None
        if not control_token:
            return jsonify({
                "status": "error",
                "code": "AGENT_GATEWAY_NOT_CONFIGURED",
                "error": "CONTENT_QUALITY_CONTROL_TOKEN is required",
            }), 503
        supplied = request.headers.get("Authorization", "")
        if not supplied.startswith("Bearer ") or not hmac.compare_digest(
            supplied[7:].strip(), control_token
        ):
            return jsonify({"status": "error", "code": "UNAUTHORIZED"}), 401
        return None

    def audited_agent_response(
        operation: str,
        parameters: Any,
        result: dict[str, Any],
        status_code: int = 200,
        started_at: float | None = None,
    ) -> tuple[Any, int]:
        encoded_parameters = json.dumps(
            parameters, sort_keys=True, separators=(",", ":"), default=str
        ).encode()
        encoded_response = json.dumps(
            result, sort_keys=True, separators=(",", ":"), default=str
        ).encode()
        created_at = time.time()
        principal = re.sub(
            r"[^A-Za-z0-9_.:@-]+", "-",
            request.headers.get("X-Agent-Principal", "local-agent"),
        )[:120]
        query_id = "agentq_" + hashlib.sha256(
            f"{principal}|{operation}|{created_at}|".encode() + encoded_parameters
        ).hexdigest()[:24]
        row_count = 1
        for collection_key in (
            "briefs", "events", "samples", "experiments", "snapshots"
        ):
            if isinstance(result.get(collection_key), list):
                row_count = len(result[collection_key])
                break
        engine.store.put_agent_query({
            "query_id": query_id,
            "principal": principal,
            "operation": operation,
            "parameters_sha256": hashlib.sha256(encoded_parameters).hexdigest(),
            "response_sha256": hashlib.sha256(encoded_response).hexdigest(),
            "outcome": "success" if status_code < 400 else "rejected",
            "row_count": row_count,
            "duration_ms": round(1000.0 * (time.monotonic() - (started_at or time.monotonic())), 3),
            "created_at": datetime.fromtimestamp(created_at, timezone.utc).isoformat(),
        })
        response = dict(result)
        response["agent_query"] = {
            "query_id": query_id,
            "response_sha256": hashlib.sha256(encoded_response).hexdigest(),
        }
        return jsonify(response), status_code

    def audited_invalid_request(
        operation: str,
        parameters: Any,
        error: ValueError,
        started_at: float,
    ) -> tuple[Any, int]:
        return audited_agent_response(
            operation,
            parameters,
            {
                "status": "error",
                "code": "INVALID_REQUEST",
                "error": str(error),
            },
            400,
            started_at,
        )

    def reference_invalid_request(
        action: str,
        parameters: Any,
        error: ValueError,
        started_at: float,
    ) -> tuple[Any, int]:
        return audited_agent_response(
            action,
            parameters,
            {
                "status": "error",
                "code": "INVALID_REQUEST",
                "error": str(error),
            },
            400,
            started_at,
        )

    register_script_experiment_routes(
        app,
        script_experiments,
        json_body=json_body,
        require_auth=require_agent_auth,
        audited_response=audited_agent_response,
        invalid_response=audited_invalid_request,
    )

    @app.get("/api/reference-corpus/health")
    def reference_corpus_health():
        return jsonify(reference_corpus.health())

    @app.get("/api/reference-corpus/status")
    def reference_corpus_status():
        denied = require_agent_auth()
        if denied:
            return denied
        started = time.monotonic()
        corpus_id = str(request.args.get("corpus_id") or "").strip()
        try:
            if not corpus_id:
                raise ValueError("corpus_id is required")
            result = reference_corpus.corpus_status(corpus_id)
        except ValueError as error:
            return reference_invalid_request(
                "reference_corpus_status", {"corpus_id": corpus_id}, error, started
            )
        except KeyError:
            result = {
                "status": "error",
                "code": "REFERENCE_CORPUS_NOT_FOUND",
                "corpus_id": corpus_id,
            }
            return audited_agent_response(
                "reference_corpus_status", {"corpus_id": corpus_id},
                result, 404, started,
            )
        return audited_agent_response(
            "reference_corpus_status", {"corpus_id": corpus_id},
            result, started_at=started,
        )

    @app.get("/api/reference-corpus/items")
    def reference_corpus_items():
        denied = require_agent_auth()
        if denied:
            return denied
        started = time.monotonic()
        corpus_id = str(request.args.get("corpus_id") or "").strip()
        limit = max(
            1, min(MAX_ITEM_PAGE_SIZE, int(request.args.get("limit", "25")))
        )
        offset = max(0, int(request.args.get("offset", "0")))
        include_text = str(request.args.get("include_transcript") or "").lower() in {
            "1", "true", "yes",
        }
        if not corpus_id:
            return reference_invalid_request(
                "reference_corpus_items",
                {"corpus_id": corpus_id},
                ValueError("corpus_id is required"),
                started,
            )
        rows = reference_corpus.list_items(
            corpus_id,
            limit=limit,
            offset=offset,
            include_transcript=include_text,
        )
        total = reference_corpus.corpus_status(corpus_id)["counts"]["items"]
        next_offset = offset + len(rows) if offset + len(rows) < total else None
        result = {
            "status": "ok",
            "contract": "content_reference_item_list_v1",
            "corpus_id": corpus_id,
            "items": rows,
            "count": len(rows),
            "limit": limit,
            "offset": offset,
            "total": total,
            "next_offset": next_offset,
        }
        return audited_agent_response(
            "reference_corpus_items",
            {
                "corpus_id": corpus_id,
                "limit": limit,
                "offset": offset,
                "include_transcript": include_text,
            },
            result,
            started_at=started,
        )

    @app.post("/api/reference-corpus/find")
    def reference_corpus_find():
        denied = require_agent_auth()
        if denied:
            return denied
        started = time.monotonic()
        payload = json_body()
        try:
            rows = reference_corpus.find_items(
                corpus_id=str(payload.get("corpus_id") or ""),
                query=str(payload.get("query") or ""),
                limit=max(1, min(20, int(payload.get("limit") or 8))),
            )
        except ValueError as error:
            return reference_invalid_request(
                "reference_corpus_find", payload, error, started
            )
        result = {
            "status": "ok",
            "contract": "content_reference_evidence_v1",
            "items": rows,
            "count": len(rows),
        }
        return audited_agent_response(
            "reference_corpus_find", payload, result, started_at=started
        )

    @app.get("/api/reference-corpus/summary")
    def reference_corpus_summary():
        denied = require_agent_auth()
        if denied:
            return denied
        started = time.monotonic()
        corpus_id = str(request.args.get("corpus_id") or "").strip()
        if not corpus_id:
            return reference_invalid_request(
                "reference_corpus_summary",
                {"corpus_id": corpus_id},
                ValueError("corpus_id is required"),
                started,
            )
        result = reference_corpus.summarize(corpus_id)
        return audited_agent_response(
            "reference_corpus_summary", {"corpus_id": corpus_id},
            result, started_at=started,
        )

    @app.post("/api/reference-corpus/context")
    def reference_corpus_context():
        denied = require_agent_auth()
        if denied:
            return denied
        started = time.monotonic()
        payload = json_body()
        try:
            result = reference_corpus.agent_context(
                corpus_id=str(payload.get("corpus_id") or ""),
                query=str(payload.get("query") or ""),
                evidence_limit=max(
                    1, min(20, int(payload.get("evidence_limit") or 8))
                ),
            )
        except ValueError as error:
            return reference_invalid_request(
                "reference_corpus_context", payload, error, started
            )
        return audited_agent_response(
            "reference_corpus_context", payload, result, started_at=started
        )

    @app.post("/api/reference-corpus/audit")
    def reference_corpus_audit():
        denied = require_agent_auth()
        if denied:
            return denied
        started = time.monotonic()
        payload = json_body()
        try:
            result = reference_corpus.audit_content(
                corpus_id=str(payload.get("corpus_id") or ""),
                title=str(payload.get("title") or ""),
                script=str(payload.get("script") or ""),
                objective=str(payload.get("objective") or ""),
                target_viewer=str(payload.get("target_viewer") or ""),
                target_seconds=int(payload.get("target_seconds") or 60),
                provenance=(
                    payload.get("provenance")
                    if isinstance(payload.get("provenance"), dict)
                    else None
                ),
            )
        except ValueError as error:
            return reference_invalid_request(
                "reference_corpus_audit", payload, error, started
            )
        return audited_agent_response(
            "reference_corpus_audit", payload, result, started_at=started
        )

    @app.post("/api/reference-corpus/write-script")
    def reference_corpus_write_script():
        denied = require_agent_auth()
        if denied:
            return denied
        started = time.monotonic()
        payload = json_body()
        try:
            result = marketing_script_compiler.compile(payload)
        except ValueError as error:
            return reference_invalid_request(
                "reference_corpus_write_script", payload, error, started
            )
        except KeyError:
            result = {
                "status": "error",
                "code": "REFERENCE_CORPUS_NOT_FOUND",
                "corpus_id": str(payload.get("corpus_id") or ""),
            }
            return audited_agent_response(
                "reference_corpus_write_script", payload, result, 404, started
            )
        return audited_agent_response(
            "reference_corpus_write_script", payload, result, started_at=started
        )

    @app.get("/api/reference-corpus/scripts/<script_id>")
    def reference_corpus_script(script_id: str):
        denied = require_agent_auth()
        if denied:
            return denied
        started = time.monotonic()
        try:
            result = marketing_script_compiler.get(script_id)
        except ValueError as error:
            return reference_invalid_request(
                "reference_corpus_script", {"script_id": script_id}, error, started
            )
        if result is None:
            return audited_agent_response(
                "reference_corpus_script",
                {"script_id": script_id},
                {"status": "error", "code": "REFERENCE_SCRIPT_NOT_FOUND"},
                404,
                started,
            )
        return audited_agent_response(
            "reference_corpus_script",
            {"script_id": script_id},
            result,
            started_at=started,
        )

    @app.post("/api/reference-corpus/acquire")
    def reference_corpus_acquire():
        denied = require_agent_auth()
        if denied:
            return denied
        started = time.monotonic()
        payload = json_body()
        try:
            result = reference_corpus.acquire_instagram(
                username=str(payload.get("username") or ""),
                limit=max(
                    1,
                    min(MAX_CORPUS_ITEMS, int(payload.get("limit") or 75)),
                ),
                corpus_id=str(payload.get("corpus_id") or "") or None,
            )
        except ValueError as error:
            return reference_invalid_request(
                "reference_corpus_acquire", payload, error, started
            )
        except RuntimeError as error:
            result = {
                "status": "error",
                "code": "REFERENCE_SOURCE_UNAVAILABLE",
                "error_type": type(error).__name__,
            }
            return audited_agent_response(
                "reference_corpus_acquire", payload, result, 503, started
            )
        return audited_agent_response(
            "reference_corpus_acquire", payload, result, 201, started
        )

    @app.post("/api/reference-corpus/extract")
    def reference_corpus_extract():
        denied = require_agent_auth()
        if denied:
            return denied
        started = time.monotonic()
        payload = json_body()
        try:
            result = reference_corpus.extract_batch(
                corpus_id=str(payload.get("corpus_id") or ""),
                limit=max(1, min(3, int(payload.get("limit") or 1))),
                transcript_model=str(payload.get("transcript_model") or "base.en"),
                semantic_ai=bool(payload.get("semantic_ai", False)),
                semantic_model=str(payload.get("semantic_model") or "gpt-5-nano"),
            )
        except ValueError as error:
            return reference_invalid_request(
                "reference_corpus_extract", payload, error, started
            )
        return audited_agent_response(
            "reference_corpus_extract", payload, result, started_at=started
        )

    @app.post("/api/viral-transcripts/discover")
    def discover_transcripts():
        payload = json_body()
        return jsonify(engine.viral.discover(str(payload.get("topic") or ""), int(payload.get("limit") or 5)))

    @app.get("/api/transcript-style-guides/status")
    def transcript_style_status():
        denied = require_agent_auth()
        if denied:
            return denied
        started = time.monotonic()
        platform = str(request.args.get("platform") or "tiktok")
        result = engine.style_guides.status(platform)
        return audited_agent_response(
            "transcript_style_status", {"platform": platform}, result,
            started_at=started,
        )

    @app.post("/api/transcript-style-guides/build")
    def build_transcript_style_guide():
        denied = require_agent_auth()
        if denied:
            return denied
        started = time.monotonic()
        payload = json_body()
        try:
            result = engine.style_guides.build(payload)
        except ValueError as error:
            return audited_invalid_request(
                "build_transcript_style_guide", payload, error, started
            )
        return audited_agent_response(
            "build_transcript_style_guide", payload, result,
            201 if result.get("status") == "ready" else 422, started,
        )

    @app.get("/api/transcript-style-guides")
    def list_transcript_style_guides():
        denied = require_agent_auth()
        if denied:
            return denied
        started = time.monotonic()
        platform = request.args.get("platform")
        limit = max(1, min(200, int(request.args.get("limit") or 50)))
        guides = engine.style_guides.list(platform, limit)
        result = {"status": "ok", "guides": guides, "count": len(guides)}
        return audited_agent_response(
            "list_transcript_style_guides",
            {"platform": platform, "limit": limit}, result,
            started_at=started,
        )

    @app.get("/api/transcript-style-guides/<guide_id>")
    def get_transcript_style_guide(guide_id: str):
        denied = require_agent_auth()
        if denied:
            return denied
        started = time.monotonic()
        receipt = engine.style_guides.resolve(guide_id)
        result = (
            {"status": "ok", "guide": receipt}
            if receipt is not None else {
                "status": "error", "code": "STYLE_GUIDE_NOT_FOUND",
                "guide_id": guide_id,
            }
        )
        return audited_agent_response(
            "get_transcript_style_guide", {"guide_id": guide_id}, result,
            200 if receipt is not None else 404, started,
        )

    @app.post("/api/transcript-style-guides/audit")
    def audit_transcript_style_guide():
        denied = require_agent_auth()
        if denied:
            return denied
        started = time.monotonic()
        payload = json_body()
        try:
            result = engine.style_guides.audit(payload)
        except ValueError as error:
            return audited_invalid_request(
                "audit_transcript_style_guide", payload, error, started
            )
        status_code = 200 if result.get("decision") == "PASS" else 422
        return audited_agent_response(
            "audit_transcript_style_guide", payload, result,
            status_code, started,
        )

    @app.post("/api/audience/human-moments")
    def human_moments():
        payload = json_body()
        return jsonify(engine.audience.human_moments(
            str(payload.get("topic") or ""), str(payload.get("audience") or ""), int(payload.get("limit") or 8)
        ))

    @app.get("/api/agent/catalog")
    def agent_catalog():
        denied = require_agent_auth()
        if denied:
            return denied
        started = time.monotonic()
        result = {
            "status": "ok",
            "contract": "content_quality_agent_catalog_v1",
            "database_access": "typed_bounded_api_only",
            "arbitrary_sql_allowed": False,
            "markdown_runtime_state": False,
            "operations": {
                "intelligence_status": {
                    "method": "GET", "path": "/api/script-intelligence/health",
                },
                "transcript_style_status": {
                    "method": "GET",
                    "path": "/api/transcript-style-guides/status",
                    "optional": ["platform"],
                },
                "build_transcript_style_guide": {
                    "method": "POST",
                    "path": "/api/transcript-style-guides/build",
                    "required": ["topic", "platform", "receipt_ids"],
                    "minimums": {
                        "verified_transcripts": 5,
                        "distinct_creators": 3,
                        "observed_views": 100000,
                    },
                },
                "audit_transcript_style": {
                    "method": "POST",
                    "path": "/api/transcript-style-guides/audit",
                    "required": ["style_guide_id", "text", "provenance"],
                    "effect": (
                        "measures aggregate style fit and rejects substantive "
                        "source expression, sequence, structure, or invalid provenance"
                    ),
                },
                "build_script_brief": {
                    "method": "POST", "path": "/api/script-intelligence/briefs",
                    "required": ["audience"],
                    "optional": [
                        "topic", "objective", "variant_index",
                        "style_platform",
                    ],
                    "bounds": {"variant_index": [0, 7]},
                    "variant_selection": (
                        "zero-based selection over text-distinct stored human "
                        "moments; the selection is immutable in the brief"
                    ),
                },
                "list_script_briefs": {
                    "method": "GET", "path": "/api/script-intelligence/briefs",
                    "bounds": {"limit": [1, 200]},
                },
                "get_script_brief": {
                    "method": "GET", "path": "/api/script-intelligence/briefs/{brief_id}",
                },
                "generate_and_audit": {
                    "method": "POST",
                    "path": "/api/script-intelligence/generate-and-audit",
                    "required": ["brief_id"],
                },
                "run_trend_to_script": {
                    "method": "POST",
                    "path": "/api/script-intelligence/run",
                    "required": ["audience"],
                    "optional": [
                        "topic", "objective", "variant_index",
                        "style_platform",
                    ],
                    "bounds": {"variant_index": [0, 7]},
                    "effect": (
                        "build immutable brief then generate and audit, or "
                        "enqueue one bounded evidence demand; each variant uses "
                        "a distinct stored source moment from the same cohort"
                    ),
                },
                "get_script_lineage": {
                    "method": "GET",
                    "path": "/api/script-intelligence/scripts/{script_id}",
                },
                "audit_qualitative_relatability": {
                    "method": "POST",
                    "path": "/api/relatability/qualitative-audit",
                    "required_one_of": [
                        ["script_id"],
                        ["text", "audience", "source_receipt_ids"],
                    ],
                    "effect": (
                        "persist a separately named, evidence-bound AI verdict; "
                        "use an explicit deterministic fallback when AI is off"
                    ),
                },
                "ingest_owned_outcome_event": {
                    "method": "POST",
                    "path": "/api/owned-outcomes/events",
                    "contract": "owned_attribution_event_v1",
                    "required": [
                        "idempotency_key", "event_type", "attribution",
                        "journey_id", "occurred_at",
                    ],
                    "event_types": ["click", "install", "trial", "purchase"],
                },
                "list_owned_outcome_events": {
                    "method": "GET",
                    "path": "/api/owned-outcomes/events",
                    "required": ["content_id"],
                    "bounds": {"limit": [1, 500]},
                },
                "ingest_owned_retention_sample": {
                    "method": "POST",
                    "path": "/api/owned-outcomes/retention-samples",
                    "contract": "owned_retention_sample_v1",
                    "required": [
                        "idempotency_key", "attribution", "measurement_id",
                        "observed_at", "elapsed_ms", "retained_percent",
                        "sample_size",
                    ],
                },
                "list_owned_retention_samples": {
                    "method": "GET",
                    "path": "/api/owned-outcomes/retention-samples",
                    "required": ["content_id"],
                    "bounds": {"limit": [1, 2000]},
                },
                "summarize_owned_outcomes": {
                    "method": "GET",
                    "path": "/api/owned-outcomes/summary",
                    "required": ["content_id"],
                    "optional": [
                        "campaign_id", "offer_id", "source_platform", "source_id",
                    ],
                    "causal_policy": (
                        "observed drops are facts; reasons are refused without causal evidence; "
                        "AI explanations are labelled interpretation_not_fact"
                    ),
                },
                "register_script_experiment": {
                    "method": "POST",
                    "path": "/api/script-experiments",
                    "contract": "script_experiment_v1",
                    "required": [
                        "brief_id", "script_id", "script_sha256|script_text",
                        "workflow_seed|workflow_id",
                    ],
                    "effect": (
                        "derive and persist one immutable experiment ID from "
                        "script and workflow lineage"
                    ),
                },
                "ingest_script_experiment_metrics": {
                    "method": "POST",
                    "path": "/api/script-experiments/metrics",
                    "contract": "script_metric_snapshot_v1",
                    "required": [
                        "idempotency_key", "experiment_id", "source_platform",
                        "provider_post_id", "provider_receipt_id", "observed_at",
                        "view_denominator_basis", "metrics",
                    ],
                    "supported_metrics": [
                        "views", "hold_1s_views", "hold_3s_views",
                        "completed_views", "shares", "saves", "cta_clicks",
                        "cta_leads", "cta_signups", "cta_trials",
                        "cta_purchases",
                    ],
                },
                "rollup_script_experiment_metrics": {
                    "method": "GET",
                    "path": "/api/script-experiments/rollup",
                    "required_one_of": [
                        ["experiment_id"], ["script_id"], ["workflow_id"],
                    ],
                    "measurement": (
                        "latest cumulative count per post and metric; "
                        "denominator-weighted descriptive rates"
                    ),
                },
                "reference_corpus_status": {
                    "method": "GET",
                    "path": "/api/reference-corpus/status",
                    "required": ["corpus_id"],
                },
                "list_reference_items": {
                    "method": "GET",
                    "path": "/api/reference-corpus/items",
                    "required": ["corpus_id"],
                    "optional": ["offset", "include_transcript"],
                    "bounds": {"limit": [1, 100]},
                },
                "find_reference_evidence": {
                    "method": "POST",
                    "path": "/api/reference-corpus/find",
                    "required": ["corpus_id", "query"],
                    "bounds": {"limit": [1, 20]},
                },
                "build_reference_context": {
                    "method": "POST",
                    "path": "/api/reference-corpus/context",
                    "required": ["corpus_id", "query"],
                    "bounds": {"evidence_limit": [1, 20]},
                },
                "summarize_reference_corpus": {
                    "method": "GET",
                    "path": "/api/reference-corpus/summary",
                    "required": ["corpus_id"],
                },
                "audit_against_reference_corpus": {
                    "method": "POST",
                    "path": "/api/reference-corpus/audit",
                    "required": ["corpus_id", "script"],
                    "optional": [
                        "title", "objective", "content_role", "target_viewer",
                        "target_seconds",
                    ],
                    "rights_gate": "patterns_only_no_copy_or_identity_imitation",
                },
                "write_reference_marketing_script": {
                    "method": "POST",
                    "path": "/api/reference-corpus/write-script",
                    "contract": "reference_marketing_script_request_v1",
                    "required": [
                        "corpus_id", "title", "topic", "audience",
                        "objective", "content_role",
                        "topic_distance_from_offer", "target_seconds",
                        "narrative",
                    ],
                    "optional": ["topic_ladder_id", "offer"],
                    "effect": (
                        "compile a deterministic spoken script, audit the exact "
                        "draft, and persist an immutable package receipt"
                    ),
                    "rights_gate": "patterns_only_no_copy_or_identity_imitation",
                },
                "get_reference_marketing_script": {
                    "method": "GET",
                    "path": "/api/reference-corpus/scripts/{script_id}",
                    "required": ["script_id"],
                },
                "acquire_reference_corpus": {
                    "method": "POST",
                    "path": "/api/reference-corpus/acquire",
                    "required": ["username"],
                    "bounds": {"limit": [1, 240]},
                },
                "extract_reference_items": {
                    "method": "POST",
                    "path": "/api/reference-corpus/extract",
                    "required": ["corpus_id"],
                    "bounds": {"limit": [1, 3]},
                    "default": "local typed analysis with optional AI enrichment",
                },
            },
        }
        return audited_agent_response("catalog", {}, result, started_at=started)

    @app.post("/api/relatability/qualitative-audit")
    def audit_qualitative_relatability():
        denied = require_agent_auth()
        if denied:
            return denied
        started = time.monotonic()
        payload = json_body()
        try:
            result = engine.ai_relatability.audit(payload)
        except ValueError as error:
            return audited_invalid_request(
                "audit_qualitative_relatability", payload, error, started
            )
        decision = result.get("decision")
        status_code = (
            200 if decision in {"PASS", NON_AI_PASS_DECISION}
            else 503 if decision == "JUDGE_UNAVAILABLE"
            else 422
        )
        return audited_agent_response(
            "audit_qualitative_relatability",
            payload,
            result,
            status_code,
            started,
        )

    def owned_outcome_query_scope() -> dict[str, Any]:
        return {
            field: request.args.get(field)
            for field in (
                "content_id", "campaign_id", "offer_id",
                "source_platform", "source_id",
            )
            if request.args.get(field) is not None
        }

    @app.post("/api/owned-outcomes/events")
    def ingest_owned_outcome_event():
        denied = require_agent_auth()
        if denied:
            return denied
        started = time.monotonic()
        payload = json_body()
        try:
            if not isinstance(payload, dict):
                raise ValueError("JSON body must be an object")
            result = engine.owned_outcomes.ingest_event(payload)
        except IdempotencyConflict as error:
            result = {
                "status": "error",
                "code": "IDEMPOTENCY_KEY_CONFLICT",
                "error": str(error),
            }
            return audited_agent_response(
                "ingest_owned_outcome_event", payload, result, 409, started
            )
        except ValueError as error:
            return audited_invalid_request(
                "ingest_owned_outcome_event", payload, error, started
            )
        return audited_agent_response(
            "ingest_owned_outcome_event", payload, result,
            201 if result["created"] else 200, started,
        )

    @app.get("/api/owned-outcomes/events")
    def list_owned_outcome_events():
        denied = require_agent_auth()
        if denied:
            return denied
        started = time.monotonic()
        parameters = request.args.to_dict(flat=True)
        try:
            scope = owned_outcome_query_scope()
            filters = engine.owned_outcomes._attribution(
                scope, require_all=False
            )
            limit = max(1, min(500, int(request.args.get("limit", "200"))))
            events = engine.store.owned_outcome_events(filters, limit=limit)
        except ValueError as error:
            return audited_invalid_request(
                "list_owned_outcome_events", parameters, error, started
            )
        result = {
            "status": "ok", "contract": "owned_attribution_event_v1",
            "events": events, "count": len(events), "limit": limit,
        }
        return audited_agent_response(
            "list_owned_outcome_events", {**scope, "limit": limit}, result,
            started_at=started,
        )

    @app.post("/api/owned-outcomes/retention-samples")
    def ingest_owned_retention_sample():
        denied = require_agent_auth()
        if denied:
            return denied
        started = time.monotonic()
        payload = json_body()
        try:
            if not isinstance(payload, dict):
                raise ValueError("JSON body must be an object")
            result = engine.owned_outcomes.ingest_retention_sample(payload)
        except IdempotencyConflict as error:
            result = {
                "status": "error",
                "code": "IDEMPOTENCY_KEY_CONFLICT",
                "error": str(error),
            }
            return audited_agent_response(
                "ingest_owned_retention_sample", payload, result, 409, started
            )
        except ValueError as error:
            return audited_invalid_request(
                "ingest_owned_retention_sample", payload, error, started
            )
        return audited_agent_response(
            "ingest_owned_retention_sample", payload, result,
            201 if result["created"] else 200, started,
        )

    @app.get("/api/owned-outcomes/retention-samples")
    def list_owned_retention_samples():
        denied = require_agent_auth()
        if denied:
            return denied
        started = time.monotonic()
        parameters = request.args.to_dict(flat=True)
        try:
            scope = owned_outcome_query_scope()
            filters = engine.owned_outcomes._attribution(
                scope, require_all=False
            )
            limit = max(1, min(2000, int(request.args.get("limit", "500"))))
            samples = engine.store.owned_retention_samples(filters, limit=limit)
        except ValueError as error:
            return audited_invalid_request(
                "list_owned_retention_samples", parameters, error, started
            )
        result = {
            "status": "ok", "contract": "owned_retention_sample_v1",
            "samples": samples, "count": len(samples), "limit": limit,
        }
        return audited_agent_response(
            "list_owned_retention_samples", {**scope, "limit": limit}, result,
            started_at=started,
        )

    @app.get("/api/owned-outcomes/summary")
    def summarize_owned_outcomes():
        denied = require_agent_auth()
        if denied:
            return denied
        started = time.monotonic()
        parameters = request.args.to_dict(flat=True)
        try:
            scope = owned_outcome_query_scope()
            result = engine.owned_outcomes.summary(scope)
        except ValueError as error:
            return audited_invalid_request(
                "summarize_owned_outcomes", parameters, error, started
            )
        return audited_agent_response(
            "summarize_owned_outcomes", scope, result, started_at=started
        )

    @app.post("/api/script-intelligence/briefs")
    def build_script_intelligence_brief():
        denied = require_agent_auth()
        if denied:
            return denied
        started = time.monotonic()
        payload = json_body()
        try:
            result = engine.script_intelligence.build_brief(payload)
        except ValueError as error:
            return audited_invalid_request(
                "build_script_brief", payload, error, started
            )
        status_code = 201 if result.get("status") == "ready" else 409
        return audited_agent_response(
            "build_script_brief", payload, result, status_code, started
        )

    @app.get("/api/script-intelligence/briefs")
    def list_script_intelligence_briefs():
        denied = require_agent_auth()
        if denied:
            return denied
        started = time.monotonic()
        limit = max(1, min(200, int(request.args.get("limit", "50"))))
        briefs = engine.store.script_briefs(limit=limit)
        result = {
            "status": "ok", "contract": SCRIPT_INTELLIGENCE_BRIEF_CONTRACT,
            "briefs": briefs, "count": len(briefs), "limit": limit,
        }
        return audited_agent_response(
            "list_script_briefs", {"limit": limit}, result, started_at=started
        )

    @app.get("/api/script-intelligence/briefs/<brief_id>")
    def get_script_intelligence_brief(brief_id: str):
        denied = require_agent_auth()
        if denied:
            return denied
        started = time.monotonic()
        brief = engine.store.script_brief(brief_id)
        result = (
            {"status": "ok", "brief": brief}
            if brief is not None
            else {"status": "error", "code": "SCRIPT_BRIEF_NOT_FOUND", "brief_id": brief_id}
        )
        return audited_agent_response(
            "get_script_brief", {"brief_id": brief_id}, result,
            200 if brief is not None else 404, started,
        )

    @app.post("/api/script-intelligence/generate-and-audit")
    def generate_and_audit_from_brief():
        denied = require_agent_auth()
        if denied:
            return denied
        started = time.monotonic()
        payload = json_body()
        result = engine.script_intelligence.generate_and_audit(payload)
        status_code = 200 if result.get("status") in {"approved", "revise"} else 422
        return audited_agent_response(
            "generate_and_audit", payload, result, status_code, started
        )

    @app.post("/api/script-intelligence/run")
    def run_script_intelligence_workflow():
        denied = require_agent_auth()
        if denied:
            return denied
        started = time.monotonic()
        payload = json_body()
        try:
            brief = engine.script_intelligence.build_brief(payload)
        except ValueError as error:
            return audited_invalid_request(
                "run_trend_to_script", payload, error, started
            )
        if brief.get("status") != "ready":
            variant_unavailable = (
                brief.get("code") == "SCRIPT_VARIANT_INDEX_NOT_AVAILABLE"
            )
            result = {
                "status": "not_ready",
                "phase": (
                    "variant_selection"
                    if variant_unavailable else "evidence_acquisition"
                ),
                "brief_attempt": brief,
                "script_generated": False,
            }
            return audited_agent_response(
                "run_trend_to_script", payload, result, 409, started
            )
        workflow = engine.script_intelligence.generate_and_audit({
            "brief_id": brief["brief_id"],
        })
        result = {
            "status": workflow.get("status"),
            "phase": "script_audited",
            "brief": brief,
            "workflow": workflow,
            "script_generated": True,
        }
        status_code = 200 if workflow.get("status") in {
            "approved", "revise"
        } else 422
        return audited_agent_response(
            "run_trend_to_script", payload, result, status_code, started
        )

    @app.get("/api/script-intelligence/scripts/<script_id>")
    def get_script_intelligence_lineage(script_id: str):
        denied = require_agent_auth()
        if denied:
            return denied
        started = time.monotonic()
        script = engine.store.script(script_id)
        workflows = engine.store.workflow_runs(script_id=script_id, limit=20)
        brief = (
            engine.store.script_brief(str(workflows[0]["brief_id"]))
            if workflows else None
        )
        result = (
            {
                "status": "ok",
                "script": script,
                "gates": engine.store.script_gate_summary(script_id),
                "workflows": workflows,
                "brief": brief,
            }
            if script is not None
            else {
                "status": "error",
                "code": "SCRIPT_NOT_FOUND",
                "script_id": script_id,
            }
        )
        return audited_agent_response(
            "get_script_lineage",
            {"script_id": script_id},
            result,
            200 if script is not None else 404,
            started,
        )

    @app.post("/api/scripts/generate")
    def generate_script():
        result = engine.scripts.generate(json_body())
        return jsonify(result), 422 if result.get("status") == "rejected" else 200

    @app.post("/api/narrative-coherence/audit")
    def narrative_coherence_audit():
        payload = json_body()
        if not payload.get("timeline"):
            raise ValueError("a semantic timeline is required to audit narrative coherence")
        return jsonify(engine.narrative.audit(payload))

    @app.get("/api/scripts/<script_id>")
    def get_script(script_id: str):
        script = engine.store.script(script_id)
        if script is None:
            return jsonify({"status": "error", "code": "SCRIPT_NOT_FOUND", "script_id": script_id}), 404
        return jsonify({"status": "ok", "script": script, "gates": engine.store.script_gate_summary(script_id)})

    @app.post("/api/relatability/script-audit")
    def relatability_audit():
        return jsonify(engine.relatability.audit(json_body()))

    @app.post("/api/attention/script-audit")
    def attention_script_audit():
        return jsonify(engine.attention.script_audit(json_body()))

    @app.post("/api/attention/video-preflight")
    def video_preflight():
        return jsonify(engine.attention.video_preflight(json_body()))

    @app.post("/api/attention/video-file-audit")
    def video_file_audit():
        return jsonify(engine.attention.video_file_audit(json_body()))

    @app.post("/api/attention/video-upload-audit")
    def video_upload_audit():
        upload = request.files.get("video")
        if upload is None or not upload.filename:
            raise ValueError("multipart field 'video' is required")
        extension = Path(upload.filename).suffix.lower()
        if extension not in {".mp4", ".mov", ".m4v", ".webm"}:
            raise ValueError("video must be mp4, mov, m4v, or webm")
        try:
            timeline = json.loads(request.form.get("timeline_json") or "[]")
        except json.JSONDecodeError as exc:
            raise ValueError("timeline_json must be valid JSON") from exc
        if not isinstance(timeline, list) or not timeline:
            raise ValueError("timeline_json must contain a non-empty semantic timeline")
        staging = engine.store.path.parent / "staging"
        staging.mkdir(parents=True, exist_ok=True)
        destination = staging / f"upload_{uuid.uuid4().hex}{extension}"
        upload.save(destination)
        result = engine.attention.video_file_audit({
            "video_path": str(destination),
            "video_id": request.form.get("video_id"),
            "script_id": request.form.get("script_id"),
            "timeline": timeline,
        })
        result["staged_video_path"] = str(destination)
        result["original_filename"] = Path(upload.filename).name
        return jsonify(result)

    @app.post("/api/retention/normalize")
    def normalize_retention():
        return jsonify(engine.retention.normalize(json_body()))

    @app.post("/api/retention/classify")
    def classify_retention():
        return jsonify(engine.retention.classify(json_body()))

    @app.get("/api/learning/receipts")
    def learning_receipts():
        limit = int(request.args.get("limit", "50"))
        return jsonify({"status": "ok", "receipts": engine.store.receipts(limit=limit), "counts": engine.store.counts()})

    @app.get("/api/learning/scripts")
    def learning_scripts():
        limit = int(request.args.get("limit", "50"))
        scripts = engine.store.scripts(limit=limit)
        return jsonify({
            "status": "ok",
            "scripts": [
                {"script": script, "gates": engine.store.script_gate_summary(script["script_id"])}
                for script in scripts
            ],
            "count": len(scripts),
        })

    return app
