ARG BASE_IMAGE=docker.m.daocloud.io/library/debian:bookworm-slim
FROM ${BASE_IMAGE}

ARG APT_DEBIAN_MIRROR=http://mirrors.ustc.edu.cn/debian
ARG APT_SECURITY_MIRROR=http://mirrors.ustc.edu.cn/debian-security
ARG AX_LLM_RELEASE_URL=https://github.com/AXERA-TECH/ax-llm/releases/download/latest/axllm-ax650-linux-arm64

ENV AX_LLM_MODEL_DIR=/models/llm/Qwen3-0.6B \
    AX_LLM_PORT=8001 \
    AX_MSP_DIR=/soc

WORKDIR /app

RUN if [ -n "$APT_DEBIAN_MIRROR" ]; then \
        sed -i "s|http://deb.debian.org/debian-security|$APT_SECURITY_MIRROR|g; s|http://deb.debian.org/debian|$APT_DEBIAN_MIRROR|g" /etc/apt/sources.list /etc/apt/sources.list.d/debian.sources 2>/dev/null || true; \
    fi \
    && apt-get update \
    && apt-get install -y --no-install-recommends \
        ca-certificates \
        curl \
        libgomp1 \
        libstdc++6 \
    && rm -rf /var/lib/apt/lists/*

COPY vendor/axllm /tmp/vendor/axllm

RUN if [ -f /tmp/vendor/axllm/axllm-ax650-linux-arm64 ]; then \
        cp /tmp/vendor/axllm/axllm-ax650-linux-arm64 /usr/local/bin/axllm; \
    else \
        curl -fL --retry 5 --connect-timeout 20 "$AX_LLM_RELEASE_URL" -o /usr/local/bin/axllm; \
    fi \
    && chmod +x /usr/local/bin/axllm \
    && rm -rf /tmp/vendor

COPY llm/start.sh /app/start.sh
RUN chmod +x /app/start.sh

EXPOSE 8001

CMD ["/app/start.sh"]
