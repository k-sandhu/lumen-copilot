"""Internal FastAPI surface for reusable sandbox lifecycle and execution."""

from __future__ import annotations

import asyncio
from functools import lru_cache
from uuid import UUID

from fastapi import FastAPI, HTTPException, Query

from lumen_sandbox_runner.engine import DockerSandboxEngine, RunnerError
from lumen_sandbox_runner.models import CancelRequest, EnsureSessionRequest, ExecuteRequest

app = FastAPI(title="Lumen Sandbox Runner", version="0.2.0", docs_url=None, redoc_url=None)


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


@app.put("/sessions/{session_id}")
async def ensure_session(session_id: UUID, body: EnsureSessionRequest) -> object:
    return await _call(get_engine().ensure, session_id, body)


@app.get("/sessions/{session_id}")
async def inspect_session(session_id: UUID) -> object:
    return await _call(get_engine().inspect, session_id)


@app.post("/sessions/{session_id}/executions")
async def execute(session_id: UUID, body: ExecuteRequest) -> object:
    return await _call(get_engine().execute_existing, session_id, body)


@app.delete("/sessions/{session_id}")
async def close_session(
    session_id: UUID, generation: int | None = Query(default=None, ge=1)
) -> dict[str, str]:
    await _call(get_engine().close, session_id, generation)
    return {"status": "closed"}


@app.post("/sessions/{session_id}/cancel")
async def cancel(session_id: UUID, body: CancelRequest) -> dict[str, str]:
    await _call(get_engine().cancel, session_id, body.generation, body.execution_id)
    return {"status": "cancelled"}
