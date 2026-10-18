#!/bin/zsh
set -euo pipefail

ROOT="${0:A:h:h}"
LABEL="com.isaiah.content-quality.api"
AGENT="$HOME/Library/LaunchAgents/$LABEL.plist"
RUNTIME_BASE="$HOME/Library/Application Support/ContentQuality"
RUNTIME_ROOT="$RUNTIME_BASE/runtime"

mkdir -p "$HOME/Library/LaunchAgents" "$RUNTIME_ROOT/services/content_quality" "$RUNTIME_BASE/data"
cp "$ROOT/content_quality_server.py" "$RUNTIME_ROOT/content_quality_server.py"
cp "$ROOT/services/__init__.py" "$RUNTIME_ROOT/services/__init__.py"
/usr/bin/ditto "$ROOT/services/content_quality" "$RUNTIME_ROOT/services/content_quality"
/usr/bin/ditto "$ROOT/services/market_tape" "$RUNTIME_ROOT/services/market_tape"
cp "$ROOT/scripts/run_content_quality_api.sh" "$RUNTIME_ROOT/run_content_quality_api.sh"
chmod +x "$RUNTIME_ROOT/run_content_quality_api.sh"
cp "$ROOT/ops/$LABEL.plist" "$AGENT"

ENV_ARGS=(
  --output "$RUNTIME_ROOT/.env.content-quality"
)
if [[ -n "${CONTENT_QUALITY_CREDENTIAL_ENV_FILE:-}" ]]; then
  ENV_ARGS+=(--credential-env "$CONTENT_QUALITY_CREDENTIAL_ENV_FILE")
fi
/opt/homebrew/bin/python3 "$ROOT/scripts/build_content_quality_runtime_env.py" "${ENV_ARGS[@]}"

/opt/homebrew/bin/python3 -m compileall -q \
  "$RUNTIME_ROOT/content_quality_server.py" \
  "$RUNTIME_ROOT/services/content_quality" \
  "$RUNTIME_ROOT/services/market_tape"

launchctl bootout "gui/$(id -u)/$LABEL" >/dev/null 2>&1 || true
sleep 1
launchctl bootstrap "gui/$(id -u)" "$AGENT"
launchctl enable "gui/$(id -u)/$LABEL"
launchctl kickstart -k "gui/$(id -u)/$LABEL"

ready=false
# A cold first integrity sweep over the multi-GB Market Tape has measured up to
# 110s. Keep installation bounded while avoiding a false failure just before
# the first honest health snapshot becomes available.
for _attempt in {1..120}; do
  if /usr/bin/curl --fail --silent --max-time 2 \
    http://127.0.0.1:6010/health >/dev/null; then
    ready=true
    break
  fi
  sleep 1
done
if [[ "$ready" != "true" ]]; then
  echo "Content Quality API did not become healthy." >&2
  exit 1
fi

echo "Content Quality API installed at http://127.0.0.1:6010"
