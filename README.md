# Trunkrs for Home Assistant

Track your [Trunkrs](https://trunkrs.nl) parcels in Home Assistant.

> ### ⚠️ Preview release — help wanted
>
> The connection to Trunkrs works: parcels are validated and polled
> successfully. What is **not** finished is reading the details out of the
> response — so every parcel currently shows up as **`unknown`**, with no
> status, delivery window or history.
>
> That last step needs **one real Trunkrs parcel payload**, which we do not
> have. If you receive Trunkrs parcels, you can unblock it in two minutes —
> see [How you can help](#how-you-can-help). Everything else is already built,
> so the moment that payload lands this becomes a complete integration.

Trunkrs has no customer account or inbox: a parcel is identified by its
**Trunkrs number together with the delivery postal code**. So, like GLS and
Dragonfly, you register each parcel you want to follow.

## Installation

### HACS (recommended)

1. HACS → ⋮ → **Custom repositories**
2. Add `https://github.com/ha-parcel-integrations/ha-trunkrs`, category
   **Integration**
3. Install **Trunkrs**, then restart Home Assistant

### Manual

Copy `custom_components/trunkrs` into your `config/custom_components/`
directory and restart Home Assistant.

## Setup

1. **Settings → Devices & services → Add integration → Trunkrs**
2. Enter the **postal code** your parcels are delivered to (e.g. `1234AB`).
   This is half of the credential pair Trunkrs uses, and becomes the default
   for every parcel you add.

You can add several hubs — one per postal code (home, work, …).

## Tracking a parcel

Open the integration's **Configure** screen and add a Trunkrs number, or call
the action from an automation or a dashboard button:

```yaml
action: trunkrs.track_parcel
data:
  trunkrs_nr: "TR123456789"
  # postal_code is optional — defaults to the hub's
```

`trunkrs.untrack_parcel` stops tracking it again. Numbers are checked with
Trunkrs before they are stored, so a typo is rejected straight away.

**Tip:** other integrations in the family feed this action straight from your
mailbox (core IMAP integration + a regex on shipping mails). The same pattern
works here, but a ready-made example is not shipped yet: we do not know the
Trunkrs number format well enough to write a regex that will not also match
order numbers. If you know it, please
[tell us](https://github.com/ha-parcel-integrations/ha-trunkrs/issues/new).

## Options

| Option | Default | What it does |
|---|---|---|
| Refresh interval | 30 min | How often Trunkrs is polled (15 / 30 / 60 / 120 / 240) |
| Delivered parcels | 7 days | Keep delivered parcels visible for N days, or the N most recent |
| Parcel history | off | Adds a per-parcel timeline attribute |

## Events

The integration fires these on the Home Assistant event bus. Each also exists
as a **device trigger**, so you can pick them from the automation UI without
writing YAML.

| Event | When |
|---|---|
| `trunkrs_parcel_registered` | A new parcel appears |
| `trunkrs_parcel_status_changed` | A parcel's status changes |
| `trunkrs_parcel_delivered` | A parcel is delivered |
| `trunkrs_parcel_delivery_time_changed` | The expected delivery time moves |

Events are suppressed on the first refresh after start-up, so you are not
flooded with notifications for parcels that already existed.

## Examples

Ready-to-paste automations and dashboard snippets live in
[`examples/`](examples/).

### Community Lovelace cards

Third-party cards that work with this integration's sensors:

- [jonisnet/hki-parcels-card](https://github.com/jonisnet/hki-parcels-card)
- [klaptafel/ha-package-tracker-card](https://github.com/klaptafel/ha-package-tracker-card)

## Related integrations

This integration is part of [**ha-parcel-integrations**](https://github.com/ha-parcel-integrations) — a family of Dutch
parcel-carrier integrations that all publish the same canonical parcel format,
statuses and events.

- [**Parcel Aggregator**](https://github.com/ha-parcel-integrations/ha-parcel-aggregator) rolls every installed carrier
  up into one set of sensors.
- Browse [the organisation](https://github.com/ha-parcel-integrations) for the current list of supported carriers.

## How you can help

The one missing piece is a real `GET /tracing/details` response body. To share
one:

1. Set up the integration and add a real Trunkrs parcel.
2. Go to **Settings → Devices & services → Trunkrs → ⋮ → Download diagnostics**.
3. **Open the file and read it.** Redaction is best-effort — because we do not
   know the field names yet, it can only redact commonly used ones. Remove
   anything personal (names, address, contact details) that survived.
4. Attach it to a
   [new issue](https://github.com/ha-parcel-integrations/ha-trunkrs/issues/new).

The payload is preserved untouched under each parcel's `raw` key — that is the
part we need. With it, the status mapping, delivery window, history and
calendar all start working.

## Disclaimer

This integration talks to the same endpoint the Trunkrs consumer tracking page
uses. It is not affiliated with, endorsed by, or supported by Trunkrs. Use at
your own risk — an endpoint change on their side can break it at any time.
