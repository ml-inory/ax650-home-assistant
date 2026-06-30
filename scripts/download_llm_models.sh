#!/bin/sh
set -eu

TARGET_DIR="${1:-models/llm/Qwen3-0.6B}"
HF_ENDPOINT="${HF_ENDPOINT:-https://hf-mirror.com}"

mkdir -p "$TARGET_DIR"
export HF_ENDPOINT

python -m pip install -U huggingface_hub
hf download AXERA-TECH/Qwen3-0.6B --local-dir "$TARGET_DIR"

echo "LLM model installed under $TARGET_DIR"
