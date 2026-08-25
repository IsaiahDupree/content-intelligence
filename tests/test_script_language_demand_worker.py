"""One-shot demand consumption against real Market Tape SQLite/services."""

from __future__ import annotations

import hashlib
import json
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta

from services.content_quality.contracts import CURRENT_TRANSCRIPT_AUDIT_CONTRACT
from services.content_quality.transcript_bank import canonical_sha256
from services.market_tape.config import MarketTapeConfig
from services.market_tape.models import MarketContent, MetricCounters, utc_now
from services.market_tape.script_demand import (
    ScriptLanguageDemandWorker,
    _acquisition_query_frontier,
    _next_acquisition_query,
    _uses_youtube_performance_discovery,
)
from services.market_tape.store import ACCEPTED_OBSERVATION_EVIDENCE_CONTRACT
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


def test_discovery_lane_exercises_the_requested_platform_policy():
    assert _uses_youtube_performance_discovery(("youtube",)) is True
    assert _uses_youtube_performance_discovery(
        ("youtube", "tiktok", "instagram", "facebook")
    ) is False


def test_query_frontier_advances_once_per_exact_platform_scope():
    request_payload = {
        "candidate_assessments": [
            {
                "rank": 2,
                "trend_id": "trend-b",
                "language_query": "AI automation creator tools",
                "exact_trend_member_count": 4,
                "qualified_language_candidate_count": 5,
            },
            {
                "rank": 9,
                "trend_id": "trend-a",
                "language_query": "AI automation small business",
                "exact_trend_member_count": 20,
                "qualified_language_candidate_count": 12,
            },
            {
                "rank": 1,
                "trend_id": "trend-noisy",
                "language_query": "AI automation ice cream",
                "exact_trend_member_count": 1,
                "qualified_language_candidate_count": 8,
            },
            {
                "rank": 3,
                "trend_id": "trend-off-topic",
                "language_query": "creator burnout",
            },
        ]
    }
    assert _acquisition_query_frontier(request_payload, "AI automation") == (
        "AI automation",
        "AI automation small business",
        "AI automation creator tools",
        "AI automation ice cream",
    )
    claim = {
        "events": [
            {
                "payload": {
                    "result": {
                        "acquisition_query": "AI automation",
                        "policy": {"platforms": ["youtube"]},
                    }
                }
            }
        ]
    }
    assert _next_acquisition_query(
        claim, request_payload, "AI automation", ("youtube",)
    ) == "AI automation small business"
    # The same text has not been attempted for this broader immutable scope.
    assert _next_acquisition_query(
        claim,
        request_payload,
        "AI automation",
        ("youtube", "tiktok", "instagram", "facebook"),
    ) == "AI automation"
    claim["events"].extend(
        {
            "payload": {
                "result": {
                    "acquisition_query": query,
                    "policy": {"platforms": ["youtube"]},
                }
            }
        }
        for query in (
            "AI automation small business",
            "AI automation creator tools",
            "AI automation ice cream",
        )
    )
    assert _next_acquisition_query(
        claim, request_payload, "AI automation", ("youtube",)
    ) == ""


