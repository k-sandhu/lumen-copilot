"""Opt-in, process-local timing hooks for the unchanged live latency harness.

Load with pytest -p chat_latency_profile --latency-profile-output <private.json>.
Never load this plugin in a production process. No provider payload is recorded.
"""

from pathlib import Path
from time import perf_counter
import json
import functools

rows = []
current = 0
origin = perf_counter()
output = None


def mark(stage, phase, **fields):
    rows.append(
        dict(
            sample=current,
            stage=stage,
            phase=phase,
            t=round(perf_counter() - origin, 6),
            **fields,
        )
    )


def async_span(cls, name, label):
    original = getattr(cls, name)

    @functools.wraps(original)
    async def wrapped(*args, **kwargs):
        mark(label, "start")
        try:
            return await original(*args, **kwargs)
        finally:
            mark(label, "end")

    setattr(cls, name, wrapped)


def pytest_addoption(parser):
    parser.addoption("--latency-profile-output", default=None)


def pytest_configure(config):
    global output
    selected = config.getoption("--latency-profile-output")
    if selected is None:
        return
    output = Path(selected)
    import httpx

    original_send = httpx.AsyncClient.send

    @functools.wraps(original_send)
    async def send(self, request, *args, **kwargs):
        original_trace = request.extensions.get("trace")
        allowed = {
            "connection.connect_tcp.started",
            "connection.connect_tcp.complete",
            "connection.start_tls.started",
            "connection.start_tls.complete",
            "http11.send_request_headers.started",
            "http11.send_request_headers.complete",
            "http11.send_request_body.started",
            "http11.send_request_body.complete",
            "http11.receive_response_headers.started",
            "http11.receive_response_headers.complete",
        }

        async def trace(name, info):
            if name in allowed:
                mark("network_" + name, "trace")
            if original_trace is not None:
                await original_trace(name, info)

        request.extensions["trace"] = trace
        mark("http_send", "start")
        try:
            return await original_send(self, request, *args, **kwargs)
        finally:
            mark("http_send", "headers_returned")

    httpx.AsyncClient.send = send
    from litellm.llms.openai.openai import OpenAIChatCompletion

    original_client = OpenAIChatCompletion._get_openai_client
    seen = []

    @functools.wraps(original_client)
    def client(self, *args, **kwargs):
        result = original_client(self, *args, **kwargs)
        reused = any(ref() is result for ref in seen)
        mark("sdk_client", "resolve", reused=reused)
        if not reused:
            import weakref

            seen.append(weakref.ref(result))
        return result

    OpenAIChatCompletion._get_openai_client = client
    from app.llm import LLMGateway
    from app.retrieval import RetrievalService
    from app.services.chat_runtime import ChatRuntime

    async_span(LLMGateway, "embed", "embedding")
    async_span(RetrievalService, "search_text", "retrieval")
    async_span(ChatRuntime, "_run_tool_batch", "tool_batch")
    original = LLMGateway.stream_tools

    @functools.wraps(original)
    async def stream(self, *args, **kwargs):
        mark("model_turn", "start")
        first = True
        first_text = True
        first_tool = True
        try:
            async for event in original(self, *args, **kwargs):
                if first:
                    mark("model_turn", "first_event")
                    first = False
                if event.text and first_text:
                    mark("model_turn", "first_text")
                    first_text = False
                if event.tool_call_started and first_tool:
                    mark("model_turn", "first_tool")
                    first_tool = False
                yield event
        finally:
            mark("model_turn", "end")

    LLMGateway.stream_tools = stream
    orig_step = ChatRuntime._emit_step

    @functools.wraps(orig_step)
    async def step(self, *args, **kwargs):
        mark("step_" + kwargs["key"], kwargs["step_state"])
        return await orig_step(self, *args, **kwargs)

    ChatRuntime._emit_step = step
    orig_publish = ChatRuntime._publish

    @functools.wraps(orig_publish)
    async def publish(self, state, envelope):
        # Only record vocabulary; never payloads, ids, tokens or text.
        typ = envelope.get("type")
        if typ in ("start", "delta", "done", "error"):
            mark("envelope_" + typ, "publish")
        return await orig_publish(self, state, envelope)

    ChatRuntime._publish = publish


def pytest_collection_modifyitems(items):
    if output is None:
        return
    for item in items:
        module = item.module
        if hasattr(module, "_run_answer_ws") and not getattr(
            module, "_profile_wrapped", False
        ):
            original = module._run_answer_ws

            @functools.wraps(original)
            def sample(*args, **kwargs):
                global current
                current += 1
                mark("sample", "start")
                try:
                    timing = original(*args, **kwargs)
                    mark("sample", "end", ttft=timing.ttft_seconds)
                    return timing
                finally:
                    output.write_text(json.dumps(rows, indent=2), encoding="utf-8")

            module._run_answer_ws = sample
            module._profile_wrapped = True
