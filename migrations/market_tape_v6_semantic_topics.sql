-- Market Tape V6: immutable semantic trend-to-topic graph and content lineage.
-- Raw labels remain immutable evidence; these tables preserve the reviewed
-- interpretation that carries a signal into a reusable content subject.

create table if not exists public.actp_semantic_topic_graph_versions (
  graph_version_id text primary key,
  graph_contract text not null,
  graph_schema_version text not null,
  graph_sha256 text not null unique check (length(graph_sha256) = 64),
  source_service text not null,
  source_receipt_id text not null,
  imported_by text not null,
  imported_at timestamptz not null,
  node_count integer not null check (node_count > 0),
  edge_count integer not null check (edge_count >= 0),
  metadata_json jsonb not null default '{}'::jsonb,
  migration_json jsonb not null default '{}'::jsonb,
  graph_json jsonb not null
);
create index if not exists actp_semantic_graph_versions_time_idx
  on public.actp_semantic_topic_graph_versions(imported_at desc);

create table if not exists public.actp_semantic_topic_nodes (
  graph_version_id text not null,
  topic_id text not null,
  name text not null,
  normalized_name text not null,
  definition text not null,
  level text not null check (level in (
    'strategic_territory', 'content_domain', 'pillar',
    'topic', 'subtopic', 'atomic_subject'
  )),
  canonical_parent_id text,
  aliases_json jsonb not null default '[]'::jsonb,
  status text not null check (status in ('active', 'deprecated', 'proposed')),
  strategic_priority integer not null check (strategic_priority between 0 and 100),
  imported_at timestamptz not null,
  primary key (graph_version_id, topic_id),
  unique (graph_version_id, level, normalized_name),
  foreign key (graph_version_id)
    references public.actp_semantic_topic_graph_versions(graph_version_id),
  foreign key (graph_version_id, canonical_parent_id)
    references public.actp_semantic_topic_nodes(graph_version_id, topic_id)
);
create index if not exists actp_semantic_topic_nodes_level_idx
  on public.actp_semantic_topic_nodes(graph_version_id, level, status);
create index if not exists actp_semantic_topic_nodes_parent_idx
  on public.actp_semantic_topic_nodes(graph_version_id, canonical_parent_id);

create table if not exists public.actp_semantic_topic_edges (
  graph_version_id text not null,
  edge_id text not null,
  source_topic_id text not null,
  target_topic_id text not null,
  relationship_type text not null check (relationship_type in (
    'is_a', 'part_of', 'applied_to', 'used_by', 'solves',
    'implemented_with', 'compared_with', 'depends_on', 'related_to'
  )),
  imported_at timestamptz not null,
  primary key (graph_version_id, edge_id),
  unique (graph_version_id, source_topic_id, target_topic_id, relationship_type),
  check (source_topic_id <> target_topic_id),
  foreign key (graph_version_id)
    references public.actp_semantic_topic_graph_versions(graph_version_id),
  foreign key (graph_version_id, source_topic_id)
    references public.actp_semantic_topic_nodes(graph_version_id, topic_id),
  foreign key (graph_version_id, target_topic_id)
    references public.actp_semantic_topic_nodes(graph_version_id, topic_id)
);
create index if not exists actp_semantic_topic_edges_source_idx
  on public.actp_semantic_topic_edges(
    graph_version_id, source_topic_id, relationship_type
  );
create index if not exists actp_semantic_topic_edges_target_idx
  on public.actp_semantic_topic_edges(
    graph_version_id, target_topic_id, relationship_type
  );

create table if not exists public.actp_semantic_signal_candidates (
  signal_id text primary key,
  graph_version_id text not null,
  signal_type text not null check (signal_type in (
    'topic', 'keyword', 'query', 'question', 'problem', 'objection',
    'claim', 'angle', 'hook', 'title', 'format', 'platform', 'offer',
    'hashtag', 'audio', 'opportunity', 'other'
  )),
  source_kind text not null check (source_kind in (
    'market_tape_trend', 'market_tape_keyword', 'market_tape_query',
    'market_tape_opportunity', 'transcript_phrase', 'external_signal',
    'software_repository_change'
  )),
  source_entity_id text not null,
  source_trend_id text,
  source_observed_at timestamptz not null,
  signal_text text not null,
  normalized_signal_text text not null,
  source_receipt_id text not null,
  evidence_sha256 text not null check (length(evidence_sha256) = 64),
  evidence_json jsonb not null,
  ingested_at timestamptz not null,
  unique (signal_id, graph_version_id),
  unique (
    graph_version_id, source_kind, source_entity_id,
    source_observed_at, signal_type, evidence_sha256
  ),
  foreign key (graph_version_id)
    references public.actp_semantic_topic_graph_versions(graph_version_id),
  foreign key (source_trend_id) references public.actp_trends(trend_id)
);
create index if not exists actp_semantic_signals_graph_type_idx
  on public.actp_semantic_signal_candidates(
    graph_version_id, signal_type, ingested_at desc
  );
