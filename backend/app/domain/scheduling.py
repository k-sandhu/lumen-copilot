"""Cadence normalization + timezone/DST-correct next-run computation — pure domain.

The scheduling *policy* half of the dynamic scheduler (ADR-0015 §1/§2, issue #236).
**Pure** — no ORM, no SQLAlchemy, no Celery, no framework imports (backend/AGENTS.md:
``domain/`` is pure). The scheduler adapter (``app.tasks.scheduler``) and the service
(``app.services.schedules_service``) consume these; the mechanism (RedBeat/Beat)
never re-implements the cadence math.

Two shapes, one normalized form (the frozen ``Cadence`` contract, ADR-0015 §2):

* a raw **cron** expression (standard 5-field ``m h dom mon dow``), or
* a **structured** cadence (``{ every: day|week|month, at: HH:MM, ... }``) — the
  human-friendly alternative — which normalizes *to* an equivalent cron.

Both store one canonical 5-field cron string; ``next_run_at`` is computed by
applying the schedule's IANA ``timezone`` so "08:00 local" lands at the correct
UTC instant across a DST transition (never "08:00 UTC"). The computation is a
pure function of ``(cron, timezone, after)`` so it is exhaustively unit-testable
offline — the load-bearing correctness property of the epic (ADR-0015 §1).

Validation is fail-closed (INV-8): a malformed cron → :class:`InvalidCronError`
(``invalid_cron``); an unknown IANA timezone → :class:`InvalidTimezoneError`
(``invalid_timezone``). The service maps both to **422** at the API boundary.
"""

from __future__ import annotations

import enum
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

# A schedule cannot fire more than once a minute (cron's finest resolution), so a
# minute-by-minute scan for the next matching wall-clock time is bounded. Two years
# of minutes is a generous ceiling that still terminates a pathological/never-firing
# expression (e.g. Feb 30) rather than looping forever.
_MAX_LOOKAHEAD_MINUTES = 366 * 2 * 24 * 60


class CadenceUnit(str, enum.Enum):
    """The recurrence unit of a structured cadence (contract ``CadenceUnit``)."""

    DAY = "day"
    WEEK = "week"
    MONTH = "month"


class InvalidCronError(ValueError):
    """A cron expression is malformed (INV-8). Carries the ``invalid_cron`` code."""

    code = "invalid_cron"


class InvalidTimezoneError(ValueError):
    """An IANA timezone name is unknown (INV-8). Carries the ``invalid_timezone`` code."""

    code = "invalid_timezone"


@dataclass(frozen=True, slots=True)
class Cadence:
    """A schedule's recurrence, normalized to one canonical 5-field cron string.

    ``cron`` is always the stored form (a structured cadence is converted on
    construction). ``structured`` preserves the original human-friendly input when
    that was how the schedule was created, so the UI can round-trip it; ``None``
    when the schedule was created from a raw cron. Both project to the same cron for
    next-run computation.
    """

    cron: str
    structured: StructuredCadence | None = None


@dataclass(frozen=True, slots=True)
class StructuredCadence:
    """A human-friendly recurrence (ADR-0015 §2 ``StructuredCadence``).

    ``at`` is a local ``HH:MM`` in the schedule's timezone. ``day_of_week`` (0=Sun..
    6=Sat) applies when ``every`` is ``week``; ``day_of_month`` (1..31) applies when
    ``every`` is ``month``. Validated on construction; converts to a cron via
    :func:`_structured_to_cron`.
    """

    every: CadenceUnit
    at: str
    day_of_week: int | None = None
    day_of_month: int | None = None


def _validate_hh_mm(at: str) -> tuple[int, int]:
    """Parse a ``HH:MM`` 24-hour local time, or raise ``InvalidCronError``."""
    parts = at.split(":")
    if len(parts) != 2:
        raise InvalidCronError(f"Time-of-day must be HH:MM, got {at!r}.")
    try:
        hour, minute = int(parts[0]), int(parts[1])
    except ValueError as exc:
        raise InvalidCronError(f"Time-of-day must be HH:MM, got {at!r}.") from exc
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        raise InvalidCronError(f"Time-of-day out of range: {at!r}.")
    return hour, minute


