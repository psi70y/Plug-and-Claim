"""Coordinator for Plug & Claim: owns the ledger and keeps it up to date.

How daily kWh are captured
--------------------------
1. Recorder long-term statistics (normal case). Sensors with a state_class
   (e.g. total_increasing, like a smart plug's "Today's consumption") get
   hourly statistics whose "change" is the energy used in that hour; the
   recorder already handles the midnight reset. We fetch hourly rows and add
   them up per local calendar day (DST-safe, see ledger.aggregate_hourly).
   Hours with no statistics (HA was off) are recorded as "gap_hours".
2. Live capture (fallback). If the sensor has no long-term statistics, we
   follow its state changes and add up each increase per local day, the
   same way the recorder does. A drop is treated as a meter reset. The
   running total is saved so it survives restarts.

When it runs
------------
- On startup (once HA has started): a full backfill the first time, then
  the last 14 days.
- Every night at 00:10 local time: yesterday plus the last 14 days, to fill
  gaps left by downtime.
- On demand via the plug_and_claim.sync action / Sync now button.

Entities read a PlugAndClaimData snapshot that is pushed after every change;
nothing is polled.
"""

import asyncio
from dataclasses import dataclass
from datetime import date, datetime, timedelta
import logging
from typing import Any

from homeassistant.components.recorder import get_instance
from homeassistant.components.recorder.statistics import (
    get_metadata,
    statistics_during_period,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import (
    ATTR_UNIT_OF_MEASUREMENT,
    STATE_UNAVAILABLE,
    STATE_UNKNOWN,
    UnitOfEnergy,
)
from homeassistant.core import Event, EventStateChangedData, HomeAssistant, State, callback
from homeassistant.helpers.event import (
    async_track_state_change_event,
    async_track_time_change,
)
from homeassistant.helpers.start import async_at_started
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator
from homeassistant.util import dt as dt_util

from .const import CONF_ENERGY_SENSOR, CONF_MONTH_SENSOR, DOMAIN
from .ledger import (
    SOURCE_LIVE,
    SOURCE_MANUAL,
    SOURCE_STATISTICS,
    Ledger,
    aggregate_hourly,
    expected_hours,
    iter_days,
    local_midnight,
)

_LOGGER = logging.getLogger(__name__)

type PlugAndClaimConfigEntry = ConfigEntry[PlugAndClaimCoordinator]

DEFAULT_SYNC_DAYS = 14
# Nightly sync time (local). 10 minutes past midnight leaves the recorder
# time to compile the 23:00-24:00 hourly statistics.
SYNC_HOUR = 0
SYNC_MINUTE = 10
# Backfill looks back this far; the recorder simply returns what it has.
BACKFILL_FROM = date(2000, 1, 1)

MODE_STATISTICS = "statistics"
MODE_LIVE = "live"

# Energy units converted to kWh for live capture.
UNIT_FACTORS = {
    UnitOfEnergy.WATT_HOUR: 0.001,
    UnitOfEnergy.KILO_WATT_HOUR: 1.0,
    UnitOfEnergy.MEGA_WATT_HOUR: 1000.0,
}


@dataclass
class PlugAndClaimData:
    """Snapshot of what the entities display."""

    last_sync: datetime | None = None
    ledger_days: int = 0
    mode: str | None = None


@dataclass
class SyncResult:
    """What a sync did, returned by the sync action."""

    mode: str
    start: date
    end: date
    written: dict[str, float]
    gaps: dict[str, int]


class PlugAndClaimCoordinator(DataUpdateCoordinator[PlugAndClaimData]):
    """Holds the ledger for one claim. No update_interval: data is pushed."""

    config_entry: PlugAndClaimConfigEntry

    def __init__(self, hass: HomeAssistant, entry: PlugAndClaimConfigEntry) -> None:
        super().__init__(
            hass,
            _LOGGER,
            config_entry=entry,
            name=f"{DOMAIN} {entry.title}",
        )
        self.energy_sensor: str = entry.data[CONF_ENERGY_SENSOR]
        self.month_sensor: str | None = entry.data.get(CONF_MONTH_SENSOR)
        self.ledger = Ledger(hass, entry.entry_id)
        self.mode: str | None = None
        self._sync_lock = asyncio.Lock()
        self._live_unsub = None

    # ------------------------------------------------------------- lifecycle

    async def _async_setup(self) -> None:
        """Called once by async_config_entry_first_refresh."""
        await self.ledger.async_load()

    async def _async_update_data(self) -> PlugAndClaimData:
        return self._snapshot()

    def _snapshot(self) -> PlugAndClaimData:
        return PlugAndClaimData(
            last_sync=self.ledger.last_sync,
            ledger_days=len(self.ledger.days),
            mode=self.mode,
        )

    def _publish(self) -> None:
        self.async_set_updated_data(self._snapshot())

    @callback
    def async_start(self) -> None:
        """Schedule the startup sync and the nightly sync."""
        entry = self.config_entry

        async def _at_started(_hass: HomeAssistant) -> None:
            await self._async_scheduled_sync()

        entry.async_on_unload(async_at_started(self.hass, _at_started))
        entry.async_on_unload(
            async_track_time_change(
                self.hass, self._async_nightly, hour=SYNC_HOUR, minute=SYNC_MINUTE, second=0
            )
        )
        entry.async_on_unload(self._stop_live)

    async def _async_nightly(self, _now: datetime) -> None:
        await self._async_scheduled_sync()

    async def _async_scheduled_sync(self) -> None:
        """Automatic sync; a failure is logged and retried at the next one."""
        try:
            await self.async_sync()
        except Exception:
            _LOGGER.exception("%s: ledger sync failed", self.config_entry.title)

    async def async_shutdown(self) -> None:
        """Flush any pending live-capture save when the claim unloads."""
        await super().async_shutdown()
        await self.ledger.async_save()

    # ------------------------------------------------------------------ sync

    async def async_sync(
        self, days_back: int | None = None, overwrite_finalised: bool = False
    ) -> SyncResult:
        """Bring the ledger up to date (see module docstring)."""
        async with self._sync_lock:
            today = dt_util.now().date()
            yesterday = today - timedelta(days=1)

            if await self._async_has_statistics():
                self._set_mode(MODE_STATISTICS)
                backfill = days_back is None and not self.ledger.backfilled
                if backfill:
                    start = BACKFILL_FROM
                else:
                    start = today - timedelta(days=days_back or DEFAULT_SYNC_DAYS)
                result = await self._async_sync_statistics(start, yesterday, overwrite_finalised)
                if backfill:
                    self.ledger.backfilled = True
                    _LOGGER.info(
                        "%s: backfilled %d days from recorder statistics",
                        self.config_entry.title,
                        len(result.written),
                    )
            else:
                self._set_mode(MODE_LIVE)
                result = self._sync_live(today)

            self.ledger.last_sync = dt_util.now()
            await self.ledger.async_save()
            self._publish()
            # Warn about gaps once, not every night while the day is in the
            # 14-day window: only for days this sync changed, or yesterday.
            new_gaps = {
                d: h
                for d, h in result.gaps.items()
                if d in result.written or d == yesterday.isoformat()
            }
            if new_gaps:
                _LOGGER.warning(
                    "%s: recorder statistics are missing hours on %s; those days "
                    "may be incomplete (days with no statistics at all are left "
                    "out of the ledger - use the set_day action to fill them)",
                    self.config_entry.title,
                    ", ".join(f"{d} ({h}h)" for d, h in sorted(new_gaps.items())),
                )
            _LOGGER.info(
                "%s: sync %s to %s (%s) updated %d day(s)",
                self.config_entry.title,
                result.start,
                result.end,
                result.mode,
                len(result.written),
            )
            return result

    async def _async_has_statistics(self) -> bool:
        metadata = await get_instance(self.hass).async_add_executor_job(
            lambda: get_metadata(self.hass, statistic_ids={self.energy_sensor})
        )
        return self.energy_sensor in metadata

    async def _async_sync_statistics(
        self, start: date, end: date, overwrite_finalised: bool
    ) -> SyncResult:
        tz = dt_util.get_default_time_zone()
        start_dt = local_midnight(start, tz)
        end_dt = local_midnight(end + timedelta(days=1), tz)

        rows = await get_instance(self.hass).async_add_executor_job(
            lambda: statistics_during_period(
                self.hass,
                start_dt,
                end_dt,
                {self.energy_sensor},
                "hour",
                {"energy": UnitOfEnergy.KILO_WATT_HOUR},
                {"change"},
            ).get(self.energy_sensor, [])
        )
        per_day = aggregate_hourly(rows, tz)

        written: dict[str, float] = {}
        gaps: dict[str, int] = {}
        if not per_day:
            return SyncResult(MODE_STATISTICS, start, end, written, gaps)

        if start == BACKFILL_FROM:
            # Remember the first day the recorder has statistics for.
            self.ledger.stats_start = min(per_day)
        first = max(start, min(per_day))
        for day in iter_days(first, end):
            kwh, hours = per_day.get(day, (0.0, 0))
            missing = expected_hours(day, tz) - hours
            if hours == 0:
                # No statistics at all for this day: leave it missing
                # rather than claiming 0 kWh.
                gaps[day.isoformat()] = missing
                continue
            if missing and day == self.ledger.stats_start:
                # Statistics began part-way through this day, so its total
                # would be short. Leave it missing (set it with set_day).
                continue
            if missing > 0:
                gaps[day.isoformat()] = missing
            if self.ledger.set_day(
                day,
                kwh,
                SOURCE_STATISTICS,
                gap_hours=max(missing, 0),
                overwrite_finalised=overwrite_finalised,
            ):
                written[day.isoformat()] = round(kwh, 6)

        return SyncResult(MODE_STATISTICS, first, end, written, gaps)

    # ---------------------------------------------------------- live capture

    def _set_mode(self, mode: str) -> None:
        if mode == self.mode:
            return
        self.mode = mode
        if mode == MODE_LIVE:
            _LOGGER.warning(
                "%s: %s has no long-term statistics (it needs a state_class such as "
                "total_increasing), so daily kWh are captured live from its state "
                "changes instead. Days when Home Assistant was off may be incomplete",
                self.config_entry.title,
                self.energy_sensor,
            )
            self._start_live()
        else:
            self._stop_live()

    def _start_live(self) -> None:
        if self._live_unsub is None:
            self._live_unsub = async_track_state_change_event(
                self.hass, self.energy_sensor, self._async_live_event
            )
            if state := self.hass.states.get(self.energy_sensor):
                self._live_update(state)

    @callback
    def _stop_live(self) -> None:
        if self._live_unsub is not None:
            self._live_unsub()
            self._live_unsub = None

    @callback
    def _async_live_event(self, event: Event[EventStateChangedData]) -> None:
        if (state := event.data["new_state"]) is not None:
            self._live_update(state)

    def _live_update(self, state: State) -> None:
        """Add this reading's increase to today's running total."""
        value = _kwh_from_state(state)
        if value is None:
            return
        today = dt_util.as_local(state.last_updated).date()
        live = self.ledger.live

        if live is None:
            # First reading ever: we don't know what was used earlier today.
            self.ledger.live = {"date": today.isoformat(), "kwh": 0.0, "prev": value, "partial": True}
            self.ledger.async_delay_save()
            return

        if live["date"] != today.isoformat():
            self._finalise_live(today)
            live = self.ledger.live

        prev = live["prev"]
        # An increase adds to the day; a drop means the meter reset (e.g. at
        # midnight), so the whole new reading counts.
        increase = value - prev if value >= prev else value
        live["kwh"] = round(live["kwh"] + increase, 6)
        live["prev"] = value
        self.ledger.async_delay_save()

    def _finalise_live(self, today: date) -> None:
        """Move a finished day's running total into the ledger."""
        live = self.ledger.live
        if live is None or live["date"] == today.isoformat():
            return
        day = date.fromisoformat(live["date"])
        if not live.get("partial"):
            self.ledger.set_day(day, live["kwh"], SOURCE_LIVE)
        else:
            _LOGGER.info(
                "%s: not recording %s from live capture because capture started part-way through the day",
                self.config_entry.title,
                day,
            )
        self.ledger.live = {"date": today.isoformat(), "kwh": 0.0, "prev": live["prev"], "partial": False}

    def _sync_live(self, today: date) -> SyncResult:
        before = dict(self.ledger.days)
        self._finalise_live(today)
        written = {
            k: v["kwh"] for k, v in self.ledger.days.items() if before.get(k) != v
        }
        return SyncResult(MODE_LIVE, today, today, written, {})

    # ---------------------------------------------------------- month check

    async def async_month_sensor_totals(self) -> dict[str, float]:
        """The optional monthly sensor's final reading for each ledger month.

        This is the plug's own month total, used to show how far the ledger
        (built from the daily sensor) is from it. A daily sensor misses the
        last few seconds before each midnight reset, so the ledger is usually
        a few Wh per charging night lower. Returns {} if there is no monthly
        sensor or it has no statistics.
        """
        if not self.month_sensor or not self.ledger.days:
            return {}
        tz = dt_util.get_default_time_zone()
        first = date.fromisoformat(min(self.ledger.days)).replace(day=1)
        rows = await get_instance(self.hass).async_add_executor_job(
            lambda: statistics_during_period(
                self.hass,
                local_midnight(first, tz),
                None,
                {self.month_sensor},
                "month",
                {"energy": UnitOfEnergy.KILO_WATT_HOUR},
                {"state"},
            ).get(self.month_sensor, [])
        )
        return {
            datetime.fromtimestamp(row["start"], tz).strftime("%Y-%m"): round(row["state"], 6)
            for row in rows
            if row.get("state") is not None
        }

    # ---------------------------------------------------------------- set_day

    async def async_set_day(self, day: date, kwh: float) -> None:
        """Manual correction (always wins, even in a finalised month)."""
        async with self._sync_lock:
            self.ledger.set_day(day, kwh, SOURCE_MANUAL)
            await self.ledger.async_save()
            self._publish()
            _LOGGER.info("%s: %s set manually to %s kWh", self.config_entry.title, day, kwh)


def _kwh_from_state(state: State) -> float | None:
    if state.state in (STATE_UNKNOWN, STATE_UNAVAILABLE):
        return None
    try:
        value = float(state.state)
    except ValueError:
        return None
    factor = UNIT_FACTORS.get(state.attributes.get(ATTR_UNIT_OF_MEASUREMENT), 1.0)
    return value * factor


def ledger_summary(ledger: Ledger) -> dict[str, Any]:
    """Per-month totals, used by diagnostics."""
    months: dict[str, dict[str, Any]] = {}
    for key, record in sorted(ledger.days.items()):
        month = months.setdefault(key[:7], {"days": 0, "kwh": 0.0})
        month["days"] += 1
        month["kwh"] = round(month["kwh"] + record["kwh"], 6)
    return months
