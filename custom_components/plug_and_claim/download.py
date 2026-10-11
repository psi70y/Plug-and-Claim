"""Secure download links for generated reports.

Home Assistant's Media browser only shows audio, video and images, so reports
are served by this small view instead:

    /api/plug_and_claim/report/<entry_id>/<file name>

It needs a logged-in user or a signed link (HA's "authSig"), and it only
serves files that Plug & Claim itself recorded when it generated a report,
so it can't be used to read any other file.

Signed links are what the Selected month sensors expose as pdf_url/xlsx_url.
HA makes a new signing secret at every start, so links stop working after a
restart; they are simply re-created. Each link lasts LINK_LIFETIME and is
renewed when less than half of that is left.
"""

from datetime import timedelta
from http import HTTPStatus
from pathlib import Path
import time

from aiohttp import web

from homeassistant.components.http import HomeAssistantView
from homeassistant.components.http.auth import async_sign_path
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.http import KEY_HASS

from .const import DOMAIN

URL_PREFIX = f"/api/{DOMAIN}/report"
LINK_LIFETIME = timedelta(hours=24)

CONTENT_TYPES = {
    ".pdf": "application/pdf",
    ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
}

# hass.data key for {path: (signed url, expiry time)}. Kept per HA instance,
# like HA's signing secret.
DATA_SIGNED_URLS = f"{DOMAIN}_signed_urls"


def report_path(entry_id: str, file: str | Path) -> str:
    return f"{URL_PREFIX}/{entry_id}/{Path(file).name}"


@callback
def async_signed_url(hass: HomeAssistant, entry_id: str, file: str | Path) -> str:
    """A signed link to a report file, reused until half its lifetime is gone."""
    path = report_path(entry_id, file)
    signed: dict[str, tuple[str, float]] = hass.data.setdefault(DATA_SIGNED_URLS, {})
    now = time.time()
    cached = signed.get(path)
    if cached and cached[1] - now > LINK_LIFETIME.total_seconds() / 2:
        return cached[0]
    url = async_sign_path(hass, path, LINK_LIFETIME, use_content_user=True)
    signed[path] = (url, now + LINK_LIFETIME.total_seconds())
    return url


class ReportDownloadView(HomeAssistantView):
    """Serves a recorded report file of one claim."""

    url = URL_PREFIX + "/{entry_id}/{file_name}"
    name = f"api:{DOMAIN}:report"
    requires_auth = True

    async def get(self, request: web.Request, entry_id: str, file_name: str) -> web.StreamResponse:
        hass: HomeAssistant = request.app[KEY_HASS]
        entry = hass.config_entries.async_get_entry(entry_id)
        if entry is None or entry.domain != DOMAIN or not hasattr(entry, "runtime_data"):
            return web.Response(status=HTTPStatus.NOT_FOUND)

        # Only files listed in the ledger's report index can be served.
        path = next(
            (
                Path(file)
                for report in entry.runtime_data.ledger.data["reports"].values()
                for file in report.get("files", [])
                if Path(file).name == file_name
            ),
            None,
        )
        if path is None or path.suffix not in CONTENT_TYPES:
            return web.Response(status=HTTPStatus.NOT_FOUND)
        if not await hass.async_add_executor_job(path.is_file):
            return web.Response(status=HTTPStatus.NOT_FOUND)

        # PDFs open in the browser; spreadsheets download.
        disposition = "inline" if path.suffix == ".pdf" else "attachment"
        return web.FileResponse(
            path,
            headers={
                "Content-Type": CONTENT_TYPES[path.suffix],
                "Content-Disposition": f'{disposition}; filename="{path.name}"',
                "Cache-Control": "no-store",
            },
        )

