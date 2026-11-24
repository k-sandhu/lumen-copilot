"""Producer-owned lease renewal; expiry remains the dead-producer recovery path."""

from __future__ import annotations

import asyncio
import math
import time
from collections.abc import Awaitable, Callable
from datetime import datetime
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
    returns the run must finalise normally (R3-001). The owning task is
    tracked only so the heartbeat dies with it — a live finalizer adopts the
    lease to keep renewing, and a dead owner's lease expires without refund
    (TR1-002).
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
        self._producer = asyncio.current_task()
        self._heartbeat = asyncio.create_task(self._renew())
        if self._producer is not None:
            self._producer.add_done_callback(self._producer_done)

    def _producer_done(self, producer: asyncio.Task[object]) -> None:
        # Expiry is the dead-producer recovery path: a heartbeat must not
        # outlive the task that owns the (possibly pending) execution.
        if producer is self._producer:
            self._heartbeat.cancel()

    async def adopt(self) -> None:
        """Move deferred cleanup to its live finalizer without orphan renewal."""
        producer = asyncio.current_task()
        if producer is self._producer:
            return
        if self._producer is not None:
            self._producer.remove_done_callback(self._producer_done)
        self._producer = producer
        if self._heartbeat.done() or self._heartbeat.cancelling():
            await asyncio.gather(self._heartbeat, return_exceptions=True)
            self._heartbeat = asyncio.create_task(self._renew())
        if producer is not None:
            producer.add_done_callback(self._producer_done)

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
        if self._producer is not None:
            self._producer.remove_done_callback(self._producer_done)
        self._heartbeat.cancel()
        await asyncio.gather(self._heartbeat, return_exceptions=True)


class AdmissionOwnership:
    """The one object that owns an admitted execution's admission (TF3).

    Owns the fenced lease + heartbeat and the daily reservation from
    admission until exactly one of: (a) confirmed termination or normal
    completion — settle once + release once, then the heartbeat stops; or
    (b) the owning producer is genuinely dead — the heartbeat stops with
    it (``AdmissionLease._producer_done``), there is no refund, and TTL
    expiry recovers the slot. Nothing else may stop the heartbeat: every
    exit that leaves the execution unconfirmed with a live producer hands
    this same object to whoever continues cleanup via
    :meth:`deferred_cleanup`.

    It also carries the per-execution cleanup ledger shared by the sandbox
    service loop and the chat confirmed-kill callback: the kill-attempt
    counter, the first-failure-only error-audit latch (one error row per
    execution), and the first-cause latch for the code run's
    ``termination_reason`` (the first established cause wins — spec 0028).
    """

    def __init__(
        self,
        lease: AdmissionLease,
        accounting: SandboxAccounting,
        *,
        tenant_id: UUID,
        run_id: UUID,
        owner_token: UUID,
        reserved_seconds: int,
        reservation_day: datetime,
        started_monotonic: float | None = None,
    ) -> None:
        self.lease = lease
        self.reserved = False
        self.attempts = 0
        self.failure_audited = False
        self.cause: str | None = None
        self._accounting = accounting
        self._tenant_id, self._run_id = tenant_id, run_id
        self._token = owner_token
        self._reserved_seconds = reserved_seconds
        self._reservation_day = reservation_day
        self._started = started_monotonic if started_monotonic is not None else time.monotonic()
        self._settled = False
        self._released = False

    def latch_cause(self, cause: str) -> str:
        """Record the first established termination cause; later ones lose."""
        if self.cause is None:
            self.cause = cause
        return self.cause

    def next_attempt(self) -> int:
        self.attempts += 1
        return self.attempts

    def mark_failure_audited(self) -> None:
        self.failure_audited = True

    async def settle_once(self) -> None:
        # Settle and release each at most once, on the reservation day,
        # whoever ends up confirming the kill. A missed settlement
        # conservatively retains the entire reservation; never refund
        # into a new day's counter after a midnight boundary.
        try:
            if self.reserved and not self._settled:
                await self._accounting.settle_daily(
                    self._tenant_id,
                    reserved_seconds=self._reserved_seconds,
                    actual_seconds=min(
                        self._reserved_seconds, math.ceil(time.monotonic() - self._started)
                    ),
                    day=self._reservation_day,
                )
                self._settled = True
        finally:
            if not self._released:
                await self._accounting.release_lease(
                    self._tenant_id, self._run_id, owner_token=self._token
                )
                self._released = True

    async def confirm_and_settle(self, confirm: Callable[[], Awaitable[None]]) -> None:
        """Deferred cleanup body: adopt, confirm, then settle/release once.

        The live finalizer adopts the same fenced lease, so its heartbeat
        renews through kill attempts and retry waits. The heartbeat stops
        only after ``confirm`` succeeds; if the finalizer dies first, the
        dead-owner callback stops renewal and expiry recovers the slot
        without refunding the reservation.
        """
        await self.lease.adopt()
        await confirm()
        self.lease.confirmed = True
        try:
            await self.settle_once()
        finally:
            await self.lease.stop()

    def deferred_cleanup(
        self, confirm: Callable[[], Awaitable[None]]
    ) -> Callable[[], Awaitable[None]]:
        """The hand-off every unconfirmed exit with a live producer takes."""

        async def cleanup() -> None:
            await self.confirm_and_settle(confirm)

        return cleanup

    async def close(self) -> None:
        """Non-deferred exit: stop renewal; settle unless pending-unconfirmed.

        An execution still pending and unconfirmed here has no live
        continuation (the dead-producer case): never settle/release, so
        the reservation stays charged and expiry recovers the slot.
        """
        await self.lease.stop()
        if not self.lease.pending or self.lease.confirmed:
            await asyncio.shield(self.settle_once())
