#!/bin/sh
set -eu

usage() {
  cat <<'EOF'
Usage: bash scripts/install_on_board.sh [options]

Options:
  --dry-run              Print commands without executing them.
  --skip-models          Do not download ASR/TTS/LLM models.
  --skip-vendor          Do not fetch upstream ASR/TTS source caches.
  --skip-build           Do not run compose up --build.
  --validate-only        Only run compose ps and the public smoke check.
  --with-homeassistant   Start the optional Home Assistant compose profile.
  --min-free-mb MB       Required free space under the repo path. Default: 8192.
  --smoke-host HOST      Host passed to scripts/smoke_check.py. Default: 127.0.0.1.
  --smoke-timeout SEC    Timeout passed to scripts/smoke_check.py. Default: 3.
  -h, --help             Show this help.

Environment:
  HF_ENDPOINT            Hugging Face endpoint. Default: https://hf-mirror.com
  PYTHON                 Python command. Default: python3
  COMPOSE                Compose command. Auto-detected when unset.
  MIN_FREE_MB            Free-space threshold in MB. Default: 8192
EOF
}

log() {
  printf '[install] %s\n' "$*"
}

warn() {
  printf '[install] WARN: %s\n' "$*" >&2
}

die() {
  printf '[install] ERROR: %s\n' "$*" >&2
  exit 1
}

quote_arg() {
  # Single-quote shell arguments for readable dry-run output.
  printf "'%s'" "$(printf '%s' "$1" | sed "s/'/'\\\\''/g")"
}

run() {
  if [ "${DRY_RUN:-0}" = "1" ]; then
    printf '+'
    for arg in "$@"; do
      printf ' '
      quote_arg "$arg"
    done
    printf '\n'
    return 0
  fi
  "$@"
}

run_sh() {
  if [ "${DRY_RUN:-0}" = "1" ]; then
    printf '+ %s\n' "$*"
    return 0
  fi
  sh -c "$*"
}

have() {
  command -v "$1" >/dev/null 2>&1
}

detect_compose() {
  if [ -n "${COMPOSE:-}" ]; then
    return 0
  fi
  if docker compose version >/dev/null 2>&1; then
    COMPOSE="docker compose"
  elif have docker-compose; then
    COMPOSE="docker-compose"
  else
    die "Docker Compose not found. Install the Docker Compose plugin or docker-compose."
  fi
}

require_cmd() {
  have "$1" || die "Required command not found: $1"
}

check_repo_root() {
  [ -f docker-compose.yml ] || die "Run this script from the repository root."
  [ -f scripts/smoke_check.py ] || die "Missing scripts/smoke_check.py."
}

check_ax_runtime() {
  if [ "$DRY_RUN" = "1" ]; then
    [ -d /dev ] || warn "/dev is missing; real installation will fail."
    [ -d /soc ] || warn "/soc is missing; real installation must run on an AX650 board with AX MSP runtime mounted."
    return 0
  fi

  [ -d /dev ] || die "/dev is missing."
  if [ ! -d /soc ]; then
    die "/soc is missing. Run on an AX650 board with AX MSP runtime mounted."
  fi
}

free_mb_for_repo() {
  df -Pm . | awk 'NR == 2 { print $4 }'
}

check_disk() {
  free_mb="$(free_mb_for_repo)"
  case "$free_mb" in
    ''|*[!0-9]*) warn "Could not determine free disk space."; return 0 ;;
  esac
  if [ "$free_mb" -lt "$MIN_FREE_MB" ]; then
    die "Only ${free_mb} MB free under $(pwd); require ${MIN_FREE_MB} MB. Move this repo/models to a mounted path or lower --min-free-mb."
  fi
  log "Free space: ${free_mb} MB"
}

prepare_dirs() {
  run mkdir -p \
    models/asr \
    models/tts \
    models/llm \
    vendor/ax_asr_api \
    vendor/ax_tts_api \
    vendor/axllm
}

