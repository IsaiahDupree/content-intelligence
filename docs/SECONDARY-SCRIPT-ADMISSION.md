# Secondary spoken-script admission

The four reviewer-flagged paths are active spoken-script writers or admission
orchestrators:

- `services/content_brief/service.py` calls `ScriptGenerator`; the actual beat
  writer and admission point is `services/content_brief/script_generator.py`.
- `services/trend_flash/flash_generator.py` writes the Trend Flash transcript.
- `services/trend_intelligence/reeltrends_service.py` writes ReelTrends beats.
- `services/narrative/content_orchestration.py` converts narrative briefs into
  spoken scripts and hands admitted text to the clip planner.

All four route candidates through `services/spoken_script_admission.py`, a
thin adapter around the canonical `services.content_quality.script_quality`
owner audit. The adapter makes at most three deterministic repair/structure
attempts. A failed candidate stays in audit metadata, while renderable
transcript and beat fields remain empty and the writer returns
`blocked_quality`. Provider errors return an explicit provider-blocked state;
they do not fall back to template copy.

Legacy topic, cluster, and brief fields are not evidence receipts. These paths
therefore pass no protected evidence phrases. The adapter rejects evidence
phrases unless receipt IDs accompany them, and it rejects every remaining
first-person token, unsupported numeric or absolute claim, and identity or
voice imitation language.

Every admitted package includes:

- a seed-selected rhetorical structure that rotates across bounded attempts;
- the owner-calibrated audit and claim-safety decision;
- the attempt ledger and final blocking reason;
- a delivery and visual plan using owned or licensed assets only;
- an interrupt schedule with no gap above three seconds; and
- declarations that reference clips and source identity, likeness, and voice
  are not used.
