"""Shared evidence-safe structure selection and owner-calibrated script quality."""

from __future__ import annotations

import hashlib
import math
import re
from collections import Counter
from typing import Any, Iterable, Sequence


OWNER_QUALITY_CONTRACT = "owner_calibrated_script_quality_v1"
RHETORICAL_STRUCTURE_CONTRACT = "evidence_safe_rhetorical_structure_v1"
DELIVERY_VISUAL_PLAN_CONTRACT = "delivery_visual_plan_v1"
MAX_QUALITY_REWRITE_ATTEMPTS = 3
OWNER_QUALITY_PASS_SCORE = 80.0

WORD_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9'’-]*")
SENTENCE_RE = re.compile(r"[^.!?]+[.!?]?")

# Calibrated from the owner's 2026-08-24 rejected-candidate receipt and V5
# profile. Topic vocabulary is allowed; these terms target internal production
# narration that asks the listener to translate implementation language.
HARD_INTERNAL_PHRASES = (
    "content machine", "production decisions", "source ids", "matrix row",
    "script receipt", "production settings", "output receipt", "handoff form",
    "typed contract", "workflow boundary", "routing rules", "destination action",
    "completion receipt", "case id", "visible state", "recovery path",
    "known input", "provider configuration",
)
TECHNICAL_TERMS = {
    "attribution", "configuration", "control", "documentation", "handoff",
    "infrastructure", "lineage", "mechanism", "orchestration", "pipeline",
    "production", "provenance", "queue", "receipt", "routing", "schema",
    "state", "system", "timestamp", "workflow",
}
FORMULA_PHRASES = (
    "the visible result is",
    "that is the first workflow to map",
    "that is the first one to map",
    "it keeps coming back to the same friction",
    "you are in the middle of product work when",
)
FORMAL_PHRASES = (
    "it is important to note", "in order to", "therefore", "furthermore",
    "moreover", "utilize", "implementation of",
)
CORPUS_COUNT_NARRATION_RE = re.compile(
    r"\b(?:across|reviewed|analyzed|studied|looked at|heard in)\s+"
    r"(?:\d+|several|many)\s+(?:public\s+)?(?:creator\s+)?"
    r"(?:videos?|transcripts?|sources?|posts?)\b|"
    r"\b(?:the|our)\s+(?:corpus|analysis|dataset)\s+(?:shows?|found)\b",
    re.IGNORECASE,
)
CONCRETE_ACTION_VERBS = {
    "adjust", "answer", "approve", "ask", "book", "build", "call", "change",
    "check", "choose", "close", "compare", "connect", "copy", "cut", "decide",
    "draft", "edit", "explain", "find", "finish", "get", "give", "hear",
    "install", "listen", "load", "look", "lower", "map", "measure", "meet",
    "message", "move", "open", "pay", "paste", "pick", "play", "post",
    "prepare", "press", "publish", "rank", "read", "record", "reply", "rewrite",
    "save", "schedule", "score", "send", "share", "show", "sit", "speak",
    "start", "stop", "test", "text", "turn", "upload", "use", "wait", "write",
}
CONCRETE_NOUNS = {
    "account", "answer", "app", "buyer", "calendar", "call", "client",
    "customer", "day", "demo", "email", "form", "inbox", "invoice", "lead",
    "line", "meeting", "message", "minute", "page", "person", "product",
    "question", "quote", "reply", "request", "result", "task", "team", "tool",
    "video", "week", "workday",
}
TENSION_TERMS = {
    "bottleneck", "burned", "burnout", "cold", "cost", "delay", "exhausted",
    "fail", "failed", "failing", "friction", "hard", "harder", "ignored",
    "interrupt", "interrupted", "interrupting", "mistake", "mistakes", "missing",
    "overwhelmed", "pressure", "problem", "risk", "stuck", "stop", "stops",
    "wait", "waiting", "waits", "worse", "wrong",
}
PAYOFF_TERMS = {
    "answer", "booked", "clear", "complete", "completed", "easier", "finish",
    "finished", "fix", "fixed", "proof", "reduce", "removes", "resolve",
    "result", "save", "saved", "sent", "works",
}
CONTRAST_TERMS = {"but", "except", "instead", "least", "more", "without", "yet"}
GRAM_STOP_WORDS = {
    "a", "an", "and", "are", "as", "at", "be", "for", "from", "in", "is",
    "it", "of", "on", "or", "that", "the", "this", "to", "was", "with",
    "you", "your",
}
FIRST_PERSON_WORDS = {
    "i", "i'm", "i've", "me", "my", "mine", "we", "we've", "our", "ours",
}

