"""Focused integration tests for the durable Market Tape semantic layer."""

from __future__ import annotations

import json
import re
import sqlite3
import sys
import threading
from copy import deepcopy
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest
from flask import Flask

from services.market_tape.api import register_market_tape_routes
from services.market_tape.cli import main as market_tape_cli
from services.market_tape.config import MarketTapeConfig
from services.market_tape.models import stable_hash
from services.market_tape.semantic import (
    GRAPH_IMPORT_CONTRACT,
    SIGNAL_CONTRACT,
    TOPIC_LEVELS,
    TOPIC_RELATIONSHIPS,
    SemanticContractError,
    SemanticTopicService,
    validate_topic_graph,
)
from services.market_tape.store import MarketTapeStore


FOUNDRY_ROOT = Path(__file__).resolve().parents[2] / "marketing-video-foundry"
FOUNDRY_GRAPH_PATH = FOUNDRY_ROOT / "configs/content-topic-graph-v2.json"
if str(FOUNDRY_ROOT) not in sys.path:
    sys.path.insert(0, str(FOUNDRY_ROOT))

from foundry.marketing.content_ontology import ContentOntology  # noqa: E402
from foundry.marketing.semantic_trend_bridge import (  # noqa: E402
    build_semantic_trend_content_plan,
)


def _config(tmp_path: Path, name: str = "market.sqlite3") -> MarketTapeConfig:
    return MarketTapeConfig(
        db_path=tmp_path / name,
        object_dir=tmp_path / "objects",
        heartbeat_path=tmp_path / "heartbeat.json",
        lock_path=tmp_path / "market.lock",
        local_research_state_path=tmp_path / "local-research-state.json",
        prediction_model_dir=tmp_path / "models",
    )


def _graph() -> dict:
    names = {
        "strategic_territory": "AI-powered media businesses",
        "content_domain": "AI media production",
        "pillar": "Automated video production",
        "topic": "AI avatar videos",
        "subtopic": "Avatar-video retention",
        "atomic_subject": "First-two-second viewer drop-off",
    }
    aliases = {
        "strategic_territory": ["AI media businesses"],
        "content_domain": ["AI content production"],
        "pillar": ["automated videos"],
        "topic": ["AI avatar"],
        "subtopic": ["avatar retention"],
        "atomic_subject": ["two second avatar dropoff"],
    }
    nodes = []
    relationships = []
    parent_id = None
    for index, level in enumerate(TOPIC_LEVELS):
        topic_id = f"{level}.avatar-test"
        nodes.append({
            "id": topic_id,
            "name": names[level],
            "definition": f"Canonical {level} subject for avatar retention tests.",
            "level": level,
            "canonical_parent_id": parent_id,
            "aliases": aliases[level],
            "status": "active",
            "strategic_priority": 90 - index,
        })
        if parent_id is not None:
            relationships.append({
                "source_topic_id": topic_id,
                "target_topic_id": parent_id,
                "relationship_type": "part_of",
            })
        parent_id = topic_id
    inventory = {
        "node_count": len(nodes),
        "relationship_count": len(relationships),
        "by_level": {level: 1 for level in TOPIC_LEVELS},
    }
    core = {
        "schema_version": "2.0",
        "contract_type": "content_topic_graph_v2",
        "levels": list(TOPIC_LEVELS),
        "relationship_types": list(TOPIC_RELATIONSHIPS),
        "nodes": nodes,
        "relationships": relationships,
        "metadata": {"graph_version": "semantic-focused-test-v1"},
        "migration": {},
        "inventory": inventory,
    }
    return {**core, "graph_sha256": stable_hash(core)}


def _rehash_graph(graph: dict) -> dict:
    core = {key: value for key, value in graph.items() if key != "graph_sha256"}
    graph["graph_sha256"] = stable_hash(core)
    return graph


def _migration_graph() -> dict:
    graph = deepcopy(_graph())
    graph["migration"] = {
        "contract_type": "content_topic_catalog_migration_v2",
        "source_catalog_version": "content-topic-catalog-v1.0.0",
        "source_file_sha256": "a" * 64,
        "source_count": 1,
        "source_coverage": "exactly_once",
        "legacy_role_semantics": "historical_treatment_hint_only",
        "sources": [
            {
                "source_id": "legacy-seed-a4cbfcd4f6e104f9b45a",
                "catalog_version": "content-topic-catalog-v1.0.0",
                "legacy_role": "SELL",
                "legacy_territory_id": "problem_aware",
                "source_text": "Website leads waiting hours for follow-up",
                "source_kind": "legacy_content_seed",
                "mapped_atomic_subject_id": "atomic_subject.avatar-test",
                "generation_authorized": False,
                "publishing_authorized": False,
            }
        ],
    }
    return _rehash_graph(graph)


def _migration_signal(
    service: SemanticTopicService,
    imported: dict,
    graph: dict,
    *,
    text: str = "Website leads waiting hours for follow-up",
    source_id: str = "legacy-seed-a4cbfcd4f6e104f9b45a",
    evidence_overrides: dict | None = None,
) -> dict:
    mapping = graph["migration"]["sources"][0]
    evidence = {
        "contract": "foundry_legacy_seed_semantic_signal_evidence_v1",
        "source_catalog_version": graph["migration"]["source_catalog_version"],
        "source_kind": mapping["source_kind"],
        "source_text": mapping["source_text"],
        "mapped_atomic_subject_id": mapping["mapped_atomic_subject_id"],
        "generation_authorized": False,
        "publishing_authorized": False,
        "topic_graph_sha256": graph["graph_sha256"],
        "source_file_sha256": graph["migration"]["source_file_sha256"],
        "metrics": {"catalog_records_observed": 1},
    }
    evidence.update(evidence_overrides or {})
    return service.ingest_signal({
        "contract": SIGNAL_CONTRACT,
        "graph_version_id": imported["graph"]["graph_version_id"],
        "signal_type": "problem",
        "source_kind": "external_signal",
        "source_entity_id": source_id,
        "source_observed_at": "2026-08-28T18:05:00Z",
        "signal_text": text,
        "source_receipt_id": f"foundry-topic-graph-sha:{graph['graph_sha256']}",
        "evidence": evidence,
    })


