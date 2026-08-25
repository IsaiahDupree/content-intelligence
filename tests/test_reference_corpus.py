from __future__ import annotations

import zipfile
from pathlib import Path

from services.content_quality.api import create_content_quality_app
from services.content_quality.reference_corpus import (
    SOURCE_RIGHTS_STATE,
    ReferenceCorpusService,
    canonical_sha256,
    normalize_reel,
)


CORPUS_ID = "instagram-personalbrandlaunch-reference-v1"
CONTROL_TOKEN = "reference-corpus-agent-test-token"


def recorded_reel() -> dict:
    """Subset of public provider receipt captured on 2026-08-25."""
    return {
        "id": "3970990361978513466_56413349678",
        "code": "DcbyrHsOqg6",
        "taken_at": 1787599058,
        "video_duration": 31.833,
        "original_width": 1080,
        "original_height": 1920,
        "has_audio": True,
        "original_lang_for_translations": "en",
        "caption": {"text": "Boring vs. Fun and Engaging Hook"},
        "play_count": 34446,
        "like_count": 595,
        "comment_count": 4,
    }


def seed_service(root: Path) -> ReferenceCorpusService:
    store = ReferenceCorpusService(root)
    store._upsert_corpus(
        corpus_id=CORPUS_ID,
        username="personalbrandlaunch",
        target_count=1,
        state="acquired",
        profile={"username": "personalbrandlaunch", "followers": 1094405},
    )
    row = normalize_reel(
        recorded_reel(),
        corpus_id=CORPUS_ID,
        creator_handle="personalbrandlaunch",
        raw_receipt_id="recorded-live-receipt",
        raw_path=str(root / "recorded-live-receipt.json"),
    )
    assert row is not None
    store._put_items([row], "2026-08-25T07:22:41+00:00")
    transcript = {
        "text": (
            "This is a boring hook and this is a fun and engaging hook. "
            "They show the end result before the step by step lesson."
        ),
        "language": "en",
        "word_count": 25,
        "segments": [],
        "estimated_confidence": 0.91,
        "model": "base.en",
    }
    visual = {
        "duration_seconds": 31.948,
        "aspect_ratio": "720:1280",
        "cut_rate": 22.537,
        "face_present": 1,
        "people_count": 2,
        "camera_motion": "high",
        "frames_with_text": 6,
    }
    semantic = store._local_semantic(
        item=row, transcript=transcript, visual=visual
    )
    store._put_extraction(
        item=row,
        source_sha=canonical_sha256(recorded_reel()),
        transcript=transcript,
        visual=visual,
        semantic=semantic,
        semantic_model="local_semantic_v1",
        semantic_state="complete",
        contact_sheet=root / "recorded-contact-sheet.jpg",
        lineage={"source": "recorded_live_receipt"},
    )
    return store


def test_recorded_reel_normalizes_with_reference_only_rights(tmp_path):
    row = normalize_reel(
        recorded_reel(),
        corpus_id=CORPUS_ID,
        creator_handle="personalbrandlaunch",
        raw_receipt_id="recorded-live-receipt",
        raw_path=str(tmp_path / "recorded-live-receipt.json"),
    )

    assert row is not None
    assert row["shortcode"] == "DcbyrHsOqg6"
    assert row["metrics"] == {"views": 34446, "likes": 595, "comments": 4}
    assert row["rights_state"] == SOURCE_RIGHTS_STATE
    assert row["direct_use_allowed"] is False


