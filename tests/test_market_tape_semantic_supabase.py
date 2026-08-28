"""Semantic control-plane tables are synced before legacy Market Tape rows."""

from __future__ import annotations

import json

from services.market_tape.config import MarketTapeConfig
from services.market_tape.migration import (
    APPEND_ONLY_TABLES,
    MARKET_TAPE_TABLES,
    REQUIRED_INDEXES,
    migration_sql,
    validate_migration,
    verification_sql,
)
from services.market_tape.sinks.supabase import ENTITY_SYNC_ORDER, ENTITY_TABLES
from services.market_tape.store import MarketTapeStore


SEMANTIC_ENTITY_ORDER = (
    "semantic_graph_version",
    "semantic_topic_node",
    "semantic_topic_edge",
    "semantic_signal_candidate",
    "semantic_signal_binding",
    "semantic_resolution_run",
    "semantic_topic_observation",
    "semantic_atomic_selection",
    "semantic_atomic_selection_source",
    "semantic_evidence_receipt",
    "semantic_lineage_registration",
    "semantic_content_brief",
    "semantic_content_asset",
    "semantic_content_lineage",
)

EXPECTED_ADVISOR_INDEXES = {
    "actp_semantic_atomic_sources_observation_idx": (
        "actp_semantic_atomic_selection_sources",
        "topic_observation_key",
    ),
    "actp_semantic_atomic_sources_signal_idx": (
        "actp_semantic_atomic_selection_sources",
        "signal_id",
    ),
    "actp_semantic_assets_graph_atomic_idx": (
        "actp_semantic_content_assets",
        "graph_version_id,atomic_topic_id",
    ),
    "actp_semantic_assets_parent_idx": (
        "actp_semantic_content_assets",
        "parent_asset_id",
    ),
    "actp_semantic_briefs_selection_idx": (
        "actp_semantic_content_briefs",
        "atomic_selection_id",
    ),
    "actp_semantic_briefs_registration_idx": (
        "actp_semantic_content_briefs",
        "registration_id",
    ),
    "actp_semantic_lineage_graph_atomic_idx": (
        "actp_semantic_content_lineage",
        "graph_version_id,atomic_topic_id",
    ),
    "actp_semantic_lineage_signal_graph_idx": (
        "actp_semantic_content_lineage",
        "signal_id,graph_version_id",
    ),
    "actp_semantic_lineage_observation_idx": (
        "actp_semantic_content_lineage",
        "topic_observation_key",
    ),
    "actp_semantic_resolution_graph_selected_idx": (
        "actp_semantic_resolution_runs",
        "graph_version_id,selected_topic_id",
    ),
    "actp_semantic_resolution_signal_graph_idx": (
        "actp_semantic_resolution_runs",
        "signal_id,graph_version_id",
    ),
    "actp_semantic_bindings_signal_graph_idx": (
        "actp_semantic_signal_bindings",
        "signal_id,graph_version_id",
    ),
    "actp_semantic_signals_source_trend_idx": (
        "actp_semantic_signal_candidates",
        "source_trend_id",
    ),
    "actp_semantic_observations_binding_fk_idx": (
        "actp_semantic_topic_observations",
        "binding_id,graph_version_id,signal_id,topic_id",
    ),
    "actp_semantic_observations_signal_graph_idx": (
        "actp_semantic_topic_observations",
        "signal_id,graph_version_id",
    ),
}


def test_semantic_migration_and_sink_registry_cover_the_same_control_plane():
    validation = validate_migration()
    semantic_tables = {
        table for table in MARKET_TAPE_TABLES if table.startswith("actp_semantic_")
    }

    assert validation["state"] == "ready", validation
    assert validation["tables_expected"] == len(MARKET_TAPE_TABLES)
    assert set(MARKET_TAPE_TABLES) == {
        definition[0] for definition in ENTITY_TABLES.values()
    }
    assert semantic_tables <= APPEND_ONLY_TABLES
    assert all(table in verification_sql() for table in semantic_tables)


def test_semantic_advisor_indexes_are_checked_and_cover_expected_columns():
    compact_sql = "".join(migration_sql().lower().split())

    assert REQUIRED_INDEXES == {
        index_name: table
        for index_name, (table, _columns) in EXPECTED_ADVISOR_INDEXES.items()
    }
    assert len(REQUIRED_INDEXES) == 15
    for index_name, (table, columns) in EXPECTED_ADVISOR_INDEXES.items():
        expected = (
            f"createindexifnotexists{index_name}"
            f"onpublic.{table}({columns})"
        )
        assert expected in compact_sql

    missing_name = "actp_semantic_assets_parent_idx"
    broken_sql = migration_sql().replace(missing_name, f"{missing_name}_missing", 1)
    validation = validate_migration(broken_sql)
    assert validation["state"] == "invalid"
    assert validation["missing_required_indexes"] == [missing_name]


def test_semantic_outbox_entities_are_dependency_ordered_ahead_of_legacy(tmp_path):
    config = MarketTapeConfig(
        db_path=tmp_path / "market.sqlite3",
        object_dir=tmp_path / "objects",
        heartbeat_path=tmp_path / "heartbeat.json",
        lock_path=tmp_path / "market.lock",
        local_research_state_path=tmp_path / "local-research-state.json",
        prediction_model_dir=tmp_path / "models",
    )
    store = MarketTapeStore(config)
    due_at = "2000-01-01T00:00:00Z"
    inserted_order = ("source_health", "creator", *reversed(SEMANTIC_ENTITY_ORDER))
    with store.connect() as connection:
        for index, entity_type in enumerate(inserted_order):
            connection.execute(
                """INSERT INTO mt_sync_outbox(
                       entity_type, entity_key, payload_json,
                       created_at, next_attempt_at
                   ) VALUES(?, ?, ?, ?, ?)""",
                (
                    entity_type,
                    f"record-{index}",
                    json.dumps({"record": index}),
                    due_at,
                    due_at,
                ),
            )

    pending = store.pending_outbox(100, entity_order=ENTITY_SYNC_ORDER)
    pending_types = tuple(row["entity_type"] for row in pending)

    assert ENTITY_SYNC_ORDER[: len(SEMANTIC_ENTITY_ORDER)] == SEMANTIC_ENTITY_ORDER
    assert pending_types[: len(SEMANTIC_ENTITY_ORDER)] == SEMANTIC_ENTITY_ORDER
    assert pending_types[-2:] == ("creator", "source_health")
    assert set(ENTITY_SYNC_ORDER) == set(ENTITY_TABLES)
    assert all(ENTITY_TABLES[entity_type][2] is False for entity_type in SEMANTIC_ENTITY_ORDER)
