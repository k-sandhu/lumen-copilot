# The sandbox EXECUTION image (issue #503) — what model-authored Python runs IN.
#
# NOT the runner. `sandbox_runner/Dockerfile` builds the control plane (fastapi +
# the Docker socket); this builds the *payload* image the runner launches per chat
# session. Before #503 `SANDBOX_IMAGE` pointed at the control-plane image, so a run
# had fastapi and pydantic available and no pandas, numpy, or matplotlib —
# "run Python" could not do the data work ADR-0013 §3 promises.
#
# Contents are exactly `requirements.txt` (installed `--no-deps`, then `pip check`
# proves the closure is complete). Nothing from this repository is copied in: no
# application source, no credentials, no entrypoint of ours. The runner supplies
# the command, and ADR-0020 gives the container no network, no host mounts, no
# engine socket, and no capabilities.
#
# Build (the runner launches it on the HOST daemon, so it must exist there):
#   docker compose --profile sandbox build sandbox-exec-image
# or, without compose:
#   docker build -t lumen-sandbox-exec:0.1.1 ./sandbox_exec
#
# Base pinned by DIGEST, never `:latest` (ADR-0013 §3, root AGENTS.md §10). The tag
# is kept alongside the digest for readability; the digest is what is resolved.
#
# KEEP THIS CURRENT. A digest pin fixes the Debian userland too, so a stale pin is
# months of unpatched libc/openssl/zlib in the image that parses tenant spreadsheets
# as (contained) root — the previous pin was a Dec-2024 3.12.8 build, ~19 months old.
# Re-resolve on every image bump:
#   docker pull python:3.12-slim-bookworm
#   docker image inspect python:3.12-slim-bookworm --format '{{index .RepoDigests 0}}'
# Resolved 2026-07-30: python 3.12.13, linux/amd64 (the 3.12.13 and 3.12 tags name
# the same manifest — both were pulled and compared).
FROM python:3.12.13-slim-bookworm@sha256:d50fb7611f86d04a3b0471b46d7557818d88983fc3136726336b2a4c657aa30b

# MPLBACKEND=Agg: the container has no display and no X libraries, so any other
# matplotlib backend raises on import of `pyplot`. MPLCONFIGDIR: matplotlib writes
# its font cache and config there on first use; left unset it targets $HOME, which
# is not guaranteed writable for the uid the run executes as.
#
# MPLBACKEND is also set per session by the application
# (`SandboxSessionService._session_env`); MPLCONFIGDIR is NOT, and cannot be — the
# runner's `EnsureSessionRequest.env` allowlist accepts only HOME, PYTHONUNBUFFERED,
# MPLBACKEND and LANG, and would reject it. It exists ONLY because it is baked here,
# which is why the build-time cache warm-up below is load-bearing rather than an
# optimisation.
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    MPLBACKEND=Agg \
    MPLCONFIGDIR=/opt/mplconfig \
    LANG=C.UTF-8

COPY requirements.txt /tmp/requirements.txt
# `--only-binary=:all:` keeps the build off compilers/toolchains; `--no-deps` makes
# the manifest exhaustive; `pip check` fails the build if it is not.
RUN python -m pip install --no-cache-dir --only-binary=:all: --no-deps \
        -r /tmp/requirements.txt \
    && python -m pip check \
    && rm -f /tmp/requirements.txt

# Warm the font cache at BUILD time and leave MPLCONFIGDIR writable by any uid.
# A cold cache costs seconds on the first plot of every session and — worse — a
# non-writable MPLCONFIGDIR makes matplotlib fall back to a temporary directory
# with a warning on stderr that lands in the run's captured output. Warming it
# here means the first `savefig` in a fresh, network-isolated container just works.
RUN mkdir -p /opt/mplconfig /workspace \
    && python -c "import matplotlib; matplotlib.use('Agg'); from matplotlib import font_manager; font_manager.fontManager; import matplotlib.pyplot as plt; fig = plt.figure(); fig.savefig('/tmp/warmup.png'); print('matplotlib', matplotlib.__version__, 'headless ok')" \
    && rm -f /tmp/warmup.png \
    && chmod -R 0777 /opt/mplconfig \
    && useradd --create-home --uid 1000 sandbox \
    && chmod 0777 /workspace

# The session workspace ADR-0020 §4 persists across executions in one generation.
#
# It is world-writable (0777) rather than owned by the `sandbox` user ON PURPOSE:
# the runner launches this image with `--cap-drop ALL`, which takes CAP_DAC_OVERRIDE
# away from uid 0 too. Root without DAC_OVERRIDE obeys ordinary permission bits, so
# a `sandbox`-owned /workspace made the runner's very first `mkdir -p` fail with
# EACCES *as root* — verified, not theorised. 0777 is safe here: one container per
# chat session, no host mount, nothing else on the filesystem to protect.
WORKDIR /workspace

# Default to an unprivileged uid: an image that is root-by-default is one launcher
# mistake away from root-by-accident. ADR-0020 deliberately OVERRIDES this with
# `--user 0:0` so governed package installs can write into the environment; that
# root is contained (no network, no mounts, no socket, no capabilities, §2).
USER sandbox

CMD ["python3"]