def _seed_passing_artifact(
    store: MarketTapeStore,
    tmp_path,
    *,
    external_id: str,
    creator_external_id: str,
    title: str,
    description: str,
    views: int,
    transcript_text: str | None = None,
    observed_at: datetime | None = None,
    transcript_id: str | None = None,
    whisper_language: str = "en",
) -> dict:
    """Persist accepted observation + immutable transcript evidence in SQLite."""

    observed_at = observed_at or utc_now()
    transcript_id = transcript_id or f"transcript-{external_id}"
    run_id = f"seed-{transcript_id}"
    item = MarketContent(
        platform="youtube",
        external_id=external_id,
        creator_external_id=creator_external_id,
        published_at=observed_at - timedelta(days=1),
        observed_at=observed_at,
        source_id="script-demand-integration-seed",
        metrics=MetricCounters(
            views=views,
            likes=max(1_000, views // 20),
            comments=300,
            shares=100,
            saves=50,
        ),
        title=title,
        description=description,
        language="en",
        url=f"https://www.youtube.com/watch?v={external_id}",
        duration_seconds=60,
        raw_payload={"external_id": external_id, "source": "integration"},
    )
    store.start_run(run_id, "integration")
    store.ingest(item, run_id)
    store.finish_run(run_id)

    artifact_root = tmp_path / "transcript-bank" / transcript_id
    artifact_root.mkdir(parents=True, exist_ok=True)
    audio_path = artifact_root / "source.audio"
    transcript_path = artifact_root / "transcript.json"
    audio_path.write_bytes(f"audited-audio-{external_id}".encode())
    transcript_payload = {
        "text": transcript_text or (
            f"{title}. {description}. This transcript contains enough measured "
            "language to represent a completed local transcription artifact."
        ),
        "segments": [{"start": 0.0, "end": 60.0, "text": title}],
    }
    transcript_path.write_text(
        json.dumps(transcript_payload, sort_keys=True), encoding="utf-8"
    )
    with store.connect() as connection:
        evidence = connection.execute(
            """SELECT observation_key, observation_id
               FROM mt_accepted_full_evidence_v1
               WHERE video_id=?
               ORDER BY accepted_at DESC, observation_id DESC
               LIMIT 1""",
            (item.video_id,),
        ).fetchone()
        assert evidence is not None
        connection.execute(
            """INSERT INTO mt_transcript_artifacts(
                   transcript_id, video_id, platform, external_id, source_url,
                   observation_key, source_metrics_json, audio_path, audio_sha256,
                   transcript_path, transcript_sha256, whisper_model,
                   whisper_language, duration_seconds, word_count, segment_count,
                   acquisition_json, audit_json, created_at
               ) VALUES(?, ?, 'youtube', ?, ?, ?, ?, ?, ?, ?, ?, 'base', ?,
                        60, 50, 1, ?, ?, ?)""",
            (
                transcript_id,
                item.video_id,
                external_id,
                item.url,
                evidence["observation_key"],
                json.dumps({"views": views}, sort_keys=True),
                str(audio_path),
                hashlib.sha256(audio_path.read_bytes()).hexdigest(),
                str(transcript_path),
                canonical_sha256(transcript_payload),
                whisper_language,
                json.dumps({"tool": "local-transcript-integration"}),
                json.dumps({
                    "contract": CURRENT_TRANSCRIPT_AUDIT_CONTRACT,
                    "decision": "PASS",
                    "transcript_payload_sha256": canonical_sha256(
                        transcript_payload
                    ),
                }, sort_keys=True),
                observed_at.isoformat(),
            ),
        )
    return {
        "video_id": item.video_id,
        "creator_id": item.creator_id,
        "transcript_id": transcript_id,
        "observation_id": int(evidence["observation_id"]),
        "observation_key": str(evidence["observation_key"]),
        "observed_at": observed_at.isoformat(),
        "audio_path": str(audio_path),
        "transcript_path": str(transcript_path),
    }


def _bind_historical_trend(
    store: MarketTapeStore,
    *,
    trend_id: str,
    member: dict,
) -> None:
    with store.connect() as connection:
        connection.execute(
            """INSERT INTO mt_trends(
                   trend_id, trend_type, canonical_key, display_name, status,
                   first_seen_at, last_seen_at
               ) VALUES(?, 'topic', 'creator retention historical lineage',
                        'Creator Retention Historical Lineage', 'active', ?, ?)""",
            (trend_id, member["observed_at"], member["observed_at"]),
        )
        connection.execute(
            """INSERT INTO mt_trend_memberships(
                   trend_id, video_id, confidence, evidence_json, first_seen_at
               ) VALUES(?, ?, 1.0, '{}', ?)""",
            (trend_id, member["video_id"], member["observed_at"]),
        )
        connection.execute(
            """INSERT INTO mt_trend_membership_lineage(
                   trend_id, video_id, observation_id, linked_at, contract
               ) VALUES(?, ?, ?, ?, ?)""",
            (
                trend_id,
                member["video_id"],
                member["observation_id"],
                member["observed_at"],
                ACCEPTED_OBSERVATION_EVIDENCE_CONTRACT,
            ),
        )


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
    refreshed = _demand("refusal-2")
    refreshed["acquisition_policy"]["discovery_limit"] = 7
    refreshed["acquisition_policy"]["transcript_limit"] = 2
    second = store.enqueue_script_language_demand(refreshed)
    assert second["demand_id"] == first["demand_id"]
    assert second["coalesced"] is True
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
    assert result["result"]["pipeline"]["discovery_lane"] == "performance_query"
    assert result["result"]["policy"]["discovery_limit"] == 7
    assert result["result"]["policy"]["transcript_limit"] == 2
    completed = store.script_language_demand(first["demand_id"])
    coalesced = store.script_language_demand(second["demand_id"])
    assert completed["state"] == "blocked"
    assert [event["event_type"] for event in completed["events"]] == [
        "requested", "claimed", "blocked",
    ]
    assert completed["events"][-1]["payload"]["result"]["goal_met"] is False
    assert completed["collection_run_id"]
    assert completed["transcript_run_id"]
    assert coalesced["state"] == "blocked"
    assert coalesced["snapshot_lineage_count"] == 2
    latest_lineage = completed["latest_snapshot_lineage"]
    claimed_event = completed["events"][1]
    terminal_event = completed["events"][2]
    assert claimed_event["request_sha256"] == latest_lineage["request_sha256"]
    assert claimed_event["snapshot_id"] == latest_lineage["snapshot_id"]
    assert claimed_event["payload"]["request_lineage"]["lineage_id"] == (
        latest_lineage["lineage_id"]
    )
    assert terminal_event["request_sha256"] == latest_lineage["request_sha256"]
    assert terminal_event["snapshot_id"] == latest_lineage["snapshot_id"]
    assert terminal_event["payload"]["request_lineage"]["lineage_id"] == (
        latest_lineage["lineage_id"]
    )

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


def test_new_snapshot_during_claim_forces_explicit_retry_on_latest_lineage(
    tmp_path,
):
    config = _config(tmp_path)
    store = MarketTapeStore(config)
    queued = store.enqueue_script_language_demand(_demand("claim-a"))
    claim = store.claim_next_script_language_demand(600)
    assert claim is not None
    claimed_lineage = claim["latest_snapshot_lineage"]

    refreshed_payload = _demand("claim-b")
    refreshed_payload["acquisition_policy"]["discovery_limit"] = 4
    refreshed = store.enqueue_script_language_demand(refreshed_payload)
    assert refreshed["demand_id"] == queued["demand_id"]
    assert refreshed["coalesced"] is True
    assert refreshed["snapshot_lineage_count"] == 2
    latest_lineage = refreshed["latest_snapshot_lineage"]
    assert latest_lineage["lineage_id"] != claimed_lineage["lineage_id"]

    finished = store.finish_script_language_demand(
        claim["demand_id"],
        claim["attempt_no"],
        "completed",
        {"goal_met": True, "bounded_attempt": "claim-a"},
    )

    assert finished["state"] == "partial"
    assert finished["retry_eligible"] is True
    assert finished["latest_snapshot_id"] == refreshed_payload["snapshot_id"]
    result = finished["events"][-1]["payload"]["result"]
    assert result["goal_met"] is False
    assert result["retry_required"] is True
    assert result["failure_code"] == "NEWER_SNAPSHOT_QUEUED_DURING_CLAIM"
    assert result["claimed_snapshot_lineage_id"] == claimed_lineage["lineage_id"]
    assert result["latest_snapshot_lineage_id"] == latest_lineage["lineage_id"]
    assert finished["events"][-1]["snapshot_id"] == claimed_lineage[
        "snapshot_id"
    ]
    replayed_finish = store.finish_script_language_demand(
        claim["demand_id"],
        claim["attempt_no"],
        "completed",
        {"goal_met": True, "bounded_attempt": "claim-a-replay"},
    )
    assert replayed_finish["state"] == "partial"
    assert replayed_finish["appended"] is False
    assert replayed_finish["deduplicated"] is True

    retry_claim = store.claim_next_script_language_demand(600)
    assert retry_claim is not None
    assert retry_claim["demand_id"] == claim["demand_id"]
    assert retry_claim["attempt_no"] == 2
    assert retry_claim["snapshot_id"] == latest_lineage["snapshot_id"]
    assert retry_claim["request_sha256"] == latest_lineage["request_sha256"]
    assert retry_claim["latest_request_payload"]["acquisition_policy"][
        "discovery_limit"
    ] == 4


def test_migrated_claim_without_embedded_lineage_cannot_close_refresh(tmp_path):
    config = _config(tmp_path)
    store = MarketTapeStore(config)
    queued = store.enqueue_script_language_demand(_demand("legacy-claim-a"))
    claim = store.claim_next_script_language_demand(600)
    assert claim is not None
    with store.connect() as connection:
        claim_row = connection.execute(
            """SELECT event_id, payload_json
               FROM mt_script_language_demand_events
               WHERE demand_id = ? AND event_type = 'claimed'""",
            (queued["demand_id"],),
        ).fetchone()
        legacy_payload = json.loads(claim_row["payload_json"])
        legacy_payload.pop("request_lineage", None)
        connection.execute(
            "DROP TRIGGER mt_script_language_demand_events_no_update"
        )
        connection.execute(
            "DROP TRIGGER mt_script_language_demand_snapshot_lineage_no_delete"
        )
        connection.execute(
            "DROP TRIGGER mt_script_language_demand_semantics_no_delete"
        )
        connection.execute(
            "UPDATE mt_script_language_demand_events SET payload_json=? "
            "WHERE event_id=?",
            (json.dumps(legacy_payload, sort_keys=True), claim_row["event_id"]),
        )
        connection.execute(
            "DELETE FROM mt_script_language_demand_snapshot_lineage"
        )
        connection.execute("DELETE FROM mt_script_language_demand_semantics")

    migrated = MarketTapeStore(config)
    backfilled = migrated.script_language_demand(queued["demand_id"])
    assert backfilled is not None
    assert backfilled["snapshot_lineage_count"] == 1
    backfilled_lineage = backfilled["latest_snapshot_lineage"]

    refreshed_payload = _demand("legacy-claim-b")
    refreshed = migrated.enqueue_script_language_demand(refreshed_payload)
    assert refreshed["demand_id"] == queued["demand_id"]
    assert refreshed["snapshot_lineage_count"] == 2
    finished = migrated.finish_script_language_demand(
        claim["demand_id"], claim["attempt_no"], "completed", {"goal_met": True}
    )

    assert finished["state"] == "partial"
    terminal = finished["events"][-1]
    assert terminal["payload"]["request_lineage"]["lineage_id"] == (
        backfilled_lineage["lineage_id"]
    )
    assert terminal["payload"]["result"]["failure_code"] == (
        "NEWER_SNAPSHOT_QUEUED_DURING_CLAIM"
    )


def test_changed_request_after_final_same_snapshot_creates_new_generation(
    tmp_path,
):
    config = _config(tmp_path)
    store = MarketTapeStore(config)
    first_payload = _demand("final-generation-a")
    first = store.enqueue_script_language_demand(first_payload)
    first_claim = store.claim_next_script_language_demand(600)
    assert first_claim is not None
    blocked = store.finish_script_language_demand(
        first_claim["demand_id"],
        first_claim["attempt_no"],
        "blocked",
        {
            "goal_met": False,
            "failure_code": "PROVIDER_BLOCKED",
            "acquisition_query": "creator retention",
            "policy": {"platforms": ["youtube"]},
        },
    )
    assert blocked["state"] == "blocked"

    continuation_payload = _demand("final-generation-b")
    continuation_payload["snapshot_id"] = first_payload["snapshot_id"]
    continuation_payload["acquisition_policy"]["discovery_limit"] = 3
    continuation = store.enqueue_script_language_demand(continuation_payload)

    assert continuation["enqueued"] is True
    assert continuation["idempotent"] is False
    assert continuation["demand_id"] != first["demand_id"]
    assert continuation["state"] == "requested"
    assert continuation["snapshot_lineage_count"] == 1
    acquisition_history = store.script_language_demand_acquisition_history(
        continuation["demand_id"]
    )
    assert acquisition_history["contract"] == (
        "market_tape_script_language_demand_acquisition_history_v1"
    )
    assert acquisition_history["demand_ids"] == sorted([
        first["demand_id"], continuation["demand_id"],
    ])
    assert [
        event["payload"]["result"]["acquisition_query"]
        for event in acquisition_history["events"]
    ] == ["creator retention"]
    assert _next_acquisition_query(
        continuation,
        continuation["latest_request_payload"],
        "creator retention",
        ("youtube",),
        semantic_events=acquisition_history["events"],
    ) == ""
    second_claim = store.claim_next_script_language_demand(600)
    assert second_claim is not None
    assert second_claim["demand_id"] == continuation["demand_id"]
    assert second_claim["request_sha256"] == continuation["request_sha256"]
    assert second_claim["latest_request_payload"]["acquisition_policy"][
        "discovery_limit"
    ] == 3


def test_concurrent_snapshot_refreshes_create_one_semantic_demand(tmp_path):
    config = _config(tmp_path)
    stores = [MarketTapeStore(config), MarketTapeStore(config)]
    barrier = threading.Barrier(2)

    def enqueue(index: int) -> dict:
        payload = _demand(f"concurrent-{index}")
        barrier.wait(timeout=5)
        return stores[index].enqueue_script_language_demand(payload)

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(enqueue, range(2)))

    assert len({result["demand_id"] for result in results}) == 1
    assert sum(bool(result["enqueued"]) for result in results) == 1
    demand = stores[0].script_language_demand(results[0]["demand_id"])
    assert demand is not None
    assert demand["state"] == "requested"
    assert demand["snapshot_lineage_count"] == 2
    assert len(stores[0].list_script_language_demands(state="requested")) == 1


