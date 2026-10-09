"""Platforms without an enforceable OS cap must reject before the ready handshake."""

from __future__ import annotations

import pytest


def test_macos_cap_fails_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.ingestion import _pdf_worker

    monkeypatch.setattr(_pdf_worker.sys, "platform", "darwin")
    with pytest.raises(OSError, match="worker limit unavailable"):
        _pdf_worker.apply_memory_limit(256 * 1024 * 1024)
