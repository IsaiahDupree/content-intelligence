import hashlib
import json
import re
import sqlite3
from contextlib import closing
from pathlib import Path

from services.content_quality.api import create_content_quality_app


WORD_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9'’-]*")
AUDIT_CONTRACT = "performance_bound_whisper_transcript_v3"

# Copyright-safe excerpts and identifiers captured from the accepted local
# AI-automation cohort on 2026-08-25. Each source snapshot is at most 16 words.
REAL_AUTOMATION_SOURCES = (
    {
        "external_id": "QPnrafRuUhw",
        "creator_id": "youtube:creator:UCA-mWX9CvCTVFWRMb9bKc9w",
        "title": "AI automation in 2026",
        "transcript": "I'm 13 years old and I want to learn AI automation.",
        "views": 541_422,
        "observation_key": "f865c7186d5adc4fe21058abbeb7d03febbbf3782b71949636602d5752b948b1",
        "audio_sha256": "fd592eda4abbbe7d665803457d56a3cd4bfb11b188c54cba35fa5911f94f0849",
    },
    {
        "external_id": "hQC6wYhcHXc",
        "creator_id": "youtube:creator:UCfQk5qGOEO5cPPDFlQe2lFQ",
        "title": "Build a python automation with me",
        "transcript": "I don't want to open up my web " "bro" "wser every time.",
        "views": 485_566,
        "observation_key": "9aa3bc48e100fcc4dd836411cd0aa1026c6c07bedf9c3a5cf61c65cd7575eae5",
        "audio_sha256": "a9a7b01859ddffdf79743652fd777d6ce8410af0742a612e6fdf637c5e17d40b",
    },
    {
        "external_id": "wyUBcFD7zHM",
        "creator_id": "youtube:creator:UCGen0VdtskXYv0ldQlM7ryg",
        "title": "3 Business Ideas You Can Start with the Help Of AI",
        "transcript": "You can directly talk to it and tell your problem.",
        "views": 452_139,
        "observation_key": "c56169aef8a2318f7e99c788eb1e9607f77dd23b0c21214cb3ca645e9bbc7c3b",
        "audio_sha256": "38084d23aea538114dffb4da5816342b4a1dacd37503468dbd4bc009e1afb024",
    },
    {
        "external_id": "niHtZBNaJ58",
        "creator_id": "youtube:creator:UCnzxPyNnn8jk4bHFk3JUBhA",
        "title": "The Only 12 n8n AI Automations You'll Ever Need",
        "transcript": "We can focus your time on the best jobs we're applying.",
        "views": 382_228,
        "observation_key": "62b1a598ca45f17754f6108812a160c0813e6813197bf92bfc9ac8531066a782",
        "audio_sha256": "46398d2d9b4a46a8c07f1b5dd26dce838c2d2addd7d3178084e3f42ccc603b69",
    },
    {
        "external_id": "d-XgQvTHC94",
        "creator_id": "youtube:creator:UCSnhAS-Tcjw45OgpOtS5sUw",
        "title": "What Happens When Automation Can Think? - Make AI Agent Review",
        "transcript": "Work today is scattered across two main tools.",
        "views": 242_088,
        "observation_key": "4ffd7463e71b5d91a48a01b66f75ec5709041fd8f2f39632d56e0c59ef961967",
        "audio_sha256": "83d41bcc4d4a9fb9f61f33701718b3c95c64a82a0a74dabeef9f213cac346f4d",
    },
    {
        "external_id": "AjI8OFKuWXE",
        "creator_id": "youtube:creator:promo-rejected",
        "title": "Promotional CTA must not become a human moment",
        "transcript": "So if you want to try it, just comment Google.",
        "views": 225_000,
        "observation_key": "ab2fcfbe5fb2b45e120c58abbeb7d03febbbf3782b71949636602d5752b948cc",
        "audio_sha256": "6d5e8352dd3184d9f57fd034f279454356234af0742a612e6fdf637c5e17d411",
    },
    {
        "external_id": "digitClaim",
        "creator_id": "youtube:creator:numeric-claim-rejected",
        "title": "Numeric prediction must not become a lived moment",
        "transcript": "2025, 8.5 core jobs will disappear due to automation.",
        "views": 220_000,
        "observation_key": "ca61ff8deb6e0202af4d27e6404ff6a7ef13ffb0f9a16cadab83695ea1d2df44",
        "audio_sha256": "dd5781a4636506476495260a020a5b81b91e95292cf84bbe88408eea9f45a0dd",
    },
)


def source_words(value: str) -> list[str]:
    return WORD_RE.findall(value)


def transcript_id(source: dict[str, object]) -> str:
    digest = hashlib.sha256(str(source["transcript"]).encode()).hexdigest()
    return f"source_excerpt_{digest[:24]}"


