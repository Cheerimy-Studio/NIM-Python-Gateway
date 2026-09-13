@echo off
rem 启动 NVIDIA NIM 网关（Windows）。依赖 Python 3.10+。
cd /d "%~dp0"

if not exist data mkdir data

set PYTHONPATH=%~dp0
python -m uvicorn server:app --host 0.0.0.0 --port 8080 >> data\server.log 2>&1
