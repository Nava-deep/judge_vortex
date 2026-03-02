FROM rust:1.75-slim
WORKDIR /app
RUN cargo new --bin vortex_app
WORKDIR /app/vortex_app
RUN cargo build --release
ENTRYPOINT ["sh", "-c", "cargo run --release --offline"]