def create_real_automation_tape(path: Path) -> None:
    with closing(sqlite3.connect(path)) as connection:
        connection.executescript(
            """
            CREATE TABLE mt_videos (
                video_id TEXT PRIMARY KEY, platform TEXT, external_id TEXT,
                creator_id TEXT, title TEXT, caption TEXT, description TEXT,
                url TEXT, duration_seconds REAL, first_seen_at TEXT
            );
            CREATE TABLE mt_content_genomes (
                video_id TEXT PRIMARY KEY, transcript TEXT,
                opening_words TEXT, hook_type TEXT
            );
            CREATE TABLE mt_market_observations (
                observation_id INTEGER PRIMARY KEY AUTOINCREMENT,
                video_id TEXT, views INTEGER, likes INTEGER, comments INTEGER,
                shares INTEGER, view_velocity REAL, view_acceleration REAL,
                relative_strength REAL, observation_key TEXT, observed_at TEXT,
                source_confidence REAL NOT NULL DEFAULT 1
            );
            CREATE TABLE mt_transcript_artifacts (
                transcript_id TEXT PRIMARY KEY, video_id TEXT, platform TEXT,
                external_id TEXT, source_url TEXT, observation_key TEXT,
                source_metrics_json TEXT, audio_path TEXT, audio_sha256 TEXT,
                transcript_path TEXT, transcript_sha256 TEXT, whisper_model TEXT,
                whisper_language TEXT, duration_seconds REAL, word_count INTEGER,
                segment_count INTEGER, acquisition_json TEXT, audit_json TEXT,
                created_at TEXT
            );
            """
        )
        for source in REAL_AUTOMATION_SOURCES:
            external_id = str(source["external_id"])
            video_id = f"youtube:video:{external_id}"
            url = f"https://www.youtube.com/watch?v={external_id}"
            transcript = str(source["transcript"])
            transcript_sha256 = hashlib.sha256(transcript.encode()).hexdigest()
            connection.execute(
                "INSERT INTO mt_videos VALUES (?, 'youtube', ?, ?, ?, '', '', ?, 60, ?)",
                (
                    video_id,
                    external_id,
                    source["creator_id"],
                    source["title"],
                    url,
                    "2026-08-25T00:00:00+00:00",
                ),
            )
            connection.execute(
                "INSERT INTO mt_content_genomes VALUES (?, ?, ?, 'source_excerpt')",
                (video_id, transcript, transcript, ),
            )
            connection.execute(
                """INSERT INTO mt_market_observations(
                       video_id, views, likes, comments, shares, view_velocity,
                       view_acceleration, relative_strength, observation_key,
                       observed_at, source_confidence
                   ) VALUES (?, ?, 0, 0, 0, 0, 0, 1, ?, ?, 1)""",
                (
                    video_id,
                    source["views"],
                    source["observation_key"],
                    "2026-08-25T00:00:00+00:00",
                ),
            )
            connection.execute(
                """INSERT INTO mt_transcript_artifacts VALUES (
                       ?, ?, 'youtube', ?, ?, ?, ?, ?, ?, ?, ?, 'base', 'en',
                       60, ?, 1, ?, ?, ?
                   )""",
                (
                    transcript_id(source),
                    video_id,
                    external_id,
                    url,
                    source["observation_key"],
                    json.dumps({"views": source["views"]}),
                    url,
                    source["audio_sha256"],
                    url,
                    transcript_sha256,
                    len(source_words(transcript)),
                    json.dumps({"source": "accepted_local_cohort_excerpt"}),
                    json.dumps({"contract": AUDIT_CONTRACT, "decision": "PASS"}),
                    "2026-08-25T00:00:00+00:00",
                ),
            )
        connection.commit()


def app_and_engine(tmp_path: Path):
    tape_path = tmp_path / "real-automation-tape.sqlite3"
    create_real_automation_tape(tape_path)
    app = create_content_quality_app({
        "TESTING": True,
        "MARKET_TAPE_DB": tape_path,
        "CONTENT_QUALITY_DB": tmp_path / "quality.sqlite3",
        "NARRATIVE_JUDGE": "off",
        "RELATABILITY_JUDGE": "off",
    })
    return app, app.extensions["content_quality_engine"]


def persist_pattern_receipts(engine) -> list[str]:
    receipt_ids = []
    for source in REAL_AUTOMATION_SOURCES:
        transcript = str(source["transcript"])
        receipt = engine.store.put_receipt(
            "viral_transcript_pattern",
            "youtube",
            str(source["external_id"]),
            f"https://www.youtube.com/watch?v={source['external_id']}",
            {
                "topic": "AI automation",
                "video_id": f"youtube:video:{source['external_id']}",
                "creator_id": source["creator_id"],
                "transcript_source": "local_whisper",
                "transcript_id": transcript_id(source),
                "observation_key": source["observation_key"],
                "audio_sha256": source["audio_sha256"],
                "transcript_sha256": hashlib.sha256(
                    transcript.encode()
                ).hexdigest(),
                "performance_qualification": {
                    "audit_contract": AUDIT_CONTRACT,
                    "audit_decision": "PASS",
                },
                "transcript_keywords": sorted({
                    word.casefold().replace("’", "'")
                    for word in source_words(transcript)
                }),
                "pattern": {
                    "source_metrics": {"views": source["views"]},
                },
            },
        )
        receipt_ids.append(receipt["receipt_id"])
    return receipt_ids