create index if not exists actp_semantic_signals_source_idx
  on public.actp_semantic_signal_candidates(
    source_kind, source_entity_id, source_observed_at desc
  );
create index if not exists actp_semantic_signals_source_trend_idx
  on public.actp_semantic_signal_candidates(source_trend_id);

create table if not exists public.actp_semantic_signal_bindings (
  binding_id text primary key,
  signal_id text not null,
  graph_version_id text not null,
  topic_id text,
  decision text not null check (decision in (
    'approved', 'rejected', 'review_required', 'revoked', 'out_of_scope'
  )),
  binding_method text not null,
  confidence double precision not null check (confidence between 0.0 and 1.0),
  rationale text not null,
  reviewer_type text not null check (reviewer_type in (
    'human', 'rules', 'ai', 'system'
  )),
  reviewed_by text not null,
  reviewed_at timestamptz not null,
  source_receipt_id text not null,
  review_receipt_id text not null,
  exclusion_reason text not null default '',
  resolver_version text not null,
  model_version text not null default '',
  output_schema_version text not null,
  input_sha256 text not null check (length(input_sha256) = 64),
  output_sha256 text not null check (length(output_sha256) = 64),
  audit_json jsonb not null default '{}'::jsonb,
  unique (binding_id, graph_version_id, signal_id, topic_id),
  check (
    (decision = 'out_of_scope' and topic_id is null
      and reviewer_type = 'human' and length(exclusion_reason) > 0
      and length(review_receipt_id) > 0)
    or decision <> 'out_of_scope'
  ),
  check (decision in ('out_of_scope', 'review_required') or topic_id is not null),
  check (reviewer_type <> 'ai' or decision = 'review_required'),
  foreign key (signal_id, graph_version_id)
    references public.actp_semantic_signal_candidates(signal_id, graph_version_id),
  foreign key (graph_version_id, topic_id)
    references public.actp_semantic_topic_nodes(graph_version_id, topic_id)
);
create index if not exists actp_semantic_bindings_signal_time_idx
  on public.actp_semantic_signal_bindings(
    signal_id, reviewed_at desc, binding_id desc
  );
create index if not exists actp_semantic_bindings_topic_time_idx
  on public.actp_semantic_signal_bindings(
    graph_version_id, topic_id, reviewed_at desc
  );
create index if not exists actp_semantic_bindings_decision_time_idx
  on public.actp_semantic_signal_bindings(
    graph_version_id, decision, reviewed_at desc
  );
create index if not exists actp_semantic_bindings_signal_graph_idx
  on public.actp_semantic_signal_bindings(signal_id, graph_version_id);

create table if not exists public.actp_semantic_resolution_runs (
  resolution_run_id text primary key,
  signal_id text not null,
  graph_version_id text not null,
  resolver_version text not null,
  provider text not null,
  model_version text not null,
  output_schema_version text not null,
  state text not null check (state in (
    'completed', 'failed', 'blocked_credential', 'no_candidates', 'deterministic'
  )),
  input_sha256 text not null check (length(input_sha256) = 64),
  output_sha256 text not null check (length(output_sha256) = 64),
  candidate_set_json jsonb not null,
  selected_topic_id text,
  provider_decision text not null,
  confidence double precision not null check (confidence between 0.0 and 1.0),
  rationale text not null,
  response_id text not null default '',
  input_tokens integer not null default 0 check (input_tokens >= 0),
  output_tokens integer not null default 0 check (output_tokens >= 0),
  total_tokens integer not null default 0 check (total_tokens >= 0),
  error_code text not null default '',
  created_at timestamptz not null,
  unique (resolution_run_id, graph_version_id, signal_id, selected_topic_id),
  foreign key (signal_id, graph_version_id)
    references public.actp_semantic_signal_candidates(signal_id, graph_version_id),
  foreign key (graph_version_id, selected_topic_id)
    references public.actp_semantic_topic_nodes(graph_version_id, topic_id)
);
create index if not exists actp_semantic_resolution_signal_time_idx
  on public.actp_semantic_resolution_runs(
    signal_id, created_at desc, resolution_run_id desc
  );
