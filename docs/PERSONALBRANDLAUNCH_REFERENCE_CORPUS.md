# Personal Brand Launch Reference Corpus

Canonical corpus: `instagram-personalbrandlaunch-reference-v1`.

## Purpose

This corpus turns a public creator feed into a source-grounded audit bank for new content. It preserves public links, immutable source receipts, timestamped counters, transcripts, deterministic visual facts, low-resolution contact sheets, typed creative patterns, hashes, and extractor lineage.

It is not a clone system. It does not preserve source clips or grant direct-use rights.

## Current Corpus

- Requested items: 75
- Platform: Instagram
- Rights state: `public_reference_analysis_only`
- Transcript model: `base.en`
- Typed analysis: `local_semantic_v1`
- AI enrichment: optional
- Source clips retained: no

The live status and generated summary are authoritative. Do not paste counters into a static document as runtime truth.

## Extracted Fields

Each item stores:

- public ID, shortcode, URL, caption, publish time, duration, frame size, language, and sound labels;
- append-only views, likes, and comments at observation time;
- transcript text, timed segments, confidence estimate, model, and hash;
- cut rate, cut times, aspect ratio, face and people detection, frame movement, sampled-frame OCR, brightness, contrast, sharpness, and contact-sheet hash;
- hook, format, viewer, opening visual, delivery, framing, edit devices, narrative beats, retention devices, CTA, reusable principles, and explicit do-not-copy rules;
- source, extractor, model, tool-version, and failure lineage.

## Content-Creation Use

Ask a specific question through `find`, inspect the returned sources, and send the draft through `audit`. The scorecard measures hook clarity, narrative flow, CTA, target duration fit, source evidence, and originality. Approval requires both a sufficient overall score and a passing copy gate.

The audit cannot prove that a creative choice caused views. It can show which choices were observed and which source examples support an abstract recommendation.

## Canonical Interfaces

- CLI: `scripts/build_reference_corpus.py`
- Python: `services.content_quality.reference_corpus.ReferenceCorpusService`
- HTTP: `/api/reference-corpus/*` on the Content Quality service
- Contract pack: `protocols/content-reference-audit-v1/`
- Agent guide: `protocols/content-reference-audit-v1/AGENT.md`
- Snapshot: `scripts/build_reference_corpus.py snapshot`

## Storage Rule

The active SQLite store uses local hot storage.

The Passport NTFS volume failed the current bounded write probe. The snapshot command still creates a consistent, hashed local bundle and reports `destination_unavailable` when the external copy times out. Retry the same command after the volume is healthy; the active store remains local.
