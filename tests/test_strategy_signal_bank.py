from __future__ import annotations

import json

import pytest

from services.content_quality.reference_corpus import (
    ReferenceCorpusService,
    SOURCE_RIGHTS_STATE,
    utc_now,
)
from services.content_quality.strategy_signal_bank import (
    CAPTION_EVIDENCE,
    METADATA_EVIDENCE,
    StrategySignalBank,
    StrategySignalDraft,
)


CREATOR = "example.creator"
CORPUS_ID = "instagram-example-creator-reference-v1"


def seed(root) -> None:
    service = ReferenceCorpusService(root)
    service._upsert_corpus(
        corpus_id=CORPUS_ID,
        username=CREATOR,
        target_count=2,
        state="acquired",
        profile={"username": CREATOR},
    )
    base = {
        "corpus_id": CORPUS_ID,
        "platform": "instagram",
        "creator_handle": CREATOR,
        "duration_seconds": 20.0,
        "width": 1080,
        "height": 1920,
        "has_audio": True,
        "source_language": "en",
        "audio": {},
        "rights_state": SOURCE_RIGHTS_STATE,
        "direct_use_allowed": False,
        "raw_receipt_id": "receipt-1",
        "raw_path": str(root / "raw.json"),
        "metrics": {"views": 100, "likes": 8, "comments": 2},
    }
    service._put_items(
        [
            {
                **base,
                "item_id": "item-actionable",
                "external_id": "external-actionable",
                "shortcode": "actionable",
                "source_url": "https://www.instagram.com/reel/actionable/",
                "published_at": "2026-08-30T12:00:00+00:00",
                "caption": (
                    "Map the customer's desired outcome, divide the delivery "
                    "process into recurring decisions, and teach those decisions "
                    "before offering implementation support."
                ),
            },
            {
                **base,
                "item_id": "item-thin",
                "external_id": "external-thin",
                "shortcode": "thin",
                "source_url": "https://www.instagram.com/reel/thin/",
                "published_at": "2026-08-29T12:00:00+00:00",
                "caption": "A quick thought.",
            },
        ],
        utc_now(),
    )


def analyzer(items, model):
    assert model == "gpt-5-nano"
    return [
        StrategySignalDraft(
            item_id=items[0]["item_id"],
            actionable=True,
            title="Teach Decisions Before Delivery",
            niche="Offers & Conversion",
            level="beginner",
            effort="low",
            audience="Expert-led service businesses",
            core_strategy=(
                "Publish the decision framework that clarifies a buyer's problem, "
                "then reserve hands-on execution for the paid engagement."
            ),
            tactics=[
                "Name one observable buyer outcome.",
                "Turn the delivery workflow into a short decision sequence.",
            ],
            monetization_logic=(
                "Useful diagnosis builds trust while implementation remains the offer."
            ),
            cautions=["Do not claim that education alone causes purchases."],
            confidence=0.86,
        )
    ], "resp_test"


def test_signal_sweep_is_resumable_and_exports_evidence_levels(tmp_path):
    seed(tmp_path)
    bank = StrategySignalBank(tmp_path, analyzer=analyzer)

    result = bank.analyze_pending([CREATOR], limit=10)

    assert result["processed_count"] == 2
    assert result["evidence_states"] == {
        CAPTION_EVIDENCE: 1,
        METADATA_EVIDENCE: 1,
    }
    assert bank.pending_items([CREATOR]) == []
    status = bank.status([CREATOR])
    assert status["signal_count"] == 2
    assert status["pending_count"] == 0

    output = tmp_path / "strategy-reference-snapshot.json"
    receipt = bank.export_snapshot([CREATOR], output)
    first_bytes = output.read_bytes()
    repeated = bank.export_snapshot([CREATOR], output)
    snapshot = json.loads(output.read_text(encoding="utf-8"))
    assert receipt["counts"]["caption_backed"] == 1
    assert receipt["counts"]["metadata_only"] == 1
    assert snapshot["source_clips_retained"] is False
    assert snapshot["items"][0]["copy_gate_passed"] is True
    assert "caption" not in snapshot["items"][0]
    assert output.read_bytes() == first_bytes
    assert repeated["unchanged"] is True
    assert repeated["snapshot_sha256"] == receipt["snapshot_sha256"]
    assert snapshot["content_sha256"] == receipt["content_sha256"]


def test_signal_sweep_rejects_incomplete_model_coverage(tmp_path):
    seed(tmp_path)

    def incomplete(items, model):
        return [], "resp_missing"

    bank = StrategySignalBank(tmp_path, analyzer=incomplete)
    with pytest.raises(ValueError, match="coverage mismatch"):
        bank.analyze_pending([CREATOR], limit=1)

    assert bank.status([CREATOR])["signal_count"] == 0
