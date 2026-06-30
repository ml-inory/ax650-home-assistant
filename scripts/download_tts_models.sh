#!/bin/sh
set -eu

TARGET_DIR="${1:-models/tts}"
HF_ENDPOINT="${HF_ENDPOINT:-https://hf-mirror.com}"
PYTHON="${PYTHON:-python3}"

mkdir -p "$TARGET_DIR/kokoro"
export HF_ENDPOINT

"$PYTHON" -m pip install -U huggingface_hub
hf download AXERA-TECH/kokoro.axera models/kokoro_part1_96.axmodel --local-dir "$TARGET_DIR/kokoro"
hf download AXERA-TECH/kokoro.axera models/kokoro_part2_96.axmodel --local-dir "$TARGET_DIR/kokoro"
hf download AXERA-TECH/kokoro.axera models/kokoro_part3_96.axmodel --local-dir "$TARGET_DIR/kokoro"
hf download AXERA-TECH/kokoro.axera models/model4_har_sim.onnx --local-dir "$TARGET_DIR/kokoro"

if [ -d "$TARGET_DIR/kokoro/models" ]; then
  mv "$TARGET_DIR/kokoro/models/"* "$TARGET_DIR/kokoro/"
  rmdir "$TARGET_DIR/kokoro/models"
fi

echo "TTS models installed under $TARGET_DIR/kokoro"
