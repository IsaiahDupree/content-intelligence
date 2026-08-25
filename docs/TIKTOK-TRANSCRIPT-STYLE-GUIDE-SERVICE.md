# TikTok Transcript Style Guide Service

## Purpose

This service turns performance-qualified TikTok transcript receipts into an
aggregate language and delivery guide, then requires that guide during script
briefing, generation, and audit. It is designed to learn repeatable patterns
without copying a creator's identity, likeness, voice, source media, or
distinctive wording.

The production path is:

```text
TikTok discovery or local archive
  -> Market Tape observations
  -> real media download and local Whisper transcript
  -> performance and provenance audit
  -> viral transcript pattern receipt
  -> cross-creator style guide receipt
  -> immutable script brief
  -> script generation
  -> style-fit and copy audit
  -> relatability, narrative, and attention gates
  -> render-ready script
```

## Existing Transcript Acquisition

Market Tape exposes one authenticated wrapper that performs discovery,
download, transcription, qualification, and persistence:

```bash
curl --fail-with-body \
  -X POST http://127.0.0.1:6006/api/market-tape/full-pipeline \
  -H "Authorization: Bearer $MARKET_TAPE_CONTROL_TOKEN" \
  -H 'Content-Type: application/json' \
  -d '{
    "discovery_mode": "full",
    "platforms": ["tiktok"],
    "topic": "creator burnout",
    "limit": 12,
    "model": "base",
    "performance_discovery": false
  }'
```

This is real production behavior. It does not synthesize provider results.
TikTok can enter through the official Research API adapter, the bounded
RapidAPI adapter, or the local archive adapter. The transcript bank uses the
exact selected source URL and stores source metrics, acquisition provenance,
audio and transcript hashes, Whisper model/language, duration, segments, and
the performance audit.

Read-only inventory and provider state are available at:

```bash
curl http://127.0.0.1:6006/api/market-tape/status
curl http://127.0.0.1:6006/api/market-tape/sources
curl 'http://127.0.0.1:6006/api/market-tape/videos?platform=tiktok&limit=100'
```

## Style Guide Contract

`TranscriptStyleGuideService` accepts only verified
`viral_transcript_pattern` receipts backed by a passing transcript artifact.
It fails closed unless a cohort has at least:

- 5 unique verified transcripts
- 3 distinct creators
- 100,000 observed views at the stored metric snapshots

The immutable `aggregate_transcript_style_guide_v1` receipt contains:

- observed words per second and duration ranges
- average and short-sentence ranges
- question and exclamation rates
- first-person and direct-address rates
- contraction and discourse-marker rates
- recurring cross-creator vocabulary and function markers
- observed hook families
- recurring structural beats
- source receipt IDs, transcript hashes, creator IDs, URLs, and metric snapshots
- explicit rights and originality restrictions

No transcript text is copied into the guide. Transcript text can estimate
cadence, punctuation, phrasing, and structure. It cannot measure actual vocal
pitch, timbre, accent, or acoustic inflection. Those require a separate
rights-safe audio feature service and are deliberately reported as unmeasured.

## API

The Content Quality API runs on `127.0.0.1:6010`. Agent operations require
`CONTENT_QUALITY_CONTROL_TOKEN`.

Inspect readiness:

```bash
curl --fail-with-body \
  -H "Authorization: Bearer $CONTENT_QUALITY_CONTROL_TOKEN" \
  'http://127.0.0.1:6010/api/transcript-style-guides/status?platform=tiktok'
```

Build a guide from verified pattern receipts:

```bash
curl --fail-with-body \
  -X POST http://127.0.0.1:6010/api/transcript-style-guides/build \
  -H "Authorization: Bearer $CONTENT_QUALITY_CONTROL_TOKEN" \
  -H 'Content-Type: application/json' \
  -d '{
    "topic": "creator burnout",
    "platform": "tiktok",
    "receipt_ids": ["receipt_1", "receipt_2", "receipt_3", "receipt_4", "receipt_5"]
  }'
```

