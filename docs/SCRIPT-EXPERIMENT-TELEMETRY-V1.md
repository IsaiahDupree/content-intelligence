# Script Experiment Telemetry V1

Script Experiment Telemetry closes the measured-learning gap between a generated
transcript and its owned post-publish outcomes. It records immutable script
lineage, normalizes a bounded projection of provider counts, and produces
coverage-aware descriptive rollups.

It does not infer why a viewer left, fabricate unavailable retention data, or
claim that an observed difference was caused by the transcript.

## Identity contract

An experiment ID is derived from four immutable fields:

- `brief_id`
- `script_id`
- the SHA-256 digest of the exact generated transcript
- `workflow_seed`, normally the persisted `workflow_id`

The ID has the form `sxp_<24 hex characters>`. Replaying the same lineage returns
the same ID. Changing the transcript or workflow seed creates a new experiment.
The transcript itself is not stored in the telemetry table; only its digest is
retained.

The Script Intelligence and reference-marketing compiler approval paths register
this identity automatically after every gate passes. A `revise` or rejected
candidate is explicitly left unregistered and cannot enter the render-learning
cohort. The returned workflow/package and persisted lineage carry the
`script_experiment_id`, so the publisher can bind the eventual provider post
without title or caption matching.

The authenticated endpoint below remains available for governed import and
idempotent replay of already persisted lineage:

```http
POST /api/script-experiments
Authorization: Bearer $CONTENT_QUALITY_CONTROL_TOKEN
Content-Type: application/json

{
  "brief_id": "<stored brief ID>",
  "script_id": "<stored script ID>",
  "script_sha256": "<SHA-256 of the exact transcript>",
  "workflow_id": "<stored workflow ID>",
  "generation_contract": "<generation contract>"
}
```

`script_text` may be supplied instead of `script_sha256`; the service hashes it
and does not persist the text. A supplied `experiment_id` is accepted only when
it exactly matches the derived lineage ID.

## Metric snapshot contract

Each provider observation is an append-only `lifetime_cumulative` snapshot. The
service requires:

- `idempotency_key`
- `experiment_id`
- `source_platform`
- `provider_post_id`
- `provider_receipt_id`
- timezone-aware `observed_at`
- `view_denominator_basis`
- a non-empty `metrics` object

The canonical count fields are:

- `views`
- `hold_1s_views`
- `hold_3s_views`
- `completed_views`
- `shares`
- `saves`
- `cta_clicks`
- `cta_leads`
- `cta_signups`
- `cta_trials`
- `cta_purchases`

CTA counts can instead be supplied under `cta_outcomes` using `clicks`, `leads`,
`signups`, `trials`, and `purchases`. Provider aliases such as Instagram
`plays`/`saved` and TikTok `videoViewCount`/`favorites` are normalized. Unknown
fields fail closed. The receipt stores both the canonical counts and the exact
provider field names used to produce them.

```http
POST /api/script-experiments/metrics
Authorization: Bearer $CONTENT_QUALITY_CONTROL_TOKEN
Content-Type: application/json

{
  "idempotency_key": "<stable provider observation key>",
  "experiment_id": "<sxp ID>",
  "source_platform": "instagram",
  "provider_post_id": "<provider post ID>",
  "provider_receipt_id": "<immutable raw-provider receipt ID>",
  "observed_at": "<ISO-8601 timestamp with timezone>",
  "view_denominator_basis": "video_starts",
  "metrics": {
    "plays": 1200,
    "oneSecondVideoViews": 900,
    "threeSecondVideoViews": 610,
    "videoCompletions": 280,
    "shares": 42,
    "saved": 31
  },
  "cta_outcomes": {
    "clicks": 24,
    "leads": 7,
    "purchases": 2
  }
}
```

Counts must be non-negative whole numbers. A 1-second, 3-second, or completion
count cannot exceed `views` in the same snapshot, and the 3-second count cannot
exceed the 1-second count. Precomputed rate fields are rejected: send the
observed numerator and its eligible view count so the service can retain the
denominator.

## Denominator policy

`views` is rate-eligible only when the bound provider receipt establishes that
it means video starts for the reported numerator. Set
`view_denominator_basis` to one of:

- `video_starts`
- `qualified_video_views`
- `impressions`
- `shown_in_feed`
- `not_available`

Only `video_starts` contributes to hold, completion, or CTA rates in V1. Other
bases preserve their raw counts but produce no rate. This prevents X
impressions, YouTube shown-in-feed counts, or a platform-specific qualified view
threshold from being silently treated as a video start.

## Rollups

```http
GET /api/script-experiments/rollup?experiment_id=<sxp ID>
GET /api/script-experiments/rollup?script_id=<script ID>
GET /api/script-experiments/rollup?workflow_id=<workflow ID>
Authorization: Bearer $CONTENT_QUALITY_CONTROL_TOKEN
```

For each experiment/platform/post/metric, the rollup uses the latest cumulative
observation. It then sums those latest counts across posts. This avoids counting
the same lifetime total again on every provider poll.

Hold, completion, and CTA rates are denominator-weighted:

```text
sum(eligible observed numerators) / sum(eligible video starts)
```

Every rate includes its numerator, eligible denominator, denominator basis,
eligible-post count, and basis-excluded-post count. Shares and saves remain raw
observed counts. Coverage reports how many attributed posts supplied every
metric.

A provider post can belong to only one experiment. Reusing an idempotency key
with different facts or attempting to reassign a post returns
`IMMUTABLE_EXPERIMENT_CONFLICT`.

## Other endpoints

- `GET /api/script-experiments/health`
- `GET /api/script-experiments?script_id=<script ID>`
- `GET /api/script-experiments/<experiment_id>`
- `GET /api/script-experiments/metrics?experiment_id=<sxp ID>`

All endpoints require the ContentQuality bearer token and produce the existing
agent-query audit receipt.

## Storage and migration

The service uses the configured local `CONTENT_QUALITY_DB`. ContentQuality
startup applies the V1 schema idempotently and creates:

- `cq_script_experiments`
- `cq_script_experiment_posts`
- `cq_script_metric_snapshots`

All three tables have update/delete rejection triggers. No Supabase or remote
schema migration is required for V1. Restart the ContentQuality runtime after
deploying the code; the first startup creates the tables before routes accept
traffic.

Verify locally:

```bash
python3 -m pytest -q \
  tests/test_script_experiment_telemetry.py \
  tests/test_script_intelligence_integration.py
```

The tests use the real Flask service, real normalization logic, and a temporary
SQLite database. They do not use production mock providers or fabricated
runtime fallbacks.
