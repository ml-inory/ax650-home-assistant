#!/bin/sh
set -eu

: "${WYOMING_OPENWAKEWORD_URI:=tcp://0.0.0.0:10400}"
: "${WYOMING_OPENWAKEWORD_MODEL:=ok_nabu}"

exec wyoming-openwakeword \
  --uri "$WYOMING_OPENWAKEWORD_URI" \
  --preload-model "$WYOMING_OPENWAKEWORD_MODEL"
