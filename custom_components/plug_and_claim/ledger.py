"""The daily kWh ledger - Plug & Claim's own source of truth.

Stored in /config/.storage/plug_and_claim.<entry_id> with HA's Store helper,
completely separate from the recorder database, so the claim history survives
a corrupted or purged database. Written with atomic writes (temp file + rename)
so a crash mid-save can't leave a half-written file.

Layout (version 1):
    {
      "days": {"2026-09-01": {"kwh": 6.09, "source": "statistics"}, ...},
      "rates": [],            # unused; rate history lives in the options
      "reports": {"2026-09": {...}},                        # Phase 3
      "finalised_months": ["2026-09"],                      # Phase 3
      "meta": {"last_sync": "<iso>", "backfilled": true, "stats_start": "2025-03-14"},
      "live": {"date": "2026-10-10", "kwh": 3.2, "prev": 3.2, "partial": false}
    }

A day may also carry "gap_hours": n when the recorder had no statistics for
n hours of it (e.g. HA was off) - the value is then possibly incomplete.

This module has no recorder or entity code, so its functions are easy to
unit-test.
"""

from collections.abc import Iterable
from datetime import date, datetime, timedelta, tzinfo
import logging
from typing import Any

from homeassistant.core import HomeAssistant
from homeassistant.helpers.storage import Store

from .const import DOMAIN

_LOGGER = logging.getLogger(__name__)

STORAGE_VERSION = 1

SOURCE_STATISTICS = "statistics"
SOURCE_LIVE = "live"
SOURCE_IMPORT = "import"
SOURCE_MANUAL = "manual"
# Values a person entered or imported are never replaced by an automatic sync.
PROTECTED_SOURCES = {SOURCE_MANUAL, SOURCE_IMPORT}

KWH_DECIMALS = 6


def empty_ledger() -> dict[str, Any]:
    return {
        "days": {},
        "rates": [],
        "reports": {},
        "finalised_months": [],
        "meta": {"last_sync": None, "backfilled": False, "stats_start": None},
        "live": None,
    }


class LedgerStore(Store[dict[str, Any]]):
    """Store with a migration hook for future layout changes."""

    async def _async_migrate_func(
        self, old_major_version: int, old_minor_version: int, old_data: dict[str, Any]
    ) -> dict[str, Any]:
        # Only version 1 exists so far. When the layout changes, bump
        # STORAGE_VERSION and convert old_data here, one version at a time.
        _LOGGER.info(
            "Migrating ledger from version %s.%s", old_major_version, old_minor_version
        )
        return {**empty_ledger(), **old_data}


# --------------------------------------------------------------- date helpers


def month_key(day: date) -> str:
    """'2026-09' for any date in September 2026."""
    return f"{day.year:04d}-{day.month:02d}"


def local_midnight(day: date, tz: tzinfo) -> datetime:
    """Start of a calendar day in the given (local) time zone."""
    return datetime(day.year, day.month, day.day, tzinfo=tz)


def day_bounds(day: date, tz: tzinfo) -> tuple[datetime, datetime]:
    """Local start and end of a day. On DST change days the day is 23 or 25
    hours long; computing both ends from the calendar date gets this right."""
    return local_midnight(day, tz), local_midnight(day + timedelta(days=1), tz)


def expected_hours(day: date, tz: tzinfo) -> int:
    """Hours in a local day: 24, or 23/25 on DST change days.

    Uses timestamps on purpose: subtracting two datetimes that share a
    tzinfo gives wall-clock time in Python, which is always 24 hours.
    """
    start, end = day_bounds(day, tz)
    return round((end.timestamp() - start.timestamp()) / 3600)


def iter_days(first: date, last: date) -> Iterable[date]:
    """Every date from first to last, inclusive."""
    day = first
    while day <= last:
        yield day
        day += timedelta(days=1)


# ------------------------------------------------------- statistics -> days


