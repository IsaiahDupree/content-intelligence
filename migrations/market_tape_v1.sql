-- Social Market Tape V1: shared control-plane mirror.
-- Local acquisition remains available during network outages via a transactional SQLite outbox.

create table if not exists public.actp_market_creators (
  creator_id text primary key,
  platform text not null,
  external_id text not null,
  handle text not null default '',
  display_name text not null default '',
  followers bigint not null default 0 check (followers >= 0),
  first_seen_at timestamptz not null,
  last_seen_at timestamptz not null,
  unique (platform, external_id)
);

create table if not exists public.actp_market_videos (
  video_id text primary key,
  platform text not null,
  external_id text not null,
  creator_id text not null references public.actp_market_creators(creator_id),
  published_at timestamptz,
  first_seen_at timestamptz not null,
  last_seen_at timestamptz not null,
  title text not null default '',
  caption text not null default '',
  description text not null default '',
  language text not null default '',
  url text not null default '',
  thumbnail_url text not null default '',
  media_type text not null default 'video',
  duration_seconds double precision,
  source_first_seen text not null,
  unique (platform, external_id)
);

create table if not exists public.actp_market_observations (
  observation_key text primary key,
  run_id text not null,
  observed_at timestamptz not null,
  wall_clock_date date not null,
  video_id text not null references public.actp_market_videos(video_id),
  creator_id text not null references public.actp_market_creators(creator_id),
  platform text not null,
  source_id text not null,
  video_age_seconds double precision,
  video_age_bucket text not null,
  views bigint not null default 0 check (views >= 0),
  likes bigint not null default 0 check (likes >= 0),
  comments bigint not null default 0 check (comments >= 0),
  shares bigint not null default 0 check (shares >= 0),
  saves bigint not null default 0 check (saves >= 0),
  creator_followers bigint not null default 0 check (creator_followers >= 0),
  view_velocity double precision not null default 0,
  view_acceleration double precision not null default 0,
  view_jerk double precision not null default 0,
  relative_strength double precision not null default 0,
  raw_sha256 text not null,
  source_confidence double precision not null default 1
);

create index if not exists actp_market_observations_video_time_idx
  on public.actp_market_observations(video_id, observed_at desc);
create index if not exists actp_market_observations_platform_time_idx
  on public.actp_market_observations(platform, observed_at desc);
create index if not exists actp_market_observations_context_idx
  on public.actp_market_observations(platform, video_age_bucket, view_velocity);

create table if not exists public.actp_content_genomes (
  video_id text primary key references public.actp_market_videos(video_id),
  schema_version integer not null default 1,
  title text not null default '',
  caption text not null default '',
  description text not null default '',
  hashtags_json jsonb not null default '[]'::jsonb,
  transcript text not null default '',
  language text not null default '',
  hook_type text not null default '',
  opening_words text not null default '',
  duration_seconds double precision,
  aspect_ratio text not null default '',
  cut_rate double precision,
  caption_style text not null default '',
  face_present integer,
  people_count integer,
  camera_motion text not null default '',
  audio_id text not null default '',
  audio_signature text not null default '',
  topic_terms_json jsonb not null default '[]'::jsonb,
  text_embedding_ref text not null default '',
  transcript_embedding_ref text not null default '',
  visual_embedding_ref text not null default '',
  audio_embedding_ref text not null default '',
  extraction_status text not null default 'metadata_complete',
  updated_at timestamptz not null
);

create table if not exists public.actp_trends (
  trend_id text primary key,
  trend_type text not null,
  canonical_key text not null,
  display_name text not null,
  status text not null default 'discovering',
  first_seen_at timestamptz not null,
  last_seen_at timestamptz not null,
  unique (trend_type, canonical_key)
);

create table if not exists public.actp_trend_memberships (
  trend_id text not null references public.actp_trends(trend_id),
  video_id text not null references public.actp_market_videos(video_id),
  confidence double precision not null,
  evidence_json jsonb not null,
  first_seen_at timestamptz not null,
  primary key (trend_id, video_id)
);

