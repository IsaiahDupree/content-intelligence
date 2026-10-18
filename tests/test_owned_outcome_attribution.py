from __future__ import annotations

import sqlite3
from contextlib import closing

import pytest

from services.content_quality.api import create_content_quality_app


TOKEN = "owned-outcome-test-token"
HEADERS = {
    "Authorization": f"Bearer {TOKEN}",
    "X-Agent-Principal": "owned-outcome-integration-test",
}
ATTRIBUTION = {
    "content_id": "content-yt-001",
    "campaign_id": "campaign-launch-001",
    "offer_id": "offer-audit-build",
    "source_platform": "YouTube",
    "source_id": "youtube-video-001",
}


@pytest.fixture()
def app(tmp_path):
    return create_content_quality_app({
        "TESTING": True,
        "NARRATIVE_COHERENCE_LLM": "off",
        "MARKET_TAPE_DB": tmp_path / "market-tape.sqlite3",
        "CONTENT_QUALITY_DB": tmp_path / "content-quality.sqlite3",
        "CONTENT_QUALITY_CONTROL_TOKEN": TOKEN,
    })


@pytest.fixture()
def client(app):
    return app.test_client()


def event_payload(idempotency_key: str, event_type: str, second: int) -> dict:
    return {
        "idempotency_key": idempotency_key,
        "event_type": event_type,
        "attribution": ATTRIBUTION,
        "journey_id": "journey-anonymous-hash-001",
        "occurred_at": f"2026-08-24T12:00:{second:02d}-04:00",
        "provider_event_id": f"provider-{idempotency_key}",
        "metadata": {"integration": "first-party-test"},
    }


def retention_payload(
    idempotency_key: str,
    elapsed_ms: int,
    retained_percent: float,
    *,
    measurement_id: str = "curve-001",
    sample_size: int = 100,
) -> dict:
    return {
        "idempotency_key": idempotency_key,
        "attribution": ATTRIBUTION,
        "measurement_id": measurement_id,
        "observed_at": "2026-08-24T13:00:00-04:00",
        "elapsed_ms": elapsed_ms,
        "retained_percent": retained_percent,
        "sample_size": sample_size,
        "metadata": {"source": "owned-analytics-export"},
    }


def test_owned_outcome_agent_routes_require_bearer_auth(client):
    responses = (
        client.post(
            "/api/owned-outcomes/events",
            json=event_payload("click-1", "click", 1),
        ),
        client.post(
            "/api/owned-outcomes/retention-samples",
            json=retention_payload("curve-1-0", 0, 100),
        ),
        client.get("/api/owned-outcomes/events?content_id=content-yt-001"),
        client.get(
            "/api/owned-outcomes/retention-samples?content_id=content-yt-001"
        ),
        client.get("/api/owned-outcomes/summary?content_id=content-yt-001"),
    )

    assert {response.status_code for response in responses} == {401}
    assert {response.get_json()["code"] for response in responses} == {
        "UNAUTHORIZED"
    }


def test_events_are_idempotent_append_only_attribution_facts(app, client):
    payload = event_payload("provider-click-001", "click", 1)

    created = client.post(
        "/api/owned-outcomes/events", json=payload, headers=HEADERS
    )
    replay = client.post(
        "/api/owned-outcomes/events", json=payload, headers=HEADERS
    )
    conflict = client.post(
        "/api/owned-outcomes/events",
        json={**payload, "event_type": "purchase"},
        headers=HEADERS,
    )

    assert created.status_code == 201
    assert created.get_json()["created"] is True
    assert replay.status_code == 200
    assert replay.get_json()["created"] is False
    assert (
        replay.get_json()["event"]["event_id"]
        == created.get_json()["event"]["event_id"]
    )
    assert created.get_json()["event"]["attribution"] == {
        **ATTRIBUTION,
        "source_platform": "youtube",
    }
    assert conflict.status_code == 409
    assert conflict.get_json()["code"] == "IDEMPOTENCY_KEY_CONFLICT"

    database = app.extensions["content_quality_engine"].store.path
    with closing(sqlite3.connect(database)) as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM cq_owned_outcome_events"
        ).fetchone()[0] == 1
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            connection.execute(
                "UPDATE cq_owned_outcome_events SET event_type='install'"
            )
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            connection.execute("DELETE FROM cq_owned_outcome_events")


