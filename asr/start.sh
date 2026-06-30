#!/bin/sh
set -eu

: "${AX_ASR_SERVER_BIN:=/opt/ax_asr_api/install/ax650/asr_server}"
: "${AX_ASR_SERVER_PORT:=8080}"
: "${AX_ASR_MODEL_PATH:=/models/asr}"
: "${AX_ASR_HTTP_URL:=http://127.0.0.1:${AX_ASR_SERVER_PORT}}"
: "${AX_ASR_MODEL:=sensevoice}"
: "${AX_ASR_LANGUAGE:=auto}"
: "${AX_ASR_ADAPTER_URI:=tcp://0.0.0.0:10300}"
: "${AX_ASR_WAIT_TIMEOUT:=30}"
: "${AX_ASR_ADAPTER_ONLY:=0}"
: "${AX_ASR_BUILD_IF_MISSING:=1}"
: "${AX_ASR_BUILD_SCRIPT:=/app/build_server.sh}"

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

if [ "$AX_ASR_ADAPTER_ONLY" != "1" ]; then
  if [ ! -x "$AX_ASR_SERVER_BIN" ] && [ "$AX_ASR_BUILD_IF_MISSING" = "1" ]; then
    "$AX_ASR_BUILD_SCRIPT"
  fi

  if [ ! -x "$AX_ASR_SERVER_BIN" ]; then
    echo "ASR server binary not executable: $AX_ASR_SERVER_BIN" >&2
    echo "Set AX_ASR_SERVER_BIN or build/download AXERA-TECH/ax_asr_api for AX650." >&2
    exit 1
  fi

  if [ ! -d "$AX_ASR_MODEL_PATH" ]; then
    echo "ASR model path does not exist: $AX_ASR_MODEL_PATH" >&2
    exit 1
  fi

  "$AX_ASR_SERVER_BIN" \
    --port "$AX_ASR_SERVER_PORT" \
    --model_path "$AX_ASR_MODEL_PATH" &
  server_pid=$!
  trap 'kill "$server_pid" 2>/dev/null || true' EXIT INT TERM
  wait_for_http "${AX_ASR_HTTP_URL%/}/healthz" "$AX_ASR_WAIT_TIMEOUT"
fi

exec python /app/wyoming_adapter.py \
  --uri "$AX_ASR_ADAPTER_URI" \
  --api-url "${AX_ASR_HTTP_URL}" \
  --model "${AX_ASR_MODEL}" \
  --language "${AX_ASR_LANGUAGE}"
