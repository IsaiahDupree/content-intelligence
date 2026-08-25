"""Deterministic marketing script authoring."""

from __future__ import annotations

import re
from typing import Any

from .reference_corpus import (
    SOURCE_RIGHTS_STATE,
    ReferenceCorpusService,
    canonical_sha256,
    stable_id,
    utc_now,
    words,
)
from .script_quality import (
    ANGLE_STRUCTURE,
    MAX_QUALITY_REWRITE_ATTEMPTS,
    arrange_role_components,
    audit_owner_calibrated_quality,
    build_delivery_visual_plan,
    owner_repair_actions,
    repair_timeline_for_owner_quality,
    retime_timeline,
    select_rhetorical_structure,
)


REQUEST_CONTRACT = "reference_marketing_script_request_v1"
PACKAGE_CONTRACT = "reference_marketing_script_package_v1"
COMPILER_VERSION = "reference_marketing_script_compiler_v2"
SPOKEN_WORDS_PER_SECOND = 2.35

OBJECTIVES = ("awareness", "educate", "engage", "convert")
ANGLES = ("contrast", "problem_first", "how_to", "myth")
PROOF_TYPES = (
    "worked_example",
    "experience",
    "owned_measurement",
    "sourced_fact",
    "hypothesis",
)
SOURCE_REQUIRED_PROOF_TYPES = ("sourced_fact",)
OWNED_REQUIRED_PROOF_TYPES = ("experience", "owned_measurement")
FIRST_PERSON_WORDS = {
    "i", "i'm", "i've", "me", "my", "mine", "we", "we've", "our", "ours",
}

JARGON_WORDS = (
    "infrastructure", "orchestration", "configuration", "pipeline",
)
SENTENCE_RE = re.compile(r"[^.!?]+[.!?]?")
LEADING_STEP_RE = re.compile(
    r"^(?:step\s+\d+\s*[:,.-]?|first|second|third|fourth|next|then|finally)\s*[:,.-]?\s*",
    re.IGNORECASE,
)
STEP_OPENERS = {
    "contrast_reveal": ("", "Now: ", "One more check: ", "Last: "),
    "stakes_then_method": ("Start here: ", "", "Also: ", "Finish here: "),
    "proof_bridge": ("Try this: ", "", "Now: ", "Close with: "),
    "myth_turn": ("Check this: ", "", "Also: ", "Last: "),
}


def _text(value: Any, field: str, *, maximum: int = 1200) -> str:
    clean = " ".join(str(value or "").split()).strip()
    if not clean:
        raise ValueError(f"{field} is required")
    if len(clean) > maximum:
        raise ValueError(f"{field} must be at most {maximum} characters")
    return clean


def _finish_sentence(value: str) -> str:
    return value if value[-1:] in ".!?" else value + "."


