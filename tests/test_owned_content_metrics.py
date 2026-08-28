from __future__ import annotations

import sqlite3
from contextlib import closing

import pytest

from services.content_quality.api import create_content_quality_app


TOKEN = "owned-content-metric-test-token"
HEADERS = {
    "Authorization": f"Bearer {TOKEN}",
    "X-Agent-Principal": "owned-content-metric-integration-test",
}


@pytest.fixture()
def app(tmp_path):
    return create_content_quality_app(
        {
            "TESTING": True,
            "NARRATIVE_COHERENCE_LLM": "off",
            "MARKET_TAPE_DB": tmp_path / "market-tape.sqlite3",
            "CONTENT_QUALITY_DB": tmp_path / "content-quality.sqlite3",
            "CONTENT_QUALITY_CONTROL_TOKEN": TOKEN,
        }
    )


@pytest.fixture()
def client(app):
    return app.test_client()


def snapshot(
    key: str,
    observed_at: str,
    *,
    metrics: dict | None = None,
    status: str = "observed",
    reason: str | None = None,
    source_id: str = "youtube-video-001",
    scope: str = "post",
) -> dict:
    payload = {
        "contract_type": "owned_content_metric_snapshot_v1",
        "idempotency_key": key,
        "measurement_status": status,
        "scope": scope,
        "attribution": {
            "content_id": "content-control-001",
            "campaign_id": "campaign-control-001",
            "offer_id": "offer-audit-build",
            "source_platform": "YouTube Shorts",
            "source_id": source_id,
            "account_id": "youtube-isaiah-primary",
            "iteration_id": "iteration-001",
            "experiment_id": "sxp_lineage_001",
            "variant_id": "hook-a",
        },
        "permalink": f"https://www.youtube.com/watch?v={source_id}",
        "provider_name": "youtube_data_api_v3",
        "provider_receipt_id": f"receipt-{key}",
        "observed_at": observed_at,
        "metrics": metrics or {},
        "metadata": {"source": "owned-provider-read"},
    }
    if scope == "redirect":
        payload["tracked_url"] = f"https://go.example/r/{source_id}"
    if reason is not None:
        payload["unavailable_reason"] = reason
    return payload


def post(client, payload):
    return client.post(
        "/api/owned-content-metrics/snapshots",
        json=payload,
        headers=HEADERS,
    )


def test_routes_require_auth_and_observed_zero_remains_observed(client):
    payload = snapshot(
        "zero-observation",
        "2026-08-28T12:00:00-04:00",
        metrics={"views": 0, "likes": 0},
    )
    denied = client.post(
        "/api/owned-content-metrics/snapshots", json=payload
    )
    created = post(client, payload)
    summary = client.get(
        "/api/owned-content-metrics/summary", headers=HEADERS
    )

    assert denied.status_code == 401
    assert created.status_code == 201
    assert created.get_json()["snapshot"]["measurement_status"] == "observed"
    assert created.get_json()["snapshot"]["metrics"] == {
        "likes": 0,
        "views": 0,
    }
    assert summary.status_code == 200
    body = summary.get_json()
    assert body["status"] == "ready"
    assert body["latest_entity_statuses"]["observed"] == 1
    assert body["totals"] == {"likes": 0, "views": 0}
    assert body["coverage"]["post_permalink_rate"] == 1.0


def test_unavailable_is_not_a_zero_and_supersedes_stale_totals(client):
    first = snapshot(
        "views-before-outage",
        "2026-08-28T12:00:00-04:00",
        metrics={"views": 83, "likes": 4},
    )
    unavailable = snapshot(
        "provider-outage",
        "2026-08-28T13:00:00-04:00",
        status="unavailable",
        reason="provider credential could not read this object",
    )
    assert post(client, first).status_code == 201
    assert post(client, unavailable).status_code == 201

    summary = client.get(
        "/api/owned-content-metrics/summary", headers=HEADERS
    ).get_json()
    assert summary["status"] == "partial"
    assert summary["latest_entity_statuses"]["unavailable"] == 1
    assert summary["observed_entity_count"] == 0
    assert summary["totals"] == {}
    rows = client.get(
        "/api/owned-content-metrics/snapshots?limit=10", headers=HEADERS
    ).get_json()["snapshots"]
    assert len(rows) == 2
    assert rows[0]["measurement_status"] == "unavailable"
    assert rows[0]["metrics"] == {}
    assert rows[0]["unavailable_reason"].startswith("provider credential")