def _import(service: SemanticTopicService, graph: dict | None = None) -> dict:
    return service.import_graph({
        "contract": GRAPH_IMPORT_CONTRACT,
        "source_service": "semantic-focused-test",
        "source_receipt_id": "receipt:graph-focused-test",
        "imported_by": "reviewer.test-owner",
        "imported_at": "2026-08-28T18:00:00Z",
        "graph": graph or _graph(),
    })


def _signal(
    service: SemanticTopicService,
    graph_version_id: str,
    text: str,
    source_id: str,
    source_kind: str = "external_signal",
) -> dict:
    return service.ingest_signal({
        "contract": SIGNAL_CONTRACT,
        "graph_version_id": graph_version_id,
        "signal_type": "keyword",
        "source_kind": source_kind,
        "source_entity_id": source_id,
        "source_observed_at": "2026-08-28T18:05:00Z",
        "signal_text": text,
        "source_receipt_id": f"receipt:{source_id}",
        "evidence": {
            "contract": "semantic-focused-evidence-v1",
            "metrics": {"views": 1200, "momentum": 0.72},
        },
    })


def _approved_binding(service: SemanticTopicService) -> tuple[dict, dict, dict]:
    imported = _import(service)
    signal = _signal(
        service,
        imported["graph"]["graph_version_id"],
        "AI avatar",
        "signal-approved",
    )
    resolved = service.resolve_signal(signal["signal_id"], use_ai=False)
    return imported, signal, resolved["binding"]


def _evidence(
    service: SemanticTopicService,
    selection_id: str,
    evidence_type: str,
    ordinal: int,
) -> dict:
    payload = {
        "selection_id": selection_id,
        "evidence_type": evidence_type,
        "status": "ready" if evidence_type == "transcript_receipt" else "verified",
        "source_system": "content.intelligence",
        "source_record_id": f"{evidence_type.replace('_', '.')}.{ordinal}",
        "source_record_sha256": f"{ordinal:x}" * 64,
        "created_at": f"2026-08-28T18:{20 + ordinal:02d}:00Z",
    }
    if evidence_type == "software_change_receipt":
        payload.update({
            "claim": "The current repository change adds a verified workflow.",
            "source_uri": (
                "https://github.com/IsaiahDupree/storyrail/commit/"
                + f"{ordinal:x}" * 40
            ),
        })
    return service.record_evidence_receipt(payload)


def _content_spec() -> dict:
    return {
        "audience": "solo creators",
        "audience_problem": "Viewers leave when avatar delivery feels unnatural.",
        "audience_intent": "Understand how to hold attention without hiding the avatar.",
        "funnel_stage": "entertain_masses",
        "angle": "Delivery quality matters before visual polish.",
        "candidate_central_ideas": [
            {
                "claim": "Avatar videos lose attention when opening delivery feels unnatural before viewers assess visual quality.",
                "counter_position": "Visual realism is always the first retention bottleneck.",
            },
            {
                "claim": "A recognizable human tension can make an avatar opening feel less synthetic.",
                "counter_position": "Avatar retention depends only on technical rendering quality.",
            },
        ],
        "selected_central_idea_index": 0,
        "narrative_structure": "recognition, tension, evidence, release",
        "desired_emotion": "recognition",
        "delivery_format": "vertical spoken story",
        "platform": "short_video",
        "offer_id": None,
        "cta": "Compare the opening before you change the avatar.",
        "hook_hypothesis": "A contrarian diagnosis creates immediate curiosity.",
        "parent_asset": {
            "format": "thirty-second vertical video",
            "platform": "short_video",
            "duration_seconds": 30,
            "account": "primary-account",
        },
        "derivatives": [{
            "derivative_type": "text_post",
            "format": "short text post",
            "platform": "x",
            "account": "x-main",
        }],
    }


class _OpenAIResponseHandler(BaseHTTPRequestHandler):
    request_body: dict = {}

    def do_POST(self):  # noqa: N802 - stdlib HTTP contract
        length = int(self.headers.get("Content-Length", "0"))
        self.__class__.request_body = json.loads(self.rfile.read(length))
        output = {
            "decision": "match",
            "selected_topic_id": "topic.avatar-test",
            "confidence": 0.82,
            "rationale": "The bounded topic candidate best fits the signal.",
        }
        body = json.dumps({
            "id": "resp_semantic_contract_test",
            "object": "response",
            "created_at": 1787941200,
            "status": "completed",
            "model": "gpt-5-nano",
            "output": [{
                "id": "msg_semantic_contract_test",
                "type": "message",
                "status": "completed",
                "role": "assistant",
                "content": [{
                    "type": "output_text",
                    "text": json.dumps(output),
                    "annotations": [],
                }],
            }],
            "usage": {
                "input_tokens": 31,
                "output_tokens": 17,
                "total_tokens": 48,
            },
        }).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, _format, *_args):
        return


@pytest.fixture
def openai_contract_server():
    _OpenAIResponseHandler.request_body = {}
    server = ThreadingHTTPServer(("127.0.0.1", 0), _OpenAIResponseHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}/v1"
    finally:
        server.shutdown()
        thread.join(timeout=5)
        server.server_close()


