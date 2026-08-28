-- Read-only post-deployment verification for Market Tape V6.

with target_tables(table_name) as (
  values
    ('actp_market_creators'),
    ('actp_market_videos'),
    ('actp_market_discovery_attributions'),
    ('actp_market_query_attempts'),
    ('actp_market_observations'),
    ('actp_market_observation_quality_flags'),
    ('actp_content_genomes'),
    ('actp_trends'),
    ('actp_trend_memberships'),
    ('actp_trend_observations'),
    ('actp_market_collection_runs'),
    ('actp_market_source_receipts'),
    ('actp_market_source_health'),
    ('actp_market_predictions'),
    ('actp_semantic_topic_graph_versions'),
    ('actp_semantic_topic_nodes'),
    ('actp_semantic_topic_edges'),
    ('actp_semantic_signal_candidates'),
    ('actp_semantic_signal_bindings'),
    ('actp_semantic_resolution_runs'),
    ('actp_semantic_topic_observations'),
    ('actp_semantic_atomic_topic_selections'),
    ('actp_semantic_atomic_selection_sources'),
    ('actp_semantic_content_evidence_receipts'),
    ('actp_semantic_lineage_registrations'),
    ('actp_semantic_content_briefs'),
    ('actp_semantic_content_assets'),
    ('actp_semantic_content_lineage')
), relation_state as (
  select
    target.table_name,
    relation.oid is not null as relation_exists,
    coalesce(relation.relrowsecurity, false) as rls_enabled,
    relation.oid as relation_oid
  from target_tables target
  left join pg_catalog.pg_namespace namespace
    on namespace.nspname = 'public'
  left join pg_catalog.pg_class relation
    on relation.relnamespace = namespace.oid
    and relation.relname = target.table_name
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
), index_state as (
  select
    relation.table_name,
    coalesce(
      jsonb_agg(distinct index_relation.relname)
        filter (where index_relation.relname is not null),
      '[]'::jsonb
    ) as index_names
  from relation_state relation
  left join pg_catalog.pg_index index_catalog
    on index_catalog.indrelid = relation.relation_oid
  left join pg_catalog.pg_class index_relation
    on index_relation.oid = index_catalog.indexrelid
  group by relation.table_name
)
select
  relation.table_name,
  relation.relation_exists,
  relation.rls_enabled,
  policy.policy_count,
  trigger.trigger_names,
  index_state.index_names
from relation_state relation
join policy_state policy using (table_name)
join trigger_state trigger using (table_name)
join index_state using (table_name)
order by relation.table_name;