def test_topic_evidence_advances_without_historical_trend_membership(
    tmp_path, monkeypatch
):
    monkeypatch.delenv("YOUTUBE_API_KEY", raising=False)
    monkeypatch.delenv("YOUTUBE_DATA_API_KEY", raising=False)
    config = _config(tmp_path)
    store = MarketTapeStore(config)
    worker = ScriptLanguageDemandWorker(
        config,
        store,
        transcript_storage_root=tmp_path / "transcript-bank",
    )
    trend_id = "trend:historical-lineage-only"

    empty = worker.cohort_snapshot(
        topic="creator retention", evidence_trend_id=trend_id
    )
    assert empty["verified_transcripts"] == 0

    topical = _seed_passing_artifact(
        store,
        tmp_path,
        external_id="topic-evidence",
        creator_external_id="creator-topic",
        title="Creator retention language that keeps attention",
        description="A measured creator retention breakdown.",
        views=175_000,
    )
    unrelated_member = _seed_passing_artifact(
        store,
        tmp_path,
        external_id="historical-member",
        creator_external_id="creator-unrelated",
        title="Restaurant cooking demonstration",
        description="A chef explains a weeknight recipe.",
        views=900_000,
    )
    _bind_historical_trend(
        store,
        trend_id=trend_id,
        member=unrelated_member,
    )

    advanced = worker.cohort_snapshot(
        topic="creator retention", evidence_trend_id=trend_id
    )
    assert advanced["evidence_trend_id"] == trend_id
    assert advanced["evidence_trend_id_role"] == "lineage_only"
    assert advanced["historical_trend_membership_required"] is False
    assert advanced["verified_transcripts"] == 1
    assert advanced["distinct_creators"] == 1
    assert advanced["observed_views"] == 175_000
    assert advanced["transcript_ids"] == [topical["transcript_id"]]
    assert advanced["creator_ids"] == [topical["creator_id"]]

    store.enqueue_script_language_demand(_demand("lineage-proof"))
    result = worker.run_next()

    assert result["processed"] == 1
    assert result["result"]["evidence_trend_id"] == trend_id
    assert result["result"]["evidence_trend_id_role"] == "lineage_only"
    assert result["result"]["before"]["verified_transcripts"] == 1
    assert result["result"]["policy"]["excluded_creator_count"] == 1
    assert result["result"]["pipeline"]["trend_ids"] == []
    assert result["result"]["pipeline"][
        "historical_trend_membership_required"
    ] is False
    assert result["result"]["pipeline"]["selection_scope"] == (
        "accepted_full_evidence_exact_topic_vocabulary"
    )
    assert trend_id in result["result"]["pipeline"][
        "auto_resolved_trend_ids_ignored"
    ]