def test_graph_import_is_idempotent_validated_and_exact_alias_is_audited(
    tmp_path: Path,
):
    store = MarketTapeStore(_config(tmp_path))
    service = SemanticTopicService(store)

    first = _import(service)
    second = _import(service)

    assert first["imported"] is True
    assert second["imported"] is False
    assert second["idempotent"] is True
    assert first["graph"]["graph_version_id"] == second["graph"]["graph_version_id"]
    assert first["inventory"]["node_count"] == 6
    assert first["inventory"]["edge_count"] == 5
    with store.connect() as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM mt_topic_graph_versions"
        ).fetchone()[0] == 1
        assert connection.execute(
            "SELECT COUNT(*) FROM mt_topic_nodes"
        ).fetchone()[0] == 6
        assert connection.execute(
            "SELECT COUNT(*) FROM mt_topic_edges"
        ).fetchone()[0] == 5

    invalid = deepcopy(_graph())
    invalid["nodes"][3]["angle"] = "forbidden treatment"
    invalid["graph_sha256"] = stable_hash({
        key: value for key, value in invalid.items() if key != "graph_sha256"
    })
    with pytest.raises(SemanticContractError, match="treatment fields"):
        validate_topic_graph(invalid)

    wrong_level = deepcopy(_graph())
    wrong_level["nodes"][3]["canonical_parent_id"] = "content_domain.avatar-test"
    wrong_level["graph_sha256"] = stable_hash({
        key: value for key, value in wrong_level.items() if key != "graph_sha256"
    })
    with pytest.raises(SemanticContractError, match="parent must be level pillar"):
        validate_topic_graph(wrong_level)

    signal = _signal(
        service, first["graph"]["graph_version_id"], "AI avatar", "alias-signal"
    )
    resolved = service.resolve_signal(signal["signal_id"], use_ai=False)
    binding = resolved["binding"]

    assert resolved["state"] == "resolved_deterministically"
    assert binding["topic_id"] == "topic.avatar-test"
    assert binding["decision"] == "approved"
    assert binding["reviewer_type"] == "rules"
    assert binding["observation"]["topic_id"] == "topic.avatar-test"
    assert binding["input_sha256"] == stable_hash(
        resolved["resolution_run"]["input_contract"]
    )
    assert binding["output_sha256"] == stable_hash(
        resolved["resolution_run"]["output_contract"]
    )


def test_exact_graph_migration_mapping_creates_one_rules_binding_and_is_idempotent(
    tmp_path: Path,
):
    store = MarketTapeStore(_config(tmp_path))
    service = SemanticTopicService(store)
    graph = _migration_graph()
    imported = _import(service, graph)
    signal = _migration_signal(service, imported, graph)

    preview = service.preview_resolution(signal["signal_id"])
    assert preview["state"] == "deterministic_match"
    assert preview["resolution_path"] == "exact_graph_migration"
    assert preview["validation_errors"] == []
    assert preview["provider_call_performed"] is False

    resolved = service.resolve_signal(signal["signal_id"], use_ai=False)
    binding = resolved["binding"]
    assert resolved["state"] == "resolved_deterministically"
    assert resolved["resolution_path"] == "exact_graph_migration"
    assert resolved["requires_human_review"] is False
    assert resolved["generation_authorized"] is False
    assert resolved["publishing_authorized"] is False
    assert binding["topic_id"] == "atomic_subject.avatar-test"
    assert binding["decision"] == "approved"
    assert binding["reviewer_type"] == "rules"
    assert binding["binding_method"] == "deterministic_exact_graph_migration"
    assert binding["model_version"] == "exact-graph-migration-v1"
    assert binding["audit"]["mapping_sha256"] == stable_hash(
        graph["migration"]["sources"][0]
    )
    assert binding["audit"]["graph_sha256"] == graph["graph_sha256"]
    assert binding["audit"]["source_file_sha256"] == "a" * 64
    assert binding["observation"]["metrics"] == {"catalog_records_observed": 1}
    assert resolved["resolution_run"]["input_sha256"] == stable_hash(
        resolved["resolution_run"]["input_contract"]
    )
    assert resolved["resolution_run"]["output_sha256"] == stable_hash(
        resolved["resolution_run"]["output_contract"]
    )

    repeated = service.resolve_signal(signal["signal_id"], use_ai=False)
    assert repeated == {
        "status": "ok",
        "contract": "market_tape_semantic_resolution_v1",
        "state": "already_resolved",
        "signal_id": signal["signal_id"],
        "graph_version_id": imported["graph"]["graph_version_id"],
        "topic_ids": ["atomic_subject.avatar-test"],
        "mutation_applied": False,
    }
    with store.connect() as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM mt_topic_resolution_runs"
        ).fetchone()[0] == 1
        assert connection.execute(
            "SELECT COUNT(*) FROM mt_topic_signal_bindings"
        ).fetchone()[0] == 1
        assert connection.execute(
            "SELECT COUNT(*) FROM mt_topic_observations"
        ).fetchone()[0] == 1


