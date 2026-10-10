"""Constants for the Plug & Claim integration."""

from homeassistant.const import Platform

DOMAIN = "plug_and_claim"

PLATFORMS: list[Platform] = [Platform.SENSOR]

# ---- Setup step 1 (source) - stored in entry.data, fixed after setup ----
CONF_NAME = "name"
CONF_ENERGY_SENSOR = "energy_sensor"
CONF_POWER_SENSOR = "power_sensor"
CONF_MONTH_SENSOR = "month_sensor"

# ---- Setup step 2 (claim details) - stored in entry.options, editable ----
CONF_RATE = "rate"
CONF_VEHICLE = "vehicle"
CONF_REGISTRATION = "registration"
CONF_AUTO_GENERATE = "auto_generate"
CONF_GENERATE_TIME = "generate_time"
CONF_OUTPUT_FOLDER = "output_folder"
CONF_NOTIFY_SERVICE = "notify_service"
CONF_INCLUDE_HEADER = "include_header"

DEFAULT_NAME = "EV charging"
DEFAULT_RATE = 0.407
DEFAULT_AUTO_GENERATE = True
DEFAULT_INCLUDE_HEADER = True
# Reports are generated on the 1st of the month at this local time.
DEFAULT_GENERATE_TIME = "01:00:00"
# Output folder is relative to HA's media folder; the claim's slug is appended.
OUTPUT_FOLDER_ROOT = "plug_and_claim"

CURRENCY = "AUD"
RATE_DECIMALS = 6

# Fired after a report is written: {entry_id, month, kwh, rate, amount, files}.
EVENT_REPORT_GENERATED = f"{DOMAIN}_report_generated"

# Translation keys (entity_id suffixes) the core TP-Link integration gives
# its plug sensors. Only used to pre-fill the setup form; any energy/power
# sensor from any integration can be chosen.
TPLINK_PLATFORM = "tplink"
TPLINK_TODAY_KEY = "consumption_today"
TPLINK_MONTH_KEY = "consumption_this_month"
TPLINK_POWER_KEY = "current_consumption"