def test_funnel_and_retention_summary_stays_inside_observed_evidence(app, client):
    for index, event_type in enumerate(("click", "install", "trial", "purchase"), 1):
        response = client.post(
            "/api/owned-outcomes/events",
            json=event_payload(f"provider-{event_type}-001", event_type, index),
            headers=HEADERS,
        )
        assert response.status_code == 201

    samples = (
        retention_payload("curve-001-0", 0, 100),
        retention_payload("curve-001-1000", 1000, 80),
        retention_payload(
            "curve-002-1000", 1000, 60,
            measurement_id="curve-002", sample_size=100,
        ),
        retention_payload("curve-001-2500", 2500, 55),
    )
    for sample in samples:
        response = client.post(
            "/api/owned-outcomes/retention-samples",
            json=sample,
            headers=HEADERS,
        )
        assert response.status_code == 201

    query = (
        "/api/owned-outcomes/summary?content_id=content-yt-001"
        "&campaign_id=campaign-launch-001&offer_id=offer-audit-build"
        "&source_platform=youtube&source_id=youtube-video-001"
    )
    response = client.get(query, headers=HEADERS)
    summary = response.get_json()

    assert response.status_code == 200
    assert summary["contract"] == "owned_outcome_summary_v1"
    assert summary["scope_precision"] == "exact"
    assert summary["funnel"]["stages"]["click"]["unique_journeys"] == 1
    assert (
        summary["funnel"]["transitions"]["click_to_install"]
        ["observed_link_rate"]
        == 1.0
    )
    assert summary["funnel"]["transitions"]["trial_to_purchase"]["causal_effect"] is None
    assert summary["funnel"]["complete_chain"] == {
        "required_sequence": ["click", "install", "trial", "purchase"],
        "complete_ordered_exact_scope_journeys": 1,
        "click_exact_scope_journeys": 1,
        "observed_complete_chain_rate": 1.0,
        "causal_effect": None,
    }
    assert summary["retention_curve"]["time_unit"] == "milliseconds"
    assert summary["retention_curve"]["fact_count"] == 4
    assert [
        point["elapsed_ms"] for point in summary["retention_curve"]["points"]
    ] == [0, 1000, 2500]
    assert summary["retention_curve"]["points"][1]["retained_percent"] == 70.0
    assert summary["observed_drop_facts"][0] == {
        "measurement_id": "curve-001",
        "journey_id": None,
        "attribution": {**ATTRIBUTION, "source_platform": "youtube"},
        "from_elapsed_ms": 0,
        "to_elapsed_ms": 1000,
        "drop_percentage_points": 20.0,
        "fact_type": "descriptive_observed_drop",
        "causal_reason": None,
    }
    assert summary["causal_drop_reasons"]["status"] == "refused"
    assert (
        summary["causal_drop_reasons"]["code"]
        == "DESCRIPTIVE_RETENTION_IS_NOT_CAUSAL_EVIDENCE"
    )
    assert summary["causal_drop_reasons"]["reasons"] == []
    assert summary["ai_interpretation"] == {
        "status": "not_generated",
        "epistemic_status": "interpretation_not_fact",
        "causal_claim": False,
        "note": (
            "Any future AI explanation must cite these event/sample facts and remain "
            "explicitly labelled as a hypothesis, not an observed cause."
        ),
    }

    events = client.get(
        "/api/owned-outcomes/events?content_id=content-yt-001&limit=10",
        headers=HEADERS,
    ).get_json()
    retained = client.get(
        "/api/owned-outcomes/retention-samples?content_id=content-yt-001&limit=10",
        headers=HEADERS,
    ).get_json()
    assert events["count"] == 4
    assert {item["event_type"] for item in events["events"]} == {
        "click", "install", "trial", "purchase",
    }
    assert retained["count"] == 4
    assert all("observed_at" in item for item in retained["samples"])
    assert all("elapsed_ms" in item for item in retained["samples"])

    database = app.extensions["content_quality_engine"].store.path
    with closing(sqlite3.connect(database)) as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM cq_owned_retention_samples"
        ).fetchone()[0] == 4
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            connection.execute(
                "UPDATE cq_owned_retention_samples SET retained_percent=99"
            )


