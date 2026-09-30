"""Bounded provider-neutral protocol probes using synthetic tools only."""

from __future__ import annotations

import argparse
import asyncio
import json
import math
from collections.abc import AsyncIterator, Sequence
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Protocol
from uuid import uuid4

from app.domain.llm import ChatMessage, Role, StreamEvent, ToolCall, ToolSpec

SUITE_VERSION = "tool-protocol-v1"
_NAME = "lumen_conformance_" + "x" * (64 - len("lumen_conformance_"))
_TOOL = ToolSpec(
    _NAME,
    "Synthetic probe. Return the supplied label's verification marker.",
    {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "label": {
                "type": "string",
                "enum": ["alpha", "beta"],
                "description": "Requested synthetic label, alpha or beta.",
            },
            "comment": {
                "type": ["string", "null"],
                "description": "Optional comment represented by required null when unused.",
            },
        },
        "required": ["label", "comment"],
    },
)


class ProbeGateway(Protocol):
    def stream_tools(
        self,
        messages: Sequence[ChatMessage],
        *,
        tools: Sequence[ToolSpec],
        model: str | None = None,
        tool_choice: str | None = None,
        max_tokens: int | None = None,
    ) -> AsyncIterator[StreamEvent]: ...


@dataclass(frozen=True, slots=True)
class CaseResult:
    name: str
    passed: bool
    reason: str


@dataclass(frozen=True, slots=True)
class ConformanceReport:
    model: str
    route_revision: str
    suite_version: str
    tested_at: datetime
    cases: tuple[CaseResult, ...]

    @property
    def qualified(self) -> bool:
        required = {"exact_once", "round_trip", "cancellation"}
        return (
            self.suite_version == SUITE_VERSION
            and all(
                sum(case.name == name and case.passed for case in self.cases) == 1
                for name in required
            )
            and len({case.name for case in self.cases}) == len(self.cases)
        )

    def is_current(
        self, model: str, route_revision: str, now: datetime, max_age: timedelta = timedelta(days=7)
    ) -> bool:
        if self.tested_at.tzinfo is None or now.tzinfo is None:
            return False
        return (
            self.qualified
            and self.model == model
            and self.route_revision == route_revision
            and timedelta(0) <= now - self.tested_at <= max_age
        )


class _Failure(Exception):
    pass


async def _close(stream: AsyncIterator[StreamEvent]) -> None:
    closer = getattr(stream, "aclose", None)
    if closer is not None:
        await closer()


async def _turn(
    gateway: ProbeGateway, messages: list[ChatMessage], model: str, choice: str
) -> tuple[str, tuple[ToolCall, ...]]:
    stream = gateway.stream_tools(
        messages, tools=(_TOOL,), model=model, tool_choice=choice, max_tokens=256
    )
    text = ""
    calls: list[ToolCall] = []
    events = 0
    try:
        async for event in stream:
            events += 1
            text += event.text
            calls.extend(event.tool_calls)
            if events > 2048 or len(text) > 4096 or len(calls) > 4:
                raise _Failure("output_budget")
    finally:
        await _close(stream)
    return text, tuple(calls)


async def _probe(gateway: ProbeGateway, model: str, name: str) -> None:
    labels = ["alpha", "beta"] if name == "parallel" else ["alpha"]
    messages = [
        ChatMessage(
            Role.SYSTEM,
            "Follow the synthetic tool protocol exactly. Use only the advertised tool. "
            "After results, output their verification markers and call no more tools.",
        ),
        ChatMessage(
            Role.USER,
            f"Probe {name}: call the advertised tool exactly once for each "
            f"label {', '.join(labels)} in one turn, with comment=null. "
            "Then output the markers returned by the tools.",
        ),
    ]
    if name == "cancellation":
        stream = gateway.stream_tools(
            messages, tools=(_TOOL,), model=model, tool_choice="auto", max_tokens=256
        )
        try:
            await anext(stream)
        except StopAsyncIteration as exc:
            raise _Failure("no_stream_signal") from exc
        finally:
            await _close(stream)
        return
    prose, calls = await _turn(gateway, messages, model, "auto")
    if len(calls) != len(labels) or len({call.id for call in calls}) != len(calls):
        raise _Failure("call_count")
    actual_labels = []
    for call in calls:
        if not call.id or call.name != _NAME or set(call.arguments) != {"label", "comment"}:
            raise _Failure("invalid_call")
        label, comment = call.arguments["label"], call.arguments["comment"]
        if label not in {"alpha", "beta"} or comment is not None:
            raise _Failure("invalid_arguments")
        actual_labels.append(label)
    if sorted(actual_labels) != sorted(labels):
        raise _Failure("invalid_arguments")
    messages.append(ChatMessage(Role.ASSISTANT, prose, tool_calls=calls))
    markers = []
    for call in calls:
        marker = f"PROBE_{call.arguments['label']}_{uuid4().hex}"
        markers.append(marker)
        messages.append(ChatMessage(Role.TOOL, marker, tool_call_id=call.id, name=call.name))
    answer, more_calls = await _turn(gateway, messages, model, "none")
    if more_calls:
        raise _Failure("turn_budget")
    if any(marker not in answer for marker in markers):
        raise _Failure("result_not_used")


async def qualify(
    gateway: ProbeGateway, *, model: str, route_revision: str, timeout_seconds: float = 30
) -> ConformanceReport:
    if (
        not model
        or not route_revision
        or not math.isfinite(timeout_seconds)
        or not (0 < timeout_seconds <= 60)
    ):
        raise ValueError("Supply model, immutable route revision and timeout in (0, 60].")
    cases = []
    for name in ("exact_once", "parallel", "round_trip", "cancellation"):
        try:
            async with asyncio.timeout(timeout_seconds):
                await _probe(gateway, model, name)
        except TimeoutError:
            cases.append(CaseResult(name, False, "timeout"))
        except _Failure as exc:
            cases.append(CaseResult(name, False, str(exc)))
        except Exception:
            # Only a safe classification survives; exception text may contain keys.
            cases.append(CaseResult(name, False, "provider_error"))
        else:
            cases.append(CaseResult(name, True, "passed"))
    return ConformanceReport(model, route_revision, SUITE_VERSION, datetime.now(UTC), tuple(cases))


def main() -> None:
    parser = argparse.ArgumentParser(description="Opt-in synthetic provider tool-protocol probes.")
    parser.add_argument("--model", required=True)
    parser.add_argument("--route-revision", required=True)
    parser.add_argument("--timeout", type=float, default=30)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--allow-provider-probes", action="store_true")
    args = parser.parse_args()
    if not args.allow_provider_probes:
        parser.error("Provider calls require explicit --allow-provider-probes.")
    from app.core.config import get_settings
    from app.llm import LLMGateway

    report = asyncio.run(
        qualify(
            LLMGateway(get_settings()),
            model=args.model,
            route_revision=args.route_revision,
            timeout_seconds=args.timeout,
        )
    )
    payload = asdict(report)
    payload["tested_at"] = report.tested_at.isoformat()
    payload["qualified"] = report.qualified
    args.output.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    raise SystemExit(0 if report.qualified else 1)


if __name__ == "__main__":
    main()
