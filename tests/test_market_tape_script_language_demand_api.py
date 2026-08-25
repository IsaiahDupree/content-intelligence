"""Typed, authenticated agent access to the real SQLite demand ledger."""

from __future__ import annotations

from flask import Flask

from services.market_tape.api import register_market_tape_routes
from services.market_tape.config import MarketTapeConfig


TOKEN = "market-tape-demand-api-test-token"


def _app(tmp_path, monkeypatch) -> Flask:
    monkeypatch.setenv("MARKET_TAPE_CONTROL_TOKEN", TOKEN)
    monkeypatch.delenv("YOUTUBE_API_KEY", raising=False)
    monkeypatch.delenv("YOUTUBE_DATA_API_KEY", raising=False)
    config = MarketTapeConfig(
        db_path=tmp_path / "market.sqlite3",
        object_dir=tmp_path / "objects",
        heartbeat_path=tmp_path / "heartbeat.json",
        lock_path=tmp_path / "market.lock",
        local_research_state_path=tmp_path / "local-state.json",
        prediction_model_dir=tmp_path / "models",
        platforms=["youtube"],
        topics=["creator retention"],
        supabase_sync_enabled=False,
    )
    app = Flask(__name__)
    register_market_tape_routes(app, config)
    return app


def _payload() -> dict:
    return {
        "contract": "market_tape_script_language_demand_v1",
        "source_service": "content-quality",
        "source_receipt_id": "cq-refusal-receipt-1",
        "topic": "creator retention",
        "audience": "software founders",
        "objective": "qualified attention",
        "evidence_trend_id": "trend:creator-retention",
        "snapshot_id": "mtsnap_integration",
        "targets": {
            "verified_transcripts": 5,
            "distinct_creators": 3,
            "observed_views": 100_000,
        },
        "acquisition_policy": {
            "cycles": 1,
            "platforms": ["youtube"],
            "discovery_limit": 50,
            "transcript_limit": 10,
            "whisper_model": "base",
            "creator_diverse": True,
            "same_call_retry": False,
        },
    }


def test_agent_catalog_and_demand_routes_are_authenticated_and_bounded(
    tmp_path, monkeypatch
):
    client = _app(tmp_path, monkeypatch).test_client()
    headers = {"Authorization": f"Bearer {TOKEN}"}

    assert client.get("/api/market-tape/agent/catalog").status_code == 401
    assert client.get("/api/market-tape/script-language-demands").status_code == 401
    assert client.post(
        "/api/market-tape/script-language-demands", json=_payload()
    ).status_code == 401

    catalog = client.get(
        "/api/market-tape/agent/catalog", headers=headers
    )
    assert catalog.status_code == 200
    catalog_body = catalog.get_json()
    assert catalog_body["database_access"] == "typed_bounded_api_only"
    assert catalog_body["arbitrary_sql_allowed"] is False
    assert catalog_body["markdown_runtime_state"] is False

    first = client.post(
        "/api/market-tape/script-language-demands",
        json=_payload(),
        headers=headers,
    )
    replay = client.post(
        "/api/market-tape/script-language-demands",
        json=_payload(),
        headers=headers,
    )
    assert first.status_code == 201
    assert replay.status_code == 200
    demand = first.get_json()
    assert demand["state"] == "requested"
    assert demand["enqueued"] is True
    assert replay.get_json()["demand_id"] == demand["demand_id"]
    assert replay.get_json()["idempotent"] is True

    listing = client.get(
        "/api/market-tape/script-language-demands?state=requested&limit=9999",
        headers=headers,
    )
    detail = client.get(
        f"/api/market-tape/script-language-demands/{demand['demand_id']}",
        headers=headers,
    )
    assert listing.status_code == 200
    assert listing.get_json()["limit"] == 500
    assert listing.get_json()["count"] == 1
    assert detail.status_code == 200
    assert detail.get_json()["demand"]["events"][0]["event_type"] == "requested"

    run = client.post(
        "/api/market-tape/script-language-demands/run-next",
        json={"lease_seconds": 7200},
        headers=headers,
    )
    assert run.status_code == 200
    assert run.get_json()["processed"] == 1
    assert run.get_json()["goal_met"] is False
    assert run.get_json()["state"] == "blocked"


def test_demand_routes_fail_closed_on_invalid_contract_or_state(
    tmp_path, monkeypatch
):
    client = _app(tmp_path, monkeypatch).test_client()
    headers = {"Authorization": f"Bearer {TOKEN}"}

    invalid = _payload()
    invalid["snapshot_id"] = ""
    response = client.post(
        "/api/market-tape/script-language-demands",
        json=invalid,
        headers=headers,
    )
    invalid_state = client.get(
        "/api/market-tape/script-language-demands?state=imaginary",
        headers=headers,
    )

    assert response.status_code == 400
    assert response.get_json()["error"] == "snapshot_id is required"
    assert invalid_state.status_code == 400
    assert "state must be" in invalid_state.get_json()["error"]

    wrong_contract = _payload()
    wrong_contract["contract"] = "untyped_payload_v0"
    rejected_contract = client.post(
        "/api/market-tape/script-language-demands",
        json=wrong_contract,
        headers=headers,
    )
    assert rejected_contract.status_code == 400
    assert "contract must be" in rejected_contract.get_json()["error"]
