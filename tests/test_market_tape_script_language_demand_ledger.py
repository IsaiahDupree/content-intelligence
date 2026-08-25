"""Append-only script-language demand scheduling on real SQLite."""

from __future__ import annotations

import sqlite3
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

import pytest

from services.market_tape.config import MarketTapeConfig
from services.market_tape.store import (
    MarketTapeStore,
    SCHEMA_VERSION,
    ScriptLanguageDemandClaimConflict,
)


UTC = timezone.utc


def _config(tmp_path) -> MarketTapeConfig:
    return MarketTapeConfig(
        db_path=tmp_path / "market.sqlite3",
        object_dir=tmp_path / "objects",
        heartbeat_path=tmp_path / "heartbeat.json",
        lock_path=tmp_path / "market.lock",
        local_research_state_path=tmp_path / "local-research-state.json",
        prediction_model_dir=tmp_path / "models",
        platforms=["youtube"],
        topics=["creator retention"],
        supabase_sync_enabled=False,
    )


def _request(
    *,
    snapshot_id: str = "snapshot-001",
    evidence_trend_id: str = "trend-001",
    source_receipt_id: str = "brief-receipt-001",
    requested_at: datetime | None = None,
) -> dict:
    return {
        "contract": "market_tape_script_language_demand_v1",
        "source_service": "content-quality",
        "source_receipt_id": source_receipt_id,
        "topic": "Creator Retention",
        "audience": "Independent software founders",
        "objective": "Qualified attention",
        "evidence_trend_id": evidence_trend_id,
        "snapshot_id": snapshot_id,
        "targets": {
            "platforms": ["youtube", "tiktok"],
            "verified_transcripts": 5,
            "distinct_creators": 3,
        },
        "requested_at": requested_at or datetime(2026, 8, 24, 12, tzinfo=UTC),
    }


def test_current_schema_keeps_append_only_demand_and_lineage_ledgers(tmp_path):
    store = MarketTapeStore(_config(tmp_path))
    event = store.enqueue_script_language_demand(_request())

    with store.connect() as connection:
        schema_version = connection.execute(
            "SELECT value FROM mt_meta WHERE key = 'schema_version'"
        ).fetchone()[0]
        columns = {
            row[1]
            for row in connection.execute(
                "PRAGMA table_info(mt_script_language_demand_events)"
            ).fetchall()
        }
        indexes = {
            row[1]
            for row in connection.execute(
                "PRAGMA index_list(mt_script_language_demand_events)"
            ).fetchall()
        }
        triggers = {
            row[0]
            for row in connection.execute(
                """SELECT name FROM sqlite_master
                   WHERE type = 'trigger'
                     AND tbl_name = 'mt_script_language_demand_events'"""
            ).fetchall()
        }

    assert schema_version == str(SCHEMA_VERSION) == "15"
    assert {
        "event_id",
        "demand_id",
        "event_type",
        "attempt_no",
        "request_sha256",
        "source_service",
        "source_receipt_id",
        "topic",
        "audience",
        "objective",
        "evidence_trend_id",
        "snapshot_id",
        "lease_until",
        "collection_run_id",
        "transcript_run_id",
        "payload_json",
        "created_at",
    } <= columns
    assert {
        "mt_script_language_demand_events_demand_time_idx",
        "mt_script_language_demand_events_type_time_idx",
        "mt_script_language_demand_events_snapshot_time_idx",
    } <= indexes
    assert {
        "mt_script_language_demand_events_no_update",
        "mt_script_language_demand_events_no_delete",
    } == triggers

    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        with store.connect() as connection:
            connection.execute(
                """UPDATE mt_script_language_demand_events
                   SET topic = 'mutated' WHERE event_id = ?""",
                (event["events"][0]["event_id"],),
            )
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        with store.connect() as connection:
            connection.execute(
                "DELETE FROM mt_script_language_demand_events WHERE event_id = ?",
                (event["events"][0]["event_id"],),
            )


def test_enqueue_deduplicates_normalized_identity_but_keeps_lineage(tmp_path):
    store = MarketTapeStore(_config(tmp_path))
    first = store.enqueue_script_language_demand(_request())
    replay = _request(
        evidence_trend_id="trend-is-lineage-not-identity",
        source_receipt_id="later-receipt",
        requested_at=datetime(2026, 8, 24, 13, tzinfo=UTC),
    )
    replay.update({
        "topic": "  creator   retention ",
        "audience": "INDEPENDENT SOFTWARE FOUNDERS",
        "objective": "qualified ATTENTION",
        "targets": {
            "distinct_creators": 3,
            "verified_transcripts": 5,
            "platforms": ["TikTok", "YouTube"],
        },
    })
    second = store.enqueue_script_language_demand(replay)
    changed_snapshot = store.enqueue_script_language_demand(
        _request(snapshot_id="snapshot-002")
    )

    assert first["enqueued"] is True
    assert first["idempotent"] is False
    assert second["demand_id"] == first["demand_id"]
    assert second["enqueued"] is False
    assert second["deduplicated"] is True
    assert second["idempotent"] is False
    assert len(second["events"]) == 1
    assert second["source_receipt_id"] == "later-receipt"
    assert second["evidence_trend_id"] == "trend-is-lineage-not-identity"
    assert changed_snapshot["demand_id"] == first["demand_id"]

    requested = store.list_script_language_demands(
        state="requested",
        as_of=datetime(2026, 8, 24, 14, tzinfo=UTC),
    )
    assert {row["demand_id"] for row in requested} == {first["demand_id"]}


