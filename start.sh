#!/bin/bash
# Start Content Intelligence Service
# Port: 6006

cd "$(dirname "$0")"

# Load environment
if [ -f .env ]; then
    export $(cat .env | grep -v '^#' | xargs)
fi

# Set default port
export PORT=${PORT:-6006}

echo "🧠 Starting content-intelligence on port $PORT..."

# Check for API keys
if [ -z "$OPENAI_API_KEY" ]; then
    echo "⚠️  Warning: OPENAI_API_KEY not set - some AI features may not work"
fi

if [ -z "$GROQ_API_KEY" ]; then
    echo "⚠️  Warning: GROQ_API_KEY not set - fallback to OpenAI only"
fi

# Start the service
python app.py
