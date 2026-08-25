"""Narrative Coherence Audit — timeline-order sense-making for generated scripts.

Owner directive 2026-08-22: the transcript/script generation service must be
audited so that the context behind the transcript, as things are mentioned in
timeline fashion, makes sense as presented to the audience. A script that is
sourced correctly in the backend (complaint data, transcript cohorts, receipts)
but never voices that context in the video timeline — or voices it AFTER the
claims that depend on it — is incoherent to a cold viewer and must not ship.

Enforcement model (owner decision 2026-08-22): block with an auto-revise loop.
Deterministic rule defects are repaired deterministically and re-audited, up to
a bounded number of attempts; on exhaustion the script is rejected fail-closed.
Once the rules pass, an LLM cold-viewer judgment runs; an incoherent verdict or
an unavailable judge also rejects fail-closed (there is no deterministic repair
for a judgment-level defect in v1).
"""
from __future__ import annotations

import hashlib
import json
import re
import subprocess
from typing import Any, Callable

MAX_REPAIR_ATTEMPTS = 3
CONTEXT_BEAT_NAME = "evidence_context"
CONTEXT_BEAT_SECONDS = 4.0
EVIDENCE_DEPENDENT_BEATS = {"claim", "proof"}

# Beat text that voices where the evidence comes from, in-timeline.
ATTRIBUTION_RE = re.compile(
    r"\b(?:this comes from|based on|according to|we (?:looked at|heard|read|studied)|"
    r"\d+\s+(?:creators?|videos?|posts?|complaints?|comments?|threads?|reviews?|stories)|"
    r"reddit|forums?|comment sections?|creator videos?)\b",
    re.IGNORECASE,
)

# "these stories", "those complaints", "that pattern" — demonstratives whose
# noun must already have been introduced in an earlier beat.
# Captures an optional adjective between the demonstrative and its noun
# ("these fancy tools" → adjective "fancy", noun "tools"): the referent check
# passes when EITHER word has an antecedent, so adjectives never false-flag.
DEMONSTRATIVE_RE = re.compile(r"\b(these|those)\s+([a-z]+)(?:\s+([a-z]+))?\b", re.IGNORECASE)

_JSON_RE = re.compile(r"\{.*\}", re.DOTALL)


def _stable_id(prefix: str, *parts: Any) -> str:
    digest = hashlib.sha256(json.dumps(parts, sort_keys=True, default=str).encode()).hexdigest()
    return f"{prefix}-{digest[:16]}"


def _noun_variants(token: str) -> set[str]:
    """Forms under which an antecedent for this noun counts as introduced."""
    token = token.lower()
    variants = {token}
    if token.endswith("ies") and len(token) > 4:
        variants.add(token[:-3] + "y")
    elif token.endswith("s") and len(token) > 3:
        variants.add(token[:-1])
    else:
        variants.add(token + "s")
    return variants


def _beat_texts_before(timeline: list[dict[str, Any]], index: int) -> str:
    return " ".join(str(item.get("text") or "") for item in timeline[:index]).lower()


def _has_evidence(evidence_summary: dict[str, Any] | None) -> bool:
    return bool(evidence_summary) and any(
        int(evidence_summary.get(key) or 0) > 0
        for key in ("viral_transcript_patterns", "creator_count", "observed_views_snapshot")
    )


