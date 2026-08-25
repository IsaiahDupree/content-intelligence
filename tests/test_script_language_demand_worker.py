"""One-shot demand consumption against real Market Tape SQLite/services."""

from __future__ import annotations

from services.market_tape.config import MarketTapeConfig
from services.market_tape.script_demand import ScriptLanguageDemandWorker
from services.market_tape.store import MarketTapeStore


def _config(tmp_path) -> MarketTapeConfig:
    return MarketTapeConfig(
        db_path=tmp_path / "market.sqlite3",
        object_dir=tmp_path / "objects",
        heartbeat_path=tmp_path / "heartbeat.json",
        lock_path=tmp_path / "market.lock",
        local_research_state_path=tmp_path / "local-state.json",
        prediction_model_dir=tmp_path / "models",
        dataset_root=tmp_path / "datasets",
        passport_mount=tmp_path,
        platforms=["youtube"],
        topics=["creator retention"],
        adaptive_topics_enabled=False,
        daily_unique_target=50,
        platform_daily_targets={"youtube": 50},
        provider_daily_request_limits={"youtube": 10},
        provider_cost_per_request_usd={"youtube": 0.0},
        supabase_sync_enabled=False,
    )


def _demand(source_receipt_id: str) -> dict:
    return {
        "contract": "market_tape_script_language_demand_v1",
        "source_service": "content-quality",
        "source_receipt_id": source_receipt_id,
        "topic": "creator retention",
        "audience": "software founders",
        "objective": "qualified attention",
        "evidence_trend_id": "trend:historical-lineage-only",
        "snapshot_id": f"snapshot-{source_receipt_id}",
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


def test_worker_claims_exactly_one_and_zero_candidates_are_not_goal_met(
    tmp_path, monkeypatch
):
    # The real YouTube source takes its explicit blocked-credential path; no
    # provider or Whisper call is fabricated and no external network is used.
    monkeypatch.delenv("YOUTUBE_API_KEY", raising=False)
    monkeypatch.delenv("YOUTUBE_DATA_API_KEY", raising=False)
    config = _config(tmp_path)
    store = MarketTapeStore(config)
    first = store.enqueue_script_language_demand(_demand("refusal-1"))
    second = store.enqueue_script_language_demand(_demand("refusal-2"))
    worker = ScriptLanguageDemandWorker(
        config,
        store,
        transcript_storage_root=tmp_path / "transcript-bank",
    )

    result = worker.run_next()

    assert result["processed"] == 1
    assert result["goal_met"] is False
    assert result["state"] == "blocked"
    assert result["result"]["one_cycle"] is True
    assert result["result"]["same_call_retry"] is False
    assert result["result"]["pipeline"]["videos_discovered"] == 0
    assert result["result"]["pipeline"]["candidate_count"] == 0
    assert result["result"]["pipeline"]["trend_ids"] == []
    completed = store.script_language_demand(first["demand_id"])
    untouched = store.script_language_demand(second["demand_id"])
    assert completed["state"] == "blocked"
    assert [event["event_type"] for event in completed["events"]] == [
        "requested", "claimed", "blocked",
    ]
    assert completed["events"][-1]["payload"]["result"]["goal_met"] is False
    assert completed["collection_run_id"]
    assert completed["transcript_run_id"]
    assert untouched["state"] == "requested"

    with store.connect() as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM mt_collection_runs"
        ).fetchone()[0] == 1


def test_worker_never_retries_a_terminal_demand_in_the_same_or_next_call(
    tmp_path, monkeypatch
):
    monkeypatch.delenv("YOUTUBE_API_KEY", raising=False)
    monkeypatch.delenv("YOUTUBE_DATA_API_KEY", raising=False)
    config = _config(tmp_path)
    store = MarketTapeStore(config)
    demand = store.enqueue_script_language_demand(_demand("only-refusal"))
    worker = ScriptLanguageDemandWorker(
        config,
        store,
        transcript_storage_root=tmp_path / "transcript-bank",
    )

    first = worker.run_next()
    second = worker.run_next()

    assert first["processed"] == 1
    assert second == {
        "contract": "market_tape_script_language_demand_run_v1",
        "state": "idle",
        "processed": 0,
        "goal_met": False,
    }
    stored = store.script_language_demand(demand["demand_id"])
    assert stored["attempt_count"] == 1
    assert len(stored["events"]) == 3
    with store.connect() as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM mt_collection_runs"
        ).fetchone()[0] == 1
