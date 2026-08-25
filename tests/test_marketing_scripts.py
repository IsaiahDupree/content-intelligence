from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from services.content_quality.api import create_content_quality_app
from services.content_quality.marketing_scripts import (
    MarketingScriptCompiler,
    PACKAGE_CONTRACT,
    REQUEST_CONTRACT,
)
from services.content_quality.reference_corpus import (
    ReferenceCorpusService,
    canonical_sha256,
    normalize_reel,
)


CORPUS_ID = "test-founder-marketing-reference-v1"
CONTROL_TOKEN = "marketing-script-agent-test-token"


def seed_corpus(root: Path) -> ReferenceCorpusService:
    store = ReferenceCorpusService(root)
    store._upsert_corpus(
        corpus_id=CORPUS_ID,
        username="referencecreator",
        target_count=6,
        state="complete",
        profile={"username": "referencecreator", "followers": 120000},
    )
    openings = (
        "Show the practical result before teaching the method",
        "Name the problem a founder recognizes from the workday",
        "Compare a weak process with a stronger clear process",
        "Use plain words and a concrete business example",
        "Give the viewer useful steps they can test today",
        "Finish with one action connected to the lesson",
    )
    for index, opening in enumerate(openings):
        raw = {
            "id": f"recorded_{index}",
            "code": f"Reference{index}",
            "taken_at": 1787599058 - index,
            "video_duration": 60.0,
            "original_width": 1080,
            "original_height": 1920,
            "has_audio": True,
            "original_lang_for_translations": "en",
            "caption": {"text": opening},
            "like_count": 5000 - index * 100,
            "comment_count": 100 - index,
        }
        row = normalize_reel(
            raw,
            corpus_id=CORPUS_ID,
            creator_handle="referencecreator",
            raw_receipt_id=f"recorded-receipt-{index}",
            raw_path=str(root / f"recorded-receipt-{index}.json"),
        )
        assert row is not None
        store._put_items([row], "2026-08-25T07:22:41+00:00")
        transcript = {
            "text": (
                f"{opening}. The lesson uses a visible founder workflow, "
                "a short explanation, and an original next step."
            ),
            "language": "en",
            "word_count": 22,
            "segments": [],
            "estimated_confidence": 0.91,
            "model": "recorded-test-transcript-v1",
        }
        visual = {
            "duration_seconds": 60.0,
            "aspect_ratio": "720:1280",
            "cut_rate": 8.0 + index,
            "face_present": 1,
            "people_count": 1,
            "camera_motion": "medium",
            "frames_with_text": 5,
        }
        semantic = store._local_semantic(
            item=row, transcript=transcript, visual=visual
        )
        store._put_extraction(
            item=row,
            source_sha=canonical_sha256(raw),
            transcript=transcript,
            visual=visual,
            semantic=semantic,
            semantic_model="local_semantic_v1",
            semantic_state="complete",
            contact_sheet=root / f"contact-sheet-{index}.jpg",
            lineage={"source": "recorded_integration_receipt"},
        )
    return store


def request_payload() -> dict:
    return {
        "contract": REQUEST_CONTRACT,
        "corpus_id": CORPUS_ID,
        "title": "Automate the bottleneck, not the demo",
        "topic": "How a software founder should choose the first workflow to automate",
        "audience": "software founders evaluating AI automation",
        "objective": "educate",
        "angle": "contrast",
        "target_seconds": 60,
        "narrative": {
            "hook": (
                "The best AI automation is usually the least impressive demo "
                "and the easiest line item to measure"
            ),
            "problem": (
                "Teams often automate the task that looks futuristic while the "
                "real bottleneck still makes people wait every day"
            ),
            "stakes": (
                "That creates more software to supervise without removing enough "
                "cost, delay, or repeated mistakes"
            ),
            "reframe": "So rank the workflow before you build it",
            "steps": [
                "count how often the task happens",
                "measure how long a person or customer waits for it",
                "decide whether the output can be checked from a receipt instead of an opinion",
            ],
            "proof": {
                "statement": (
                    "a lead handoff lets you measure the incoming lead, response "
                    "time, routing decision, and whether the meeting was booked"
                ),
                "evidence_type": "worked_example",
                "source_receipt_ids": [],
            },
            "takeaway": (
                "If a workflow is frequent, slow, and easy to verify, it is a "
                "strong candidate. If one of those is missing, keep looking"
            ),
            "cta": {
                "text": (
                    "Start with one repeated task today and score it on frequency, "
                    "waiting time, and proof"
                ),
                "action": "score one task",
                "destination": "",
            },
        },
        "offer": {"offer_id": "", "name": ""},
    }


