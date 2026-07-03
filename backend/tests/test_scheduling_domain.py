"""Cadence normalization + timezone/DST-correct next-run — pure domain (ADR-0015 §1, #236).

The load-bearing correctness piece: ``compute_next_run`` must land "08:00 local" at
the right UTC instant on either side of a DST boundary (never "08:00 UTC"). Pure —
no DB, no Celery, no Redis. Covers the DST spring-forward + fall-back, structured →
cron normalization, and the fail-closed validation (malformed cron / unknown tz →
the typed 422 errors, INV-8).
"""

from __future__ import annotations

from datetime import UTC, datetime
from zoneinfo import ZoneInfo

import pytest

from app.domain.scheduling import (
    CadenceUnit,
    InvalidCronError,
    InvalidTimezoneError,
    StructuredCadence,
    cadence_from_cron,
    cadence_from_structured,
    compute_next_run,
    validate_timezone,
)

_NY = "America/New_York"


def test_daily_cron_next_run_before_dst_spring_forward_is_est() -> None:
    """A daily 08:00 NY schedule fires at 13:00 UTC (EST, UTC-5) before spring-forward."""
    cadence = cadence_from_cron("0 8 * * *")
    # Sat Mar 8 2025 12:00 UTC — the DST transition is Sun Mar 9 02:00 local.
    after = datetime(2025, 3, 8, 12, 0, tzinfo=UTC)
    fire = compute_next_run(cadence, _NY, after=after)
    # Still EST (UTC-5): 08:00 local == 13:00 UTC, same day.
    assert fire == datetime(2025, 3, 8, 13, 0, tzinfo=UTC)
    assert fire.astimezone(ZoneInfo(_NY)).hour == 8


def test_daily_cron_next_run_after_dst_spring_forward_is_edt() -> None:
    """The SAME 08:00 NY schedule fires at 12:00 UTC (EDT, UTC-4) after spring-forward.

    The local hour stays 08:00; the UTC offset shifts by an hour — the whole point
    of tz/DST-correct next-run (ADR-0015 §1). A naive "08:00 UTC" or a fixed-offset
    computation would drift.
    """
    cadence = cadence_from_cron("0 8 * * *")
    # Sun Mar 9 2025 13:30 UTC — after the 02:00 local spring-forward; next fire is
    # Mon Mar 10 08:00 local, now EDT (UTC-4) == 12:00 UTC.
    after = datetime(2025, 3, 9, 13, 30, tzinfo=UTC)
    fire = compute_next_run(cadence, _NY, after=after)
    assert fire == datetime(2025, 3, 10, 12, 0, tzinfo=UTC)
    assert fire.astimezone(ZoneInfo(_NY)).hour == 8


def test_daily_cron_next_run_across_fall_back_stays_local_time() -> None:
    """Across the autumn fall-back the 08:00 NY fire moves from 12:00 to 13:00 UTC."""
    cadence = cadence_from_cron("0 8 * * *")
    # Fall-back 2025 is Sun Nov 2 02:00 local. Before: EDT (UTC-4) → 08:00 == 12:00 UTC.
    before = compute_next_run(cadence, _NY, after=datetime(2025, 11, 1, 9, 0, tzinfo=UTC))
    assert before == datetime(2025, 11, 1, 12, 0, tzinfo=UTC)
    # After: EST (UTC-5) → 08:00 == 13:00 UTC.
    after = compute_next_run(cadence, _NY, after=datetime(2025, 11, 2, 14, 0, tzinfo=UTC))
    assert after == datetime(2025, 11, 3, 13, 0, tzinfo=UTC)
    assert after.astimezone(ZoneInfo(_NY)).hour == 8


def test_next_run_is_strictly_after() -> None:
    """A fire exactly at ``after`` is not returned — the next occurrence is."""
    cadence = cadence_from_cron("0 8 * * *")
    at_fire = datetime(2025, 6, 2, 12, 0, tzinfo=UTC)  # 08:00 EDT == 12:00 UTC
    nxt = compute_next_run(cadence, _NY, after=at_fire)
    assert nxt == datetime(2025, 6, 3, 12, 0, tzinfo=UTC)  # the following day


def test_structured_daily_normalizes_to_cron() -> None:
    cadence = cadence_from_structured(StructuredCadence(every=CadenceUnit.DAY, at="08:30"))
    assert cadence.cron == "30 8 * * *"
    assert cadence.structured is not None
    assert cadence.structured.every is CadenceUnit.DAY


def test_structured_weekly_normalizes_to_cron_with_dow() -> None:
    cadence = cadence_from_structured(
        StructuredCadence(every=CadenceUnit.WEEK, at="09:00", day_of_week=1)  # Monday
    )
    assert cadence.cron == "0 9 * * 1"


def test_structured_monthly_normalizes_to_cron_with_dom() -> None:
    cadence = cadence_from_structured(
        StructuredCadence(every=CadenceUnit.MONTH, at="00:15", day_of_month=15)
    )
    assert cadence.cron == "15 0 15 * *"


def test_structured_weekly_fires_on_correct_weekday() -> None:
    """A weekly Monday cadence's next fire is a Monday in the schedule's timezone."""
    cadence = cadence_from_structured(
        StructuredCadence(every=CadenceUnit.WEEK, at="09:00", day_of_week=1)
    )
    # Wed Jun 4 2025 → next Monday is Jun 9.
    fire = compute_next_run(cadence, _NY, after=datetime(2025, 6, 4, 20, 0, tzinfo=UTC))
    local = fire.astimezone(ZoneInfo(_NY))
    assert local.weekday() == 0  # Python Monday
    assert (local.hour, local.minute) == (9, 0)


@pytest.mark.parametrize(
    "cron",
    ["not a cron", "0 8 * *", "0 8 * * * *", "60 8 * * *", "0 25 * * *", "0 8 32 * *"],
)
def test_malformed_cron_is_invalid_cron(cron: str) -> None:
    """A wrong field count or an out-of-range field is a typed ``invalid_cron`` (INV-8)."""
    with pytest.raises(InvalidCronError) as exc:
        cadence_from_cron(cron)
    assert exc.value.code == "invalid_cron"


def test_unknown_timezone_is_invalid_timezone() -> None:
    """An unknown IANA name is a typed ``invalid_timezone`` (INV-8)."""
    with pytest.raises(InvalidTimezoneError) as exc:
        validate_timezone("Mars/Olympus_Mons")
    assert exc.value.code == "invalid_timezone"


def test_blank_timezone_is_invalid_timezone() -> None:
    with pytest.raises(InvalidTimezoneError):
        validate_timezone("   ")


def test_compute_next_run_rejects_unknown_timezone() -> None:
    cadence = cadence_from_cron("0 8 * * *")
    with pytest.raises(InvalidTimezoneError):
        compute_next_run(cadence, "Not/AZone")


def test_cron_ranges_and_steps_parse() -> None:
    """Ranges + steps + lists in a cron field are honored (e.g. every 15 min, weekdays)."""
    cadence = cadence_from_cron("*/15 9-17 * * 1-5")
    # A Tuesday 09:07 UTC (EDT) → next quarter-hour at/after 09:15 local is 09:15.
    fire = compute_next_run(cadence, _NY, after=datetime(2025, 6, 3, 13, 7, tzinfo=UTC))
    local = fire.astimezone(ZoneInfo(_NY))
    assert local.minute % 15 == 0
    assert 9 <= local.hour <= 17
    assert local.weekday() < 5  # Mon-Fri
