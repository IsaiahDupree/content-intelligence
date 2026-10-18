"""Threads Graph integration coverage uses a real loopback HTTP server."""

from __future__ import annotations

import json
import threading
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

import pytest

from services.market_tape.collector import MarketTapeCollector
from services.market_tape.config import MarketTapeConfig
from services.market_tape.models import MarketContent, MetricCounters, SourceState
from services.market_tape.sources.registry import build_sources
from services.market_tape.sources.social import ThreadsKeywordSearchSource
from services.market_tape.store import MarketTapeStore


class ThreadsProviderHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    requests = []

    def do_GET(self):  # noqa: N802 - HTTP handler contract
        parsed = urlparse(self.path)
        query = parse_qs(parsed.query)
        self.__class__.requests.append({
            "path": parsed.path,
            "query": query,
            "authorization": self.headers.get("Authorization", ""),
        })
        if parsed.path == "/keyword_search":
            topic = query.get("q", [""])[0]
            records = {
                "ai automation": [
                    {
                        "id": "thread-ai-1",
                        "media_product_type": "THREADS",
                        "media_type": "TEXT_POST",
                        "permalink": "https://www.threads.net/@systems/post/thread-ai-1",
                        "owner": {"id": "owner-systems"},
                        "username": "systems",
                        "text": "Measured AI automation beats guesses #AI",
                        "timestamp": "2026-08-22T15:00:00+0000",
                        "shortcode": "thread-ai-1",
                        "is_quote_post": False,
                    },
                    {
                        "id": "thread-ai-2",
                        "media_product_type": "THREADS",
                        "media_type": "IMAGE",
                        "permalink": "https://www.threads.net/@builders/post/thread-ai-2",
                        "owner": {"id": "owner-builders"},
                        "username": "builders",
                        "text": "A visual automation teardown",
                        "timestamp": "2026-08-22T15:05:00+0000",
                        "thumbnail_url": "https://example.test/thread-ai-2.jpg",
                    },
                ],
                "creator economy": [
                    {
                        "id": "thread-creator-1",
                        "media_product_type": "THREADS",
                        "media_type": "VIDEO",
                        "permalink": "https://www.threads.net/@creators/post/thread-creator-1",
                        "owner": {"id": "owner-creators"},
                        "username": "creators",
                        "text": "Creator economics with evidence",
                        "timestamp": "2026-08-22T15:10:00+0000",
                    },
                ],
            }.get(topic, [])
            self._json({
                "data": records,
                "paging": {"cursors": {"after": f"after-{topic}"}},
            })
            return
        if parsed.path == "/thread-refresh-1":
            self._json({
                "id": "thread-refresh-1",
                "media_product_type": "THREADS",
                "media_type": "TEXT_POST",
                "permalink": "https://www.threads.net/@fresh/post/thread-refresh-1",
                "owner": {"id": "owner-fresh"},
                "username": "fresh",
                "text": "Fresh object lookup without invented engagement metrics",
                "timestamp": "2026-08-22T16:00:00+0000",
            })
            return
        self._json({"error": "not found"}, status=404)

    def _json(self, body, status=200):
        encoded = json.dumps(body).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)

    def log_message(self, *_):
        return


