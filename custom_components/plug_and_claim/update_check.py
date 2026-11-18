"""New-version notification.

Asks GitHub for the integration's latest release at most once a day and
shows a Home Assistant notification when it's newer than the installed
version. Only GitHub is contacted.

There's one checker per Home Assistant instance, however many claims are
set up. It runs while at least one claim is loaded and every loaded claim
has the "Notify me about new versions" option on (see async_sync_update_check).
"""

from __future__ import annotations

import asyncio
from datetime import timedelta
import logging
import time
from typing import Any

import aiohttp
from awesomeversion import AwesomeVersion, AwesomeVersionException

from homeassistant.components import persistent_notification
from homeassistant.config_entries import ConfigEntry, ConfigEntryState
from homeassistant.core import CALLBACK_TYPE, HomeAssistant, callback
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.event import async_call_later, async_track_time_interval
from homeassistant.helpers.storage import Store
from homeassistant.loader import async_get_integration

from .const import CONF_UPDATE_NOTIFY, DEFAULT_UPDATE_NOTIFY, DOMAIN

_LOGGER = logging.getLogger(__name__)

LATEST_RELEASE_URL = "https://api.github.com/repos/psi70y/Plug-and-Claim/releases/latest"
RELEASES_PAGE_URL = "https://github.com/psi70y/Plug-and-Claim/releases"

# hass.data key holding the running checker. Each claim keeps its own data in
# entry.runtime_data, so this is the integration's only hass.data key.
UPDATE_CHECKER = f"{DOMAIN}_update_checker"
NOTIFICATION_ID = f"{DOMAIN}_update_available"

UPDATE_STORAGE_VERSION = 1
UPDATE_STORAGE_KEY = f"{DOMAIN}_update_check"
# Ask GitHub at most this often. The time of the last check is saved to disk,
# so Home Assistant restarts don't cause extra checks.
CHECK_INTERVAL_SECONDS = 24 * 3600
# How often to see whether a check is due (cheap: no network unless it is).
DUE_POLL_INTERVAL = timedelta(hours=1)
# Wait a little after startup, so the check doesn't add to the startup load.
STARTUP_DELAY_SECONDS = 120
REQUEST_TIMEOUT_SECONDS = 15

# Entries that count as "loaded" for the checker. An entry is still
# SETUP_IN_PROGRESS when it calls async_sync_update_check at the end of its
# own setup, so that state counts too. An entry being unloaded is
# UNLOAD_IN_PROGRESS, so it's left out when it calls in from its unload.
_ACTIVE_STATES = (ConfigEntryState.LOADED, ConfigEntryState.SETUP_IN_PROGRESS)


def _parse_version(value: Any) -> AwesomeVersion | None:
    """Turn "v0.2.0" / "0.2.0" into a comparable version, or None."""
    if value is None:
        return None
    text = str(value).strip().lstrip("vV")
    try:
        version = AwesomeVersion(text)
        # Comparing raises for strings that aren't version-like; check now.
        version > AwesomeVersion("0")  # noqa: B015
    except AwesomeVersionException:
        return None
    return version


