#!/usr/bin/env bash
# Spin up the cc-mem web app in DEV mode: Python API + Vite hot-reload.
#   ./dev.sh
# Open http://localhost:5173  (Vite proxies /api to the Python backend :8765)
# Ctrl-C stops both.
set -e
HERE="$(cd "$(dirname "$0")" && pwd)"
cd "$HERE"

# Backend: venv python + local semantic embedder, against the real DB.
CC_MEM_EMBEDDER=local "$HERE/.venv/bin/python" manage.py --port 8765 &
API_PID=$!
trap 'kill $API_PID 2>/dev/null' EXIT

# Frontend: install once if needed, then hot-reload dev server.
cd "$HERE/ui"
[ -d node_modules ] || npm install
npm run dev
