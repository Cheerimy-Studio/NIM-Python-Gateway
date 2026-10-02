#!/usr/bin/env bash
# Start NVIDIA NIM Gateway (Linux/macOS). Requires Python 3.10+.
set -e
cd "$(dirname "$0")"
mkdir -p data
exec python -m uvicorn server:app --host 0.0.0.0 --port "${PORT:-8080}"
