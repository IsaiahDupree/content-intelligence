"""MarketTapeReader must find candidates on every tape shape it meets.

Regression (2026-08-23): the schema-v11 backfill created the accepted-evidence
views but every backfilled row was evidence_scope='metric_only', so a reader
that required mt_accepted_full_evidence_v1 returned zero candidates for every
topic and :6010 human-moments / viral-transcript discovery went dark. The
reader now names the tier it used on each row and in health().
"""
import json
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path

from services.content_quality.engine import MarketTapeReader

LEGACY_SCHEMA = """
CREATE TABLE mt_videos (
    video_id TEXT PRIMARY KEY, platform TEXT, external_id TEXT, creator_id TEXT,
    title TEXT, caption TEXT, description TEXT, url TEXT, duration_seconds REAL,
    first_seen_at TEXT
);
CREATE TABLE mt_content_genomes (
    video_id TEXT PRIMARY KEY, transcript TEXT, opening_words TEXT, hook_type TEXT
);
CREATE TABLE mt_market_observations (
    observation_id INTEGER PRIMARY KEY AUTOINCREMENT,
    video_id TEXT, views INTEGER, likes INTEGER, comments INTEGER, shares INTEGER,
    view_velocity REAL, view_acceleration REAL, relative_strength REAL,
    observation_key TEXT, observed_at TEXT, source_confidence REAL NOT NULL DEFAULT 1
);
CREATE TABLE mt_transcript_artifacts (
    transcript_id TEXT PRIMARY KEY, video_id TEXT, platform TEXT, external_id TEXT,
    source_url TEXT, observation_key TEXT, source_metrics_json TEXT, audio_path TEXT,
    audio_sha256 TEXT, transcript_path TEXT, transcript_sha256 TEXT, whisper_model TEXT,
    whisper_language TEXT, duration_seconds REAL, word_count INTEGER,
    segment_count INTEGER, acquisition_json TEXT, audit_json TEXT, created_at TEXT
);
"""

V11_SCHEMA = """
CREATE TABLE mt_observation_quality_flags (
    flag_id TEXT PRIMARY KEY, observation_id INTEGER, video_id TEXT, error_code TEXT
);
CREATE TABLE mt_accepted_observation_evidence (
    evidence_id TEXT PRIMARY KEY, observation_id INTEGER, observation_key TEXT,
    video_id TEXT, creator_id TEXT, accepted_at TEXT, contract TEXT, evidence_scope TEXT,
    published_at TEXT, title TEXT, caption TEXT, description TEXT, language TEXT, url TEXT,
    thumbnail_url TEXT, media_type TEXT, duration_seconds REAL
);
CREATE VIEW mt_accepted_metric_observations_v1 AS
    SELECT observation.* FROM mt_market_observations observation
    WHERE observation.source_confidence > 0
      AND EXISTS (SELECT 1 FROM mt_accepted_observation_evidence evidence
                  WHERE evidence.observation_id = observation.observation_id
                    AND evidence.contract = 'market_tape_accepted_observation_evidence_v1')
      AND NOT EXISTS (SELECT 1 FROM mt_observation_quality_flags quality
                      WHERE quality.observation_id = observation.observation_id);
CREATE VIEW mt_accepted_full_evidence_v1 AS
    SELECT evidence.* FROM mt_accepted_observation_evidence evidence
    WHERE evidence.contract = 'market_tape_accepted_observation_evidence_v1'
      AND evidence.evidence_scope = 'full'
      AND NOT EXISTS (SELECT 1 FROM mt_observation_quality_flags quality
                      WHERE quality.observation_id = evidence.observation_id);
"""

TRANSCRIPT = ("You feel burned out and stuck after trying to automate every part of the work. "
              "Creators worry the next video will fail and the pressure makes it harder.")