create index if not exists actp_semantic_resolution_graph_selected_idx
  on public.actp_semantic_resolution_runs(
    graph_version_id, selected_topic_id
  );
create index if not exists actp_semantic_resolution_signal_graph_idx
  on public.actp_semantic_resolution_runs(signal_id, graph_version_id);

create table if not exists public.actp_semantic_topic_observations (
  topic_observation_id bigint generated by default as identity primary key,
  topic_observation_key text not null unique,
  graph_version_id text not null,
  topic_id text not null,
  signal_id text not null,
  binding_id text not null unique,
  source_kind text not null,
  source_entity_id text not null,
  source_observed_at timestamptz not null,
  observed_at timestamptz not null,
  signal_type text not null,
  source_receipt_id text not null,
  evidence_sha256 text not null check (length(evidence_sha256) = 64),
  metrics_json jsonb not null default '{}'::jsonb,
  foreign key (signal_id, graph_version_id)
    references public.actp_semantic_signal_candidates(signal_id, graph_version_id),
  foreign key (binding_id, graph_version_id, signal_id, topic_id)
    references public.actp_semantic_signal_bindings(
      binding_id, graph_version_id, signal_id, topic_id
    ),
  foreign key (graph_version_id, topic_id)
    references public.actp_semantic_topic_nodes(graph_version_id, topic_id)
);
create index if not exists actp_semantic_observations_topic_time_idx
  on public.actp_semantic_topic_observations(
    graph_version_id, topic_id, observed_at desc
  );
create index if not exists actp_semantic_observations_signal_idx
  on public.actp_semantic_topic_observations(signal_id, observed_at desc);
create index if not exists actp_semantic_observations_binding_fk_idx
  on public.actp_semantic_topic_observations(
    binding_id, graph_version_id, signal_id, topic_id
  );
create index if not exists actp_semantic_observations_signal_graph_idx
  on public.actp_semantic_topic_observations(signal_id, graph_version_id);

create table if not exists public.actp_semantic_atomic_topic_selections (
  selection_id text primary key,
  status text not null check (status = 'approved'),
  graph_version_id text not null,
  graph_sha256 text not null check (length(graph_sha256) = 64),
  atomic_topic_id text not null,
  reviewer_type text not null check (reviewer_type in ('human', 'rules')),
  reviewer_id text not null,
  reviewed_at timestamptz not null,
  review_receipt_id text not null unique,
  review_receipt_sha256 text not null check (length(review_receipt_sha256) = 64),
  rationale text not null,
  selection_sha256 text not null unique check (length(selection_sha256) = 64),
  selection_json jsonb not null,
  created_at timestamptz not null,
  foreign key (graph_version_id)
    references public.actp_semantic_topic_graph_versions(graph_version_id),
  foreign key (graph_version_id, atomic_topic_id)
    references public.actp_semantic_topic_nodes(graph_version_id, topic_id)
);
create index if not exists actp_semantic_atomic_selections_topic_time_idx
  on public.actp_semantic_atomic_topic_selections(
    graph_version_id, atomic_topic_id, reviewed_at desc
  );

create table if not exists public.actp_semantic_atomic_selection_sources (
  selection_id text not null,
  binding_id text not null,
  topic_observation_key text not null,
  signal_id text not null,
  primary key (selection_id, binding_id, topic_observation_key),
  foreign key (selection_id)
    references public.actp_semantic_atomic_topic_selections(selection_id),
  foreign key (binding_id)
    references public.actp_semantic_signal_bindings(binding_id),
  foreign key (topic_observation_key)
    references public.actp_semantic_topic_observations(topic_observation_key),
  foreign key (signal_id)
    references public.actp_semantic_signal_candidates(signal_id)
);
create index if not exists actp_semantic_atomic_sources_binding_idx
  on public.actp_semantic_atomic_selection_sources(binding_id, selection_id);
