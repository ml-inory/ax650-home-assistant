FROM debian:bookworm-slim

ENV AX_LLM_MODEL_DIR=/models/llm/Qwen3-0.6B \
    AX_LLM_PORT=8001

WORKDIR /app

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        ca-certificates \
        curl \
    && rm -rf /var/lib/apt/lists/*

# Install the upstream AX-LLM command line on the target board. The script
# selects the platform-specific release artifacts maintained by AXERA-TECH.
RUN curl -fsSL https://raw.githubusercontent.com/AXERA-TECH/ax-llm/axllm/install.sh | bash

EXPOSE 8001

CMD ["sh", "-c", "axllm serve \"$AX_LLM_MODEL_DIR\" --port \"$AX_LLM_PORT\""]
