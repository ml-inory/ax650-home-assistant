FROM python:3.11-slim-bookworm

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    AX_TTS_HTTP_URL=http://127.0.0.1:8081 \
    AX_TTS_MODEL=kokoro \
    AX_TTS_LANGUAGE=zh \
    AX_TTS_VOICE=jm_kumo \
    AX_TTS_MODEL_PATH=/models/tts \
    AX_TTS_SERVER_BIN=/opt/ax_tts_api/install/ax650/tts_server \
    AX_TTS_SERVER_PORT=8081 \
    AX_TTS_ADAPTER_URI=tcp://0.0.0.0:10200 \
    AX_TTS_WAIT_TIMEOUT=30 \
    AX_TTS_ADAPTER_ONLY=0

WORKDIR /app

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        ca-certificates \
        cmake \
        build-essential \
        curl \
        git \
        libatomic1 \
        libgomp1 \
        libstdc++6 \
    && rm -rf /var/lib/apt/lists/*

COPY tts/requirements.txt /app/requirements.txt
RUN python -m pip install --upgrade pip \
    && python -m pip install -r /app/requirements.txt

# Board validation must confirm the exact AX650 runtime/BSP layout. This
# scaffold follows AXERA-TECH/ax_tts_api's documented AX650 build entrypoint.
RUN git clone --depth=1 https://github.com/AXERA-TECH/ax_tts_api.git /opt/ax_tts_api

COPY tts/wyoming_adapter.py /app/wyoming_adapter.py
COPY tts/start.sh /app/start.sh
COPY tts/build_server.sh /app/build_server.sh
RUN chmod +x /app/start.sh /app/build_server.sh

EXPOSE 10200

CMD ["/app/start.sh"]
