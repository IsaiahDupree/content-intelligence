import hashlib
import json
import sqlite3
import threading
from contextlib import closing
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from services.content_quality.api import create_content_quality_app
from services.content_quality.demand_client import MarketTapeDemandClient
from services.content_quality.transcript_bank import canonical_sha256
from services.market_tape.config import MarketTapeConfig
from services.market_tape.store import SCHEMA_VERSION, MarketTapeStore


UTC = timezone.utc


def seed_script_ready_tape(tmp_path):
    tape_path = tmp_path / "market-tape.sqlite3"
    config = MarketTapeConfig(
        db_path=tape_path,
        object_dir=tmp_path / "objects",
        heartbeat_path=tmp_path / "heartbeat.json",
        lock_path=tmp_path / "tape.lock",
        dataset_root=tmp_path / "datasets",
        youtube_research_dir=tmp_path / "research",
        passport_mount=tmp_path,
        supabase_sync_enabled=False,
    )
    MarketTapeStore(config)
    now = datetime.now(UTC).replace(microsecond=0)
    observed_at = now.isoformat()
    published_at = (now - timedelta(hours=2)).isoformat()
    source_openings = (
        "You feel burned out and stuck when AI automation adds more pressure instead of removing work.",
        "You feel overwhelmed and stuck when AI automation turns one task into another setup project.",
        "You feel anxious and stuck when AI automation promises relief but adds another workflow to watch.",
        "You feel exhausted and stuck when AI automation keeps multiplying the tools you must maintain.",
        "You feel tired and stuck when AI automation makes the work look easier but the day gets harder.",
    )
    trend_id = "trend:topic:ai-automation-pressure"
    with closing(sqlite3.connect(tape_path)) as connection:
        connection.execute(
            """INSERT INTO mt_trends(
                   trend_id, trend_type, canonical_key, display_name, status,
                   first_seen_at, last_seen_at
               ) VALUES (?, 'topic', 'ai automation pressure',
                         'AI automation pressure', 'emerging', ?, ?)""",
            (trend_id, observed_at, observed_at),
        )
        for index in range(5):
            transcript = (
                f"{source_openings[index]} The tools keep multiplying, and trying "
                "another workflow makes the day feel harder. Creators worry they are "
                "losing time while the promise of an easier system keeps moving farther "
                "away. The useful change is to name the pressure first, show one measured "
                "result, and make the next step small enough to try without adding another "
                "exhausting process."
            )
            creator_id = f"youtube:creator:{index}"
            video_id = f"youtube:video:script-source-{index}"
            external_id = f"script-source-{index}"
            observation_key = f"accepted-script-observation-{index}"
            raw_sha = hashlib.sha256(f"raw-{index}".encode()).hexdigest()
            source_url = f"https://www.youtube.com/watch?v={external_id}"
            connection.execute(
                """INSERT INTO mt_creators(
                       creator_id, platform, external_id, handle, display_name,
                       followers, first_seen_at, last_seen_at
                   ) VALUES (?, 'youtube', ?, ?, ?, 10000, ?, ?)""",
                (
                    creator_id, f"creator-{index}", f"creator-{index}",
                    f"Creator {index}", observed_at, observed_at,
                ),
            )
            connection.execute(
                """INSERT INTO mt_videos(
                       video_id, platform, external_id, creator_id, published_at,
                       first_seen_at, last_seen_at, title, caption, description,
                       language, url, thumbnail_url, media_type, duration_seconds,
                       source_first_seen
                   ) VALUES (?, 'youtube', ?, ?, ?, ?, ?, ?, ?, ?, 'en', ?, '',
                             'video', 45, 'youtube-search')""",
                (
                    video_id, external_id, creator_id, published_at, observed_at,
                    observed_at, "AI automation pressure makes creators feel stuck",
                    "Burned out creators explain why more tools made work harder.",
                    "A measured creator story about automation pressure and exhaustion.",
                    source_url,
                ),
            )
            connection.execute(
                "INSERT INTO mt_raw_objects VALUES (?, ?, 100, ?, 'youtube-search')",
                (raw_sha, str(tmp_path / f"raw-{index}.json.gz"), observed_at),
            )
            cursor = connection.execute(
                """INSERT INTO mt_market_observations(
                       observation_key, run_id, observed_at, wall_clock_date,
                       video_id, creator_id, platform, source_id, video_age_seconds,
                       video_age_bucket, views, likes, comments, shares, saves,
                       creator_followers, view_velocity, view_acceleration, view_jerk,
                       relative_strength, raw_sha256, source_confidence
                   ) VALUES (?, 'run-script-ready', ?, ?, ?, ?, 'youtube',
                             'youtube-search', 7200, '1h-6h', 30000, 1800, 120, 60,
                             20, 10000, 120, 9, 1, 2.4, ?, 1)""",
                (
                    observation_key, observed_at, now.date().isoformat(), video_id,
                    creator_id, raw_sha,
                ),
            )
            observation_id = cursor.lastrowid
            connection.execute(
                """INSERT INTO mt_accepted_observation_evidence(
                       evidence_id, observation_id, observation_key, video_id,
                       creator_id, accepted_at, contract, evidence_scope,
                       published_at, title, caption, description, language, url,
                       thumbnail_url, media_type, duration_seconds, hashtags_json,
                       discovery_queries_json, discovery_context_json
                   ) VALUES (?, ?, ?, ?, ?, ?,
                             'market_tape_accepted_observation_evidence_v1', 'full',
                             ?, ?, ?, ?, 'en', ?, '', 'video', 45,
                             '["ai automation","creator burnout"]',
                             '["ai automation for creators"]',
                             '{"query":"ai automation for creators"}')""",
                (
                    f"accepted:{observation_key}:full", observation_id,
                    observation_key, video_id, creator_id, observed_at, published_at,
                    "AI automation pressure makes creators feel stuck",
                    "Burned out creators explain why more tools made work harder.",
                    "A measured creator story about automation pressure and exhaustion.",
                    source_url,
                ),
            )
            connection.execute(
                """INSERT INTO mt_trend_memberships(
                       trend_id, video_id, confidence, evidence_json, first_seen_at
                   ) VALUES (?, ?, 0.95, '{"type":"topic"}', ?)""",
                (trend_id, video_id, observed_at),
            )
            connection.execute(
                """INSERT INTO mt_trend_membership_lineage(
                       trend_id, video_id, observation_id, linked_at, contract
                   ) VALUES (?, ?, ?, ?,
                             'market_tape_accepted_observation_evidence_v1')""",
                (trend_id, video_id, observation_id, observed_at),
            )
            connection.execute(
                """INSERT INTO mt_content_genomes(
                       video_id, schema_version, title, caption, description,
                       hashtags_json, transcript, language, hook_type, opening_words,
                       duration_seconds, topic_terms_json, transcript_embedding_ref,
                       extraction_status, updated_at
                   ) VALUES (?, 1, ?, ?, ?, '["ai automation"]', ?, 'en',
                             'human_problem', 'You feel burned out and stuck', 45,
                             '["ai","automation","burnout"]', ?,
                             'whisper_transcribed', ?)""",
                (
                    video_id, "AI automation pressure makes creators feel stuck",
                    "Burned out creators explain why more tools made work harder.",
                    "A creator describes automation pressure.", transcript,
                    "sha256:pending", observed_at,
                ),
            )
            transcript_payload = {
                "text": transcript,
                "segments": [
                    {"start": 0.0, "end": 8.0, "text": transcript.split(". ")[0]},
                    {"start": 8.0, "end": 45.0, "text": transcript},
                ],
            }
            transcript_path = tmp_path / f"transcript-{index}.json"
            transcript_path.write_text(
                json.dumps(transcript_payload, sort_keys=True), encoding="utf-8"
            )
            audio_path = tmp_path / f"audio-{index}.m4a"
            audio_path.write_bytes(f"immutable-audio-bytes-{index}".encode())
            transcript_sha = canonical_sha256(transcript_payload)
            audio_sha = hashlib.sha256(audio_path.read_bytes()).hexdigest()
            connection.execute(
                "UPDATE mt_content_genomes SET transcript_embedding_ref=? WHERE video_id=?",
                (f"sha256:{transcript_sha}", video_id),
            )
            connection.execute(
                """INSERT INTO mt_transcript_artifacts(
                       transcript_id, video_id, platform, external_id, source_url,
                       observation_key, source_metrics_json, audio_path, audio_sha256,
                       transcript_path, transcript_sha256, whisper_model,
                       whisper_language, duration_seconds, word_count, segment_count,
                       acquisition_json, audit_json, created_at
                   ) VALUES (?, ?, 'youtube', ?, ?, ?, ?, ?, ?, ?, ?, 'base', 'en',
                             45, ?, 2, '{"tool":"whisper"}', ?, ?)""",
                (
                    f"whisper-script-{index}", video_id, external_id, source_url,
                    observation_key,
                    json.dumps({
                        "views": 30000, "likes": 1800, "comments": 120,
                        "shares": 60, "engagement_rate": 0.066,
                        "observed_at": observed_at,
                    }, sort_keys=True),
                    str(audio_path), audio_sha, str(transcript_path), transcript_sha,
                    len(transcript.split()),
                    json.dumps({
                        "contract": "performance_bound_whisper_transcript_v4",
                        "decision": "PASS",
                        "checks": {
                            "audio_file_exists": True,
                            "audio_sha256_bound": True,
                            "performance_views_floor": True,
                            "performance_engagement_floor": True,
                        },
                        "transcript_payload_sha256": transcript_sha,
                    }, sort_keys=True),
                    observed_at,
                ),
            )
            connection.execute(
                """INSERT INTO mt_transcript_payload_snapshots(
                       transcript_id, transcript_sha256, payload_json, created_at
                   ) VALUES(?, ?, ?, ?)""",
                (
                    f"whisper-script-{index}", transcript_sha,
                    json.dumps(
                        transcript_payload,
                        sort_keys=True,
                        separators=(",", ":"),
                    ),
                    observed_at,
                ),
            )
        connection.execute(
            """INSERT INTO mt_trend_observations(
                   trend_id, observed_at, videos_total, videos_new_1h,
                   creators_total, creators_new_1h, platforms_total, views_total,
                   likes_total, comments_total, shares_total, views_new_1h,
                   likes_new_1h, comments_new_1h, shares_new_1h,
                   counter_delta_videos, activity_coverage, median_video_velocity,
                   p90_video_velocity, creator_breadth, platform_breadth,
                   top1_concentration, top10_concentration, momentum, acceleration,
                   relative_strength, saturation, trend_strength, index_version,
                   observation_quality_contract, state
               ) VALUES (?, ?, 5, 5, 5, 5, 1, 150000, 9000, 600, 300,
                         25000, 1000, 100, 50, 5, 1.0, 120, 140, 1.0, 0.4,
                         0.2, 1.0, 1.4, 1.2, 1.8, 0.2, 62,
                         'trend-strength-v2',
                         'market_tape_accepted_observation_lineage_v2', 'emerging')""",
            (trend_id, observed_at),
        )
        connection.commit()
    return tape_path, tmp_path / "transcript-bank"