STRUCTURE_PLANS: dict[str, tuple[tuple[str, tuple[str, ...]], ...]] = {
    "reference_marketing": (
        ("contrast_reveal", ("hook", "problem", "stakes", "proof", "reframe", "steps", "takeaway", "cta")),
        ("stakes_then_method", ("hook", "stakes", "problem", "reframe", "steps", "proof", "takeaway", "cta")),
        ("proof_bridge", ("hook", "problem", "proof", "stakes", "reframe", "steps", "takeaway", "cta")),
        ("myth_turn", ("hook", "problem", "reframe", "proof", "stakes", "steps", "takeaway", "cta")),
    ),
    "evidence_story": (
        ("tension_proof_action", ("hook", "stakes", "context", "proof", "claim", "method", "payoff", "cta")),
        ("tension_action_proof", ("hook", "stakes", "context", "proof", "method", "claim", "payoff", "cta")),
        ("proof_then_turn", ("hook", "context", "proof", "stakes", "claim", "method", "payoff", "cta")),
        ("demonstrate_then_explain", ("hook", "method", "context", "proof", "stakes", "claim", "payoff", "cta")),
    ),
}
ANGLE_STRUCTURE = {
    "contrast": "contrast_reveal",
    "problem_first": "stakes_then_method",
    "how_to": "proof_bridge",
    "myth": "myth_turn",
}

ROLE_DIRECTIONS = {
    "hook": ("fast_clear", "direct_to_camera", "opening_text_emphasis"),
    "human_hook": ("fast_clear", "direct_to_camera", "opening_text_emphasis"),
    "problem": ("conversational", "closer_crop", "friction_detail"),
    "stakes": ("measured_emphasis", "closer_crop", "consequence_shift"),
    "context": ("brief_grounded", "owned_evidence_card", "evidence_source_shift"),
    "evidence_context": ("brief_grounded", "owned_evidence_card", "evidence_source_shift"),
    "proof": ("confident", "owned_example_or_demo", "proof_shift"),
    "claim": ("plain_emphasis", "direct_to_camera", "claim_text_shift"),
    "reframe": ("slower_turn", "direct_to_camera", "perspective_shift"),
    "method": ("brisk_instruction", "owned_screen_demo", "action_shift"),
    "teaching_step": ("brisk_instruction", "owned_screen_demo", "action_shift"),
    "payoff": ("settled", "owned_result_view", "result_shift"),
    "takeaway": ("settled", "direct_to_camera", "summary_shift"),
    "call_to_action": ("direct", "direct_to_camera", "cta_shift"),
    "cta": ("direct", "direct_to_camera", "cta_shift"),
}


def words(text: str) -> list[str]:
    return [token.casefold().replace("’", "'") for token in WORD_RE.findall(text or "")]


def select_rhetorical_structure(
    family: str,
    *,
    seed: str,
    attempt: int = 0,
    preferred: str | None = None,
) -> dict[str, Any]:
    """Choose a stable structure, rotating on bounded rewrite attempts."""

    plans = STRUCTURE_PLANS.get(family)
    if not plans:
        raise ValueError(f"unknown rhetorical structure family: {family}")
    names = [name for name, _order in plans]
    if preferred in names:
        base_index = names.index(str(preferred))
    else:
        digest = hashlib.sha256(str(seed).encode("utf-8")).hexdigest()
        base_index = int(digest[:8], 16) % len(plans)
    index = (base_index + max(0, int(attempt))) % len(plans)
    name, order = plans[index]
    return {
        "contract": RHETORICAL_STRUCTURE_CONTRACT,
        "family": family,
        "structure_id": name,
        "role_order": list(order),
        "selection": (
            "preferred_then_bounded_rotation"
            if preferred else "stable_seed_then_bounded_rotation"
        ),
        "attempt": max(0, int(attempt)),
        "source_text_modified": False,
    }