create index if not exists actp_semantic_atomic_sources_observation_idx
  on public.actp_semantic_atomic_selection_sources(topic_observation_key);
create index if not exists actp_semantic_atomic_sources_signal_idx
  on public.actp_semantic_atomic_selection_sources(signal_id);

create table if not exists public.actp_semantic_content_evidence_receipts (
  receipt_id text primary key,
  selection_id text not null,
  evidence_type text not null check (evidence_type in (
    'transcript_receipt', 'audience_evidence', 'human_moment',
    'conversion_evidence', 'external_reference', 'software_change_receipt'
  )),
  status text not null check (status in ('ready', 'verified', 'accepted')),
  source_system text not null,
  source_record_id text not null,
  source_record_sha256 text not null check (length(source_record_sha256) = 64),
  observation_ids_json jsonb not null,
  claim text,
  source_uri text,
  receipt_sha256 text not null unique check (length(receipt_sha256) = 64),
  receipt_json jsonb not null,
  created_at timestamptz not null,
  foreign key (selection_id)
    references public.actp_semantic_atomic_topic_selections(selection_id)
);
create index if not exists actp_semantic_evidence_selection_idx
  on public.actp_semantic_content_evidence_receipts(
    selection_id, evidence_type, created_at
  );

create table if not exists public.actp_semantic_lineage_registrations (
  registration_id text primary key,
  status text not null check (status = 'ready'),
  registration_sha256 text not null unique check (length(registration_sha256) = 64),
  lineage_sha256 text not null check (length(lineage_sha256) = 64),
  canonical_plan_sha256 text not null check (length(canonical_plan_sha256) = 64),
  identifiers_json jsonb not null,
  registration_json jsonb not null,
  source_service text not null,
  source_receipt_id text not null,
  registered_at timestamptz not null
);
create index if not exists actp_semantic_registrations_lineage_idx
  on public.actp_semantic_lineage_registrations(
    lineage_sha256, registered_at desc
  );

create table if not exists public.actp_semantic_content_briefs (
  brief_id text primary key,
  registration_id text not null,
  graph_version_id text not null,
  atomic_topic_id text not null,
  brief_contract text not null,
  brief_sha256 text not null unique check (length(brief_sha256) = 64),
  status text not null,
  atomic_selection_id text not null,
  atomic_selection_sha256 text not null check (length(atomic_selection_sha256) = 64),
  source_binding_ids_json jsonb not null,
  lineage_sha256 text not null check (length(lineage_sha256) = 64),
  brief_json jsonb not null,
  source_service text not null,
  source_receipt_id text not null,
  registered_at timestamptz not null,
  foreign key (registration_id)
    references public.actp_semantic_lineage_registrations(registration_id),
  foreign key (graph_version_id)
    references public.actp_semantic_topic_graph_versions(graph_version_id),
  foreign key (graph_version_id, atomic_topic_id)
    references public.actp_semantic_topic_nodes(graph_version_id, topic_id),
  foreign key (atomic_selection_id)
    references public.actp_semantic_atomic_topic_selections(selection_id)
);
create index if not exists actp_semantic_briefs_topic_time_idx
  on public.actp_semantic_content_briefs(
    graph_version_id, atomic_topic_id, registered_at desc
  );
create index if not exists actp_semantic_briefs_selection_idx
  on public.actp_semantic_content_briefs(atomic_selection_id);
create index if not exists actp_semantic_briefs_registration_idx
  on public.actp_semantic_content_briefs(registration_id);

create table if not exists public.actp_semantic_content_assets (
  asset_id text primary key,
  brief_id text not null,
  graph_version_id text not null,
  atomic_topic_id text not null,
  parent_asset_id text,
  derivative_type text not null,
  platform text not null,
  account text,
  content_id text not null default '',
  asset_contract text not null,
  asset_sha256 text not null unique check (length(asset_sha256) = 64),
  status text not null,
  lineage_sha256 text not null check (length(lineage_sha256) = 64),
  asset_json jsonb not null,
  source_service text not null,
  source_receipt_id text not null,
  registered_at timestamptz not null,
  foreign key (brief_id) references public.actp_semantic_content_briefs(brief_id),
  foreign key (parent_asset_id)
    references public.actp_semantic_content_assets(asset_id),
  foreign key (graph_version_id, atomic_topic_id)
    references public.actp_semantic_topic_nodes(graph_version_id, topic_id)
);
create index if not exists actp_semantic_assets_brief_idx
  on public.actp_semantic_content_assets(brief_id, registered_at, asset_id);
