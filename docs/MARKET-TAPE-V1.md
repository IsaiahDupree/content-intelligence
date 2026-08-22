# Social Market Tape V4

Status: autonomous local production runtime installed
Service: `content-intelligence`
Loopback API: `http://127.0.0.1:6006`
Schema version: `7`
Daily unique target: `5,000` UTC-day items

## Purpose

Market Tape records the social market as immutable observations. It is not a list of links and it does not overwrite a video's counters. Every new read creates another timestamped tick, allowing the system to measure view velocity, acceleration, jerk, relative strength, adoption breadth, saturation, and decay.

The permanent asset is the historical trajectory:

```text
provider response
  -> immutable compressed raw object
  -> immutable query-attempt receipt, including zero-result searches
  -> versioned canonical creator and content IDs
  -> append-only market observation
  -> adaptive next poll
  -> content genome
  -> trend membership
  -> append-only trend observation
  -> social candle and versioned prediction
  -> transactional central outbox
  -> daily verified Passport snapshot and prediction backtest
```

No AI assistant is required for configuration or execution. Environment configuration, source adapters, scheduler policy, storage, status, and receipts are deterministic software contracts.

Discovery begins with provider charts that do not depend on configured keywords. The tape then mines titles and hashtags from recently published high-performing content and creates an adaptive query frontier. Ranking favors fresh view throughput, p75 and median implied views/hour, creator and platform breadth, engagement, repeated observations, and low single-video concentration. Each signal includes example content and its source URL, making query selection auditable without an LLM.

The checked-in signed-in search runner turns an external demand list or an adaptive frontier export into an auditable batch. Every query receives its own command, elapsed time, state, output path, SHA-256, row count, and error. Completed and partial outputs are eligible for immutable ingest; partial, failed, and timed-out states remain visible so they can be retried without discarding useful rows.

```bash
python3 scripts/research_youtube_queries.py \
  --query-file docs/adaptive-query-expansion-2026-08-19.txt \
  --output-dir '/Volumes/My Passport/MarketTape/trend-frontier/manual' \
  --limit 5 --candidate-multiplier 2 --max-age-days 3 --workers 3
```

## Performance-bound local transcript bank

The transcript bank reads Market Tape directly; it does not depend on the ACTP worker or ACD. Every accepted artifact contains:

- the canonical video ID and exact append-only `observation_key`;
- the observed views, likes, comments, shares, saves, and engagement rate;
- the local source-audio path and SHA-256;
- timestamped Whisper segments, language, model, word count, and transcript-payload SHA-256;
- an explicit audit decision and every failed check.

The default artifact floors are 10,000 views and 0.5% engagement for YouTube, 100,000 and 2% for TikTok, 50,000 and 1.5% for Instagram, and 25,000 and 1% for Facebook. A topic cohort also requires at least two topic terms in the spoken transcript, 40 transcript words, five English members, three creators, and 100,000 combined observed views. Language mismatches, misleading metadata, download errors, empty transcripts, insufficient evidence, and hash mismatches are recorded rather than silently accepted. TikTok handles are extracted only from scalar identity fields or a canonical TikTok URL; provider mappings are never stringified into a URL. Encoded object paths, wrong video IDs, and other malformed legacy TikTok URLs fail closed during candidate selection and immediately before download.

```bash
# Ingest signed-in YouTube search JSONL with file/query provenance.
python3 scripts/ingest_yt_dlp_search.py \
  --input 'creator burnout=/absolute/path/creator-burnout.jsonl'

# Curate and transcribe an exact related-content cohort.
python3 scripts/transcribe_performance_cohort.py \
  --topic 'creator burnout creative struggle content views work' \
  --platform youtube --video-id VIDEO_ID --model base \
  --cookies-from-browser chrome

# Resume the next performance-ranked backfill batch; existing artifacts are skipped.
python3 scripts/backfill_transcript_bank.py \
  --platform youtube --limit 20 --model base \
  --cookies-from-browser chrome \
  --topic 'creator burnout creative struggle content views work'

# Supersede a script gate with a cohort/hash-backed audit.
python3 scripts/audit_script_relatability.py \
  --script-id SCRIPT_ID --cohort-manifest /absolute/path/cohort.json
```

`PASS_PREDICTED_RELATABILITY` means the script is supported by the performance-qualified transcript cohort. It is not a claim about actual human response. Scores are capped at 85 until retention, engagement, and audience-response evidence exists for that exact published script.

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

### Latest certified live state: 2026-08-19 07:13 UTC

The production spool reported after four market-led query waves and relevance-gated browser archive promotion:

| Record | Live count |
|---|---:|
| Canonical content items | 7,410 |
| Market observations | 7,677 |
| Canonical creators | 5,691 |
| Trend objects | 20,585 |
| Trend observations | 20,779 |
| Versioned predictions | 26,622 |
| Due trajectory polls | 4,111 |

