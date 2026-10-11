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
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from decimal import Decimal
import logging
from pathlib import Path
from typing import Any

from homeassistant.components.recorder import get_instance
from homeassistant.components.recorder.statistics import (
    get_metadata,
    statistic_during_period,
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
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.event import (
    async_track_state_change_event,
    async_track_time_change,
)
from homeassistant.helpers.start import async_at_started
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator
from homeassistant.util import dt as dt_util

from .const import (
    CONF_ENERGY_SENSOR,
    CONF_INCLUDE_HEADER,
    CONF_MONTH_SENSOR,
    CONF_NOTIFY_SERVICE,
    CONF_OUTPUT_FOLDER,
    CONF_RATE,
    CONF_REGISTRATION,
    CONF_VEHICLE,
    DEFAULT_INCLUDE_HEADER,
    DEFAULT_RATE,
    DOMAIN,
    EVENT_REPORT_GENERATED,
)
from .ledger import (
    SOURCE_LIVE,
    SOURCE_MANUAL,
    SOURCE_STATISTICS,
    Ledger,
    aggregate_hourly,
    expected_hours,
    iter_days,
    local_midnight,
    month_key,
)
from .report import MonthReport, build_report, write_pdf, write_xlsx

_LOGGER = logging.getLogger(__name__)

type PlugAndClaimConfigEntry = ConfigEntry[PlugAndClaimCoordinator]

DEFAULT_SYNC_DAYS = 14
# How often "this month" (which includes today so far) is recalculated.
REFRESH_INTERVAL = timedelta(minutes=5)
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
class MonthSummary:
    """One month's figures as the sensors show them."""

    month: str
    kwh: Decimal
    amount: Decimal
    rate: float
    day_count: int
    missing_days: list[str]
    report: dict[str, Any] | None = None


@dataclass
class PlugAndClaimData:
    """Snapshot of what the entities display."""

    last_sync: datetime | None = None
    ledger_days: int = 0
    mode: str | None = None
    this_month: MonthSummary | None = None
    last_month: MonthSummary | None = None
    selected: MonthSummary | None = None
    # Finished months that can be reported on, newest first ("2026-09").
    report_months: list[str] = field(default_factory=list)


class ReportError(Exception):
    """A report couldn't be generated; .key is a translation key."""

    def __init__(self, key: str, **placeholders: str) -> None:
        super().__init__(key)
        self.key = key
        self.placeholders = placeholders


@dataclass
class SyncResult:
    """What a sync did, returned by the sync action."""

    mode: str
    start: date
    end: date
    written: dict[str, float]
    gaps: dict[str, int]


class PlugAndClaimCoordinator(DataUpdateCoordinator[PlugAndClaimData]):
    """Holds the ledger for one claim.

    Ledger changes are pushed straight away (async_refresh after each sync,
    report or correction). The 5-minute interval only keeps "this month",
    which includes today's usage so far, up to date.
    """

    config_entry: PlugAndClaimConfigEntry

    def __init__(self, hass: HomeAssistant, entry: PlugAndClaimConfigEntry) -> None:
        super().__init__(
            hass,
            _LOGGER,
            config_entry=entry,
            name=f"{DOMAIN} {entry.title}",
            update_interval=REFRESH_INTERVAL,
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
        today = dt_util.now().date()
        this_month = month_key(today)
        last_month = month_key(today.replace(day=1) - timedelta(days=1))
        months = self.report_months(today)
        selected = self.selected_month(months)
        return PlugAndClaimData(
            last_sync=self.ledger.last_sync,
            ledger_days=len(self.ledger.days),
            mode=self.mode,
            this_month=self.month_summary(this_month, today, await self._async_today_kwh(today)),
            last_month=self.month_summary(last_month, today),
            selected=self.month_summary(selected, today) if selected else None,
            report_months=months,
        )

    async def _async_publish(self) -> None:
        """Recalculate what the entities show after the ledger changed."""
        await self.async_refresh()

    async def _async_today_kwh(self, today: date) -> float | None:
        """Energy used so far today (not in the ledger until after midnight)."""
        if self.mode == MODE_LIVE:
            live = self.ledger.live
            return live["kwh"] if live and live["date"] == today.isoformat() else None
        if self.mode != MODE_STATISTICS:
            return None
        start = local_midnight(today, dt_util.get_default_time_zone())
        try:
            result = await get_instance(self.hass).async_add_executor_job(
                lambda: statistic_during_period(
                    self.hass, start, None, self.energy_sensor, {"change"},
                    {"energy": UnitOfEnergy.KILO_WATT_HOUR},
                )
            )
        except Exception:  # an estimate; never fail the refresh over it
            _LOGGER.debug("Couldn't read today's usage so far", exc_info=True)
            return None
        return result.get("change")

    # ------------------------------------------------------- month figures

    def report_months(self, today: date) -> list[str]:
        """Finished months that have ledger days or a report, newest first."""
        current = month_key(today)
        months = {key[:7] for key in self.ledger.days} | set(self.ledger.data["reports"])
        return sorted((m for m in months if m < current), reverse=True)

    def selected_month(self, months: list[str]) -> str | None:
        """The Report month select's choice; defaults to the newest month."""
        chosen = self.ledger.data["meta"].get("selected_month")
        if chosen in months:
            return chosen
        return months[0] if months else None

    async def async_select_month(self, month: str) -> None:
        self.ledger.data["meta"]["selected_month"] = month
        self.ledger.async_delay_save(5)
        await self._async_publish()

    def month_summary(self, month: str, today: date, today_kwh: float | None = None) -> MonthSummary:
        """Totals for a month from the ledger, as the report would show them.

        For the current month, today's usage so far is added and only days
        before today can count as missing.
        """
        days = {k: v["kwh"] for k, v in self.ledger.month_days(month).items()}
        if today_kwh is not None and month == month_key(today):
            days[today.isoformat()] = today_kwh
        rate = self.rate_for_month(month)
        report = build_report(month, days, rate)
        missing = [d.isoformat() for d in report.missing_days if d < today]
        return MonthSummary(
            month=month,
            kwh=report.total_kwh,
            amount=report.amount,
            rate=rate,
            day_count=len(days),
            missing_days=missing,
            report=self.ledger.data["reports"].get(month),
        )

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
        self,
        days_back: int | None = None,
        overwrite_finalised: bool = False,
        *,
        first_day: date | None = None,
        last_day: date | None = None,
    ) -> SyncResult:
        """Bring the ledger up to date (see module docstring).

        first_day/last_day re-read one specific range instead (used when a
        report is regenerated with refresh_data).
        """
        async with self._sync_lock:
            today = dt_util.now().date()
            yesterday = today - timedelta(days=1)

            if await self._async_has_statistics():
                self._set_mode(MODE_STATISTICS)
                backfill = days_back is None and first_day is None and not self.ledger.backfilled
                if backfill:
                    start = BACKFILL_FROM
                elif first_day is not None:
                    start = first_day
                else:
                    start = today - timedelta(days=days_back or DEFAULT_SYNC_DAYS)
                end = min(last_day or yesterday, yesterday)
                result = await self._async_sync_statistics(start, end, overwrite_finalised)
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
            await self._async_publish()
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

    # --------------------------------------------------------------- reports

    def rate_for_month(self, month: str) -> float:
        """The rate that applies to a month.

        Rate history (ledger "rates", added in Phase 5) wins: the latest entry
        whose "from" month is on or before this month. Otherwise the current
        rate from the claim's settings.
        """
        applicable = [r for r in self.ledger.data["rates"] if r["from"] <= month]
        if applicable:
            return max(applicable, key=lambda r: r["from"])["rate"]
        return self.config_entry.options.get(CONF_RATE, DEFAULT_RATE)

    def media_root(self) -> Path:
        """HA's media folder (/media on HA OS), as the Media browser sees it."""
        media_dirs = self.hass.config.media_dirs
        root = media_dirs.get("local") or next(iter(media_dirs.values()), None)
        return Path(root or self.hass.config.path("media"))

    def report_folder(self, month: str) -> Path:
        """<media>/<output folder>/<YYYY>, refusing anything outside media."""
        root = self.media_root().resolve()
        folder = (root / self.config_entry.options[CONF_OUTPUT_FOLDER] / month[:4]).resolve()
        if not folder.is_relative_to(root):
            raise ReportError("invalid_folder", folder=str(folder))
        return folder

    async def async_generate_report(
        self, month: str, *, refresh_data: bool = False, notify: bool | None = None
    ) -> dict[str, Any]:
        """Write the XLSX and PDF for one finished month and record it.

        refresh_data re-reads that month from the recorder first, even if the
        month was finalised. notify defaults to "yes if a notify action is
        configured". Returns the same data as the report-generated event.
        """
        today = dt_util.now().date()
        if month >= month_key(today):
            raise ReportError("month_not_finished", month=month)

        first = date.fromisoformat(f"{month}-01")
        if refresh_data:
            last = (first + timedelta(days=32)).replace(day=1) - timedelta(days=1)
            await self.async_sync(first_day=first, last_day=last, overwrite_finalised=True)

        options = self.config_entry.options
        rate = self.rate_for_month(month)
        report = build_report(
            month,
            {k: v["kwh"] for k, v in self.ledger.month_days(month).items()},
            rate,
            vehicle=options.get(CONF_VEHICLE),
            registration=options.get(CONF_REGISTRATION),
            source=self.energy_sensor,
            generated=dt_util.now(),
            include_header=options.get(CONF_INCLUDE_HEADER, DEFAULT_INCLUDE_HEADER),
        )
        folder = self.report_folder(month)
        files = await self.hass.async_add_executor_job(_write_report_files, report, folder)

        self._update_missing_days_issue(report)

        result = {
            "entry_id": self.config_entry.entry_id,
            "month": month,
            "kwh": float(report.total_kwh),
            "rate": float(report.rate),
            "amount": float(report.amount),
            "files": [str(f) for f in files],
        }
        async with self._sync_lock:
            self.ledger.data["reports"][month] = {
                "generated": report.generated.isoformat(),
                "kwh": result["kwh"],
                "rate": result["rate"],
                "amount": result["amount"],
                "files": result["files"],
                "missing_days": [d.isoformat() for d in report.missing_days],
            }
            # A month with a report is finalised: automatic syncs leave it alone.
            if month not in self.ledger.data["finalised_months"]:
                self.ledger.data["finalised_months"].append(month)
                self.ledger.data["finalised_months"].sort()
            await self.ledger.async_save()
        await self._async_publish()

        _LOGGER.info(
            "%s: %s report: %s kWh x $%s = $%s, saved to %s",
            self.config_entry.title,
            report.long_name,
            report.total_kwh,
            report.rate,
            report.amount,
            folder,
        )
        self.hass.bus.async_fire(EVENT_REPORT_GENERATED, result)

        notify_service = options.get(CONF_NOTIFY_SERVICE)
        if notify_service and (notify is None or notify):
            await self._async_notify(notify_service, report, folder)
        return result

    def _update_missing_days_issue(self, report: MonthReport) -> None:
        """Raise a Repairs issue when a report has days with no data, or clear
        it once the month is regenerated complete (e.g. after set_day)."""
        issue_id = missing_days_issue_id(self.config_entry.entry_id, report.month)
        if not report.missing_days:
            ir.async_delete_issue(self.hass, DOMAIN, issue_id)
            return
        days = ", ".join(d.isoformat() for d in report.missing_days)
        _LOGGER.warning(
            "%s: %s report generated with no data for %s",
            self.config_entry.title,
            report.long_name,
            days,
        )
        ir.async_create_issue(
            self.hass,
            DOMAIN,
            issue_id,
            is_fixable=False,
            severity=ir.IssueSeverity.WARNING,
            translation_key="missing_days",
            translation_placeholders={
                "name": self.config_entry.title,
                "month": report.long_name,
                "days": days,
            },
        )

    async def _async_notify(self, service: str, report: MonthReport, folder: Path) -> None:
        # HA's Media browser only lists audio/video/images, so point to the
        # folder itself (reachable via Samba or the File editor).
        domain, _, name = service.partition(".")
        message = (
            f"{report.long_name} {self.config_entry.title}: "
            f"{report.total_kwh:.2f} kWh × ${report.rate.normalize()} = ${report.amount}. "
            f"Report saved to {folder}."
        )
        if report.missing_days:
            message += f" {len(report.missing_days)} day(s) have no data."
        try:
            await self.hass.services.async_call(
                domain, name, {"title": "Plug & Claim", "message": message}, blocking=True
            )
        except Exception:
            # A failed notification must not lose the report itself.
            _LOGGER.exception("%s: couldn't send the report notification via %s", self.config_entry.title, service)

    # ---------------------------------------------------------------- set_day

    async def async_set_day(self, day: date, kwh: float) -> None:
        """Manual correction (always wins, even in a finalised month)."""
        async with self._sync_lock:
            self.ledger.set_day(day, kwh, SOURCE_MANUAL)
            await self.ledger.async_save()
            await self._async_publish()
            _LOGGER.info("%s: %s set manually to %s kWh", self.config_entry.title, day, kwh)


def missing_days_issue_id(entry_id: str, month: str) -> str:
    return f"missing_days_{entry_id}_{month}"


def _write_report_files(report: MonthReport, folder: Path) -> list[Path]:
    """Runs in the executor: create the year folder and write both files."""
    folder.mkdir(parents=True, exist_ok=True)
    xlsx = folder / f"{report.file_stem}.xlsx"
    pdf = folder / f"{report.file_stem}.pdf"
    write_xlsx(report, xlsx)
    write_pdf(report, pdf)
    return [xlsx, pdf]


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
