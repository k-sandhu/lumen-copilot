"""#666: bounded execution and orchestration cancellation, all offline."""

from __future__ import annotations

import pytest


def test_runtime_budget_cancellation_and_reuse() -> None:
    native = pytest.importorskip("lumen_docintel")
    from app.domain.native_runtime import RuntimeBudget
    from app.ingestion.native import CancellationHandle, NativeExecutor

    executor = NativeExecutor(threads=2, max_documents=1)
    with pytest.raises(native.DocIntelBudgetError):
        executor.run_units(("too large",), budget=RuntimeBudget(max_memory_bytes=4))
    token = CancellationHandle()
    token.cancel()
    with pytest.raises(native.DocIntelCancelledError):
        executor.run_units(("cancelled",), cancellation=token)
    result = executor.run_units(("A😀", "第二"))
    assert result.units == ("A😀", "第二")
    assert result.output_chars == 4
    assert result.peak_accounted_bytes > 0


def test_streaming_windows_share_document_budgets() -> None:
    native = pytest.importorskip("lumen_docintel")
    from app.domain.native_runtime import RuntimeBudget
    from app.ingestion.native import NativeExecutor

    executor = NativeExecutor(threads=2, max_documents=1)
    windows = executor.stream_units(
        iter([("one",), ("two",)]), budget=RuntimeBudget(max_output_chars=5)
    )
    assert next(windows).units == ("one",)
    with pytest.raises(native.DocIntelBudgetError):
        next(windows)
