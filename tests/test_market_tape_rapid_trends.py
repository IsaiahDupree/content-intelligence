"""Real-SQLite contracts for provider-free rapid-trend triggering."""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta, timezone

import pytest
from flask import Flask

from services.market_tape.api import register_market_tape_routes
from services.market_tape.config import MarketTapeConfig
from services.market_tape.models import isoformat, stable_hash
from services.market_tape.rapid_trend import (
    RAPID_TREND_SCRIPT_RESPONSE_CONTRACT,
    RAPID_TREND_TRIGGER_CONTRACT,
    RapidTrendTriggerService,
)
from services.market_tape.semantic import (
    GRAPH_IMPORT_CONTRACT,
    TOPIC_LEVELS,
    TOPIC_RELATIONSHIPS,
    SemanticTopicService,
)
from services.market_tape.sinks.supabase import (
    ENTITY_SYNC_ORDER,
    ENTITY_TABLES,
    _required_parent_entities,
)
from services.market_tape.store import (
    OBSERVATION_QUALITY_CONTRACT,
    SCHEMA_VERSION,
    TREND_INDEX_VERSION,
    MarketTapeStore,
)


TOKEN = "rapid-trend-test-token"


def _config(tmp_path) -> MarketTapeConfig:
    return MarketTapeConfig(
        db_path=tmp_path / "market.sqlite3",
        object_dir=tmp_path / "objects",
        heartbeat_path=tmp_path / "heartbeat.json",
        lock_path=tmp_path / "market.lock",
        local_research_state_path=tmp_path / "local-state.json",
        prediction_model_dir=tmp_path / "models",
        supabase_sync_enabled=False,
        rapid_trend_max_per_cycle=3,
        rapid_trend_max_per_day=10,
    )


def _graph() -> dict:
    nodes = []
    relationships = []
    parent_id = None
    for index, level in enumerate(TOPIC_LEVELS):
        topic_id = f"{level}.rapid-trend-test"
        nodes.append({
            "id": topic_id,
            "name": f"Rapid trend {level.replace('_', ' ')}",
            "definition": f"Canonical {level} used by rapid trend tests.",
            "level": level,
            "canonical_parent_id": parent_id,
            "aliases": [f"rapid {level}"],
            "status": "active",
            "strategic_priority": 100 - index,
        })
        if parent_id is not None:
            relationships.append({
                "source_topic_id": topic_id,
                "target_topic_id": parent_id,
                "relationship_type": "part_of",
            })
        parent_id = topic_id
    core = {
        "schema_version": "2.0",
        "contract_type": "content_topic_graph_v2",
        "levels": list(TOPIC_LEVELS),
        "relationship_types": list(TOPIC_RELATIONSHIPS),
        "nodes": nodes,
        "relationships": relationships,
        "metadata": {"graph_version": "rapid-trend-test-v1"},
        "migration": {},
        "inventory": {
            "node_count": len(nodes),
            "relationship_count": len(relationships),
            "by_level": {level: 1 for level in TOPIC_LEVELS},
        },
    }
    return {**core, "graph_sha256": stable_hash(core)}


def _import_graph(store: MarketTapeStore, imported_at: datetime) -> dict:
    return SemanticTopicService(store).import_graph({
        "contract": GRAPH_IMPORT_CONTRACT,
        "source_service": "rapid-trend-tests",
        "source_receipt_id": "rapid-trend-test-graph-receipt",
        "imported_by": "reviewer.rapid-trend-test",
        "imported_at": isoformat(imported_at),
        "graph": _graph(),
    })


def _insert_trend(store: MarketTapeStore, trend_id: str, display_name: str) -> None:
    with store.connect() as connection:
        connection.execute(
            """INSERT INTO mt_trends(
                   trend_id, trend_type, canonical_key, display_name,
                   status, first_seen_at, last_seen_at
               ) VALUES(?, 'topic', ?, ?, 'recurring', ?, ?)""",
            (
                trend_id,
                display_name.casefold().replace(" ", "-"),
                display_name,
                "2026-01-01T00:00:00+00:00",
                "2026-01-01T00:00:00+00:00",
            ),
        )


