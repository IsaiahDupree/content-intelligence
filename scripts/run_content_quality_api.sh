#!/bin/zsh
set -euo pipefail

RUNTIME_ROOT="$HOME/Library/Application Support/ContentQuality/runtime"
CONTENT_INTELLIGENCE_RUNTIME="$HOME/Library/Application Support/ContentIntelligence/runtime"

if [[ -f "$RUNTIME_ROOT/.env.content-quality" ]]; then
  set -a
  source "$RUNTIME_ROOT/.env.content-quality"
  set +a
fi

if [[ -n "${CONTENT_QUALITY_CREDENTIAL_ENV_FILE:-}" && -f "$CONTENT_QUALITY_CREDENTIAL_ENV_FILE" ]]; then
  set -a
  source "$CONTENT_QUALITY_CREDENTIAL_ENV_FILE"
  set +a
fi

if [[ -f "$CONTENT_INTELLIGENCE_RUNTIME/.env.market-tape" ]]; then
  set -a
  source "$CONTENT_INTELLIGENCE_RUNTIME/.env.market-tape"
  set +a
fi

export MARKET_TAPE_DB="${MARKET_TAPE_DB:-$HOME/Library/Application Support/ContentIntelligence/data/market-tape.sqlite3}"
export CONTENT_QUALITY_DB="${CONTENT_QUALITY_DB:-$HOME/Library/Application Support/ContentQuality/data/content-quality.sqlite3}"

cd "$RUNTIME_ROOT"
exec /opt/homebrew/bin/python3 content_quality_server.py --host 127.0.0.1 --port 6010
