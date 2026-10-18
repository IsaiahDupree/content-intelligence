"""Visual feature extraction for the Market Tape content genome (MT-007).

Mirrors the transcript bank's rights posture: only public items that already
passed the transcript cohort policy are eligible; a lowest-quality public
rendition is fetched with yt-dlp for *local analysis only* and deleted after
feature extraction. Every artifact records model/tool version lineage so a
feature value is reproducible and attributable.

Features written to ``mt_content_genomes``: cut_rate (cuts per minute),
face_present, people_count, camera_motion, aspect_ratio, duration_seconds,
extraction_status='visual_extracted'. The immutable per-item receipt lives in
``mt_visual_artifacts``.
"""

from __future__ import annotations

import hashlib
import json
import re
import shutil
import sqlite3
import subprocess
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

EXTRACTOR_VERSION = "visual-bank-v1"
SAMPLE_FPS = 1.0
MAX_SAMPLED_FRAMES = 90
SCENE_THRESHOLD = 0.3


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _tool(binary: str) -> str:
    found = shutil.which(binary)
    if found:
        return found
    for prefix in ("/opt/homebrew/bin", "/usr/local/bin"):
        if Path(prefix, binary).exists():
            return str(Path(prefix, binary))
    return binary


def tool_version(binary: str) -> str:
    try:
        out = subprocess.run([_tool(binary), "-version"], capture_output=True,
                             text=True, timeout=20)
        return (out.stdout or out.stderr).splitlines()[0].strip()[:120]
    except (OSError, subprocess.SubprocessError, IndexError):
        return "unknown"


def _ffprobe(path: Path) -> Dict[str, Any]:
    out = subprocess.run(
        [_tool("ffprobe"), "-v", "error", "-select_streams", "v:0",
         "-show_entries", "stream=width,height:format=duration", "-of", "json",
         str(path)],
        capture_output=True, text=True, timeout=60, check=True,
    )
    payload = json.loads(out.stdout)
    stream = (payload.get("streams") or [{}])[0]
    return {
        "width": int(stream.get("width") or 0),
        "height": int(stream.get("height") or 0),
        "duration": float(payload.get("format", {}).get("duration") or 0.0),
    }


def _scene_cuts(path: Path) -> List[float]:
    out = subprocess.run(
        [_tool("ffmpeg"), "-i", str(path), "-vf",
         f"select='gt(scene,{SCENE_THRESHOLD})',showinfo", "-f", "null", "-"],
        capture_output=True, text=True, timeout=600,
    )
    return [round(float(m.group(1)), 3)
            for m in re.finditer(r"pts_time:(\d+\.?\d*)", out.stderr)]


def _cascade() -> tuple[Any, str, str]:
    import cv2  # local import: heavy, optional at import time
    cascade_path = Path(cv2.data.haarcascades) / "haarcascade_frontalface_default.xml"
    digest = hashlib.sha256(cascade_path.read_bytes()).hexdigest()
    return cv2.CascadeClassifier(str(cascade_path)), cascade_path.name, digest


def extract_visual_features(path: Path) -> Dict[str, Any]:
    """Real visual features from the actual file via ffmpeg + OpenCV."""
    import cv2

    probe = _ffprobe(path)
    duration = probe["duration"]
    cuts = _scene_cuts(path)
    cut_rate = round(len(cuts) / (duration / 60.0), 3) if duration > 0 else 0.0
    aspect = (f"{probe['width']}:{probe['height']}" if probe["width"] and probe["height"]
              else "")

    classifier, cascade_name, cascade_sha = _cascade()
    capture = cv2.VideoCapture(str(path))
    native_fps = capture.get(cv2.CAP_PROP_FPS) or 30.0
    step = max(int(native_fps / SAMPLE_FPS), 1)
    faces_per_frame: List[int] = []
    motion_scores: List[float] = []
    previous = None
    index = 0
    sampled = 0
    while sampled < MAX_SAMPLED_FRAMES:
        ok, frame = capture.read()
        if not ok:
            break
        if index % step == 0:
            gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
            small = cv2.resize(gray, (160, 90))
            faces = classifier.detectMultiScale(gray, scaleFactor=1.2, minNeighbors=5)
            faces_per_frame.append(len(faces))
            if previous is not None:
                diff = cv2.absdiff(small, previous)
                motion_scores.append(float(diff.mean()) / 255.0)
            previous = small
            sampled += 1
        index += 1
    capture.release()

    mean_motion = (sum(motion_scores) / len(motion_scores)) if motion_scores else 0.0
    camera_motion = ("static" if mean_motion < 0.02 else
                     "low" if mean_motion < 0.08 else "high")
    return {
        "duration_seconds": round(duration, 3),
        "aspect_ratio": aspect,
        "cut_rate": cut_rate,
        "cut_times": cuts[:200],
        "face_present": int(any(count > 0 for count in faces_per_frame)),
        "people_count": max(faces_per_frame) if faces_per_frame else 0,
        "camera_motion": camera_motion,
        "mean_frame_motion": round(mean_motion, 5),
        "sampled_frames": sampled,
        "lineage": {
            "extractor_version": EXTRACTOR_VERSION,
            "opencv_version": cv2.__version__,
            "face_model": cascade_name,
            "face_model_sha256": cascade_sha,
            "ffmpeg": tool_version("ffmpeg"),
            "scene_threshold": SCENE_THRESHOLD,
            "sample_fps": SAMPLE_FPS,
        },
    }


