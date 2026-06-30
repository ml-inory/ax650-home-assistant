#!/bin/sh
set -eu

: "${AX_TTS_SERVER_BIN:=/opt/ax_tts_api/install/ax650/tts_server}"
: "${AX_TTS_SERVER_PORT:=8081}"
: "${AX_TTS_MODEL_PATH:=/models/tts}"
: "${AX_TTS_HTTP_URL:=http://127.0.0.1:${AX_TTS_SERVER_PORT}}"
: "${AX_TTS_MODEL:=kokoro}"
: "${AX_TTS_LANGUAGE:=zh}"
: "${AX_TTS_VOICE:=jm_kumo}"
: "${AX_TTS_ADAPTER_URI:=tcp://0.0.0.0:10200}"
: "${AX_TTS_WAIT_TIMEOUT:=30}"
: "${AX_TTS_ADAPTER_ONLY:=0}"

wait_for_http() {
  url="$1"
  timeout="$2"
  elapsed=0
  while [ "$elapsed" -lt "$timeout" ]; do
    if curl -fsS "$url" >/dev/null 2>&1; then
      return 0
    fi
    sleep 1
    elapsed=$((elapsed + 1))
  done
  echo "Timed out waiting for $url after ${timeout}s" >&2
  return 1
}

if [ "$AX_TTS_ADAPTER_ONLY" != "1" ]; then
  if [ ! -x "$AX_TTS_SERVER_BIN" ]; then
    echo "TTS server binary not executable: $AX_TTS_SERVER_BIN" >&2
    echo "Set AX_TTS_SERVER_BIN or build/download AXERA-TECH/ax_tts_api for AX650." >&2
    exit 1
  fi

  if [ ! -d "$AX_TTS_MODEL_PATH" ]; then
    echo "TTS model path does not exist: $AX_TTS_MODEL_PATH" >&2
    exit 1
  fi

  "$AX_TTS_SERVER_BIN" \
    --port "$AX_TTS_SERVER_PORT" \
    --model_path "$AX_TTS_MODEL_PATH" &
  server_pid=$!
  trap 'kill "$server_pid" 2>/dev/null || true' EXIT INT TERM
  wait_for_http "${AX_TTS_HTTP_URL%/}/healthz" "$AX_TTS_WAIT_TIMEOUT"
fi

exec python /app/wyoming_adapter.py \
  --uri "$AX_TTS_ADAPTER_URI" \
  --api-url "${AX_TTS_HTTP_URL}" \
  --model "${AX_TTS_MODEL}" \
  --language "${AX_TTS_LANGUAGE}" \
  --voice "${AX_TTS_VOICE}"
