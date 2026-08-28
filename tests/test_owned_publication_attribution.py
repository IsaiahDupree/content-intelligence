from __future__ import annotations

import hashlib
import json
import sqlite3
from contextlib import closing

import pytest

from services.content_quality.api import create_content_quality_app


TOKEN = "owned-publication-test-token"
HEADERS = {
    "Authorization": f"Bearer {TOKEN}",
    "X-Agent-Principal": "owned-publication-integration-test",
}
CONTENT_ID = "reference-grounded-bottleneck"
ASSET_ID = "asset-reference-grounded-bottleneck"
SEMANTIC_ASSET_SHA256 = "a" * 64
PLATFORM_POST_ID = "youtube-native-post-001"
PLATFORM_POST_URL = (
    "https://www.youtube.com/watch?v=youtube-native-post-001"
)


def canonical_sha256(value: dict) -> str:
    encoded = json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=True
    )
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


@pytest.fixture()
def app(tmp_path):
    tape_path = tmp_path / "market-tape.sqlite3"
    with closing(sqlite3.connect(tape_path)) as connection:
        connection.execute(
            """
            CREATE TABLE mt_content_assets (
                asset_id TEXT NOT NULL,
                brief_id TEXT NOT NULL,
                graph_version_id TEXT NOT NULL,
                atomic_topic_id TEXT NOT NULL,
                parent_asset_id TEXT,
                platform TEXT,
                account TEXT,
                content_id TEXT NOT NULL,
                asset_contract TEXT NOT NULL,
                asset_sha256 TEXT NOT NULL,
                status TEXT NOT NULL,
                lineage_sha256 TEXT NOT NULL,
                source_service TEXT NOT NULL,
                source_receipt_id TEXT NOT NULL,
                registered_at TEXT NOT NULL
            )
            """
        )
        connection.execute(
            """
            INSERT INTO mt_content_assets(
                asset_id, brief_id, graph_version_id, atomic_topic_id,
                parent_asset_id, platform, account, content_id,
                asset_contract, asset_sha256, status, lineage_sha256,
                source_service, source_receipt_id, registered_at
            ) VALUES (?, ?, ?, ?, NULL, NULL, NULL, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                ASSET_ID,
                "brief-bottleneck",
                "graph-version-001",
                "atomic-topic-bottleneck",
                CONTENT_ID,
                "semantic_content_asset_v1",
                SEMANTIC_ASSET_SHA256,
                "approved",
                "b" * 64,
                "marketing-video-foundry",
                "semantic-receipt-001",
                "2026-08-28T12:00:00+00:00",
            ),
        )
        connection.commit()

    asset_path = tmp_path / "reference-grounded-bottleneck.mp4"
    asset_path.write_bytes(b"real integration fixture video bytes")
    application = create_content_quality_app(
        {
            "TESTING": True,
            "NARRATIVE_COHERENCE_LLM": "off",
            "MARKET_TAPE_DB": tape_path,
            "CONTENT_QUALITY_DB": tmp_path / "content-quality.sqlite3",
            "CONTENT_QUALITY_CONTROL_TOKEN": TOKEN,
        }
    )
    application.config["TEST_ASSET_PATH"] = asset_path
    return application


@pytest.fixture()
def client(app):
    return app.test_client()


def publication_payload(app, **overrides) -> dict:
    asset_path = app.config["TEST_ASSET_PATH"]
    asset_sha256 = hashlib.sha256(asset_path.read_bytes()).hexdigest()
    provider_receipt = {
        "receipt_id": "publisher-receipt-001",
        "status": "published",
        "platform_post_id": PLATFORM_POST_ID,
        "public_url": PLATFORM_POST_URL,
    }
    payload = {
        "idempotency_key": "publish-youtube-main-bottleneck-v1",
        "content_id": CONTENT_ID,
        "campaign_id": "campaign-reference-grounded-week",
        "offer_id": "offer-ai-automation-audit",
        "local_asset_path": str(asset_path),
        "local_asset_sha256": asset_sha256,
        "source_platform": "youtube_shorts",
        "account_id": "youtube-main",
        "publisher": "blotato",
        "provider_post_id": PLATFORM_POST_ID,
        "provider_post_url": PLATFORM_POST_URL,
        "published_at": "2026-08-28T12:30:00-04:00",
        "provider_receipt_id": "publisher-receipt-001",
        "provider_receipt_sha256": canonical_sha256(provider_receipt),
        "provider_receipt": provider_receipt,
        "metadata": {
            "job_id": "hg-ref-20260825-bottleneck",
            "owner_bundle_id": "bundle-bottleneck-001",
        },
    }
    payload.update(overrides)
    return payload


def register(client, app, **overrides):
    return client.post(
        "/api/v2/owned-publications",
        json=publication_payload(app, **overrides),
        headers=HEADERS,
    )


def strict_attribution() -> dict:
    return {
        "content_id": CONTENT_ID,
        "campaign_id": "campaign-reference-grounded-week",
        "offer_id": "offer-ai-automation-audit",
        "source_platform": "youtube",
        "source_id": PLATFORM_POST_ID,
    }


def strict_binding(publication: dict) -> dict:
    return {
        "publication_id": publication["publication_id"],
        "publication_receipt_sha256": publication[
            "publication_receipt_sha256"
        ],
    }


def test_registration_is_terminal_exact_idempotent_and_append_only(app, client):
    created = register(client, app)
    replay = register(client, app)

    assert created.status_code == 201
    assert replay.status_code == 200
    body = created.get_json()
    publication = body["publication"]
    assert body["created"] is True
    assert replay.get_json()["created"] is False
    assert publication["contract"] == "owned_publication_receipt_v1"
    assert publication["attribution"] == strict_attribution()
    assert publication["semantic_asset"] == {
        "asset_id": ASSET_ID,
        "asset_sha256": SEMANTIC_ASSET_SHA256,
    }
    assert publication["local_asset"]["sha256"] == publication_payload(app)[
        "local_asset_sha256"
    ]
    assert publication["provider_post_url"] == PLATFORM_POST_URL

    changed_receipt = dict(publication_payload(app)["provider_receipt"])
    changed_receipt["delivery_region"] = "us-east"
    conflict = register(
        client,
        app,
        provider_receipt=changed_receipt,
        provider_receipt_sha256=canonical_sha256(changed_receipt),
    )
    assert conflict.status_code == 409
    assert conflict.get_json()["code"] == "IDEMPOTENCY_KEY_CONFLICT"

    database = app.extensions["content_quality_engine"].store.path
    with closing(sqlite3.connect(database)) as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM cq_owned_publication_receipts"
        ).fetchone()[0] == 1
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            connection.execute(
                "UPDATE cq_owned_publication_receipts SET publisher='other'"
            )
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            connection.execute("DELETE FROM cq_owned_publication_receipts")


def test_registration_rejects_submission_receipt_and_wrong_asset_hash(app, client):
    pending_receipt = {
        "receipt_id": "publisher-receipt-001",
        "status": "submitted",
        "platform_post_id": PLATFORM_POST_ID,
        "public_url": PLATFORM_POST_URL,
    }
    pending = register(
        client,
        app,
        provider_receipt=pending_receipt,
        provider_receipt_sha256=canonical_sha256(pending_receipt),
    )
    wrong_hash = register(client, app, local_asset_sha256="f" * 64)

    assert pending.status_code == 400
    assert "non-terminal" in pending.get_json()["error"]
    assert wrong_hash.status_code == 400
    assert "does not match" in wrong_hash.get_json()["error"]


def test_strict_outcomes_require_registered_exact_publication_binding(app, client):
    missing = client.post(
        "/api/v2/owned-outcomes/events",
        json={
            "publication_id": "missing-publication",
            "publication_receipt_sha256": "c" * 64,
            "idempotency_key": "owned-click-before-registration",
            "event_type": "click",
            "attribution": strict_attribution(),
            "journey_id": "journey-001",
            "occurred_at": "2026-08-28T17:00:00+00:00",
        },
        headers=HEADERS,
    )
    assert missing.status_code == 400

    publication = register(client, app).get_json()["publication"]
    readiness_url = (
        f"/api/v2/owned-publications/{publication['publication_id']}/readiness"
        f"?publication_receipt_sha256={publication['publication_receipt_sha256']}"
    )
    before = client.get(readiness_url, headers=HEADERS)
    assert before.status_code == 200
    assert before.get_json()["status"] == "awaiting_owned_evidence"

    mismatched = strict_attribution()
    mismatched["source_id"] = "different-video"
    rejected = client.post(
        "/api/v2/owned-outcomes/events",
        json={
            **strict_binding(publication),
            "idempotency_key": "owned-click-wrong-source",
            "event_type": "click",
            "attribution": mismatched,
            "journey_id": "journey-001",
            "occurred_at": "2026-08-28T17:00:00+00:00",
        },
        headers=HEADERS,
    )
    assert rejected.status_code == 400
    assert "does not match" in rejected.get_json()["error"]

    click_payload = {
        **strict_binding(publication),
        "idempotency_key": "owned-click-001",
        "event_type": "click",
        "attribution": strict_attribution(),
        "journey_id": "journey-001",
        "occurred_at": "2026-08-28T17:00:00+00:00",
        "provider_event_id": "posthog-click-001",
    }
    created = client.post(
        "/api/v2/owned-outcomes/events", json=click_payload, headers=HEADERS
    )
    replay = client.post(
        "/api/v2/owned-outcomes/events", json=click_payload, headers=HEADERS
    )
    assert created.status_code == 201
    assert replay.status_code == 200
    assert created.get_json()["event"]["contract"] == "owned_attribution_event_v2"
    assert created.get_json()["event"]["publication_binding"] == {
        "contract": "owned_publication_binding_v1",
        **strict_binding(publication),
    }

    after = client.get(readiness_url, headers=HEADERS).get_json()
    assert after["status"] == "owned_evidence_observed"
    assert after["strictly_bound_event_count"] == 1
    assert after["event_stage_counts"]["click"] == 1
    assert after["first_owned_evidence_observed"] is True
    assert after["causal_claim"] is False


def test_strict_retention_is_bound_and_readiness_never_invents_cause(app, client):
    publication = register(client, app).get_json()["publication"]
    payload = {
        **strict_binding(publication),
        "idempotency_key": "owned-retention-001",
        "attribution": strict_attribution(),
        "measurement_id": "youtube-retention-curve-001",
        "observed_at": "2026-08-28T18:00:00+00:00",
        "elapsed_ms": 1_000,
        "retained_percent": 72.5,
        "sample_size": 200,
    }
    created = client.post(
        "/api/v2/owned-outcomes/retention-samples",
        json=payload,
        headers=HEADERS,
    )

    assert created.status_code == 201
    sample = created.get_json()["sample"]
    assert sample["contract"] == "owned_retention_sample_v2"
    assert sample["publication_binding"] == {
        "contract": "owned_publication_binding_v1",
        **strict_binding(publication),
    }
    readiness = client.get(
        f"/api/v2/owned-publications/{publication['publication_id']}/readiness"
        f"?publication_receipt_sha256={publication['publication_receipt_sha256']}",
        headers=HEADERS,
    ).get_json()
    assert readiness["strictly_bound_retention_sample_count"] == 1
    assert readiness["outcome_claim"] == "observed_first_party_facts_only"
    assert readiness["causal_claim"] is False


def test_publication_routes_require_agent_auth(app, client):
    registration = client.post(
        "/api/v2/owned-publications", json=publication_payload(app)
    )
    strict_event = client.post(
        "/api/v2/owned-outcomes/events", json={}
    )

    assert registration.status_code == 401
    assert strict_event.status_code == 401
