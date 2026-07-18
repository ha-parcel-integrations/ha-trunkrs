# TODO — finish the payload mapping

Everything in this integration is built **except** the part that reads fields
out of the Trunkrs response. This file is the exact ask.

## What is already proven

Live-probed against `api.trunkrs.app/v2`:

- **Auth works**: HTTP Basic, username = Trunkrs number, password = receiver
  postcode. One credential pair identifies one parcel.
- **`GET /tracing/verify`** returns 200 for a valid pair, 401 for an invalid
  one — already wired into the config flow and the `track_parcel` service.
- **`GET /tracing/details`** is the tracking payload; it answers 401 without
  valid credentials, so the route and the auth scheme are confirmed.

## What is missing

The **body** of a successful `/tracing/details` response. Capturing it needs a
real Trunkrs parcel, which the maintainer does not have.

Because of that, three things in `coordinator.py` are deliberately left
unmapped rather than guessed (guessing would produce an integration that looks
like it works while silently reporting wrong data):

| Symbol | State | Needed to fill it |
|---|---|---|
| `_STATUS_MAP` | empty dict | the set of raw status values and their meaning |
| `normalize_parcel` | only `carrier`, `barcode`, `url`, `raw` populated | field names for status, delivered, ETA window, sender/receiver, pickup |
| `build_history` | returns `[]` | the key holding the event array + per-event field names |

Everything else — polling, caching, the delivered filter, sorting, all four bus
events, device triggers, sensors, calendar, button, diagnostics — is complete
and covered by tests, and starts working the moment the mapping lands.

## How to get the payload

Two routes, easiest first.

### 1. Diagnostics download (preferred)

Anyone who installs this integration with a real parcel can produce it:

1. Add the parcel to the integration.
2. **Settings → Devices & services → Trunkrs → ⋮ → Download diagnostics**.
3. Read the file and strip anything personal that survived redaction (see the
   warning below), then attach it to an issue.

The untouched payload sits under each parcel's `raw` key.

> **Redaction warning.** `diagnostics.py` redacts a broad list of *commonly
> used* personal key names, but since the real field names are unknown it
> cannot be precise. Always read the file before sharing it publicly.

### 2. curl

```
curl -s -u "<TRUNKRS_NR>:<POSTCODE>" \
  -H 'Accept: application/json' \
  https://api.trunkrs.app/v2/tracing/details | python3 -m json.tool
```

## Also unknown (nice to resolve at the same time)

- **Trunkrs number format.** `config_flow._TRUNKRS_NR_RE` is deliberately
  permissive (`^[A-Z0-9][A-Z0-9-]{3,29}$`) because we have not seen enough real
  numbers; the API's own `verify` call does the real validation. Knowing the
  format would also let us ship an e-mail → `track_parcel` example like the
  other carriers have.
- **Deep link.** `TRACKING_URL` is the plain tracking page; no per-parcel
  deep-link format has been confirmed.
- **Pickup points.** Unknown whether the payload exposes a ServicePoint. If it
  does, add the two pickup sensors the GLS integration has (`en_route_to_...`
  / `awaiting_pickup`) — they were left out on purpose rather than shipped
  permanently reading zero.
- **Countries.** The postcode regex is NL-only. Broaden it if Trunkrs coverage
  elsewhere is confirmed against the same endpoint.

## When the payload arrives

1. Fill `_STATUS_MAP` and the marked sections of `normalize_parcel` /
   `build_history`.
2. Replace the placeholder payload in the tests with the real sample, and add
   the status-mapping assertions the other carriers have.
3. Drop the "preview release" banner from `README.md`, bump to `1.0.0`.
4. Add `trunkrs` to the aggregator's `KNOWN_CARRIERS` +
   `CARRIER_EVENT_PREFIXES` so it slots into the combined sensors and calendar.