def test_availability_contract_rejects_ambiguous_payloads(client):
    missing_metrics = snapshot(
        "ambiguous-observed",
        "2026-08-28T12:00:00-04:00",
    )
    outage_with_metrics = snapshot(
        "ambiguous-outage",
        "2026-08-28T12:05:00-04:00",
        status="unavailable",
        reason="provider down",
        metrics={"views": 0},
    )
    assert post(client, missing_metrics).status_code == 400
    response = post(client, outage_with_metrics)
    assert response.status_code == 400
    assert "cannot include metrics" in response.get_json()["error"]


def test_batch_is_atomic_idempotent_and_conflicts_on_changed_evidence(
    app, client
):
    first = snapshot(
        "batch-one",
        "2026-08-28T12:00:00-04:00",
        metrics={"views": 10},
    )
    second = snapshot(
        "batch-two",
        "2026-08-28T12:00:00-04:00",
        metrics={"clicks": 2},
        source_id="redirect-001",
        scope="redirect",
    )
    endpoint = "/api/owned-content-metrics/snapshots/batch"
    created = client.post(
        endpoint, json={"snapshots": [first, second]}, headers=HEADERS
    )
    replay = client.post(
        endpoint, json={"snapshots": [first, second]}, headers=HEADERS
    )
    assert created.status_code == 201
    assert created.get_json()["created"] == 2
    assert replay.status_code == 200
    assert replay.get_json()["idempotent_replays"] == 2

    conflict = {**first, "metrics": {"views": 11}}
    third = snapshot(
        "batch-three",
        "2026-08-28T12:10:00-04:00",
        metrics={"views": 1},
        source_id="youtube-video-002",
    )
    failed = client.post(
        endpoint, json={"snapshots": [third, conflict]}, headers=HEADERS
    )
    assert failed.status_code == 409

    database = app.extensions["owned_content_metric_telemetry"].path
    with closing(sqlite3.connect(database)) as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM cq_owned_content_metric_snapshots"
        ).fetchone()[0] == 2


def test_metric_evidence_is_append_only_and_health_reports_coverage(app, client):
    post_payload = snapshot(
        "append-only-post",
        "2026-08-28T12:00:00-04:00",
        metrics={"viewCount": 20, "shareCount": 2},
    )
    redirect_payload = snapshot(
        "append-only-redirect",
        "2026-08-28T12:05:00-04:00",
        metrics={"redirect_clicks": 3},
        source_id="redirect-002",
        scope="redirect",
    )
    assert post(client, post_payload).status_code == 201
    assert post(client, redirect_payload).status_code == 201

    health = client.get(
        "/api/owned-content-metrics/health", headers=HEADERS
    ).get_json()
    assert health["snapshot_count"] == 2
    assert health["entity_count"] == 2
    assert health["totals"] == {
        "link_clicks": 3,
        "shares": 2,
        "views": 20,
    }
    assert health["coverage"]["post_permalink_rate"] == 1.0
    assert health["coverage"]["tracked_url_rate"] == 1.0
    assert health["score_is_probability"] is False

    database = app.extensions["owned_content_metric_telemetry"].path
    with closing(sqlite3.connect(database)) as connection:
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            connection.execute(
                "UPDATE cq_owned_content_metric_snapshots SET source_id='x'"
            )
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            connection.execute(
                "DELETE FROM cq_owned_content_metric_snapshots"
            )