Today the tape has acquired 4,030 unique items at a recorded provider cost of `$0.00`:

| Platform | Acquired today | Configured lane target | Current result |
|---|---:|---:|---|
| YouTube | 3,270 | 2,500 | Target exceeded through charts plus signed-in demand expansion |
| TikTok | 24 | 1,000 | Relevance gate rejected most historical browser rows; API access remains limited |
| Instagram | 31 | 750 | URL discovery live; caption/metric evidence is insufficient for promotion |
| X | 225 | 500 | Controlled-browser evidence live at 56.2% whole-archive precision |
| Facebook | 0 | 125 | No usable current evidence |
| Threads | 480 | 125 | Target exceeded; whole-archive precision 72.7% |
| **Total** | **4,030** | **5,000** | **970-item shortfall remains explicit** |

Archive bootstrap `mt-run-5ca422be-36c3-4e91-9e42-68e4a14af819` examined 2,041 deduplicated browser candidates, wrote 562 observations, added 557 unique content items, and produced 1,515 trend observations plus 2,077 predictions. Every browser row was evaluated by `niche-token-overlap-v1`; stale and off-topic rows were rejected rather than counted. The resulting 11,922 outbox records were synchronized in three bounded batches with zero failures. A full parity audit then found 811 historical local entities that predated outbox enrollment; `sync --reconcile` queued and synchronized only those missing entities in one batch, leaving zero pending records.

### Certified central mirror: 2026-08-19 04:10 UTC

Migration `market_tape_v1` is live on Supabase project `ivhfuhxorppptyuofbgq`.
The read-only catalog verifier certified all 11 relations, RLS on every table,
zero anon/authenticated policies, and both append-only observation triggers.
The developer and launchd-supervised outboxes were drained to zero after a
5,000-row smoke batch. The central mirror then reported:

| Supabase table | Rows |
|---|---:|
| `actp_market_creators` | 4,174 |
| `actp_market_videos` | 5,660 |
| `actp_market_observations` | 5,778 |
| `actp_content_genomes` | 5,660 |
| `actp_trends` | 15,437 |
| `actp_trend_memberships` | 26,010 |
| `actp_trend_observations` | 16,720 |
| `actp_market_collection_runs` | 28 |
| `actp_market_source_receipts` | 183 |
| `actp_market_source_health` | 13 |
| `actp_market_predictions` | 21,372 |
| **Total** | **101,035** |

After the drain and final runtime deployment, supervised recheck
`mt-run-ea49d1f8-23a3-4445-b62e-2af900e02424` completed through the loopback
API, synchronized 14 changed entities with zero failures, and left the central
outbox at zero. Its heartbeat records the deployed API process and confirms the
15-minute launchd loop is operating on the production spool.

### Latest central mirror: 2026-08-19 07:13 UTC

The V2 verifier certified all 12 relations, RLS on every table, zero unexpected anonymous/authenticated policies, and append-only triggers on observations, trend observations, and discovery attributions. Authoritative Management API counts after the final drain were:

| Supabase table | Rows |
|---|---:|
| `actp_market_creators` | 5,691 |
| `actp_market_videos` | 7,410 |
| `actp_market_observations` | 7,677 |
| `actp_content_genomes` | 7,410 |
| `actp_trends` | 20,585 |
| `actp_trend_memberships` | 34,160 |
| `actp_trend_observations` | 20,779 |
| `actp_market_collection_runs` | 49 |
| `actp_market_source_receipts` | 285 |
| `actp_market_source_health` | 14 |
| `actp_market_predictions` | 26,622 |
| `actp_market_discovery_attributions` | 3,308 |
| **Total** | **133,990** |

Launchd retry run `mt-run-888fabdb-514e-49d7-a61a-aedb59dff50f` completed a real unattended recheck after the API startup race, synchronized 751 changed entities with zero failures, refreshed the heartbeat under the deployed API PID, and left local/central parity intact.

### Supervised V4 state: 2026-08-20 06:40 UTC

This is an intraday operating receipt, not a claim that the current UTC day is
complete or fully certified:

| Record | Live count |
|---|---:|
| Canonical content items | 11,391 |
| Market observations | 12,228 |
| Canonical creators | 8,860 |
| Trend objects | 32,575 |
| Trend observations | 101,098 |
| Versioned predictions | 132,116 |
| Immutable query attempts | 425 |
| Due trajectory polls | 4,627 |

The day currently contains 3,939 newly acquired YouTube items at `$0.00`
configured direct provider cost, or 78.78% of the 5,000-item global target.
TikTok, Instagram, X, Facebook, and Threads remain at zero newly acquired items
for this UTC day; historical archive evidence from those platforms remains on
tape, but it is not relabeled as today's collection. Controlled-browser job
`job_1787204984974_j3ttqd` is actively researching 30 market-led topics across
five platform lanes. Its artifacts are promoted incrementally by scheduled
rechecks, so the collector does not need to wait for the complete cross-platform
job.

