#!/usr/bin/env python3
"""Register a foundry-approved script in ContentQuality so it can be audited.

The foundry writes the words. ContentQuality remains the independent auditor
that checks them against the immutable transcript cohort; it never edits them.
"""

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPOSITORY_ROOT))

from services.content_quality.engine import QualityStore  # noqa: E402

DEFAULT_QUALITY = (
    Path.home()
    / "Library/Application Support/ContentQuality/data/content-quality.sqlite3"
)
FOUNDRY_STORE = Path(
    "/Users/isaiahdupree/foundry-state/marketing-video-foundry/"
    "work/ops-console/content-intelligence.json"
)
# The foundry transcript receipt ids were imported from these ContentQuality
# receipts, so the cohort is the same six observed posts on both sides.
RECEIPT_MAP = {
    "rcpt_24abc063eb3c868e3fbc": "rcpt_24abc063eb3c868e3fbc",
    "rcpt_59d63d03d9645786804c": "rcpt_59d63d03d9645786804c",
    "rcpt_b86351793fae69ebfe64": "rcpt_b86351793fae69ebfe64",
    "rcpt_885be30b84945a70ccbe": "rcpt_885be30b84945a70ccbe",
    "rcpt_5af02086e2ddc6a46e09": "rcpt_5af02086e2ddc6a46e09",
    "rcpt_d00d302fba60940bbd23": "rcpt_d00d302fba60940bbd23",
}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--foundry-script-id", required=True)
    parser.add_argument("--quality-db", type=Path, default=DEFAULT_QUALITY)
    args = parser.parse_args()

    registry = json.loads(FOUNDRY_STORE.read_text())
    collection = registry.get("script_receipts")
    rows = collection.get("records") if isinstance(collection, dict) else collection
    script = next(
        (row for row in rows if str(row.get("id")) == args.foundry_script_id), None
    )
    if script is None:
        raise SystemExit(f"foundry script not found: {args.foundry_script_id}")
    if script.get("status") != "approved":
        raise SystemExit(f"foundry script is not approved: {script.get('status')}")

    receipt_ids = [
        RECEIPT_MAP[str(value)]
        for value in script.get("transcript_receipt_ids") or []
        if str(value) in RECEIPT_MAP
    ]
    if len(receipt_ids) != len(script.get("transcript_receipt_ids") or []):
        raise SystemExit(
            "every foundry transcript receipt must map to a ContentQuality receipt"
        )

    timeline = [
        {
            "beat": beat.get("beat"),
            "purpose": beat.get("purpose"),
            "text": beat.get("text"),
            "start": beat.get("start_seconds"),
            "end": beat.get("end_seconds"),
        }
        for beat in script.get("beat_map") or []
    ]
    record = {
        "script_id": args.foundry_script_id,
        "topic": script.get("niche"),
        "objective": script.get("objective"),
        "audience": script.get("audience"),
        "source_receipt_ids": receipt_ids,
        "text": script.get("script"),
        "hook": script.get("hook"),
        "cta": script.get("cta"),
        "timeline": timeline,
        "status": "awaiting_audit",
        "authored_by": "marketing_video_foundry_content_loop",
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
    QualityStore(args.quality_db).put_script(record)
    print(
        json.dumps(
            {
                "script_id": record["script_id"],
                "source_receipt_ids": receipt_ids,
                "words": len(str(record["text"]).split()),
                "timeline_beats": len(timeline),
            },
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
