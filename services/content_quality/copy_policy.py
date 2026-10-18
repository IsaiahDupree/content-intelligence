"""Deterministic substantive-copy and byte-bound provenance policy.

No fixed number of matching words decides this gate. It evaluates complete
expression units, their order, and source-specific structure instead.
"""

from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime
from difflib import SequenceMatcher
from typing import Any, Iterable
from urllib.parse import urlsplit


COPY_GATE_CONTRACT = "substantive_copy_provenance_gate_v1"
PROVENANCE_CONTRACT = "content_copy_provenance_v1"
INDEPENDENT_VERIFICATION_CONTRACT = "independent_claim_verification_v1"

WORD_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9'’-]*")
UNIT_SPLIT_RE = re.compile(
    r"(?:\r?\n)+|(?<=[.!?;])\s+|\s+[—–]\s+|"
    r",\s+(?=(?:but|because|so|then|instead|while|after|before)\b)",
    re.IGNORECASE,
)
HEX_64_RE = re.compile(r"^[0-9a-f]{64}$")

STOP_WORDS = {
    "a", "an", "and", "are", "as", "at", "be", "because", "been", "but",
    "by", "do", "for", "from", "had", "has", "have", "he", "her", "hers",
    "him", "his", "i", "if", "in", "into", "is", "it", "its", "me", "my",
    "of", "on", "or", "our", "ours", "she", "so", "that", "the", "their",
    "theirs", "them", "then", "there", "these", "they", "this", "those",
    "to", "us", "was", "we", "were", "what", "when", "where", "which",
    "who", "why", "will", "with", "you", "your", "yours",
}
GENERIC_EXPRESSIONS = {
    "follow for more",
    "save this for later",
    "share this with someone",
    "here is how it works",
    "here is what happened",
    "let me know what you think",
}
ALLOWED_SOURCE_USAGE = {
    "none", "abstract_patterns_only", "facts_or_general_ideas_only",
}
ALLOWED_VERIFICATION_SOURCE_KINDS = {
    "official_source", "owner_first_party_measurement",
    "peer_reviewed_source", "primary_source",
}
FORBIDDEN_USE_FIELDS = (
    "creator_identity_used", "creator_likeness_used", "creator_voice_used",
    "source_clips_used",
)
FORBIDDEN_INPUT_FIELDS = (
    "creator_identity_input_ids", "creator_likeness_input_ids",
    "creator_voice_input_ids", "source_clip_ids",
)
PROVENANCE_FIELDS = {
    "contract", "candidate_sha256", "source_material_usage",
    "reference_item_ids", "creator_identity_used", "creator_likeness_used",
    "creator_voice_used", "source_clips_used", "creator_identity_input_ids",
    "creator_likeness_input_ids", "creator_voice_input_ids", "source_clip_ids",
    "independent_verification_receipts", "provenance_sha256",
}
VERIFICATION_RECEIPT_FIELDS = {
    "contract", "receipt_id", "claim", "claim_sha256", "source_url",
    "source_kind", "source_sha256", "verified_at", "receipt_sha256",
}


def sha256_text(value: str) -> str:
    return hashlib.sha256(str(value).encode("utf-8")).hexdigest()


def canonical_sha256(value: Any) -> str:
    encoded = json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=True,
        default=str,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _words(value: str) -> list[str]:
    return [word.casefold().replace("’", "'") for word in WORD_RE.findall(value)]


def _normalized(value: str) -> str:
    return " ".join(_words(value))


def _content_words(value: str) -> list[str]:
    return [word for word in _words(value) if word not in STOP_WORDS]


def _contains_token_sequence(container: str, value: str) -> bool:
    container_words = _words(container)
    value_words = _words(value)
    if not value_words or len(value_words) > len(container_words):
        return False
    return any(
        container_words[index:index + len(value_words)] == value_words
        for index in range(len(container_words) - len(value_words) + 1)
    )


def _structural_role(value: str) -> str:
    normalized = _normalized(value)
    tokens = set(_words(value))
    if "?" in value or normalized.startswith(("how ", "what ", "why ", "when ")):
        return "question_or_hook"
    if tokens & {"risk", "stuck", "problem", "wrong", "fails", "failure"}:
        return "tension"
    if tokens & {"but", "instead", "yet", "however"}:
        return "turn"
    if tokens & {"because", "means", "works", "happens", "causes"}:
        return "mechanism"
    if tokens & {"example", "evidence", "measured", "observed", "proof", "study"}:
        return "evidence"
    if tokens & {"result", "save", "clear", "fixed", "payoff", "finished"}:
        return "payoff"
    if tokens & {
        "ask", "check", "open", "reply", "send", "share", "start", "try",
        "use", "watch",
    }:
        return "action"
    return "exposition"


