"""End-to-end sync tests against HA's test recorder, in Sydney time."""

from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from freezegun.api import FrozenDateTimeFactory
from homeassistant.components.recorder.models import StatisticMeanType
from homeassistant.components.recorder.statistics import async_import_statistics
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ServiceValidationError
import pytest
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_fire_time_changed,
)
from pytest_homeassistant_custom_component.components.recorder.common import (
    async_wait_recording_done,
)

from custom_components.plug_and_claim.const import (
    CONF_AUTO_GENERATE,
    CONF_ENERGY_SENSOR,
    CONF_GENERATE_TIME,
    CONF_MONTH_SENSOR,
    CONF_NAME,
    CONF_OUTPUT_FOLDER,
    CONF_RATE,
    DOMAIN,
)

SYDNEY = ZoneInfo("Australia/Sydney")
SENSOR = "sensor.plug_today_s_consumption"
MONTH_SENSOR = "sensor.plug_this_month_s_consumption"


def _entry() -> MockConfigEntry:
    return MockConfigEntry(
        domain=DOMAIN,
        title="EV charging",
        data={CONF_NAME: "EV charging", CONF_ENERGY_SENSOR: SENSOR, CONF_MONTH_SENSOR: MONTH_SENSOR},
        options={
            CONF_RATE: 0.407,
            CONF_AUTO_GENERATE: True,
            CONF_GENERATE_TIME: "01:00:00",
            CONF_OUTPUT_FOLDER: "plug_and_claim/ev_charging",
        },
    )


async def _setup(hass: HomeAssistant) -> MockConfigEntry:
    entry = _entry()
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    await async_wait_recording_done(hass)
    await hass.async_block_till_done()
    return entry


def _import_hourly(
    hass: HomeAssistant, hours: dict[datetime, float], statistic_id: str = SENSOR
) -> None:
    """Import hourly statistics: {local hour start: kWh used in that hour}."""
    total = 0.0
    stats = []
    for start, kwh in sorted(hours.items()):
        total += kwh
        stats.append({"start": start, "state": total, "sum": total})
    async_import_statistics(
        hass,
        {
            "mean_type": StatisticMeanType.NONE,
            "has_sum": True,
            "name": None,
            "source": "recorder",
            "statistic_id": statistic_id,
            "unit_class": "energy",
            "unit_of_measurement": "kWh",
        },
        stats,
    )


def _local_hours(day: date, first_hour: int = 0, last_hour: int | None = None):
    """UTC-correct hour starts for a local day (23/24/25 on DST days)."""
    start = datetime(day.year, day.month, day.day, tzinfo=SYDNEY)
    following = day + timedelta(days=1)
    end = datetime(following.year, following.month, following.day, tzinfo=SYDNEY)
    hours = []
    t = start.astimezone(ZoneInfo("UTC"))
    while t < end:
        local = t.astimezone(SYDNEY)
        if local.hour >= first_hour and (last_hour is None or local.hour <= last_hour):
            hours.append(local)
        t += timedelta(hours=1)
    return hours


@pytest.fixture
async def sydney(hass: HomeAssistant, freezer: FrozenDateTimeFactory) -> FrozenDateTimeFactory:
    await hass.config.async_set_time_zone("Australia/Sydney")
    freezer.move_to(datetime(2026, 10, 6, 12, 0, tzinfo=SYDNEY))
    return freezer


