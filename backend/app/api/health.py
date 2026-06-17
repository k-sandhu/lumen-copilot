"""Health probes — liveness and readiness.

Implements ``GET /health`` and ``GET /health/ready`` exactly per
``contracts/openapi.yaml`` (``HealthStatus`` / ``ReadinessStatus`` /
``DependencyStatus``). Liveness is a pure process check. Readiness actually
reaches each critical dependency — Postgres ``SELECT 1``, Redis ``PING``, and
object-store reachability — and returns 200 ``ready`` only if all pass, else
503 ``degraded`` with a per-dependency breakdown. Each check runs through that
system's owning adapter (DB session, object store) so the boundary table holds;
the Redis ping is a thin readiness-only client.
"""

from __future__ import annotations

import asyncio

from fastapi import APIRouter, Response, status
from pydantic import BaseModel, ConfigDict

from app.api.deps import SettingsDep, get_object_store
from app.core.logging import get_logger
from app.db import session as db_session
from app.realtime import backplane

router = APIRouter(tags=["health"])
log = get_logger(__name__)


# --- Response models (mirror contracts/openapi.yaml) -------------------------


class HealthStatus(BaseModel):
    """``#/components/schemas/HealthStatus`` — liveness payload."""

    model_config = ConfigDict(extra="forbid")

    status: str = "ok"
    service: str
    version: str


class DependencyStatus(BaseModel):
    """``#/components/schemas/DependencyStatus`` — one dependency's state."""

    model_config = ConfigDict(extra="forbid")

    name: str
    ok: bool
    detail: str | None = None


class ReadinessStatus(BaseModel):
    """``#/components/schemas/ReadinessStatus`` — readiness payload."""

    model_config = ConfigDict(extra="forbid")

    status: str  # "ready" | "degraded"
    dependencies: list[DependencyStatus]


# --- Liveness ----------------------------------------------------------------


@router.get("/health", response_model=HealthStatus, summary="Liveness")
async def get_health(settings: SettingsDep) -> HealthStatus:
    """Liveness: the process is up. Used by the compose healthcheck."""
    return HealthStatus(service=settings.service_name, version=settings.version)


# --- Readiness dependency checks ---------------------------------------------


async def _check_postgres() -> DependencyStatus:
    """Postgres reachability — delegated to ``db`` (SQL stays inside ``db/``)."""
    try:
        await db_session.ping()
        return DependencyStatus(name="postgres", ok=True)
    except Exception as exc:  # noqa: BLE001 — reported as detail, never raised
        return DependencyStatus(name="postgres", ok=False, detail=str(exc))


async def _check_redis(redis_url: str) -> DependencyStatus:
    """Redis reachability — delegated to ``realtime`` (the Redis owner)."""
    try:
        await backplane.ping(redis_url)
        return DependencyStatus(name="redis", ok=True)
    except Exception as exc:  # noqa: BLE001
        return DependencyStatus(name="redis", ok=False, detail=str(exc))


async def _check_object_store() -> DependencyStatus:
    """Object-store (MinIO/S3) reachability via the storage adapter."""
    try:
        await get_object_store().ping()
        return DependencyStatus(name="minio", ok=True)
    except Exception as exc:  # noqa: BLE001
        return DependencyStatus(name="minio", ok=False, detail=str(exc))


@router.get(
    "/health/ready",
    response_model=ReadinessStatus,
    summary="Readiness",
    responses={503: {"model": ReadinessStatus, "description": "A dependency is not ready."}},
)
async def get_readiness(settings: SettingsDep, response: Response) -> ReadinessStatus:
    """Readiness: process + all critical dependencies reachable.

    Checks run concurrently. Returns 200 ``ready`` when every dependency is ok,
    otherwise 503 ``degraded`` with the per-dependency breakdown so an operator
    sees exactly which one is down.
    """
    dependencies = await asyncio.gather(
        _check_postgres(),
        _check_redis(settings.redis_url),
        _check_object_store(),
    )
    all_ok = all(dep.ok for dep in dependencies)
    if not all_ok:
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
        log.warning(
            "readiness.degraded",
            failing=[d.name for d in dependencies if not d.ok],
        )
    return ReadinessStatus(
        status="ready" if all_ok else "degraded",
        dependencies=list(dependencies),
    )
