"""Real loopback coverage for fail-closed MarketSource refresh counters."""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

import pytest

from services.market_tape.config import MarketTapeConfig
from services.market_tape.models import SourceState
from services.market_tape.sources.local_research import LocalResearchSource
from services.market_tape.sources.social import (
    InstagramRapidSource,
    MetaGraphSource,
    ThreadsKeywordSearchSource,
    TikTokRapidSource,
    TikTokResearchSource,
    XRecentSearchSource,
)
from services.market_tape.sources.youtube import YouTubeSource


class RefreshCounterProviderHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    response = {}
    requests = []

    def do_GET(self):  # noqa: N802 - HTTP handler contract
        self._handle("GET")

    def do_POST(self):  # noqa: N802 - HTTP handler contract
        self._handle("POST")

    def _handle(self, method):
        parsed = urlparse(self.path)
        length = int(self.headers.get("Content-Length", "0"))
        body = json.loads(self.rfile.read(length) or b"{}") if length else None
        self.__class__.requests.append({
            "method": method,
            "path": parsed.path,
            "query": parse_qs(parsed.query),
            "body": body,
        })
        encoded = json.dumps(self.__class__.response).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)

    def log_message(self, *_):
        return


@pytest.fixture
def refresh_counter_provider():
    RefreshCounterProviderHandler.response = {}
    RefreshCounterProviderHandler.requests = []
    server = ThreadingHTTPServer(
        ("127.0.0.1", 0),
        RefreshCounterProviderHandler,
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def _config(tmp_path, platform, *, youtube_batch_stats=False):
    return MarketTapeConfig(
        db_path=tmp_path / f"{platform}.sqlite3",
        object_dir=tmp_path / f"{platform}-objects",
        heartbeat_path=tmp_path / f"{platform}-heartbeat.json",
        lock_path=tmp_path / f"{platform}.lock",
        local_research_dir=tmp_path / "local-research",
        local_research_state_path=tmp_path / "local-research-state.json",
        prediction_model_dir=tmp_path / "models",
        platforms=[platform],
        topics=["creator economy"],
        adaptive_topics_enabled=False,
        daily_unique_target=1,
        platform_daily_targets={platform: 1},
        provider_daily_request_limits={platform: 10},
        provider_cost_per_request_usd={platform: 0.0},
        allow_metered_reads=True,
        youtube_batch_stats=youtube_batch_stats,
        supabase_sync_enabled=False,
    )


def _meta_source(config, run_id, platform, base_url, *, fields):
    account_env = (
        "INSTAGRAM_BUSINESS_ACCOUNT_ID"
        if platform == "instagram"
        else "FACEBOOK_PAGE_ID"
    )
    token_env = (
        "INSTAGRAM_ACCESS_TOKEN"
        if platform == "instagram"
        else "FACEBOOK_ACCESS_TOKEN"
    )
    return MetaGraphSource(
        config,
        run_id,
        10,
        platform=platform,
        account_env=account_env,
        token_envs=(token_env,),
        source_id=f"{platform}-graph-contract",
        edge="media" if platform == "instagram" else "videos",
        fields=fields,
        base_url=base_url,
    )


def test_terminal_metric_capabilities_are_fail_closed(tmp_path):
    sources = [
        YouTubeSource(_config(tmp_path, "youtube"), "youtube", 10),
        XRecentSearchSource(_config(tmp_path, "x"), "x", 10),
        TikTokResearchSource(_config(tmp_path, "tiktok"), "research", 10),
        TikTokRapidSource(_config(tmp_path, "tiktok"), "rapid", 10),
        InstagramRapidSource(
            _config(tmp_path, "instagram"),
            "instagram-rapid",
            10,
        ),
        _meta_source(
            _config(tmp_path, "instagram"),
            "instagram-graph",
            "instagram",
            "http://127.0.0.1:9",
            fields="id,caption,like_count,comments_count",
        ),
        _meta_source(
            _config(tmp_path, "facebook"),
            "facebook-graph",
            "facebook",
            "http://127.0.0.1:9",
            fields="id,views,likes.summary(true),comments.summary(true)",
        ),
        _meta_source(
            _config(tmp_path, "facebook"),
            "facebook-without-views",
            "facebook",
            "http://127.0.0.1:9",
            fields="id,likes.summary(true),comments.summary(true)",
        ),
        ThreadsKeywordSearchSource(
            _config(tmp_path, "threads"),
            "threads",
            10,
        ),
        LocalResearchSource(
            _config(tmp_path, "threads"),
            "local",
            10,
            platform="threads",
            api_platform="threads",
        ),
    ]
    try:
        assert sources[0].terminal_metrics_capable() is True
        assert sources[1].terminal_metrics_capable() is True
        assert [source.terminal_metrics_capable() for source in sources[2:6]] == [
            False,
            False,
            False,
            False,
        ]
        assert sources[6].terminal_metrics_capable() is True
        assert sources[7].terminal_metrics_capable() is False
        assert sources[8].terminal_metrics_capable() is False
        assert sources[9].terminal_metrics_capable() is False
    finally:
        for source in sources:
            source.close()


SOURCE_KINDS = (
    "youtube",
    "tiktok_research",
    "tiktok_rapid",
    "instagram_rapid",
    "x",
    "instagram_graph",
    "facebook_graph",
    "threads",
)


def _counter_payload(source_kind, variant):
    include_counter = variant != "absent"
    counter = 0 if variant == "zero" else "not-a-number"
    external_id = "content-1"
    if source_kind == "youtube":
        raw = {"id": external_id, "statistics": {}}
        if include_counter:
            raw["statistics"]["viewCount"] = counter
        return {"items": [raw]}
    if source_kind == "tiktok_research":
        raw = {"id": external_id, "username": "creator"}
        if include_counter:
            raw["view_count"] = counter
        return {"data": {"videos": [raw]}}
    if source_kind == "tiktok_rapid":
        raw = {
            "video_id": external_id,
            "author": {"id": "creator-id", "unique_id": "creator"},
        }
        if include_counter:
            raw["play_count"] = counter
        return {"data": raw}
    if source_kind == "instagram_rapid":
        raw = {
            "id": external_id,
            "code": "shortcode-1",
            "media_type": 2,
            "user": {"id": "creator-id", "username": "creator"},
        }
        if include_counter:
            raw["play_count"] = counter
        return {"data": raw}
    if source_kind == "x":
        metrics = {
            "like_count": 0,
            "reply_count": 0,
            "retweet_count": 0,
            "quote_count": 0,
            "bookmark_count": 0,
        }
        if include_counter:
            metrics["impression_count"] = counter
        return {
            "data": [{
                "id": external_id,
                "author_id": "creator-id",
                "public_metrics": metrics,
            }],
            "includes": {"users": []},
        }
    raw = {"id": external_id}
    if source_kind == "threads":
        raw.update({"owner": {"id": "creator-id"}, "username": "creator"})
    if include_counter:
        raw["views"] = counter
    return raw


def _refresh_source(source_kind, tmp_path, base_url, monkeypatch):
    if source_kind == "youtube":
        monkeypatch.setenv("YOUTUBE_API_KEY", "loopback-youtube-key")
        return YouTubeSource(
            _config(tmp_path, "youtube"),
            "youtube-refresh",
            10,
            base_url=base_url,
        )
    if source_kind == "tiktok_research":
        monkeypatch.setenv(
            "TIKTOK_RESEARCH_ACCESS_TOKEN",
            "loopback-research-token",
        )
        return TikTokResearchSource(
            _config(tmp_path, "tiktok"),
            "research-refresh",
            10,
            base_url=base_url,
        )
    if source_kind == "tiktok_rapid":
        monkeypatch.setenv("RAPIDAPI_KEY", "loopback-rapid-key")
        return TikTokRapidSource(
            _config(tmp_path, "tiktok"),
            "rapid-refresh",
            10,
            base_url=base_url,
        )
    if source_kind == "instagram_rapid":
        monkeypatch.setenv("RAPIDAPI_KEY", "loopback-rapid-key")
        return InstagramRapidSource(
            _config(tmp_path, "instagram"),
            "instagram-rapid-refresh",
            10,
            base_url=base_url,
        )
    if source_kind == "x":
        monkeypatch.setenv("X_BEARER_TOKEN", "loopback-x-token")
        return XRecentSearchSource(
            _config(tmp_path, "x"),
            "x-refresh",
            10,
            base_url=base_url,
        )
    if source_kind == "instagram_graph":
        monkeypatch.setenv("INSTAGRAM_BUSINESS_ACCOUNT_ID", "123")
        monkeypatch.setenv("INSTAGRAM_ACCESS_TOKEN", "loopback-meta-token")
        return _meta_source(
            _config(tmp_path, "instagram"),
            "instagram-graph-refresh",
            "instagram",
            base_url,
            fields="id,caption,like_count,comments_count",
        )
    if source_kind == "facebook_graph":
        monkeypatch.setenv("FACEBOOK_PAGE_ID", "123")
        monkeypatch.setenv("FACEBOOK_ACCESS_TOKEN", "loopback-meta-token")
        return _meta_source(
            _config(tmp_path, "facebook"),
            "facebook-graph-refresh",
            "facebook",
            base_url,
            fields="id,views,likes.summary(true),comments.summary(true)",
        )
    monkeypatch.setenv("THREADS_ACCESS_TOKEN", "loopback-threads-token")
    return ThreadsKeywordSearchSource(
        _config(tmp_path, "threads"),
        "threads-refresh",
        10,
        base_url=base_url,
    )


def _assert_request_contract(source_kind, request, source):
    assert request["method"] == (
        "POST" if source_kind == "tiktok_research" else "GET"
    )
    if source_kind == "youtube":
        assert request["path"] == "/videos"
        assert request["query"]["part"] == [
            "snippet,statistics,contentDetails"
        ]
    elif source_kind == "tiktok_research":
        assert request["path"] == "/v2/research/video/query/"
        assert "view_count" in request["query"]["fields"][0].split(",")
        assert request["body"]["query"]["and"][0]["field_name"] == "video_id"
    elif source_kind == "tiktok_rapid":
        assert request["path"] == "/video/info"
        assert request["query"]["video_id"] == ["content-1"]
    elif source_kind == "instagram_rapid":
        assert request["path"] == "/post-info"
        assert request["query"]["code"] == ["shortcode-1"]
    elif source_kind == "x":
        assert request["path"] == "/tweets"
        assert "public_metrics" in request["query"]["tweet.fields"][0]
    else:
        assert request["path"] == "/content-1"
        assert request["query"]["fields"] == [source.fields]


@pytest.mark.parametrize("source_kind", SOURCE_KINDS)
@pytest.mark.parametrize("variant", ("absent", "nonnumeric", "zero"))
def test_refresh_requires_an_explicit_numeric_primary_counter(
    source_kind,
    variant,
    refresh_counter_provider,
    tmp_path,
    monkeypatch,
):
    RefreshCounterProviderHandler.response = _counter_payload(
        source_kind,
        variant,
    )
    source = _refresh_source(
        source_kind,
        tmp_path,
        refresh_counter_provider,
        monkeypatch,
    )
    prior = {
        "external_id": "content-1",
        "creator_external_id": "creator-id",
        "creator_handle": "creator",
        "url": "https://www.instagram.com/p/shortcode-1/",
        "shortcode": "shortcode-1",
    }
    try:
        batch = source.refresh([prior])
    finally:
        source.close()

    assert batch.receipt.state == SourceState.READY
    assert batch.receipt.request_count == 1
    _assert_request_contract(
        source_kind,
        RefreshCounterProviderHandler.requests[-1],
        source,
    )
    expected_missing = 0 if variant == "zero" else 1
    assert batch.receipt.metadata["metadata_only_count"] == expected_missing
    assert batch.receipt.metadata["missing_counter_count"] == expected_missing
    assert batch.receipt.metadata["item_failure_code"] == (
        "" if variant == "zero" else "engagement_metrics_unavailable"
    )
    assert batch.receipt.refreshed_count == (1 if variant == "zero" else 0)
    assert len(batch.items) == batch.receipt.refreshed_count
    if batch.items:
        assert batch.items[0].metrics.views == 0
