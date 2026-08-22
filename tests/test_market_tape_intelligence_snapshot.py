"""Real-SQLite contract for the unified read-only intelligence surface."""

from __future__ import annotations

from services.market_tape.config import MarketTapeConfig
from services.market_tape.intelligence import build_intelligence_snapshot
from services.market_tape.store import MarketTapeStore


def test_empty_tape_snapshot_is_observed_only_and_lineage_complete(tmp_path):
    config = MarketTapeConfig(
        db_path=tmp_path / "market.sqlite3",
        object_dir=tmp_path / "objects",
        heartbeat_path=tmp_path / "heartbeat.json",
        lock_path=tmp_path / "market.lock",
        local_research_state_path=tmp_path / "local-research-state.json",
        prediction_model_dir=tmp_path / "models",
        dataset_root=tmp_path / "passport-datasets",
        passport_mount=tmp_path,
        platforms=["youtube"],
        topics=["measured trend"],
        supabase_sync_enabled=False,
    )
    store = MarketTapeStore(config)

    snapshot = build_intelligence_snapshot(
        config,
        store,
        limit=5,
        window_hours=24,
        minimum_videos=2,
    )

    assert snapshot["contract"] == "market_tape_intelligence_snapshot_v1"
    assert snapshot["state"] == "observed_only"
    assert snapshot["read_only"] is True
    assert snapshot["parameters"] == {
        "limit": 5,
        "window_hours": 24,
        "minimum_videos": 2,
    }
    assert snapshot["lineage"]["live_database_path"] == str(config.db_path)
    assert snapshot["lineage"]["passport_dataset"]["state"] == "not_run"
    assert snapshot["keywords"]["derived_terms"] == []
    assert snapshot["keywords"]["exact_discovery_queries"] == []
    assert snapshot["forecast"]["probability_admitted"] is False
    assert snapshot["forecast"]["admission"]["admission_reason"] == (
        "no_active_model"
    )
    assert snapshot["trends"]["score_is_probability"] is False

