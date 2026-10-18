"""Clean-room benchmark for external content-factory repository methods."""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal

import httpx
from openai import OpenAI
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from .copy_policy import audit_substantive_copy, build_script_only_provenance
from .script_quality import (
    audit_owner_calibrated_quality,
    repair_owner_quality_text,
    words,
)


BENCHMARK_CONTRACT = "external_content_repo_benchmark_v1"
GENERATION_CONTRACT = "repo_profile_structured_output_v1"
DEFAULT_CORPUS_ID = "instagram-personalbrandlaunch-reference-v1"
DEFAULT_MODEL = "gpt-5.6"
TARGET_SECONDS = 60
WORD_RANGE = (125, 155)

ReusePolicy = Literal[
    "permissive_clean_room",
    "copyleft_isolated",
    "evaluation_only_no_license",
    "quarantined_security_review",
    "owned_control",
]
NativeStatus = Literal[
    "working",
    "partial",
    "roadmap_only",
    "production_only",
    "quarantined",
    "owned_control",
]
AdapterMode = Literal[
    "profile_adapted", "native_prompt_profile", "owned_control_profile"
]
ProfileRole = Literal[
    "closed_loop_factory",
    "content_factory",
    "viral_intelligence",
    "intelligence_layer",
    "production_layer",
    "owned_control",
]


class BenchmarkBeat(BaseModel):
    model_config = ConfigDict(extra="forbid")

    label: str = Field(min_length=1, max_length=40)
    text: str = Field(min_length=1, max_length=500)


class GeneratedTranscript(BaseModel):
    model_config = ConfigDict(extra="forbid")

    brief_id: str = Field(min_length=1, max_length=80)
    title: str = Field(min_length=1, max_length=100)
    hook: str = Field(min_length=1, max_length=240)
    beats: list[BenchmarkBeat] = Field(min_length=3, max_length=8)
    transcript: str = Field(min_length=200, max_length=2000)
    cta: str = Field(min_length=1, max_length=240)
    word_count: int = Field(ge=80, le=220)
    methodology_choices: list[str] = Field(min_length=1, max_length=8)
    factual_claims_used: list[str] = Field(default_factory=list, max_length=12)

    @field_validator("transcript")
    @classmethod
    def transcript_must_be_spoken_copy(cls, value: str) -> str:
        return value.strip()


class GeneratedBatch(BaseModel):
    model_config = ConfigDict(extra="forbid")

    profile_id: str = Field(min_length=1, max_length=80)
    transcripts: list[GeneratedTranscript] = Field(min_length=3, max_length=3)


class SingleTranscriptEnvelope(BaseModel):
    model_config = ConfigDict(extra="forbid")

    transcript: GeneratedTranscript


class BenchmarkBrief(BaseModel):
    model_config = ConfigDict(extra="forbid")

    brief_id: str
    topic: str
    audience: str
    objective: str
    thesis: str
    allowed_facts: list[str]
    required_elements: list[str]
    prohibited_claims: list[str]


class RepositoryProfile(BaseModel):
    """Reviewed facts and method abstractions for one evaluation checkout."""

    model_config = ConfigDict(extra="forbid")

    profile_id: str
    label: str
    repo_full_name: str
    repo_url: str | None = None
    checkout_dir: str | None = None
    source_commit: str | None = None
    license_spdx: str
    reuse_policy: ReusePolicy
    native_status: NativeStatus
    adapter_mode: AdapterMode
    role: ProfileRole
    capabilities: list[str] = Field(min_length=1)
    generation_principles: list[str] = Field(min_length=1)
    limitations: list[str] = Field(min_length=1)
    evidence_paths: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_reuse_policy(self) -> "RepositoryProfile":
        no_assertion = self.license_spdx.upper() == "NOASSERTION"
        allowed = {
            "evaluation_only_no_license", "quarantined_security_review"
        }
        if no_assertion and self.reuse_policy not in allowed:
            raise ValueError("unlicensed sources are evaluation-only")
        if (
            self.reuse_policy == "quarantined_security_review"
            and self.native_status != "quarantined"
        ):
            raise ValueError("security-review checkouts must stay quarantined")
        if self.reuse_policy == "owned_control" and self.role != "owned_control":
            raise ValueError("owned-control policy requires owned-control role")
        return self

    def prompt_projection(self) -> dict[str, Any]:
        """Return the only repository information allowed into generation."""

        return {
            "profile_id": self.profile_id,
            "label": self.label,
            "role": self.role,
            "adapter_mode": self.adapter_mode,
            "method_capabilities": list(self.capabilities),
            "generation_principles": list(self.generation_principles),
            "known_limitations": list(self.limitations),
        }