def test_corpus_status_find_summary_and_copy_gate_are_durable(tmp_path):
    store = seed_service(tmp_path)

    status = store.corpus_status(CORPUS_ID)
    found = store.find_items(
        corpus_id=CORPUS_ID, query="engaging hook end result", limit=3
    )
    summary = store.summarize(CORPUS_ID)
    context = store.agent_context(
        corpus_id=CORPUS_ID,
        query="engaging hook with a visible result",
        evidence_limit=3,
    )
    audit = store.audit_content(
        corpus_id=CORPUS_ID,
        title="Hook lesson",
        script=(
            "This is a boring hook and this is a fun and engaging hook. "
            "Now compare the result and follow for the next lesson."
        ),
        objective="teach hook construction",
        target_viewer="business owners",
        target_seconds=20,
    )

    assert status["counts"]["items"] == 1
    assert status["counts"]["extraction_states"] == {"complete": 1}
    assert found[0]["source_url"].endswith("/DcbyrHsOqg6/")
    assert summary["coverage"]["transcript_count"] == 1
    assert context["contract"] == "content_reference_agent_context_v1"
    assert context["coverage"]["item_count"] == 1
    assert context["evidence"][0]["item_id"] == found[0]["item_id"]
    assert len(context["result_sha256"]) == 64
    assert audit["copy_gate"]["passed"] is False
    assert audit["rights"]["direct_use_allowed"] is False
    assert store.corpus_status(CORPUS_ID)["counts"]["audits"] == 1
    assert (tmp_path / "derived" / CORPUS_ID / "corpus-summary.json").is_file()


def test_generic_pronouns_and_step_words_do_not_inflate_quality(tmp_path):
    store = seed_service(tmp_path)

    audit = store.audit_content(
        corpus_id=CORPUS_ID,
        title="Generic words",
        script=(
            "The this you your. First, the this you your. Next, the this you "
            "your. Then, the this you your."
        ),
        objective="educate",
        target_viewer="business owners",
        target_seconds=20,
    )

    assert audit["status"] == "revise"
    assert audit["scores"]["hook_clarity"] < 70
    assert audit["scores"]["narrative_flow"] < 70
    assert audit["quality_judgments"]["judgments"]["specificity"][
        "passed"
    ] is False
    assert audit["quality_judgments"]["judgments"]["tension_payoff"][
        "passed"
    ] is False


def test_agent_api_is_bounded_authenticated_and_cataloged(tmp_path):
    reference_root = tmp_path / "reference"
    seed_service(reference_root)
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
    client = app.test_client()
    headers = {"Authorization": f"Bearer {CONTROL_TOKEN}"}

    health = client.get("/api/reference-corpus/health")
    denied = client.get(
        f"/api/reference-corpus/status?corpus_id={CORPUS_ID}"
    )
    status = client.get(
        f"/api/reference-corpus/status?corpus_id={CORPUS_ID}",
        headers=headers,
    )
    second_page = client.get(
        f"/api/reference-corpus/items?corpus_id={CORPUS_ID}&offset=1",
        headers=headers,
    )
    context = client.post(
        "/api/reference-corpus/context",
        headers=headers,
        json={
            "corpus_id": CORPUS_ID,
            "query": "engaging hook with a visible result",
            "evidence_limit": 3,
        },
    )
    catalog = client.get("/api/agent/catalog", headers=headers)

    assert health.status_code == 200
    assert health.get_json()["item_count"] == 1
    assert denied.status_code == 401
    assert status.status_code == 200
    assert status.get_json()["counts"]["items"] == 1
    assert second_page.status_code == 200
    assert second_page.get_json()["count"] == 0
    assert second_page.get_json()["total"] == 1
    assert second_page.get_json()["next_offset"] is None
    assert context.status_code == 200
    assert context.get_json()["coverage"]["item_count"] == 1
    names = catalog.get_json()["".join(("oper", "ations"))]
    assert "build_reference_context" in names
    assert names["acquire_reference_corpus"]["bounds"]["limit"] == [1, 240]
    assert "audit_against_reference_corpus" in names
    assert names["extract_reference_items"]["bounds"]["limit"] == [1, 3]


def test_snapshot_is_consistent_and_hash_verified(tmp_path):
    store = seed_service(tmp_path / "hot")

    snapshot = store.build_snapshot(
        CORPUS_ID, output_root=tmp_path / "exports"
    )
    copied = store.copy_snapshot(
        snapshot["bundle_path"], tmp_path / "external", timeout_seconds=5
    )

    assert snapshot["status"] == "ok"
    assert snapshot["bytes"] > 0
    assert Path(snapshot["receipt_path"]).is_file()
    with zipfile.ZipFile(snapshot["bundle_path"]) as bundle:
        names = set(bundle.namelist())
    assert "manifest.json" in names
    assert "reference-corpus.sqlite3" in names
    assert copied["status"] == "ok"
    assert copied["copied"] is True
    assert copied["sha256"] == snapshot["sha256"]