async def test_backfill_from_statistics(hass: HomeAssistant, sydney) -> None:
    hours: dict[datetime, float] = {}
    # 1 Oct: statistics only start at 05:00, so the day is incomplete.
    for h in _local_hours(date(2026, 10, 1), first_hour=5):
        hours[h] = 0.5
    # 2 Oct: a normal 24-hour day.
    for h in _local_hours(date(2026, 10, 2)):
        hours[h] = 0.25
    # 3 Oct: HA was off 10:00-12:59; the energy shows up in the 13:00 hour.
    for h in _local_hours(date(2026, 10, 3)):
        if 10 <= h.hour <= 12:
            continue
        hours[h] = 1.0 + (3.0 if h.hour == 13 else 0)
    # 4 Oct: DST starts - only 23 hours, all present.
    for h in _local_hours(date(2026, 10, 4)):
        hours[h] = 0.1
    # 5 Oct: one hour of charging at 22:00.
    for h in _local_hours(date(2026, 10, 5)):
        hours[h] = 7.5 if h.hour == 22 else 0.0
    # 6 Oct (today, incomplete): must not be written.
    for h in _local_hours(date(2026, 10, 6), last_hour=10):
        hours[h] = 1.0
    assert len(_local_hours(date(2026, 10, 4))) == 23
    _import_hourly(hass, hours)
    await async_wait_recording_done(hass)

    entry = await _setup(hass)
    ledger = entry.runtime_data.ledger

    assert ledger.days == {
        "2026-10-02": {"kwh": 6.0, "source": "statistics"},
        "2026-10-03": {"kwh": 24.0, "source": "statistics", "gap_hours": 3},
        "2026-10-04": {"kwh": 2.3, "source": "statistics"},
        "2026-10-05": {"kwh": 7.5, "source": "statistics"},
    }
    assert ledger.backfilled
    assert ledger.stats_start == date(2026, 10, 1)
    assert entry.runtime_data.mode == "statistics"
    assert hass.states.get("sensor.ev_charging_ledger_days").state == "4"
    assert hass.states.get("sensor.ev_charging_last_sync").state != "unknown"
    # This month = 2-5 Oct from the ledger (39.8) + today so far (00:00-10:59, 11 kWh).
    assert float(hass.states.get("sensor.ev_charging_this_month_energy").state) == 50.8

    # A manual correction survives later syncs.
    await hass.services.async_call(
        DOMAIN, "set_day", {"config_entry_id": entry.entry_id, "date": "2026-10-03", "kwh": 25.1}, blocking=True
    )
    response = await hass.services.async_call(
        DOMAIN, "sync", {"config_entry_id": entry.entry_id, "days_back": 7}, blocking=True, return_response=True
    )
    assert ledger.get_day(date(2026, 10, 3)) == {"kwh": 25.1, "source": "manual"}
    assert response["mode"] == "statistics"
    assert response["updated_days"] == {}
    assert response["days_with_missing_hours"] == {"2026-10-03": 3}

    # The ledger is saved to its own storage file.
    await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.runtime_data.ledger.get_day(date(2026, 10, 3))["kwh"] == 25.1


async def test_nightly_sync_writes_yesterday(hass: HomeAssistant, sydney) -> None:
    _import_hourly(hass, {h: 0.5 for h in _local_hours(date(2026, 10, 5))})
    await async_wait_recording_done(hass)
    entry = await _setup(hass)
    ledger = entry.runtime_data.ledger
    # 5 Oct is the first statistics day, but it is complete, so it is kept.
    assert ledger.get_day(date(2026, 10, 5)) == {"kwh": 12.0, "source": "statistics"}
    assert ledger.get_day(date(2026, 10, 6)) is None

    _import_hourly(
        hass,
        {**{h: 0.5 for h in _local_hours(date(2026, 10, 5))}, **{h: 1.0 for h in _local_hours(date(2026, 10, 6))}},
    )
    await async_wait_recording_done(hass)
    night = datetime(2026, 10, 7, 0, 10, tzinfo=SYDNEY)
    sydney.move_to(night)
    async_fire_time_changed(hass, night)
    # Timer jobs run as background tasks.
    await hass.async_block_till_done(wait_background_tasks=True)
    await async_wait_recording_done(hass)
    await hass.async_block_till_done()

    assert ledger.get_day(date(2026, 10, 6)) == {"kwh": 24.0, "source": "statistics"}


