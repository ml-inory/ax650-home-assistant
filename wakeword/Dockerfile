ARG BASE_IMAGE=docker.m.daocloud.io/library/python:3.10-slim-bookworm
FROM ${BASE_IMAGE}

ARG APT_DEBIAN_MIRROR=http://mirrors.ustc.edu.cn/debian
ARG APT_SECURITY_MIRROR=http://mirrors.ustc.edu.cn/debian-security

ENV PYTHONUNBUFFERED=1 \
    WYOMING_OPENWAKEWORD_URI=tcp://0.0.0.0:10400 \
    WYOMING_OPENWAKEWORD_MODEL=ok_nabu

WORKDIR /app

RUN if [ -n "$APT_DEBIAN_MIRROR" ]; then \
        sed -i "s|http://deb.debian.org/debian-security|$APT_SECURITY_MIRROR|g; s|http://deb.debian.org/debian|$APT_DEBIAN_MIRROR|g" /etc/apt/sources.list /etc/apt/sources.list.d/debian.sources 2>/dev/null || true; \
    fi \
    && apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates \
    && rm -rf /var/lib/apt/lists/*

COPY wakeword/requirements.txt /app/requirements.txt

RUN python -m pip install --upgrade pip \
    && python -m pip install -r /app/requirements.txt \
    && python -m pip install --no-deps wyoming-openwakeword==1.8.2

COPY wakeword/start.sh /app/start.sh
RUN chmod +x /app/start.sh

CMD ["/app/start.sh"]
