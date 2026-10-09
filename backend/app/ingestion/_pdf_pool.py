"""Bounded supervised execution slots. Each slot recycles its process per document."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import time
from dataclasses import asdict
from pathlib import Path
from typing import Protocol

from app.domain.native_runtime import RuntimeBudget
from app.ingestion._pdf_worker import read_exact, read_frame, write_frame


class Cancellation(Protocol):
    def is_cancelled(self) -> bool: ...


class PdfWorkerError(Exception):
    """Stable safe error; never attach document/native exception payloads."""

    def __init__(self, code: str, *, peak_rss_bytes: int = 0) -> None:
        self.code = code
        self.peak_rss_bytes = peak_rss_bytes
        super().__init__(f"PDF worker: {code}")


class PdfProcessPool:
    def __init__(
        self,
        *,
        workers: int | None = None,
        memory_cap_bytes: int = 256 * 1024 * 1024,
        total_memory_bytes: int = 512 * 1024 * 1024,
    ) -> None:
        capacity = total_memory_bytes // memory_cap_bytes if memory_cap_bytes > 0 else 0
        width = workers if workers is not None else min(os.cpu_count() or 1, 2, capacity)
        if width < 1 or width > capacity or width > 64:
            raise ValueError("PDF pool exceeds memory budget")
        self.workers = width
        self.memory_cap_bytes = memory_cap_bytes
        self._slots = threading.BoundedSemaphore(width)

    def extract(
        self,
        data: bytes,
        *,
        library: str,
        engine: str,
        budget: RuntimeBudget,
        cancellation: Cancellation | None = None,
    ) -> tuple[str, int]:
        deadline = time.monotonic() + budget.timeout_ms / 1000

        def checkpoint() -> None:
            if cancellation is not None and cancellation.is_cancelled():
                raise PdfWorkerError("cancelled")
            if time.monotonic() >= deadline:
                raise PdfWorkerError("timed_out")

        if len(data) > budget.max_input_bytes:
            raise PdfWorkerError("input_budget")
        checkpoint()
        while not self._slots.acquire(timeout=0.01):
            checkpoint()
        process = None
        threads: list[threading.Thread] = []
        try:
            checkpoint()
            # Do not transmit credentials, storage/network configuration or source paths.
            environment = {
                "PYTHONPATH": str(Path(__file__).resolve().parents[2]),
                "PYTHONIOENCODING": "utf-8",
            }
            process = subprocess.Popen(
                [
                    sys.executable,
                    "-m",
                    "app.ingestion._pdf_worker",
                    "--memory-bytes",
                    str(self.memory_cap_bytes),
                ],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                env=environment,
                creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0,
            )
            assert process.stdin is not None and process.stdout is not None
            output_cap = min(
                budget.max_memory_bytes // 2, budget.max_output_chars * 16 + 1024 * 1024
            )
            config = json.dumps(
                {
                    "library": library,
                    "engine": engine,
                    "budget": asdict(budget),
                    "output_cap": output_cap,
                }
            ).encode()
            response: list[bytes] = []
            failures: list[BaseException] = []
            done = threading.Event()

            def exchange() -> None:
                try:
                    assert (
                        process is not None
                        and process.stdout is not None
                        and process.stdin is not None
                    )
                    if read_exact(process.stdout, 1) != b"R":
                        raise PdfWorkerError("isolation_unavailable")
                    write_frame(process.stdin, config)
                    write_frame(process.stdin, data)
                    process.stdin.close()
                    response.append(read_frame(process.stdout, output_cap))
                except BaseException as error:
                    failures.append(error)
                finally:
                    done.set()

            thread = threading.Thread(target=exchange, daemon=True)
            threads.append(thread)
            thread.start()
            while not done.wait(0.01):
                checkpoint()
            checkpoint()
            if failures:
                if isinstance(failures[0], PdfWorkerError):
                    raise failures[0]
                raise PdfWorkerError("worker_crashed")
            raw = json.loads(response[0])
            peak = int(raw.get("peak_rss_bytes", 0))
            if raw["code"] is not None:
                raise PdfWorkerError(raw["code"], peak_rss_bytes=peak)
            return str(raw["result"]), peak
        finally:
            if process is not None:
                if process.poll() is None:
                    process.kill()
                process.wait(timeout=5)
                for thread in threads:
                    thread.join(timeout=5)
                for stream in (process.stdin, process.stdout):
                    if stream is not None:
                        stream.close()
            self._slots.release()