def test_cohort_rejects_topical_metadata_when_transcript_is_off_topic(tmp_path):
    config = _config(tmp_path)
    store = MarketTapeStore(config)
    worker = ScriptLanguageDemandWorker(
        config,
        store,
        transcript_storage_root=tmp_path / "transcript-bank",
    )
    _seed_passing_artifact(
        store,
        tmp_path,
        external_id="metadata-only-topic",
        creator_external_id="creator-metadata-only",
        title="Creator retention language that keeps attention",
        description="A measured creator retention breakdown.",
        views=250_000,
        transcript_text=(
            "A chef demonstrates a weeknight recipe with onions, peppers, "
            "olive oil, and a cast iron pan while explaining kitchen timing."
        ),
    )

    snapshot = worker.cohort_snapshot(topic="creator retention")

    assert snapshot["verified_transcripts"] == 0
    assert snapshot["rejected_candidates"]["transcript_topic_mismatch"] == 1


def test_cohort_rejects_non_target_language_transcript_artifact(tmp_path):
    config = _config(tmp_path)
    store = MarketTapeStore(config)
    _seed_passing_artifact(
        store,
        tmp_path,
        external_id="non-english-artifact",
        creator_external_id="creator-non-english",
        title="Creator retention language that keeps attention",
        description="A measured creator retention breakdown.",
        views=250_000,
        whisper_language="es",
    )

    cohort = ScriptLanguageDemandWorker(
        config,
        store,
        transcript_storage_root=tmp_path / "transcript-bank",
    ).cohort_snapshot(topic="creator retention")

    assert cohort["target_language"] == "en"
    assert cohort["verified_transcripts"] == 0
    assert cohort["distinct_creators"] == 0
    assert cohort["observed_views"] == 0
    assert cohort["rejected_candidates"]["transcript_language_mismatch"] == 1