def benchmark_briefs() -> list[BenchmarkBrief]:
    audience = "Software founders and teams at $500K-$5M ARR"
    shared_prohibitions = [
        "Do not invent metrics, customers, experiments, or results.",
        "Do not claim a repository or method caused audience performance.",
        "Do not imitate a person's identity, likeness, voice, or phrasing.",
        "Do not turn associations into causal claims.",
    ]
    return [
        BenchmarkBrief(
            brief_id="control_plane",
            topic="AI agent control planes",
            audience=audience,
            objective="Show a founder how to make an agent safe enough to run.",
            thesis="An agent becomes useful when its permissions, evaluations, traces, and stop controls are explicit.",
            allowed_facts=[
                "Permissions limit which actions an agent may take.",
                "Evaluations test behavior against defined cases.",
                "Traces record the steps and tool calls in a run.",
                "A kill switch gives a person a way to halt execution.",
            ],
            required_elements=[
                "Open with an observable founder problem.",
                "Name at least three of permissions, evaluations, traces, and kill switches.",
                "End with one concrete audit action.",
            ],
            prohibited_claims=shared_prohibitions,
        ),
        BenchmarkBrief(
            brief_id="automation_roi",
            topic="AI automation ROI",
            audience=audience,
            objective="Help a founder choose the first workflow worth automating.",
            thesis="The best first automation removes a recurring business bottleneck and has a measurable result.",
            allowed_facts=[
                "A recurring bottleneck is easier to measure than a one-time task.",
                "Baseline time, delay, error, or conversion can be recorded before automation.",
                "A narrow workflow is easier to verify than an end-to-end demo.",
                "The result should be compared with the baseline after deployment.",
            ],
            required_elements=[
                "Contrast a flashy demo with a measurable bottleneck.",
                "Give a simple selection test.",
                "End with one action the viewer can take today.",
            ],
            prohibited_claims=shared_prohibitions,
        ),
        BenchmarkBrief(
            brief_id="practical_ai",
            topic="Practical AI implementation",
            audience=audience,
            objective="Turn a vague AI initiative into a small verifiable implementation.",
            thesis="Practical AI starts with a real input, a bounded decision, an owned action, and a visible result.",
            allowed_facts=[
                "A workflow needs a defined input and an expected output.",
                "A bounded decision is easier to evaluate than an open-ended mandate.",
                "The team should decide where the output goes and how to recover if it cannot be used.",
                "A visible result makes verification possible.",
            ],
            required_elements=[
                "Start with a concrete workday moment.",
                "Explain the four-part implementation test in plain language.",
                "End with a small implementation step.",
            ],
            prohibited_claims=shared_prohibitions,
        ),
    ]


SYSTEM_INSTRUCTIONS = """You write original short-form scripts for a clean-room software-method benchmark.
Use only facts in each brief. Apply the abstract method profile without mentioning a repository, benchmark, source collection, person, or internal process. Never copy or imitate identity, likeness, voice, signature phrasing, or source assets. Write exactly three spoken scripts, one per brief. Each must be 125-155 words for about 60 seconds and use short, natural sentences. In the first 30 words, include one plain hook word such as how, if, most, stop, what, or why plus an observable problem. Put an early tension word such as stuck, delay, risk, problem, wrong, or waiting before a later payoff word such as clear, fix, result, save, works, or finished. Include a turn such as but, because, so, or instead. End with one concrete CTA whose exact closing text includes ask, check, open, reply, save, send, share, start, try, use, or watch. Never use the phrases destination action or recovery path; say where the output goes and how the team gets back on track. Avoid repeating workflow, system, production, process, or other internal terms more than twice in total. Separate claims from evidence and do not invent outcomes, metrics, customers, quotations, or causal claims. Make transcript.word_count equal the whitespace-delimited transcript count."""


REPAIR_GUIDANCE = {
    "OWNER_SPOKEN_NATURALNESS": (
        "Use short spoken sentences; keep the average under 18 words and no "
        "sentence over 32 words."
    ),
    "OWNER_SPECIFICITY": (
        "Use at least three concrete action verbs and two concrete nouns such "
        "as app, client, email, inbox, message, task, team, or tool."
    ),
    "OWNER_TENSION_PAYOFF": (
        "Place an early tension word such as stuck, delay, risk, problem, wrong, "
        "or waiting before a later payoff word such as clear, fix, result, save, "
        "works, or finished; add but or instead."
    ),
    "OWNER_TECHNICAL_LANGUAGE_LEAKAGE": (
        "Replace destination action with next step and recovery path with way "
        "back. Use task, steps, or setup in place of repeated workflow, system, "
        "production, process, routing, or pipeline language."
    ),
    "OWNER_REPEATED_PHRASING": (
        "Remove repeated four-word phrases and use a distinct opening."
    ),
}


def build_generation_input(
    profile: RepositoryProfile,
    briefs: list[BenchmarkBrief] | None = None,
) -> str:
    payload = {
        "contract": GENERATION_CONTRACT,
        "profile": profile.prompt_projection(),
        "briefs": [
            item.model_dump(mode="json") for item in (briefs or benchmark_briefs())
        ],
    }
    return json.dumps(payload, sort_keys=True, ensure_ascii=True)


def validate_batch_coverage(
    batch: GeneratedBatch,
    profile: RepositoryProfile,
    briefs: list[BenchmarkBrief],
) -> None:
    if batch.profile_id != profile.profile_id:
        raise ValueError("generated profile_id does not match requested profile")
    expected = [item.brief_id for item in briefs]
    actual = [item.brief_id for item in batch.transcripts]
    if sorted(actual) != sorted(expected) or len(actual) != len(set(actual)):
        raise ValueError(f"generated brief coverage mismatch: {actual!r}")


def sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def longest_exact_word_run(left: str, right: str) -> tuple[int, str]:
    """Return the longest normalized contiguous word run shared by two texts."""

    left_words = words(left)
    right_words = words(right)
    if not left_words or not right_words:
        return 0, ""
    previous = [0] * (len(right_words) + 1)
    longest = 0
    longest_end = 0
    for left_index, left_word in enumerate(left_words, start=1):
        current = [0] * (len(right_words) + 1)
        for right_index, right_word in enumerate(right_words, start=1):
            if left_word == right_word:
                current[right_index] = previous[right_index - 1] + 1
                if current[right_index] > longest:
                    longest = current[right_index]
                    longest_end = left_index
        previous = current
    phrase = " ".join(left_words[longest_end - longest:longest_end])
    return longest, phrase


def peer_overlap_receipt(
    text: str,
    prior_scripts: list[tuple[str, str]],
) -> dict[str, Any]:
    """Audit peer reuse without a fixed matching-word cutoff."""

    longest = 0
    nearest_script_id = ""
    shared_phrase = ""
    for script_id, prior_text in prior_scripts:
        run_length, phrase = longest_exact_word_run(text, prior_text)
        if run_length > longest:
            longest = run_length
            nearest_script_id = script_id
            shared_phrase = phrase
    comparison_sources = [
        dict(source_id=identifier, text=value)
        for identifier, value in prior_scripts
    ]
    copy_gate = audit_substantive_copy(
        text,
        comparison_sources,
        provenance=build_script_only_provenance(text),
    )
    return {
        "passed": copy_gate["passed"],
        "fixed_matching_word_limit_applied": False,
        "longest_exact_word_run": longest,
        "nearest_script_id": nearest_script_id,
        "shared_phrase": shared_phrase,
        "copy_gate": copy_gate,
    }


def normalize_factual_claims(
    item: GeneratedTranscript,
    brief: BenchmarkBrief,
) -> list[str]:
    """Keep only canonical brief facts in generated lineage metadata."""

    canonical = {
        " ".join(value.split()).casefold(): value for value in brief.allowed_facts
    }
    kept: list[str] = []
    removed: list[str] = []
    for raw_value in item.factual_claims_used:
        normalized = " ".join(str(raw_value).split()).casefold()
        value = canonical.get(normalized)
        if value is None:
            removed.append(str(raw_value))
        elif value not in kept:
            kept.append(value)
    item.factual_claims_used = kept
    return removed


def validate_transcript_shape(item: GeneratedTranscript) -> list[str]:
    findings: list[str] = []
    actual = len(item.transcript.split())
    item.word_count = actual
    if not WORD_RANGE[0] <= actual <= WORD_RANGE[1]:
        findings.append(
            f"word_count must be {WORD_RANGE[0]}-{WORD_RANGE[1]}; got {actual}"
        )
    if not item.transcript.startswith(item.hook):
        findings.append("hook must be the exact opening text")
    if not item.transcript.rstrip().endswith(item.cta.rstrip()):
        findings.append("cta must be the exact closing text")
    return findings


def deterministic_owner_repair(
    item: GeneratedTranscript,
    report: dict[str, Any],
    *,
    attempt: int = 1,
) -> GeneratedTranscript:
    """Apply the canonical literal quality edits to every spoken text field."""

    repaired = item.model_copy(deep=True)
    repaired.hook = repair_owner_quality_text(
        repaired.hook, report, attempt=attempt
    )
    repaired.cta = repair_owner_quality_text(
        repaired.cta, report, attempt=attempt
    )
    repaired.transcript = repair_owner_quality_text(
        repaired.transcript, report, attempt=attempt
    )
    repaired.beats = [
        beat.model_copy(update={
            "text": repair_owner_quality_text(
                beat.text, report, attempt=attempt
            )
        })
        for beat in repaired.beats
    ]
    repaired.word_count = len(repaired.transcript.split())
    return repaired


def generate_batch(
    client: OpenAI,
    profile: RepositoryProfile,
    briefs: list[BenchmarkBrief],
    *,
    model: str = DEFAULT_MODEL,
) -> tuple[GeneratedBatch, dict[str, Any]]:
    response = client.responses.parse(
        model=model,
        instructions=SYSTEM_INSTRUCTIONS,
        input=build_generation_input(profile, briefs),
        text_format=GeneratedBatch,
        max_output_tokens=8000,
        store=False,
        timeout=180.0,
    )
    if response.output_parsed is None:
        raise RuntimeError("generation returned no parsed batch")
    batch = response.output_parsed
    validate_batch_coverage(batch, profile, briefs)
    briefs_by_id = {item.brief_id: item for item in briefs}
    for item in batch.transcripts:
        normalize_factual_claims(item, briefs_by_id[item.brief_id])
        validate_transcript_shape(item)
    lineage = {
        "response_id": response.id,
        "model": getattr(response, "model", model),
        "generation_input_sha256": sha256_text(
            build_generation_input(profile, briefs)
        ),
    }
    return batch, lineage


