"""MT-007: real visual feature extraction on a real rendered MP4 (no mocks)."""

from __future__ import annotations

import shutil
import sqlite3
import subprocess
from pathlib import Path

import pytest

from services.content_quality.visual_bank import (
    EXTRACTOR_VERSION,
    VisualBank,
    extract_visual_features,
)
from services.market_tape.config import MarketTapeConfig
from services.market_tape.store import MarketTapeStore


def _render(path: Path, seconds: int = 12) -> None:
    assert shutil.which("ffmpeg"), "ffmpeg is required; this test never skips"
    subprocess.run(
        ["ffmpeg", "-y", "-filter_complex",
         f"color=c=red:s=320x240:d={seconds/2}[a];color=c=blue:s=320x240:d={seconds/2}[b];[a][b]concat=n=2:v=1",
         "-t", str(seconds), "-pix_fmt", "yuv420p", str(path)],
        capture_output=True, check=True, timeout=120,
    )


def test_extract_visual_features_from_real_file(tmp_path):
    video = tmp_path / "cuts.mp4"
    _render(video)
    features = extract_visual_features(video)
    assert features["duration_seconds"] == pytest.approx(12.0, abs=0.2)
    assert features["aspect_ratio"] == "320:240"
    assert features["cut_rate"] > 0            # the real hard cut at 6s
    assert features["face_present"] == 0       # solid colors: no faces
    assert features["camera_motion"] == "static"
    lineage = features["lineage"]
    for field in ("extractor_version", "opencv_version", "face_model",
                  "face_model_sha256", "ffmpeg"):
        assert lineage[field]
    assert lineage["extractor_version"] == EXTRACTOR_VERSION


def test_bind_writes_artifact_and_genome(tmp_path):
    config = MarketTapeConfig(
        db_path=tmp_path / "tape.sqlite3", object_dir=tmp_path / "objects",
        heartbeat_path=tmp_path / "hb.json", lock_path=tmp_path / "lock",
        local_research_state_path=tmp_path / "lr.json",
        prediction_model_dir=tmp_path / "models", local_research_min_free_bytes=0,
        platforms=["youtube"], topics=["x"], adaptive_topics_enabled=False,
        regions=["US"], youtube_chart_categories=["all"], daily_unique_target=10,
    )
    MarketTapeStore(config)  # creates the genome table
    with sqlite3.connect(config.db_path) as connection:
        connection.execute(
            "INSERT INTO mt_content_genomes(video_id, extraction_status, updated_at)"
            " VALUES ('youtube:video:test', 'whisper_transcribed', 'now')")
        connection.commit()
    video = tmp_path / "cuts.mp4"
    _render(video)
    bank = VisualBank(config.db_path)
    features = extract_visual_features(video)
    receipt = bank.bind("youtube:video:test", video, features)
    assert receipt["artifact_id"].startswith("visual_")
    with sqlite3.connect(config.db_path) as connection:
        genome = connection.execute(
            "SELECT cut_rate, face_present, camera_motion, extraction_status"
            " FROM mt_content_genomes WHERE video_id='youtube:video:test'").fetchone()
        artifacts = connection.execute("SELECT COUNT(*) FROM mt_visual_artifacts").fetchone()[0]
    assert genome[0] > 0 and genome[1] == 0 and genome[2] == "static"
    assert genome[3] == "visual_extracted"
    assert artifacts == 1
