#!/bin/bash
# Stop Content Intelligence Service

echo "🛑 Stopping content-intelligence..."

# Find and kill the process
pkill -f "python app.py" 2>/dev/null || true
pkill -f "content-intelligence" 2>/dev/null || true

# Kill by port if still running
lsof -ti:6006 | xargs kill -9 2>/dev/null || true

echo "✅ content-intelligence stopped"
