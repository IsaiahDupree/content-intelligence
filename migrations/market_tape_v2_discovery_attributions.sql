-- Market Tape V2: preserve the exact query-to-video discovery lineage.

create table if not exists public.actp_market_discovery_attributions (
  attribution_key text primary key,
  run_id text not null,
  video_id text not null references public.actp_market_videos(video_id),
  source_id text not null,
  discovered_at timestamptz not null,
  surface text not null default '',
  query text not null,
  context_json jsonb not null default '{}'::jsonb
);

create index if not exists actp_market_discovery_attributions_query_idx
  on public.actp_market_discovery_attributions(query, discovered_at desc);

create index if not exists actp_market_discovery_attributions_video_idx
  on public.actp_market_discovery_attributions(video_id, discovered_at desc);

drop trigger if exists actp_market_discovery_attributions_no_update
  on public.actp_market_discovery_attributions;
create trigger actp_market_discovery_attributions_no_update
before update or delete on public.actp_market_discovery_attributions
for each row execute function public.actp_reject_market_tape_mutation();

alter table public.actp_market_discovery_attributions enable row level security;

comment on table public.actp_market_discovery_attributions is
  'Immutable query-to-video lineage used to learn the next autonomous trend frontier.';