def _units(value: str) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for raw in UNIT_SPLIT_RE.split(str(value or "")):
        normalized = _normalized(raw)
        if normalized:
            result.append({
                "text": raw.strip(),
                "normalized": normalized,
                "tokens": _words(raw),
                "content": _content_words(raw),
                "role": _structural_role(raw),
            })
    return result


def _unit_similarity(left: dict[str, Any], right: dict[str, Any]) -> dict[str, float]:
    left_content = set(left["content"])
    right_content = set(right["content"])
    shared = left_content & right_content
    return {
        "sequence_similarity": SequenceMatcher(
            None, left["tokens"], right["tokens"], autojunk=False
        ).ratio(),
        "content_containment": len(shared) / max(
            1, min(len(left_content), len(right_content))
        ),
        "content_jaccard": len(shared) / max(1, len(left_content | right_content)),
    }


def _expression_match(
    left: dict[str, Any], right: dict[str, Any]
) -> dict[str, Any] | None:
    if (
        left["normalized"] in GENERIC_EXPRESSIONS
        or right["normalized"] in GENERIC_EXPRESSIONS
        or not left["content"]
        or not right["content"]
    ):
        return None
    similarity = _unit_similarity(left, right)
    exact = left["normalized"] == right["normalized"]
    near = (
        similarity["sequence_similarity"] >= 0.82
        and similarity["content_containment"] >= 0.75
        and similarity["content_jaccard"] >= 0.55
    )
    if not exact and not near:
        return None
    return {
        "exact": exact,
        **{key: round(value, 6) for key, value in similarity.items()},
    }


def _ordered_match_exists(matches: list[dict[str, Any]]) -> bool:
    ordered = sorted(
        matches, key=lambda row: (row["candidate_unit"], row["source_unit"])
    )
    return any(
        second["candidate_unit"] > first["candidate_unit"]
        and second["source_unit"] > first["source_unit"]
        for index, first in enumerate(ordered)
        for second in ordered[index + 1:]
    )


def _structure_match(
    candidate_units: list[dict[str, Any]], source_units: list[dict[str, Any]]
) -> dict[str, Any]:
    candidate_roles = [unit["role"] for unit in candidate_units]
    source_roles = [unit["role"] for unit in source_units]
    if len(candidate_roles) <= 1 or len(source_roles) <= 1:
        return {
            "copied": False,
            "role_sequence_similarity": 0.0,
            "content_vocabulary_similarity": 0.0,
        }
    role_matcher = SequenceMatcher(
        None, candidate_roles, source_roles, autojunk=False
    )
    role_similarity = sum(
        block.size for block in role_matcher.get_matching_blocks()
    ) / max(1, min(len(candidate_roles), len(source_roles)))
    candidate_content = {
        token for unit in candidate_units for token in unit["content"]
    }
    source_content = {token for unit in source_units for token in unit["content"]}
    vocabulary_similarity = len(candidate_content & source_content) / max(
        1, len(candidate_content | source_content)
    )
    return {
        "copied": role_similarity >= 0.90 and vocabulary_similarity >= 0.55,
        "role_sequence_similarity": round(role_similarity, 6),
        "content_vocabulary_similarity": round(vocabulary_similarity, 6),
    }


def build_independent_verification_receipt(
    *,
    receipt_id: str,
    claim: str,
    source_url: str,
    source_kind: str,
    source_sha256: str,
    verified_at: str,
) -> dict[str, Any]:
    """Build a self-hashing record for an independently checked claim."""

    core = {
        "contract": INDEPENDENT_VERIFICATION_CONTRACT,
        "receipt_id": str(receipt_id).strip(),
        "claim": str(claim).strip(),
        "claim_sha256": sha256_text(str(claim).strip()),
        "source_url": str(source_url).strip(),
        "source_kind": str(source_kind).strip(),
        "source_sha256": str(source_sha256).strip().lower(),
        "verified_at": str(verified_at).strip(),
    }
    return {**core, "receipt_sha256": canonical_sha256(core)}


def build_script_only_provenance(
    candidate: str,
    *,
    reference_item_ids: Iterable[str] = (),
    source_material_usage: str = "abstract_patterns_only",
    independent_verification_receipts: Iterable[dict[str, Any]] = (),
) -> dict[str, Any]:
    """Create an explicit script-only declaration bound to candidate bytes."""

    core = {
        "contract": PROVENANCE_CONTRACT,
        "candidate_sha256": sha256_text(candidate),
        "source_material_usage": source_material_usage,
        "reference_item_ids": sorted({
            str(value).strip() for value in reference_item_ids if str(value).strip()
        }),
        "creator_identity_used": False,
        "creator_likeness_used": False,
        "creator_voice_used": False,
        "source_clips_used": False,
        "creator_identity_input_ids": [],
        "creator_likeness_input_ids": [],
        "creator_voice_input_ids": [],
        "source_clip_ids": [],
        "independent_verification_receipts": list(
            independent_verification_receipts
        ),
    }
    return {**core, "provenance_sha256": canonical_sha256(core)}


