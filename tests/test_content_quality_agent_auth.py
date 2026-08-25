from __future__ import annotations

import sqlite3
from contextlib import closing

import pytest

from services.content_quality.api import create_content_quality_app


CONTROL_TOKEN = "content-quality-agent-test-token"
AGENT_ENDPOINTS = (
    ("GET", "/api/agent/catalog", None),
    ("POST", "/api/script-intelligence/briefs", {"audience": "founders"}),
    ("GET", "/api/script-intelligence/briefs", None),
    ("GET", "/api/script-intelligence/briefs/missing-brief", None),
    ("GET", "/api/script-intelligence/scripts/missing-script", None),
    (
        "POST",
        "/api/script-intelligence/generate-and-audit",
        {"brief_id": "missing-brief"},
    ),
    (
        "POST",
        "/api/script-intelligence/run",
        {"topic": "AI automation", "audience": "software founders"},
    ),
)


def _production_app(tmp_path, *, token=CONTROL_TOKEN):
    return create_content_quality_app(
        {
            "TESTING": False,
            "NARRATIVE_COHERENCE_LLM": "off",
            "MARKET_TAPE_DB": tmp_path / "market-tape.sqlite3",
            "CONTENT_QUALITY_DB": tmp_path / "content-quality.sqlite3",
            "CONTENT_QUALITY_CONTROL_TOKEN": token,
        }
    )


@pytest.mark.parametrize(("method", "path", "payload"), AGENT_ENDPOINTS)
def test_agent_endpoints_require_bearer_auth_outside_testing(
    tmp_path, method, path, payload
):
    client = _production_app(tmp_path).test_client()

    missing = client.open(path, method=method, json=payload)
    invalid = client.open(
        path,
        method=method,
        json=payload,
        headers={"Authorization": "Bearer incorrect-token"},
    )

    assert missing.status_code == 401
    assert missing.get_json()["code"] == "UNAUTHORIZED"
    assert invalid.status_code == 401
    assert invalid.get_json()["code"] == "UNAUTHORIZED"


def test_agent_gateway_without_a_configured_token_fails_closed(tmp_path, monkeypatch):
    monkeypatch.delenv("CONTENT_QUALITY_CONTROL_TOKEN", raising=False)
    client = _production_app(tmp_path, token="").test_client()

    response = client.get("/api/agent/catalog")

    assert response.status_code == 503
    assert response.get_json()["code"] == "AGENT_GATEWAY_NOT_CONFIGURED"


def test_successful_agent_reads_append_hash_only_query_audits(tmp_path):
    app = _production_app(tmp_path)
    client = app.test_client()
    headers = {
        "Authorization": f"Bearer {CONTROL_TOKEN}",
        "X-Agent-Principal": "research agent/one",
    }

    catalog = client.get("/api/agent/catalog", headers=headers)
    briefs = client.get(
        "/api/script-intelligence/briefs?limit=7", headers=headers
    )

    assert catalog.status_code == 200
    assert briefs.status_code == 200
    assert len(catalog.get_json()["agent_query"]["response_sha256"]) == 64
    assert len(briefs.get_json()["agent_query"]["response_sha256"]) == 64

    database_path = app.extensions["content_quality_engine"].store.path
    with closing(sqlite3.connect(database_path)) as connection:
        rows = connection.execute(
            """
            SELECT principal, operation, parameters_sha256, response_sha256,
                   outcome, row_count
            FROM cq_agent_queries
            ORDER BY created_at, operation
            """
        ).fetchall()

    assert len(rows) == 2
    assert {row[1] for row in rows} == {"catalog", "list_script_briefs"}
    assert {row[0] for row in rows} == {"research-agent-one"}
    assert {row[4] for row in rows} == {"success"}
    assert all(len(row[2]) == 64 and len(row[3]) == 64 for row in rows)
    assert all(CONTROL_TOKEN not in str(row) for row in rows)
