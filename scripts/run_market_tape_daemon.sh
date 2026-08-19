#!/bin/zsh
set -eo pipefail

ROOT="${0:A:h:h}"
set -a
for env_file in \
  "${MARKET_TAPE_ENV_FILE:-}" \
  "$ROOT/../actp-worker/.env" \
  "$ROOT/.env" \
  "$ROOT/.env.production" \
  "$ROOT/.env.market-tape"; do
  if [[ -n "$env_file" && -f "$env_file" ]]; then
    source "$env_file"
  fi
done
set +a
set -u

PYTHON_BIN="${MARKET_TAPE_PYTHON_BIN:-/opt/homebrew/bin/python3}"
if [[ ! -x "$PYTHON_BIN" ]]; then
  PYTHON_BIN="$(command -v python3)"
fi

cd /tmp
exec "$PYTHON_BIN" -P "$ROOT/scripts/market_tape_daemon_entry.py"
