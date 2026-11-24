"""Owner-checked Stop, acknowledged only after the producer has settled (#574)."""

from __future__ import annotations

import asyncio
from contextlib import aclosing
from typing import Any

from app.auth.principal import Principal
from app.core.errors import DependencyError, NotFoundError
from app.realtime.backplane import Backplane, is_terminal
from app.services.audit import PermissionDeniedContext, audited_resource
from app.services.stream_registry import StreamRegistry


class StreamCancellationService:
    def __init__(
        self,
        *,
        backplane: Backplane,
        registry: StreamRegistry,
        denials: PermissionDeniedContext,
        timeout_seconds: float,
    ) -> None:
        self._backplane = backplane
        self._registry = registry
        self._denials = denials
        self._timeout_seconds = timeout_seconds

    @audited_resource("chat.stream.cancel", "chat_stream", "stream_id")
    async def cancel(self, *, stream_id: str, principal: Principal) -> dict[str, Any]:
        self._denials.assert_user(principal.tenant_id, principal.user_id)
        owner = await self._backplane.get_owner(stream_id)
        if (
            owner is None
            or owner.owner_id != principal.user_id
            or owner.tenant_id != principal.tenant_id
        ):
            await self._denials.emit(
                resource_type="chat_stream",
                resource_id=stream_id,
                attempted_action="chat.stream.cancel",
                reason="not_visible",
            )
            raise NotFoundError("Chat stream not found.")
        try:
            async with asyncio.timeout(self._timeout_seconds):
                terminal = await self._backplane.get_terminal(stream_id)
                if terminal is None:
                    await self._backplane.signal_cancel(stream_id)
                    self._registry.cancel(stream_id)
                    # Subscribe-then-replay closes the completion race, including
                    # when the producer lives on another serving replica.
                    async with aclosing(self._backplane.subscribe(stream_id)) as events:
                        async for event in events:
                            if is_terminal(event):
                                terminal = event
                                break
                if terminal is None:
                    raise DependencyError("Cancellation could not be confirmed. Retry Stop.")
                if terminal.get("problem", {}).get("code") == "cancellation_persist_failed":
                    raise DependencyError(
                        "Cancellation state could not be saved. Reload the session."
                    )
                return terminal
        except TimeoutError:
            raise DependencyError("Cancellation could not be confirmed. Retry Stop.") from None
