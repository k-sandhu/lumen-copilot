"""Checkpoint orchestration; Python retains adapter calls and persistence."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Awaitable, Callable
from typing import Protocol

from app.domain.ingestion_stages import STAGES, StageOutput, StageOutputInvalid


class StageStore(Protocol):
    async def ensure_owned(self) -> None: ...
    async def get(self, stage: str, fingerprint: str) -> StageOutput | None: ...
    async def save(self, output: StageOutput) -> None: ...


def canonical_json(value: object) -> str:
    try:
        return json.dumps(
            value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
        )
    except (TypeError, ValueError) as exc:
        raise StageOutputInvalid("stage output is not finite JSON") from exc


def checksum(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


class CheckpointPipeline:
    def __init__(
        self,
        store: StageStore,
        *,
        max_output_bytes: int = 32 * 1024 * 1024,
        fault: Callable[[str, str], None] | None = None,
    ) -> None:
        self._store = store
        self._max_output_bytes = max_output_bytes
        self._fault = fault

    def _boundary(self, point: str, stage: str) -> None:
        if self._fault is not None:
            self._fault(point, stage)

    async def run(
        self,
        stage: str,
        *,
        upstream: str,
        config: dict[str, object],
        compute: Callable[[], Awaitable[dict[str, object]]],
    ) -> StageOutput:
        if stage not in STAGES:
            raise StageOutputInvalid("unknown ingestion stage")
        await self._store.ensure_owned()
        fingerprint = checksum(
            canonical_json(
                {"schema_version": 1, "stage": stage, "upstream": upstream, "config": config}
            )
        )
        cached = await self._store.get(stage, fingerprint)
        if (
            cached is not None
            and len(cached.payload_json.encode("utf-8")) <= self._max_output_bytes
            and checksum(cached.payload_json) == cached.output_sha256
        ):
            await self._store.ensure_owned()
            return cached
        self._boundary("before_compute", stage)
        payload = canonical_json(await compute())
        if len(payload.encode("utf-8")) > self._max_output_bytes:
            raise StageOutputInvalid("stage output exceeds configured budget")
        self._boundary("after_compute", stage)
        await self._store.ensure_owned()
        output = StageOutput(stage, fingerprint, checksum(payload), payload)
        await self._store.save(output)
        self._boundary("after_commit", stage)
        return output