@pytest.mark.parametrize(
    ("case", "expected_error"),
    [
        ("signal_text", "signal_text_not_exact_migration_source_text"),
        ("graph_sha", "evidence_graph_sha256_mismatch"),
        ("source_file_sha", "evidence_source_file_sha256_mismatch"),
        ("mapped_evidence", "evidence_mapped_atomic_subject_id_mismatch"),
        ("duplicate_source_id", "source_entity_mapping_not_unique"),
        ("non_atomic_target", "mapped_target_not_atomic_subject"),
        ("inactive_target", "mapped_atomic_subject_missing_or_inactive"),
        ("unknown_source_id", "source_entity_mapping_not_unique"),
    ],
)
def test_graph_migration_mapping_fails_closed_for_tampering_or_ambiguity(
    tmp_path: Path,
    case: str,
    expected_error: str,
):
    store = MarketTapeStore(_config(tmp_path))
    service = SemanticTopicService(store)
    graph = _migration_graph()
    text = "Website leads waiting hours for follow-up"
    source_id = "legacy-seed-a4cbfcd4f6e104f9b45a"
    evidence_overrides: dict = {}
    if case == "duplicate_source_id":
        graph["migration"]["sources"].append(
            deepcopy(graph["migration"]["sources"][0])
        )
        graph["migration"]["source_count"] = 2
        _rehash_graph(graph)
    elif case == "non_atomic_target":
        graph["migration"]["sources"][0]["mapped_atomic_subject_id"] = (
            "subtopic.avatar-test"
        )
        _rehash_graph(graph)
    elif case == "inactive_target":
        next(
            node
            for node in graph["nodes"]
            if node["id"] == "atomic_subject.avatar-test"
        )["status"] = "deprecated"
        _rehash_graph(graph)
    elif case == "signal_text":
        text = "Website leads waiting minutes for follow-up"
    elif case == "graph_sha":
        evidence_overrides["topic_graph_sha256"] = "b" * 64
    elif case == "source_file_sha":
        evidence_overrides["source_file_sha256"] = "b" * 64
    elif case == "mapped_evidence":
        evidence_overrides["mapped_atomic_subject_id"] = "subtopic.avatar-test"
    elif case == "unknown_source_id":
        source_id = "legacy-seed-does-not-exist"

    imported = _import(service, graph)
    signal = _migration_signal(
        service,
        imported,
        graph,
        text=text,
        source_id=source_id,
        evidence_overrides=evidence_overrides,
    )
    preview = service.preview_resolution(signal["signal_id"])
    assert preview["state"] == "review_required"
    assert preview["resolution_path"] == "exact_graph_migration"
    assert expected_error in preview["validation_errors"]
    assert preview["provider_call_performed"] is False

    result = service.resolve_signal(signal["signal_id"], use_ai=False)
    assert result["state"] == "review_required"
    assert result["requires_human_review"] is True
    assert result["ai_evaluated"] is False
    assert result["generation_authorized"] is False
    assert result["publishing_authorized"] is False
    assert expected_error in result["validation_errors"]
    assert result["binding"]["decision"] == "review_required"
    assert result["binding"]["reviewer_type"] == "rules"
    assert result["binding"]["observation"] is None

    repeated = service.resolve_signal(signal["signal_id"], use_ai=False)
    assert repeated["binding"]["binding_id"] == result["binding"]["binding_id"]
    assert repeated["binding"]["idempotent"] is True
    with store.connect() as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM mt_topic_observations"
        ).fetchone()[0] == 0


def test_ai_provider_is_bounded_review_only_and_out_of_scope_is_not_gamed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    openai_contract_server: str,
):
    store = MarketTapeStore(_config(tmp_path))
    service = SemanticTopicService(store)
    imported = _import(service)
    graph_id = imported["graph"]["graph_version_id"]
    candidate = _signal(
        service,
        graph_id,
        "avatar video audience retention",
        "ambiguous-signal",
    )
    monkeypatch.setenv("OPENAI_API_KEY", "test-semantic-key")
    monkeypatch.setenv("OPENAI_API_BASE_URL", openai_contract_server)

    result = service.resolve_signal(
        candidate["signal_id"], use_ai=True, max_candidates=4
    )

    assert result["state"] == "review_required"
    assert result["requires_human_review"] is True
    assert result["generation_authorized"] is False
    assert result["proposed_node_authorized"] is False
    assert result["binding"]["decision"] == "review_required"
    assert result["binding"]["reviewer_type"] == "ai"
    assert result["binding"]["observation"] is None
    assert result["resolution_run"]["model_version"] == "gpt-5-nano"
    assert result["resolution_run"]["input_tokens"] == 31
    assert result["resolution_run"]["output_tokens"] == 17
    assert result["resolution_run"]["total_tokens"] == 48
    request_body = _OpenAIResponseHandler.request_body
    assert request_body["model"] == "gpt-5-nano"
    assert request_body["max_output_tokens"] == 600
    assert request_body["reasoning"] == {"effort": "minimal"}
    assert request_body["store"] is False
    assert request_body["text"]["format"]["strict"] is True
    assert request_body["text"]["format"]["type"] == "json_schema"

    with pytest.raises(SemanticContractError, match="AI output may only"):
        service.record_binding({
            "signal_id": candidate["signal_id"],
            "topic_id": "topic.avatar-test",
            "decision": "approved",
            "reviewer_type": "ai",
            "reviewed_by": "gpt-5-nano",
            "source_receipt_id": candidate["source_receipt_id"],
            "review_receipt_id": "receipt:invalid-ai-approval",
            "confidence": 0.99,
            "rationale": "AI must not approve this binding.",
            "binding_method": "invalid-ai-self-approval",
        })

    entertainment = _signal(service, graph_id, "GTA 6", "gta-6-signal")
    with pytest.raises(SemanticContractError, match="requires human review"):
        service.record_binding({
            "signal_id": entertainment["signal_id"],
            "decision": "out_of_scope",
            "reviewer_type": "rules",
            "reviewed_by": "rules.semantic-v1",
            "source_receipt_id": entertainment["source_receipt_id"],
            "review_receipt_id": "receipt:rules-out-of-scope",
            "exclusion_reason": "Unrelated entertainment demand.",
            "confidence": 1.0,
            "rationale": "The signal is not part of the current brand graph.",
            "binding_method": "rules",
        })
    excluded = service.record_binding({
        "signal_id": entertainment["signal_id"],
        "decision": "out_of_scope",
        "reviewer_type": "human",
        "reviewed_by": "reviewer.test-owner",
        "source_receipt_id": entertainment["source_receipt_id"],
        "review_receipt_id": "receipt:human-out-of-scope",
        "exclusion_reason": "Unrelated entertainment demand.",
        "confidence": 1.0,
        "rationale": "The reviewed signal is outside the current brand graph.",
        "binding_method": "human_scope_review",
    })
    assert excluded["review_state"] == "reviewed_out_of_scope"

    health = service.mapping_health(graph_version_id=graph_id)
    assert health["all_candidates"] == {
        "total": 2,
        "dispositioned": 1,
        "disposition_coverage": 0.5,
    }
    assert health["in_scope_candidates"]["total"] == 1
    assert health["in_scope_candidates"]["unresolved_in_scope"] == 1
    assert health["in_scope_candidates"]["review_required"] == 1
    assert health["in_scope_candidates"]["mapping_coverage"] == 0.0
    assert health["reviewed_out_of_scope"] == {
        "count": 1,
        "reasons": [{"reason": "Unrelated entertainment demand.", "count": 1}],
        "receipt_complete": True,
    }


