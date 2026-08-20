#!/usr/bin/env python3
"""Rank PASS transcripts by topical fit with the solo-builder marketing story."""

import json
import re
import sqlite3
import sys
from pathlib import Path

TAPE = Path.home() / "Library/Application Support/ContentIntelligence/data/market-tape.sqlite3"

TOPIC = {
    "marketing", "market", "customers", "customer", "sell", "selling", "sales",
    "launch", "launched", "product", "products", "app", "apps", "build",
    "building", "built", "business", "revenue", "money", "paid", "price",
    "pricing", "audience", "growth", "startup", "founder", "indie", "software",
    "shipped", "ship",
}
HUMAN = {
    "alone", "anxious", "anxiety", "burned", "burnout", "burnt", "care",
    "exhausted", "fear", "feel", "feeling", "frustrated", "hard", "hate",
    "overwhelmed", "pressure", "quit", "struggle", "stuck", "tired",
    "trying", "worry", "worse",
}


def tokens(text):
    return re.findall(r"[a-z']+", text.lower())


def main():
    con = sqlite3.connect(f"file:{TAPE}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    rows = con.execute(
        "SELECT a.*, v.creator_id, v.title FROM mt_transcript_artifacts a "
        "JOIN mt_videos v ON v.video_id = a.video_id"
    ).fetchall()
    scored = []
    for row in rows:
        audit = json.loads(row["audit_json"] or "{}")
        if audit.get("decision") != "PASS":
            continue
        if not str(row["whisper_language"] or "").lower().startswith("en"):
            continue
        tpath = Path(str(row["transcript_path"] or ""))
        apath = Path(str(row["audio_path"] or ""))
        if not tpath.is_file() or not apath.is_file():
            continue
        try:
            text = str(json.loads(tpath.read_text())["text"])
        except Exception:
            continue
        toks = tokens(text)
        if not toks:
            continue
        uniq = set(toks)
        topic_hits = len(uniq & TOPIC)
        human_hits = sorted(uniq & HUMAN)
        metrics = json.loads(row["source_metrics_json"] or "{}")
        scored.append({
            "transcript_id": row["transcript_id"],
            "video_id": row["video_id"],
            "creator_id": row["creator_id"],
            "title": (row["title"] or "")[:70],
            "views": int(metrics.get("views") or 0),
            "duration": float(row["duration_seconds"] or 0),
            "words": int(row["word_count"] or 0),
            "topic_hits": topic_hits,
            "human_terms": human_hits,
        })
    scored.sort(key=lambda r: (-r["topic_hits"], -len(r["human_terms"]), -r["views"]))
    print(f"{len(scored)} PASS english transcripts with artifacts present\n")
    for row in scored[:25]:
        print(
            f"{row['topic_hits']:>3} topic | human={','.join(row['human_terms'])[:40]:<40} | "
            f"{row['views']:>10,} views | {row['duration']:>6.1f}s | {row['creator_id'][:18]:<18} | {row['title']}"
        )
    Path("/tmp/marketing_cohort_candidates.json").write_text(json.dumps(scored, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
