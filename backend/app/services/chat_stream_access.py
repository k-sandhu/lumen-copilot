"""Verified-principal stream visibility and durable denial ownership (#579)."""

from __future__ import annotations

from app.auth.principal import Principal
from app.realtime.backplane import Backplane
from app.services.audit import PermissionDeniedContext, audited_resource


class ChatStreamAccessService:
    def __init__(
        self,
        *,
        backplane: Backplane,
        principal: Principal,
        denials: PermissionDeniedContext,
    ) -> None:
        denials.assert_user(principal.tenant_id, principal.user_id)
        self._backplane = backplane
        self._principal = principal
        self._denials = denials

    @audited_resource("chat.stream.subscribe", "chat_stream", "stream_id", missing_result=True)
    async def authorize(self, stream_id: str) -> bool:
        """Private, foreign and unknown targets have identical pre-accept denials."""
        owner = await self._backplane.get_owner(stream_id)
        if (
            owner is None
            or owner.owner_id != self._principal.user_id
            or owner.tenant_id != self._principal.tenant_id
        ):
            await self._denials.emit(
                resource_type="chat_stream",
                resource_id=stream_id,
                attempted_action="chat.stream.subscribe",
                reason="not_visible",
            )
            return False
        return True