create table if not exists public.actp_trend_observations (
  trend_observation_key text primary key,
  trend_id text not null references public.actp_trends(trend_id),
  observed_at timestamptz not null,
  videos_total integer not null,
  videos_new_1h integer not null,
  creators_total integer not null,
  creators_new_1h integer not null,
  platforms_total integer not null,
  views_total bigint not null,
  likes_total bigint not null,
  comments_total bigint not null,
  shares_total bigint not null,
  median_video_velocity double precision not null,
  p90_video_velocity double precision not null,
  creator_breadth double precision not null,
  platform_breadth double precision not null,
  top1_concentration double precision not null,
  top10_concentration double precision not null,
  momentum double precision not null,
  acceleration double precision not null,
  relative_strength double precision not null,
  saturation double precision not null,
  trend_strength double precision not null,
  index_version text not null,
  state text not null
);

create index if not exists actp_trend_observations_time_idx
  on public.actp_trend_observations(trend_id, observed_at desc);

create table if not exists public.actp_market_collection_runs (
  run_id text primary key,
  mode text not null,
  started_at timestamptz not null,
  finished_at timestamptz,
  state text not null,
  items_seen integer not null default 0,
  observations_added integer not null default 0,
  unique_videos_added integer not null default 0,
  requests integer not null default 0,
  estimated_cost_usd numeric(14,6) not null default 0,
  error_detail text not null default ''
);

create table if not exists public.actp_market_source_receipts (
  receipt_key text primary key,
  run_id text not null references public.actp_market_collection_runs(run_id),
  source_id text not null,
  platform text not null,
  state text not null,
  started_at timestamptz not null,
  finished_at timestamptz not null,
  request_count integer not null,
  discovered_count integer not null,
  refreshed_count integer not null,
  accepted_count integer not null,
  duplicate_count integer not null,
  failed_count integer not null,
  quota_remaining integer,
  estimated_cost_usd numeric(14,6) not null,
  error_code text not null,
  error_detail text not null,
  cursor text not null,
  metadata_json jsonb not null default '{}'::jsonb
);

create table if not exists public.actp_market_source_health (
  source_id text primary key,
  platform text not null,
  state text not null,
  checked_at timestamptz not null,
  last_success_at timestamptz,
  consecutive_failures integer not null default 0,
  next_retry_at timestamptz,
  error_code text not null default '',
  error_detail text not null default '',
  receipt_json jsonb not null default '{}'::jsonb
);

create table if not exists public.actp_market_predictions (
  prediction_key text primary key,
  subject_type text not null check (subject_type in ('video', 'trend')),
  subject_id text not null,
  model_version text not null,
  predicted_at timestamptz not null,
  horizon text not null,
  probability double precision not null check (probability >= 0 and probability <= 1),
  expected_peak_at timestamptz,
  expected_remaining_life_hours double precision,
  features_json jsonb not null default '{}'::jsonb,
  outcome_json jsonb
);

create index if not exists actp_market_predictions_subject_time_idx
  on public.actp_market_predictions(subject_type, subject_id, predicted_at desc);

create or replace function public.actp_reject_market_tape_mutation()
returns trigger language plpgsql as $$
begin
  raise exception 'market tape rows are append-only';
end;
$$;

drop trigger if exists actp_market_observations_no_update on public.actp_market_observations;
create trigger actp_market_observations_no_update
before update or delete on public.actp_market_observations
for each row execute function public.actp_reject_market_tape_mutation();

drop trigger if exists actp_trend_observations_no_update on public.actp_trend_observations;
create trigger actp_trend_observations_no_update
before update or delete on public.actp_trend_observations
for each row execute function public.actp_reject_market_tape_mutation();

alter table public.actp_market_creators enable row level security;
alter table public.actp_market_videos enable row level security;
alter table public.actp_market_observations enable row level security;
alter table public.actp_content_genomes enable row level security;
alter table public.actp_trends enable row level security;
alter table public.actp_trend_memberships enable row level security;
alter table public.actp_trend_observations enable row level security;
alter table public.actp_market_collection_runs enable row level security;
alter table public.actp_market_source_receipts enable row level security;
alter table public.actp_market_source_health enable row level security;
alter table public.actp_market_predictions enable row level security;

comment on table public.actp_market_observations is
  'Immutable social metric observations. Service-role writes only; no anon/authenticated policies.';
