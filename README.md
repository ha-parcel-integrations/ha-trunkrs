# Trunkrs Parcel Tracker

[![Release](https://img.shields.io/github/v/release/ha-parcel-integrations/ha-trunkrs.svg)](https://github.com/ha-parcel-integrations/ha-trunkrs/releases)
[![Downloads](https://img.shields.io/github/downloads/ha-parcel-integrations/ha-trunkrs/total.svg)](https://github.com/ha-parcel-integrations/ha-trunkrs/releases)
[![HACS](https://img.shields.io/badge/HACS-Custom-41BDF5.svg)](https://github.com/hacs/integration)
[![License](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)

> 💬 Questions or feedback? Join the discussion on the [Home Assistant community](https://community.home-assistant.io/t/packages-postnl-dhl-nl-dpd-and-gls-parcel-integration/112433/).

A custom Home Assistant integration that tracks your [Trunkrs](https://trunkrs.nl) parcels. Trunkrs has no customer account or inbox: a parcel is identified by its **Trunkrs number together with the delivery postal code**. So, like GLS and Dragonfly, you register each parcel you want to follow.

Part of the [ha-parcel-integrations](https://github.com/ha-parcel-integrations) family: it publishes the same canonical parcel format, statuses and events as the other carrier integrations, so it plugs straight into the [Parcel Aggregator](https://github.com/ha-parcel-integrations/ha-parcel-aggregator) and cross-carrier automations.

> ### ⚠️ Early release — one thing still incomplete
>
> The integration is functional: parcels are validated, polled and mapped —
> sender, receiver, delivery window, history and the delivered state all work.
>
> What is still incomplete is the **status vocabulary**. Only
> `SHIPMENT_DELIVERED`, `SHIPMENT_SORTED`, `SHIPMENT_ACCEPTED_BY_DRIVER` and
> `SHIPMENT_SORTED_AT_SUB_DEPOT` have been observed so far, so a parcel in any
> other state reports **`unknown`** (it is never wrongly marked delivered).
> Each unmapped status logs a one-shot warning with a ready-made issue link —
> see [Troubleshooting](#troubleshooting).

## Contents

- [Features](#features)
- [Requirements](#requirements)
- [Installation](#installation)
- [Configuration](#configuration)
- [Options](#options)
- [Dynamic polling](#dynamic-polling)
- [Removal](#removal)
- [Sensors](#sensors)
- [Parcel status reference](#parcel-status-reference)
- [Events](#events)
- [Services](#services)
- [Examples](#examples)
- [Debugging](#debugging)
- [Troubleshooting](#troubleshooting)
- [Related integrations](#related-integrations)
- [Disclaimer](#disclaimer)
- [Contributing](#contributing)
- [License](#license)

## Features

- Track any number of Trunkrs parcels by Trunkrs number + postal code — no account needed
- Per-parcel sensor with the canonical status (`in_transit` / `out_for_delivery` / `delivered` / …), the carrier's own status text, sender/receiver and a tracking deep-link
- Summary sensors: incoming parcels, next delivery, recently delivered parcels
- Read-only **Deliveries** calendar with the expected delivery windows
- `trunkrs.track_parcel` / `trunkrs.untrack_parcel` services, so a dashboard button can add a parcel — numbers are checked with Trunkrs before they are stored, so a typo is rejected straight away
- Events + device triggers for no-code automations (parcel registered, status changed, delivered, delivery time changed)
- Opt-in per-parcel status history
- Manual refresh button and a diagnostic last-update sensor

## Requirements

- A Trunkrs parcel and its Trunkrs number, plus the delivery postal code (e.g. `1234AB`) — no account needed

## Installation

### HACS (recommended)

1. In HACS, choose the three-dot menu → **Custom repositories**.
2. Add `https://github.com/ha-parcel-integrations/ha-trunkrs` as an **Integration**.
3. Install **Trunkrs** and restart Home Assistant.

### Manual

Copy `custom_components/trunkrs` into your `config/custom_components/` folder and restart Home Assistant.

## Configuration

Add the integration via **Settings → Devices & services → Add integration → Trunkrs**. Enter the **postal code** your parcels are delivered to — this is half of the credential pair Trunkrs uses, and becomes the default for every parcel you add. You can add several hubs, one per postal code (home, work, …).

Then add parcels via the integration's **Configure** dialog, the [`trunkrs.track_parcel`](#services) service, or a [dashboard button](examples/dashboards/add_parcel_card.yaml).

**Tip:** other integrations in the family feed this action straight from your
mailbox (core IMAP integration + a regex on shipping mails). The same pattern
works here, but a ready-made example is not shipped yet: we do not know the
Trunkrs number format well enough to write a regex that will not also match
order numbers. If you know it, please
[tell us](https://github.com/ha-parcel-integrations/ha-trunkrs/issues/new).

## Options

Open **Configure** on the integration entry:

| Section | Option | Default | Description |
|---|---|---|---|
| Parcels | Add / remove | — | Manage the tracked Trunkrs numbers. Changes apply immediately, no restart. |
| Delivered parcels | Filter by / amount | last 7 days | How long delivered parcels stay visible on the delivered sensor. |
| Parcel history | Include status history | off | Adds a `history` attribute per parcel with each status update. |

## Dynamic polling

Instead of polling Trunkrs at the same rate around the clock, the integration
adjusts its own cadence to what your tracked parcels are actually doing:

- **Quiet hours** — no polling between 00:00–06:00 local time, aside from one
  catch-up check at each end of that window (around midnight and around 6
  AM).
- **Hot (every 15 minutes)** — as soon as a tracked parcel is
  `out_for_delivery`, starting an hour before its expected delivery time (or
  immediately if no time is known).
- **Mid (every 45 minutes)** — any other in-progress parcel.
- **Fully stopped** — nothing is tracked, or every tracked parcel has been
  delivered. Adding a parcel back (via the options dialog, the
  `trunkrs.track_parcel` service, or a dashboard button) resumes polling
  immediately.
- A small, fixed per-hub offset is added on top, so not every Trunkrs hub out
  there polls at exactly the same second.

This is not user-configurable — it is the only polling behaviour this
integration has.

## Removal

Standard HA removal applies: **Settings → Devices & services → Trunkrs → ⋮ → Delete**. Nothing is stored on Trunkrs's side.

## Sensors

| Entity | Description |
|---|---|
| `sensor.trunkrs_incoming_parcels` | Number of active tracked parcels, full list under the `parcels` attribute |
| `sensor.trunkrs_parcel_<code>` | One per tracked parcel; state is the canonical status, attributes carry the full normalised parcel |
| `sensor.trunkrs_next_delivery` | Earliest expected delivery moment across all active parcels |
| `sensor.trunkrs_delivered_parcels` | Recently delivered parcels (see the retention option) |
| `sensor.trunkrs_last_successful_update` | Diagnostic: when Trunkrs was last polled successfully |

A delivered parcel moves from its per-parcel sensor to the delivered sensor automatically.

A **Deliveries** calendar entity is also created, showing expected delivery windows for active parcels — read-only, no extra API calls.

A **Refresh** button entity forces an immediate poll, without waiting for the next scheduled interval.

## Parcel status reference

The `status` field is the carrier-agnostic enum shared by the whole integration family. Trunkrs's status vocabulary is still incomplete (see the banner above), so only these are currently reachable from a real parcel:

| Status | Meaning |
|---|---|
| `in_transit` | Sorted at a facility, not yet on today's delivery vehicle |
| `out_for_delivery` | Handed to the delivery driver's route |
| `delivered` | Delivered |
| `unknown` | Not yet scanned, or a status we have not mapped yet |

The carrier's own status name is always available as `raw_status`. `registered`, `at_pickup_point`, `returning` and `problem` exist in the canonical enum but have no confirmed Trunkrs trigger yet.

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

## Services

| Service | Fields | Description |
|---|---|---|
| `trunkrs.track_parcel` | `tracking_code`, `postal_code` (optional, defaults to the hub's) | Start tracking a parcel — validated against Trunkrs before it is stored |
| `trunkrs.untrack_parcel` | `tracking_code` | Stop tracking a parcel |

## Examples

Ready-to-paste automations and dashboard snippets live in
[`examples/`](examples/).

### Community Lovelace cards

Third-party cards that work with this integration's sensors:

- [jonisnet/hki-parcels-card](https://github.com/jonisnet/hki-parcels-card)
- [klaptafel/ha-package-tracker-card](https://github.com/klaptafel/ha-package-tracker-card)

## Debugging

```yaml
logger:
  logs:
    custom_components.trunkrs: debug
```

## Troubleshooting

The main open gap is the **status vocabulary**. Trunkrs reports a parcel's
state as a `SHIPMENT_*` name, and only the four values listed in the banner
above have been seen in the wild. Anything else maps to `unknown` —
deliberately, because guessing a status is worse than admitting we do not
know it.

If one of your parcels shows `unknown`, your log contains a line like:

```
Unrecognised Trunkrs status — help us map it. Open an issue and paste this line: …
  status='SHIPMENT_SOMETHING' → reported as 'unknown'
```

Paste that line into an
[issue](https://github.com/ha-parcel-integrations/ha-trunkrs/issues/new?template=unrecognised_status.yml)
together with what the parcel was actually doing at the time ("on its way",
"at the depot", …) and it gets mapped. No personal data involved — just the
status name.

## Related integrations

This integration is part of [**ha-parcel-integrations**](https://github.com/ha-parcel-integrations) — a family of
parcel-carrier integrations that all publish the same canonical parcel format,
statuses and events.

- [**Parcel Aggregator**](https://github.com/ha-parcel-integrations/ha-parcel-aggregator) rolls every installed carrier
  up into one set of sensors.
- Browse [the organisation](https://github.com/ha-parcel-integrations) for the current list of supported carriers.

## Disclaimer

This integration talks to the same endpoint the Trunkrs consumer tracking page
uses. It is not affiliated with, endorsed by, or supported by Trunkrs. Use at
your own risk — an endpoint change on their side can break it at any time.

## Contributing

Pull requests and issues are welcome. Please open an issue before
submitting a large change.

## License

[MIT](LICENSE)