def _observation(
    store: MarketTapeStore,
    trend_id: str,
    observed_at: datetime,
    *,
    state: str,
    quality: str = OBSERVATION_QUALITY_CONTRACT,
    index_version: str = TREND_INDEX_VERSION,
    platforms_total: int = 2,
    top1_concentration: float = 0.50,
    activity_coverage: float = 0.75,
    views_new_1h: int = 5000,
    counter_delta_videos: int = 5,
) -> int:
    breakout = state == "breakout"
    values = {
        "trend_id": trend_id,
        "observed_at": isoformat(observed_at),
        "videos_total": 12 if breakout else 8,
        "videos_new_1h": 4 if breakout else 1,
        "creators_total": 12 if breakout else 8,
        "creators_new_1h": 4 if breakout else 1,
        "platforms_total": platforms_total,
        "views_total": 200000 if breakout else 100000,
        "likes_total": 10000,
        "comments_total": 1000,
        "shares_total": 500,
        "views_new_1h": views_new_1h,
        "likes_new_1h": 500,
        "comments_new_1h": 50,
        "shares_new_1h": 25,
        "counter_delta_videos": counter_delta_videos,
        "activity_coverage": activity_coverage,
        "median_video_velocity": 1.5 if breakout else 0.5,
        "p90_video_velocity": 2.0 if breakout else 0.75,
        "creator_breadth": 0.8,
        "platform_breadth": 0.5,
        "top1_concentration": top1_concentration,
        "top10_concentration": 0.95,
        "momentum": 2.0 if breakout else 0.75,
        "acceleration": 0.8 if breakout else -0.1,
        "relative_strength": 3.5 if breakout else 1.0,
        "saturation": 0.12,
        "trend_strength": 82.0 if breakout else 40.0,
        "index_version": index_version,
        "observation_quality_contract": quality,
        "state": state,
    }
    columns = list(values)
    with store.connect() as connection:
        cursor = connection.execute(
            f"INSERT INTO mt_trend_observations({','.join(columns)}) "
            f"VALUES({','.join('?' for _ in columns)})",
            [values[column] for column in columns],
        )
        return int(cursor.lastrowid)


def _seed_crossing(
    store: MarketTapeStore,
    now: datetime,
    *,
    trend_id: str = "trend:topic:ai-agent-kill-switch",
    display_name: str = "AI agent kill switches",
    baseline_kwargs: dict | None = None,
    current_kwargs: dict | None = None,
) -> tuple[int, int]:
    _insert_trend(store, trend_id, display_name)
    baseline = _observation(
        store,
        trend_id,
        now - timedelta(minutes=35),
        state="recurring",
        **(baseline_kwargs or {}),
    )
    current = _observation(
        store,
        trend_id,
        now - timedelta(minutes=5),
        state="breakout",
        **(current_kwargs or {}),
    )
    return baseline, current


def test_breakout_crossing_creates_one_trigger_signal_and_evidence_demand(
    tmp_path,
):
    now = datetime.now(timezone.utc).replace(microsecond=0)
    store = MarketTapeStore(_config(tmp_path))
    _import_graph(store, now - timedelta(hours=1))
    baseline_id, current_id = _seed_crossing(store, now)
    service = RapidTrendTriggerService(store.config, store)

    first = service.evaluate(source_run_id="rapid-cycle-1", as_of=now)
    replay = service.evaluate(source_run_id="rapid-cycle-1", as_of=now)

    assert first["provider_calls_made"] == 0
    assert first["examined"] == 1
    assert first["eligible"] == 1
    assert first["created"] == 1
    assert replay["created"] == 0
    assert replay["idempotent"] == 1
    trigger = first["triggers"][0]
    assert trigger["contract"] == RAPID_TREND_TRIGGER_CONTRACT
    assert trigger["baseline_trend_observation_id"] == baseline_id
    assert trigger["trigger_trend_observation_id"] == current_id
    assert trigger["state"] == "context_acquisition_queued"
    assert trigger["semantic_signal"]["signal_id"].startswith("topic-signal:")
    assert trigger["evidence_demand"]["demand_id"].startswith(
        "script-language-demand:"
    )
    assert [event["event_type"] for event in trigger["events"]] == [
        "detected",
        "semantic_materialized",
        "evidence_demand_enqueued",
    ]
    assert trigger["evidence"]["raw_source_content_included"] is False
    assert trigger["evidence"]["generation_authorized"] is False
    immutable_core = {
        "contract": trigger["contract"],
        "policy_sha256": trigger["policy_sha256"],
        "trend_id": trigger["trend_id"],
        "baseline_trend_observation_id": baseline_id,
        "trigger_trend_observation_id": current_id,
        "trigger_id": trigger["trigger_id"],
        "policy_version": trigger["policy_version"],
        "source_run_id": trigger["source_run_id"],
        "source_receipt_id": trigger["source_receipt_id"],
        "evidence_sha256": trigger["evidence_sha256"],
        "evidence": trigger["evidence"],
        "detected_at": trigger["detected_at"],
        "expires_at": trigger["expires_at"],
    }
    assert stable_hash(immutable_core) == trigger["trigger_sha256"]
    with store.connect() as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM mt_topic_signal_candidates"
        ).fetchone()[0] == 1
        assert connection.execute(
            """SELECT COUNT(*) FROM mt_script_language_demand_events
               WHERE event_type = 'requested'"""
        ).fetchone()[0] == 1
        assert connection.execute(
            "SELECT COUNT(*) FROM mt_rapid_trend_triggers"
        ).fetchone()[0] == 1


