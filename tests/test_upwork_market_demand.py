from __future__ import annotations

import json
import inspect
import sqlite3
import sys
import threading
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Iterator

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from services.market_tape.config import MarketTapeConfig  # noqa: E402
from services.market_tape.models import isoformat, stable_hash  # noqa: E402
from services.market_tape.semantic import (  # noqa: E402
    TOPIC_LEVELS,
    TOPIC_RELATIONSHIPS,
    SemanticTopicService,
)
from services.market_tape.sources.upwork import UpworkAPIError  # noqa: E402
from services.market_tape.store import MarketTapeStore  # noqa: E402
from services.market_tape.upwork_demand import (  # noqa: E402
    UPWORK_SEMANTIC_LINK_CONTRACT,
    UPWORK_SCRIPT_CONTEXT_CONTRACT,
    UPWORK_TABLE_ENTITY_TYPES,
    UpworkDemandService,
    _classify,
)


def _job(number: int, *, client: str | None = None) -> dict[str, Any]:
    return {
        "id": f"~02{number:04d}",
        "title": f"Build AI automation workflow {number}",
        "url": f"https://www.upwork.com/jobs/~02{number:04d}/?source=test",
        "description": {
            "snippet": "Use OpenAI to automate a real estate sales workflow.",
            "text": "Private full public job description retained only in evidence.",
        },
        "postedText": f"2026-08-{20 + number:02d}T12:00:00Z",
        "client": {"id": client or f"client-{number}", "country": "US"},
        "budget": {"type": "fixed", "amount": 1000 + number, "currency": "USD"},
        "proposalCount": number,
        "skills": [{"name": "OpenAI"}, {"name": "Workflow automation"}],
        "experienceLevel": "expert",
    }


class _DemandRapidAPIHandler(BaseHTTPRequestHandler):
    batches: list[dict[str, Any]] = []
    requests: list[dict[str, Any]] = []
    base_url = ""

    def log_message(self, *_: object) -> None:
        return

    def do_POST(self) -> None:  # noqa: N802 - stdlib callback contract
        length = int(self.headers.get("content-length", "0"))
        body = json.loads(self.rfile.read(length))
        index = len(self.__class__.requests)
        self.__class__.requests.append({"path": self.path, "body": body})
        batch = self.__class__.batches[min(index, len(self.__class__.batches) - 1)]
        status = int(batch.get("status", 200))
        if status >= 400:
            response = batch.get("response") or {
                "code": "provider_timeout",
                "message": "provider timed out",
                "status": status,
                "retryable": True,
            }
            payload = json.dumps(response).encode()
            self.send_response(status)
            self.send_header("content-type", "application/json")
            self.send_header("content-length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)
            return
        response = {
            "data": {
                "jobs": batch["jobs"],
                "count": len(batch["jobs"]),
                "estimatedTotal": len(batch["jobs"]),
                "truncated": bool(batch.get("truncated", False)),
                "partial": bool(batch.get("partial", False)),
            },
            "meta": {
                "creditsUsed": 1,
                "requestId": f"req-{index}",
                "tool": "upwork-jobs",
            },
        }
        payload = json.dumps(response).encode()
        self.send_response(200)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)


