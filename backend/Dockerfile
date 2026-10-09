# Lumen Copilot backend image (ADR-0003 / ADR-0005).
# Pinned base (no :latest per AGENTS.md §3). Deps installed into the SYSTEM
# environment so the compose `./backend:/app` bind-mount does not shadow them.
FROM rust:1.99.0-bookworm AS rust-toolchain
FROM python:3.12-bookworm AS native-builder
COPY --from=rust-toolchain /usr/local/cargo /usr/local/cargo
COPY --from=rust-toolchain /usr/local/rustup /usr/local/rustup
ENV PATH="/usr/local/cargo/bin:${PATH}" RUSTUP_HOME=/usr/local/rustup CARGO_HOME=/usr/local/cargo CARGO_BUILD_JOBS=2
RUN pip install --no-cache-dir maturin==1.15.0
WORKDIR /build
COPY rust ./rust
RUN maturin build --release --locked --manifest-path rust/crates/lumen-docintel-py/Cargo.toml --interpreter python3.12 --out /wheels

FROM python:3.12-slim-bookworm AS runtime

# Worker media runtime (spec 0008 / ADR-0023).  A versioned static image keeps
# FFmpeg/ffprobe identical across local, CI, and production without an unpinned
# apt resolution. Only the two binaries cross into the application image.
COPY --from=mwader/static-ffmpeg:7.1.1 /ffmpeg /usr/local/bin/ffmpeg
COPY --from=mwader/static-ffmpeg:7.1.1 /ffprobe /usr/local/bin/ffprobe

# Faster, quieter Python in a container.
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

# uv: fast resolver/installer. Pinned copy from the official distroless image.
COPY --from=ghcr.io/astral-sh/uv:0.5.11 /uv /uvx /bin/

WORKDIR /app

# Install dependencies from pyproject.toml — the SINGLE source of truth for
# versions (no duplicated list to drift). Resolve to a locked set first, then
# install with `--system` into the image's site-packages (NOT a .venv under
# /app), so the compose bind-mount of source over /app keeps the deps reachable.
# This layer is cached until pyproject.toml changes, not on every source edit.
COPY backend/pyproject.toml backend/README.md ./
RUN uv pip compile pyproject.toml --output-file /tmp/requirements.txt \
 && uv pip install --system --no-cache --requirement /tmp/requirements.txt

# Copy the application source. In compose this is overlaid by the bind-mount;
# baked in for non-mounted (e.g. CI/prod) use of the same image (WORKDIR /app is
# on sys.path, so `app.main:app` resolves without installing the app as a wheel).
COPY --from=native-builder /wheels /tmp/native-wheels
RUN uv pip install --system --no-cache /tmp/native-wheels/*.whl \
 && python -c "import lumen_docintel; assert lumen_docintel.core_version() == '0.1.0'" \
 && rm -rf /tmp/native-wheels
COPY backend/ .

EXPOSE 8000

# Entrypoint: apply migrations (idempotent), then serve. --reload for the
# local dev inner loop; production overrides the command without --reload.
COPY backend/docker-entrypoint.sh /usr/local/bin/docker-entrypoint.sh
RUN chmod +x /usr/local/bin/docker-entrypoint.sh
ENTRYPOINT ["/usr/local/bin/docker-entrypoint.sh"]
# --no-access-log: uvicorn's access logger writes the FULL request path incl.
# query strings — the OAuth callback carries `state`/`code` there (ADR-0019 §1),
# so the default logger would leak authorization codes into stdout. Request
# telemetry stays on the app's structlog middleware, which never logs queries.
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--reload", "--no-access-log"]
