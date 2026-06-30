#!/bin/sh
set -eu

: "${AX_LLM_MODEL_DIR:=/models/llm/Qwen3-0.6B}"
: "${AX_LLM_PORT:=8001}"
: "${AX_MSP_DIR:=/soc}"
: "${AX_LLM_SERVER_TIMEOUT_MS:=300000}"

if [ ! -x /usr/local/bin/axllm ]; then
  echo "axllm binary not executable: /usr/local/bin/axllm" >&2
  exit 1
fi

if [ ! -d "$AX_LLM_MODEL_DIR" ]; then
  echo "LLM model path does not exist: $AX_LLM_MODEL_DIR" >&2
  exit 1
fi

export LD_LIBRARY_PATH="${AX_MSP_DIR}/lib:${LD_LIBRARY_PATH:-}"

exec axllm serve "$AX_LLM_MODEL_DIR" \
  --port "$AX_LLM_PORT" \
  --server_timeout_ms "$AX_LLM_SERVER_TIMEOUT_MS"
