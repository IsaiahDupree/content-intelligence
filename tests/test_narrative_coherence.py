import json
import os
import tempfile
import threading
import unittest
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from services.content_quality.engine import QualityStore
from services.content_quality.narrative_coherence import (
    CONTEXT_BEAT_NAME,
    NarrativeCoherenceService,
    openai_llm_runner,
    repair,
    rules_audit,
)

EVIDENCE = {
    "viral_transcript_patterns": 5,
    "creator_count": 5,
    "observed_views_snapshot": 150000,
    "recurring_human_terms": ["feeling stuck", "exhaustion"],
}


class OpenAIProviderHandler(BaseHTTPRequestHandler):
    response_status = 200
    response_payload = {}
    received = []

    def do_POST(self):  # noqa: N802 - HTTP server interface
        length = int(self.headers.get("Content-Length", "0"))
        body = json.loads(self.rfile.read(length) or b"{}")
        self.__class__.received.append({
            "path": self.path,
            "body": body,
            "authorization_present": self.headers.get("Authorization", "").startswith("Bearer "),
        })
        encoded = json.dumps(self.__class__.response_payload).encode()
        self.send_response(self.__class__.response_status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)

    def log_message(self, _format, *_args):
        return


@contextmanager
def openai_provider(*, status: int, payload: dict):
    OpenAIProviderHandler.response_status = status
    OpenAIProviderHandler.response_payload = payload
    OpenAIProviderHandler.received = []
    server = ThreadingHTTPServer(("127.0.0.1", 0), OpenAIProviderHandler)
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
    names = ("OPENAI_API_KEY", "NARRATIVE_JUDGE_MODEL", "OPENAI_API_BASE_URL")
    previous = {name: os.environ.get(name) for name in names}
    os.environ.update({
        "OPENAI_API_KEY": "sk-test-contract",
        "NARRATIVE_JUDGE_MODEL": "gpt-5-nano",
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


def template_timeline():
    """The generator's historical shape — including its known coherence defect:
    the proof beat says 'these stories' but no earlier beat introduces stories."""
    return [
        {"start": 0.0, "end": 3.0, "beat": "human_hook", "text": "You feel burned out after another video fails."},
        {"start": 3.0, "end": 8.0, "beat": "stakes", "text": "It matters because time is being lost."},
        {"start": 8.0, "end": 15.0, "beat": "claim", "text": "The best content begins with a human problem."},
        {"start": 15.0, "end": 23.0, "beat": "proof", "text": "Across these stories, the same signs keep showing up: feeling stuck."},
        {"start": 23.0, "end": 31.0, "beat": "method", "text": "Choose the smallest pressure you can remove today."},
        {"start": 31.0, "end": 38.0, "beat": "payoff", "text": "The work starts to feel possible again."},
        {"start": 38.0, "end": 43.0, "beat": "cta", "text": "Which part feels heaviest right now?"},
    ]


class RulesAuditTests(unittest.TestCase):
    def test_template_defect_is_caught(self):
        defects = rules_audit(template_timeline(), EVIDENCE)
        codes = {item["code"] for item in defects}
        self.assertIn("DANGLING_REFERENT", codes)
        self.assertIn("EVIDENCE_NEVER_VOICED", codes)

    def test_coherent_timeline_passes(self):
        timeline = template_timeline()
        timeline.insert(2, {"start": 0.0, "end": 4.0, "beat": CONTEXT_BEAT_NAME,
                            "text": "Quick context: this comes from 5 creator videos — real stories from 5 creators."})
        cursor = 0.0
        for item in timeline:
            duration = item["end"] - item["start"]
            item["start"], item["end"] = cursor, cursor + duration
            cursor += duration
        self.assertEqual(rules_audit(timeline, EVIDENCE), [])

    def test_discontinuity_is_caught(self):
        timeline = template_timeline()
        timeline[3]["start"] = 17.0
        codes = {item["code"] for item in rules_audit(timeline, EVIDENCE)}
        self.assertIn("TIMELINE_DISCONTINUITY", codes)

    def test_claim_first_is_caught(self):
        timeline = [{"start": 0.0, "end": 5.0, "beat": "claim", "text": "Automation fixes everything."}]
        codes = {item["code"] for item in rules_audit(timeline, None)}
        self.assertIn("CLAIM_BEFORE_SETUP", codes)

    def test_cta_not_last_is_caught(self):
        timeline = [
            {"start": 0.0, "end": 3.0, "beat": "human_hook", "text": "You know the feeling."},
            {"start": 3.0, "end": 6.0, "beat": "cta", "text": "Follow for more."},
            {"start": 6.0, "end": 10.0, "beat": "payoff", "text": "It gets easier."},
        ]
        codes = {item["code"] for item in rules_audit(timeline, None)}
        self.assertIn("CTA_NOT_LAST", codes)

    def test_no_evidence_means_no_attribution_requirement(self):
        timeline = [
            {"start": 0.0, "end": 3.0, "beat": "human_hook", "text": "You know the feeling."},
            {"start": 3.0, "end": 8.0, "beat": "payoff", "text": "It gets easier."},
        ]
        self.assertEqual(rules_audit(timeline, None), [])

    def test_empty_timeline_fails_closed(self):
        self.assertEqual(rules_audit([], EVIDENCE)[0]["code"], "EMPTY_TIMELINE")


class RepairTests(unittest.TestCase):
    def script(self):
        return {"timeline": template_timeline(), "evidence_summary": EVIDENCE,
                "text": " ".join(item["text"] for item in template_timeline())}

    def test_repair_inserts_context_beat_and_resolves_defects(self):
        script = self.script()
        defects = rules_audit(script["timeline"], EVIDENCE)
        revised = repair(script, defects)
        beats = [item["beat"] for item in revised["timeline"]]
        self.assertIn(CONTEXT_BEAT_NAME, beats)
        self.assertLess(beats.index(CONTEXT_BEAT_NAME), beats.index("claim"))
        self.assertEqual(rules_audit(revised["timeline"], EVIDENCE), [])
        self.assertIn("stories", revised["timeline"][beats.index(CONTEXT_BEAT_NAME)]["text"])

    def test_repair_keeps_attention_gate_constraints(self):
        revised = repair(self.script(), rules_audit(template_timeline(), EVIDENCE))
        timeline = revised["timeline"]
        proof_start = next(item["start"] for item in timeline if item["beat"] == "proof")
        self.assertLessEqual(proof_start, 20.0)
        self.assertLessEqual(timeline[0]["end"], 3.5)
        total = timeline[-1]["end"]
        cta_start = next(item["start"] for item in timeline if item["beat"] == "cta")
        self.assertGreaterEqual(cta_start, total * 0.66)
        self.assertLessEqual(max(item["end"] - item["start"] for item in timeline), 10.0)

    def test_repair_retimes_contiguously(self):
        revised = repair(self.script(), rules_audit(template_timeline(), EVIDENCE))
        cursor = 0.0
        for item in revised["timeline"]:
            self.assertAlmostEqual(item["start"], cursor, places=2)
            self.assertGreater(item["end"], item["start"])
            cursor = item["end"]

    def test_repair_rebuilds_full_text(self):
        revised = repair(self.script(), rules_audit(template_timeline(), EVIDENCE))
        self.assertEqual(revised["text"], " ".join(item["text"] for item in revised["timeline"]))


class EnforceLoopTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.store = QualityStore(Path(self.tempdir.name) / "quality.sqlite3")

    def tearDown(self):
        self.tempdir.cleanup()

    def script(self):
        return {"timeline": template_timeline(), "evidence_summary": EVIDENCE,
                "text": " ".join(item["text"] for item in template_timeline())}

    def test_loop_repairs_template_defect_and_passes(self):
        service = NarrativeCoherenceService(self.store, llm_runner=None)
        final, outcome = service.enforce(self.script())
        self.assertEqual(outcome["decision"], "PASS")
        self.assertGreater(len(outcome["attempts"]), 1)
        self.assertIn(CONTEXT_BEAT_NAME, [item["beat"] for item in final["timeline"]])

    def test_unrepairable_defect_fails_closed_after_max_attempts(self):
        service = NarrativeCoherenceService(self.store, llm_runner=None)
        script = {"timeline": [{"start": 0.0, "end": 5.0, "beat": "claim", "text": "Trust these results."}],
                  "evidence_summary": None, "text": "Trust these results."}
        _, outcome = service.enforce(script)
        self.assertEqual(outcome["decision"], "FAIL_RULES")
        self.assertTrue(outcome["defects_open"])

    def test_llm_incoherent_verdict_rejects(self):
        service = NarrativeCoherenceService(
            self.store, llm_runner=lambda prompt: json.dumps({"coherent": False, "issues": ["claim floats unsupported"]})
        )
        _, outcome = service.enforce(self.script())
        self.assertEqual(outcome["decision"], "FAIL_JUDGMENT")
        self.assertEqual(outcome["llm_judgment"]["issues"], ["claim floats unsupported"])

    def test_llm_coherent_verdict_passes(self):
        service = NarrativeCoherenceService(
            self.store, llm_runner=lambda prompt: 'Sure! {"coherent": true, "issues": []}'
        )
        _, outcome = service.enforce(self.script())
        self.assertEqual(outcome["decision"], "PASS")
        self.assertEqual(outcome["llm_judgment"]["status"], "ok")

    def test_unavailable_judge_fails_closed(self):
        def broken(prompt):
            raise RuntimeError("cli exploded")
        service = NarrativeCoherenceService(self.store, llm_runner=broken)
        _, outcome = service.enforce(self.script())
        self.assertEqual(outcome["decision"], "JUDGE_UNAVAILABLE")

    def test_garbled_judge_output_fails_closed(self):
        service = NarrativeCoherenceService(self.store, llm_runner=lambda prompt: "looks fine to me!")
        _, outcome = service.enforce(self.script())
        self.assertEqual(outcome["decision"], "JUDGE_UNAVAILABLE")

    def test_truthy_but_not_boolean_verdict_is_not_a_pass(self):
        service = NarrativeCoherenceService(
            self.store, llm_runner=lambda prompt: json.dumps({"coherent": "yes", "issues": []})
        )
        _, outcome = service.enforce(self.script())
        self.assertEqual(outcome["decision"], "JUDGE_UNAVAILABLE")

    def test_one_shot_audit_persists_receipt(self):
        service = NarrativeCoherenceService(self.store, llm_runner=None)
        record = service.audit({"timeline": template_timeline(), "evidence_summary": EVIDENCE,
                                "script_id": "script-x"})
        self.assertEqual(record["decision"], "FAIL_RULES")
        self.assertEqual(record["subject_id"], "script-x")
        latest = self.store.script_gate_summary("script-x")["latest_audits"]
        self.assertIn("narrative_coherence", latest)


class OpenAIRunnerContractTests(unittest.TestCase):
    def test_gpt5_request_uses_supported_completion_budget(self):
        with openai_provider(status=200, payload={
            "choices": [{"message": {"content": '{"coherent": true, "issues": []}'}}]
        }) as base_url, provider_environment(base_url):
            result = openai_llm_runner("judge this")
        received = OpenAIProviderHandler.received[0]
        body = received["body"]

        self.assertIn('"coherent": true', result)
        self.assertEqual(received["path"], "/v1/chat/completions")
        self.assertTrue(received["authorization_present"])
        self.assertEqual(body["max_completion_tokens"], 300)
        self.assertEqual(body["reasoning_effort"], "minimal")
        self.assertEqual(body["response_format"]["type"], "json_schema")
        self.assertTrue(body["response_format"]["json_schema"]["strict"])
        self.assertNotIn("max_tokens", body)
        self.assertNotIn("temperature", body)

    def test_api_error_classification_does_not_persist_response_message(self):
        provider_message = "Incorrect API key provided with sensitive suffix"
        with openai_provider(status=401, payload={
            "error": {
                "type": "invalid_request_error",
                "code": "invalid_api_key",
                "param": None,
                "message": provider_message,
            }
        }) as base_url, provider_environment(base_url):
            with self.assertRaises(RuntimeError) as raised:
                openai_llm_runner("judge this")

        detail = str(raised.exception)
        self.assertIn("code=invalid_api_key", detail)
        self.assertNotIn(provider_message, detail)


if __name__ == "__main__":
    unittest.main()