def arrange_role_components(
    components: dict[str, Sequence[dict[str, Any]]],
    structure: dict[str, Any],
) -> list[dict[str, Any]]:
    """Arrange every component exactly once and keep the CTA last."""

    arranged: list[dict[str, Any]] = []
    consumed: set[str] = set()
    for role in structure["role_order"]:
        if role == "cta":
            continue
        values = [dict(item) for item in components.get(role, ())]
        if values:
            arranged.extend(values)
            consumed.add(role)
    arranged.extend(
        dict(item)
        for role, values in components.items()
        if role not in consumed and role != "cta"
        for item in values
    )
    ctas = [dict(item) for item in components.get("cta", ())]
    arranged.extend(ctas)
    node_ids = [
        str(item.get("node_id") or item.get("beat") or index)
        for index, item in enumerate(arranged)
    ]
    if len(node_ids) != len(set(node_ids)):
        raise ValueError("rhetorical components must have unique node IDs")
    if ctas and arranged[-1] not in ctas:
        raise ValueError("call to action must remain last")
    return arranged


def build_delivery_visual_plan(
    timeline: Sequence[dict[str, Any]],
    *,
    structure_id: str,
) -> dict[str, Any]:
    """Create a timed plan without claiming that any production asset exists."""

    maximum_gap_seconds = 3.0
    cues: list[dict[str, Any]] = []
    for index, item in enumerate(timeline):
        role = str(item.get("block") or item.get("beat") or "beat")
        delivery, visual, interrupt = ROLE_DIRECTIONS.get(
            role, ("conversational", "owned_supporting_visual", "semantic_shift")
        )
        start = float(item.get("start_seconds", item.get("start", 0.0)) or 0.0)
        end = float(item.get("end_seconds", item.get("end", start)) or start)
        duration = max(0.0, end - start)
        reset_count = max(1, math.ceil(duration / maximum_gap_seconds))
        for reset_index in range(reset_count):
            cue_start = start + duration * reset_index / reset_count
            cue_end = (
                end
                if reset_index == reset_count - 1
                else start + duration * (reset_index + 1) / reset_count
            )
            cues.append({
                "cue_id": f"cue_{len(cues) + 1}",
                "beat_index": index,
                "reset_index": reset_index,
                "role": role,
                "start_seconds": round(cue_start, 3),
                "end_seconds": round(cue_end, 3),
                "delivery": {
                    "pace_and_tone": delivery,
                    "pause_before": (
                        reset_index == 0
                        and index > 0
                        and role in {
                            "proof", "claim", "reframe", "payoff", "takeaway",
                            "cta", "call_to_action",
                        }
                    ),
                },
                "visual": {
                    "mode": visual,
                    "interrupt": (
                        interrupt if reset_index == 0 else "intra_beat_reset"
                    ),
                    "asset_status": "not_selected",
                    "text_source": "approved_original_script_beat",
                    "reference_clip_allowed": False,
                    "reference_identity_or_voice_allowed": False,
                },
            })
    actual_gap = max(
        (
            float(item["end_seconds"]) - float(item["start_seconds"])
            for item in cues
        ),
        default=0.0,
    )
    return {
        "contract": DELIVERY_VISUAL_PLAN_CONTRACT,
        "structure_id": structure_id,
        "beat_count": len(timeline),
        "cue_count": len(cues),
        "maximum_visual_interrupt_gap_seconds": maximum_gap_seconds,
        "actual_maximum_visual_interrupt_gap_seconds": round(actual_gap, 3),
        "cues": cues,
        "asset_policy": {
            "owned_or_licensed_assets_required": True,
            "reference_clips_used": False,
            "reference_identity_likeness_or_voice_used": False,
        },
    }


def _sentences(text: str) -> list[list[str]]:
    return [
        words(match.group(0))
        for match in SENTENCE_RE.finditer(text)
        if words(match.group(0))
    ]


def _remove_protected(text: str, protected_phrases: Iterable[str]) -> str:
    result = text
    values = {str(value).strip() for value in protected_phrases if str(value).strip()}
    for phrase in sorted(values, key=len, reverse=True):
        result = re.sub(re.escape(phrase), " ", result, flags=re.IGNORECASE)
    return result