At 07:00 UTC the external research service restarted after a Safari AppleEvent
timeout and lost that in-memory job registry. Ten completed TikTok/Instagram
intermediate artifacts remained durable. Market Tape now persists the first
`provider_job_not_found` detection, enforces a one-hour cooldown from that
detection, then retries the recorded lanes rather than waiting the full 24-hour
refresh interval or immediately duplicating browser work.

Fair recheck run `mt-run-6087441b-84b7-4d34-8b72-a51426cbc348` advanced 1,000
cross-platform queue positions, used five actual YouTube API requests, and
created 201 true second observations plus 851 V2 trend ticks. Repeated scheduler
passes have since reduced the due queue without manufacturing observations from
unchanged archive snapshots. Provider-free reindex runs
`mt-run-c631b006-0f8f-4a89-aa06-0d8b22b48e87` and
`mt-run-60605d1c-40db-435b-b75b-97c2974501c2` rebuilt the full 32,575-trend
activity layer under `trend-strength-v2`.

The highest actionable V5 signals with at least three measured videos are:

| Rank | Trend | New views in measured hour | Measured videos | Evidence |
|---:|---|---:|---:|---|
| 1 | Nobody Wants / US home-buying demand | 8,319 | 3 | medium |
| 2 | Yankees vs. Orioles | 8,161 | 3 | medium |
| 14 | Indiana Fever | 13,349 | 3 | medium, recurring |

The V4 Supabase audit verified all 13 RLS-protected relations and append-only
triggers. A 23-batch drain synchronized 110,074 queued records with zero
failures, leaving central pending at zero and 485,472 total rows across the
Market Tape relations. The existing promoted progression model remains retained
and re-testable, but it is marked incompatible with V2 index inputs. A fresh V2
early-entry candidate correctly remains `collecting_labels` with zero matured
six-hour labels rather than receiving an unsupported promotion.

At 07:15 UTC an explicit Passport recertification returned `blocked_storage`
after the bounded 30-second write probe. It did not hold the scheduler open.
The latest successful package remains the partial 2026-08-19 receipt at
`/Volumes/My Passport/MarketTape/datasets/2026-08-19/mt-dataset-2026-08-19-20260820T053505Z-989997e5/certification.json`;
the next scheduled certification can replace it once the external filesystem is
responsive. The following unattended recheck used zero provider calls, imported
nine additional Instagram query-attempt receipts, reduced due polls to 1,539,
and left central sync at zero pending records.

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

`request_count` is the number of provider operations represented by that
specific receipt, not the source adapter's cumulative process count. Sequential
discovery and refresh receipts therefore sum exactly to the provider calls that
were made. Quota remaining stays cumulative and is reported separately.

Secrets are sanitized before entering error details, receipts, generated reports, or commits.

## Storage

The installed production runtime uses:

```text
~/Library/Application Support/ContentIntelligence/runtime
~/Library/Application Support/ContentIntelligence/data/market-tape.sqlite3
~/Library/Application Support/ContentIntelligence/data/market-tape-objects
~/Library/Application Support/ContentIntelligence/data/market-tape-heartbeat.json
~/Library/Application Support/SafariAutomation/market-research-data
/Volumes/My Passport/MarketTape/datasets/YYYY-MM-DD/<certification-id>
/Volumes/My Passport/MarketTape/raw-objects
```

Source remains in this Git repository. The installers deploy both collector and browser-research runtime snapshots outside `~/Documents` because macOS can suspend process file access to protected Documents paths while the screen is locked. Runtime secrets are allowlisted from existing private env files into mode-`600` environment files. Secret values are not printed. The five-platform browser service writes directly to the Application Support archive consumed by Market Tape.

The local SQLite spool is authoritative while disconnected. Database triggers reject updates and deletes to market and trend observations. Raw payloads are gzip-compressed and content-addressed by SHA-256.

The live spool stays on internal storage so a disconnected external drive cannot stop collection. The daily certifier pins one WAL read view after its short prediction-preparation section, then releases the collection lock before the online backup, raw mirroring, JSONL export, quality analysis, hashing, and gzip work. The recovery SQLite file, every canonical `mt_*` JSONL table, the raw-object registry, schema receipt, and quality report therefore share one auditable cutoff while new collection continues in WAL. Model bytes are captured immediately before that cutoff. A crash-safe local `flock` permits one certification at a time; a duplicate request returns HTTP 409 without overwriting the owner's progress receipt.

