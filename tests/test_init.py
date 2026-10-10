"""Setup / unload tests."""

from homeassistant.config_entries import ConfigEntryState
from homeassistant.const import STATE_UNKNOWN
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
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


def _entry() -> MockConfigEntry:
    return MockConfigEntry(
        domain=DOMAIN,
        title="EV charging",
        data={CONF_NAME: "EV charging", CONF_ENERGY_SENSOR: "sensor.plug_today_s_consumption"},
        options={
            CONF_RATE: 0.407,
            CONF_AUTO_GENERATE: True,
            CONF_GENERATE_TIME: "01:00:00",
            CONF_OUTPUT_FOLDER: "plug_and_claim/ev_charging",
        },
    )


async def test_setup_and_unload(hass: HomeAssistant) -> None:
    entry = _entry()
    entry.add_to_hass(hass)

    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.LOADED

    devices = dr.async_entries_for_config_entry(dr.async_get(hass), entry.entry_id)
    assert [d.name for d in devices] == ["EV charging"]
    assert devices[0].identifiers == {(DOMAIN, entry.entry_id)}

    entity_id = er.async_get(hass).async_get_entity_id("sensor", DOMAIN, f"{entry.entry_id}_last_sync")
    assert entity_id == "sensor.ev_charging_last_sync"
    assert hass.states.get(entity_id).state == STATE_UNKNOWN

    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.NOT_LOADED


async def test_options_change_reloads(hass: HomeAssistant) -> None:
    entry = _entry()
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    coordinator = entry.runtime_data

    hass.config_entries.async_update_entry(entry, options={**entry.options, CONF_RATE: 0.5})
    await hass.async_block_till_done()

    assert entry.state is ConfigEntryState.LOADED
    assert entry.runtime_data is not coordinator
