"""HTTP request-duration metrics (ADR-0027 §3) — a pure ASGI middleware.

Records ``http.server.request.duration`` by method, **route template** and
status code. The template (``/api/v1/chat/sessions/{session_id}``) comes from
the route FastAPI matched, so ids never become label values; an unmatched path
is one ``unmatched`` series rather than one series per probe. No span is
created: a chat answer outlives its 202 request, so request spans would be
short, misleading roots (ADR-0027 §2).
"""

from __future__ import annotations

import time

from starlette.types import ASGIApp, Message, Receive, Scope, Send

from app.observability import telemetry

_UNMATCHED = "unmatched"


class HttpMetricsMiddleware:
    """Time every HTTP request and record it against its route template."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        started = time.monotonic()
        status_code = 500

        async def send_with_status(message: Message) -> None:
            nonlocal status_code
            if message["type"] == "http.response.start":
                status_code = message["status"]
            await send(message)

        try:
            await self.app(scope, receive, send_with_status)
        finally:
            route = scope.get("route")
            template = getattr(route, "path", None) or _UNMATCHED
            telemetry.instruments().http_duration.record(
                time.monotonic() - started,
                {
                    "http.request.method": scope.get("method", "GET"),
                    "http.route": template,
                    "http.response.status_code": status_code,
                },
            )
