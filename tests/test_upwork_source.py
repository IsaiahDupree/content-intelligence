from __future__ import annotations

import json
import sys
import threading
import time
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Iterator

import httpx
import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from services.market_tape.config import MarketTapeConfig  # noqa: E402
from services.market_tape.sources.upwork import (  # noqa: E402
    UpworkAPIError,
    UpworkRapidAPIClient,
)


class _RapidAPIHandler(BaseHTTPRequestHandler):
    requests: list[dict[str, Any]] = []

    def log_message(self, *_: object) -> None:
        return

    def do_POST(self) -> None:  # noqa: N802 - stdlib callback contract
        length = int(self.headers.get("content-length", "0"))
        body = json.loads(self.rfile.read(length))
        self.__class__.requests.append(
            {"path": self.path, "headers": dict(self.headers), "body": body}
        )
        if body.get("keyword") == "slow response":
            time.sleep(0.15)
        if body.get("keyword") == "provider error":
            self._json(
                429,
                {
                    "type": "error",
                    "status": "429",
                    "code": "quota_exceeded",
                    "message": "credit ceiling reached",
                    "requestId": "req-error",
                    "docUrl": "https://scraper.run/docs/errors",
                    "retryable": False,
                },
            )
            return
        if body.get("keyword") == "non json":
            payload = b"temporarily unavailable"
            self.send_response(502)
            self.send_header("content-type", "text/plain")
            self.send_header("content-length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)
            return
        if body.get("keyword") == "missing jobs":
            self._json(200, {"data": {"count": 0}, "meta": {}})
            return
        if body.get("keyword") == "non list jobs":
            self._json(200, {"data": {"jobs": {}}, "meta": {}})
            return
        if body.get("keyword") == "mixed jobs":
            self._json(
                200,
                {
                    "data": {
                        "jobs": [
                            {
                                "id": "~0123",
                                "title": "Build an AI automation",
                                "url": "https://www.upwork.com/jobs/~0123/",
                            },
                            "malformed-provider-item",
                        ],
                        "count": 2,
                    },
                    "meta": {},
                },
            )
            return
        if self.path == "/jobs":
            self._json(
                200,
                {
                    "data": {
                        "jobs": [
                            {
                                "id": "~0123",
                                "title": "Build an AI automation",
                                "url": "https://www.upwork.com/jobs/~0123/",
                            }
                        ],
                        "count": 1,
                        "estimatedTotal": 91,
                        "truncated": False,
                        "partial": False,
                    },
                    "meta": {
                        "creditsUsed": 1,
                        "requestId": "req-search",
                        "tool": "upwork-jobs",
                    },
                },
            )
            return
        if self.path == "/job-detail":
            if body.get("jobId") == "missing-job":
                self._json(200, {"data": {}, "meta": {}})
                return
            if body.get("jobId") == "null-job":
                self._json(200, {"data": {"job": None}, "meta": {}})
                return
            if body.get("jobId") == "malformed-job":
                self._json(200, {"data": {"job": ["not", "an", "object"]}, "meta": {}})
                return
            self._json(
                201,
                {
                    "data": {"job": {"id": body.get("jobId"), "title": "Detail"}},
                    "meta": {
                        "creditsUsed": 1,
                        "requestId": "req-detail",
                        "tool": "upwork-job-detail",
                    },
                },
            )
            return
        self._json(404, {"code": "not_found", "message": "missing"})

    def _json(self, status: int, body: dict[str, Any]) -> None:
        payload = json.dumps(body).encode()
        self.send_response(status)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(payload)))
        self.end_headers()
        try:
            self.wfile.write(payload)
        except (BrokenPipeError, ConnectionResetError):
            # A timeout test intentionally closes the client connection before
            # this real loopback server finishes its delayed response.
            return


@contextmanager
def _rapidapi_server() -> Iterator[tuple[str, type[_RapidAPIHandler]]]:
    _RapidAPIHandler.requests = []
    server = ThreadingHTTPServer(("127.0.0.1", 0), _RapidAPIHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}", _RapidAPIHandler
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def _config(tmp_path: Path, *, allow_metered_reads: bool = True) -> MarketTapeConfig:
    return MarketTapeConfig(
        db_path=tmp_path / "market-tape.sqlite3",
        object_dir=tmp_path / "objects",
        allow_metered_reads=allow_metered_reads,
    )


def _test_client(
    config: MarketTapeConfig,
    base_url: str,
    *,
    client: httpx.Client | None = None,
) -> UpworkRapidAPIClient:
    return UpworkRapidAPIClient(
        config,
        client=client,
        test_base_url=base_url,
        allow_loopback_test_transport=True,
    )