def seed(tape: Path, v11: bool, scope: str | None, count: int = 5) -> None:
    with closing(sqlite3.connect(tape)) as connection:
        connection.executescript(LEGACY_SCHEMA + (V11_SCHEMA if v11 else ""))
        audit = json.dumps({"contract": "performance_bound_whisper_transcript_v3", "decision": "PASS",
                            "checks": {}})
        for index in range(count):
            video_id, key = f"youtube:video:src-{index}", f"obs-{index}"
            connection.execute(
                "INSERT INTO mt_videos VALUES (?, 'youtube', ?, ?, ?, ?, ?, ?, 45, ?)",
                (video_id, f"src-{index}", f"creator-{index}",
                 "Creator burnout: why your content feels stuck",
                 "When you feel stuck, name the creator struggle first.",
                 "Observed creator burnout research source.",
                 f"https://www.youtube.com/watch?v=src-{index}", "2026-08-18T00:00:00Z"))
            connection.execute("INSERT INTO mt_content_genomes VALUES (?, ?, ?, 'human_problem')",
                               (video_id, TRANSCRIPT, "You feel burned out"))
            cursor = connection.execute(
                """INSERT INTO mt_market_observations(video_id, views, likes, comments, shares,
                   view_velocity, view_acceleration, relative_strength, observation_key, observed_at,
                   source_confidence) VALUES (?, 30000, 1800, 120, 60, 120, 9, 2.4, ?, ?, 1)""",
                (video_id, key, "2026-08-18T00:00:00Z"))
            if v11 and scope:
                # metric_only rows carry NO descriptive text — exactly what the backfill produced
                descriptive = ("Creator burnout: why your content feels stuck", "caption", "desc",
                               f"https://www.youtube.com/watch?v=src-{index}", 45) if scope == "full" \
                    else (None, None, None, None, None)
                connection.execute(
                    """INSERT INTO mt_accepted_observation_evidence VALUES
                       (?, ?, ?, ?, ?, ?, 'market_tape_accepted_observation_evidence_v1', ?, ?, ?, ?, ?, 'en', ?, NULL, 'video', ?)""",
                    (f"ev-{index}", cursor.lastrowid, key, video_id, f"creator-{index}",
                     "2026-08-23T00:00:00Z", scope, "2026-08-18T00:00:00Z", *descriptive))
            connection.execute(
                "INSERT INTO mt_transcript_artifacts VALUES (?, ?, 'youtube', ?, ?, ?, ?, ?, ?, ?, ?, 'base', 'en', 45, 72, 8, ?, ?, ?)",
                (f"whisper-{index}", video_id, f"src-{index}", f"https://www.youtube.com/watch?v=src-{index}",
                 key, json.dumps({"views": 30000}), f"/tmp/{index}.m4a", "a" * 64, f"/tmp/{index}.json",
                 "b" * 64, json.dumps({"tool": "whisper"}), audit, "2026-08-18T00:00:00Z"))
        connection.commit()


class EvidenceTierTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.base = Path(self.tempdir.name)

    def tearDown(self):
        self.tempdir.cleanup()

    def reader(self, name, v11, scope):
        tape = self.base / f"{name}.sqlite3"
        seed(tape, v11=v11, scope=scope)
        return MarketTapeReader(tape)

    def test_metric_only_backfill_still_yields_candidates_and_says_so(self):
        reader = self.reader("metric", v11=True, scope="metric_only")
        health = reader.health()
        self.assertEqual(health["evidence_tier"], "metric_only")
        self.assertEqual(health["full_evidence_rows"], 0)
        self.assertEqual(health["analytics_eligible_observations"], 5)
        self.assertEqual(health["transcripts"], 5)
        rows = reader.candidates("creator burnout content", limit=10)
        self.assertEqual(len(rows), 5)
        self.assertTrue(all(r["evidence_scope"] == "metric_only" for r in rows))
        self.assertTrue(all(r["descriptive_source"] == "mt_videos" for r in rows))
        self.assertIn("burnout", rows[0]["title"].lower())        # descriptive text came from mt_videos
        self.assertEqual(rows[0]["observation_key"], "obs-0")      # metric lineage still from the view

    def test_full_evidence_is_preferred_and_labelled(self):
        reader = self.reader("full", v11=True, scope="full")
        self.assertEqual(reader.health()["evidence_tier"], "full")
        rows = reader.candidates("creator burnout content", limit=10)
        self.assertEqual(len(rows), 5)
        self.assertTrue(all(r["evidence_scope"] == "full" for r in rows))
        self.assertTrue(all(r["descriptive_source"] == "accepted_evidence" for r in rows))

    def test_legacy_tape_without_views_still_works(self):
        reader = self.reader("legacy", v11=False, scope=None)
        self.assertEqual(reader.health()["evidence_tier"], "legacy")
        rows = reader.candidates("creator burnout content", limit=10)
        self.assertEqual(len(rows), 5)
        self.assertTrue(all(r["evidence_scope"] == "legacy" for r in rows))

    def test_quarantined_observation_is_excluded_on_the_v11_tape(self):
        reader = self.reader("quarantine", v11=True, scope="metric_only")
        with closing(sqlite3.connect(reader.path)) as connection:
            observation_id = connection.execute(
                "SELECT observation_id FROM mt_market_observations WHERE video_id='youtube:video:src-0'"
            ).fetchone()[0]
            connection.execute("INSERT INTO mt_observation_quality_flags VALUES ('f1', ?, 'youtube:video:src-0', 'counter_regression')",
                               (observation_id,))
            connection.commit()
        rows = reader.candidates("creator burnout content", limit=10)
        self.assertEqual({r["video_id"] for r in rows}, {f"youtube:video:src-{i}" for i in range(1, 5)})
        self.assertEqual(reader.health()["quarantined_observations"], 1)

    def test_whisper_integrity_gate_is_unchanged_by_the_tier(self):
        # The descriptive fallback only affects topic matching; a moment or a
        # pattern receipt still needs an artifact whose observation_key matches.
        reader = self.reader("integrity", v11=True, scope="metric_only")
        rows = reader.candidates("creator burnout content", limit=10)
        for row in rows:
            artifact = reader.transcript_artifact(row["video_id"])
            self.assertEqual(artifact["observation_key"], row["observation_key"])
            self.assertEqual(artifact["audit"]["decision"], "PASS")


if __name__ == "__main__":
    unittest.main()
