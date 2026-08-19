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
cp "$ROOT/scripts/run_content_quality_api.sh" "$RUNTIME_ROOT/run_content_quality_api.sh"
chmod +x "$RUNTIME_ROOT/run_content_quality_api.sh"
cp "$ROOT/ops/$LABEL.plist" "$AGENT"

launchctl bootout "gui/$(id -u)/$LABEL" >/dev/null 2>&1 || true
sleep 1
launchctl bootstrap "gui/$(id -u)" "$AGENT"
launchctl enable "gui/$(id -u)/$LABEL"
launchctl kickstart -k "gui/$(id -u)/$LABEL"

echo "Content Quality API installed at http://127.0.0.1:6010"
