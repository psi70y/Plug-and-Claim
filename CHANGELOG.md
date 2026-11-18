# Changelog

## 0.2.0 - 2026-11-18

- New-version notifications: once a day the integration asks GitHub for the
  latest release and, if it's newer than the installed version, shows a Home
  Assistant notification with a link to the release notes. One notification
  per release; it clears itself after you update. Only GitHub is contacted,
  and the time of the last check is saved so restarts don't add checks.
- New option **Notify me about new versions** (on by default) under
  Configure. Turning it off in any claim turns it off for the whole
  integration.
- Notifications start with the release after this one.

## 0.1.0 - 2026-10-11

First release.

- Config flow (claim name and sensors, then claim details) and options flow.
- One device per claim with a diagnostic "Last sync" sensor.
- Daily kWh ledger stored in its own file, filled from recorder long-term
  statistics (local days, DST-safe), with live capture as a fallback.
- Backfill on first start, nightly sync at 00:10 covering the last 14 days.
- `sync` and `set_day` actions, "Ledger days" sensor and diagnostics download.
- Diagnostics compare each month's ledger total with the optional monthly
  sensor's own total.
- Monthly claim report as XLSX and PDF (one A4 page) in the Tapo-export
  layout, with an optional header block; amounts rounded half-up to cents.
- Reports saved under the media folder by year; `generate_report` action,
  `plug_and_claim_report_generated` event and optional notification.
- Generating a report finalises the month.
- Sensors for this month (including today so far), last month and a selected
  month (kWh and AUD), a "Report month" select, "Generate report" and
  "Sync now" buttons.
- Days with no data count as 0 kWh (listed in diagnostics only).
- Secure download links (signed, 24 hours, not recorded in history) for the
  selected month's PDF and XLSX, and an example dashboard card.
- Automatic report for the previous month on the 1st at the set time, with
  catch-up at startup during the first 7 days if Home Assistant was off.
- Rate history: changing the rate asks which month it applies from, so
  earlier months keep their rate.
- Brand icon and logo.
- GitHub workflow running hassfest, the HACS check and the tests.