def repair_transcript(
    client: OpenAI,
    *,
    profile: RepositoryProfile,
    brief: BenchmarkBrief,
    item: GeneratedTranscript,
    findings: list[str],
    model: str = DEFAULT_MODEL,
) -> tuple[GeneratedTranscript, str]:
    payload = {
        "contract": GENERATION_CONTRACT,
        "task": "Repair only the listed quality failures while preserving the brief.",
        "profile": profile.prompt_projection(),
        "brief": brief.model_dump(mode="json"),
        "current": item.model_dump(mode="json"),
        "quality_failures": findings,
        "constraints": {
            "word_range": list(WORD_RANGE),
            "original_spoken_copy": True,
            "allowed_facts_only": True,
            "exact_hook_at_start": True,
            "exact_cta_at_end": True,
        },
    }
    response = client.responses.parse(
        model=model,
        instructions=SYSTEM_INSTRUCTIONS.replace(
            "Write exactly three spoken scripts, one per brief.",
            "Return exactly one repaired spoken script for the supplied brief.",
        ),
        input=json.dumps(payload, sort_keys=True, ensure_ascii=True),
        text_format=SingleTranscriptEnvelope,
        max_output_tokens=3500,
        store=False,
        timeout=180.0,
    )
    if response.output_parsed is None:
        raise RuntimeError("repair returned no parsed transcript")
    repaired = response.output_parsed.transcript
    if repaired.brief_id != brief.brief_id:
        raise ValueError("repair changed brief_id")
    normalize_factual_claims(repaired, brief)
    validate_transcript_shape(repaired)
    return repaired, response.id


class ContentQualityClient:
    """Small authenticated client for the local quality service."""

    def __init__(
        self,
        base_url: str,
        *,
        token: str | None = None,
        timeout_seconds: float = 90.0,
    ) -> None:
        headers = {"Content-Type": "application/json"}
        effective_token = token
        if effective_token is None:
            effective_token = os.environ.get(
                "CONTENT_QUALITY_CONTROL_TOKEN", ""
            ).strip()
        if effective_token:
            headers["Authorization"] = f"Bearer {effective_token}"
        self._client = httpx.Client(
            base_url=base_url.rstrip("/"),
            headers=headers,
            timeout=timeout_seconds,
        )

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> "ContentQualityClient":
        return self

    def __exit__(self, *_args: object) -> None:
        self.close()

    @staticmethod
    def _unwrap(value: Any) -> dict[str, Any]:
        if not isinstance(value, dict):
            raise RuntimeError("quality service returned a non-object")
        result = value.get("result")
        return result if isinstance(result, dict) else value

    def _post(self, path: str, payload: dict[str, Any]) -> dict[str, Any]:
        response = self._client.post(path, json=payload)
        response.raise_for_status()
        return self._unwrap(response.json())

    def _get(
        self, path: str, params: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        response = self._client.get(path, params=params)
        response.raise_for_status()
        return self._unwrap(response.json())

    def health(self) -> dict[str, Any]:
        return self._get("/api/reference-corpus/health")

    def corpus_status(self, corpus_id: str) -> dict[str, Any]:
        return self._get(
            "/api/reference-corpus/status", {"corpus_id": corpus_id}
        )

    def context_receipt(self, corpus_id: str) -> dict[str, Any]:
        result = self._post(
            "/api/reference-corpus/context",
            {
                "corpus_id": corpus_id,
                "query": (
                    "Short-form structure for AI agent controls, automation ROI, "
                    "and practical AI implementation for software founders"
                ),
                "evidence_limit": 8,
            },
        )
        return {
            "context_id": result.get("context_id"),
            "result_sha256": result.get("result_sha256"),
            "corpus_id": corpus_id,
            "rights": result.get("rights"),
        }

    def audit(
        self,
        *,
        corpus_id: str,
        title: str,
        script: str,
        objective: str,
        target_viewer: str,
        target_seconds: int = TARGET_SECONDS,
    ) -> dict[str, Any]:
        return self._post(
            "/api/reference-corpus/audit",
            {
                "corpus_id": corpus_id,
                "title": title,
                "script": script,
                "objective": objective,
                "target_viewer": target_viewer,
                "target_seconds": target_seconds,
                "provenance": build_script_only_provenance(
                    script,
                    source_material_usage="abstract_patterns_only",
                ),
            },
        )

    def register_experiment(
        self,
        *,
        brief_id: str,
        script_id: str,
        script_text: str,
        workflow_id: str,
        metadata: dict[str, Any],
    ) -> dict[str, Any]:
        return self._post(
            "/api/script-experiments",
            {
                "brief_id": brief_id,
                "script_id": script_id,
                "script_text": script_text,
                "workflow_id": workflow_id,
                "generation_contract": GENERATION_CONTRACT,
                "metadata": metadata,
            },
        )


def load_registry(path: Path) -> list[RepositoryProfile]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict) or value.get("contract") != BENCHMARK_CONTRACT:
        raise ValueError("invalid benchmark registry contract")
    raw_profiles = value.get("profiles")
    if not isinstance(raw_profiles, list) or not raw_profiles:
        raise ValueError("benchmark registry has no profiles")
    profiles = [RepositoryProfile.model_validate(item) for item in raw_profiles]
    ids = [item.profile_id for item in profiles]
    names = [item.repo_full_name.casefold() for item in profiles]
    if len(ids) != len(set(ids)):
        raise ValueError("profile_id values must be unique")
    if len(names) != len(set(names)):
        raise ValueError("repository names must be unique")
    return profiles


