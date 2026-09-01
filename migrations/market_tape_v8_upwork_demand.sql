-- Market Tape V8: immutable RapidAPI Upwork buyer-demand evidence.
-- Provider requests are reserved before execution and every derived record
-- retains an append-only path back to its scan and raw archive receipt.

begin;

create table if not exists public.actp_upwork_request_reservations (
  request_reservation_id text primary key,
  contract text not null check (
    contract = 'market_tape_upwork_request_reservation_v1'
  ),
  reserved_at timestamptz not null,
  usage_date date not null,
  request_units integer not null check (request_units > 0),
  query_set_sha256 text not null check (length(query_set_sha256) = 64),
  reservation_sha256 text not null unique check (length(reservation_sha256) = 64)
);
create index if not exists actp_upwork_reservations_usage_idx
  on public.actp_upwork_request_reservations(usage_date, reserved_at);

create table if not exists public.actp_upwork_scan_runs (
  scan_run_id text primary key,
  contract text not null check (contract = 'market_tape_upwork_scan_v1'),
  request_reservation_id text not null unique,
  started_at timestamptz not null,
  finished_at timestamptz not null,
  observed_at timestamptz not null,
  query_count integer not null check (query_count > 0),
  request_units integer not null check (request_units >= 0),
  accepted_job_count integer not null check (accepted_job_count >= 0),
  rejected_job_count integer not null check (rejected_job_count >= 0),
  state text not null check (state in ('complete', 'partial', 'failed')),
  raw_archive_sha256 text not null check (
    raw_archive_sha256 = '' or length(raw_archive_sha256) = 64
  ),
  error_code text not null default '',
  error_detail text not null default '',
  scan_sha256 text not null unique check (length(scan_sha256) = 64),
  foreign key (request_reservation_id)
    references public.actp_upwork_request_reservations(request_reservation_id)
);
create index if not exists actp_upwork_scans_observed_idx
  on public.actp_upwork_scan_runs(observed_at desc, scan_run_id);

-- ``actp_upwork_jobs`` belongs to the legacy proposal/build workflow and has
-- a different UUID/status-oriented contract. This append-only Market Tape
-- identity ledger intentionally uses a non-colliding table name.
create table if not exists public.actp_upwork_market_jobs (
  job_id text primary key,
  contract text not null check (contract = 'market_tape_upwork_job_v1'),
  provider_job_id text not null unique,
  canonical_url text not null unique,
  first_seen_at timestamptz not null,
  identity_sha256 text not null unique check (length(identity_sha256) = 64)
);

create table if not exists public.actp_upwork_job_versions (
  job_version_id text primary key,
  contract text not null check (
    contract = 'market_tape_upwork_job_version_v1'
  ),
  job_id text not null,
  observed_at timestamptz not null,
  title text not null,
  description text not null,
  published_at timestamptz,
  client_id text not null default '',
  budget_type text not null default '',
  budget_amount double precision,
  budget_currency text not null default '',
  hourly_min double precision,
  hourly_max double precision,
  proposal_count integer,
  experience_level text not null default '',
  country text not null default '',
  skills_json jsonb not null,
  category text not null check (category in (
    'ai_demand', 'ai_enabled_vertical', 'general_freelancing', 'other'
  )),
  request_intent text not null,
  raw_archive_sha256 text not null check (length(raw_archive_sha256) = 64),
  payload_sha256 text not null check (length(payload_sha256) = 64),
  version_sha256 text not null unique check (length(version_sha256) = 64),
  foreign key (job_id) references public.actp_upwork_market_jobs(job_id)
);
create index if not exists actp_upwork_versions_job_time_idx
  on public.actp_upwork_job_versions(job_id, observed_at desc, job_version_id);

create table if not exists public.actp_upwork_query_observations (
  query_observation_id text primary key,
  contract text not null check (
    contract = 'market_tape_upwork_query_observation_v1'
  ),
  scan_run_id text not null,
  query_text text not null,
  normalized_query text not null,
  observed_at timestamptz not null,
  returned_count integer not null check (returned_count >= 0),
  accepted_count integer not null check (accepted_count >= 0),
  rejected_count integer not null check (rejected_count >= 0),
  partial_evidence smallint not null check (partial_evidence in (0, 1)),
  response_sha256 text not null check (length(response_sha256) = 64),
  observation_sha256 text not null unique check (length(observation_sha256) = 64),
  foreign key (scan_run_id) references public.actp_upwork_scan_runs(scan_run_id)
);
create index if not exists actp_upwork_queries_query_time_idx
  on public.actp_upwork_query_observations(
    normalized_query, observed_at desc, query_observation_id
  );
create index if not exists actp_upwork_query_observations_scan_idx
  on public.actp_upwork_query_observations(scan_run_id);