def test_atomic_selection_evidence_gates_and_foundry_registration_are_durable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    graph = json.loads(FOUNDRY_GRAPH_PATH.read_text(encoding="utf-8"))
    ontology = ContentOntology(graph)
    store = MarketTapeStore(_config(tmp_path))
    service = SemanticTopicService(store)
    imported = _import(service, graph)
    graph_id = imported["graph"]["graph_version_id"]
    atomic_topic_id = (
        "atomic_subject.reducing-first-two-second-viewer-drop-off-in-ai-avatar-videos"
    )
    topic = ontology.ancestor(atomic_topic_id, "topic")
    signal = _signal(service, graph_id, topic.name, "avatar-topic-demand")
    resolved = service.resolve_signal(signal["signal_id"], use_ai=False)
    binding = resolved["binding"]
    selection_payload = {
        "graph_version_id": graph_id,
        "atomic_topic_id": atomic_topic_id,
        "binding_ids": [binding["binding_id"]],
        "reviewer_type": "human",
        "reviewer_id": "reviewer.test-owner",
        "reviewed_at": "2026-08-28T18:15:00Z",
        "review_receipt_id": "receipt.atomic.review.1",
        "rationale": "The broad avatar-video signal supports a separately reviewed retention problem.",
    }

    with pytest.raises(SemanticContractError, match="cannot be performed or approved by AI"):
        service.record_atomic_selection({
            **selection_payload,
            "reviewer_type": "ai",
            "reviewer_id": "gpt.5.nano",
        })

    selected = service.record_atomic_selection(selection_payload)
    selection = selected["selection"]
    assert binding["topic_id"] == "topic.ai-avatar-videos"
    assert selection["atomic_topic_id"] == atomic_topic_id
    assert selected["selection_approved"] is True
    assert selected["generation_authorized"] is False
    assert selected["generation_handoff_ready"] is False
    assert selected["ai_selected"] is False

    with pytest.raises(
        SemanticContractError,
        match="selection sourced only from software_repository_change",
    ):
        _evidence(
            service,
            selection["selection_id"],
            "software_change_receipt",
            3,
        )
    with pytest.raises(SemanticContractError, match="transcript receipt"):
        service.generation_handoff(selection["selection_id"])
    _evidence(service, selection["selection_id"], "transcript_receipt", 1)
    with pytest.raises(SemanticContractError, match="human moment"):
        service.generation_handoff(selection["selection_id"])
    _evidence(service, selection["selection_id"], "human_moment", 2)

    handoff = service.generation_handoff(selection["selection_id"])
    assert handoff["state"] == "ready"
    assert handoff["ready_for_foundry_plan_request"] is True
    assert handoff["generation_authorized_by_ai"] is False
    assert "source_policy" not in handoff
    assert "source_policy" not in handoff["plan_request_base"]
    assert "source_kind" not in handoff["plan_request_base"]["topic_bindings"][0]
    request = {**handoff["plan_request_base"], "content_spec": _content_spec()}
    foundry_plan = build_semantic_trend_content_plan(ontology, request)
    registration = foundry_plan["persistence_registration"]
    write = {
        "source_service": "marketing-video-foundry",
        "source_receipt_id": "foundry:semantic-focused-test",
        "registered_at": "2026-08-28T18:30:00Z",
        "registration": registration,
    }

    first = service.register_content_lineage(write)
    second = service.register_content_lineage(write)
    later_retry = deepcopy(write)
    later_retry["registered_at"] = "2026-08-28T18:31:00Z"
    third = service.register_content_lineage(later_retry)

    assert first["created"] is True
    assert second["created"] is False
    assert second["idempotent"] is True
    assert third["created"] is False
    assert third["idempotent"] is True
    assert first["registration_id"] == registration["registration_id"]
    assert first["content_id"] == registration["identifiers"]["content_id"]
    with store.connect() as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM mt_semantic_lineage_registrations"
        ).fetchone()[0] == 1
        assert connection.execute(
            "SELECT COUNT(*) FROM mt_content_briefs"
        ).fetchone()[0] == 1
        assert connection.execute(
            "SELECT COUNT(*) FROM mt_content_assets"
        ).fetchone()[0] == 2
        assert connection.execute(
            "SELECT COUNT(*) FROM mt_semantic_content_lineage"
        ).fetchone()[0] == 2

    tampered = deepcopy(write)
    tampered["registration"]["topic_bindings"][0]["review_status"] = "pending"
    with pytest.raises(SemanticContractError, match="durable Market Tape lineage"):
        service.register_content_lineage(tampered)

    empty_owned_db = tmp_path / "content-quality.sqlite3"
    with sqlite3.connect(empty_owned_db) as connection:
        connection.executescript(
            """
            CREATE TABLE cq_owned_outcome_events (
                event_id TEXT PRIMARY KEY, event_type TEXT NOT NULL,
                content_id TEXT NOT NULL, campaign_id TEXT NOT NULL,
                offer_id TEXT NOT NULL, source_platform TEXT NOT NULL,
                source_id TEXT NOT NULL, journey_id TEXT NOT NULL,
                occurred_at TEXT NOT NULL
            );
            CREATE TABLE cq_owned_retention_samples (
                sample_id TEXT PRIMARY KEY, content_id TEXT NOT NULL,
                measurement_id TEXT NOT NULL, elapsed_ms INTEGER NOT NULL,
                observed_at TEXT NOT NULL
            );
            """
        )
    monkeypatch.setenv("CONTENT_QUALITY_DB", str(empty_owned_db))
    lineage = service.lineage(content_id=first["content_id"])
    assert lineage["count"] == 1
    assert lineage["bindings"][0]["topic_id"] == "topic.ai-avatar-videos"
    assert lineage["atomic_topic_selections"][0]["atomic_topic_id"] == atomic_topic_id
    assert lineage["atomic_topic_selections"][0]["generation_handoff_ready"] is True
    assert lineage["owned_outcomes"]["state"] == "no_owned_outcomes"
    assert lineage["owned_outcomes"]["attribution_readiness"] == "no_owned_outcomes"

    content_id = first["content_id"]
    with sqlite3.connect(empty_owned_db) as connection:
        connection.executemany(
            """INSERT INTO cq_owned_outcome_events (
                   event_id, event_type, content_id, campaign_id, offer_id,
                   source_platform, source_id, journey_id, occurred_at
               ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            [
                (
                    "event:click",
                    "click",
                    content_id,
                    "campaign:semantic",
                    "offer:creator-system",
                    "instagram",
                    "post:semantic-parent",
                    "journey:one",
                    "2026-08-28T18:40:00Z",
                ),
                (
                    "event:install",
                    "install",
                    content_id,
                    "campaign:semantic",
                    "offer:creator-system",
                    "instagram",
                    "post:semantic-parent",
                    "journey:one",
                    "2026-08-28T18:41:00Z",
                ),
                (
                    "event:trial",
                    "trial",
                    content_id,
                    "campaign:semantic",
                    "offer:creator-system",
                    "instagram",
                    "post:semantic-parent",
                    "journey:one",
                    "2026-08-28T18:42:00Z",
                ),
                (
                    "event:purchase",
                    "purchase",
                    content_id,
                    "campaign:semantic",
                    "offer:creator-system",
                    "instagram",
                    "post:semantic-parent",
                    "journey:one",
                    "2026-08-28T18:43:00Z",
                ),
            ],
        )
        connection.executemany(
            """INSERT INTO cq_owned_retention_samples (
                   sample_id, content_id, measurement_id, elapsed_ms, observed_at
               ) VALUES (?, ?, ?, ?, ?)""",
            [
                (
                    "retention:zero",
                    content_id,
                    "measurement:one",
                    0,
                    "2026-08-28T18:44:00Z",
                ),
                (
                    "retention:two-seconds",
                    content_id,
                    "measurement:one",
                    2000,
                    "2026-08-28T18:44:00Z",
                ),
            ],
        )

    attributed = service.lineage(content_id=content_id)["owned_outcomes"]
    assert attributed["state"] == "ready"
    assert attributed["event_count"] == 4
    assert attributed["retention_sample_count"] == 2
    assert attributed["complete_ordered_exact_scope_journeys"] == 1
    assert attributed["attribution_readiness"] == "outcome_and_retention_ready"
    assert attributed["causal_effect"] is None


def test_fresh_software_change_handoff_uses_repository_receipt_without_transcript(
    tmp_path: Path,
):
    store = MarketTapeStore(_config(tmp_path))
    service = SemanticTopicService(store)
    imported = _import(service)
    graph_id = imported["graph"]["graph_version_id"]
    signal = _signal(
        service,
        graph_id,
        "AI avatar",
        "storyrail-current-commit",
        source_kind="software_repository_change",
    )
    binding = service.resolve_signal(
        signal["signal_id"], use_ai=False
    )["binding"]

    def select(ordinal: int) -> dict:
        return service.record_atomic_selection({
            "graph_version_id": graph_id,
            "atomic_topic_id": "atomic_subject.avatar-test",
            "binding_ids": [binding["binding_id"]],
            "reviewer_type": "human",
            "reviewer_id": "reviewer.test-owner",
            "reviewed_at": f"2026-08-28T19:0{ordinal}:00Z",
            "review_receipt_id": f"receipt.software.review.{ordinal}",
            "rationale": "The current code change supports this reviewed subject.",
        })["selection"]

    selection = select(1)
    with pytest.raises(
        SemanticContractError, match="software_change_receipt requires a claim"
    ):
        service.record_evidence_receipt({
            "selection_id": selection["selection_id"],
            "evidence_type": "software_change_receipt",
            "status": "verified",
            "source_system": "github",
            "source_record_id": "commit.current.1",
            "source_record_sha256": "1" * 64,
            "source_uri": "https://github.com/IsaiahDupree/storyrail/commit/one",
        })
    with pytest.raises(SemanticContractError, match="software_change_receipt"):
        service.generation_handoff(selection["selection_id"])
    software_receipt = _evidence(
        service, selection["selection_id"], "software_change_receipt", 1
    )["receipt"]
    with pytest.raises(SemanticContractError, match="human moment"):
        service.generation_handoff(selection["selection_id"])
    _evidence(service, selection["selection_id"], "human_moment", 2)

    handoff = service.generation_handoff(selection["selection_id"])

    assert handoff["state"] == "ready"
    assert handoff["source_policy"] == "fresh_software_evidence_only_v1"
    assert (
        handoff["plan_request_base"]["source_policy"]
        == "fresh_software_evidence_only_v1"
    )
    assert handoff["plan_request_base"]["topic_bindings"][0]["source_kind"] == (
        "software_repository_change"
    )
    assert software_receipt["evidence_type"] == "software_change_receipt"
    assert {
        receipt["evidence_type"]
        for receipt in handoff["plan_request_base"]["evidence_receipts"]
    } == {"software_change_receipt", "human_moment"}
    health = service.mapping_health(graph_version_id=graph_id)
    assert health["atomic_selection_reviews"]["generation_handoff_ready"] == 1

    _evidence(service, selection["selection_id"], "transcript_receipt", 3)
    with pytest.raises(
        SemanticContractError, match="cannot include transcript receipts"
    ):
        service.generation_handoff(selection["selection_id"])

    external_selection = select(2)
    _evidence(
        service,
        external_selection["selection_id"],
        "software_change_receipt",
        4,
    )
    _evidence(service, external_selection["selection_id"], "human_moment", 5)
    _evidence(
        service,
        external_selection["selection_id"],
        "external_reference",
        6,
    )
    with pytest.raises(
        SemanticContractError, match="external references cannot authorize"
    ):
        service.generation_handoff(external_selection["selection_id"])
    _evidence(service, external_selection["selection_id"], "human_moment", 7)
    with pytest.raises(SemanticContractError, match="exactly one human moment"):
        service.generation_handoff(external_selection["selection_id"])


def test_semantic_read_apis_are_stable_and_writes_require_token(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    config = _config(tmp_path)
    monkeypatch.setenv("MARKET_TAPE_CONTROL_TOKEN", "semantic-control-token")
    app = Flask("market-tape-semantic-api-test")
    register_market_tape_routes(app, config)
    client = app.test_client()
    graph_payload = {
        "contract": GRAPH_IMPORT_CONTRACT,
        "source_service": "semantic-api-test",
        "source_receipt_id": "receipt:api-graph",
        "imported_by": "reviewer.test-owner",
        "graph": _graph(),
    }

    no_graph = client.get("/api/market-tape/semantic/graph-summary")
    assert no_graph.status_code == 200
    assert no_graph.get_json()["state"] == "no_graph"
    denied = client.post(
        "/api/market-tape/semantic/graphs/import", json=graph_payload
    )
    assert denied.status_code == 401
    accepted = client.post(
        "/api/market-tape/semantic/graphs/import",
        json=graph_payload,
        headers={"Authorization": "Bearer semantic-control-token"},
    )
    assert accepted.status_code == 201
    graph_id = accepted.get_json()["graph"]["graph_version_id"]

    summary = client.get(
        "/api/market-tape/semantic/graph-summary",
        query_string={"graph_version_id": graph_id},
    )
    health = client.get(
        "/api/market-tape/semantic/mapping-health",
        query_string={"graph_version_id": graph_id},
    )
    bad_lineage = client.get("/api/market-tape/semantic/lineage")
    assert summary.status_code == 200
    assert summary.get_json()["contract"] == "market_tape_semantic_graph_summary_v1"
    assert health.status_code == 200
    assert health.get_json()["contract"] == "market_tape_semantic_mapping_health_v1"
    assert bad_lineage.status_code == 400
    assert "exactly one" in bad_lineage.get_json()["error"]


def test_semantic_cli_defaults_to_dry_run_and_apply_is_explicit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
):
    graph_path = tmp_path / "topic-graph.json"
    graph_path.write_text(json.dumps(_graph()), encoding="utf-8")
    db_path = tmp_path / "cli-market.sqlite3"
    environment = {
        "MARKET_TAPE_DB_PATH": str(db_path),
        "MARKET_TAPE_OBJECT_DIR": str(tmp_path / "cli-objects"),
        "MARKET_TAPE_HEARTBEAT_PATH": str(tmp_path / "cli-heartbeat.json"),
        "MARKET_TAPE_LOCK_PATH": str(tmp_path / "cli.lock"),
        "MARKET_TAPE_LOCAL_RESEARCH_STATE_PATH": str(tmp_path / "cli-state.json"),
        "MARKET_TAPE_PREDICTION_MODEL_DIR": str(tmp_path / "cli-models"),
        "MARKET_TAPE_SUPABASE_SYNC_ENABLED": "false",
    }
    for name, value in environment.items():
        monkeypatch.setenv(name, value)
    base_args = [
        "market-tape",
        "semantic-graph-import",
        "--path",
        str(graph_path),
        "--source-service",
        "semantic-cli-test",
        "--source-receipt-id",
        "receipt:cli-graph",
        "--imported-by",
        "reviewer.test-owner",
    ]

    monkeypatch.setattr(sys, "argv", base_args)
    assert market_tape_cli() == 0
    dry_run = json.loads(capsys.readouterr().out)
    assert dry_run["dry_run"] is True
    assert dry_run["mutation_applied"] is False
    with sqlite3.connect(db_path) as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM mt_topic_graph_versions"
        ).fetchone()[0] == 0

    monkeypatch.setattr(sys, "argv", [*base_args, "--apply"])
    assert market_tape_cli() == 0
    applied = json.loads(capsys.readouterr().out)
    graph_id = applied["graph"]["graph_version_id"]
    assert applied["imported"] is True
    store = MarketTapeStore(MarketTapeConfig.from_environment())
    service = SemanticTopicService(store)
    signal = _signal(service, graph_id, "AI avatar", "cli-alias-signal")

    resolve_args = [
        "market-tape",
        "semantic-resolve",
        "--signal-id",
        signal["signal_id"],
        "--ai",
    ]
    monkeypatch.setattr(sys, "argv", resolve_args)
    assert market_tape_cli() == 0
    preview = json.loads(capsys.readouterr().out)
    assert preview["dry_run"] is True
    assert preview["provider_call_performed"] is False
    assert preview["state"] == "deterministic_match"
    with store.connect() as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM mt_topic_resolution_runs"
        ).fetchone()[0] == 0
        assert connection.execute(
            "SELECT COUNT(*) FROM mt_topic_signal_bindings"
        ).fetchone()[0] == 0

    monkeypatch.setattr(sys, "argv", [*resolve_args, "--apply"])
    assert market_tape_cli() == 0
    resolved = json.loads(capsys.readouterr().out)
    assert resolved["state"] == "resolved_deterministically"
    assert resolved["binding"]["decision"] == "approved"


def test_v15_upgrade_preserves_existing_rows_and_self_heals_semantic_tables(
    tmp_path: Path,
):
    config = _config(tmp_path, "upgrade.sqlite3")
    config.db_path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(config.db_path) as connection:
        connection.executescript(
            """
            CREATE TABLE mt_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
            INSERT INTO mt_meta(key, value) VALUES('schema_version', '15');
            CREATE TABLE pre_upgrade_sentinel (
                sentinel_id TEXT PRIMARY KEY,
                payload TEXT NOT NULL
            );
            INSERT INTO pre_upgrade_sentinel(sentinel_id, payload)
            VALUES('sentinel-1', 'must survive');
            """
        )

    store = MarketTapeStore(config)

    with store.connect() as connection:
        assert connection.execute(
            "SELECT value FROM mt_meta WHERE key='schema_version'"
        ).fetchone()[0] == "17"
        assert connection.execute(
            "SELECT payload FROM pre_upgrade_sentinel WHERE sentinel_id='sentinel-1'"
        ).fetchone()[0] == "must survive"
        tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
        assert {
            "mt_topic_graph_versions",
            "mt_topic_nodes",
            "mt_topic_edges",
            "mt_topic_signal_candidates",
            "mt_topic_signal_bindings",
            "mt_topic_resolution_runs",
            "mt_topic_observations",
            "mt_atomic_topic_selections",
            "mt_atomic_topic_selection_sources",
            "mt_content_evidence_receipts",
            "mt_semantic_lineage_registrations",
            "mt_content_briefs",
            "mt_content_assets",
            "mt_semantic_content_lineage",
        }.issubset(tables)
        assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"


def test_v16_semantic_enum_upgrade_archives_and_preserves_existing_lineage(
    tmp_path: Path,
):
    config = _config(tmp_path, "semantic-v16-upgrade.sqlite3")
    store = MarketTapeStore(config)
    service = SemanticTopicService(store)
    imported, _signal_row, binding = _approved_binding(service)
    selection = service.record_atomic_selection({
        "graph_version_id": imported["graph"]["graph_version_id"],
        "atomic_topic_id": "atomic_subject.avatar-test",
        "binding_ids": [binding["binding_id"]],
        "reviewer_type": "human",
        "reviewer_id": "reviewer.test-owner",
        "reviewed_at": "2026-08-28T20:00:00Z",
        "review_receipt_id": "receipt.semantic-v16-upgrade",
        "rationale": "Seed durable lineage before the enum-only upgrade.",
    })["selection"]
    _evidence(service, selection["selection_id"], "transcript_receipt", 1)
    _evidence(service, selection["selection_id"], "human_moment", 2)

    with sqlite3.connect(config.db_path) as connection:
        connection.execute("PRAGMA foreign_keys = OFF")
        connection.execute("PRAGMA legacy_alter_table = ON")
        source_sql = connection.execute(
            """SELECT sql FROM sqlite_master
               WHERE type='table' AND name='mt_topic_signal_candidates'"""
        ).fetchone()[0]
        receipt_sql = connection.execute(
            """SELECT sql FROM sqlite_master
               WHERE type='table' AND name='mt_content_evidence_receipts'"""
        ).fetchone()[0]
        source_columns = ", ".join(
            row[1]
            for row in connection.execute(
                "PRAGMA table_info(mt_topic_signal_candidates)"
            )
        )
        receipt_columns = ", ".join(
            row[1]
            for row in connection.execute(
                "PRAGMA table_info(mt_content_evidence_receipts)"
            )
        )
        connection.execute(
            """ALTER TABLE mt_topic_signal_candidates
               RENAME TO mt_topic_signal_candidates_v17_seed"""
        )
        connection.execute(
            re.sub(r",\s*'software_repository_change'", "", source_sql)
        )
        connection.execute(
            f"""INSERT INTO mt_topic_signal_candidates({source_columns})
                SELECT {source_columns}
                FROM mt_topic_signal_candidates_v17_seed"""
        )
        connection.execute(
            """ALTER TABLE mt_content_evidence_receipts
               RENAME TO mt_content_evidence_receipts_v17_seed"""
        )
        connection.execute(
            re.sub(r",\s*'software_change_receipt'", "", receipt_sql)
        )
        connection.execute(
            f"""INSERT INTO mt_content_evidence_receipts({receipt_columns})
                SELECT {receipt_columns}
                FROM mt_content_evidence_receipts_v17_seed"""
        )
        connection.commit()

    upgraded = MarketTapeStore(config)
    with upgraded.connect() as connection:
        source_table_sql = connection.execute(
            """SELECT sql FROM sqlite_master
               WHERE type='table' AND name='mt_topic_signal_candidates'"""
        ).fetchone()[0]
        receipt_table_sql = connection.execute(
            """SELECT sql FROM sqlite_master
               WHERE type='table' AND name='mt_content_evidence_receipts'"""
        ).fetchone()[0]
        assert "software_repository_change" in source_table_sql
        assert "software_change_receipt" in receipt_table_sql
        assert connection.execute(
            "SELECT COUNT(*) FROM mt_topic_signal_candidates"
        ).fetchone()[0] == 1
        assert connection.execute(
            "SELECT COUNT(*) FROM mt_topic_signal_candidates_v16_archive"
        ).fetchone()[0] == 1
        assert connection.execute(
            "SELECT COUNT(*) FROM mt_content_evidence_receipts"
        ).fetchone()[0] == 2
        assert connection.execute(
            "SELECT COUNT(*) FROM mt_content_evidence_receipts_v16_archive"
        ).fetchone()[0] == 2
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
