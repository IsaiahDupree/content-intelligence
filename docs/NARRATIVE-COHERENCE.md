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
     (`evidence_summary` with nonzero counts), some beat must tell the
     audience where it comes from.
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
participates in `gates.ready_for_render` (a fourth required `PASS` alongside
`relatability_script`, `attention_script`, `attention_video_preflight`).

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
all four gates; nothing publishes without the foundry dispatcher's separate
auto-approval policy on top.

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

# 4. Remaining render gates.
curl -s -X POST $BASE/api/relatability/script-audit  -H 'content-type: application/json' -d @script.json
curl -s -X POST $BASE/api/attention/script-audit     -H 'content-type: application/json' -d @script.json
curl -s -X POST $BASE/api/attention/video-preflight  -H 'content-type: application/json' -d @script.json

# 5. Handoff check — all four gates must be PASS.
curl -s $BASE/api/scripts/{script_id}
# -> {"gates": {"ready_for_render": true, "required_decisions": {
#      "narrative_coherence": "PASS", "relatability_script": "PASS",
#      "attention_script": "PASS", "attention_video_preflight": "PASS"}}}
```

Generation reject codes an agent must handle: `REJECT_NO_RECEIPTS`,
`REJECT_UNKNOWN_RECEIPTS`, `REJECT_INSUFFICIENT_TRANSCRIPT_COHORT`,
`REJECT_CONVERSION_UNPROVEN`, `REJECT_NO_RECURRING_HUMAN_LANGUAGE`,
`REJECT_NARRATIVE_INCOHERENT`, `REJECT_COHERENCE_JUDGE_UNAVAILABLE`.
All are 422 with `status: "rejected"`. Rejections are fail-closed by design;
fix the input (or the judge), never bypass the gate.

## Configuration

Environment of the service process (sourced by
`run_content_quality_api.sh` from the ContentIntelligence runtime env file):

| Variable | Default | Meaning |
|---|---|---|
| `NARRATIVE_COHERENCE_LLM` | `openai` | judge provider: `openai`, `claude` (local CLI), or `off` (rules-only) |
| `NARRATIVE_JUDGE_MODEL` | `gpt-4o-mini` | OpenAI model for the judgment pass |
| `OPENAI_API_KEY` | — | required for the `openai` provider; never stored in source control |

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
