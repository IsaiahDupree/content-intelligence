#!/bin/zsh
set -euo pipefail

ROOT="${0:A:h:h}"
LABEL="com.isaiah.content-intelligence.visual-backfill"
AGENT="$HOME/Library/LaunchAgents/$LABEL.plist"
RUNTIME_ROOT="$HOME/Library/Application Support/ContentQuality/runtime"

mkdir -p "$HOME/Library/LaunchAgents" "$RUNTIME_ROOT/scripts" "$RUNTIME_ROOT/services/content_quality" "$RUNTIME_ROOT/services/market_tape"
cp "$ROOT/services/__init__.py" "$RUNTIME_ROOT/services/__init__.py"
/usr/bin/ditto "$ROOT/services/content_quality" "$RUNTIME_ROOT/services/content_quality"
/usr/bin/ditto "$ROOT/services/market_tape" "$RUNTIME_ROOT/services/market_tape"
cp "$ROOT/scripts/extract_visual_cohort.py" "$RUNTIME_ROOT/scripts/extract_visual_cohort.py"
cp "$ROOT/ops/$LABEL.plist" "$AGENT"

launchctl bootout "gui/$(id -u)/$LABEL" >/dev/null 2>&1 || true
sleep 1
launchctl bootstrap "gui/$(id -u)" "$AGENT"
launchctl enable "gui/$(id -u)/$LABEL"
launchctl kickstart -k "gui/$(id -u)/$LABEL"

echo "Visual backfill installed: $LABEL"
echo "Logs: /tmp/content-intelligence-visual-backfill{,.error}.log"