class UpdateChecker:
    """Checks GitHub for a newer release and shows a notification."""

    def __init__(self, hass: HomeAssistant) -> None:
        self.hass = hass
        self._store: Store[dict[str, Any]] = Store(hass, UPDATE_STORAGE_VERSION, UPDATE_STORAGE_KEY)
        # {"last_check": epoch seconds, "notified_version": "0.3.0"}
        self._data: dict[str, Any] = {}
        self._unsubs: list[CALLBACK_TYPE] = []
        self._lock = asyncio.Lock()

    async def async_start(self) -> None:
        self._data = await self._store.async_load() or {}
        self._unsubs.append(
            async_call_later(self.hass, STARTUP_DELAY_SECONDS, self._async_maybe_check)
        )
        self._unsubs.append(
            async_track_time_interval(self.hass, self._async_maybe_check, DUE_POLL_INTERVAL)
        )

    @callback
    def async_stop(self) -> None:
        while self._unsubs:
            self._unsubs.pop()()

    async def _async_maybe_check(self, _now: Any = None) -> None:
        async with self._lock:
            installed = _parse_version((await async_get_integration(self.hass, DOMAIN)).version)
            if installed is None:
                return

            # After an update, clear a notification about the version now
            # installed (or an older one).
            notified = _parse_version(self._data.get("notified_version"))
            if notified is not None and installed >= notified:
                persistent_notification.async_dismiss(self.hass, NOTIFICATION_ID)

            # time.time() (wall clock), not monotonic, so this survives restarts.
            if time.time() - float(self._data.get("last_check", 0)) < CHECK_INTERVAL_SECONDS:
                return

            release = await self._async_fetch_latest_release()
            # Count failed checks too, so a GitHub outage doesn't cause a
            # retry every hour.
            self._data["last_check"] = time.time()
            if release is not None:
                self._async_handle_release(installed, release)
            await self._store.async_save(self._data)

    async def _async_fetch_latest_release(self) -> dict[str, Any] | None:
        session = async_get_clientsession(self.hass)
        try:
            async with session.get(
                LATEST_RELEASE_URL,
                headers={"Accept": "application/vnd.github+json"},
                timeout=aiohttp.ClientTimeout(total=REQUEST_TIMEOUT_SECONDS),
            ) as resp:
                if resp.status != 200:
                    _LOGGER.debug("Update check got HTTP %s from GitHub", resp.status)
                    return None
                data = await resp.json()
        except (aiohttp.ClientError, asyncio.TimeoutError, ValueError) as err:
            _LOGGER.debug("Update check failed: %s", err)
            return None
        return data if isinstance(data, dict) else None

    @callback
    def _async_handle_release(self, installed: AwesomeVersion, release: dict[str, Any]) -> None:
        latest = _parse_version(release.get("tag_name"))
        if latest is None or latest <= installed:
            return
        if str(latest) == self._data.get("notified_version"):
            # Already told about this one (and maybe dismissed it).
            return

        notes_url = release.get("html_url") or RELEASES_PAGE_URL
        _LOGGER.info("Plug & Claim %s is available (installed: %s)", latest, installed)
        persistent_notification.async_create(
            self.hass,
            (
                f"Plug & Claim **v{latest}** is available (installed: v{installed}).\n\n"
                f"[View release notes]({notes_url})\n\n"
                "Update it from HACS. To stop these notifications, turn off "
                "\"Notify me about new versions\" under Configure on any claim."
            ),
            title="Plug & Claim update available",
            notification_id=NOTIFICATION_ID,
        )
        self._data["notified_version"] = str(latest)


def _update_notify_enabled(entry: ConfigEntry) -> bool:
    return bool(entry.options.get(CONF_UPDATE_NOTIFY, DEFAULT_UPDATE_NOTIFY))


async def async_sync_update_check(hass: HomeAssistant) -> None:
    """Start or stop the checker to match the loaded claims.

    Called at the end of every entry setup and unload. It runs while at
    least one claim is loaded and none of them has the option turned off.
    """
    loaded = [
        entry
        for entry in hass.config_entries.async_entries(DOMAIN)
        if entry.state in _ACTIVE_STATES
    ]
    wanted = bool(loaded) and all(_update_notify_enabled(entry) for entry in loaded)
    checker: UpdateChecker | None = hass.data.get(UPDATE_CHECKER)

    if wanted and checker is None:
        checker = UpdateChecker(hass)
        hass.data[UPDATE_CHECKER] = checker
        await checker.async_start()
    elif not wanted and checker is not None:
        checker.async_stop()
        hass.data.pop(UPDATE_CHECKER, None)
        if loaded:
            # Turned off in Configure: remove a notification still showing.
            persistent_notification.async_dismiss(hass, NOTIFICATION_ID)
