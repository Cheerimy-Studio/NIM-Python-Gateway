#!/usr/bin/env bash
# 启动 NVIDIA NIM 网关（Linux/macOS）。依赖 Python 3.10+。
set -e
cd "$(dirname "$0")"
mkdir -p data
exec python -m uvicorn server:app --host 0.0.0.0 --port "${PORT:-8080}"
