# ─────────────────────────────────────────────────────────────────────────────
# Stage 1: Build isolate from source
# ─────────────────────────────────────────────────────────────────────────────
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


# ─────────────────────────────────────────────────────────────────────────────
# Stage 2: Common executor base (Python + isolate binary)
# ─────────────────────────────────────────────────────────────────────────────
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
COPY shared /app/shared
COPY executor_service/requirements.txt /tmp/requirements.txt
RUN python3 -m venv "${VIRTUAL_ENV}" \
    && pip install --no-cache-dir -r /tmp/requirements.txt

WORKDIR /app/executor_service
COPY executor_service /app/executor_service
RUN chmod +x /app/executor_service/docker-entrypoint.sh

ENTRYPOINT ["/app/executor_service/docker-entrypoint.sh"]
CMD ["python3", "main.py"]


# ─────────────────────────────────────────────────────────────────────────────
# Stage 3: executor-core
#   Works on both amd64 AND arm64 (Apple Silicon)
#
#   C/C++      → GCC 12 (bookworm default, available on all arches)  C++20/C17
#   Go         → 1.24.5 official tarball (arch-aware)
#   Node.js    → 22 LTS via NodeSource (supports arm64)
#   Rust       → latest stable via rustup (supports arm64)
#   PHP        → 8.2 from bookworm main (available on all arches)
#   Ruby       → 3.1 from bookworm main (available on all arches)
#   SQLite     → system package
#   TypeScript → latest via npm
# ─────────────────────────────────────────────────────────────────────────────
FROM executor-common AS executor-core

ENV DEBIAN_FRONTEND=noninteractive \
    GOROOT=/usr/local/go \
    GOPATH=/opt/go \
    RUSTUP_HOME=/opt/rustup \
    CARGO_HOME=/opt/cargo \
    PATH=/usr/local/go/bin:/opt/cargo/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin

# ── C/C++ 12, Ruby 3.1, PHP 8.2, SQLite — all from bookworm main (arm64+amd64)
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    g++ \
    gcc \
    curl \
    ca-certificates \
    gnupg \
    php-cli \
    ruby-full \
    sqlite3 \
    && rm -rf /var/lib/apt/lists/*

# ── Go 1.24.5 — official upstream tarball, architecture-aware ────────────────
RUN ARCH="$(dpkg --print-architecture)" \
    && curl -fsSL "https://go.dev/dl/go1.24.5.linux-${ARCH}.tar.gz" \
       | tar -C /usr/local -xz \
    && mkdir -p /opt/go

# ── Node.js 22 LTS — NodeSource official (supports arm64 + amd64) ────────────
RUN curl -fsSL https://deb.nodesource.com/setup_22.x | bash - \
    && apt-get install -y --no-install-recommends nodejs \
    && rm -rf /var/lib/apt/lists/* \
    && npm install -g typescript

# ── Rust latest stable via rustup (supports arm64 + amd64) ───────────────────
RUN curl -fsSL https://sh.rustup.rs \
    | sh -s -- -y --no-modify-path --default-toolchain stable --profile minimal \
    && /opt/cargo/bin/rustup update stable \
    && chmod -R a+rx /opt/rustup /opt/cargo


# ─────────────────────────────────────────────────────────────────────────────
# Stage 4: executor-java
#   Java 21 LTS via Temurin (Adoptium) — supports arm64 + amd64
# ─────────────────────────────────────────────────────────────────────────────
FROM executor-common AS executor-java

ENV DEBIAN_FRONTEND=noninteractive

RUN apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates \
    curl \
    gnupg \
    && curl -fsSL https://packages.adoptium.net/artifactory/api/gpg/key/public \
       | gpg --dearmor -o /etc/apt/trusted.gpg.d/adoptium.gpg \
    && echo "deb https://packages.adoptium.net/artifactory/deb bookworm main" \
       > /etc/apt/sources.list.d/adoptium.list \
    && apt-get update \
    && apt-get install -y --no-install-recommends \
       temurin-21-jdk \
    && rm -rf /var/lib/apt/lists/*
