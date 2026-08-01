# Working in this repository

Home Assistant custom integration for **Trunkrs** (NL same-day courier) parcel
tracking. Distributed via HACS; not part of HA core. Code-based carrier (no
inbox), built on the GLS/Dragonfly shape. No DTO layer.

## ⚠️ Read TODO.md first (pre-1.0 status vocabulary)

Field mapping is **done** (`docs/api/tracing_details.md`, gitignored). What
remains is the **status vocabulary**: `_STATUS_MAP` holds exactly one confirmed
value, `SHIPMENT_DELIVERED`; everything else reports `unknown` + a one-shot
warning. **Do not add speculative `SHIPMENT_*` entries** — a wrong guess silently
reports the wrong status, while `unknown` is honest and collects real names from
users. Add a value only when confirmed against a real parcel. See TODO.md.

## Shared conventions — fetch when relevant

Suite-wide rules live in
[`.github/CONVENTIONS.md`](https://github.com/ha-parcel-integrations/.github/blob/main/CONVENTIONS.md)
and are **not** repeated here. Don't fetch it every session — fetch it **before**
you act in one of these areas:

| Before you … | Fetch `CONVENTIONS.md` § |
|---|---|
| touch entities, sensors, config/options flow, coordinator, diagnostics, translations | *Home Assistant developer docs* (its table points on to the canonical HA page — don't rely on memory) |
| add/rename a parcel field, a `ParcelStatus`, or a bus event; change first-refresh or unmapped-status logging | *Parcel contract* (this repo implements it; below is only where Trunkrs deviates) |
| ship anything while below 1.0.0 (status vocabulary unconfirmed) | *Pre-1.0 releases* — one-shot WARNINGs for every guessed status |
| consider "fixing" a lint/pattern the skill flags (poll interval, inline client) | *Deliberate skill divergences* |
| commit, bump, tag, release, or write release notes; add a feature without a test | *Workflow / Commits / Versioning / Testing* |

**Suite-wide tripwires, kept inline on purpose:**
- **First refresh in `__init__.py`, before `async_forward_entry_setups`** — from
  a forwarded platform HA can't catch `ConfigEntryNotReady` cleanly.
- **Setup cleanup filters `domain == "sensor"` and excludes
  `non_parcel_unique_ids`** — else it deletes the refresh button / diagnostic
  sensors. Per-parcel sensors are removed via the **entity registry**, never by
  self-removal (which races and leaves ghosts).

## The Trunkrs API

Reverse-engineered from the `parcel.trunkrs.nl` consumer SPA.
- **Base** `https://api.trunkrs.app/v2` (note `.app` — the documented business
  client API on docs.trunkrs.nl is a separate API-key product, **not** this).
- **Auth is HTTP Basic**: username = Trunkrs number, password = receiver postcode.
  No account/token/key — **one credential pair identifies exactly one parcel**.
  Hence tracked parcels are `{trunkrs_nr, postal_code}` pairs and the auth header
  is built per request.
- **Service field is `tracking_code`** (suite standard); the old `trunkrs_nr`
  field is a **deprecated alias** — `_resolve_code` accepts either, logs a
  one-shot deprecation warning, **to be removed**. The *stored* dict key stays
  `trunkrs_nr` (`CONF_TRUNKRS_NR`) — only the service field was renamed; don't
  conflate them.
- **`GET /tracing/verify`** → 200 valid / 401 invalid — validates before storing
  (a capability GLS lacks, so unlike GLS this rejects typos up front).
- **`GET /tracing/details`** → the tracking payload (no query params; identity is
  entirely in the Basic auth header). Mapping in `docs/api/tracing_details.md`:
  - `currentState.stateName` drives `status`; `deliveryAttempts[]` (same
    vocabulary) drives `history`.
  - `timeSlot` carries **two** windows: `low`/`high` (wide promised slot) and
    `from`/`to` (narrow live prediction). We prefer `from`/`to`, fall back to
    `low`/`high` (the narrow one only appears once the tour is planned); both
    cleared once delivered.
  - `barcode` is the number the **user entered**, never `raw["trunkrsNr"]` — it
    drives the sensor `unique_id`; deriving it from the payload would churn the
    entity (losing history) on the first successful poll.
  - `auditLogs[]` is richer than `deliveryAttempts[]` but is internal ops text
    identifying a driver (`userSub`) — deliberately unused.
  - PII to keep redacting: `recipientLocation` (address + lat/long),
    `recipientName`/`senderName`/`merchantName`, the neighbour fields on
    `currentState`, free-text `remark`/`leaveBehindRemark`, and the driver's
    `userSub`/`driverId`/`polyline`.

## Architecture (GLS shape) & deliberate decisions

- **Postcode-keyed hubs**: `unique_id` is the postcode, so several hubs (home,
  work) coexist; `single_config_entry` is intentionally absent. Setup asks only
  the postcode (the default for every added parcel). Parcels live in
  `entry.options[CONF_PARCELS]`, managed three ways (sectioned options flow,
  `track_parcel`/`untrack_parcel` services, a dashboard button). Option changes
  apply **live** via an options update listener that refreshes the coordinator
  (**not** a reload — avoids the config-entry-listener deprecation).
- **`TrunkrsAuthError` is split from `TrunkrsApiError`**: a 401/403 means the
  number/postcode pair is wrong (user-fixable); anything else is an outage
  (retryable). Do not collapse them.
- **`async_verify_parcel` returns `True`/`False`/`None`.** `None` = "couldn't
  reach Trunkrs" → callers **accept the parcel anyway** (an outage must never stop
  a user adding a valid parcel). Only a definite `False` blocks.
- **`delivered` is hard-coded `False`** while the payload is unmapped, so no
  parcel is wrongly filed as completed and vanishes from view.
- **No pickup sensors** (unlike GLS's `en_route_to_parcel_shop` /
  `awaiting_pickup`) — we don't know whether Trunkrs exposes pickup points; two
  permanently-zero sensors are worse than adding them later.
- **Diagnostics is the collection mechanism**, not just debugging: it carries the
  raw payload + a "read this before sharing" note; `TO_REDACT` is deliberately
  broad because the real field names are unknown. The coordinator also logs the
  payload's top-level keys once (`_log_payload_shape`) as a lighter channel.
- Four bus events + device triggers carrying `device_id`, suppressed on first
  refresh, a change **to** DELIVERED fires only `_delivered`. Entities:
  `has_entity_name` + `translation_key`, `icons.json`, translated units (no
  `_attr_name`/`_attr_icon`/`_attr_native_unit_of_measurement`).

## Running tests

```
python -m pytest tests/ --cov=custom_components.trunkrs
```

Coverage must stay **above 95%** (silver `test-coverage` rule). Payload-dependent
paths are tested by injecting *normalised* parcels directly (`test_calendar.py`,
`test_sensor.py`, `test_events.py`) — that verifies finished behaviour despite the
missing mapping.
