# Social Market Tape V1

Status: autonomous local production runtime installed
Service: `content-intelligence`
Loopback API: `http://127.0.0.1:6006`
Schema version: `3`
Daily unique target: `5,000` UTC-day items

## Purpose

Market Tape records the social market as immutable observations. It is not a list of links and it does not overwrite a video's counters. Every new read creates another timestamped tick, allowing the system to measure view velocity, acceleration, jerk, relative strength, adoption breadth, saturation, and decay.

The permanent asset is the historical trajectory:

```text
provider response
  -> immutable compressed raw object
  -> versioned canonical creator and content IDs
  -> append-only market observation
  -> adaptive next poll
  -> content genome
  -> trend membership
  -> append-only trend observation
  -> social candle and versioned prediction
  -> transactional central outbox
```

No AI assistant is required for configuration or execution. Environment configuration, source adapters, scheduler policy, storage, status, and receipts are deterministic software contracts.

## Proven Production State

The initial production tape contains:

| Record | Count before the current supervised cycle |
|---|---:|
| Canonical content items | 3,262 |
| Market observations | 3,262 |
| Canonical creators | 2,181 |
| Trend objects | 8,319 |
| Trend observations | 8,353 |
| Versioned predictions | 10,489 |
| Due trajectory polls | 3,230 |

The data came from real YouTube API reads, real controlled-browser research archives, and a small real TikTok licensed-provider probe. No synthetic content rows or provider mocks are in the production spool.

The configured target is 5,000 unique current items per UTC day. The system is installed to pursue that target, but no completed 5,000-unique production day has yet been certified. Ops Console reports acquired, target, and remaining by platform so a shortfall remains visible.

### Certified live state: 2026-08-19 02:43 UTC

The production spool and supervised API reported:

| Record | Live count |
|---|---:|
| Canonical content items | 5,660 |
| Market observations | 5,777 |
| Canonical creators | 4,174 |
| Trend objects | 15,437 |
| Trend observations | 16,717 |
| Versioned predictions | 21,368 |
| Due trajectory polls | 1,081 |

Today the tape acquired 2,280 unique items at a recorded provider cost of `$0.00`:

| Platform | Acquired today | Configured lane target | Current result |
|---|---:|---:|---|
| YouTube | 2,077 | 2,500 | Live; search quota stopped further discovery |
| TikTok | 20 | 1,000 | Live RapidAPI and archive lanes; Research API blocked |
| Instagram | 31 | 750 | Archive live; Graph credential invalid; Rapid endpoint degraded |
| X | 28 | 500 | Live controlled-browser lane; API credential remains blocked |
| Facebook | 0 | 125 | Connector live; current archive empty and Graph request degraded |
| Threads | 124 | 125 | Archive live; Graph request degraded |
| **Total** | **2,280** | **5,000** | **Thousands acquired; global target remains partial** |

Discovery run `mt-run-820747b0-2712-471d-94ff-6f2af60537c6` accepted 2,177 provider items and completed the full mapping, trend, prediction, receipt, and outbox loop. After deployment, unattended recheck run `mt-run-4ed4d78c-d5d0-4b7b-ba7b-2a86ca97a745` recovered from an initial API-start race, completed under launchd, and refreshed the heartbeat. Five-platform browser job `job_1787107045867_2psds9` then completed X, Threads, Instagram, Facebook, and TikTok in one 299.9-second coordinated run. It returned 24 X items and 34 Threads items while the other three signed-in pages returned measured zero-item results. Recheck `mt-run-aa241df0-26cf-439d-a3ac-d8f888d4403e` promoted those artifacts without provider spend. The retry, coordination, and promotion behavior is now part of the scheduler software.

## Source Registry

Every lane implements the same `MarketSource` contract and returns a `SourceReceipt`. A lane is never deleted because it is temporarily unavailable.

| Platform | Sources | Current operating interpretation |
|---|---|---|
| YouTube | Data API v3 search, charts, videos and batch statistics | Live and production-qualified |
| TikTok | Controlled browser archive, Research API v2, RapidAPI | Browser archive populated; API lanes remain re-testable based on credentials and approval |
| Instagram | Controlled browser archive, authorized Graph media, RapidAPI | Browser archive populated; current Graph/Rapid lanes report their real provider errors |
| X | Controlled browser archive, API v2 recent search | Browser collection is live and receipt-backed; API lane currently reports auth failure |
| Facebook | Controlled browser archive, authorized Graph videos | Registered; current collection returned no new browser items and Graph access is degraded |
| Threads | Controlled browser archive, authorized Graph threads | Browser archive populated; Graph lane remains re-testable |

Source states are typed:

- `ready`: last operation completed without a provider or normalization error.
- `running`: an operation is in flight.
- `degraded`: the lane responded but could not complete correctly.
- `blocked_credential`: required credentials or account IDs are absent or invalid.
- `blocked_approval`: a metered lane is not approved.
- `blocked_quota`: provider quota or access limits prevent reads.
- `disabled`: configuration excludes the lane.

