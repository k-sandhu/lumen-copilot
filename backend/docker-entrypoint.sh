#!/usr/bin/env bash
# Backend container entrypoint (ADR-0005): migrate, then exec the given command.
# `alembic upgrade head` is idempotent and safe to run on every boot; it brings
# the compose Postgres up to the latest schema (incl. CREATE EXTENSION vector).
# The web service runs uvicorn (default CMD); the worker overrides CMD with celery.
set -euo pipefail

echo "[entrypoint] applying database migrations (alembic upgrade head)..."
alembic upgrade head

echo "[entrypoint] starting: $*"
exec "$@"
