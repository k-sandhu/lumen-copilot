"""Cancel must work while every execution thread is blocked (#519 part 3).

Route bodies are blocking docker-py calls dispatched to a thread pool. They all shared
the interpreter's default executor — `min(32, cpu_count + 4)`, i.e. SIX threads on a
2-vCPU host — and `exec_run` blocks until the model's process exits with no wall clock
anywhere. Six chats running `while True: pass` therefore occupied every thread, and
`POST /sessions/{id}/cancel` queued behind the very executions it exists to stop.

That is worse than a DoS. ADR-0020 accepts UNBOUNDED execution *explicitly on the
premise that cancellation is always available*; sharing one pool made that premise
false, so the risk the ADR accepted was larger than the ADR said.
"""

from __future__ import annotations

import asyncio
import threading

import pytest
from httpx import ASGITransport, AsyncClient

from lumen_sandbox_runner import main as runner_main
from lumen_sandbox_runner.auth import TOKEN_ENV, TOKEN_HEADER

_TOKEN = "t" * 48
_AUTH = {TOKEN_HEADER: _TOKEN}
_SESSION = "00000000-0000-0000-0000-000000000001"


@pytest.fixture(autouse=True)
def _token(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(TOKEN_ENV, _TOKEN)


class _BlockingEngine:
    """An engine whose executions hang until released — a `while True: pass` run."""

    def __init__(self) -> None:
        self.release = threading.Event()
        self.entered = threading.Semaphore(0)
        self.cancelled: list[str] = []

    def execute_existing(self, session_id: object, body: object) -> dict[str, object]:
        self.entered.release()
        # Blocks exactly like `exec_run` against a non-terminating process.
        self.release.wait(timeout=30)
        return {
            "status": "succeeded",
            "exit_code": 0,
            "stdout": "",
            "stderr": "",
            "duration_ms": 1,
            "image_digest": "sha256:abc",
            "output_files": [],
        }

    def cancel(self, session_id: object, generation: object, execution_id: object) -> None:
        self.cancelled.append(str(session_id))
        self.release.set()

    def close(self, session_id: object, generation: object = None) -> None:
        self.release.set()


async def test_cancel_succeeds_while_every_execution_thread_is_occupied(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Fill the execution pool completely, then cancel. It must not queue.

    The pool is shrunk to two threads rather than launching sixteen blocked requests:
    the property under test is "teardown does not share the execution pool", and that
    is independent of the pool's size. A test that depends on the production number
    would silently stop proving anything the moment someone tuned it.
    """
    engine = _BlockingEngine()
    monkeypatch.setattr(runner_main, "get_engine", lambda: engine)

    small = runner_main.ThreadPoolExecutor(max_workers=2, thread_name_prefix="test-exec")
    monkeypatch.setattr(runner_main, "_execution_pool", small)
    try:
        transport = ASGITransport(app=runner_main.app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            body = {
                "generation": 1,
                "execution_id": "00000000-0000-0000-0000-000000000002",
                "code": "while True: pass",
                "packages": [],
            }
            blocked = [
                asyncio.create_task(
                    client.post(f"/sessions/{_SESSION}/executions", json=body, headers=_AUTH)
                )
                for _ in range(2)
            ]

            # Both execution threads are genuinely inside the engine — not merely
            # scheduled — before we ask for teardown.
            for _ in range(2):
                assert await asyncio.to_thread(
                    engine.entered.acquire, True, 10
                ), "an execution never reached the engine"

            # The whole point: this must complete now, not after the runs finish.
            cancelled = await asyncio.wait_for(
                client.post(
                    f"/sessions/{_SESSION}/cancel",
                    json={"generation": 1, "execution_id": "00000000-0000-0000-0000-000000000002"},
                    headers=_AUTH,
                ),
                timeout=10,
            )

            assert cancelled.status_code == 200
            assert cancelled.json() == {"status": "cancelled"}
            assert engine.cancelled == [_SESSION]

            await asyncio.gather(*blocked)
    finally:
        engine.release.set()
        small.shutdown(wait=False, cancel_futures=True)


def test_teardown_and_execution_do_not_share_a_pool() -> None:
    """The structural guarantee behind the test above.

    Asserted directly as well, because the behavioural test could keep passing for the
    wrong reason — a fast fake, a generous timeout — long after someone merged the two
    pools back together.
    """
    assert runner_main._execution_pool is not runner_main._teardown_pool
    assert runner_main._TEARDOWN_THREADS >= 1


# --- #519 part 2: concurrent collections are bounded process-wide --------------


def test_concurrent_collections_cannot_exceed_the_declared_budget() -> None:
    """Every other output bound is per-EXECUTION; this one is per-PROCESS.

    Locks are per-session, so executions in different sessions collect concurrently,
    and one max-size collection peaks at roughly 4x its byte budget (raw member,
    accumulated base64, the JSON string, its encoded bytes) — ~256 MiB against the
    runner's 1 GiB compose limit. Three or four concurrent tenants at the ceiling
    therefore OOM the Docker socket holder: exactly what the per-execution budget was
    added to prevent, reached from a different direction.

    Asserted on the observed PEAK rather than on the semaphore's value, because the
    property that matters is how many collections are simultaneously holding memory —
    not what a counter says.
    """
    from lumen_sandbox_runner import engine as engine_module

    in_flight = 0
    peak = 0
    guard = threading.Lock()
    hold = threading.Event()
    entered = threading.Semaphore(0)

    def fake_collect(container: object, output_dir: str, byte_budget: int) -> tuple[list, bool]:
        nonlocal in_flight, peak
        with guard:
            in_flight += 1
            peak = max(peak, in_flight)
        entered.release()
        hold.wait(timeout=20)
        with guard:
            in_flight -= 1
        return [], False

    original = engine_module.DockerSandboxEngine._collect_outputs_unlocked
    engine_module.DockerSandboxEngine._collect_outputs_unlocked = staticmethod(fake_collect)
    try:
        attempts = engine_module._MAX_CONCURRENT_COLLECTIONS + 4
        threads = [
            threading.Thread(
                target=engine_module.DockerSandboxEngine._collect_outputs,
                args=(object(), "/out", 1024),
            )
            for _ in range(attempts)
        ]
        for t in threads:
            t.start()
        # Wait until the permitted number are genuinely inside, then confirm the rest
        # are held OUT rather than merely slow to start.
        for _ in range(engine_module._MAX_CONCURRENT_COLLECTIONS):
            assert entered.acquire(timeout=10), "a permitted collection never started"
        assert not entered.acquire(timeout=0.5), "more collections ran than permits allow"

        hold.set()
        for t in threads:
            t.join(timeout=20)

        assert peak <= engine_module._MAX_CONCURRENT_COLLECTIONS, (
            f"{peak} collections held memory at once, above the declared "
            f"{engine_module._MAX_CONCURRENT_COLLECTIONS}"
        )
        # And the permit is released, not leaked — a leak would wedge the runner.
        assert engine_module._collection_permits.acquire(timeout=5)
        engine_module._collection_permits.release()
    finally:
        hold.set()
        engine_module.DockerSandboxEngine._collect_outputs_unlocked = original
