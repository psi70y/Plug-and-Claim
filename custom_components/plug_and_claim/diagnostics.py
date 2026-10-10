"""Diagnostics download for Plug & Claim (Settings -> device -> Download
diagnostics). Shows the settings, ledger size, per-month totals (with the
plug's own month total alongside, if a monthly sensor was chosen), the last
31 days and the report index, so problems can be checked without opening the
storage file."""

from datetime import timedelta
from typing import Any

from homeassistant.components.diagnostics import async_redact_data
from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util

from .const import CONF_REGISTRATION
from .coordinator import PlugAndClaimConfigEntry, ledger_summary

TO_REDACT = {CONF_REGISTRATION}
RECENT_DAYS = 31


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: PlugAndClaimConfigEntry
) -> dict[str, Any]:
    coordinator = entry.runtime_data
    ledger = coordinator.ledger
    since = (dt_util.now().date() - timedelta(days=RECENT_DAYS)).isoformat()

    # Ledger month totals next to the plug's own month total (if a monthly
    # sensor was chosen), so the small daily-reset shortfall is visible.
    months = ledger_summary(ledger)
    plug_totals = await coordinator.async_month_sensor_totals()
    for month, summary in months.items():
        if (plug := plug_totals.get(month)) is not None:
            summary["month_sensor_kwh"] = plug
            summary["difference_kwh"] = round(summary["kwh"] - plug, 6)

    return {
        "entry": {
            "title": entry.title,
            "data": dict(entry.data),
            "options": async_redact_data(dict(entry.options), TO_REDACT),
        },
        "capture_mode": coordinator.mode,
        "ledger": {
            "day_count": len(ledger.days),
            "first_day": min(ledger.days, default=None),
            "last_day": max(ledger.days, default=None),
            "meta": ledger.data["meta"],
            "live": ledger.live,
            "months": months,
            "recent_days": {k: v for k, v in sorted(ledger.days.items()) if k >= since},
            "rates": ledger.data["rates"],
            "reports": ledger.data["reports"],
            "finalised_months": ledger.data["finalised_months"],
        },
    }
