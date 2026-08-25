import hashlib
import json
import sqlite3
import threading
from contextlib import closing
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

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
    transcript = (
        "You feel burned out and stuck when AI automation adds more pressure instead of "
        "removing work. The tools keep multiplying, and trying another workflow makes the "
        "day feel harder. Creators worry they are losing time while the promise of an easier "
        "system keeps moving farther away. The useful change is to name the pressure first, "
        "show one measured result, and make the next step small enough to try without adding "
        "another exhausting process."
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
                            "performance_views_floor": True,
                            "performance_engagement_floor": True,
                        },
                    }, sort_keys=True),
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
    script_id = result["script"]["script_id"]
    lineage = client.get(
        f"/api/script-intelligence/scripts/{script_id}",
        headers={"X-Agent-Principal": "integration-test-agent"},
    ).get_json()
    assert lineage["gates"]["ready_for_render"] is True
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
    assert detail["selection_contract"] == "script_intelligence_trend_selection_v2"
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
        "platforms": ["youtube"],
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
