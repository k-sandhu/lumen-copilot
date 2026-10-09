"""Valid generated notebook measurement while malformed foundation fixture awaits #731."""

import base64
import hashlib
import json
from dataclasses import asdict
from pathlib import Path

from tests.eval.docintel.candidates import child
from tests.eval.docintel.metrics import Gold


def main() -> None:
    data = json.dumps(
        {
            "nbformat": 4,
            "metadata": {},
            "cells": [
                {"cell_type": "markdown", "source": "# Intro\n\nNorth -120 kg"},
                {
                    "cell_type": "code",
                    "source": "raise RuntimeError('inert')",
                    "outputs": [{"output_type": "stream", "text": "PageTwo 東京"}],
                },
            ],
        },
        ensure_ascii=False,
    ).encode()
    gold = Gold(
        facts=("Intro", "North", "-120", "kg", "PageTwo", "東京"),
        associations=(("North", "-120", "kg"),),
        order=("Intro", "North", "PageTwo"),
    )
    rows = []
    for arm in ("python-baseline", "native-candidate"):
        payload = {
            "arm": arm,
            "family": "notebook",
            "mime": "application/x-ipynb+json",
            "data": base64.b64encode(data).decode(),
            "gold": asdict(gold),
        }
        rows.append(
            {
                "fixture": "valid-generated-notebook",
                "sha256": hashlib.sha256(data).hexdigest(),
                **child(payload),
            }
        )
    report = {
        "rows": rows,
        "promotion": "held for owner evaluation",
        "measurement": "tiny in-process correctness sample; no performance claim",
    }
    Path("../docs/ingestion/reports/notebook-valid.json").write_text(
        json.dumps(report, indent=2) + "\n", encoding="utf-8"
    )
    print(json.dumps(rows, indent=2))


if __name__ == "__main__":
    main()