async def test_live_capture_fallback(hass: HomeAssistant, sydney, caplog) -> None:
    """Without statistics, daily kWh come from the sensor's state changes."""
    sydney.move_to(datetime(2026, 10, 5, 10, 0, tzinfo=SYDNEY))
    hass.states.async_set(SENSOR, "2.0", {"unit_of_measurement": "kWh"})
    entry = await _setup(hass)
    coordinator = entry.runtime_data
    assert coordinator.mode == "live"
    assert "has no long-term statistics" in caplog.text

    async def reading(when: datetime, value: str) -> None:
        sydney.move_to(when)
        hass.states.async_set(SENSOR, value, {"unit_of_measurement": "kWh"})
        await hass.async_block_till_done()

    await reading(datetime(2026, 10, 5, 18, 0, tzinfo=SYDNEY), "5.0")
    # Midnight reset: 5 Oct started part-way, so it is not recorded.
    await reading(datetime(2026, 10, 6, 0, 0, 30, tzinfo=SYDNEY), "0.2")
    await reading(datetime(2026, 10, 6, 9, 0, tzinfo=SYDNEY), "1.0")

    # Restart mid-day: the running total is restored.
    await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()
    coordinator = entry.runtime_data
    assert coordinator.ledger.live["kwh"] == 1.0

    await reading(datetime(2026, 10, 6, 20, 0, tzinfo=SYDNEY), "4.0")
    await reading(datetime(2026, 10, 7, 0, 0, 20, tzinfo=SYDNEY), "unavailable")
    night = datetime(2026, 10, 7, 0, 10, tzinfo=SYDNEY)
    sydney.move_to(night)
    async_fire_time_changed(hass, night)
    # Timer jobs run as background tasks, and the sync first asks the
    # recorder (on its own database thread) whether statistics exist.
    for _ in range(3):
        await hass.async_block_till_done(wait_background_tasks=True)
        await async_wait_recording_done(hass)

    assert coordinator.ledger.days == {"2026-10-06": {"kwh": 4.0, "source": "live"}}


async def test_service_errors(hass: HomeAssistant, sydney) -> None:
    entry = await _setup(hass)
    with pytest.raises(ServiceValidationError):
        await hass.services.async_call(
            DOMAIN, "set_day", {"config_entry_id": entry.entry_id, "date": "2026-10-06", "kwh": 1}, blocking=True
        )
    with pytest.raises(ServiceValidationError):
        await hass.services.async_call(DOMAIN, "sync", {"config_entry_id": "nope"}, blocking=True)


async def test_diagnostics_month_sensor_check(hass: HomeAssistant, sydney) -> None:
    """Diagnostics show the ledger month next to the plug's own month total."""
    from custom_components.plug_and_claim.diagnostics import (
        async_get_config_entry_diagnostics,
    )

    # Daily sensor: 2 kWh on each of 2-5 Oct, but 3 Wh lost at one midnight.
    def two_kwh_a_day() -> dict[datetime, float]:
        # Spread over each day's real hours (4 Oct is a 23-hour DST day).
        return {
            h: 2.0 / len(_local_hours(date(2026, 10, d)))
            for d in range(2, 6)
            for h in _local_hours(date(2026, 10, d))
        }

    daily = two_kwh_a_day()
    daily[max(h for h in daily if h.date() == date(2026, 10, 3))] -= 0.003
    _import_hourly(hass, daily)
    # Monthly sensor: doesn't reset at midnight, so nothing is lost.
    monthly = two_kwh_a_day()
    _import_hourly(hass, monthly, MONTH_SENSOR)
    await async_wait_recording_done(hass)

    entry = await _setup(hass)
    diag = await async_get_config_entry_diagnostics(hass, entry)
    assert diag["ledger"]["months"]["2026-10"] == {
        "days": 4,
        "kwh": 7.997,
        "month_sensor_kwh": 8.0,
        "difference_kwh": -0.003,
    }
    assert diag["entry"]["options"][CONF_RATE] == 0.407
