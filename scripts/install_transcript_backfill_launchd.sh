#!/bin/zsh
set -euo pipefail

ROOT="${0:A:h:h}"
LABEL="com.isaiah.content-intelligence.transcript-backfill"
AGENT="$HOME/Library/LaunchAgents/$LABEL.plist"
STORAGE_ROOT="/Volumes/My Passport/MarketTape/transcript-bank"
RUNTIME_ROOT="$HOME/Library/Application Support/ContentQuality/runtime"
RUNTIME_PARENT="$HOME/Library/Application Support/ContentQuality"
PYTHON_BIN="/opt/homebrew/bin/python3"

if [[ ! -x "$PYTHON_BIN" ]]; then
  echo "Transcript Python runtime is unavailable: $PYTHON_BIN" >&2
  exit 1
fi
if ! "$PYTHON_BIN" -c \
  'import os, sys; sys.exit(0 if os.path.ismount("/Volumes/My Passport") else 1)'; then
  echo "External transcript storage is not a mounted filesystem: /Volumes/My Passport" >&2
  exit 1
fi
for command_name in yt-dlp ffmpeg; do
  if ! command -v "$command_name" >/dev/null 2>&1; then
    echo "Required transcript executable is unavailable: $command_name" >&2
    exit 1
  fi
done
if ! "$PYTHON_BIN" -c 'import whisper' >/dev/null 2>&1; then
  echo "Required Python package is unavailable: openai-whisper" >&2
  exit 1
fi
if /usr/bin/pgrep -f "$RUNTIME_ROOT/scripts/backfill_transcript_bank.py" >/dev/null 2>&1; then
  echo "Transcript backfill is active; refusing to replace its runtime" >&2
  exit 75
fi

mkdir -p "$HOME/Library/LaunchAgents" "$STORAGE_ROOT/_tmp" "$RUNTIME_PARENT"
STAGING_CONTAINER="$(mktemp -d "$RUNTIME_PARENT/.transcript-stage.XXXXXX")"
BACKUP_CONTAINER="$(mktemp -d "$RUNTIME_PARENT/.transcript-backup.XXXXXX")"
STAGED_RUNTIME="$STAGING_CONTAINER/runtime"
STAGED_AGENT="$STAGING_CONTAINER/$LABEL.plist"

cleanup_container() {
  local target="$1"
  case "$target" in
    "$RUNTIME_PARENT"/.transcript-stage.*|"$RUNTIME_PARENT"/.transcript-backup.*)
      /bin/rm -rf -- "$target"
      ;;
    *)
      echo "Refusing unsafe installer cleanup target: $target" >&2
      return 1
      ;;
  esac
}

mkdir -p \
  "$STAGED_RUNTIME/scripts" \
  "$STAGED_RUNTIME/services/content_quality" \
  "$STAGED_RUNTIME/services/market_tape"
cp "$ROOT/services/__init__.py" "$STAGED_RUNTIME/services/__init__.py"
/usr/bin/ditto "$ROOT/services/content_quality" "$STAGED_RUNTIME/services/content_quality"
/usr/bin/ditto "$ROOT/services/market_tape" "$STAGED_RUNTIME/services/market_tape"
cp "$ROOT/scripts/backfill_transcript_bank.py" "$STAGED_RUNTIME/scripts/backfill_transcript_bank.py"
cp "$ROOT/ops/$LABEL.plist" "$STAGED_AGENT"
/usr/bin/plutil -lint "$STAGED_AGENT" >/dev/null

if ! (
  cd "$STAGED_RUNTIME"
  "$PYTHON_BIN" -c \
    'from services.content_quality.transcript_bank import TranscriptBank; from services.market_tape.source_urls import is_usable_source_url'
); then
  cleanup_container "$STAGING_CONTAINER"
  cleanup_container "$BACKUP_CONTAINER"
  echo "Staged transcript runtime import preflight failed; live runtime unchanged" >&2
  exit 1
fi

