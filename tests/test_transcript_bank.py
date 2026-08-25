"""Integration coverage for the local transcript/artifact/cohort audit chain."""

from __future__ import annotations

import json
import sqlite3
import wave
from datetime import datetime, timedelta, timezone

import pytest

from services.content_quality.transcript_bank import (
    TranscriptBank,
    canonical_sha256,
    file_sha256,
    transcribe_cohort,
)
from services.market_tape.config import MarketTapeConfig
from services.market_tape.models import MarketContent, MetricCounters
from services.market_tape.store import SCHEMA_VERSION, MarketTapeStore


def _single_video_bank(
    tmp_path,
    *,
    external_id="claim-source",
    source_url: str | None = None,
):
    config = MarketTapeConfig(
        db_path=tmp_path / "market.sqlite3",
        object_dir=tmp_path / "objects",
        heartbeat_path=tmp_path / "heartbeat.json",
        lock_path=tmp_path / "market.lock",
        platforms=["youtube"],
        topics=["AI automation"],
        supabase_sync_enabled=False,
    )
    store = MarketTapeStore(config)
    observed_at = datetime.now(timezone.utc)
    store.start_run(f"run-{external_id}", "integration")
    store.ingest(
        MarketContent(
            platform="youtube",
            external_id=external_id,
            creator_external_id=f"creator-{external_id}",
            published_at=observed_at - timedelta(days=1),
            observed_at=observed_at,
            source_id="transcript-ledger-integration",
            metrics=MetricCounters(views=100_000, likes=8_000, comments=400),
            title="AI automation retention attention breakdown",
            description="A practical AI automation retention attention analysis.",
            url=source_url or f"https://www.youtube.com/watch?v={external_id}",
            duration_seconds=45,
            raw_payload={"external_id": external_id},
        ),
        f"run-{external_id}",
    )
    store.finish_run(f"run-{external_id}")
    bank = TranscriptBank(config.db_path, tmp_path / "transcript-bank")
    candidate = bank.select_backfill_candidates(
        limit=1,
        platforms=["youtube"],
    )[0]
    return config, bank, candidate, observed_at


