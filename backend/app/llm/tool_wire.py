"""Shared provider tool payload for transmission and context accounting."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from app.core.logging import get_logger
from app.domain.llm import ToolSpec
from app.domain.tool_schema import strict_schema
from app.llm.tool_names import ToolNameMap

log = get_logger(__name__)


def wire_tools(tools: Sequence[ToolSpec]) -> list[dict[str, Any]]:
    """Project names and closed schemas; exclude invalid definitions safely."""
    name_map = ToolNameMap(tool.name for tool in tools)
    payload: list[dict[str, Any]] = []
    for tool in tools:
        try:
            parameters = strict_schema(tool.parameters)
        except ValueError:
            # No schema, arguments or exception text enters the operational log.
            log.warning("tool_schema_excluded", reason="invalid_configuration")
            continue
        payload.append(
            {
                "type": "function",
                "function": {
                    "name": name_map.to_wire(tool.name),
                    "description": tool.description,
                    "parameters": parameters,
                },
            }
        )
    return payload
