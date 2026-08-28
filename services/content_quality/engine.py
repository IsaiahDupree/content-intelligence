from __future__ import annotations

import hashlib
import json
import math
import os
import re
import sqlite3
import subprocess
import uuid
from contextlib import closing
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Sequence

from .ai_relatability import AIRelatabilityAdjudicator, NON_AI_PASS_DECISION
from .contracts import is_supported_transcript_audit_contract
from .narrative_coherence import NarrativeCoherenceService
from .owned_content_metrics import OwnedContentMetricTelemetry
from .owned_publication import OwnedPublicationAttributionService
from .script_experiments import ScriptExperimentTelemetry
from .script_intelligence import ScriptIntelligenceService
from .script_quality import (
    MAX_QUALITY_REWRITE_ATTEMPTS,
    OWNER_QUALITY_CONTRACT,
    arrange_role_components,
    audit_owner_calibrated_quality,
    build_delivery_visual_plan,
    owner_repair_actions,
    repair_timeline_for_owner_quality,
    retime_timeline,
    select_rhetorical_structure,
)
from .transcript_bank import immutable_artifact_attestation
from .transcript_style import TranscriptStyleGuideService


UTC = timezone.utc
OWNED_ATTRIBUTION_EVENT_CONTRACT = "owned_attribution_event_v1"
OWNED_RETENTION_SAMPLE_CONTRACT = "owned_retention_sample_v1"
OWNED_OUTCOME_SUMMARY_CONTRACT = "owned_outcome_summary_v1"
OWNED_PUBLICATION_RECEIPT_CONTRACT = "owned_publication_receipt_v1"
OWNED_PUBLICATION_BINDING_CONTRACT = "owned_publication_binding_v1"
OWNED_OUTCOME_EVENT_TYPES = ("click", "install", "trial", "purchase")
WORD_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9'’-]*")
SENTENCE_RE = re.compile(r"(?<=[.!?])\s+")
STOP_WORDS = {
    "about", "after", "again", "also", "because", "been", "before", "being",
    "could", "does", "doing", "from", "have", "here", "into", "just", "more",
    "most", "only", "other", "over", "should", "some", "than", "that", "their",
    "them", "then", "there", "these", "they", "this", "those", "through", "very",
    "want", "what", "when", "where", "which", "while", "with", "would", "your",
}
JARGON = {
    "api", "automation", "backend", "conversion", "crm", "framework", "frontend",
    "integration", "kpi", "llm", "optimization", "pipeline", "platform", "saas",
    "sdk", "stack", "system", "workflow",
}
PROOF_WORDS = {
    "because", "data", "evidence", "measured", "proof", "receipt", "result",
    "tested", "views", "watched",
}
CHANGE_WORDS = {
    "but", "except", "here's", "instead", "look", "now", "proof", "so", "then",
    "watch", "yet",
}
MAX_SOURCE_EXCERPT_WORDS = 10
MIN_HUMAN_MOMENT_SOURCE_SCORE = 55
EVERYDAY_HUMAN_LANGUAGE_BY_CATEGORY = {
    "problem": {
        "alone", "anxious", "anxiety", "burned", "burnout", "burnt",
        "challenge", "challenges", "difficult", "exhausted", "fail", "failed",
        "failing", "fear", "feel", "feeling", "frustrated", "hard", "hate",
        "hopeless", "issue", "issues", "mistake", "mistakes", "overwhelmed",
        "pressure", "problem", "problems", "quit", "scattered", "struggle",
        "struggling", "stuck", "tired", "worry", "worse",
    },
    "need": {
        "can't", "cannot", "care", "don't", "must", "need", "needed", "needs",
        "solution", "solutions", "trying", "wish",
    },
    "time": {
        "daily", "deadline", "deadlines", "hour", "hours", "late",
        "minute", "minutes", "morning", "mornings", "night", "nights", "time",
        "week", "weeks",
    },
    "work": {
        "client", "clients", "customer", "customers", "email", "emails", "job",
        "jobs", "form", "forms", "invoice", "invoices", "lead", "leads",
        "meeting", "meetings", "quote", "quotes", "sales", "support", "task",
        "tasks", "team", "teams", "work", "working",
    },
}
HUMAN_EXPERIENCE_WORDS = set().union(
    *EVERYDAY_HUMAN_LANGUAGE_BY_CATEGORY.values()
)
HUMAN_MOMENT_CATEGORY_WEIGHTS = {
    "problem": 40,
    "time": 35,
    "need": 30,
    "work": 20,
}
HUMAN_MOMENT_PROMOTIONAL_WORDS = {
    "bio", "buy", "check", "click", "comment", "download", "follow", "free",
    "guide", "link", "save", "subscribe",
}
HUMAN_MOMENT_LIVED_CONTEXT_WORDS = {
    "i", "i'm", "i've", "me", "my", "we", "we're", "you", "you're", "your",
}
HUMAN_MOMENT_NEGATION_WORDS = {
    "can't", "cannot", "don't", "failed", "failing", "hard", "not", "worse",
}
HUMAN_MOMENT_WEAK_OPENERS = {
    "and", "but", "so", "that", "the", "then", "thing", "this",
}
HUMAN_MOMENT_INCOMPLETE_OPENERS = {
    "a", "also", "am", "an", "any", "are", "can", "could", "from", "in", "is",
    "of", "on", "to", "was", "were", "which",
}
HUMAN_MOMENT_WEAK_ENDINGS = {
    "a", "am", "an", "and", "are", "be", "been", "but", "can", "can't",
    "could", "don't", "every", "for", "from", "i'm", "i've", "in", "is",
    "my", "of", "on", "our", "should", "that", "the", "their", "this", "to", "was",
    "we're", "were",
    "when", "where", "which", "who", "will", "with", "workflow", "workflows",
    "would", "your", "you're",
}
HUMAN_MOMENT_INCOMPLETE_BIGRAMS = {
    ("i", "and"), ("we", "and"), ("you", "and"), ("you", "into"),
}
AUDIENCE_CONTEXT_BY_TERM = {
    "founder": {
        "business", "client", "customer", "customers", "form", "forms",
        "invoice", "invoices", "lead", "leads", "meeting", "meetings",
        "product", "quote", "quotes", "revenue", "sales", "support", "team",
        "user", "users", "website", "websites",
    },
    "software": {
        "app", "apps", "build", "code", "coding", "developer", "product",
        "software",
    },
}
AUDIENCE_OFF_CONTEXT_BY_TERM = {
    "founder": {"applying", "job", "jobs", "resume"},
    "software": {"applying", "job", "jobs", "resume"},
}


def audience_context_vocabulary(audience: str) -> tuple[set[str], set[str]]:
    audience_tokens = {
        normalized_source_word(token) for token in words(audience)
    }
    context = set(audience_tokens)
    off_context: set[str] = set()
    for audience_term in audience_tokens:
        singular = audience_term[:-1] if audience_term.endswith("s") else audience_term
        context.update(AUDIENCE_CONTEXT_BY_TERM.get(singular, set()))
        off_context.update(AUDIENCE_OFF_CONTEXT_BY_TERM.get(singular, set()))
    return context, off_context


def utc_now() -> str:
    return datetime.now(UTC).isoformat()


def stable_id(prefix: str, *parts: Any) -> str:
    raw = "|".join(json.dumps(part, sort_keys=True, default=str) for part in parts)
    return f"{prefix}_{hashlib.sha256(raw.encode('utf-8')).hexdigest()[:20]}"


SCRIPT_IDENTITY_FIELDS = (
    "topic", "audience", "objective", "brief_id", "trend_id",
    "parent_script_id", "variant_index", "variant_selection_contract",
    "source_receipt_ids", "evidence_binding_receipt_ids", "human_moment",
    "style_guide_id",
    "style_guide_receipt_id", "style_application",
    "speaker_claim_gate",
    "evidence_summary", "rhetorical_structure", "owner_quality_contract",
    "owner_quality", "delivery_visual_plan", "quality_revision",
    "timeline", "text",
)
OWNED_CLAIM_EVIDENCE_CONTRACT = "owned_claim_evidence_v1"
FIRST_PERSON_TOKENS = {"i", "i'm", "i've", "me", "my", "mine", "we", "we've", "our", "ours"}


def script_identity_payload(script: dict[str, Any]) -> dict[str, Any]:
    return {field: script.get(field) for field in SCRIPT_IDENTITY_FIELDS}


class IdempotencyConflict(ValueError):
    """The same provider key was reused for a different immutable fact."""


def words(text: str) -> list[str]:
    return WORD_RE.findall(text or "")


def contains_first_person(text: str) -> bool:
    return bool({token.casefold().replace("’", "'") for token in words(text)} & FIRST_PERSON_TOKENS)


def normalized_source_word(value: str) -> str:
    return value.casefold().replace("’", "'")


def source_exact_everyday_excerpt(
    source_span: str,
    max_words: int = MAX_SOURCE_EXCERPT_WORDS,
    *,
    audience_vocabulary: set[str] | None = None,
    audience_off_context: set[str] | None = None,
    source_audience_terms: Sequence[str] | None = None,
    source_off_context_terms: Sequence[str] | None = None,
) -> dict[str, Any] | None:
    """Return a cue-bearing contiguous substring without paraphrasing it."""

    token_matches = list(WORD_RE.finditer(source_span or ""))
    if not token_matches:
        return None
    cue_indexes = [
        index
        for index, match in enumerate(token_matches)
        if normalized_source_word(match.group(0)) in HUMAN_EXPERIENCE_WORDS
    ]
    if not cue_indexes:
        return None
    bounded_words = max(1, min(int(max_words), MAX_SOURCE_EXCERPT_WORDS))
    audience_vocabulary = set(audience_vocabulary or ())
    audience_off_context = set(audience_off_context or ())
    source_audience_terms = sorted(set(source_audience_terms or ()))
    source_off_context_terms = set(source_off_context_terms or ())
    candidates: list[dict[str, Any]] = []
    seen_windows: set[tuple[int, int]] = set()
    for cue_index in cue_indexes:
        minimum_window_words = min(5, len(token_matches))
        first_min = max(0, cue_index - bounded_words + 1)
        first_max = cue_index
        for first_index in range(first_min, first_max + 1):
            final_min = max(cue_index, first_index + minimum_window_words - 1)
            final_max = min(
                len(token_matches) - 1,
                first_index + bounded_words - 1,
            )
            for final_index in range(final_min, final_max + 1):
                window_key = (first_index, final_index)
                if window_key in seen_windows:
                    continue
                seen_windows.add(window_key)
                window = token_matches[first_index:final_index + 1]
                normalized_tokens = [
                    normalized_source_word(match.group(0)) for match in window
                ]
                excerpt_tokens = set(normalized_tokens)
                if any(
                    any(char.isdigit() for char in token)
                    for token in normalized_tokens
                ):
                    continue
                matched_terms = sorted(
                    excerpt_tokens & HUMAN_EXPERIENCE_WORDS
                )
                categories = sorted(
                    category
                    for category, terms
                    in EVERYDAY_HUMAN_LANGUAGE_BY_CATEGORY.items()
                    if excerpt_tokens & terms
                )
                promotional_terms = sorted(
                    excerpt_tokens & HUMAN_MOMENT_PROMOTIONAL_WORDS
                )
                # A CTA or offer is not a lived audience moment. An actual
                # problem or time constraint in the same exact excerpt remains
                # admissible.
                if promotional_terms and not (
                    {"problem", "time"} & set(categories)
                ):
                    continue
                lived_terms = sorted(
                    excerpt_tokens & HUMAN_MOMENT_LIVED_CONTEXT_WORDS
                )
                negation_terms = sorted(
                    excerpt_tokens & HUMAN_MOMENT_NEGATION_WORDS
                )
                first_token = normalized_tokens[0]
                final_token = normalized_tokens[-1]
                score = (
                    max(
                        HUMAN_MOMENT_CATEGORY_WEIGHTS[category]
                        for category in categories
                    )
                    + 5 * max(0, len(categories) - 1)
                    + 3 * max(0, len(matched_terms) - 1)
                    + len(normalized_tokens)
                    + (7 if negation_terms else 0)
                    + (12 if lived_terms else 0)
                    + (
                        15
                        if first_token
                        in HUMAN_MOMENT_LIVED_CONTEXT_WORDS
                        else 0
                    )
                    - (
                        8
                        if first_token in HUMAN_MOMENT_WEAK_OPENERS
                        else 0
                    )
                    - (
                        12
                        if first_token in HUMAN_MOMENT_INCOMPLETE_OPENERS
                        else 0
                    )
                    - (
                        10
                        if final_token in HUMAN_MOMENT_WEAK_ENDINGS
                        else 0
                    )
                    - (
                        20
                        if len(normalized_tokens) > 1
                        and tuple(normalized_tokens[:2])
                        in HUMAN_MOMENT_INCOMPLETE_BIGRAMS
                        else 0
                    )
                    - (
                        20
                        if len(normalized_tokens) >= 4
                        and normalized_tokens[-2:] == ["customer", "support"]
                        and "a" in normalized_tokens[-4:-2]
                        else 0
                    )
                    - (
                        20
                        if final_token == "customer"
                        and "a" in normalized_tokens[-3:-1]
                        else 0
                    )
                    - (
                        25
                        if first_token in {"my", "our", "their", "your"}
                        and "using" in normalized_tokens
                        and not (
                            {"am", "are", "is", "was", "were"}
                            & set(normalized_tokens)
                        )
                        else 0
                    )
                )
                excerpt = source_span[
                    token_matches[first_index].start():
                    token_matches[final_index].end()
                ]
                audience_match_terms = sorted(
                    excerpt_tokens & audience_vocabulary
                )
                audience_off_context_terms = sorted(
                    (excerpt_tokens & audience_off_context)
                    | source_off_context_terms
                )
                adjusted_score = (
                    score
                    + 15 * min(2, len(audience_match_terms))
                    + 5 * min(2, len(source_audience_terms))
                    - 30 * min(1, len(audience_off_context_terms))
                )
                candidates.append({
                    "text": excerpt,
                    "word_count": final_index - first_index + 1,
                    "source_span_word_count": len(token_matches),
                    "truncated": len(token_matches) > bounded_words,
                    "word_start": first_index,
                    "word_end_exclusive": final_index + 1,
                    "categories": categories,
                    "matched_terms": matched_terms,
                    "lived_context_terms": lived_terms,
                    "negation_terms": negation_terms,
                    "promotional_terms": promotional_terms,
                    "source_selection_score": max(0, min(100, score)),
                    "audience_adjusted_selection_score": max(
                        0, min(100, adjusted_score)
                    ),
                    "audience_match_terms": audience_match_terms,
                    "source_audience_match_terms": source_audience_terms,
                    "audience_off_context_terms": (
                        audience_off_context_terms
                    ),
                    "score_is_probability": False,
                })
    return max(
        candidates,
        key=lambda item: (
            int(item["audience_adjusted_selection_score"]),
            int(item["source_selection_score"]),
            len(item["categories"]),
            len(item["matched_terms"]),
            -int(item["word_start"]),
        ),
        default=None,
    )


def clamp(value: float, low: float = 0.0, high: float = 100.0) -> float:
    return max(low, min(high, value))