def test_cohort_rejects_missing_or_hash_mismatched_artifact_files(tmp_path):
    config = _config(tmp_path)
    store = MarketTapeStore(config)
    worker = ScriptLanguageDemandWorker(
        config,
        store,
        transcript_storage_root=tmp_path / "transcript-bank",
    )
    missing = _seed_passing_artifact(
        store,
        tmp_path,
        external_id="missing-artifact",
        creator_external_id="creator-missing",
        title="Creator retention language that keeps attention",
        description="A measured creator retention breakdown.",
        views=200_000,
    )
    mismatched = _seed_passing_artifact(
        store,
        tmp_path,
        external_id="hash-mismatch",
        creator_external_id="creator-mismatch",
        title="Creator retention language that keeps attention",
        description="A measured creator retention breakdown.",
        views=200_000,
    )
    from pathlib import Path

    Path(missing["transcript_path"]).unlink()
    Path(mismatched["audio_path"]).write_bytes(b"tampered-audio")

    snapshot = worker.cohort_snapshot(topic="creator retention")

    assert snapshot["verified_transcripts"] == 0
    assert snapshot["rejected_candidates"]["artifact_file_missing"] == 1
    assert snapshot["rejected_candidates"]["artifact_hash_mismatch"] == 1


def test_cohort_fails_closed_when_legacy_artifact_read_times_out(
    tmp_path, monkeypatch
):
    config = _config(tmp_path)
    store = MarketTapeStore(config)
    _seed_passing_artifact(
        store,
        tmp_path,
        external_id="blocked-legacy-payload",
        creator_external_id="creator-blocked-payload",
        title="Creator retention language that keeps attention",
        description="A measured creator retention breakdown.",
        views=250_000,
    )

    def blocked_read(*_args, **_kwargs):
        raise TimeoutError("simulated bounded child-process deadline")

    monkeypatch.setattr(
        "services.market_tape.script_demand.read_legacy_json_payload_bounded",
        blocked_read,
    )
    snapshot = ScriptLanguageDemandWorker(
        config,
        store,
        transcript_storage_root=tmp_path / "transcript-bank",
    ).cohort_snapshot(
        topic="creator retention",
        read_timeout_seconds=0.1,
    )

    assert snapshot["verified_transcripts"] == 0
    assert snapshot["observed_views"] == 0
    assert snapshot["artifact_read_timeout_seconds"] == 0.1
    assert snapshot["rejected_candidates"]["artifact_read_timeout"] == 1


