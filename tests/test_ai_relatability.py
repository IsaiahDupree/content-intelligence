"""Integration tests for the standalone qualitative relatability verdict."""
from __future__ import annotations

import json
import os
import sqlite3
import threading
from contextlib import closing, contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from services.content_quality.ai_relatability import (
    AUDIT_TYPE,
    NON_AI_PASS_DECISION,
    VERDICT_NAME,
    AIRelatabilityAdjudicator,
    deterministic_assessment,
    openai_relatability_runner,
)
from services.content_quality.contracts import CURRENT_TRANSCRIPT_AUDIT_CONTRACT
from services.content_quality.engine import QualityStore


SCRIPT = {
    "script_id": "script-relatable-1",
    "audience": "software founders",
    "text": (
        "You feel stuck after another launch. Customers ignored the demo and "
        "the pressure feels hard. Founders worry they waste time trying new "
        "automation. Name the problem, show what changed, and make the next "
        "step easier."
    ),
}

AI_PASS = {
    "relatable": True,
    "score": 84,
    "rubric_scores": {
        "concrete_lived_moment": 22,
        "clear_personal_stakes": 17,
        "visible_input_action_output": 16,
        "source_language_support": 13,
        "direct_audience_perspective": 8,
        "non_alienating_framing": 8,
    },
    "audience_moment": "A founder watches another launch get ignored.",
    "why_it_feels_human": ["It names the pressure and wasted time."],
    "alienating_language": [],
    "source_language_used": ["feel", "stuck", "pressure", "trying"],
    "rewrite_guidance": [],
}


def add_transcript_receipts(
    store: QualityStore,
    *,
    views_by_index: dict[int, object] | None = None,
) -> list[str]:
    keywords = [
        "feel", "stuck", "another", "launch", "customers", "ignored",
        "demo", "pressure", "feels", "hard", "founders", "worry", "waste",
        "time", "trying", "automation", "name", "problem", "show",
        "changed", "make", "next", "step", "easier",
    ]
    receipt_ids: list[str] = []
    for index in range(5):
        receipt = store.put_receipt(
            "viral_transcript_pattern",
            "youtube",
            f"video-{index}",
            f"https://www.youtube.com/watch?v=video-{index}",
            {
                "video_id": f"youtube:video:video-{index}",
                "creator_id": f"creator-{index % 3}",
                "transcript_source": "local_whisper",
                "transcript_id": f"whisper-{index}",
                "observation_key": f"observation-{index}",
                "audio_sha256": f"{index:x}" * 64,
                "transcript_sha256": f"{index + 5:x}" * 64,
                "performance_qualification": {
                    "audit_contract": CURRENT_TRANSCRIPT_AUDIT_CONTRACT,
                    "audit_decision": "PASS",
                },
                "transcript_keywords": keywords,
                "pattern": {"source_metrics": {
                    "views": (views_by_index or {}).get(index, 25_000),
                }},
            },
        )
        receipt_ids.append(receipt["receipt_id"])
    return receipt_ids


def script_with_receipts(
    store: QualityStore,
    *,
    views_by_index: dict[int, object] | None = None,
) -> dict:
    moment = {
        "moment_id": "moment-source-0",
        "situation": "A founder watches another launch get ignored.",
        "audience": SCRIPT["audience"],
        "stakes": "The pressure and wasted time keep growing.",
        "source_transcript_id": "whisper-0",
        "source_observation_key": "observation-0",
        "stakes_source_moment_id": "moment-source-0",
        "stakes_source_transcript_id": "whisper-0",
        "stakes_source_observation_key": "observation-0",
    }
    moment_receipt = store.put_receipt(
        "audience_human_moments",
        "market_tape",
        "audience-test",
        None,
        {"moments": [moment]},
    )
    script = {
        **SCRIPT,
        "topic": "AI automation",
        "objective": "qualified_attention",
        "brief_id": None,
        "status": "generated_pending_gates",
        "created_at": "2026-08-24T00:00:00+00:00",
        "source_receipt_ids": add_transcript_receipts(
            store, views_by_index=views_by_index
        ),
        "human_moment": {
            **moment,
            "source_moment_receipt_id": moment_receipt["receipt_id"],
        },
        "timeline": [{"text": SCRIPT["text"]}],
    }
    store.put_script(script)
    return script