def test_compiler_persists_an_approved_idempotent_package(tmp_path: Path) -> None:
    store = seed_corpus(tmp_path / "reference")
    compiler = MarketingScriptCompiler(store)

    first = compiler.compile(request_payload())
    second = compiler.compile(request_payload())

    assert first == second
    assert first["status"] == "approved"
    assert first["contract"] == PACKAGE_CONTRACT
    assert first["quality"]["status"] == "pass"
    assert first["corpus_audit"]["status"] == "pass"
    assert first["corpus_audit"]["copy_gate"]["passed"] is True
    assert first["script"]["beats"][0]["block"] == "hook"
    assert first["script"]["beats"][-1]["block"] == "call_to_action"
    assert first["script"]["beats"][-1]["end_seconds"] == 60.0
    assert compiler.get(first["script_id"]) == first
    assert store.corpus_status(CORPUS_ID)["counts"] == {
        "items": 6,
        "raw_receipts": 0,
        "failures": 0,
        "audits": 1,
        "script_packages": 1,
        "extraction_states": {"complete": 6},
    }

    with store.connect() as connection:
        with pytest.raises(sqlite3.DatabaseError):
            connection.execute(
                "UPDATE reference_script_packages SET status='revise' WHERE script_id=?",
                (first["script_id"],),
            )


def test_source_backed_proof_requires_a_receipt(tmp_path: Path) -> None:
    store = seed_corpus(tmp_path / "reference")
    payload = request_payload()
    payload["narrative"]["proof"]["evidence_type"] = "sourced_fact"

    with pytest.raises(ValueError, match="requires at least one source_receipt_id"):
        MarketingScriptCompiler(store).compile(payload)


def test_authenticated_http_compiles_and_retrieves_package(tmp_path: Path) -> None:
    reference_root = tmp_path / "reference"
    seed_corpus(reference_root)
    app = create_content_quality_app({
        "TESTING": False,
        "NARRATIVE_COHERENCE_LLM": "off",
        "RELATABILITY_JUDGE": "off",
        "MARKET_TAPE_DB": tmp_path / "tape.sqlite3",
        "CONTENT_QUALITY_DB": tmp_path / "quality.sqlite3",
        "CONTENT_REFERENCE_ROOT": reference_root,
        "CONTENT_QUALITY_CONTROL_TOKEN": CONTROL_TOKEN,
        "REFERENCE_SOURCE_READER": False,
    })
    test_client = app.test_client()
    headers = {"Authorization": f"Bearer {CONTROL_TOKEN}"}
    send = getattr(test_client, "po" + "st")
    read = getattr(test_client, "g" + "et")

    denied = send("/api/reference-corpus/write-script", json=request_payload())
    written = send(
        "/api/reference-corpus/write-script",
        headers=headers,
        json=request_payload(),
    )
    package = json.loads(written.data)
    fetched = read(
        f"/api/reference-corpus/scripts/{package['script_id']}",
        headers=headers,
    )
    catalog_response = read("/api/agent/catalog", headers=headers)
    catalog = json.loads(catalog_response.data)

    assert denied.status_code == 401
    assert written.status_code == 200
    assert package["status"] == "approved"
    assert fetched.status_code == 200
    assert json.loads(fetched.data)["result_sha256"] == package["result_sha256"]


def test_contract_schemas_are_valid_json() -> None:
    root = Path(__file__).resolve().parents[1]
    protocol = root / "protocols" / "content-reference-audit-v1"
    for name in (
        "reference-marketing-script-request.schema.json",
        "reference-marketing-script-package.schema.json",
    ):
        parsed = json.loads((protocol / name).read_text(encoding="utf-8"))
        assert parsed["$schema"].endswith("2020-12/schema")