def aggregate_hourly(
    rows: Iterable[dict[str, Any]], tz: tzinfo
) -> dict[date, tuple[float, int]]:
    """Sum hourly recorder statistics into local calendar days.

    rows are recorder StatisticsRow dicts with "start" (UTC timestamp) and
    "change" (kWh used in that hour). Returns {date: (kwh, hours_with_data)}.
    Grouping by the local date of each hour's start is exactly how the
    recorder builds its own "day" periods, DST included.
    """
    days: dict[date, list[float]] = {}
    for row in rows:
        change = row.get("change")
        if change is None:
            continue
        day = datetime.fromtimestamp(row["start"], tz).date()
        days.setdefault(day, []).append(change)
    return {day: (sum(values), len(values)) for day, values in days.items()}


# ---------------------------------------------------------------- the ledger


class Ledger:
    """In-memory ledger plus its Store. All writes go through these methods."""

    def __init__(self, hass: HomeAssistant, entry_id: str) -> None:
        self._store = LedgerStore(
            hass, STORAGE_VERSION, f"{DOMAIN}.{entry_id}", atomic_writes=True
        )
        self.data: dict[str, Any] = empty_ledger()

    async def async_load(self) -> None:
        stored = await self._store.async_load()
        if stored is not None:
            self.data = {**empty_ledger(), **stored}

    async def async_save(self) -> None:
        await self._store.async_save(self.data)

    def async_delay_save(self, delay: float = 60) -> None:
        """Save soon; used for the frequent live-capture updates."""
        self._store.async_delay_save(lambda: self.data, delay)

    async def async_remove(self) -> None:
        """Delete the file (when the claim is deleted from HA)."""
        await self._store.async_remove()

    # ---- days

    @property
    def days(self) -> dict[str, dict[str, Any]]:
        return self.data["days"]

    def get_day(self, day: date) -> dict[str, Any] | None:
        return self.days.get(day.isoformat())

    def is_finalised(self, day: date) -> bool:
        return month_key(day) in self.data["finalised_months"]

    def set_day(
        self,
        day: date,
        kwh: float,
        source: str,
        *,
        gap_hours: int = 0,
        overwrite_finalised: bool = False,
    ) -> bool:
        """Write one day's kWh. Returns True if the ledger changed.

        Automatic sources (statistics/live) never replace a manual or imported
        value, and never touch a finalised month unless overwrite_finalised.
        Manual and import values always win.
        """
        existing = self.get_day(day)
        automatic = source not in PROTECTED_SOURCES
        if automatic:
            if existing and existing["source"] in PROTECTED_SOURCES:
                return False
            if self.is_finalised(day) and not overwrite_finalised:
                return False

        record: dict[str, Any] = {"kwh": round(max(kwh, 0.0), KWH_DECIMALS), "source": source}
        if gap_hours:
            record["gap_hours"] = gap_hours
        if existing == record:
            return False
        self.days[day.isoformat()] = record
        return True

    def month_days(self, month: str) -> dict[str, dict[str, Any]]:
        """All ledger days of one month ('YYYY-MM'), sorted by date."""
        return {k: v for k, v in sorted(self.days.items()) if k.startswith(f"{month}-")}

    # ---- meta

    @property
    def last_sync(self) -> datetime | None:
        value = self.data["meta"].get("last_sync")
        return datetime.fromisoformat(value) if value else None

    @last_sync.setter
    def last_sync(self, value: datetime) -> None:
        self.data["meta"]["last_sync"] = value.isoformat()

    @property
    def backfilled(self) -> bool:
        return bool(self.data["meta"].get("backfilled"))

    @backfilled.setter
    def backfilled(self, value: bool) -> None:
        self.data["meta"]["backfilled"] = value

    @property
    def stats_start(self) -> date | None:
        """First day the recorder had statistics for (found by the backfill)."""
        value = self.data["meta"].get("stats_start")
        return date.fromisoformat(value) if value else None

    @stats_start.setter
    def stats_start(self, value: date) -> None:
        self.data["meta"]["stats_start"] = value.isoformat()

    @property
    def live(self) -> dict[str, Any] | None:
        return self.data.get("live")

    @live.setter
    def live(self, value: dict[str, Any] | None) -> None:
        self.data["live"] = value