class OpenAIContractHandler(BaseHTTPRequestHandler):
    response_status = 200
    response_payload: dict = {}
    received: list[dict] = []

    def do_POST(self):  # noqa: N802 - stdlib server contract
        length = int(self.headers.get("Content-Length", "0"))
        body = json.loads(self.rfile.read(length) or b"{}")
        self.__class__.received.append({
            "path": self.path,
            "body": body,
            "authorization_present": self.headers.get(
                "Authorization", ""
            ).startswith("Bearer "),
        })
        encoded = json.dumps(self.__class__.response_payload).encode("utf-8")
        self.send_response(self.__class__.response_status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)

    def log_message(self, _format, *_args):
        return


@contextmanager
def openai_contract_server(*, status: int, payload: dict):
    OpenAIContractHandler.response_status = status
    OpenAIContractHandler.response_payload = payload
    OpenAIContractHandler.received = []
    server = ThreadingHTTPServer(("127.0.0.1", 0), OpenAIContractHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}/v1"
    finally:
        server.shutdown()
        thread.join(timeout=5)
        server.server_close()


@contextmanager
def provider_environment(base_url: str):
    names = (
        "OPENAI_API_KEY", "RELATABILITY_JUDGE_MODEL", "OPENAI_API_BASE_URL",
    )
    previous = {name: os.environ.get(name) for name in names}
    os.environ.update({
        "OPENAI_API_KEY": "integration-contract-token",
        "RELATABILITY_JUDGE_MODEL": "gpt-5-nano",
        "OPENAI_API_BASE_URL": base_url,
    })
    try:
        yield
    finally:
        for name, value in previous.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value


def test_non_ai_pass_is_explicit_and_persisted(tmp_path: Path):
    store = QualityStore(tmp_path / "quality.sqlite3")
    script_with_receipts(store)
    result = AIRelatabilityAdjudicator(store).audit({
        "script_id": SCRIPT["script_id"],
    })

    assert result["decision"] == NON_AI_PASS_DECISION
    assert result["audit_type"] == AUDIT_TYPE
    assert result["qualitative_verdict"]["name"] == VERDICT_NAME
    assert result["qualitative_verdict"]["evaluation_mode"] == "deterministic_non_ai"
    assert result["qualitative_verdict"]["ai_evaluated"] is False
    assert result["findings"]["qualitative_verdict"] == result["qualitative_verdict"]
    assert result["findings"]["input_binding"]["stored_script_bound"] is True
    assert len(result["findings"]["input_binding"]["text_sha256"]) == 64
    latest = store.script_gate_summary(SCRIPT["script_id"])["latest_audits"]
    assert latest[AUDIT_TYPE]["audit_id"] == result["audit_id"]


def test_time_and_work_language_count_as_human_experience(tmp_path: Path):
    store = QualityStore(tmp_path / "quality.sqlite3")
    script = script_with_receipts(store)
    receipts = store.receipts(script["source_receipt_ids"])

    result = deterministic_assessment(
        text=(
            "You lose time when another job becomes a problem. "
            "Name the pressure and make the next step easier."
        ),
        audience=script["audience"],
        receipts=receipts,
        source_human_moment=script["human_moment"],
        source_moment_lineage_verified=True,
        source_moment_lineage_basis="test",
    )

    assert result["checks"]["human_experience_in_opening"] is True
    assert {"time", "problem"}.issubset(
        result["evidence"]["opening_human_terms"]
    )


@pytest.mark.parametrize(
    ("field", "replacement", "message"),
    (
        ("text", "Unrelated caller-controlled text.", "text"),
        ("brief_id", "brief-attacker", "brief_id"),
        ("source_receipt_ids", ["receipt-attacker"], "source_receipt_ids"),
    ),
)
def test_stored_script_id_rejects_unbound_caller_evidence(
    tmp_path: Path,
    field: str,
    replacement: object,
    message: str,
):
    store = QualityStore(tmp_path / "quality.sqlite3")
    script_with_receipts(store)

    with pytest.raises(ValueError, match=message):
        AIRelatabilityAdjudicator(store).audit({
            "script_id": SCRIPT["script_id"],
            field: replacement,
        })

    with closing(sqlite3.connect(store.path)) as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM cq_audits WHERE audit_type=?",
            (AUDIT_TYPE,),
        ).fetchone()[0] == 0


