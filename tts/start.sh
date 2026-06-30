#!/bin/sh
set -eu

if [ -x /opt/ax_tts_api/install/ax650/tts_server ]; then
  /opt/ax_tts_api/install/ax650/tts_server \
    --port 8081 \
    --model_path "${AX_TTS_MODEL_PATH}" &
else
  echo "tts_server binary not found; build AXERA-TECH/ax_tts_api for AX650 before running this image." >&2
fi

exec python /app/wyoming_adapter.py \
  --uri tcp://0.0.0.0:10200 \
  --api-url "${AX_TTS_HTTP_URL}" \
  --model "${AX_TTS_MODEL}" \
  --language "${AX_TTS_LANGUAGE}" \
  --voice "${AX_TTS_VOICE}"
