FROM python:3.12-slim-bookworm AS isolate-builder

ARG ISOLATE_VERSION=v2.2.1
ENV DEBIAN_FRONTEND=noninteractive

RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    ca-certificates \
    git \
    libcap-dev \
    libsystemd-dev \
    pkg-config \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /tmp/isolate-src
RUN git clone --depth 1 --branch ${ISOLATE_VERSION} https://github.com/ioi/isolate.git . \
    && make install DESTDIR=/opt/isolate-root \
    && chmod 4755 /opt/isolate-root/usr/local/bin/isolate


FROM python:3.12-slim-bookworm AS executor-common

ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    VIRTUAL_ENV=/opt/executor-venv \
    PATH=/opt/executor-venv/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin \
    PYTHONPATH=/app:/app/executor_service

RUN apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates \
    libcap2 \
    libsystemd0 \
    && rm -rf /var/lib/apt/lists/*

COPY --from=isolate-builder /opt/isolate-root/ /
RUN chmod 4755 /usr/local/bin/isolate

COPY execution_routing.py /app/execution_routing.py
COPY executor_service/requirements.txt /tmp/requirements.txt
RUN python3 -m venv "${VIRTUAL_ENV}" \
    && pip install --no-cache-dir -r /tmp/requirements.txt

WORKDIR /app/executor_service
COPY executor_service /app/executor_service
RUN chmod +x /app/executor_service/docker-entrypoint.sh

ENTRYPOINT ["/app/executor_service/docker-entrypoint.sh"]
CMD ["python3", "main.py"]


FROM executor-common AS executor-core

RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    golang-go \
    nodejs \
    npm \
    php-cli \
    ruby-full \
    rustc \
    sqlite3 \
    && npm install -g typescript \
    && rm -rf /var/lib/apt/lists/*


FROM executor-common AS executor-java

RUN apt-get update && apt-get install -y --no-install-recommends \
    openjdk-17-jdk-headless \
    && rm -rf /var/lib/apt/lists/*


FROM executor-common AS executor-haskell

RUN apt-get update && apt-get install -y --no-install-recommends \
    ghc \
    && rm -rf /var/lib/apt/lists/*


FROM executor-common AS executor-csharp

RUN apt-get update && apt-get install -y --no-install-recommends \
    mono-mcs \
    mono-runtime \
    && rm -rf /var/lib/apt/lists/*


FROM swift:6.2.2-bookworm AS executor-swift

ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    VIRTUAL_ENV=/opt/executor-venv \
    PATH=/opt/executor-venv/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin \
    PYTHONPATH=/app:/app/executor_service

RUN apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates \
    libcap2 \
    libsystemd0 \
    python3 \
    python3-pip \
    python3-venv \
    && ln -sf /usr/bin/python3 /usr/local/bin/python3 \
    && ln -sf /usr/bin/pip3 /usr/local/bin/pip3 \
    && rm -rf /var/lib/apt/lists/*

COPY --from=isolate-builder /opt/isolate-root/ /
RUN chmod 4755 /usr/local/bin/isolate

COPY execution_routing.py /app/execution_routing.py
COPY executor_service/requirements.txt /tmp/requirements.txt
RUN python3 -m venv "${VIRTUAL_ENV}" \
    && pip install --no-cache-dir -r /tmp/requirements.txt

WORKDIR /app/executor_service
COPY executor_service /app/executor_service
RUN chmod +x /app/executor_service/docker-entrypoint.sh

ENTRYPOINT ["/app/executor_service/docker-entrypoint.sh"]
CMD ["python3", "main.py"]