Failure states accumulate a bounded cooldown. Calls during cooldown produce a zero-request `circuit_open` receipt. A credential or approval change bypasses stale cooldown state so every model and provider lane remains re-testable.

Controlled-browser archive reads are deliberately exempt from provider circuit blocking. A failed Safari trigger can cool down, but any completed artifact already on disk is still ingested on the next tick.

## Canonical Contracts

`MarketContent` carries provider-independent identity, timestamps, creator data, counters, text, media metadata, and source context. Canonical IDs have the form:

```text
youtube:video:<external-id>
youtube:creator:<external-id>
```

`SourceReceipt` records:

```text
run_id
source_id
platform
state
started_at / finished_at
request_count
discovered_count / refreshed_count
accepted_count / duplicate_count / failed_count
quota_remaining
estimated_cost_usd
error_code / redacted error_detail
cursor
operation metadata
```

Secrets are sanitized before entering error details, receipts, generated reports, or commits.

## Storage

The installed production runtime uses:

```text
~/Library/Application Support/ContentIntelligence/runtime
~/Library/Application Support/ContentIntelligence/data/market-tape.sqlite3
~/Library/Application Support/ContentIntelligence/data/market-tape-objects
~/Library/Application Support/ContentIntelligence/data/market-tape-heartbeat.json
~/Library/Application Support/SafariAutomation/market-research-data
```

Source remains in this Git repository. The installers deploy both collector and browser-research runtime snapshots outside `~/Documents` because macOS can suspend process file access to protected Documents paths while the screen is locked. Runtime secrets are allowlisted from existing private env files into mode-`600` environment files. Secret values are not printed. The five-platform browser service writes directly to the Application Support archive consumed by Market Tape.

The local SQLite spool is authoritative while disconnected. Database triggers reject updates and deletes to market and trend observations. Raw payloads are gzip-compressed and content-addressed by SHA-256.

The Supabase sink uses a transactional outbox. Central sync failure does not discard or stop local collection. The shared target project currently lacks the `actp_market_*` tables, so the outbox is intentionally degraded and pending until `migrations/market_tape_v1.sql` is applied to the authorized target project.

## Autonomous Scheduling

Two user LaunchAgents are installed:

```text
com.isaiah.content-intelligence.api
com.isaiah.content-intelligence.market-tape
```

The scheduler calls `POST /api/market-tape/tick` every 900 seconds. Policy remains in Python:

- Run `full` when no discovery exists or the last discovery is at least four hours old.
- Run `recheck` otherwise.
- Every recheck also promotes newly completed controlled-browser archive files.
- Reject concurrent write operations with HTTP `409`.
- Bound each scheduler HTTP call with a timeout.
- Save the latest scheduler response to `/tmp/content-intelligence-market-tape-last-tick.json`.
- Write heartbeat only after a complete collector receipt.
- Retry a failed local API/tick connection after 30 seconds; keep the configured 15-minute cadence after success.

Young videos poll most frequently. Positive acceleration or relative strength at least 2 sigma enables hot mode. Poll frequency decays with video age.

## Daily Scale And Cost Controls

Defaults are explicit in `MarketTapeConfig` and overridable by environment:

| Platform | Daily unique target | Daily request ceiling |
|---|---:|---:|
| YouTube | 2,500 | 180 |
| TikTok | 1,000 | 120 |
| Instagram | 750 | 100 |
| X | 500 | 60 |
| Facebook | 125 | 60 |
| Threads | 125 | 60 |
| Total | 5,000 | source-specific |

The total configured provider-cost ceiling is `$5.00` per UTC day. A source receives zero request budget after the cost ceiling is reached. Metered reads default to off in source configuration and require `MARKET_TAPE_ALLOW_METERED_READS=true`. Every request and estimated cost is written to the daily usage ledger and source receipt.

