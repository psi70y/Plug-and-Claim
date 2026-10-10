"""Config flow and options flow tests."""

from unittest.mock import patch

from homeassistant import config_entries
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.plug_and_claim.const import (
    CONF_AUTO_GENERATE,
    CONF_ENERGY_SENSOR,
    CONF_GENERATE_TIME,
    CONF_MONTH_SENSOR,
    CONF_NAME,
    CONF_NOTIFY_SERVICE,
    CONF_OUTPUT_FOLDER,
    CONF_POWER_SENSOR,
    CONF_RATE,
    CONF_REGISTRATION,
    CONF_VEHICLE,
    DOMAIN,
)

ENERGY = "sensor.plug_today_s_consumption"

CLAIM_INPUT = {
    CONF_RATE: 0.407,
    CONF_VEHICLE: "Mitsubishi Outlander PHEV",
    CONF_AUTO_GENERATE: True,
    CONF_GENERATE_TIME: "01:00:00",
    CONF_OUTPUT_FOLDER: "plug_and_claim/ev_charging",
}


def _suggested(result, key):
    """The suggested (pre-filled) value of a form field."""
    for field in result["data_schema"].schema:
        if field == key:
            return (field.description or {}).get("suggested_value")
    raise KeyError(key)


async def _start(hass: HomeAssistant):
    return await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )


async def _to_claim_step(hass: HomeAssistant):
    result = await _start(hass)
    return await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_NAME: "  EV charging ", CONF_ENERGY_SENSOR: ENERGY}
    )


def _no_setup():
    return patch("custom_components.plug_and_claim.async_setup_entry", return_value=True)


async def test_full_flow(hass: HomeAssistant) -> None:
    result = await _start(hass)
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "user"

    result = await _to_claim_step(hass)
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "claim"
    assert _suggested(result, CONF_RATE) == 0.407
    assert _suggested(result, CONF_OUTPUT_FOLDER) == "plug_and_claim/ev_charging"

    with _no_setup():
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {**CLAIM_INPUT, CONF_REGISTRATION: "  "}
        )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["title"] == "EV charging"
    assert result["data"] == {CONF_NAME: "EV charging", CONF_ENERGY_SENSOR: ENERGY}
    # The blank registration is dropped rather than stored as "".
    assert result["options"] == CLAIM_INPUT


async def test_suggests_tplink_sensors(hass: HomeAssistant) -> None:
    """Sensors of a TP-Link plug are pre-filled; the user can still pick others."""
    config_entry = MockConfigEntry(domain="tplink")
    config_entry.add_to_hass(hass)
    device = dr.async_get(hass).async_get_or_create(
        config_entry_id=config_entry.entry_id, identifiers={("tplink", "plug1")}
    )
    registry = er.async_get(hass)
    ids = {}
    for key in ("consumption_today", "consumption_this_month", "current_consumption"):
        ids[key] = registry.async_get_or_create(
            "sensor", "tplink", f"plug1_{key}",
            device_id=device.id, translation_key=key, suggested_object_id=f"charger_{key}",
        ).entity_id

    result = await _start(hass)
    assert _suggested(result, CONF_ENERGY_SENSOR) == ids["consumption_today"]
    assert _suggested(result, CONF_MONTH_SENSOR) == ids["consumption_this_month"]
    assert _suggested(result, CONF_POWER_SENSOR) == ids["current_consumption"]


async def test_no_suggestion_without_tplink(hass: HomeAssistant) -> None:
    result = await _start(hass)
    assert _suggested(result, CONF_ENERGY_SENSOR) is None


async def test_duplicate_sensor_aborts(hass: HomeAssistant) -> None:
    MockConfigEntry(domain=DOMAIN, data={CONF_NAME: "Car", CONF_ENERGY_SENSOR: ENERGY}).add_to_hass(hass)
    result = await _to_claim_step(hass)
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "already_configured"


async def test_claim_validation_errors(hass: HomeAssistant) -> None:
    result = await _to_claim_step(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {
            **CLAIM_INPUT,
            CONF_RATE: 0,
            CONF_OUTPUT_FOLDER: "../secrets",
            CONF_NOTIFY_SERVICE: "notify.nope",
        },
    )
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {
        CONF_RATE: "invalid_rate",
        CONF_OUTPUT_FOLDER: "invalid_folder",
        CONF_NOTIFY_SERVICE: "notify_not_found",
    }


async def test_notify_service_accepted_when_it_exists(hass: HomeAssistant) -> None:
    hass.services.async_register("notify", "pushover", lambda call: None)
    result = await _to_claim_step(hass)
    with _no_setup():
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            {**CLAIM_INPUT, CONF_OUTPUT_FOLDER: "/claims/car/", CONF_NOTIFY_SERVICE: "notify.pushover"},
        )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["options"][CONF_NOTIFY_SERVICE] == "notify.pushover"
    # Leading/trailing slashes are tidied away.
    assert result["options"][CONF_OUTPUT_FOLDER] == "claims/car"


async def test_options_flow(hass: HomeAssistant) -> None:
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="EV charging",
        data={CONF_NAME: "EV charging", CONF_ENERGY_SENSOR: ENERGY},
        options={**CLAIM_INPUT, CONF_REGISTRATION: "ABC123"},
    )
    entry.add_to_hass(hass)

    result = await hass.config_entries.options.async_init(entry.entry_id)
    assert result["type"] is FlowResultType.FORM
    assert _suggested(result, CONF_REGISTRATION) == "ABC123"

    # Change the rate and clear the registration (an Optional field left out).
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {**CLAIM_INPUT, CONF_RATE: 0.42}
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert entry.options == {**CLAIM_INPUT, CONF_RATE: 0.42}
