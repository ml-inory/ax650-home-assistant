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
hf download AXERA-TECH/kokoro.axera cpp/dict/vocab.txt --local-dir "$TARGET_DIR/kokoro"
hf download AXERA-TECH/kokoro.axera --include 'cpp/voices/*.bin' --local-dir "$TARGET_DIR/kokoro"

if [ -d "$TARGET_DIR/kokoro/models" ]; then
  mv "$TARGET_DIR/kokoro/models/"* "$TARGET_DIR/kokoro/"
  rmdir "$TARGET_DIR/kokoro/models"
fi

if [ -f "$TARGET_DIR/kokoro/cpp/dict/vocab.txt" ]; then
  mv "$TARGET_DIR/kokoro/cpp/dict/vocab.txt" "$TARGET_DIR/kokoro/vocab.txt"
fi

if [ -d "$TARGET_DIR/kokoro/cpp/voices" ]; then
  rm -rf "$TARGET_DIR/kokoro/voices"
  mv "$TARGET_DIR/kokoro/cpp/voices" "$TARGET_DIR/kokoro/voices"
fi

if [ -d "$TARGET_DIR/kokoro/cpp" ]; then
  rm -rf "$TARGET_DIR/kokoro/cpp"
fi

echo "TTS models installed under $TARGET_DIR/kokoro"
