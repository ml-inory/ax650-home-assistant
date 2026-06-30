# Home Assistant Voice on AX650

A local Home Assistant Assist voice stack for AX650 boards.

This project mirrors the service layout from `rk3576-home-assistant-voice`, but replaces the RKNN/RKLLM pieces with AX650 services:

- Speech-to-text with AXERA `ax_asr_api`, wrapped as Wyoming on `10300`
- Text-to-speech with AXERA `ax_tts_api`, wrapped as Wyoming on `10200`
- Wake-word detection with `rhasspy/wyoming-openwakeword` on `10400`
- Local conversation handling with AXERA `axllm`, OpenAI-compatible API on `8001`
- Optional Home Assistant container profile

## What You Need

- AX650 board running Linux ARM64
- Docker Engine with the Docker Compose plugin
- Access to AX650 device nodes through `/dev`
- Model files under `models/`, downloaded by scripts or copied manually
- Home Assistant on the same network, unless using the optional profile

The first implementation is a reviewable scaffold with locally tested Wyoming adapters. Real AX650 image/runtime validation is a follow-up step.

## Quick Start

Prepare models on the AX650 board:

```bash
bash scripts/download_asr_models.sh
bash scripts/download_tts_models.sh
bash scripts/download_llm_models.sh
```

Start only the voice stack:

```bash
sudo docker compose up -d --build
```

Start the voice stack plus Home Assistant:

```bash
sudo docker compose --profile homeassistant up -d --build
```

Check status:

```bash
sudo docker compose ps
sudo docker compose logs -f
```

Run a service-surface smoke check from the machine that can reach the stack:

```bash
python scripts/smoke_check.py --host AX650_BOARD_IP
```

## Services

| Service | Purpose | Port |
| --- | --- | ---: |
| ASR | Wyoming STT adapter for `ax_asr_api` | `10300` |
| TTS | Wyoming TTS adapter for `ax_tts_api` | `10200` |
| openWakeWord | Wyoming wake-word detection | `10400` |
| LLM | OpenAI-compatible `axllm serve` API | `8001` |

## Configure Home Assistant

Add Wyoming integrations with the AX650 board IP:

| Service | Host | Port |
| --- | --- | ---: |
| AX650 ASR | AX650 board IP | `10300` |
| AX650 TTS | AX650 board IP | `10200` |
| openWakeWord | AX650 board IP | `10400` |

For local conversation, add a Local LLM/OpenAI-compatible integration with:

```text
API hostname: AX650 board IP
API port: 8001
API path: /v1
API key: sk-local
Model name: axllm-model
```

Then create or edit an Assist pipeline and select the AX650 STT, TTS, wake-word, and local conversation services.

## Models

Model artifacts are not committed. The compose file bind-mounts:

```text
models/asr  -> /models/asr
models/tts  -> /models/tts
models/llm  -> /models/llm
```

Defaults:

- ASR: `sensevoice`, language `auto`
- TTS: `kokoro`, language `zh`, voice `jm_kumo`
- LLM: `AXERA-TECH/Qwen3-0.6B`, expected at `models/llm/Qwen3-0.6B`

## Runtime Contract

The ASR and TTS containers run two processes:

1. the upstream AXERA HTTP server on localhost
2. the Wyoming adapter exposed to Home Assistant

Startup fails fast if the upstream server binary or model path is missing. Override paths and ports with environment variables:

| Service | Variable | Default |
| --- | --- | --- |
| ASR | `AX_ASR_SERVER_BIN` | `/opt/ax_asr_api/install/ax650/asr_server` |
| ASR | `AX_ASR_SERVER_PORT` | `8080` |
| ASR | `AX_ASR_MODEL_PATH` | `/models/asr` |
| ASR | `AX_ASR_ADAPTER_URI` | `tcp://0.0.0.0:10300` |
| TTS | `AX_TTS_SERVER_BIN` | `/opt/ax_tts_api/install/ax650/tts_server` |
| TTS | `AX_TTS_SERVER_PORT` | `8081` |
| TTS | `AX_TTS_MODEL_PATH` | `/models/tts` |
| TTS | `AX_TTS_ADAPTER_URI` | `tcp://0.0.0.0:10200` |

For adapter-only debugging against an already running HTTP server, set `AX_ASR_ADAPTER_ONLY=1` or `AX_TTS_ADAPTER_ONLY=1` and point `AX_ASR_HTTP_URL` or `AX_TTS_HTTP_URL` at that server.

## Development Checks

Install local test dependencies:

```bash
python -m pip install -r requirements-dev.txt
```

Run validation:

```bash
python -m pytest -q
docker compose config
```

Run smoke checks against a local or board-side stack:

```bash
python scripts/smoke_check.py --host 127.0.0.1
python scripts/smoke_check.py --host AX650_BOARD_IP
```

The smoke check verifies ASR/TTS HTTP health endpoints, LLM HTTP health or model listing, and the three Wyoming TCP ports.

## Troubleshooting

If ASR or TTS logs say the `asr_server` or `tts_server` binary is missing, build the corresponding AXERA upstream project for AX650 inside the image or replace the scaffold with a prebuilt runtime artifact:

- `https://github.com/AXERA-TECH/ax_asr_api`
- `https://github.com/AXERA-TECH/ax_tts_api`

If the LLM does not answer, confirm the model path exists and the API responds:

```bash
curl http://AX650_IP:8001/v1/models
curl http://AX650_IP:8001/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{"model":"axllm-model","messages":[{"role":"user","content":"你好"}]}'
```
