"""Fail-closed admission for secondary spoken-script writers.

This module is intentionally a thin adapter around the canonical owner quality
contract.  It lets legacy writers share the same bounded audit/repair behavior
without creating another quality rubric.
"""

from __future__ import annotations

import re
from typing import Any, Iterable, Sequence

from services.content_quality.script_quality import (
    MAX_QUALITY_REWRITE_ATTEMPTS,
    arrange_role_components,
    audit_owner_calibrated_quality,
    build_delivery_visual_plan,
    owner_repair_actions,
    repair_timeline_for_owner_quality,
    retime_timeline,
    select_rhetorical_structure,
)


ADMISSION_CONTRACT = "secondary_spoken_script_admission_v1"
CLAIM_SAFETY_CONTRACT = "evidence_safe_claim_check_v1"

FIRST_PARTY_TOKEN_RE = re.compile(
    r"\b(?:i|i['’]m|i['’]ve|i['’]ll|me|my|mine|we|we['’]re|we['’]ve|"
    r"we['’]ll|us|our|ours)\b",
    re.IGNORECASE,
)
IDENTITY_OR_VOICE_RE = re.compile(
    r"\b(?:in the style of|sound(?:s|ing)? like|voice of|impersonat(?:e|ing)|"
    r"copy (?:their|his|her) voice)\b",
    re.IGNORECASE,
)
UNSUPPORTED_ABSOLUTE_RE = re.compile(
    r"\b(?:everyone|no one|nobody|most people)\b|\ball (?:the )?(?:comments|creators|"
    r"people)\b|\b(?:always|never) works\b",
    re.IGNORECASE,
)
NUMBER_RE = re.compile(r"(?<![A-Za-z])\d+(?:\.\d+)?%?")


def _remove_allowed_evidence(text: str, evidence_phrases: Iterable[str]) -> str:
    result = text
    phrases = {str(value).strip() for value in evidence_phrases if str(value).strip()}
    for phrase in sorted(phrases, key=len, reverse=True):
        result = re.sub(re.escape(phrase), " ", result, flags=re.IGNORECASE)
    return result


def audit_claim_safety(
    text: str,
    *,
    evidence_phrases: Iterable[str] = (),
) -> dict[str, Any]:
    """Reject common fabricated claims while allowing supplied evidence verbatim."""

    clean = " ".join(str(text or "").split()).strip()
    evidence = tuple(
        str(value).strip() for value in evidence_phrases if str(value).strip()
    )
    ungrounded = _remove_allowed_evidence(clean, evidence)
    evidence_numbers = set(NUMBER_RE.findall(" ".join(evidence)))
    unsupported_numbers = sorted(
        set(NUMBER_RE.findall(ungrounded)) - evidence_numbers
    )
    first_party = [match.group(0) for match in FIRST_PARTY_TOKEN_RE.finditer(ungrounded)]
    identity_or_voice = [match.group(0) for match in IDENTITY_OR_VOICE_RE.finditer(clean)]
    absolutes = [match.group(0) for match in UNSUPPORTED_ABSOLUTE_RE.finditer(ungrounded)]
    failures: list[str] = []
    if first_party:
        failures.append("UNSUPPORTED_FIRST_PARTY_ASSERTION")
    if unsupported_numbers:
        failures.append("UNSUPPORTED_NUMERIC_ASSERTION")
    if absolutes:
        failures.append("UNSUPPORTED_ABSOLUTE_ASSERTION")
    if identity_or_voice:
        failures.append("SOURCE_IDENTITY_OR_VOICE_REFERENCE")
    return {
        "contract": CLAIM_SAFETY_CONTRACT,
        "decision": "PASS" if clean and not failures else "REVISE",
        "failure_codes": failures or ([] if clean else ["EMPTY_TRANSCRIPT"]),
        "unsupported_first_party_assertions": first_party,
        "unsupported_numeric_assertions": unsupported_numbers,
        "unsupported_absolute_assertions": absolutes,
        "identity_or_voice_references": identity_or_voice,
        "evidence_phrase_count": len(evidence),
        "source_identity_likeness_or_voice_used": False,
    }


def _normalized_components(
    components: dict[str, Sequence[dict[str, Any]]],
) -> dict[str, list[dict[str, Any]]]:
    normalized: dict[str, list[dict[str, Any]]] = {}
    index = 0
    for role, values in components.items():
        for value in values:
            item = dict(value)
            index += 1
            item.setdefault("node_id", f"secondary_beat_{index}")
            item["quality_role"] = role
            item.setdefault("source_beat", item.get("beat") or role)
            item["beat"] = role
            item["text"] = " ".join(str(item.get("text") or "").split()).strip()
            if item["text"]:
                normalized.setdefault(role, []).append(item)
    return normalized


def _regroup_timeline(
    timeline: Sequence[dict[str, Any]],
) -> dict[str, list[dict[str, Any]]]:
    grouped: dict[str, list[dict[str, Any]]] = {}
    for value in timeline:
        item = dict(value)
        role = str(item.get("quality_role") or item.get("beat") or "context")
        grouped.setdefault(role, []).append(item)
    return grouped