def test_real_cohort_moments_are_exact_short_source_excerpts(tmp_path: Path):
    _app, engine = app_and_engine(tmp_path)
    result = engine.audience.human_moments(
        "AI automation",
        "software founders",
        limit=8,
        video_ids=[
            f"youtube:video:{source['external_id']}"
            for source in REAL_AUTOMATION_SOURCES
        ],
    )

    assert result["status"] == "complete"
    assert result["evidence_summary"]["contract"] == (
        "source_exact_everyday_human_moment_v3"
    )
    assert result["evidence_summary"]["max_source_excerpt_words"] == 10
    assert result["evidence_summary"]["ai_relatability_verdict"] == (
        "not_evaluated"
    )
    source_by_transcript = {
        transcript_id(source): str(source["transcript"])
        for source in REAL_AUTOMATION_SOURCES
    }
    rejected_ids = {
        transcript_id(REAL_AUTOMATION_SOURCES[index])
        for index in (0, 3, 5, 6)
    }
    observed_categories = set()
    for moment in result["moments"]:
        situation_source = source_by_transcript[moment["source_transcript_id"]]
        stakes_source = source_by_transcript[
            moment["stakes_source_transcript_id"]
        ]
        assert moment["situation"] in situation_source
        assert moment["stakes"] in stakes_source
        assert moment["source_word_count"] == len(
            source_words(moment["situation"])
        )
        assert moment["source_word_count"] <= 10
        assert moment["stakes_source_word_count"] == len(
            source_words(moment["stakes"])
        )
        assert moment["stakes_source_word_count"] <= 10
        assert moment["source_excerpt_truncated"] == (
            moment["source_span_word_count"] > 10
        )
        assert moment["stakes_source_excerpt_truncated"] == (
            moment["stakes_source_span_word_count"] > 10
        )
        assert moment["ai_relatability_verdict"] == "not_evaluated"
        assert moment["stakes_pairing_contract"] == (
            "source_context_substantive_or_self_v2"
        )
        # These isolated source excerpts share only broad AI/software audience
        # context. That is not enough to splice one creator's stakes onto
        # another creator's situation.
        assert moment["stakes_source_video_id"] == moment["source_video_id"]
        assert moment["stakes"] == moment["situation"]
        assert moment["source_selection_score"] >= 0
        assert moment["audience_adjusted_selection_score"] >= 0
        assert moment["score_is_probability"] is False
        observed_categories.update(moment["moment_categories"])

    assert {"problem", "need", "work"}.issubset(observed_categories)
    assert rejected_ids.isdisjoint({
        moment["source_transcript_id"] for moment in result["moments"]
    })
    assert result["receipt"]["payload"]["moments"] == result["moments"]


def test_recurring_gate_records_two_real_creators_without_ai_verdict(
    tmp_path: Path,
):
    app, engine = app_and_engine(tmp_path)
    client = app.test_client()
    moments = engine.audience.human_moments(
        "AI automation",
        "software founders",
        limit=8,
        video_ids=[
            f"youtube:video:{source['external_id']}"
            for source in REAL_AUTOMATION_SOURCES
        ],
    )
    receipt_ids = persist_pattern_receipts(engine)
    generated = client.post(
        "/api/scripts/generate",
        json={
            "topic": "AI automation",
            "audience": "software founders",
            "objective": "qualified_attention",
            "claim": "Start with the everyday work people describe",
            "human_moment": {
                **moments["moments"][0],
                "source_moment_receipt_id": moments["receipt"]["receipt_id"],
            },
            "receipt_ids": receipt_ids,
        },
    ).get_json()

    assert generated["status"] == "generated_pending_gates"
    gate = generated["evidence_summary"]["recurring_human_language_gate"]
    assert gate["pass"] is True
    assert gate["evidence_kind"] == "non_ai_source_language_recurrence"
    assert gate["ai_relatability_verdict"] == "not_evaluated"
    time = next(item for item in gate["terms"] if item["term"] == "time")
    assert time["distinct_creator_count"] == 2
    assert time["creator_ids"] == sorted({
        REAL_AUTOMATION_SOURCES[1]["creator_id"],
        REAL_AUTOMATION_SOURCES[3]["creator_id"],
    })
    assert len(time["source_receipt_ids"]) == 2
    assert len(time["source_transcript_ids"]) == 2
    assert "want" not in {item["term"] for item in gate["terms"]}
    assert "feeling stuck" not in generated["evidence_summary"][
        "recurring_human_terms"
    ]