def test_retranscription_of_same_source_observation_counts_once(tmp_path):
    config = _config(tmp_path)
    store = MarketTapeStore(config)
    member = _seed_passing_artifact(
        store,
        tmp_path,
        external_id="same-source-retranscribed",
        creator_external_id="creator-one-source",
        title="Creator retention language that keeps attention",
        description="A measured creator retention breakdown.",
        views=60_000,
    )
    with store.connect() as connection:
        connection.execute(
            """INSERT INTO mt_transcript_artifacts(
                   transcript_id, video_id, platform, external_id, source_url,
                   observation_key, source_metrics_json, audio_path,
                   audio_sha256, transcript_path, transcript_sha256,
                   whisper_model, whisper_language, duration_seconds,
                   word_count, segment_count, acquisition_json, audit_json,
                   created_at
               )
               SELECT ?, video_id, platform, external_id, source_url,
                      observation_key, source_metrics_json, audio_path,
                      audio_sha256, transcript_path, transcript_sha256,
                      'small', whisper_language, duration_seconds,
                      word_count, segment_count, acquisition_json, audit_json,
                      created_at
               FROM mt_transcript_artifacts WHERE transcript_id=?""",
            ("transcript-second-model", member["transcript_id"]),
        )

    snapshot = ScriptLanguageDemandWorker(
        config,
        store,
        transcript_storage_root=tmp_path / "transcript-bank",
    ).cohort_snapshot(topic="creator retention")

    assert snapshot["verified_transcripts"] == 1
    assert snapshot["observed_views"] == 60_000
    assert len(snapshot["transcript_ids"]) == 1


