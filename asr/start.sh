#!/bin/sh
set -eu

if [ -x /opt/ax_asr_api/install/ax650/asr_server ]; then
  /opt/ax_asr_api/install/ax650/asr_server \
    --port 8080 \
    --model_path "${AX_ASR_MODEL_PATH}" &
else
  echo "asr_server binary not found; build AXERA-TECH/ax_asr_api for AX650 before running this image." >&2
fi

exec python /app/wyoming_adapter.py \
  --uri tcp://0.0.0.0:10300 \
  --api-url "${AX_ASR_HTTP_URL}" \
  --model "${AX_ASR_MODEL}" \
  --language "${AX_ASR_LANGUAGE}"