YouTube discovery uses quota-bounded round-robin pagination across configured topics and regions. It excludes canonical IDs already on tape before those IDs consume the requested item count, so later cycles advance through known pages to find new content. The adapter separately persists a conservative rolling 24-hour count for the `search.list` bucket, whose official default is 100 calls per day, and keeps the configured ceiling at 80. If YouTube terminates a run for quota, every valid partial result is still normalized and receipted before the source enters cooldown. See [YouTube's official quota calculator](https://developers.google.com/youtube/v3/determine_quota_cost) and [`search.list` reference](https://developers.google.com/youtube/v3/docs/search/list).

Healthy-provider overflow is explicit. All normal platform lanes run first; platforms listed in `MARKET_TAPE_OVERFLOW_PLATFORMS` may then exceed their lane target to close the remaining global target. Platform receipts and target progress remain separate, so overflow never disguises a blocked TikTok, Instagram, X, Facebook, or Threads integration as successful coverage.

## Commands

Initialize or inspect the source tree configuration:

```bash
python3 -m services.market_tape.cli init
python3 -m services.market_tape.cli doctor
python3 -m services.market_tape.cli status
```

Run explicit cycles:

```bash
python3 -m services.market_tape.cli cycle --mode discovery
python3 -m services.market_tape.cli cycle --mode recheck
python3 -m services.market_tape.cli cycle --mode full
```

Promote existing controlled-browser archives without provider writes:

```bash
python3 -m services.market_tape.cli bootstrap-local --limit-per-platform 10000
```

Inspect output:

```bash
python3 -m services.market_tape.cli videos --limit 100
python3 -m services.market_tape.cli videos --platform youtube --limit 100
python3 -m services.market_tape.cli trends --limit 100
python3 -m services.market_tape.cli predictions --subject-type trend --limit 100
python3 -m services.market_tape.cli candles --window-minutes 15 --limit 96
```

Retry central sync only after the target schema is authorized:

```bash
python3 -m services.market_tape.cli sync --force
```

Install or update the private runtime and LaunchAgents:

```bash
./scripts/install_market_tape_launchd.sh
```

Check supervision:

```bash
launchctl print gui/$(id -u)/com.isaiah.content-intelligence.api
launchctl print gui/$(id -u)/com.isaiah.content-intelligence.market-tape
curl -fsS http://127.0.0.1:6006/health
curl -fsS http://127.0.0.1:6006/api/market-tape/status
```

Logs and latest tick:

```text
/tmp/content-intelligence-api.log
/tmp/content-intelligence-api.error.log
/tmp/content-intelligence-market-tape.log
/tmp/content-intelligence-market-tape.error.log
/tmp/content-intelligence-market-tape-last-tick.json
```

## HTTP API

Read-only routes:

| Method | Route | Purpose |
|---|---|---|
| GET | `/health` | Service and Market Tape availability |
| GET | `/api/market-tape/status` | Daemon, daily progress, totals, sources and central sync |
| GET | `/api/market-tape/sources` | Latest state per source lane |
| GET | `/api/market-tape/videos` | Latest canonical content and observation state |
| GET | `/api/market-tape/trends` | Current trend objects and strengths |
| GET | `/api/market-tape/runs` | Collection receipts |
| GET | `/api/market-tape/predictions` | Versioned video and trend predictions |
| GET | `/api/market-tape/candles` | Delta-based social candles |

Control routes are loopback-only unless `MARKET_TAPE_CONTROL_TOKEN` is configured:

| Method | Route | Purpose |
|---|---|---|
| POST | `/api/market-tape/tick` | Select and run the next due autonomous cycle |
| POST | `/api/market-tape/cycles` | Run explicit full, discovery, or recheck mode |
| POST | `/api/market-tape/bootstrap-local` | Promote existing browser archives |
| POST | `/api/market-tape/sync` | Flush or force-retry the central outbox |

## Trend And Prediction Semantics

Each observation stores cumulative counters plus their derivatives. Relative strength is a z-score against recent observations in the same platform and video-age bucket.

Trend aggregation tracks video and creator totals, new adoption, counter deltas, median and p90 velocity, creator and platform breadth, top-1 and top-10 concentration, momentum, acceleration, relative strength, saturation, trend strength, index version, and lifecycle state.

V1 prediction models are transparent scored baselines. They store model version and input features for:

- Video exceeds 10x creator baseline within 24 hours.
- Trend reaches breakout within six hours.
- Expected peak time.
- Expected remaining useful life.

They are not yet outcome-calibrated learned temporal models. Outcome evaluation and model training remain next-stage work.

## Verification

```bash
python3 -m pytest tests/test_market_tape.py -q
python3 -m json.tool Sources/OpsConsole/Resources/content-intelligence-contract.json
```

The Market Tape integration suite uses real temporary SQLite databases and real loopback HTTP servers. It verifies append-only enforcement, raw archiving, derivative math, adaptive polling, YouTube pagination and known-ID skipping, healthy-provider overflow, persistent search quota accounting, partial-batch preservation, provider receipts, spend accounting, circuit breakers, local archive normalization, transactional outbox behavior, API authorization, and automatic tick selection. The repository currently passes 91 tests, including 15 Market Tape integration tests.

## Remaining Work

1. Apply `migrations/market_tape_v1.sql` to the authorized shared Supabase project and drain the durable outbox.
2. Certify one complete 5,000-unique UTC production day and tune per-platform targets from observed capacity.
3. Restore or obtain valid authorized TikTok, Instagram, X, Facebook, and Threads provider access where platform policy permits.
4. Add transcript, visual, audio, and embedding workers to populate the existing content-genome contract.
5. Add outcome labels, backtesting, calibration, and learned temporal predictors.
6. Add ClickHouse and vector storage when local observation volume justifies separation from the control-plane spool.
7. Feed qualified emerging and breakout trends into the experiment planner without authorizing generation or publishing automatically.

The runtime continues collecting and rechecking while these gaps remain visible.
