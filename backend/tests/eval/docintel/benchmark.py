"""Offline, subprocess-isolated Python baseline and native model measurements."""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import platform
import subprocess
import sys
import time
from dataclasses import asdict
from pathlib import Path
from typing import Any

from tests.eval.docintel.fixtures import Fixture, corpus
from tests.eval.docintel.metrics import Gold, evaluate

_MAX_INPUT = 32 * 1024 * 1024


def _rss() -> int:
    if sys.platform == "linux":
        # Per-address-space peak avoids a fork/exec predecessor's rusage watermark.
        with Path("/proc/self/status").open(encoding="utf-8") as status:
            for line in status:
                if line.startswith("VmHWM:"):
                    return int(line.split()[1]) * 1024
        raise RuntimeError("process peak memory metric unavailable")
    if sys.platform == "win32":
        import ctypes
        from ctypes import wintypes

        class Counters(ctypes.Structure):
            _fields_ = [
                ("cb", wintypes.DWORD),
                ("PageFaultCount", wintypes.DWORD),
                ("PeakWorkingSetSize", ctypes.c_size_t),
                ("WorkingSetSize", ctypes.c_size_t),
                ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
                ("QuotaPagedPoolUsage", ctypes.c_size_t),
                ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
                ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
                ("PagefileUsage", ctypes.c_size_t),
                ("PeakPagefileUsage", ctypes.c_size_t),
            ]

        counters = Counters()
        counters.cb = ctypes.sizeof(counters)
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.GetCurrentProcess.restype = wintypes.HANDLE
        psapi = ctypes.WinDLL("psapi", use_last_error=True)
        psapi.GetProcessMemoryInfo.argtypes = [
            wintypes.HANDLE,
            ctypes.POINTER(Counters),
            wintypes.DWORD,
        ]
        if not psapi.GetProcessMemoryInfo(
            kernel.GetCurrentProcess(), ctypes.byref(counters), counters.cb
        ):
            raise ctypes.WinError(ctypes.get_last_error())
        return int(counters.PeakWorkingSetSize)
    import resource

    peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    return int(peak if sys.platform == "darwin" else peak * 1024)


def _child(payload: dict[str, Any]) -> dict[str, Any]:
    from app.ingestion.parsers import DocumentParseError, UnsupportedMimeTypeError, parse_document

    gold = Gold(
        **{
            key: tuple(tuple(g) if isinstance(g, list) else g for g in value)
            if isinstance(value, list)
            else value
            for key, value in payload["gold"].items()
        }
    )
    arm = payload["arm"]
    before = _rss()
    start = time.perf_counter()
    text = ""
    code = None
    spans = ()
    outcome = "indexed"
    accounted = None
    try:
        data = base64.b64decode(payload["data"], validate=True)
        if len(data) > _MAX_INPUT:
            outcome = "failed"
            code = "input_budget"
        elif arm == "python-baseline":
            text = parse_document(data, mime_type=payload["mime"])
            outcome = "indexed" if text.strip() else "empty"
            # Existing chunker exact slices are checked, not invented page provenance.
            from app.ingestion.chunking import chunk_text

            chunks = chunk_text(text, chunk_size=1200, overlap=200)
            spans = tuple((c.char_start, c.char_end, c.text) for c in chunks)
        elif arm == "native-model-control":
            # This control measures rendering only. It is never an extraction candidate.
            import lumen_docintel

            model = {
                "blocks": [{"id": "control", "kind": "paragraph", "text": data.decode("utf-8")}]
            }
            rendered = json.loads(lumen_docintel.render_document(json.dumps(model)))
            text = rendered["rendered_text"]
            expected_text = model["blocks"][0]["text"]
            spans = tuple(
                (s["char_start"], s["char_end"], expected_text) for s in rendered["spans"]
            )
        elif arm == "native-executor-control":
            from app.ingestion.native import NativeExecutor

            executor = NativeExecutor(threads=payload["threads"], max_documents=1)
            units = tuple(data.decode("utf-8").splitlines())
            computed = executor.run_units(units)
            if computed.units != units:
                raise ValueError("executor changed input")
            text = "\n".join(computed.units)
            spans = ((0, len(text), data.decode("utf-8")),)
            accounted = computed.peak_accounted_bytes
        else:
            outcome = "unsupported"
            code = "native_parser_not_landed"
    except UnsupportedMimeTypeError:
        outcome = "unsupported"
        code = "unsupported_format"
    except DocumentParseError:
        outcome = "failed"
        code = "parse_error"
    except (
        Exception
    ) as exc:  # benchmark isolates implementation failures; never stores raw exception text
        outcome = "failed"
        code = type(exc).__name__
    elapsed = time.perf_counter() - start
    peak = _rss()
    return {
        "arm": arm,
        "outcome": outcome,
        "code": code,
        "seconds": elapsed,
        "input_bytes_per_second": len(data) / elapsed if elapsed else None,
        "output_chars_per_second": len(text) / elapsed if elapsed else None,
        "peak_rss_bytes": peak,
        "peak_accounted_bytes": accounted,
        "threads": payload.get("threads"),
        "peak_rss_increment_bytes": max(0, peak - before),
        "score": asdict(evaluate(text, gold, spans=spans, outcome=outcome)),
    }