create table if not exists public.actp_upwork_job_observations (
  job_observation_id text primary key,
  contract text not null check (
    contract = 'market_tape_upwork_job_observation_v1'
  ),
  scan_run_id text not null,
  query_observation_id text not null,
  job_id text not null,
  job_version_id text not null,
  observed_at timestamptz not null,
  is_new_job smallint not null check (is_new_job in (0, 1)),
  result_position integer not null check (result_position >= 0),
  observation_sha256 text not null unique check (length(observation_sha256) = 64),
  unique (scan_run_id, query_observation_id, job_id),
  foreign key (scan_run_id) references public.actp_upwork_scan_runs(scan_run_id),
  foreign key (query_observation_id)
    references public.actp_upwork_query_observations(query_observation_id),
  foreign key (job_id) references public.actp_upwork_market_jobs(job_id),
  foreign key (job_version_id)
    references public.actp_upwork_job_versions(job_version_id)
);
create index if not exists actp_upwork_job_observations_job_time_idx
  on public.actp_upwork_job_observations(
    job_id, observed_at desc, job_observation_id
  );
create index if not exists actp_upwork_job_observations_query_idx
  on public.actp_upwork_job_observations(query_observation_id);
create index if not exists actp_upwork_job_observations_version_idx
  on public.actp_upwork_job_observations(job_version_id);

create table if not exists public.actp_upwork_demand_snapshots (
  demand_snapshot_id text primary key,
  contract text not null check (
    contract = 'market_tape_upwork_demand_snapshot_v1'
  ),
  scan_run_id text not null,
  cohort_type text not null check (cohort_type in (
    'query', 'category', 'skill', 'intent'
  )),
  cohort_key text not null,
  observed_at timestamptz not null,
  unique_jobs integer not null check (unique_jobs >= 0),
  new_jobs integer not null check (
    new_jobs >= 0 and new_jobs <= unique_jobs
  ),
  unique_clients integer not null check (
    unique_clients >= 0 and unique_clients <= unique_jobs
  ),
  fixed_budget_usd_coverage double precision not null check (
    fixed_budget_usd_coverage between 0.0 and 1.0
  ),
  median_fixed_budget_usd double precision check (
    median_fixed_budget_usd is null or median_fixed_budget_usd >= 0
  ),
  hourly_rate_usd_coverage double precision not null check (
    hourly_rate_usd_coverage between 0.0 and 1.0
  ),
  median_hourly_rate_usd double precision check (
    median_hourly_rate_usd is null or median_hourly_rate_usd >= 0
  ),
  proposal_coverage double precision not null check (
    proposal_coverage between 0.0 and 1.0
  ),
  median_proposals double precision check (
    median_proposals is null or median_proposals >= 0
  ),
  velocity double precision not null,
  acceleration double precision not null,
  evidence_state text not null check (evidence_state in (
    'complete', 'partial', 'insufficient'
  )),
  partial_evidence smallint not null check (partial_evidence in (0, 1)),
  evidence_sha256 text not null check (length(evidence_sha256) = 64),
  snapshot_sha256 text not null unique check (length(snapshot_sha256) = 64),
  unique (scan_run_id, cohort_type, cohort_key),
  foreign key (scan_run_id) references public.actp_upwork_scan_runs(scan_run_id)
);
create index if not exists actp_upwork_snapshots_cohort_time_idx
  on public.actp_upwork_demand_snapshots(
    cohort_type, cohort_key, observed_at desc, demand_snapshot_id
  );

create table if not exists public.actp_upwork_predictions (
  prediction_id text primary key,
  contract text not null check (
    contract = 'market_tape_upwork_demand_prediction_v1'
  ),
  demand_snapshot_id text not null unique,
  cohort_type text not null,
  cohort_key text not null,
  as_of timestamptz not null,
  direction text not null check (direction in (
    'rising', 'falling', 'flat', 'abstain'
  )),
  confidence double precision not null check (confidence between 0.0 and 1.0),
  model_version text not null,
  history_snapshot_ids_json jsonb not null,
  input_sha256 text not null check (length(input_sha256) = 64),
  prediction_sha256 text not null unique check (length(prediction_sha256) = 64),
  foreign key (demand_snapshot_id)
    references public.actp_upwork_demand_snapshots(demand_snapshot_id)
);
create index if not exists actp_upwork_predictions_cohort_time_idx
  on public.actp_upwork_predictions(cohort_type, cohort_key, as_of desc);

create table if not exists public.actp_upwork_prediction_outcomes (
  prediction_outcome_id text primary key,
  contract text not null check (
    contract = 'market_tape_upwork_prediction_outcome_v1'
  ),
  prediction_id text not null unique,
  observed_snapshot_id text not null,
  evaluated_at timestamptz not null,
  actual_direction text not null check (actual_direction in (
    'rising', 'falling', 'flat'
  )),
  directional_correct smallint check (directional_correct in (0, 1)),
  brier_score double precision,
  outcome_sha256 text not null unique check (length(outcome_sha256) = 64),
  foreign key (prediction_id)
    references public.actp_upwork_predictions(prediction_id),
  foreign key (observed_snapshot_id)
    references public.actp_upwork_demand_snapshots(demand_snapshot_id)
);
create index if not exists actp_upwork_outcomes_evaluated_idx
  on public.actp_upwork_prediction_outcomes(evaluated_at desc);
