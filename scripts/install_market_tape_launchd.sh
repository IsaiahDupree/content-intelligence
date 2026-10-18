#!/bin/zsh
set -euo pipefail

ROOT="${0:A:h:h}"
AGENTS="$HOME/Library/LaunchAgents"
RUNTIME_BASE="$HOME/Library/Application Support/ContentIntelligence"
RUNTIME_ROOT="$RUNTIME_BASE/runtime"
RUNTIME_DATA="$RUNTIME_BASE/data"
mkdir -p "$AGENTS"
mkdir -p "$RUNTIME_ROOT/scripts" "$RUNTIME_DATA"

/usr/bin/ditto "$ROOT/services" "$RUNTIME_ROOT/services"
cp "$ROOT/app.py" "$RUNTIME_ROOT/app.py"
cp "$ROOT/market_tape_app.py" "$RUNTIME_ROOT/market_tape_app.py"
cp "$ROOT/scripts/run_market_tape_api.sh" "$RUNTIME_ROOT/scripts/run_market_tape_api.sh"
cp "$ROOT/scripts/run_market_tape_scheduler.sh" "$RUNTIME_ROOT/scripts/run_market_tape_scheduler.sh"
cp "$ROOT/scripts/run_market_tape_certifier.sh" "$RUNTIME_ROOT/scripts/run_market_tape_certifier.sh"
chmod +x \
  "$RUNTIME_ROOT/scripts/run_market_tape_api.sh" \
  "$RUNTIME_ROOT/scripts/run_market_tape_scheduler.sh" \
  "$RUNTIME_ROOT/scripts/run_market_tape_certifier.sh"

PYTHON_BIN="${MARKET_TAPE_PYTHON_BIN:-/opt/homebrew/bin/python3}"
runtime_env_args=(
  --repo-root "$ROOT" \
  --runtime-base "$RUNTIME_BASE" \
  --output "$RUNTIME_ROOT/.env.market-tape"
)
if [[ "${MARKET_TAPE_ROTATE_CONTROL_TOKEN:-false}" == "true" ]]; then
  runtime_env_args+=(--rotate-control-token)
fi
"$PYTHON_BIN" "$ROOT/scripts/build_market_tape_runtime_env.py" \
  "${runtime_env_args[@]}"

"$PYTHON_BIN" -m compileall -q \
  "$RUNTIME_ROOT/market_tape_app.py" \
  "$RUNTIME_ROOT/services/market_tape" \
  "$RUNTIME_ROOT/services/middleware"

if [[ ! -f "$RUNTIME_DATA/market-tape.sqlite3" && -f "$ROOT/data/market-tape.sqlite3" ]]; then
  cp "$ROOT/data/market-tape.sqlite3" "$RUNTIME_DATA/market-tape.sqlite3"
fi
if [[ ! -d "$RUNTIME_DATA/market-tape-objects" && -d "$ROOT/data/market-tape-objects" ]]; then
  /usr/bin/ditto "$ROOT/data/market-tape-objects" "$RUNTIME_DATA/market-tape-objects"
fi
if [[ ! -f "$RUNTIME_DATA/market-tape-heartbeat.json" && -f "$ROOT/data/market-tape-heartbeat.json" ]]; then
  cp "$ROOT/data/market-tape-heartbeat.json" "$RUNTIME_DATA/market-tape-heartbeat.json"
fi

labels=(
  com.isaiah.content-intelligence.api
  com.isaiah.content-intelligence.market-tape
  com.isaiah.content-intelligence.market-tape-dataset
)

for label in "${labels[@]}"; do
  destination="$AGENTS/$label.plist"
  cp "$ROOT/ops/$label.plist" "$destination"
done

for label in "${labels[@]}"; do
  destination="$AGENTS/$label.plist"
  launchctl bootout "gui/$(id -u)/$label" >/dev/null 2>&1 || true
done

# launchd can briefly retain a just-removed label and return EIO on immediate bootstrap.
sleep 1

bootstrap_agent() {
  local label="$1"
  local destination="$AGENTS/$label.plist"
  local registered=false
  for attempt in 1 2 3 4 5; do
    if launchctl bootstrap "gui/$(id -u)" "$destination"; then
      registered=true
      break
    fi
    sleep "$attempt"
  done
  if [[ "$registered" != "true" ]]; then
    echo "Unable to register $label after 5 attempts." >&2
    exit 1
  fi
  launchctl enable "gui/$(id -u)/$label"
  launchctl kickstart "gui/$(id -u)/$label"
}

bootstrap_agent com.isaiah.content-intelligence.api
api_ready=false
for _attempt in {1..30}; do
  if /usr/bin/curl --fail --silent --max-time 2 \
    http://127.0.0.1:6006/health >/dev/null; then
    api_ready=true
    break
  fi
  sleep 1
done
if [[ "$api_ready" != "true" ]]; then
  echo "Market Tape API did not become healthy before dependent agents started." >&2
  exit 1
fi

bootstrap_agent com.isaiah.content-intelligence.market-tape
bootstrap_agent com.isaiah.content-intelligence.market-tape-dataset

echo "Market Tape scheduler, daily dataset certifier, and local API installed."
echo "Runtime: $RUNTIME_ROOT"
echo "Data: $RUNTIME_DATA"
