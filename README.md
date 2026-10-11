# Plug & Claim

A Home Assistant integration that turns an energy sensor into a monthly
charging claim.

I charge a novated-lease car at home through a smart plug, and every month I
claim the charging cost back. Plug & Claim keeps its own daily kWh ledger for
the plug (separate from the recorder database) and produces the monthly claim
report (XLSX + PDF) for me, saved in Home Assistant's Media folder.

It isn't tied to one brand: any `sensor` with the `energy` device class
(kWh) works, so you can use it with other smart plugs or EV chargers.

> **Status: early development (0.1.0, not released yet).** The daily ledger,
> reports and entities work; report downloads from the dashboard and the
> monthly schedule are still being built.

## Installation (HACS custom repository)

1. In HACS, open the menu (⋮) → **Custom repositories**.
2. Add `https://github.com/psi70y/Plug-and-Claim` with type **Integration**.
3. Install **Plug & Claim** and restart Home Assistant.

Manual install: copy `custom_components/plug_and_claim` into your
`config/custom_components/` folder and restart.

## Setup

**Settings → Devices & services → Add integration → Plug & Claim.**

1. **Choose what to claim:** a name for the claim and the daily energy sensor
   (a sensor that resets every day, like a smart plug's "Today's consumption",
   works best). A power sensor and a monthly energy sensor are optional. If
   you have a TP-Link/Tapo plug, its sensors are filled in for you.
2. **Claim details:** your rate per kWh (AUD, up to 6 decimals), the vehicle
   and registration for the report header, when to generate the report
   automatically, the report folder inside Media, and an optional notify
   action (e.g. `notify.pushover`).

You can change the claim details later with **Configure**. You can add more
than one claim, e.g. one per car or plug.

## How the daily ledger works

Plug & Claim keeps one kWh figure per day in its own file
(`/config/.storage/plug_and_claim.<entry id>`), separate from the recorder
database, so the claim history survives a corrupted or purged database.

- **From long-term statistics (normal case).** If your sensor has a
  `state_class` (smart plugs' "Today's consumption" is `total_increasing`),
  the recorder keeps hourly statistics for it. Plug & Claim adds those up per
  local calendar day, so daylight-saving days are handled correctly.
  - **First start:** it backfills every day the recorder has statistics for.
    The very first day is skipped if the statistics started part-way through it.
  - **Every night at 00:10:** it writes yesterday and re-checks the last
    14 days, which fills gaps after Home Assistant has been off.
  - Days with **no statistics at all** are left out rather than saved as 0.
    Days with some hours missing are kept and flagged (`gap_hours`), and a
    warning is logged.
- **Live capture (fallback).** If the sensor has no statistics, Plug & Claim
  follows its state changes and adds up each day's increase. A warning in the
  log explains this. Days when Home Assistant was off may be incomplete.

Values you set by hand (`set_day`) are never replaced by an automatic sync.

### Small shortfall with daily-reset sensors

A sensor that resets at midnight (like "Today's consumption") is only read
every few seconds, so if the car is charging at midnight the last few seconds
before the reset are never seen. In my case that's 2–3 Wh on a charging night
and about 0.03 kWh a month (around a cent), always slightly under what the
plug's app shows. If you choose the plug's monthly sensor during setup, the
diagnostics show each month's ledger total next to the plug's own month total
so you can see the difference.

## Reports

Each report is an XLSX and a PDF in the same layout as the Tapo app export my
lease provider already knows: a summary row (month, kWh, rate, amount) and one
row per day, including zero days. An optional header block above it shows the
vehicle, registration, period, data source and when it was generated.

- **Saved to:** `<media>/<report folder>/<year>/Energy_Usage_Sep_2026.xlsx`
  and `.pdf`. On Home Assistant OS that's
  `/media/plug_and_claim/ev_charging/2026/`, reachable through the Samba
  share or the File editor. (Home Assistant's Media browser only lists audio,
  video and images, so the reports don't show up there.)
- **Amount** is total kWh × rate, rounded half-up to the cent
  (e.g. 255.303087 × $0.407 = $103.908… → **$103.91**).
- **Days with no data** are shown as "no data" (never as 0), listed in the
  header, and logged as a warning.
- Generating a month **finalises** it: automatic syncs no longer change it.
  Use `refresh_data: true` to re-read it from the recorder; values you set by
  hand are kept.
- If you set a notify action, you get a message like
  "September 2026 EV charging: 255.30 kWh × $0.407 = $103.91. Report saved to
  /media/plug_and_claim/ev_charging/2026."
- The event `plug_and_claim_report_generated` is fired with `entry_id`,
  `month`, `kwh`, `rate`, `amount` and `files`, so you can build your own
  automations (e.g. email the PDF).

## Entities

| Entity | Notes |
| --- | --- |
| This month energy | kWh so far this month, including today (updated every 5 minutes). |
| This month estimated claim | This month's kWh × rate, in AUD. |
| Last month energy / Last month claim | Last month's totals, the same figures the report uses. |
| Report month | Select a finished month, e.g. "2026-09 (Sep 2026)". Remembered across restarts. |
| Selected month energy / Selected month claim | Totals for the month in Report month. Attributes: rate, days with data, missing days, report files, when the report was generated. |
| Generate report for selected month | Button. Writes the report for the month in Report month. |
| Sync now | Diagnostic button. Re-reads the last 14 days from the recorder. |
| Last sync | Diagnostic. When the ledger was last updated. |
| Ledger days | Diagnostic. Number of days in the ledger; attributes show the capture mode and first/last day. |

The month sensors also have `month`, `rate`, `day_count` and `missing_days`
attributes. If a report is generated for a month with days that have no data,
a **Repairs** issue lists them; it clears itself once you fill the days (with
`set_day`) and generate the report again.

## Actions

| Action | What it does |
| --- | --- |
| `plug_and_claim.sync` | Re-reads the last `days_back` days (default 14) from the recorder. Call it with a response to see which days changed. |
| `plug_and_claim.set_day` | Sets one past day's kWh by hand, e.g. to fill a gap. |
| `plug_and_claim.generate_report` | Writes the report for a finished `month` (`YYYY-MM`). Optional `refresh_data` and `notify`. Call it with a response to get the totals and file paths. |

Example (Developer tools → Actions, with **Return response** ticked):

```yaml
action: plug_and_claim.sync
data:
  config_entry_id: <your claim>
  days_back: 45
```

```yaml
action: plug_and_claim.generate_report
data:
  config_entry_id: <your claim>
  month: "2026-09"
```

## Diagnostics

**Settings → Devices & services → Plug & Claim → ⋮ → Download diagnostics**
gives the settings, per-month totals (with the plug's own month total and the
difference, if you chose a monthly sensor) and the last 31 days of the ledger.

## License

MIT
