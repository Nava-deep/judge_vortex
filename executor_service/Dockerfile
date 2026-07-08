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
#   - C/C++  → GCC 14 (gcc-14 / g++-14) via bookworm-backports
#   - Go     → 1.24.5 official tarball (upstream)
#   - Node   → 22 LTS via NodeSource
#   - Rust   → latest stable via rustup
#   - PHP    → 8.3 via Sury repo
#   - Ruby   → 3.3 via bookworm-backports
#   - SQLite → system package
#   - TypeScript → latest via npm
# ─────────────────────────────────────────────────────────────────────────────
FROM executor-common AS executor-core

ENV DEBIAN_FRONTEND=noninteractive \
    GOROOT=/usr/local/go \
    GOPATH=/opt/go \
    RUSTUP_HOME=/opt/rustup \
    CARGO_HOME=/opt/cargo \
    PATH=/usr/local/go/bin:/opt/cargo/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin

# ── System tools & backports for GCC 14 / Ruby 3.3 ──────────────────────────
RUN apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates \
    curl \
    gnupg \
    lsb-release \
    sqlite3 \
    && echo "deb http://deb.debian.org/debian bookworm-backports main" \
       > /etc/apt/sources.list.d/backports.list \
    && apt-get update \
    && apt-get install -y --no-install-recommends \
       gcc-14 \
       g++-14 \
       cpp-14 \
       ruby3.3 \
       ruby3.3-dev \
    && update-alternatives --install /usr/bin/gcc gcc /usr/bin/gcc-14 100 \
    && update-alternatives --install /usr/bin/g++ g++ /usr/bin/g++-14 100 \
    && update-alternatives --install /usr/bin/cc  cc  /usr/bin/gcc-14 100 \
    && update-alternatives --install /usr/bin/c++ c++ /usr/bin/g++-14 100 \
    && update-alternatives --install /usr/bin/ruby ruby /usr/bin/ruby3.3 100 \
    && rm -rf /var/lib/apt/lists/*

# ── Go 1.24.5 (official upstream tarball) ────────────────────────────────────
RUN curl -fsSL https://go.dev/dl/go1.24.5.linux-amd64.tar.gz \
    | tar -C /usr/local -xz \
    && mkdir -p /opt/go

# ── Node.js 22 LTS (NodeSource) ──────────────────────────────────────────────
RUN curl -fsSL https://deb.nodesource.com/setup_22.x | bash - \
    && apt-get install -y --no-install-recommends nodejs \
    && rm -rf /var/lib/apt/lists/* \
    && npm install -g typescript

# ── PHP 8.3 (Sury official repo) ─────────────────────────────────────────────
RUN curl -fsSL https://packages.sury.org/php/apt.gpg \
       -o /etc/apt/trusted.gpg.d/sury-php.gpg \
    && echo "deb https://packages.sury.org/php/ bookworm main" \
       > /etc/apt/sources.list.d/sury-php.list \
    && apt-get update \
    && apt-get install -y --no-install-recommends \
       php8.3-cli \
       php8.3-common \
    && update-alternatives --install /usr/bin/php php /usr/bin/php8.3 100 \
    && rm -rf /var/lib/apt/lists/*

# ── Rust latest stable (rustup) ───────────────────────────────────────────────
RUN curl -fsSL https://sh.rustup.rs \
    | sh -s -- -y --no-modify-path --default-toolchain stable --profile minimal \
    && /opt/cargo/bin/rustup update stable \
    && chmod -R a+rx /opt/rustup /opt/cargo


# ─────────────────────────────────────────────────────────────────────────────
# Stage 4: executor-java
#   - Java 21 LTS (Temurin) — stable LTS, widely used in competitive programming
#   - Java 24 (Temurin)     — latest non-LTS for modern features
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
    && update-alternatives --install /usr/bin/java   java   /opt/java/21/bin/java   100 \
    && update-alternatives --install /usr/bin/javac  javac  /opt/java/21/bin/javac  100 \
    && rm -rf /var/lib/apt/lists/*