The Passport destination is disposable staging until atomic promotion. Its backup uses `journal_mode=OFF`, `synchronous=OFF`, and `temp_store=MEMORY` to avoid external-drive pager churn, then is reopened read-only and must pass `PRAGMA quick_check`; a failure removes staging and cannot replace the last successful pointer. Local raw objects are hash-verified every run. New Passport copies are written atomically and hash-verified after copying; unchanged content-addressed copies are discovered in one directory inventory and reuse their successful copy-time provenance receipt instead of reopening thousands of external-drive files. The manifest records `market_tape_dataset_snapshot_consistency_v1`, the cutoff, staging PRAGMAs, and snapshot-derived artifact sources. A Passport write probe is bounded to 30 seconds; timeout returns `blocked_storage` and retains the last successful certification receipt separately. `certification.json` reports storage integrity, collection SLA, query/platform coverage, trajectory coverage, and prediction validation. A package is `partial`, never `certified`, when any gate is missing.

The Supabase sink uses a transactional outbox. Central sync failure does not discard or stop local collection. The shared target schema is live and verified. Sink batches use a fixed dependency order so creators precede videos, trends precede memberships and trend observations, and collection runs precede source receipts even when a batch begins in the middle of a run's outbox records.

## Autonomous Scheduling

Three user LaunchAgents are installed:

```text
com.isaiah.content-intelligence.api
com.isaiah.content-intelligence.market-tape
com.isaiah.content-intelligence.market-tape-dataset
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
- At 01:10 local time, snapshot and certify the prior UTC day to the Passport. `RunAtLoad` also closes an uncaptured prior day after deployment or reboot.
- Reuse a valid same-day Passport receipt written within the prior six hours when launchd is reinstalled; permit explicit force and later recertification so late-arriving evidence can still close a partial day.
- Publish `checking_storage`, prediction, raw-mirror, snapshot, table-export, and manifest phases through dataset status; raw-mirror status includes processed, total, verified, missing, and corrupt counts.
- Refuse a browser-research dispatch when the temporary volume has less than 5 GiB free, while continuing to ingest already archived evidence.
- Inspect the previous browser job before honoring the 24-hour refresh cooldown. Running jobs are not duplicated, completed jobs remain cooled down, failed jobs retry after one hour using only provider lanes whose receipts failed, and provider restarts that forget an in-memory job receive a persisted one-hour missing-job cooldown before recorded lanes retry.
- Select due trajectory polls with cross-platform round-robin fairness so a large stale archive cannot starve fresh YouTube or another provider lane.
- Defer a provider row that returns an unchanged cumulative snapshot for one hour with `unchanged_source_snapshot`; do not replay the same non-tick on every scheduler cycle.

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

Measured query feedback has a separate deterministic admission budget. The defaults admit at most 20 evidence-derived query families per UTC day, admit the same family at most once per UTC day, and cool a selected or actually attempted family for 24 hours. Before any feedback query reaches a provider, reservation acquires SQLite `BEGIN IMMEDIATE`, rechecks committed `mt_adaptive_query_admissions` and `mt_query_attempts` rows at or after the explicit rolling boundary, and only then performs the UTC-day count-and-insert. Attempt families come from `metadata.query_family` when present and otherwise from the exact attempted query. This closes the planner-preflight race and makes cooldown, daily, and family decisions atomic across independent collectors, including across a UTC-day boundary. Provider fanout cannot multiply the selection count: `market-tape-adaptive-query-planner` writes a separate zero-request receipt that mirrors durable admission keys and cooldown rejection evidence into the normal outbox. The local admission table is also included in certified SQLite and JSONL datasets. Provider request ceilings and the global cost ceiling still apply independently.

| Control | Default | Environment override |
|---|---:|---|
| Evidence-derived families per UTC day | 20 | `MARKET_TAPE_ADAPTIVE_TOPIC_DAILY_FEEDBACK_LIMIT` |
| Admissions per query family per UTC day | 1 | `MARKET_TAPE_ADAPTIVE_TOPIC_FAMILY_DAILY_LIMIT` |
| Query-family cooldown | 24 hours | `MARKET_TAPE_ADAPTIVE_TOPIC_COOLDOWN_HOURS` |
| Direct-current exact-query reserve | 25% | `MARKET_TAPE_ADAPTIVE_TOPIC_DIRECT_QUERY_FRACTION` |
| Configured broad-market reserve | 20% | `MARKET_TAPE_ADAPTIVE_TOPIC_EXPLORATION_FRACTION` |

YouTube discovery first enumerates `mostPopular` across configured regions and categories. This chart lane is independent of seed keywords and remains available when keyword-search quota is exhausted. The adapter then uses quota-bounded round-robin pagination across the adaptive frontier. It excludes canonical IDs already on tape before those IDs consume the requested item count, so later cycles advance through known pages to find new content. The adapter separately persists a conservative rolling 24-hour count for the `search.list` bucket, whose official default is 100 calls per day, and keeps the configured ceiling at 80. If search quota terminates a run, every valid chart and partial search result is still normalized and receipted; only the search lane is marked blocked, so chart collection and statistics refreshes continue. See [YouTube's official quota calculator](https://developers.google.com/youtube/v3/determine_quota_cost) and [`search.list` reference](https://developers.google.com/youtube/v3/docs/search/list).

### Adaptive Keyword Frontier

The frontier is recomputed from immutable observations before each discovery cycle. It does not start from the business niche list.

```text
broad category charts
  -> fresh content cohort
  -> exact discovery-query attribution + title and hashtag candidates
  -> views/hour and freshness scoring
  -> breadth and concentration checks
  -> duplicate-topic suppression
  -> adaptive provider queries
  -> new observations
  -> next frontier iteration
