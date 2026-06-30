#!/bin/sh
set -eu

: "${AX_TTS_SOURCE_DIR:=/opt/ax_tts_api}"
: "${AX_TTS_INSTALL_DIR:=/opt/ax_tts_api/install/ax650}"
: "${AX_MSP_DIR:=/soc}"

if [ ! -d "$AX_TTS_SOURCE_DIR" ]; then
  echo "TTS source directory missing: $AX_TTS_SOURCE_DIR" >&2
  exit 1
fi

if [ ! -d "$AX_MSP_DIR/include" ] || [ ! -d "$AX_MSP_DIR/lib" ]; then
  echo "AX MSP directory must contain include/ and lib/: $AX_MSP_DIR" >&2
  exit 1
fi

cd "$AX_TTS_SOURCE_DIR"
python3 - <<'PY'
from pathlib import Path

path = Path("cmake/msp_dependencies.cmake")
text = path.read_text()
for name in ("ax_sys", "ax_engine", "ax_interpreter", "ax_dmadim"):
    text = text.replace(f"lib{name}.a", name)
path.write_text(text)

audio_file = Path("src/utils/AudioFile.h")
audio_text = audio_file.read_text()
if "#include <limits>" not in audio_text:
    audio_text = audio_text.replace("#include <algorithm>", "#include <algorithm>\n#include <limits>")
    audio_file.write_text(audio_text)
PY
mkdir -p build_ax650_native
cd build_ax650_native

cmake .. \
  -DCHIP_AX650=ON \
  -DBSP_MSP_DIR="$AX_MSP_DIR" \
  -DCMAKE_INSTALL_PREFIX="$AX_TTS_INSTALL_DIR" \
  -DCMAKE_BUILD_TYPE=Release \
  -DBUILD_SERVER=ON
make -j"$(nproc)"
make install