def test_summary_refuses_causal_claims_without_curve(client):
    created = client.post(
        "/api/owned-outcomes/events",
        json=event_payload("only-click-001", "click", 1),
        headers=HEADERS,
    )
    assert created.status_code == 201

    response = client.get(
        "/api/owned-outcomes/summary?content_id=content-yt-001",
        headers=HEADERS,
    )
    summary = response.get_json()

    assert response.status_code == 200
    assert summary["retention_curve"]["status"] == "no_owned_samples"
    assert summary["retention_curve"]["points"] == []
    assert summary["observed_drop_facts"] == []
    assert summary["causal_drop_reasons"]["status"] == "refused"
    assert summary["causal_drop_reasons"]["code"] == "NO_RETENTION_SAMPLES"


def test_disjoint_measurement_cohorts_do_not_create_a_false_drop(client):
    first = retention_payload(
        "disjoint-m1-0", 0, 100, measurement_id="measurement-one"
    )
    second = retention_payload(
        "disjoint-m2-1000", 1000, 20, measurement_id="measurement-two"
    )
    for sample in (first, second):
        assert client.post(
            "/api/owned-outcomes/retention-samples",
            json=sample,
            headers=HEADERS,
        ).status_code == 201

    summary = client.get(
        "/api/owned-outcomes/summary?content_id=content-yt-001",
        headers=HEADERS,
    ).get_json()

    assert [point["elapsed_ms"] for point in summary["retention_curve"]["points"]] == [
        0, 1000,
    ]
    assert summary["observed_drop_facts"] == []
    assert {
        curve["measurement_id"]: [
            point["elapsed_ms"] for point in curve["points"]
        ]
        for curve in summary["retention_curve"]["measurement_curves"]
    } == {"measurement-one": [0], "measurement-two": [1000]}


def test_reused_measurement_id_across_attribution_scopes_cannot_create_drop(
    client,
):
    first = retention_payload(
        "reused-scope-a", 0, 100, measurement_id="reused-curve"
    )
    second = retention_payload(
        "reused-scope-b", 1000, 20, measurement_id="reused-curve"
    )
    second["attribution"] = {
        **ATTRIBUTION,
        "campaign_id": "campaign-launch-002",
    }
    for sample in (first, second):
        assert client.post(
            "/api/owned-outcomes/retention-samples",
            json=sample,
            headers=HEADERS,
        ).status_code == 201

    summary = client.get(
        "/api/owned-outcomes/summary?content_id=content-yt-001",
        headers=HEADERS,
    ).get_json()

    assert summary["observed_drop_facts"] == []
    assert summary["retention_curve"]["measurement_count"] == 2
    assert {
        point["measurement_count"]
        for point in summary["retention_curve"]["points"]
    } == {1}
    curves = summary["retention_curve"]["measurement_curves"]
    assert len(curves) == 2
    assert {curve["attribution"]["campaign_id"] for curve in curves} == {
        "campaign-launch-001", "campaign-launch-002",
    }
    assert all(len(curve["points"]) == 1 for curve in curves)


def test_aggregate_point_counts_full_scope_measurement_identity(client):
    first = retention_payload(
        "same-elapsed-scope-a", 0, 100, measurement_id="reused-curve"
    )
    second = retention_payload(
        "same-elapsed-scope-b", 0, 80, measurement_id="reused-curve"
    )
    second["attribution"] = {
        **ATTRIBUTION,
        "campaign_id": "campaign-launch-002",
    }
    for sample in (first, second):
        assert client.post(
            "/api/owned-outcomes/retention-samples",
            json=sample,
            headers=HEADERS,
        ).status_code == 201

    summary = client.get(
        "/api/owned-outcomes/summary?content_id=content-yt-001",
        headers=HEADERS,
    ).get_json()

    assert summary["retention_curve"]["measurement_count"] == 2
    assert summary["retention_curve"]["points"][0]["measurement_count"] == 2
    assert len(summary["retention_curve"]["measurement_curves"]) == 2