```

Defaults use a seven-day current-clock window, require at least two independent videos and two independent creators for automatic querying, select at most 30 terms, and always preserve configured broad-market capacity. Exact current discovery queries have a reserved lane before remaining capacity is offered to title, phrase, and hashtag terms; unused lane capacity may be filled without displacing the configured baseline. Query priority combines performance with evidence confidence and a specificity bonus, so an exact measured event query can outrank a generic word. Generic navigation terms such as `live`, `news`, `trailer`, `breakdown`, and `highlights` remain visible for analysis but cannot consume autonomous query slots. Canonical spelling overlap and shared top-video evidence suppress variants from consuming multiple frontier slots. Single-video and single-creator terms remain visible with lower confidence but are never query-ready.

The adaptive selection contract is `market_tape_adaptive_query_feedback_v1`. Its planner receipt includes the UTC selection time and freshness cutoff; selected baseline, direct-current, and derived lanes; atomic daily and family admission state; evidence video IDs and source URLs; and a SHA-256 over the complete decision. Every excluded candidate and reason is persisted and covered by that hash; `excluded_candidates_preview` is only a bounded display field and never substitutes for the complete decision. The same final hash and signal lineage are attached to each resulting query attempt. A selection and an attempt remain different facts: blocked providers cannot fabricate an attempt, and multiple providers cannot make one query family appear to have received multiple planner admissions.

Full collection cycles use three explicit phases against the shared provider budget: terminal-forecast-only rechecks, discovery, then ordinary scheduled rechecks. The terminal planner evaluates exact-video coverage for valid active-model forecasts but never selects ordinary due work. The scheduled planner runs after discovery, does not load or claim forecast coverage, and selects only ordinary due polls. Both planner receipts and every provider recheck receipt identify their phase and lane. Standalone `recheck` mode retains the combined terminal-first planner. The existing unscorable and fail-closed semantics remain unchanged when exact coverage cannot be obtained.

Every provider or signed-in search can attach `query`, `queries`, `topic`, or `niche` discovery context. Market Tape writes one semantic immutable `mt_discovery_attributions` record per query/video/source, mirrors it to `actp_market_discovery_attributions`, and computes an exact-query frontier independently from title fragments. Rereading an unchanged browser archive cannot multiply that evidence. `mt_query_attempts` separately records every source/query execution, including empty, partial, failed, and timed-out searches, with artifact path and SHA-256 where available. Exact provider queries remain auditable, while `metadata.query_family` maps provider-specific expansions back to the configured cross-platform topic used by the daily coverage gate. This closes the loop without hiding zero-result research or making one provider's expansion syntax a false coverage failure.

Browser expansion uses the research service's explicit `trend` query mode. That mode searches the measured term, exact phrase, and platform-native hashtag or current-event variants; it does not append business-niche suffixes such as `tips`, `strategy`, or `community`. On archive ingest, `niche-token-overlap-v1` independently rejects carryover from a prior browser query. Each source receipt reports evaluated, accepted, rejected, unscoped, and precision counts under `metadata.archive_qc`, so raw browser output cannot silently become validated trend evidence.

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
python3 -m services.market_tape.cli keywords --limit 100 --window-hours 168 --min-videos 2
python3 -m services.market_tape.cli query-frontier --limit 100 --window-hours 168 --min-videos 2
python3 -m services.market_tape.cli query-attempts --limit 5000
python3 -m services.market_tape.cli backfill-query-attempts
python3 -m services.market_tape.cli reindex-trends --forecast-limit 50000
python3 -m services.market_tape.cli predictions --subject-type trend --limit 100
python3 -m services.market_tape.cli evaluate-predictions
python3 -m services.market_tape.cli train-predictor
python3 -m services.market_tape.cli predictor-status
python3 -m services.market_tape.cli forecast-trends --limit 5000
python3 -m services.market_tape.cli opportunities --limit 100 --max-saturation 0.75 --min-videos 2 --min-measured-videos 2
python3 -m services.market_tape.cli prediction-backtest
python3 -m services.market_tape.cli certify-dataset --date 2026-08-19
python3 -m services.market_tape.cli dataset-status
python3 -m services.market_tape.cli candles --window-minutes 15 --limit 96
```

