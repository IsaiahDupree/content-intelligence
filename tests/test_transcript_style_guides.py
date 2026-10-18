import json
import runpy
import sqlite3
from contextlib import closing
from pathlib import Path

from services.content_quality.api import create_content_quality_app
from services.content_quality.copy_policy import build_script_only_provenance


SCRIPT_FIXTURES = runpy.run_path(
    str(Path(__file__).with_name("test_script_intelligence_integration.py"))
)
seed_script_ready_tape = SCRIPT_FIXTURES["seed_script_ready_tape"]


def tiktok_app_and_engine(tmp_path, token=""):
    tape_path, transcript_root = seed_script_ready_tape(tmp_path)
    with closing(sqlite3.connect(tape_path)) as connection:
        trigger_rows = connection.execute(
            """SELECT name, sql FROM sqlite_master
               WHERE type='trigger'
                 AND tbl_name IN (
                     'mt_creators', 'mt_videos', 'mt_market_observations',
                     'mt_transcript_artifacts'
                 )"""
        ).fetchall()
        for name, _sql in trigger_rows:
            connection.execute(f'DROP TRIGGER "{name}"')
        connection.execute("UPDATE mt_creators SET platform='tiktok'")
        connection.execute(
            """UPDATE mt_videos
               SET platform='tiktok',
                   url='https://www.tiktok.com/@source/video/' || external_id"""
        )
        connection.execute(
            "UPDATE mt_market_observations SET platform='tiktok'"
        )
        connection.execute(
            """UPDATE mt_transcript_artifacts
               SET platform='tiktok',
                   source_url='https://www.tiktok.com/@source/video/' || external_id"""
        )
        for _name, sql in trigger_rows:
            if sql:
                connection.execute(sql)
        connection.commit()
    app = create_content_quality_app({
        "TESTING": True,
        "CONTENT_QUALITY_CONTROL_TOKEN": token,
        "MARKET_TAPE_DB": tape_path,
        "CONTENT_QUALITY_DB": tmp_path / "content-quality.sqlite3",
        "TRANSCRIPT_BANK_ROOT": transcript_root,
        "HEALTH_CACHE_SECONDS": 0,
    })
    return app, app.extensions["content_quality_engine"]


def tiktok_pattern_receipts(engine):
    video_ids = [
        f"youtube:video:script-source-{index}" for index in range(5)
    ]
    result = engine.viral.discover_for_videos(
        "AI automation", video_ids, limit=5
    )
    assert result["status"] == "complete"
    assert result["receipt_count"] == 5
    return result["receipts"]


def test_tiktok_style_guide_is_aggregate_durable_and_rights_safe(tmp_path):
    _app, engine = tiktok_app_and_engine(tmp_path)
    receipts = tiktok_pattern_receipts(engine)
    result = engine.style_guides.build({
        "topic": "AI automation pressure",
        "platform": "tiktok",
        "receipt_ids": [row["receipt_id"] for row in receipts],
    })

    assert result["status"] == "ready"
    guide = result["guide"]
    assert guide["contract"] == "aggregate_transcript_style_guide_v1"
    assert guide["evidence"]["verified_transcript_count"] == 5
    assert guide["evidence"]["distinct_creator_count"] == 5
    assert guide["evidence"]["observed_views_snapshot"] == 150000
    assert guide["delivery"][
        "pitch_timbre_or_actual_vocal_inflection_measured"
    ] is False
    assert guide["rights_and_originality"][
        "source_identity_likeness_or_voice_allowed"
    ] is False
    assert guide["rights_and_originality"][
        "distinctive_source_wording_allowed"
    ] is False
    assert "You feel burned out and stuck" not in json.dumps(guide)

    stored = engine.style_guides.resolve(result["receipt"]["receipt_id"])
    assert stored is not None
    assert stored["payload"]["guide_id"] == guide["guide_id"]