def _structured_to_cron(sc: StructuredCadence) -> str:
    """Convert a structured cadence to an equivalent 5-field cron string.

    ``day`` → every day at HH:MM; ``week`` → that weekday (default Monday) at HH:MM;
    ``month`` → that day-of-month (default 1) at HH:MM. The timezone is applied
    separately in :func:`compute_next_run` — the cron carries the *local* fields.
    """
    hour, minute = _validate_hh_mm(sc.at)
    if sc.every is CadenceUnit.DAY:
        return f"{minute} {hour} * * *"
    if sc.every is CadenceUnit.WEEK:
        dow = sc.day_of_week if sc.day_of_week is not None else 1  # default Monday
        if not (0 <= dow <= 6):
            raise InvalidCronError(f"day_of_week must be 0..6, got {dow}.")
        return f"{minute} {hour} * * {dow}"
    # month
    dom = sc.day_of_month if sc.day_of_month is not None else 1
    if not (1 <= dom <= 31):
        raise InvalidCronError(f"day_of_month must be 1..31, got {dom}.")
    return f"{minute} {hour} {dom} * *"


def cadence_from_structured(sc: StructuredCadence) -> Cadence:
    """Normalize a structured cadence to a :class:`Cadence` (validates + converts)."""
    cron = _structured_to_cron(sc)
    return Cadence(cron=cron, structured=sc)


def cadence_from_cron(cron: str) -> Cadence:
    """Normalize a raw cron string to a :class:`Cadence`, validating it (INV-8)."""
    normalized = _parse_cron(cron)  # raises InvalidCronError on a bad expression
    return Cadence(cron=normalized, structured=None)


def validate_timezone(name: str) -> ZoneInfo:
    """Resolve an IANA timezone name to a :class:`ZoneInfo`, or raise (INV-8).

    An unknown/blank name is an :class:`InvalidTimezoneError` (``invalid_timezone``),
    mapped to **422** by the service. A valid name yields the tzinfo used for the
    DST-correct next-run computation.
    """
    if not name or not name.strip():
        raise InvalidTimezoneError("Timezone must be a non-empty IANA name.")
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError, KeyError) as exc:
        raise InvalidTimezoneError(f"Unknown IANA timezone: {name!r}.") from exc


# --- Cron parsing (5 fields: minute hour day-of-month month day-of-week) ----


@dataclass(frozen=True, slots=True)
class _CronFields:
    minutes: frozenset[int]
    hours: frozenset[int]
    days_of_month: frozenset[int]
    months: frozenset[int]
    days_of_week: frozenset[int]  # 0..6, Sunday=0


_FIELD_BOUNDS = ((0, 59), (0, 23), (1, 31), (1, 12), (0, 6))


def _parse_field(spec: str, low: int, high: int) -> frozenset[int]:
    """Parse one cron field into the set of matching integers (``*``, lists, ranges, steps)."""
    values: set[int] = set()
    for part in spec.split(","):
        step = 1
        body = part
        if "/" in part:
            body, _, step_str = part.partition("/")
            try:
                step = int(step_str)
            except ValueError as exc:
                raise InvalidCronError(f"Invalid step in cron field: {part!r}.") from exc
            if step <= 0:
                raise InvalidCronError(f"Cron step must be positive: {part!r}.")
        if body == "*":
            start, end = low, high
        elif "-" in body:
            start_str, _, end_str = body.partition("-")
            try:
                start, end = int(start_str), int(end_str)
            except ValueError as exc:
                raise InvalidCronError(f"Invalid range in cron field: {part!r}.") from exc
        else:
            try:
                start = end = int(body)
            except ValueError as exc:
                raise InvalidCronError(f"Invalid cron field value: {part!r}.") from exc
        if start < low or end > high or start > end:
            raise InvalidCronError(
                f"Cron field value out of range [{low},{high}]: {part!r}."
            )
        values.update(range(start, end + 1, step))
    if not values:
        raise InvalidCronError(f"Cron field matched nothing: {spec!r}.")
    return frozenset(values)