Equivalent read API:

```text
GET /api/market-tape/keywords?limit=100&window_hours=168&min_videos=2
GET /api/market-tape/query-frontier?limit=100&window_hours=168&min_videos=2
```

Retry or drain central sync after the target schema is authorized:

```bash
python3 -m services.market_tape.cli sync --reconcile --force --drain --max-batches 250
```

`--reconcile` queues only canonical local entities that never entered the durable outbox. This repairs historical pre-outbox gaps without replaying already-synchronized rows.

Validate, inspect, or apply the shared Supabase schema:

```bash
python3 scripts/market_tape_migration.py validate
python3 scripts/market_tape_migration.py status
python3 scripts/market_tape_migration.py apply --project-ref ivhfuhxorppptyuofbgq
python3 scripts/market_tape_migration.py verify --project-ref ivhfuhxorppptyuofbgq
python3 scripts/market_tape_migration.py counts --project-ref ivhfuhxorppptyuofbgq
```

`apply` uses the official Supabase Management API and requires
`SUPABASE_ACCESS_TOKEN` with `database_write` permission. It refuses to run if
the explicit project ref differs from the project encoded in `SUPABASE_URL`.
The migration manager applies the idempotent V1 base schema followed by
`migrations/market_tape_v2_discovery_attributions.sql` and
`migrations/market_tape_v3_query_attempts.sql`, followed by
`migrations/market_tape_v4_trend_activity.sql`; print the exact
combined checked-in SQL for an authorized SQL Editor session with:

```bash
python3 scripts/market_tape_migration.py sql
```

`status` probes all 13 PostgREST table contracts with the service-role
credential. `verify` uses a read-only Management API query to certify relation,
RLS, policy, and append-only trigger state. `counts` uses the same read-only
path to return an authoritative row-count receipt. None print credentials. The
drain command processes bounded configured batches, stops when the queue is
empty or cannot make progress, and returns aggregate batch, success, failure,
and pending counts.

To operate on the installed production spool from the source checkout, point
the CLI at the private runtime environment explicitly:

```bash
MARKET_TAPE_ENV_FILES="$HOME/Library/Application Support/ContentIntelligence/runtime/.env.market-tape" \
  python3 -m services.market_tape.cli status
MARKET_TAPE_ENV_FILES="$HOME/Library/Application Support/ContentIntelligence/runtime/.env.market-tape" \
  python3 -m services.market_tape.cli sync --force --drain --max-batches 250
```

Without `MARKET_TAPE_ENV_FILES`, source-tree commands can target the developer
spool at `data/market-tape.sqlite3`; always inspect `database_path` before a
production operation.

Database owners can additionally run the checked-in read-only catalog audit at
`migrations/verify_market_tape_v2.sql`. It reports relation existence, RLS,
policy count, and trigger names for every Market Tape table. The expected state
is 13 existing relations with RLS enabled, zero anon/authenticated policies,
and append-only triggers on observations, trend observations, discovery
attributions, and query attempts.

Install or update the private runtime and LaunchAgents:

```bash
./scripts/install_market_tape_launchd.sh
```

Check supervision:

