#!/bin/zsh
set -euo pipefail

ROOT="${0:A:h:h}"
LABEL="com.isaiah.content-intelligence.transcript-backfill"
AGENT="$HOME/Library/LaunchAgents/$LABEL.plist"
STORAGE_ROOT="/Volumes/My Passport/MarketTape/transcript-bank"

if [[ ! -d "/Volumes/My Passport" ]]; then
  echo "External transcript storage is not mounted: /Volumes/My Passport" >&2
  exit 1
fi

mkdir -p "$HOME/Library/LaunchAgents" "$STORAGE_ROOT/_tmp"
cp "$ROOT/ops/$LABEL.plist" "$AGENT"

launchctl bootout "gui/$(id -u)/$LABEL" >/dev/null 2>&1 || true
sleep 1
launchctl bootstrap "gui/$(id -u)" "$AGENT"
launchctl enable "gui/$(id -u)/$LABEL"
launchctl kickstart -k "gui/$(id -u)/$LABEL"

echo "Transcript backfill installed: $LABEL"
echo "Storage: $STORAGE_ROOT"
echo "Logs: /tmp/content-intelligence-transcript-backfill{,.error}.log"
