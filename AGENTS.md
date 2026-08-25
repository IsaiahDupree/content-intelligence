# Content Intelligence Agent Rules

Follow the workspace agent rules first.

Before using creator examples for script or edit decisions, read `protocols/content-reference-audit-v1/AGENT.md` and use the typed `reference-corpus` endpoints. Do not collect the same source set through a parallel path.

The canonical corpus is `instagram-personalbrandlaunch-reference-v1`. It is reference-only: keep public links and derived facts, delete source clips, do not imitate identity or voice, and require the copy gate before production.

Runtime counters come from `GET /api/reference-corpus/status`; Markdown is guidance, not live state.
