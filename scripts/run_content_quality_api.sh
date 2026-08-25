#!/bin/zsh
set -euo pipefail

export HOME="/Users/isaiahdupree"
export PATH="/opt/homebrew/bin:/usr/bin:/bin:/usr/sbin:/sbin"
print -u2 "content-quality-start: init"

RUNTIME_ROOT="$HOME/Library/Application Support/ContentQuality/runtime"
CONTENT_INTELLIGENCE_RUNTIME="$HOME/Library/Application Support/ContentIntelligence/runtime"

if [[ -f "$RUNTIME_ROOT/.env.content-quality" ]]; then
  set -a
  source "$RUNTIME_ROOT/.env.content-quality"
  set +a
fi
print -u2 "content-quality-start: runtime-config"

if [[ -n "${CONTENT_QUALITY_CREDENTIAL_ENV_FILE:-}" && -f "$CONTENT_QUALITY_CREDENTIAL_ENV_FILE" ]]; then
  OPENAI_API_KEY="$(/opt/homebrew/bin/python3 - "$CONTENT_QUALITY_CREDENTIAL_ENV_FILE" <<'PY'
from pathlib import Path
from dotenv import dotenv_values
import sys

value = str(
    dotenv_values(Path(sys.argv[1]).expanduser()).get("OPENAI_API_KEY") or ""
).strip()
if value and not value.startswith("__"):
    print(value, end="")
PY
)"
  if [[ -n "$OPENAI_API_KEY" ]]; then
    export OPENAI_API_KEY
  else
    unset OPENAI_API_KEY
  fi
fi
print -u2 "content-quality-start: credential-config"

if [[ -f "$CONTENT_INTELLIGENCE_RUNTIME/.env.market-tape" ]]; then
  set -a
  source "$CONTENT_INTELLIGENCE_RUNTIME/.env.market-tape"
  set +a
fi
print -u2 "content-quality-start: tape-config"

export MARKET_TAPE_DB="${MARKET_TAPE_DB:-$HOME/Library/Application Support/ContentIntelligence/data/market-tape.sqlite3}"
export CONTENT_QUALITY_DB="${CONTENT_QUALITY_DB:-$HOME/Library/Application Support/ContentQuality/data/content-quality.sqlite3}"

cd "$RUNTIME_ROOT"
print -u2 "content-quality-start: exec"
exec /opt/homebrew/bin/python3 content_quality_server.py --host 127.0.0.1 --port 6010
