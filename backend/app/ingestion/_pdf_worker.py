"""Private stdio worker: establish OS limits before the PDF engine or input arrives."""

from __future__ import annotations

import argparse
import ctypes
import json
import struct
import sys
from typing import IO, Any

_JOB: Any = None


def apply_memory_limit(cap: int) -> None:
    global _JOB
    if cap < 1:
        raise ValueError("invalid worker memory cap")
    if sys.platform == "win32":
        from ctypes import wintypes

        class Basic(ctypes.Structure):
            _fields_ = [
                ("PerProcessUserTimeLimit", ctypes.c_int64),
                ("PerJobUserTimeLimit", ctypes.c_int64),
                ("LimitFlags", wintypes.DWORD),
                ("MinimumWorkingSetSize", ctypes.c_size_t),
                ("MaximumWorkingSetSize", ctypes.c_size_t),
                ("ActiveProcessLimit", wintypes.DWORD),
                ("Affinity", ctypes.c_size_t),
                ("PriorityClass", wintypes.DWORD),
                ("SchedulingClass", wintypes.DWORD),
            ]

        class Io(ctypes.Structure):
            _fields_ = [
                (name, ctypes.c_uint64)
                for name in (
                    "ReadOperationCount",
                    "WriteOperationCount",
                    "OtherOperationCount",
                    "ReadTransferCount",
                    "WriteTransferCount",
                    "OtherTransferCount",
                )
            ]

        class Extended(ctypes.Structure):
            _fields_ = [
                ("BasicLimitInformation", Basic),
                ("IoInfo", Io),
                ("ProcessMemoryLimit", ctypes.c_size_t),
                ("JobMemoryLimit", ctypes.c_size_t),
                ("PeakProcessMemoryUsed", ctypes.c_size_t),
                ("PeakJobMemoryUsed", ctypes.c_size_t),
            ]

        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
        kernel.CreateJobObjectW.restype = wintypes.HANDLE
        kernel.SetInformationJobObject.argtypes = [
            wintypes.HANDLE,
            ctypes.c_int,
            ctypes.c_void_p,
            wintypes.DWORD,
        ]
        kernel.SetInformationJobObject.restype = wintypes.BOOL
        kernel.GetCurrentProcess.restype = wintypes.HANDLE
        kernel.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
        kernel.AssignProcessToJobObject.restype = wintypes.BOOL
        _JOB = kernel.CreateJobObjectW(None, None)
        info = Extended()
        # PROCESS_MEMORY | KILL_ON_JOB_CLOSE | ACTIVE_PROCESS (no child processes).
        info.BasicLimitInformation.LimitFlags = 0x100 | 0x2000 | 0x8
        info.BasicLimitInformation.ActiveProcessLimit = 1
        info.ProcessMemoryLimit = cap
        if not _JOB or not kernel.SetInformationJobObject(
            _JOB, 9, ctypes.byref(info), ctypes.sizeof(info)
        ):
            raise OSError("worker limit unavailable")
        if not kernel.AssignProcessToJobObject(_JOB, kernel.GetCurrentProcess()):
            raise OSError("worker limit unavailable")
    elif sys.platform == "linux":
        import resource

        resource.setrlimit(resource.RLIMIT_AS, (cap, cap))
        resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
    else:
        raise OSError("worker limit unavailable")


def read_exact(stream: IO[bytes], size: int) -> bytes:
    parts = bytearray()
    while len(parts) < size:
        chunk = stream.read(min(size - len(parts), 65536))
        if not chunk:
            raise EOFError("worker frame incomplete")
        parts.extend(chunk)
    return bytes(parts)


def read_frame(stream: IO[bytes], cap: int) -> bytes:
    size = struct.unpack("!Q", read_exact(stream, 8))[0]
    if size > cap:
        raise ValueError("worker frame exceeds budget")
    return read_exact(stream, size)


def write_frame(stream: IO[bytes], value: bytes) -> None:
    stream.write(struct.pack("!Q", len(value)))
    stream.write(value)
    stream.flush()


def peak_rss() -> int:
    if sys.platform != "win32":
        import resource

        peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
        return int(peak if sys.platform == "darwin" else peak * 1024)
    from ctypes import wintypes

    class Counters(ctypes.Structure):
        _fields_ = [("cb", wintypes.DWORD), ("PageFaultCount", wintypes.DWORD)] + [
            (name, ctypes.c_size_t)
            for name in (
                "PeakWorkingSetSize",
                "WorkingSetSize",
                "QuotaPeakPagedPoolUsage",
                "QuotaPagedPoolUsage",
                "QuotaPeakNonPagedPoolUsage",
                "QuotaNonPagedPoolUsage",
                "PagefileUsage",
                "PeakPagefileUsage",
            )
        ]

    kernel = ctypes.WinDLL("kernel32")
    kernel.GetCurrentProcess.restype = wintypes.HANDLE
    api = ctypes.WinDLL("psapi")
    api.GetProcessMemoryInfo.argtypes = [wintypes.HANDLE, ctypes.c_void_p, wintypes.DWORD]
    value = Counters()
    value.cb = ctypes.sizeof(value)
    if not api.GetProcessMemoryInfo(kernel.GetCurrentProcess(), ctypes.byref(value), value.cb):
        raise OSError("RSS unavailable")
    return int(value.PeakWorkingSetSize)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--memory-bytes", type=int, required=True)
    args = parser.parse_args()
    output = sys.stdout.buffer
    try:
        apply_memory_limit(args.memory_bytes)
        output.write(b"R")
        output.flush()
        config = json.loads(read_frame(sys.stdin.buffer, 65536))
        budget = config["budget"]
        data = read_frame(sys.stdin.buffer, budget["max_input_bytes"])
        # Native imports stay at the single facade chokepoint.
        from app.ingestion.native import _pdf_worker_extract

        result = _pdf_worker_extract(data, config["library"], budget, config["engine"])
        payload = {"code": None, "result": result, "peak_rss_bytes": peak_rss()}
        encoded = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        if len(encoded) > config["output_cap"]:
            raise MemoryError
    except BaseException as error:
        code = {
            "DocIntelEncryptedError": "encrypted",
            "DocIntelParseError": "parse_error",
            "DocIntelUnsupportedError": "unsupported",
            "DocIntelBudgetError": "budget",
            "DocIntelCancelledError": "cancelled",
            "MemoryError": "memory_limit",
        }.get(type(error).__name__, "worker_failed")
        if type(error).__name__ == "PdfWorkerError":
            code = str(error.code)  # type: ignore[attr-defined]
        encoded = json.dumps({"code": code, "peak_rss_bytes": peak_rss()}).encode()
    write_frame(output, encoded)


if __name__ == "__main__":
    main()