create index if not exists actp_semantic_assets_content_idx
  on public.actp_semantic_content_assets(content_id, registered_at desc);
create index if not exists actp_semantic_assets_graph_atomic_idx
  on public.actp_semantic_content_assets(graph_version_id, atomic_topic_id);
create index if not exists actp_semantic_assets_parent_idx
  on public.actp_semantic_content_assets(parent_asset_id);

create table if not exists public.actp_semantic_content_lineage (
  lineage_link_id text primary key,
  lineage_sha256 text not null check (length(lineage_sha256) = 64),
  graph_version_id text not null,
  signal_id text not null,
  binding_id text not null,
  topic_id text not null,
  topic_observation_key text not null,
  brief_id text not null,
  atomic_topic_id text not null,
  asset_id text not null,
  content_id text not null default '',
  source_service text not null,
  source_receipt_id text not null,
  linked_at timestamptz not null,
  link_json jsonb not null,
  unique (binding_id, brief_id, asset_id),
  foreign key (signal_id, graph_version_id)
    references public.actp_semantic_signal_candidates(signal_id, graph_version_id),
  foreign key (binding_id)
    references public.actp_semantic_signal_bindings(binding_id),
  foreign key (topic_observation_key)
    references public.actp_semantic_topic_observations(topic_observation_key),
  foreign key (brief_id) references public.actp_semantic_content_briefs(brief_id),
  foreign key (asset_id) references public.actp_semantic_content_assets(asset_id),
  foreign key (graph_version_id, topic_id)
    references public.actp_semantic_topic_nodes(graph_version_id, topic_id),
  foreign key (graph_version_id, atomic_topic_id)
    references public.actp_semantic_topic_nodes(graph_version_id, topic_id)
);
create index if not exists actp_semantic_lineage_signal_idx
  on public.actp_semantic_content_lineage(signal_id, linked_at desc);
create index if not exists actp_semantic_lineage_topic_idx
  on public.actp_semantic_content_lineage(
    graph_version_id, topic_id, linked_at desc
  );
create index if not exists actp_semantic_lineage_brief_idx
  on public.actp_semantic_content_lineage(brief_id, linked_at desc);
create index if not exists actp_semantic_lineage_asset_idx
  on public.actp_semantic_content_lineage(asset_id, linked_at desc);
create index if not exists actp_semantic_lineage_content_idx
  on public.actp_semantic_content_lineage(content_id, linked_at desc);
create index if not exists actp_semantic_lineage_graph_atomic_idx
  on public.actp_semantic_content_lineage(
    graph_version_id, atomic_topic_id
  );
create index if not exists actp_semantic_lineage_signal_graph_idx
  on public.actp_semantic_content_lineage(signal_id, graph_version_id);
create index if not exists actp_semantic_lineage_observation_idx
  on public.actp_semantic_content_lineage(topic_observation_key);

-- Content-addressed semantic records are immutable even for privileged writes.
drop trigger if exists actp_semantic_graph_versions_no_update
  on public.actp_semantic_topic_graph_versions;
create trigger actp_semantic_graph_versions_no_update
before update or delete on public.actp_semantic_topic_graph_versions
for each row execute function public.actp_reject_market_tape_mutation();

drop trigger if exists actp_semantic_topic_nodes_no_update
  on public.actp_semantic_topic_nodes;
create trigger actp_semantic_topic_nodes_no_update
before update or delete on public.actp_semantic_topic_nodes
for each row execute function public.actp_reject_market_tape_mutation();

drop trigger if exists actp_semantic_topic_edges_no_update
  on public.actp_semantic_topic_edges;
create trigger actp_semantic_topic_edges_no_update
before update or delete on public.actp_semantic_topic_edges
for each row execute function public.actp_reject_market_tape_mutation();

drop trigger if exists actp_semantic_signal_candidates_no_update
  on public.actp_semantic_signal_candidates;
create trigger actp_semantic_signal_candidates_no_update
before update or delete on public.actp_semantic_signal_candidates
for each row execute function public.actp_reject_market_tape_mutation();

