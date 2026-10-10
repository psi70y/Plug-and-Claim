# Plug & Claim

A Home Assistant integration that turns an energy sensor into a monthly
charging claim.

I charge a novated-lease car at home through a smart plug, and every month I
claim the charging cost back. Plug & Claim keeps its own daily kWh ledger for
the plug (separate from the recorder database) and produces the monthly claim
report (XLSX + PDF) for me, saved in Home Assistant's Media folder.

It isn't tied to one brand: any `sensor` with the `energy` device class
(kWh) works, so you can use it with other smart plugs or EV chargers.

> **Status: early development (0.1.0, not released yet).** The daily ledger
> works; the reports and the rest of the entities are still being built.

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

## Entities

| Entity | Notes |
| --- | --- |
| Last sync | Diagnostic. When the ledger was last updated. |
| Ledger days | Diagnostic. Number of days in the ledger; attributes show the capture mode and first/last day. |

## Actions

| Action | What it does |
| --- | --- |
| `plug_and_claim.sync` | Re-reads the last `days_back` days (default 14) from the recorder. Call it with a response to see which days changed. |
| `plug_and_claim.set_day` | Sets one past day's kWh by hand, e.g. to fill a gap. |

Example (Developer tools → Actions, with **Return response** ticked):

```yaml
action: plug_and_claim.sync
data:
  config_entry_id: <your claim>
  days_back: 45
```

## Diagnostics

**Settings → Devices & services → Plug & Claim → ⋮ → Download diagnostics**
gives the settings, per-month totals (with the plug's own month total and the
difference, if you chose a monthly sensor) and the last 31 days of the ledger.

## License

MIT