def verify_checkouts(
    profiles: list[RepositoryProfile], source_root: Path
) -> list[dict[str, Any]]:
    receipts: list[dict[str, Any]] = []
    for profile in profiles:
        if profile.reuse_policy == "owned_control":
            receipts.append({
                "profile_id": profile.profile_id,
                "status": "owned_control",
                "verified": True,
            })
            continue
        checkout = source_root / str(profile.checkout_dir or "")
        if not checkout.is_dir():
            raise FileNotFoundError(f"missing checkout for {profile.profile_id}")
        head = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=checkout,
            text=True,
            capture_output=True,
            check=True,
        ).stdout.strip()
        origin = subprocess.run(
            ["git", "config", "--get", "remote.origin.url"],
            cwd=checkout,
            text=True,
            capture_output=True,
            check=True,
        ).stdout.strip()
        if profile.source_commit and head != profile.source_commit:
            raise ValueError(f"commit mismatch for {profile.profile_id}")
        receipts.append({
            "profile_id": profile.profile_id,
            "status": profile.native_status,
            "verified": True,
            "checkout": str(checkout),
            "commit": head,
            "origin_sha256": sha256_text(origin),
        })
    return receipts


def quality_findings(
    item: GeneratedTranscript,
    audit: dict[str, Any],
    *,
    prior_texts: list[str],
) -> tuple[list[str], dict[str, Any]]:
    findings = validate_transcript_shape(item)
    owner = audit_owner_calibrated_quality(
        item.transcript, prior_texts=prior_texts
    )
    if owner.get("decision") != "PASS":
        for code in owner.get("failure_codes", []):
            findings.append(f"Resolve {code}.")
            guidance = REPAIR_GUIDANCE.get(str(code))
            if guidance:
                findings.append(guidance)
    if audit.get("status") != "pass":
        findings.extend(str(note) for note in audit.get("notes", []))
    return list(dict.fromkeys(findings)), owner


def annotate_peer_overlaps(
    results: list[dict[str, Any]],
) -> dict[str, Any]:
    """Attach deterministic cross-benchmark overlap receipts in run order."""

    prior_scripts: list[tuple[str, str]] = []
    maximum = 0
    failures = 0
    for result in results:
        profile_id = str(result["profile"]["profile_id"])
        for record in result["transcripts"]:
            script_id = f"{profile_id}/{record['brief_id']}"
            transcript = str(record["transcript"]["transcript"])
            receipt = peer_overlap_receipt(transcript, prior_scripts)
            record["peer_overlap"] = receipt
            maximum = max(maximum, int(receipt["longest_exact_word_run"]))
            if not receipt["passed"]:
                failures += 1
            prior_scripts.append((script_id, transcript))
    return {
        "fixed_matching_word_limit_applied": False,
        "maximum_exact_word_run": maximum,
        "failure_count": failures,
        "passed": failures == 0,
    }


