"""Internal FastAPI surface for reusable sandbox lifecycle and execution."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from functools import lru_cache
from uuid import UUID

from fastapi import APIRouter, Depends, FastAPI, HTTPException, Query

from lumen_sandbox_runner.auth import configured_token, require_token
from lumen_sandbox_runner.engine import DockerSandboxEngine, RunnerError
from lumen_sandbox_runner.models import CancelRequest, EnsureSessionRequest, ExecuteRequest


@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncIterator[None]:
    """Refuse to serve without a shared secret (#508).

    A runner that boots without a token would serve the Docker socket to anything on
    its network. Checking at startup means a misconfigured deploy fails visibly at
    `docker compose up` rather than at the first execution — and, because the
    healthcheck never turns green, it fails where an operator is already looking.
    """
    configured_token()
    yield


app = FastAPI(
    title="Lumen Sandbox Runner",
    version="0.2.0",
    docs_url=None,
    redoc_url=None,
    lifespan=lifespan,
)


#: Every capability endpoint hangs off this router, so authentication is a property
#: of the ROUTER rather than something each route remembers to declare. `/health` is
#: registered on the app directly and stays open — see `auth.py` for why.
sessions = APIRouter(dependencies=[Depends(require_token)])


@lru_cache(maxsize=1)
def get_engine() -> DockerSandboxEngine:
    return DockerSandboxEngine()


async def _call(method: object, *args: object) -> object:
    try:
        return await asyncio.to_thread(method, *args)  # type: ignore[arg-type]
    except RunnerError as exc:
        raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}


@sessions.put("/sessions/{session_id}")
async def ensure_session(session_id: UUID, body: EnsureSessionRequest) -> object:
    return await _call(get_engine().ensure, session_id, body)


@sessions.get("/sessions/{session_id}")
async def inspect_session(session_id: UUID) -> object:
    return await _call(get_engine().inspect, session_id)


@sessions.post("/sessions/{session_id}/executions")
async def execute(session_id: UUID, body: ExecuteRequest) -> object:
    return await _call(get_engine().execute_existing, session_id, body)


@sessions.delete("/sessions/{session_id}")
async def close_session(
    session_id: UUID, generation: int | None = Query(default=None, ge=1)
) -> dict[str, str]:
    await _call(get_engine().close, session_id, generation)
    return {"status": "closed"}


@sessions.post("/sessions/{session_id}/cancel")
async def cancel(session_id: UUID, body: CancelRequest) -> dict[str, str]:
    await _call(get_engine().cancel, session_id, body.generation, body.execution_id)
    return {"status": "cancelled"}


app.include_router(sessions)
