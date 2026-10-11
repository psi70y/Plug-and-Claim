"""Month sensors, the Report month select, buttons and the Repairs issue."""

from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from freezegun.api import FrozenDateTimeFactory
from homeassistant.core import HomeAssistant
from homeassistant.helpers import issue_registry as ir
import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

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
ENTRY_ID = "claim1"
ISSUE_ID = f"missing_days_{ENTRY_ID}_2026-09"


def _ledger() -> dict:
    data = empty_ledger()
    days = {**SEPTEMBER, "2026-08-31": 4.0, "2026-10-01": 0, "2026-10-02": 2.5, "2026-10-03": 1.25}
    data["days"] = {k: {"kwh": v, "source": "statistics"} for k, v in days.items()}
    del data["days"]["2026-09-13"]  # a day with no data
    return data


@pytest.fixture
async def entry(
    hass: HomeAssistant, hass_storage, freezer: FrozenDateTimeFactory, tmp_path: Path
) -> MockConfigEntry:
    await hass.config.async_set_time_zone("Australia/Sydney")
    freezer.move_to(datetime(2026, 10, 4, 9, 0, tzinfo=SYDNEY))
    hass.config.media_dirs = {"local": str(tmp_path)}
    hass_storage[f"{DOMAIN}.{ENTRY_ID}"] = {"version": 1, "minor_version": 1, "key": f"{DOMAIN}.{ENTRY_ID}", "data": _ledger()}
    entry = MockConfigEntry(
        domain=DOMAIN,
        entry_id=ENTRY_ID,
        title="EV charging",
        data={CONF_NAME: "EV charging", CONF_ENERGY_SENSOR: "sensor.plug_today_s_consumption"},
        options={
            CONF_RATE: 0.407,
            CONF_AUTO_GENERATE: True,
            CONF_GENERATE_TIME: "01:00:00",
            CONF_OUTPUT_FOLDER: "plug_and_claim/ev_charging",
        },
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return entry


def _value(hass: HomeAssistant, entity_id: str) -> float:
    return float(hass.states.get(entity_id).state)


async def test_month_sensors(hass: HomeAssistant, entry: MockConfigEntry) -> None:
    # Last month = September without the (0 kWh) 13th.
    assert _value(hass, "sensor.ev_charging_last_month_energy") == 255.303087
    assert _value(hass, "sensor.ev_charging_last_month_claim") == 103.91
    last = hass.states.get("sensor.ev_charging_last_month_energy")
    assert last.attributes["missing_days"] == ["2026-09-13"]
    assert last.attributes["day_count"] == 29
    assert last.attributes["unit_of_measurement"] == "kWh"
    assert hass.states.get("sensor.ev_charging_last_month_claim").attributes["unit_of_measurement"] == "AUD"

    # This month so far: 1-3 Oct (no statistics here, so nothing for today).
    assert _value(hass, "sensor.ev_charging_this_month_energy") == 3.75
    assert _value(hass, "sensor.ev_charging_this_month_estimated_claim") == 1.53
    assert hass.states.get("sensor.ev_charging_this_month_energy").attributes["missing_days"] == []


async def test_report_month_select(hass: HomeAssistant, entry: MockConfigEntry) -> None:
    select = hass.states.get("select.ev_charging_report_month")
    # Finished months only (not October), newest first; defaults to the newest.
    assert select.attributes["options"] == ["2026-09 (Sep 2026)", "2026-08 (Aug 2026)"]
    assert select.state == "2026-09 (Sep 2026)"
    assert _value(hass, "sensor.ev_charging_selected_month_energy") == 255.303087

    await hass.services.async_call(
        "select", "select_option",
        {"entity_id": "select.ev_charging_report_month", "option": "2026-08 (Aug 2026)"},
        blocking=True,
    )
    assert hass.states.get("select.ev_charging_report_month").state == "2026-08 (Aug 2026)"
    assert _value(hass, "sensor.ev_charging_selected_month_energy") == 4.0
    selected = hass.states.get("sensor.ev_charging_selected_month_claim")
    assert float(selected.state) == 1.63
    assert len(selected.attributes["missing_days"]) == 30  # only 31 Aug has data
    assert selected.attributes["report_files"] == []

    # The choice survives a reload.
    await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()
    assert hass.states.get("select.ev_charging_report_month").state == "2026-08 (Aug 2026)"


async def test_generate_button_and_repairs_issue(
    hass: HomeAssistant, entry: MockConfigEntry, tmp_path: Path
) -> None:
    issues = ir.async_get(hass)
    await hass.services.async_call(
        "button", "press", {"entity_id": "button.ev_charging_generate_report_for_selected_month"}, blocking=True
    )
    await hass.async_block_till_done()
    assert (tmp_path / "plug_and_claim/ev_charging/2026/Energy_Usage_Sep_2026.pdf").exists()

    issue = issues.async_get_issue(DOMAIN, ISSUE_ID)
    assert issue is not None
    assert issue.translation_placeholders["days"] == "2026-09-13"

    selected = hass.states.get("sensor.ev_charging_selected_month_energy")
    assert selected.attributes["report_generated"] is not None
    assert len(selected.attributes["report_files"]) == 2

    # Fill the gap, regenerate: the issue clears itself.
    await hass.services.async_call(
        DOMAIN, "set_day", {"config_entry_id": entry.entry_id, "date": "2026-09-13", "kwh": 0}, blocking=True
    )
    await hass.services.async_call(
        "button", "press", {"entity_id": "button.ev_charging_generate_report_for_selected_month"}, blocking=True
    )
    await hass.async_block_till_done()
    assert issues.async_get_issue(DOMAIN, ISSUE_ID) is None


async def test_issue_removed_with_claim(hass: HomeAssistant, entry: MockConfigEntry) -> None:
    await hass.services.async_call(
        "button", "press", {"entity_id": "button.ev_charging_generate_report_for_selected_month"}, blocking=True
    )
    assert ir.async_get(hass).async_get_issue(DOMAIN, ISSUE_ID) is not None
    await hass.config_entries.async_remove(entry.entry_id)
    await hass.async_block_till_done()
    assert ir.async_get(hass).async_get_issue(DOMAIN, ISSUE_ID) is None


async def test_sync_now_button(hass: HomeAssistant, entry: MockConfigEntry) -> None:
    before = hass.states.get("sensor.ev_charging_last_sync").state
    await hass.services.async_call("button", "press", {"entity_id": "button.ev_charging_sync_now"}, blocking=True)
    await hass.async_block_till_done()
    assert hass.states.get("sensor.ev_charging_last_sync").state != "unknown"
    assert before != "unknown"