if /usr/bin/pgrep -f "$RUNTIME_ROOT/scripts/backfill_transcript_bank.py" >/dev/null 2>&1; then
  cleanup_container "$STAGING_CONTAINER"
  cleanup_container "$BACKUP_CONTAINER"
  echo "Transcript backfill started during staging; live runtime unchanged" >&2
  exit 75
fi

HAD_RUNTIME=0
HAD_AGENT=0
if [[ -d "$RUNTIME_ROOT" ]]; then
  HAD_RUNTIME=1
fi
if [[ -f "$AGENT" ]]; then
  cp "$AGENT" "$BACKUP_CONTAINER/agent.plist"
  HAD_AGENT=1
fi

launchctl bootout "gui/$(id -u)/$LABEL" >/dev/null 2>&1 || true
if /usr/bin/pgrep -f "$RUNTIME_ROOT/scripts/backfill_transcript_bank.py" >/dev/null 2>&1; then
  launchctl bootstrap "gui/$(id -u)" "$AGENT" >/dev/null 2>&1 || true
  cleanup_container "$STAGING_CONTAINER"
  cleanup_container "$BACKUP_CONTAINER"
  echo "Transcript backfill remained active after unload; live runtime unchanged" >&2
  exit 75
fi

if (( HAD_RUNTIME )) && ! mv "$RUNTIME_ROOT" "$BACKUP_CONTAINER/runtime"; then
  if (( HAD_AGENT )); then
    launchctl bootstrap "gui/$(id -u)" "$AGENT" >/dev/null 2>&1 || true
  fi
  cleanup_container "$STAGING_CONTAINER"
  cleanup_container "$BACKUP_CONTAINER"
  echo "Could not stage the previous runtime for rollback; live runtime unchanged" >&2
  exit 1
fi

INSTALL_OK=1
mv "$STAGED_RUNTIME" "$RUNTIME_ROOT" || INSTALL_OK=0
if (( INSTALL_OK )); then
  cp "$STAGED_AGENT" "$AGENT.next" || INSTALL_OK=0
fi
if (( INSTALL_OK )); then
  mv "$AGENT.next" "$AGENT" || INSTALL_OK=0
fi
if (( INSTALL_OK )); then
  launchctl bootstrap "gui/$(id -u)" "$AGENT" || INSTALL_OK=0
fi
if (( INSTALL_OK )); then
  launchctl enable "gui/$(id -u)/$LABEL" || INSTALL_OK=0
fi
if (( INSTALL_OK )); then
  launchctl kickstart -k "gui/$(id -u)/$LABEL" || INSTALL_OK=0
fi

if (( ! INSTALL_OK )); then
  launchctl bootout "gui/$(id -u)/$LABEL" >/dev/null 2>&1 || true
  /bin/rm -f -- "$AGENT.next"
  if [[ -d "$RUNTIME_ROOT" ]]; then
    mv "$RUNTIME_ROOT" "$STAGING_CONTAINER/failed-runtime"
  fi
  if (( HAD_RUNTIME )); then
    mv "$BACKUP_CONTAINER/runtime" "$RUNTIME_ROOT"
  fi
  if (( HAD_AGENT )); then
    cp "$BACKUP_CONTAINER/agent.plist" "$AGENT"
    launchctl bootstrap "gui/$(id -u)" "$AGENT" >/dev/null 2>&1 || true
  else
    /bin/rm -f -- "$AGENT" "$AGENT.next"
  fi
  cleanup_container "$STAGING_CONTAINER"
  cleanup_container "$BACKUP_CONTAINER"
  echo "Transcript runtime install failed and the previous runtime was restored" >&2
  exit 1
fi

cleanup_container "$STAGING_CONTAINER"
cleanup_container "$BACKUP_CONTAINER"

echo "Transcript backfill installed: $LABEL"
echo "Runtime: $RUNTIME_ROOT"
echo "Storage: $STORAGE_ROOT"
echo "Logs: /tmp/content-intelligence-transcript-backfill{,.error}.log"