def test_complete_chain_rejects_reversed_and_disconnected_events(client):
    payloads = [
        event_payload("reversed-click", "click", 4),
        event_payload("reversed-install", "install", 3),
        {
            **event_payload("disconnected-trial", "trial", 5),
            "journey_id": "another-journey",
        },
        event_payload("reversed-purchase", "purchase", 6),
    ]
    for payload in payloads:
        assert client.post(
            "/api/owned-outcomes/events", json=payload, headers=HEADERS
        ).status_code == 201

    summary = client.get(
        "/api/owned-outcomes/summary?content_id=content-yt-001",
        headers=HEADERS,
    ).get_json()

    chain = summary["funnel"]["complete_chain"]
    assert chain["click_exact_scope_journeys"] == 1
    assert chain["complete_ordered_exact_scope_journeys"] == 0
    assert chain["observed_complete_chain_rate"] == 0.0


def test_retention_samples_are_idempotent_append_only_facts(app, client):
    payload = retention_payload("retention-replay", 1000, 80)
    created = client.post(
        "/api/owned-outcomes/retention-samples", json=payload, headers=HEADERS
    )
    replay = client.post(
        "/api/owned-outcomes/retention-samples", json=payload, headers=HEADERS
    )
    conflict = client.post(
        "/api/owned-outcomes/retention-samples",
        json={**payload, "retained_percent": 79},
        headers=HEADERS,
    )

    assert created.status_code == 201
    assert replay.status_code == 200
    assert replay.get_json()["created"] is False
    assert conflict.status_code == 409
    assert conflict.get_json()["code"] == "IDEMPOTENCY_KEY_CONFLICT"

    database = app.extensions["content_quality_engine"].store.path
    with closing(sqlite3.connect(database)) as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM cq_owned_retention_samples"
        ).fetchone()[0] == 1
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            connection.execute("DELETE FROM cq_owned_retention_samples")


def test_owned_retention_readiness_requires_complete_chain_and_same_measurement_curve(
    app, client
):
    store = app.extensions["content_quality_engine"].store
    assert store.owned_outcome_readiness()["status"] == "no_owned_outcomes"

    assert client.post(
        "/api/owned-outcomes/retention-samples",
        json=retention_payload("readiness-0", 0, 100),
        headers=HEADERS,
    ).status_code == 201
    assert store.owned_outcome_readiness()["status"] == "partial"

    for second, event_type in enumerate(
        ("click", "install", "trial", "purchase"), 1
    ):
        assert client.post(
            "/api/owned-outcomes/events",
            json=event_payload(f"readiness-{event_type}", event_type, second),
            headers=HEADERS,
        ).status_code == 201
    complete_chain_only = store.owned_outcome_readiness()
    assert complete_chain_only["status"] == "partial"
    assert complete_chain_only["complete_ordered_exact_scope_journey_count"] == 1
    assert complete_chain_only["same_measurement_retention_curve_count"] == 0

    assert client.post(
        "/api/owned-outcomes/retention-samples",
        json=retention_payload(
            "readiness-other-measurement-1000",
            1000,
            75,
            measurement_id="another-curve",
        ),
        headers=HEADERS,
    ).status_code == 201
    split_curve = store.owned_outcome_readiness()
    assert split_curve["status"] == "partial"
    assert split_curve["same_measurement_retention_curve_count"] == 0
    assert split_curve["linked_complete_chain_retention_curve_scope_count"] == 0

    assert client.post(
        "/api/owned-outcomes/retention-samples",
        json=retention_payload("readiness-1000", 1000, 75),
        headers=HEADERS,
    ).status_code == 201
    readiness = store.owned_outcome_readiness()
    assert readiness["status"] == "ready"
    assert readiness["complete_ordered_exact_scope_journey_count"] == 1
    assert readiness["same_measurement_retention_curve_count"] == 1
    assert readiness["linked_complete_chain_retention_curve_scope_count"] == 1
    assert readiness["linked_click_retention_curve_scope_count"] == 1


