#!/bin/bash
cd "$(dirname "$0")" || exit 1
export CORS_ALLOW_ORIGIN="http://localhost:5173;http://localhost:5174;http://localhost:51374;http://127.0.0.1:5173;http://127.0.0.1:5174;http://127.0.0.1:51374"
PORT="${PORT:-8080}"
if [ -z "$WEBUI_SECRET_KEY" ]; then
  export WEBUI_SECRET_KEY=$(openssl rand -base64 32)
fi
python -m uvicorn avexie.main:app --port $PORT --host 127.0.0.1 --forwarded-allow-ips "127.0.0.1" --ws-per-message-deflate "${UVICORN_WS_PER_MESSAGE_DEFLATE:-true}" --reload
