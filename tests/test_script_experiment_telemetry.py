from __future__ import annotations

import hashlib
import sqlite3
from contextlib import closing

import pytest

from services.content_quality.api import create_content_quality_app
from services.content_quality.script_experiments import stable_experiment_id


TOKEN = "script-experiment-test-token"
HEADERS = {
    "Authorization": f"Bearer {TOKEN}",
    "X-Agent-Principal": "script-experiment-integration-test",
}
SCRIPT_TEXT = (
    "I lost three hours rebuilding a follow-up that should have taken ten "
    "minutes. Here is the exact handoff that fixed it."
)


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


def experiment_payload(**overrides):
    payload = {
        "brief_id": "brief_founder_followup_001",
        "script_id": "script_founder_followup_001",
        "script_text": SCRIPT_TEXT,
        "workflow_id": "workflow_founder_followup_001",
        "generation_contract": "evidence_first_script_v1",
        "metadata": {"owner_calibration_set": "v5"},
    }
    payload.update(overrides)
    return payload


def register(client, **overrides):
    response = client.post(
        "/api/script-experiments",
        json=experiment_payload(**overrides),
        headers=HEADERS,
    )
    assert response.status_code in {200, 201}
    return response.get_json()["experiment"]


def metric_payload(
    experiment_id: str,
    idempotency_key: str,
    observed_at: str,
    metrics: dict,
    *,
    platform: str = "Instagram Reels",
    post_id: str = "ig-reel-001",
    receipt_id: str | None = None,
    cta_outcomes: dict | None = None,
):
    payload = {
        "idempotency_key": idempotency_key,
        "experiment_id": experiment_id,
        "source_platform": platform,
        "provider_post_id": post_id,
        "provider_receipt_id": receipt_id or f"provider-receipt-{idempotency_key}",
        "provider_event_id": f"provider-event-{idempotency_key}",
        "view_denominator_basis": "video_starts",
        "observed_at": observed_at,
        "metrics": metrics,
        "metadata": {"source": "owned-provider-export"},
    }
    if cta_outcomes is not None:
        payload["cta_outcomes"] = cta_outcomes
    return payload


def test_stable_experiment_identity_is_lineage_derived():
    digest = hashlib.sha256(SCRIPT_TEXT.encode("utf-8")).hexdigest()
    first = stable_experiment_id(
        brief_id="brief-1",
        script_id="script-1",
        script_sha256=digest,
        workflow_seed="workflow-1",
    )
    replay = stable_experiment_id(
        brief_id="brief-1",
        script_id="script-1",
        script_sha256=digest,
        workflow_seed="workflow-1",
    )
    different_workflow = stable_experiment_id(
        brief_id="brief-1",
        script_id="script-1",
        script_sha256=digest,
        workflow_seed="workflow-2",
    )

    assert first == replay
    assert first.startswith("sxp_")
    assert different_workflow != first


def test_registration_is_authenticated_stable_and_does_not_store_script_text(
    app, client
):
    denied = client.post("/api/script-experiments", json=experiment_payload())
    assert denied.status_code == 401

    created = client.post(
        "/api/script-experiments", json=experiment_payload(), headers=HEADERS
    )
    replay = client.post(
        "/api/script-experiments", json=experiment_payload(), headers=HEADERS
    )

    assert created.status_code == 201
    assert replay.status_code == 200
    first = created.get_json()["experiment"]
    assert replay.get_json()["experiment"]["experiment_id"] == first["experiment_id"]
    assert "script_text" not in first
    assert first["script_sha256"] == hashlib.sha256(
        SCRIPT_TEXT.encode("utf-8")
    ).hexdigest()

    database = app.extensions["script_experiment_telemetry"].path
    with closing(sqlite3.connect(database)) as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM cq_script_experiments"
        ).fetchone()[0] == 1
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            connection.execute(
                "UPDATE cq_script_experiments SET workflow_seed='changed'"
            )


