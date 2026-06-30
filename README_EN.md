# AX650 Home Assistant Voice

[中文](README.md) | [English](README_EN.md)

This is a local Home Assistant Assist voice stack for AX650/AX650N boards. It mirrors the service layout from `rk3576-home-assistant-voice`, but replaces the RKNN/RKLLM runtime pieces with AXERA components:

- ASR: AXERA `ax_asr_api`, exposed as a Wyoming STT service on `10300`
- TTS: AXERA `ax_tts_api`, exposed as a Wyoming TTS service on `10200`
- Wake word: `wyoming-openwakeword`, exposed on `10400`
- Local conversation: AXERA `axllm`, exposed as an OpenAI-compatible API on `8001`
- Optional Home Assistant container profile

## Board Requirements

- AX650 or AX650N Linux ARM64 board
- Docker Engine and the Docker Compose v2 plugin, or `docker-compose`
- Board access to `/dev` and `/soc`; containers mount `/dev:/dev` and `/soc:/soc:ro`
- Python 3, pip, git, and curl
- Enough space for models and images; at least 8 GB free under the repository path is recommended
- Home Assistant on the same network, unless using the optional Home Assistant profile in this repository

Model artifacts and upstream source caches are not committed to git. By default they are written to:

```text
models/asr  -> /models/asr
models/tts  -> /models/tts
models/llm  -> /models/llm
vendor/ax_asr_api
vendor/ax_tts_api
vendor/axllm
```

## One-Click Install

After placing the repository on the board, run this from the repository root:

```bash
bash scripts/install_on_board.sh
```

The installer performs these steps:

1. Checks Docker, Compose, Python, git, curl, `/dev`, `/soc`, and free disk space.
2. Creates the `models/` and `vendor/` directories.
3. Fetches ASR/TTS upstream source caches to avoid repeated GitHub access during Docker builds.
4. Downloads ASR, TTS, and LLM models, using `https://hf-mirror.com` by default.
5. Builds and starts the ASR, TTS, openWakeWord, and LLM services.
6. Runs the public port smoke check.

Common options:

```bash
# Print commands without changing the filesystem
bash scripts/install_on_board.sh --dry-run

# Skip model downloads when models are already prepared
bash scripts/install_on_board.sh --skip-models

# Skip vendor source fetching when caches are already prepared
bash scripts/install_on_board.sh --skip-vendor

# Only check already running services without rebuilding or restarting
bash scripts/install_on_board.sh --validate-only

# Also start the Home Assistant profile from this repository
bash scripts/install_on_board.sh --with-homeassistant

# Lower the free-space requirement, in MB
bash scripts/install_on_board.sh --min-free-mb 4096
```

Common environment variables:

| Variable | Default | Description |
| --- | --- | --- |
| `HF_ENDPOINT` | `https://hf-mirror.com` | Hugging Face download endpoint |
| `PYTHON` | `python3` | Board-side Python command |
| `COMPOSE` | auto-detected | `docker compose` or `docker-compose` |
| `SMOKE_HOST` | `127.0.0.1` | Host passed to the smoke check |
| `SMOKE_TIMEOUT` | `3` | Per-check smoke timeout |
| `MIN_FREE_MB` | `8192` | Minimum free space under the repository path |
| `AX_LLM_RELEASE_URL` | Compose default | AX LLM release binary URL |

If board disk space is not enough, place the repository or `models/` directory on a host-mounted path and run the installer from that mounted directory.

## Manual Start

Without the one-click installer, run the steps manually:

```bash
bash scripts/fetch_upstream_sources.sh
bash scripts/download_asr_models.sh
bash scripts/download_tts_models.sh
bash scripts/download_llm_models.sh
docker compose up -d --build
```

Start the voice stack plus Home Assistant:

```bash
docker compose --profile homeassistant up -d --build
```

Check status:

```bash
docker compose ps
docker compose logs -f
python3 scripts/smoke_check.py --host 127.0.0.1 --timeout 3 --public-only
```

If the board only has the legacy `docker-compose` command, replace `docker compose` with `docker-compose`.

## Service Ports

| Service | Purpose | Port |
| --- | --- | ---: |
| ASR | Wyoming STT adapter for `ax_asr_api` | `10300` |
| TTS | Wyoming TTS adapter for `ax_tts_api` | `10200` |
| openWakeWord | Wyoming wake-word detection | `10400` |
| LLM | OpenAI-compatible `axllm serve` API | `8001` |

## Home Assistant Setup

Add Wyoming integrations in Home Assistant:

| Integration | Host | Port |
| --- | --- | ---: |
| AX650 ASR | AX650 board IP | `10300` |
| AX650 TTS | AX650 board IP | `10200` |
| openWakeWord | AX650 board IP | `10400` |

Configure the local conversation service as an OpenAI-compatible API:

```text
API hostname: AX650 board IP
API port: 8001
API path: /v1
API key: sk-local
Model name: axllm-model
```

Then create or edit an Assist pipeline and select the AX650 STT, TTS, wake-word, and local conversation services.

## Default Models

| Module | Default model/path |
| --- | --- |
| ASR | `sensevoice`, language `auto`, path `models/asr/sensevoice` |
| TTS | `kokoro`, language `zh`, voice `zf_xiaoxiao`, path `models/tts/kokoro` |
| LLM | `AXERA-TECH/Qwen3-0.6B`, path `models/llm/Qwen3-0.6B` |

## Runtime Variables

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
| LLM | `AX_LLM_MODEL_DIR` | `/models/llm/Qwen3-0.6B` |
| LLM | `AX_LLM_PORT` | `8001` |
| LLM | `AX_LLM_RELEASE_URL` | `https://github.com/AXERA-TECH/ax-llm/releases/download/latest/axllm-ax650-linux-arm64` |

For adapter-only debugging against already running ASR/TTS HTTP services, set `AX_ASR_ADAPTER_ONLY=1` or `AX_TTS_ADAPTER_ONLY=1`, then point `AX_ASR_HTTP_URL` or `AX_TTS_HTTP_URL` to the corresponding service.

## Local Development Checks

Install test dependencies:

```bash
python -m pip install -r requirements-dev.txt
```

Run validation:

```bash
python -m pytest -q
docker-compose config
sh -n scripts/install_on_board.sh
```

Run a smoke check against an already started board-side stack:

```bash
python3 scripts/smoke_check.py --host AX650_BOARD_IP --timeout 3 --public-only
```

## Troubleshooting

- `/soc` does not exist: confirm the command is running on an AX650 board and that the board system provides the AX MSP runtime.
- Docker image pulls are slow or fail: the default compose file uses DaoCloud Python/Debian images and USTC apt mirrors; if it still fails, check board network and DNS first.
- ASR/TTS reports missing `asr_server` or `tts_server`: confirm `vendor/ax_asr_api` and `vendor/ax_tts_api` are fetched. Containers will try to compile them on the board at startup.
- Models are missing: rerun the corresponding download script or manually place assets under `models/asr`, `models/tts`, and `models/llm`.
- LLM does not respond: check that `models/llm/Qwen3-0.6B` is complete, then try:

```bash
curl http://AX650_IP:8001/v1/models
curl http://AX650_IP:8001/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{"model":"axllm-model","messages":[{"role":"user","content":"你好"}]}'
```
