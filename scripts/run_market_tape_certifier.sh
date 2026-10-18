#!/bin/zsh
set -eo pipefail

ROOT="${0:A:h:h}"
if [[ -f "$ROOT/.env.market-tape" ]]; then
  set -a
  source "$ROOT/.env.market-tape"
  set +a
fi

API_BASE_URL="${MARKET_TAPE_API_BASE_URL:-http://127.0.0.1:6006}"
RECEIPT_PATH="${MARKET_TAPE_DATASET_RECEIPT_PATH:-/tmp/content-intelligence-market-tape-dataset.json}"
REQUEST_TIMEOUT_SECONDS="${MARKET_TAPE_DATASET_TIMEOUT_SECONDS:-7200}"
MIN_RECERTIFY_SECONDS="${MARKET_TAPE_DATASET_MIN_RECERTIFY_SECONDS:-21600}"
PYTHON_BIN="${MARKET_TAPE_PYTHON_BIN:-/opt/homebrew/bin/python3}"

if [[ "${MARKET_TAPE_DATASET_FORCE_RECERTIFY:-false}" != "true" ]]; then
  recent_manifest="$($PYTHON_BIN - "$API_BASE_URL" "$MIN_RECERTIFY_SECONDS" "$RECEIPT_PATH" <<'PY'
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.request import urlopen

api_base = sys.argv[1].rstrip("/")
minimum_age = max(0, int(sys.argv[2]))
receipt_path = Path(sys.argv[3])
now = datetime.now(timezone.utc)
target = (now - timedelta(days=1)).date().isoformat()
try:
    with urlopen(f"{api_base}/api/market-tape/datasets/status", timeout=5) as response:
        payload = json.load(response)
    candidate = payload.get("latest_success") or payload
    created = datetime.fromisoformat(
        str(candidate["created_at"]).replace("Z", "+00:00")
    ).astimezone(timezone.utc)
    manifest = Path(str(candidate["manifest_path"]))
    age = (now - created).total_seconds()
    if (
        candidate.get("contract") == "market_tape_daily_dataset_v1"
        and candidate.get("dataset_date") == target
        and candidate.get("manifest_available") is True
        and 0 <= age < minimum_age
    ):
        receipt_path.parent.mkdir(parents=True, exist_ok=True)
        receipt_path.write_text(
            json.dumps(payload, sort_keys=True, separators=(",", ":")) + "\n",
            encoding="utf-8",
        )
        print(manifest)
except (OSError, KeyError, TypeError, ValueError, json.JSONDecodeError):
    pass
PY
)"
  if [[ -n "$recent_manifest" ]]; then
    print -- "$(date -u +%Y-%m-%dT%H:%M:%SZ) dataset certification skipped recent_manifest=$recent_manifest"
    exit 0
  fi
fi

typeset MARKET_TAPE_CERTIFIER_CONTROL_TOKEN="${MARKET_TAPE_CONTROL_TOKEN:-}"
unset MARKET_TAPE_CONTROL_TOKEN
if [[ "$MARKET_TAPE_CERTIFIER_CONTROL_TOKEN" == *$'\n'* \
   || "$MARKET_TAPE_CERTIFIER_CONTROL_TOKEN" == *'"'* \
   || "$MARKET_TAPE_CERTIFIER_CONTROL_TOKEN" == *'\\'* ]]; then
  print -u2 -- "refusing unsafe MARKET_TAPE_CONTROL_TOKEN characters"
  exit 1
fi

curl_auth_config() {
  if [[ -n "$MARKET_TAPE_CERTIFIER_CONTROL_TOKEN" ]]; then
    print -r -- \
      "header = \"Authorization: Bearer $MARKET_TAPE_CERTIFIER_CONTROL_TOKEN\""
  fi
}

for attempt in 1 2 3 4 5 6; do
  if /usr/bin/curl \
    --fail \
    --silent \
    --show-error \
    --max-time "$REQUEST_TIMEOUT_SECONDS" \
    --request POST \
    --config <(curl_auth_config) \
    -H "Content-Type: application/json" \
    --data '{}' \
    --output "$RECEIPT_PATH" \
    "$API_BASE_URL/api/market-tape/datasets/certify"; then
    print -- "$(date -u +%Y-%m-%dT%H:%M:%SZ) dataset certification completed receipt=$RECEIPT_PATH"
    exit 0
  fi
  print -u2 -- "$(date -u +%Y-%m-%dT%H:%M:%SZ) certification attempt=$attempt failed"
  sleep 300
done

exit 1