@pytest.fixture
def threads_provider_server():
    ThreadsProviderHandler.requests = []
    server = ThreadingHTTPServer(("127.0.0.1", 0), ThreadsProviderHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


@pytest.fixture
def threads_config(tmp_path):
    return MarketTapeConfig(
        db_path=tmp_path / "market.sqlite3",
        object_dir=tmp_path / "objects",
        heartbeat_path=tmp_path / "heartbeat.json",
        lock_path=tmp_path / "market.lock",
        local_research_state_path=tmp_path / "local-research-state.json",
        prediction_model_dir=tmp_path / "models",
        platforms=["threads"],
        topics=["ai automation", "creator economy"],
        adaptive_topics_enabled=False,
        daily_unique_target=3,
        platform_daily_targets={"threads": 3},
        provider_daily_request_limits={"threads": 10},
        provider_cost_per_request_usd={"threads": 0.0},
        max_daily_provider_cost_usd=1.0,
        supabase_sync_enabled=False,
    )


def test_threads_source_uses_official_host_and_token_contract(threads_config):
    source = ThreadsKeywordSearchSource(threads_config, "contract-run", 10)
    try:
        assert source.base_url == "https://graph.threads.net"
        assert source.credential_names == ("THREADS_ACCESS_TOKEN",)
        assert source.source_id == "threads-graph-keyword-search"
        assert source.terminal_metrics_capable() is False
    finally:
        source.close()


def test_meta_token_does_not_satisfy_threads_preflight(
    threads_provider_server,
    threads_config,
    monkeypatch,
):
    monkeypatch.delenv("THREADS_ACCESS_TOKEN", raising=False)
    monkeypatch.setenv("META_ACCESS_TOKEN", "loopback-meta-token")
    source = ThreadsKeywordSearchSource(
        threads_config,
        "missing-token-run",
        10,
        base_url=threads_provider_server,
    )
    try:
        batch = source.discover(3)
    finally:
        source.close()

    assert batch.receipt.state == SourceState.BLOCKED_CREDENTIAL
    assert batch.receipt.error_code == "credential_missing"
    assert batch.receipt.request_count == 0
    assert "THREADS_ACCESS_TOKEN" in batch.receipt.error_detail
    assert ThreadsProviderHandler.requests == []


def test_threads_keyword_discovery_uses_recent_keyword_edge_and_receipts(
    threads_provider_server,
    threads_config,
    monkeypatch,
):
    monkeypatch.setenv("THREADS_ACCESS_TOKEN", "loopback-threads-token")
    source = ThreadsKeywordSearchSource(
        threads_config,
        "discovery-run",
        10,
        base_url=threads_provider_server,
    )
    try:
        batch = source.discover(3)
    finally:
        source.close()

    assert batch.receipt.state == SourceState.READY
    assert batch.receipt.request_count == 2
    assert batch.receipt.discovered_count == 3
    assert batch.receipt.cursor == "after-creator economy"
    assert batch.receipt.metadata["metered"] is False
    assert batch.receipt.metadata["operation"] == "discover"
    assert batch.receipt.metadata["scope"] == "public_keyword_search"
    assert batch.receipt.metadata["endpoint"] == "keyword_search"
    assert batch.receipt.metadata["search_type"] == "RECENT"
    assert batch.receipt.metadata["search_mode"] == "KEYWORD"
    assert batch.receipt.metadata["required_scope"] == "threads_keyword_search"
    assert batch.receipt.metadata["engagement_metrics_observed"] is False
    assert [attempt.query for attempt in batch.query_attempts] == [
        "ai automation",
        "creator economy",
    ]
    assert [attempt.result_count for attempt in batch.query_attempts] == [2, 1]
    assert all(attempt.state == "completed" for attempt in batch.query_attempts)
    assert all(
        attempt.metadata["surface"] == "keyword_search"
        and attempt.metadata["required_scope"] == "threads_keyword_search"
        for attempt in batch.query_attempts
    )

    requests = ThreadsProviderHandler.requests
    assert [request["path"] for request in requests] == [
        "/keyword_search",
        "/keyword_search",
    ]
    assert [request["query"]["q"] for request in requests] == [
        ["ai automation"],
        ["creator economy"],
    ]
    assert all(request["query"]["search_type"] == ["RECENT"] for request in requests)
    assert all(request["query"]["search_mode"] == ["KEYWORD"] for request in requests)
    assert all(request["query"]["limit"] in (["3"], ["1"]) for request in requests)
    assert all("owner" in request["query"]["fields"][0] for request in requests)
    assert all(
        request["authorization"] == "Bearer loopback-threads-token"
        for request in requests
    )

    items = {item.external_id: item for item in batch.items}
    ai = items["thread-ai-1"]
    assert ai.creator_external_id == "owner-systems"
    assert ai.creator_handle == "systems"
    assert ai.caption == "Measured AI automation beats guesses #AI"
    assert ai.hashtags == ["ai"]
    assert ai.media_type == "text_post"
    assert ai.metrics.views == 0
    assert ai.metrics.likes == 0
    assert ai.discovery_context["topic"] == "ai automation"
    assert ai.discovery_context["search_type"] == "RECENT"
    assert ai.discovery_context["search_mode"] == "KEYWORD"
    assert ai.discovery_context["engagement_metrics_observed"] is False
    assert ai.discovery_context["metric_contract"] == "content_metadata_only"


def test_threads_object_refresh_uses_object_edge_without_invented_metrics(
    threads_provider_server,
    threads_config,
    monkeypatch,
):
    monkeypatch.setenv("THREADS_ACCESS_TOKEN", "loopback-threads-token")
    source = ThreadsKeywordSearchSource(
        threads_config,
        "refresh-run",
        10,
        base_url=threads_provider_server,
    )
    prior = {
        "external_id": "thread-refresh-1",
        "creator_external_id": "prior-owner",
        "creator_handle": "prior-handle",
        "caption": "Prior text",
        "url": "https://www.threads.net/@prior/post/thread-refresh-1",
        "published_at": "2026-08-22T15:30:00+00:00",
    }
    try:
        batch = source.refresh([prior])
    finally:
        source.close()

    assert batch.receipt.state == SourceState.READY
    assert batch.receipt.request_count == 1
    assert batch.receipt.refreshed_count == 0
    assert batch.receipt.metadata["metered"] is False
    assert batch.receipt.metadata["operation"] == "refresh"
    assert batch.receipt.metadata["scope"] == "public_object_lookup"
    assert batch.receipt.metadata["endpoint"] == "thread_object"
    assert batch.receipt.metadata["required_scope"] == "threads_basic"
    assert batch.receipt.metadata["engagement_metrics_observed"] is False
    assert batch.receipt.metadata["metadata_only_count"] == 1
    assert batch.receipt.metadata["item_failure_code"] == (
        "engagement_metrics_unavailable"
    )
    assert batch.receipt.metadata["metric_contract"] == (
        "provider_counters_required_for_observation"
    )
    assert ThreadsProviderHandler.requests == [{
        "path": "/thread-refresh-1",
        "query": {"fields": [ThreadsKeywordSearchSource.fields]},
        "authorization": "Bearer loopback-threads-token",
    }]

    assert batch.items == []


def test_registry_replaces_legacy_threads_meta_edge(threads_config):
    sources = build_sources(
        threads_config,
        "registry-run",
        lambda _source_id, daily_limit: daily_limit,
    )
    try:
        threads_sources = [
            source for source in sources
            if source.source_id == ThreadsKeywordSearchSource.source_id
        ]
        assert len(threads_sources) == 1
        assert isinstance(threads_sources[0], ThreadsKeywordSearchSource)
        assert all(source.source_id != "threads-graph-authorized" for source in sources)
    finally:
        for source in sources:
            source.close()


def test_threads_metadata_refresh_cannot_poison_prior_positive_observation(
    threads_provider_server,
    threads_config,
    monkeypatch,
):
    monkeypatch.setenv("THREADS_ACCESS_TOKEN", "loopback-threads-token")
    store = MarketTapeStore(threads_config)
    observed_at = datetime.now(timezone.utc) - timedelta(hours=2)
    item = MarketContent(
        platform="threads",
        external_id="thread-refresh-1",
        creator_external_id="owner-prior",
        creator_handle="prior",
        published_at=observed_at - timedelta(hours=1),
        observed_at=observed_at,
        source_id=ThreadsKeywordSearchSource.source_id,
        metrics=MetricCounters.from_values(
            views=1250,
            likes=95,
            comments=14,
            shares=8,
        ),
        caption="Prior measured Threads post",
        url="https://www.threads.net/@prior/post/thread-refresh-1",
    )
    store.start_run("threads-positive-seed", "discover")
    assert store.ingest(item, "threads-positive-seed") == (True, True)
    with store.connect() as connection:
        connection.execute(
            "UPDATE mt_poll_queue SET due_at = ? WHERE video_id = ?",
            (
                (datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat(),
                item.video_id,
            ),
        )

    def source_builder(resolved, run_id, budget_for):
        return [ThreadsKeywordSearchSource(
            resolved,
            run_id,
            budget_for(
                ThreadsKeywordSearchSource.source_id,
                resolved.request_limit_for("threads"),
            ),
            base_url=threads_provider_server,
        )]

    run_id = "threads-metadata-refresh"
    store.start_run(run_id, "recheck")
    receipts = MarketTapeCollector(
        threads_config,
        store,
        source_builder=source_builder,
    )._run_rechecks(run_id, phase="scheduled")

    planner = next(
        receipt for receipt in receipts
        if receipt["source_id"] == "market-tape-recheck-planner-scheduled"
    )
    assert planner["metadata"]["recheck_plan"]["source_capability"] == [{
        "source_id": ThreadsKeywordSearchSource.source_id,
        "platform": "threads",
        "state": "metadata_only_no_terminal_metrics",
        "request_budget_remaining": 10,
        "metered": False,
    }]
    provider = next(
        receipt for receipt in receipts
        if receipt["source_id"] == ThreadsKeywordSearchSource.source_id
    )
    assert provider["metadata"]["item_failure_code"] == (
        "engagement_metrics_unavailable"
    )
    assert provider["metadata"]["new_observation_count"] == 0
    with store.connect() as connection:
        observations = connection.execute(
            "SELECT views, likes, comments, shares FROM mt_market_observations "
            "WHERE video_id = ? ORDER BY observed_at",
            (item.video_id,),
        ).fetchall()
        poll = connection.execute(
            "SELECT failure_count, last_error_code FROM mt_poll_queue "
            "WHERE video_id = ?",
            (item.video_id,),
        ).fetchone()
    assert [tuple(row) for row in observations] == [(1250, 95, 14, 8)]
    assert tuple(poll) == (1, "engagement_metrics_unavailable")