def audit_provenance(
    candidate: str, provenance: dict[str, Any] | None
) -> dict[str, Any]:
    failures: list[str] = []
    candidate_digest = sha256_text(candidate)
    if not isinstance(provenance, dict):
        return {
            "passed": False,
            "failure_codes": ["MISSING_COPY_PROVENANCE"],
            "candidate_sha256": candidate_digest,
            "provenance_sha256": None,
            "source_material_usage": None,
            "independent_verification_receipt_ids": [],
        }
    if provenance.get("contract") != PROVENANCE_CONTRACT:
        failures.append("INVALID_COPY_PROVENANCE_CONTRACT")
    if set(provenance) != PROVENANCE_FIELDS:
        failures.append("COPY_PROVENANCE_FIELDS_INVALID")
    provenance_core = {
        key: value for key, value in provenance.items()
        if key != "provenance_sha256"
    }
    if str(provenance.get("provenance_sha256") or "").lower() != canonical_sha256(
        provenance_core
    ):
        failures.append("COPY_PROVENANCE_HASH_MISMATCH")
    if str(provenance.get("candidate_sha256") or "").lower() != candidate_digest:
        failures.append("COPY_PROVENANCE_CANDIDATE_HASH_MISMATCH")
    usage = str(provenance.get("source_material_usage") or "").strip()
    if usage not in ALLOWED_SOURCE_USAGE:
        failures.append("INVALID_SOURCE_MATERIAL_USAGE")
    for field in FORBIDDEN_USE_FIELDS:
        if provenance.get(field) is not False:
            failures.append(f"FORBIDDEN_{field.upper()}")
    for field in FORBIDDEN_INPUT_FIELDS:
        value = provenance.get(field)
        if not isinstance(value, list):
            failures.append(f"INVALID_{field.upper()}")
        elif value:
            failures.append(f"FORBIDDEN_{field.upper()}")
    reference_item_ids = provenance.get("reference_item_ids")
    if (
        not isinstance(reference_item_ids, list)
        or any(not isinstance(value, str) or not value.strip() for value in reference_item_ids)
    ):
        failures.append("INVALID_REFERENCE_ITEM_IDS")
    receipts = provenance.get("independent_verification_receipts")
    if not isinstance(receipts, list):
        failures.append("INVALID_INDEPENDENT_VERIFICATION_RECEIPTS")
        receipts = []
    valid_receipt_ids: list[str] = []
    for index, raw in enumerate(receipts):
        prefix = f"INDEPENDENT_VERIFICATION_{index}"
        failure_count_before = len(failures)
        if not isinstance(raw, dict):
            failures.append(f"{prefix}_INVALID")
            continue
        core = {key: value for key, value in raw.items() if key != "receipt_sha256"}
        if set(raw) != VERIFICATION_RECEIPT_FIELDS:
            failures.append(f"{prefix}_FIELDS_INVALID")
        if raw.get("contract") != INDEPENDENT_VERIFICATION_CONTRACT:
            failures.append(f"{prefix}_CONTRACT_INVALID")
        receipt_id = str(raw.get("receipt_id") or "").strip()
        if not receipt_id:
            failures.append(f"{prefix}_ID_MISSING")
        claim = str(raw.get("claim") or "").strip()
        if not claim or not _contains_token_sequence(candidate, claim):
            failures.append(f"{prefix}_CLAIM_NOT_BOUND_TO_CANDIDATE")
        if str(raw.get("claim_sha256") or "").lower() != sha256_text(claim):
            failures.append(f"{prefix}_CLAIM_HASH_MISMATCH")
        source_url = str(raw.get("source_url") or "").strip()
        parsed_source = urlsplit(source_url)
        if parsed_source.scheme not in {"http", "https"} or not parsed_source.netloc:
            failures.append(f"{prefix}_SOURCE_URL_INVALID")
        if raw.get("source_kind") not in ALLOWED_VERIFICATION_SOURCE_KINDS:
            failures.append(f"{prefix}_SOURCE_KIND_INVALID")
        if not HEX_64_RE.fullmatch(str(raw.get("source_sha256") or "").lower()):
            failures.append(f"{prefix}_SOURCE_HASH_INVALID")
        verified_at = str(raw.get("verified_at") or "").strip()
        try:
            parsed_time = datetime.fromisoformat(verified_at.replace("Z", "+00:00"))
        except ValueError:
            parsed_time = None
        if parsed_time is None or parsed_time.tzinfo is None:
            failures.append(f"{prefix}_VERIFIED_AT_INVALID")
        if str(raw.get("receipt_sha256") or "").lower() != canonical_sha256(core):
            failures.append(f"{prefix}_RECEIPT_HASH_MISMATCH")
        if receipt_id and len(failures) == failure_count_before:
            valid_receipt_ids.append(receipt_id)
    if usage == "facts_or_general_ideas_only" and not receipts:
        failures.append("INDEPENDENT_VERIFICATION_REQUIRED")
    return {
        "passed": not failures,
        "failure_codes": failures,
        "candidate_sha256": candidate_digest,
        "provenance_sha256": str(provenance.get("provenance_sha256") or "") or None,
        "source_material_usage": usage or None,
        "independent_verification_receipt_ids": valid_receipt_ids,
    }