def repair_peer_overlap_record(
    *,
    record: dict[str, Any],
    profile: RepositoryProfile,
    brief: BenchmarkBrief,
    prior_scripts: list[tuple[str, str]],
    ai_client: OpenAI,
    quality_client: ContentQualityClient,
    corpus_id: str,
    model: str,
    max_repairs: int,
) -> dict[str, Any]:
    """Rewrite one candidate until it clears source, owner, and peer gates."""

    item = GeneratedTranscript.model_validate(record["transcript"])
    removed_claims = normalize_factual_claims(item, brief)
    original_digest = sha256_text(item.transcript)
    prior_texts = [text for _, text in prior_scripts]
    candidates: list[
        tuple[
            GeneratedTranscript,
            dict[str, Any],
            dict[str, Any],
            list[str],
            dict[str, Any],
        ]
    ] = []
    attempts: list[dict[str, Any]] = []
    next_repair_type = "peer_overlap_repair"
    for step in range(max_repairs + 1):
        final_audit = quality_client.audit(
            corpus_id=corpus_id,
            title=item.title,
            script=item.transcript,
            objective=brief.objective,
            target_viewer=brief.audience,
        )
        findings, final_owner = quality_findings(
            item,
            final_audit,
            prior_texts=prior_texts,
        )
        peer = peer_overlap_receipt(item.transcript, prior_scripts)
        if not peer["passed"]:
            findings.extend([
                (
                    "Rewrite the source-like expression, ordered passage, or "
                    "source-specific structure identified by the substantive-copy gate."
                ),
                f"Shared passage to replace: {peer['shared_phrase']}",
            ])
        findings = list(dict.fromkeys(findings))
        candidates.append((
            item.model_copy(deep=True),
            final_audit,
            final_owner,
            findings,
            peer,
        ))
        attempt_receipt = {
            "attempt": len(record.get("attempts") or []) + step,
            "repair_phase": "peer_diversity",
            "input_repair_type": next_repair_type,
            "script_sha256": sha256_text(item.transcript),
            "audit_id": final_audit.get("audit_id"),
            "audit_status": final_audit.get("status"),
            "overall_score": final_audit.get("overall_score"),
            "owner_decision": final_owner.get("decision"),
            "peer_overlap": peer,
            "findings": findings,
        }
        attempts.append(attempt_receipt)
        if not findings or step == max_repairs:
            break
        if peer["passed"]:
            literal = deterministic_owner_repair(
                item,
                final_owner,
                attempt=step + 1,
            )
            normalize_factual_claims(literal, brief)
            if literal.transcript != item.transcript:
                item = literal
                next_repair_type = "canonical_literal_quality_repair"
                continue
        item, response_id = repair_transcript(
            ai_client,
            profile=profile,
            brief=brief,
            item=item,
            findings=findings,
            model=model,
        )
        attempts[-1]["repair_response_id"] = response_id
        next_repair_type = "structured_peer_overlap_repair"

    item, final_audit, final_owner, findings, peer = max(
        candidates,
        key=lambda value: (
            not bool(value[3]),
            bool(value[4]["passed"]),
            -float(
                value[4]["copy_gate"]["substantive_copy"]
                ["maximum_expression_similarity"]
            ),
            value[2].get("decision") == "PASS",
            float(value[1].get("overall_score") or 0.0),
        ),
    )
    accepted = not findings
    experiment = None
    if accepted:
        digest = sha256_text(item.transcript)
        value = quality_client.register_experiment(
            brief_id=f"repo_benchmark_{brief.brief_id}",
            script_id=(
                f"repo_bench_{profile.profile_id}_{brief.brief_id}_{digest[:12]}"
            ),
            script_text=item.transcript,
            workflow_id=(
                f"{BENCHMARK_CONTRACT}:{profile.profile_id}:{brief.brief_id}"
            ),
            metadata={
                "profile_id": profile.profile_id,
                "adapter_mode": profile.adapter_mode,
                "native_status": profile.native_status,
                "source_commit": profile.source_commit,
                "audit_id": final_audit.get("audit_id"),
                "peer_overlap_repair": True,
                "outcomes_measured": False,
            },
        )
        experiment = value.get("experiment", value)
    return {
        **record,
        "accepted": accepted,
        "transcript": item.model_dump(mode="json"),
        "audit": final_audit,
        "owner_quality_within_batch": final_owner,
        "attempts": list(record.get("attempts") or []) + attempts,
        "experiment": experiment,
        "peer_overlap": peer,
        "peer_overlap_repair": {
            "original_script_sha256": original_digest,
            "final_script_sha256": sha256_text(item.transcript),
            "changed": original_digest != sha256_text(item.transcript),
            "removed_noncanonical_factual_claims": removed_claims,
        },
    }


def enforce_peer_diversity(
    *,
    results: list[dict[str, Any]],
    ai_client: OpenAI,
    quality_client: ContentQualityClient,
    corpus_id: str,
    model: str,
    max_repairs: int = 3,
) -> dict[str, Any]:
    """Apply the substantive candidate-to-candidate diversity gate."""

    briefs = {item.brief_id: item for item in benchmark_briefs()}
    prior_scripts: list[tuple[str, str]] = []
    repaired_count = 0
    normalized_claim_count = 0
    for result in results:
        profile = RepositoryProfile.model_validate(result["profile"])
        updated_records: list[dict[str, Any]] = []
        for record in result["transcripts"]:
            brief = briefs[str(record["brief_id"])]
            item = GeneratedTranscript.model_validate(record["transcript"])
            removed = normalize_factual_claims(item, brief)
            normalized_claim_count += len(removed)
            record = {**record, "transcript": item.model_dump(mode="json")}
            peer = peer_overlap_receipt(item.transcript, prior_scripts)
            record["peer_overlap"] = peer
            if bool(record.get("accepted")) and not peer["passed"]:
                record = repair_peer_overlap_record(
                    record=record,
                    profile=profile,
                    brief=brief,
                    prior_scripts=prior_scripts,
                    ai_client=ai_client,
                    quality_client=quality_client,
                    corpus_id=corpus_id,
                    model=model,
                    max_repairs=max_repairs,
                )
                if record["accepted"]:
                    repaired_count += 1
            updated_records.append(record)
            if record.get("accepted"):
                prior_scripts.append((
                    f"{profile.profile_id}/{record['brief_id']}",
                    str(record["transcript"]["transcript"]),
                ))
        result["transcripts"] = updated_records
        scores = [
            float(record["audit"].get("overall_score") or 0.0)
            for record in updated_records
        ]
        result["summary"] = {
            "generated_count": len(updated_records),
            "accepted_count": sum(
                bool(record.get("accepted")) for record in updated_records
            ),
            "average_prepublication_quality": round(
                sum(scores) / max(1, len(scores)), 3
            ),
            "outcomes_measured": False,
        }
    peer_summary = annotate_peer_overlaps(results)
    return {
        **peer_summary,
        "repaired_count": repaired_count,
        "removed_noncanonical_factual_claim_count": normalized_claim_count,
    }