def _token_positions(tokens: Sequence[str], vocabulary: set[str]) -> list[int]:
    return [index for index, token in enumerate(tokens) if token in vocabulary]


def _action_hits(tokens: Sequence[str]) -> list[str]:
    hits: list[str] = []
    for token in tokens:
        forms = {token}
        if token.endswith("ing") and len(token) > 5:
            forms.update((token[:-3], token[:-3] + "e"))
        if token.endswith("ed") and len(token) > 4:
            forms.update((token[:-2], token[:-1]))
        if token.endswith("s") and len(token) > 3:
            forms.add(token[:-1])
        if forms & CONCRETE_ACTION_VERBS:
            hits.append(token)
    return hits


def _repeated_ngrams(tokens: Sequence[str], size: int = 4) -> list[dict[str, Any]]:
    counts: Counter[tuple[str, ...]] = Counter()
    for index in range(max(0, len(tokens) - size + 1)):
        gram = tuple(tokens[index:index + size])
        if sum(token not in GRAM_STOP_WORDS for token in gram) < 2:
            continue
        counts[gram] += 1
    return [
        {"phrase": " ".join(gram), "count": count}
        for gram, count in counts.most_common(8)
        if count > 1
    ]


def audit_owner_calibrated_quality(
    text: str,
    *,
    timeline: Sequence[dict[str, Any]] | None = None,
    protected_phrases: Iterable[str] = (),
    prior_texts: Iterable[str] = (),
) -> dict[str, Any]:
    """Return five explicit, owner-calibrated deterministic judgments."""

    clean = " ".join(str(text or "").split()).strip()
    tokens = words(clean)
    sentence_tokens = _sentences(clean)
    sentence_lengths = [len(value) for value in sentence_tokens]
    average_sentence_words = (
        sum(sentence_lengths) / len(sentence_lengths) if sentence_lengths else 0.0
    )
    long_sentence_count = sum(length > 24 for length in sentence_lengths)
    long_sentence_fraction = long_sentence_count / max(1, len(sentence_lengths))
    formal_hits = [phrase for phrase in FORMAL_PHRASES if phrase in clean.casefold()]
    personal_tokens = sorted(
        set(tokens) & (FIRST_PERSON_WORDS | {"you", "your"})
    )
    spoken_score = max(
        0.0,
        100.0
        - max(0.0, average_sentence_words - 14.0) * 3.0
        - long_sentence_fraction * 35.0
        - len(formal_hits) * 20.0,
    )
    spoken_pass = (
        bool(sentence_tokens)
        and average_sentence_words <= 18.0
        and max(sentence_lengths, default=0) <= 32
        and long_sentence_fraction <= 0.25
        and not formal_hits
    )

    action_hits = _action_hits(tokens)
    noun_hits = [token for token in tokens if token in CONCRETE_NOUNS]
    numeric_hits = re.findall(r"(?<![A-Za-z])\d+(?:\.\d+)?%?", clean)
    specificity_score = min(
        100.0,
        25.0
        + min(5, len(set(action_hits))) * 9.0
        + min(4, len(set(noun_hits))) * 7.5
        + min(2, len(numeric_hits)) * 7.5,
    )
    specificity_pass = len(set(action_hits)) >= 3 and (
        len(set(noun_hits)) >= 2 or bool(numeric_hits)
    )

    tension_positions = _token_positions(tokens, TENSION_TERMS)
    payoff_positions = _token_positions(tokens, PAYOFF_TERMS)
    ordered_lexical_turn = bool(
        tension_positions
        and payoff_positions
        and tension_positions[0] < payoff_positions[-1]
    )
    roles = [
        str(item.get("block") or item.get("beat") or "").casefold()
        for item in (timeline or ())
    ]
    tension_role_indexes = [
        index for index, role in enumerate(roles) if role in {"problem", "stakes"}
    ]
    payoff_role_indexes = [
        index
        for index, role in enumerate(roles)
        if role in {"payoff", "takeaway", "reframe"}
    ]
    ordered_role_turn = bool(
        tension_role_indexes
        and payoff_role_indexes
        and tension_role_indexes[0] < payoff_role_indexes[-1]
    )
    contrast_hits = sorted(set(tokens) & CONTRAST_TERMS)
    tension_score = min(
        100.0,
        (35.0 if ordered_lexical_turn or ordered_role_turn else 0.0)
        + min(3, len(set(tokens) & TENSION_TERMS)) * 10.0
        + min(3, len(set(tokens) & PAYOFF_TERMS)) * 8.0
        + min(2, len(contrast_hits)) * 5.5,
    )
    tension_pass = (
        bool(tension_positions and payoff_positions)
        and ordered_lexical_turn
        and tension_score >= 70.0
    )

    protected_values = tuple(
        str(value).strip() for value in protected_phrases if str(value).strip()
    )
    unprotected = _remove_protected(clean, protected_values)
    unprotected_lower = unprotected.casefold()
    hard_internal_hits = [
        phrase for phrase in HARD_INTERNAL_PHRASES if phrase in unprotected_lower
    ]
    technical_hits = [token for token in words(unprotected) if token in TECHNICAL_TERMS]
    corpus_count_narration_hits = [
        match.group(0) for match in CORPUS_COUNT_NARRATION_RE.finditer(clean)
    ]
    technical_score = max(
        0.0,
        100.0
        - len(hard_internal_hits) * 40.0
        - len(corpus_count_narration_hits) * 40.0
        - max(0, len(technical_hits) - 2) * 15.0,
    )
    technical_pass = (
        not hard_internal_hits
        and not corpus_count_narration_hits
        and len(technical_hits) <= 2
    )

    repeated_ngrams = _repeated_ngrams(tokens)
    formula_hits = [phrase for phrase in FORMULA_PHRASES if phrase in clean.casefold()]
    opening = tuple(tokens[:6])
    prior_opening_matches = sum(
        1
        for prior in prior_texts
        if tuple(words(str(prior))[:6]) == opening
        and opening
    )
    repetition_score = max(
        0.0,
        100.0
        - len(formula_hits) * 25.0
        - len(repeated_ngrams) * 12.0
        - prior_opening_matches * 20.0,
    )
    repetition_pass = (
        not formula_hits
        and len(repeated_ngrams) <= 1
        and prior_opening_matches == 0
    )

    judgments = {
        "spoken_naturalness": {
            "passed": spoken_pass,
            "score": round(spoken_score, 3),
            "average_sentence_words": round(average_sentence_words, 3),
            "maximum_sentence_words": max(sentence_lengths, default=0),
            "long_sentence_fraction": round(long_sentence_fraction, 6),
            "formal_phrase_hits": formal_hits,
            "first_or_second_person_tokens": personal_tokens,
            "perspective_authorization_evaluated": False,
            "perspective_authorization_required_elsewhere": bool(
                set(personal_tokens) & FIRST_PERSON_WORDS
            ),
        },
        "specificity": {
            "passed": specificity_pass,
            "score": round(specificity_score, 3),
            "concrete_action_hits": sorted(set(action_hits)),
            "concrete_noun_hits": sorted(set(noun_hits)),
            "numeric_anchors": numeric_hits,
        },
        "tension_payoff": {
            "passed": tension_pass,
            "score": round(tension_score, 3),
            "tension_terms": sorted(set(tokens) & TENSION_TERMS),
            "payoff_terms": sorted(set(tokens) & PAYOFF_TERMS),
            "contrast_terms": contrast_hits,
            "ordered_lexical_turn": ordered_lexical_turn,
            "ordered_role_turn": ordered_role_turn,
        },
        "technical_language_leakage": {
            "passed": technical_pass,
            "score": round(technical_score, 3),
            "hard_internal_phrase_hits": hard_internal_hits,
            "corpus_count_narration_hits": corpus_count_narration_hits,
            "technical_term_hits": technical_hits,
            "protected_source_phrases_excluded": len(protected_values),
        },
        "repeated_phrasing": {
            "passed": repetition_pass,
            "score": round(repetition_score, 3),
            "formula_phrase_hits": formula_hits,
            "repeated_four_word_phrases": repeated_ngrams,
            "prior_opening_matches": prior_opening_matches,
            "opening_prefix": " ".join(opening),
        },
    }
    score = round(
        judgments["spoken_naturalness"]["score"] * 0.22
        + judgments["specificity"]["score"] * 0.20
        + judgments["tension_payoff"]["score"] * 0.23
        + judgments["technical_language_leakage"]["score"] * 0.20
        + judgments["repeated_phrasing"]["score"] * 0.15,
        3,
    )
    failed = [
        name for name, judgment in judgments.items() if not judgment["passed"]
    ]
    decision = (
        "PASS"
        if not failed and score >= OWNER_QUALITY_PASS_SCORE
        else "REVISE"
    )
    return {
        "contract": OWNER_QUALITY_CONTRACT,
        "decision": decision,
        "score": score,
        "threshold": OWNER_QUALITY_PASS_SCORE,
        "judgments": judgments,
        "failure_codes": [f"OWNER_{name.upper()}" for name in failed],
        "calibration": {
            "authority": "owner_review_2026_08_24",
            "outcomes_measured": False,
            "reference_identity_or_voice_used": False,
        },
    }


