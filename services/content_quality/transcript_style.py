"""Aggregate transcript style guides backed by qualified evidence."""

from __future__ import annotations

import hashlib
import json
import math
import re
import sqlite3
import statistics
from collections import Counter, defaultdict
from contextlib import closing
from typing import Any, Sequence

from .contracts import is_supported_transcript_audit_contract
from .copy_policy import audit_substantive_copy


STYLE_GUIDE_CONTRACT = "aggregate_transcript_style_guide_v1"
STYLE_AUDIT_CONTRACT = "aggregate_transcript_style_fit_audit_v1"
MINIMUM_TRANSCRIPTS = 5
MINIMUM_CREATORS = 3
MINIMUM_OBSERVED_VIEWS = 100_000
WORD_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9'-]*")
SENTENCE_RE = re.compile(r"(?<=[.!?])\s+")
FIRST_PERSON = {"i", "i'm", "i've", "i'd", "i'll", "me", "my", "mine"}
SECOND_PERSON = {"you", "you're", "you've", "you'd", "you'll", "your", "yours"}
CONTRACTIONS = {
    "aren't", "can't", "couldn't", "didn't", "doesn't", "don't", "hadn't",
    "hasn't", "haven't", "here's", "i'd", "i'll", "i'm", "i've", "isn't",
    "it's", "let's", "shouldn't", "that's", "there's", "they're", "they've",
    "wasn't", "we'd", "we'll", "we're", "we've", "weren't", "what's",
    "won't", "wouldn't", "you'd", "you'll", "you're", "you've",
}
DISCOURSE_MARKERS = {
    "actually", "basically", "honestly", "here's", "like", "now", "okay",
    "right", "seriously", "so", "the thing is", "you know",
}
STRUCTURE_BEATS = {"hook", "human_problem", "evidence", "method", "payoff"}


def _words(value: str) -> list[str]:
    return [token.lower() for token in WORD_RE.findall(value or "")]


def _sentences(value: str) -> list[str]:
    return [item.strip() for item in SENTENCE_RE.split(value or "") if item.strip()]


def _ratio(numerator: float, denominator: float) -> float:
    return round(float(numerator) / float(denominator), 6) if denominator else 0.0


def _percentile(values: Sequence[float], fraction: float) -> float | None:
    ordered = sorted(float(value) for value in values if math.isfinite(float(value)))
    if not ordered:
        return None
    if len(ordered) == 1:
        return ordered[0]
    position = (len(ordered) - 1) * fraction
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return ordered[lower]
    return ordered[lower] + (ordered[upper] - ordered[lower]) * (position - lower)


def _observed_range(values: Sequence[float]) -> dict[str, float | None]:
    cleaned = [float(value) for value in values if math.isfinite(float(value))]
    if not cleaned:
        return {"low": None, "median": None, "high": None}
    median = float(statistics.median(cleaned))
    low = float(_percentile(cleaned, 0.20) or 0.0)
    high = float(_percentile(cleaned, 0.80) or 0.0)
    span = max(high - low, abs(median) * 0.15, 0.05)
    return {
        "low": round(max(0.0, low - span * 0.5), 4),
        "median": round(median, 4),
        "high": round(high + span * 0.5, 4),
    }


def _hook_shape(text: str) -> str:
    lowered = text.lower().strip()
    if "?" in text or lowered.startswith(("what", "why", "how", "do you", "have you")):
        return "question"
    if any(token in lowered for token in ("i tried", "i spent", "i made", "i lost", "i was")):
        return "personal_receipt"
    if any(token in lowered for token in ("stop", "don't", "never", "mistake", "wrong")):
        return "contrarian_warning"
    if any(char.isdigit() for char in text):
        return "specific_result"
    return "direct_claim"