def test_health_is_credit_free_and_never_returns_key(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with _rapidapi_server() as (base_url, handler):
        monkeypatch.setenv("UPWORK_SCRAPER_RAPIDAPI_KEY", "top-secret-override")
        monkeypatch.setenv("RAPIDAPI_KEY", "shared-secret")
        client = _test_client(_config(tmp_path), base_url)
        try:
            health = client.health()
        finally:
            client.close()

    assert health["status"] == "ready"
    assert health["credential_source"] == "UPWORK_SCRAPER_RAPIDAPI_KEY"
    assert health["execute_metered_reads_default"] is False
    assert handler.requests == []
    assert "top-secret-override" not in json.dumps(health)
    assert "shared-secret" not in json.dumps(health)


def test_search_and_detail_match_provider_contract(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with _rapidapi_server() as (base_url, handler):
        monkeypatch.setenv("UPWORK_SCRAPER_RAPIDAPI_KEY", "secret")
        with httpx.Client(timeout=5) as transport:
            client = _test_client(_config(tmp_path), base_url, client=transport)
            search = client.search_jobs(
                keyword="ai automation",
                sort="recency",
                execute_metered_reads=True,
            )
            detail = client.job_detail(job_id="~0123", execute_metered_reads=True)

    assert search == {
        "ok": True,
        "query": {
            "keyword": "ai automation",
            "search_url": None,
            "sort": "recency",
        },
        "jobs": [
            {
                "id": "~0123",
                "title": "Build an AI automation",
                "url": "https://www.upwork.com/jobs/~0123/",
            }
        ],
        "count": 1,
        "estimated_total": 91,
        "truncated": False,
        "partial": False,
        "credits_used": 1,
        "request_id": "req-search",
        "tool": "upwork-jobs",
    }
    assert detail["job"] == {"id": "~0123", "title": "Detail"}
    assert detail["credits_used"] == 1
    assert handler.requests[0]["path"] == "/jobs"
    assert handler.requests[0]["body"] == {
        "keyword": "ai automation",
        "sort": "recency",
    }
    assert handler.requests[1]["path"] == "/job-detail"
    assert handler.requests[1]["body"] == {"jobId": "~0123"}
    assert (
        handler.requests[0]["headers"]["X-RapidAPI-Host"]
        == "upwork-jobs-scraper-api.p.rapidapi.com"
    )
    assert handler.requests[0]["headers"]["X-RapidAPI-Key"] == "secret"


def test_upwork_default_timeout_is_separate_from_generic_source_timeout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("MARKET_TAPE_UPWORK_REQUEST_TIMEOUT_SECONDS", raising=False)
    monkeypatch.setenv("UPWORK_SCRAPER_RAPIDAPI_KEY", "secret")
    config = MarketTapeConfig(
        db_path=tmp_path / "market-tape.sqlite3",
        object_dir=tmp_path / "objects",
        allow_metered_reads=True,
        request_timeout_seconds=0.02,
    )

    with _rapidapi_server() as (base_url, handler):
        client = _test_client(config, base_url)
        try:
            health = client.health()
            result = client.search_jobs(
                keyword="slow response", execute_metered_reads=True
            )
        finally:
            client.close()

    assert health["request_timeout_seconds"] == 60.0
    assert result["count"] == 1
    assert [row["body"]["keyword"] for row in handler.requests] == [
        "slow response"
    ]


def test_upwork_timeout_environment_override_reaches_real_transport(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("MARKET_TAPE_UPWORK_REQUEST_TIMEOUT_SECONDS", "0.05")
    monkeypatch.setenv("UPWORK_SCRAPER_RAPIDAPI_KEY", "secret")
    config = _config(tmp_path)

    with _rapidapi_server() as (base_url, handler):
        client = _test_client(config, base_url)
        try:
            assert client.health()["request_timeout_seconds"] == 0.05
            with pytest.raises(UpworkAPIError) as error:
                client.search_jobs(
                    keyword="slow response", execute_metered_reads=True
                )
        finally:
            client.close()

    assert error.value.code == "upstream_unreachable"
    assert error.value.retryable is True
    assert isinstance(error.value.__cause__, httpx.ReadTimeout)
    assert [row["body"]["keyword"] for row in handler.requests] == [
        "slow response"
    ]


def test_metered_gates_fail_before_transport(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with _rapidapi_server() as (base_url, handler):
        monkeypatch.setenv("UPWORK_SCRAPER_RAPIDAPI_KEY", "secret")
        disabled_call = _test_client(_config(tmp_path), base_url)
        with pytest.raises(UpworkAPIError, match="execute_metered_reads") as error:
            disabled_call.search_jobs(keyword="ai")
        disabled_call.close()
        assert error.value.code == "metered_reads_disabled"

        disabled_config = _test_client(
            _config(tmp_path, allow_metered_reads=False), base_url
        )
        with pytest.raises(UpworkAPIError, match="ALLOW_METERED") as error:
            disabled_config.search_jobs(
                keyword="ai", execute_metered_reads=True
            )
        disabled_config.close()

    assert error.value.code == "metered_reads_not_configured"
    assert handler.requests == []


def test_provider_error_envelope_and_non_json_are_preserved(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with _rapidapi_server() as (base_url, _):
        monkeypatch.setenv("UPWORK_SCRAPER_RAPIDAPI_KEY", "secret")
        client = _test_client(_config(tmp_path), base_url)
        with pytest.raises(UpworkAPIError) as error:
            client.search_jobs(
                keyword="provider error", execute_metered_reads=True
            )
        assert error.value.code == "quota_exceeded"
        assert error.value.status_code == 429
        assert error.value.request_id == "req-error"
        assert error.value.retryable is False
        with pytest.raises(UpworkAPIError) as non_json:
            client.search_jobs(keyword="non json", execute_metered_reads=True)
        client.close()

    assert non_json.value.code == "non_json_response"
    assert non_json.value.retryable is True


@pytest.mark.parametrize("keyword", ["missing jobs", "non list jobs"])
def test_search_rejects_missing_or_non_list_jobs(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    keyword: str,
) -> None:
    with _rapidapi_server() as (base_url, _):
        monkeypatch.setenv("UPWORK_SCRAPER_RAPIDAPI_KEY", "secret")
        client = _test_client(_config(tmp_path), base_url)
        try:
            with pytest.raises(UpworkAPIError) as error:
                client.search_jobs(
                    keyword=keyword, execute_metered_reads=True
                )
        finally:
            client.close()

    assert error.value.code == "unexpected_shape"


def test_search_preserves_non_mapping_job_items_for_ledger_rejection(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with _rapidapi_server() as (base_url, _):
        monkeypatch.setenv("UPWORK_SCRAPER_RAPIDAPI_KEY", "secret")
        client = _test_client(_config(tmp_path), base_url)
        try:
            result = client.search_jobs(
                keyword="mixed jobs", execute_metered_reads=True
            )
        finally:
            client.close()

    assert result["jobs"][1] == "malformed-provider-item"
    assert len(result["jobs"]) == 2


@pytest.mark.parametrize(
    "job_id", ["missing-job", "null-job", "malformed-job"]
)
def test_job_detail_rejects_missing_null_or_malformed_job(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    job_id: str,
) -> None:
    with _rapidapi_server() as (base_url, _):
        monkeypatch.setenv("UPWORK_SCRAPER_RAPIDAPI_KEY", "secret")
        client = _test_client(_config(tmp_path), base_url)
        try:
            with pytest.raises(UpworkAPIError) as error:
                client.job_detail(
                    job_id=job_id,
                    execute_metered_reads=True,
                )
        finally:
            client.close()

    assert error.value.code == "unexpected_shape"
    assert error.value.status_code == 502


@pytest.mark.parametrize(
    ("host", "base_url"),
    [
        (
            "upwork-jobs-scraper-api.p.rapidapi.com",
            "http://upwork-jobs-scraper-api.p.rapidapi.com",
        ),
        (
            "upwork-jobs-scraper-api.p.rapidapi.com",
            "https://evil.example.com",
        ),
        (
            "upwork-jobs-scraper-api.p.rapidapi.com",
            "https://user:secret@upwork-jobs-scraper-api.p.rapidapi.com",
        ),
        (
            "upwork-jobs-scraper-api.p.rapidapi.com",
            "https://upwork-jobs-scraper-api.p.rapidapi.com?redirect=evil",
        ),
        ("evil.example.com", "https://evil.example.com"),
    ],
)
def test_production_transport_rejects_unsafe_or_cross_origin_configuration(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    host: str,
    base_url: str,
) -> None:
    monkeypatch.setenv("UPWORK_SCRAPER_HOST", host)
    monkeypatch.setenv("UPWORK_SCRAPER_BASE_URL", base_url)
    monkeypatch.setenv("UPWORK_SCRAPER_RAPIDAPI_KEY", "must-not-leak")
    with pytest.raises(ValueError):
        UpworkRapidAPIClient(_config(tmp_path))


def test_injected_transport_must_disable_redirects(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("UPWORK_SCRAPER_RAPIDAPI_KEY", "must-not-leak")
    with _rapidapi_server() as (base_url, handler):
        with httpx.Client(follow_redirects=True) as transport:
            with pytest.raises(ValueError, match="disable redirects"):
                _test_client(_config(tmp_path), base_url, client=transport)
    assert handler.requests == []


@pytest.mark.parametrize(
    ("method", "arguments"),
    [
        ("search_jobs", {}),
        ("search_jobs", {"keyword": "a", "search_url": "https://example.com"}),
        ("job_detail", {}),
        ("job_detail", {"job_id": "1", "job_url": "https://example.com"}),
    ],
)
def test_argument_contract_requires_exactly_one_selector(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    method: str,
    arguments: dict[str, str],
) -> None:
    monkeypatch.setenv("UPWORK_SCRAPER_RAPIDAPI_KEY", "secret")
    client = UpworkRapidAPIClient(_config(tmp_path))
    try:
        with pytest.raises(ValueError, match="exactly one"):
            getattr(client, method)(**arguments)
    finally:
        client.close()