@pytest.mark.parametrize("field", ("audience", "stakes"))
def test_source_moment_requires_exact_audience_and_stakes_lineage(
    tmp_path: Path,
    field: str,
):
    store = QualityStore(tmp_path / "quality.sqlite3")
    payload = script_with_receipts(store)
    payload = {key: value for key, value in payload.items() if key != "script_id"}
    payload["human_moment"] = {
        **payload["human_moment"],
        field: "tampered value",
    }

    result = AIRelatabilityAdjudicator(
        store, lambda _prompt: json.dumps(AI_PASS)
    ).audit(payload)

    assert result["decision"] == "REJECT_NOT_RELATABLE"
    deterministic = result["findings"]["deterministic_assessment"]
    assert deterministic["checks"]["source_human_moment_bound"] is False
    assert deterministic["evidence"]["source_human_moment"][
        "lineage_verified"
    ] is False


def test_injected_ai_runner_returns_separately_named_verdict(tmp_path: Path):
    store = QualityStore(tmp_path / "quality.sqlite3")
    prompts: list[str] = []

    def runner(prompt: str) -> str:
        prompts.append(prompt)
        return json.dumps(AI_PASS)

    result = AIRelatabilityAdjudicator(store, runner).audit(
        script_with_receipts(store)
    )

    verdict = result["qualitative_verdict"]
    assert result["decision"] == "PASS"
    assert verdict["evaluation_mode"] == "ai"
    assert verdict["ai_evaluated"] is True
    assert verdict["judgment"]["audience_moment"].startswith("A founder")
    assert "software founders" in prompts[0]
    assert '"accepted_transcript_count": 5' in prompts[0]
    assert '"source_support"' in prompts[0]
    assert '"cross_creator_term_support"' in prompts[0]
    assert '"lineage_verified": true' in prompts[0]
    assert "0 through 100 scale" in prompts[0]
    assert "at least 70" in prompts[0]
    assert "25 points for a concrete lived moment" in prompts[0]
    assert "do not make source-backed language unrelatable" in prompts[0]
    assert "Start non-alienating framing at 10" in prompts[0]
    assert "Second-person language such as you or your" in prompts[0]


def test_ai_rejection_is_not_overridden_by_deterministic_pass(tmp_path: Path):
    store = QualityStore(tmp_path / "quality.sqlite3")
    rejected = {
        **AI_PASS,
        "relatable": False,
        "score": 42,
        "rubric_scores": {
            "concrete_lived_moment": 10,
            "clear_personal_stakes": 8,
            "visible_input_action_output": 8,
            "source_language_support": 7,
            "direct_audience_perspective": 4,
            "non_alienating_framing": 5,
        },
        "alienating_language": ["Generic advice"],
        "rewrite_guidance": ["Name the exact customer reaction."],
    }
    result = AIRelatabilityAdjudicator(
        store, lambda _prompt: json.dumps(rejected)
    ).audit(script_with_receipts(store))

    assert result["decision"] == "REJECT_NOT_RELATABLE"
    assert result["score"] == 42.0
    assert result["qualitative_verdict"]["ai_evaluated"] is True


def test_rejection_normalizes_same_side_rubric_total_and_drops_unsupported_terms(
    tmp_path: Path,
):
    """GPT structured output can satisfy JSON Schema but miss local arithmetic."""
    store = QualityStore(tmp_path / "quality.sqlite3")
    live_shaped_rejection = {
        **AI_PASS,
        "relatable": False,
        "score": 62,
        "rubric_scores": {
            "concrete_lived_moment": 25,
            "clear_personal_stakes": 0,
            "visible_input_action_output": 0,
            "source_language_support": 15,
            "direct_audience_perspective": 10,
            "non_alienating_framing": 0,
        },
        "source_language_used": ["feel", "invented-phrase"],
        "alienating_language": ["The stakes are not concrete."],
        "rewrite_guidance": ["Name the specific stakes and visible output."],
    }

    result = AIRelatabilityAdjudicator(
        store, lambda _prompt: json.dumps(live_shaped_rejection)
    ).audit(script_with_receipts(store))

    verdict = result["qualitative_verdict"]
    judgment = verdict["judgment"]
    assert result["decision"] == "REJECT_NOT_RELATABLE"
    assert result["score"] == 50.0
    assert verdict["ai_evaluated"] is True
    assert verdict["judge_attempt_count"] == 1
    assert judgment["source_language_used"] == ["feel"]
    assert judgment["semantic_normalizations"] == [
        {
            "code": "score_derived_from_rubric",
            "provider_reported_score": 62,
            "normalized_score": 50,
        },
        {
            "code": "unsupported_source_terms_removed",
            "removed_count": 1,
        },
    ]