def test_trigger_fails_closed_and_exposes_all_current_evidence_reasons(tmp_path):
    now = datetime.now(timezone.utc).replace(microsecond=0)
    store = MarketTapeStore(_config(tmp_path))
    _seed_crossing(
        store,
        now,
        trend_id="trend:topic:concentrated-breakout",
        display_name="Concentrated AI automation surge",
        current_kwargs={
            "platforms_total": 1,
            "top1_concentration": 0.91,
            "activity_coverage": 0.20,
            "views_new_1h": 100,
            "counter_delta_videos": 1,
        },
    )
    _seed_crossing(
        store,
        now,
        trend_id="trend:topic:legacy-breakout",
        display_name="Legacy AI workflow spike",
        current_kwargs={"quality": "legacy_unverified"},
    )

    result = RapidTrendTriggerService(store.config, store).evaluate(
        source_run_id="rapid-fail-closed", as_of=now
    )

    assert result["created"] == 0
    assert result["eligible"] == 0
    assert result["examined"] == 2
    reasons = result["suppressed_by_reason"]
    assert reasons["insufficient_cross_platform_evidence"] == 1
    assert reasons["excessive_top_creator_concentration"] == 1
    assert reasons["insufficient_activity_coverage"] == 1
    assert reasons["insufficient_recent_views"] == 1
    assert reasons["insufficient_measured_activity"] == 1
    assert reasons["unaccepted_observation_quality"] == 1
    assert store.status()["totals"]["rapid_trend_triggers"] == 0


def test_unaccepted_baseline_and_continuing_breakout_never_retrigger(tmp_path):
    now = datetime.now(timezone.utc).replace(microsecond=0)
    store = MarketTapeStore(_config(tmp_path))
    _seed_crossing(
        store,
        now,
        trend_id="trend:topic:unaccepted-baseline",
        display_name="AI coding agent demand",
        baseline_kwargs={"quality": "legacy_unverified"},
    )
    _insert_trend(
        store,
        "trend:topic:continuing-breakout",
        "AI agent observability tools",
    )
    _observation(
        store,
        "trend:topic:continuing-breakout",
        now - timedelta(minutes=35),
        state="breakout",
    )
    _observation(
        store,
        "trend:topic:continuing-breakout",
        now - timedelta(minutes=5),
        state="breakout",
    )

    result = RapidTrendTriggerService(store.config, store).evaluate(
        source_run_id="rapid-no-retrigger", as_of=now
    )

    assert result["created"] == 0
    assert result["suppressed_by_reason"] == {
        "already_breakout": 1,
        "baseline_observation_not_accepted": 1,
    }


def test_trigger_and_event_tables_are_append_only(tmp_path):
    now = datetime.now(timezone.utc).replace(microsecond=0)
    store = MarketTapeStore(_config(tmp_path))
    _import_graph(store, now - timedelta(hours=1))
    _seed_crossing(store, now)
    trigger = RapidTrendTriggerService(store.config, store).evaluate(
        source_run_id="rapid-append-only", as_of=now
    )["triggers"][0]

    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        with store.connect() as connection:
            connection.execute(
                "UPDATE mt_rapid_trend_triggers SET source_run_id='changed'"
            )
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        with store.connect() as connection:
            connection.execute(
                "DELETE FROM mt_rapid_trend_trigger_events WHERE trigger_id = ?",
                (trigger["trigger_id"],),
            )