def test_tiktok_style_guide_fails_closed_below_verified_cohort(tmp_path):
    app, engine = tiktok_app_and_engine(tmp_path)
    receipts = tiktok_pattern_receipts(engine)
    response = app.test_client().post(
        "/api/transcript-style-guides/build",
        json={
            "topic": "AI automation pressure",
            "platform": "tiktok",
            "receipt_ids": [row["receipt_id"] for row in receipts[:4]],
        },
    )

    assert response.status_code == 422
    body = response.get_json()
    assert body["code"] == "INSUFFICIENT_TIKTOK_STYLE_EVIDENCE"
    assert body["gates"]["minimum_verified_transcripts"]["pass"] is False
    assert body["acquisition"]["path"] == "/api/market-tape/full-pipeline"


def test_style_copy_gate_rejects_source_transcript_reuse(tmp_path):
    _app, engine = tiktok_app_and_engine(tmp_path)
    receipts = tiktok_pattern_receipts(engine)
    built = engine.style_guides.build({
        "topic": "AI automation pressure",
        "platform": "tiktok",
        "receipt_ids": [row["receipt_id"] for row in receipts],
    })
    source = engine.tape.artifact_bound_candidates([
        receipts[0]["payload"]["video_id"]
    ])[0]["transcript"]
    audit = engine.style_guides.audit({
        "style_guide_id": built["guide"]["guide_id"],
        "text": source,
        "target_duration_seconds": 45,
        "provenance": build_script_only_provenance(source),
    })

    assert audit["decision"] == "REVISE"
    assert audit["findings"]["copy_gate"]["passed"] is False
    assert "COPIED_EXPRESSION" in audit["findings"]["copy_gate"][
        "failure_codes"
    ]


def test_tiktok_brief_freezes_style_receipt_and_script_passes_style_gate(
    tmp_path,
):
    app, _engine = tiktok_app_and_engine(tmp_path)
    client = app.test_client()
    brief_response = client.post(
        "/api/script-intelligence/briefs",
        json={
            "topic": "AI automation",
            "audience": "software founders",
            "style_platform": "tiktok",
        },
    )

    assert brief_response.status_code == 201, brief_response.get_json()
    brief = brief_response.get_json()
    style = brief["language"]["style_guide"]
    assert style["platform"] == "tiktok"
    assert brief["generation_input"]["style_guide_id"] == style["guide_id"]
    assert brief["generation_input"]["style_guide_receipt_id"] == style[
        "receipt_id"
    ]

    generated = client.post(
        "/api/script-intelligence/generate-and-audit",
        json={"brief_id": brief["brief_id"]},
    )
    assert generated.status_code == 200, generated.get_json()
    body = generated.get_json()
    assert body["decisions"]["transcript_style"] is True
    assert body["audits"]["transcript_style"]["decision"] == "PASS"
    assert body["script"]["style_guide_receipt_id"] == style["receipt_id"]
    assert style["receipt_id"] not in body["script"]["source_receipt_ids"]


def test_style_service_status_and_agent_auth_are_exposed(tmp_path):
    token = "style-test-token"
    app, engine = tiktok_app_and_engine(tmp_path, token=token)
    tiktok_pattern_receipts(engine)
    client = app.test_client()

    assert client.get(
        "/api/transcript-style-guides/status?platform=tiktok"
    ).status_code == 401
    response = client.get(
        "/api/transcript-style-guides/status?platform=tiktok",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert response.status_code == 200
    status = response.get_json()
    assert status["transcript_artifacts"]["total"] == 5
    assert status["transcript_artifacts"]["performance_audit_pass"] == 5
    assert status["verified_pattern_receipts"] == 5
    assert status["status"] == "ready"

    catalog = client.get(
        "/api/agent/catalog",
        headers={"Authorization": f"Bearer {token}"},
    ).get_json()
    assert "build_transcript_style_guide" in catalog["operations"]
    assert "audit_transcript_style" in catalog["operations"]