def rules_audit(timeline: list[dict[str, Any]], evidence_summary: dict[str, Any] | None) -> list[dict[str, Any]]:
    """Deterministic timeline-coherence defects, in viewer order."""
    defects: list[dict[str, Any]] = []
    if not timeline:
        return [{"code": "EMPTY_TIMELINE", "detail": "no beats to audit"}]

    # R1 — continuity: monotonic, contiguous, starts at zero.
    previous_end = 0.0
    for index, item in enumerate(timeline):
        start, end = float(item.get("start") or 0), float(item.get("end") or 0)
        if start >= end or abs(start - previous_end) > 0.05:
            defects.append({
                "code": "TIMELINE_DISCONTINUITY", "beat_index": index,
                "detail": f"beat {index} spans {start}-{end}; previous beat ended at {previous_end}",
            })
        previous_end = max(previous_end, end)

    # R2 — dangling referent: a demonstrative pointing at something never introduced.
    for index, item in enumerate(timeline):
        text = str(item.get("text") or "")
        earlier = _beat_texts_before(timeline, index)
        for match in DEMONSTRATIVE_RE.finditer(text):
            candidates = [w.lower() for w in (match.group(2), match.group(3)) if w]
            resolved = any(variant in earlier
                           for word in candidates
                           for variant in _noun_variants(word))
            if candidates and not resolved:
                noun = candidates[-1]
                defects.append({
                    "code": "DANGLING_REFERENT", "beat_index": index, "referent": match.group(0),
                    "noun": noun,
                    "detail": f"beat {index} says '{match.group(0)}' but no earlier beat introduces '{noun}'",
                })

    # R3 — evidence context must be voiced in-timeline, before the beats that lean on it.
    if _has_evidence(evidence_summary):
        attribution_index = next(
            (index for index, item in enumerate(timeline)
             if item.get("beat") == CONTEXT_BEAT_NAME or ATTRIBUTION_RE.search(str(item.get("text") or ""))),
            None,
        )
        dependent_index = next(
            (index for index, item in enumerate(timeline) if item.get("beat") in EVIDENCE_DEPENDENT_BEATS),
            None,
        )
        if attribution_index is None:
            defects.append({
                "code": "EVIDENCE_NEVER_VOICED",
                "detail": "backend evidence exists but no beat tells the audience where it comes from",
            })
        elif dependent_index is not None and attribution_index > dependent_index:
            defects.append({
                "code": "CONTEXT_AFTER_DEPENDENT_CLAIM", "beat_index": attribution_index,
                "detail": f"evidence context is voiced at beat {attribution_index}, after the claim/proof at beat {dependent_index}",
            })

    # R4 — order grammar: open on the hook, close on the CTA, never open with the claim.
    beats = [str(item.get("beat") or "") for item in timeline]
    if beats and beats[0] in EVIDENCE_DEPENDENT_BEATS:
        defects.append({"code": "CLAIM_BEFORE_SETUP", "beat_index": 0,
                        "detail": "the timeline opens on a claim/proof with no setup"})
    if "cta" in beats and beats[-1] != "cta":
        defects.append({"code": "CTA_NOT_LAST", "beat_index": beats.index("cta"),
                        "detail": "the call to action is not the final beat"})
    return defects