def test_later_observation_of_same_video_replaces_instead_of_inflating(
    tmp_path,
):
    config = _config(tmp_path)
    store = MarketTapeStore(config)
    first_observed_at = utc_now() - timedelta(minutes=2)
    first = _seed_passing_artifact(
        store,
        tmp_path,
        external_id="same-video-new-observation",
        creator_external_id="same-creator",
        title="Creator retention language that keeps attention",
        description="A measured creator retention breakdown.",
        views=60_000,
        observed_at=first_observed_at,
        transcript_id="transcript-earlier-observation",
    )
    latest = _seed_passing_artifact(
        store,
        tmp_path,
        external_id="same-video-new-observation",
        creator_external_id="same-creator",
        title="Creator retention language that keeps attention",
        description="A measured creator retention breakdown.",
        views=90_000,
        observed_at=first_observed_at + timedelta(minutes=1),
        transcript_id="transcript-latest-observation",
    )

    snapshot = ScriptLanguageDemandWorker(
        config,
        store,
        transcript_storage_root=tmp_path / "transcript-bank",
    ).cohort_snapshot(topic="creator retention")

    assert first["video_id"] == latest["video_id"]
    assert first["observation_key"] != latest["observation_key"]
    assert snapshot["source_deduplication_key"] == ["video_id"]
    assert snapshot["verified_transcripts"] == 1
    assert snapshot["distinct_creators"] == 1
    assert snapshot["observed_views"] == 90_000
    assert snapshot["transcript_ids"] == [latest["transcript_id"]]
    assert snapshot["observation_keys"] == [latest["observation_key"]]
    assert snapshot["source_observation_lineage"] == [{
        "video_id": latest["video_id"],
        "creator_id": latest["creator_id"],
        "observation_key": latest["observation_key"],
        "observed_at": latest["observed_at"],
        "transcript_id": latest["transcript_id"],
        "views": 90_000,
    }]
    assert snapshot["rejected_candidates"][
        "valid_artifact_candidates_not_selected_same_video"
    ] == 1


