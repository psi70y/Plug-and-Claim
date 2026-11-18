"""New-version notification tests (update_check.py)."""

from datetime import timedelta
import time
from unittest.mock import patch

from homeassistant.components import persistent_notification
from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_fire_time_changed,
)

from custom_components.plug_and_claim.const import (
    CONF_AUTO_GENERATE,
    CONF_ENERGY_SENSOR,
    CONF_GENERATE_TIME,
    CONF_NAME,
    CONF_OUTPUT_FOLDER,
    CONF_RATE,
    CONF_UPDATE_NOTIFY,
    DOMAIN,
)
from custom_components.plug_and_claim.update_check import (
    CHECK_INTERVAL_SECONDS,
    LATEST_RELEASE_URL,
    NOTIFICATION_ID,
    STARTUP_DELAY_SECONDS,
    UPDATE_CHECKER,
    UPDATE_STORAGE_KEY,
    _parse_version,
)

INSTALLED = "0.2.0"
RELEASE_URL = "https://github.com/psi70y/Plug-and-Claim/releases/tag/v0.3.0"


def _entry(sensor: str = "sensor.plug_today_s_consumption", **options) -> MockConfigEntry:
    return MockConfigEntry(
        domain=DOMAIN,
        title="EV charging",
        data={CONF_NAME: "EV charging", CONF_ENERGY_SENSOR: sensor},
        options={
            CONF_RATE: 0.407,
            CONF_AUTO_GENERATE: False,
            CONF_GENERATE_TIME: "01:00:00",
            CONF_OUTPUT_FOLDER: "plug_and_claim/ev_charging",
            **options,
        },
    )


def _reply(aioclient_mock, tag: str = "v0.3.0", status: int = 200) -> None:
    aioclient_mock.clear_requests()
    aioclient_mock.get(
        LATEST_RELEASE_URL, status=status, json={"tag_name": tag, "html_url": RELEASE_URL}
    )


def _notification(hass: HomeAssistant):
    return persistent_notification._async_get_or_create_notifications(hass).get(NOTIFICATION_ID)


async def _setup(hass: HomeAssistant, entry: MockConfigEntry, installed: str = INSTALLED) -> None:
    entry.add_to_hass(hass)
    with _installed(installed):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()


def _installed(version: str):
    """Pretend this version of the integration is installed."""
    from homeassistant.loader import Integration

    return patch.object(Integration, "version", property(lambda self: version))


async def _run_startup_check(hass: HomeAssistant, installed: str = INSTALLED) -> None:
    with _installed(installed):
        async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=STARTUP_DELAY_SECONDS + 1))
        await hass.async_block_till_done()


def test_parse_version() -> None:
    assert _parse_version("v0.3.0") == _parse_version("0.3.0")
    assert _parse_version("v0.10.0") > _parse_version("0.9.1")
    assert _parse_version(None) is None
    assert _parse_version("not a version") is None


async def test_notifies_once_per_release(hass: HomeAssistant, aioclient_mock, hass_storage) -> None:
    _reply(aioclient_mock)
    await _setup(hass, _entry())
    await _run_startup_check(hass)

    assert aioclient_mock.call_count == 1
    note = _notification(hass)
    assert note is not None
    assert "v0.3.0" in note["message"] and "v0.2.0" in note["message"]
    assert RELEASE_URL in note["message"]
    assert hass_storage[UPDATE_STORAGE_KEY]["data"]["notified_version"] == "0.3.0"

    # Dismissed, then checked again a day later: not shown again.
    persistent_notification.async_dismiss(hass, NOTIFICATION_ID)
    hass.data[UPDATE_CHECKER]._data["last_check"] = 0
    with _installed(INSTALLED):
        async_fire_time_changed(hass, dt_util.utcnow() + timedelta(hours=2))
        await hass.async_block_till_done()
    assert aioclient_mock.call_count == 2
    assert _notification(hass) is None


async def test_no_notification_when_up_to_date(hass: HomeAssistant, aioclient_mock) -> None:
    _reply(aioclient_mock, tag="v0.2.0")
    await _setup(hass, _entry())
    await _run_startup_check(hass)
    assert aioclient_mock.call_count == 1
    assert _notification(hass) is None


async def test_checks_at_most_once_a_day(hass: HomeAssistant, aioclient_mock, hass_storage) -> None:
    """A check saved less than 24 h ago (e.g. before a restart) means no call."""
    hass_storage[UPDATE_STORAGE_KEY] = {
        "version": 1,
        "key": UPDATE_STORAGE_KEY,
        "data": {"last_check": time.time() - CHECK_INTERVAL_SECONDS / 2},
    }
    _reply(aioclient_mock)
    await _setup(hass, _entry())
    await _run_startup_check(hass)
    assert aioclient_mock.call_count == 0


async def test_failed_check_counts(hass: HomeAssistant, aioclient_mock, hass_storage) -> None:
    _reply(aioclient_mock, status=500)
    await _setup(hass, _entry())
    await _run_startup_check(hass)
    assert aioclient_mock.call_count == 1
    assert _notification(hass) is None
    assert hass_storage[UPDATE_STORAGE_KEY]["data"]["last_check"] > 0

    # The hourly poll doesn't retry within the 24 hours.
    with _installed(INSTALLED):
        async_fire_time_changed(hass, dt_util.utcnow() + timedelta(hours=2))
        await hass.async_block_till_done()
    assert aioclient_mock.call_count == 1


async def test_dismissed_after_update(hass: HomeAssistant, aioclient_mock, hass_storage) -> None:
    hass_storage[UPDATE_STORAGE_KEY] = {
        "version": 1,
        "key": UPDATE_STORAGE_KEY,
        "data": {"last_check": time.time(), "notified_version": "0.3.0"},
    }
    persistent_notification.async_create(hass, "old", notification_id=NOTIFICATION_ID)
    await _setup(hass, _entry(), installed="0.3.0")
    await _run_startup_check(hass, installed="0.3.0")
    assert _notification(hass) is None


async def test_option_off_in_any_claim(hass: HomeAssistant) -> None:
    first = _entry()
    second = _entry("sensor.other_plug_today_s_consumption", **{CONF_UPDATE_NOTIFY: False})

    await _setup(hass, first)
    assert UPDATE_CHECKER in hass.data

    # A second claim with the option off stops it for the whole integration
    # and removes a notification still showing.
    persistent_notification.async_create(hass, "update", notification_id=NOTIFICATION_ID)
    await _setup(hass, second)
    assert UPDATE_CHECKER not in hass.data
    assert _notification(hass) is None

    # Removing that claim starts it again.
    assert await hass.config_entries.async_unload(second.entry_id)
    await hass.async_block_till_done()
    assert UPDATE_CHECKER in hass.data

    # Turning it on/off through options (which reloads) takes effect at once.
    hass.config_entries.async_update_entry(first, options={**first.options, CONF_UPDATE_NOTIFY: False})
    await hass.async_block_till_done()
    assert UPDATE_CHECKER not in hass.data

    # Unloading the last claim leaves nothing running.
    hass.config_entries.async_update_entry(first, options={**first.options, CONF_UPDATE_NOTIFY: True})
    await hass.async_block_till_done()
    assert UPDATE_CHECKER in hass.data
    assert await hass.config_entries.async_unload(first.entry_id)
    await hass.async_block_till_done()
    assert UPDATE_CHECKER not in hass.data
