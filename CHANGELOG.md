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
