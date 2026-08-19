from __future__ import annotations

import json
import os
import uuid
from pathlib import Path
from typing import Any

from flask import Flask, jsonify, request

from .engine import ContentQualityEngine


DEFAULT_MARKET_TAPE = Path.home() / "Library/Application Support/ContentIntelligence/data/market-tape.sqlite3"
DEFAULT_QUALITY_DB = Path.home() / "Library/Application Support/ContentQuality/data/content-quality.sqlite3"


def create_content_quality_app(config: dict[str, Any] | None = None) -> Flask:
    app = Flask("content-quality")
    app.config.update(config or {})
    engine = ContentQualityEngine(
        app.config.get("MARKET_TAPE_DB") or os.getenv("MARKET_TAPE_DB") or DEFAULT_MARKET_TAPE,
        app.config.get("CONTENT_QUALITY_DB") or os.getenv("CONTENT_QUALITY_DB") or DEFAULT_QUALITY_DB,
    )
    app.extensions["content_quality_engine"] = engine

    @app.errorhandler(ValueError)
    def invalid_request(error: ValueError):
        return jsonify({"status": "error", "code": "INVALID_REQUEST", "error": str(error)}), 400

    @app.errorhandler(404)
    def not_found(_error):
        return jsonify({"status": "error", "code": "NOT_FOUND"}), 404

    @app.get("/health")
    def health():
        report = engine.health()
        return jsonify(report), 200 if report["status"] == "healthy" else 503

    def capability_health(name: str, dependency: str | None = None) -> tuple[Any, int]:
        report = engine.health()
        dependency_status = report["market_tape"]["status"] if dependency == "market_tape" else "up"
        status = "healthy" if dependency_status == "up" else "degraded"
        return jsonify({
            "status": status,
            "service": name,
            "dependency": dependency,
            "dependency_status": dependency_status,
            "checked_at": report["checked_at"],
        }), 200 if status == "healthy" else 503

    health_routes = {
        "/api/audience-intelligence/health": ("audience-intelligence", "market_tape"),
        "/api/viral-transcripts/health": ("viral-transcripts", "market_tape"),
        "/api/scripts/health": ("evidence-first-scripts", None),
        "/api/relatability/health": ("relatability", None),
        "/api/attention/health": ("attention", None),
        "/api/retention/health": ("post-publish-retention", None),
        "/api/learning/health": ("learning-memory", None),
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

    @app.post("/api/viral-transcripts/discover")
    def discover_transcripts():
        payload = json_body()
        return jsonify(engine.viral.discover(str(payload.get("topic") or ""), int(payload.get("limit") or 5)))

    @app.post("/api/audience/human-moments")
    def human_moments():
        payload = json_body()
        return jsonify(engine.audience.human_moments(
            str(payload.get("topic") or ""), str(payload.get("audience") or ""), int(payload.get("limit") or 8)
        ))

    @app.post("/api/scripts/generate")
    def generate_script():
        result = engine.scripts.generate(json_body())
        return jsonify(result), 422 if result.get("status") == "rejected" else 200

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

    return app