def admit_spoken_components(
    components: dict[str, Sequence[dict[str, Any]]],
    *,
    family: str,
    seed: str,
    target_seconds: float,
    evidence_phrases: Iterable[str] = (),
    evidence_receipt_ids: Iterable[str] = (),
    evidence_receipts: Sequence[dict[str, Any]] = (),
    prior_texts: Iterable[str] = (),
    preferred_structure: str | None = None,
) -> dict[str, Any]:
    """Audit, locally repair, and either admit or explicitly block a script."""

    working = _normalized_components(components)
    evidence = tuple(
        str(value).strip() for value in evidence_phrases if str(value).strip()
    )
    requested_receipt_ids = tuple(
        str(value).strip() for value in evidence_receipt_ids if str(value).strip()
    )
    resolved_receipts = tuple(
        dict(item) for item in evidence_receipts
        if isinstance(item, dict)
        and item.get("receipt_resolved") is True
        and str(item.get("receipt_id") or "").strip()
        and str(item.get("exact_text") or "").strip()
    )
    receipt_texts = {
        str(item["exact_text"]).strip() for item in resolved_receipts
    }
    receipts = tuple(str(item["receipt_id"]).strip() for item in resolved_receipts)
    if evidence and (
        not resolved_receipts or any(value not in receipt_texts for value in evidence)
    ):
        raise ValueError(
            "evidence_phrases require receipt-resolved exact evidence records"
        )
    if requested_receipt_ids and set(requested_receipt_ids) != set(receipts):
        raise ValueError("evidence_receipt_ids must match resolved evidence receipts")
    prior = tuple(str(value) for value in prior_texts if str(value).strip())
    attempts: list[dict[str, Any]] = []
    timeline: list[dict[str, Any]] = []
    transcript = ""
    structure: dict[str, Any] = {}
    owner_quality: dict[str, Any] = {}
    claim_safety: dict[str, Any] = {}
    approved = False

    for attempt_index in range(MAX_QUALITY_REWRITE_ATTEMPTS):
        structure = select_rhetorical_structure(
            family,
            seed=seed,
            attempt=attempt_index,
            preferred=preferred_structure,
        )
        timeline = retime_timeline(
            arrange_role_components(working, structure),
            target_seconds=float(target_seconds),
        )
        transcript = " ".join(
            str(item.get("text") or "").strip() for item in timeline
        ).strip()
        initial_quality = audit_owner_calibrated_quality(
            transcript,
            timeline=timeline,
            protected_phrases=evidence,
            prior_texts=prior,
        )
        repair_actions = owner_repair_actions(initial_quality)
        repaired_timeline = repair_timeline_for_owner_quality(
            timeline,
            initial_quality,
            protected_phrases=evidence,
            attempt=attempt_index + 1,
            target_seconds=float(target_seconds),
        )
        repair_applied = repaired_timeline != timeline
        timeline = repaired_timeline
        transcript = " ".join(
            str(item.get("text") or "").strip() for item in timeline
        ).strip()
        owner_quality = audit_owner_calibrated_quality(
            transcript,
            timeline=timeline,
            protected_phrases=evidence,
            prior_texts=prior,
        )
        claim_safety = audit_claim_safety(
            transcript,
            evidence_phrases=evidence,
        )
        approved = (
            owner_quality["decision"] == "PASS"
            and claim_safety["decision"] == "PASS"
        )
        attempts.append({
            "attempt": attempt_index + 1,
            "structure_id": structure["structure_id"],
            "initial_owner_failure_codes": initial_quality["failure_codes"],
            "repair_actions": repair_actions,
            "local_repair_applied": repair_applied,
            "owner_decision": owner_quality["decision"],
            "owner_failure_codes": owner_quality["failure_codes"],
            "claim_safety_decision": claim_safety["decision"],
            "claim_safety_failure_codes": claim_safety["failure_codes"],
            "approved": approved,
        })
        if approved:
            break
        working = _regroup_timeline(timeline)

    blocking_failures = list(owner_quality.get("failure_codes") or ())
    blocking_failures.extend(claim_safety.get("failure_codes") or ())
    delivery_visual_plan = build_delivery_visual_plan(
        timeline,
        structure_id=str(structure.get("structure_id") or "unselected"),
    )
    duration = max(
        (
            float(item.get("end_seconds", item.get("end", 0.0)) or 0.0)
            for item in timeline
        ),
        default=0.0,
    )
    interrupt_times = [0.0]
    cursor = 3.0
    while cursor < duration:
        interrupt_times.append(round(cursor, 3))
        cursor += 3.0
    if duration and interrupt_times[-1] != round(duration, 3):
        interrupt_times.append(round(duration, 3))
    delivery_visual_plan.update({
        "maximum_interrupt_gap_seconds": 3.0,
        "interrupt_schedule": [
            {
                "at_seconds": value,
                "mode": "owned_visual_or_framing_shift",
                "reference_clip_allowed": False,
                "reference_identity_or_voice_allowed": False,
            }
            for value in interrupt_times
        ] if duration else [],
    })
    return {
        "contract": ADMISSION_CONTRACT,
        "status": "ready" if approved else "blocked_quality",
        "block_reason": None if approved else "bounded_quality_rewrite_exhausted",
        "blocking_failure_codes": list(dict.fromkeys(blocking_failures)),
        "transcript": transcript,
        "timeline": timeline,
        "rhetorical_structure": structure,
        "owner_quality": owner_quality,
        "claim_safety": claim_safety,
        "delivery_visual_plan": delivery_visual_plan,
        "revision": {
            "contract": "bounded_script_quality_rewrite_v1",
            "maximum_attempts": MAX_QUALITY_REWRITE_ATTEMPTS,
            "attempt_count": len(attempts),
            "exhausted": not approved and len(attempts) == MAX_QUALITY_REWRITE_ATTEMPTS,
            "attempts": attempts,
        },
        "rights": {
            "source_clips_used": False,
            "source_identity_likeness_or_voice_used": False,
            "owned_or_licensed_assets_required": True,
        },
        "evidence_receipt_ids": list(receipts),
    }


__all__ = [
    "ADMISSION_CONTRACT",
    "CLAIM_SAFETY_CONTRACT",
    "admit_spoken_components",
    "audit_claim_safety",
]