def test_invalid_newest_observation_falls_back_to_older_valid_lineage(tmp_path):
    config = _config(tmp_path)
    store = MarketTapeStore(config)
    first_observed_at = utc_now() - timedelta(minutes=2)
    first = _seed_passing_artifact(
        store,
        tmp_path,
        external_id="same-video-invalid-newest",
        creator_external_id="same-creator",
        title="Creator retention language that keeps attention",
        description="A measured creator retention breakdown.",
        views=60_000,
        observed_at=first_observed_at,
        transcript_id="transcript-valid-older-observation",
    )
    newest = _seed_passing_artifact(
        store,
        tmp_path,
        external_id="same-video-invalid-newest",
        creator_external_id="same-creator",
        title="Creator retention language that keeps attention",
        description="A measured creator retention breakdown.",
        views=90_000,
        observed_at=first_observed_at + timedelta(minutes=1),
        transcript_id="transcript-invalid-newest-observation",
    )
    from pathlib import Path

    Path(newest["audio_path"]).write_bytes(b"tampered-newest-audio")

    cohort = ScriptLanguageDemandWorker(
        config,
        store,
        transcript_storage_root=tmp_path / "transcript-bank",
    ).cohort_snapshot(topic="creator retention")

    assert cohort["verified_transcripts"] == 1
    assert cohort["observed_views"] == 60_000
    assert cohort["transcript_ids"] == [first["transcript_id"]]
    assert cohort["observation_keys"] == [first["observation_key"]]
    assert cohort["rejected_candidates"]["artifact_hash_mismatch"] == 1


def test_already_satisfied_demand_closes_without_an_acquisition_cycle(tmp_path):
    config = _config(tmp_path)
    store = MarketTapeStore(config)
    for index in range(5):
        _seed_passing_artifact(
            store,
            tmp_path,
            external_id=f"already-satisfied-{index}",
            creator_external_id=f"creator-{index % 3}",
            title="Creator retention language that keeps attention",
            description="A measured creator retention breakdown.",
            views=25_000,
        )
    queued = store.enqueue_script_language_demand(
        _demand("already-satisfied")
    )
    worker = ScriptLanguageDemandWorker(
        config,
        store,
        transcript_storage_root=tmp_path / "transcript-bank",
    )

    result = worker.run_next()

    assert result["state"] == "completed"
    assert result["goal_met"] is True
    assert result["result"]["already_satisfied_before_acquisition"] is True
    assert result["result"]["pipeline"] is None
    assert result["result"]["before"] == result["result"]["after"]
    assert result["result"]["goal_checks"] == {
        "verified_transcripts": {"actual": 5, "minimum": 5, "pass": True},
        "distinct_creators": {"actual": 3, "minimum": 3, "pass": True},
        "observed_views": {"actual": 125_000, "minimum": 100_000, "pass": True},
    }
    stored = store.script_language_demand(queued["demand_id"])
    assert [event["event_type"] for event in stored["events"]] == [
        "requested", "claimed", "completed",
    ]
    with store.connect() as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM mt_collection_runs"
        ).fetchone()[0] == 5
