"""Internal FastAPI surface for reusable sandbox lifecycle and execution."""

from __future__ import annotations

import asyncio
import os
from collections.abc import AsyncIterator
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
from functools import lru_cache
from uuid import UUID

from fastapi import APIRouter, Depends, FastAPI, HTTPException, Query

from lumen_sandbox_runner.auth import configured_token, require_token
from lumen_sandbox_runner.engine import DockerSandboxEngine, RunnerError
from lumen_sandbox_runner.models import CancelRequest, EnsureSessionRequest, ExecuteRequest

# --- Thread pools: teardown must never queue behind execution (#519) -----------
#
# Every route body runs a BLOCKING docker-py call, dispatched to a thread. Until now
# that was `asyncio.to_thread`, i.e. the interpreter's default executor, sized
# `min(32, cpu_count + 4)` — SIX threads on a 2-vCPU host. `exec_run` blocks until
# the model's process exits and there is no wall clock anywhere, so six chats running
# `while True: pass` occupy every thread. New requests queue for all tenants, and
# `POST /sessions/{id}/cancel` — the documented and only recovery — queues behind the
# very executions it exists to stop, and never starts. Recovery degraded to
# `docker kill` on the host.
#
# That is not merely a DoS: ADR-0020 accepts UNBOUNDED execution explicitly on the
# premise that cancellation is always available. Sharing one pool made that premise
# false, so the accepted risk was larger than the ADR said it was.
#
# Two pools, and the split is the whole point: teardown has its own threads, so a
# saturated execution pool cannot delay it. Teardown calls are short and bounded
# (`kill`, `rm`), so a small pool suffices.
_EXECUTION_THREADS = max(4, int(os.environ.get("SANDBOX_RUNNER_EXECUTION_THREADS", "16")))
_TEARDOWN_THREADS = 4

_execution_pool = ThreadPoolExecutor(
    max_workers=_EXECUTION_THREADS, thread_name_prefix="sandbox-exec"
)
_teardown_pool = ThreadPoolExecutor(
    max_workers=_TEARDOWN_THREADS, thread_name_prefix="sandbox-teardown"
)


@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncIterator[None]:
    """Refuse to serve without a shared secret (#508).

    A runner that boots without a token would serve the Docker socket to anything on
    its network. Checking at startup means a misconfigured deploy fails visibly at
    `docker compose up` rather than at the first execution — and, because the
    healthcheck never turns green, it fails where an operator is already looking.
    """
    configured_token()
    try:
        yield
    finally:
        # `cancel_futures=False`: a teardown already dispatched should finish, or a
        # restart could strand the container it was removing.
        _execution_pool.shutdown(wait=False, cancel_futures=True)
        _teardown_pool.shutdown(wait=True)


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


async def _call(method: object, *args: object, pool: ThreadPoolExecutor) -> object:
    """Run a blocking engine call on an EXPLICIT pool (#519).

    The pool is a required keyword, not a default, so adding an endpoint forces the
    author to decide which one it belongs to. A default would silently put the next
    teardown-shaped route on the execution pool, which is how this class of bug
    returns.
    """
    loop = asyncio.get_running_loop()
    try:
        return await loop.run_in_executor(pool, lambda: method(*args))  # type: ignore[operator]
    except RunnerError as exc:
        raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}


@sessions.put("/sessions/{session_id}")
async def ensure_session(session_id: UUID, body: EnsureSessionRequest) -> object:
    return await _call(get_engine().ensure, session_id, body, pool=_execution_pool)


@sessions.get("/sessions/{session_id}")
async def inspect_session(session_id: UUID) -> object:
    return await _call(get_engine().inspect, session_id, pool=_execution_pool)


@sessions.post("/sessions/{session_id}/executions")
async def execute(session_id: UUID, body: ExecuteRequest) -> object:
    return await _call(get_engine().execute_existing, session_id, body, pool=_execution_pool)


@sessions.delete("/sessions/{session_id}")
async def close_session(
    session_id: UUID, generation: int | None = Query(default=None, ge=1)
) -> dict[str, str]:
    await _call(get_engine().close, session_id, generation, pool=_teardown_pool)
    return {"status": "closed"}


@sessions.post("/sessions/{session_id}/cancel")
async def cancel(session_id: UUID, body: CancelRequest) -> dict[str, str]:
    await _call(
        get_engine().cancel, session_id, body.generation, body.execution_id, pool=_teardown_pool
    )
    return {"status": "cancelled"}


app.include_router(sessions)