def _retime(timeline: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Preserve each beat's duration; recompute contiguous starts from zero."""
    cursor, retimed = 0.0, []
    for item in timeline:
        duration = max(0.5, float(item.get("end") or 0) - float(item.get("start") or 0))
        retimed.append({**item, "start": round(cursor, 2), "end": round(cursor + duration, 2)})
        cursor += duration
    return retimed


def _context_beat_text(evidence_summary: dict[str, Any], dangling_nouns: list[str]) -> str:
    """Voice the backend evidence to the audience, introducing any dangling nouns."""
    patterns = int(evidence_summary.get("viral_transcript_patterns") or 0)
    creators = int(evidence_summary.get("creator_count") or 0)
    terms = [str(term) for term in evidence_summary.get("recurring_human_terms") or []][:3]
    # Use the raw dangling words verbatim so the antecedent match is exact.
    noun_phrase = " and ".join(sorted({noun.lower() for noun in dangling_nouns if noun})) or "stories"
    parts = [f"Quick context: this comes from {patterns or 'several'} creator videos"]
    if creators:
        parts.append(f"— real {noun_phrase} from {creators} different creators")
    else:
        parts.append(f"— real {noun_phrase} people shared")
    if terms:
        parts.append(f"— and the same complaints keep coming up: {', '.join(terms)}")
    return " ".join(parts) + "."


def repair(script: dict[str, Any], defects: list[dict[str, Any]]) -> dict[str, Any]:
    """Deterministic repairs for rule defects. Returns a new script dict."""
    timeline = [dict(item) for item in script.get("timeline") or []]
    codes = {item["code"] for item in defects}
    needs_context = codes & {"EVIDENCE_NEVER_VOICED", "CONTEXT_AFTER_DEPENDENT_CLAIM", "DANGLING_REFERENT"}
    # Never fabricate a source-context beat for a script with no backend evidence:
    # the repair may only voice evidence that actually exists. Without evidence,
    # these defects have no deterministic repair and the loop must fail closed.
    if (needs_context and _has_evidence(script.get("evidence_summary"))
            and not any(item.get("beat") == CONTEXT_BEAT_NAME for item in timeline)):
        dangling = [item.get("noun", "") for item in defects if item["code"] == "DANGLING_REFERENT"]
        text = _context_beat_text(script.get("evidence_summary") or {}, dangling)
        insert_at = next(
            (index for index, item in enumerate(timeline) if item.get("beat") in EVIDENCE_DEPENDENT_BEATS),
            min(1, len(timeline)),
        )
        timeline.insert(insert_at, {
            "start": 0.0, "end": CONTEXT_BEAT_SECONDS, "beat": CONTEXT_BEAT_NAME, "text": text,
        })
    if "CTA_NOT_LAST" in codes:
        cta = [item for item in timeline if item.get("beat") == "cta"]
        timeline = [item for item in timeline if item.get("beat") != "cta"] + cta
    timeline = _retime(timeline)
    revised = {**script, "timeline": timeline,
               "text": " ".join(str(item.get("text") or "") for item in timeline)}
    return revised


def default_llm_runner(prompt: str, timeout_seconds: int = 120) -> str:
    """Cold-viewer judgment via the local claude CLI. Raises on any failure."""
    completed = subprocess.run(
        ["/opt/homebrew/bin/claude", "-p", prompt],
        capture_output=True, text=True, timeout=timeout_seconds, check=True,
        stdin=subprocess.DEVNULL,
    )
    return completed.stdout


def openai_llm_runner(prompt: str, timeout_seconds: int = 90) -> str:
    """Cold-viewer judgment via the OpenAI API. Raises on any failure.

    Owner decision 2026-08-22: the judge runs on OpenAI (the local claude CLI
    had no usable credential). Key comes from OPENAI_API_KEY — sourced by the
    service entrypoint from the ContentIntelligence runtime env, never stored
    in source control. Model override: NARRATIVE_JUDGE_MODEL.
    """
    import os
    import urllib.error
    import urllib.request

    api_key = os.environ.get("OPENAI_API_KEY") or ""
    if not api_key or api_key.startswith("__"):
        raise RuntimeError("OPENAI_API_KEY is missing or a scrubbed placeholder")
    model = os.environ.get("NARRATIVE_JUDGE_MODEL", "gpt-5-nano")
    body_payload = {
        "model": model,
        "messages": [{"role": "user", "content": prompt}],
        # GPT-5-family Chat Completions uses max_completion_tokens and only
        # supports its default temperature. Keeping the request minimal also
        # preserves compatibility with the configured low-cost judge model.
        "max_completion_tokens": 300,
        "response_format": {
            "type": "json_schema",
            "json_schema": {
                "name": "narrative_coherence_verdict",
                "strict": True,
                "schema": {
                    "type": "object",
                    "properties": {
                        "coherent": {"type": "boolean"},
                        "issues": {
                            "type": "array",
                            "items": {"type": "string"},
                        },
                    },
                    "required": ["coherent", "issues"],
                    "additionalProperties": False,
                },
            },
        },
    }
    if model.startswith("gpt-5"):
        body_payload["reasoning_effort"] = "minimal"
    body = json.dumps(body_payload).encode()
    base_url = os.environ.get(
        "OPENAI_API_BASE_URL", "https://api.openai.com/v1"
    ).rstrip("/")
    request = urllib.request.Request(
        f"{base_url}/chat/completions", data=body,
        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout_seconds) as response:
            payload = json.loads(response.read().decode())
    except urllib.error.HTTPError as exc:
        try:
            error = (json.loads(exc.read().decode()).get("error") or {})
        except (UnicodeDecodeError, json.JSONDecodeError, AttributeError):
            error = {}
        # Never carry response messages into the audit ledger: authentication
        # errors can echo a masked key suffix. Code/type/parameter are enough
        # for safe, actionable classification.
        raise RuntimeError(
            "OpenAI API request failed "
            f"http={exc.code} type={error.get('type') or 'unknown'} "
            f"code={error.get('code') or 'unknown'} "
            f"param={error.get('param') or 'none'}"
        ) from exc
    return str(payload["choices"][0]["message"]["content"])


def _judge_prompt(timeline: list[dict[str, Any]]) -> str:
    beats = "\n".join(
        f"{index + 1}. [{item.get('start')}s-{item.get('end')}s] ({item.get('beat')}) {item.get('text')}"
        for index, item in enumerate(timeline)
    )
    return (
        "You are auditing a short-form social video script for narrative coherence. Judge it ONLY "
        "as a first-time viewer who hears the beats strictly in this order, with no backend "
        "knowledge. Short-form conventions apply: compression is normal, sources are summarized in "
        "a sentence, and beats are only a few seconds each.\n\n"
        f"{beats}\n\n"
        "Mark it INCOHERENT only if a viewer could not follow the logical thread: a beat refers to "
        "specific things ('these stories', 'that number') never mentioned earlier, evidence is "
        "leaned on but never introduced at all, or the order is a non-sequitur (the payoff or proof "
        "of something never claimed). Do NOT fail it for brevity, missing biographical detail about "
        "sources, unnamed creators, weak transitions, or stylistic preferences — those are normal "
        "in this format. The bar is 'can a viewer follow what is being said and why', not 'is this "
        "excellent'. When in doubt between coherent and incoherent, answer coherent and list your "
        "concern in issues. "
        'Respond with ONLY this JSON, nothing else: {"coherent": true|false, "issues": ["..."]}'
    )


class NarrativeCoherenceService:
    """Blocking audit with a bounded deterministic auto-revise loop.

    llm_runner: Callable[[str], str] | None. None disables the judgment pass
    (rules still enforce); the production entrypoint wires the claude CLI.
    """

    def __init__(self, store: Any, llm_runner: Callable[[str], str] | None = None):
        self.store = store
        self.llm_runner = llm_runner

    def audit(self, payload: dict[str, Any]) -> dict[str, Any]:
        """One-shot audit of an arbitrary {timeline, evidence_summary} payload."""
        timeline = payload.get("timeline") or []
        defects = rules_audit(timeline, payload.get("evidence_summary"))
        judgment = None
        if not defects and self.llm_runner is not None:
            judgment = self._judge(timeline)
        decision = self._decision(defects, judgment)
        subject_id = payload.get("script_id")
        record = self.store.put_audit(
            "narrative_coherence", subject_id and str(subject_id), decision,
            100.0 if decision == "PASS" else 0.0,
            {"defects": defects, "llm_judgment": judgment, "attempts": 1},
        )
        return record

    def enforce(self, script: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
        """Audit → repair → re-audit loop. Returns (final_script, outcome).

        outcome: {decision, attempts: [...], defects_open, llm_judgment}.
        decision PASS means the returned script is the (possibly revised) one
        to persist; anything else must be rejected fail-closed by the caller.
        """
        current, attempts = script, []
        for attempt in range(1, MAX_REPAIR_ATTEMPTS + 1):
            defects = rules_audit(current.get("timeline") or [], current.get("evidence_summary"))
            attempts.append({"attempt": attempt, "defects": defects,
                            "repaired": bool(defects) and attempt < MAX_REPAIR_ATTEMPTS})
            if not defects:
                break
            if attempt == MAX_REPAIR_ATTEMPTS:
                return current, {"decision": "FAIL_RULES", "attempts": attempts,
                                 "defects_open": defects, "llm_judgment": None}
            current = repair(current, defects)
        judgment = self._judge(current.get("timeline") or []) if self.llm_runner else None
        decision = self._decision([], judgment)
        return current, {"decision": decision, "attempts": attempts,
                         "defects_open": [], "llm_judgment": judgment}

    def _judge(self, timeline: list[dict[str, Any]]) -> dict[str, Any]:
        try:
            raw = self.llm_runner(_judge_prompt(timeline))
            match = _JSON_RE.search(raw or "")
            verdict = json.loads(match.group(0)) if match else None
            if not isinstance(verdict, dict) or not isinstance(verdict.get("coherent"), bool):
                return {"status": "unavailable", "error": "judge returned no parseable verdict"}
            return {"status": "ok", "coherent": verdict["coherent"],
                    "issues": [str(item) for item in verdict.get("issues") or []]}
        except Exception as exc:  # fail closed: an unreachable judge is not a pass
            return {"status": "unavailable", "error": f"{type(exc).__name__}: {exc}"}

    @staticmethod
    def _decision(defects: list[dict[str, Any]], judgment: dict[str, Any] | None) -> str:
        if defects:
            return "FAIL_RULES"
        if judgment is None:
            return "PASS"
        if judgment.get("status") != "ok":
            return "JUDGE_UNAVAILABLE"
        return "PASS" if judgment.get("coherent") else "FAIL_JUDGMENT"
