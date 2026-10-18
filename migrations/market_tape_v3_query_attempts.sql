-- Market Tape V3: immutable query-attempt coverage, including empty results.

create table if not exists public.actp_market_query_attempts (
  attempt_key text primary key,
  run_id text not null references public.actp_market_collection_runs(run_id),
  source_id text not null,
  platform text not null,
  query text not null,
  attempted_at timestamptz not null,
  finished_at timestamptz not null,
  state text not null,
  result_count integer not null default 0 check (result_count >= 0),
  request_count integer not null default 0 check (request_count >= 0),
  error_code text not null default '',
  error_detail text not null default '',
  artifact_path text not null default '',
  artifact_sha256 text not null default '',
  metadata_json jsonb not null default '{}'::jsonb
);

create index if not exists actp_market_query_attempts_query_time_idx
  on public.actp_market_query_attempts(query, attempted_at desc);

create index if not exists actp_market_query_attempts_platform_time_idx
  on public.actp_market_query_attempts(platform, attempted_at desc);

drop trigger if exists actp_market_query_attempts_no_update
  on public.actp_market_query_attempts;
create trigger actp_market_query_attempts_no_update
before update or delete on public.actp_market_query_attempts
for each row execute function public.actp_reject_market_tape_mutation();

alter table public.actp_market_query_attempts enable row level security;

comment on table public.actp_market_query_attempts is
  'Immutable proof of every platform/query attempt, including empty and failed searches.';