def test_unexplained_non_alienating_deduction_retries_fail_closed(
    tmp_path: Path,
):
    store = QualityStore(tmp_path / "quality.sqlite3")
    inconsistent = {
        **AI_PASS,
        "relatable": False,
        "score": 65,
        "rubric_scores": {
            "concrete_lived_moment": 25,
            "clear_personal_stakes": 15,
            "visible_input_action_output": 10,
            "source_language_support": 10,
            "direct_audience_perspective": 5,
            "non_alienating_framing": 0,
        },
        "alienating_language": ["none identified"],
        "rewrite_guidance": ["Clarify the personal stake."],
    }
    result = AIRelatabilityAdjudicator(
        store, lambda _prompt: json.dumps(inconsistent)
    ).audit(script_with_receipts(store))

    verdict = result["qualitative_verdict"]
    assert verdict["decision"] == "JUDGE_UNAVAILABLE"
    assert verdict["judge_attempt_count"] == 3
    assert verdict["judge_unavailable_reason"] == "invalid_response_contract"


def test_non_alienating_default_normalizes_only_on_same_decision_side(
    tmp_path: Path,
):
    store = QualityStore(tmp_path / "quality.sqlite3")
    same_side_rejection = {
        **AI_PASS,
        "relatable": False,
        "score": 55,
        "rubric_scores": {
            "concrete_lived_moment": 20,
            "clear_personal_stakes": 10,
            "visible_input_action_output": 10,
            "source_language_support": 10,
            "direct_audience_perspective": 5,
            "non_alienating_framing": 0,
        },
        "alienating_language": [],
        "rewrite_guidance": ["Make the personal stakes more concrete."],
    }
    result = AIRelatabilityAdjudicator(
        store, lambda _prompt: json.dumps(same_side_rejection)
    ).audit(script_with_receipts(store))

    verdict = result["qualitative_verdict"]
    assert verdict["decision"] == "REJECT_NOT_RELATABLE"
    assert verdict["score"] == 65.0
    assert verdict["judgment"]["rubric_scores"][
        "non_alienating_framing"
    ] == 10
    assert verdict["judgment"]["semantic_normalizations"][-1] == {
        "code": "non_alienating_default_without_identified_language",
        "normalized_score": 65,
    }
    assert verdict["judge_attempts"][0]["validation"] == "accepted"
    assert len(verdict["judge_attempts"][0]["response_sha256"]) == 64


def test_all_zero_model_responses_persist_content_free_failure_codes(
    tmp_path: Path,
):
    store = QualityStore(tmp_path / "quality.sqlite3")
    empty = {
        "relatable": False,
        "score": 0,
        "rubric_scores": {
            "concrete_lived_moment": 0,
            "clear_personal_stakes": 0,
            "visible_input_action_output": 0,
            "source_language_support": 0,
            "direct_audience_perspective": 0,
            "non_alienating_framing": 0,
        },
        "audience_moment": "",
        "why_it_feels_human": [],
        "alienating_language": [],
        "source_language_used": [],
        "rewrite_guidance": [],
    }
    result = AIRelatabilityAdjudicator(
        store, lambda _prompt: json.dumps(empty)
    ).audit(script_with_receipts(store))

    verdict = result["qualitative_verdict"]
    assert verdict["decision"] == "JUDGE_UNAVAILABLE"
    assert verdict["judge_attempt_count"] == 3
    assert len(verdict["judge_attempts"]) == 3
    assert all(
        "all_zero_empty_verdict" in attempt["failure_codes"]
        for attempt in verdict["judge_attempts"]
    )
    persisted = json.dumps(result["findings"], sort_keys=True)
    assert '"audience_moment": ""' not in persisted


