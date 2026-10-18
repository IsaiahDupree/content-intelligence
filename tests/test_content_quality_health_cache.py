"""The capability-health endpoints must not each trigger a full market-tape sweep.

Incident 2026-08-23: the Ops Console probes all eight capability health routes every
refresh. Each one ran engine.health() — a COUNT(*) sweep over a 1.9 GB tape — so a single
console cycle opened eight concurrent sweeps. The server climbed past 400 open tape handles
and 360 threads, then failed every request with OSError 24 (too many open files) and hung,
which took the narrative-coherence gate down with it. These tests pin the fix.
"""
import sys
import threading
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from services.content_quality import create_content_quality_app  # noqa: E402


def _app(tmp_path, ttl=60.0):
    return create_content_quality_app({
        "TESTING": True,
        "MARKET_TAPE_DB": str(tmp_path / "tape.sqlite3"),
        "CONTENT_QUALITY_DB": str(tmp_path / "quality.sqlite3"),
        "HEALTH_CACHE_SECONDS": ttl,
    })


def _count_sweeps(app):
    """Wrap the engine so every real health sweep is counted."""
    engine = app.extensions["content_quality_engine"]
    calls = []
    original = engine.health

    def counting():
        calls.append(1)
        return original()

    engine.health = counting
    return calls


def test_eight_capability_probes_cause_one_sweep(tmp_path):
    app = _app(tmp_path)
    calls = _count_sweeps(app)
    client = app.test_client()
    routes = ["/api/audience-intelligence/health", "/api/viral-transcripts/health",
              "/api/scripts/health", "/api/relatability/health",
              "/api/relatability/qualitative-health", "/api/attention/health",
              "/api/retention/health", "/api/learning/health",
              "/api/narrative-coherence/health"]
    for route in routes:
        assert client.get(route).status_code in (200, 503)
    assert len(calls) == 1, f"one console cycle ran {len(calls)} tape sweeps"


def test_concurrent_probes_collapse_into_one_sweep(tmp_path):
    app = _app(tmp_path)
    calls = _count_sweeps(app)
    client = app.test_client()
    barrier = threading.Barrier(8)

    def probe():
        barrier.wait()
        client.get("/api/narrative-coherence/health")

    threads = [threading.Thread(target=probe) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)
    assert len(calls) == 1, f"{len(calls)} simultaneous sweeps got through the lock"


def test_stale_cache_refreshes_without_blocking_the_caller(tmp_path):
    """A probe must never wait on the sweep once a snapshot exists: the console's 5s
    timeout is shorter than a ~12s cold sweep over a multi-GB tape."""
    app = _app(tmp_path, ttl=0.0)          # every call is stale
    engine = app.extensions["content_quality_engine"]
    started, release = threading.Event(), threading.Event()
    original = engine.health
    calls = []

    def slow():
        calls.append(1)
        if len(calls) > 1:                 # the background refresh blocks; the caller must not
            started.set()
            release.wait(timeout=10)
        return original()

    engine.health = slow
    client = app.test_client()
    client.get("/health")                  # cold start: one caller pays for the sweep
    began = time.monotonic()
    response = client.get("/health")       # stale: must return the snapshot immediately
    assert response.status_code in (200, 503)
    assert time.monotonic() - began < 2.0, "stale probe blocked on the background sweep"
    assert started.wait(timeout=5), "a stale read did not trigger a background refresh"
    release.set()


def test_only_one_background_refresh_runs_at_a_time(tmp_path):
    app = _app(tmp_path, ttl=0.0)
    engine = app.extensions["content_quality_engine"]
    original = engine.health
    calls, release = [], threading.Event()

    def slow():
        calls.append(1)
        if len(calls) > 1:
            release.wait(timeout=10)
        return original()

    engine.health = slow
    client = app.test_client()
    client.get("/health")                  # cold sweep -> calls == 1
    for _ in range(6):                     # six stale reads while one refresh is in flight
        client.get("/health")
    time.sleep(0.3)
    assert len(calls) == 2, f"{len(calls) - 1} background sweeps ran at once"
    release.set()


def test_health_payload_is_unchanged_by_caching(tmp_path):
    app = _app(tmp_path)
    client = app.test_client()
    first = client.get("/health")
    second = client.get("/health")
    assert first.status_code == second.status_code
    body = first.get_json()
    assert {"status", "service", "market_tape", "checked_at"} <= set(body)
    assert second.get_json() == body


def test_production_cold_start_returns_starting_while_one_scan_warms(tmp_path):
    app = create_content_quality_app({
        "TESTING": False,
        "NARRATIVE_COHERENCE_LLM": "off",
        "MARKET_TAPE_DB": str(tmp_path / "tape.sqlite3"),
        "CONTENT_QUALITY_DB": str(tmp_path / "quality.sqlite3"),
        "HEALTH_CACHE_SECONDS": 60,
    })
    engine = app.extensions["content_quality_engine"]
    original = engine.health
    started, release = threading.Event(), threading.Event()

    def slow():
        started.set()
        release.wait(timeout=10)
        return original()

    engine.health = slow
    began = time.monotonic()
    response = app.test_client().get("/health")

    assert response.status_code == 503
    assert response.get_json()["status"] == "starting"
    assert time.monotonic() - began < 1.0
    assert started.wait(timeout=2)
    release.set()
