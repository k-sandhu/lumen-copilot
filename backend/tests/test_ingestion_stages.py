"""Fault-injected stage boundaries, configuration isolation and safe reuse."""

from __future__ import annotations

from collections import Counter

import pytest

from app.domain.ingestion_stages import STAGES, StageOutput
from app.services.ingestion_stages import CheckpointPipeline


class Store:
    def __init__(self) -> None:
        self.rows: dict[str, StageOutput] = {}

    async def get(self, stage: str, fingerprint: str) -> StageOutput | None:
        row = self.rows.get(stage)
        return row if row is not None and row.fingerprint == fingerprint else None

    async def save(self, output: StageOutput) -> None:
        self.rows[output.stage] = output
        for stage in STAGES[STAGES.index(output.stage) + 1 :]:
            self.rows.pop(stage, None)

    async def ensure_owned(self) -> None:
        return None


@pytest.mark.parametrize("stage", STAGES)
@pytest.mark.parametrize("boundary", ["before_compute", "after_compute", "after_commit"])
async def test_fault_at_every_stage_boundary_resumes_without_duplicate_outputs(
    stage, boundary
) -> None:
    store = Store()
    calls: Counter[str] = Counter()
    tripped = False

    def fault(point: str, name: str) -> None:
        nonlocal tripped
        if point == boundary and name == stage and not tripped:
            tripped = True
            raise RuntimeError("injected crash")

    async def run(hook) -> None:
        pipeline = CheckpointPipeline(store, fault=hook)
        upstream = "a" * 64
        for name in STAGES:

            async def compute(name=name):
                calls[name] += 1
                return {"result": name}

            output = await pipeline.run(
                name, upstream=upstream, config={"version": 1}, compute=compute
            )
            upstream = output.output_sha256

    with pytest.raises(RuntimeError, match="injected"):
        await run(fault)
    prior = dict(calls)
    await run(None)
    assert set(store.rows) == set(STAGES)
    for name in STAGES[: STAGES.index(stage)]:
        assert calls[name] == prior[name] == 1
    if boundary == "after_commit":
        assert calls[stage] == 1
    assert len(store.rows) == len(STAGES)


async def test_downstream_config_change_reuses_extraction_but_invalidates_dependents() -> None:
    store = Store()
    calls: Counter[str] = Counter()

    async def run(chunk_size: int, model: str) -> None:
        pipeline = CheckpointPipeline(store)
        upstream = "a" * 64
        for name in STAGES:

            async def compute(name=name):
                calls[name] += 1
                return {"stage": name, "chunk_size": chunk_size if name == "chunk" else None}

            config = (
                {"size": chunk_size}
                if name == "chunk"
                else {"model": model}
                if name == "embed"
                else {}
            )
            output = await pipeline.run(name, upstream=upstream, config=config, compute=compute)
            upstream = output.output_sha256

    await run(10, "one")
    await run(20, "one")
    await run(20, "two")
    assert calls["extract"] == 1
    assert calls["chunk"] == 2
    assert calls["embed"] == 3


async def test_corrupted_cached_output_is_recomputed() -> None:
    store = Store()
    pipeline = CheckpointPipeline(store)
    calls = 0

    async def compute():
        nonlocal calls
        calls += 1
        return {"text": "exact"}

    output = await pipeline.run("extract", upstream="a" * 64, config={}, compute=compute)
    store.rows["extract"] = StageOutput(
        output.stage, output.fingerprint, "0" * 64, output.payload_json
    )
    await pipeline.run("extract", upstream="a" * 64, config={}, compute=compute)
    assert calls == 2


async def test_reduced_output_budget_cannot_reuse_oversized_cache() -> None:
    from app.domain.ingestion_stages import StageOutputInvalid

    store = Store()

    async def compute():
        return {"text": "a" * 100}

    await CheckpointPipeline(store).run("extract", upstream="a" * 64, config={}, compute=compute)
    with pytest.raises(StageOutputInvalid, match="budget"):
        await CheckpointPipeline(store, max_output_bytes=10).run(
            "extract", upstream="a" * 64, config={}, compute=compute
        )


async def test_ocr_incomplete_checkpoint_recomputes_then_reuses_completed_result():
    import json

    store = Store()
    pipeline = CheckpointPipeline(store)
    incomplete = True
    calls = 0

    async def compute():
        nonlocal calls
        calls += 1
        return {"needs_ocr": incomplete}

    def reusable(output):
        return json.loads(output.payload_json)["needs_ocr"] is False

    first = await pipeline.run(
        "ocr", upstream="a" * 64, config={}, compute=compute, reuse_if=reusable
    )
    assert json.loads(first.payload_json)["needs_ocr"] is True
    incomplete = False
    second = await pipeline.run(
        "ocr", upstream="a" * 64, config={}, compute=compute, reuse_if=reusable
    )
    third = await pipeline.run(
        "ocr", upstream="a" * 64, config={}, compute=compute, reuse_if=reusable
    )
    assert second == third and calls == 2