drop trigger if exists actp_semantic_signal_bindings_no_update
  on public.actp_semantic_signal_bindings;
create trigger actp_semantic_signal_bindings_no_update
before update or delete on public.actp_semantic_signal_bindings
for each row execute function public.actp_reject_market_tape_mutation();

drop trigger if exists actp_semantic_resolution_runs_no_update
  on public.actp_semantic_resolution_runs;
create trigger actp_semantic_resolution_runs_no_update
before update or delete on public.actp_semantic_resolution_runs
for each row execute function public.actp_reject_market_tape_mutation();

drop trigger if exists actp_semantic_topic_observations_no_update
  on public.actp_semantic_topic_observations;
create trigger actp_semantic_topic_observations_no_update
before update or delete on public.actp_semantic_topic_observations
for each row execute function public.actp_reject_market_tape_mutation();

drop trigger if exists actp_semantic_atomic_selections_no_update
  on public.actp_semantic_atomic_topic_selections;
create trigger actp_semantic_atomic_selections_no_update
before update or delete on public.actp_semantic_atomic_topic_selections
for each row execute function public.actp_reject_market_tape_mutation();

drop trigger if exists actp_semantic_atomic_sources_no_update
  on public.actp_semantic_atomic_selection_sources;
create trigger actp_semantic_atomic_sources_no_update
before update or delete on public.actp_semantic_atomic_selection_sources
for each row execute function public.actp_reject_market_tape_mutation();

drop trigger if exists actp_semantic_evidence_receipts_no_update
  on public.actp_semantic_content_evidence_receipts;
create trigger actp_semantic_evidence_receipts_no_update
before update or delete on public.actp_semantic_content_evidence_receipts
for each row execute function public.actp_reject_market_tape_mutation();

drop trigger if exists actp_semantic_content_briefs_no_update
  on public.actp_semantic_content_briefs;
create trigger actp_semantic_content_briefs_no_update
before update or delete on public.actp_semantic_content_briefs
for each row execute function public.actp_reject_market_tape_mutation();

drop trigger if exists actp_semantic_content_assets_no_update
  on public.actp_semantic_content_assets;
create trigger actp_semantic_content_assets_no_update
before update or delete on public.actp_semantic_content_assets
for each row execute function public.actp_reject_market_tape_mutation();

drop trigger if exists actp_semantic_content_lineage_no_update
  on public.actp_semantic_content_lineage;
create trigger actp_semantic_content_lineage_no_update
before update or delete on public.actp_semantic_content_lineage
for each row execute function public.actp_reject_market_tape_mutation();

drop trigger if exists actp_semantic_lineage_registrations_no_update
  on public.actp_semantic_lineage_registrations;
create trigger actp_semantic_lineage_registrations_no_update
before update or delete on public.actp_semantic_lineage_registrations
for each row execute function public.actp_reject_market_tape_mutation();

alter table public.actp_semantic_topic_graph_versions enable row level security;
alter table public.actp_semantic_topic_nodes enable row level security;
alter table public.actp_semantic_topic_edges enable row level security;
alter table public.actp_semantic_signal_candidates enable row level security;
alter table public.actp_semantic_signal_bindings enable row level security;
alter table public.actp_semantic_resolution_runs enable row level security;
alter table public.actp_semantic_topic_observations enable row level security;
alter table public.actp_semantic_atomic_topic_selections enable row level security;
alter table public.actp_semantic_atomic_selection_sources enable row level security;
alter table public.actp_semantic_content_evidence_receipts enable row level security;
alter table public.actp_semantic_lineage_registrations enable row level security;
alter table public.actp_semantic_content_briefs enable row level security;
alter table public.actp_semantic_content_assets enable row level security;
alter table public.actp_semantic_content_lineage enable row level security;

-- Match the existing Market Tape security boundary: there are deliberately no
-- anon/authenticated policies.  Supabase service_role bypasses RLS, while the
-- append-only triggers still reject privileged UPDATE and DELETE statements.

comment on table public.actp_semantic_signal_bindings is
  'Immutable reviewed dispositions. AI may only create review_required candidates.';
comment on table public.actp_semantic_content_lineage is
  'Immutable signal-to-topic-to-brief-to-asset/content join records.';
