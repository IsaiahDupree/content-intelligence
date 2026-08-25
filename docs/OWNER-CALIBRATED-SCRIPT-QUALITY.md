# Owner-calibrated script quality

The deterministic writers in `marketing_scripts.py` and `engine.py` share
`script_quality.py`. This keeps style checks, rewrite limits, structure choice,
and delivery planning consistent while each writer keeps its own evidence and
copy gates.

## Stable contracts

- `evidence_safe_rhetorical_structure_v1` records the selected structure and
  role order. Each writer has four structures. A stable seed selects the first
  one; a bounded retry rotates to the next one.
- `owner_calibrated_script_quality_v1` records five explicit judgments.
- `bounded_script_quality_rewrite_v1` records no more than three attempts.
- `delivery_visual_plan_v1` gives timed delivery and visual-reset cues for the
  final draft, including drafts returned with `revise`.

The structure layer only reorders supplied roles. It does not add a fact,
number, result, quote, first-person statement, or call to action. The call to
action remains last and each source-bound phrase remains present exactly once.

## Five judgments

Every draft is checked for:

1. Spoken naturalness: sentence length, long-sentence share, and formal filler.
2. Specificity: concrete actions, concrete nouns, and any verified number.
3. Tension and payoff: both sides must exist, with tension before payoff.
4. Technical-language leakage: internal build terms, hard internal phrases,
   and spoken corpus-count narration fail this check.
5. Repeated phrasing: repeated four-word runs, known formulas, and a matching
   six-word opening from up to 20 recent stored scripts.

The style judge never grants speaker perspective. Its result says
`perspective_authorization_evaluated: false`. First-person text is handled by
the separate evidence gate.

## Evidence and perspective

Public creator text is analysis-only. It is never treated as owner proof and
never grants owner voice. If a stored public moment contains first-person
words, the script attributes the exact quote with `One person said ...` and
records that choice in `verified_speaker_claim_gate_v1`.

Caller-supplied `human_moment` text must match an immutable audience-moment
receipt, its source keys, and the performance-qualified transcript set. Raw
caller `owned_proof` is rejected. An owned claim must resolve to a stored
owned-source receipt whose exact statement, statement hash, source hash, byte
count, owner ID, and current file bytes all match. A caller boolean cannot
grant first-person voice. First-person is admitted only when the exact
first-person statement itself is in that verified owned source.

Reference-compiler proof uses the same rule. `experience`,
`owned_measurement`, and any first-person proof require exact stored owned
evidence. Public reference receipt IDs cannot satisfy that gate.

## Bounded rewrite and audit

A qualitative failure gets at most three attempts. Safe local edits are
limited to literal plain-language replacements, clause splits, transition
rotation, and structure rotation. Protected evidence text is masked during
local edits and then restored exactly. Each attempt runs the owner judge and
the reference exact-copy gate again.

The immutable rewrite receipt records parent script ID and hash, failure
codes, repair actions, attempt count, all audit IDs, and final exact-copy audit
ID. Script Intelligence also places both `owner_quality_audit_id` and
`bounded_rewrite_audit_id` in `stage_receipts`.

Evidence, rights, copy, cohort-integrity, and unavailable-judge failures are
not rewritten into a pass. They stay fail-closed.

## Delivery and visual resets

Every returned transcript includes a timed delivery and visual plan. A long
beat is split into neutral planning cues so no cue lasts more than three
seconds. Each cue names a delivery direction and a visual mode, but its asset
state remains `not_selected`; the plan does not claim that an asset exists.

All cues require owned or licensed assets. Reference clips, identity,
likeness, and voice are forbidden. Consumers can enforce the contract with:

```text
actual_maximum_visual_interrupt_gap_seconds <= 3.0
reference_clips_used == false
reference_identity_likeness_or_voice_used == false
```

## Focused verification

- `tests/test_script_quality.py`
- `tests/test_marketing_scripts.py`
- `tests/test_reference_corpus.py`
- `tests/test_script_intelligence_integration.py`

The tests use deterministic functions, temporary SQLite stores, real local
files, and the existing local HTTP test server. They do not add provider
stubs or fake provider responses.