def run_profile(
    *,
    profile: RepositoryProfile,
    ai_client: OpenAI,
    quality_client: ContentQualityClient,
    corpus_id: str = DEFAULT_CORPUS_ID,
    model: str = DEFAULT_MODEL,
    max_repairs: int = 2,
) -> dict[str, Any]:
    briefs = benchmark_briefs()
    batch, lineage = generate_batch(ai_client, profile, briefs, model=model)
    by_brief = {item.brief_id: item for item in batch.transcripts}
    prior_texts: list[str] = []
    completed: list[dict[str, Any]] = []
    for brief in briefs:
        item = by_brief[brief.brief_id]
        attempt_receipts: list[dict[str, Any]] = []
        attempted_values: list[
            tuple[
                GeneratedTranscript,
                dict[str, Any],
                dict[str, Any],
                list[str],
            ]
        ] = []
        final_audit: dict[str, Any] = {}
        final_owner: dict[str, Any] = {}
        findings: list[str] = []
        for attempt in range(max_repairs + 1):
            final_audit = quality_client.audit(
                corpus_id=corpus_id,
                title=item.title,
                script=item.transcript,
                objective=brief.objective,
                target_viewer=brief.audience,
            )
            findings, final_owner = quality_findings(
                item, final_audit, prior_texts=prior_texts
            )
            attempted_values.append((
                item.model_copy(deep=True),
                final_audit,
                final_owner,
                list(findings),
            ))
            attempt_receipts.append({
                "attempt": attempt,
                "script_sha256": sha256_text(item.transcript),
                "audit_id": final_audit.get("audit_id"),
                "audit_status": final_audit.get("status"),
                "overall_score": final_audit.get("overall_score"),
                "owner_decision": final_owner.get("decision"),
                "findings": findings,
            })
            if not findings:
                break
            if attempt == max_repairs:
                continue
            literal_repair = deterministic_owner_repair(
                item, final_owner, attempt=attempt + 1
            )
            if literal_repair.transcript != item.transcript:
                item = literal_repair
                attempt_receipts[-1]["repair_type"] = (
                    "canonical_literal_quality_repair"
                )
                continue
            item, repair_id = repair_transcript(
                ai_client,
                profile=profile,
                brief=brief,
                item=item,
                findings=findings,
                model=model,
            )
            attempt_receipts[-1]["repair_type"] = "structured_model_repair"
            attempt_receipts[-1]["repair_response_id"] = repair_id

        item, final_audit, final_owner, findings = max(
            attempted_values,
            key=lambda value: (
                not bool(value[3]),
                -len(value[2].get("failure_codes", [])),
                float(value[1].get("overall_score") or 0.0),
            ),
        )
        accepted = not findings
        experiment: dict[str, Any] | None = None
        if accepted:
            digest = sha256_text(item.transcript)
            script_id = (
                f"repo_bench_{profile.profile_id}_{brief.brief_id}_{digest[:12]}"
            )
            value = quality_client.register_experiment(
                brief_id=f"repo_benchmark_{brief.brief_id}",
                script_id=script_id,
                script_text=item.transcript,
                workflow_id=(
                    f"{BENCHMARK_CONTRACT}:{profile.profile_id}:{brief.brief_id}"
                ),
                metadata={
                    "profile_id": profile.profile_id,
                    "adapter_mode": profile.adapter_mode,
                    "native_status": profile.native_status,
                    "source_commit": profile.source_commit,
                    "audit_id": final_audit.get("audit_id"),
                    "outcomes_measured": False,
                },
            )
            experiment = value.get("experiment", value)
            prior_texts.append(item.transcript)
        completed.append({
            "brief_id": brief.brief_id,
            "accepted": accepted,
            "transcript": item.model_dump(mode="json"),
            "audit": final_audit,
            "owner_quality_within_batch": final_owner,
            "attempts": attempt_receipts,
            "experiment": experiment,
        })

    scores = [
        float(item["audit"].get("overall_score") or 0.0) for item in completed
    ]
    return {
        "contract": BENCHMARK_CONTRACT,
        "profile": profile.model_dump(mode="json"),
        "generation_lineage": lineage,
        "transcripts": completed,
        "summary": {
            "generated_count": len(completed),
            "accepted_count": sum(bool(item["accepted"]) for item in completed),
            "average_prepublication_quality": round(
                sum(scores) / max(1, len(scores)), 3
            ),
            "outcomes_measured": False,
        },
    }


def assemble_run(
    *,
    results: list[dict[str, Any]],
    checkout_receipts: list[dict[str, Any]],
    model: str,
    corpus_id: str,
    context_receipt: dict[str, Any] | None = None,
) -> dict[str, Any]:
    peer_summary = annotate_peer_overlaps(results)
    generated = sum(
        int(item["summary"]["generated_count"]) for item in results
    )
    accepted = sum(
        int(item["summary"]["accepted_count"]) for item in results
    )
    return {
        "contract": BENCHMARK_CONTRACT,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "model": model,
        "corpus_id": corpus_id,
        "context_receipt": context_receipt,
        "checkout_receipts": checkout_receipts,
        "results": results,
        "summary": {
            "profile_count": len(results),
            "generated_count": generated,
            "accepted_count": accepted,
            "outcomes_measured": False,
            "peer_overlap": peer_summary,
            "interpretation": (
                "Scores describe prepublication script checks only; they do not "
                "predict views, retention, leads, or revenue."
            ),
        },
    }


