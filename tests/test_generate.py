"""generate_report action: files in the media folder, event, notify, finalising."""

from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from freezegun.api import FrozenDateTimeFactory
from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.exceptions import ServiceValidationError
from openpyxl import load_workbook
import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry, async_capture_events

from custom_components.plug_and_claim.const import (
    CONF_AUTO_GENERATE,
    CONF_ENERGY_SENSOR,
    CONF_GENERATE_TIME,
    CONF_NAME,
    CONF_NOTIFY_SERVICE,
    CONF_OUTPUT_FOLDER,
    CONF_RATE,
    CONF_VEHICLE,
    DOMAIN,
    EVENT_REPORT_GENERATED,
)
from custom_components.plug_and_claim.ledger import SOURCE_STATISTICS

from .test_report import SEPTEMBER

SYDNEY = ZoneInfo("Australia/Sydney")


@pytest.fixture
async def entry(hass: HomeAssistant, freezer: FrozenDateTimeFactory, tmp_path: Path) -> MockConfigEntry:
    await hass.config.async_set_time_zone("Australia/Sydney")
    freezer.move_to(datetime(2026, 10, 11, 9, 0, tzinfo=SYDNEY))
    hass.config.media_dirs = {"local": str(tmp_path)}
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="EV charging",
        data={CONF_NAME: "EV charging", CONF_ENERGY_SENSOR: "sensor.plug_today_s_consumption"},
        options={
            CONF_RATE: 0.407,
            CONF_VEHICLE: "Mitsubishi Outlander PHEV",
            CONF_AUTO_GENERATE: True,
            CONF_GENERATE_TIME: "01:00:00",
            CONF_OUTPUT_FOLDER: "plug_and_claim/ev_charging",
            CONF_NOTIFY_SERVICE: "notify.pushover",
        },
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    ledger = entry.runtime_data.ledger
    for day, kwh in SEPTEMBER.items():
        ledger.set_day(date.fromisoformat(day), kwh, SOURCE_STATISTICS)
    return entry


async def test_generate_september(hass: HomeAssistant, entry: MockConfigEntry, tmp_path: Path) -> None:
    notifications: list[ServiceCall] = []
    hass.services.async_register("notify", "pushover", notifications.append)
    events = async_capture_events(hass, EVENT_REPORT_GENERATED)

    response = await hass.services.async_call(
        DOMAIN,
        "generate_report",
        {"config_entry_id": entry.entry_id, "month": "2026-09"},
        blocking=True,
        return_response=True,
    )

    folder = tmp_path / "plug_and_claim" / "ev_charging" / "2026"
    xlsx = folder / "Energy_Usage_Sep_2026.xlsx"
    pdf = folder / "Energy_Usage_Sep_2026.pdf"
    assert response == {
        "entry_id": entry.entry_id,
        "month": "2026-09",
        "kwh": 255.303087,
        "rate": 0.407,
        "amount": 103.91,
        "files": [str(xlsx.resolve()), str(pdf.resolve())],
    }
    assert pdf.read_bytes().startswith(b"%PDF")
    ws = load_workbook(xlsx).active
    assert ws.title == "Sep-26"

    await hass.async_block_till_done()
    assert [e.data for e in events] == [response]
    assert len(notifications) == 1
    assert notifications[0].data["message"] == (
        "September 2026 EV charging: 255.30 kWh × $0.407 = $103.91. "
        f"Report saved to {folder.resolve()}."
    )

    ledger = entry.runtime_data.ledger
    assert ledger.data["finalised_months"] == ["2026-09"]
    assert ledger.data["reports"]["2026-09"]["amount"] == 103.91
    assert ledger.data["reports"]["2026-09"]["missing_days"] == []
    # Finalised: an automatic sync can no longer change September.
    assert not ledger.set_day(date(2026, 9, 1), 99.0, SOURCE_STATISTICS)


async def test_missing_days_and_no_notify(hass: HomeAssistant, entry: MockConfigEntry, caplog) -> None:
    notifications: list[ServiceCall] = []
    hass.services.async_register("notify", "pushover", notifications.append)
    del entry.runtime_data.ledger.days["2026-09-13"]

    response = await hass.services.async_call(
        DOMAIN,
        "generate_report",
        {"config_entry_id": entry.entry_id, "month": "2026-09", "notify": False},
        blocking=True,
        return_response=True,
    )
    assert response["kwh"] == 255.303087  # 13 Sep was 0 kWh anyway
    assert entry.runtime_data.ledger.data["reports"]["2026-09"]["missing_days"] == ["2026-09-13"]
    assert "no data for 2026-09-13" in caplog.text
    assert notifications == []


async def test_failed_notify_keeps_report(hass: HomeAssistant, entry: MockConfigEntry, caplog) -> None:
    # notify.pushover isn't registered here, so the call fails.
    response = await hass.services.async_call(
        DOMAIN, "generate_report", {"config_entry_id": entry.entry_id, "month": "2026-09"},
        blocking=True, return_response=True,
    )
    assert response["amount"] == 103.91
    assert "couldn't send the report notification" in caplog.text


@pytest.mark.parametrize("month", ["2026-10", "2026-11"])
async def test_unfinished_month_rejected(hass: HomeAssistant, entry: MockConfigEntry, month: str) -> None:
    with pytest.raises(ServiceValidationError):
        await hass.services.async_call(
            DOMAIN, "generate_report", {"config_entry_id": entry.entry_id, "month": month}, blocking=True
        )


async def test_bad_month_format_rejected(hass: HomeAssistant, entry: MockConfigEntry) -> None:
    with pytest.raises(Exception):
        await hass.services.async_call(
            DOMAIN, "generate_report", {"config_entry_id": entry.entry_id, "month": "Sep 2026"}, blocking=True
        )


async def test_rate_history_used_for_old_months(hass: HomeAssistant, entry: MockConfigEntry) -> None:
    coordinator = entry.runtime_data
    coordinator.ledger.data["rates"] = [{"from": "2025-01", "rate": 0.39}, {"from": "2026-10", "rate": 0.42}]
    assert coordinator.rate_for_month("2026-09") == 0.39
    assert coordinator.rate_for_month("2026-10") == 0.42
    assert coordinator.rate_for_month("2024-12") == 0.407  # before any history: current setting