def audit_substantive_copy(
    candidate: str,
    sources: Iterable[dict[str, Any]],
    *,
    provenance: dict[str, Any] | None,
) -> dict[str, Any]:
    """Reject copied expression, ordered expression, or source structure."""

    candidate_units = _units(candidate)
    source_rows = list(sources)
    source_findings: list[dict[str, Any]] = []
    creator_identity_references: list[dict[str, str]] = []
    copied_expression = False
    copied_sequence = False
    copied_structure = False
    maximum_expression_similarity = 0.0
    for source in source_rows:
        source_id = str(source.get("source_id") or source.get("item_id") or "")
        source_text = str(source.get("text") or "")
        for raw_identifier in source.get("creator_identifiers") or []:
            identifier = str(raw_identifier or "").strip().lstrip("@")
            if identifier and _contains_token_sequence(candidate, identifier):
                creator_identity_references.append({
                    "source_id": source_id,
                    "identifier": identifier,
                })
        source_units = _units(source_text)
        matches: list[dict[str, Any]] = []
        for candidate_index, candidate_unit in enumerate(candidate_units):
            for source_index, source_unit in enumerate(source_units):
                match = _expression_match(candidate_unit, source_unit)
                if match is None:
                    continue
                maximum_expression_similarity = max(
                    maximum_expression_similarity,
                    float(match["sequence_similarity"]),
                )
                matches.append({
                    "candidate_unit": candidate_index,
                    "source_unit": source_index,
                    "candidate_text": candidate_unit["text"],
                    "source_text": source_unit["text"],
                    **match,
                })
        ordered_copy = _ordered_match_exists(matches)
        structure = _structure_match(candidate_units, source_units)
        expression_copy = bool(matches)
        if expression_copy or ordered_copy or structure["copied"]:
            source_findings.append({
                "source_id": source_id,
                "source_sha256": sha256_text(source_text),
                "copied_expression": expression_copy,
                "copied_sequence": ordered_copy,
                "copied_structure": structure["copied"],
                "expression_matches": matches,
                "structure": structure,
            })
        copied_expression = copied_expression or expression_copy
        copied_sequence = copied_sequence or ordered_copy
        copied_structure = copied_structure or bool(structure["copied"])
    provenance_gate = audit_provenance(candidate, provenance)
    failure_codes: list[str] = []
    if copied_expression:
        failure_codes.append("COPIED_EXPRESSION")
    if copied_sequence:
        failure_codes.append("COPIED_SEQUENCE")
    if copied_structure:
        failure_codes.append("COPIED_STRUCTURE")
    if creator_identity_references:
        failure_codes.append("CREATOR_IDENTITY_REFERENCE")
    failure_codes.extend(provenance_gate["failure_codes"])
    return {
        "contract": COPY_GATE_CONTRACT,
        "passed": not failure_codes,
        "policy": {
            "fixed_matching_word_limit_applied": False,
            "copied_expression_allowed": False,
            "copied_sequence_allowed": False,
            "copied_structure_allowed": False,
            "facts_or_general_ideas_require_independent_verification": True,
            "creator_identity_likeness_voice_or_source_clips_allowed": False,
        },
        "failure_codes": failure_codes,
        "substantive_copy": {
            "copied_expression": copied_expression,
            "copied_sequence": copied_sequence,
            "copied_structure": copied_structure,
            "creator_identity_references": creator_identity_references,
            "maximum_expression_similarity": round(
                maximum_expression_similarity, 6
            ),
            "source_findings": source_findings,
        },
        "provenance_gate": provenance_gate,
    }