def render_markdown_report(run: dict[str, Any]) -> str:
    results = sorted(
        run["results"],
        key=lambda item: (
            -float(item["summary"]["average_prepublication_quality"]),
            str(item["profile"]["label"]).casefold(),
        ),
    )
    lines = [
        "# External content-repository transcript benchmark",
        "",
        f"Created: {run['created_at']}",
        "",
        "This is a clean-room method comparison. Raw upstream prompts, source "
        "assets, identities, likenesses, and voices were not generation inputs. "
        "Scores are prepublication quality checks, not predictions of audience "
        "outcomes.",
        "",
        "## Scorecard",
        "",
        "| Method profile | Native state | Adapter | License | Accepted | Mean quality |",
        "|---|---:|---:|---:|---:|---:|",
    ]
    for result in results:
        profile = result["profile"]
        summary = result["summary"]
        lines.append(
            "| {label} | {state} | {adapter} | {license} | {accepted}/3 | {score:.3f} |".format(
                label=profile["label"].replace("|", "\\|"),
                state=profile["native_status"],
                adapter=profile["adapter_mode"],
                license=profile["license_spdx"],
                accepted=summary["accepted_count"],
                score=float(summary["average_prepublication_quality"]),
            )
        )
    lines.extend([
        "",
        "Native state and transcript score answer different questions. A "
        "profile-adapted transcript can score well even when the upstream "
        "checkout is partial, unlicensed, or isolated from runtime use.",
        "",
        "## Transcripts and receipts",
        "",
    ])
    for result in results:
        profile = result["profile"]
        lines.extend([
            f"### {profile['label']}",
            "",
            f"- Repository: `{profile['repo_full_name']}`",
            f"- Native state: `{profile['native_status']}`",
            f"- Reuse policy: `{profile['reuse_policy']}`",
            f"- Adapter: `{profile['adapter_mode']}`",
            "",
            "Limitations: " + "; ".join(profile["limitations"]),
            "",
        ])
        for record in result["transcripts"]:
            transcript = record["transcript"]
            audit = record["audit"]
            copy_gate = audit.get("copy_gate") or {}
            peer_overlap = record.get("peer_overlap") or {}
            owner = record.get("owner_quality_within_batch") or {}
            lines.extend([
                f"#### {transcript['title']}",
                "",
                transcript["transcript"],
                "",
                (
                    "Receipt: brief `{brief}`; accepted `{accepted}`; words "
                    "`{words}`; quality `{score}`; source copy gate `{copy}`; "
                    "source findings `{source_findings}`; peer gate "
                    "`{peer_passed}`; peer findings `{peer_findings}`; "
                    "owner check `{owner}`; audit `{audit_id}`."
                ).format(
                    brief=record["brief_id"],
                    accepted=str(bool(record["accepted"])).lower(),
                    words=transcript["word_count"],
                    score=audit.get("overall_score"),
                    copy=copy_gate.get("passed"),
                    source_findings=", ".join(
                        copy_gate.get("failure_codes") or []
                    ) or "none",
                    peer_passed=peer_overlap.get("passed"),
                    peer_findings=", ".join(
                        (peer_overlap.get("copy_gate") or {}).get(
                            "failure_codes"
                        ) or []
                    ) or "none",
                    owner=owner.get("decision"),
                    audit_id=audit.get("audit_id"),
                ),
                "",
            ])
    lines.extend([
        "## Interpretation limits",
        "",
        "- The scripts used one shared model and three shared briefs so the method profile was the changed input.",
        "- Native install state is reported separately from profile-adapted writing quality.",
        "- Source and peer copy gates reject substantive expression, ordered passage, and source-specific structure; no fixed matching-word limit decides either gate.",
        "- No audience outcome has been observed for these scripts.",
        "- Any performance claim in an upstream project remains an upstream claim unless separately verified.",
        "- Publishing, source-asset reuse, identity imitation, and voice imitation were outside this run.",
        "",
    ])
    return "\n".join(lines)


def atomic_write_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temp_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
    )
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(value, handle, indent=2, sort_keys=True, ensure_ascii=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_name, path)
    except BaseException:
        Path(temp_name).unlink(missing_ok=True)
        raise


def write_artifacts(output_dir: Path, run: dict[str, Any]) -> dict[str, str]:
    output_dir.mkdir(parents=True, exist_ok=True)
    json_path = output_dir / "benchmark-results.json"
    report_path = output_dir / "benchmark-report.md"
    atomic_write_json(json_path, run)
    report_path.write_text(render_markdown_report(run), encoding="utf-8")
    return {"json": str(json_path), "report": str(report_path)}


def configured_ai_client() -> OpenAI:
    api_key = os.environ.get("OPENAI_API_KEY", "").strip()
    if not api_key:
        raise RuntimeError("OPENAI_API_KEY is required")
    return OpenAI(api_key=api_key)