def _structure(text: str) -> list[str]:
    lowered = text.lower()
    result = ["hook"]
    if any(token in lowered for token in ("struggle", "problem", "mistake", "hard", "stuck", "tired")):
        result.append("human_problem")
    if any(token in lowered for token in ("because", "data", "proof", "result", "tested", "show")):
        result.append("evidence")
    if any(token in lowered for token in ("first", "second", "here's how", "step", "do this")):
        result.append("method")
    result.append("payoff")
    return list(dict.fromkeys(result))


def _marker_counts(text: str) -> Counter[str]:
    lowered = text.lower()
    tokens = _words(lowered)
    counts: Counter[str] = Counter()
    for marker in DISCOURSE_MARKERS:
        if " " in marker:
            counts[marker] = len(re.findall(rf"\b{re.escape(marker)}\b", lowered))
        else:
            counts[marker] = tokens.count(marker)
    return counts


def _text_metrics(text: str, duration_seconds: float | None = None) -> dict[str, Any]:
    tokens = _words(text)
    sentences = _sentences(text)
    sentence_lengths = [len(_words(sentence)) for sentence in sentences] or [len(tokens)]
    marker_counts = _marker_counts(text)
    opening = sentences[0] if sentences else text
    return {
        "word_count": len(tokens),
        "sentence_count": len(sentences) or (1 if tokens else 0),
        "mean_sentence_words": round(statistics.mean(sentence_lengths), 4),
        "short_sentence_ratio": _ratio(
            sum(value <= 8 for value in sentence_lengths), len(sentence_lengths)
        ),
        "question_sentence_ratio": _ratio(
            sum("?" in sentence for sentence in sentences), len(sentences)
        ),
        "exclamation_sentence_ratio": _ratio(
            sum("!" in sentence for sentence in sentences), len(sentences)
        ),
        "first_person_per_100_words": round(
            100 * _ratio(sum(token in FIRST_PERSON for token in tokens), len(tokens)), 4
        ),
        "second_person_per_100_words": round(
            100 * _ratio(sum(token in SECOND_PERSON for token in tokens), len(tokens)), 4
        ),
        "contractions_per_100_words": round(
            100 * _ratio(sum(token in CONTRACTIONS for token in tokens), len(tokens)), 4
        ),
        "discourse_markers_per_100_words": round(
            100 * _ratio(sum(marker_counts.values()), len(tokens)), 4
        ),
        "hook_shape": _hook_shape(opening),
        "structure": _structure(text),
        "words_per_second": (
            round(len(tokens) / float(duration_seconds), 4)
            if duration_seconds and duration_seconds > 0 else None
        ),
        "duration_seconds": round(float(duration_seconds), 4) if duration_seconds else None,
        "marker_counts": dict(marker_counts),
    }


def _in_range(value: float, target: dict[str, Any], tolerance: float = 0.0) -> bool:
    low = target.get("low")
    high = target.get("high")
    if low is None or high is None:
        return True
    width = max(float(high) - float(low), 0.05)
    return float(low) - width * tolerance <= value <= float(high) + width * tolerance


