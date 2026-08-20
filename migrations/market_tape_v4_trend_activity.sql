-- Market Tape V4: separate measured counter movement from crawler expansion.

alter table public.actp_trend_observations
  add column if not exists views_new_1h bigint not null default 0,
  add column if not exists likes_new_1h bigint not null default 0,
  add column if not exists comments_new_1h bigint not null default 0,
  add column if not exists shares_new_1h bigint not null default 0,
  add column if not exists counter_delta_videos integer not null default 0,
  add column if not exists activity_coverage double precision not null default 0;

comment on column public.actp_trend_observations.views_new_1h is
  'Non-negative view-counter movement attributable to the prior hour; excludes lifetime views first discovered on old posts.';

comment on column public.actp_trend_observations.activity_coverage is
  'Fraction of trend members with a prior counter tick or publication inside the measured hour.';