def owner_repair_actions(report: dict[str, Any]) -> list[str]:
    failures = set(report.get("failure_codes") or ())
    actions: list[str] = []
    if "OWNER_TECHNICAL_LANGUAGE_LEAKAGE" in failures:
        actions.append("plain_language_substitution")
    if "OWNER_REPEATED_PHRASING" in failures:
        actions.extend(("formula_phrase_substitution", "transition_rotation"))
    if "OWNER_SPOKEN_NATURALNESS" in failures:
        actions.append("clause_split")
    if failures & {"OWNER_SPECIFICITY", "OWNER_TENSION_PAYOFF"}:
        actions.append("structure_rotation")
    return actions


TECHNICAL_REPLACEMENTS = {
    "attribution": "credit",
    "configuration": "settings",
    "documentation": "notes",
    "handoff": "step",
    "infrastructure": "tools",
    "lineage": "source",
    "mechanism": "reason",
    "orchestration": "coordination",
    "pipeline": "process",
    "production": "making it",
    "provenance": "source",
    "queue": "list",
    "receipt": "record",
    "routing": "sending",
    "schema": "fields",
    "state": "result",
    "system": "tool",
    "timestamp": "time",
    "workflow": "task",
}
PHRASE_REPLACEMENTS = {
    "content machine": "content process",
    "production decisions": "choices",
    "source ids": "source links",
    "matrix row": "one comparison",
    "script receipt": "script record",
    "production settings": "setup choices",
    "output receipt": "saved result",
    "handoff form": "request form",
    "typed contract": "clear format",
    "workflow boundary": "handoff point",
    "routing rules": "sending rules",
    "destination action": "next step",
    "completion receipt": "saved result",
    "case id": "record number",
    "visible state": "visible result",
    "recovery path": "way back",
    "known input": "starting input",
    "provider configuration": "provider settings",
    "the visible result is": "Now you have",
    "that is the first workflow to map": "start there",
    "that is the first one to map": "start there",
    "it keeps coming back to the same friction": "The same snag keeps showing up",
    "you are in the middle of product work when": "A software founder is building when",
}


