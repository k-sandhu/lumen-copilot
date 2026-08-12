"""FastAPI application factory.

Wires the whole backend together: structured logging, request/tenant
correlation middleware, a lifespan that initialises and tears down shared
resources (object-store bucket, DB engine), the typed-error -> problem+json
exception handlers, and the router + WebSocket mounts. This module composes; it
holds no business logic or I/O of its own (that lives in adapters/services).
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import structlog
from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.responses import Response

from app.api.deps import aclose_backplane, get_object_store
from app.api.health import router as health_router
from app.api.v1 import router as v1_router
from app.core.config import Settings, get_settings
from app.core.errors import (
    PROBLEM_CONTENT_TYPE,
    AppError,
    FieldError,
    Problem,
    problem_from_exception,
)
from app.core.logging import configure_logging, get_logger
from app.db.session import dispose_engine
from app.ingestion.contract import provision_embedding_contract
from app.realtime.chat_ws import router as chat_ws_router
from app.realtime.health_ws import router as health_ws_router
from app.search import aclose_search_store
from app.tasks.rate_limit import aclose_async_rate_limit_pools

log = get_logger(__name__)

# Header names for inbound correlation. If absent, we mint a request id.
_REQUEST_ID_HEADER = "x-request-id"
_TENANT_ID_HEADER = "x-tenant-id"


class CorrelationMiddleware(BaseHTTPMiddleware):
    """Bind request/tenant correlation ids into the structlog context.

    Every log line emitted while handling the request inherits these ids via
    ``structlog.contextvars``. The request id is echoed back on the response so
    a client/log can be traced end-to-end. Tenant resolution proper is CC-3;
    here we only propagate an inbound header if present.
    """

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        structlog.contextvars.clear_contextvars()
        request_id = request.headers.get(_REQUEST_ID_HEADER) or uuid.uuid4().hex
        tenant_id = request.headers.get(_TENANT_ID_HEADER)
        structlog.contextvars.bind_contextvars(
            request_id=request_id,
            tenant_id=tenant_id,
            path=request.url.path,
            method=request.method,
        )
        try:
            response = await call_next(request)
        finally:
            structlog.contextvars.clear_contextvars()
        response.headers[_REQUEST_ID_HEADER] = request_id
        return response


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Initialise shared resources on startup; tear them down on shutdown.

    Startup best-effort ensures the object-store bucket exists so uploads have a
    target from first boot; a transient failure here is logged but does not
    block the process from coming up (readiness will report the real state).
    Shutdown cancels any still-running answer-producer tasks and drains them
    under a bounded grace window so a hung answer can't wedge uvicorn's graceful
    shutdown (issue #156), then disposes the DB engine cleanly.
    """
    settings: Settings = app.state.settings
    log.info("startup.begin", environment=settings.environment, version=settings.version)
    try:
        await get_object_store().ensure_bucket()
        log.info("startup.bucket_ready", bucket=settings.s3_bucket)
    except Exception as exc:  # noqa: BLE001 — non-fatal; readiness surfaces it
        log.warning("startup.bucket_unavailable", error=str(exc))

    try:
        fingerprint = await provision_embedding_contract(settings)
        log.info("startup.embedding_contract_ready", fingerprint=fingerprint)
    except Exception as exc:  # noqa: BLE001 — process stays live; readiness rejects traffic
        log.warning("startup.embedding_contract_unavailable", error=type(exc).__name__)

    yield

    await _drain_answer_tasks(app, grace_seconds=settings.chat_shutdown_grace_seconds)
    # Release the shared retrieval-store HTTP client (ADR-0010). Created lazily
    # on the serving loop by the first search — a no-op if never created (e.g.
    # offline tests) — so create/close stay on one event loop (#140 hygiene).
    await aclose_search_store()
    # Same rule for the realtime backplane's pooled Redis client (issue #487):
    # created lazily on the serving loop by the first publish, closed here.
    # Ordered after the answer drain so a producer still finishing its terminal
    # envelope is not publishing into a closed client.
    await aclose_backplane()
    # Same rule again for the per-tenant rate limiter's pooled async client
    # (#527): created lazily on the serving loop by the first request-path
    # admission check, and process-wide rather than owned by any one limiter
    # instance, so the lifespan is what closes it.
    await aclose_async_rate_limit_pools()
    await dispose_engine()
    log.info("shutdown.complete")