create index if not exists actp_upwork_prediction_outcomes_snapshot_idx
  on public.actp_upwork_prediction_outcomes(observed_snapshot_id);

create table if not exists public.actp_upwork_semantic_links (
  semantic_link_id text primary key,
  contract text not null check (contract = 'upwork_market_demand_signal_v1'),
  demand_snapshot_id text not null,
  signal_id text not null,
  graph_version_id text not null,
  cohort_type text not null,
  cohort_key text not null,
  created_at timestamptz not null,
  automatic_binding smallint not null check (automatic_binding = 0),
  link_sha256 text not null unique check (length(link_sha256) = 64),
  unique (demand_snapshot_id, signal_id, graph_version_id),
  foreign key (demand_snapshot_id)
    references public.actp_upwork_demand_snapshots(demand_snapshot_id),
  foreign key (signal_id, graph_version_id)
    references public.actp_semantic_signal_candidates(signal_id, graph_version_id),
  foreign key (graph_version_id)
    references public.actp_semantic_topic_graph_versions(graph_version_id)
);
create index if not exists actp_upwork_semantic_links_signal_idx
  on public.actp_upwork_semantic_links(
    graph_version_id, signal_id, created_at desc
  );
create index if not exists actp_upwork_semantic_links_signal_graph_idx
  on public.actp_upwork_semantic_links(signal_id, graph_version_id);

drop trigger if exists actp_upwork_request_reservations_no_update
  on public.actp_upwork_request_reservations;
create trigger actp_upwork_request_reservations_no_update
before update or delete on public.actp_upwork_request_reservations
for each row execute function public.actp_reject_market_tape_mutation();

drop trigger if exists actp_upwork_scan_runs_no_update
  on public.actp_upwork_scan_runs;
create trigger actp_upwork_scan_runs_no_update
before update or delete on public.actp_upwork_scan_runs
for each row execute function public.actp_reject_market_tape_mutation();

drop trigger if exists actp_upwork_market_jobs_no_update
  on public.actp_upwork_market_jobs;
create trigger actp_upwork_market_jobs_no_update
before update or delete on public.actp_upwork_market_jobs
for each row execute function public.actp_reject_market_tape_mutation();

drop trigger if exists actp_upwork_job_versions_no_update
  on public.actp_upwork_job_versions;
create trigger actp_upwork_job_versions_no_update
before update or delete on public.actp_upwork_job_versions
for each row execute function public.actp_reject_market_tape_mutation();

drop trigger if exists actp_upwork_query_observations_no_update
  on public.actp_upwork_query_observations;
create trigger actp_upwork_query_observations_no_update
before update or delete on public.actp_upwork_query_observations
for each row execute function public.actp_reject_market_tape_mutation();

drop trigger if exists actp_upwork_job_observations_no_update
  on public.actp_upwork_job_observations;
create trigger actp_upwork_job_observations_no_update
before update or delete on public.actp_upwork_job_observations
for each row execute function public.actp_reject_market_tape_mutation();

drop trigger if exists actp_upwork_demand_snapshots_no_update
  on public.actp_upwork_demand_snapshots;
create trigger actp_upwork_demand_snapshots_no_update
before update or delete on public.actp_upwork_demand_snapshots
for each row execute function public.actp_reject_market_tape_mutation();

drop trigger if exists actp_upwork_predictions_no_update
  on public.actp_upwork_predictions;
create trigger actp_upwork_predictions_no_update
before update or delete on public.actp_upwork_predictions
for each row execute function public.actp_reject_market_tape_mutation();

drop trigger if exists actp_upwork_prediction_outcomes_no_update
  on public.actp_upwork_prediction_outcomes;
create trigger actp_upwork_prediction_outcomes_no_update
before update or delete on public.actp_upwork_prediction_outcomes
for each row execute function public.actp_reject_market_tape_mutation();

drop trigger if exists actp_upwork_semantic_links_no_update
  on public.actp_upwork_semantic_links;
create trigger actp_upwork_semantic_links_no_update
before update or delete on public.actp_upwork_semantic_links
for each row execute function public.actp_reject_market_tape_mutation();

alter table public.actp_upwork_request_reservations enable row level security;
alter table public.actp_upwork_scan_runs enable row level security;
alter table public.actp_upwork_market_jobs enable row level security;
alter table public.actp_upwork_job_versions enable row level security;
alter table public.actp_upwork_query_observations enable row level security;
alter table public.actp_upwork_job_observations enable row level security;
alter table public.actp_upwork_demand_snapshots enable row level security;
alter table public.actp_upwork_predictions enable row level security;
alter table public.actp_upwork_prediction_outcomes enable row level security;
alter table public.actp_upwork_semantic_links enable row level security;

comment on table public.actp_upwork_request_reservations is
  'Immutable pre-request RapidAPI credit reservations. Service-role writes only.';
comment on table public.actp_upwork_job_versions is
  'Immutable provider evidence. Descriptions are audit evidence, not script language.';
comment on table public.actp_upwork_semantic_links is
  'Immutable external-signal lineage; automatic topic binding is forbidden.';

commit;
