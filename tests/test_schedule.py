"""Automatic monthly report: on the 1st at the set time, and catch-up at startup."""

from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from freezegun.api import FrozenDateTimeFactory
from homeassistant.core import HomeAssistant
import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry, async_fire_time_changed
from pytest_homeassistant_custom_component.components.recorder.common import async_wait_recording_done

from custom_components.plug_and_claim.const import (
    CONF_AUTO_GENERATE,
    CONF_ENERGY_SENSOR,
    CONF_GENERATE_TIME,
    CONF_NAME,
    CONF_OUTPUT_FOLDER,
    CONF_RATE,
    DOMAIN,
)
from custom_components.plug_and_claim.ledger import empty_ledger

from .test_report import SEPTEMBER

SYDNEY = ZoneInfo("Australia/Sydney")


async def _setup(hass: HomeAssistant, hass_storage, tmp_path: Path, auto: bool = True) -> MockConfigEntry:
    hass.config.media_dirs = {"local": str(tmp_path)}
    ledger = empty_ledger()
    ledger["days"] = {k: {"kwh": v, "source": "statistics"} for k, v in SEPTEMBER.items()}
    hass_storage[f"{DOMAIN}.claim1"] = {"version": 1, "minor_version": 1, "key": f"{DOMAIN}.claim1", "data": ledger}
    entry = MockConfigEntry(
        domain=DOMAIN,
        entry_id="claim1",
        title="EV charging",
        data={CONF_NAME: "EV charging", CONF_ENERGY_SENSOR: "sensor.plug_today_s_consumption"},
        options={
            CONF_RATE: 0.407,
            CONF_AUTO_GENERATE: auto,
            CONF_GENERATE_TIME: "06:30:00",
            CONF_OUTPUT_FOLDER: "plug_and_claim/ev_charging",
        },
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    for _ in range(3):
        await hass.async_block_till_done()
        await async_wait_recording_done(hass)
    return entry


@pytest.fixture
async def sydney(hass: HomeAssistant, freezer: FrozenDateTimeFactory) -> FrozenDateTimeFactory:
    await hass.config.async_set_time_zone("Australia/Sydney")
    return freezer


async def _fire(hass: HomeAssistant, freezer: FrozenDateTimeFactory, when: datetime) -> None:
    freezer.move_to(when)
    async_fire_time_changed(hass, when)
    # Timer jobs run as background tasks, and the report syncs first through
    # the recorder's own database thread; wait for both.
    for _ in range(3):
        await hass.async_block_till_done(wait_background_tasks=True)
        await async_wait_recording_done(hass)


async def test_report_on_the_first(hass: HomeAssistant, hass_storage, sydney, tmp_path: Path) -> None:
    sydney.move_to(datetime(2026, 9, 30, 20, 0, tzinfo=SYDNEY))
    entry = await _setup(hass, hass_storage, tmp_path)
    reports = entry.runtime_data.ledger.data["reports"]

    await _fire(hass, sydney, datetime(2026, 10, 1, 6, 29, 0, tzinfo=SYDNEY))
    assert reports == {}
    await _fire(hass, sydney, datetime(2026, 10, 1, 6, 30, 0, tzinfo=SYDNEY))
    assert reports["2026-09"]["amount"] == 103.91
    assert (tmp_path / "plug_and_claim/ev_charging/2026/Energy_Usage_Sep_2026.pdf").exists()

    # The same time on other days does nothing.
    generated = reports["2026-09"]["generated"]
    await _fire(hass, sydney, datetime(2026, 10, 2, 6, 30, 0, tzinfo=SYDNEY))
    assert reports["2026-09"]["generated"] == generated


async def test_catch_up_after_downtime(hass: HomeAssistant, hass_storage, sydney, tmp_path: Path) -> None:
    """HA was off at 06:30 on the 1st: the report is made at startup."""
    sydney.move_to(datetime(2026, 10, 3, 9, 0, tzinfo=SYDNEY))
    entry = await _setup(hass, hass_storage, tmp_path)
    assert entry.runtime_data.ledger.data["reports"]["2026-09"]["kwh"] == 255.303087


@pytest.mark.parametrize(
    ("now", "auto"),
    [
        (datetime(2026, 10, 1, 6, 0, tzinfo=SYDNEY), True),  # before the report time
        (datetime(2026, 10, 12, 9, 0, tzinfo=SYDNEY), True),  # too late to catch up
        (datetime(2026, 10, 3, 9, 0, tzinfo=SYDNEY), False),  # automatic reports off
    ],
)
async def test_no_catch_up(
    hass: HomeAssistant, hass_storage, sydney, tmp_path: Path, now: datetime, auto: bool
) -> None:
    sydney.move_to(now)
    entry = await _setup(hass, hass_storage, tmp_path, auto=auto)
    assert entry.runtime_data.ledger.data["reports"] == {}


async def test_existing_report_not_redone(hass: HomeAssistant, hass_storage, sydney, tmp_path: Path) -> None:
    sydney.move_to(datetime(2026, 9, 30, 20, 0, tzinfo=SYDNEY))
    entry = await _setup(hass, hass_storage, tmp_path)
    reports = entry.runtime_data.ledger.data["reports"]
    reports["2026-09"] = {"generated": "manual", "kwh": 1.0, "rate": 0.407, "amount": 0.41, "files": []}
    await _fire(hass, sydney, datetime(2026, 10, 1, 6, 30, 0, tzinfo=SYDNEY))
    assert reports["2026-09"]["generated"] == "manual"
