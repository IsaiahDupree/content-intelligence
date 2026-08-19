-- Read-only post-deployment verification for Market Tape V2.

with target_tables(table_name) as (
  values
    ('actp_market_creators'),
    ('actp_market_videos'),
    ('actp_market_discovery_attributions'),
    ('actp_market_observations'),
    ('actp_content_genomes'),
    ('actp_trends'),
    ('actp_trend_memberships'),
    ('actp_trend_observations'),
    ('actp_market_collection_runs'),
    ('actp_market_source_receipts'),
    ('actp_market_source_health'),
    ('actp_market_predictions')
), relation_state as (
  select
    target.table_name,
    relation.oid is not null as relation_exists,
    coalesce(relation.relrowsecurity, false) as rls_enabled,
    relation.oid as relation_oid
  from target_tables target
  left join pg_catalog.pg_class relation
    on relation.relname = target.table_name
  left join pg_catalog.pg_namespace namespace
    on namespace.oid = relation.relnamespace
    and namespace.nspname = 'public'
), policy_state as (
  select
    target.table_name,
    count(policy.policyname)::integer as policy_count
  from target_tables target
  left join pg_catalog.pg_policies policy
    on policy.schemaname = 'public'
    and policy.tablename = target.table_name
  group by target.table_name
), trigger_state as (
  select
    relation.table_name,
    coalesce(
      jsonb_agg(distinct trigger.tgname)
        filter (where trigger.tgname is not null and not trigger.tgisinternal),
      '[]'::jsonb
    ) as trigger_names
  from relation_state relation
  left join pg_catalog.pg_trigger trigger
    on trigger.tgrelid = relation.relation_oid
  group by relation.table_name
)
select
  relation.table_name,
  relation.relation_exists,
  relation.rls_enabled,
  policy.policy_count,
  trigger.trigger_names
from relation_state relation
join policy_state policy using (table_name)
join trigger_state trigger using (table_name)
order by relation.table_name;
