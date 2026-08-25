# Content Reference Audit Agent Contract

## Discovery

- Service: `content-quality`
- Loopback base: `http://127.0.0.1:6010`
- Public probe: `GET /api/reference-corpus/health`
- Agent auth: `Authorization: Bearer $CONTENT_QUALITY_CONTROL_TOKEN`
- API definition: `openapi.yaml`
- Corpus schema: `content-reference-corpus.schema.json`
- Audit schema: `content-creation-audit.schema.json`
- Canonical first corpus: `instagram-personalbrandlaunch-reference-v1`

## Required Call Order

1. Call `health`.
2. Call `status` and inspect item count, extraction states, and coverage.
3. Call `find` with the content question. Use returned excerpts and public source links as evidence.
4. Call `audit` before approving a script or edit plan.
5. Reject a result when `copy_gate.passed` is false. Rewrite and audit again.

Do not assume a creator's popularity proves a tactic. The corpus provides observed examples, not causal proof. Separate observed clip choices from claimed performance reasons.

## Write Calls

`acquire` fetches bounded public source data and stores immutable receipts. The limit is 100. `extract` handles at most three clips per API call, derives the transcript, visual facts, contact sheet, and typed analysis, then deletes each source clip.

For bulk local work, use:

```bash
python3 scripts/build_reference_corpus.py acquire \
  --username personalbrandlaunch --limit 75

python3 scripts/build_reference_corpus.py extract-all \
  --corpus-id instagram-personalbrandlaunch-reference-v1 \
  --batch-size 10 --transcript-model base.en --no-semantic
```

The local typed pass is always available. AI enrichment is optional and cannot be a completion gate.

Create a consistent local snapshot before copying to removable storage:

```bash
python3 scripts/build_reference_corpus.py snapshot \
  --corpus-id instagram-personalbrandlaunch-reference-v1 \
  --destination "/Volumes/My Passport/MarketTape/reference-corpora" \
  --timeout-seconds 15
```

The command writes and hashes the local bundle first. A slow or unhealthy target returns `destination_unavailable` without changing the active store.

## Rights Rules

- `rights_state` must remain `public_reference_analysis_only`.
- Source clips must not be retained.
- Do not copy source hooks, scripts, footage, branded art, identity, likeness, or voice.
- A source link may support an abstract lesson; it does not grant direct-use rights.
- New content must pass the five-word overlap gate before production.

## Receipt Rules

Every source call, extraction, and audit must keep a stable ID, timestamps, hashes, model or tool lineage, status, error type, and rights state. Failed items remain retryable. Never delete failed or rejected states to make coverage look complete.

## Storage

The hot store defaults to `~/Library/Application Support/ContentReferenceCorpus`. Set `CONTENT_REFERENCE_ROOT` only to a writable low-latency volume. Export snapshots to external storage after its write probe succeeds; do not put the live SQLite write path on an unhealthy external volume.
