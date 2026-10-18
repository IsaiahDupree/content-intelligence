"""Shared evidence contracts for the Content Quality product boundary."""

from __future__ import annotations


CURRENT_TRANSCRIPT_AUDIT_CONTRACT = "performance_bound_whisper_transcript_v4"
SUPPORTED_TRANSCRIPT_AUDIT_CONTRACTS = frozenset({
    "performance_bound_whisper_transcript_v3",
    CURRENT_TRANSCRIPT_AUDIT_CONTRACT,
})

SCRIPT_INTELLIGENCE_BRIEF_CONTRACT = "script_intelligence_brief_v1"
SCRIPT_LANGUAGE_DEMAND_CONTRACT = "market_tape_script_language_demand_v1"
TREND_OBSERVATION_QUALITY_CONTRACT = "market_tape_accepted_observation_lineage_v2"
ACCEPTED_OBSERVATION_EVIDENCE_CONTRACT = "market_tape_accepted_observation_evidence_v1"


def is_supported_transcript_audit_contract(value: object) -> bool:
    return str(value or "").strip() in SUPPORTED_TRANSCRIPT_AUDIT_CONTRACTS
