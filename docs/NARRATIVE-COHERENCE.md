# Narrative Coherence Service

Loopback-only quality gate on the Content Quality API (`http://127.0.0.1:6010`).
Part of the content-intelligence stack; registered in the intel-node service
registry as `narrative-coherence` (tier: Content Quality Gates).

Owner directive 2026-08-22: the transcript/script generation service must be
audited so that the context behind the transcript — as things are mentioned in
timeline fashion — makes sense as presented to the audience. The production
failure this guards against: a script drew on backend complaint data (Reddit
and elsewhere) that was never presented in the timeline of the video, so
claims landed on the viewer without the context that justified them. Sourced
correctly is not the same as presented coherently.

Contract requirements: `NAR-001` .. `NAR-003` in
`OpsConsole/Sources/OpsConsole/Resources/content-intelligence-contract.json`.
Implementation: `services/content_quality/narrative_coherence.py`.
Tests: `tests/test_narrative_coherence.py` (19 tests).

## What it decides

A script's beat timeline is audited strictly in viewer order:

1. **Deterministic rules** (always on, never configurable):
   - `TIMELINE_DISCONTINUITY` — beats must be contiguous, monotonic, starting at 0.
   - `DANGLING_REFERENT` — "these stories" / "those complaints" must have an
     antecedent in an earlier beat (singular/plural variants both count).
   - `EVIDENCE_NEVER_VOICED` — if the script has backend evidence
     (`evidence_summary` with nonzero counts), some beat must present the
     source-bound human context. Corpus size and source-system details stay in
     receipts rather than spoken copy.
   - `CONTEXT_AFTER_DEPENDENT_CLAIM` — that context beat must come BEFORE the
     first `claim`/`proof` beat that leans on it.
   - `CLAIM_BEFORE_SETUP` — the timeline may not open on a claim/proof.
   - `CTA_NOT_LAST` — if a `cta` beat exists, it must be the final beat.
2. **Auto-revise loop** (inside `/api/scripts/generate` only, max 3 attempts):
   deterministic repairs — insert a 4-second `evidence_context` beat before the
   first dependent claim, retime contiguously, move the CTA last — then
   re-audit. The repair may only voice evidence that actually exists; a script
   with no backend evidence never has a source invented for it. The inserted
   beat is sized so a repaired script still clears the attention gate
   (`proof_by_20_seconds`, `cta_in_final_third`, no beat over 10s).
3. **Cold-viewer LLM judgment** (after rules pass): an LLM reads the beats in
   presentation order with no backend knowledge and answers
   `{"coherent": true|false, "issues": [...]}`. The bar is "can a viewer follow
   what is being said and why", not "is this excellent".

Fail-closed doctrine: an unreachable judge, a garbled verdict, or a
truthy-but-not-boolean `coherent` value is never a pass.

## Decisions

| Decision | Meaning |
|---|---|
| `PASS` | rules clean (possibly after repair) and judge coherent |
| `FAIL_RULES` | rule defects remain (after repairs, where applicable) |
| `FAIL_JUDGMENT` | rules clean but the cold-viewer judge found it incoherent |
| `JUDGE_UNAVAILABLE` | the judge could not produce a valid boolean verdict |

Every audit is persisted to `cq_audits` with `audit_type = "narrative_coherence"`,
so `GET /api/scripts/{script_id}` reports it in `gates.latest_audits` and it
participates in `gates.ready_for_render`. New scripts require eight hash-bound
decisions: the original six plus transcript-style fit and owner-calibrated
quality.

## Endpoints

### `GET /api/narrative-coherence/health`

Standard capability health envelope; 200 when up.

### `POST /api/narrative-coherence/audit`

One-shot audit of any timeline. Rules always run; the judge runs only when the
rules pass. Persists an audit row (use `script_id` to attach it to a script).

Request:

```json
{
  "script_id": "optional-subject-id",
  "evidence_summary": {
    "viral_transcript_patterns": 5,
    "creator_count": 5,
    "observed_views_snapshot": 150000,
    "recurring_human_terms": ["feeling stuck"]
  },
  "timeline": [
    {"start": 0.0, "end": 3.0, "beat": "human_hook", "text": "..."},
    {"start": 3.0, "end": 8.0, "beat": "stakes", "text": "..."}
  ]
}
```

Response (`findings.defects` carries codes from the table above; each defect
has `code`, `detail`, and usually `beat_index`):

```json
{
  "audit_id": "audit-...",
  "audit_type": "narrative_coherence",
  "subject_id": "optional-subject-id",
  "decision": "FAIL_RULES",
  "score": 0.0,
  "findings": {
    "defects": [
      {"code": "DANGLING_REFERENT", "beat_index": 3, "referent": "these stories", "noun": "stories",
       "detail": "beat 3 says 'these stories' but no earlier beat introduces 'stories'"},
      {"code": "EVIDENCE_NEVER_VOICED",
       "detail": "backend evidence exists but no beat tells the audience where it comes from"}
    ],
    "llm_judgment": null,
    "attempts": 1
  },
  "created_at": "..."
}
```

A passing audit returns `decision: "PASS"` and, when the judge ran,
`findings.llm_judgment = {"status": "ok", "coherent": true, "issues": []}`.

Errors: `400 INVALID_REQUEST` when `timeline` is missing/empty.

### Inside `POST /api/scripts/generate`

Generation runs the full enforce loop automatically. Callers see either a
generated script whose payload includes:

```json
"narrative_coherence": {"decision": "PASS", "attempts": 2, "revised": true}
```

or a rejection:

```json
{"status": "rejected", "code": "REJECT_NARRATIVE_INCOHERENT",
 "reason": "The script cannot be presented coherently in timeline order.",
 "narrative_coherence": {"decision": "FAIL_RULES", "attempts": [], "defects_open": []}}
```

`REJECT_COHERENCE_JUDGE_UNAVAILABLE` means the LLM judge itself was down —
fix the judge (see Configuration); do not retry expecting a different answer.

## The script-generation pathway (for agents)

The full evidence-first pathway on `:6010`, in order. Nothing renders without
all applicable gates; publishing has a separate approval policy on top.

The integrated workflow currently emits
`generation_contract = "evidence_bound_category_script_v9"`. That contract
binds each generated script to its immutable brief, selected source moment,
performance-qualified cohort manifest, transcript payload snapshots, and all
applicable persisted pre-render decisions. A consumer must treat a different
generation-contract value as a different script contract, not silently coerce
it to v9.

```bash
BASE=http://127.0.0.1:6010

# 1. Discover performance-qualified transcript receipts (needs >=5 receipts,
#    >=3 creators, >=100k observed views before generation will accept them).
curl -s -X POST $BASE/api/viral-transcripts/discover \
  -H 'content-type: application/json' \
  -d '{"topic": "AI automation", "limit": 5}'
# -> {"receipts": [{"receipt_id": "..."}]}

# 2. (Optional) surface observed human moments to write from.
curl -s -X POST $BASE/api/audience/human-moments \
  -H 'content-type: application/json' \
  -d '{"topic": "AI automation", "audience": "software founders"}'

# 3. Generate. The narrative-coherence enforce loop runs inside this call.
curl -s -X POST $BASE/api/scripts/generate \
  -H 'content-type: application/json' \
  -d '{
    "topic": "AI automation",
    "audience": "software founders",
    "objective": "qualified_attention",
    "claim": "The best automation content begins with a recognizable human problem",
    "human_moment": {"situation": "you feel burned out after another video fails",
                      "stakes": "another tool has cost time without reducing the work"},
    "receipt_ids": ["...from step 1..."]
  }'
# -> script with timeline, evidence_summary, narrative_coherence, script_id
# Conversion objectives additionally require "owned_proof": ["..."].

# 4. Remaining render gates. The integrated script-intelligence workflow also
#    persists the separately named qualitative relatability decision and the
#    immutable transcript-cohort verdict. Direct generation cannot satisfy
#    that cohort gate and therefore remains ineligible for rendering.
curl -s -X POST $BASE/api/relatability/script-audit  -H 'content-type: application/json' -d @script.json
curl -s -X POST $BASE/api/attention/script-audit     -H 'content-type: application/json' -d @script.json
curl -s -X POST $BASE/api/attention/video-preflight  -H 'content-type: application/json' -d @script.json

# 5. Handoff check — all applicable gates must be accepted and hash-bound.
curl -s $BASE/api/scripts/{script_id}
# -> {"gates": {"ready_for_render": true, "required_decisions": {
#      "narrative_coherence": "PASS", "relatability_script": "PASS",
#      "relatability_ai_qualitative": "PASS",
#      "relatability_transcript_cohort": "PASS",
#      "attention_script": "PASS", "attention_video_preflight": "PASS",
#      "transcript_style_fit": "PASS", "owner_calibrated_quality": "PASS"}}}
```