def test_provider_metrics_normalize_and_roll_up_without_double_counting(client):
    experiment = register(client)
    experiment_id = experiment["experiment_id"]
    first = metric_payload(
        experiment_id,
        "ig-snapshot-001",
        "2026-08-25T12:00:00-04:00",
        {
            "plays": 1000,
            "oneSecondVideoViews": 800,
            "threeSecondVideoViews": 600,
            "videoCompletions": 250,
            "shares": 40,
            "saved": 30,
        },
        cta_outcomes={
            "clicks": 20,
            "leads": 5,
            "signups": 4,
            "trials": 3,
            "purchases": 2,
        },
    )
    created = client.post(
        "/api/script-experiments/metrics", json=first, headers=HEADERS
    )
    replay = client.post(
        "/api/script-experiments/metrics", json=first, headers=HEADERS
    )
    assert created.status_code == 201
    assert replay.status_code == 200
    normalized = created.get_json()["snapshot"]
    assert normalized["source_platform"] == "instagram"
    assert normalized["metrics"] == {
        "views": 1000,
        "hold_1s_views": 800,
        "hold_3s_views": 600,
        "completed_views": 250,
        "shares": 40,
        "saves": 30,
        "cta_clicks": 20,
        "cta_leads": 5,
        "cta_signups": 4,
        "cta_trials": 3,
        "cta_purchases": 2,
    }
    assert normalized["provider_metric_names"]["views"] == ["plays"]
    assert normalized["provider_metric_names"]["cta_clicks"] == [
        "cta_outcomes.clicks"
    ]
    assert normalized["provider_receipt_id"] == "provider-receipt-ig-snapshot-001"

    later = metric_payload(
        experiment_id,
        "ig-snapshot-002",
        "2026-08-26T12:00:00-04:00",
        {
            "plays": 2000,
            "oneSecondVideoViews": 1500,
            "threeSecondVideoViews": 1000,
            "videoCompletions": 500,
            "shares": 60,
            "saved": 50,
        },
        cta_outcomes={
            "clicks": 30,
            "leads": 8,
            "signups": 6,
            "trials": 4,
            "purchases": 3,
        },
    )
    second_post = metric_payload(
        experiment_id,
        "tt-snapshot-001",
        "2026-08-26T13:00:00Z",
        {
            "videoViewCount": 100,
            "oneSecondViews": 90,
            "threeSecondViews": 50,
            "completedViews": 20,
            "shares": 3,
            "favorites": 5,
        },
        platform="TikTok",
        post_id="tt-video-001",
    )
    for payload in (later, second_post):
        assert client.post(
            "/api/script-experiments/metrics", json=payload, headers=HEADERS
        ).status_code == 201

    response = client.get(
        "/api/script-experiments/rollup?script_id=script_founder_followup_001",
        headers=HEADERS,
    )
    rollup = response.get_json()
    assert response.status_code == 200
    assert rollup["experiment_ids"] == [experiment_id]
    assert rollup["metric_snapshot_count"] == 3
    assert rollup["post_count"] == 2
    assert rollup["totals"] == {
        "views": 2100,
        "hold_1s_views": 1590,
        "hold_3s_views": 1050,
        "completed_views": 520,
        "shares": 63,
        "saves": 55,
        "cta_clicks": 30,
        "cta_leads": 8,
        "cta_signups": 6,
        "cta_trials": 4,
        "cta_purchases": 3,
    }
    assert rollup["rates"]["hold_1s"] == {
        "status": "observed",
        "numerator": 1590,
        "eligible_denominator": 2100,
        "denominator_metric": "views",
        "rate": 0.757143,
        "posts_with_numerator_and_denominator": 2,
        "posts_excluded_for_denominator_basis": 0,
        "required_denominator_basis": "video_starts",
        "aggregation": "denominator_weighted",
        "causal_claim": False,
    }
    assert rollup["rates"]["hold_3s"]["rate"] == 0.5
    assert rollup["rates"]["completion"]["rate"] == 0.247619
    assert rollup["rates"]["cta"]["clicks"]["numerator"] == 30
    assert rollup["rates"]["cta"]["clicks"]["eligible_denominator"] == 2000
    assert rollup["rates"]["cta"]["clicks"]["rate"] == 0.015
    assert rollup["causal_policy"]["causal_claim"] is False