fetch_vendor_sources() {
  if [ "$SKIP_VENDOR" = "1" ]; then
    log "Skipping upstream source cache fetch."
    return 0
  fi
  log "Fetching ASR/TTS upstream source caches."
  run sh scripts/fetch_upstream_sources.sh
}

download_models() {
  if [ "$SKIP_MODELS" = "1" ]; then
    log "Skipping model downloads."
    return 0
  fi
  export HF_ENDPOINT PYTHON
  log "Downloading ASR model."
  run sh scripts/download_asr_models.sh
  log "Downloading TTS model."
  run sh scripts/download_tts_models.sh
  log "Downloading LLM model."
  run sh scripts/download_llm_models.sh
}

compose_up() {
  if [ "$SKIP_BUILD" = "1" ]; then
    log "Skipping compose build/start."
    return 0
  fi

  if [ "$WITH_HOMEASSISTANT" = "1" ]; then
    log "Starting voice stack with Home Assistant profile."
    run_sh "$COMPOSE --profile homeassistant up -d --build"
  else
    log "Starting voice stack."
    run_sh "$COMPOSE up -d --build"
  fi
}

validate_stack() {
  log "Compose service status."
  run_sh "$COMPOSE ps"

  log "Running public smoke check."
  run "$PYTHON" scripts/smoke_check.py \
    --host "$SMOKE_HOST" \
    --timeout "$SMOKE_TIMEOUT" \
    --public-only
}

DRY_RUN=0
SKIP_MODELS=0
SKIP_VENDOR=0
SKIP_BUILD=0
VALIDATE_ONLY=0
WITH_HOMEASSISTANT=0
MIN_FREE_MB="${MIN_FREE_MB:-8192}"
SMOKE_HOST="${SMOKE_HOST:-127.0.0.1}"
SMOKE_TIMEOUT="${SMOKE_TIMEOUT:-3}"
HF_ENDPOINT="${HF_ENDPOINT:-https://hf-mirror.com}"
PYTHON="${PYTHON:-python3}"

while [ "$#" -gt 0 ]; do
  case "$1" in
    --dry-run)
      DRY_RUN=1
      ;;
    --skip-models)
      SKIP_MODELS=1
      ;;
    --skip-vendor)
      SKIP_VENDOR=1
      ;;
    --skip-build)
      SKIP_BUILD=1
      ;;
    --validate-only)
      VALIDATE_ONLY=1
      SKIP_MODELS=1
      SKIP_VENDOR=1
      SKIP_BUILD=1
      ;;
    --with-homeassistant)
      WITH_HOMEASSISTANT=1
      ;;
    --min-free-mb)
      shift
      [ "$#" -gt 0 ] || die "--min-free-mb requires a value."
      MIN_FREE_MB="$1"
      ;;
    --smoke-host)
      shift
      [ "$#" -gt 0 ] || die "--smoke-host requires a value."
      SMOKE_HOST="$1"
      ;;
    --smoke-timeout)
      shift
      [ "$#" -gt 0 ] || die "--smoke-timeout requires a value."
      SMOKE_TIMEOUT="$1"
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      usage >&2
      die "Unknown option: $1"
      ;;
  esac
  shift
done

case "$MIN_FREE_MB" in
  ''|*[!0-9]*) die "--min-free-mb must be an integer." ;;
esac

case "$SMOKE_TIMEOUT" in
  ''|*[!0-9]*) die "--smoke-timeout must be an integer." ;;
esac

check_repo_root
require_cmd docker
require_cmd "$PYTHON"
require_cmd git
require_cmd curl
detect_compose

log "Compose command: $COMPOSE"
log "Python command: $PYTHON"
log "HF endpoint: $HF_ENDPOINT"

check_ax_runtime
check_disk
prepare_dirs

if [ "$VALIDATE_ONLY" != "1" ]; then
  fetch_vendor_sources
  download_models
  compose_up
fi

validate_stack
log "AX650 Home Assistant voice stack installation finished."