Generation reject codes an agent must handle: `REJECT_NO_RECEIPTS`,
`REJECT_UNKNOWN_RECEIPTS`, `REJECT_INSUFFICIENT_TRANSCRIPT_COHORT`,
`REJECT_CONVERSION_UNPROVEN`, `REJECT_NO_RECURRING_HUMAN_LANGUAGE`,
`REJECT_NARRATIVE_INCOHERENT`, `REJECT_COHERENCE_JUDGE_UNAVAILABLE`.
All are 422 with `status: "rejected"`. Rejections are fail-closed by design;
fix the input (or the judge), never bypass the gate.

### Source-bound script variants

The integrated brief and run endpoints accept an optional integer
`variant_index` from 0 through 7, defaulting to 0. It selects a text-distinct
human moment already stored in the same verified Whisper cohort. The selector
and source moment IDs are part of the immutable brief identity, while sibling
variants retain the same cohort ID and evidence hash. A missing source variant
returns `SCRIPT_VARIANT_INDEX_NOT_AVAILABLE` without starting acquisition; an
invalid type or fixed-bound violation returns an audited `INVALID_REQUEST`.
Variants 0, 1, and 2 therefore produce three source-backed scripts, each of
which must independently satisfy the same applicable hash-bound gates.

Human-moment extraction never paraphrases the source. It evaluates contiguous
windows no longer than 10 words, records source offsets and immutable transcript
lineage, rejects CTA/numeric fragments, and favors complete lived-context
phrasing. `source_selection_score` and the separate audience-adjusted score are
deterministic ranking signals, not probabilities or audience-outcome
measurements. Off-audience context is scored from the full source sentence so a
short excerpt cannot hide that it came from an unrelated job-seeker scenario.

Situation/stakes pairing uses
`source_context_substantive_or_self_v2`. The stakes excerpt may come from the
same source video, or from another verified cohort transcript only when the two
excerpts share substantive non-audience context. Broad category or audience
words such as `software`, `app`, `founder`, and `automation` do not establish a
relationship. When no substantive relationship exists, the source moment is
self-paired; the service does not splice together unrelated creator stories.

### Separate AI relatability verdict

The separately named AI decision uses
`human_relatability_qualitative_verdict_v4`. It runs only after the deterministic
relatability evidence checks pass and remains a prediction: views demonstrate
exposure, not measured relatability, retention, or conversion. The structured
100-point rubric is:

| Dimension | Maximum |
|---|---:|
| Concrete lived moment | 25 |
| Clear personal stakes | 20 |
| Visible input/action/output | 20 |
| Supplied source-language support | 15 |
| Direct audience perspective | 10 |
| Non-alienating framing | 10 |