def _normalize_request(payload: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise ValueError("script request must be an object")
    supplied_contract = str(payload.get("contract") or REQUEST_CONTRACT).strip()
    if supplied_contract != REQUEST_CONTRACT:
        raise ValueError(f"contract must be {REQUEST_CONTRACT}")

    objective = str(payload.get("objective") or "educate").strip().lower()
    if objective not in OBJECTIVES:
        raise ValueError("objective must be awareness, educate, engage, or convert")
    angle = str(payload.get("angle") or "problem_first").strip().lower()
    if angle not in ANGLES:
        raise ValueError("angle must be contrast, problem_first, how_to, or myth")
    target_seconds = int(payload.get("target_seconds") or 60)
    if target_seconds < 15 or target_seconds > 1800:
        raise ValueError("target_seconds must be between 15 and 1800")

    narrative = payload.get("narrative")
    if not isinstance(narrative, dict):
        raise ValueError("narrative must be an object")
    raw_steps = narrative.get("steps")
    if not isinstance(raw_steps, list) or not 2 <= len(raw_steps) <= 8:
        raise ValueError("narrative.steps must contain between 2 and 8 steps")
    steps = [
        _text(value, f"narrative.steps[{index}]", maximum=500)
        for index, value in enumerate(raw_steps)
    ]

    raw_proof = narrative.get("proof")
    if not isinstance(raw_proof, dict):
        raise ValueError("narrative.proof must be an object")
    proof_type = str(raw_proof.get("evidence_type") or "").strip().lower()
    if proof_type not in PROOF_TYPES:
        raise ValueError(
            "narrative.proof.evidence_type must be worked_example, experience, "
            "owned_measurement, sourced_fact, or hypothesis"
        )
    source_receipt_ids = sorted({
        str(value).strip()
        for value in (raw_proof.get("source_receipt_ids") or [])
        if str(value).strip()
    })
    proof_statement = _text(
        raw_proof.get("statement"),
        "narrative.proof.statement",
        maximum=1200,
    )
    first_person_claim = bool(
        {token.casefold().replace("’", "'") for token in words(proof_statement)}
        & FIRST_PERSON_WORDS
    )
    owned_binding_required = (
        proof_type in OWNED_REQUIRED_PROOF_TYPES or first_person_claim
    )
    if proof_type in SOURCE_REQUIRED_PROOF_TYPES and not source_receipt_ids:
        raise ValueError(
            f"{proof_type} proof requires at least one source_receipt_id"
        )
    if owned_binding_required and not source_receipt_ids:
        raise ValueError(
            "experience, owned measurement, and first-person proof require "
            "stored owned evidence"
        )

    raw_cta = narrative.get("cta")
    if not isinstance(raw_cta, dict):
        raise ValueError("narrative.cta must be an object")
    cta = {
        "text": _text(raw_cta.get("text"), "narrative.cta.text", maximum=400),
        "action": _text(raw_cta.get("action"), "narrative.cta.action", maximum=80),
        "destination": str(raw_cta.get("destination") or "").strip()[:500],
    }

    raw_offer = payload.get("offer") or {}
    if not isinstance(raw_offer, dict):
        raise ValueError("offer must be an object")
    offer = {
        "offer_id": str(raw_offer.get("offer_id") or "").strip()[:160],
        "name": str(raw_offer.get("name") or "").strip()[:240],
    }

    return {
        "contract": REQUEST_CONTRACT,
        "corpus_id": _text(payload.get("corpus_id"), "corpus_id", maximum=240),
        "title": _text(payload.get("title"), "title", maximum=240),
        "topic": _text(payload.get("topic"), "topic", maximum=500),
        "audience": _text(payload.get("audience"), "audience", maximum=500),
        "objective": objective,
        "angle": angle,
        "target_seconds": target_seconds,
        "narrative": {
            "hook": _text(narrative.get("hook"), "narrative.hook", maximum=400),
            "problem": _text(
                narrative.get("problem"), "narrative.problem", maximum=700
            ),
            "stakes": _text(
                narrative.get("stakes"), "narrative.stakes", maximum=700
            ),
            "reframe": _text(
                narrative.get("reframe"), "narrative.reframe", maximum=700
            ),
            "steps": steps,
            "proof": {
                "statement": proof_statement,
                "evidence_type": proof_type,
                "source_receipt_ids": source_receipt_ids,
            },
            "takeaway": _text(
                narrative.get("takeaway"), "narrative.takeaway", maximum=700
            ),
            "cta": cta,
        },
        "offer": offer,
    }


def _step_text(value: str, index: int, structure_id: str) -> str:
    clean = LEADING_STEP_RE.sub("", value).strip()
    openers = STEP_OPENERS.get(structure_id, ())
    opener = openers[index] if index < len(openers) else ""
    line = f"{opener}{clean}" if opener else clean[:1].upper() + clean[1:]
    return _finish_sentence(line)


def _build_beats(
    request: dict[str, Any], structure: dict[str, Any]
) -> list[dict[str, Any]]:
    narrative = request["narrative"]
    components: dict[str, list[dict[str, Any]]] = {
        "hook": [{
            "node_id": "hook",
            "block": "hook",
            "purpose": "earn attention",
            "text": _finish_sentence(narrative["hook"]),
        }],
        "problem": [{
            "node_id": "problem",
            "block": "problem",
            "purpose": "create recognition",
            "text": _finish_sentence(narrative["problem"]),
        }],
        "stakes": [{
            "node_id": "stakes",
            "block": "stakes",
            "purpose": "make the consequence concrete",
            "text": _finish_sentence(narrative["stakes"]),
        }],
        "reframe": [{
            "node_id": "reframe",
            "block": "reframe",
            "purpose": "replace the weak mental model",
            "text": _finish_sentence(narrative["reframe"]),
        }],
        "steps": [{
            "node_id": f"step_{index + 1}",
            "block": "teaching_step",
            "purpose": "give a usable action",
            "text": _step_text(step, index, structure["structure_id"]),
        } for index, step in enumerate(narrative["steps"])],
    }
    proof_text = narrative["proof"]["statement"]
    components.update({
        "proof": [{
            "node_id": "proof",
            "block": "proof",
            "purpose": "make the lesson believable",
            "text": _finish_sentence(proof_text),
        }],
        "takeaway": [{
            "node_id": "takeaway",
            "block": "takeaway",
            "purpose": "compress the lesson",
            "text": _finish_sentence(narrative["takeaway"]),
        }],
        "cta": [{
            "node_id": "cta",
            "block": "call_to_action",
            "purpose": "give one relevant next step",
            "text": _finish_sentence(narrative["cta"]["text"]),
        }],
    })
    return arrange_role_components(components, structure)


def _time_beats(
    beats: list[dict[str, Any]], target_seconds: int
) -> list[dict[str, Any]]:
    return retime_timeline(beats, target_seconds=float(target_seconds))


def _quality_report(
    request: dict[str, Any],
    beats: list[dict[str, Any]],
    transcript: str,
    owner_quality: dict[str, Any],
) -> dict[str, Any]:
    transcript_words = words(transcript)
    sentences = [
        words(match.group(0))
        for match in SENTENCE_RE.finditer(transcript)
        if words(match.group(0))
    ]
    sentence_lengths = [len(value) for value in sentences]
    jargon_hits = sorted(
        value for value in transcript_words if value in JARGON_WORDS
    )
    target_words = request["target_seconds"] * SPOKEN_WORDS_PER_SECOND
    duration_fit = max(
        0.0,
        1.0 - abs(len(transcript_words) - target_words) / max(1.0, target_words),
    )
    hook_words = len(words(beats[0]["text"]))
    cta_words = len(words(beats[-1]["text"]))
    direct_address_count = sum(
        1
        for value in transcript_words
        if value in ("you", "your", "youre", "you'll")
    )
    offer_name = request["offer"]["name"].lower()
    pre_cta = " ".join(beat["text"] for beat in beats[:-1]).lower()
    value_before_offer = not offer_name or offer_name not in pre_cta
    proof = request["narrative"]["proof"]
    claim_evidence_passed = (
        proof["evidence_type"] not in SOURCE_REQUIRED_PROOF_TYPES
        or bool(proof["source_receipt_ids"])
    )
    average_sentence_words = sum(sentence_lengths) / max(1, len(sentence_lengths))
    checks = {
        "duration_fit": {
            "passed": duration_fit >= 0.75,
            "score": round(duration_fit * 100.0, 3),
            "target_words": round(target_words),
            "actual_words": len(transcript_words),
        },
        "hook": {
            "passed": 6 <= hook_words <= 28,
            "score": 100.0 if 6 <= hook_words <= 28 else 40.0,
            "word_count": hook_words,
        },
        "plain_language": {
            "passed": (
                average_sentence_words <= 20
                and max(sentence_lengths, default=0) <= 34
                and len(jargon_hits) <= 4
            ),
            "score": max(0.0, round(100.0 - len(jargon_hits) * 10.0, 3)),
            "average_sentence_words": round(average_sentence_words, 3),
            "maximum_sentence_words": max(sentence_lengths, default=0),
            "jargon_hits": jargon_hits,
        },
        "useful_steps": {
            "passed": 2 <= len(request["narrative"]["steps"]) <= 8,
            "score": 100.0,
            "count": len(request["narrative"]["steps"]),
        },
        "claim_evidence": {
            "passed": claim_evidence_passed,
            "score": 100.0 if claim_evidence_passed else 0.0,
            "evidence_type": proof["evidence_type"],
            "source_receipt_ids": proof["source_receipt_ids"],
        },
        "direct_audience_language": {
            "passed": direct_address_count >= 2,
            "score": min(100.0, direct_address_count * 25.0),
            "direct_address_count": direct_address_count,
        },
        "value_before_offer": {
            "passed": value_before_offer,
            "score": 100.0 if value_before_offer else 0.0,
        },
        "single_cta": {
            "passed": 4 <= cta_words <= 30,
            "score": 100.0 if 4 <= cta_words <= 30 else 40.0,
            "action": request["narrative"]["cta"]["action"],
            "word_count": cta_words,
        },
        "owner_calibrated_quality": {
            "passed": owner_quality["decision"] == "PASS",
            "score": owner_quality["score"],
            "contract": owner_quality["contract"],
            "failure_codes": owner_quality["failure_codes"],
        },
    }
    baseline_weighted = (
        checks["duration_fit"]["score"] * 0.20
        + checks["hook"]["score"] * 0.12
        + checks["plain_language"]["score"] * 0.16
        + checks["useful_steps"]["score"] * 0.14
        + checks["claim_evidence"]["score"] * 0.14
        + checks["direct_audience_language"]["score"] * 0.08
        + checks["value_before_offer"]["score"] * 0.08
        + checks["single_cta"]["score"] * 0.08
    )
    weighted = baseline_weighted * 0.75 + owner_quality["score"] * 0.25
    failed = [name for name, check in checks.items() if not check["passed"]]
    return {
        "status": "pass" if not failed and weighted >= 80 else "revise",
        "score": round(weighted, 3),
        "checks": checks,
        "failed_checks": failed,
        "owner_calibrated": owner_quality,
    }


class MarketingScriptCompiler:
    """Compile typed marketing briefs against a rights-aware reference corpus."""

    def __init__(
        self,
        corpus: ReferenceCorpusService,
        script_experiments: Any | None = None,
    ) -> None:
        self.corpus = corpus
        self.script_experiments = script_experiments

    def compile(self, payload: dict[str, Any]) -> dict[str, Any]:
        request = _normalize_request(payload)
        proof_request = request["narrative"]["proof"]
        proof_first_person = bool(
            {
                token.casefold().replace("’", "'")
                for token in words(proof_request["statement"])
            }
            & FIRST_PERSON_WORDS
        )
        owned_binding_required = (
            proof_request["evidence_type"] in OWNED_REQUIRED_PROOF_TYPES
            or proof_first_person
        )
        if owned_binding_required:
            proof_evidence_gate = self.corpus.validate_owned_evidence(
                receipt_ids=proof_request["source_receipt_ids"],
                statement=proof_request["statement"],
                first_person_claim=proof_first_person,
            )
            if not proof_evidence_gate["passed"]:
                raise ValueError(
                    "owned proof did not resolve to exact stored evidence: "
                    + ", ".join(
                        proof_evidence_gate["failure_codes"]
                        or ["OWNED_EVIDENCE_REQUIRED"]
                    )
                )
        else:
            proof_evidence_gate = {
                "contract": "owned_evidence_validation_v1",
                "passed": True,
                "required": False,
                "receipt_ids": [],
                "failure_codes": [],
            }
        request_sha = canonical_sha256(request)
        query = " ".join((
            request["topic"],
            request["audience"],
            request["narrative"]["problem"],
            request["narrative"]["reframe"],
        ))
        context = self.corpus.agent_context(
            corpus_id=request["corpus_id"], query=query, evidence_limit=8
        )
        script_id = stable_id(
            "refscript_", request_sha, context["context_id"], COMPILER_VERSION
        )
        existing = self.corpus.get_script_package(script_id)
        if existing is not None:
            return existing

        proof_text = request["narrative"]["proof"]["statement"]
        attempts: list[dict[str, Any]] = []
        approved = False
        beats: list[dict[str, Any]] = []
        transcript = ""
        quality: dict[str, Any] = {}
        audit: dict[str, Any] = {}
        structure: dict[str, Any] = {}
        for attempt_index in range(MAX_QUALITY_REWRITE_ATTEMPTS):
            structure = select_rhetorical_structure(
                "reference_marketing",
                seed=request_sha,
                attempt=attempt_index,
                preferred=ANGLE_STRUCTURE[request["angle"]],
            )
            beats = _time_beats(
                _build_beats(request, structure), int(request["target_seconds"])
            )
            transcript = " ".join(beat["text"] for beat in beats)
            initial_owner_quality = audit_owner_calibrated_quality(
                transcript,
                timeline=beats,
                protected_phrases=(proof_text,),
            )
            repair_actions = owner_repair_actions(initial_owner_quality)
            repaired = False
            if repair_actions:
                revised_beats = repair_timeline_for_owner_quality(
                    beats,
                    initial_owner_quality,
                    protected_phrases=(proof_text,),
                    attempt=attempt_index + 1,
                    target_seconds=float(request["target_seconds"]),
                )
                repaired = revised_beats != beats
                beats = revised_beats
                transcript = " ".join(beat["text"] for beat in beats)
            owner_quality = audit_owner_calibrated_quality(
                transcript,
                timeline=beats,
                protected_phrases=(proof_text,),
            )
            quality = _quality_report(request, beats, transcript, owner_quality)
            audit = self.corpus.audit_content(
                corpus_id=request["corpus_id"],
                title=request["title"],
                script=transcript,
                objective=request["objective"],
                target_viewer=request["audience"],
                target_seconds=request["target_seconds"],
            )
            approved = (
                quality["status"] == "pass"
                and audit["status"] == "pass"
                and bool(audit["copy_gate"]["passed"])
            )
            attempts.append({
                "attempt": attempt_index + 1,
                "structure_id": structure["structure_id"],
                "candidate_sha256": canonical_sha256(transcript),
                "initial_owner_failure_codes": initial_owner_quality[
                    "failure_codes"
                ],
                "repair_actions": repair_actions,
                "repair_applied": repaired,
                "owner_decision": owner_quality["decision"],
                "owner_failure_codes": owner_quality["failure_codes"],
                "corpus_audit_id": audit["audit_id"],
                "copy_gate_passed": bool(audit["copy_gate"]["passed"]),
                "approved": approved,
            })
            if approved:
                break
        delivery_visual_plan = build_delivery_visual_plan(
            beats, structure_id=structure["structure_id"]
        )
        created_at = utc_now()
        package = {
            "status": "approved" if approved else "revise",
            "contract": PACKAGE_CONTRACT,
            "script_id": script_id,
            "corpus_id": request["corpus_id"],
            "context_id": context["context_id"],
            "request_contract": REQUEST_CONTRACT,
            "request_sha256": request_sha,
            "request": request,
            "marketing_logic": {
                "objective": request["objective"],
                "angle": request["angle"],
                "audience": request["audience"],
                "value_sequence": structure["role_order"],
                "rhetorical_structure": structure,
                "offer_id": request["offer"]["offer_id"],
            },
            "script": {
                "title": request["title"],
                "target_seconds": request["target_seconds"],
                "word_count": len(words(transcript)),
                "estimated_seconds_at_reference_pace": round(
                    len(words(transcript)) / SPOKEN_WORDS_PER_SECOND, 3
                ),
                "required_words_per_second": round(
                    len(words(transcript)) / request["target_seconds"], 3
                ),
                "transcript": transcript,
                "beats": beats,
                "rhetorical_structure": structure,
                "delivery_visual_plan": delivery_visual_plan,
            },
            "quality": quality,
            "proof_evidence_gate": proof_evidence_gate,
            "corpus_audit": audit,
            "reference_context": {
                "contract": context["contract"],
                "context_id": context["context_id"],
                "query": context["query"],
                "coverage": context["coverage"],
                "numeric_profile": context["numeric_profile"],
                "observed_patterns": context["observed_patterns"],
                "descriptive_associations": context["descriptive_associations"],
                "evidence": context["evidence"],
                "result_sha256": context["result_sha256"],
            },
            "rights": {
                "state": SOURCE_RIGHTS_STATE,
                "source_clips_used": False,
                "direct_use_allowed": False,
                "identity_imitation_allowed": False,
                "voice_imitation_allowed": False,
                "exact_draft_copy_gate_passed": bool(
                    audit["copy_gate"]["passed"]
                ),
            },
            "revision": {
                "contract": "bounded_script_quality_rewrite_v1",
                "maximum_attempts": MAX_QUALITY_REWRITE_ATTEMPTS,
                "attempt_count": len(attempts),
                "final_attempt": len(attempts),
                "attempts": attempts,
                "exact_copy_audit_ids": [
                    item["corpus_audit_id"] for item in attempts
                ],
                "final_exact_copy_audit_id": audit["audit_id"],
                "source_proof_text_modified": proof_text not in transcript,
            },
            "lineage": {
                "compiler": COMPILER_VERSION,
                "writer_mode": "deterministic_no_model",
                "request_sha256": request_sha,
                "context_result_sha256": context["result_sha256"],
                "audit_result_sha256": audit["result_sha256"],
                "exact_copy_audit_ids": [
                    item["corpus_audit_id"] for item in attempts
                ],
            },
            "created_at": created_at,
        }
        if approved and self.script_experiments is not None:
            brief_id = stable_id("refbrief_", request_sha)
            workflow_id = stable_id(
                "refworkflow_", script_id, audit["audit_id"], COMPILER_VERSION
            )
            registration = self.script_experiments.register_experiment({
                "brief_id": brief_id,
                "script_id": script_id,
                "script_text": transcript,
                "workflow_id": workflow_id,
                "generation_contract": COMPILER_VERSION,
                "metadata": {
                    "registration_source": "reference_marketing_compiler",
                    "corpus_id": request["corpus_id"],
                    "copy_audit_id": audit["audit_id"],
                    "owner_quality_contract": owner_quality["contract"],
                },
            })
            package["workflow_id"] = workflow_id
            package["script_experiment_id"] = registration["experiment"][
                "experiment_id"
            ]
            package["script_experiment_registration"] = {
                "status": registration["status"],
                "created": registration["created"],
                "experiment": registration["experiment"],
            }
        else:
            package["script_experiment_id"] = None
            package["script_experiment_registration"] = {
                "status": "not_registered",
                "reason": (
                    "compiler_telemetry_not_configured"
                    if approved else "script_not_approved"
                ),
            }
        package["result_sha256"] = canonical_sha256(package)
        return self.corpus.put_script_package(package)

    def get(self, script_id: str) -> dict[str, Any] | None:
        return self.corpus.get_script_package(script_id)


def render_package_markdown(package: dict[str, Any]) -> str:
    """Render a review-friendly view without changing the package."""
    script = package["script"]
    lines = [
        f"# {script['title']}",
        "",
        f"Status: `{package['status']}`",
        f"Script ID: `{package['script_id']}`",
        f"Target: `{script['target_seconds']}s`",
        f"Words: `{script['word_count']}`",
        f"Marketing score: `{package['quality']['score']}`",
        f"Corpus score: `{package['corpus_audit']['overall_score']}`",
        "",
        "## Transcript",
        "",
        script["transcript"],
        "",
        "## Beat Map",
        "",
    ]
    for beat in script["beats"]:
        lines.append(
            f"- `{beat['start_seconds']:.3f}-{beat['end_seconds']:.3f}s` "
            f"**{beat['block']}**: {beat['text']}"
        )
    lines.extend(("", "## Evidence", ""))
    for row in package["reference_context"]["evidence"]:
        lines.append(
            f"- [{row['item_id']}]({row['source_url']}) "
            f"match `{row['match_score']}`"
        )
    lines.extend((
        "",
        "## Rights",
        "",
        "Reference patterns only. No source clips, identity, likeness, or voice are used.",
        f"Exact-draft copy gate: `{package['rights']['exact_draft_copy_gate_passed']}`",
        "",
    ))
    return "\n".join(lines)