def _real_transcript_artifact(tmp_path, bank, candidate, observed_at):
    video_root = (
        tmp_path / "transcript-bank" / "videos" / "youtube" / candidate.external_id
    )
    video_root.mkdir(parents=True, exist_ok=True)
    audio_path = video_root / "source.wav"
    with wave.open(str(audio_path), "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(16_000)
        handle.writeframes(b"\x00\x00" * 1_600)
    transcript_text = (
        "AI automation can improve retention attention when the opening demonstrates a "
        "specific problem and the explanation keeps moving. This practical breakdown "
        "shows the audience why the workflow matters, what changes, and how each useful "
        "step connects to the result without inventing audience behavior or performance."
    )
    payload = {
        "schema_version": 1,
        "video_id": candidate.video_id,
        "platform": candidate.platform,
        "external_id": candidate.external_id,
        "source_url": candidate.source_url,
        "source_observation": candidate.source_metrics,
        "audio_sha256": file_sha256(audio_path),
        "whisper_model": "base",
        "language": "en",
        "text": transcript_text,
        "segments": [
            {"id": 0, "start": 0.0, "end": 10.0, "text": transcript_text}
        ],
    }
    transcript_hash = canonical_sha256(payload)
    transcript_path = video_root / f"whisper_{transcript_hash[:24]}.json"
    transcript_path.write_text(json.dumps(payload, sort_keys=True), encoding="utf-8")
    audit = bank._transcript_audit(
        candidate=candidate,
        transcript_text=transcript_text,
        segments=payload["segments"],
        audio_hash=payload["audio_sha256"],
        transcript_hash=transcript_hash,
    )
    return {
        "transcript_id": f"whisper_{transcript_hash[:24]}",
        "video_id": candidate.video_id,
        "platform": candidate.platform,
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
        "duration_seconds": candidate.duration_seconds,
        "word_count": len(transcript_text.split()),
        "segment_count": 1,
        "acquisition": {"tool": "real-wave-integration"},
        "audit": audit,
        "created_at": observed_at.isoformat(),
    }, transcript_text


def test_youtube_backfill_accepts_twelve_minutes_but_not_longer(tmp_path):
    config = MarketTapeConfig(
        db_path=tmp_path / "market.sqlite3",
        object_dir=tmp_path / "objects",
        heartbeat_path=tmp_path / "heartbeat.json",
        lock_path=tmp_path / "market.lock",
        platforms=["youtube"],
        topics=["AI automation"],
        supabase_sync_enabled=False,
    )
    store = MarketTapeStore(config)
    store.start_run("duration-policy-run", "integration")
    observed_at = datetime.now(timezone.utc)
    for external_id, duration in (("accepted-603", 603), ("rejected-721", 721)):
        store.ingest(
            MarketContent(
                platform="youtube",
                external_id=external_id,
                creator_external_id=f"creator-{external_id}",
                published_at=observed_at - timedelta(days=1),
                observed_at=observed_at,
                source_id="duration-policy-integration",
                metrics=MetricCounters(views=250_000, likes=12_000, comments=500),
                title="What happens when AI automation can think?",
                description="A measured review of AI automation and agents.",
                url=f"https://www.youtube.com/watch?v={external_id}",
                duration_seconds=duration,
                raw_payload={"duration_seconds": duration},
            ),
            "duration-policy-run",
        )
    store.finish_run("duration-policy-run")

    bank = TranscriptBank(config.db_path, tmp_path / "transcript-bank")
    candidates = bank.select_backfill_candidates(
        topic="AI automation",
        limit=5,
        platforms=["youtube"],
    )

    assert [candidate.external_id for candidate in candidates] == ["accepted-603"]


def test_topic_backfill_prefilters_before_global_performance_ranking(tmp_path):
    config = MarketTapeConfig(
        db_path=tmp_path / "market.sqlite3",
        object_dir=tmp_path / "objects",
        heartbeat_path=tmp_path / "heartbeat.json",
        lock_path=tmp_path / "market.lock",
        platforms=["youtube"],
        topics=["AI automation"],
        supabase_sync_enabled=False,
    )
    store = MarketTapeStore(config)
    store.start_run("topic-prefilter-run", "integration")
    observed_at = datetime.now(timezone.utc)
    for index in range(501):
        store.ingest(
            MarketContent(
                platform="youtube",
                external_id=f"unrelated-{index}",
                creator_external_id=f"unrelated-creator-{index}",
                published_at=observed_at - timedelta(days=1),
                observed_at=observed_at,
                source_id="topic-prefilter-integration",
                metrics=MetricCounters(views=1_000_000 + index, likes=60_000),
                title="A globally popular cooking demonstration",
                description="Food, recipes, and kitchen technique.",
                url=f"https://www.youtube.com/watch?v=unrelated-{index}",
                duration_seconds=45,
                raw_payload={"index": index},
            ),
            "topic-prefilter-run",
        )
    store.ingest(
        MarketContent(
            platform="youtube",
            external_id="relevant-ai-automation",
            creator_external_id="relevant-creator",
            published_at=observed_at - timedelta(days=1),
            observed_at=observed_at,
            source_id="topic-prefilter-integration",
            metrics=MetricCounters(views=25_000, likes=1_500, comments=100),
            title="AI automation for the work you keep forgetting",
            description="A practical AI automation demonstration.",
            url="https://www.youtube.com/watch?v=relevant-ai-automation",
            duration_seconds=70,
            raw_payload={"relevant": True},
        ),
        "topic-prefilter-run",
    )
    store.finish_run("topic-prefilter-run")

    bank = TranscriptBank(config.db_path, tmp_path / "transcript-bank")
    candidates = bank.select_backfill_candidates(
        topic="AI automation",
        limit=1,
        platforms=["youtube"],
    )

    assert [candidate.external_id for candidate in candidates] == [
        "relevant-ai-automation"
    ]


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


def test_just_in_time_claim_and_success_ledger_are_atomic_and_append_only(tmp_path):
    config, bank, candidate, observed_at = _single_video_bank(tmp_path)

    first_claim = bank.claim_candidate(run_id="claim-run-one", candidate=candidate)
    assert first_claim["admitted"] is True
    competing_claim = bank.claim_candidate(
        run_id="claim-run-two",
        candidate=candidate,
    )
    assert competing_claim["admitted"] is False
    assert competing_claim["reason"] == "already_claimed"
    assert bank.release_claim(
        run_id="claim-run-one",
        claim_id=first_claim["claim_id"],
        reason="integration_reclaim",
    )

    active_claim = bank.claim_candidate(run_id="claim-run-three", candidate=candidate)
    assert active_claim["admitted"] is True
    artifact, transcript_text = _real_transcript_artifact(
        tmp_path,
        bank,
        candidate,
        observed_at,
    )
    attempt = bank.persist_successful_acquisition(
        artifact=artifact,
        transcript_text=transcript_text,
        run_id="claim-run-three",
        candidate=candidate,
        model_name="base",
        started_at=observed_at.isoformat(),
        finished_at=(observed_at + timedelta(seconds=5)).isoformat(),
        claim_id=active_claim["claim_id"],
    )

    connection = sqlite3.connect(config.db_path)
    connection.row_factory = sqlite3.Row
    attempt_row = connection.execute(
        "SELECT * FROM mt_transcript_acquisition_attempts WHERE attempt_id=?",
        (attempt["attempt_id"],),
    ).fetchone()
    claim_row = connection.execute(
        "SELECT * FROM mt_transcript_acquisition_claims WHERE claim_id=?",
        (active_claim["claim_id"],),
    ).fetchone()
    assert attempt_row["outcome"] == "success"
    assert attempt_row["claim_id"] == active_claim["claim_id"]
    assert attempt_row["receipt_sha256"] == attempt["receipt_sha256"]
    assert len(attempt_row["receipt_sha256"]) == 64
    assert claim_row["released_at"]
    assert claim_row["release_reason"] == "success"
    assert connection.execute(
        "SELECT COUNT(*) FROM mt_transcript_artifacts WHERE video_id=?",
        (candidate.video_id,),
    ).fetchone()[0] == 1
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        connection.execute(
            "UPDATE mt_transcript_acquisition_attempts SET error='changed' "
            "WHERE attempt_id=?",
            (attempt["attempt_id"],),
        )
    connection.rollback()
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        connection.execute(
            "DELETE FROM mt_transcript_acquisition_attempts WHERE attempt_id=?",
            (attempt["attempt_id"],),
        )
    connection.close()

    stale_candidate_claim = bank.claim_candidate(
        run_id="claim-run-four",
        candidate=candidate,
    )
    assert stale_candidate_claim["admitted"] is False
    assert stale_candidate_claim["reason"] == "artifact_already_exists"


def test_legacy_attempt_migration_is_complete_honest_and_runs_once(tmp_path):
    config = MarketTapeConfig(
        db_path=tmp_path / "market.sqlite3",
        object_dir=tmp_path / "objects",
        heartbeat_path=tmp_path / "heartbeat.json",
        lock_path=tmp_path / "market.lock",
        platforms=["youtube"],
        topics=["AI automation"],
        supabase_sync_enabled=False,
    )
    store = MarketTapeStore(config)
    observed_at = datetime.now(timezone.utc)
    store.start_run("legacy-history-source", "integration")
    for external_id in ("legacy-success", "legacy-failure"):
        store.ingest(
            MarketContent(
                platform="youtube",
                external_id=external_id,
                creator_external_id=f"creator-{external_id}",
                published_at=observed_at - timedelta(days=1),
                observed_at=observed_at,
                source_id="legacy-history-integration",
                metrics=MetricCounters(views=100_000, likes=8_000, comments=300),
                title="AI automation retention attention",
                description="A practical AI automation retention attention analysis.",
                url=f"https://www.youtube.com/watch?v={external_id}",
                duration_seconds=45,
                raw_payload={"external_id": external_id},
            ),
            "legacy-history-source",
        )
    store.finish_run("legacy-history-source")

    storage_root = tmp_path / "transcript-bank"
    success_root = storage_root / "videos" / "youtube" / "legacy-success"
    success_root.mkdir(parents=True)
    audio_path = success_root / "source.wav"
    with wave.open(str(audio_path), "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(16_000)
        handle.writeframes(b"\x00\x00" * 1_600)
    transcript_path = success_root / "legacy-transcript.json"
    transcript_path.write_text(
        json.dumps({"text": "real legacy transcript", "segments": []}),
        encoding="utf-8",
    )
    with store.connect() as connection:
        success_video = connection.execute(
            "SELECT video_id, url FROM mt_videos WHERE external_id='legacy-success'"
        ).fetchone()
        failure_video = connection.execute(
            "SELECT video_id FROM mt_videos WHERE external_id='legacy-failure'"
        ).fetchone()
        observation_key = connection.execute(
            "SELECT observation_key FROM mt_market_observations WHERE video_id=?",
            (success_video["video_id"],),
        ).fetchone()[0]
        connection.execute(
            """
            INSERT INTO mt_transcript_artifacts(
                transcript_id, video_id, platform, external_id, source_url,
                observation_key, source_metrics_json, audio_path, audio_sha256,
                transcript_path, transcript_sha256, whisper_model,
                whisper_language, duration_seconds, word_count, segment_count,
                acquisition_json, audit_json, created_at
            ) VALUES(?, ?, 'youtube', 'legacy-success', ?, ?, ?, ?, ?, ?, ?,
                     'base', 'en', 45, 3, 1, ?, ?, ?)
            """,
            (
                "legacy-transcript-id",
                success_video["video_id"],
                success_video["url"],
                observation_key,
                json.dumps({"views": 100_000}),
                str(audio_path),
                file_sha256(audio_path),
                str(transcript_path),
                file_sha256(transcript_path),
                json.dumps({"tool": "historical-real-file"}),
                json.dumps({"decision": "REJECTED"}),
                observed_at.isoformat(),
            ),
        )
        connection.execute(
            """
            INSERT INTO mt_transcript_backfill_runs(
                run_id, status, policy_json, candidate_ids_json,
                artifact_ids_json, failures_json, manifest_path,
                started_at, finished_at
            ) VALUES(?, 'failed', ?, ?, '[]', ?, ?, ?, ?)
            """,
            (
                "legacy-failure-run",
                json.dumps({"model": "base"}),
                json.dumps([failure_video["video_id"]]),
                json.dumps([{
                    "video_id": failure_video["video_id"],
                    "external_id": "legacy-failure",
                    "error_type": "RuntimeError",
                    "error": "ERROR: Unsupported URL",
                }]),
                str(storage_root / "runs" / "legacy-failure-run.json"),
                observed_at.isoformat(),
                (observed_at + timedelta(seconds=10)).isoformat(),
            ),
        )

    bank = TranscriptBank(config.db_path, storage_root)
    with bank.connect() as connection:
        attempts = connection.execute(
            "SELECT * FROM mt_transcript_acquisition_attempts ORDER BY outcome"
        ).fetchall()
        migration = json.loads(connection.execute(
            "SELECT receipt_json FROM mt_transcript_ledger_migrations"
        ).fetchone()[0])
        schema_version = connection.execute(
            "SELECT value FROM mt_meta WHERE key='schema_version'"
        ).fetchone()[0]
    assert len(attempts) == 2
    assert {row["outcome"] for row in attempts} == {"failure", "success"}
    assert all(len(row["receipt_sha256"]) == 64 for row in attempts)
    failure_attempt = next(row for row in attempts if row["outcome"] == "failure")
    assert failure_attempt["retryable"] == 1
    assert failure_attempt["failure_class"] == "extractor_unsupported"
    assert failure_attempt["runtime_fingerprint"] == ""
    assert failure_attempt["receipt_source"] == (
        "legacy_backfill_run_current_url_fallback"
    )
    assert migration["source_failure_events"] == 1
    assert migration["source_success_artifacts"] == 1
    assert migration["failure_source_url_current_row_fallbacks"] == 1
    assert schema_version == str(SCHEMA_VERSION)
    # Legacy attempts have no extractor fingerprint. A current runtime must
    # re-admit these videos instead of converting a historical parser gap into
    # a permanent source blacklist.
    selected = bank.select_backfill_candidates(limit=5, platforms=["youtube"])
    assert [candidate.external_id for candidate in selected] == ["legacy-failure"]

    TranscriptBank(config.db_path, storage_root)
    with bank.connect() as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM mt_transcript_acquisition_attempts"
        ).fetchone()[0] == 2


def test_extractor_upgrade_restarts_unsupported_failure_cooldown(tmp_path):
    config, bank, candidate, observed_at = _single_video_bank(
        tmp_path,
        external_id="extractor-upgrade",
    )
    old_finished_at = observed_at.isoformat()
    with bank.connect() as connection:
        connection.execute(
            """
            INSERT INTO mt_transcript_acquisition_attempts(
                attempt_id, run_id, video_id, platform, external_id,
                source_url, model_name, outcome, failure_class,
                retryable, retry_after, error_type, error,
                attempt_ordinal, receipt_source, runtime_fingerprint,
                started_at, finished_at
            ) VALUES(?, ?, ?, ?, ?, ?, 'base', 'failure',
                     'extractor_unsupported', 1, ?, 'RuntimeError',
                     'ERROR: Unsupported URL', 1, 'integration_old_runtime',
                     'retired-extractor-fingerprint', ?, ?)
            """,
            (
                "old-extractor-attempt",
                "old-extractor-run",
                candidate.video_id,
                candidate.platform,
                candidate.external_id,
                candidate.source_url,
                (observed_at + timedelta(days=7)).isoformat(),
                old_finished_at,
                old_finished_at,
            ),
        )

    # An unsupported failure from a retired extractor must not block the new
    # runtime or count toward its exponential retry ordinal.
    selected = bank.select_backfill_candidates(limit=1, platforms=["youtube"])
    assert [item.external_id for item in selected] == ["extractor-upgrade"]
    finished_at = observed_at + timedelta(hours=1)
    receipt = bank.record_acquisition_attempt(
        run_id="current-extractor-run",
        candidate=candidate,
        model_name="base",
        outcome="failure",
        started_at=finished_at.isoformat(),
        finished_at=finished_at.isoformat(),
        error_type="RuntimeError",
        error="ERROR: Unsupported URL",
        claim_id="",
    )
    assert datetime.fromisoformat(receipt["retry_after"]) == (
        finished_at + timedelta(hours=24)
    )
    assert bank.select_backfill_candidates(
        limit=1,
        platforms=["youtube"],
    ) == []


def test_cohort_acquisition_uses_real_claim_and_failure_ledger(tmp_path):
    config, bank, candidate, _observed_at = _single_video_bank(
        tmp_path,
        external_id="cohort-unreachable",
        source_url="http://127.0.0.1:1/cohort-unreachable.mp4",
    )

    result = transcribe_cohort(
        tape_path=config.db_path,
        storage_root=tmp_path / "transcript-bank",
        topic="AI automation retention attention",
        external_ids=[candidate.external_id],
        platforms=["youtube"],
        limit=1,
        model_name="base",
    )

    assert result["candidate_count"] == 1
    assert result["transcribed_count"] == 0
    assert result["failure_count"] == 1
    assert len(result["claim_receipts"]) == 1
    assert result["claim_receipts"][0]["admitted"] is True
    assert len(result["attempts"]) == 1
    assert result["attempts"][0]["outcome"] == "failure"
    with bank.connect() as connection:
        attempt = connection.execute(
            """
            SELECT outcome, claim_id, receipt_sha256
            FROM mt_transcript_acquisition_attempts
            WHERE video_id=?
            """,
            (candidate.video_id,),
        ).fetchone()
        claim = connection.execute(
            """
            SELECT released_at, release_reason
            FROM mt_transcript_acquisition_claims
            WHERE claim_id=?
            """,
            (attempt["claim_id"],),
        ).fetchone()
    assert attempt["outcome"] == "failure"
    assert len(attempt["receipt_sha256"]) == 64
    assert claim["released_at"]
    assert claim["release_reason"] == "failure"