def test_journey_bound_retention_curve_links_only_to_its_complete_chain(
    app, client
):
    store = app.extensions["content_quality_engine"].store
    journey_a = "journey-anonymous-hash-001"
    journey_b = "journey-anonymous-hash-002"

    for second, event_type in enumerate(
        ("click", "install", "trial", "purchase"), 1
    ):
        assert client.post(
            "/api/owned-outcomes/events",
            json={
                **event_payload(f"journey-a-{event_type}", event_type, second),
                "journey_id": journey_a,
            },
            headers=HEADERS,
        ).status_code == 201

    for elapsed_ms, retained_percent in ((0, 100), (1000, 70)):
        sample = retention_payload(
            f"journey-b-curve-{elapsed_ms}",
            elapsed_ms,
            retained_percent,
            measurement_id="journey-bound-curve",
        )
        sample["journey_id"] = journey_b
        assert client.post(
            "/api/owned-outcomes/retention-samples",
            json=sample,
            headers=HEADERS,
        ).status_code == 201

    mismatched = store.owned_outcome_readiness()
    assert mismatched["status"] == "partial"
    assert mismatched["complete_ordered_exact_scope_journey_count"] == 1
    assert mismatched["same_measurement_retention_curve_count"] == 1
    assert mismatched["linked_complete_chain_retention_curve_scope_count"] == 0

    for elapsed_ms, retained_percent in ((0, 100), (1000, 75)):
        sample = retention_payload(
            f"journey-a-curve-{elapsed_ms}",
            elapsed_ms,
            retained_percent,
            measurement_id="journey-bound-curve",
        )
        sample["journey_id"] = journey_a
        assert client.post(
            "/api/owned-outcomes/retention-samples",
            json=sample,
            headers=HEADERS,
        ).status_code == 201

    matched = store.owned_outcome_readiness()
    assert matched["status"] == "ready"
    assert matched["same_measurement_retention_curve_count"] == 2
    assert matched["linked_complete_chain_retention_curve_scope_count"] == 1

    summary = client.get(
        "/api/owned-outcomes/summary?content_id=content-yt-001",
        headers=HEADERS,
    ).get_json()
    assert summary["retention_curve"]["measurement_count"] == 2
    assert {
        curve["journey_id"]
        for curve in summary["retention_curve"]["measurement_curves"]
    } == {journey_a, journey_b}


def test_null_journey_retention_curve_remains_aggregate_scope(app, client):
    store = app.extensions["content_quality_engine"].store
    for second, event_type in enumerate(
        ("click", "install", "trial", "purchase"), 1
    ):
        assert client.post(
            "/api/owned-outcomes/events",
            json=event_payload(f"aggregate-{event_type}", event_type, second),
            headers=HEADERS,
        ).status_code == 201

    for elapsed_ms, retained_percent in ((0, 100), (1000, 72)):
        assert client.post(
            "/api/owned-outcomes/retention-samples",
            json=retention_payload(
                f"aggregate-curve-{elapsed_ms}",
                elapsed_ms,
                retained_percent,
                measurement_id="aggregate-curve",
            ),
            headers=HEADERS,
        ).status_code == 201

    readiness = store.owned_outcome_readiness()
    assert readiness["status"] == "ready"
    assert readiness["linked_complete_chain_retention_curve_scope_count"] == 1

    summary = client.get(
        "/api/owned-outcomes/summary?content_id=content-yt-001",
        headers=HEADERS,
    ).get_json()
    curves = summary["retention_curve"]["measurement_curves"]
    assert len(curves) == 1
    assert curves[0]["journey_id"] is None


def test_owned_retention_readiness_rejects_mixed_journey_and_scope_chain(
    app, client
):
    for elapsed_ms, retained_percent in ((0, 100), (1000, 70)):
        assert client.post(
            "/api/owned-outcomes/retention-samples",
            json=retention_payload(
                f"mixed-chain-curve-{elapsed_ms}", elapsed_ms, retained_percent
            ),
            headers=HEADERS,
        ).status_code == 201

    payloads = [
        event_payload("mixed-chain-click", "click", 1),
        event_payload("mixed-chain-install", "install", 2),
        {
            **event_payload("mixed-chain-trial", "trial", 3),
            "journey_id": "different-journey",
        },
        {
            **event_payload("mixed-chain-purchase", "purchase", 4),
            "attribution": {**ATTRIBUTION, "source_id": "different-source"},
        },
    ]
    for payload in payloads:
        assert client.post(
            "/api/owned-outcomes/events", json=payload, headers=HEADERS
        ).status_code == 201

    readiness = app.extensions[
        "content_quality_engine"
    ].store.owned_outcome_readiness()
    assert readiness["status"] == "partial"
    assert readiness["complete_ordered_exact_scope_journey_count"] == 0
    assert readiness["same_measurement_retention_curve_count"] == 1
    assert readiness["linked_complete_chain_retention_curve_scope_count"] == 0