def _protect_text(
    text: str, protected_phrases: Iterable[str]
) -> tuple[str, dict[str, str]]:
    protected: dict[str, str] = {}
    result = text
    values = {
        str(value).strip() for value in protected_phrases if str(value).strip()
    }
    for index, phrase in enumerate(sorted(values, key=len, reverse=True)):
        marker = f"ZZPROTECTED{index}ZZ"
        result, count = re.subn(
            re.escape(phrase), marker, result, flags=re.IGNORECASE
        )
        if count:
            protected[marker] = phrase
    return result, protected


def _restore_text(text: str, protected: dict[str, str]) -> str:
    result = text
    for marker, phrase in protected.items():
        result = result.replace(marker, phrase)
    return result


def repair_owner_quality_text(
    text: str,
    report: dict[str, Any],
    *,
    protected_phrases: Iterable[str] = (),
    attempt: int = 1,
) -> str:
    """Apply literal edits without inventing evidence or altering protected text."""

    result, protected = _protect_text(str(text or ""), protected_phrases)
    failures = set(report.get("failure_codes") or ())
    if "OWNER_TECHNICAL_LANGUAGE_LEAKAGE" in failures:
        for phrase, replacement in PHRASE_REPLACEMENTS.items():
            result = re.sub(
                re.escape(phrase), replacement, result, flags=re.IGNORECASE
            )
        for term, replacement in TECHNICAL_REPLACEMENTS.items():
            result = re.sub(
                rf"\b{re.escape(term)}s?\b",
                replacement,
                result,
                flags=re.IGNORECASE,
            )
    if "OWNER_REPEATED_PHRASING" in failures:
        for phrase, replacement in PHRASE_REPLACEMENTS.items():
            result = re.sub(
                re.escape(phrase), replacement, result, flags=re.IGNORECASE
            )
        rotations = (
            (("For example,", "Look at this:"), ("First,", "Start here:")),
            (("For example,", "Here is the test:"), ("First,", "Try this:")),
        )
        chosen = rotations[(max(1, attempt) - 1) % len(rotations)]
        for old, new in chosen:
            result = result.replace(old, new, 1)
    if "OWNER_SPOKEN_NATURALNESS" in failures:
        result = re.sub(r";\s+", ". ", result)
        result = re.sub(
            r",\s+(but|yet)\s+", r". \1 ", result, flags=re.IGNORECASE
        )
    result = _restore_text(result, protected)
    result = re.sub(r"\s+", " ", result).strip()
    return re.sub(r"\s+([,.!?;:])", r"\1", result)


