"""Unit tests — MCP domain-type invariants + fail-fast config (ADR-0012 §1/§4/§7).

Pure, offline: the ``McpToolResult`` ``ok XOR error`` invariant, the transport enum
(no stdio member — it is deferred, §1), the tool-spec/read-only mapping, and the
``core/config.py`` MCP settings that must **fail fast** on a misconfiguration (an
unshippable transport, a non-positive timeout/rate).
"""

from __future__ import annotations

import pytest

from app.mcp.types import (
    McpHealth,
    McpHealthState,
    McpToolResult,
    McpTransport,
)


def test_tool_result_ok_xor_error_invariant() -> None:
    """A well-formed result has ok XOR error — both mismatches raise at construction."""
    ok = McpToolResult(ok=True, content="done")
    assert ok.ok and ok.error_code is None

    fail = McpToolResult.failure(error_code="mcp_unavailable", content="down")
    assert not fail.ok and fail.error_code == "mcp_unavailable" and fail.is_error

    with pytest.raises(ValueError):
        McpToolResult(ok=True, content="x", error_code="mcp_unavailable")  # ok + error
    with pytest.raises(ValueError):
        McpToolResult(ok=False, content="x")  # failure without a code


def test_transport_enum_has_no_stdio_member() -> None:
    """stdio/local-process is deferred (§1) — it is not even a valid transport value."""
    values = {t.value for t in McpTransport}
    assert values == {"streamable_http", "sse"}
    assert "stdio" not in values


def test_health_ok_property() -> None:
    """``McpHealth.ok`` is True only in the READY state."""
    assert McpHealth(state=McpHealthState.READY, tool_count=3).ok is True
    assert McpHealth(state=McpHealthState.ERROR, detail="down").ok is False


# --- config fail-fast -------------------------------------------------------


def _base_env() -> dict[str, str]:
    """The minimum required env for Settings to construct (infra URLs)."""
    return {
        "DATABASE_URL": "postgresql+asyncpg://u:p@localhost/db",
        "REDIS_URL": "redis://localhost:6379/0",
        "CELERY_BROKER_URL": "redis://localhost:6379/1",
        "CELERY_RESULT_BACKEND": "redis://localhost:6379/2",
        "S3_ENDPOINT_URL": "http://localhost:9000",
        "S3_ACCESS_KEY": "k",
        "S3_SECRET_KEY": "s",
        "S3_BUCKET": "b",
    }


def _settings(env: dict[str, str]):  # type: ignore[no-untyped-def]
    from app.core.config import Settings

    return Settings(**env)  # type: ignore[arg-type]


def test_config_defaults_allow_remote_transports_only() -> None:
    """The default MCP config permits the two remote transports and no stdio."""
    settings = _settings(_base_env())
    assert settings.mcp_allowed_transports == frozenset({"streamable_http", "sse"})
    assert settings.mcp_connect_timeout_seconds > 0
    assert settings.mcp_call_timeout_seconds > 0
    assert settings.mcp_rate_max_per_window > 0
    assert settings.mcp_endpoint_allowlist == frozenset()


def test_config_rejects_stdio_transport() -> None:
    """Configuring a stdio (or any non-remote) transport fails fast at startup (§1)."""
    env = _base_env() | {"MCP_ALLOWED_TRANSPORTS": "streamable_http,stdio"}
    with pytest.raises(Exception):  # noqa: B017 - pydantic ValidationError wraps ValueError
        _settings(env)


def test_config_rejects_empty_transport_set() -> None:
    """An empty allowed-transport set fails fast (no transport could ever connect)."""
    env = _base_env() | {"MCP_ALLOWED_TRANSPORTS": ""}
    with pytest.raises(Exception):  # noqa: B017
        _settings(env)


def test_config_rejects_non_positive_timeout() -> None:
    """A non-positive MCP timeout fails fast (it would disable the bound, §7)."""
    env = _base_env() | {"MCP_CALL_TIMEOUT_SECONDS": "0"}
    with pytest.raises(Exception):  # noqa: B017
        _settings(env)


def test_config_accepts_endpoint_allowlist() -> None:
    """An admin endpoint allowlist is parsed from a comma-separated env string."""
    env = _base_env() | {"MCP_ENDPOINT_ALLOWLIST": "a.example.com, b.example.com"}
    settings = _settings(env)
    assert settings.mcp_endpoint_allowlist == frozenset({"a.example.com", "b.example.com"})