def test_score_normalization_never_crosses_the_pass_threshold(tmp_path: Path):
    store = QualityStore(tmp_path / "quality.sqlite3")
    inconsistent = {
        **AI_PASS,
        "relatable": False,
        "score": 62,
        "rubric_scores": {
            "concrete_lived_moment": 20,
            "clear_personal_stakes": 15,
            "visible_input_action_output": 15,
            "source_language_support": 15,
            "direct_audience_perspective": 5,
            "non_alienating_framing": 5,
        },
        "alienating_language": ["The framing is generic."],
        "rewrite_guidance": ["Make the moment more concrete."],
    }

    result = AIRelatabilityAdjudicator(
        store, lambda _prompt: json.dumps(inconsistent)
    ).audit(script_with_receipts(store))

    assert result["decision"] == "JUDGE_UNAVAILABLE"
    assert result["qualitative_verdict"]["ai_evaluated"] is False
    assert result["qualitative_verdict"]["judge_attempt_count"] == 3


def test_ai_pass_still_requires_at_least_one_supported_source_term(
    tmp_path: Path,
):
    store = QualityStore(tmp_path / "quality.sqlite3")
    unsupported = {
        **AI_PASS,
        "source_language_used": ["invented-phrase"],
    }

    result = AIRelatabilityAdjudicator(
        store, lambda _prompt: json.dumps(unsupported)
    ).audit(script_with_receipts(store))

    assert result["decision"] == "JUDGE_UNAVAILABLE"
    assert result["qualitative_verdict"]["ai_evaluated"] is False
    assert result["qualitative_verdict"]["judge_attempt_count"] == 3


def test_configured_judge_failure_is_fail_closed_and_secret_safe(tmp_path: Path):
    store = QualityStore(tmp_path / "quality.sqlite3")

    def unavailable(_prompt: str) -> str:
        raise RuntimeError("provider echoed secret-value-that-must-not-persist")

    result = AIRelatabilityAdjudicator(store, unavailable).audit(
        script_with_receipts(store)
    )

    assert result["decision"] == "JUDGE_UNAVAILABLE"
    assert result["qualitative_verdict"]["ai_evaluated"] is False
    assert result["qualitative_verdict"]["judge_unavailable_reason"] == "RuntimeError"
    with closing(sqlite3.connect(store.path)) as connection:
        persisted = connection.execute(
            "SELECT findings_json FROM cq_audits WHERE audit_id=?",
            (result["audit_id"],),
        ).fetchone()[0]
    assert "secret-value-that-must-not-persist" not in persisted


def test_latest_identical_failure_supersedes_an_intervening_pass(tmp_path: Path):
    store = QualityStore(tmp_path / "quality.sqlite3")
    payload = script_with_receipts(store)

    def unavailable(_prompt: str) -> str:
        raise RuntimeError("provider unavailable")

    first = AIRelatabilityAdjudicator(store, unavailable).audit(payload)
    passed = AIRelatabilityAdjudicator(
        store, lambda _prompt: json.dumps(AI_PASS)
    ).audit(payload)
    latest = AIRelatabilityAdjudicator(store, unavailable).audit(payload)

    assert [first["decision"], passed["decision"], latest["decision"]] == [
        "JUDGE_UNAVAILABLE", "PASS", "JUDGE_UNAVAILABLE",
    ]
    assert len({first["audit_id"], passed["audit_id"], latest["audit_id"]}) == 3
    gate = store.script_gate_summary(SCRIPT["script_id"])
    assert gate["latest_audits"][AUDIT_TYPE]["audit_id"] == latest["audit_id"]
    assert gate["latest_audits"][AUDIT_TYPE]["decision"] == "JUDGE_UNAVAILABLE"


def test_invalid_ai_contract_is_fail_closed(tmp_path: Path):
    store = QualityStore(tmp_path / "quality.sqlite3")
    result = AIRelatabilityAdjudicator(
        store, lambda _prompt: json.dumps({"relatable": True, "score": 99})
    ).audit(script_with_receipts(store))

    assert result["decision"] == "JUDGE_UNAVAILABLE"
    assert (
        result["qualitative_verdict"]["judge_unavailable_reason"]
        == "invalid_response_contract"
    )


