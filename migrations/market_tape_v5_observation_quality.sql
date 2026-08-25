-- Market Tape V5: immutable quarantine evidence for cumulative-counter regressions.

alter table public.actp_trend_observations
  add column if not exists observation_quality_contract text not null
  default 'legacy_unverified';

create table if not exists public.actp_market_observation_quality_flags (
  flag_id text primary key,
  observation_key text not null unique
    references public.actp_market_observations(observation_key),
  prior_observation_key text not null
    references public.actp_market_observations(observation_key),
  run_id text not null references public.actp_market_collection_runs(run_id),
  video_id text not null references public.actp_market_videos(video_id),
  source_id text not null,
  detected_at timestamptz not null,
  observed_at timestamptz not null,
  views bigint not null check (views >= 0),
  prior_observed_at timestamptz not null,
  prior_views bigint not null check (prior_views >= 0),
  error_code text not null check (error_code = 'counter_regression'),
  raw_sha256 text not null,
  metadata_json jsonb not null default '{}'::jsonb,
  constraint actp_market_observation_quality_flag_identity_ck
    check (flag_id = 'counter-regression:' || observation_key)
);

create index if not exists actp_market_observation_quality_video_time_idx
  on public.actp_market_observation_quality_flags(video_id, observed_at desc);
create index if not exists actp_market_observation_quality_error_time_idx
  on public.actp_market_observation_quality_flags(error_code, detected_at desc);

drop trigger if exists actp_market_observation_quality_flags_no_update
  on public.actp_market_observation_quality_flags;
create trigger actp_market_observation_quality_flags_no_update
before update or delete on public.actp_market_observation_quality_flags
for each row execute function public.actp_reject_market_tape_mutation();

alter table public.actp_market_observation_quality_flags enable row level security;

comment on table public.actp_market_observation_quality_flags is
  'Immutable audit ledger for raw cumulative-counter rows quarantined from analytics.';