class TranscriptStyleGuideService:
    """Build, persist, resolve, and audit aggregate transcript style guides."""

    def __init__(self, tape: Any, store: Any):
        self.tape = tape
        self.store = store

    @staticmethod
    def _verified_pattern(receipt: dict[str, Any]) -> bool:
        if receipt.get("receipt_type") != "viral_transcript_pattern":
            return False
        payload = receipt.get("payload") or {}
        qualification = payload.get("performance_qualification") or {}
        return bool(
            qualification.get("audit_decision") == "PASS"
            and is_supported_transcript_audit_contract(
                qualification.get("audit_contract")
            )
            and payload.get("transcript_id")
            and payload.get("video_id")
            and payload.get("creator_id")
        )

    def _source_receipts(
        self,
        receipt_ids: Sequence[str] | None,
        platform: str,
    ) -> tuple[list[dict[str, Any]], list[str]]:
        requested = [str(value) for value in receipt_ids or [] if str(value)]
        receipts = (
            self.store.receipts(requested, limit=500)
            if requested else self.store.receipts(
                limit=500, receipt_type="viral_transcript_pattern"
            )
        )
        unknown = sorted(set(requested) - {row["receipt_id"] for row in receipts})
        filtered: list[dict[str, Any]] = []
        seen: set[tuple[str, str]] = set()
        for receipt in receipts:
            if not self._verified_pattern(receipt):
                continue
            payload = receipt["payload"]
            source_platform = str(
                payload.get("platform") or receipt.get("source_type") or ""
            ).lower()
            if platform not in {"", "any", "cross_platform"} and source_platform != platform:
                continue
            identity = (
                str(payload.get("transcript_id") or ""),
                str(payload.get("observation_key") or ""),
            )
            if identity in seen:
                continue
            seen.add(identity)
            filtered.append(receipt)
        return filtered, unknown

    def build(self, payload: dict[str, Any]) -> dict[str, Any]:
        if not isinstance(payload, dict):
            raise ValueError("JSON body must be an object")
        topic = str(payload.get("topic") or "").strip()
        platform = str(payload.get("platform") or "tiktok").strip().lower()
        minimum_transcripts = max(
            MINIMUM_TRANSCRIPTS,
            min(50, int(payload.get("minimum_transcripts") or MINIMUM_TRANSCRIPTS)),
        )
        minimum_creators = max(
            MINIMUM_CREATORS,
            min(20, int(payload.get("minimum_creators") or MINIMUM_CREATORS)),
        )
        minimum_views = max(
            MINIMUM_OBSERVED_VIEWS,
            int(payload.get("minimum_observed_views") or MINIMUM_OBSERVED_VIEWS),
        )
        receipts, unknown = self._source_receipts(payload.get("receipt_ids"), platform)
        if unknown:
            return {
                "status": "rejected",
                "code": "UNKNOWN_TRANSCRIPT_RECEIPTS",
                "unknown_receipt_ids": unknown,
            }
        creators = {
            str(row["payload"].get("creator_id")) for row in receipts
            if row["payload"].get("creator_id")
        }
        observed_views = sum(
            int((row["payload"].get("pattern") or {}).get(
                "source_metrics", {}
            ).get("views") or 0)
            for row in receipts
        )
        gates = {
            "minimum_verified_transcripts": {
                "actual": len(receipts), "minimum": minimum_transcripts,
                "pass": len(receipts) >= minimum_transcripts,
            },
            "minimum_distinct_creators": {
                "actual": len(creators), "minimum": minimum_creators,
                "pass": len(creators) >= minimum_creators,
            },
            "minimum_observed_views": {
                "actual": observed_views, "minimum": minimum_views,
                "pass": observed_views >= minimum_views,
            },
        }
        failed = [name for name, gate in gates.items() if not gate["pass"]]
        if failed:
            return {
                "status": "rejected",
                "code": (
                    "INSUFFICIENT_TIKTOK_STYLE_EVIDENCE"
                    if platform == "tiktok" else "INSUFFICIENT_STYLE_EVIDENCE"
                ),
                "platform": platform,
                "topic": topic or None,
                "gates": gates,
                "failed_gates": failed,
                "acquisition": {
                    "service": "market-tape",
                    "method": "POST",
                    "path": "/api/market-tape/full-pipeline",
                    "recommended_parameters": {
                        "platforms": [platform]
                        if platform not in {"any", "cross_platform"}
                        else ["tiktok"],
                        "topic": topic or "creator problems",
                        "limit": max(12, minimum_transcripts * 2),
                        "performance_discovery": True,
                    },
                },
            }

        video_ids = [str(row["payload"]["video_id"]) for row in receipts]
        tape_rows = {
            str(row["video_id"]): row
            for row in self.tape.artifact_bound_candidates(video_ids)
        }
        documents: list[dict[str, Any]] = []
        integrity_failures: list[dict[str, str]] = []
        for receipt in receipts:
            source = receipt["payload"]
            video_id = str(source["video_id"])
            row = tape_rows.get(video_id)
            text = str((row or {}).get("transcript") or "").strip()
            expected_text_hash = str(
                (source.get("pattern") or {}).get("transcript_sha256")
                or source.get("transcript_sha256")
                or ""
            )
            actual_text_hash = (
                hashlib.sha256(text.encode("utf-8")).hexdigest() if text else ""
            )
            if not row or not text or actual_text_hash != expected_text_hash:
                integrity_failures.append({
                    "receipt_id": receipt["receipt_id"],
                    "video_id": video_id,
                    "error": "bound_market_tape_transcript_hash_mismatch",
                })
                continue
            duration = (source.get("pattern") or {}).get("duration_seconds")
            documents.append({
                "receipt": receipt,
                "text": text,
                "metrics": _text_metrics(
                    text, float(duration) if duration else None
                ),
            })
        gates["bound_transcript_integrity"] = {
            "actual": len(documents),
            "minimum": minimum_transcripts,
            "pass": len(documents) >= minimum_transcripts,
            "failures": integrity_failures,
        }
        if not gates["bound_transcript_integrity"]["pass"]:
            return {
                "status": "rejected",
                "code": "STYLE_SOURCE_INTEGRITY_FAILED",
                "platform": platform,
                "topic": topic or None,
                "gates": gates,
            }

        metric_names = (
            "mean_sentence_words", "short_sentence_ratio",
            "question_sentence_ratio", "exclamation_sentence_ratio",
            "first_person_per_100_words", "second_person_per_100_words",
            "contractions_per_100_words", "discourse_markers_per_100_words",
            "words_per_second", "duration_seconds",
        )
        distributions: dict[str, list[float]] = {
            name: [
                float(document["metrics"][name]) for document in documents
                if document["metrics"].get(name) is not None
            ]
            for name in metric_names
        }
        hook_shapes = Counter(
            str(document["metrics"]["hook_shape"]) for document in documents
        )
        structure_counts = Counter(
            beat
            for document in documents
            for beat in document["metrics"]["structure"]
            if beat in STRUCTURE_BEATS
        )
        marker_creators: dict[str, set[str]] = defaultdict(set)
        vocabulary_creators: dict[str, set[str]] = defaultdict(set)
        for document in documents:
            source = document["receipt"]["payload"]
            creator_id = str(source["creator_id"])
            for marker, count in document["metrics"]["marker_counts"].items():
                if count:
                    marker_creators[str(marker)].add(creator_id)
            for term in source.get("transcript_keywords") or []:
                normalized = str(term).lower().strip()
                if normalized:
                    vocabulary_creators[normalized].add(creator_id)
        recurrence_floor = min(3, minimum_creators)
        recurring_markers = [
            {
                "marker": marker,
                "distinct_creator_count": len(marker_creators[marker]),
            }
            for marker in sorted(
                (
                    value for value in marker_creators
                    if len(marker_creators[value]) >= recurrence_floor
                ),
                key=lambda value: (-len(marker_creators[value]), value),
            )
        ]
        recurring_vocabulary = [
            {
                "term": term,
                "distinct_creator_count": len(vocabulary_creators[term]),
            }
            for term in sorted(
                (
                    value for value in vocabulary_creators
                    if len(vocabulary_creators[value]) >= recurrence_floor
                ),
                key=lambda value: (-len(vocabulary_creators[value]), value),
            )[:40]
        ]
        source_rows = [
            {
                "receipt_id": document["receipt"]["receipt_id"],
                "video_id": document["receipt"]["payload"]["video_id"],
                "transcript_id": document["receipt"]["payload"]["transcript_id"],
                "transcript_sha256": document["receipt"]["payload"][
                    "transcript_sha256"
                ],
                "creator_id": document["receipt"]["payload"]["creator_id"],
                "platform": document["receipt"]["payload"].get("platform"),
                "source_url": document["receipt"].get("source_url"),
                "observed_metrics": (
                    document["receipt"]["payload"].get("pattern") or {}
                ).get("source_metrics") or {},
            }
            for document in documents
        ]
        source_material = {
            "contract": STYLE_GUIDE_CONTRACT,
            "topic": topic,
            "platform": platform,
            "source_receipt_ids": sorted(
                row["receipt_id"] for row in source_rows
            ),
            "source_transcript_hashes": sorted(
                row["transcript_sha256"] for row in source_rows
            ),
        }
        encoded_source = json.dumps(
            source_material, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
        guide_id = "style_" + hashlib.sha256(encoded_source).hexdigest()[:24]
        ranges = {
            name: _observed_range(values)
            for name, values in distributions.items()
        }
        top_hooks = [
            {
                "shape": shape,
                "source_count": count,
                "share": round(count / len(documents), 4),
            }
            for shape, count in hook_shapes.most_common()
        ]
        top_structures = [
            {
                "beat": beat,
                "source_count": count,
                "share": round(count / len(documents), 4),
            }
            for beat, count in structure_counts.most_common()
        ]
        guide = {
            "guide_id": guide_id,
            "contract": STYLE_GUIDE_CONTRACT,
            "status": "ready",
            "topic": topic or None,
            "platform": platform,
            "evidence": {
                "measurement_kind": (
                    "cross_creator_observational_transcript_style"
                ),
                "performance_is_causal_proof": False,
                "verified_transcript_count": len(documents),
                "distinct_creator_count": len({
                    row["creator_id"] for row in source_rows
                }),
                "observed_views_snapshot": observed_views,
                "gates": gates,
                "sources": source_rows,
            },
            "speech": {
                "target_ranges": ranges,
                "recurring_function_markers": recurring_markers,
                "recurring_vocabulary": recurring_vocabulary,
            },
            "hooks": {
                "observed_shapes": top_hooks,
                "preferred_shapes": [row["shape"] for row in top_hooks[:3]],
            },
            "structure": {
                "observed_beats": top_structures,
                "required_beats": [
                    row["beat"] for row in top_structures
                    if row["share"] >= 0.60
                ],
            },
            "delivery": {
                "basis": "transcript cadence and punctuation only",
                "pitch_timbre_or_actual_vocal_inflection_measured": False,
                "direction": {
                    "opening": (
                        "State recognizable tension immediately; omit brand and "
                        "pipeline context from the opening."
                    ),
                    "cadence": (
                        "Use observed sentence-length and speech-rate ranges; "
                        "preserve semantic pauses at sentence boundaries."
                    ),
                    "emphasis": (
                        "Emphasize the human consequence, proof, and payoff."
                    ),
                    "transitions": [
                        row["marker"] for row in recurring_markers[:4]
                    ],
                },
            },
            "rights_and_originality": {
                "rights_state": "public_reference_analysis_only",
                "aggregate_across_creators": True,
                "source_identity_likeness_or_voice_allowed": False,
                "distinctive_source_wording_allowed": False,
                "source_media_direct_use_allowed": False,
                "fixed_matching_word_limit_applied": False,
            },
            "source_material_sha256": hashlib.sha256(
                encoded_source
            ).hexdigest(),
        }
        receipt = self.store.put_receipt(
            "transcript_style_guide",
            platform,
            guide_id,
            None,
            guide,
        )
        return {"status": "ready", "guide": guide, "receipt": receipt}

    def resolve(self, identifier: str) -> dict[str, Any] | None:
        identifier = str(identifier or "").strip()
        if not identifier:
            return None
        direct = self.store.receipt(identifier)
        if direct and direct.get("receipt_type") == "transcript_style_guide":
            return direct
        for receipt in self.store.receipts(
            limit=500, receipt_type="transcript_style_guide"
        ):
            if (
                receipt.get("source_id") == identifier
                or receipt.get("payload", {}).get("guide_id") == identifier
            ):
                return receipt
        return None

    def list(
        self, platform: str | None = None, limit: int = 50
    ) -> list[dict[str, Any]]:
        normalized = str(platform or "").strip().lower()
        rows = self.store.receipts(
            limit=max(1, min(int(limit), 200)),
            receipt_type="transcript_style_guide",
        )
        if normalized:
            rows = [
                row for row in rows
                if str(row["payload"].get("platform") or "").lower()
                == normalized
            ]
        return rows

    def status(self, platform: str = "tiktok") -> dict[str, Any]:
        normalized = str(platform or "tiktok").strip().lower()
        artifacts_total = 0
        artifacts_passing = 0
        try:
            with closing(self.tape.connect()) as connection:
                artifacts_total = int(connection.execute(
                    "SELECT COUNT(*) FROM mt_transcript_artifacts WHERE platform=?",
                    (normalized,),
                ).fetchone()[0])
                artifacts_passing = int(connection.execute(
                    """SELECT COUNT(*) FROM mt_transcript_artifacts
                       WHERE platform=?
                         AND json_extract(audit_json, '$.decision')='PASS'""",
                    (normalized,),
                ).fetchone()[0])
        except sqlite3.Error:
            pass
        patterns, _unknown = self._source_receipts(None, normalized)
        creators = {
            str(row["payload"].get("creator_id")) for row in patterns
            if row["payload"].get("creator_id")
        }
        observed_views = sum(
            int((row["payload"].get("pattern") or {}).get(
                "source_metrics", {}
            ).get("views") or 0)
            for row in patterns
        )
        ready = (
            len(patterns) >= MINIMUM_TRANSCRIPTS
            and len(creators) >= MINIMUM_CREATORS
            and observed_views >= MINIMUM_OBSERVED_VIEWS
        )
        return {
            "status": "ready" if ready else "needs_evidence",
            "contract": "transcript_style_guide_status_v1",
            "platform": normalized,
            "transcript_artifacts": {
                "total": artifacts_total,
                "performance_audit_pass": artifacts_passing,
            },
            "verified_pattern_receipts": len(patterns),
            "distinct_creators": len(creators),
            "observed_views_snapshot": observed_views,
            "style_guides": len(self.list(normalized, 200)),
            "minimums": {
                "verified_transcripts": MINIMUM_TRANSCRIPTS,
                "distinct_creators": MINIMUM_CREATORS,
                "observed_views": MINIMUM_OBSERVED_VIEWS,
            },
            "acquisition_endpoint": "/api/market-tape/full-pipeline",
        }

    def audit(self, payload: dict[str, Any]) -> dict[str, Any]:
        if not isinstance(payload, dict):
            raise ValueError("JSON body must be an object")
        input_binding = {
            "contract": "unpersisted_script_style_audit_v1",
            "stored_script_bound": False,
        }
        if str(payload.get("script_id") or "").strip():
            payload, input_binding = self.store.bind_script_audit_payload(payload)
        identifier = str(
            payload.get("style_guide_receipt_id")
            or payload.get("style_guide_id")
            or ""
        ).strip()
        text = str(payload.get("text") or "").strip()
        if not identifier or not text:
            raise ValueError("style_guide_id and text are required")
        receipt = self.resolve(identifier)
        if receipt is None:
            return {
                "status": "rejected",
                "code": "UNKNOWN_TRANSCRIPT_STYLE_GUIDE",
                "style_guide_id": identifier,
            }
        guide = receipt["payload"]
        target_duration = payload.get("target_duration_seconds")
        if target_duration is None:
            target_duration = (
                (guide.get("speech") or {})
                .get("target_ranges", {})
                .get("duration_seconds", {})
                .get("median")
            )
        duration = float(target_duration) if target_duration else None
        metrics = _text_metrics(text, duration)
        targets = (guide.get("speech") or {}).get("target_ranges") or {}
        preferred_hooks = set(
            (guide.get("hooks") or {}).get("preferred_shapes") or []
        )
        required_beats = set(
            (guide.get("structure") or {}).get("required_beats") or []
        )
        actual_beats = set(metrics["structure"])
        recurrent_markers = {
            row["marker"]
            for row in (
                (guide.get("speech") or {}).get(
                    "recurring_function_markers"
                ) or []
            )
        }
        actual_markers = {
            marker for marker, count in metrics["marker_counts"].items()
            if count
        }
        checks = {
            "words_per_second_in_observed_range": _in_range(
                float(metrics.get("words_per_second") or 0.0),
                targets.get("words_per_second") or {},
                tolerance=3.0,
            ),
            "sentence_length_in_observed_range": _in_range(
                float(metrics["mean_sentence_words"]),
                targets.get("mean_sentence_words") or {},
                tolerance=2.0,
            ),
            "short_sentence_ratio_in_observed_range": _in_range(
                float(metrics["short_sentence_ratio"]),
                targets.get("short_sentence_ratio") or {},
                tolerance=3.0,
            ),
            "direct_address_in_observed_range": _in_range(
                float(metrics["second_person_per_100_words"]),
                targets.get("second_person_per_100_words") or {},
                tolerance=1.0,
            ),
            "contractions_in_observed_range": _in_range(
                float(metrics["contractions_per_100_words"]),
                targets.get("contractions_per_100_words") or {},
                tolerance=1.0,
            ),
            "observed_hook_family": (
                not preferred_hooks or metrics["hook_shape"] in preferred_hooks
            ),
            "recurring_structure_present": (
                not required_beats
                or len(required_beats & actual_beats)
                >= max(1, math.ceil(len(required_beats) * 0.60))
            ),
            "recurring_transition_present": (
                not recurrent_markers or bool(recurrent_markers & actual_markers)
            ),
        }
        source_ids = [
            str(row.get("video_id") or "")
            for row in guide.get("evidence", {}).get("sources") or []
            if row.get("video_id")
        ]
        source_rows = self.tape.artifact_bound_candidates(source_ids)
        copy_gate = audit_substantive_copy(
            text,
            (
                {
                    "source_id": str(row.get("video_id") or ""),
                    "text": str(row.get("transcript") or ""),
                    "creator_identifiers": [str(row.get("creator_id") or "")],
                }
                for row in source_rows
            ),
            provenance=(
                payload.get("provenance")
                if isinstance(payload.get("provenance"), dict)
                else None
            ),
        )
        originality_pass = bool(copy_gate["passed"])
        weights = {
            "words_per_second_in_observed_range": 20,
            "sentence_length_in_observed_range": 15,
            "short_sentence_ratio_in_observed_range": 10,
            "direct_address_in_observed_range": 10,
            "contractions_in_observed_range": 5,
            "observed_hook_family": 15,
            "recurring_structure_present": 15,
            "recurring_transition_present": 10,
        }
        score = float(sum(
            weight for name, weight in weights.items() if checks[name]
        ))
        decision = "PASS" if score >= 70 and originality_pass else "REVISE"
        findings = {
            "contract": STYLE_AUDIT_CONTRACT,
            "measurement_kind": (
                "style_fit_prediction_from_observed_transcripts"
            ),
            "performance_outcome_predicted": False,
            "style_guide_receipt_id": receipt["receipt_id"],
            "style_guide_id": guide["guide_id"],
            "checks": checks,
            "failed_checks": [
                name for name, passed in checks.items() if not passed
            ],
            "metrics": metrics,
            "copy_gate": copy_gate,
            "identity_voice_or_likeness_imitation_allowed": False,
            "input_binding": input_binding,
        }
        audit = self.store.put_audit(
            "transcript_style_fit",
            str(payload.get("script_id") or "") or None,
            decision,
            score,
            findings,
        )
        return {
            "status": "complete",
            "decision": decision,
            "score": score,
            "audit_id": audit["audit_id"],
            "findings": findings,
        }