def test_ai_pass_must_cite_supported_source_terms_and_a_human_moment(
    tmp_path: Path,
):
    store = QualityStore(tmp_path / "quality.sqlite3")
    unsupported = {
        **AI_PASS,
        "audience_moment": "",
        "source_language_used": ["invented-phrase"],
    }

    result = AIRelatabilityAdjudicator(
        store, lambda _prompt: json.dumps(unsupported)
    ).audit(script_with_receipts(store))

    assert result["decision"] == "JUDGE_UNAVAILABLE"
    assert (
        result["qualitative_verdict"]["judge_unavailable_reason"]
        == "invalid_response_contract"
    )


def test_malformed_source_metrics_fail_closed_without_crashing(tmp_path: Path):
    store = QualityStore(tmp_path / "quality.sqlite3")
    payload = script_with_receipts(
        store, views_by_index={0: "not-a-number", 1: "not-a-number"}
    )

    result = AIRelatabilityAdjudicator(
        store, lambda _prompt: json.dumps(AI_PASS)
    ).audit(payload)

    assert result["decision"] == "REJECT_NOT_RELATABLE"
    assert result["findings"]["deterministic_assessment"]["evidence"][
        "observed_views_snapshot"
    ] == 75_000


def test_insufficient_evidence_rejects_before_ai_call(tmp_path: Path):
    store = QualityStore(tmp_path / "quality.sqlite3")
    called = False

    def runner(_prompt: str) -> str:
        nonlocal called
        called = True
        return json.dumps(AI_PASS)

    payload = {
        key: value for key, value in SCRIPT.items() if key != "script_id"
    }
    payload["source_receipt_ids"] = add_transcript_receipts(store)[:2]
    result = AIRelatabilityAdjudicator(store, runner).audit(payload)

    assert result["decision"] == "REJECT_NOT_RELATABLE"
    assert result["qualitative_verdict"]["evaluation_mode"] == "deterministic_rejection"
    assert result["qualitative_verdict"]["ai_evaluated"] is False
    assert called is False


def test_gpt5_runner_uses_strict_supported_request_contract():
    response = {
        "id": "resp_rel_contract",
        "status": "completed",
        "output": [{
            "type": "message",
            "role": "assistant",
            "status": "completed",
            "content": [{
                "type": "output_text",
                "text": json.dumps(AI_PASS),
            }],
        }],
    }
    with openai_contract_server(status=200, payload=response) as base_url:
        with provider_environment(base_url):
            returned = openai_relatability_runner("judge this")

    assert json.loads(returned) == AI_PASS
    received = OpenAIContractHandler.received[0]
    body = received["body"]
    assert received["path"] == "/v1/responses"
    assert received["authorization_present"] is True
    assert body["model"] == "gpt-5-nano"
    assert body["max_output_tokens"] == 2400
    assert body["reasoning"] == {"effort": "minimal"}
    assert body["input"][0]["role"] == "developer"
    assert "untrusted quoted data" in body["input"][0]["content"]
    assert body["store"] is False
    assert body["text"]["format"]["type"] == "json_schema"
    schema = body["text"]["format"]
    assert schema["name"] == VERDICT_NAME
    assert schema["strict"] is True
    assert schema["schema"]["additionalProperties"] is False
    assert "max_completion_tokens" not in body
    assert "temperature" not in body


def test_provider_error_classification_omits_response_message():
    provider_message = "Incorrect credential with sensitive suffix"
    response = {
        "error": {
            "type": "invalid_request_error",
            "code": "invalid_api_key",
            "param": None,
            "message": provider_message,
        }
    }
    with openai_contract_server(status=401, payload=response) as base_url:
        with provider_environment(base_url):
            try:
                openai_relatability_runner("judge this")
            except RuntimeError as exc:
                detail = str(exc)
            else:  # pragma: no cover - contract failure would make this explicit
                raise AssertionError("expected provider error")

    assert "code=invalid_api_key" in detail
    assert provider_message not in detail
