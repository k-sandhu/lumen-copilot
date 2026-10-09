"""Subprocess-isolated fidelity measurements for registered Rust candidates."""

from __future__ import annotations

import argparse
import base64
import json
import subprocess
import sys
import time
from dataclasses import asdict
from pathlib import Path
from typing import Any

from tests.eval.docintel.benchmark import _child as baseline_child
from tests.eval.docintel.benchmark import _rss
from tests.eval.docintel.fixtures import corpus
from tests.eval.docintel.metrics import Gold, evaluate


def child(payload: dict[str, Any]) -> dict[str, Any]:
    if payload["arm"] == "python-baseline":
        return baseline_child(payload)
    from app.ingestion.native import extract_candidate

    gold = Gold(
        **{
            key: tuple(tuple(v) if isinstance(v, list) else v for v in value)
            if isinstance(value, list)
            else value
            for key, value in payload["gold"].items()
        }
    )
    before = _rss()
    start = time.perf_counter()
    text = ""
    spans = ()
    outcome = "failed"
    code = None
    try:
        document = extract_candidate(
            base64.b64decode(payload["data"]), family=payload["family"], mime=payload["mime"]
        )
        text = document.rendered_text
        spans = tuple(
            (s.char_start, s.char_end, b.text)
            for s, b in zip(document.spans, document.blocks, strict=True)
        )
        native_outcome = json.loads(document.generation_json)["outcome"]
        outcome = "indexed" if native_outcome == "success" else native_outcome
    except Exception as exc:  # benchmark failures stay in denominator
        code = type(exc).__name__
    elapsed = time.perf_counter() - start
    return {
        "arm": payload["arm"],
        "outcome": outcome,
        "code": code,
        "seconds": elapsed,
        "input_bytes_per_second": len(base64.b64decode(payload["data"])) / elapsed,
        "peak_rss_bytes": _rss(),
        "peak_rss_increment_bytes": max(0, _rss() - before),
        "score": asdict(evaluate(text, gold, spans=spans, outcome=outcome)),
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--family")
    parser.add_argument("--report", type=Path)
    parser.add_argument("--child", action="store_true")
    args = parser.parse_args()
    if args.child:
        print(json.dumps(child(json.load(sys.stdin))))
        return
    if not args.report or not args.family:
        parser.error("--family and --report required")
    from app.ingestion.candidates import candidates

    candidate = next(c for c in candidates() if c.family == args.family)
    rows = []
    for fixture in corpus():
        if fixture.mime not in candidate.mimes or fixture.format not in candidate.formats:
            continue
        for arm in ("python-baseline", "native-candidate"):
            payload = {
                "arm": arm,
                "family": args.family,
                "mime": fixture.mime,
                "data": base64.b64encode(fixture.data).decode(),
                "gold": asdict(fixture.gold),
            }
            measured = subprocess.run(
                [sys.executable, "-m", "tests.eval.docintel.candidates", "--child"],
                input=json.dumps(payload),
                text=True,
                capture_output=True,
                timeout=60,
                check=False,
            )
            result = (
                json.loads(measured.stdout)
                if measured.returncode == 0
                else {"outcome": "failed", "code": "subprocess_failure"}
            )
            rows.append(
                {
                    "fixture": fixture.id,
                    "sha256": fixture.sha256,
                    "format": fixture.format,
                    "language": fixture.language,
                    "expected": fixture.expected,
                    "arm": arm,
                    **result,
                }
            )
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(
        json.dumps(
            {
                "rows": rows,
                "promotion": "held for owner evaluation",
                "answer_quality": "unmeasured",
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    print(json.dumps(rows, indent=2))


if __name__ == "__main__":
    main()
