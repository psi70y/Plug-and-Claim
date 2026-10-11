# Changelog

## Unreleased

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
- Repairs issue when a report has days with no data, cleared automatically
  once the month is complete.
- Secure download links (signed, 24 hours, not recorded in history) for the
  selected month's PDF and XLSX, and an example dashboard card.