def test_expired_lease_reclaims_with_next_attempt_and_terminal_is_idempotent(
    tmp_path,
):
    store = MarketTapeStore(_config(tmp_path))
    demand = store.enqueue_script_language_demand(_request())
    claimed_at = datetime(2026, 8, 24, 12, 1, tzinfo=UTC)

    first_claim = store.claim_next_script_language_demand(
        10,
        as_of=claimed_at,
        source_service="transcript-acquisition",
        collection_run_id="collection-001",
    )
    assert first_claim is not None
    assert first_claim["demand_id"] == demand["demand_id"]
    assert first_claim["state"] == "claimed"
    assert first_claim["attempt_no"] == 1
    assert first_claim["lease_active"] is True
    assert store.claim_next_script_language_demand(
        10, as_of=claimed_at + timedelta(seconds=9)
    ) is None

    reclaimed = store.claim_next_script_language_demand(
        10,
        as_of=claimed_at + timedelta(seconds=11),
        transcript_run_id="transcript-002",
    )
    assert reclaimed is not None
    assert reclaimed["demand_id"] == demand["demand_id"]
    assert reclaimed["attempt_no"] == 2
    assert reclaimed["attempt_count"] == 2
    assert reclaimed["events"][-1]["payload"]["reclaimed_expired_lease"] is True

    with pytest.raises(ValueError, match="latest claim"):
        store.finish_script_language_demand(
            demand["demand_id"],
            1,
            "completed",
            {"artifacts": 5},
            as_of=claimed_at + timedelta(seconds=12),
        )

    completed = store.finish_script_language_demand(
        demand["demand_id"],
        2,
        "completed",
        {"artifacts": 5, "distinct_creators": 3},
        as_of=claimed_at + timedelta(seconds=12),
        source_receipt_id="transcript-terminal-002",
    )
    replayed_terminal = store.finish_script_language_demand(
        demand["demand_id"],
        2,
        "completed",
        {"artifacts": 999},
        as_of=claimed_at + timedelta(seconds=30),
    )

    assert completed["state"] == "completed"
    assert completed["appended"] is True
    assert replayed_terminal["state"] == "completed"
    assert replayed_terminal["appended"] is False
    assert replayed_terminal["deduplicated"] is True
    assert [event["event_type"] for event in replayed_terminal["events"]] == [
        "requested",
        "claimed",
        "claimed",
        "completed",
    ]
    assert store.claim_next_script_language_demand(
        10, as_of=claimed_at + timedelta(seconds=40)
    ) is None
    fetched = store.script_language_demand(demand["demand_id"])
    assert fetched is not None
    assert fetched["state"] == "completed"
    assert store.list_script_language_demands(state="completed")[0][
        "demand_id"
    ] == demand["demand_id"]


def test_two_concurrent_claimers_cannot_claim_the_same_demand(tmp_path):
    config = _config(tmp_path)
    writer = MarketTapeStore(config)
    demand = writer.enqueue_script_language_demand(_request())
    claimers = [MarketTapeStore(config), MarketTapeStore(config)]
    barrier = threading.Barrier(2)
    claimed_at = datetime(2026, 8, 24, 12, 1, tzinfo=UTC)

    def claim(store: MarketTapeStore):
        barrier.wait(timeout=5)
        return store.claim_next_script_language_demand(
            60, as_of=claimed_at
        )

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(claim, claimers))

    successful = [result for result in results if result is not None]
    assert len(successful) == 1
    assert successful[0]["demand_id"] == demand["demand_id"]
    assert successful[0]["attempt_no"] == 1
    with writer.connect() as connection:
        claim_count = connection.execute(
            """SELECT COUNT(*)
               FROM mt_script_language_demand_events
               WHERE demand_id = ? AND event_type = 'claimed'""",
            (demand["demand_id"],),
        ).fetchone()[0]
    assert claim_count == 1


def test_expected_demand_id_mismatch_is_atomic_and_appends_no_claim(tmp_path):
    store = MarketTapeStore(_config(tmp_path))
    first = store.enqueue_script_language_demand(
        _request(
            snapshot_id="expected-first-snapshot",
            source_receipt_id="expected-first-receipt",
            requested_at=datetime(2026, 8, 24, 12, tzinfo=UTC),
        )
    )
    second_request = _request(
        snapshot_id="expected-second-snapshot",
        source_receipt_id="expected-second-receipt",
        requested_at=datetime(2026, 8, 24, 13, tzinfo=UTC),
    )
    second_request["topic"] = "AI Automation"
    second = store.enqueue_script_language_demand(second_request)
    assert first["demand_id"] != second["demand_id"]

    with pytest.raises(ScriptLanguageDemandClaimConflict) as raised:
        store.claim_next_script_language_demand(
            600,
            expected_demand_id=second["demand_id"],
        )

    assert raised.value.payload() == {
        "status": "error",
        "state": "conflict",
        "code": "SCRIPT_LANGUAGE_DEMAND_CLAIM_CONFLICT",
        "error": "expected_demand_id does not match the next claimable demand",
        "expected_demand_id": second["demand_id"],
        "next_demand_id": first["demand_id"],
        "mutation_applied": False,
    }
    with store.connect() as connection:
        assert connection.execute(
            """SELECT COUNT(*) FROM mt_script_language_demand_events
               WHERE event_type='claimed'"""
        ).fetchone()[0] == 0

    claimed = store.claim_next_script_language_demand(
        600,
        expected_demand_id=first["demand_id"],
    )
    assert claimed is not None
    assert claimed["demand_id"] == first["demand_id"]
