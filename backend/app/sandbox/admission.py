"""Producer-owned lease renewal; expiry remains the dead-producer recovery path."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from typing import Any
from uuid import UUID

from app.core.logging import get_logger
from app.sandbox.accounting import AccountingUnavailable, SandboxAccounting

log = get_logger(__name__)


class AdmissionLease:
    """Never resurrect expired ownership or confuse a lost lease with a kill.

    Renewal failure latches :attr:`accounting_lost` and may cancel only the
    currently registered in-flight remote call. It never cancels the producer
    task itself: before that call starts the run can be refused, and after it
    returns the run must finalise normally (R3-001).
    """

    def __init__(
        self,
        accounting: SandboxAccounting,
        tenant_id: UUID,
        run_id: UUID,
        owner_token: UUID,
        ttl_seconds: int,
        sleep: Callable[[float], Awaitable[None]],
    ) -> None:
        self.pending = False
        self.confirmed = False
        self.accounting_lost = False
        self._accounting = accounting
        self._tenant_id, self._run_id = tenant_id, run_id
        self._token, self._ttl = owner_token, ttl_seconds
        self._sleep = sleep
        self._in_flight: asyncio.Task[Any] | None = None
        self._heartbeat = asyncio.create_task(self._renew())

    def attach_in_flight(self, task: asyncio.Task[Any]) -> None:
        """Register the remote call a renewal failure is allowed to terminate."""
        self._in_flight = task
        if self.accounting_lost and not self.pending and not task.done():
            task.cancel()

    def detach_in_flight(self, task: asyncio.Task[Any]) -> None:
        if self._in_flight is task:
            self._in_flight = None

    async def _renew(self) -> None:
        while True:
            await self._sleep(min(30.0, self._ttl / 3))
            try:
                await self._accounting.renew_lease(
                    self._tenant_id,
                    self._run_id,
                    owner_token=self._token,
                    ttl_seconds=self._ttl,
                )
            except AccountingUnavailable:
                log.warning(
                    "sandbox.lease_renewal_failed",
                    code_run_id=str(self._run_id),
                    reason_code="sandbox_accounting_unavailable",
                )
                self.accounting_lost = True
                # Loss of accounting requests a real kill of work that is still
                # running remotely. Pending cleanup already owns that request;
                # do not interrupt its backoff or refund it. Outside the
                # in-flight window there is nothing to cancel here, so local
                # finalisation proceeds normally.
                in_flight = self._in_flight
                if not self.pending and in_flight is not None and not in_flight.done():
                    in_flight.cancel()

    async def stop(self) -> None:
        self._heartbeat.cancel()
        await asyncio.gather(self._heartbeat, return_exceptions=True)