def test_trigger_and_events_enqueue_portable_dependency_ordered_outbox(
    tmp_path,
):
    now = datetime.now(timezone.utc).replace(microsecond=0)
    store = MarketTapeStore(_config(tmp_path))
    _import_graph(store, now - timedelta(hours=1))
    _seed_crossing(store, now)
    trigger = RapidTrendTriggerService(store.config, store).evaluate(
        source_run_id="rapid-outbox", as_of=now
    )["triggers"][0]

    with store.connect() as connection:
        rows = connection.execute(
            """SELECT entity_type, entity_key, payload_json
               FROM mt_sync_outbox
               WHERE entity_type IN (
                   'rapid_trend_trigger', 'rapid_trend_trigger_event'
               )
               ORDER BY outbox_id"""
        ).fetchall()
    assert [row["entity_type"] for row in rows] == [
        "rapid_trend_trigger",
        "rapid_trend_trigger_event",
        "rapid_trend_trigger_event",
        "rapid_trend_trigger_event",
    ]
    trigger_payload = json.loads(rows[0]["payload_json"])
    assert trigger_payload["trigger_id"] == trigger["trigger_id"]
    assert "baseline_trend_observation_id" not in trigger_payload
    assert "trigger_trend_observation_id" not in trigger_payload
    assert trigger_payload["baseline_trend_observation_key"] == stable_hash({
        "trend_id": trigger["trend_id"],
        "observed_at": trigger["evidence"]["baseline_observation"][
            "observed_at"
        ],
    })
    assert trigger_payload["trigger_trend_observation_key"] == stable_hash({
        "trend_id": trigger["trend_id"],
        "observed_at": trigger["evidence"]["trigger_observation"][
            "observed_at"
        ],
    })
    assert ENTITY_TABLES["rapid_trend_trigger"] == (
        "actp_market_rapid_trend_triggers",
        "trigger_id",
        False,
    )
    assert ENTITY_TABLES["rapid_trend_trigger_event"] == (
        "actp_market_rapid_trend_trigger_events",
        "event_id",
        False,
    )
    assert ENTITY_SYNC_ORDER.index("trend_observation") < (
        ENTITY_SYNC_ORDER.index("rapid_trend_trigger")
    ) < ENTITY_SYNC_ORDER.index("rapid_trend_trigger_event")
    assert _required_parent_entities(
        "rapid_trend_trigger", trigger_payload
    ) == frozenset({
        ("trend", trigger["trend_id"]),
        (
            "trend_observation",
            trigger_payload["baseline_trend_observation_key"],
        ),
        (
            "trend_observation",
            trigger_payload["trigger_trend_observation_key"],
        ),
    })
    event_payload = json.loads(rows[1]["payload_json"])
    assert _required_parent_entities(
        "rapid_trend_trigger_event", event_payload
    ) == frozenset({("rapid_trend_trigger", trigger["trigger_id"])})

    with store.connect() as connection:
        connection.execute(
            """DELETE FROM mt_sync_outbox WHERE entity_type IN (
                   'rapid_trend_trigger', 'rapid_trend_trigger_event'
               )"""
        )
    store.enqueue_missing_for_sync()
    with store.connect() as connection:
        assert connection.execute(
            """SELECT COUNT(*) FROM mt_sync_outbox WHERE entity_type IN (
                   'rapid_trend_trigger', 'rapid_trend_trigger_event'
               )"""
        ).fetchone()[0] == 4


def test_authenticated_routes_expose_detail_and_block_unauthorized_handoff(
    tmp_path, monkeypatch
):
    now = datetime.now(timezone.utc).replace(microsecond=0)
    config = _config(tmp_path)
    store = MarketTapeStore(config)
    _import_graph(store, now - timedelta(hours=1))
    _seed_crossing(store, now)
    monkeypatch.setenv("MARKET_TAPE_CONTROL_TOKEN", TOKEN)
    app = Flask(__name__)
    register_market_tape_routes(app, config)
    client = app.test_client()
    headers = {"Authorization": f"Bearer {TOKEN}"}

    assert client.get("/api/market-tape/rapid-trends").status_code == 401
    assert client.post("/api/market-tape/rapid-trends/evaluate").status_code == 401
    evaluated = client.post(
        "/api/market-tape/rapid-trends/evaluate",
        json={"source_run_id": "rapid-api-test"},
        headers=headers,
    )
    assert evaluated.status_code == 200
    assert evaluated.get_json()["created"] == 1
    trigger_id = evaluated.get_json()["triggers"][0]["trigger_id"]

    listing = client.get("/api/market-tape/rapid-trends", headers=headers)
    detail = client.get(
        f"/api/market-tape/rapid-trends/{trigger_id}", headers=headers
    )
    blocked = client.get(
        f"/api/market-tape/rapid-trends/{trigger_id}/script-request",
        headers=headers,
    )
    assert listing.status_code == 200
    assert listing.get_json()["count"] == 1
    assert detail.status_code == 200
    assert detail.get_json()["trigger"]["trigger_sha256"]
    assert detail.get_json()["trigger"]["semantic_signal"]["signal_id"]
    assert blocked.status_code == 409
    blocked_body = blocked.get_json()
    assert blocked_body["contract"] == RAPID_TREND_SCRIPT_RESPONSE_CONTRACT
    assert blocked_body["generation_authorized"] is False
    assert blocked_body["blockers"] == ["approved_atomic_selection_pending"]
    assert blocked_body["script_request"] is None
    assert client.get(
        "/api/market-tape/rapid-trends/not-a-trigger",
        headers=headers,
    ).status_code == 404
    assert store.status()["schema_version"] == SCHEMA_VERSION == 19
    assert store.status()["rapid_trends"]["total"] == 1


