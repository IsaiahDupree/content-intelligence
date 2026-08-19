"""Integration coverage for the local transcript/artifact/cohort audit chain."""

from __future__ import annotations

import json
import wave
from datetime import datetime, timedelta, timezone

from services.content_quality.transcript_bank import (
    TranscriptBank,
    canonical_sha256,
    file_sha256,
)
from services.market_tape.config import MarketTapeConfig
from services.market_tape.models import MarketContent, MetricCounters
from services.market_tape.store import MarketTapeStore


def test_real_files_are_hash_bound_and_bad_script_claim_fails_closed(tmp_path):
    config = MarketTapeConfig(
        db_path=tmp_path / "market.sqlite3",
        object_dir=tmp_path / "objects",
        heartbeat_path=tmp_path / "heartbeat.json",
        lock_path=tmp_path / "market.lock",
        platforms=["youtube"],
        topics=["creator burnout"],
        supabase_sync_enabled=False,
    )
    store = MarketTapeStore(config)
    store.start_run("transcript-test-run", "integration")
    observed_at = datetime.now(timezone.utc)
    for index in range(5):
        store.ingest(
            MarketContent(
                platform="youtube",
                external_id=f"source-{index}",
                creator_external_id=f"creator-{index}",
                published_at=observed_at - timedelta(days=1),
                observed_at=observed_at,
                source_id="real-file-integration",
                metrics=MetricCounters(views=30_000, likes=1_800, comments=120),
                title="Creator burnout and creative pressure",
                description="A creator describes feeling stuck and exhausted by content work.",
                url=f"https://www.youtube.com/watch?v=source-{index}",
                duration_seconds=45,
                raw_payload={"source": index, "views": 30_000},
            ),
            "transcript-test-run",
        )
    store.finish_run("transcript-test-run")

    bank = TranscriptBank(config.db_path, tmp_path / "transcript-bank")
    candidates = bank.select_candidates(
        topic="creator burnout creative pressure content work",
        limit=5,
        platforms=["youtube"],
    )
    assert len(candidates) == 5

    transcript_text = (
        "Creator burnout feels like pressure that never stops. You feel tired and stuck, "
        "and creative work becomes harder even when you keep trying. The audience asks for "
        "more content while the creator worries that the next idea will fail. Taking a real "
        "break can refill the creative energy needed to make the work feel possible again."
    )
    artifacts = []
    for candidate in candidates:
        root = tmp_path / "transcript-bank" / "videos" / "youtube" / candidate.external_id
        root.mkdir(parents=True)
        audio_path = root / "source.wav"
        with wave.open(str(audio_path), "wb") as handle:
            handle.setnchannels(1)
            handle.setsampwidth(2)
            handle.setframerate(16_000)
            handle.writeframes(b"\x00\x00" * 1_600)
        payload = {
            "schema_version": 1,
            "video_id": candidate.video_id,
            "platform": "youtube",
            "external_id": candidate.external_id,
            "source_url": candidate.source_url,
            "source_observation": candidate.source_metrics,
            "audio_sha256": file_sha256(audio_path),
            "whisper_model": "base",
            "language": "en",
            "text": transcript_text,
            "segments": [{"id": 0, "start": 0.0, "end": 10.0, "text": transcript_text}],
        }
        transcript_hash = canonical_sha256(payload)
        transcript_path = root / f"whisper_{transcript_hash[:24]}.json"
        transcript_path.write_text(json.dumps(payload, sort_keys=True), encoding="utf-8")
        audit = bank._transcript_audit(
            candidate=candidate,
            transcript_text=transcript_text,
            segments=payload["segments"],
            audio_hash=payload["audio_sha256"],
            transcript_hash=transcript_hash,
        )
        artifact = {
            "transcript_id": f"whisper_{transcript_hash[:24]}",
            "video_id": candidate.video_id,
            "platform": "youtube",
            "external_id": candidate.external_id,
            "source_url": candidate.source_url,
            "observation_key": candidate.observation_key,
            "source_metrics": candidate.source_metrics,
            "audio_path": str(audio_path),
            "audio_sha256": payload["audio_sha256"],
            "transcript_path": str(transcript_path),
            "transcript_sha256": transcript_hash,
            "whisper_model": "base",
            "whisper_language": "en",
            "duration_seconds": 45,
            "word_count": len(transcript_text.split()),
            "segment_count": 1,
            "acquisition": {"tool": "real-wave-integration"},
            "audit": audit,
            "created_at": observed_at.isoformat(),
        }
        assert audit["decision"] == "PASS"
        bank._persist_artifact(artifact, transcript_text)
        artifacts.append(artifact)

    cohort = bank.build_cohort(topic="creator burnout", artifacts=artifacts)
    assert cohort["decision"] == "PASS"
    assert cohort["aggregate_metrics"]["member_count"] == 5
    assert cohort["aggregate_metrics"]["total_views"] == 150_000

    rejected = bank.audit_script_against_cohort(
        script_id="bad-script",
        script_text=(
            "You know that moment when the content factory fails? I reviewed 3 source "
            "transcript patterns with 297 observed views. Reveal the mechanism."
        ),
        cohort_manifest_path=cohort["manifest_path"],
    )
    assert rejected["decision"] == "REJECT_NOT_RELATABLE"
    assert rejected["score"] <= 69
    assert rejected["findings"]["checks"]["stated_source_claim_matches_cohort"] is False
    assert rejected["findings"]["checks"]["audience_facing_not_pipeline_meta"] is False

    accepted = bank.audit_script_against_cohort(
        script_id="supported-script",
        script_text=(
            "Do you feel tired and stuck when the audience asks for more content? Creator "
            "burnout can make creative work feel harder even when you keep trying. Take a "
            "real break and make the next step feel possible again."
        ),
        cohort_manifest_path=cohort["manifest_path"],
    )
    assert accepted["decision"] == "PASS_PREDICTED_RELATABILITY"
    assert accepted["score"] <= 85
    assert accepted["findings"]["actual_audience_relatability_measured"] is False
