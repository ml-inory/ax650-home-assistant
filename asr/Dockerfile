FROM python:3.11-slim-bookworm

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    AX_ASR_HTTP_URL=http://127.0.0.1:8080 \
    AX_ASR_MODEL=sensevoice \
    AX_ASR_LANGUAGE=auto \
    AX_ASR_MODEL_PATH=/models/asr

WORKDIR /app

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        ca-certificates \
        cmake \
        build-essential \
        git \
        libatomic1 \
        libgomp1 \
        libstdc++6 \
    && rm -rf /var/lib/apt/lists/*

COPY asr/requirements.txt /app/requirements.txt
RUN python -m pip install --upgrade pip \
    && python -m pip install -r /app/requirements.txt

# Board validation must confirm the exact AX650 runtime/BSP layout. This
# scaffold follows AXERA-TECH/ax_asr_api's documented AX650 build entrypoint.
RUN git clone --depth=1 https://github.com/AXERA-TECH/ax_asr_api.git /opt/ax_asr_api

COPY asr/wyoming_adapter.py /app/wyoming_adapter.py
COPY asr/start.sh /app/start.sh
RUN chmod +x /app/start.sh

EXPOSE 10300

CMD ["/app/start.sh"]
