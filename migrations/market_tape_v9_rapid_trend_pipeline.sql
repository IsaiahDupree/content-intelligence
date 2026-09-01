-- Market Tape V9: immutable rapid-trend trigger and pipeline-event lineage.
-- Local SQLite observation ids are intentionally not synchronized. The two
-- content-derived observation keys bind each trigger to portable tape rows.

begin;

create table if not exists public.actp_market_rapid_trend_triggers (
  trigger_id text primary key,
  contract text not null check (
    contract = 'market_tape_rapid_trend_trigger_v1'
  ),
  trigger_sha256 text not null unique check (length(trigger_sha256) = 64),
  policy_version text not null,
  policy_sha256 text not null check (length(policy_sha256) = 64),
  trend_id text not null,
  baseline_trend_observation_key text not null,
  trigger_trend_observation_key text not null unique,
  source_run_id text not null default '',
  source_receipt_id text not null,
  evidence_sha256 text not null check (length(evidence_sha256) = 64),
  evidence_json jsonb not null,
  detected_at timestamptz not null,
  expires_at timestamptz not null check (expires_at > detected_at),
  unique (
    policy_sha256,
    trend_id,
    baseline_trend_observation_key,
    trigger_trend_observation_key
  ),
  foreign key (trend_id) references public.actp_trends(trend_id),
  foreign key (baseline_trend_observation_key)
    references public.actp_trend_observations(trend_observation_key),
  foreign key (trigger_trend_observation_key)
    references public.actp_trend_observations(trend_observation_key)
);

create index if not exists actp_market_rapid_triggers_detected_idx
  on public.actp_market_rapid_trend_triggers(detected_at desc, trigger_id);
create index if not exists actp_market_rapid_triggers_trend_idx
  on public.actp_market_rapid_trend_triggers(
    trend_id,
    detected_at desc,
    trigger_id
  );
create index if not exists actp_market_rapid_triggers_baseline_idx
  on public.actp_market_rapid_trend_triggers(
    baseline_trend_observation_key
  );
create index if not exists actp_market_rapid_triggers_expiry_idx
  on public.actp_market_rapid_trend_triggers(expires_at, trigger_id);

create table if not exists public.actp_market_rapid_trend_trigger_events (
  event_id text primary key,
  trigger_id text not null,
  event_type text not null check (event_type in (
    'detected',
    'semantic_materialized',
    'evidence_demand_enqueued',
    'handoff_ready',
    'script_queued',
    'script_completed',
    'video_queued',
    'video_completed',
    'blocked',
    'failed'
  )),
  attempt_no integer not null default 0 check (attempt_no >= 0),
  source_service text not null,
  source_receipt_id text not null,
  payload_sha256 text not null check (length(payload_sha256) = 64),
  payload_json jsonb not null,
  created_at timestamptz not null,
  unique (trigger_id, event_type, attempt_no),
  foreign key (trigger_id)
    references public.actp_market_rapid_trend_triggers(trigger_id)
);

create index if not exists actp_market_rapid_events_trigger_time_idx
  on public.actp_market_rapid_trend_trigger_events(
    trigger_id,
    created_at,
    event_id
  );
create index if not exists actp_market_rapid_events_type_time_idx
  on public.actp_market_rapid_trend_trigger_events(
    event_type,
    created_at desc,
    trigger_id
  );

drop trigger if exists actp_market_rapid_triggers_no_update
  on public.actp_market_rapid_trend_triggers;
create trigger actp_market_rapid_triggers_no_update
before update or delete on public.actp_market_rapid_trend_triggers
for each row execute function public.actp_reject_market_tape_mutation();

drop trigger if exists actp_market_rapid_events_no_update
  on public.actp_market_rapid_trend_trigger_events;
create trigger actp_market_rapid_events_no_update
before update or delete on public.actp_market_rapid_trend_trigger_events
for each row execute function public.actp_reject_market_tape_mutation();

alter table public.actp_market_rapid_trend_triggers enable row level security;
alter table public.actp_market_rapid_trend_trigger_events enable row level security;

comment on table public.actp_market_rapid_trend_triggers is
  'Immutable evidence-bound breakout crossings; service-role writes only.';
comment on table public.actp_market_rapid_trend_trigger_events is
  'Immutable provider-free pipeline-start events; service-role writes only.';
comment on column public.actp_market_rapid_trend_triggers.evidence_json is
  'Canonical trigger evidence; raw source media and transcript text are excluded.';

commit;