def retime_timeline(
    timeline: Sequence[dict[str, Any]],
    *,
    target_seconds: float,
) -> list[dict[str, Any]]:
    """Retime beats by a blend of word share and equal screen-time share."""

    beats = [dict(item) for item in timeline]
    if not beats:
        return []
    counts = [max(1, len(words(str(item.get("text") or "")))) for item in beats]
    total_words = sum(counts)
    equal_share = 1.0 / len(beats)
    shares = [
        0.65 * (count / total_words) + 0.35 * equal_share for count in counts
    ]
    cursor = 0.0
    result: list[dict[str, Any]] = []
    engine_shape = (
        "beat" in beats[0]
        or "start" in beats[0]
        or "end" in beats[0]
    ) and "block" not in beats[0]
    if engine_shape and len(shares) > 1:
        hook_share = min(shares[0], 3.0 / max(3.0, float(target_seconds)))
        remaining_share = sum(shares[1:])
        shares = [hook_share] + [
            share * (1.0 - hook_share) / remaining_share
            for share in shares[1:]
        ]
    for index, (item, share, count) in enumerate(zip(beats, shares, counts)):
        end = (
            float(target_seconds)
            if index == len(beats) - 1
            else cursor + target_seconds * share
        )
        if engine_shape:
            item.update({"start": round(cursor, 3), "end": round(end, 3)})
        else:
            item.update({
                "start_seconds": round(cursor, 3),
                "end_seconds": round(end, 3),
                "word_count": count,
            })
        result.append(item)
        cursor = end
    return result


def repair_timeline_for_owner_quality(
    timeline: Sequence[dict[str, Any]],
    report: dict[str, Any],
    *,
    protected_phrases: Iterable[str] = (),
    attempt: int = 1,
    target_seconds: float,
) -> list[dict[str, Any]]:
    repaired = [
        {
            **dict(item),
            "text": repair_owner_quality_text(
                str(item.get("text") or ""),
                report,
                protected_phrases=protected_phrases,
                attempt=attempt,
            ),
        }
        for item in timeline
    ]
    return retime_timeline(repaired, target_seconds=target_seconds)


__all__ = [
    "ANGLE_STRUCTURE",
    "DELIVERY_VISUAL_PLAN_CONTRACT",
    "MAX_QUALITY_REWRITE_ATTEMPTS",
    "OWNER_QUALITY_CONTRACT",
    "RHETORICAL_STRUCTURE_CONTRACT",
    "arrange_role_components",
    "audit_owner_calibrated_quality",
    "build_delivery_visual_plan",
    "owner_repair_actions",
    "repair_owner_quality_text",
    "repair_timeline_for_owner_quality",
    "retime_timeline",
    "select_rhetorical_structure",
]
