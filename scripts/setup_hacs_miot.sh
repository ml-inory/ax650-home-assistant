#!/usr/bin/env bash
# setup_hacs_miot.sh
# ==================
# Downloads HACS and Xiaomi Miot Auto custom components into
# homeassistant/config/custom_components/ for pre-configured HA profile.
#
# Usage:
#   bash scripts/setup_hacs_miot.sh            # download both
#   bash scripts/setup_hacs_miot.sh --dry-run  # print commands only
#
# This script is idempotent: existing downloads are skipped.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
CUSTOM_DIR="$REPO_ROOT/homeassistant/config/custom_components"

HACS_REPO="hacs/integration"
MIOT_REPO="al-one/hass-xiaomi-miot"

# Use GitHub mirror if available
GH_DOWNLOAD="${GH_DOWNLOAD:-https://github.com}"

DRY_RUN=0
if [[ "${1:-}" == "--dry-run" ]]; then
  DRY_RUN=1
  echo "[DRY-RUN] Would download HACS and Xiaomi Miot Auto"
fi

mkdir_d() {
  if [[ $DRY_RUN -eq 1 ]]; then
    echo "[DRY-RUN] mkdir -p $1"
  else
    mkdir -p "$1"
  fi
}

download_repo() {
  local repo="$1"
  local dest="$2"
  local label="$3"

  if [[ -d "$dest" ]]; then
    echo "[skip] $label already exists at $dest"
    return
  fi

  echo "[fetch] $label from $repo"
  if [[ $DRY_RUN -eq 1 ]]; then
    echo "[DRY-RUN] git clone --depth 1 $GH_DOWNLOAD/$repo.git $dest"
    return
  fi

  git clone --depth 1 "$GH_DOWNLOAD/$repo.git" "$dest"
  echo "[done] $label → $dest"
}

main() {
  echo "=== HACS & Xiaomi Miot Auto Setup ==="
  echo "Target: $CUSTOM_DIR"
  echo ""

  mkdir_d "$CUSTOM_DIR"

  # HACS (Home Assistant Community Store)
  download_repo \
    "$HACS_REPO" \
    "$CUSTOM_DIR/hacs" \
    "HACS"

  # Xiaomi Miot Auto
  download_repo \
    "$MIOT_REPO" \
    "$CUSTOM_DIR/xiaomi_miot" \
    "Xiaomi Miot Auto"

  echo ""
  echo "=== Setup Complete ==="
  echo "Next: docker compose --profile homeassistant up -d"
  echo "Then: In HA UI → Settings → Devices → Add Integration → Xiaomi Miot Auto"
  echo "      Log in with Xiaomi account to discover your devices."
}

main