def _isolated(fixture: Fixture, arm: str, *, threads: int | None = None) -> dict[str, Any]:
    payload = {
        "data": base64.b64encode(fixture.data).decode("ascii"),
        "gold": asdict(fixture.gold),
        "mime": fixture.mime,
        "arm": arm,
        "threads": threads,
    }
    try:
        child = subprocess.run(
            [sys.executable, "-m", "tests.eval.docintel.benchmark", "--child"],
            input=json.dumps(payload),
            text=True,
            capture_output=True,
            timeout=35,
            check=False,
        )
        if child.returncode == 0:
            return json.loads(child.stdout)
        code = "child_exit"
    except subprocess.TimeoutExpired:
        code = "time_budget"
    return {
        "arm": arm,
        "outcome": "failed",
        "code": code,
        "seconds": None,
        "peak_rss_bytes": None,
        "peak_rss_increment_bytes": None,
        "score": asdict(evaluate("", fixture.gold, outcome="failed")),
    }


def _external(manifest: Path) -> list[Fixture]:
    # Caller supplies only authorized local files and annotations. Report never includes paths/text.
    if manifest.stat().st_size > 4 * 1024 * 1024:
        raise ValueError("external manifest exceeds 4 MiB annotation budget")
    rows = json.loads(manifest.read_text(encoding="utf-8"))
    result = []
    for row in rows:
        if not {"path", "format", "mime", "gold"} <= row.keys():
            raise ValueError("external fixtures require path, format, mime and gold annotations")
        path = manifest.parent / row["path"]
        digest = hashlib.sha256()
        with path.open("rb") as stream:
            while block := stream.read(1024 * 1024):
                digest.update(block)
        ident = "external-" + digest.hexdigest()
        if path.stat().st_size > _MAX_INPUT:
            # Budget rows are kept without reading oversized files into memory.
            result.append(
                Fixture(
                    ident,
                    row["format"],
                    row["mime"],
                    b"",
                    Gold(),
                    case="external-input-budget",
                    expected="failed",
                    fidelity_eligible=False,
                )
            )
        else:
            g = row["gold"]
            result.append(
                Fixture(
                    ident,
                    row["format"],
                    row["mime"],
                    path.read_bytes(),
                    Gold(
                        tuple(g.get("facts", ())),
                        tuple(tuple(v) for v in g.get("associations", ())),
                        tuple(g.get("order", ())),
                        g.get("native_regions", 0),
                        tuple(tuple(pair) for pair in g.get("headers", ())),
                        tuple(tuple(region) for region in g.get("regions", ())),
                    ),
                    language=row.get("language", "unknown"),
                    case="external",
                )
            )
    return result