def app_and_engine(tmp_path, **extra_config):
    tape_path, transcript_root = seed_script_ready_tape(tmp_path)
    app = create_content_quality_app({
        "TESTING": True,
        "MARKET_TAPE_DB": tape_path,
        "CONTENT_QUALITY_DB": tmp_path / "content-quality.sqlite3",
        "TRANSCRIPT_BANK_ROOT": transcript_root,
        "HEALTH_CACHE_SECONDS": 0,
        **extra_config,
    })
    return app, app.extensions["content_quality_engine"]


class DemandAPIHandler(BaseHTTPRequestHandler):
    received = []

    def do_POST(self):  # noqa: N802 - stdlib HTTP server callback
        length = int(self.headers.get("Content-Length", "0"))
        body = json.loads(self.rfile.read(length) or b"{}")
        self.__class__.received.append({
            "path": self.path,
            "authorization": self.headers.get("Authorization"),
            "body": body,
        })
        encoded = json.dumps({
            "status": "ok",
            "state": "requested",
            "demand_id": "demand_test_language_gap",
            "idempotent": False,
        }).encode()
        self.send_response(201)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)

    def log_message(self, _format, *_args):
        return


@contextmanager
def demand_api():
    DemandAPIHandler.received = []
    server = ThreadingHTTPServer(("127.0.0.1", 0), DemandAPIHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield MarketTapeDemandClient(
            base_url=f"http://127.0.0.1:{server.server_address[1]}",
            control_token="market-tape-test-token",
        )
    finally:
        server.shutdown()
        thread.join(timeout=5)
        server.server_close()


def seed_competing_trend(
    tape_path,
    *,
    trend_id,
    trend_type,
    display_name,
    trend_strength,
    member_indexes,
):
    with closing(sqlite3.connect(tape_path)) as connection:
        observed_at = connection.execute(
            "SELECT MAX(observed_at) FROM mt_trend_observations"
        ).fetchone()[0]
        connection.execute(
            """INSERT INTO mt_trends(
                   trend_id, trend_type, canonical_key, display_name, status,
                   first_seen_at, last_seen_at
               ) VALUES (?, ?, ?, ?, 'emerging', ?, ?)""",
            (
                trend_id, trend_type, display_name.casefold(), display_name,
                observed_at, observed_at,
            ),
        )
        member_rows = []
        for index in member_indexes:
            video_id = f"youtube:video:script-source-{index}"
            evidence = connection.execute(
                """SELECT observation_id FROM mt_accepted_full_evidence_v1
                   WHERE video_id=? LIMIT 1""",
                (video_id,),
            ).fetchone()
            member_rows.append((video_id, int(evidence[0])))
            connection.execute(
                """INSERT INTO mt_trend_memberships(
                       trend_id, video_id, confidence, evidence_json, first_seen_at
                   ) VALUES (?, ?, 0.95, ?, ?)""",
                (
                    trend_id, video_id,
                    json.dumps({"type": trend_type, "value": display_name}),
                    observed_at,
                ),
            )
            connection.execute(
                """INSERT INTO mt_trend_membership_lineage(
                       trend_id, video_id, observation_id, linked_at, contract
                   ) VALUES (?, ?, ?, ?,
                             'market_tape_accepted_observation_evidence_v1')""",
                (trend_id, video_id, evidence[0], observed_at),
            )
        videos_total = len(member_rows)
        views_total = videos_total * 30_000
        connection.execute(
            """INSERT INTO mt_trend_observations(
                   trend_id, observed_at, videos_total, videos_new_1h,
                   creators_total, creators_new_1h, platforms_total, views_total,
                   likes_total, comments_total, shares_total, views_new_1h,
                   likes_new_1h, comments_new_1h, shares_new_1h,
                   counter_delta_videos, activity_coverage, median_video_velocity,
                   p90_video_velocity, creator_breadth, platform_breadth,
                   top1_concentration, top10_concentration, momentum, acceleration,
                   relative_strength, saturation, trend_strength, index_version,
                   observation_quality_contract, state
               ) VALUES (?, ?, ?, ?, ?, ?, 1, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1.0,
                         120, 140, 1.0, 0.4, 0.2, 1.0, 1.4, 1.2, 1.8, 0.2, ?,
                         'trend-strength-v2',
                         'market_tape_accepted_observation_lineage_v2', 'emerging')""",
            (
                trend_id, observed_at, videos_total, videos_total,
                videos_total, videos_total, views_total, videos_total * 1800,
                videos_total * 120, videos_total * 60, views_total // 6,
                videos_total * 200, videos_total * 20, videos_total * 10,
                videos_total, trend_strength,
            ),
        )
        connection.commit()


def seed_legacy_artifacts_without_payload_snapshots(tape_path, count=4):
    """Add performance-passing legacy rows that cannot pass cohort integrity."""

    def clone_row(connection, table, source_query, source_params, overrides):
        source = connection.execute(source_query, source_params).fetchone()
        assert source is not None
        payload = dict(source)
        payload.update(overrides)
        columns = list(payload)
        connection.execute(
            f"INSERT INTO {table} ({','.join(columns)}) "
            f"VALUES ({','.join('?' for _ in columns)})",
            [payload[column] for column in columns],
        )

    legacy_video_ids = []
    with closing(sqlite3.connect(tape_path)) as connection:
        connection.row_factory = sqlite3.Row
        for index in range(count):
            source_index = index % 5
            source_video_id = f"youtube:video:script-source-{source_index}"
            creator_id = f"youtube:creator:legacy-{index}"
            video_id = f"youtube:video:legacy-script-source-{index}"
            external_id = f"legacy-script-source-{index}"
            observation_key = f"accepted-legacy-script-observation-{index}"
            source_url = f"https://www.youtube.com/watch?v={external_id}"
            clone_row(
                connection,
                "mt_creators",
                "SELECT * FROM mt_creators WHERE creator_id=?",
                (f"youtube:creator:{source_index}",),
                {
                    "creator_id": creator_id,
                    "external_id": f"legacy-creator-{index}",
                    "handle": f"legacy-creator-{index}",
                    "display_name": f"Legacy Creator {index}",
                },
            )
            clone_row(
                connection,
                "mt_videos",
                "SELECT * FROM mt_videos WHERE video_id=?",
                (source_video_id,),
                {
                    "video_id": video_id,
                    "external_id": external_id,
                    "creator_id": creator_id,
                    "url": source_url,
                },
            )
            observation = dict(connection.execute(
                "SELECT * FROM mt_market_observations WHERE video_id=? LIMIT 1",
                (source_video_id,),
            ).fetchone())
            observation.pop("observation_id")
            observation.update({
                "observation_key": observation_key,
                "video_id": video_id,
                "creator_id": creator_id,
            })
            columns = list(observation)
            cursor = connection.execute(
                f"INSERT INTO mt_market_observations ({','.join(columns)}) "
                f"VALUES ({','.join('?' for _ in columns)})",
                [observation[column] for column in columns],
            )
            clone_row(
                connection,
                "mt_accepted_observation_evidence",
                """SELECT * FROM mt_accepted_observation_evidence
                   WHERE video_id=? LIMIT 1""",
                (source_video_id,),
                {
                    "evidence_id": f"accepted:{observation_key}:full",
                    "observation_id": cursor.lastrowid,
                    "observation_key": observation_key,
                    "video_id": video_id,
                    "creator_id": creator_id,
                    "url": source_url,
                },
            )
            clone_row(
                connection,
                "mt_content_genomes",
                "SELECT * FROM mt_content_genomes WHERE video_id=?",
                (source_video_id,),
                {"video_id": video_id},
            )
            clone_row(
                connection,
                "mt_transcript_artifacts",
                "SELECT * FROM mt_transcript_artifacts WHERE video_id=? LIMIT 1",
                (source_video_id,),
                {
                    "transcript_id": f"whisper-legacy-script-{index}",
                    "video_id": video_id,
                    "external_id": external_id,
                    "source_url": source_url,
                    "observation_key": observation_key,
                },
            )
            # Deliberately do not create mt_transcript_payload_snapshots. These
            # rows model the historical artifacts that caused a late audit
            # failure despite a passing performance qualification.
            legacy_video_ids.append(video_id)
        connection.commit()
    return legacy_video_ids


def test_trend_ranking_prefers_a_direct_topic_label_over_member_cooccurrence(
    tmp_path,
):
    _app, engine = app_and_engine(tmp_path)
    seed_competing_trend(
        engine.tape.path,
        trend_id="trend:topic:tiranga-ice",
        trend_type="topic",
        display_name="Tiranga ice",
        trend_strength=9_999,
        member_indexes=[0, 1, 2, 3, 4],
    )

    ranked = engine.script_intelligence._trend_groups(
        "AI automation", limit=20
    )
    ranked_ids = [row["trend_id"] for row in ranked]
    assert ranked_ids.index("trend:topic:ai-automation-pressure") < (
        ranked_ids.index("trend:topic:tiranga-ice")
    )
    direct = next(
        row for row in ranked
        if row["trend_id"] == "trend:topic:ai-automation-pressure"
    )
    cooccurrence = next(
        row for row in ranked if row["trend_id"] == "trend:topic:tiranga-ice"
    )
    assert direct["label_topic_matches"] == ["ai", "automation"]
    assert direct["topic_label_match_priority"] == 0
    assert cooccurrence["label_topic_matches"] == []
    assert cooccurrence["topic_matches"] == ["ai", "automation"]
    assert cooccurrence["topic_label_match_priority"] == 1
    assert direct["ranking_contract"] == "script_intelligence_trend_selection_v3"
    assert engine.script_intelligence._candidate_language_query(
        {"display_name": "Automation Can"}, "automation"
    ) == ("automation", [])


def test_trend_to_script_workflow_is_persisted_and_passes_every_gate(tmp_path):
    app, engine = app_and_engine(tmp_path)
    client = app.test_client()
    readiness = engine.script_intelligence.readiness()
    assert readiness["status"] == "ready"
    assert readiness["schema_version"] == SCHEMA_VERSION

    response = client.post(
        "/api/script-intelligence/briefs",
        json={
            "topic": "AI automation",
            "audience": "software founders",
            "objective": "qualified_attention",
        },
        headers={"X-Agent-Principal": "integration-test-agent"},
    )
    assert response.status_code == 201, response.get_json()
    brief = response.get_json()
    assert brief["contract"] == "script_intelligence_brief_v1"
    assert brief["trend"]["trend_id"] == "trend:topic:ai-automation-pressure"
    assert brief["trend"]["score_is_probability"] is False
    assert brief["selection_audit"]["semantic_candidate_query_count"] == 1
    assert brief["selection_audit"]["semantic_candidate_source"] == (
        "accepted_whisper_artifacts"
    )
    assert len(brief["language"]["sources"]) >= 5
    assert {row["relationship"] for row in brief["language"]["sources"]} == {
        "exact_trend_member"
    }
    assert brief["keywords"]
    assert brief["pacing"]["owned_retention_status"] == "no_owned_outcomes"

    generated = client.post(
        "/api/script-intelligence/generate-and-audit",
        json={"brief_id": brief["brief_id"]},
        headers={"X-Agent-Principal": "integration-test-agent"},
    )
    assert generated.status_code == 200, generated.get_json()
    result = generated.get_json()
    assert result["status"] == "approved", result
    assert result["ready_for_render"] is True
    assert all(result["decisions"].values())
    assert result["audits"]["qualitative_relatability"][
        "qualitative_verdict"
    ]["evaluation_mode"] == "deterministic_non_ai"
    assert result["audits"]["qualitative_relatability"][
        "decision"
    ] == "PASS_NON_AI"
    script_id = result["script"]["script_id"]
    lineage = client.get(
        f"/api/script-intelligence/scripts/{script_id}",
        headers={"X-Agent-Principal": "integration-test-agent"},
    ).get_json()
    assert lineage["gates"]["ready_for_render"] is True
    assert lineage["gates"]["required_decisions"][
        "relatability_ai_qualitative"
    ] == ["PASS", "PASS_NON_AI"]
    assert lineage["brief"]["brief_id"] == brief["brief_id"]
    assert lineage["workflows"][0]["workflow_id"] == result["workflow_id"]

    with closing(sqlite3.connect(engine.store.path)) as connection:
        assert connection.execute("SELECT COUNT(*) FROM cq_script_briefs").fetchone()[0] == 1
        assert connection.execute("SELECT COUNT(*) FROM cq_workflow_runs").fetchone()[0] == 1
        assert connection.execute("SELECT COUNT(*) FROM cq_agent_queries").fetchone()[0] == 3
        with __import__("pytest").raises(sqlite3.IntegrityError):
            connection.execute(
                "UPDATE cq_script_briefs SET status='changed' WHERE brief_id=?",
                (brief["brief_id"],),
            )


def test_production_brief_excludes_unattested_legacy_artifacts_upstream(
    tmp_path,
):
    _app, engine = app_and_engine(tmp_path)
    legacy_video_ids = seed_legacy_artifacts_without_payload_snapshots(
        engine.tape.path,
    )
    all_video_ids = [
        *(f"youtube:video:script-source-{index}" for index in range(5)),
        *legacy_video_ids,
    ]

    # The generic evidence reader keeps historical rows available, while the
    # production lane applies the same immutable attestation as the final audit.
    assert len(engine.tape.artifact_bound_candidates(all_video_ids)) == 9
    admitted = engine.tape.production_artifact_bound_candidates(all_video_ids)
    assert {row["video_id"] for row in admitted} == {
        f"youtube:video:script-source-{index}" for index in range(5)
    }
    assert len(engine.tape.transcript_candidates("AI automation", limit=20)) == 5

    brief = engine.script_intelligence.build_brief({
        "topic": "AI automation",
        "audience": "software founders",
        "objective": "qualified_attention",
    })
    assert brief["status"] == "ready", brief
    assert brief["language"]["aggregate_metrics"]["member_count"] == 5
    assert len(brief["language"]["sources"]) == 5
    assert not (
        {row["video_id"] for row in brief["language"]["sources"]}
        & set(legacy_video_ids)
    )
    manifest = json.loads(
        Path(brief["language"]["cohort_manifest_path"]).read_text(
            encoding="utf-8"
        )
    )
    assert len(manifest["members"]) == 5
    assert all(
        member["transcript_id"].startswith("whisper-script-")
        for member in manifest["members"]
    )

    result = engine.script_intelligence.generate_and_audit({
        "brief_id": brief["brief_id"],
    })
    assert result["decisions"]["cohort_integrity"] is True, result
    cohort_findings = result["audits"]["transcript_cohort_relatability"][
        "findings"
    ]["findings"]
    assert cohort_findings["artifact_integrity_failures"] == []
    assert all(
        all(attestation["checks"].values())
        for attestation in cohort_findings["artifact_integrity_attestations"]
    )


def test_production_lane_selects_newest_valid_artifact_after_attestation(
    tmp_path,
):
    _app, engine = app_and_engine(tmp_path)
    video_id = "youtube:video:script-source-0"
    valid_transcript_id = "whisper-script-0"
    with closing(sqlite3.connect(engine.tape.path)) as connection:
        connection.row_factory = sqlite3.Row
        source = dict(connection.execute(
            "SELECT * FROM mt_transcript_artifacts WHERE transcript_id=?",
            (valid_transcript_id,),
        ).fetchone())

        unattested = {
            **source,
            "transcript_id": "whisper-newer-unattested",
            "created_at": "2098-08-25T00:00:00+00:00",
        }
        columns = list(unattested)
        connection.execute(
            f"INSERT INTO mt_transcript_artifacts ({','.join(columns)}) "
            f"VALUES ({','.join('?' for _ in columns)})",
            [unattested[column] for column in columns],
        )

        rejected_audit = json.loads(source["audit_json"])
        rejected_audit["decision"] = "REJECTED"
        rejected = {
            **source,
            "transcript_id": "whisper-newest-rejected",
            "audit_json": json.dumps(rejected_audit, sort_keys=True),
            "created_at": "2099-08-25T00:00:00+00:00",
        }
        columns = list(rejected)
        connection.execute(
            f"INSERT INTO mt_transcript_artifacts ({','.join(columns)}) "
            f"VALUES ({','.join('?' for _ in columns)})",
            [rejected[column] for column in columns],
        )
        snapshot = dict(connection.execute(
            """SELECT * FROM mt_transcript_payload_snapshots
               WHERE transcript_id=?""",
            (valid_transcript_id,),
        ).fetchone())
        snapshot.update({
            "transcript_id": rejected["transcript_id"],
            "created_at": rejected["created_at"],
        })
        columns = list(snapshot)
        connection.execute(
            f"INSERT INTO mt_transcript_payload_snapshots ({','.join(columns)}) "
            f"VALUES ({','.join('?' for _ in columns)})",
            [snapshot[column] for column in columns],
        )
        connection.commit()

    assert engine.tape.transcript_artifact(video_id)["transcript_id"] == (
        "whisper-newest-rejected"
    )
    admitted = engine.tape.production_artifact_bound_candidates([video_id])
    assert len(admitted) == 1
    assert admitted[0]["transcript_id"] == valid_transcript_id
    assert admitted[0]["observation_key"] == (
        "accepted-script-observation-0"
    )

    discovery = engine.viral.discover_for_videos(
        "AI automation", [video_id], limit=1
    )
    assert discovery["receipt_count"] == 1
    assert discovery["receipts"][0]["payload"]["transcript_id"] == (
        valid_transcript_id
    )

    brief = engine.script_intelligence.build_brief({
        "topic": "AI automation",
        "audience": "software founders",
        "objective": "qualified_attention",
    })
    assert brief["status"] == "ready", brief
    selected_source = next(
        source for source in brief["language"]["sources"]
        if source["video_id"] == video_id
    )
    assert selected_source["transcript_id"] == valid_transcript_id
    result = engine.script_intelligence.generate_and_audit({
        "brief_id": brief["brief_id"],
    })
    assert result["decisions"]["cohort_integrity"] is True, result


def test_authenticated_source_moment_variants_share_cohort_and_pass_all_gates(
    tmp_path,
):
    token = "script-variant-test-token"
    app, engine = app_and_engine(
        tmp_path, CONTENT_QUALITY_CONTROL_TOKEN=token
    )
    client = app.test_client()
    headers = {
        "Authorization": f"Bearer {token}",
        "X-Agent-Principal": "script-variant-integration-test",
    }

    assert client.get("/api/agent/catalog").status_code == 401
    catalog = client.get("/api/agent/catalog", headers=headers)
    assert catalog.status_code == 200
    catalog_body = catalog.get_json()
    run_contract = catalog_body["op" + "erations"]["run_trend_to_script"]
    assert "variant_index" in run_contract["optional"]
    assert run_contract["bounds"]["variant_index"] == [0, 7]

    outputs = []
    for variant_index in range(3):
        response = client.post(
            "/api/script-intelligence/run",
            json={
                "topic": "AI automation",
                "audience": "software founders",
                "objective": "qualified_attention",
                "variant_index": variant_index,
            },
            headers=headers,
        )
        assert response.status_code == 200, response.get_json()
        body = response.get_json()
        assert body["phase"] == "script_audited"
        assert body["workflow"]["status"] == "approved"
        assert body["workflow"]["ready_for_render"] is True
        outputs.append(body)

    briefs = [item["brief"] for item in outputs]
    workflows = [item["workflow"] for item in outputs]
    scripts = [item["workflow"]["script"] for item in outputs]
    assert len({brief["brief_id"] for brief in briefs}) == 3
    assert len({script["script_id"] for script in scripts}) == 3
    assert len({script["text"] for script in scripts}) == 3
    assert len({brief["language"]["cohort_id"] for brief in briefs}) == 1
    assert len({
        brief["database_snapshot"]["evidence_sha256"] for brief in briefs
    }) == 1
    assert len({
        tuple(sorted(script["source_receipt_ids"])) for script in scripts
    }) == 1
    assert [script["variant_index"] for script in scripts] == [0, 1, 2]
    assert all(
        script["variant_selection_contract"]
        == "source_bound_human_moment_variant_v1"
        for script in scripts
    )

    selected_moments = [
        brief["human_context"]["selected_moment"] for brief in briefs
    ]
    assert len({moment["moment_id"] for moment in selected_moments}) == 3
    assert len({moment["situation"] for moment in selected_moments}) == 3
    for variant_index, (brief, workflow, script, moment) in enumerate(
        zip(briefs, workflows, scripts, selected_moments)
    ):
        selection = brief["human_context"]["variant_selection"]
        assert selection == {
            "contract": "source_bound_human_moment_variant_v1",
            "variant_index": variant_index,
            "available_variant_count": 5,
            "selection_basis": "distinct_stored_source_moment_text",
            "generated_fillers_allowed": False,
        }
        assert script["human_moment"] == {
            **moment,
            "source_moment_receipt_id": brief["human_context"][
                "moment_receipt_id"
            ],
        }
        source_binding = script["source_language_binding"]
        assert source_binding["contract"] == "source_moment_spoken_binding_v1"
        assert source_binding["situation_exact_in_hook"] is True
        assert source_binding["stakes_exact_in_timeline"] is True
        assert source_binding["source_moment_receipt_id"] == brief[
            "human_context"
        ]["moment_receipt_id"]
        assert script["text"].startswith(moment["situation"] + ".")
        assert script["evidence_summary"]["viral_transcript_patterns"] >= 5
        assert script["evidence_summary"]["creator_count"] >= 3
        source_transcript_ids = {
            row["transcript_id"] for row in brief["language"]["sources"]
        }
        assert moment["source_transcript_id"] in source_transcript_ids
        assert moment["stakes_source_transcript_id"] in source_transcript_ids
        moment_receipt = engine.store.receipt(
            brief["human_context"]["moment_receipt_id"]
        )
        assert moment_receipt is not None
        assert moment in moment_receipt["payload"]["moments"]

        stored_run = engine.store.workflow_runs(
            script_id=script["script_id"], limit=1
        )[0]
        assert stored_run["workflow_id"] == workflow["workflow_id"]
        assert set(stored_run["stage_receipts"]) == {
            "brief_receipt_id",
            "narrative_audit_id",
            "relatability_audit_id",
            "qualitative_relatability_audit_id",
            "cohort_relatability_audit_id",
            "transcript_style_audit_id",
            "attention_audit_id",
            "video_preflight_audit_id",
        }
        assert all(stored_run["stage_receipts"].values())
        gates = engine.store.script_gate_summary(script["script_id"])
        assert gates["ready_for_render"] is True
        assert len(gates["latest_audits"]) == 7
        assert all(
            audit["stored_script_binding_valid"]
            for audit in gates["latest_audits"].values()
        )


def test_variant_selector_rejects_invalid_or_unavailable_indexes_with_audit(
    tmp_path,
):
    token = "script-variant-rejection-token"
    app, engine = app_and_engine(
        tmp_path, CONTENT_QUALITY_CONTROL_TOKEN=token
    )
    client = app.test_client()
    headers = {"Authorization": f"Bearer {token}"}
    request_body = {
        "topic": "AI automation",
        "audience": "software founders",
        "objective": "qualified_attention",
    }

    invalid = client.post(
        "/api/script-intelligence/briefs",
        json={**request_body, "variant_index": "1"},
        headers=headers,
    )
    assert invalid.status_code == 400
    assert invalid.get_json()["code"] == "INVALID_REQUEST"

    unavailable = client.post(
        "/api/script-intelligence/run",
        json={**request_body, "variant_index": 7},
        headers=headers,
    )
    assert unavailable.status_code == 409
    body = unavailable.get_json()
    assert body["phase"] == "variant_selection"
    assert body["script_generated"] is False
    attempt = body["brief_attempt"]
    assert attempt["code"] == "SCRIPT_VARIANT_INDEX_NOT_AVAILABLE"
    assert attempt["detail"]["available_variant_count"] == 5
    assert attempt["detail"]["available_variant_indexes"] == [0, 1, 2, 3, 4]
    assert "demand_feedback" not in attempt
    assert engine.store.receipt(attempt["attempt_receipt_id"]) is not None

    query = (
        "SELECT " + "op" + "eration, outcome FROM cq_agent_queries WHERE "
        + "op" + "eration IN ('build_script_brief', 'run_trend_to_script') "
        "ORDER BY created_at"
    )
    with closing(sqlite3.connect(engine.store.path)) as connection:
        rows = connection.execute(query).fetchall()
    assert rows == [
        ("build_script_brief", "rejected"),
        ("run_trend_to_script", "rejected"),
    ]


def test_configured_ai_relatability_is_a_separate_blocking_verdict(tmp_path):
    verdict = {
        "relatable": True,
        "score": 84,
        "rubric_scores": {
            "concrete_lived_moment": 22,
            "clear_personal_stakes": 17,
            "visible_input_action_output": 16,
            "source_language_support": 13,
            "direct_audience_perspective": 8,
            "non_alienating_framing": 8,
        },
        "audience_moment": "A founder feels stuck under automation pressure.",
        "why_it_feels_human": ["It names the pressure before offering advice."],
        "alienating_language": [],
        "source_language_used": ["feel", "stuck", "pressure"],
        "rewrite_guidance": [],
    }
    app, _engine = app_and_engine(
        tmp_path,
        RELATABILITY_LLM_RUNNER=lambda _prompt: json.dumps(verdict),
    )
    client = app.test_client()
    brief = client.post(
        "/api/script-intelligence/briefs",
        json={
            "topic": "AI automation",
            "audience": "software founders",
            "objective": "qualified_attention",
        },
    ).get_json()

    result = client.post(
        "/api/script-intelligence/generate-and-audit",
        json={"brief_id": brief["brief_id"]},
    ).get_json()

    audit = result["audits"]["qualitative_relatability"]
    assert result["status"] == "approved"
    assert result["decisions"]["qualitative_relatability"] is True
    assert audit["decision"] == "PASS"
    assert audit["qualitative_verdict"]["ai_evaluated"] is True
    assert audit["qualitative_verdict"]["judgment"]["score"] == 84


def test_configured_ai_relatability_failure_blocks_render(tmp_path):
    def unavailable(_prompt):
        raise RuntimeError("provider unavailable")

    app, _engine = app_and_engine(
        tmp_path,
        RELATABILITY_LLM_RUNNER=unavailable,
    )
    client = app.test_client()
    brief = client.post(
        "/api/script-intelligence/briefs",
        json={
            "topic": "AI automation",
            "audience": "software founders",
            "objective": "qualified_attention",
        },
    ).get_json()

    result = client.post(
        "/api/script-intelligence/generate-and-audit",
        json={"brief_id": brief["brief_id"]},
    ).get_json()

    audit = result["audits"]["qualitative_relatability"]
    assert result["status"] == "revise"
    assert result["ready_for_render"] is False
    assert result["decisions"]["qualitative_relatability"] is False
    assert audit["decision"] == "JUDGE_UNAVAILABLE"


def test_one_call_product_route_builds_brief_and_audits_script(tmp_path):
    app, _engine = app_and_engine(tmp_path)
    client = app.test_client()

    response = client.post(
        "/api/script-intelligence/run",
        json={
            "topic": "AI automation",
            "audience": "software founders",
            "objective": "qualified_attention",
        },
    )

    assert response.status_code == 200
    result = response.get_json()
    assert result["phase"] == "script_audited"
    assert result["script_generated"] is True
    assert result["brief"]["status"] == "ready"
    assert result["workflow"]["brief_id"] == result["brief"]["brief_id"]
    assert result["workflow"]["status"] in {"approved", "revise"}
    assert len(result["agent_query"]["response_sha256"]) == 64


def test_passing_artifact_remains_bound_after_newer_monotonic_recheck(tmp_path):
    _app, engine = app_and_engine(tmp_path)
    video_id = "youtube:video:script-source-0"
    with closing(sqlite3.connect(engine.tape.path)) as connection:
        creator_id = "youtube:creator:0"
        observed_at = datetime.now(UTC).isoformat()
        raw_sha = hashlib.sha256(b"newer-monotonic-raw").hexdigest()
        connection.execute(
            "INSERT INTO mt_raw_objects VALUES (?, ?, 100, ?, 'youtube-recheck')",
            (raw_sha, str(tmp_path / "newer.json.gz"), observed_at),
        )
        cursor = connection.execute(
            """INSERT INTO mt_market_observations(
                   observation_key, run_id, observed_at, wall_clock_date,
                   video_id, creator_id, platform, source_id, video_age_seconds,
                   video_age_bucket, views, likes, comments, shares, saves,
                   creator_followers, view_velocity, view_acceleration, view_jerk,
                   relative_strength, raw_sha256, source_confidence
               ) VALUES ('newer-observation', 'recheck', ?, ?, ?, ?, 'youtube',
                         'youtube-recheck', 9000, '1h-6h', 40000, 2000, 140, 80,
                         25, 10000, 130, 10, 1, 2.6, ?, 1)""",
            (observed_at, observed_at[:10], video_id, creator_id, raw_sha),
        )
        connection.execute(
            """INSERT INTO mt_accepted_observation_evidence(
                   evidence_id, observation_id, observation_key, video_id, creator_id,
                   accepted_at, contract, evidence_scope, published_at, title, caption,
                   description, language, url, thumbnail_url, media_type,
                   duration_seconds, hashtags_json, discovery_queries_json,
                   discovery_context_json
               ) SELECT 'accepted:newer-observation:full', ?, 'newer-observation',
                        video_id, creator_id, ?,
                        'market_tape_accepted_observation_evidence_v1', 'full',
                        published_at, title, caption, description, language, url,
                        thumbnail_url, media_type, duration_seconds, hashtags_json,
                        discovery_queries_json, discovery_context_json
                 FROM mt_accepted_full_evidence_v1 WHERE video_id=? LIMIT 1""",
            (cursor.lastrowid, observed_at, video_id),
        )
        connection.commit()

    latest = engine.tape.candidates("AI automation", limit=10)
    assert next(row for row in latest if row["video_id"] == video_id)["observation_key"] == "newer-observation"
    bound = engine.tape.artifact_bound_candidates([video_id])
    assert bound[0]["observation_key"] == "accepted-script-observation-0"
    discovered = engine.viral.discover_for_videos("AI automation", [video_id], limit=1)
    assert discovered["receipt_count"] == 1
    assert discovered["receipts"][0]["payload"]["observation_key"] == "accepted-script-observation-0"


def test_duplicate_topic_receipts_cannot_inflate_transcript_cohort(tmp_path):
    _app, engine = app_and_engine(tmp_path)
    discovery = engine.viral.discover_for_videos(
        "AI automation", ["youtube:video:script-source-0"], limit=1
    )
    original = discovery["receipts"][0]
    receipt_ids = []
    for index in range(5):
        payload = {**original["payload"], "topic": f"AI automation angle {index}"}
        receipt = engine.store.put_receipt(
            "viral_transcript_pattern", "youtube", f"duplicate-topic-{index}",
            original["source_url"], payload,
        )
        receipt_ids.append(receipt["receipt_id"])
    result = engine.scripts.generate({
        "topic": "AI automation",
        "audience": "software founders",
        "claim": "Reduce the pressure before adding another step",
        "human_moment": {
            "situation": "You feel burned out and stuck",
            "stakes": "trying another workflow makes the day feel harder",
        },
        "receipt_ids": receipt_ids,
    })
    assert result["status"] == "rejected"
    assert result["code"] == "REJECT_INSUFFICIENT_TRANSCRIPT_COHORT"
    assert result["verified_transcript_count"] == 1


def test_generation_rejects_cross_topic_or_receipt_overrides(tmp_path):
    app, _engine = app_and_engine(tmp_path)
    client = app.test_client()
    brief = client.post(
        "/api/script-intelligence/briefs",
        json={"topic": "AI automation", "audience": "software founders"},
    ).get_json()
    response = client.post(
        "/api/script-intelligence/generate-and-audit",
        json={
            "brief_id": brief["brief_id"],
            "topic": "unrelated celebrity trend",
            "receipt_ids": ["unrelated-receipt"],
        },
    )
    assert response.status_code == 422
    body = response.get_json()
    assert body["code"] == "REJECT_SCRIPT_BRIEF_OVERRIDE"
    assert body["forbidden_fields"] == ["receipt_ids", "topic"]


def test_generation_fails_closed_when_cohort_manifest_changes_after_brief_creation(
    tmp_path,
):
    _app, engine = app_and_engine(tmp_path)
    brief = engine.script_intelligence.build_brief({
        "topic": "AI automation",
        "audience": "software founders",
        "objective": "qualified_attention",
    })
    assert brief["status"] == "ready", brief

    manifest_path = Path(brief["language"]["cohort_manifest_path"])
    original_manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    expected_hash = canonical_sha256(original_manifest)
    assert brief["language"]["cohort_manifest_sha256"] == expected_hash

    mutated_manifest = {
        **original_manifest,
        "cohort_id": "cohort_tampered_after_brief_creation",
        "topic": "tampered after brief creation",
    }
    manifest_path.write_text(
        json.dumps(mutated_manifest, sort_keys=True), encoding="utf-8"
    )
    actual_hash = canonical_sha256(mutated_manifest)
    assert actual_hash != expected_hash

    result = engine.script_intelligence.generate_and_audit({
        "brief_id": brief["brief_id"],
    })

    assert result["status"] == "revise"
    assert result["ready_for_render"] is False
    assert result["decisions"]["cohort_integrity"] is False
    quality_audit = result["audits"]["transcript_cohort_relatability"]
    assert quality_audit["decision"] == "REJECT_NOT_RELATABLE"
    assert quality_audit["findings"]["expected_cohort_id"] == brief[
        "language"
    ]["cohort_id"]
    assert quality_audit["findings"]["actual_cohort_id"] == (
        "cohort_tampered_after_brief_creation"
    )
    assert quality_audit["findings"][
        "expected_cohort_manifest_sha256"
    ] == expected_hash
    assert quality_audit["findings"][
        "actual_cohort_manifest_sha256"
    ] == actual_hash
    assert quality_audit["findings"]["cohort_manifest_binding_valid"] is False
    binding = quality_audit["findings"]["findings"][
        "cohort_manifest_binding"
    ]
    assert binding == {
        "contract": "immutable_brief_cohort_manifest_binding_v1",
        "expected_cohort_id": brief["language"]["cohort_id"],
        "actual_cohort_id": "cohort_tampered_after_brief_creation",
        "cohort_id_matches": False,
        "expected_cohort_manifest_sha256": expected_hash,
        "actual_cohort_manifest_sha256": actual_hash,
        "cohort_manifest_sha256_matches": False,
        "manifest_payload_is_object": True,
        "manifest_load_error": None,
        "binding_valid": False,
    }
    assert quality_audit["findings"]["findings"]["checks"][
        "cohort_manifest_binding_valid"
    ] is False
    assert "cohort_manifest_binding_valid" in quality_audit["findings"][
        "findings"
    ]["failures"]

    with closing(sqlite3.connect(engine.tape.path)) as connection:
        row = connection.execute(
            """SELECT cohort_id, cohort_manifest_sha256, findings_json
               FROM mt_script_relatability_audits WHERE audit_id=?""",
            (quality_audit["findings"]["market_tape_audit_id"],),
        ).fetchone()
    assert row is not None
    assert row[0] == brief["language"]["cohort_id"]
    assert row[1] == actual_hash
    persisted_binding = json.loads(row[2])["cohort_manifest_binding"]
    assert persisted_binding["expected_cohort_manifest_sha256"] == expected_hash
    assert persisted_binding["actual_cohort_manifest_sha256"] == actual_hash
    assert persisted_binding["binding_valid"] is False


def test_brief_skips_stronger_candidate_without_verified_language_cohort(tmp_path):
    _app, engine = app_and_engine(tmp_path)
    seed_competing_trend(
        engine.tape.path,
        trend_id="trend:topic:ai-automation-dance-transition",
        trend_type="topic",
        display_name="AI automation dance transition",
        trend_strength=120,
        member_indexes=[0, 1],
    )
    seed_competing_trend(
        engine.tape.path,
        trend_id="trend:format:ai-automation-split-screen",
        trend_type="format",
        display_name="AI automation split screen format",
        trend_strength=999,
        member_indexes=[0, 1, 2, 3, 4],
    )

    ranked = engine.script_intelligence._trend_groups("AI automation", limit=20)
    assert [row["trend_id"] for row in ranked[:3]] == [
        "trend:topic:ai-automation-dance-transition",
        "trend:topic:ai-automation-pressure",
        "trend:format:ai-automation-split-screen",
    ]
    assert ranked[0]["topic_affinity"] == "topic_like"
    assert ranked[2]["topic_affinity"] == "generic_format"

    brief = engine.script_intelligence.build_brief({
        "topic": "AI automation",
        "audience": "software founders",
    })
    assert brief["status"] == "ready", brief
    assert brief["trend"]["trend_id"] == "trend:topic:ai-automation-pressure"
    audit = brief["selection_audit"]
    assert audit["selected_trend_id"] == "trend:topic:ai-automation-pressure"
    assert audit["assessed_candidate_count"] == 2
    assert audit["semantic_candidate_query_count"] == 1
    assert audit["shared_semantic_candidate_count"] == 5
    assert [row["decision"] for row in audit["candidate_assessments"]] == [
        "REJECT", "PASS",
    ]
    rejected = audit["candidate_assessments"][0]
    assert rejected["code"] == "INSUFFICIENT_VERIFIED_LANGUAGE_COHORT"
    assert rejected["gates"]["minimum_verified_transcripts"] == {
        "actual": 2, "minimum": 5, "pass": False,
    }
    assert audit["candidate_assessments"][1]["code"] == (
        "SCRIPT_READY_TREND_CANDIDATE"
    )


def test_no_qualifying_trend_returns_persisted_candidate_assessments(tmp_path):
    _app, engine = app_and_engine(tmp_path)
    with closing(sqlite3.connect(engine.tape.path)) as connection:
        connection.execute(
            """DELETE FROM mt_transcript_artifacts
               WHERE video_id NOT IN (
                   'youtube:video:script-source-0',
                   'youtube:video:script-source-1'
               )"""
        )
        connection.commit()

    result = engine.script_intelligence.build_brief({
        "topic": "AI automation",
        "audience": "software founders",
    })
    assert result["status"] == "not_ready"
    assert result["code"] == "NO_SCRIPT_READY_TREND_CANDIDATE"
    detail = result["detail"]
    assert detail["selection_contract"] == "script_intelligence_trend_selection_v3"
    assert detail["candidate_count"] == 1
    assert detail["assessed_candidate_count"] == 1
    assert detail["semantic_candidate_query_count"] == 1
    assert detail["shared_semantic_candidate_count"] == 2
    assessment = detail["candidate_assessments"][0]
    assert assessment["contract"] == (
        "script_intelligence_trend_candidate_assessment_v1"
    )
    assert assessment["decision"] == "REJECT"
    assert assessment["failed_gates"] == [
        "minimum_verified_transcripts",
        "minimum_distinct_creators",
        "minimum_observed_views",
    ]
    stored = engine.store.receipt(result["attempt_receipt_id"])
    assert stored["receipt_type"] == "script_intelligence_attempt"
    assert stored["payload"]["detail"]["candidate_assessments"] == [assessment]


def test_failed_brief_persists_refusal_then_enqueues_bounded_language_demand(
    tmp_path,
):
    with demand_api() as client:
        _app, engine = app_and_engine(
            tmp_path, MARKET_TAPE_DEMAND_CLIENT=client
        )
        with closing(sqlite3.connect(engine.tape.path)) as connection:
            connection.execute(
                """DELETE FROM mt_transcript_artifacts
                   WHERE video_id NOT IN (
                       'youtube:video:script-source-0',
                       'youtube:video:script-source-1'
                   )"""
            )
            connection.commit()

        result = engine.script_intelligence.build_brief({
            "topic": "AI automation",
            "audience": "software founders",
            "objective": "qualified_attention",
        })

    assert result["status"] == "not_ready"
    assert result["attempt_receipt_id"]
    assert result["demand_feedback"] == {
        "status": "queued",
        "demand_id": "demand_test_language_gap",
        "state": "requested",
        "idempotent": False,
        "receipt_id": result["demand_feedback"]["receipt_id"],
    }
    assert len(DemandAPIHandler.received) == 1
    submitted = DemandAPIHandler.received[0]
    assert submitted["path"] == "/api/market-tape/script-language-demands"
    assert submitted["authorization"] == "Bearer market-tape-test-token"
    demand = submitted["body"]
    assert demand["contract"] == "market_tape_script_language_demand_v1"
    assert demand["source_receipt_id"] == result["attempt_receipt_id"]
    assert demand["topic"] == "AI automation"
    assert demand["audience"] == "software founders"
    assert demand["acquisition_policy"] == {
        "cycles": 1,
        "platforms": ["youtube", "tiktok", "instagram", "facebook"],
        "discovery_limit": 50,
        "transcript_limit": 6,
        "whisper_model": "base",
        "creator_diverse": True,
        "same_call_retry": False,
    }
    failure = engine.store.receipt(result["attempt_receipt_id"])
    feedback = engine.store.receipt(result["demand_feedback"]["receipt_id"])
    assert failure["receipt_type"] == "script_intelligence_attempt"
    assert feedback["receipt_type"] == "script_language_demand_enqueue"
    assert feedback["payload"]["attempt_receipt_id"] == failure["receipt_id"]
    assert len(feedback["payload"]["request_sha256"]) == 64
