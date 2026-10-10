# Plug & Claim

A Home Assistant integration that turns an energy sensor into a monthly
charging claim.

I charge a novated-lease car at home through a smart plug, and every month I
claim the charging cost back. Plug & Claim keeps its own daily kWh ledger for
the plug (separate from the recorder database) and produces the monthly claim
report (XLSX + PDF) for me, saved in Home Assistant's Media folder.

It isn't tied to one brand: any `sensor` with the `energy` device class
(kWh) works, so you can use it with other smart plugs or EV chargers.

> **Status: early development (0.1.0, not released yet).** Right now it sets up
> the claim and its device. The daily ledger, reports, and the rest of the
> entities are still being built.

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

## Entities

| Entity | Notes |
| --- | --- |
| Last sync | Diagnostic. When the ledger was last updated. |

## License

MIT
