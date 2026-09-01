# Upwork Market Demand

The Upwork demand integration is a bounded, direct RapidAPI data source for
measuring what buyers are requesting from freelancers. It is an external
market-demand signal inside Market Tape; it is not a browser automation path
and it is not evidence of social-platform engagement.

## Safety and cost controls

`GET /api/market-tape/upwork/health` is local and credit-free. Every provider
search costs one RapidAPI request unit and requires both gates:

- `MARKET_TAPE_ALLOW_METERED_READS=true` in runtime configuration.
- `execute_metered_reads=true` on that individual API or service call.

Before any provider request, the service appends a request reservation to the
local ledger. A reservation consumes the configured daily allowance even if
the process or provider call fails before a scan result can be stored. This
fail-closed rule prevents an interrupted process from spending untracked
credits or bypassing the daily cap.

The default daily limit is 10 requests and the default scan accepts at most
five queries. Configure these independently:

```text
MARKET_TAPE_UPWORK_DEFAULT_QUERIES=AI automation,AI agent,workflow automation,OpenAI
MARKET_TAPE_UPWORK_MAX_QUERIES_PER_SCAN=5
MARKET_TAPE_UPWORK_DAILY_REQUEST_LIMIT=10
MARKET_TAPE_UPWORK_PREDICTION_MIN_SNAPSHOTS=3
MARKET_TAPE_SUPABASE_SYNC_POST_BATCH_SIZE=50
```

Provider credentials and routing are supplied only through environment
variables:

```text
UPWORK_SCRAPER_RAPIDAPI_KEY=...
UPWORK_SCRAPER_HOST=upwork-jobs-scraper-api.p.rapidapi.com
UPWORK_SCRAPER_BASE_URL=https://upwork-jobs-scraper-api.p.rapidapi.com
```

`RAPIDAPI_KEY` is accepted as a fallback credential. Keys are never returned
by health, API, CLI, archive, or database records.

Production transport is HTTPS-only, does not follow redirects, and requires
the base-URL hostname to exactly equal the `.p.rapidapi.com` host header. HTTP
loopback is available only through an explicit constructor-only test transport;
it cannot be enabled by production environment variables.

## Typed API

Read operations are bounded and do not contact RapidAPI:

```text
GET /api/market-tape/upwork/health
GET /api/market-tape/upwork/jobs?query=AI%20automation&limit=100
GET /api/market-tape/upwork/demand?cohort_type=skill&limit=100
GET /api/market-tape/upwork/backtest?cohort_type=query&cohort_key=AI%20automation
GET /api/market-tape/upwork/script-context?selection_id=selection-id&limit=20
```

Mutations require the existing local control token:

```text
POST /api/market-tape/upwork/scans
POST /api/market-tape/upwork/signals/materialize
```

A metered scan body is explicit:

```json
{
  "queries": ["AI automation", "AI agent"],
  "sort": "recency",
  "max_jobs_per_query": 50,
  "execute_metered_reads": true
}
```

Acquisition time is captured by the service clock when the provider call is
made. API and CLI callers cannot supply or backdate `observed_at`.

The equivalent CLI keeps the same per-call gate:

```text
python -m services.market_tape.cli upwork-health
python -m services.market_tape.cli upwork-scan \
  --query "AI automation" \
  --query "AI agent" \
  --execute-metered-reads
python -m services.market_tape.cli upwork-demand --cohort-type skill
python -m services.market_tape.cli upwork-backtest --cohort-type query
python -m services.market_tape.cli upwork-script-context --selection-id selection-id
```

Omitting `--execute-metered-reads` never falls through to a live request.

## Evidence, forecasts, and script policy

Provider jobs, immutable job versions, query observations, job observations,
demand snapshots, forecasts, outcomes, and semantic links are stored as
separate append-only records. The normalized provider payload is also archived
content-addressably for audit. One canonical job identity can appear in
several query cohorts without duplicating the underlying job.

Forecasts use only snapshots observed at or before their prediction time. A
current partial scan always abstains. Older partial scans are excluded rather
than poisoning later predictions after enough complete evidence arrives. Only
a subsequent complete snapshot appends the prediction outcome used by the
backtest report. Demand velocity and direction measure newly arrived jobs per
elapsed hour, so turnover in a fixed-size top-N provider result can still show
rising demand.

Budget aggregates never mix fixed project totals with hourly rates. The typed
metrics are `fixed_budget_usd_coverage`, `median_fixed_budget_usd`,
`hourly_rate_usd_coverage`, and `median_hourly_rate_usd`; coverage always uses
unique jobs as its denominator. `$`, `US$`, and `USD` normalize to USD, while
unknown and non-USD currencies are excluded from these USD metrics.

Semantic materialization creates `external_signal` candidates. It does not
automatically approve a topic binding or authorize generation. Script context
is available only through an approved, selection-linked semantic observation
and includes aggregate buyer-demand metrics and lineage identifiers. Raw job
descriptions are never returned as script language and must never be copied or
lightly rewritten into a script.

## Supabase rollout

Apply migrations through Market Tape's validated migration manager, then run
the read-only V8 verification query. The transactional outbox mirrors parent
records in dependency order, beginning with request reservations and scan
runs. Supabase delivery posts bounded subsets and recursively halves an
idempotent subset after an ambiguous read timeout or timeout-like gateway
response. Only confirmed subsets are marked synchronized, and dependent entity
types wait if a parent type reaches a terminal leaf failure. Dependency
deferral follows an explicit foreign-key entity graph, so unrelated semantic
and legacy records continue syncing during an Upwork-specific failure. A
deferred child keeps its existing attempt count and is moved to at least the
latest unsynced parent retry time; it is not recorded as a failed HTTP attempt.
This parent preflight also applies when the parent was already in backoff before
the current batch. Use the normal bounded reconciliation and sync operations;
do not bypass the outbox with direct application writes.
