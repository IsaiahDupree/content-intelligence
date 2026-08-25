#!/bin/zsh
set -eo pipefail

ROOT="${0:A:h:h}"
if [[ -f "$ROOT/.env.market-tape" ]]; then
  set -a
  source "$ROOT/.env.market-tape"
  set +a
fi

API_BASE_URL="${MARKET_TAPE_API_BASE_URL:-http://127.0.0.1:6006}"
CYCLE_SECONDS="${MARKET_TAPE_CYCLE_SECONDS:-900}"
REQUEST_TIMEOUT_SECONDS="${MARKET_TAPE_SCHEDULER_TIMEOUT_SECONDS:-7200}"
RECEIPT_PATH="${MARKET_TAPE_SCHEDULER_RECEIPT_PATH:-/tmp/content-intelligence-market-tape-last-tick.json}"

typeset MARKET_TAPE_SCHEDULER_CONTROL_TOKEN="${MARKET_TAPE_CONTROL_TOKEN:-}"
unset MARKET_TAPE_CONTROL_TOKEN
if [[ "$MARKET_TAPE_SCHEDULER_CONTROL_TOKEN" == *$'\n'* \
   || "$MARKET_TAPE_SCHEDULER_CONTROL_TOKEN" == *'"'* \
   || "$MARKET_TAPE_SCHEDULER_CONTROL_TOKEN" == *'\\'* ]]; then
  print -u2 -- "refusing unsafe MARKET_TAPE_CONTROL_TOKEN characters"
  exit 1
fi

curl_auth_config() {
  if [[ -n "$MARKET_TAPE_SCHEDULER_CONTROL_TOKEN" ]]; then
    print -r -- \
      "header = \"Authorization: Bearer $MARKET_TAPE_SCHEDULER_CONTROL_TOKEN\""
  fi
}

while true; do
  started_at="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
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
    "$API_BASE_URL/api/market-tape/tick"; then
    print -- "$started_at tick completed receipt=$RECEIPT_PATH"
    next_delay="$CYCLE_SECONDS"
  else
    curl_status=$?
    print -u2 -- "$started_at tick failed curl_status=$curl_status"
    next_delay=30
  fi
  sleep "$next_delay"
done
