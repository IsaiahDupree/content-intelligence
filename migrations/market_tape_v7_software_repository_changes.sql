-- Add fresh repository-change evidence to the durable semantic layer.
-- This changes only enum checks; no existing rows are rewritten.

begin;

alter table public.actp_semantic_signal_candidates
  drop constraint if exists actp_semantic_signal_candidates_source_kind_check;
alter table public.actp_semantic_signal_candidates
  add constraint actp_semantic_signal_candidates_source_kind_check
  check (source_kind in (
    'market_tape_trend', 'market_tape_keyword', 'market_tape_query',
    'market_tape_opportunity', 'transcript_phrase', 'external_signal',
    'software_repository_change'
  ));

alter table public.actp_semantic_content_evidence_receipts
  drop constraint if exists actp_semantic_content_evidence_receipts_evidence_type_check;
alter table public.actp_semantic_content_evidence_receipts
  add constraint actp_semantic_content_evidence_receipts_evidence_type_check
  check (evidence_type in (
    'transcript_receipt', 'audience_evidence', 'human_moment',
    'conversion_evidence', 'external_reference', 'software_change_receipt'
  ));

commit;