Audit a proposed script:

```bash
curl --fail-with-body \
  -X POST http://127.0.0.1:6010/api/transcript-style-guides/audit \
  -H "Authorization: Bearer $CONTENT_QUALITY_CONTROL_TOKEN" \
  -H 'Content-Type: application/json' \
  -d '{
    "style_guide_id": "style_...",
    "text": "Original proposed script text",
    "target_duration_seconds": 30
  }'
```

The audit checks speech-rate fit, sentence shape, direct address,
contractions, hook family, recurring structure, and transitions. It separately
rejects a script when its maximum five-word overlap with any source transcript
reaches the guide's 0.20 threshold.

## Generation Enforcement

Requesting a TikTok-native brief is explicit:

```bash
curl --fail-with-body \
  -X POST http://127.0.0.1:6010/api/script-intelligence/briefs \
  -H "Authorization: Bearer $CONTENT_QUALITY_CONTROL_TOKEN" \
  -H 'Content-Type: application/json' \
  -d '{
    "topic": "creator burnout",
    "audience": "content creators and software founders",
    "style_platform": "tiktok"
  }'
```

The brief freezes the guide ID, receipt ID, source hash, targets, and evidence.
Generation cannot replace those fields. The generated script stores its style
lineage and receives a `transcript_style_fit` audit. A new style-bound script
cannot become render-ready unless that audit is current, hash-bound, and
passing. Source transcript receipts remain separate from style receipts so the
relatability and originality chains cannot confuse evidence with instructions.

Use `style_platform: cross_platform` only when a TikTok-only cohort is not a
hard requirement. A request for `style_platform: tiktok` returns the typed
`INSUFFICIENT_TIKTOK_STYLE_EVIDENCE` state rather than silently substituting
YouTube evidence.

## Current Evidence Snapshot

On 2026-08-25 the production Market Tape contained:

| Platform | Transcript artifacts | Passing artifacts | Observed views |
|---|---:|---:|---:|
| YouTube | 586 | 233 | 4,267,181,630 |
| TikTok | 17 | 0 | 593,515,751 |

The TikTok rows have substantial view snapshots but currently fail the
transcript performance/provenance audit, so the service correctly reports
`needs_evidence` and will not manufacture a TikTok style guide from them. The
17 artifacts are mostly too short, off-topic, non-English, music, or noisy
speech: all 17 fail transcript-topic relevance, 13 fail the 40-word floor, and
one lacks timestamped segments. Only five are English with at least 12 words,
and inspection shows that four are music, gaming, or malformed speech rather
than a credible spoken-content cohort.

A live authenticated wrapper run completed successfully on 2026-08-25 and
wrote both discovery and transcript-run receipts, but found no eligible new
TikTok candidate. Market Tape holds 575 TikTok videos and 306 accepted,
untranscribed observations; none currently satisfy the complete duration,
performance, engagement, source URL, and full-evidence policy together. The
local TikTok archive edge most recently succeeded and then cooled down after
provider counters regressed. The configured RapidAPI plan is at its monthly
quota and its per-video refresh path also recorded endpoint drift. The
official Research API edge lacks its credential. These are concrete
acquisition/data-readiness gaps, not a missing style or generation interface.

## Safety and Interpretation

- Public performance is observational evidence, not proof that a style caused
  the views.
- The guide aggregates across creators; it never asks generation to impersonate
  one person.
- Source clips, likenesses, and voices remain reference-only.
- Exact or near-exact source wording is blocked by the copy gate.
- A passing style audit predicts contract fit, not future views or conversion.
- Published outcomes and controlled variants must update the learning layer
  before any pattern is promoted to causal evidence.

## Verification

```bash
python3 -m pytest -q tests/test_transcript_style_guides.py
python3 -m pytest -q tests/test_script_intelligence_integration.py
python3 -m pytest -q tests
```

The dedicated tests cover durable aggregate guides, insufficient evidence,
copy rejection, immutable brief linkage, generation enforcement, API auth, and
agent catalog discovery.
