"""Secure report download links."""

from http import HTTPStatus

from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import MockConfigEntry

from .test_entities import entry  # noqa: F401  (fixture: September ledger, Sydney time)

SELECTED = "sensor.ev_charging_selected_month_claim"
GENERATE = "button.ev_charging_generate_report_for_selected_month"


async def _generate(hass: HomeAssistant) -> dict:
    await hass.services.async_call("button", "press", {"entity_id": GENERATE}, blocking=True)
    await hass.async_block_till_done()
    return hass.states.get(SELECTED).attributes


async def test_signed_links_download(hass: HomeAssistant, entry: MockConfigEntry, hass_client_no_auth) -> None:  # noqa: F811
    assert "pdf_url" not in hass.states.get(SELECTED).attributes  # no report yet
    attributes = await _generate(hass)
    pdf_url, xlsx_url = attributes["pdf_url"], attributes["xlsx_url"]
    assert pdf_url.startswith("/api/plug_and_claim/report/claim1/Energy_Usage_Sep_2026.pdf?authSig=")

    client = await hass_client_no_auth()
    response = await client.get(pdf_url)
    assert response.status == HTTPStatus.OK
    assert response.headers["Content-Type"] == "application/pdf"
    assert response.headers["Content-Disposition"].startswith("inline")
    assert (await response.read()).startswith(b"%PDF")

    response = await client.get(xlsx_url)
    assert response.status == HTTPStatus.OK
    assert response.headers["Content-Disposition"] == 'attachment; filename="Energy_Usage_Sep_2026.xlsx"'

    # The same link is reused rather than re-signed on every update.
    await hass.services.async_call("button", "press", {"entity_id": "button.ev_charging_sync_now"}, blocking=True)
    assert hass.states.get(SELECTED).attributes["pdf_url"] == pdf_url


async def test_links_need_a_valid_signature(
    hass: HomeAssistant, entry: MockConfigEntry, hass_client_no_auth  # noqa: F811
) -> None:
    pdf_url = (await _generate(hass))["pdf_url"]
    client = await hass_client_no_auth()
    assert (await client.get(pdf_url.split("?")[0])).status == HTTPStatus.UNAUTHORIZED
    assert (await client.get(pdf_url[:-3] + "abc")).status == HTTPStatus.UNAUTHORIZED


async def test_only_recorded_report_files(
    hass: HomeAssistant, entry: MockConfigEntry, hass_client  # noqa: F811
) -> None:
    await _generate(hass)
    client = await hass_client()  # logged in
    base = "/api/plug_and_claim/report"
    assert (await client.get(f"{base}/claim1/Energy_Usage_Sep_2026.pdf")).status == HTTPStatus.OK
    for path in (
        f"{base}/claim1/Energy_Usage_Aug_2026.pdf",  # not generated
        f"{base}/claim1/configuration.yaml",
        f"{base}/claim1/..%2F..%2Fsecrets.yaml",
        f"{base}/other_entry/Energy_Usage_Sep_2026.pdf",
    ):
        # 404 from Plug & Claim, or 400 when HA's own security filter
        # rejects the path first (e.g. "../" tricks).
        status = (await client.get(path)).status
        assert status in (HTTPStatus.NOT_FOUND, HTTPStatus.BAD_REQUEST), path


async def test_links_not_recorded_in_history(hass: HomeAssistant, entry: MockConfigEntry) -> None:  # noqa: F811
    from custom_components.plug_and_claim.sensor import MonthSensor

    assert {"pdf_url", "xlsx_url"} <= MonthSensor._unrecorded_attributes