class VisualBank:
    """Binds visual features to the tape with immutable receipts."""

    def __init__(self, tape_path: Path):
        self.tape_path = tape_path
        with self.connect() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS mt_visual_artifacts (
                    artifact_id TEXT PRIMARY KEY,
                    video_id TEXT NOT NULL,
                    media_sha256 TEXT NOT NULL,
                    features_json TEXT NOT NULL,
                    lineage_json TEXT NOT NULL,
                    source_contract TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS mt_visual_artifacts_video_idx
                    ON mt_visual_artifacts(video_id);
                """
            )

    def connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(str(self.tape_path), timeout=30)
        connection.row_factory = sqlite3.Row
        return connection

    def candidates(self, limit: int, platform: str = "youtube") -> List[Dict[str, Any]]:
        """Eligible = already in the transcript cohort (public, policy-passed)
        and not yet visually extracted."""
        with self.connect() as connection:
            rows = connection.execute(
                """SELECT g.video_id, v.url, v.platform, v.duration_seconds
                   FROM mt_content_genomes g JOIN mt_videos v USING (video_id)
                   WHERE g.extraction_status = 'whisper_transcribed'
                     AND g.cut_rate IS NULL AND v.platform = ?
                   ORDER BY v.duration_seconds ASC LIMIT ?""",
                (platform, limit),
            ).fetchall()
        return [dict(row) for row in rows]

    def download(self, url: str, workdir: Path) -> Path:
        template = workdir / "source.%(ext)s"
        command = [
            _tool("yt-dlp"), "--no-playlist", "--quiet",
            "-f", "worstvideo[ext=mp4]/worst[ext=mp4]/worst",
            "-o", str(template), url,
        ]
        result = subprocess.run(command, capture_output=True, text=True, timeout=600)
        if result.returncode != 0:
            raise RuntimeError(f"yt-dlp failed ({result.returncode}): {result.stderr[-300:]}")
        files = sorted(p for p in workdir.iterdir() if p.suffix.lower() in {".mp4", ".webm", ".mkv"})
        if not files:
            raise RuntimeError("yt-dlp produced no video file")
        return files[0]

    def bind(self, video_id: str, media_path: Path, features: Dict[str, Any]) -> Dict[str, Any]:
        media_sha = hashlib.sha256(media_path.read_bytes()).hexdigest()
        lineage = features["lineage"]
        artifact_id = "visual_" + hashlib.sha256(
            f"{video_id}:{media_sha}:{EXTRACTOR_VERSION}".encode()).hexdigest()[:24]
        created_at = utc_now()
        with self.connect() as connection:
            connection.execute(
                """INSERT OR IGNORE INTO mt_visual_artifacts(
                       artifact_id, video_id, media_sha256, features_json,
                       lineage_json, source_contract, created_at)
                   VALUES (?,?,?,?,?,?,?)""",
                (artifact_id, video_id, media_sha, json.dumps(features),
                 json.dumps(lineage), "lowest_quality_public_source_local_analysis_v1",
                 created_at),
            )
            connection.execute(
                """UPDATE mt_content_genomes
                   SET cut_rate = ?, face_present = ?, people_count = ?,
                       camera_motion = ?, aspect_ratio = ?, duration_seconds = ?,
                       extraction_status = 'visual_extracted', updated_at = ?
                   WHERE video_id = ?""",
                (features["cut_rate"], features["face_present"],
                 features["people_count"], features["camera_motion"],
                 features["aspect_ratio"], features["duration_seconds"],
                 created_at, video_id),
            )
            connection.commit()
        return {"artifact_id": artifact_id, "video_id": video_id,
                "media_sha256": media_sha, "created_at": created_at}

    def extract_cohort(self, limit: int = 5, platform: str = "youtube") -> Dict[str, Any]:
        receipts, failures = [], []
        for candidate in self.candidates(limit, platform):
            workdir = Path(tempfile.mkdtemp(prefix="visual-bank-"))
            try:
                media = self.download(candidate["url"], workdir)
                features = extract_visual_features(media)
                receipts.append({**self.bind(candidate["video_id"], media, features),
                                 "cut_rate": features["cut_rate"],
                                 "face_present": features["face_present"],
                                 "camera_motion": features["camera_motion"]})
            except (RuntimeError, subprocess.SubprocessError, OSError, ValueError) as error:
                failures.append({"video_id": candidate["video_id"],
                                 "error": f"{type(error).__name__}: {str(error)[:200]}"})
            finally:
                shutil.rmtree(workdir, ignore_errors=True)  # local analysis only
        return {"contract": "visual_cohort_extraction_v1", "extracted": receipts,
                "failures": failures, "extractor_version": EXTRACTOR_VERSION,
                "finished_at": utc_now()}