```bash
launchctl print gui/$(id -u)/com.isaiah.content-intelligence.api
launchctl print gui/$(id -u)/com.isaiah.content-intelligence.market-tape
launchctl print gui/$(id -u)/com.isaiah.content-intelligence.market-tape-dataset
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
/tmp/content-intelligence-market-tape-dataset.json
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
| GET | `/api/market-tape/keywords` | Evidence-ranked title, hashtag, phrase, and exact-query terms |
| GET | `/api/market-tape/query-frontier` | Exact discovery queries ranked independently from text fragments |
| GET | `/api/market-tape/query-attempts` | Query/platform execution ledger, including zero-result attempts |
| GET | `/api/market-tape/runs` | Collection receipts |
| GET | `/api/market-tape/predictions` | Versioned video and trend predictions |
| GET | `/api/market-tape/prediction-backtest` | Brier, calibration, AUC, and model-readiness receipts |
| GET | `/api/market-tape/predictions/model` | Active predictor and immutable candidate/rejection registry |
| GET | `/api/market-tape/opportunities` | Specific, evidenced, unsaturated opportunities with transparent score components |
| GET | `/api/market-tape/datasets/status` | Latest Passport certification state and manifest path |
| GET | `/api/market-tape/candles` | Delta-based social candles |

Control routes are loopback-only unless `MARKET_TAPE_CONTROL_TOKEN` is configured:

| Method | Route | Purpose |
|---|---|---|
| POST | `/api/market-tape/tick` | Select and run the next due autonomous cycle |
| POST | `/api/market-tape/cycles` | Run explicit full, discovery, or recheck mode |
| POST | `/api/market-tape/bootstrap-local` | Promote existing browser archives |
| POST | `/api/market-tape/query-attempts/backfill` | Verify and import historical Safari and signed-in YouTube keyword receipts without provider calls |
| POST | `/api/market-tape/trends/reindex` | Recompute V2 trend activity and deterministic forecasts from existing observations without provider calls |
| POST | `/api/market-tape/sync` | Flush or force-retry the central outbox |
| POST | `/api/market-tape/predictions/evaluate` | Label forecasts whose horizons have measurable follow-up |
| POST | `/api/market-tape/predictions/train` | Train, grouped-cross-validate, and promote or reject a versioned trend predictor |
| POST | `/api/market-tape/predictions/forecast` | Apply the promoted model to current non-dead trend states and queue receipts for central sync |
| POST | `/api/market-tape/datasets/certify` | Build and verify a daily Passport dataset |

## Trend And Prediction Semantics

Every daily certification evaluates due forecast horizons, trains a dependency-free regularized logistic candidate from scored trend labels, and applies the promoted model to current eligible trend states. New `market_tape_trend_predictor_v2` candidates use expanding, chronological `predicted_at` walk-forward validation. Each validation window purges its `trend_id` groups from training and admits a training label only after its forecast horizon has closed. Promotion requires at least three measured walk-forward folds, zero group or time overlap, at least 80% in-distribution validation coverage, the configured label minimums, positive Brier skill over fold-specific prevalence, and ROC-AUC of at least `0.65`. The loader remains backward-compatible with the hashed v1 active artifact, but only a v2 artifact carrying a complete passing promotion receipt can be newly activated.

V2 inference uses the artifact's training means, scales, and per-feature support profile. Missing, non-finite, contract-invalid, or out-of-distribution snapshots produce an explicit `market_tape_trend_inference_v2` abstention receipt and no prediction row. Accepted standardized values are bounded before scoring, and reported probabilities are constrained to `[0.005, 0.995]`, preventing rounded `0.0` or `1.0` certainty.

A trend forecast also requires at least two videos and two creators, plus a source trend observation no more than 30 minutes old. The source observation ID and timestamp are persisted as lineage and cannot be forecast twice for the same model, horizon, and subject. Once a horizon plus the two-hour coverage grace closes without a sufficiently late observation, the forecast becomes `unscorable: missing_future_trend_coverage`; it never becomes a guessed negative label. Opportunity ranking assigns zero probability weight until the exact active model has enough prospective labels, positive and negative classes, independent subjects, time batches, positive Brier skill, acceptable calibration error, and an unexpired prediction.

`GET /api/market-tape/intelligence` and `market-tape intelligence` provide one read-only snapshot containing current-clock-decayed keyword/query evidence, observed trend opportunities, exact probability-admission receipts, backtests, live database totals, and Passport certification lineage. Keyword and opportunity scores are explicitly ranks, not probabilities. The endpoint uses the existing subject/time indexes to seek the latest observation and exact-model prediction for each trend; it does not materialize and cross-join whole prediction/observation windows. Coarse evidence gates are applied to the actual latest row, one bounded keyword source rowset feeds both derived-term and exact-query rankings, and the response exposes elapsed component timings, candidate rows loaded, whether the candidate scan was truncated, and its deterministic model-neutral preselection order.

Prediction contracts are deliberately distinct:

- `is_or_reaches_breakout_within_6h` describes the deployed V2 market-state progression model. It may rank a trend that is already hot. Its artifact was trained on `trend-strength-v1`, so it is explicitly incompatible with current `trend-strength-v2` observations and receives zero weight until retrained on the new index.
- `enters_breakout_within_6h` describes the V3 early-entry candidate. Training and inference exclude trends already in `breakout`, `expanding`, or `saturating`, and trends already at strength 70 or above.
- `actionable-opportunity-v6` is a deterministic derived rank, not a probability. It combines measured one-hour activity volume, activity-coverage-adjusted momentum and acceleration, relative strength, breadth, unsaturation, and evidence reliability. An incompatible model receives exactly zero rank weight. The feed requires at least two independently ticking videos by default and excludes format aggregates, generic distribution tags, generic hook phrases, incoherent single-token hashtags, incomplete phrases, declining/dead/saturating states, insufficient evidence, and near-duplicates measured by both label and shared-video overlap. Context coherence is measured from member titles/captions after label, URL, handle, numeric-ID, platform-boilerplate, and generic-token removal and is exposed in every candidate receipt. Opaque single-token labels also receive a shared-term `resolved_display_name` while retaining the canonical label. Portfolio caps stop one trend type or lifecycle class from crowding out the rest. Each result carries `high`, `medium`, or `provisional` evidence grade plus every weight, score component, suppression count, platform distribution, and representative source-content receipt.

Each observation stores cumulative counters plus their derivatives. Relative strength is a z-score against recent observations in the same platform and video-age bucket.

Recheck receipts distinguish source-item availability from canonical corruption. A
tracked ID absent from a bounded fallback archive increments `missing_tracked_count`
and the poll's `provider_item_missing` status without opening the source circuit.
Only an exception while committing a returned typed record can emit
`normalization_failed`; receipts expose exception-class counts without content or
secret-bearing error values.

`trend-strength-v2` derives recent activity only from measured per-video counter
deltas. A lifetime total first observed on an old post contributes zero recent
views; a first observation contributes only when the post was actually published
inside the measured window. Long-gap deltas are scaled to their one-hour overlap.
Every trend observation stores `views_new_1h`, `likes_new_1h`,
`comments_new_1h`, `shares_new_1h`, `counter_delta_videos`, and
`activity_coverage`, making sparse evidence visible instead of projecting one
tick across a broad trend. Creator adoption likewise uses provider publication
time rather than crawler first-seen time.

Trend aggregation also tracks video and creator totals, new adoption, median and p90 velocity, creator and platform breadth, top-1 and top-10 concentration, momentum, acceleration, relative strength, saturation, trend strength, index version, and lifecycle state. Reindexing is deterministic and provider-free; it rebuilds this layer from the immutable tape while preserving prior index observations and model artifacts for audit and retest.

Transparent controls store model version and input features for:

- Video exceeds 10x creator baseline within 24 hours.
- Trend enters breakout within six hours from a genuinely pre-breakout baseline.
- Expected peak time.
- Expected remaining useful life.

The evaluator labels only horizons with adequate future tape, marks missing creator baselines as unscorable, and explicitly rejects an early-entry label when the baseline was already hot. It reports Brier score, baseline skill, log loss, AUC, accuracy, ten-bin calibration error, label sufficiency, and `validated` versus `measured_not_validated`. Dataset certification fails the prediction gate until a model has enough two-class outcomes and positive Brier skill from live future outcomes.

## Verification

```bash
python3 -m pytest \
  tests/test_market_tape.py \
  tests/test_market_tape_adaptive_feedback.py \
  tests/test_market_tape_dataset.py \
  tests/test_market_tape_predictor.py \
  tests/test_youtube_query_research.py \
  tests/test_health.py \
  tests/test_api_e2e.py -q
