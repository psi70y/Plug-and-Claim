"""Ledger tests: DST day boundaries, hourly aggregation and write rules."""

from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from homeassistant.components.recorder.statistics import reduce_day_ts_factory
from homeassistant.core import HomeAssistant
import pytest

from custom_components.plug_and_claim.ledger import (
    SOURCE_IMPORT,
    SOURCE_LIVE,
    SOURCE_MANUAL,
    SOURCE_STATISTICS,
    Ledger,
    aggregate_hourly,
    day_bounds,
    expected_hours,
    month_key,
)

SYDNEY = ZoneInfo("Australia/Sydney")
DST_ENDS = date(2026, 4, 5)  # clocks go back: 25-hour day
DST_STARTS = date(2026, 10, 4)  # clocks go forward: 23-hour day


@pytest.mark.parametrize(
    ("day", "hours"),
    [(date(2026, 9, 15), 24), (DST_ENDS, 25), (DST_STARTS, 23)],
)
def test_day_length(day: date, hours: int) -> None:
    assert expected_hours(day, SYDNEY) == hours
    start, end = day_bounds(day, SYDNEY)
    assert (start.hour, end.hour) == (0, 0)
    assert end.date() == day + timedelta(days=1)


@pytest.mark.parametrize("day", [date(2026, 9, 15), DST_ENDS, DST_STARTS, date(2026, 12, 31)])
async def test_days_match_recorder_day_periods(hass: HomeAssistant, day: date) -> None:
    """Our local-midnight days are the same as the recorder's own "day" periods."""
    await hass.config.async_set_time_zone("Australia/Sydney")
    _, recorder_day_start_end = reduce_day_ts_factory()
    start, end = day_bounds(day, SYDNEY)
    # Any instant inside the day maps to the same period the recorder uses.
    for probe in (start, start + timedelta(hours=12), end - timedelta(seconds=1)):
        assert recorder_day_start_end(probe.timestamp()) == (start.timestamp(), end.timestamp())


def _hourly(start: datetime, hours: int, kwh: float = 1.0) -> list[dict]:
    """Hourly rows as the recorder returns them (UTC timestamps)."""
    ts = start.timestamp()
    return [{"start": ts + h * 3600, "change": kwh} for h in range(hours)]


def test_aggregate_hourly_dst_days() -> None:
    rows = (
        _hourly(day_bounds(DST_ENDS, SYDNEY)[0], 25)
        + _hourly(day_bounds(DST_STARTS, SYDNEY)[0], 23)
        + [{"start": day_bounds(date(2026, 10, 5), SYDNEY)[0].timestamp(), "change": None}]
    )
    assert aggregate_hourly(rows, SYDNEY) == {DST_ENDS: (25.0, 25), DST_STARTS: (23.0, 23)}


def test_aggregate_hourly_midnight_boundary() -> None:
    """The 23:00 hour belongs to that day, the 00:00 hour to the next."""
    start = datetime(2026, 9, 1, 23, tzinfo=SYDNEY)
    rows = [{"start": start.timestamp(), "change": 2.0}, {"start": start.timestamp() + 3600, "change": 3.0}]
    assert aggregate_hourly(rows, SYDNEY) == {date(2026, 9, 1): (2.0, 1), date(2026, 9, 2): (3.0, 1)}


def test_month_key() -> None:
    assert month_key(date(2026, 9, 30)) == "2026-09"


async def test_set_day_rules(hass: HomeAssistant) -> None:
    ledger = Ledger(hass, "test")
    day = date(2026, 9, 10)

    assert ledger.set_day(day, 7.0955041, SOURCE_STATISTICS)
    assert ledger.get_day(day) == {"kwh": 7.095504, "source": SOURCE_STATISTICS}
    # Same value again is not a change.
    assert not ledger.set_day(day, 7.095504, SOURCE_STATISTICS)

    # Manual values win, and automatic sources never replace them.
    assert ledger.set_day(day, 7.2, SOURCE_MANUAL)
    assert not ledger.set_day(day, 9.0, SOURCE_STATISTICS)
    assert not ledger.set_day(day, 9.0, SOURCE_LIVE, overwrite_finalised=True)
    assert ledger.set_day(day, 7.3, SOURCE_IMPORT)
    assert ledger.get_day(day)["kwh"] == 7.3

    # Finalised months are left alone unless explicitly refreshed.
    other = date(2026, 9, 11)
    ledger.set_day(other, 2.5, SOURCE_STATISTICS)
    ledger.data["finalised_months"].append("2026-09")
    assert not ledger.set_day(other, 3.0, SOURCE_STATISTICS)
    assert ledger.set_day(other, 3.0, SOURCE_STATISTICS, overwrite_finalised=True)
    assert ledger.set_day(other, 3.1, SOURCE_MANUAL)

    # Gap hours are kept with the day; negative values are clamped to 0.
    assert ledger.set_day(date(2026, 10, 1), -1, SOURCE_STATISTICS, gap_hours=3)
    assert ledger.get_day(date(2026, 10, 1)) == {"kwh": 0.0, "source": SOURCE_STATISTICS, "gap_hours": 3}

    assert list(ledger.month_days("2026-09")) == ["2026-09-10", "2026-09-11"]


async def test_ledger_persists(hass: HomeAssistant, hass_storage) -> None:
    ledger = Ledger(hass, "abc")
    ledger.set_day(date(2026, 9, 1), 6.09, SOURCE_STATISTICS)
    await ledger.async_save()
    assert hass_storage["plug_and_claim.abc"]["data"]["days"]["2026-09-01"]["kwh"] == 6.09

    reloaded = Ledger(hass, "abc")
    await reloaded.async_load()
    assert reloaded.get_day(date(2026, 9, 1)) == {"kwh": 6.09, "source": SOURCE_STATISTICS}
    assert reloaded.data["meta"]["backfilled"] is False