async def _drain_answer_tasks(app: FastAPI, *, grace_seconds: float) -> None:
    """Cancel outstanding answer tasks and await them, bounded by ``grace_seconds``.

    Snapshots the live tasks, cancels each, and gathers them under
    ``asyncio.wait_for``; the runtime turns ``CancelledError`` into its existing
    terminal envelope so cancellation stays contract-clean. On timeout we log and
    return so shutdown proceeds to engine disposal rather than blocking forever.
    """
    tasks = list(app.state.answer_tasks)
    if not tasks:
        return
    for task in tasks:
        task.cancel()
    try:
        await asyncio.wait_for(
            asyncio.gather(*tasks, return_exceptions=True),
            timeout=grace_seconds,
        )
        log.info("shutdown.answer_tasks_drained", count=len(tasks))
    except TimeoutError:
        log.warning("shutdown.answer_tasks_timeout", count=len(tasks), grace=grace_seconds)


def _register_exception_handlers(app: FastAPI) -> None:
    """Map the typed error hierarchy + framework errors to problem+json."""

    @app.exception_handler(AppError)
    async def _handle_app_error(request: Request, exc: AppError) -> JSONResponse:
        status_code, body = problem_from_exception(exc, instance=request.url.path)
        if status_code >= 500:
            log.error("app_error", code=exc.code, status=status_code, detail=exc.detail)
        return JSONResponse(status_code=status_code, content=body, media_type=PROBLEM_CONTENT_TYPE)

    @app.exception_handler(RequestValidationError)
    async def _handle_validation(request: Request, exc: RequestValidationError) -> JSONResponse:
        problem = Problem(
            title="Unprocessable Entity",
            status=422,
            code="validation_error",
            instance=request.url.path,
            errors=[
                FieldError(
                    field=".".join(str(p) for p in err.get("loc", [])),
                    message=err.get("msg", "invalid"),
                )
                for err in exc.errors()
            ],
        )
        return JSONResponse(
            status_code=422,
            content=problem.model_dump(exclude_none=True),
            media_type=PROBLEM_CONTENT_TYPE,
        )

    @app.exception_handler(Exception)
    async def _handle_unexpected(request: Request, exc: Exception) -> JSONResponse:
        # Never leak the original error/stack to the client.
        log.exception("unhandled_exception", path=request.url.path)
        status_code, body = problem_from_exception(exc, instance=request.url.path)
        return JSONResponse(status_code=status_code, content=body, media_type=PROBLEM_CONTENT_TYPE)


def create_app(settings: Settings | None = None) -> FastAPI:
    """Build and return the FastAPI application.

    A factory (not a module-level singleton with side effects) so tests can
    construct an app with injected settings.
    """
    settings = settings or get_settings()
    configure_logging(
        log_level=settings.log_level,
        json_logs=settings.environment != "local",
    )

    app = FastAPI(
        title="Lumen Copilot API",
        version=settings.version,
        lifespan=lifespan,
    )
    app.state.settings = settings
    # Process-wide registry of in-flight answer-producer tasks (issue #156). Held
    # on app.state alongside settings; initialised here (not only in the lifespan)
    # so it exists even for transports that skip the lifespan (e.g. tests driving
    # the ASGI app directly). The lifespan teardown drains it.
    app.state.answer_tasks = set()

    app.add_middleware(CorrelationMiddleware)
    _register_exception_handlers(app)

    # Unversioned health probes (contracts/openapi.yaml).
    app.include_router(health_router)
    # Versioned feature routes mount under /api/v1 (empty in the skeleton).
    app.include_router(v1_router, prefix="/api/v1")
    # WebSocket transport: the health heartbeat (proves the WS path + envelope)
    # and the chat answer stream consumer (CC-6 #24 / CC-11 #26).
    app.include_router(health_ws_router)
    app.include_router(chat_ws_router)

    return app


# ASGI entrypoint used by uvicorn / the compose CMD: ``app.main:app``.
app = create_app()
