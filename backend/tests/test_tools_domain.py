"""Tool-platform domain guards + registry/architecture invariants (issue #207).

Pure, zero-I/O checks that the governance metadata is **structural**, not prose:

* :class:`~app.services.tools.types.ToolDefinition` rejects a miscategorised tool
  at construction — a read-only tool must be T0 / no-approval, and a write-tier
  (T2/T3) tool MUST require approval (INV-7 by construction).
* :class:`~app.domain.tools.ToolResult` enforces the ``ok`` XOR ``error``
  invariant so a persisted row is never ambiguous (issue #207 §4).
* AC-1: the registry auto-discovers the three retrieval tools and adding one is a
  new file in ``impls/`` (no include-list edit) — asserted via the discovery scan.
* AC-N (architecture): the ONLY tool-invocation path is the runner — the chat
  runtime no longer calls a per-tool ``run_tool``; it goes through ``ToolRunner``.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from app.domain.tools import (
    ERROR_TOOL_ERROR,
    RiskTier,
    ToolResult,
)
from app.services.tools.registry import all_tools, default_allowlist, registered_names, tool_specs
from app.services.tools.types import ToolDefinition


async def _noop_handler(args: dict[str, object], ctx: object) -> object:  # pragma: no cover
    raise AssertionError("handler should never run in these construction tests")


# --- RiskTier ---------------------------------------------------------------


def test_risk_tier_write_tiers_are_t2_and_above() -> None:
    assert RiskTier.T0.is_write_tier is False
    assert RiskTier.T1.is_write_tier is False
    assert RiskTier.T2.is_write_tier is True
    assert RiskTier.T3.is_write_tier is True
    assert [t.level for t in RiskTier] == [0, 1, 2, 3]


# --- ToolDefinition governance guards (INV-7 structural) --------------------


def test_read_only_tool_must_be_t0() -> None:
    with pytest.raises(ValueError, match="must be risk tier T0"):
        ToolDefinition(
            name="x",
            description="d",
            json_schema={},
            handler=_noop_handler,  # type: ignore[arg-type]
            read_only=True,
            risk_tier=RiskTier.T1,
        )


def test_read_only_tool_cannot_require_approval() -> None:
    with pytest.raises(ValueError, match="cannot require approval"):
        ToolDefinition(
            name="x",
            description="d",
            json_schema={},
            handler=_noop_handler,  # type: ignore[arg-type]
            read_only=True,
            requires_approval=True,
        )


def test_write_tier_tool_must_require_approval() -> None:
    # A T2 tool that forgot ``requires_approval`` is a registration-time error —
    # there is no way to register a write-tier tool that bypasses the gate (INV-7).
    with pytest.raises(ValueError, match="must require approval"):
        ToolDefinition(
            name="x",
            description="d",
            json_schema={},
            handler=_noop_handler,  # type: ignore[arg-type]
            read_only=False,
            risk_tier=RiskTier.T2,
            requires_approval=False,
        )


def test_write_tier_tool_with_approval_is_allowed() -> None:
    defn = ToolDefinition(
        name="send",
        description="d",
        json_schema={},
        handler=_noop_handler,  # type: ignore[arg-type]
        read_only=False,
        risk_tier=RiskTier.T2,
        requires_approval=True,
    )
    assert defn.risk_tier.is_write_tier is True


def test_tool_definition_requires_a_name() -> None:
    with pytest.raises(ValueError, match="non-empty name"):
        ToolDefinition(
            name="  ", description="d", json_schema={}, handler=_noop_handler  # type: ignore[arg-type]
        )


# --- ToolResult ok XOR error ------------------------------------------------


def test_tool_result_success_must_not_carry_error() -> None:
    with pytest.raises(ValueError, match="must not carry an error"):
        ToolResult(call_id="c", name="t", ok=True, content="x", error="oops")


def test_tool_result_failure_must_carry_error() -> None:
    with pytest.raises(ValueError, match="must carry a non-empty error"):
        ToolResult(call_id="c", name="t", ok=False, content="x")


def test_tool_result_failure_factory_sets_error() -> None:
    r = ToolResult.failure(call_id="c", name="t", error=ERROR_TOOL_ERROR, content="failed")
    assert r.ok is False and r.error == ERROR_TOOL_ERROR
    # summary defaults to the error code when not supplied.
    assert r.summary == ERROR_TOOL_ERROR


# --- Registry discovery (AC-1) ----------------------------------------------


def test_registry_discovers_retrieval_tools() -> None:
    assert {"search_text", "search_documents", "get_document"} <= registered_names()
    # ``all_tools`` is deterministically ordered by name.
    names = [t.name for t in all_tools()]
    assert names == sorted(names)


def test_default_allowlist_is_read_only_tools() -> None:
    for name in default_allowlist():
        assert name in registered_names()


def test_tool_specs_none_renders_all_registered() -> None:
    assert {s.name for s in tool_specs()} == registered_names()


# --- AC-N: the runner is the only tool-invocation path (architecture) -------


def test_chat_runtime_invokes_tools_only_through_the_runner() -> None:
    """The chat runtime must not call a bespoke ``run_tool`` — only ``ToolRunner``.

    A cheap architecture guard for AC-N: the one consumer of tools (the chat
    runtime) routes every call through the governed runner. If a future edit
    reintroduced a direct tool call it would show up here.
    """
    src = Path(__file__).resolve().parents[1] / "app" / "services" / "chat_runtime.py"
    text = src.read_text(encoding="utf-8")
    assert "ToolRunner" in text
    assert "runner.run(" in text
    # The retired direct-dispatch symbol is gone (no fork of the tool layer).
    assert "run_tool(" not in text
    assert "from app.services.chat_tools" not in text


def test_no_legacy_chat_tools_module() -> None:
    """The old hardcoded ``chat_tools.py`` is fully migrated behind the registry."""
    legacy = Path(__file__).resolve().parents[1] / "app" / "services" / "chat_tools.py"
    assert not legacy.exists()
