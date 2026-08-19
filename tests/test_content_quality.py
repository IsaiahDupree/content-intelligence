import json
import sqlite3
import subprocess
import tempfile
import unittest
from contextlib import closing
from pathlib import Path

from services.content_quality.api import create_content_quality_app


class ContentQualityIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        base = Path(self.tempdir.name)
        tape = base / "market-tape.sqlite3"
        quality = base / "content-quality.sqlite3"
        with closing(sqlite3.connect(tape)) as connection:
            connection.executescript(
                """
                CREATE TABLE mt_videos (
                    video_id TEXT PRIMARY KEY, platform TEXT, external_id TEXT, creator_id TEXT,
                    title TEXT, caption TEXT, description TEXT, url TEXT, duration_seconds REAL,
                    first_seen_at TEXT
                );
                CREATE TABLE mt_content_genomes (
                    video_id TEXT PRIMARY KEY, transcript TEXT, opening_words TEXT, hook_type TEXT
                );
                CREATE TABLE mt_market_observations (
                    video_id TEXT, views INTEGER, likes INTEGER, comments INTEGER, shares INTEGER,
                    view_velocity REAL, view_acceleration REAL, relative_strength REAL,
                    observation_key TEXT, observed_at TEXT
                );
                CREATE TABLE mt_transcript_artifacts (
                    transcript_id TEXT PRIMARY KEY, video_id TEXT, platform TEXT, external_id TEXT,
                    source_url TEXT, observation_key TEXT, source_metrics_json TEXT, audio_path TEXT,
                    audio_sha256 TEXT, transcript_path TEXT, transcript_sha256 TEXT, whisper_model TEXT,
                    whisper_language TEXT, duration_seconds REAL, word_count INTEGER,
                    segment_count INTEGER, acquisition_json TEXT, audit_json TEXT, created_at TEXT
                );
                """
            )
            transcript = (
                "You feel burned out and stuck after trying to automate every part of the work. "
                "The pressure makes creative work feel harder, and creators worry the next video "
                "will fail. The evidence shows the human struggle must come before the automation "
                "explanation. Name the problem, show what changed, and give one concrete next step. "
                "That is how content can help a tired founder feel understood before teaching them."
            )
            artifact_audit = json.dumps({
                "contract": "performance_bound_whisper_transcript_v3",
                "decision": "PASS",
                "checks": {"performance_views_floor": True, "performance_engagement_floor": True},
            })
            for index in range(5):
                video_id = f"youtube:video:real-source-{index}"
                external_id = f"real-source-{index}"
                observation_key = f"observation-{index}"
                connection.execute(
                    "INSERT INTO mt_videos VALUES (?, 'youtube', ?, ?, ?, ?, ?, ?, 45, ?)",
                    (
                        video_id, external_id, f"creator-{index}",
                        "AI automation content for burned out creators",
                        "When you feel stuck, name the creator struggle first.",
                        "Observed creator burnout research source.",
                        f"https://www.youtube.com/watch?v={external_id}",
                        "2026-08-18T00:00:00Z",
                    ),
                )
                connection.execute(
                    "INSERT INTO mt_content_genomes VALUES (?, ?, ?, 'human_problem')",
                    (video_id, transcript, "You feel burned out and stuck"),
                )
                connection.execute(
                    "INSERT INTO mt_market_observations VALUES (?, 30000, 1800, 120, 60, 120, 9, 2.4, ?, ?)",
                    (video_id, observation_key, "2026-08-18T00:00:00Z"),
                )
                connection.execute(
                    """
                    INSERT INTO mt_transcript_artifacts VALUES (
                        ?, ?, 'youtube', ?, ?, ?, ?, ?, ?, ?, ?, 'base', 'en', 45, 72, 8, ?, ?, ?
                    )
                    """,
                    (
                        f"whisper-{index}", video_id, external_id,
                        f"https://www.youtube.com/watch?v={external_id}", observation_key,
                        json.dumps({"views": 30000, "likes": 1800, "comments": 120, "shares": 60}),
                        f"/tmp/source-{index}.m4a", "a" * 64,
                        f"/tmp/transcript-{index}.json", "b" * 64,
                        json.dumps({"tool": "whisper"}), artifact_audit,
                        "2026-08-18T00:00:00Z",
                    ),
                )
            connection.commit()
        app = create_content_quality_app({"TESTING": True, "MARKET_TAPE_DB": tape, "CONTENT_QUALITY_DB": quality})
        self.client = app.test_client()

    def tearDown(self):
        self.tempdir.cleanup()

    def test_evidence_first_script_passes_both_gates(self):
        discovery = self.client.post("/api/viral-transcripts/discover", json={"topic": "AI automation", "limit": 5})
        self.assertEqual(discovery.status_code, 200)
        discovered_receipts = discovery.get_json()["receipts"]
        self.assertEqual(len(discovered_receipts), 5)
        receipt_ids = [item["receipt_id"] for item in discovered_receipts]
        generated = self.client.post(
            "/api/scripts/generate",
            json={
                "topic": "AI automation",
                "audience": "software founders",
                "objective": "qualified_attention",
                "claim": "The best automation content begins with a recognizable human problem",
                "human_moment": {
                    "situation": "you feel burned out after another video fails",
                    "stakes": "another tool has cost time without reducing the work",
                },
                "receipt_ids": receipt_ids,
            },
        )
        self.assertEqual(generated.status_code, 200)
        script = generated.get_json()
        relatability = self.client.post("/api/relatability/script-audit", json=script).get_json()
        attention = self.client.post("/api/attention/script-audit", json=script).get_json()
        preflight = self.client.post("/api/attention/video-preflight", json=script).get_json()
        self.assertEqual(relatability["decision"], "PASS")
        self.assertEqual(attention["decision"], "PASS")
        self.assertEqual(preflight["decision"], "PASS")
        handoff = self.client.get(f"/api/scripts/{script['script_id']}")
        self.assertEqual(handoff.status_code, 200)
        self.assertTrue(handoff.get_json()["gates"]["ready_for_render"])

    def test_script_generation_fails_closed_without_receipts(self):
        response = self.client.post(
            "/api/scripts/generate",
            json={
                "topic": "AI automation", "audience": "founders", "claim": "A claim",
                "human_moment": {"situation": "you feel stuck", "stakes": "time is lost"},
            },
        )
        self.assertEqual(response.status_code, 422)
        self.assertEqual(response.get_json()["code"], "REJECT_NO_RECEIPTS")

    def test_instagram_aggregate_retention_does_not_invent_curve(self):
        response = self.client.post(
            "/api/retention/normalize",
            json={"platform": "instagram", "source_id": "post-1", "average_watch_seconds": 7.2, "completion_rate": 0.31},
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()["normalized"]["kind"], "aggregate_only")
        self.assertNotIn("points", response.get_json()["normalized"])

    def test_real_uploaded_video_is_decoded_and_semantically_audited(self):
        video = Path(self.tempdir.name) / "real-test-pattern.mp4"
        subprocess.run(
            [
                "/opt/homebrew/bin/ffmpeg", "-y", "-v", "error",
                "-f", "lavfi", "-i", "testsrc=size=320x180:rate=10",
                "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=44100",
                "-t", "3", "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", str(video),
            ],
            check=True,
            timeout=30,
        )
        timeline = [
            {"start": 0.0, "end": 0.5, "beat": "human_hook", "text": "You know this moment?"},
            {"start": 0.5, "end": 1.0, "beat": "stakes", "text": "The cost is real."},
            {"start": 1.0, "end": 1.5, "beat": "proof", "text": "Here is the receipt."},
            {"start": 1.5, "end": 2.0, "beat": "payoff", "text": "Now the pattern is visible."},
            {"start": 2.0, "end": 3.0, "beat": "cta", "text": "Test it on the next result."},
        ]
        with video.open("rb") as stream:
            response = self.client.post(
                "/api/attention/video-upload-audit",
                data={"video": (stream, video.name), "timeline_json": json.dumps(timeline), "script_id": "script-real"},
                content_type="multipart/form-data",
            )
        self.assertEqual(response.status_code, 200)
        result = response.get_json()
        self.assertEqual(result["decision"], "PASS")
        self.assertTrue(result["findings"]["media_probe"]["has_audio"])
        self.assertGreaterEqual(result["findings"]["frame_change_report"]["change_count"], 1)


if __name__ == "__main__":
    unittest.main()