The provider returns only the six bounded rubric values and its explanations.
The service sums those values and derives `relatable = score >= 70` locally, so
the model cannot return a contradictory top-level score or boolean. A passing
vote must name at least one supported source term and give a non-empty human
reason. The service requires two matching valid votes and stops after at most
five attempts. Provider errors, invalid responses, or a split result without
two matching votes yield `JUDGE_UNAVAILABLE`; they are never converted into an
AI pass. Scores are capped at 90 until post-publication outcomes exist.

### Immutable transcript payload verification

New transcript acquisition writes an append-only row to
`mt_transcript_payload_snapshots`. The script/cohort audit loads that local
SQLite payload snapshot, recomputes its canonical payload hash, substitutes the
current atomic Market Tape transcript text, and verifies the resulting hash
against the acquisition hash. A missing or mismatched snapshot fails the cohort
gate closed, including edits that preserve the original word count.

Legacy payloads are migrated only through the explicit bounded command:

```bash
python3 scripts/backfill_transcript_payload_snapshots.py \
  --cohort-manifest /absolute/path/to/cohort.json \
  --limit 5
```

The command accepts either an exact cohort manifest or repeated exact
`--transcript-id` values, reads at most the requested bounded set, verifies each
Passport payload against both its acquisition hash and the SQLite transcript,
and records an append-only backfill-run receipt. Brief generation and normal
script auditing never call this backfill and never read transcript files from
the Passport. Their normal latency path reads the local cohort manifest,
immutable SQLite payload snapshots, and atomic Market Tape rows only.

### Owned outcomes and retention curve identity

Owned outcome readiness requires one exact-scope, time-ordered
`click -> install -> trial -> purchase` journey plus at least two measured
retention points within one `measurement_id` for the same
content/campaign/offer/source scope. A non-null retention `journey_id` is part
of the curve identity: that curve can link only to the exact same journey. A
NULL `journey_id` is intentionally aggregate-scope and may describe the exact
content/campaign/offer/source scope without claiming an individual journey.
NULL and non-null curves are never merged into one measurement curve.

Retention points and drops are descriptive observations. The service reports
`causal_drop_reasons_available = false`; it does not fabricate millisecond
drop-off explanations or infer causality from a curve.

## Configuration

Environment of the service process (sourced by
`run_content_quality_api.sh` from the ContentIntelligence runtime env file):

| Variable | Default | Meaning |
|---|---|---|
| `NARRATIVE_COHERENCE_LLM` | `openai` | judge provider: `openai`, `claude` (local CLI), or `off` (rules-only) |
| `NARRATIVE_JUDGE_MODEL` | `gpt-5-nano` | OpenAI model for the narrative judgment pass |
| `RELATABILITY_JUDGE` | `openai` | qualitative relatability provider: `openai` or `off` |
| `RELATABILITY_JUDGE_MODEL` | `gpt-5-nano` | OpenAI model for the separate qualitative relatability verdict |
| `OPENAI_API_KEY` | — | required for the `openai` provider; never stored in source control |

Content Quality stores its small cohort manifests under its local runtime data
directory. Immutable source audio and Whisper transcript artifacts remain at
the absolute Passport paths recorded in Market Tape. Acquisition and the
explicit legacy snapshot backfill may read those files. Normal brief generation
and script auditing instead use local immutable payload snapshots and atomic
Market Tape rows, so they perform no removable-volume reads or writes.

Rules and the repair loop are NOT configurable — only the judgment pass is.
Flask test configs may inject `NARRATIVE_LLM_RUNNER` (a callable) or set
`NARRATIVE_COHERENCE_LLM: "off"`; `TESTING: True` defaults the judge off.

## Operational notes

- Deployed by `scripts/install_content_quality_launchd.sh`
  (launchd `com.isaiah.content-quality.api`, KeepAlive). The transcript
  backfill installer shares the same runtime directory — after running it,
  re-run the content-quality installer so the API entrypoint is restored.
- The judge call adds seconds to `/api/scripts/generate`; budget ~10-60s.
- The audit endpoint is cheap when rules fail (no LLM call is made).
