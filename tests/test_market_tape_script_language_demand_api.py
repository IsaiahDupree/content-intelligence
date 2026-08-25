"""Typed, authenticated agent access to the real SQLite demand ledger."""

from __future__ import annotations

import json

from flask import Flask

from services.market_tape.api import register_market_tape_routes
from services.market_tape.config import MarketTapeConfig
from services.market_tape.store import MarketTapeStore


TOKEN = "market-tape-demand-api-test-token"


def _config(tmp_path) -> MarketTapeConfig:
    return MarketTapeConfig(
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


def _app(tmp_path, monkeypatch) -> Flask:
    monkeypatch.setenv("MARKET_TAPE_CONTROL_TOKEN", TOKEN)
    monkeypatch.delenv("YOUTUBE_API_KEY", raising=False)
    monkeypatch.delenv("YOUTUBE_DATA_API_KEY", raising=False)
    config = _config(tmp_path)
    app = Flask(__name__)
    register_market_tape_routes(
        app,
        config,
        transcript_storage_root=tmp_path / "transcript-bank",
    )
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


def _seed_legacy_v12_partial_duplicates(
    config: MarketTapeConfig,
) -> list[str]:
    """Build three real pre-lineage partial generations in one SQLite spool."""

    store = MarketTapeStore(config)
    demand_ids = [f"legacy-demand-{index}" for index in range(1, 4)]
    with store.connect() as connection:
        connection.execute(
            "UPDATE mt_meta SET value = '12' WHERE key = 'schema_version'"
        )
        for index, demand_id in enumerate(demand_ids, start=1):
            hour = 10 + index
            requested_at = f"2026-08-24T{hour:02d}:00:00+00:00"
            claimed_at = f"2026-08-24T{hour:02d}:01:00+00:00"
            partial_at = f"2026-08-24T{hour:02d}:02:00+00:00"
            request_sha256 = f"legacy-request-sha256-{index}"
            snapshot_id = f"legacy-snapshot-{index}"
            source_receipt_id = f"legacy-receipt-{index}"
            request_payload = _payload()
            request_payload.update({
                "source_receipt_id": source_receipt_id,
                "snapshot_id": snapshot_id,
                "evidence_trend_id": f"legacy-trend-{index}",
                "requested_at": requested_at,
            })
            common = (
                demand_id,
                request_sha256,
                "creator retention",
                "software founders",
                "qualified attention",
                f"legacy-trend-{index}",
                snapshot_id,
            )
            connection.execute(
                """INSERT INTO mt_script_language_demand_events(
                       event_id, demand_id, event_type, attempt_no,
                       request_sha256, source_service, source_receipt_id,
                       topic, audience, objective, evidence_trend_id,
                       snapshot_id, lease_until, collection_run_id,
                       transcript_run_id, payload_json, created_at
                   ) VALUES(?, ?, 'requested', 0, ?, 'content-quality', ?,
                            ?, ?, ?, ?, ?, NULL, '', '', ?, ?)""",
                (
                    f"legacy-request-event-{index}",
                    common[0], common[1], source_receipt_id,
                    common[2], common[3], common[4], common[5], common[6],
                    json.dumps(request_payload, sort_keys=True), requested_at,
                ),
            )
            connection.execute(
                """INSERT INTO mt_script_language_demand_events(
                       event_id, demand_id, event_type, attempt_no,
                       request_sha256, source_service, source_receipt_id,
                       topic, audience, objective, evidence_trend_id,
                       snapshot_id, lease_until, collection_run_id,
                       transcript_run_id, payload_json, created_at
                   ) VALUES(?, ?, 'claimed', 1, ?,
                            'legacy-demand-worker', ?, ?, ?, ?, ?, ?, ?, '',
                            '', ?, ?)""",
                (
                    f"legacy-claim-event-{index}",
                    common[0], common[1], f"legacy-claim-{index}",
                    common[2], common[3], common[4], common[5], common[6],
                    f"2026-08-24T{hour:02d}:01:30+00:00",
                    json.dumps({
                        "contract": "market_tape_script_language_demand_event_v1",
                        "event_type": "claimed",
                        "snapshot_id": snapshot_id,
                    }, sort_keys=True),
                    claimed_at,
                ),
            )
            connection.execute(
                """INSERT INTO mt_script_language_demand_events(
                       event_id, demand_id, event_type, attempt_no,
                       request_sha256, source_service, source_receipt_id,
                       topic, audience, objective, evidence_trend_id,
                       snapshot_id, lease_until, collection_run_id,
                       transcript_run_id, payload_json, created_at
                   ) VALUES(?, ?, 'partial', 1, ?,
                            'legacy-demand-worker', ?, ?, ?, ?, ?, ?, NULL, '',
                            '', ?, ?)""",
                (
                    f"legacy-partial-event-{index}",
                    common[0], common[1], f"legacy-partial-{index}",
                    common[2], common[3], common[4], common[5], common[6],
                    json.dumps({
                        "contract": "market_tape_script_language_demand_event_v1",
                        "event_type": "partial",
                        "result": {"goal_met": False},
                    }, sort_keys=True),
                    partial_at,
                ),
            )
    return demand_ids


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
    refreshed_payload = _payload()
    refreshed_payload["snapshot_id"] = "mtsnap_integration_2"
    refreshed_payload["source_receipt_id"] = "cq-refusal-receipt-2"
    refreshed_payload["evidence_trend_id"] = "trend:creator-retention:2"
    refreshed = client.post(
        replay.request.path,
        json=refreshed_payload,
        headers=headers,
    )
    assert first.status_code == 201
    assert replay.status_code == 200
    assert refreshed.status_code == 200
    demand = first.get_json()
    assert demand["state"] == "requested"
    assert demand["enqueued"] is True
    assert replay.get_json()["demand_id"] == demand["demand_id"]
    assert replay.get_json()["idempotent"] is True
    refreshed_demand = refreshed.get_json()
    assert refreshed_demand["demand_id"] == demand["demand_id"]
    assert refreshed_demand["coalesced"] is True
    assert refreshed_demand["idempotent"] is False
    assert refreshed_demand["snapshot_lineage_appended"] is True
    assert refreshed_demand["latest_snapshot_id"] == "mtsnap_integration_2"
    assert refreshed_demand["latest_source_receipt_id"] == (
        "cq-refusal-receipt-2"
    )

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
    listed_demand = listing.get_json()["demands"][0]
    assert [row["snapshot_id"] for row in listed_demand["snapshot_lineage"]] == [
        "mtsnap_integration",
        "mtsnap_integration_2",
    ]
    assert listed_demand["latest_snapshot_id"] == "mtsnap_integration_2"
    assert listed_demand["latest_source_receipt_id"] == "cq-refusal-receipt-2"
    assert detail.status_code == 200
    detailed_demand = detail.get_json()["demand"]
    assert detailed_demand["events"][0]["event_type"] == "requested"
    assert detailed_demand["latest_snapshot_id"] == "mtsnap_integration_2"
    assert detailed_demand["latest_source_receipt_id"] == "cq-refusal-receipt-2"

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


def test_run_next_expected_demand_conflict_returns_409_without_mutation(
    tmp_path, monkeypatch
):
    app = _app(tmp_path, monkeypatch)
    client = app.test_client()
    headers = {"Authorization": f"Bearer {TOKEN}"}
    enqueued = client.post(
        "/api/market-tape/script-language-demands",
        json=_payload(),
        headers=headers,
    ).get_json()
    demand_id = enqueued["demand_id"]

    conflict = client.post(
        "/api/market-tape/script-language-demands/run-next",
        json={"expected_demand_id": "script-language-demand:not-next"},
        headers=headers,
    )

    assert conflict.status_code == 409
    assert conflict.get_json() == {
        "status": "error",
        "state": "conflict",
        "code": "SCRIPT_LANGUAGE_DEMAND_CLAIM_CONFLICT",
        "error": "expected_demand_id does not match the next claimable demand",
        "expected_demand_id": "script-language-demand:not-next",
        "next_demand_id": demand_id,
        "mutation_applied": False,
    }
    store = MarketTapeStore(_config(tmp_path))
    demand = store.script_language_demand(demand_id)
    assert demand is not None
    assert [event["event_type"] for event in demand["events"]] == [
        "requested"
    ]

    matched = client.post(
        "/api/market-tape/script-language-demands/run-next",
        json={"expected_demand_id": demand_id, "lease_seconds": 7200},
        headers=headers,
    )
    assert matched.status_code == 200
    assert matched.get_json()["processed"] == 1


def test_v12_partial_duplicates_only_expose_newest_semantic_as_claimable(
    tmp_path, monkeypatch
):
    config = _config(tmp_path)
    demand_ids = _seed_legacy_v12_partial_duplicates(config)
    monkeypatch.setenv("MARKET_TAPE_CONTROL_TOKEN", TOKEN)
    app = Flask(__name__)
    store = register_market_tape_routes(app, config)
    client = app.test_client()
    headers = {"Authorization": f"Bearer {TOKEN}"}

    response = client.get(
        "/api/market-tape/script-language-demands?state=partial",
        headers=headers,
    )
    assert response.status_code == 200
    demands = {
        row["demand_id"]: row for row in response.get_json()["demands"]
    }
    assert set(demands) == set(demand_ids)

    newest = demands[demand_ids[-1]]
    assert newest["state"] == "partial"
    assert newest["effective_state"] == "partial"
    assert newest["semantic_generation_role"] == "authoritative"
    assert newest["semantic_authority_demand_id"] == demand_ids[-1]
    assert newest["superseded"] is False
    assert newest["retry_eligible"] is True
    assert newest["claimable"] is True

    for demand_id in demand_ids[:-1]:
        older = demands[demand_id]
        assert older["state"] == "partial"
        assert older["effective_state"] == "superseded"
        assert older["semantic_generation_role"] == "superseded"
        assert older["superseded"] is True
        assert older["superseded_by_demand_id"] == demand_ids[-1]
        assert older["retry_eligible"] is False
        assert older["claimable"] is False
        assert older["supersession"] == {
            "contract": "market_tape_script_language_demand_supersession_v1",
            "reason": "newer_semantic_snapshot_lineage",
            "semantic_key": newest["semantic_key"],
            "superseded_demand_id": demand_id,
            "authoritative_demand_id": demand_ids[-1],
            "authoritative_lineage_sequence": (
                newest["semantic_authority_lineage_sequence"]
            ),
        }

    claim = store.claim_next_script_language_demand(600)
    assert claim is not None
    assert claim["demand_id"] == demand_ids[-1]
    assert claim["attempt_no"] == 2
    assert store.claim_next_script_language_demand(600) is None

    detail = client.get(
        f"/api/market-tape/script-language-demands/{demand_ids[0]}",
        headers=headers,
    )
    assert detail.status_code == 200
    older_detail = detail.get_json()["demand"]
    assert older_detail["effective_state"] == "superseded"
    assert older_detail["superseded_by_demand_id"] == demand_ids[-1]
    assert older_detail["claimable"] is False