@contextmanager
def _provider(
    batches: list[dict[str, Any]], monkeypatch: pytest.MonkeyPatch
) -> Iterator[type[_DemandRapidAPIHandler]]:
    _DemandRapidAPIHandler.batches = batches
    _DemandRapidAPIHandler.requests = []
    server = ThreadingHTTPServer(("127.0.0.1", 0), _DemandRapidAPIHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    monkeypatch.setenv(
        "UPWORK_SCRAPER_RAPIDAPI_KEY", "test-secret"
    )
    _DemandRapidAPIHandler.base_url = (
        f"http://127.0.0.1:{server.server_port}"
    )
    try:
        yield _DemandRapidAPIHandler
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


class _Clock:
    def __init__(self, value: datetime | None = None) -> None:
        self.value = value or datetime(2026, 8, 28, 12, tzinfo=timezone.utc)

    def __call__(self) -> datetime:
        return self.value

    def advance(self, *, hours: int = 1) -> None:
        self.value += timedelta(hours=hours)


def _service(
    config: MarketTapeConfig,
    provider: type[_DemandRapidAPIHandler],
    clock: _Clock | None = None,
) -> UpworkDemandService:
    return UpworkDemandService(
        config,
        clock=clock or _Clock(),
        test_base_url=provider.base_url,
        allow_loopback_test_transport=True,
    )


def _config(
    tmp_path: Path,
    *,
    allow_metered_reads: bool = True,
    daily_limit: int = 20,
) -> MarketTapeConfig:
    return MarketTapeConfig(
        db_path=tmp_path / "market.sqlite3",
        object_dir=tmp_path / "objects",
        heartbeat_path=tmp_path / "heartbeat.json",
        lock_path=tmp_path / "market.lock",
        local_research_state_path=tmp_path / "local-state.json",
        prediction_model_dir=tmp_path / "models",
        allow_metered_reads=allow_metered_reads,
        upwork_default_queries=["AI automation"],
        upwork_max_queries_per_scan=3,
        upwork_daily_request_limit=daily_limit,
        upwork_prediction_min_snapshots=3,
    )


def _graph() -> dict[str, Any]:
    nodes: list[dict[str, Any]] = []
    relationships: list[dict[str, str]] = []
    parent_id: str | None = None
    for index, level in enumerate(TOPIC_LEVELS):
        topic_id = f"{level}.freelance-demand-test"
        nodes.append(
            {
                "id": topic_id,
                "name": f"Freelance demand {level.replace('_', ' ')}",
                "definition": f"Canonical {level} for Upwork demand tests.",
                "level": level,
                "canonical_parent_id": parent_id,
                "aliases": [f"Upwork {level}"],
                "status": "active",
                "strategic_priority": 90 - index,
            }
        )
        if parent_id:
            relationships.append(
                {
                    "source_topic_id": topic_id,
                    "target_topic_id": parent_id,
                    "relationship_type": "part_of",
                }
            )
        parent_id = topic_id
    core = {
        "schema_version": "2.0",
        "contract_type": "content_topic_graph_v2",
        "levels": list(TOPIC_LEVELS),
        "relationship_types": list(TOPIC_RELATIONSHIPS),
        "nodes": nodes,
        "relationships": relationships,
        "metadata": {"graph_version": "upwork-demand-test-v1"},
        "migration": {},
        "inventory": {
            "node_count": len(nodes),
            "relationship_count": len(relationships),
            "by_level": {level: 1 for level in TOPIC_LEVELS},
        },
    }
    return {**core, "graph_sha256": stable_hash(core)}


def test_ai_classification_uses_token_boundaries() -> None:
    assert _classify(
        "Repair email detail page",
        "Fix delivery settings and a chair catalog.",
        ["Email support"],
    ) == ("general_freelancing", "general_delivery")
    assert _classify(
        "Build AI email assistant",
        "Use an LLM for support triage.",
        ["OpenAI"],
    ) == ("ai_demand", "build_ai_product")


def test_scan_is_append_only_deduplicated_and_passport_registered(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with _provider([{"jobs": [_job(1), _job(2)]}], monkeypatch) as provider:
        config = _config(tmp_path)
        store = MarketTapeStore(config)
        clock = _Clock()
        service = _service(config, provider, clock)
        first = service.scan(execute_metered_reads=True)
        clock.advance()
        second = service.scan(execute_metered_reads=True)
        jobs = service.list_jobs()
        service.close()

    assert len(provider.requests) == 2
    assert first["request_units_reserved"] == first["request_units_executed"] == 1
    assert second["unique_jobs_inserted"] == 0
    assert jobs["count"] == 2
    assert jobs["description_included"] is False
    assert all("description" not in job for job in jobs["jobs"])
    with store.connect() as connection:
        counts = {
            table: connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            for table, _, _ in UPWORK_TABLE_ENTITY_TYPES
        }
        raw = connection.execute(
            "SELECT * FROM mt_raw_objects WHERE source_id = 'rapidapi_upwork'"
        ).fetchall()
        outbox_types = {
            row[0]
            for row in connection.execute(
                "SELECT entity_type FROM mt_sync_outbox"
            ).fetchall()
        }
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            connection.execute(
                """UPDATE mt_upwork_request_reservations
                   SET request_units = 2 WHERE request_reservation_id = ?""",
                (first["request_reservation_id"],),
            )
    assert counts["mt_upwork_request_reservations"] == 2
    assert counts["mt_upwork_scan_runs"] == 2
    assert counts["mt_upwork_jobs"] == 2
    assert counts["mt_upwork_job_versions"] == 2
    assert counts["mt_upwork_job_observations"] == 4
    assert len(raw) == 2
    for row in raw:
        assert row["object_path"].startswith("upwork/")
        assert (config.object_dir / row["object_path"]).is_file()
        assert row["bytes_compressed"] > 0
    assert "upwork_request_reservation" in outbox_types
    assert "upwork_scan_run" in outbox_types
    assert "upwork_demand_snapshot" in outbox_types


def test_gates_do_not_reserve_or_call_provider(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with _provider([{"jobs": [_job(1)]}], monkeypatch) as provider:
        config = _config(tmp_path)
        store = MarketTapeStore(config)
        service = _service(config, provider)
        with pytest.raises(UpworkAPIError) as error:
            service.scan(execute_metered_reads=False)
        service.close()

    assert error.value.code == "metered_reads_disabled"
    assert provider.requests == []
    with store.connect() as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM mt_upwork_request_reservations"
        ).fetchone()[0] == 0


def test_scan_accounts_for_malformed_items_and_local_truncation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    provider_jobs: list[Any] = [
        _job(1),
        "malformed-provider-item",
        _job(2),
    ]
    with _provider([{"jobs": provider_jobs}], monkeypatch) as provider:
        config = _config(tmp_path)
        store = MarketTapeStore(config)
        service = _service(config, provider)
        result = service.scan(
            execute_metered_reads=True,
            max_jobs_per_query=2,
        )
        service.close()

    assert result["state"] == "partial"
    assert result["accepted_job_observations"] == 1
    assert result["rejected_jobs"] == 1
    with store.connect() as connection:
        observation = connection.execute(
            "SELECT * FROM mt_upwork_query_observations"
        ).fetchone()
    assert observation["returned_count"] == 3
    assert observation["accepted_count"] == 1
    assert observation["rejected_count"] == 1
    assert observation["partial_evidence"] == 1


def test_duplicate_provider_job_id_is_rejected_without_orphaning_scan(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    duplicate = _job(1)
    with _provider([{"jobs": [duplicate, dict(duplicate)]}], monkeypatch) as provider:
        config = _config(tmp_path)
        store = MarketTapeStore(config)
        service = _service(config, provider)
        result = service.scan(execute_metered_reads=True)
        service.close()

    assert result["state"] == "partial"
    assert result["accepted_job_observations"] == 1
    assert result["rejected_jobs"] == 1
    with store.connect() as connection:
        counts = {
            table: connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            for table in (
                "mt_upwork_request_reservations",
                "mt_upwork_scan_runs",
                "mt_upwork_jobs",
                "mt_upwork_job_observations",
            )
        }
        observation = connection.execute(
            "SELECT * FROM mt_upwork_query_observations"
        ).fetchone()
    assert counts == {
        "mt_upwork_request_reservations": 1,
        "mt_upwork_scan_runs": 1,
        "mt_upwork_jobs": 1,
        "mt_upwork_job_observations": 1,
    }
    assert observation["returned_count"] == 2
    assert observation["accepted_count"] == 1
    assert observation["rejected_count"] == 1
    assert observation["partial_evidence"] == 1


def test_equivalent_upwork_hosts_share_one_identity_without_orphaning_scan(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    first = _job(1)
    second = _job(1)
    second["url"] = "https://upwork.com/jobs/~020001"
    with _provider(
        [{"jobs": [first]}, {"jobs": [second]}], monkeypatch
    ) as provider:
        config = _config(tmp_path)
        store = MarketTapeStore(config)
        service = _service(config, provider)
        result = service.scan(
            queries=["AI automation", "OpenAI"],
            execute_metered_reads=True,
        )
        service.close()

    assert result["state"] == "complete"
    assert result["accepted_job_observations"] == 2
    assert result["rejected_jobs"] == 0
    with store.connect() as connection:
        counts = {
            table: connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            for table in (
                "mt_upwork_request_reservations",
                "mt_upwork_scan_runs",
                "mt_upwork_jobs",
                "mt_upwork_job_observations",
            )
        }
        job = connection.execute("SELECT * FROM mt_upwork_jobs").fetchone()
    assert counts == {
        "mt_upwork_request_reservations": 1,
        "mt_upwork_scan_runs": 1,
        "mt_upwork_jobs": 1,
        "mt_upwork_job_observations": 2,
    }
    assert job["canonical_url"] == "https://www.upwork.com/jobs/~020001"


def test_orphan_reservation_is_visible_and_consumes_daily_ceiling(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with _provider([{"jobs": [_job(1)]}], monkeypatch) as provider:
        config = _config(tmp_path, daily_limit=1)
        MarketTapeStore(config)
        clock = _Clock()
        service = _service(config, provider, clock)
        reservation = service._reserve_requests(  # audit crash boundary
            ["AI automation"], str(isoformat(clock()))
        )
        health = service.health()
        with pytest.raises(UpworkAPIError) as exhausted:
            service.scan(execute_metered_reads=True)
        service.close()

    assert reservation["request_units"] == 1
    assert health["status"] == "degraded_audit"
    assert health["ledger"]["unfulfilled_reservations"] == 1
    assert exhausted.value.code == "request_budget_exhausted"
    assert provider.requests == []


def test_prediction_abstains_then_backtests_without_future_leakage(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    batches = [
        {"jobs": [_job(1)]},
        {"jobs": [_job(1), _job(2)]},
        {"jobs": [_job(1), _job(2), _job(3)]},
        {"jobs": [_job(1), _job(2), _job(3), _job(4)]},
    ]
    with _provider(batches, monkeypatch) as provider:
        config = _config(tmp_path)
        MarketTapeStore(config)
        clock = _Clock(datetime(2026, 8, 29, 12, tzinfo=timezone.utc))
        service = _service(config, provider, clock)
        for _ in range(4):
            service.scan(execute_metered_reads=True)
            clock.advance()
        demand = service.demand_report(
            cohort_type="query", cohort_key="AI automation", limit=10
        )
        backtest = service.backtest_report(
            cohort_type="query", cohort_key="AI automation"
        )
        service.close()

    latest = demand["cohorts"][0]
    assert latest["unique_jobs"] == 4
    assert latest["prediction"]["direction"] == "rising"
    assert latest["prediction"]["as_of"] == latest["observed_at"]
    assert any(
        cohort["prediction"]["direction"] == "abstain"
        for cohort in demand["cohorts"]
    )
    assert backtest["scored_count"] >= 1
    # The next complete snapshot kept the same arrival rate, so an earlier
    # rising call is honestly scored incorrect rather than rewritten.
    assert backtest["directional_accuracy"] == 0.0
    assert backtest["future_leakage"] is False


def test_old_partial_snapshot_does_not_poison_complete_prediction_history(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    batches = [
        {"jobs": [_job(1)], "partial": True},
        {"jobs": [_job(1)]},
        {"jobs": [_job(1), _job(2)]},
        {"jobs": [_job(1), _job(2), _job(3)]},
    ]
    with _provider(batches, monkeypatch) as provider:
        config = _config(tmp_path)
        store = MarketTapeStore(config)
        clock = _Clock(datetime(2026, 8, 29, 16, tzinfo=timezone.utc))
        service = _service(config, provider, clock)
        for _ in range(4):
            service.scan(execute_metered_reads=True)
            clock.advance()
        service.close()

    with store.connect() as connection:
        rows = connection.execute(
            """SELECT snapshot.demand_snapshot_id, snapshot.observed_at,
                      snapshot.partial_evidence, prediction.direction,
                      prediction.history_snapshot_ids_json
               FROM mt_upwork_demand_snapshots snapshot
               JOIN mt_upwork_predictions prediction
                 ON prediction.demand_snapshot_id = snapshot.demand_snapshot_id
               WHERE snapshot.cohort_type = 'query'
                 AND snapshot.cohort_key = 'ai automation'
               ORDER BY snapshot.observed_at"""
        ).fetchall()
    assert rows[0]["partial_evidence"] == 1
    assert rows[0]["direction"] == "abstain"
    assert rows[-1]["direction"] == "rising"
    history_ids = json.loads(rows[-1]["history_snapshot_ids_json"])
    assert rows[0]["demand_snapshot_id"] not in history_ids
    assert history_ids == [row["demand_snapshot_id"] for row in rows[1:]]


def test_fixed_size_result_turnover_registers_rising_arrival_demand(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    batches = [
        {"jobs": [_job(1), _job(2)]},
        {"jobs": [_job(2), _job(3)]},
        {"jobs": [_job(3), _job(4)]},
    ]
    with _provider(batches, monkeypatch) as provider:
        config = _config(tmp_path)
        MarketTapeStore(config)
        clock = _Clock(datetime(2026, 8, 29, 20, tzinfo=timezone.utc))
        service = _service(config, provider, clock)
        for _ in range(3):
            service.scan(execute_metered_reads=True)
            clock.advance()
        report = service.demand_report(
            cohort_type="query", cohort_key="AI automation", limit=3
        )
        service.close()

    assert [row["unique_jobs"] for row in report["cohorts"]] == [2, 2, 2]
    assert report["cohorts"][0]["new_jobs"] == 1
    assert report["cohorts"][0]["velocity"] == 1.0
    assert report["cohorts"][0]["prediction"]["direction"] == "rising"


def test_scan_timestamp_is_internal_clock_only(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    captured = datetime(2026, 8, 30, 9, 15, tzinfo=timezone.utc)
    with _provider([{"jobs": [_job(1)]}], monkeypatch) as provider:
        config = _config(tmp_path)
        MarketTapeStore(config)
        service = _service(config, provider, _Clock(captured))
        result = service.scan(execute_metered_reads=True)
        with pytest.raises(TypeError, match="observed_at"):
            service.scan(  # type: ignore[call-arg]
                execute_metered_reads=True,
                observed_at="2000-01-01T00:00:00Z",
            )
        service.close()

    assert "observed_at" not in inspect.signature(UpworkDemandService.scan).parameters
    assert result["observed_at"] == isoformat(captured)


def test_budget_metrics_separate_usd_fixed_totals_from_hourly_rates(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fixed_usd = _job(1)
    fixed_usd["budget"]["currency"] = "US$"
    hourly_usd = _job(2)
    hourly_usd["budget"] = {"type": "hourly", "currency": "$"}
    hourly_usd["hourlyBudget"] = {"min": 20, "max": 40}
    fixed_eur = _job(3)
    fixed_eur["budget"]["currency"] = "EUR"
    fixed_eur["budget"]["amount"] = 9000
    with _provider(
        [{"jobs": [fixed_usd, hourly_usd, fixed_eur]}], monkeypatch
    ) as provider:
        config = _config(tmp_path)
        MarketTapeStore(config)
        clock = _Clock(datetime(2026, 8, 29, 21, tzinfo=timezone.utc))
        service = _service(config, provider, clock)
        service.scan(execute_metered_reads=True)
        report = service.demand_report(
            cohort_type="query", cohort_key="AI automation"
        )
        service.close()

    cohort = report["cohorts"][0]
    assert cohort["unique_jobs"] == 3
    assert cohort["fixed_budget_usd_coverage"] == pytest.approx(1 / 3)
    assert cohort["median_fixed_budget_usd"] == 1001
    assert cohort["hourly_rate_usd_coverage"] == pytest.approx(1 / 3)
    assert cohort["median_hourly_rate_usd"] == 30


def test_semantic_selection_is_required_for_aggregate_script_context(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with _provider([{"jobs": [_job(1)]}], monkeypatch) as provider:
        config = _config(tmp_path)
        store = MarketTapeStore(config)
        clock = _Clock(datetime(2026, 8, 30, 12, tzinfo=timezone.utc))
        service = _service(config, provider, clock)
        service.scan(execute_metered_reads=True)
        blocked = service.script_context()
        semantic = SemanticTopicService(store)
        imported = semantic.import_graph(
            {
                "source_service": "upwork-demand-test",
                "source_receipt_id": "receipt:graph",
                "imported_by": "pytest",
                "imported_at": "2026-08-30T12:01:00Z",
                "graph": _graph(),
            }
        )
        materialized = service.materialize_signals(
            graph_version_id=imported["graph"]["graph_version_id"], limit=1
        )
        signal_id = materialized["created_signal_ids"][0]
        binding = semantic.record_binding(
            {
                "signal_id": signal_id,
                "topic_id": "atomic_subject.freelance-demand-test",
                "decision": "approved",
                "binding_method": "reviewed_rules",
                "confidence": 1.0,
                "rationale": "The aggregate demand cohort matches the canonical subject.",
                "reviewer_type": "rules",
                "reviewed_by": "pytest",
                "reviewed_at": "2026-08-30T12:02:00Z",
                "source_receipt_id": "receipt:upwork-demand",
                "review_receipt_id": "receipt:binding-review",
                "audit": {"automatic_binding": False},
            }
        )
        second_binding = semantic.record_binding(
            {
                "signal_id": signal_id,
                "topic_id": "atomic_subject.freelance-demand-test",
                "decision": "approved",
                "binding_method": "reviewed_rules",
                "confidence": 1.0,
                "rationale": "A second approved receipt resolves the same demand signal.",
                "reviewer_type": "rules",
                "reviewed_by": "pytest-second-review",
                "reviewed_at": "2026-08-30T12:02:30Z",
                "source_receipt_id": "receipt:upwork-demand-second",
                "review_receipt_id": "receipt:binding-review-second",
                "audit": {"automatic_binding": False},
            }
        )
        selection = semantic.record_atomic_selection(
            {
                "graph_version_id": imported["graph"]["graph_version_id"],
                "atomic_topic_id": "atomic_subject.freelance-demand-test",
                "binding_ids": [
                    binding["binding_id"],
                    second_binding["binding_id"],
                ],
                "reviewer_type": "rules",
                "reviewer_id": "pytest",
                # Intentionally backdated: the script context must remain
                # deterministic while never predating the evidence it carries.
                "reviewed_at": "2026-08-30T11:59:00Z",
                "review_receipt_id": "receipt:selection-review",
                "rationale": "Approved aggregate demand evidence for script planning.",
            }
        )
        context = service.script_context(
            selection_id=selection["selection"]["selection_id"]
        )
        repeated_context = service.script_context(
            selection_id=selection["selection"]["selection_id"]
        )
        semantic.record_binding(
            {
                "signal_id": signal_id,
                "topic_id": "atomic_subject.freelance-demand-test",
                "decision": "revoked",
                "binding_method": "human_review",
                "confidence": 1.0,
                "rationale": "The selected demand observation was withdrawn.",
                "reviewer_type": "human",
                "reviewed_by": "pytest-revoker",
                "reviewed_at": "2026-08-30T12:03:00Z",
                "source_receipt_id": "receipt:upwork-demand-revoked",
                "review_receipt_id": "receipt:binding-revoked",
                "audit": {"automatic_binding": False},
            }
        )
        revoked_context = service.script_context(
            selection_id=selection["selection"]["selection_id"]
        )
        service.close()

    assert blocked["generation_authorized"] is False
    assert blocked["cohorts"] == []
    assert context["contract"] == UPWORK_SCRIPT_CONTEXT_CONTRACT
    assert context["demand_source"] == "upwork_rapidapi"
    assert context["generation_authorized"] is True
    assert context["blockers"] == []
    assert context == repeated_context
    assert context["generated_at"] == "2026-08-30T12:00:00+00:00"
    assert all(
        cohort["prediction"]["as_of"] <= context["generated_at"]
        for cohort in context["cohorts"]
    )
    assert set(context["selection"]) == {
        "selection_id",
        "review_status",
        "atomic_topic_id",
        "semantic_link_ids",
        "observation_ids",
    }
    assert context["selection"]["review_status"] == "approved"
    assert context["selection"]["semantic_link_ids"] == materialized[
        "created_semantic_link_ids"
    ]
    assert context["selection"]["observation_ids"] == sorted(
        [
            binding["observation"]["topic_observation_key"],
            second_binding["observation"]["topic_observation_key"],
        ]
    )
    assert len(context["cohorts"]) == 1
    assert context["cohorts"][0]["cohort_key"].startswith("upwork-cohort:")
    assert "openai" not in context["cohorts"][0]["cohort_key"].lower()
    assert set(context["cohorts"][0]) == {
        "cohort_type",
        "cohort_key",
        "observed_at",
        "unique_jobs",
        "new_jobs",
        "unique_clients",
        "velocity",
        "acceleration",
        "fixed_budget_usd_coverage",
        "median_fixed_budget_usd",
        "hourly_rate_usd_coverage",
        "median_hourly_rate_usd",
        "proposal_coverage",
        "median_proposals",
        "evidence_state",
        "partial_evidence",
        "prediction",
    }
    assert context["policy"] == {
        "aggregate_only": True,
        "raw_job_text_included": False,
        "automatic_binding": False,
        "claims_require_receipts": True,
    }
    assert "Private full public job description" not in json.dumps(context)
    context_core = {
        key: value for key, value in context.items() if key != "context_sha256"
    }
    assert context["context_sha256"] == stable_hash(context_core)
    assert revoked_context["generation_authorized"] is False
    assert revoked_context["cohorts"] == []
    assert "selection_binding_no_longer_approved" in revoked_context["blockers"]


def test_failed_zero_job_snapshot_cannot_reach_script_generation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    batches = [
        {"jobs": [_job(1)]},
        {"status": 502},
    ]
    with _provider(batches, monkeypatch) as provider:
        config = _config(tmp_path)
        store = MarketTapeStore(config)
        clock = _Clock(datetime(2026, 8, 30, 12, tzinfo=timezone.utc))
        service = _service(config, provider, clock)
        valid_scan = service.scan(execute_metered_reads=True)
        clock.advance()
        failed_scan = service.scan(execute_metered_reads=True)
        semantic = SemanticTopicService(store)
        imported = semantic.import_graph(
            {
                "source_service": "upwork-demand-test",
                "source_receipt_id": "receipt:graph-zero-evidence",
                "imported_by": "pytest",
                "imported_at": "2026-08-30T14:00:00Z",
                "graph": _graph(),
            }
        )
        graph_version_id = imported["graph"]["graph_version_id"]
        materialized = service.materialize_signals(
            graph_version_id=graph_version_id, limit=10
        )
        with store.connect() as connection:
            materialized_links = connection.execute(
                """SELECT signal_id, demand_snapshot_id
                   FROM mt_upwork_semantic_links
                   WHERE graph_version_id = ? AND cohort_type = 'query'
                   ORDER BY created_at, semantic_link_id""",
                (graph_version_id,),
            ).fetchall()
        signal_id = str(materialized_links[0]["signal_id"])
        bindings = []
        for index in range(2):
            bindings.append(
                semantic.record_binding(
                    {
                        "signal_id": signal_id,
                        "topic_id": "atomic_subject.freelance-demand-test",
                        "decision": "approved",
                        "binding_method": "reviewed_rules",
                        "confidence": 1.0,
                        "rationale": "The non-empty aggregate cohort matches the subject.",
                        "reviewer_type": "rules",
                        "reviewed_by": f"pytest-{index}",
                        "reviewed_at": f"2026-08-30T14:0{index + 1}:00Z",
                        "source_receipt_id": f"receipt:upwork-valid-{index}",
                        "review_receipt_id": f"receipt:binding-valid-{index}",
                        "audit": {"automatic_binding": False},
                    }
                )
            )
        selection = semantic.record_atomic_selection(
            {
                "graph_version_id": graph_version_id,
                "atomic_topic_id": "atomic_subject.freelance-demand-test",
                "binding_ids": [binding["binding_id"] for binding in bindings],
                "reviewer_type": "rules",
                "reviewer_id": "pytest",
                "reviewed_at": "2026-08-30T14:03:00Z",
                "review_receipt_id": "receipt:selection-zero-evidence",
                "rationale": "Use only the non-empty buyer-demand observation.",
            }
        )

        # Simulate a pre-fix link that already associated the selected signal
        # with a terminal failed scan.  The read gate must still fail closed.
        failed_snapshot_id = failed_scan["demand_snapshot_ids"][0]
        link_core = {
            "contract": UPWORK_SEMANTIC_LINK_CONTRACT,
            "demand_snapshot_id": failed_snapshot_id,
            "signal_id": signal_id,
            "graph_version_id": graph_version_id,
            "cohort_type": "query",
            "cohort_key": "ai automation",
            "created_at": "2026-08-30T14:04:00+00:00",
            "automatic_binding": 0,
        }
        link_core["link_sha256"] = stable_hash(link_core)
        with store.connect() as connection:
            failed_snapshot = connection.execute(
                """SELECT evidence_state FROM mt_upwork_demand_snapshots
                   WHERE demand_snapshot_id = ?""",
                (failed_snapshot_id,),
            ).fetchone()
            connection.execute(
                """INSERT INTO mt_upwork_semantic_links(
                       semantic_link_id, contract, demand_snapshot_id,
                       signal_id, graph_version_id, cohort_type, cohort_key,
                       created_at, automatic_binding, link_sha256
                   ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    "upwork-semantic-link:legacy-zero-evidence",
                    link_core["contract"],
                    link_core["demand_snapshot_id"],
                    link_core["signal_id"],
                    link_core["graph_version_id"],
                    link_core["cohort_type"],
                    link_core["cohort_key"],
                    link_core["created_at"],
                    link_core["automatic_binding"],
                    link_core["link_sha256"],
                ),
            )
        context = service.script_context(
            selection_id=selection["selection"]["selection_id"]
        )
        service.close()

    assert valid_scan["state"] == "complete"
    assert failed_scan["state"] == "failed"
    assert failed_scan["accepted_job_observations"] == 0
    assert failed_snapshot["evidence_state"] == "insufficient"
    assert materialized["created"] >= 1
    assert failed_snapshot_id not in {
        str(link["demand_snapshot_id"]) for link in materialized_links
    }
    assert context["generation_authorized"] is False
    assert context["cohorts"] == []
    assert "insufficient_demand_evidence" in context["blockers"]