def run(*, external: Path | None = None, model_control: bool = False) -> dict[str, Any]:
    fixtures = corpus() + (_external(external) if external else [])
    rows = []
    for fixture in fixtures:
        for arm in ("python-baseline", "native-extraction"):
            measured = (
                _isolated(fixture, arm)
                if fixture.case != "external-input-budget"
                else {
                    "arm": arm,
                    "outcome": "failed",
                    "code": "input_budget",
                    "seconds": None,
                    "peak_rss_bytes": None,
                    "peak_rss_increment_bytes": None,
                    "score": asdict(evaluate("", fixture.gold, outcome="failed")),
                }
            )
            # External ids carry the streamed digest for over-budget rows.
            digest = (
                fixture.id.removeprefix("external-")
                if fixture.id.startswith("external-")
                else fixture.sha256
            )
            rows.append(
                {
                    "fixture": fixture.id,
                    "sha256": digest,
                    "format": fixture.format,
                    "language": fixture.language,
                    "case": fixture.case,
                    "expected": fixture.expected,
                    "fidelity_eligible": fixture.fidelity_eligible,
                    **measured,
                }
            )
    controls = []
    if model_control:
        control = Fixture(
            "model-unicode",
            "text",
            "text/plain",
            "Intro\nNorth -120 kg\nPageTwo 😀世界".encode(),
            Gold(facts=("Intro", "North", "-120", "kg", "PageTwo")),
        )
        controls.append(_isolated(control, "native-model-control"))
        units = "\n".join("A😀世界" * 128 for _ in range(1024))
        workload = Fixture("executor-unicode", "text", "text/plain", units.encode(), Gold())
        for threads in (1, 2, 4):
            controls.append(_isolated(workload, "native-executor-control", threads=threads))
    groups = {}
    for row in rows:
        key = (
            f'{row["arm"]}/{row["format"]}/{row["language"]}/'
            f'{row["case"]}/{row["outcome"]}/{row["code"]}'
        )
        group = groups.setdefault(
            key,
            {
                "documents": 0,
                "indexed": 0,
                "failed": 0,
                "unsupported": 0,
                "empty": 0,
                "fact_coverage_sum": 0.0,
            },
        )
        group["documents"] += 1
        group[row["outcome"]] += 1
        group["fact_coverage_sum"] += row["score"]["fact_coverage"]
    for group in groups.values():
        group["fact_coverage"] = group.pop("fact_coverage_sum") / group["documents"]
    root = Path(__file__).resolve().parents[3]
    parser = root / "app/ingestion/parsers.py"
    try:
        commit = subprocess.check_output(
            ["git", "merge-base", "origin/main", "HEAD"], cwd=root, text=True
        ).strip()
    except (subprocess.CalledProcessError, FileNotFoundError):
        commit = "unavailable"
    return {
        "schema_version": 1,
        "corpus_version": 1,
        "baseline_commit": commit,
        "parser_sha256": hashlib.sha256(parser.read_bytes()).hexdigest(),
        "environment": {"python": platform.python_version(), "platform": sys.platform},
        "rows": rows,
        "breakdown": groups,
        "model_controls": controls,
        "promotion": (
            "blocked: native format parsers and " "owner-approved held-out thresholds required"
        ),
        "speedup": None,
        "answer_level_quality": "not measured",
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--report", type=Path)
    parser.add_argument("--external", type=Path)
    parser.add_argument("--model-control", action="store_true")
    parser.add_argument("--child", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args.child:
        print(json.dumps(_child(json.load(sys.stdin)), ensure_ascii=False))
        return
    if args.report is None:
        parser.error("--report is required")
    report = run(external=args.external, model_control=args.model_control)
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(
        json.dumps(report, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    print(f'{len(report["rows"])} document-arm rows; {report["promotion"]}')


if __name__ == "__main__":
    main()