IDENTIFIER_CASES = (
    ("events", "idempotency_key", False),
    ("events", "journey_id", False),
    ("events", "provider_event_id", False),
    ("events", "content_id", True),
    ("events", "campaign_id", True),
    ("events", "offer_id", True),
    ("events", "source_platform", True),
    ("events", "source_id", True),
    ("retention-samples", "idempotency_key", False),
    ("retention-samples", "measurement_id", False),
    ("retention-samples", "journey_id", False),
    ("retention-samples", "content_id", True),
    ("retention-samples", "campaign_id", True),
    ("retention-samples", "offer_id", True),
    ("retention-samples", "source_platform", True),
    ("retention-samples", "source_id", True),
)


@pytest.mark.parametrize("bad_value", (True, 17, {"nested": "id"}, ["id"]))
@pytest.mark.parametrize(
    ("route", "field", "nested_attribution"), IDENTIFIER_CASES
)
def test_owned_outcome_identifiers_reject_non_string_json_types(
    app, client, route, field, nested_attribution, bad_value
):
    if route == "events":
        payload = event_payload("strict-event-id", "click", 1)
    else:
        payload = retention_payload("strict-retention-id", 0, 100)
    if nested_attribution:
        payload["attribution"] = {
            **payload["attribution"],
            field: bad_value,
        }
    else:
        payload[field] = bad_value

    response = client.post(
        f"/api/owned-outcomes/{route}", json=payload, headers=HEADERS
    )

    assert response.status_code == 400
    result = response.get_json()
    assert result["code"] == "INVALID_REQUEST"
    assert result["error"] == f"{field} must be a string"
    database = app.extensions["content_quality_engine"].store.path
    table = (
        "cq_owned_outcome_events"
        if route == "events"
        else "cq_owned_retention_samples"
    )
    with closing(sqlite3.connect(database)) as connection:
        assert connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] == 0


@pytest.mark.parametrize(
    ("method", "path", "payload", "message", "operation"),
    (
        (
            "post",
            "/api/owned-outcomes/events",
            {**event_payload("bad-attribution", "click", 1), "attribution": {}},
            "content_id is required",
            "ingest_owned_outcome_event",
        ),
        (
            "post",
            "/api/owned-outcomes/events",
            {
                **event_payload("bad-time", "click", 1),
                "occurred_at": "2026-08-24T12:00:00",
            },
            "occurred_at must include a timezone",
            "ingest_owned_outcome_event",
        ),
        (
            "post",
            "/api/owned-outcomes/retention-samples",
            retention_payload("bad-elapsed", -1, 90),
            "elapsed_ms must be at least 0",
            "ingest_owned_retention_sample",
        ),
        (
            "post",
            "/api/owned-outcomes/retention-samples",
            retention_payload("bad-retained", 1000, 101),
            "retained_percent must be a number from 0 to 100",
            "ingest_owned_retention_sample",
        ),
        (
            "get",
            "/api/owned-outcomes/events",
            None,
            "content_id is required",
            "list_owned_outcome_events",
        ),
        (
            "get",
            "/api/owned-outcomes/retention-samples",
            None,
            "content_id is required",
            "list_owned_retention_samples",
        ),
        (
            "get",
            "/api/owned-outcomes/summary",
            None,
            "content_id is required",
            "summarize_owned_outcomes",
        ),
    ),
)
def test_owned_outcome_contract_validation(
    app, client, method, path, payload, message, operation
):
    response = getattr(client, method)(path, json=payload, headers=HEADERS)
    result = response.get_json()

    assert response.status_code == 400
    assert result["code"] == "INVALID_REQUEST"
    assert result["error"] == message
    assert result["agent_query"]["query_id"].startswith("agentq_")
    assert len(result["agent_query"]["response_sha256"]) == 64

    database = app.extensions["content_quality_engine"].store.path
    with closing(sqlite3.connect(database)) as connection:
        row = connection.execute(
            """
            SELECT operation, parameters_sha256, response_sha256, outcome
            FROM cq_agent_queries
            """
        ).fetchone()
    assert row[0] == operation
    assert len(row[1]) == 64
    assert len(row[2]) == 64
    assert row[3] == "rejected"
