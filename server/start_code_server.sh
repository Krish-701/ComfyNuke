#!/usr/bin/env bash
# Start ComfyNuke read-only code server on port 6000 (Ubuntu hub).
# ComfyUI itself should already run on :8188 separately.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
HOST="${COMFYNUKE_CODE_HOST:-0.0.0.0}"
PORT="${COMFYNUKE_CODE_PORT:-6000}"

cd "$ROOT"
echo "ComfyNuke root: $ROOT"
echo "Code HTTP:      http://${HOST}:${PORT}/"
echo "ComfyUI API:    http://192.168.91.13:8188  (separate process)"
exec python3 "$ROOT/server/serve_code.py" --root "$ROOT" --host "$HOST" --port "$PORT"