def test_script_request_is_content_addressed_and_live_revocation_blocks_it(
    tmp_path,
):
    now = datetime.now(timezone.utc).replace(microsecond=0)
    store = MarketTapeStore(_config(tmp_path))
    imported = _import_graph(store, now - timedelta(hours=1))
    _seed_crossing(store, now)
    rapid = RapidTrendTriggerService(store.config, store)
    trigger = rapid.evaluate(
        source_run_id="rapid-ready-handoff", as_of=now
    )["triggers"][0]
    semantic = SemanticTopicService(store)
    graph_id = imported["graph"]["graph_version_id"]
    topic_id = "topic.rapid-trend-test"
    atomic_topic_id = "atomic_subject.rapid-trend-test"
    binding = semantic.record_binding({
        "signal_id": trigger["signal_id"],
        "topic_id": topic_id,
        "decision": "approved",
        "reviewer_type": "human",
        "reviewed_by": "reviewer.rapid-trend-owner",
        "reviewed_at": isoformat(now),
        "source_receipt_id": trigger["trigger_id"],
        "review_receipt_id": "receipt.rapid-trend.binding-approval",
        "confidence": 1.0,
        "rationale": "The owner reviewed this breakout against the canonical topic.",
        "binding_method": "human_review",
    })
    selection = semantic.record_atomic_selection({
        "graph_version_id": graph_id,
        "atomic_topic_id": atomic_topic_id,
        "binding_ids": [binding["binding_id"]],
        "reviewer_type": "human",
        "reviewer_id": "reviewer.rapid-trend-owner",
        "reviewed_at": isoformat(now),
        "review_receipt_id": "receipt.rapid-trend.atomic-selection",
        "rationale": "The reviewed breakout supports this atomic subject.",
    })["selection"]
    for index, evidence_type in enumerate(
        ("transcript_receipt", "human_moment"), start=1
    ):
        semantic.record_evidence_receipt({
            "selection_id": selection["selection_id"],
            "evidence_type": evidence_type,
            "status": "verified",
            "source_system": "rapid-trend-tests",
            "source_record_id": f"rapid-trend-evidence-{index}",
            "source_record_sha256": stable_hash({
                "evidence_type": evidence_type,
                "index": index,
            }),
            "created_at": isoformat(now),
        })

    semantic.generation_handoff(selection["selection_id"])

    ready, status_code = rapid.script_request(trigger["trigger_id"], as_of=now)

    assert status_code == 200, ready
    assert ready["generation_authorized"] is True
    request = ready["script_request"]
    request_core = {
        key: value
        for key, value in request.items()
        if key not in {"request_id", "request_sha256"}
    }
    assert stable_hash(request_core) == request["request_sha256"]
    assert request["request_id"] == (
        "rapid-trend-script-request:" + request["request_sha256"]
    )
    assert request["trigger"]["trigger_sha256"] == trigger["trigger_sha256"]
    assert request["semantic"]["atomic_selection_id"] == selection["selection_id"]
    assert request["generation_policy"] == {
        "script": True,
        "video": True,
        "publish": False,
    }

    semantic.record_binding({
        "signal_id": trigger["signal_id"],
        "topic_id": topic_id,
        "decision": "revoked",
        "reviewer_type": "human",
        "reviewed_by": "reviewer.rapid-trend-owner",
        "reviewed_at": isoformat(now + timedelta(minutes=1)),
        "source_receipt_id": trigger["trigger_id"],
        "review_receipt_id": "receipt.rapid-trend.binding-revocation",
        "confidence": 1.0,
        "rationale": "New review revoked the earlier topic binding.",
        "binding_method": "human_review",
    })
    revoked, revoked_status = rapid.script_request(
        trigger["trigger_id"], as_of=now + timedelta(minutes=1)
    )
    assert revoked_status == 409
    assert revoked["generation_authorized"] is False
    assert revoked["blockers"] == ["semantic_generation_handoff_not_ready"]
    assert revoked["script_request"] is None
