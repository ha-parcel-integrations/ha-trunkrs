# TODO — remaining work

The original blocker (no known response shape) is **closed**: @joerimul
contributed a real delivered-parcel payload in
[#1](https://github.com/ha-parcel-integrations/ha-trunkrs/issues/1), and the
field mapping is implemented and tested. Full sample and mapping table:
`docs/api/tracing_details.md` (local-only, gitignored).

## 1. Status vocabulary — the one real gap

`_STATUS_MAP` in `coordinator.py` holds **one confirmed value**:

| `currentState.stateName` | Canonical |
|---|---|
| `SHIPMENT_DELIVERED` | `delivered` |

Everything else maps to `unknown` and logs a one-shot warning with a
copy-paste issue link. The remaining `SHIPMENT_*` names are deliberately not
guessed — a wrong guess silently reports the wrong status, while `unknown` is
honest and self-collecting.

**To close it:** add each confirmed value as users report it. Still needed
(unconfirmed): pre-announcement, in-transit/sorted, out-for-delivery, and a
failed/not-delivered state — the payload has a `reasonCode` field that is
presumably populated for that last one.

`deliveryAttempts[]` shares the same vocabulary, so every value added improves
both the parcel status and the history timeline at once.

## 2. Smaller open questions

- **Trunkrs number format.** One sample: `419719666` — 9 digits. Not enough to
  write an e-mail regex that will not also match order numbers, so this repo
  ships no `track_parcels_from_email` example yet (GLS and Dragonfly do).
  `config_flow._TRUNKRS_NR_RE` stays permissive because `/tracing/verify` is
  the real check.
- **Tracking deep link.** No confirmed URL format that opens a specific parcel,
  so `url` points at the plain `https://parcel.trunkrs.nl/` page.
- **Pickup points.** The payload has no ServicePoint block and Trunkrs is a
  home-delivery courier, so `pickup` is hard-coded `False` and the GLS
  template's two pickup sensors were left out rather than shipped permanently
  reading zero. Revisit only if a payload turns up showing otherwise.
- **`auditLogs` as a richer history.** Deliberately unused: internal ops text,
  and every entry carries a `userSub` identifying a driver. Only revisit if
  `deliveryAttempts` proves too sparse in practice.
- **Live driver position.** `/tracing/shipment/location` exists, and the
  payload carries `tourDetails.polyline` / `eta`. A possible out-for-delivery
  enrichment — but it is location data about a *driver*, so weigh privacy
  before surfacing any of it.
- **Countries.** The postcode regex is NL-only. Broaden it if Trunkrs coverage
  elsewhere is confirmed against the same endpoint.

## 3. Before a 1.0

- No release has been tagged yet; the integration installs via HACS as a custom
  repository.
- Once the status vocabulary is reasonably complete, drop the "early release"
  banner from `README.md` and cut `1.0.0`.
- Add `trunkrs` to the aggregator's `KNOWN_CARRIERS` +
  `CARRIER_EVENT_PREFIXES` so it slots into the combined sensors, calendar and
  unified event stream.
