#!/bin/sh
set -eu

TARGET_DIR="${1:-models/asr}"
HF_ENDPOINT="${HF_ENDPOINT:-https://hf-mirror.com}"

mkdir -p "$TARGET_DIR"
export HF_ENDPOINT

python -m pip install -U huggingface_hub
hf download AXERA-TECH/SenseVoice --local-dir "$TARGET_DIR/SenseVoice"

mkdir -p "$TARGET_DIR/sensevoice"
cp -rf "$TARGET_DIR/SenseVoice/sensevoice_ax650/." "$TARGET_DIR/sensevoice/"
rm -rf "$TARGET_DIR/SenseVoice"

echo "ASR models installed under $TARGET_DIR/sensevoice"
