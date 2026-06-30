#!/bin/sh
set -eu

: "${AX_ASR_REPO:=https://github.com/AXERA-TECH/ax_asr_api.git}"
: "${AX_TTS_REPO:=https://github.com/AXERA-TECH/ax_tts_api.git}"
: "${AX_ASR_REF:=HEAD}"
: "${AX_TTS_REF:=HEAD}"
: "${TARGET_DIR:=vendor}"

fetch_repo() {
  name="$1"
  repo="$2"
  ref="$3"
  dest="$TARGET_DIR/$name"
  tmp="$TARGET_DIR/.tmp-$name"

  rm -rf "$tmp"
  if [ "$ref" = "HEAD" ]; then
    git clone --depth=1 "$repo" "$tmp"
  else
    git clone --depth=1 --branch "$ref" "$repo" "$tmp" 2>/dev/null \
      || git clone --depth=1 "$repo" "$tmp"
    git -C "$tmp" fetch --depth=1 origin "$ref"
    git -C "$tmp" checkout --detach FETCH_HEAD
  fi

  rm -rf "$dest"
  mkdir -p "$dest"
  cp -a "$tmp"/. "$dest"/
  rm -rf "$dest/.git" "$tmp"
}

mkdir -p "$TARGET_DIR"
fetch_repo ax_asr_api "$AX_ASR_REPO" "$AX_ASR_REF"
fetch_repo ax_tts_api "$AX_TTS_REPO" "$AX_TTS_REF"
