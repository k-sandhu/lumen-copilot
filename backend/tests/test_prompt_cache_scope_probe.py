"""Offline proof that the live observer retains router attribution without changing chunks."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from pathlib import Path
from types import SimpleNamespace

from tests.test_prompt_cache_scope_live import _ObservedStream


class Stream:
    def __init__(self, parts: list[SimpleNamespace]) -> None:
        self.parts = parts
        self.closed = False

    async def __aiter__(self) -> AsyncIterator[SimpleNamespace]:
        for part in self.parts:
            yield part

    async def aclose(self) -> None:
        self.closed = True


async def test_router_ids_persist_before_lookup_and_upstream_id_cannot_replace_them(
    tmp_path: Path,
) -> None:
    private_path = tmp_path / "generations.json"
    identifiers: list[str] = []
    parts = [
        SimpleNamespace(id="chatcmpl-temporary"),
        SimpleNamespace(id="gen-synthetic-one"),
        SimpleNamespace(id="chatcmpl-upstream"),
    ]
    inner = Stream(parts)
    observer = _ObservedStream(inner, identifiers, private_path)
    assert [part async for part in observer] == parts
    assert identifiers == json.loads(private_path.read_text()) == ["gen-synthetic-one"]
    # The next completion appends a distinct router id rather than overwriting call1.
    second_parts = [SimpleNamespace(id="gen-synthetic-two")]
    second = _ObservedStream(Stream(second_parts), identifiers, private_path)
    assert [part async for part in second] == second_parts
    assert json.loads(private_path.read_text()) == ["gen-synthetic-one", "gen-synthetic-two"]
    await observer.aclose()
    assert inner.closed


async def test_upstream_only_ids_remain_identifiable_as_missing_router_evidence(
    tmp_path: Path,
) -> None:
    private_path = tmp_path / "generations.json"
    identifiers: list[str] = []
    parts = [SimpleNamespace(id="chatcmpl-upstream-only")]
    observer = _ObservedStream(Stream(parts), identifiers, private_path)
    assert [part async for part in observer] == parts
    assert identifiers == ["chatcmpl-upstream-only"]
    assert not any(identifier.startswith("gen-") for identifier in identifiers)
    assert json.loads(private_path.read_text()) == identifiers