def verified_transcript_patterns(
    receipts: Sequence[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Admit one receipt per immutable transcript/observation artifact.

    One transcript can be discovered under several topic queries. Counting
    those topic-specific receipts as independent sources inflates cohort size,
    views, and language recurrence, so every downstream gate deduplicates on
    the artifact identity before measuring evidence sufficiency.
    """

    unique: dict[tuple[str, str], dict[str, Any]] = {}
    for item in receipts:
        payload = item.get("payload") or {}
        qualification = payload.get("performance_qualification") or {}
        transcript_id = str(payload.get("transcript_id") or "").strip()
        observation_key = str(payload.get("observation_key") or "").strip()
        if (
            item.get("receipt_type") != "viral_transcript_pattern"
            or payload.get("transcript_source") != "local_whisper"
            or qualification.get("audit_decision") != "PASS"
            or not is_supported_transcript_audit_contract(
                qualification.get("audit_contract")
            )
            or not transcript_id
            or not observation_key
            or len(str(payload.get("audio_sha256") or "")) != 64
            or len(str(payload.get("transcript_sha256") or "")) != 64
        ):
            continue
        unique.setdefault((transcript_id, observation_key), item)
    return list(unique.values())


class QualityStore:
    SCRIPT_AUDIT_FIELDS = (
        "text", "timeline", "evidence_summary", "source_receipt_ids",
        "evidence_binding_receipt_ids",
        "audience", "human_moment", "brief_id", "topic", "objective",
        "variant_index", "variant_selection_contract",
        "style_guide_id", "style_guide_receipt_id", "style_application",
        "speaker_claim_gate",
    )

    def __init__(self, path: str | Path):
        self.path = Path(path).expanduser()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    def connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=15)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA foreign_keys=ON")
        return connection

    def _initialize(self) -> None:
        with closing(self.connect()) as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS cq_receipts (
                    receipt_id TEXT PRIMARY KEY,
                    receipt_type TEXT NOT NULL,
                    source_type TEXT NOT NULL,
                    source_id TEXT,
                    source_url TEXT,
                    payload_json TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS cq_scripts (
                    script_id TEXT PRIMARY KEY,
                    topic TEXT NOT NULL,
                    objective TEXT NOT NULL,
                    source_receipts_json TEXT NOT NULL,
                    script_json TEXT NOT NULL,
                    status TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS cq_audits (
                    audit_id TEXT PRIMARY KEY,
                    audit_type TEXT NOT NULL,
                    subject_id TEXT,
                    decision TEXT NOT NULL,
                    score REAL NOT NULL,
                    findings_json TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS cq_retention (
                    receipt_id TEXT PRIMARY KEY,
                    platform TEXT NOT NULL,
                    source_id TEXT,
                    payload_json TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS cq_script_briefs (
                    brief_id TEXT PRIMARY KEY,
                    contract TEXT NOT NULL,
                    trend_id TEXT NOT NULL,
                    snapshot_id TEXT NOT NULL,
                    status TEXT NOT NULL,
                    receipt_id TEXT NOT NULL UNIQUE,
                    payload_json TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    FOREIGN KEY(receipt_id) REFERENCES cq_receipts(receipt_id)
                );
                CREATE TABLE IF NOT EXISTS cq_workflow_runs (
                    workflow_id TEXT PRIMARY KEY,
                    brief_id TEXT NOT NULL,
                    script_id TEXT,
                    state TEXT NOT NULL,
                    stage_receipts_json TEXT NOT NULL,
                    result_json TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    FOREIGN KEY(brief_id) REFERENCES cq_script_briefs(brief_id)
                );
                CREATE TABLE IF NOT EXISTS cq_agent_queries (
                    query_id TEXT PRIMARY KEY,
                    principal TEXT NOT NULL,
                    operation TEXT NOT NULL,
                    parameters_sha256 TEXT NOT NULL,
                    response_sha256 TEXT NOT NULL,
                    outcome TEXT NOT NULL,
                    row_count INTEGER NOT NULL,
                    duration_ms REAL NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS cq_owned_outcome_events (
                    event_id TEXT PRIMARY KEY,
                    contract TEXT NOT NULL,
                    idempotency_key TEXT NOT NULL UNIQUE,
                    event_type TEXT NOT NULL CHECK(
                        event_type IN ('click', 'install', 'trial', 'purchase')
                    ),
                    content_id TEXT NOT NULL,
                    campaign_id TEXT NOT NULL,
                    offer_id TEXT NOT NULL,
                    source_platform TEXT NOT NULL,
                    source_id TEXT NOT NULL,
                    journey_id TEXT NOT NULL,
                    occurred_at TEXT NOT NULL,
                    provider_event_id TEXT,
                    payload_sha256 TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS cq_owned_retention_samples (
                    sample_id TEXT PRIMARY KEY,
                    contract TEXT NOT NULL,
                    idempotency_key TEXT NOT NULL UNIQUE,
                    content_id TEXT NOT NULL,
                    campaign_id TEXT NOT NULL,
                    offer_id TEXT NOT NULL,
                    source_platform TEXT NOT NULL,
                    source_id TEXT NOT NULL,
                    measurement_id TEXT NOT NULL,
                    journey_id TEXT,
                    observed_at TEXT NOT NULL,
                    elapsed_ms INTEGER NOT NULL CHECK(elapsed_ms >= 0),
                    retained_percent REAL NOT NULL CHECK(
                        retained_percent >= 0 AND retained_percent <= 100
                    ),
                    sample_size INTEGER NOT NULL CHECK(sample_size > 0),
                    payload_sha256 TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS cq_owned_publication_receipts (
                    publication_id TEXT PRIMARY KEY,
                    contract TEXT NOT NULL,
                    idempotency_key TEXT NOT NULL UNIQUE,
                    content_id TEXT NOT NULL,
                    campaign_id TEXT NOT NULL,
                    offer_id TEXT NOT NULL,
                    semantic_asset_id TEXT NOT NULL,
                    semantic_asset_sha256 TEXT NOT NULL,
                    local_asset_path TEXT NOT NULL,
                    local_asset_sha256 TEXT NOT NULL,
                    local_asset_bytes INTEGER NOT NULL CHECK(local_asset_bytes > 0),
                    source_platform TEXT NOT NULL,
                    account_id TEXT NOT NULL,
                    publisher TEXT NOT NULL,
                    provider_post_id TEXT NOT NULL,
                    provider_post_url TEXT NOT NULL,
                    published_at TEXT NOT NULL,
                    provider_receipt_id TEXT NOT NULL,
                    provider_receipt_sha256 TEXT NOT NULL,
                    publication_receipt_sha256 TEXT NOT NULL UNIQUE,
                    payload_json TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    UNIQUE(source_platform, provider_post_id),
                    UNIQUE(content_id, source_platform, account_id)
                );
                CREATE TABLE IF NOT EXISTS cq_owned_outcome_publication_bindings (
                    event_id TEXT PRIMARY KEY,
                    publication_id TEXT NOT NULL,
                    publication_receipt_sha256 TEXT NOT NULL,
                    contract TEXT NOT NULL,
                    bound_at TEXT NOT NULL,
                    FOREIGN KEY(event_id) REFERENCES cq_owned_outcome_events(event_id),
                    FOREIGN KEY(publication_id)
                        REFERENCES cq_owned_publication_receipts(publication_id)
                );
                CREATE TABLE IF NOT EXISTS cq_owned_retention_publication_bindings (
                    sample_id TEXT PRIMARY KEY,
                    publication_id TEXT NOT NULL,
                    publication_receipt_sha256 TEXT NOT NULL,
                    contract TEXT NOT NULL,
                    bound_at TEXT NOT NULL,
                    FOREIGN KEY(sample_id) REFERENCES cq_owned_retention_samples(sample_id),
                    FOREIGN KEY(publication_id)
                        REFERENCES cq_owned_publication_receipts(publication_id)
                );
                CREATE INDEX IF NOT EXISTS idx_cq_receipts_type_created
                    ON cq_receipts(receipt_type, created_at DESC);
                CREATE INDEX IF NOT EXISTS idx_cq_audits_type_created
                    ON cq_audits(audit_type, created_at DESC);
                CREATE INDEX IF NOT EXISTS idx_cq_briefs_created
                    ON cq_script_briefs(created_at DESC);
                CREATE INDEX IF NOT EXISTS idx_cq_workflows_created
                    ON cq_workflow_runs(created_at DESC);
                CREATE INDEX IF NOT EXISTS idx_cq_agent_queries_created
                    ON cq_agent_queries(created_at DESC);
                CREATE INDEX IF NOT EXISTS idx_cq_owned_events_attribution
                    ON cq_owned_outcome_events(
                        content_id, campaign_id, offer_id,
                        source_platform, source_id, occurred_at
                    );
                CREATE INDEX IF NOT EXISTS idx_cq_owned_events_journey
                    ON cq_owned_outcome_events(journey_id, occurred_at);
                CREATE INDEX IF NOT EXISTS idx_cq_owned_retention_attribution
                    ON cq_owned_retention_samples(
                        content_id, campaign_id, offer_id,
                        source_platform, source_id, elapsed_ms, observed_at
                    );
                CREATE INDEX IF NOT EXISTS idx_cq_owned_publication_content
                    ON cq_owned_publication_receipts(
                        content_id, source_platform, account_id, published_at
                    );
                CREATE INDEX IF NOT EXISTS idx_cq_owned_event_publication
                    ON cq_owned_outcome_publication_bindings(
                        publication_id, event_id
                    );
                CREATE INDEX IF NOT EXISTS idx_cq_owned_retention_publication
                    ON cq_owned_retention_publication_bindings(
                        publication_id, sample_id
                    );
                CREATE TRIGGER IF NOT EXISTS cq_receipts_no_update
                BEFORE UPDATE ON cq_receipts
                BEGIN
                    SELECT RAISE(ABORT, 'content quality receipts are append-only');
                END;
                CREATE TRIGGER IF NOT EXISTS cq_receipts_no_delete
                BEFORE DELETE ON cq_receipts
                BEGIN
                    SELECT RAISE(ABORT, 'content quality receipts are append-only');
                END;
                CREATE TRIGGER IF NOT EXISTS cq_scripts_no_update
                BEFORE UPDATE ON cq_scripts
                BEGIN
                    SELECT RAISE(ABORT, 'content quality scripts are append-only');
                END;
                CREATE TRIGGER IF NOT EXISTS cq_scripts_no_delete
                BEFORE DELETE ON cq_scripts
                BEGIN
                    SELECT RAISE(ABORT, 'content quality scripts are append-only');
                END;
                CREATE TRIGGER IF NOT EXISTS cq_audits_no_update
                BEFORE UPDATE ON cq_audits
                BEGIN
                    SELECT RAISE(ABORT, 'content quality audits are append-only');
                END;
                CREATE TRIGGER IF NOT EXISTS cq_audits_no_delete
                BEFORE DELETE ON cq_audits
                BEGIN
                    SELECT RAISE(ABORT, 'content quality audits are append-only');
                END;
                CREATE TRIGGER IF NOT EXISTS cq_retention_no_update
                BEFORE UPDATE ON cq_retention
                BEGIN
                    SELECT RAISE(ABORT, 'content quality retention receipts are append-only');
                END;
                CREATE TRIGGER IF NOT EXISTS cq_retention_no_delete
                BEFORE DELETE ON cq_retention
                BEGIN
                    SELECT RAISE(ABORT, 'content quality retention receipts are append-only');
                END;
                CREATE TRIGGER IF NOT EXISTS cq_script_briefs_no_update
                BEFORE UPDATE ON cq_script_briefs
                BEGIN
                    SELECT RAISE(ABORT, 'script intelligence briefs are append-only');
                END;
                CREATE TRIGGER IF NOT EXISTS cq_script_briefs_no_delete
                BEFORE DELETE ON cq_script_briefs
                BEGIN
                    SELECT RAISE(ABORT, 'script intelligence briefs are append-only');
                END;
                CREATE TRIGGER IF NOT EXISTS cq_workflow_runs_no_update
                BEFORE UPDATE ON cq_workflow_runs
                BEGIN
                    SELECT RAISE(ABORT, 'script workflow runs are append-only');
                END;
                CREATE TRIGGER IF NOT EXISTS cq_workflow_runs_no_delete
                BEFORE DELETE ON cq_workflow_runs
                BEGIN
                    SELECT RAISE(ABORT, 'script workflow runs are append-only');
                END;
                CREATE TRIGGER IF NOT EXISTS cq_agent_queries_no_update
                BEFORE UPDATE ON cq_agent_queries
                BEGIN
                    SELECT RAISE(ABORT, 'agent queries are append-only');
                END;
                CREATE TRIGGER IF NOT EXISTS cq_agent_queries_no_delete
                BEFORE DELETE ON cq_agent_queries
                BEGIN
                    SELECT RAISE(ABORT, 'agent queries are append-only');
                END;
                CREATE TRIGGER IF NOT EXISTS cq_owned_outcome_events_no_update
                BEFORE UPDATE ON cq_owned_outcome_events
                BEGIN
                    SELECT RAISE(ABORT, 'owned outcome events are append-only');
                END;
                CREATE TRIGGER IF NOT EXISTS cq_owned_outcome_events_no_delete
                BEFORE DELETE ON cq_owned_outcome_events
                BEGIN
                    SELECT RAISE(ABORT, 'owned outcome events are append-only');
                END;
                CREATE TRIGGER IF NOT EXISTS cq_owned_retention_samples_no_update
                BEFORE UPDATE ON cq_owned_retention_samples
                BEGIN
                    SELECT RAISE(ABORT, 'owned retention samples are append-only');
                END;
                CREATE TRIGGER IF NOT EXISTS cq_owned_retention_samples_no_delete
                BEFORE DELETE ON cq_owned_retention_samples
                BEGIN
                    SELECT RAISE(ABORT, 'owned retention samples are append-only');
                END;
                CREATE TRIGGER IF NOT EXISTS cq_owned_publication_receipts_no_update
                BEFORE UPDATE ON cq_owned_publication_receipts
                BEGIN
                    SELECT RAISE(ABORT, 'owned publication receipts are append-only');
                END;
                CREATE TRIGGER IF NOT EXISTS cq_owned_publication_receipts_no_delete
                BEFORE DELETE ON cq_owned_publication_receipts
                BEGIN
                    SELECT RAISE(ABORT, 'owned publication receipts are append-only');
                END;
                CREATE TRIGGER IF NOT EXISTS cq_owned_outcome_publication_bindings_no_update
                BEFORE UPDATE ON cq_owned_outcome_publication_bindings
                BEGIN
                    SELECT RAISE(ABORT, 'owned outcome publication bindings are append-only');
                END;
                CREATE TRIGGER IF NOT EXISTS cq_owned_outcome_publication_bindings_no_delete
                BEFORE DELETE ON cq_owned_outcome_publication_bindings
                BEGIN
                    SELECT RAISE(ABORT, 'owned outcome publication bindings are append-only');
                END;
                CREATE TRIGGER IF NOT EXISTS cq_owned_retention_publication_bindings_no_update
                BEFORE UPDATE ON cq_owned_retention_publication_bindings
                BEGIN
                    SELECT RAISE(ABORT, 'owned retention publication bindings are append-only');
                END;
                CREATE TRIGGER IF NOT EXISTS cq_owned_retention_publication_bindings_no_delete
                BEFORE DELETE ON cq_owned_retention_publication_bindings
                BEGIN
                    SELECT RAISE(ABORT, 'owned retention publication bindings are append-only');
                END;
                """
            )
            connection.commit()

    def put_receipt(
        self,
        receipt_type: str,
        source_type: str,
        source_id: str | None,
        source_url: str | None,
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        receipt_id = stable_id("rcpt", receipt_type, source_type, source_id, payload)
        created_at = utc_now()
        with closing(self.connect()) as connection:
            connection.execute(
                """
                INSERT INTO cq_receipts
                    (receipt_id, receipt_type, source_type, source_id, source_url, payload_json, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(receipt_id) DO NOTHING
                """,
                (
                    receipt_id,
                    receipt_type,
                    source_type,
                    source_id,
                    source_url,
                    json.dumps(payload, sort_keys=True),
                    created_at,
                ),
            )
            connection.commit()
            stored = connection.execute(
                "SELECT * FROM cq_receipts WHERE receipt_id=?", (receipt_id,)
            ).fetchone()
        return self._receipt_row(stored)

    def put_owned_claim_receipt(
        self,
        *,
        statement: str,
        evidence_kind: str,
        owner_id: str,
        evidence_path: str | Path,
    ) -> dict[str, Any]:
        clean = " ".join(str(statement or "").split()).strip()
        kind = str(evidence_kind or "").strip().lower()
        identity = str(owner_id or "").strip()
        if not clean or not kind or not identity:
            raise ValueError("statement, evidence_kind, and owner_id are required")
        source_path = Path(evidence_path).expanduser().resolve()
        if not source_path.is_file():
            raise ValueError("evidence_path must identify an existing file")
        source_bytes = source_path.read_bytes()
        source_text = " ".join(
            source_bytes.decode("utf-8", errors="strict").split()
        )
        if clean not in source_text:
            raise ValueError("the exact statement is not present in the evidence file")
        source_sha = hashlib.sha256(source_bytes).hexdigest()
        source_receipt = self.put_receipt(
            "owned_evidence_file",
            "owned_file",
            str(source_path),
            None,
            {
                "contract": "owned_evidence_file_v1",
                "owner_id": identity,
                "source_path": str(source_path),
                "source_sha256": source_sha,
                "source_byte_count": len(source_bytes),
                "statement": clean,
                "statement_sha256": hashlib.sha256(
                    clean.encode("utf-8")
                ).hexdigest(),
                "perspective_basis": "exact_owned_statement_in_source_bytes",
            },
        )
        payload = {
            "contract": OWNED_CLAIM_EVIDENCE_CONTRACT,
            "statement": clean,
            "statement_sha256": hashlib.sha256(clean.encode("utf-8")).hexdigest(),
            "evidence_kind": kind,
            "owner_id": identity,
            "evidence_receipt_ids": [source_receipt["receipt_id"]],
            "evidence_sha256": source_sha,
            "verification_basis": "exact_statement_in_stored_source_bytes",
        }
        return self.put_receipt(
            "owned_claim_evidence", "owned", identity, None, payload
        )

    def receipts(
        self,
        receipt_ids: Sequence[str] | None = None,
        limit: int = 50,
        receipt_type: str | None = None,
    ) -> list[dict[str, Any]]:
        with closing(self.connect()) as connection:
            if receipt_ids:
                marks = ",".join("?" for _ in receipt_ids)
                query = f"SELECT * FROM cq_receipts WHERE receipt_id IN ({marks})"
                parameters: list[Any] = list(receipt_ids)
                if receipt_type:
                    query += " AND receipt_type=?"
                    parameters.append(receipt_type)
                query += " ORDER BY created_at DESC"
                rows = connection.execute(query, parameters).fetchall()
            else:
                where = "WHERE receipt_type=?" if receipt_type else ""
                parameters = [receipt_type] if receipt_type else []
                parameters.append(max(1, min(limit, 500)))
                rows = connection.execute(
                    f"SELECT * FROM cq_receipts {where} ORDER BY created_at DESC LIMIT ?",
                    parameters,
                ).fetchall()
        return [self._receipt_row(row) for row in rows]

    def receipt(self, receipt_id: str) -> dict[str, Any] | None:
        rows = self.receipts([receipt_id])
        return rows[0] if rows else None

    @staticmethod
    def _receipt_row(row: sqlite3.Row) -> dict[str, Any]:
        return {
            "receipt_id": row["receipt_id"],
            "receipt_type": row["receipt_type"],
            "source_type": row["source_type"],
            "source_id": row["source_id"],
            "source_url": row["source_url"],
            "payload": json.loads(row["payload_json"]),
            "created_at": row["created_at"],
        }

    def put_script(self, script: dict[str, Any]) -> dict[str, Any]:
        with closing(self.connect()) as connection:
            connection.execute(
                """
                INSERT INTO cq_scripts
                    (script_id, topic, objective, source_receipts_json, script_json, status, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(script_id) DO NOTHING
                """,
                (
                    script["script_id"],
                    script["topic"],
                    script["objective"],
                    json.dumps(script["source_receipt_ids"], sort_keys=True),
                    json.dumps(script, sort_keys=True),
                    script["status"],
                    script["created_at"],
                ),
            )
            connection.commit()
            row = connection.execute(
                "SELECT script_json FROM cq_scripts WHERE script_id=?",
                (script["script_id"],),
            ).fetchone()
        if row is None:
            raise RuntimeError("script write was not durable")
        stored = json.loads(row["script_json"])
        if script_identity_payload(stored) != script_identity_payload(script):
            raise ValueError(
                "script_id already exists with different immutable content"
            )
        return stored

    def script(self, script_id: str) -> dict[str, Any] | None:
        with closing(self.connect()) as connection:
            row = connection.execute(
                "SELECT script_json FROM cq_scripts WHERE script_id=?", (script_id,)
            ).fetchone()
        return json.loads(row["script_json"]) if row else None

    def scripts(self, limit: int = 50) -> list[dict[str, Any]]:
        with closing(self.connect()) as connection:
            rows = connection.execute(
                "SELECT script_json FROM cq_scripts ORDER BY created_at DESC LIMIT ?",
                (max(1, min(limit, 500)),),
            ).fetchall()
        return [json.loads(row["script_json"]) for row in rows]

    @staticmethod
    def script_audit_sha256(script: dict[str, Any]) -> str:
        return hashlib.sha256(json.dumps(
            script, sort_keys=True, separators=(",", ":"), default=str,
        ).encode("utf-8")).hexdigest()

    def bind_script_audit_payload(
        self, payload: dict[str, Any]
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        """Bind a script-scoped audit to the immutable stored script.

        Ad-hoc audits remain available when no script_id is supplied, but only
        an audit whose payload exactly matches the stored script can affect its
        render gates.
        """
        if not isinstance(payload, dict):
            raise ValueError("a JSON object is required")
        script_id = str(payload.get("script_id") or "").strip()
        if not script_id:
            return dict(payload), {
                "stored_script_bound": False,
                "script_id": None,
                "script_sha256": None,
            }
        stored = self.script(script_id)
        if not isinstance(stored, dict):
            raise ValueError("script_id was not found")
        for field in self.SCRIPT_AUDIT_FIELDS:
            if field not in payload:
                continue
            supplied = json.dumps(
                payload.get(field), sort_keys=True, separators=(",", ":"),
                default=str,
            )
            expected = json.dumps(
                stored.get(field), sort_keys=True, separators=(",", ":"),
                default=str,
            )
            if supplied != expected:
                raise ValueError(f"{field} does not match the stored script")
        if "source_human_moment" in payload:
            supplied = json.dumps(
                payload.get("source_human_moment"), sort_keys=True,
                separators=(",", ":"), default=str,
            )
            expected = json.dumps(
                stored.get("human_moment"), sort_keys=True,
                separators=(",", ":"), default=str,
            )
            if supplied != expected:
                raise ValueError(
                    "source_human_moment does not match the stored script"
                )
        bound = dict(payload)
        bound.update({field: stored.get(field) for field in self.SCRIPT_AUDIT_FIELDS})
        bound["script_id"] = script_id
        return bound, {
            "contract": "stored_script_audit_binding_v1",
            "stored_script_bound": True,
            "script_id": script_id,
            "script_sha256": self.script_audit_sha256(stored),
        }

    def script_gate_summary(self, script_id: str) -> dict[str, Any]:
        stored_script = self.script(script_id)
        expected_script_sha256 = (
            self.script_audit_sha256(stored_script)
            if isinstance(stored_script, dict) else None
        )
        with closing(self.connect()) as connection:
            rows = connection.execute(
                """
                SELECT audit_type, decision, score, audit_id, findings_json,
                       created_at
                FROM cq_audits
                WHERE subject_id=?
                ORDER BY created_at DESC, rowid DESC
                """,
                (script_id,),
            ).fetchall()
        latest: dict[str, dict[str, Any]] = {}
        for row in rows:
            if row["audit_type"] not in latest:
                item = dict(row)
                findings = json.loads(item.pop("findings_json") or "{}")
                binding = findings.get("input_binding") or {}
                item["input_binding"] = binding
                item["stored_script_binding_valid"] = bool(
                    expected_script_sha256
                    and binding.get("stored_script_bound") is True
                    and binding.get("script_sha256")
                        == expected_script_sha256
                )
                latest[row["audit_type"]] = item
        accepted = {
            "narrative_coherence": ("PASS",),
            "relatability_script": ("PASS",),
            "relatability_ai_qualitative": (
                "PASS", NON_AI_PASS_DECISION,
            ),
            "relatability_transcript_cohort": ("PASS",),
            "attention_script": ("PASS",),
            "attention_video_preflight": ("PASS",),
        }
        # Existing stored scripts predate aggregate style receipts. New scripts
        # always carry a style guide and cannot become render-ready without its
        # bound audit; historical scripts retain their original six-gate contract.
        if isinstance(stored_script, dict) and stored_script.get(
            "style_guide_receipt_id"
        ):
            accepted["transcript_style_fit"] = ("PASS",)
        if isinstance(stored_script, dict) and stored_script.get(
            "owner_quality_contract"
        ):
            accepted["owner_calibrated_quality"] = ("PASS",)
        return {
            "ready_for_render": all(
                latest.get(kind, {}).get("decision") in decisions
                and latest.get(kind, {}).get("stored_script_binding_valid")
                for kind, decisions in accepted.items()
            ),
            "required_decisions": {
                kind: list(decisions) for kind, decisions in accepted.items()
            },
            "latest_audits": latest,
        }

    def put_audit(
        self,
        audit_type: str,
        subject_id: str | None,
        decision: str,
        score: float,
        findings: dict[str, Any],
    ) -> dict[str, Any]:
        created_at = utc_now()
        # Every evaluation is an immutable attempt.  Reusing an ID derived
        # from verdict content can resurrect an older timestamp and make a
        # newer provider failure appear to leave an earlier PASS current.
        audit_id = "audit_" + uuid.uuid4().hex
        with closing(self.connect()) as connection:
            connection.execute(
                """
                INSERT INTO cq_audits
                    (audit_id, audit_type, subject_id, decision, score, findings_json, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (audit_id, audit_type, subject_id, decision, score, json.dumps(findings, sort_keys=True), created_at),
            )
            connection.commit()
            row = connection.execute(
                "SELECT * FROM cq_audits WHERE audit_id=?", (audit_id,)
            ).fetchone()
        return {
            "audit_id": row["audit_id"],
            "audit_type": row["audit_type"],
            "subject_id": row["subject_id"],
            "decision": row["decision"],
            "score": round(float(row["score"]), 1),
            "findings": json.loads(row["findings_json"]),
            "created_at": row["created_at"],
        }

    def put_script_brief(self, brief: dict[str, Any], receipt_id: str) -> dict[str, Any]:
        with closing(self.connect()) as connection:
            connection.execute(
                """
                INSERT INTO cq_script_briefs(
                    brief_id, contract, trend_id, snapshot_id, status,
                    receipt_id, payload_json, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(brief_id) DO NOTHING
                """,
                (
                    brief["brief_id"], brief["contract"], brief["trend"]["trend_id"],
                    brief["database_snapshot"]["snapshot_id"], brief["status"],
                    receipt_id, json.dumps(brief, sort_keys=True), brief["created_at"],
                ),
            )
            connection.commit()
            row = connection.execute(
                "SELECT payload_json FROM cq_script_briefs WHERE brief_id=?",
                (brief["brief_id"],),
            ).fetchone()
        return json.loads(row["payload_json"])

    def script_brief(self, brief_id: str) -> dict[str, Any] | None:
        with closing(self.connect()) as connection:
            row = connection.execute(
                "SELECT payload_json FROM cq_script_briefs WHERE brief_id=?",
                (brief_id,),
            ).fetchone()
        return json.loads(row["payload_json"]) if row else None

    def script_brief_receipt_id(self, brief_id: str) -> str | None:
        with closing(self.connect()) as connection:
            row = connection.execute(
                "SELECT receipt_id FROM cq_script_briefs WHERE brief_id=?",
                (brief_id,),
            ).fetchone()
        return str(row["receipt_id"]) if row else None

    def script_briefs(self, limit: int = 50) -> list[dict[str, Any]]:
        with closing(self.connect()) as connection:
            rows = connection.execute(
                "SELECT payload_json FROM cq_script_briefs ORDER BY created_at DESC LIMIT ?",
                (max(1, min(limit, 200)),),
            ).fetchall()
        return [json.loads(row["payload_json"]) for row in rows]

    def put_workflow_run(self, run: dict[str, Any]) -> dict[str, Any]:
        with closing(self.connect()) as connection:
            connection.execute(
                """
                INSERT INTO cq_workflow_runs(
                    workflow_id, brief_id, script_id, state, stage_receipts_json,
                    result_json, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(workflow_id) DO NOTHING
                """,
                (
                    run["workflow_id"], run["brief_id"], run.get("script_id"),
                    run["state"], json.dumps(run.get("stage_receipts") or {}, sort_keys=True),
                    json.dumps(run.get("result") or {}, sort_keys=True), run["created_at"],
                ),
            )
            connection.commit()
        return run

    def workflow_runs(
        self,
        *,
        brief_id: str | None = None,
        script_id: str | None = None,
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        clauses: list[str] = []
        parameters: list[Any] = []
        if brief_id:
            clauses.append("brief_id=?")
            parameters.append(brief_id)
        if script_id:
            clauses.append("script_id=?")
            parameters.append(script_id)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        parameters.append(max(1, min(limit, 200)))
        with closing(self.connect()) as connection:
            rows = connection.execute(
                f"""SELECT workflow_id, brief_id, script_id, state,
                           stage_receipts_json, result_json, created_at
                    FROM cq_workflow_runs {where}
                    ORDER BY created_at DESC LIMIT ?""",
                parameters,
            ).fetchall()
        return [
            {
                "workflow_id": row["workflow_id"],
                "brief_id": row["brief_id"],
                "script_id": row["script_id"],
                "state": row["state"],
                "stage_receipts": json.loads(row["stage_receipts_json"]),
                "result": json.loads(row["result_json"]),
                "created_at": row["created_at"],
            }
            for row in rows
        ]

    def put_agent_query(self, row: dict[str, Any]) -> dict[str, Any]:
        with closing(self.connect()) as connection:
            connection.execute(
                """
                INSERT INTO cq_agent_queries(
                    query_id, principal, operation, parameters_sha256,
                    response_sha256, outcome, row_count, duration_ms, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(query_id) DO NOTHING
                """,
                (
                    row["query_id"], row["principal"], row["operation"],
                    row["parameters_sha256"], row["response_sha256"], row["outcome"],
                    int(row.get("row_count") or 0), float(row.get("duration_ms") or 0.0),
                    row["created_at"],
                ),
            )
            connection.commit()
        return row

    @staticmethod
    def _owned_publication_row(row: sqlite3.Row) -> dict[str, Any]:
        return {
            "publication_id": str(row["publication_id"]),
            "contract": str(row["contract"]),
            "idempotency_key": str(row["idempotency_key"]),
            "attribution": {
                "content_id": str(row["content_id"]),
                "campaign_id": str(row["campaign_id"]),
                "offer_id": str(row["offer_id"]),
                "source_platform": str(row["source_platform"]),
                "source_id": str(row["provider_post_id"]),
            },
            "semantic_asset": {
                "asset_id": str(row["semantic_asset_id"]),
                "asset_sha256": str(row["semantic_asset_sha256"]),
            },
            "local_asset": {
                "path": str(row["local_asset_path"]),
                "sha256": str(row["local_asset_sha256"]),
                "bytes": int(row["local_asset_bytes"]),
            },
            "account_id": str(row["account_id"]),
            "publisher": str(row["publisher"]),
            "provider_post_id": str(row["provider_post_id"]),
            "provider_post_url": str(row["provider_post_url"]),
            "published_at": str(row["published_at"]),
            "provider_receipt_id": str(row["provider_receipt_id"]),
            "provider_receipt_sha256": str(row["provider_receipt_sha256"]),
            "publication_receipt_sha256": str(
                row["publication_receipt_sha256"]
            ),
            "metadata": json.loads(row["payload_json"]).get("metadata", {}),
            "created_at": str(row["created_at"]),
        }

    def put_owned_publication(
        self, publication: dict[str, Any]
    ) -> tuple[dict[str, Any], bool]:
        payload_json = json.dumps(
            publication, sort_keys=True, separators=(",", ":")
        )
        receipt_sha256 = hashlib.sha256(payload_json.encode("utf-8")).hexdigest()
        publication_id = stable_id("publication", publication["idempotency_key"])
        created_at = utc_now()
        with closing(self.connect()) as connection:
            existing = connection.execute(
                "SELECT * FROM cq_owned_publication_receipts WHERE idempotency_key=?",
                (publication["idempotency_key"],),
            ).fetchone()
            if existing is not None:
                if str(existing["publication_receipt_sha256"]) != receipt_sha256:
                    raise IdempotencyConflict(
                        "idempotency_key already identifies a different publication receipt"
                    )
                return self._owned_publication_row(existing), False
            try:
                connection.execute(
                    """
                    INSERT INTO cq_owned_publication_receipts(
                        publication_id, contract, idempotency_key, content_id,
                        campaign_id, offer_id, semantic_asset_id,
                        semantic_asset_sha256, local_asset_path,
                        local_asset_sha256, local_asset_bytes, source_platform,
                        account_id, publisher, provider_post_id,
                        provider_post_url, published_at, provider_receipt_id,
                        provider_receipt_sha256, publication_receipt_sha256,
                        payload_json, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                              ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        publication_id, OWNED_PUBLICATION_RECEIPT_CONTRACT,
                        publication["idempotency_key"], publication["content_id"],
                        publication["campaign_id"], publication["offer_id"],
                        publication["semantic_asset_id"],
                        publication["semantic_asset_sha256"],
                        publication["local_asset_path"],
                        publication["local_asset_sha256"],
                        publication["local_asset_bytes"],
                        publication["source_platform"], publication["account_id"],
                        publication["publisher"], publication["provider_post_id"],
                        publication["provider_post_url"], publication["published_at"],
                        publication["provider_receipt_id"],
                        publication["provider_receipt_sha256"], receipt_sha256,
                        payload_json, created_at,
                    ),
                )
            except sqlite3.IntegrityError as exc:
                provider = connection.execute(
                    """SELECT 1 FROM cq_owned_publication_receipts
                       WHERE source_platform=? AND provider_post_id=?""",
                    (publication["source_platform"], publication["provider_post_id"]),
                ).fetchone()
                account = connection.execute(
                    """SELECT 1 FROM cq_owned_publication_receipts
                       WHERE content_id=? AND source_platform=? AND account_id=?""",
                    (
                        publication["content_id"], publication["source_platform"],
                        publication["account_id"],
                    ),
                ).fetchone()
                if provider is not None:
                    raise IdempotencyConflict(
                        "provider post is already bound to a different publication receipt"
                    ) from exc
                if account is not None:
                    raise IdempotencyConflict(
                        "content is already bound to a publication on this platform account"
                    ) from exc
                raise
            row = connection.execute(
                "SELECT * FROM cq_owned_publication_receipts WHERE publication_id=?",
                (publication_id,),
            ).fetchone()
            if row is None:
                raise sqlite3.IntegrityError(
                    "owned publication receipt insert was not readable"
                )
            connection.commit()
        return self._owned_publication_row(row), True

    def owned_publication(
        self,
        *,
        publication_id: str | None = None,
        publication_receipt_sha256: str | None = None,
    ) -> dict[str, Any] | None:
        if not publication_id and not publication_receipt_sha256:
            raise ValueError(
                "publication_id or publication_receipt_sha256 is required"
            )
        clauses: list[str] = []
        parameters: list[str] = []
        if publication_id:
            clauses.append("publication_id=?")
            parameters.append(publication_id)
        if publication_receipt_sha256:
            clauses.append("publication_receipt_sha256=?")
            parameters.append(publication_receipt_sha256)
        with closing(self.connect()) as connection:
            row = connection.execute(
                f"""SELECT * FROM cq_owned_publication_receipts
                    WHERE {' AND '.join(clauses)}""",
                parameters,
            ).fetchone()
        return self._owned_publication_row(row) if row is not None else None

    def owned_publications(
        self, filters: dict[str, str], *, limit: int = 100
    ) -> list[dict[str, Any]]:
        clauses: list[str] = []
        parameters: list[Any] = []
        mapping = {
            "content_id": "content_id",
            "campaign_id": "campaign_id",
            "offer_id": "offer_id",
            "source_platform": "source_platform",
            "source_id": "provider_post_id",
            "account_id": "account_id",
            "publication_id": "publication_id",
        }
        for name, column in mapping.items():
            value = filters.get(name)
            if value:
                clauses.append(f"{column}=?")
                parameters.append(value)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        parameters.append(max(1, min(int(limit), 500)))
        with closing(self.connect()) as connection:
            rows = connection.execute(
                f"""SELECT * FROM cq_owned_publication_receipts {where}
                    ORDER BY published_at DESC, publication_id DESC LIMIT ?""",
                parameters,
            ).fetchall()
        return [self._owned_publication_row(row) for row in rows]

    def put_owned_outcome_event(
        self,
        event: dict[str, Any],
        publication: dict[str, Any] | None = None,
    ) -> tuple[dict[str, Any], bool]:
        payload_json = json.dumps(event, sort_keys=True, separators=(",", ":"))
        payload_sha256 = hashlib.sha256(payload_json.encode()).hexdigest()
        event_id = stable_id("outcome", event["idempotency_key"])
        created_at = utc_now()
        with closing(self.connect()) as connection:
            cursor = connection.execute(
                """
                INSERT INTO cq_owned_outcome_events(
                    event_id, contract, idempotency_key, event_type,
                    content_id, campaign_id, offer_id, source_platform,
                    source_id, journey_id, occurred_at, provider_event_id,
                    payload_sha256, payload_json, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(idempotency_key) DO NOTHING
                """,
                (
                    event_id, event["contract"],
                    event["idempotency_key"], event["event_type"],
                    event["content_id"], event["campaign_id"], event["offer_id"],
                    event["source_platform"], event["source_id"],
                    event["journey_id"], event["occurred_at"],
                    event.get("provider_event_id"), payload_sha256, payload_json,
                    created_at,
                ),
            )
            created = cursor.rowcount == 1
            if publication is not None:
                connection.execute(
                    """INSERT OR IGNORE INTO cq_owned_outcome_publication_bindings(
                           event_id, publication_id,
                           publication_receipt_sha256, contract, bound_at
                       ) VALUES (?, ?, ?, ?, ?)""",
                    (
                        event_id, publication["publication_id"],
                        publication["publication_receipt_sha256"],
                        OWNED_PUBLICATION_BINDING_CONTRACT, created_at,
                    ),
                )
                binding = connection.execute(
                    """SELECT publication_id, publication_receipt_sha256
                       FROM cq_owned_outcome_publication_bindings
                       WHERE event_id=?""",
                    (event_id,),
                ).fetchone()
                if (
                    binding is None
                    or str(binding["publication_id"])
                        != publication["publication_id"]
                    or str(binding["publication_receipt_sha256"])
                        != publication["publication_receipt_sha256"]
                ):
                    raise IdempotencyConflict(
                        "owned outcome event is bound to a different publication receipt"
                    )
            row = connection.execute(
                """SELECT event.*,
                          binding.publication_id AS bound_publication_id,
                          binding.publication_receipt_sha256
                           AS bound_publication_receipt_sha256
                   FROM cq_owned_outcome_events event
                   LEFT JOIN cq_owned_outcome_publication_bindings binding
                     ON binding.event_id=event.event_id
                   WHERE event.idempotency_key=?""",
                (event["idempotency_key"],),
            ).fetchone()
            if row is None:
                raise sqlite3.IntegrityError("owned outcome event insert was not readable")
            if row["payload_sha256"] != payload_sha256:
                raise IdempotencyConflict(
                    "idempotency_key already identifies a different owned outcome event"
                )
            connection.commit()
        return self._owned_event_row(row), created

    def put_owned_retention_sample(
        self,
        sample: dict[str, Any],
        publication: dict[str, Any] | None = None,
    ) -> tuple[dict[str, Any], bool]:
        payload_json = json.dumps(sample, sort_keys=True, separators=(",", ":"))
        payload_sha256 = hashlib.sha256(payload_json.encode()).hexdigest()
        sample_id = stable_id("retention", sample["idempotency_key"])
        created_at = utc_now()
        with closing(self.connect()) as connection:
            cursor = connection.execute(
                """
                INSERT INTO cq_owned_retention_samples(
                    sample_id, contract, idempotency_key, content_id,
                    campaign_id, offer_id, source_platform, source_id,
                    measurement_id, journey_id, observed_at, elapsed_ms,
                    retained_percent, sample_size, payload_sha256,
                    payload_json, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(idempotency_key) DO NOTHING
                """,
                (
                    sample_id, sample["contract"],
                    sample["idempotency_key"], sample["content_id"],
                    sample["campaign_id"], sample["offer_id"],
                    sample["source_platform"], sample["source_id"],
                    sample["measurement_id"], sample.get("journey_id"),
                    sample["observed_at"], sample["elapsed_ms"],
                    sample["retained_percent"], sample["sample_size"],
                    payload_sha256, payload_json, created_at,
                ),
            )
            created = cursor.rowcount == 1
            if publication is not None:
                connection.execute(
                    """INSERT OR IGNORE INTO cq_owned_retention_publication_bindings(
                           sample_id, publication_id,
                           publication_receipt_sha256, contract, bound_at
                       ) VALUES (?, ?, ?, ?, ?)""",
                    (
                        sample_id, publication["publication_id"],
                        publication["publication_receipt_sha256"],
                        OWNED_PUBLICATION_BINDING_CONTRACT, created_at,
                    ),
                )
                binding = connection.execute(
                    """SELECT publication_id, publication_receipt_sha256
                       FROM cq_owned_retention_publication_bindings
                       WHERE sample_id=?""",
                    (sample_id,),
                ).fetchone()
                if (
                    binding is None
                    or str(binding["publication_id"])
                        != publication["publication_id"]
                    or str(binding["publication_receipt_sha256"])
                        != publication["publication_receipt_sha256"]
                ):
                    raise IdempotencyConflict(
                        "owned retention sample is bound to a different publication receipt"
                    )
            row = connection.execute(
                """SELECT sample.*,
                          binding.publication_id AS bound_publication_id,
                          binding.publication_receipt_sha256
                           AS bound_publication_receipt_sha256
                   FROM cq_owned_retention_samples sample
                   LEFT JOIN cq_owned_retention_publication_bindings binding
                     ON binding.sample_id=sample.sample_id
                   WHERE sample.idempotency_key=?""",
                (sample["idempotency_key"],),
            ).fetchone()
            if row is None:
                raise sqlite3.IntegrityError("owned retention sample insert was not readable")
            if row["payload_sha256"] != payload_sha256:
                raise IdempotencyConflict(
                    "idempotency_key already identifies a different owned retention sample"
                )
            connection.commit()
        return self._owned_retention_row(row), created

    @staticmethod
    def _owned_event_row(row: sqlite3.Row) -> dict[str, Any]:
        result = {
            "event_id": row["event_id"],
            "contract": row["contract"],
            "idempotency_key": row["idempotency_key"],
            "event_type": row["event_type"],
            "attribution": {
                "content_id": row["content_id"],
                "campaign_id": row["campaign_id"],
                "offer_id": row["offer_id"],
                "source_platform": row["source_platform"],
                "source_id": row["source_id"],
            },
            "journey_id": row["journey_id"],
            "occurred_at": row["occurred_at"],
            "provider_event_id": row["provider_event_id"],
            "metadata": json.loads(row["payload_json"]).get("metadata", {}),
            "payload_sha256": row["payload_sha256"],
            "created_at": row["created_at"],
        }
        keys = set(row.keys())
        if "bound_publication_id" in keys and row["bound_publication_id"]:
            result["publication_binding"] = {
                "contract": OWNED_PUBLICATION_BINDING_CONTRACT,
                "publication_id": str(row["bound_publication_id"]),
                "publication_receipt_sha256": str(
                    row["bound_publication_receipt_sha256"]
                ),
            }
        else:
            result["publication_binding"] = None
        return result

    @staticmethod
    def _owned_retention_row(row: sqlite3.Row) -> dict[str, Any]:
        result = {
            "sample_id": row["sample_id"],
            "contract": row["contract"],
            "idempotency_key": row["idempotency_key"],
            "attribution": {
                "content_id": row["content_id"],
                "campaign_id": row["campaign_id"],
                "offer_id": row["offer_id"],
                "source_platform": row["source_platform"],
                "source_id": row["source_id"],
            },
            "measurement_id": row["measurement_id"],
            "journey_id": row["journey_id"],
            "observed_at": row["observed_at"],
            "elapsed_ms": int(row["elapsed_ms"]),
            "retained_percent": round(float(row["retained_percent"]), 4),
            "sample_size": int(row["sample_size"]),
            "metadata": json.loads(row["payload_json"]).get("metadata", {}),
            "payload_sha256": row["payload_sha256"],
            "created_at": row["created_at"],
        }
        keys = set(row.keys())
        if "bound_publication_id" in keys and row["bound_publication_id"]:
            result["publication_binding"] = {
                "contract": OWNED_PUBLICATION_BINDING_CONTRACT,
                "publication_id": str(row["bound_publication_id"]),
                "publication_receipt_sha256": str(
                    row["bound_publication_receipt_sha256"]
                ),
            }
        else:
            result["publication_binding"] = None
        return result

    @staticmethod
    def _owned_attribution_where(
        filters: dict[str, str], *, alias: str = ""
    ) -> tuple[str, list[Any]]:
        prefix = f"{alias}." if alias else ""
        clauses = [f"{prefix}content_id=?"]
        parameters: list[Any] = [filters["content_id"]]
        for field in ("campaign_id", "offer_id", "source_platform", "source_id"):
            value = filters.get(field)
            if value:
                clauses.append(f"{prefix}{field}=?")
                parameters.append(value)
        return " AND ".join(clauses), parameters

    def owned_outcome_events(
        self, filters: dict[str, str], *, limit: int = 200
    ) -> list[dict[str, Any]]:
        where, parameters = self._owned_attribution_where(filters)
        parameters.append(max(1, min(int(limit), 500)))
        with closing(self.connect()) as connection:
            rows = connection.execute(
                f"""SELECT event.*,
                           binding.publication_id AS bound_publication_id,
                           binding.publication_receipt_sha256
                            AS bound_publication_receipt_sha256
                    FROM cq_owned_outcome_events event
                    LEFT JOIN cq_owned_outcome_publication_bindings binding
                      ON binding.event_id=event.event_id
                    WHERE {where}
                    ORDER BY event.occurred_at, event.event_id LIMIT ?""",
                parameters,
            ).fetchall()
        return [self._owned_event_row(row) for row in rows]

    def owned_retention_samples(
        self, filters: dict[str, str], *, limit: int = 500
    ) -> list[dict[str, Any]]:
        where, parameters = self._owned_attribution_where(filters)
        parameters.append(max(1, min(int(limit), 2000)))
        with closing(self.connect()) as connection:
            rows = connection.execute(
                f"""SELECT sample.*,
                           binding.publication_id AS bound_publication_id,
                           binding.publication_receipt_sha256
                            AS bound_publication_receipt_sha256
                    FROM cq_owned_retention_samples sample
                    LEFT JOIN cq_owned_retention_publication_bindings binding
                      ON binding.sample_id=sample.sample_id
                    WHERE {where}
                    ORDER BY sample.elapsed_ms, sample.observed_at,
                             sample.sample_id LIMIT ?""",
                parameters,
            ).fetchall()
        return [self._owned_retention_row(row) for row in rows]

    def owned_outcome_rollup(self, filters: dict[str, str]) -> dict[str, Any]:
        where, parameters = self._owned_attribution_where(filters)
        with closing(self.connect()) as connection:
            rows = connection.execute(
                f"""SELECT event_type, COUNT(*) AS event_count,
                           COUNT(DISTINCT journey_id) AS unique_journeys,
                           MIN(occurred_at) AS first_observed_at,
                           MAX(occurred_at) AS last_observed_at
                    FROM cq_owned_outcome_events
                    WHERE {where}
                    GROUP BY event_type""",
                parameters,
            ).fetchall()
            linked: dict[str, int] = {}
            for previous, current in zip(
                OWNED_OUTCOME_EVENT_TYPES, OWNED_OUTCOME_EVENT_TYPES[1:]
            ):
                row = connection.execute(
                    f"""WITH scoped AS (
                            SELECT event_type, journey_id, occurred_at,
                                   campaign_id, offer_id, source_platform, source_id
                            FROM cq_owned_outcome_events WHERE {where}
                        )
                        SELECT COUNT(DISTINCT current.journey_id)
                        FROM scoped current
                        WHERE current.event_type=?
                          AND EXISTS (
                              SELECT 1 FROM scoped previous
                              WHERE previous.journey_id=current.journey_id
                                AND previous.event_type=?
                                AND previous.occurred_at <= current.occurred_at
                                AND previous.campaign_id=current.campaign_id
                                AND previous.offer_id=current.offer_id
                                AND previous.source_platform=current.source_platform
                                AND previous.source_id=current.source_id
                          )""",
                    [*parameters, current, previous],
                ).fetchone()
                linked[f"{previous}_to_{current}"] = int(row[0])
            chain_row = connection.execute(
                f"""WITH scoped AS (
                        SELECT event_type, journey_id, occurred_at,
                               campaign_id, offer_id, source_platform, source_id
                        FROM cq_owned_outcome_events WHERE {where}
                    )
                    SELECT COUNT(DISTINCT
                        click.journey_id || char(31) || click.campaign_id ||
                        char(31) || click.offer_id || char(31) ||
                        click.source_platform || char(31) || click.source_id
                    )
                    FROM scoped click
                    JOIN scoped install
                      ON install.journey_id=click.journey_id
                     AND install.campaign_id=click.campaign_id
                     AND install.offer_id=click.offer_id
                     AND install.source_platform=click.source_platform
                     AND install.source_id=click.source_id
                     AND install.event_type='install'
                     AND click.occurred_at <= install.occurred_at
                    JOIN scoped trial_event
                      ON trial_event.journey_id=install.journey_id
                     AND trial_event.campaign_id=install.campaign_id
                     AND trial_event.offer_id=install.offer_id
                     AND trial_event.source_platform=install.source_platform
                     AND trial_event.source_id=install.source_id
                     AND trial_event.event_type='trial'
                     AND install.occurred_at <= trial_event.occurred_at
                    JOIN scoped purchase
                      ON purchase.journey_id=trial_event.journey_id
                     AND purchase.campaign_id=trial_event.campaign_id
                     AND purchase.offer_id=trial_event.offer_id
                     AND purchase.source_platform=trial_event.source_platform
                     AND purchase.source_id=trial_event.source_id
                     AND purchase.event_type='purchase'
                     AND trial_event.occurred_at <= purchase.occurred_at
                    WHERE click.event_type='click'""",
                parameters,
            ).fetchone()
            click_scope_row = connection.execute(
                f"""SELECT COUNT(DISTINCT
                           journey_id || char(31) || campaign_id || char(31) ||
                           offer_id || char(31) || source_platform || char(31) ||
                           source_id
                       )
                    FROM cq_owned_outcome_events
                    WHERE {where} AND event_type='click'""",
                parameters,
            ).fetchone()
        by_type = {
            event_type: {
                "event_count": 0,
                "unique_journeys": 0,
                "first_observed_at": None,
                "last_observed_at": None,
            }
            for event_type in OWNED_OUTCOME_EVENT_TYPES
        }
        for row in rows:
            by_type[row["event_type"]] = {
                "event_count": int(row["event_count"]),
                "unique_journeys": int(row["unique_journeys"]),
                "first_observed_at": row["first_observed_at"],
                "last_observed_at": row["last_observed_at"],
            }
        complete_chain_journeys = int(chain_row[0])
        click_scope_journeys = int(click_scope_row[0])
        return {
            "by_type": by_type,
            "linked_journeys": linked,
            "complete_chain": {
                "required_sequence": ["click", "install", "trial", "purchase"],
                "complete_ordered_exact_scope_journeys": complete_chain_journeys,
                "click_exact_scope_journeys": click_scope_journeys,
                "observed_complete_chain_rate": (
                    round(complete_chain_journeys / click_scope_journeys, 6)
                    if click_scope_journeys else None
                ),
                "causal_effect": None,
            },
        }

    def owned_retention_rollup(
        self, filters: dict[str, str], *, point_limit: int = 2000
    ) -> dict[str, Any]:
        where, parameters = self._owned_attribution_where(filters)
        bounded_limit = max(1, min(int(point_limit), 2000))
        with closing(self.connect()) as connection:
            total_row = connection.execute(
                    f"""SELECT COUNT(*) AS fact_count,
                           COUNT(DISTINCT
                               content_id || char(31) || campaign_id || char(31) ||
                               offer_id || char(31) || source_platform || char(31) ||
                               source_id || char(31) || measurement_id || char(31) ||
                               CASE
                                   WHEN journey_id IS NULL THEN char(30)
                                   ELSE char(29) || journey_id
                               END
                           ) AS measurement_count,
                           COUNT(DISTINCT elapsed_ms) AS elapsed_point_count,
                           COUNT(DISTINCT
                               content_id || char(31) || campaign_id || char(31) ||
                               offer_id || char(31) || source_platform || char(31) ||
                               source_id || char(31) || measurement_id || char(31) ||
                               CASE
                                   WHEN journey_id IS NULL THEN char(30)
                                   ELSE char(29) || journey_id
                               END || char(31) ||
                               elapsed_ms
                           )
                               AS measurement_point_count,
                           MIN(observed_at) AS first_observed_at,
                           MAX(observed_at) AS last_observed_at
                    FROM cq_owned_retention_samples WHERE {where}""",
                parameters,
            ).fetchone()
            rows = connection.execute(
                f"""SELECT elapsed_ms,
                           SUM(retained_percent * sample_size) /
                               SUM(sample_size) AS retained_percent,
                           SUM(sample_size) AS represented_sample_size,
                           COUNT(*) AS fact_count,
                           COUNT(DISTINCT
                               content_id || char(31) || campaign_id || char(31) ||
                               offer_id || char(31) || source_platform || char(31) ||
                               source_id || char(31) || measurement_id || char(31) ||
                               CASE
                                   WHEN journey_id IS NULL THEN char(30)
                                   ELSE char(29) || journey_id
                               END
                           ) AS measurement_count,
                           MIN(observed_at) AS first_observed_at,
                           MAX(observed_at) AS last_observed_at
                    FROM cq_owned_retention_samples
                    WHERE {where}
                    GROUP BY elapsed_ms
                    ORDER BY elapsed_ms LIMIT ?""",
                [*parameters, bounded_limit],
            ).fetchall()
            measurement_rows = connection.execute(
                f"""SELECT content_id, campaign_id, offer_id,
                           source_platform, source_id, measurement_id, journey_id,
                           elapsed_ms,
                           SUM(retained_percent * sample_size) /
                               SUM(sample_size) AS retained_percent,
                           SUM(sample_size) AS represented_sample_size,
                           COUNT(*) AS fact_count,
                           MIN(observed_at) AS first_observed_at,
                           MAX(observed_at) AS last_observed_at
                    FROM cq_owned_retention_samples
                    WHERE {where}
                    GROUP BY content_id, campaign_id, offer_id,
                             source_platform, source_id, measurement_id, journey_id,
                             elapsed_ms
                    ORDER BY content_id, campaign_id, offer_id,
                             source_platform, source_id, measurement_id, journey_id,
                             elapsed_ms LIMIT ?""",
                [*parameters, bounded_limit],
            ).fetchall()
        measurement_curves: dict[
            tuple[str, str, str, str, str, str, str | None],
            list[dict[str, Any]],
        ] = {}
        for row in measurement_rows:
            curve_key = tuple(str(row[field]) for field in (
                "content_id", "campaign_id", "offer_id", "source_platform",
                "source_id", "measurement_id",
            )) + (row["journey_id"],)
            measurement_curves.setdefault(curve_key, []).append({
                "elapsed_ms": int(row["elapsed_ms"]),
                "retained_percent": round(float(row["retained_percent"]), 4),
                "represented_sample_size": int(row["represented_sample_size"]),
                "fact_count": int(row["fact_count"]),
                "first_observed_at": row["first_observed_at"],
                "last_observed_at": row["last_observed_at"],
            })
        return {
            "fact_count": int(total_row["fact_count"]),
            "measurement_count": int(total_row["measurement_count"]),
            "elapsed_point_count": int(total_row["elapsed_point_count"]),
            "measurement_point_count": int(total_row["measurement_point_count"]),
            "first_observed_at": total_row["first_observed_at"],
            "last_observed_at": total_row["last_observed_at"],
            "curve_truncated": int(total_row["measurement_point_count"]) > bounded_limit,
            "curve_semantics": (
                "points is a descriptive cross-measurement rollup; observed drops "
                "are computed only within each measurement_curve"
            ),
            "points": [
                {
                    "elapsed_ms": int(row["elapsed_ms"]),
                    "retained_percent": round(float(row["retained_percent"]), 4),
                    "represented_sample_size": int(row["represented_sample_size"]),
                    "fact_count": int(row["fact_count"]),
                    "measurement_count": int(row["measurement_count"]),
                    "first_observed_at": row["first_observed_at"],
                    "last_observed_at": row["last_observed_at"],
                }
                for row in rows
            ],
            "measurement_curves": [
                {
                    "measurement_id": curve_key[5],
                    "journey_id": curve_key[6],
                    "attribution": dict(zip(
                        (
                            "content_id", "campaign_id", "offer_id",
                            "source_platform", "source_id",
                        ),
                        curve_key[:5],
                    )),
                    "points": points,
                }
                for curve_key, points in measurement_curves.items()
            ],
        }

    def owned_outcome_readiness(self) -> dict[str, Any]:
        with closing(self.connect()) as connection:
            event_count = int(connection.execute(
                "SELECT COUNT(*) FROM cq_owned_outcome_events"
            ).fetchone()[0])
            retention_sample_count = int(connection.execute(
                "SELECT COUNT(*) FROM cq_owned_retention_samples"
            ).fetchone()[0])
            readiness_row = connection.execute(
                """WITH ordered_chains AS (
                       SELECT DISTINCT
                              click.content_id, click.campaign_id, click.offer_id,
                              click.source_platform, click.source_id,
                              click.journey_id
                       FROM cq_owned_outcome_events click
                       JOIN cq_owned_outcome_events install
                         ON install.content_id=click.content_id
                        AND install.campaign_id=click.campaign_id
                        AND install.offer_id=click.offer_id
                        AND install.source_platform=click.source_platform
                        AND install.source_id=click.source_id
                        AND install.journey_id=click.journey_id
                        AND install.event_type='install'
                        AND click.occurred_at <= install.occurred_at
                       JOIN cq_owned_outcome_events trial_event
                         ON trial_event.content_id=install.content_id
                        AND trial_event.campaign_id=install.campaign_id
                        AND trial_event.offer_id=install.offer_id
                        AND trial_event.source_platform=install.source_platform
                        AND trial_event.source_id=install.source_id
                        AND trial_event.journey_id=install.journey_id
                        AND trial_event.event_type='trial'
                        AND install.occurred_at <= trial_event.occurred_at
                       JOIN cq_owned_outcome_events purchase
                         ON purchase.content_id=trial_event.content_id
                        AND purchase.campaign_id=trial_event.campaign_id
                        AND purchase.offer_id=trial_event.offer_id
                        AND purchase.source_platform=trial_event.source_platform
                        AND purchase.source_id=trial_event.source_id
                        AND purchase.journey_id=trial_event.journey_id
                        AND purchase.event_type='purchase'
                        AND trial_event.occurred_at <= purchase.occurred_at
                       WHERE click.event_type='click'
                   ), eligible_curves AS (
                       SELECT content_id, campaign_id, offer_id,
                              source_platform, source_id, measurement_id,
                              journey_id
                       FROM cq_owned_retention_samples
                       GROUP BY content_id, campaign_id, offer_id,
                                source_platform, source_id, measurement_id,
                                journey_id
                       HAVING COUNT(DISTINCT elapsed_ms) >= 2
                   ), ready_scopes AS (
                       SELECT DISTINCT
                              chain.content_id, chain.campaign_id, chain.offer_id,
                              chain.source_platform, chain.source_id
                       FROM ordered_chains chain
                       JOIN eligible_curves curve
                         ON curve.content_id=chain.content_id
                        AND curve.campaign_id=chain.campaign_id
                        AND curve.offer_id=chain.offer_id
                        AND curve.source_platform=chain.source_platform
                        AND curve.source_id=chain.source_id
                        AND (
                            curve.journey_id IS NULL
                            OR curve.journey_id=chain.journey_id
                        )
                   )
                   SELECT
                       (SELECT COUNT(*) FROM ordered_chains)
                           AS complete_journey_count,
                       (SELECT COUNT(*) FROM eligible_curves)
                           AS same_measurement_curve_count,
                       (SELECT COUNT(*) FROM ready_scopes)
                           AS ready_scope_count"""
            ).fetchone()
        complete_journey_count = int(readiness_row["complete_journey_count"])
        same_measurement_curve_count = int(
            readiness_row["same_measurement_curve_count"]
        )
        ready_scope_count = int(readiness_row["ready_scope_count"])
        if ready_scope_count:
            status = "ready"
        elif event_count or retention_sample_count:
            status = "partial"
        else:
            status = "no_owned_outcomes"
        return {
            "status": status,
            "outcome_event_count": event_count,
            "retention_sample_count": retention_sample_count,
            "complete_ordered_exact_scope_journey_count": complete_journey_count,
            "same_measurement_retention_curve_count": same_measurement_curve_count,
            "linked_complete_chain_retention_curve_scope_count": ready_scope_count,
            # Compatibility alias for existing health consumers. Its value now
            # follows the stricter complete-chain/same-measurement contract.
            "linked_click_retention_curve_scope_count": ready_scope_count,
            "readiness_requirement": (
                "an ordered exact-scope click-to-install-to-trial-to-purchase "
                "journey and at least two observed elapsed points within one "
                "measurement_id in that same content/campaign/offer/source scope; "
                "a journey-bound curve must match that journey, while a null "
                "journey_id is explicitly aggregate-scope"
            ),
            "causal_drop_reasons_available": False,
        }

    def counts(self) -> dict[str, int]:
        with closing(self.connect()) as connection:
            return {
                table: int(connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
                for table in (
                    "cq_receipts", "cq_scripts", "cq_audits", "cq_retention",
                    "cq_script_briefs", "cq_workflow_runs", "cq_agent_queries",
                    "cq_owned_outcome_events", "cq_owned_retention_samples",
                    "cq_owned_publication_receipts",
                    "cq_owned_outcome_publication_bindings",
                    "cq_owned_retention_publication_bindings",
                    "cq_owned_content_metric_snapshots",
                )
            }


class MarketTapeReader:
    def __init__(self, path: str | Path):
        self.path = Path(path).expanduser()

    # ── evidence tiers ────────────────────────────────────────────────
    # The v11 tape exposes accepted-evidence views; a backfilled tape may hold
    # only metric-scope evidence (no descriptive lineage yet); an older tape
    # has no views at all. Every reader path names the tier it used so a
    # receipt can say what its topic match rested on.
    def _tape_shape(self, connection: sqlite3.Connection) -> dict[str, Any]:
        names = {
            row[0] for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type IN ('table', 'view')"
            ).fetchall()
        }
        observation_columns = {
            row[1] for row in connection.execute(
                "PRAGMA table_info(mt_market_observations)"
            ).fetchall()
        }
        has_metric_view = "mt_accepted_metric_observations_v1" in names
        has_full_view = "mt_accepted_full_evidence_v1" in names
        full_rows = (
            connection.execute("SELECT COUNT(*) FROM mt_accepted_full_evidence_v1").fetchone()[0]
            if has_full_view else 0
        )
        quality_table = "mt_observation_quality_flags" in names
        confidence_predicate = (
            "observation.source_confidence > 0"
            if "source_confidence" in observation_columns else "1 = 1"
        )
        quality_predicate = (
            f"""{confidence_predicate} AND NOT EXISTS (
                   SELECT 1 FROM mt_observation_quality_flags quality
                   WHERE quality.observation_id = observation.observation_id
               )"""
            if quality_table else confidence_predicate
        )
        if has_metric_view and full_rows > 0:
            tier = "full"
        elif has_metric_view:
            tier = "metric_only"
        else:
            tier = "legacy"
        observation_source = (
            "mt_accepted_metric_observations_v1" if has_metric_view else
            f"(SELECT observation.* FROM mt_market_observations observation WHERE {quality_predicate})"
        )
        return {
            "tier": tier,
            "has_metric_view": has_metric_view,
            "has_full_view": has_full_view,
            "full_rows": int(full_rows),
            "quality_table": quality_table,
            "quality_predicate": quality_predicate,
            "observation_source": observation_source,
        }

    def health(self) -> dict[str, Any]:
        if not self.path.exists():
            return {"status": "down", "path": str(self.path), "error": "market_tape_database_missing"}
        try:
            with closing(self.connect()) as connection:
                shape = self._tape_shape(connection)
                videos = connection.execute("SELECT COUNT(*) FROM mt_videos").fetchone()[0]
                observations = connection.execute("SELECT COUNT(*) FROM mt_market_observations").fetchone()[0]
                quarantined = (
                    connection.execute(
                        "SELECT COUNT(*) FROM mt_observation_quality_flags"
                    ).fetchone()[0]
                    if shape["quality_table"] else 0
                )
                analytics_eligible = connection.execute(
                    f"SELECT COUNT(*) FROM {shape['observation_source']}"
                ).fetchone()[0]
                transcripts = connection.execute(
                    f"""SELECT COUNT(DISTINCT genome.video_id)
                        FROM mt_content_genomes genome
                        JOIN {shape['observation_source']} observation
                          ON observation.video_id = genome.video_id
                        WHERE length(trim(COALESCE(genome.transcript, ''))) > 0"""
                ).fetchone()[0]
            return {
                "status": "up",
                "path": str(self.path),
                "videos": int(videos),
                "observations": int(observations),
                "analytics_eligible_observations": int(analytics_eligible),
                "quarantined_observations": int(quarantined),
                "transcripts": int(transcripts),
                "evidence_tier": shape["tier"],
                "full_evidence_rows": shape["full_rows"],
            }
        except (sqlite3.Error, OSError) as exc:
            return {"status": "down", "path": str(self.path), "error": str(exc)}

    def connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(f"file:{self.path}?mode=ro", uri=True, timeout=15)
        connection.row_factory = sqlite3.Row
        return connection

    def semantic_content_asset(self, content_id: str) -> dict[str, Any] | None:
        """Resolve exactly one canonical semantic parent asset."""
        if not self.path.is_file():
            raise ValueError("market tape database is unavailable")
        try:
            with closing(self.connect()) as connection:
                table = connection.execute(
                    """SELECT 1 FROM sqlite_master
                       WHERE type='table' AND name='mt_content_assets'"""
                ).fetchone()
                if table is None:
                    raise ValueError(
                        "market tape semantic content assets are unavailable"
                    )
                rows = connection.execute(
                    """SELECT asset_id, brief_id, graph_version_id,
                              atomic_topic_id, parent_asset_id, platform,
                              account, content_id, asset_contract,
                              asset_sha256, status, lineage_sha256,
                              source_service, source_receipt_id, registered_at
                       FROM mt_content_assets WHERE content_id=?
                       ORDER BY registered_at DESC LIMIT 2""",
                    (content_id,),
                ).fetchall()
        except sqlite3.Error as exc:
            raise ValueError(
                f"market tape semantic asset lookup failed: {exc}"
            ) from exc
        if not rows:
            return None
        if len(rows) != 1:
            raise ValueError(
                "content_id resolves to more than one semantic content asset"
            )
        row = rows[0]
        if row["parent_asset_id"] is not None:
            raise ValueError("content_id must resolve to a semantic parent asset")
        return {name: row[name] for name in row.keys()}

    def candidates(self, topic: str, limit: int = 20) -> list[dict[str, Any]]:
        tokens = [
            token.lower() for token in words(topic)
            if (len(token) > 2 or token.lower() == "ai") and token.lower() not in STOP_WORDS
        ][:6]
        # Descriptive text comes from accepted full evidence when the tape has
        # it, otherwise from mt_videos (labelled so). It is only used to match
        # the topic; persistence downstream still demands a Whisper artifact.
        descriptive = {
            "title": "COALESCE(e.title, v.title)",
            "caption": "COALESCE(e.caption, v.caption)",
            "description": "COALESCE(e.description, v.description)",
            "url": "COALESCE(e.url, v.url)",
            "duration_seconds": "COALESCE(e.duration_seconds, v.duration_seconds)",
        }
        where = ""
        params: list[Any] = []
        if tokens:
            clauses = []
            for token in tokens:
                clauses.append(
                    f"lower(COALESCE({descriptive['title']},'') || ' ' || "
                    f"COALESCE({descriptive['caption']},'') || ' ' || "
                    f"COALESCE({descriptive['description']},'') || ' ' || "
                    "COALESCE(g.transcript,'')) LIKE ?"
                )
                params.append(f"%{token}%")
            where = "WHERE (" + " OR ".join(clauses) + ")"
        # Pull a wider ranked window, then enforce multi-token topic relevance in Python.
        # A raw OR query let globally strong but unrelated videos through on words such as
        # "for"; those could become persisted evidence receipts. Stop words are excluded
        # above and multi-word topics must match at least two meaningful terms.
        params.append(max(1, min(limit * 5, 500)))
        with closing(self.connect()) as connection:
            shape = self._tape_shape(connection)
            tier = shape["tier"]
            if shape["has_full_view"]:
                evidence_join = (
                    "LEFT JOIN mt_accepted_full_evidence_v1 e ON e.observation_id = o.observation_id"
                )
                scope_expr = f"CASE WHEN e.observation_id IS NULL THEN '{tier}' ELSE 'full' END"
            else:
                evidence_join = (
                    "LEFT JOIN (SELECT NULL AS observation_id, NULL AS title, NULL AS caption, "
                    "NULL AS description, NULL AS url, NULL AS duration_seconds) e "
                    "ON e.observation_id = o.observation_id"
                )
                scope_expr = f"'{tier}'"
            query = f"""
            WITH latest AS (
                SELECT o.*,
                       ROW_NUMBER() OVER (
                           PARTITION BY o.video_id
                           ORDER BY o.observed_at DESC, o.observation_id DESC
                       ) AS row_number
                FROM {shape['observation_source']} o
            )
            SELECT v.video_id, v.platform, v.external_id, v.creator_id,
                   {descriptive['title']} AS title, {descriptive['caption']} AS caption,
                   {descriptive['description']} AS description, {descriptive['url']} AS url,
                   {descriptive['duration_seconds']} AS duration_seconds, v.first_seen_at,
                   COALESCE(g.transcript, '') AS transcript,
                   COALESCE(g.opening_words, '') AS opening_words,
                   COALESCE(g.hook_type, '') AS hook_type,
                   COALESCE(o.views, 0) AS views, COALESCE(o.likes, 0) AS likes,
                   COALESCE(o.comments, 0) AS comments, COALESCE(o.shares, 0) AS shares,
                   COALESCE(o.view_velocity, 0) AS velocity,
                   COALESCE(o.view_acceleration, 0) AS acceleration,
                   COALESCE(o.relative_strength, 0) AS relative_strength,
                   o.observation_key, o.observed_at,
                   {scope_expr} AS evidence_scope,
                   CASE WHEN e.observation_id IS NULL THEN 'mt_videos' ELSE 'accepted_evidence' END
                       AS descriptive_source
            FROM mt_videos v
            JOIN latest o ON o.video_id=v.video_id AND o.row_number=1
            {evidence_join}
            LEFT JOIN mt_content_genomes g ON g.video_id=v.video_id
            {where}
            ORDER BY CASE WHEN e.observation_id IS NULL THEN 1 ELSE 0 END,
                     CASE WHEN length(trim(COALESCE(g.transcript, ''))) > 0 THEN 0 ELSE 1 END,
                     COALESCE(o.relative_strength, 0) DESC,
                     COALESCE(o.view_velocity, 0) DESC,
                     COALESCE(o.views, 0) DESC
            LIMIT ?
        """
            rows = connection.execute(query, params).fetchall()
        result = [dict(row) for row in rows]
        if tokens:
            minimum_matches = 1 if len(tokens) == 1 else 2
            qualified = []
            for row in result:
                source_words = {
                    token.lower() for token in words(" ".join(
                        str(row.get(field) or "")
                        for field in ("title", "caption", "description", "transcript")
                    ))
                }
                match_count = sum(token in source_words for token in tokens)
                if match_count >= minimum_matches:
                    row["topic_match_count"] = match_count
                    qualified.append(row)
            result = qualified
        return result[:limit]

    def transcript_candidates(
        self,
        topic: str,
        limit: int = 500,
    ) -> list[dict[str, Any]]:
        """Search only accepted, artifact-bound language evidence.

        Trend discovery needs the whole Market Tape; script language does not.
        Scanning every latest market observation to find the small subset with a
        Whisper artifact made a cold product request take nearly two minutes.
        This lane starts at the immutable artifact ledger, uses accepted evidence
        only when resolving each artifact, and retains the same exact-word topic
        qualification as :meth:`candidates`.
        """

        bounded_limit = max(1, min(int(limit), 500))
        tokens = [
            token.lower() for token in words(topic)
            if (len(token) > 2 or token.lower() == "ai")
            and token.lower() not in STOP_WORDS
        ][:6]
        clauses: list[str] = []
        parameters: list[Any] = []
        searchable = (
            "COALESCE(evidence.title, video.title, '') || ' ' || "
            "COALESCE(evidence.caption, video.caption, '') || ' ' || "
            "COALESCE(evidence.description, video.description, '') || ' ' || "
            "COALESCE(genome.transcript, '')"
        )
        for token in tokens:
            clauses.append(f"lower({searchable}) LIKE ?")
            parameters.append(f"%{token}%")
        where = "WHERE (" + " OR ".join(clauses) + ")" if clauses else ""
        # Widen small calls before exact-word filtering, but keep one bounded
        # artifact lookup compatible with SQLite's parameter limits.
        parameters.append(max(bounded_limit, min(500, bounded_limit * 5)))
        with closing(self.connect()) as connection:
            shape = self._tape_shape(connection)
            if shape["has_full_view"]:
                evidence_join = (
                    "LEFT JOIN mt_accepted_full_evidence_v1 evidence "
                    "ON evidence.video_id = artifact.video_id "
                    "AND evidence.observation_key = artifact.observation_key"
                )
            else:
                evidence_join = (
                    "LEFT JOIN (SELECT NULL AS video_id, NULL AS observation_key, "
                    "NULL AS title, NULL AS caption, NULL AS description) evidence "
                    "ON 1 = 0"
                )
            rows = connection.execute(
                f"""
                SELECT artifact.video_id, MAX(artifact.created_at) AS created_at
                FROM mt_transcript_artifacts artifact
                JOIN mt_transcript_payload_snapshots payload_snapshot
                  ON payload_snapshot.transcript_id=artifact.transcript_id
                JOIN mt_videos video ON video.video_id = artifact.video_id
                {evidence_join}
                LEFT JOIN mt_content_genomes genome
                  ON genome.video_id = artifact.video_id
                {where}
                GROUP BY artifact.video_id
                ORDER BY created_at DESC, artifact.video_id
                LIMIT ?
                """,
                parameters,
            ).fetchall()
        resolved = self.production_artifact_bound_candidates([
            str(row["video_id"]) for row in rows
        ])
        if tokens:
            minimum_matches = 1 if len(tokens) == 1 else 2
            qualified: list[dict[str, Any]] = []
            for row in resolved:
                source_words = {
                    token.lower() for token in words(" ".join(
                        str(row.get(field) or "")
                        for field in (
                            "title", "caption", "description", "transcript",
                        )
                    ))
                }
                match_count = sum(token in source_words for token in tokens)
                if match_count >= minimum_matches:
                    row["topic_match_count"] = match_count
                    qualified.append(row)
            resolved = qualified
        return resolved[:bounded_limit]

    def artifact_bound_candidates(
        self,
        video_ids: Sequence[str],
    ) -> list[dict[str, Any]]:
        """Return each video's newest artifact and its original accepted snapshot.

        A later monotonic metric recheck must not invalidate a transcript that was
        already hash-bound and audited against an earlier accepted observation.
        Fresh trend selection and immutable transcript qualification are separate
        clocks, so this query joins the artifact back to *its* observation_key.
        """

        identifiers = list(dict.fromkeys(str(value) for value in video_ids if value))[:500]
        if not identifiers:
            return []
        marks = ",".join("?" for _ in identifiers)
        with closing(self.connect()) as connection:
            shape = self._tape_shape(connection)
            if shape["has_full_view"]:
                evidence_join = (
                    "LEFT JOIN mt_accepted_full_evidence_v1 evidence "
                    "ON evidence.observation_id = observation.observation_id"
                )
                scope_expr = (
                    "CASE WHEN evidence.observation_id IS NULL "
                    f"THEN '{shape['tier']}' ELSE 'full' END"
                )
            else:
                evidence_join = (
                    "LEFT JOIN (SELECT NULL AS observation_id, NULL AS title, "
                    "NULL AS caption, NULL AS description, NULL AS url, "
                    "NULL AS duration_seconds) evidence "
                    "ON evidence.observation_id = observation.observation_id"
                )
                scope_expr = f"'{shape['tier']}'"
            rows = connection.execute(
                f"""
                WITH accepted_artifacts AS (
                    SELECT artifact.video_id, artifact.observation_key,
                           artifact.created_at,
                           ROW_NUMBER() OVER (
                               PARTITION BY artifact.video_id
                               ORDER BY artifact.created_at DESC,
                                        artifact.transcript_id DESC
                           ) AS row_number
                    FROM mt_transcript_artifacts artifact
                    JOIN {shape['observation_source']} accepted
                      ON accepted.video_id = artifact.video_id
                     AND accepted.observation_key = artifact.observation_key
                    WHERE artifact.video_id IN ({marks})
                )
                SELECT video.video_id, video.platform, video.external_id,
                       video.creator_id,
                       COALESCE(evidence.title, video.title) AS title,
                       COALESCE(evidence.caption, video.caption) AS caption,
                       COALESCE(evidence.description, video.description) AS description,
                       COALESCE(evidence.url, video.url) AS url,
                       COALESCE(evidence.duration_seconds, video.duration_seconds)
                           AS duration_seconds,
                       video.first_seen_at,
                       COALESCE(genome.transcript, '') AS transcript,
                       COALESCE(genome.opening_words, '') AS opening_words,
                       COALESCE(genome.hook_type, '') AS hook_type,
                       COALESCE(observation.views, 0) AS views,
                       COALESCE(observation.likes, 0) AS likes,
                       COALESCE(observation.comments, 0) AS comments,
                       COALESCE(observation.shares, 0) AS shares,
                       COALESCE(observation.view_velocity, 0) AS velocity,
                       COALESCE(observation.view_acceleration, 0) AS acceleration,
                       COALESCE(observation.relative_strength, 0) AS relative_strength,
                       observation.observation_key, observation.observed_at,
                       {scope_expr} AS evidence_scope,
                       CASE WHEN evidence.observation_id IS NULL THEN 'mt_videos'
                            ELSE 'accepted_evidence' END AS descriptive_source
                FROM accepted_artifacts artifact_pick
                JOIN {shape['observation_source']} observation
                  ON observation.video_id = artifact_pick.video_id
                 AND observation.observation_key = artifact_pick.observation_key
                JOIN mt_videos video ON video.video_id = artifact_pick.video_id
                {evidence_join}
                LEFT JOIN mt_content_genomes genome
                  ON genome.video_id = artifact_pick.video_id
                WHERE artifact_pick.row_number = 1
                ORDER BY COALESCE(observation.relative_strength, 0) DESC,
                         COALESCE(observation.view_velocity, 0) DESC,
                         COALESCE(observation.views, 0) DESC,
                         video.video_id
                """,
                identifiers,
            ).fetchall()
        return [dict(row) for row in rows]

    def production_artifact_bound_candidates(
        self,
        video_ids: Sequence[str],
    ) -> list[dict[str, Any]]:
        """Return only artifacts that can pass the production cohort audit.

        Legacy performance-passing rows may predate the immutable acquisition
        payload snapshot.  They remain valid historical evidence, but they must
        not enter a production brief because the later script audit cannot
        attest them.  Apply the shared immutable attestation first, then select
        the newest valid artifact per video without reading its legacy file
        path.
        """

        identifiers = list(dict.fromkeys(
            str(value) for value in video_ids if value
        ))[:500]
        if not identifiers:
            return []
        marks = ",".join("?" for _ in identifiers)
        try:
            with closing(self.connect()) as connection:
                shape = self._tape_shape(connection)
                if shape["has_full_view"]:
                    evidence_join = (
                        "LEFT JOIN mt_accepted_full_evidence_v1 evidence "
                        "ON evidence.observation_id = observation.observation_id"
                    )
                    scope_expr = (
                        "CASE WHEN evidence.observation_id IS NULL "
                        f"THEN '{shape['tier']}' ELSE 'full' END"
                    )
                else:
                    evidence_join = (
                        "LEFT JOIN (SELECT NULL AS observation_id, "
                        "NULL AS title, NULL AS caption, NULL AS description, "
                        "NULL AS url, NULL AS duration_seconds) evidence "
                        "ON evidence.observation_id = observation.observation_id"
                    )
                    scope_expr = f"'{shape['tier']}'"
                rows = connection.execute(
                    f"""
                    SELECT video.video_id, video.platform, video.external_id,
                           video.creator_id,
                           COALESCE(evidence.title, video.title) AS title,
                           COALESCE(evidence.caption, video.caption) AS caption,
                           COALESCE(evidence.description, video.description)
                               AS description,
                           COALESCE(evidence.url, video.url) AS url,
                           COALESCE(
                               evidence.duration_seconds,
                               video.duration_seconds
                           ) AS duration_seconds,
                           video.first_seen_at,
                           COALESCE(genome.transcript, '') AS transcript,
                           COALESCE(genome.opening_words, '') AS opening_words,
                           COALESCE(genome.hook_type, '') AS hook_type,
                           COALESCE(observation.views, 0) AS views,
                           COALESCE(observation.likes, 0) AS likes,
                           COALESCE(observation.comments, 0) AS comments,
                           COALESCE(observation.shares, 0) AS shares,
                           COALESCE(observation.view_velocity, 0) AS velocity,
                           COALESCE(observation.view_acceleration, 0)
                               AS acceleration,
                           COALESCE(observation.relative_strength, 0)
                               AS relative_strength,
                           observation.observation_key,
                           observation.observed_at,
                           {scope_expr} AS evidence_scope,
                           CASE WHEN evidence.observation_id IS NULL
                                THEN 'mt_videos'
                                ELSE 'accepted_evidence'
                           END AS descriptive_source,
                           artifact.transcript_id AS _artifact_transcript_id,
                           artifact.transcript_sha256
                               AS _artifact_transcript_sha256,
                           artifact.audio_sha256 AS _artifact_audio_sha256,
                           artifact.word_count AS _artifact_word_count,
                           artifact.audit_json AS _artifact_audit_json,
                           artifact.created_at AS _artifact_created_at,
                           genome.transcript_embedding_ref
                               AS _artifact_transcript_embedding_ref,
                           genome.extraction_status
                               AS _artifact_extraction_status,
                           snapshot.payload_json AS _artifact_payload_json
                    FROM mt_transcript_artifacts artifact
                    JOIN {shape['observation_source']} observation
                      ON observation.video_id=artifact.video_id
                     AND observation.observation_key=artifact.observation_key
                    JOIN mt_videos video ON video.video_id=artifact.video_id
                    {evidence_join}
                    LEFT JOIN mt_content_genomes genome
                      ON genome.video_id=artifact.video_id
                    LEFT JOIN mt_transcript_payload_snapshots snapshot
                      ON snapshot.transcript_id=artifact.transcript_id
                    WHERE artifact.video_id IN ({marks})
                    ORDER BY artifact.created_at DESC,
                             artifact.transcript_id DESC
                    """,
                    identifiers,
                ).fetchall()
        except sqlite3.Error:
            # A pre-snapshot tape cannot supply production-grade evidence.
            return []

        admitted_by_video: dict[str, dict[str, Any]] = {}
        for row in rows:
            video_id = str(row["video_id"])
            if video_id in admitted_by_video:
                continue
            try:
                artifact_audit = json.loads(row["_artifact_audit_json"])
            except (TypeError, json.JSONDecodeError):
                continue
            if (
                not isinstance(artifact_audit, dict)
                or artifact_audit.get("decision") != "PASS"
                or not is_supported_transcript_audit_contract(
                    artifact_audit.get("contract")
                )
            ):
                continue
            attestation = immutable_artifact_attestation(
                artifact={
                    "transcript_id": row["_artifact_transcript_id"],
                    "transcript_sha256": row[
                        "_artifact_transcript_sha256"
                    ],
                    "audio_sha256": row["_artifact_audio_sha256"],
                    "word_count": row["_artifact_word_count"],
                    "audit": artifact_audit,
                },
                genome={
                    "transcript": row["transcript"],
                    "transcript_embedding_ref": row[
                        "_artifact_transcript_embedding_ref"
                    ],
                    "extraction_status": row[
                        "_artifact_extraction_status"
                    ],
                },
                raw_transcript_payload=str(
                    row["_artifact_payload_json"] or ""
                ),
            )
            if all(attestation["checks"].values()):
                admitted_by_video[video_id] = {
                    **{
                        key: value for key, value in dict(row).items()
                        if not key.startswith("_artifact_")
                    },
                    "transcript_id": row["_artifact_transcript_id"],
                }
        return sorted(
            admitted_by_video.values(),
            key=lambda row: (
                -float(row.get("relative_strength") or 0),
                -float(row.get("velocity") or 0),
                -int(row.get("views") or 0),
                str(row["video_id"]),
            ),
        )

    def transcript_artifact(
        self,
        video_id: str,
        observation_key: str | None = None,
        transcript_id: str | None = None,
    ) -> dict[str, Any] | None:
        """Return the latest local Whisper artifact for a video, if one exists."""

        try:
            with closing(self.connect()) as connection:
                query = "SELECT * FROM mt_transcript_artifacts WHERE video_id=?"
                parameters: list[Any] = [video_id]
                if observation_key:
                    query += " AND observation_key=?"
                    parameters.append(observation_key)
                if transcript_id:
                    query += " AND transcript_id=?"
                    parameters.append(transcript_id)
                query += " ORDER BY created_at DESC, transcript_id DESC LIMIT 1"
                row = connection.execute(query, parameters).fetchone()
        except sqlite3.Error:
            return None
        if not row:
            return None
        result = dict(row)
        for source, target in (
            ("source_metrics_json", "source_metrics"),
            ("acquisition_json", "acquisition"),
            ("audit_json", "audit"),
        ):
            result[target] = json.loads(result.pop(source))
        return result


@dataclass
class TranscriptDocument:
    text: str
    segments: list[dict[str, Any]]
    source: str


class ViralTranscriptService:
    def __init__(self, tape: MarketTapeReader, store: QualityStore):
        self.tape = tape
        self.store = store

    @staticmethod
    def _fetch_youtube(external_id: str) -> TranscriptDocument:
        from youtube_transcript_api import YouTubeTranscriptApi

        fetched = YouTubeTranscriptApi().fetch(external_id, languages=("en", "en-US", "en-GB"))
        raw = fetched.to_raw_data() if hasattr(fetched, "to_raw_data") else list(fetched)
        segments: list[dict[str, Any]] = []
        for item in raw:
            if isinstance(item, dict):
                text = str(item.get("text") or "").strip()
                start = float(item.get("start") or 0.0)
                duration = float(item.get("duration") or 0.0)
            else:
                text = str(getattr(item, "text", "")).strip()
                start = float(getattr(item, "start", 0.0))
                duration = float(getattr(item, "duration", 0.0))
            if text:
                segments.append({"text": text, "start": start, "duration": duration})
        return TranscriptDocument(
            text=" ".join(item["text"] for item in segments),
            segments=segments,
            source="youtube_transcript_api",
        )

    @staticmethod
    def _pattern(document: TranscriptDocument, row: dict[str, Any]) -> dict[str, Any]:
        transcript_words = words(document.text)
        lowered = [word.lower() for word in transcript_words]
        duration = float(row.get("duration_seconds") or 0.0)
        if not duration and document.segments:
            last = document.segments[-1]
            duration = float(last["start"]) + float(last["duration"])
        proof_positions = [index for index, word in enumerate(lowered) if word in PROOF_WORDS]
        change_positions = [index for index, word in enumerate(lowered) if word in CHANGE_WORDS]
        first_proof_seconds = None
        if proof_positions and transcript_words and duration:
            first_proof_seconds = round(duration * proof_positions[0] / len(transcript_words), 1)
        hook_text = " ".join(transcript_words[:28])
        return {
            "opening_word_count": min(28, len(transcript_words)),
            "opening_shape": classify_hook(hook_text),
            "proof_marker_count": len(proof_positions),
            "first_proof_seconds": first_proof_seconds,
            "pattern_interrupt_count": len(change_positions),
            "estimated_words_per_second": round(len(transcript_words) / duration, 2) if duration else None,
            "duration_seconds": round(duration, 1) if duration else None,
            "structure": infer_structure(document.text),
            "source_metrics": {
                "views": int(row.get("views") or 0),
                "likes": int(row.get("likes") or 0),
                "comments": int(row.get("comments") or 0),
                "shares": int(row.get("shares") or 0),
                "velocity": float(row.get("velocity") or 0.0),
                "relative_strength": float(row.get("relative_strength") or 0.0),
                "observed_at": row.get("observed_at"),
            },
            "transcript_sha256": hashlib.sha256(document.text.encode("utf-8")).hexdigest(),
            "transcript_word_count": len(transcript_words),
        }

    def discover(self, topic: str, limit: int = 5) -> dict[str, Any]:
        if not topic.strip():
            raise ValueError("topic is required")
        # Artifact coverage is intentionally sparse during backfill. Search a wide
        # metadata window, then resolve each artifact to the immutable observation
        # it was audited against. A newer metric recheck does not stale a transcript.
        candidates = self.tape.candidates(topic, limit=max(limit * 50, 200))
        rows = self.tape.artifact_bound_candidates(
            [str(row["video_id"]) for row in candidates]
        )
        return self._discover_rows(topic, rows, limit)

    def discover_for_videos(
        self,
        topic: str,
        video_ids: Sequence[str],
        limit: int = 10,
    ) -> dict[str, Any]:
        if not topic.strip():
            raise ValueError("topic is required")
        return self._discover_rows(
            topic,
            self.tape.production_artifact_bound_candidates(video_ids),
            limit,
        )

    def _discover_rows(
        self,
        topic: str,
        rows: Sequence[dict[str, Any]],
        limit: int,
    ) -> dict[str, Any]:
        receipts: list[dict[str, Any]] = []
        failures: list[dict[str, str]] = []
        for row in rows:
            if len(receipts) >= limit:
                break
            artifact = self.tape.transcript_artifact(
                str(row["video_id"]),
                str(row.get("observation_key") or ""),
                str(row.get("transcript_id") or "") or None,
            )
            if not artifact:
                failures.append({
                    "source_id": str(row.get("external_id") or row["video_id"]),
                    "error": "local_whisper_artifact_missing",
                })
                continue
            artifact_audit = dict(artifact.get("audit") or {})
            if (
                artifact_audit.get("decision") != "PASS"
                or not is_supported_transcript_audit_contract(
                    artifact_audit.get("contract")
                )
                or artifact.get("observation_key") != row.get("observation_key")
            ):
                failures.append({
                    "source_id": str(row.get("external_id") or row["video_id"]),
                    "error": "whisper_artifact_audit_or_observation_mismatch",
                })
                continue
            text = str(row.get("transcript") or "").strip()
            if not text:
                failures.append({
                    "source_id": str(row.get("external_id") or row["video_id"]),
                    "error": "associated_transcript_missing",
                })
                continue
            document = TranscriptDocument(text=text, segments=[], source="local_whisper")
            if len(words(document.text)) < 40:
                failures.append({"source_id": str(row.get("external_id") or row["video_id"]), "error": "transcript_too_short"})
                continue
            pattern = self._pattern(document, row)
            transcript_keywords = sorted({
                normalized_source_word(token) for token in words(document.text)
                if (
                    normalized_source_word(token) in HUMAN_EXPERIENCE_WORDS
                    or (
                        len(token) >= 4
                        and normalized_source_word(token) not in STOP_WORDS
                    )
                )
            })[:300]
            payload = {
                "topic": topic,
                "video_id": row.get("video_id"),
                "platform": row.get("platform"),
                "title": row.get("title"),
                "creator_id": row.get("creator_id"),
                "evidence_scope": row.get("evidence_scope"),
                "descriptive_source": row.get("descriptive_source"),
                "transcript_source": document.source,
                "transcript_id": artifact.get("transcript_id"),
                "observation_key": artifact.get("observation_key"),
                "audio_sha256": artifact.get("audio_sha256"),
                "transcript_sha256": artifact.get("transcript_sha256"),
                "performance_qualification": {
                    "audit_contract": artifact_audit.get("contract"),
                    "audit_decision": artifact_audit.get("decision"),
                    "checks": artifact_audit.get("checks"),
                },
                "transcript_keywords": transcript_keywords,
                "pattern": pattern,
            }
            receipts.append(
                self.store.put_receipt(
                    "viral_transcript_pattern",
                    str(row.get("platform") or "unknown"),
                    str(row.get("external_id") or row["video_id"]),
                    row.get("url"),
                    payload,
                )
            )
        return {
            "status": "complete" if receipts else "no_qualified_transcripts",
            "topic": topic,
            "candidate_count": len(rows),
            "receipt_count": len(receipts),
            "receipts": receipts,
            "failures": failures[:20],
        }


def classify_hook(text: str) -> str:
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


def infer_structure(text: str) -> list[str]:
    lowered = text.lower()
    structure = ["hook"]
    if any(token in lowered for token in ("struggle", "problem", "mistake", "hard", "stuck")):
        structure.append("human_problem")
    if any(token in lowered for token in PROOF_WORDS):
        structure.append("evidence")
    if any(token in lowered for token in ("step", "first", "second", "here's how", "do this")):
        structure.append("method")
    if any(token in lowered for token in ("follow", "subscribe", "comment", "download", "try")):
        structure.append("call_to_action")
    if structure[-1] != "payoff":
        structure.insert(-1 if structure[-1] == "call_to_action" else len(structure), "payoff")
    return structure


class AudienceIntelligenceService:
    def __init__(self, tape: MarketTapeReader, store: QualityStore):
        self.tape = tape
        self.store = store

    def human_moments(
        self,
        topic: str,
        audience: str,
        limit: int = 8,
        video_ids: Sequence[str] | None = None,
        require_immutable_artifacts: bool = False,
    ) -> dict[str, Any]:
        if not topic.strip() or not audience.strip():
            raise ValueError("topic and audience are required")
        candidates = self.tape.candidates(topic, limit=60)
        if video_ids is not None and require_immutable_artifacts:
            candidates = self.tape.production_artifact_bound_candidates(
                video_ids
            )
        else:
            candidates = self.tape.artifact_bound_candidates(
                (
                    video_ids
                    if video_ids is not None
                    else [str(row["video_id"]) for row in candidates]
                )
            )
        audience_vocabulary, audience_off_context = (
            audience_context_vocabulary(audience)
        )
        candidate_moments: list[dict[str, Any]] = []
        for row in candidates:
            artifact = self.tape.transcript_artifact(
                str(row["video_id"]),
                str(row.get("observation_key") or ""),
                str(row.get("transcript_id") or "") or None,
            )
            if not artifact:
                continue
            audit = artifact.get("audit") or {}
            if (
                audit.get("decision") != "PASS"
                or not is_supported_transcript_audit_contract(audit.get("contract"))
                or artifact.get("observation_key") != row.get("observation_key")
                or not str(artifact.get("whisper_language") or "").lower().startswith("en")
            ):
                continue
            source = str(row.get("transcript") or "")
            if not source.strip():
                continue
            sentences = SENTENCE_RE.split(source)
            source_options: list[dict[str, Any]] = []
            source_context_tokens = {
                normalized_source_word(token)
                for token in words(" ".join(
                    str(row.get(field) or "")
                    for field in ("title", "caption", "description")
                ))
            }
            source_audience_terms = sorted(
                source_context_tokens & audience_vocabulary
            )
            for sentence in sentences:
                extracted = source_exact_everyday_excerpt(
                    sentence,
                    audience_vocabulary=audience_vocabulary,
                    audience_off_context=audience_off_context,
                    source_audience_terms=source_audience_terms,
                    source_off_context_terms=(
                        {
                            normalized_source_word(token)
                            for token in words(sentence)
                        }
                        & audience_off_context
                    ),
                )
                if not extracted or extracted["word_count"] < 5:
                    continue
                if (
                    extracted["audience_adjusted_selection_score"]
                    < MIN_HUMAN_MOMENT_SOURCE_SCORE
                ):
                    continue
                source_options.append(extracted)
            if not source_options:
                continue
            extracted = max(source_options, key=lambda option: (
                int(option["audience_adjusted_selection_score"]),
                int(option["source_selection_score"]),
                len(option["categories"]),
                len(option["matched_terms"]),
                -int(option["word_start"]),
            ))
            excerpt = str(extracted["text"])
            moment_id = stable_id("moment", row.get("video_id"), excerpt)
            candidate_moments.append(
                {
                    "moment_id": moment_id,
                    "situation": excerpt,
                    "audience": audience,
                    "source_video_id": row.get("video_id"),
                    "source_creator_id": row.get("creator_id"),
                    "source_transcript_id": artifact.get("transcript_id"),
                    "source_observation_key": artifact.get("observation_key"),
                    "source_url": row.get("url"),
                    "source_word_count": extracted["word_count"],
                    "source_span_word_count": extracted[
                        "source_span_word_count"
                    ],
                    "source_excerpt_truncated": extracted["truncated"],
                    "source_excerpt_word_start": extracted["word_start"],
                    "source_excerpt_word_end_exclusive": extracted[
                        "word_end_exclusive"
                    ],
                    "moment_categories": extracted["categories"],
                    "matched_source_terms": extracted["matched_terms"],
                    "lived_context_terms": extracted[
                        "lived_context_terms"
                    ],
                    "negation_terms": extracted["negation_terms"],
                    "source_selection_score": extracted[
                        "source_selection_score"
                    ],
                    "audience_adjusted_selection_score": extracted[
                        "audience_adjusted_selection_score"
                    ],
                    "audience_match_terms": extracted[
                        "audience_match_terms"
                    ],
                    "source_audience_match_terms": extracted[
                        "source_audience_match_terms"
                    ],
                    "audience_off_context_terms": extracted[
                        "audience_off_context_terms"
                    ],
                    "score_is_probability": False,
                    "basis": "source_exact_performance_qualified_local_whisper_excerpt",
                    "ai_relatability_verdict": "not_evaluated",
                    "source_views": int(row.get("views") or 0),
                }
            )
        ranked_moments = sorted(candidate_moments, key=lambda moment: (
            -int(moment["audience_adjusted_selection_score"]),
            -int(moment["source_selection_score"]),
            -len(moment["moment_categories"]),
            -int(moment["source_views"]),
            str(moment.get("source_creator_id") or ""),
            str(moment["moment_id"]),
        ))
        # Prefer one source per creator before admitting a second source from
        # the same creator. Selection quality and creator diversity are both
        # deterministic; the score is explicitly not a probability.
        moments: list[dict[str, Any]] = []
        seen_creators: set[str] = set()
        for moment in ranked_moments:
            creator_id = str(moment.get("source_creator_id") or "")
            if creator_id and creator_id in seen_creators:
                continue
            moments.append(moment)
            if creator_id:
                seen_creators.add(creator_id)
            if len(moments) >= limit:
                break
        if len(moments) < limit:
            selected_ids = {str(moment["moment_id"]) for moment in moments}
            for moment in ranked_moments:
                if str(moment["moment_id"]) in selected_ids:
                    continue
                moments.append(moment)
                if len(moments) >= limit:
                    break
        # A second exact excerpt is useful only when it belongs to the same
        # workflow context. Audience/topic words are too broad to establish
        # that relationship: two excerpts are not related merely because both
        # mention software, an app, or a founder. Prefer the same source video,
        # otherwise require substantive non-audience overlap. Self-pairing is
        # the honest fallback.
        broad_context_terms = {
            "agent", "agents", "automation", "business", "businesses",
            "founder", "founders", "software", "app", "apps", "website",
            "websites",
        }
        for moment in moments:
            audience_terms = {
                normalized_source_word(token)
                for token in moment.get("audience_match_terms") or []
            }
            situation_terms = {
                normalized_source_word(token)
                for token in words(str(moment["situation"]))
                if len(normalized_source_word(token)) >= 4
                and normalized_source_word(token) not in STOP_WORDS
                and normalized_source_word(token) not in HUMAN_EXPERIENCE_WORDS
                and normalized_source_word(token) not in broad_context_terms
                and normalized_source_word(token) not in audience_terms
            }
            related: list[tuple[tuple[Any, ...], dict[str, Any]]] = []
            for option in ranked_moments:
                if option["moment_id"] == moment["moment_id"]:
                    continue
                option_terms = {
                    normalized_source_word(token)
                    for token in words(str(option["situation"]))
                    if len(normalized_source_word(token)) >= 4
                    and normalized_source_word(token) not in STOP_WORDS
                    and normalized_source_word(token)
                    not in HUMAN_EXPERIENCE_WORDS
                    and normalized_source_word(token)
                    not in broad_context_terms
                    and normalized_source_word(token)
                    not in audience_terms
                }
                shared_content = situation_terms & option_terms
                same_video = (
                    str(option.get("source_video_id") or "")
                    == str(moment.get("source_video_id") or "")
                )
                if not (same_video or shared_content):
                    continue
                related.append((
                    (
                        int(same_video),
                        len(shared_content),
                        int(option["audience_adjusted_selection_score"]),
                        int(option["source_selection_score"]),
                    ),
                    option,
                ))
            stakes_source = (
                max(related, key=lambda item: item[0])[1]
                if related else moment
            )
            moment.update({
                "stakes": stakes_source["situation"],
                "stakes_pairing_contract": (
                    "source_context_substantive_or_self_v2"
                ),
                "stakes_source_moment_id": stakes_source["moment_id"],
                "stakes_source_video_id": stakes_source["source_video_id"],
                "stakes_source_creator_id": stakes_source["source_creator_id"],
                "stakes_source_transcript_id": stakes_source[
                    "source_transcript_id"
                ],
                "stakes_source_word_count": stakes_source[
                    "source_word_count"
                ],
                "stakes_source_span_word_count": stakes_source[
                    "source_span_word_count"
                ],
                "stakes_source_excerpt_truncated": stakes_source[
                    "source_excerpt_truncated"
                ],
                "stakes_source_excerpt_word_start": stakes_source[
                    "source_excerpt_word_start"
                ],
                "stakes_source_excerpt_word_end_exclusive": stakes_source[
                    "source_excerpt_word_end_exclusive"
                ],
                "stakes_source_observation_key": stakes_source[
                    "source_observation_key"
                ],
            })
        creator_ids = sorted({
            str(moment["source_creator_id"])
            for moment in moments
            if moment.get("source_creator_id")
        })
        category_creators = {
            category: sorted({
                str(moment["source_creator_id"])
                for moment in moments
                if moment.get("source_creator_id")
                and category in moment.get("moment_categories", [])
            })
            for category in EVERYDAY_HUMAN_LANGUAGE_BY_CATEGORY
        }
        evidence_summary = {
            "contract": "source_exact_everyday_human_moment_v3",
            "evidence_kind": "non_ai_source_language_extraction",
            "audience_fit_contract": "audience_context_term_ranking_v1",
            "max_source_excerpt_words": MAX_SOURCE_EXCERPT_WORDS,
            "source_creator_count": len(creator_ids),
            "source_creator_ids": creator_ids,
            "category_creator_counts": {
                category: len(ids) for category, ids in category_creators.items()
            },
            "ai_relatability_verdict": "not_evaluated",
        }
        receipt = self.store.put_receipt(
            "audience_human_moments",
            "market_tape",
            stable_id("audience", audience, topic),
            None,
            {
                "topic": topic,
                "audience": audience,
                "candidate_count": len(candidates),
                "moments": moments,
                "evidence_summary": evidence_summary,
                "note": "Moments are extracted from observed source language; none are invented when evidence is absent.",
            },
        )
        return {
            "status": "complete" if moments else "insufficient_observed_human_moments",
            "topic": topic,
            "audience": audience,
            "moments": moments,
            "evidence_summary": evidence_summary,
            "receipt": receipt,
        }


class ScriptService:
    def __init__(
        self,
        store: QualityStore,
        narrative: NarrativeCoherenceService | None = None,
        style_guides: TranscriptStyleGuideService | None = None,
    ):
        self.store = store
        self.narrative = narrative
        self.style_guides = style_guides

    def _source_moment_is_bound(
        self,
        human: dict[str, Any],
        patterns: Sequence[dict[str, Any]],
    ) -> bool:
        receipt_id = str(human.get("source_moment_receipt_id") or "").strip()
        receipt = self.store.receipt(receipt_id) if receipt_id else None
        if not isinstance(receipt, dict):
            return False
        payload = receipt.get("payload") or {}
        evidence = payload.get("evidence_summary") or {}
        if (
            receipt.get("receipt_type") != "audience_human_moments"
            or receipt.get("source_type") != "market_tape"
            or evidence.get("contract")
            != "source_exact_everyday_human_moment_v3"
            or evidence.get("evidence_kind")
            != "non_ai_source_language_extraction"
        ):
            return False
        pattern_keys = {
            (
                str((item.get("payload") or {}).get("transcript_id") or ""),
                str((item.get("payload") or {}).get("observation_key") or ""),
            )
            for item in patterns
        }
        supplied_id = str(human.get("moment_id") or "").strip()
        supplied_situation = str(human.get("situation") or "").strip()
        supplied_stakes = str(human.get("stakes") or "").strip()
        return any(
            bool(supplied_id)
            and str(item.get("moment_id") or "") == supplied_id
            and supplied_id == stable_id(
                "moment", item.get("source_video_id"), supplied_situation
            )
            and str(item.get("situation") or "").strip() == supplied_situation
            and str(item.get("stakes") or "").strip() == supplied_stakes
            and all(
                str(human.get(key) or "") == str(item.get(key) or "")
                for key in (
                    "source_video_id",
                    "source_transcript_id",
                    "source_observation_key",
                    "stakes_source_video_id",
                    "stakes_source_transcript_id",
                    "stakes_source_observation_key",
                )
            )
            and item.get("basis")
            == "source_exact_performance_qualified_local_whisper_excerpt"
            and (
                str(item.get("source_transcript_id") or ""),
                str(item.get("source_observation_key") or ""),
            ) in pattern_keys
            and (
                str(item.get("stakes_source_transcript_id") or ""),
                str(item.get("stakes_source_observation_key") or ""),
            ) in pattern_keys
            for item in payload.get("moments") or []
        )

    def _resolve_owned_claims(
        self, payload: dict[str, Any], claim: str
    ) -> tuple[list[str], list[str], dict[str, Any] | None]:
        raw_values = [
            " ".join(str(item).split()).strip()
            for item in payload.get("owned_proof") or []
            if str(item).strip()
        ]
        receipt_ids = list(dict.fromkeys(
            str(item).strip()
            for item in payload.get("owned_proof_receipt_ids") or []
            if str(item).strip()
        ))
        if raw_values and not receipt_ids:
            return [], [], {
                "status": "rejected",
                "code": "REJECT_UNBOUND_OWNED_PROOF",
                "reason": "Owned proof text requires an immutable owned claim receipt.",
            }
        receipts = self.store.receipts(receipt_ids) if receipt_ids else []
        if len(receipts) != len(receipt_ids):
            found = {str(item.get("receipt_id") or "") for item in receipts}
            return [], [], {
                "status": "rejected",
                "code": "REJECT_UNKNOWN_OWNED_PROOF_RECEIPT",
                "unknown_receipt_ids": sorted(set(receipt_ids) - found),
            }
        statements: list[str] = []
        for receipt in receipts:
            evidence = receipt.get("payload") or {}
            statement = " ".join(str(evidence.get("statement") or "").split()).strip()
            expected_sha = hashlib.sha256(statement.encode("utf-8")).hexdigest()
            source_ids = [
                str(item) for item in evidence.get("evidence_receipt_ids") or []
                if str(item).strip()
            ]
            source_receipt = (
                self.store.receipt(source_ids[0]) if len(source_ids) == 1 else None
            )
            source_payload = (
                source_receipt.get("payload") or {}
                if isinstance(source_receipt, dict) else {}
            )
            source_path = Path(
                str(source_payload.get("source_path") or "")
            ).expanduser()
            source_bytes = source_path.read_bytes() if source_path.is_file() else b""
            source_sha = hashlib.sha256(source_bytes).hexdigest()
            try:
                source_text = " ".join(source_bytes.decode("utf-8").split())
            except UnicodeDecodeError:
                source_text = ""
            source_valid = (
                isinstance(source_receipt, dict)
                and source_receipt.get("receipt_type") == "owned_evidence_file"
                and source_receipt.get("source_type") == "owned_file"
                and source_payload.get("contract") == "owned_evidence_file_v1"
                and source_payload.get("owner_id") == evidence.get("owner_id")
                and source_payload.get("source_sha256") == source_sha
                and int(source_payload.get("source_byte_count") or -1)
                == len(source_bytes)
                and source_payload.get("statement_sha256") == expected_sha
                and source_payload.get("statement") == statement
                and statement in source_text
                and evidence.get("evidence_sha256") == source_sha
            )
            valid = (
                receipt.get("receipt_type") == "owned_claim_evidence"
                and receipt.get("source_type") == "owned"
                and evidence.get("contract") == OWNED_CLAIM_EVIDENCE_CONTRACT
                and evidence.get("verification_basis")
                == "exact_statement_in_stored_source_bytes"
                and evidence.get("statement_sha256") == expected_sha
                and bool(statement)
                and source_valid
                and (
                    not contains_first_person(statement)
                    or source_payload.get("perspective_basis")
                    == "exact_owned_statement_in_source_bytes"
                )
            )
            if not valid:
                return [], [], {
                    "status": "rejected",
                    "code": "REJECT_INVALID_OWNED_PROOF_RECEIPT",
                    "receipt_id": receipt.get("receipt_id"),
                }
            statements.append(statement)
        if raw_values and set(raw_values) != set(statements):
            return [], [], {
                "status": "rejected",
                "code": "REJECT_OWNED_PROOF_TEXT_MISMATCH",
            }
        if contains_first_person(claim) and claim not in statements:
            return [], [], {
                "status": "rejected",
                "code": "REJECT_UNBOUND_FIRST_PERSON_CLAIM",
            }
        return statements, receipt_ids, None

    def generate(self, payload: dict[str, Any]) -> dict[str, Any]:
        topic = str(payload.get("topic") or "").strip()
        audience = str(payload.get("audience") or "").strip()
        objective = str(payload.get("objective") or "qualified_attention").strip()
        claim = str(payload.get("claim") or "").strip()
        human = payload.get("human_moment") or {}
        situation = str(human.get("situation") or "").strip()
        stakes = str(human.get("stakes") or "").strip()
        receipt_ids = [str(item) for item in payload.get("receipt_ids") or []]
        proof, proof_receipt_ids, proof_rejection = self._resolve_owned_claims(
            payload, claim
        )
        quality_attempt = payload.get("quality_attempt", 0)
        if type(quality_attempt) is not int or not (
            0 <= quality_attempt < MAX_QUALITY_REWRITE_ATTEMPTS
        ):
            raise ValueError(
                "quality_attempt must be a bounded zero-based integer"
            )
        missing = [
            name
            for name, value in (("topic", topic), ("audience", audience), ("claim", claim), ("human_moment.situation", situation), ("human_moment.stakes", stakes))
            if not value
        ]
        if missing:
            raise ValueError("missing required evidence context: " + ", ".join(missing))
        if proof_rejection is not None:
            return proof_rejection
        if not receipt_ids:
            return {
                "status": "rejected",
                "code": "REJECT_NO_RECEIPTS",
                "reason": "At least one persisted source receipt is required before script generation.",
            }
        receipts = self.store.receipts(receipt_ids)
        found_ids = {item["receipt_id"] for item in receipts}
        unknown = sorted(set(receipt_ids) - found_ids)
        if unknown:
            return {"status": "rejected", "code": "REJECT_UNKNOWN_RECEIPTS", "unknown_receipt_ids": unknown}
        conversion_objectives = {"conversion", "purchase", "signup", "trial", "sale"}
        if objective.lower() in conversion_objectives and not proof:
            return {
                "status": "rejected",
                "code": "REJECT_CONVERSION_UNPROVEN",
                "reason": "A conversion objective requires owned proof supplied by the operator.",
            }

        pattern_receipts = [
            item for item in receipts
            if item["receipt_type"] == "viral_transcript_pattern"
        ]
        verified_patterns = verified_transcript_patterns(pattern_receipts)
        if len(verified_patterns) < 5:
            return {
                "status": "rejected",
                "code": "REJECT_INSUFFICIENT_TRANSCRIPT_COHORT",
                "reason": "At least five performance-qualified local Whisper transcript receipts are required.",
                "verified_transcript_count": len(verified_patterns),
            }
        creators = {
            str(item["payload"].get("creator_id") or "")
            for item in verified_patterns
            if item["payload"].get("creator_id")
        }
        metrics = [item["payload"].get("pattern", {}).get("source_metrics", {}) for item in verified_patterns]
        observed_views = sum(int(item.get("views") or 0) for item in metrics)
        source_count = len(verified_patterns)
        if len(creators) < 3 or observed_views < 100_000:
            return {
                "status": "rejected",
                "code": "REJECT_INSUFFICIENT_TRANSCRIPT_COHORT",
                "reason": "The cohort requires at least three creators and 100,000 observed views.",
                "verified_transcript_count": source_count,
                "creator_count": len(creators),
                "observed_views_snapshot": observed_views,
            }
        if not self._source_moment_is_bound(human, verified_patterns):
            return {
                "status": "rejected",
                "code": "REJECT_UNBOUND_HUMAN_MOMENT",
                "reason": (
                    "The human moment must match an immutable audience moment "
                    "receipt before it can be spoken."
                ),
            }
        style_receipts = [
            item for item in receipts
            if item["receipt_type"] == "transcript_style_guide"
        ]
        requested_style_receipt = str(
            payload.get("style_guide_receipt_id") or ""
        ).strip()
        if requested_style_receipt and not style_receipts:
            requested = self.store.receipt(requested_style_receipt)
            if requested is None:
                return {
                    "status": "rejected",
                    "code": "REJECT_UNKNOWN_STYLE_GUIDE",
                    "style_guide_receipt_id": requested_style_receipt,
                }
            style_receipts = [requested]
        if len(style_receipts) > 1:
            return {
                "status": "rejected",
                "code": "REJECT_MULTIPLE_STYLE_GUIDES",
                "style_guide_receipt_ids": sorted(
                    item["receipt_id"] for item in style_receipts
                ),
            }
        if not style_receipts:
            if self.style_guides is None:
                return {
                    "status": "rejected",
                    "code": "REJECT_STYLE_GUIDE_SERVICE_UNAVAILABLE",
                }
            pattern_platforms = {
                str(item["payload"].get("platform") or "").lower()
                for item in verified_patterns
                if item["payload"].get("platform")
            }
            style_platform = str(
                payload.get("style_platform")
                or (
                    next(iter(pattern_platforms))
                    if len(pattern_platforms) == 1 else "cross_platform"
                )
            ).lower()
            built_style = self.style_guides.build({
                "topic": topic,
                "platform": style_platform,
                "receipt_ids": [
                    item["receipt_id"] for item in verified_patterns
                ],
                "minimum_transcripts": 5,
                "minimum_creators": 3,
                "minimum_observed_views": 100_000,
            })
            if built_style.get("status") != "ready":
                return {
                    "status": "rejected",
                    "code": "REJECT_STYLE_GUIDE_NOT_READY",
                    "style_guide_result": built_style,
                }
            style_receipts = [built_style["receipt"]]
        style_receipt = style_receipts[0]
        if style_receipt.get("receipt_type") != "transcript_style_guide":
            return {
                "status": "rejected",
                "code": "REJECT_INVALID_STYLE_GUIDE_RECEIPT",
            }
        style_guide = style_receipt["payload"]
        style_source_ids = {
            str(item.get("receipt_id") or "")
            for item in style_guide.get("evidence", {}).get("sources") or []
        }
        verified_pattern_ids = {
            item["receipt_id"] for item in verified_patterns
        }
        if not style_source_ids or not style_source_ids.issubset(
            verified_pattern_ids
        ):
            return {
                "status": "rejected",
                "code": "REJECT_STYLE_GUIDE_SOURCE_MISMATCH",
            }
        human_term_sources: dict[str, set[str]] = {}
        human_term_receipts: dict[str, set[str]] = {}
        human_term_transcripts: dict[str, set[str]] = {}
        for item in verified_patterns:
            source_terms = {
                normalized_source_word(str(token))
                for token in item["payload"].get("transcript_keywords") or []
            }
            creator_identity = str(
                item["payload"].get("creator_id") or ""
            ).strip()
            if not creator_identity:
                continue
            for term in HUMAN_EXPERIENCE_WORDS & source_terms:
                human_term_sources.setdefault(term, set()).add(creator_identity)
                human_term_receipts.setdefault(term, set()).add(item["receipt_id"])
                transcript_id = str(
                    item["payload"].get("transcript_id") or ""
                ).strip()
                if transcript_id:
                    human_term_transcripts.setdefault(term, set()).add(
                        transcript_id
                    )
        recurring_human_terms = sorted(
            (term for term, sources in human_term_sources.items() if len(sources) >= 2),
            key=lambda term: (-len(human_term_sources[term]), term),
        )
        recurring_human_language_evidence = [
            {
                "term": term,
                "categories": sorted(
                    category
                    for category, terms in (
                        EVERYDAY_HUMAN_LANGUAGE_BY_CATEGORY.items()
                    )
                    if term in terms
                ),
                "distinct_creator_count": len(human_term_sources[term]),
                "creator_ids": sorted(human_term_sources[term]),
                "source_receipt_ids": sorted(human_term_receipts[term]),
                "source_transcript_ids": sorted(
                    human_term_transcripts.get(term, set())
                ),
            }
            for term in recurring_human_terms
        ]
        recurring_human_language_gate = {
            "contract": "cross_creator_everyday_human_language_v1",
            "evidence_kind": "non_ai_source_language_recurrence",
            "minimum_distinct_creators_per_term": 2,
            "pass": bool(recurring_human_terms),
            "terms": recurring_human_language_evidence,
            "ai_relatability_verdict": "not_evaluated",
        }
        if not recurring_human_terms:
            return {
                "status": "rejected",
                "code": "REJECT_NO_RECURRING_HUMAN_LANGUAGE",
                "reason": (
                    "At least one source-derived problem, need, time, or work "
                    "term must recur across two distinct creators."
                ),
                "recurring_human_language_gate": (
                    recurring_human_language_gate
                ),
            }
        _audience_context, audience_off_context = (
            audience_context_vocabulary(audience)
        )
        weak_display_terms = {
            "can't", "cannot", "don't", "must", "trying",
            *audience_off_context,
        }
        named_terms = [
            term for term in recurring_human_terms
            if term not in weak_display_terms
        ][:4] or recurring_human_terms[:4]
        if len(named_terms) == 1:
            term_phrase = named_terms[0]
        else:
            term_phrase = (
                ", ".join(named_terms[:-1])
                + f" and {named_terms[-1]}"
            )
        proof_line = proof[0] if proof else (
            "It keeps coming back to the same friction: "
            f"{term_phrase} {'stays' if len(named_terms) == 1 else 'stay'} "
            "tied to a handoff because the next step still "
            "waits on a person."
        )
        moment_categories = {
            str(value) for value in human.get("moment_categories") or []
        }
        situation_terms = {
            normalized_source_word(token) for token in words(situation)
        }
        claim_text = claim.rstrip(".") + "."
        if {"quote", "form"} & situation_terms:
            stakes_text = (
                "You are in the middle of product work when the form lands. "
                "Either you stop to copy the details into a meeting or invoice, "
                "or a live buyer waits in your inbox."
            )
            claim_text = (
                "Build the first AI automation around that one handoff: quote "
                "request in, then scheduled meeting or sent invoice out."
            )
            method_text = (
                "On screen, the email request hits the app. The AI agent reads it, "
                "creates the meeting or invoice, and shows the finished result."
            )
            payoff_text = (
                "The visible result is a buyer with a next step while your "
                "product work stays open."
            )
            cta_text = (
                "Comment ‘inbox’ with the live request that keeps waiting; that "
                "is the first workflow to map."
            )
        elif "results" in situation_terms:
            stakes_text = (
                "You are in the middle of a customer task when the question "
                "appears. Either you leave the work to hunt for the answer, or "
                "the task waits."
            )
            claim_text = (
                "The first useful version has one job: one question in, one answer "
                "back in the same software."
            )
            method_text = (
                "Show the exact question as input. Show the answer returned in the "
                "same software. Then compare it with leaving and coming back."
            )
            payoff_text = (
                "The visible result is an answer inside the work, without the "
                "detour to another tool."
            )
            cta_text = (
                "Comment ‘answer’ with the question that keeps sending you to "
                "another tool; that is the first workflow to map."
            )
        elif "software" in situation_terms or {"email", "emails"} & situation_terms:
            if "software" in situation_terms:
                stakes_text = (
                    "Then a customer email lands while you are building. Either "
                    "you stop the product work to decide the next action, or the "
                    "customer waits and the request goes cold."
                )
            else:
                stakes_text = (
                    "You are in the middle of product work when the inbox fills. "
                    "Either you stop to decide, reply, schedule, or invoice, or "
                    "the customer waits."
                )
            claim_text = (
                "Build the first AI app around that exact interruption: incoming "
                "emails in, one useful next action out."
            )
            method_text = (
                "On screen, an email arrives. The AI agent reads whether it is "
                "a quote, meeting, or support request, then drafts the reply, "
                "schedules the meeting, or prepares the invoice."
            )
            payoff_text = (
                "The visible result is a customer with a next step while your "
                "product work stays open."
            )
            cta_text = (
                "Comment ‘inbox’ with the email that keeps interrupting your "
                "week; that is the first workflow to map."
            )
        elif "time" in moment_categories or "work" in moment_categories:
            stakes_text = (
                "You are in the middle of product work when the repeated job "
                "comes back. Either you stop to move it forward, or it waits on "
                "a person."
            )
            claim_text = (
                f"For {audience}, show one request enter and one completed task leave."
            )
            method_text = (
                "Show the request arrive. Show the finished task appear. Then "
                "explain only the time the automation gives back."
            )
            payoff_text = (
                "The visible result is one completed task while your product "
                "work stays open."
            )
            cta_text = (
                "Comment ‘workflow’ with the repeated job that interrupts your "
                "week; that is the first one to map."
            )
        elif "problem" in moment_categories:
            stakes_text = (
                "You are in the middle of product work when the problem appears. "
                "Either you stop to make the next usable result, or the customer "
                "waits."
            )
            claim_text = (
                f"For {audience}, show that problem move from one input to one "
                "usable output."
            )
            method_text = (
                "Put the viewer's exact problem language on screen, show the input "
                "and usable output, then explain only what changed."
            )
            payoff_text = (
                "The visible result is a usable output while product work "
                "stays open."
            )
            cta_text = (
                "Comment ‘problem’ with the issue that keeps interrupting the "
                "week; that is the first workflow to map."
            )
        else:
            stakes_text = (
                "You are in the middle of product work when the extra step "
                "appears. Either you stop to handle it, or the answer waits."
            )
            method_text = (
                "Show the unwanted step. Show the answer arrive where the work "
                "already happens. Then explain only what changed."
            )
            payoff_text = (
                "The visible result is an answer where the work already happens."
            )
            cta_text = (
                "Comment ‘step’ with the extra task that interrupts your week; "
                "that is the first workflow to map."
            )
        source_stakes_is_hook = stakes.strip() == situation.strip()
        contextual_stakes_text = stakes_text
        if not source_stakes_is_hook:
            source_stakes_text = stakes.strip()
            if source_stakes_text[-1] not in ".?!":
                source_stakes_text += "."
            context_text = (
                f"One person also said, “{source_stakes_text}”"
                if contains_first_person(source_stakes_text)
                else source_stakes_text
            )
            context_basis = "source_stakes_exact"
            stakes_text = contextual_stakes_text
        else:
            context_text = (
                "That pressure is the part to solve before another step is added."
            )
            context_basis = "source_hook_bridge"
        preferred_hook_shapes = list(
            style_guide.get("hooks", {}).get("preferred_shapes") or []
        )
        preferred_hook = (
            preferred_hook_shapes[0]
            if preferred_hook_shapes else "direct_claim"
        )
        hook_text = situation.strip()
        if hook_text[-1] not in ".?!":
            hook_text += "."
        if contains_first_person(hook_text):
            hook_text = f"One person said, “{hook_text}”"
        applied_hook = "source_moment_direct"
        if preferred_hook == "question" and not hook_text.endswith("?"):
            hook_text = f"Does this happen to you? {hook_text}"
            applied_hook = "question_then_source_moment"
        elif preferred_hook == "contrarian_warning":
            hook_text = f"Don't normalize this. {hook_text}"
            applied_hook = "contrarian_warning_then_source_moment"
        elif preferred_hook == "personal_receipt" and contains_first_person(
            situation
        ):
            applied_hook = "attributed_source_first_person_quote"
        source_first_person_attributed = bool(
            contains_first_person(situation)
            or contains_first_person(stakes)
        )
        transitions = list(
            style_guide.get("delivery", {}).get("direction", {}).get(
                "transitions"
            ) or []
        )
        applied_transition = next(
            (
                marker for marker in transitions
                if marker in {"actually", "honestly", "here's", "so", "the thing is"}
            ),
            None,
        )
        if applied_transition and not proof_line.lower().startswith(
            applied_transition
        ):
            separator = ": " if applied_transition in {"here's", "the thing is"} else ", "
            proof_line = (
                applied_transition.capitalize()
                + separator
                + proof_line[:1].lower()
                + proof_line[1:]
            )
        target_duration_range = (
            style_guide.get("speech", {}).get("target_ranges", {}).get(
                "duration_seconds", {}
            )
        )
        target_duration = max(
            24.0,
            min(45.0, float(target_duration_range.get("median") or 43.0)),
        )
        structure = select_rhetorical_structure(
            "evidence_story",
            seed="|".join((
                str(payload.get("brief_id") or ""),
                str(human.get("moment_id") or ""),
                topic,
            )),
            attempt=quality_attempt,
        )
        target_duration = min(48.0, target_duration * 1.0666667)
        components = {
            "hook": [{"beat": "human_hook", "text": hook_text}],
            "stakes": [{"beat": "stakes", "text": stakes_text}],
            "context": [{"beat": "evidence_context", "text": context_text}],
            "proof": [{
                "beat": "proof", "text": proof_line.rstrip(".") + "."
            }],
            "claim": [{"beat": "claim", "text": claim_text}],
            "method": [{"beat": "method", "text": method_text}],
            "payoff": [{"beat": "payoff", "text": payoff_text}],
            "cta": [{"beat": "cta", "text": cta_text}],
        }
        timeline = retime_timeline(
            arrange_role_components(components, structure),
            target_seconds=target_duration,
        )
        full_text = " ".join(beat["text"] for beat in timeline)
        parent_script_id = str(payload.get("parent_script_id") or "").strip()
        current_brief_id = str(payload.get("brief_id") or "").strip()
        prior_texts = [
            str(item.get("text") or "")
            for item in self.store.scripts(limit=20)
            if str(item.get("script_id") or "") != parent_script_id
            and not (
                current_brief_id
                and str(item.get("brief_id") or "") == current_brief_id
            )
            and str(item.get("text") or "").strip()
        ]
        protected_phrases = tuple(
            value for value in (situation, stakes, *proof) if value
        )
        initial_owner_quality = audit_owner_calibrated_quality(
            full_text,
            timeline=timeline,
            protected_phrases=protected_phrases,
            prior_texts=prior_texts,
        )
        repair_actions = owner_repair_actions(initial_owner_quality)
        local_repair_applied = False
        if repair_actions:
            revised_timeline = repair_timeline_for_owner_quality(
                timeline,
                initial_owner_quality,
                protected_phrases=protected_phrases,
                attempt=quality_attempt + 1,
                target_seconds=target_duration,
            )
            local_repair_applied = revised_timeline != timeline
            timeline = revised_timeline
            full_text = " ".join(beat["text"] for beat in timeline)
        owner_quality = audit_owner_calibrated_quality(
            full_text,
            timeline=timeline,
            protected_phrases=protected_phrases,
            prior_texts=prior_texts,
        )
        parent_script = self.store.script(parent_script_id) if parent_script_id else None
        parent_script_sha256 = (
            self.store.script_audit_sha256(parent_script)
            if isinstance(parent_script, dict) else None
        )
        quality_revision = {
            "contract": "bounded_script_quality_rewrite_v1",
            "attempt": quality_attempt + 1,
            "maximum_attempts": MAX_QUALITY_REWRITE_ATTEMPTS,
            "parent_script_id": parent_script_id or None,
            "parent_script_sha256": parent_script_sha256,
            "initial_failure_codes": initial_owner_quality["failure_codes"],
            "repair_actions": repair_actions,
            "local_repair_applied": local_repair_applied,
            "final_failure_codes": owner_quality["failure_codes"],
            "source_text_modified": not all(
                value in full_text for value in protected_phrases
            ),
        }
        delivery_visual_plan = build_delivery_visual_plan(
            timeline, structure_id=structure["structure_id"]
        )
        moment_receipt_id = str(
            human.get("source_moment_receipt_id") or ""
        ).strip()
        evidence_binding_receipt_ids = list(dict.fromkeys((
            moment_receipt_id,
            *proof_receipt_ids,
        )))
        speaker_claim_gate = {
            "contract": "verified_speaker_claim_gate_v1",
            "decision": "PASS",
            "source_moment_receipt_id": moment_receipt_id,
            "source_moment_bound": True,
            "public_first_person_excerpt_present": (
                source_first_person_attributed
            ),
            "public_first_person_excerpt_attributed": (
                source_first_person_attributed
            ),
            "template_first_person_inferred_from_source": False,
            "owned_claim_receipt_ids": proof_receipt_ids,
            "unbound_first_person_claims": [],
        }
        result = {
            "status": "generated_pending_gates",
            "topic": topic,
            "audience": audience,
            "objective": objective,
            "brief_id": payload.get("brief_id"),
            "trend_id": payload.get("trend_id"),
            "parent_script_id": parent_script_id or None,
            "variant_index": payload.get("variant_index"),
            "variant_selection_contract": payload.get(
                "variant_selection_contract"
            ),
            "source_receipt_ids": receipt_ids,
            "evidence_binding_receipt_ids": evidence_binding_receipt_ids,
            "style_guide_id": style_guide["guide_id"],
            "style_guide_receipt_id": style_receipt["receipt_id"],
            "style_application": {
                "contract": "aggregate_style_application_v1",
                "preferred_hook_shape": preferred_hook,
                "applied_hook": applied_hook,
                "applied_transition": applied_transition,
                "target_duration_seconds": target_duration,
                "actual_voice_or_likeness_imitation": False,
            },
            "speaker_claim_gate": speaker_claim_gate,
            "rhetorical_structure": structure,
            "owner_quality_contract": OWNER_QUALITY_CONTRACT,
            "owner_quality": owner_quality,
            "quality_revision": quality_revision,
            "delivery_visual_plan": delivery_visual_plan,
            "human_moment": dict(human),
            "source_language_binding": {
                "contract": "source_moment_spoken_binding_v1",
                "situation_exact_in_hook": situation in hook_text,
                "stakes_exact_in_timeline": (
                    stakes in hook_text if source_stakes_is_hook
                    else stakes in context_text
                ),
                "stakes_exact_location": (
                    "human_hook"
                    if source_stakes_is_hook else "evidence_context"
                ),
                "evidence_context_basis": context_basis,
                "contextual_stakes_classification": (
                    "template_expansion_after_source_moment"
                ),
                "source_moment_receipt_id": human.get(
                    "source_moment_receipt_id"
                ),
            },
            "evidence_summary": {
                "viral_transcript_patterns": source_count,
                "creator_count": len(creators),
                "observed_views_snapshot": observed_views,
                "recurring_human_terms": recurring_human_terms,
                "recurring_human_language_gate": (
                    recurring_human_language_gate
                ),
                "generation_contract": payload.get("generation_contract"),
                "owned_proof_count": len(proof),
                "owned_proof_receipt_ids": proof_receipt_ids,
                "style_guide_receipt_id": style_receipt["receipt_id"],
            },
            "timeline": timeline,
            "text": full_text,
            "created_at": utc_now(),
        }
        # Owner directive 2026-08-22: the context behind the transcript must make
        # sense in timeline order as presented to the audience. Audit, auto-revise
        # deterministically, and fail closed if coherence cannot be reached.
        coherence: dict[str, Any] | None = None
        if self.narrative is not None:
            result, coherence = self.narrative.enforce(result)
            if coherence["decision"] != "PASS":
                self.store.put_audit(
                    "narrative_coherence", None, coherence["decision"], 0.0,
                    {"attempts": coherence["attempts"], "defects_open": coherence["defects_open"],
                     "llm_judgment": coherence["llm_judgment"], "topic": topic},
                )
                code = ("REJECT_COHERENCE_JUDGE_UNAVAILABLE"
                        if coherence["decision"] == "JUDGE_UNAVAILABLE"
                        else "REJECT_NARRATIVE_INCOHERENT")
                return {
                    "status": "rejected", "code": code,
                    "reason": "The script cannot be presented coherently in timeline order.",
                    "narrative_coherence": coherence,
                }
            result["narrative_coherence"] = {
                "decision": "PASS",
                "attempts": len(coherence["attempts"]),
                "revised": len(coherence["attempts"]) > 1,
            }
        final_owner_quality = audit_owner_calibrated_quality(
            result["text"],
            timeline=result["timeline"],
            protected_phrases=protected_phrases,
            prior_texts=prior_texts,
        )
        result["owner_quality"] = final_owner_quality
        result["quality_revision"]["final_failure_codes"] = (
            final_owner_quality["failure_codes"]
        )
        result["delivery_visual_plan"] = build_delivery_visual_plan(
            result["timeline"], structure_id=structure["structure_id"]
        )
        result["script_id"] = stable_id(
            "script", script_identity_payload(result)
        )
        result = self.store.put_script(result)
        owner_quality_audit = self.store.put_audit(
            "owner_calibrated_quality",
            result["script_id"],
            (
                "PASS"
                if result["owner_quality"]["decision"] == "PASS"
                else "REVISE_OWNER_QUALITY"
            ),
            float(result["owner_quality"]["score"]),
            {
                "quality": result["owner_quality"],
                "revision": result["quality_revision"],
                "input_binding": {
                    "contract": "stored_script_audit_binding_v1",
                    "stored_script_bound": True,
                    "script_id": result["script_id"],
                    "script_sha256": self.store.script_audit_sha256(result),
                },
            },
        )
        if coherence is not None:
            self.store.put_audit(
                "narrative_coherence", result["script_id"], "PASS", 100.0,
                {
                    "attempts": coherence["attempts"],
                    "llm_judgment": coherence["llm_judgment"],
                    "input_binding": {
                        "contract": "stored_script_audit_binding_v1",
                        "stored_script_bound": True,
                        "script_id": result["script_id"],
                        "script_sha256": self.store.script_audit_sha256(result),
                    },
                },
            )
        return {
            **result,
            "owner_quality_audit_id": owner_quality_audit["audit_id"],
            "owner_quality_audit": owner_quality_audit,
        }


class RelatabilityService:
    def __init__(self, store: QualityStore):
        self.store = store

    def audit(self, payload: dict[str, Any]) -> dict[str, Any]:
        payload, input_binding = self.store.bind_script_audit_payload(payload)
        text = str(payload.get("text") or "").strip()
        timeline = payload.get("timeline") or []
        subject_id = str(payload.get("script_id") or stable_id("subject", text))
        if not text:
            raise ValueError("text is required")
        token_list = [item.lower() for item in words(text)]
        significant_tokens = {
            token for token in token_list if len(token) >= 3 and token not in STOP_WORDS
        }
        opening_tokens = token_list[:45]
        opening = " ".join(opening_tokens)
        first_sentence = SENTENCE_RE.split(text)[0].lower()
        receipt_ids = [str(item) for item in payload.get("source_receipt_ids") or []]
        receipts = self.store.receipts(receipt_ids) if receipt_ids else []
        patterns = [
            item for item in receipts
            if item["receipt_type"] == "viral_transcript_pattern"
        ]
        verified_patterns = verified_transcript_patterns(patterns)
        creators = {
            str(item["payload"].get("creator_id") or "")
            for item in verified_patterns if item["payload"].get("creator_id")
        }
        observed_views = sum(
            int(item["payload"].get("pattern", {}).get("source_metrics", {}).get("views") or 0)
            for item in verified_patterns
        )
        keyword_sets = [
            {str(token).lower() for token in item["payload"].get("transcript_keywords") or []}
            for item in verified_patterns
        ]
        union_keywords = set().union(*keyword_sets) if keyword_sets else set()
        vocabulary_overlap = (
            len(significant_tokens & union_keywords) / len(significant_tokens)
            if significant_tokens else 0.0
        )
        supported_sources = sum(
            len(significant_tokens & source_keywords) >= 3 for source_keywords in keyword_sets
        )
        supported_creators = {
            str(item["payload"].get("creator_id") or "")
            for item, source_keywords in zip(verified_patterns, keyword_sets)
            if len(significant_tokens & source_keywords) >= 3
            and item["payload"].get("creator_id")
        }
        opening_human_terms = sorted(
            set(opening_tokens) & HUMAN_EXPERIENCE_WORDS & union_keywords
        )
        pipeline_meta_phrases = (
            "attention gate", "content factory", "human-relatability", "source receipt",
            "transcript pattern", "passes human", "passes attention", "reveal the mechanism",
            "spoken pattern", "test the structure", "recognize themselves",
        )
        pipeline_meta_matches = sorted(
            phrase for phrase in pipeline_meta_phrases if phrase in text.lower()
        )
        source_claim = re.search(
            r"reviewed\s+([\d,]+)\s+source transcript patterns?\s+with\s+([\d,]+)\s+observed views",
            text,
            re.IGNORECASE,
        )
        source_claim_matches = not source_claim or (
            int(source_claim.group(1).replace(",", "")) == len(verified_patterns)
            and int(source_claim.group(2).replace(",", "")) == observed_views
        )
        checks = {
            "performance_transcript_cohort": (
                len(verified_patterns) >= 5 and len(creators) >= 3 and observed_views >= 100_000
            ),
            "all_receipts_artifact_verified": len(verified_patterns) == len(patterns) and bool(patterns),
            "source_claim_matches_evidence": source_claim_matches,
            "human_experience_in_opening": bool(opening_human_terms),
            "audience_facing_not_pipeline_meta": not pipeline_meta_matches,
            "script_vocabulary_supported": vocabulary_overlap >= 0.18,
            "supported_by_three_transcripts": supported_sources >= 3,
            "supported_by_three_creators": len(supported_creators) >= 3,
            "concrete_stakes_present": any(token in text.lower() for token in ("because", "cost", "lose", "matters", "stuck", "waste", "without")),
            "audience_language_present": any(token in token_list for token in ("you", "your", "we", "i")),
            "not_product_first": not any(token in first_sentence for token in ("app", "platform", "product", "service", "software", "tool")),
            "not_jargon_dense_opening": sum(token in JARGON for token in words(opening)) <= 2,
            "timeline_has_human_hook": bool(timeline and timeline[0].get("beat") == "human_hook"),
        }
        failures = [name for name, passed in checks.items() if not passed]
        hard_requirements = (
            "performance_transcript_cohort",
            "all_receipts_artifact_verified",
            "source_claim_matches_evidence",
            "human_experience_in_opening",
            "audience_facing_not_pipeline_meta",
            "supported_by_three_transcripts",
            "supported_by_three_creators",
        )
        score = min(85.0, 100.0 * sum(checks.values()) / len(checks))
        hard_requirements_pass = all(checks[name] for name in hard_requirements)
        if not hard_requirements_pass:
            score = min(score, 69.0)
        decision = (
            "PASS" if score >= 70 and hard_requirements_pass
            else "REJECT_NOT_RELATABLE"
        )
        return self.store.put_audit(
            "relatability_script",
            subject_id,
            decision,
            score,
            {
                "contract": "performance_transcript_script_gate_v1",
                "measurement_kind": "prediction_from_source_transcripts",
                "actual_audience_relatability_measured": False,
                "score_cap_without_post_publication_outcomes": 85,
                "checks": checks,
                "failures": failures,
                "hard_requirements": list(hard_requirements),
                "threshold": 70,
                "verified_transcript_count": len(verified_patterns),
                "creator_count": len(creators),
                "observed_views_snapshot": observed_views,
                "script_vocabulary_overlap": round(vocabulary_overlap, 6),
                "supported_source_count": supported_sources,
                "supported_creator_count": len(supported_creators),
                "opening_human_terms": opening_human_terms,
                "pipeline_meta_phrases_in_script": pipeline_meta_matches,
                "input_binding": input_binding,
            },
        )


class AttentionService:
    def __init__(self, store: QualityStore):
        self.store = store

    def script_audit(self, payload: dict[str, Any]) -> dict[str, Any]:
        payload, input_binding = self.store.bind_script_audit_payload(payload)
        text = str(payload.get("text") or "").strip()
        timeline = payload.get("timeline") or []
        subject_id = str(payload.get("script_id") or stable_id("subject", text))
        if not text or not timeline:
            raise ValueError("text and timeline are required")
        beats = [str(item.get("beat") or "") for item in timeline]
        durations = [float(item.get("end") or 0) - float(item.get("start") or 0) for item in timeline]
        proof_start = next((float(item.get("start") or 0) for item in timeline if item.get("beat") == "proof"), math.inf)
        cta_index = next((index for index, item in enumerate(timeline) if item.get("beat") == "cta"), -1)
        total_duration = max(float(item.get("end") or 0) for item in timeline)
        checks = {
            "hook_by_3_seconds": float(timeline[0].get("start") or 0) == 0 and float(timeline[0].get("end") or 0) <= 3.5,
            "proof_by_20_seconds": proof_start <= 20,
            "no_dead_beat_over_10_seconds": max(durations) <= 10,
            "at_least_five_semantic_beats": len(timeline) >= 5,
            "payoff_present": "payoff" in beats,
            "cta_after_payoff": cta_index > beats.index("payoff") if "payoff" in beats and cta_index >= 0 else False,
            "pattern_changes_present": len(set(beats)) >= 5,
            "cta_in_final_third": cta_index >= 0 and float(timeline[cta_index].get("start") or 0) >= total_duration * 0.66,
        }
        score = 100.0 * sum(checks.values()) / len(checks)
        decision = "PASS" if score >= 85 and checks["hook_by_3_seconds"] and checks["proof_by_20_seconds"] else "REVISE_ATTENTION"
        return self.store.put_audit(
            "attention_script",
            subject_id,
            decision,
            score,
            {
                "checks": checks,
                "failures": [
                    name for name, passed in checks.items() if not passed
                ],
                "threshold": 85,
                "input_binding": input_binding,
            },
        )

    def video_preflight(self, payload: dict[str, Any]) -> dict[str, Any]:
        payload, input_binding = self.store.bind_script_audit_payload(payload)
        timeline = payload.get("timeline") or []
        subject_id = str(payload.get("script_id") or payload.get("video_id") or stable_id("timeline", timeline))
        if not timeline:
            raise ValueError("semantic timeline is required; pixel-change guesses cannot substitute for planned meaning")
        required = ("start", "end", "beat", "text")
        complete = all(all(key in item and item[key] not in (None, "") for key in required) for item in timeline)
        ordered = all(
            float(item["start"]) < float(item["end"]) and (index == 0 or float(item["start"]) >= float(timeline[index - 1]["end"]) - 0.05)
            for index, item in enumerate(timeline)
        ) if complete else False
        durations = [float(item["end"]) - float(item["start"]) for item in timeline] if complete else []
        checks = {
            "semantic_timeline_complete": complete,
            "timeline_ordered": ordered,
            "no_semantic_dead_zone_over_10_seconds": bool(durations) and max(durations) <= 10,
            "visual_or_semantic_change_density": len(timeline) >= 5,
            "proof_beat_present": any(item.get("beat") == "proof" for item in timeline),
        }
        score = 100.0 * sum(checks.values()) / len(checks)
        decision = "PASS" if all(checks.values()) else "REVISE_VIDEO_PREFLIGHT"
        return self.store.put_audit(
            "attention_video_preflight",
            subject_id,
            decision,
            score,
            {
                "checks": checks,
                "failures": [
                    name for name, passed in checks.items() if not passed
                ],
                "input_binding": input_binding,
            },
        )

    def video_file_audit(self, payload: dict[str, Any]) -> dict[str, Any]:
        video_path = Path(str(payload.get("video_path") or "")).expanduser()
        timeline = payload.get("timeline") or []
        if not video_path.is_file():
            raise ValueError(f"video file does not exist: {video_path}")
        if not timeline:
            raise ValueError("semantic timeline is required; an actual-video audit cannot pass on pixel changes alone")
        probe = probe_media(video_path)
        frame_report = sample_frame_changes(video_path, float(probe.get("duration_seconds") or 0.0))
        preflight = self.video_preflight({"video_id": str(video_path), "timeline": timeline})
        video_duration = float(probe.get("duration_seconds") or 0.0)
        timeline_duration = max(float(item.get("end") or 0.0) for item in timeline)
        coverage_tolerance = max(1.0, video_duration * 0.05)
        checks = {
            "decodable_video": bool(probe.get("has_video")),
            "audio_present": bool(probe.get("has_audio")),
            "positive_duration": video_duration > 0,
            "semantic_preflight_passed": preflight["decision"] == "PASS",
            "semantic_timeline_covers_video": abs(timeline_duration - video_duration) <= coverage_tolerance,
            "frames_sampled": frame_report["sample_count"] >= 3,
            "observable_visual_changes": frame_report["change_count"] >= 1,
        }
        score = 100.0 * sum(checks.values()) / len(checks)
        decision = "PASS" if all(checks.values()) else "REVISE_ACTUAL_VIDEO"
        return self.store.put_audit(
            "attention_video_file",
            str(video_path),
            decision,
            score,
            {
                "checks": checks,
                "failures": [name for name, passed in checks.items() if not passed],
                "media_probe": probe,
                "frame_change_report": frame_report,
                "semantic_preflight_audit_id": preflight["audit_id"],
                "policy": "Pixel differences are supporting evidence only; semantic timeline and media validity are mandatory.",
            },
        )


def probe_media(path: Path) -> dict[str, Any]:
    command = [
        os.environ.get("FFPROBE_BIN", "/opt/homebrew/bin/ffprobe"),
        "-v", "error", "-show_entries", "format=duration:stream=codec_type,codec_name,width,height,r_frame_rate",
        "-of", "json", str(path),
    ]
    try:
        completed = subprocess.run(command, check=True, capture_output=True, text=True, timeout=30)
    except subprocess.CalledProcessError as exc:
        detail = (exc.stderr or exc.stdout or "ffprobe failed").strip().splitlines()[-1:]
        raise ValueError("media probe failed: " + (detail[0][:240] if detail else "ffprobe failed")) from exc
    except subprocess.TimeoutExpired as exc:
        raise ValueError("media probe timed out after 30 seconds") from exc
    payload = json.loads(completed.stdout)
    streams = payload.get("streams") or []
    video = next((item for item in streams if item.get("codec_type") == "video"), {})
    return {
        "duration_seconds": round(float((payload.get("format") or {}).get("duration") or 0.0), 3),
        "has_video": bool(video),
        "has_audio": any(item.get("codec_type") == "audio" for item in streams),
        "video_codec": video.get("codec_name"),
        "width": video.get("width"),
        "height": video.get("height"),
        "frame_rate": video.get("r_frame_rate"),
    }


def sample_frame_changes(path: Path, duration: float) -> dict[str, Any]:
    import cv2

    capture = cv2.VideoCapture(str(path))
    if not capture.isOpened():
        return {"sample_count": 0, "change_count": 0, "mean_change": None, "error": "video_decode_failed"}
    sample_count = max(3, min(24, int(math.ceil(duration / 2.5)))) if duration else 3
    # Avoid sampling exactly at EOF, where valid files commonly return no frame.
    sample_span = duration * 0.98 if duration else 0.0
    times = [sample_span * index / max(sample_count - 1, 1) for index in range(sample_count)]
    previous = None
    changes: list[float] = []
    successful = 0
    for second in times:
        capture.set(cv2.CAP_PROP_POS_MSEC, second * 1000)
        ok, frame = capture.read()
        if not ok:
            continue
        gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
        gray = cv2.resize(gray, (160, 90))
        successful += 1
        if previous is not None:
            changes.append(float(cv2.absdiff(previous, gray).mean()))
        previous = gray
    capture.release()
    return {
        "sample_count": successful,
        "change_count": sum(value >= 3.0 for value in changes),
        "mean_change": round(sum(changes) / len(changes), 3) if changes else None,
        "threshold": 3.0,
    }


class OwnedOutcomeAttributionService:
    """First-party, immutable attribution facts and descriptive rollups.

    The service deliberately separates an observed retention change (a fact)
    from a proposed explanation (an interpretation). A curve alone can locate
    a drop; it cannot establish why a viewer left.
    """

    ATTRIBUTION_FIELDS = (
        "content_id", "campaign_id", "offer_id", "source_platform", "source_id",
    )

    def __init__(
        self,
        store: QualityStore,
        publications: OwnedPublicationAttributionService | None = None,
    ):
        self.store = store
        self.publications = publications

    @staticmethod
    def _required_text(
        payload: dict[str, Any], field: str, *, maximum: int = 240
    ) -> str:
        raw = payload.get(field)
        if raw is None:
            raise ValueError(f"{field} is required")
        if not isinstance(raw, str):
            raise ValueError(f"{field} must be a string")
        value = raw.strip()
        if not value:
            raise ValueError(f"{field} is required")
        if len(value) > maximum:
            raise ValueError(f"{field} must be at most {maximum} characters")
        return value

    @staticmethod
    def _optional_text(
        payload: dict[str, Any], field: str, *, maximum: int = 240
    ) -> str | None:
        raw = payload.get(field)
        if raw is None:
            return None
        if not isinstance(raw, str):
            raise ValueError(f"{field} must be a string")
        value = raw.strip()
        if not value:
            return None
        if len(value) > maximum:
            raise ValueError(f"{field} must be at most {maximum} characters")
        return value

    @staticmethod
    def _timestamp(payload: dict[str, Any], field: str) -> str:
        value = str(payload.get(field) or "").strip()
        if not value:
            raise ValueError(f"{field} is required")
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError as exc:
            raise ValueError(f"{field} must be an ISO-8601 timestamp") from exc
        if parsed.tzinfo is None or parsed.utcoffset() is None:
            raise ValueError(f"{field} must include a timezone")
        return parsed.astimezone(UTC).isoformat()

    @classmethod
    def _attribution(
        cls, payload: dict[str, Any], *, require_all: bool = True
    ) -> dict[str, str]:
        source = payload.get("attribution")
        if source is None:
            source = payload
        if not isinstance(source, dict):
            raise ValueError("attribution must be an object")
        result: dict[str, str] = {}
        fields = cls.ATTRIBUTION_FIELDS if require_all else ("content_id",)
        for field in fields:
            result[field] = cls._required_text(source, field)
        if not require_all:
            for field in cls.ATTRIBUTION_FIELDS[1:]:
                value = cls._optional_text(source, field)
                if value:
                    result[field] = value.lower() if field == "source_platform" else value
        if "source_platform" in result:
            result["source_platform"] = result["source_platform"].lower()
        return result

    @staticmethod
    def _metadata(payload: dict[str, Any]) -> dict[str, Any]:
        metadata = payload.get("metadata") or {}
        if not isinstance(metadata, dict):
            raise ValueError("metadata must be an object")
        encoded = json.dumps(metadata, sort_keys=True, separators=(",", ":"))
        if len(encoded.encode()) > 32_768:
            raise ValueError("metadata must be at most 32768 encoded bytes")
        return metadata

    @staticmethod
    def _whole_number(
        payload: dict[str, Any], field: str, *, minimum: int
    ) -> int:
        value = payload.get(field)
        if isinstance(value, bool):
            raise ValueError(f"{field} must be a whole number")
        try:
            numeric = float(value)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"{field} must be a whole number") from exc
        if not math.isfinite(numeric) or not numeric.is_integer():
            raise ValueError(f"{field} must be a whole number")
        result = int(numeric)
        if result < minimum:
            raise ValueError(f"{field} must be at least {minimum}")
        return result

    def ingest_event(self, payload: dict[str, Any]) -> dict[str, Any]:
        event_type = str(payload.get("event_type") or "").strip().lower()
        if event_type not in OWNED_OUTCOME_EVENT_TYPES:
            raise ValueError(
                "event_type must be one of: " + ", ".join(OWNED_OUTCOME_EVENT_TYPES)
            )
        event = {
            "contract": OWNED_ATTRIBUTION_EVENT_CONTRACT,
            "idempotency_key": self._required_text(
                payload, "idempotency_key", maximum=300
            ),
            "event_type": event_type,
            **self._attribution(payload),
            "journey_id": self._required_text(payload, "journey_id", maximum=300),
            "occurred_at": self._timestamp(payload, "occurred_at"),
            "provider_event_id": self._optional_text(
                payload, "provider_event_id", maximum=300
            ),
            "metadata": self._metadata(payload),
        }
        stored, created = self.store.put_owned_outcome_event(event)
        return {
            "status": "created" if created else "idempotent_replay",
            "created": created,
            "event": stored,
        }

    def _strict_publication(
        self, payload: dict[str, Any], attribution: dict[str, str]
    ) -> dict[str, Any]:
        if self.publications is None:
            raise ValueError("owned publication registry is unavailable")
        publication = self.publications.binding(payload)
        expected = publication["attribution"]
        mismatches = [
            field for field in self.ATTRIBUTION_FIELDS
            if attribution.get(field) != expected.get(field)
        ]
        if mismatches:
            raise ValueError(
                "attribution does not match the registered publication receipt: "
                + ", ".join(mismatches)
            )
        return publication

    def ingest_event_strict(self, payload: dict[str, Any]) -> dict[str, Any]:
        event_type = str(payload.get("event_type") or "").strip().lower()
        if event_type not in OWNED_OUTCOME_EVENT_TYPES:
            raise ValueError(
                "event_type must be one of: " + ", ".join(OWNED_OUTCOME_EVENT_TYPES)
            )
        attribution = self._attribution(payload)
        publication = self._strict_publication(payload, attribution)
        event = {
            "contract": "owned_attribution_event_v2",
            "idempotency_key": self._required_text(
                payload, "idempotency_key", maximum=300
            ),
            "event_type": event_type,
            **attribution,
            "journey_id": self._required_text(payload, "journey_id", maximum=300),
            "occurred_at": self._timestamp(payload, "occurred_at"),
            "provider_event_id": self._optional_text(
                payload, "provider_event_id", maximum=300
            ),
            "publication_binding": {
                "contract": OWNED_PUBLICATION_BINDING_CONTRACT,
                "publication_id": publication["publication_id"],
                "publication_receipt_sha256": publication[
                    "publication_receipt_sha256"
                ],
            },
            "metadata": self._metadata(payload),
        }
        stored, created = self.store.put_owned_outcome_event(event, publication)
        return {
            "status": "created" if created else "idempotent_replay",
            "created": created,
            "event": stored,
        }

    def ingest_retention_sample(self, payload: dict[str, Any]) -> dict[str, Any]:
        retained = payload.get("retained_percent")
        if isinstance(retained, bool):
            raise ValueError("retained_percent must be a number from 0 to 100")
        try:
            retained_percent = float(retained)
        except (TypeError, ValueError) as exc:
            raise ValueError("retained_percent must be a number from 0 to 100") from exc
        if not math.isfinite(retained_percent) or not 0 <= retained_percent <= 100:
            raise ValueError("retained_percent must be a number from 0 to 100")
        sample = {
            "contract": OWNED_RETENTION_SAMPLE_CONTRACT,
            "idempotency_key": self._required_text(
                payload, "idempotency_key", maximum=300
            ),
            **self._attribution(payload),
            "measurement_id": self._required_text(
                payload, "measurement_id", maximum=300
            ),
            "journey_id": self._optional_text(payload, "journey_id", maximum=300),
            "observed_at": self._timestamp(payload, "observed_at"),
            "elapsed_ms": self._whole_number(payload, "elapsed_ms", minimum=0),
            "retained_percent": round(retained_percent, 6),
            "sample_size": self._whole_number(payload, "sample_size", minimum=1),
            "metadata": self._metadata(payload),
        }
        stored, created = self.store.put_owned_retention_sample(sample)
        return {
            "status": "created" if created else "idempotent_replay",
            "created": created,
            "sample": stored,
        }

    def ingest_retention_sample_strict(
        self, payload: dict[str, Any]
    ) -> dict[str, Any]:
        retained = payload.get("retained_percent")
        if isinstance(retained, bool):
            raise ValueError("retained_percent must be a number from 0 to 100")
        try:
            retained_percent = float(retained)
        except (TypeError, ValueError) as exc:
            raise ValueError(
                "retained_percent must be a number from 0 to 100"
            ) from exc
        if not math.isfinite(retained_percent) or not 0 <= retained_percent <= 100:
            raise ValueError("retained_percent must be a number from 0 to 100")
        attribution = self._attribution(payload)
        publication = self._strict_publication(payload, attribution)
        sample = {
            "contract": "owned_retention_sample_v2",
            "idempotency_key": self._required_text(
                payload, "idempotency_key", maximum=300
            ),
            **attribution,
            "measurement_id": self._required_text(
                payload, "measurement_id", maximum=300
            ),
            "journey_id": self._optional_text(payload, "journey_id", maximum=300),
            "observed_at": self._timestamp(payload, "observed_at"),
            "elapsed_ms": self._whole_number(payload, "elapsed_ms", minimum=0),
            "retained_percent": round(retained_percent, 6),
            "sample_size": self._whole_number(payload, "sample_size", minimum=1),
            "publication_binding": {
                "contract": OWNED_PUBLICATION_BINDING_CONTRACT,
                "publication_id": publication["publication_id"],
                "publication_receipt_sha256": publication[
                    "publication_receipt_sha256"
                ],
            },
            "metadata": self._metadata(payload),
        }
        stored, created = self.store.put_owned_retention_sample(
            sample, publication
        )
        return {
            "status": "created" if created else "idempotent_replay",
            "created": created,
            "sample": stored,
        }

    def summary(self, payload: dict[str, Any]) -> dict[str, Any]:
        filters = self._attribution(payload, require_all=False)
        event_rollup = self.store.owned_outcome_rollup(filters)
        retention = self.store.owned_retention_rollup(filters)
        transitions: dict[str, dict[str, Any]] = {}
        for previous, current in zip(
            OWNED_OUTCOME_EVENT_TYPES, OWNED_OUTCOME_EVENT_TYPES[1:]
        ):
            previous_count = event_rollup["by_type"][previous]["unique_journeys"]
            linked_count = event_rollup["linked_journeys"][f"{previous}_to_{current}"]
            transitions[f"{previous}_to_{current}"] = {
                "linked_journeys": linked_count,
                "prior_stage_journeys": previous_count,
                "observed_link_rate": (
                    round(linked_count / previous_count, 6)
                    if previous_count else None
                ),
                "causal_effect": None,
            }

        observed_drops: list[dict[str, Any]] = []
        for curve in retention["measurement_curves"]:
            points = curve["points"]
            for before, after in zip(points, points[1:]):
                drop = before["retained_percent"] - after["retained_percent"]
                if drop > 0:
                    observed_drops.append({
                        "measurement_id": curve["measurement_id"],
                        "journey_id": curve["journey_id"],
                        "attribution": curve["attribution"],
                        "from_elapsed_ms": before["elapsed_ms"],
                        "to_elapsed_ms": after["elapsed_ms"],
                        "drop_percentage_points": round(drop, 4),
                        "fact_type": "descriptive_observed_drop",
                        "causal_reason": None,
                    })

        has_retention = retention["fact_count"] > 0
        causal_code = (
            "DESCRIPTIVE_RETENTION_IS_NOT_CAUSAL_EVIDENCE"
            if has_retention else "NO_RETENTION_SAMPLES"
        )
        exact_dimensions = all(filters.get(field) for field in self.ATTRIBUTION_FIELDS)
        return {
            "status": "ok",
            "contract": OWNED_OUTCOME_SUMMARY_CONTRACT,
            "attribution_scope": {
                field: filters.get(field) or "all"
                for field in self.ATTRIBUTION_FIELDS
            },
            "scope_precision": "exact" if exact_dimensions else "aggregated",
            "funnel": {
                "stages": event_rollup["by_type"],
                "transitions": transitions,
                "complete_chain": event_rollup["complete_chain"],
                "measurement": "observed_first_party_events",
                "causal_claim": False,
            },
            "retention_curve": {
                "status": "observed" if has_retention else "no_owned_samples",
                **retention,
                "time_unit": "milliseconds",
            },
            "observed_drop_facts": observed_drops,
            "causal_drop_reasons": {
                "status": "refused",
                "code": causal_code,
                "reasons": [],
                "note": (
                    "The stored retention facts locate observed changes but do not "
                    "prove why a viewer left. A causal reason requires additional "
                    "experimental or directly observed evidence."
                    if has_retention else
                    "No owned retention samples exist in this attribution scope, so "
                    "no millisecond drop location or reason can be claimed."
                ),
            },
            "ai_interpretation": {
                "status": "not_generated",
                "epistemic_status": "interpretation_not_fact",
                "causal_claim": False,
                "note": (
                    "Any future AI explanation must cite these event/sample facts and "
                    "remain explicitly labelled as a hypothesis, not an observed cause."
                ),
            },
        }


class RetentionService:
    def __init__(self, store: QualityStore):
        self.store = store

    def normalize(self, payload: dict[str, Any]) -> dict[str, Any]:
        platform = str(payload.get("platform") or "").lower()
        source_id = str(payload.get("source_id") or "") or None
        duration = float(payload.get("duration_seconds") or 0.0)
        curve = payload.get("curve") or []
        if not platform:
            raise ValueError("platform is required")
        if platform in {"instagram", "facebook", "tiktok"} and not curve:
            normalized = {
                "kind": "aggregate_only",
                "average_watch_seconds": payload.get("average_watch_seconds"),
                "completion_rate": payload.get("completion_rate"),
                "note": "No retention curve was fabricated from aggregate platform metrics.",
            }
        else:
            if duration <= 0 or not curve:
                raise ValueError("duration_seconds and a real curve are required for curve normalization")
            points = []
            for item in curve:
                elapsed = item.get("elapsed_seconds")
                if elapsed is None and item.get("elapsed_ratio") is not None:
                    elapsed = float(item["elapsed_ratio"]) * duration
                retained = item.get("retained_percent")
                if elapsed is None or retained is None:
                    raise ValueError("each curve point requires elapsed_seconds or elapsed_ratio plus retained_percent")
                points.append({"elapsed_seconds": round(float(elapsed), 3), "retained_percent": round(float(retained), 3)})
            points.sort(key=lambda item: item["elapsed_seconds"])
            normalized = {"kind": "retention_curve", "duration_seconds": duration, "points": points}
        receipt = self.store.put_receipt(
            "post_publish_retention",
            platform,
            source_id,
            payload.get("source_url"),
            normalized,
        )
        return {"status": "normalized", "platform": platform, "normalized": normalized, "receipt": receipt}

    def classify(self, payload: dict[str, Any]) -> dict[str, Any]:
        points = (payload.get("normalized") or {}).get("points") or payload.get("points") or []
        if len(points) < 2:
            return {"status": "aggregate_only", "events": [], "note": "A real curve with at least two points is required."}
        events = []
        for before, after in zip(points, points[1:]):
            drop = float(before["retained_percent"]) - float(after["retained_percent"])
            if drop >= 8:
                elapsed = float(after["elapsed_seconds"])
                events.append(
                    {
                        "elapsed_seconds": elapsed,
                        "drop_percent": round(drop, 2),
                        "classification": (
                            "observed_early_retention_drop"
                            if elapsed <= 5 else "observed_retention_drop"
                        ),
                        "fact_type": "descriptive_observed_drop",
                        "causal_reason": None,
                    }
                )
        return {
            "status": "classified",
            "events": events,
            "event_count": len(events),
            "causal_drop_reasons": {
                "status": "refused",
                "code": "DESCRIPTIVE_RETENTION_IS_NOT_CAUSAL_EVIDENCE",
                "reasons": [],
            },
        }


class ContentQualityEngine:
    def __init__(
        self,
        market_tape_path: str | Path,
        quality_db_path: str | Path,
        narrative_llm_runner: Any = None,
        relatability_llm_runner: Any = None,
        transcript_storage_root: str | Path | None = None,
        script_language_demand_enqueuer: Any = None,
    ):
        self.store = QualityStore(quality_db_path)
        self.owned_content_metrics = OwnedContentMetricTelemetry(self.store.path)
        self.script_experiments = ScriptExperimentTelemetry(self.store.path)
        self.tape = MarketTapeReader(market_tape_path)
        self.owned_publications = OwnedPublicationAttributionService(
            self.store, self.tape
        )
        self.narrative = NarrativeCoherenceService(self.store, narrative_llm_runner)
        self.viral = ViralTranscriptService(self.tape, self.store)
        self.audience = AudienceIntelligenceService(self.tape, self.store)
        self.style_guides = TranscriptStyleGuideService(self.tape, self.store)
        self.scripts = ScriptService(
            self.store, self.narrative, self.style_guides
        )
        self.relatability = RelatabilityService(self.store)
        self.ai_relatability = AIRelatabilityAdjudicator(
            self.store, relatability_llm_runner
        )
        self.attention = AttentionService(self.store)
        self.retention = RetentionService(self.store)
        self.owned_outcomes = OwnedOutcomeAttributionService(
            self.store, self.owned_publications
        )
        self.script_intelligence = ScriptIntelligenceService(
            tape=self.tape,
            store=self.store,
            viral=self.viral,
            audience=self.audience,
            style_guides=self.style_guides,
            scripts=self.scripts,
            relatability=self.relatability,
            ai_relatability=self.ai_relatability,
            attention=self.attention,
            script_experiments=self.script_experiments,
            transcript_storage_root=(
                transcript_storage_root
                or os.getenv(
                    "TRANSCRIPT_BANK_ROOT",
                    str(
                        Path.home()
                        / "Library/Application Support/ContentQuality/data/"
                        "transcript-bank"
                    ),
                )
            ),
            demand_enqueuer=script_language_demand_enqueuer,
        )

    def health(self) -> dict[str, Any]:
        tape = self.tape.health()
        script_intelligence = self.script_intelligence.readiness()
        store_counts = self.store.counts()
        owned_outcome_readiness = self.store.owned_outcome_readiness()
        tiktok_style_readiness = self.style_guides.status("tiktok")
        openai_key = str(os.getenv("OPENAI_API_KEY") or "").strip()
        usable_openai_key = bool(
            openai_key and not openai_key.startswith("__")
        )
        narrative_ai_configured = bool(
            self.narrative.llm_runner is not None
            and usable_openai_key
        )
        relatability_ai_configured = bool(
            self.ai_relatability.llm_runner is not None
            and usable_openai_key
        )
        return {
            "status": "healthy" if tape["status"] == "up" else "degraded",
            "service": "content-quality",
            "market_tape": tape,
            "learning_store": {"status": "up", "path": str(self.store.path), "counts": store_counts},
            "capabilities": [
                "audience-intelligence", "viral-transcripts", "evidence-first-scripts",
                "narrative-coherence", "relatability", "attention", "retention", "learning-memory",
                "script-intelligence", "owned-outcome-attribution",
                "owned-publication-attribution-v2",
                "owned-content-metrics", "transcript-style-guides",
                "script-experiment-telemetry",
            ],
            "data_readiness": {
                "script_intelligence": script_intelligence,
                "script_language_demand_feedback": {
                    "status": (
                        "ready"
                        if self.script_intelligence.demand_enqueuer is not None
                        else "not_configured"
                    ),
                    "transport": "authenticated_loopback_api",
                    "direct_cross_database_writes": False,
                },
                "owned_retention": {
                    **owned_outcome_readiness,
                },
                "owned_content_metrics": self.owned_content_metrics.health(),
                "script_experiment_telemetry": (
                    self.script_experiments.health()
                ),
                "tiktok_transcript_style": tiktok_style_readiness,
            },
            "ai_readiness": {
                "narrative_judge_configured": narrative_ai_configured,
                "relatability_judge_configured": relatability_ai_configured,
                "deterministic_services_available": True,
                "note": (
                    "Narrative and relatability AI judgments are configured."
                    if narrative_ai_configured and relatability_ai_configured
                    else (
                        "Deterministic services remain available; one or more "
                        "production AI judges are not configured."
                    )
                ),
            },
            "checked_at": utc_now(),
        }