python3 -m json.tool Sources/OpsConsole/Resources/content-intelligence-contract.json
```

The Market Tape integration suite uses real temporary SQLite databases and real loopback HTTP servers. It verifies append-only enforcement, raw archiving, derivative math, lifetime-counter exclusion, adaptive and fair polling, bounded query feedback, cross-connection atomic admission, authoritative contention budget fields, post-preflight attempt cooldown enforcement with metadata-family and query fallback, exact cooldown evidence in flattened exclusions, rolling admission cooldown across a UTC-day boundary, baseline and direct-current lane reservation, complete hashed exclusion decisions beyond the display preview, planner-versus-attempt receipts, terminal forecast refresh priority under a one-request full-cycle ceiling, discovery priority over ordinary due work under the same ceiling, phase-accurate recheck receipts, unchanged-snapshot deferral, YouTube pagination and known-ID skipping, healthy-provider overflow, persistent search quota accounting, per-operation request receipts, partial-batch preservation, fallback-cache miss classification, genuine canonical-failure reporting, spend accounting, circuit breakers, browser disk-pressure gates, failed-lane and missing-job retry, local archive normalization and relevance QC, exact-query lineage, provider-free context backfill and trend reindexing, dependency-ordered transactional outbox behavior, grouped predictor validation, index/model compatibility, early-entry label semantics, generic-phrase rejection, URL-resistant context cohesion, actionable-opportunity filtering, bounded live-scale intelligence, TikTok URL admission, WAL-pinned certification consistency, duplicate-certifier exclusion, staging-integrity rejection, central parity reconciliation, Management API verification, API authorization, and automatic tick selection.

## Remaining Work

1. Certify one complete 5,000-unique UTC production day and tune per-platform targets from observed capacity.
2. Restore or obtain valid authorized TikTok, Instagram, X, Facebook, and Threads provider access where platform policy permits.
3. Add transcript, visual, audio, and embedding workers to populate the existing content-genome contract.
4. Accumulate enough independent trajectory outcomes to validate or replace the transparent baseline with a learned temporal predictor.
5. Add ClickHouse and vector storage when local observation volume justifies separation from the control-plane spool.
6. Feed qualified emerging and breakout trends into the experiment planner without authorizing generation or publishing automatically.

The runtime continues collecting and rechecking while these gaps remain visible.