def _parse_cron(cron: str) -> str:
    """Validate a 5-field cron expression and return its normalized (trimmed) form.

    Raises :class:`InvalidCronError` on the wrong field count or any malformed field
    (INV-8). Normalization collapses internal whitespace so the stored form is
    stable; the parsed field sets are recomputed at compute time.
    """
    fields = cron.strip().split()
    if len(fields) != 5:
        raise InvalidCronError(
            f"A cron expression must have exactly 5 fields (m h dom mon dow), got {len(fields)}."
        )
    # Validate each field parses (raises on failure); discard the result here.
    for spec, (low, high) in zip(fields, _FIELD_BOUNDS, strict=True):
        _parse_field(spec, low, high)
    return " ".join(fields)


def _cron_fields(cron: str) -> _CronFields:
    fields = cron.strip().split()
    return _CronFields(
        minutes=_parse_field(fields[0], 0, 59),
        hours=_parse_field(fields[1], 0, 23),
        days_of_month=_parse_field(fields[2], 1, 31),
        months=_parse_field(fields[3], 1, 12),
        days_of_week=_parse_field(fields[4], 0, 6),
    )


def _matches(fields: _CronFields, local: datetime) -> bool:
    """Whether a local wall-clock ``datetime`` matches the cron fields.

    Standard cron day semantics: when *both* day-of-month and day-of-week are
    restricted (not ``*``-equivalent, i.e. not the full set), a time matches if it
    matches *either* — the union. When one is unrestricted, only the other gates.
    """
    if local.minute not in fields.minutes:
        return False
    if local.hour not in fields.hours:
        return False
    if local.month not in fields.months:
        return False
    dom_restricted = fields.days_of_month != frozenset(range(1, 32))
    dow_restricted = fields.days_of_week != frozenset(range(0, 7))
    # Python weekday(): Monday=0..Sunday=6; cron: Sunday=0..Saturday=6.
    cron_dow = (local.weekday() + 1) % 7
    dom_ok = local.day in fields.days_of_month
    dow_ok = cron_dow in fields.days_of_week
    if dom_restricted and dow_restricted:
        return dom_ok or dow_ok
    if dom_restricted:
        return dom_ok
    if dow_restricted:
        return dow_ok
    return True  # both unrestricted → every day


def compute_next_run(
    cadence: Cadence,
    timezone: str,
    *,
    after: datetime | None = None,
) -> datetime:
    """The next UTC instant this cadence fires strictly after ``after`` (tz/DST-correct).

    Applies the schedule's IANA ``timezone`` to the cron's *local* fields: it scans
    minute-by-minute in the schedule's local wall clock from just after ``after``,
    finds the first minute matching the cron, and converts that local time back to
    UTC — so "08:00 local" lands at the right UTC instant on either side of a DST
    boundary (the local hour is fixed; the UTC offset shifts). The returned datetime
    is timezone-aware UTC.

    ``after`` defaults to now (UTC). A cron that never matches within the bounded
    lookahead (e.g. an impossible date) raises :class:`InvalidCronError` rather than
    looping forever — a fail-closed guard, not a normal outcome.
    """
    tz = validate_timezone(timezone)
    fields = _cron_fields(cadence.cron)
    base_utc = (after or datetime.now(UTC)).astimezone(UTC)
    # Start at the next whole minute after ``after`` (a fire is minute-aligned; we
    # want strictly-after so a schedule does not re-fire the same minute).
    candidate_utc = base_utc.replace(second=0, microsecond=0) + timedelta(minutes=1)
    for _ in range(_MAX_LOOKAHEAD_MINUTES):
        local = candidate_utc.astimezone(tz)
        if _matches(fields, local):
            return candidate_utc.astimezone(UTC)
        candidate_utc += timedelta(minutes=1)
    raise InvalidCronError(
        f"Cron expression {cadence.cron!r} has no fire time within the lookahead window."
    )


__all__ = [
    "Cadence",
    "CadenceUnit",
    "InvalidCronError",
    "InvalidTimezoneError",
    "StructuredCadence",
    "cadence_from_cron",
    "cadence_from_structured",
    "compute_next_run",
    "validate_timezone",
]
