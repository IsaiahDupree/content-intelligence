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
                    view_velocity REAL, view_acceleration REAL, relative_strength REAL, observed_at TEXT
                );
                INSERT INTO mt_videos VALUES (
                    'youtube:real-source', 'youtube', 'real-source', 'creator-1',
                    'AI automation without losing the human story',
                    'When you feel stuck building automation, start with the human moment.',
                    'Observed research source.', 'https://www.youtube.com/watch?v=real-source', 45, '2026-08-18T00:00:00Z'
                );
                INSERT INTO mt_content_genomes VALUES (
                    'youtube:real-source',
                    'You know when you are stuck trying to automate everything and the work still feels hard. The mistake is starting with the system. Here is the proof because we tested the opening and watched the result. First show the human problem, then explain the method, and finally ask the viewer to try it.',
                    'You know when you are stuck', 'human_problem'
                );
                INSERT INTO mt_market_observations VALUES (
                    'youtube:real-source', 12000, 800, 75, 44, 120, 9, 2.4, '2026-08-18T00:00:00Z'
                );
                INSERT INTO mt_videos VALUES (
                    'youtube:irrelevant', 'youtube', 'irrelevant', 'creator-2',
                    'Relaxing music for focus', '', 'A soundtrack for software work.',
                    'https://www.youtube.com/watch?v=irrelevant', 45, '2026-08-18T00:00:00Z'
                );
                INSERT INTO mt_content_genomes VALUES (
                    'youtube:irrelevant',
                    'This relaxing music is a soundtrack for focused work and calm evenings. Listen to the melody and enjoy the sound. This description continues so that it is long enough to resemble a transcript but contains no automation evidence or founder problem at all.',
                    'This relaxing music', 'direct_claim'
                );
                INSERT INTO mt_market_observations VALUES (
                    'youtube:irrelevant', 900000, 50000, 2000, 1000, 9000, 500, 99, '2026-08-18T00:00:00Z'
                );
                """
            )
            connection.commit()
        app = create_content_quality_app({"TESTING": True, "MARKET_TAPE_DB": tape, "CONTENT_QUALITY_DB": quality})
        self.client = app.test_client()

    def tearDown(self):
        self.tempdir.cleanup()

    def test_evidence_first_script_passes_both_gates(self):
        discovery = self.client.post("/api/viral-transcripts/discover", json={"topic": "AI automation", "limit": 1})
        self.assertEqual(discovery.status_code, 200)
        discovered_receipt = discovery.get_json()["receipts"][0]
        self.assertEqual(discovered_receipt["source_id"], "real-source")
        receipt_id = discovered_receipt["receipt_id"]
        generated = self.client.post(
            "/api/scripts/generate",
            json={
                "topic": "AI automation",
                "audience": "software founders",
                "objective": "qualified_attention",
                "claim": "The best automation content begins with a recognizable human problem",
                "human_moment": {
                    "situation": "you open the dashboard and nothing feels simpler",
                    "stakes": "another tool has cost time without reducing the work",
                },
                "receipt_ids": [receipt_id],
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