def test_metric_contract_rejects_rates_bad_counts_and_missing_lineage(client):
    experiment_id = register(client)["experiment_id"]
    invalid_payloads = (
        metric_payload(
            experiment_id,
            "bad-rate",
            "2026-08-25T12:00:00Z",
            {"views": 100, "completion_rate": 0.5},
        ),
        metric_payload(
            experiment_id,
            "bad-retention",
            "2026-08-25T12:00:00Z",
            {"views": 100, "hold_1s_views": 101},
        ),
        metric_payload(
            experiment_id,
            "bad-fraction",
            "2026-08-25T12:00:00Z",
            {"views": 100.5},
        ),
    )
    invalid_payloads[2].pop("provider_receipt_id")
    for payload in invalid_payloads:
        response = client.post(
            "/api/script-experiments/metrics", json=payload, headers=HEADERS
        )
        assert response.status_code == 400
        assert response.get_json()["code"] == "INVALID_REQUEST"

    no_scope = client.get("/api/script-experiments/rollup", headers=HEADERS)
    assert no_scope.status_code == 400


def test_ineligible_view_basis_keeps_counts_but_refuses_rates(client):
    experiment = register(client)
    payload = metric_payload(
        experiment["experiment_id"],
        "feed-impression-snapshot",
        "2026-08-25T12:00:00Z",
        {
            "views": 1000,
            "hold_1s_views": 500,
            "hold_3s_views": 300,
            "completed_views": 100,
            "shares": 25,
            "saves": 10,
        },
    )
    payload["view_denominator_basis"] = "shown_in_feed"
    assert client.post(
        "/api/script-experiments/metrics", json=payload, headers=HEADERS
    ).status_code == 201

    rollup = client.get(
        f"/api/script-experiments/rollup?experiment_id={experiment['experiment_id']}",
        headers=HEADERS,
    ).get_json()
    assert rollup["totals"]["hold_1s_views"] == 500
    assert rollup["totals"]["shares"] == 25
    assert rollup["rates"]["hold_1s"]["rate"] is None
    assert rollup["rates"]["hold_1s"]["status"] == (
        "denominator_basis_not_eligible"
    )
    assert rollup["rates"]["hold_1s"]["eligible_denominator"] == 0
    assert rollup["rates"]["hold_1s"][
        "posts_excluded_for_denominator_basis"
    ] == 1


def test_idempotency_and_provider_post_attribution_are_immutable(app, client):
    first_experiment = register(client)
    second_experiment = register(
        client,
        script_id="script_founder_followup_002",
        workflow_id="workflow_founder_followup_002",
    )
    first_payload = metric_payload(
        first_experiment["experiment_id"],
        "immutable-metric-001",
        "2026-08-25T12:00:00Z",
        {"views": 100, "hold_1s_views": 80},
        post_id="shared-provider-post",
    )
    assert client.post(
        "/api/script-experiments/metrics", json=first_payload, headers=HEADERS
    ).status_code == 201

    changed_replay = {
        **first_payload,
        "metrics": {"views": 101, "hold_1s_views": 80},
    }
    conflict = client.post(
        "/api/script-experiments/metrics", json=changed_replay, headers=HEADERS
    )
    assert conflict.status_code == 409
    assert conflict.get_json()["code"] == "IMMUTABLE_EXPERIMENT_CONFLICT"

    reassigned = metric_payload(
        second_experiment["experiment_id"],
        "immutable-metric-002",
        "2026-08-25T13:00:00Z",
        {"views": 100},
        post_id="shared-provider-post",
    )
    conflict = client.post(
        "/api/script-experiments/metrics", json=reassigned, headers=HEADERS
    )
    assert conflict.status_code == 409
    assert conflict.get_json()["code"] == "IMMUTABLE_EXPERIMENT_CONFLICT"

    listed = client.get(
        f"/api/script-experiments/metrics?experiment_id={first_experiment['experiment_id']}",
        headers=HEADERS,
    )
    assert listed.status_code == 200
    assert listed.get_json()["count"] == 1

    database = app.extensions["script_experiment_telemetry"].path
    with closing(sqlite3.connect(database)) as connection:
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            connection.execute(
                "UPDATE cq_script_metric_snapshots SET metrics_json='{}'"
            )
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            connection.execute("DELETE FROM cq_script_experiment_posts")
