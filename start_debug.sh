#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

# Create virtualenv if it doesn't exist
if [ ! -d ".venv" ]; then
  echo "Creating virtual environment..."
  python3 -m venv .venv
fi

# Install / sync dependencies
echo "Installing dependencies..."
.venv/bin/pip install --quiet -r requirements.txt

# Load .env if present
if [ -f ".env" ]; then
  echo "Loading .env..."
  set -a
  source .env
  set +a
fi

echo "Starting Claude Code Proxy (debug) on port 8082..."
export DEBUG_MODE=true
export DEBUG_LOG_DIR="${DEBUG_LOG_DIR:-logs}"

exec .venv/bin/uvicorn main:app \
  --host 0.0.0.0 \
  --port 8082 \
  --reload \
  --reload-exclude "${DEBUG_LOG_DIR:-logs}/*" \
  --log-level debug
